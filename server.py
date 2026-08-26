"""
שרת Webhook רב-ערוצי (omnichannel) שמקבל הודעות נכנסות ומעדכן את ה-CRM.
בשלב זה השרת רץ מקומית; חיבור אמיתי לאינטרנט (ngrok) הוא שלב נפרד.

תמיכה בערוצים כרגע:
- whatsapp: פורמט Twilio האמיתי (form-encoded, שדות From/Body) - עובד בפועל, כולל
  מענה אוטומטי חזרה ללקוח (ראו process_message_with_reply ב-extract.py). זו תגובה
  בתוך חלון השיחה שהלקוח פתח, לא הודעה יזומה, ולכן אינה דורשת תבנית מאושרת מול מטא.
- instagram / facebook: פורמט JSON גנרי (source/contact_id/text) - PLACEHOLDER בלבד.
  חיבור אמיתי ל-Instagram/Facebook דורש אפליקציית Meta מאושרת ופענוח הפורמט
  האמיתי של Meta Graph API webhooks (שונה לגמרי מ-Twilio) - עוד לא ממומש כאן. אין
  עדיין מענה אוטומטי לערוצים אלה.

בהרצה ישירה (python server.py) השרת גם מפעיל את scheduler.py (Background Scheduler
Worker) כ-thread רקע שסורק תקופתית לידים קרים לפי חוקיות ורטיקל (contacts.csv) ומריץ
עליהם reactivate.py. ⚠️ ברירת המחדל היא dry-run בלבד - ראו האזהרה המפורטת בראש
scheduler.py לפני שמדליקים שליחה אוטומטית אמיתית (SCHEDULER_AUTO_SEND=true).
בפריסת production דרך Gunicorn (ראו Procfile) ה-thread הזה לא רץ בכלל (Gunicorn לא
מפעיל את if __name__=="__main__") - במקומו, scheduler_worker.py רץ כ-process נפרד
לגמרי (`clock`), כדי למנוע הכפלה בין כמה workers של Gunicorn.

--- הקשחה ל-production (2026-08-25) ---
- **משתני סביבה:** PORT/HOST/FLASK_DEBUG/LOG_LEVEL/VERIFY_TWILIO_SIGNATURE נקראים
  מ-.env עם ברירות מחדל בטוחות (ראו הבלוק "הגדרות מ-.env" למטה). FLASK_DEBUG חייב
  להיות false/לא-מוגדר ב-production אמיתי - מצב debug של Flask חושף Werkzeug
  debugger שמאפשר הרצת קוד שרירותי למי שמגיע לשגיאה לא מטופלת.
- **לוגים:** logging סטנדרטי של Python - קונסולה + קובץ מתגלגל (RotatingFileHandler)
  ל-server_error.log, בנפרד מ-chat_history.txt (שנשאר "לוג עסקי" של הודעות, לא לוג
  שגיאות טכני).
- **אימות Twilio:** כל בקשה ל-/webhook בפורמט Twilio (form-encoded, לא JSON) מאומתת
  מול חתימת X-Twilio-Signature (ראו _verify_twilio_request) - בלי זה, כל אחד שמנחש
  את כתובת ה-webhook יכול לשלוח בקשות מזויפות שיתפרשו כהודעות לקוח אמיתיות. השרת
  רץ מאחורי ngrok/reverse-proxy, ולכן יש ProxyFix כדי ש-request.url ישקף את הכתובת
  הציבורית האמיתית שטוויליו חתם עליה (אחרת האימות תמיד ייכשל בטעות).
"""

import csv
import io
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from openpyxl import load_workbook
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה
# חייב לרוץ לפני ה-import-ים הבאים - db/reactivate/scheduler/extract קוראים os.environ בזמן טעינה

try:
    # כל הייבואים האלה - כולל db/reactivate/scheduler, לא רק extract/whatsapp_send
    # ישירות - תלויים ב-extract.py, שדורש ANTHROPIC_API_KEY בזמן טעינה (os.environ[...]).
    # לכן כל השרשרת חייבת להיות בתוך אותו try/except - אחרת ה-KeyError קורה כבר
    # ב-"import reactivate" למשל, לפני שמגיעים בכלל לבלוק שתופס אותו.
    import db
    import reactivate
    import scheduler
    from extract import (
        DEFAULT_TENANT_ID,
        import_lead,
        load_customers,
        log_manual_reply,
        process_message,
        process_message_with_reply,
        update_lead_status,
    )
    from whatsapp_send import TWILIO_AUTH_TOKEN, _to_e164, send_whatsapp_message
except KeyError as exc:
    # משתנה סביבה קריטי חסר (כרגע רק ANTHROPIC_API_KEY נדרש קשיח - os.environ[...] ולא
    # os.environ.get(...)) - נכשלים מיד עם הודעה ברורה, לא עם traceback גולמי שקשה
    # להבין ממנו מה בדיוק חסר. ב-Render: זה בדיוק מה שקורה אם שוכחים להגדיר משתנה
    # סביבה בדשבורד לפני ה-deploy הראשון - "Exit status 1" בלוג הוא הסימפטום החיצוני.
    print(f"שגיאת הגדרה: משתנה סביבה חסר - {exc}. בדקו את משתני הסביבה (.env מקומית / Render Environment).", file=sys.stderr)
    sys.exit(1)

# ---- הגדרות מ-.env (עם ברירות מחדל בטוחות) ----
PORT = int(os.environ.get("PORT", "5000"))
HOST = os.environ.get("HOST", "127.0.0.1")  # production מאחורי container/load-balancer: HOST=0.0.0.0
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "false").strip().lower() == "true"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
VERIFY_TWILIO_SIGNATURE = os.environ.get("VERIFY_TWILIO_SIGNATURE", "true").strip().lower() == "true"

BASE_DIR = Path(__file__).parent
from paths import DATA_DIR  # noqa: E402 - chat_history.txt (state) נשמר כאן; server_error.log/index.html נשארים ב-BASE_DIR

# ---- לוגים: קונסולה + קובץ מתגלגל (לא גדל לאינסוף) ----
logger = logging.getLogger("whatsapp_crm")
logger.setLevel(LOG_LEVEL)
_log_formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
logger.addHandler(_console_handler)

_file_handler = RotatingFileHandler(
    BASE_DIR / "server_error.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
)
_file_handler.setLevel(logging.WARNING)  # קובץ הלוג מתמקד בשגיאות/אזהרות - לא רעש כללי
_file_handler.setFormatter(_log_formatter)
logger.addHandler(_file_handler)

TRIAL_RESTRICTION_STATUSES = {400, 422}


def _is_trial_restriction(exc: Exception) -> bool:
    """מזהה שגיאת חסימה אופיינית לחשבון Twilio מסוג Trial (למשל נמען לא מאומת, או
    פרמטרים חסומים ל-Trial) - בניגוד לשגיאה אמיתית אחרת (פרטי חיבור שגויים וכו')."""
    return (
        isinstance(exc, TwilioRestException)
        and exc.status in TRIAL_RESTRICTION_STATUSES
        and "trial" in str(exc).lower()
    )


app = Flask(__name__)
# מאחורי ngrok/reverse-proxy: משקף את ה-scheme/host/port הציבוריים האמיתיים מתוך
# X-Forwarded-* במקום 127.0.0.1 המקומי - קריטי גם לאימות חתימת Twilio (_verify_twilio_request)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

_twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None


def _verify_twilio_request(req) -> bool:
    """מאמת שבקשת ה-webhook הגיעה באמת מ-Twilio, לפי חתימת X-Twilio-Signature (HMAC
    עם ה-Auth Token) - https://www.twilio.com/docs/usage/webhooks/webhooks-security.
    בלי זה, כל אחד שמנחש את כתובת ה-webhook יכול לשלוח בקשות מזויפות."""
    if _twilio_validator is None:
        logger.warning("אימות Twilio מבוקש אבל TWILIO_AUTH_TOKEN לא מוגדר ב-.env - דוחה את הבקשה")
        return False
    signature = req.headers.get("X-Twilio-Signature", "")
    return _twilio_validator.validate(req.url, req.form, signature)


@app.errorhandler(Exception)
def handle_uncaught_exception(exc):
    logger.error("שגיאה לא מטופלת בבקשה ל-%s: %s", request.path, exc, exc_info=True)
    return jsonify({"error": "שגיאת שרת פנימית"}), 500


SUPPORTED_SOURCES = {"whatsapp", "instagram", "facebook"}
CHAT_HISTORY_FILE = DATA_DIR / "chat_history.txt"


def parse_incoming() -> tuple[str, str, str]:
    """מנרמל הודעה נכנסת מכל ערוץ לפורמט אחיד: (contact_id, message_text, source)."""
    source = request.args.get("source", "whatsapp")
    if source not in SUPPORTED_SOURCES:
        source = "whatsapp"

    if request.is_json:
        # PLACEHOLDER: פורמט גנרי, לא הפורמט האמיתי של Meta Graph API
        data = request.get_json(silent=True) or {}
        source = data.get("source", source)
        contact_id = str(data.get("contact_id", ""))
        message_text = data.get("text", "")
    else:
        # Twilio (WhatsApp) שולח form-encoded עם השדות From ו-Body
        contact_id = request.form.get("From", "").replace("whatsapp:", "")
        message_text = request.form.get("Body", "")

    return contact_id, message_text, source


def _log_incoming_message(tenant_id: str, source: str, contact_id: str, message_text: str) -> None:
    """מוסיף רשומה מסודרת של הודעה נכנסת מליד ל-chat_history.txt (לוג טקסטואלי,
    בנוסף לרישום המובנה בהיסטוריית הכרטיס בתוך customers.json)."""
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] tenant={tenant_id} source={source} from={contact_id}: {message_text}\n"
    with CHAT_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/leads")
def api_leads():
    """רשימת כל הלידים מ-customers.json (מקור האמת) - לשימוש הדשבורד ב-index.html.
    ?include_last_message=1 (משמש את תצוגת ה-Unified Inbox): מוסיף לכל ליד את ההודעה
    האחרונה מטבלת messages (תצוגה מקדימה + מיון לפי פעילות) - כבוי כברירת מחדל כדי
    לא להאט את טעינת הטבלה הרגילה עם שאילתת DB נוספת לכל שורה."""
    include_last_message = request.args.get("include_last_message") == "1"

    leads = []
    for key, card in load_customers().items():
        tenant_id, _, key_phone = key.partition("::")
        phone = card.get("phone", key_phone)
        resolved_tenant_id = card.get("tenant_id", tenant_id)
        lead = {
            "tenant_id": resolved_tenant_id,
            "phone": phone,
            "customer_name": card.get("customer_name"),
            "business_name": card.get("business_name"),
            "location": card.get("location"),
            "source_channel": card.get("source_channel"),
            "import_source": card.get("import_source"),
            "lead_status": card.get("lead_status"),
        }
        if include_last_message:
            last = db.get_last_message(phone, tenant_id=resolved_tenant_id)
            lead["last_message"] = last["message"] if last else None
            lead["last_message_at"] = last["timestamp"] if last else None
            lead["last_message_direction"] = last["direction"] if last else None
        leads.append(lead)
    return jsonify(leads)


VALID_LEAD_STATUSES = {"new", "contacted", "hot", "not_relevant"}


@app.route("/api/leads/status", methods=["POST"])
def api_update_lead_status():
    """מעדכן lead_status ידנית מהדשבורד (שורת הטבלה או פאנל ההיסטוריה). זו רק עדכון
    סטטוס ב-customers.json - לא הודעה, ולכן לא נרשם בטבלת messages."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    status = data.get("status")

    if not phone or status not in VALID_LEAD_STATUSES:
        return jsonify({"error": "טלפון או סטטוס לא תקינים"}), 400

    card = update_lead_status(phone, status, tenant_id=tenant_id)
    return jsonify({"ok": True, "card": card})


# ייבוא לידים מ-CSV/Excel - מיפוי אוטומטי של עמודות (בעברית או באנגלית) לשדות הסטנדרטיים
ALLOWED_IMPORT_EXTENSIONS = {".csv", ".xlsx"}
IMPORT_FIELD_ALIASES = {
    "customer_name": {"name", "customer_name", "full name", "שם", "שם לקוח", "שם מלא"},
    "phone": {"phone", "phone_number", "mobile", "טלפון", "מספר טלפון", "נייד", "מס' טלפון"},
    "business_name": {"business", "business_name", "company", "עסק", "שם עסק", "חברה"},
    "import_source": {"source", "lead_source", "מקור", "מקור ליד", "ערוץ מקור"},
    "lead_status": {"status", "lead_status", "סטטוס"},
}


def _normalize_header(h) -> str:
    return str(h or "").strip().lower()


def _map_import_row(raw_row: dict) -> dict:
    """ממפה שורת CSV/Excel גולמית (עמודות בכל שם סביר, עברית או אנגלית) לשדות התקניים."""
    normalized = {_normalize_header(k): v for k, v in raw_row.items()}
    mapped = {}
    for field, aliases in IMPORT_FIELD_ALIASES.items():
        for alias in aliases:
            value = normalized.get(_normalize_header(alias))
            if value not in (None, ""):
                mapped[field] = str(value).strip()
                break
    return mapped


def _parse_import_csv(file_stream) -> list[dict]:
    text = file_stream.read().decode("utf-8-sig")  # utf-8-sig סופג BOM מ-CSV שיוצא מ-Excel
    return list(csv.DictReader(io.StringIO(text)))


def _parse_import_excel(file_stream) -> list[dict]:
    workbook = load_workbook(file_stream, read_only=True, data_only=True)
    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
    except StopIteration:
        return []
    rows = []
    for row in rows_iter:
        if all(cell is None for cell in row):
            continue
        rows.append({headers[i]: row[i] for i in range(min(len(headers), len(row)))})
    return rows


@app.route("/api/leads/import", methods=["POST"])
def api_import_leads():
    """מייבא לידים מקובץ CSV/XLSX שהועלה מהדשבורד. ממפה אוטומטית שם/טלפון/עסק/מקור/
    סטטוס (בעברית או באנגלית), ומונע כפילויות ע"י upsert לפי מספר טלפון מנורמל
    (E.164) + tenant_id - ראו import_lead ב-extract.py."""
    if "file" not in request.files:
        return jsonify({"error": "לא צורף קובץ (שדה 'file')"}), 400

    upload = request.files["file"]
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        return jsonify({"error": f"סוג קובץ לא נתמך: '{ext or 'ללא סיומת'}'. נתמכים: CSV, XLSX"}), 400

    tenant_id = request.form.get("tenant_id") or DEFAULT_TENANT_ID

    try:
        raw_rows = _parse_import_csv(upload.stream) if ext == ".csv" else _parse_import_excel(upload.stream)
    except Exception as exc:
        return jsonify({"error": f"שגיאה בקריאת הקובץ: {exc}"}), 400

    imported = updated = 0
    skipped = []
    for row_num, raw_row in enumerate(raw_rows, start=2):  # שורה 1 = כותרות
        mapped = _map_import_row(raw_row)
        phone = mapped.get("phone")
        if not phone:
            skipped.append({"row": row_num, "reason": "אין מספר טלפון"})
            continue

        status = mapped.get("lead_status", "").lower()
        if status not in VALID_LEAD_STATUSES:
            status = None  # סטטוס לא מוכר - מתעלמים ממנו, לא נכשלים על כל השורה

        _card, is_new = import_lead(
            _to_e164(phone),
            tenant_id=tenant_id,
            customer_name=mapped.get("customer_name"),
            business_name=mapped.get("business_name"),
            lead_status=status,
            import_source=mapped.get("import_source"),
        )
        if is_new:
            imported += 1
        else:
            updated += 1

    return jsonify({
        "ok": True,
        "total_rows": len(raw_rows),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
    })


@app.route("/api/messages")
def api_messages():
    """היסטוריית ההודעות של ליד מסוים מטבלת messages ב-crm_data.db - לשימוש הדשבורד.
    since (אופציונלי): מחזיר רק הודעות חדשות יותר - משמש את ה-polling של חלון הצ'אט
    החי (ראו index.html) כדי לרענן בלי לשלוף את כל ההיסטוריה בכל בדיקה."""
    phone = request.args.get("phone", "")
    tenant_id = request.args.get("tenant_id", DEFAULT_TENANT_ID)
    since = request.args.get("since") or None
    if not phone:
        return Response(status=400)
    return jsonify(db.get_messages(phone, tenant_id=tenant_id, since=since))


@app.route("/api/reactivate", methods=["POST"])
def api_reactivate():
    """מפעיל את קמפיין חימום הלידים הקרים (reactivate.py) מתוך הדשבורד. ברירת המחדל
    היא preview בלבד (send=False, כברירת המחדל של reactivate.py) - לא נשלח שום דבר.
    שליחה בפועל דורשת send:true מפורש בגוף הבקשה; זה האישור האנושי הנדרש (קליק מפורש
    בממשק אחרי צפייה בתצוגה המקדימה) - ראו מדיניות הבטיחות ב-CLAUDE.md."""
    data = request.get_json(silent=True) or {}
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    send = bool(data.get("send", False))

    results = reactivate.run_reactivation_campaign(tenant_id=tenant_id, send=send)
    return jsonify({"send": send, "count": len(results), "results": results})


@app.route("/api/scheduler/status")
def api_scheduler_status():
    """מצב מנוע התזמון האוטומטי (scheduler.py) - קריאה בלבד. חשוב לבדוק את auto_send:
    כשהוא False (ברירת המחדל) המנוע רץ במצב dry-run בלבד ולא שולח הודעות אמיתיות."""
    return jsonify(scheduler.status())


@app.route("/api/scheduler/runs")
def api_scheduler_runs():
    """היסטוריית מחזורי הסריקה האחרונים של מנוע התזמון, מטבלת scheduler_runs."""
    return jsonify(db.get_scheduler_runs())


@app.route("/api/scheduler/run-now", methods=["POST"])
def api_scheduler_run_now():
    """מפעיל מחזור סריקה אחד מיידית, בלי לחכות למרווח התזמון (שימושי לבדיקה/דמו).
    מכבד את אותה הגדרת SCHEDULER_AUTO_SEND כמו הרצה רגילה - לא שולח הודעות אמיתיות
    אם auto_send=False."""
    data = request.get_json(silent=True) or {}
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    results = scheduler.run_scan(tenant_id=tenant_id)
    return jsonify({"auto_send": scheduler.AUTO_SEND, "count": len(results), "results": results})


@app.route("/api/messages/send", methods=["POST"])
def api_send_message():
    """שליחת הודעה ידנית בפועל דרך Twilio, מתוך הדשבורד. כל קריאה כאן מגיעה מקליק
    מפורש של נציג אנושי על "שלח" - זה האישור האנושי הנדרש למדיניות השליחה בפרויקט
    (ראו CLAUDE.md). שים לב: הודעה ליד שלא כתב הודעה ב-24 השעות האחרונות נחשבת
    "business-initiated conversation" ועלולה לדרוש הודעת-תבנית מאושרת מול מטא."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    message = (data.get("message") or "").strip()

    if not phone or not message:
        return jsonify({"error": "חסר טלפון או תוכן הודעה"}), 400

    simulated = False
    sid = None
    try:
        sid = send_whatsapp_message(phone, message)
    except Exception as exc:
        if not _is_trial_restriction(exc):
            return jsonify({"error": str(exc)}), 502
        # חשבון Twilio מסוג Trial חסם את השליחה בפועל (למשל נמען לא מאומת) - זו לא
        # שגיאת קוד; רושמים את ההודעה כ"מדומה" (simulated) כדי לאפשר לבדוק את חלון
        # השיחות והזרימה בדשבורד בלי להיחסם. הלקוח לא קיבל את ההודעה בפועל.
        simulated = True

    card = log_manual_reply(phone, message, tenant_id=tenant_id, simulated=simulated)
    db.log_message(phone, message, direction="out", tenant_id=tenant_id, channel="whatsapp", simulated=simulated)

    response = {"ok": True, "sid": sid, "card": card, "simulated": simulated}
    if simulated:
        response["warning"] = (
            "חשבון Twilio מסוג Trial חסם את השליחה בפועל (נמען לא מאומת) - "
            "ההודעה נרשמה כסימולציה לצורך בדיקה, אך לא נשלחה ללקוח."
        )
    return jsonify(response)


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<tenant_id>", methods=["POST"])
def webhook(tenant_id: str = DEFAULT_TENANT_ID):
    # כל עסק (tenant) מקבל כתובת webhook משלו עם ה-tenant_id שלו בנתיב, למשל:
    # https://<ngrok-url>/webhook/business_a - כך שהנתונים של כל עסק מבודדים זה מזה.

    # אימות Twilio חל רק על בקשות בפורמט Twilio האמיתי (form-encoded) - לא על
    # ה-JSON הגנרי של instagram/facebook (PLACEHOLDER, לא תעבורת Twilio אמיתית).
    if VERIFY_TWILIO_SIGNATURE and not request.is_json:
        if not _verify_twilio_request(request):
            logger.warning(
                "בקשת webhook נדחתה - חתימת X-Twilio-Signature לא תקינה/חסרה (מ-%s, tenant=%s)",
                request.remote_addr, tenant_id,
            )
            return Response(status=403)

    contact_id, message_text, source = parse_incoming()

    if not contact_id or not message_text:
        return Response(status=400)

    _log_incoming_message(tenant_id, source, contact_id, message_text)
    db.log_message(contact_id, message_text, direction="in", tenant_id=tenant_id, channel=source)

    reply_text = None
    try:
        if source == "whatsapp":
            # ל-whatsapp מייצרים גם תשובה אוטומטית ושולחים אותה חזרה ללקוח דרך TwiML.
            # זו תגובה בתוך חלון השיחה שהלקוח פתח - לא הודעה יזומה - ולכן אינה דורשת
            # הודעת-תבנית מאושרת מול מטא.
            card, reply_text = process_message_with_reply(
                contact_id, message_text, tenant_id=tenant_id, source_channel=source
            )
            db.log_message(contact_id, reply_text, direction="out", tenant_id=tenant_id, channel=source)
        else:
            card = process_message(contact_id, message_text, tenant_id=tenant_id, source_channel=source)
        logger.info("[tenant=%s] [%s] עודכן כרטיס לקוח: %s", tenant_id, source, card)
    except Exception as exc:
        logger.error("שגיאה בעיבוד הודעה מ-%s (tenant=%s): %s", source, tenant_id, exc, exc_info=True)

    if source == "whatsapp":
        # Twilio מצפה לתגובת TwiML; <Message> בתוכה נשלח כתשובה בוואטסאפ ללקוח
        body = f"<Message>{escape(reply_text)}</Message>" if reply_text else ""
        twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"
        return Response(twiml, mimetype="text/xml")

    return Response(status=200)


if __name__ == "__main__":
    logger.info(
        "מפעיל שרת על %s:%s (debug=%s, verify_twilio_signature=%s, log_level=%s)",
        HOST, PORT, FLASK_DEBUG, VERIFY_TWILIO_SIGNATURE, LOG_LEVEL,
    )
    scheduler.start()
    app.run(host=HOST, port=PORT, debug=FLASK_DEBUG, use_reloader=False)
