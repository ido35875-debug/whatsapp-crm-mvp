"""
שלב 1 של ה-MVP: "המוח" שמחלץ פרטי לקוח מהודעת טקסט.
בשלב הזה עוד אין חיבור אמיתי לוואטסאפ - אנחנו מדמים הודעה נכנסת כמחרוזת טקסט,
ובודקים שה-AI באמת יודע לחלץ ממנה שם לקוח, שם עסק ומיקום.
"""

import copy
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# נתיב מפורש (לא הזיהוי-אוטומטי המבוסס-stack של load_dotenv) - עמיד לחלוטין לכל דרך
# הרצה/פריסה (gunicorn, אריזה בענן וכו'), בלי תלות ב-cwd או בשרשרת הקריאות בזמן import
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

import ai_json  # noqa: E402 - עוזר משותף לפענוח JSON מתגובת Claude (מגן מפני עטיפת ```json, ראו שם)
from paths import DATA_DIR  # noqa: E402 - חייב אחרי load_dotenv (DATA_DIR עצמו נקרא מ-os.environ)
from whatsapp_send import _to_e164  # noqa: E402 - לשימוש resolve_existing_phone
import prompts  # noqa: E402 - הקבועים *_PROMPT למטה משמשים כברירת מחדל ל-prompts.get_prompt
import db  # noqa: E402 - לשימוש rekey_lead/delete_lead (rekey/מחיקה של messages/calls/calendar_tasks) - db.py לא תלוי ב-extract.py, אין circular import

CUSTOMERS_FILE = DATA_DIR / "customers.json"
DEFAULT_TENANT_ID = "default"  # מזהה העסק כשאין tenant_id מפורש (תאימות לאחור)

# מקור האמת היחיד לערכי lead_status תקינים - הועבר לכאן מ-server.py כדי
# ש-import_leads.py (סקריפט CLI + /api/leads/import) יוכל להשתמש באותו set
# בלי ליצור תלות מעגלית ב-server.py (טעינת server.py כוללת יצירת Flask app
# מלאה - כבד ולא מתאים לייבוא ממודול CLI עצמאי). server.py ממשיך להשתמש
# באותו שם משתנה בדיוק (from extract import VALID_LEAD_STATUSES) - אין שינוי
# התנהגות, רק מיקום הגדרה אחד.
VALID_LEAD_STATUSES = {
    "new", "contacted", "follow_up", "hot", "not_relevant", "reactivation", "human_handling", "no_answer",
}

EXTRACTION_PROMPT = """\
אתה עוזר שמחלץ פרטי לקוח מהודעת וואטסאפ בעברית או באנגלית.
מתוך ההודעה הבאה, חלץ חמישה שדות: customer_name (שם הלקוח), business_name (שם העסק שלו, אם צוין),
location (עיר/אזור, אם צוין), property_type (סוג נכס מבוקש - למשל דירה/בית/מגרש/משרד, רק אם רלוונטי לנדל"ן),
budget (תקציב/טווח מחיר שהוזכר, כמחרוזת כפי שנאמרה - למשל "עד 2 מיליון").
אם שדה מסוים לא מופיע בהודעה, החזר עבורו null.
החזר אך ורק JSON תקין בפורמט הבא, בלי שום טקסט נוסף:
{"customer_name": "...", "business_name": "...", "location": "...", "property_type": "...", "budget": "..."}

הודעה:
"""


def extract_customer_info(message_text: str) -> dict:
    """{} אם הניתוח נכשל (JSON לא-תקין אחרי ניקוי code fence, שגיאת רשת/ספק
    וכו') - במקום להעלות חריגה ולהפיל את עיבוד ההודעה הנכנסת (ראו process_
    message למטה, שקוראת לפונקציה הזו בלי try/except משלה - עד כאן זה היה
    פער אמיתי: תגובת AI עטופה ב-```json (נמדד בפועל: ~1/3-2/3 מהקריאות
    לפרומפט דומה, ראו ai_json.py) הייתה גורמת ל-JSONDecodeError שמעולם לא
    נתפס, מפילה את עיבוד ההודעה כולו ומונעת יצירת/עדכון כרטיס הלקוח)."""
    prompt_template = prompts.get_prompt("customer_extraction", EXTRACTION_PROMPT)
    try:
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=500,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt_template + message_text}],
        )
        raw_text = next(block.text for block in response.content if block.type == "text").strip()
        return ai_json.parse_json_response(raw_text)
    except Exception as exc:
        logging.getLogger("whatsapp_crm").warning("חילוץ פרטי לקוח מהודעה נכשל: %s", exc)
        return {}


def _use_database() -> bool:
    return bool(db.DATABASE_URL)


# ---- שכבת האחסון של הכרטיסים ----
# כל כתיבה שוטפת היא **פר-כרטיס ואטומית** (update_card/_move_card/_delete_card) -
# לא load הכל → שינוי כרטיס אחד → save הכל. בפרודקשן gunicorn מריץ 2 workers +
# threads ברקע (תור webhook, תשובות מושהות, scheduler), ומחזור read-modify-write
# על כל המילון דרס עדכונים מקביליים לכרטיסים *אחרים* (lost update) ומחק כרטיס
# שנוצר בינתיים (replace_customers_rows מחק כל שורה שחסרה במילון).
#
# מצב JSON מקומי (DATABASE_URL ריק): _json_lock (process-wide) עוטף load→mutate→
# write, והכתיבה אטומית (קובץ זמני באותה תיקייה + os.replace) - קורא לעולם לא
# רואה קובץ קטוע. ⚠️ הגנה בין *processes* שונים לא מובטחת במצב המקומי (אין נעילת
# קובץ) - ההרצה המקומית היא process יחיד; בפרודקשן (Postgres) הנעילה היא FOR UPDATE.
_json_lock = threading.RLock()


def _read_customers_file() -> dict:
    if CUSTOMERS_FILE.exists():
        return json.loads(CUSTOMERS_FILE.read_text(encoding="utf-8"))
    return {}


def _write_customers_file_atomic(customers: dict) -> None:
    """כתיבה לקובץ זמני באותה תיקייה ואז os.replace (אטומי באותה מערכת קבצים).
    ב-Windows os.replace עלול להיכשל ב-PermissionError אם תהליך אחר מחזיק את
    הקובץ פתוח לקריאה ברגע הזה (למשל עורך/סקריפט חיצוני) - כמה ניסיונות חוזרים
    קצרים לפני שמוותרים."""
    target = CUSTOMERS_FILE
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(customers, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp_name, target)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_customers() -> dict:
    """כש-DATABASE_URL מוגדר - הכרטיסים נטענים מהמסד (הקבוע בענן). אחרת מקובץ
    customers.json המקומי. קריאה בלבד - **לא** לשנות את המילון ולשמור אותו
    בחזרה; לכתיבה השתמשו ב-update_card (או בפונקציות update_lead_* למטה)."""
    if _use_database():
        return db.load_customers_rows()
    with _json_lock:
        return _read_customers_file()


def update_card(key: str, mutate_fn, default: dict | None = None) -> dict:
    """עדכון אטומי של כרטיס **אחד** - הפרימיטיב היחיד לכתיבה שוטפת לכרטיסים.

    mutate_fn(card, is_new) משנה את card במקום (ערך ההחזרה שלו מתעלם).
    - הכרטיס קיים: card = הכרטיס הנוכחי (טרי, נקרא בתוך הנעילה), is_new=False.
    - הכרטיס חסר ו-default לא None: card = עותק של default, is_new=True (יצירה).
    - הכרטיס חסר ו-default None: KeyError(key), שום דבר לא נכתב.
    חריגה מתוך mutate_fn = שום שינוי לא נכתב. מחזיר את הכרטיס אחרי השינוי.

    Postgres: טרנזקציה אחת, SELECT ... FOR UPDATE על השורה הזו בלבד (ראו
    db.update_customer_row). JSON מקומי: _json_lock + כתיבה אטומית.
    mutate_fn רץ בתוך הנעילה - לא לבצע בו קריאות רשת/AI/שליחה (רק שינוי ה-dict)."""

    def _apply(current: dict | None) -> dict:
        if current is None:
            if default is None:
                raise KeyError(key)
            card, is_new = copy.deepcopy(default), True
        else:
            card, is_new = current, False
        mutate_fn(card, is_new)
        return card

    if _use_database():
        return db.update_customer_row(key, _apply, create=default is not None)
    with _json_lock:
        customers = _read_customers_file()
        card = _apply(customers.get(key))
        customers[key] = card
        _write_customers_file_atomic(customers)
        return card


def _move_card(old_key: str, new_key: str, mutate_fn) -> dict:
    """rekey אטומי של כרטיס בודד (old_key→new_key). KeyError אם old_key חסר,
    db.CustomerKeyConflict אם new_key כבר קיים. לא נוגע בכרטיסים אחרים."""
    if _use_database():
        return db.move_customer_row(old_key, new_key, mutate_fn)
    with _json_lock:
        customers = _read_customers_file()
        if old_key not in customers:
            raise KeyError(old_key)
        if new_key in customers:
            raise db.CustomerKeyConflict(new_key)
        card = mutate_fn(customers[old_key])
        del customers[old_key]
        customers[new_key] = card
        _write_customers_file_atomic(customers)
        return card


def _delete_card(key: str) -> bool:
    """מחיקת כרטיס בודד - **רק** עבור delete_lead (המחיקה המפורשת הקיימת)."""
    if _use_database():
        return db.delete_customer_row(key)
    with _json_lock:
        customers = _read_customers_file()
        if key not in customers:
            return False
        del customers[key]
        _write_customers_file_atomic(customers)
        return True


def save_customers(customers: dict) -> None:
    """⚠️ שמירה בכמות (bulk) - לתאימות לאחור לסקריפטים בלבד (deduplicate_customers.py,
    _pilot_reset_status.py וכו'), **לא** לקוד שוטף (השתמשו ב-update_card).

    בטוחה יותר מבעבר: **לעולם לא מוחקת** כרטיסים שחסרים במילון (לא במסד ולא
    בקובץ - במצב JSON המילון ממוזג לתוך התוכן הנוכחי של הקובץ). עדיין חשופה
    ל-lost update על כרטיסים שבמילון: ערך שנטען מוקדם דורס שינוי מקבילי שקרה
    בינתיים. למחיקת כרטיס - delete_lead."""
    log = logging.getLogger("whatsapp_crm")
    log.warning("⚠️ save_customers: שמירה בכמות של %d כרטיסים (bulk) - לא אטומית פר-כרטיס; "
                "לקוד שוטף להשתמש ב-extract.update_card", len(customers))
    if _use_database():
        summary = db.upsert_customers_rows(customers)
        log.info("✅ כרטיסי לידים נשמרו במסד (%d לידים, עודכנו %d, לא נמחק דבר)",
                 len(customers), summary["upserted"])
        return
    with _json_lock:
        merged = _read_customers_file()
        merged.update(customers)
        _write_customers_file_atomic(merged)
    log.info("✅ customers.json נכתב: %s (סה\"כ %d לידים)", CUSTOMERS_FILE, len(merged))


def _append_history(
    card: dict, channel: str, message: str, direction: str = "in", simulated: bool = False
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "direction": direction,
        "message": message,
    }
    if simulated:
        # ההודעה נרשמה אבל לא נשלחה בפועל (למשל חסימת Twilio Trial) - ראו log_manual_reply
        entry["simulated"] = True
    card.setdefault("history", []).append(entry)


def _customer_key(tenant_id: str, phone: str) -> str:
    # מפתח מורכב tenant::phone - מונע התנגשות בין לקוחות של עסקים (tenants) שונים
    # שבמקרה יש להם אותו מספר טלפון/מזהה
    return f"{tenant_id}::{phone}"


def _new_card(phone: str, tenant_id: str) -> dict:
    """כרטיס ברירת מחדל ליצירה (update_card(..., default=...)) - זהה ל-
    customers.get(key, {"phone": ..., "tenant_id": ...}) שהיה בכל פונקציה."""
    return {"phone": phone, "tenant_id": tenant_id}


# קידומות ידועות לשמות פרטיים מרובי-מילים בעברית - אם המילה הראשונה היא אחת
# מאלה, השם הפרטי "האמיתי" הוא שתי המילים הראשונות יחד (למשל "בת שבע", לא רק "בת")
_COMPOUND_FIRST_NAME_PREFIXES = {"בן", "בת", "אבי", "אבו"}

# מילות-מפתח גנריות שמעידות שזה לא שם אמיתי אלא placeholder/תיאור-תפקיד - בדיקת
# "מכיל" (substring) ולא התאמה מדויקת, כדי לתפוס גם "לקוח פוטנציאלי"/"מתעניין חדש" וכו'
_GENERIC_NAME_KEYWORDS = {
    "לקוח", "לקוחה", "מתעניין", "מתעניינת", "ליד", "אורח", "אורחת", "לא ידוע",
    "customer", "lead", "guest", "unknown", "test", "בדיקה",
}


def extract_safe_first_name(full_name: str | None) -> str | None:
    """מנקה רווחים מיותרים ומחלץ שם פרטי "בטוח" מתוך שדה שם חופשי - לשימוש בהודעות
    מנוסחות אוטומטית (ראו reactivate.generate_outreach_message). מגן מפני הודעות
    רובוטיות/מביכות: מחזיר None (לא ממציא/מנחש שם) אם השדה ריק, מכיל ספרה, או
    מכיל מילת-מפתח גנרית ("לקוח"/"מתעניין" וכו', ראו _GENERIC_NAME_KEYWORDS) -
    הקורא אמור להשתמש בברכה ניטרלית בלי שם במקרה הזה, לא להטמיע ערך גרוע בהודעה.
    תומך בשמות פרטיים מרובי-מילים נפוצים (למשל "בת שבע") - ראו _COMPOUND_FIRST_NAME_PREFIXES."""
    if not full_name:
        return None
    cleaned = " ".join(full_name.strip().split())  # מכווץ רווחים כפולים/טאבים/שורות חדשות
    if not cleaned:
        return None
    if any(ch.isdigit() for ch in cleaned):
        return None
    if any(keyword in cleaned.lower() for keyword in _GENERIC_NAME_KEYWORDS):
        return None

    tokens = cleaned.split(" ")
    first_name = tokens[0]
    if first_name in _COMPOUND_FIRST_NAME_PREFIXES and len(tokens) > 1:
        first_name = f"{tokens[0]} {tokens[1]}"
    return first_name


def resolve_existing_phone(phone: str, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """מחזיר את מחרוזת הטלפון שתחתיה כבר קיים כרטיס לליד הזה, אם יש - בודק גם את
    הפורמט הגולמי שהתקבל וגם את הפורמט המנורמל (E.164). נועד למנוע כרטיס כפול
    בייבוא CSV/הוספת ליד ידנית: אם כרטיס קיים כבר נשמר בעבר בפורמט מקומי
    (למשל "0501234567") ומייבאים קובץ שבו הטלפון באותו פורמט מקומי בדיוק (למשל
    את אותו קובץ שיוצא מ-/api/leads/export), נרמול אוטומטי ל-E.164 היה יוצר מפתח
    *שונה* ("default::+972501234567") ומכפיל את הליד במקום לעדכן אותו - זה בדיוק
    מה שהפונקציה הזו מונעת. **לא** מתקן/ממזג כרטיסים כפולים שכבר קיימים משתי
    הצורות בעבר (ראו המגבלה הידועה ב-CLAUDE.md) - רק מונע יצירת כפילות *חדשה*.
    אם אין כרטיס קיים בשום פורמט, מחזיר את הפורמט המנורמל (E.164) - ברירת המחדל
    לליד חדש לגמרי."""
    customers = load_customers()
    normalized = _to_e164(phone)
    if _customer_key(tenant_id, phone) in customers:
        return phone
    if _customer_key(tenant_id, normalized) in customers:
        return normalized
    return normalized


def last_contact_at(card: dict | None) -> datetime | None:
    """זמן ההודעה האחרונה (נכנסת או יוצאת) בהיסטוריית הכרטיס, אם יש היסטוריה כלשהי -
    None אם אין כרטיס/היסטוריה בכלל. משותף בין reactivate.py (סף ימים כללי להחייאה
    ידנית) ל-scheduler.py (סף ימים פר-ורטיקל להחייאה אוטומטית) - כדי לא לשכפל את
    לוגיקת ה-parsing פעמיים."""
    if not card or not card.get("history"):
        return None
    try:
        return max(datetime.fromisoformat(h["timestamp"]) for h in card["history"])
    except (KeyError, ValueError):
        return None


def upsert_customer(
    phone: str,
    extracted: dict,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
    raw_message: str | None = None,
) -> dict:
    def _mutate(card: dict, _is_new: bool) -> None:
        card.setdefault("source_channel", source_channel)  # הערוץ שממנו הגיע הלקוח לראשונה
        for field in ("customer_name", "business_name", "location", "property_type", "budget"):
            if extracted.get(field):
                card[field] = extracted[field]
        if raw_message:
            _append_history(card, source_channel, raw_message)

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def process_message(
    phone: str,
    message_text: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
) -> dict:
    extracted = extract_customer_info(message_text)
    return upsert_customer(
        phone, extracted, tenant_id=tenant_id, source_channel=source_channel, raw_message=message_text
    )


REPLY_PROMPT = """\
אתה נציג שירות של עסק קטן, עונה ללקוח פוטנציאלי בוואטסאפ - בחום, בקצרה, ובהקשר של השיחה.

היסטוריית השיחה עד עכשיו (מהישנה לחדשה):
{history_text}

ההודעה החדשה שהתקבלה מהלקוח:
"{message}"

פרטים שכבר ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}, מיקום - {location}.

כתוב תגובה אחת קצרה בעברית (1-2 משפטים), טבעית כמו נציג אמיתי בוואטסאפ:
- ענה ישירות למה שהלקוח כתב בהודעה החדשה, בהתאם להקשר מההיסטוריה. אל תתחיל מחדש ואל תברך שוב אם כבר דיברתם.
- אם יש שם ללקוח, פנה אליו בשמו.
- אם חסר פרט חשוב (למשל עיר או סוג השירות), שאל שאלה אחת קצרה כדי להתקדם.
- אם זו ההודעה הראשונה מהלקוח הזה (אין היסטוריה למעלה) וההודעה כללית/לא ברורה (כמו "היי"/"שלום"/"אשמח לפרטים" בלי פרטים נוספים) - אל תנחש למה הוא מתכוון ואל תענה בברכה כללית סתמית. שאל בנימוס שאלת מיקוד אחת: על מה הוא מתעניין (סוג נכס/אזור/תקציב בערך).
- אל תמציא פרטים שלא ניתנו, ואל תבטיח מחיר, הנחה או תאריך מדויק.
- בלי אימוג'ים בכלל. בלי סימני קריאה מוגזמים, בלי ריבוי משפטי נימוס, בלי פתיחה רובוטית.
- כתוב כמו שנציג מכירות כותב מהטלפון: קצר, ישיר, בגובה העיניים.
החזר רק את טקסט ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""


def _format_history_for_reply(card: dict, limit: int = 10) -> str:
    """היסטוריית השיחה מהכרטיס (ללא ההודעה הנוכחית שהיא האחרונה בו), כטקסט לפרומפט
    המענה. הכרטיס הוא מקור האמת לשיחה (customers.json), לכן לא צריך שאילתת DB נוספת."""
    entries = (card.get("history") or [])[:-1][-limit:]
    if not entries:
        return "(אין הודעות קודמות בשיחה הזו)"
    return "\n".join(
        f"[{'לקוח' if e.get('direction') == 'in' else 'אנחנו'}] {e.get('message', '')}"
        for e in entries
    )


def generate_reply(message_text: str, card: dict, history_text: str = "") -> str:
    prompt_template = prompts.get_prompt("speed_to_lead_reply", REPLY_PROMPT)
    prompt = prompt_template.format(
        message=message_text,
        history_text=history_text or "(אין הודעות קודמות בשיחה הזו)",
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
        location=card.get("location") or "לא ידוע",
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=200,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    from whatsapp_provider import strip_emojis
    return strip_emojis(next(block.text for block in response.content if block.type == "text").strip())


CALL_SUMMARY_PROMPT = """\
אתה עוזר שמסכם שיחת טלפון בין נציג מכירות/שירות ללקוח, על סמך הערות חופשיות שהקליד
הנציג מיד אחרי השיחה.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}, מיקום - {location}.

הערות הנציג מהשיחה:
"{notes}"

כתוב תקציר קצר וברור בעברית (2-4 משפטים): מה עלה בשיחה, מה הלקוח ביקש/הביע, ומה הצעדים
הבאים שסוכמו (אם יש). אל תמציא פרטים שלא נכתבו בהערות. החזר רק את טקסט התקציר, בלי
מרכאות ובלי הסברים נוספים.
"""


def generate_call_summary(notes: str, card: dict) -> str:
    prompt_template = prompts.get_prompt("call_summary", CALL_SUMMARY_PROMPT)
    prompt = prompt_template.format(
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
        location=card.get("location") or "לא ידוע",
        notes=notes,
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=300,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(block.text for block in response.content if block.type == "text").strip()


# ---- AI Sales Copilot (בתוך פאנל ה-Timeline) - אותו דפוס בדיוק כמו generate_reply/
# generate_call_summary למעלה: קבוע כברירת מחדל בקוד + prompts.get_prompt כמקור
# אמת בזמן ריצה (עריכה חיה מ-"🤖 תבניות וסוכנים", בלי לגעת בקוד) ----
#
# הצעת מענה להתנגדות עברה ל-sales_agent.py (מודול ייעודי לסוכן המכירות/התנגדויות -
# ראו "חלוקת סוכנים" ב-CLAUDE.md) - suggest_replies שם מחליפה את generate_
# objection_response שהיה כאן קודם (הצעה בודדת, בלי סיווג התנגדות/עד 3 חלופות).

EXECUTIVE_SUMMARY_PROMPT = """\
אתה עוזר שמסכם עבור מנהל מכירות עסוק את כל ההיסטוריה מול ליד מסוים, בקצרה וממוקד.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}, מיקום - {location}.

היסטוריית ההודעות/שיחות עם הליד (מהישנה לחדשה):
{history_text}

כתוב סיכום מנהלים בעברית בדיוק בפורמט הבא - שלושה בולטים (•), כל אחד משפט אחד קצר
וממוקד: היכן עומד התהליך מול הליד, מה החשש/העניין המרכזי שעלה, ומה הצעד הבא המומלץ.
אל תמציא פרטים שלא עלו בהיסטוריה בפועל. החזר רק את שלושת הבולטים, בלי כותרת ובלי
הסברים נוספים.
"""


FOLLOWUP_PROMPT = """\
אתה כותב הודעת מעקב (follow-up) קצרה וטבעית בעברית ללקוח שיצרתם איתו קשר
לאחרונה (סומן "נוצר קשר"), אך הוא עדיין לא השיב.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}.

כתוב הודעת פולו-אפ קצרה (1-2 משפטים), עדינה ולא לחוצה, שמזכירה בעדינות שעדיין
מחכים לתשובתו ומזמינה אותו להמשיך את השיחה כשנוח לו - שונה בטון מפנייה ראשונה
ללקוח קר (זה לא "החייאה" - השיחה כבר התחילה). אל תמציא פרטים שלא ניתנו. החזר
רק את טקסט ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""

FOLLOWUP_FINAL_PROMPT = """\
אתה כותב הודעת מעקב **אחרונה** (follow-up סופי) קצרה בעברית ללקוח שיצרתם איתו
קשר, קיבל כבר תזכורת פולו-אפ אחת, ועדיין לא השיב בכלל.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}.

כתוב הודעה קצרה (1-2 משפטים), חמה ולא לוחצת, שמבהירה בעדינות שזו הפנייה
האחרונה בנושא הזה בשלב זה, ומשאירה את הדלת פתוחה לפנות חזרה כשיתאים לו - בלי
טון של ויתור/תלונה, ובלי לחץ מכירתי. אל תמציא פרטים שלא ניתנו. החזר רק את
טקסט ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""


def generate_followup_message(card: dict, final: bool = False) -> str:
    """הודעת פולו-אפ אוטומטית ללידים שסומנו "נוצר קשר"/"follow_up" ולא ענו תוך
    X שעות - ראו scheduler.check_pending_followups. final=True: הודעת המעקב
    השנייה/האחרונה (72 שעות, טון סופי יותר) במקום הראשונה (24 שעות). פרומפט
    נפרד מ-reactivate.generate_outreach_message (החייאת לידים קרים) בכוונה -
    טון שונה: "עדיין מחכים לתשובה משיחה שכבר התחילה", לא "פונים מחדש אחרי
    הפסקה ארוכה"."""
    key = "followup_final_message" if final else "followup_message"
    default = FOLLOWUP_FINAL_PROMPT if final else FOLLOWUP_PROMPT
    prompt_template = prompts.get_prompt(key, default)
    prompt = prompt_template.format(
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=150,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    from whatsapp_provider import strip_emojis
    return strip_emojis(next(block.text for block in response.content if block.type == "text").strip())


MISSED_CALL_FOLLOWUP_PROMPT = """\
אתה כותב הודעת וואטסאפ קצרה וקז'ואלית בעברית ללקוח שניסינו להתקשר אליו טלפונית
ולא הצלחנו להשיג אותו (השיחה לא נענתה).
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}.

כתוב הודעה קצרה (1-2 משפטים), טבעית וידידותית כמו הודעה שאדם אמיתי שולח מהטלפון -
לא רשמית, בלי "לתשומת ליבך"/"הריני", בלי תחושת רובוט. ציין בעדינות שניסינו
להתקשר ולא הצלחנו להשיג, ושאל מתי נוח לו שנחזור/אם אפשר להמשיך כאן בוואטסאפ.
אל תמציא פרטים שלא ניתנו. החזר רק את טקסט ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""


def generate_missed_call_followup_message(card: dict) -> str:
    """הודעת פולו-אפ אוטומטית בוואטסאפ אחרי שיחה שלא נענתה (No-Answer Call Log -
    ראו server.voice_dial_status, scheduler.check_pending_call_followups). טון
    קז'ואלי ושונה מ-generate_followup_message (זה לא "לא ענית לי בוואטסאפ",
    זה "התקשרתי ולא עניתי בטלפון")."""
    prompt_template = prompts.get_prompt("missed_call_followup_message", MISSED_CALL_FOLLOWUP_PROMPT)
    prompt = prompt_template.format(
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=150,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    from whatsapp_provider import strip_emojis
    return strip_emojis(next(block.text for block in response.content if block.type == "text").strip())


# שדות שנחשבים "חובה" להשלמה בכרטיס ליד - קבועה ניתנת להרחבה. תקציב/סוג-נכס
# (property_type/budget) לא נכללים בברירת המחדל בכוונה: הם רלוונטיים רק לוורטיקל
# נדל"ן ומתמלאים מחילוץ טקסט רגיל (extract_customer_info) וגם מהודעה קולית
# (update_lead_voice_extraction) - אבל ברוב הלידים הקיימים בפועל (רובם עסקי-
# קמעונאות, לא נדל"ן) budget חסר כמעט תמיד, כך שהכללתו כ"חובה" הייתה מסמנת כמעט
# כל ליד ומרוקנת את האינדיקטור ממשמעות. email לא נכלל - השדה הזה לא קיים בשום
# מקום בסכמה של הפרויקט (לא נאסף בשום טופס/webhook/חילוץ) - יש להוסיף רק אחרי
# שייבנה מנגנון איסוף אמיתי.
REQUIRED_LEAD_FIELDS: dict[str, str] = {
    "customer_name": "שם",
    "location": "מיקום",
}


def get_missing_fields(lead_data: dict) -> list[str]:
    """מחזיר את מפתחות השדות (מתוך REQUIRED_LEAD_FIELDS) שריקים/חסרים בכרטיס -
    מחרוזת ריקה/רק-רווחים/None נחשבים "חסר". סדר יציב (לפי סדר ההגדרה למעלה).
    משמש הן לזיהוי מועמדים לפנייה (GET /api/leads - שדה missing_fields לכל ליד,
    ואינדיקטור בדשבורד) והן לניסוח הודעת ההשלמה (generate_missing_fields_message)."""
    missing = []
    for field in REQUIRED_LEAD_FIELDS:
        value = lead_data.get(field)
        if not value or not str(value).strip():
            missing.append(field)
    return missing


MISSING_FIELDS_PROMPT = """\
אתה כותב הודעת וואטסאפ קצרה וטבעית בעברית ללקוח שכבר בקשר איתנו, כדי לבקש ממנו
בעדינות להשלים פרט אחד או שניים שעדיין חסרים לנו אצלו.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}.
הפרטים החסרים שצריך לבקש: {missing_labels}.

כתוב הודעה קצרה (1-2 משפטים), עדינה ולא פולשנית, שמסבירה בטבעיות שרוצים להכיר
אותו טוב יותר / להתאים את השירות אליו, ומבקשת רק את הפרטים החסרים שצוינו למעלה -
בלי לנחש/להמציא פרטים אחרים, ובלי לחזור על מידע שכבר ידוע. החזר רק את טקסט
ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""


def generate_missing_fields_message(card: dict, missing_fields: list[str]) -> str:
    """מנסחת הודעת פנייה עדינה לבקשת השלמת פרטים חסרים (ראו get_missing_fields) -
    כפתור "🧩 הצע הודעת השלמה" בפאנל ה-Timeline. בדיוק כמו generate_objection_
    response/generate_executive_summary - לעולם לא שולחת בעצמה, רק מחזירה טקסט
    מוצע (POST /api/leads/missing-fields/suggest); השליחה בפועל תמיד דרך לחיצה
    אנושית מפורשת על "אישור ושליחה" בדשבורד -> אותו נתיב קיים POST /api/messages/
    send (חוק בטיחות #1 ב-CLAUDE.md - לא נבנה מסלול שליחה מקביל/נוסף)."""
    labels = [REQUIRED_LEAD_FIELDS.get(field, field) for field in missing_fields]
    prompt_template = prompts.get_prompt("missing_fields_outreach", MISSING_FIELDS_PROMPT)
    prompt = prompt_template.format(
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
        missing_labels=", ".join(labels),
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=150,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(block.text for block in response.content if block.type == "text").strip()


def generate_executive_summary(history_text: str, card: dict) -> str:
    """סיכום מנהלים ב-3 בולטים של כל ההיסטוריה מול הליד - כפתור "📋 סיכום מנהלים"
    בפאנל ה-Timeline. history_text: טקסט מרוכז של כל ההודעות/תקצירי שיחות (ראו
    server.py - api_executive_summary, בונה מ-db.get_messages)."""
    prompt_template = prompts.get_prompt("executive_summary", EXECUTIVE_SUMMARY_PROMPT)
    prompt = prompt_template.format(
        history_text=history_text or "(אין עדיין היסטוריה מתועדת)",
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
        location=card.get("location") or "לא ידוע",
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=400,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(block.text for block in response.content if block.type == "text").strip()


def process_message_with_reply(
    phone: str,
    message_text: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
) -> tuple[dict, str]:
    """כמו process_message, ובנוסף מייצר תשובה אוטומטית ורושם אותה בהיסטוריה כהודעה יוצאת.
    זו גם נקודת ה"מגע הראשון/מחודש" היחידה שמקדמת lead_status אוטומטית (סגמנטציה):
    "new"/"follow_up"/"no_answer" (או חסר) -> "contacted" - כי כאן, ובכאן בלבד, יש
    וידוא שהבוט בפועל ענה ללקוח (בניגוד ל-log_manual_reply/update_lead_voice_extraction,
    שבכוונה לא נוגעים ב-lead_status - ראו שם). "no_answer" נכלל כאן - אם הלקוח כן
    כותב בוואטסאפ אחרי שיחה שלא נענתה, זה מגע חדש אמיתי, לא "עדיין לא ענה לטלפון".
    לא דורס "hot"/"not_relevant"/"reactivation"/"human_handling" - אלו סטטוסים
    שנקבעו ע"י כוונה מפורשת (סוכן/נציג אנושי), לא ע"י עצם קבלת הודעה."""
    card = process_message(phone, message_text, tenant_id=tenant_id, source_channel=source_channel)
    reply_text = generate_reply(message_text, card, history_text=_format_history_for_reply(card))
    card = record_automated_reply(phone, reply_text, tenant_id=tenant_id, source_channel=source_channel)
    return card, reply_text


def record_automated_reply(
    phone: str, reply_text: str, tenant_id: str = DEFAULT_TENANT_ID, source_channel: str = "whatsapp",
) -> dict:
    """רושם תשובה אוטומטית *שכבר נוצרה* (ולא ע"י generate_reply - ראו הזרימה
    המקורה ב-process_message_with_reply מעל) ב-history המוטבע בכרטיס (card,
    נפרד מ-crm_data.db.messages) ומקדמת lead_status ל-"contacted" באותם
    תנאים מדויקים - נחלץ מ-process_message_with_reply כדי ששתי זרימות-
    תשובה-אוטומטית (ה"רגילה" כאן, ובירור הצרכים הקצר - ראו server._run_lead_
    qualification_turn) יחלקו את אותה לוגיקת-תוצאה-אחרי-שליחה, בלי כפילות קוד
    ובלי שבירור הצרכים "ישכח" לעדכן lead_status/history המוטבע כמו שהתשובה
    הרגילה כבר עושה."""
    def _mutate(card: dict, _is_new: bool) -> None:
        _append_history(card, source_channel, reply_text, direction="out")
        if card.get("lead_status") in (None, "", "new", "follow_up", "no_answer"):
            card["lead_status"] = "contacted"
            card["status_changed_at"] = datetime.now(timezone.utc).isoformat()

    # בלי default - כרטיס חסר = KeyError, בדיוק כמו customers[key] שהיה כאן
    return update_card(_customer_key(tenant_id, phone), _mutate)


MEETING_REMINDER_PROMPT = """\
אתה כותב הודעת תזכורת קצרה וטבעית בעברית ללקוח על פגישה שנקבעה איתו, שעתידה
להתקיים ב-{due_date} בשעה {due_time}.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}.

כתוב הודעה קצרה (1-2 משפטים), חמה וידידותית כמו הודעה שאדם אמיתי שולח מהטלפון -
לא רשמית, בלי "לתשומת ליבך"/"הריני". תזכיר בעדינות את מועד הפגישה (תאריך ושעה)
ותשאל אם המועד עדיין נוח/מזמינה לאשר. אל תמציא פרטים שלא ניתנו (למשל מקום
הפגישה, אם לא צוין). החזר רק את טקסט ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""


def generate_meeting_reminder_message(card: dict, task: dict) -> str:
    """תזכורת פגישה אוטומטית בוואטסאפ - נשלחת בערב הקודם (פגישת בוקר) או בבוקר
    אותו היום (פגישת אחה"צ/ערב), ראו scheduler.compute_meeting_reminder_time/
    check_pending_meeting_reminders. task: שורת calendar_tasks (due_date/due_time)."""
    prompt_template = prompts.get_prompt("meeting_reminder_message", MEETING_REMINDER_PROMPT)
    prompt = prompt_template.format(
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
        due_date=task.get("due_date") or "",
        due_time=task.get("due_time") or "",
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=150,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    from whatsapp_provider import strip_emojis
    return strip_emojis(next(block.text for block in response.content if block.type == "text").strip())


MEETING_INVITE_PROMPT = """\
אתה כותב הודעת וואטסאפ מנומסת ללקוח, יום לפני פגישה שנקבעה איתו, עם פרטי הפגישה.
תאריך ושעת הפגישה: {due_date} בשעה {due_time}.
מיקום/כתובת הפגישה (אם צוין): {location}.
מי מטעמנו ייפגש איתו (אם צוין): {host}.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}.

כתוב הודעה קצרה (2-4 משפטים) בעברית, מנומסת וחמה - לא רשמית מדי, בלי "לתשומת
ליבך"/"הריני". ציין את תאריך ושעת הפגישה בבירור, ואת המיקום ומי ייפגש איתו אם
סופקו (אם לא סופקו - אל תמציא/תנחש, פשוט אל תזכיר אותם). סיים בנימה נעימה
שמחכים לו. אל תמציא פרטים שלא ניתנו. החזר רק את טקסט ההודעה, בלי מרכאות ובלי
הסברים נוספים.
"""


def generate_meeting_invite_message(card: dict, task: dict) -> str:
    """הודעת "זימון" מנומסת ליום לפני הפגישה (Meeting Confirmation & No-Show
    Prevention - ראו scheduler.check_pending_meeting_invites) - שונה מ-
    generate_meeting_reminder_message (זו לא שואלת "עדיין נוח?", היא מודיעה
    את פרטי הפגישה: מיקום + מי לפגוש). location/host נשלפים מ-task["notes"]/
    card["agent"] בהתאמה - אין שדות DB נפרדים למיקום פגישה/מארח (ראו ההערה
    ב-scheduler.py - task.notes הוא כבר השדה החופשי המתאים לזה, לא כפל מבנה)."""
    prompt_template = prompts.get_prompt("meeting_invite_message", MEETING_INVITE_PROMPT)
    prompt = prompt_template.format(
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
        due_date=task.get("due_date") or "",
        due_time=task.get("due_time") or "",
        location=task.get("notes") or "לא צוין",
        host=card.get("agent") or "לא צוין",
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=200,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    from whatsapp_provider import strip_emojis
    return strip_emojis(next(block.text for block in response.content if block.type == "text").strip())


# תבנית קבועה ומילולית (לא פרומפט-הנחיה ל-AI) - ראו הנחיה מפורשת מהמשתמש:
# "באמצעות התבנית הבאה בלבד, תוך התאמה דינמית של השם/היום/התאריך/השעה בלבד" -
# כל שאר הטקסט (כתובת/הנחיות חניה/קישור Waze/חתימה, כולל האימוג'ים 📍🙏) חייב
# להישאר מילה-במילה בכל הודעה, לא ניסוח-AI משתנה - טעות בכתובת/קישור כאן
# פוגעת בעסק בפועל, לא רק "ניסוח פחות טוב". בכוונה *בלי* קריאת Claude בכלל -
# זה גם אומר שהפיצ'ר הזה ממשיך לעבוד גם כש-יתרת הקרדיט של Anthropic אפסה
# (ראו האבחון בשיחה) - אין שום תלות ב-AI להודעה הקבועה-מילולית הזו.
MEETING_BOOKING_CONFIRMATION_PROMPT = """\
היי {customer_name}, בהמשך לשיחתנו נקבעה לך פגישה ליום {day} {date} בשעה {time} .
כתובתנו : דרך מנחם בגין 7, רמת-גן - נקסוס נדל"ן  (מגדל בית גיבור , קומה 21).
לפגישה יש להצטייד עם ת״ז לצורך כניסה למגדל ואמצעי תשלום למידת הצורך.

חנייה שמורה עבורך בחניון המגדל - חניון בית גיבור . (יש להחנות בחניית האורחים)

קישור למיקום החניון :📍

https://waze.com/ul/hsv8wxchc3

זמין עבורך לכל שאלה🙏"""

_HEBREW_WEEKDAYS_MEETING = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


def generate_meeting_booking_confirmation_message(card: dict, task: dict) -> str:
    """הודעת אישור קביעת פגישה (Automated Meeting Booking Confirmation) - נשלחת
    ברגע שנציג מאשר בפועל הצעת פגישה חדשה שזוהתה ע"י scheduling_agent (ראו
    server.api_task_confirm/_send_meeting_confirmation_after_delay) - לא ברגע
    שה-AI רק *מזהה* כוונת תזמון בהודעה (detect_scheduling_intent נשאר ניתוח-
    בלבד, ראו חוק הבטיחות ב-scheduling_agent.py) - אחרת היינו שולחים ללקוח
    "הפגישה אושרה" על בסיס ניחוש AI בלי שום אישור אנושי, בניגוד מוחלט לעיקרון
    שה-agent הזה קיים כדי לאכוף.

    תבנית קבועה (ראו MEETING_BOOKING_CONFIRMATION_PROMPT) - לא קריאת AI: רק
    customer_name/day/date/time מותאמים דינמית מ-card/task, שאר הטקסט (כתובת/
    הנחיות/קישור) קבוע-לחלוטין בכל הודעה. ⚠️ אם task["due_date"] חסר/לא-תקין
    (לא אמור לקרות בפועל - משימה מאושרת תמיד עם תאריך, אבל הגנה בכל זאת) -
    day נופל ל-"" במקום לקרוס, כדי שההודעה עדיין תישלח (בלי שם היום) ולא
    תיכשל שקט על כל זרימת האישור."""
    prompt_template = prompts.get_prompt("meeting_booking_confirmation_message", MEETING_BOOKING_CONFIRMATION_PROMPT)
    due_date = task.get("due_date") or ""
    try:
        day = _HEBREW_WEEKDAYS_MEETING[datetime.strptime(due_date, "%Y-%m-%d").weekday()]
        date_label = datetime.strptime(due_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        day = ""
        date_label = due_date
    return prompt_template.format(
        customer_name=card.get("customer_name") or "לקוח/ה יקר/ה",
        day=day,
        date=date_label,
        time=task.get("due_time") or "",
    )


# Speed-to-Lead - הודעת פתיחה יזומה לליד חדש מקמפיין (ראו server.api_create_lead/
# _send_speed_to_lead_outreach_and_mute) - נשלחת **אוטומטית ומיידית** ברגע שנוצר
# כרטיס חדש (is_new=True מ-import_lead), **לפני** שהליד בכלל כתב הודעה - לא תגובה
# לשום דבר, פתיחה יזומה. בכוונה תבנית קבועה-מילולית (⚠️ DRAFT - טקסט ראשוני
# לעריכה חופשית דרך פאנל ה"🧠 פרומפטים" בדשבורד, או ישירות כאן, בהתאם למה
# שהעסק בפועל ירצה לנסח) - **לא** AI free-form, מאותה סיבה בדיוק כמו הודעת
# אישור הפגישה: המטרה המוצהרת היא "להיראות כנציג אנושי לחלוטין" - ניסוח קבוע
# ועקבי משרת את זה טוב יותר מניסוח AI שמשתנה בכל פעם, ולא תלוי ביתרת קרדיט
# Anthropic. {project_line} הוא שורה שלמה-אופציונלית (לא רק מילה) - ריקה
# לגמרי אם project לא ידוע, כדי לא "למחוק" משפט באמצע הטקסט בצורה מגושמת.
SPEED_TO_LEAD_OUTREACH_PROMPT = """\
היי {customer_name}, תודה שהשארת פרטים אצלנו{project_line}!
אני כאן בשביל לעזור - משווק מטעמנו יחזור אליך בהקדם האפשרי, אבל רציתי להיות בקשר כבר עכשיו.
מה הכי חשוב לך בחיפוש? אשמח לכמה פרטים ראשוניים כדי שנוכל להתאים לך את הדירה/הפרויקט המתאים ביותר."""


def generate_speed_to_lead_outreach_message(card: dict) -> str:
    """הודעת הפתיחה היזומה הראשונה לליד חדש מקמפיין (Speed-to-Lead) - ראו
    ההערה המלאה ב-SPEED_TO_LEAD_OUTREACH_PROMPT. תבנית קבועה (לא AI) -
    customer_name/project_line מותאמים דינמית, שאר הטקסט קבוע. נשלחת **פעם
    אחת בלבד** לכל ליד (server.py דואג שזה יקרה רק כש-is_new=True, ומשתיק
    את ה-AI אוטומטית ברגע שהיא נשלחת - ראו _send_speed_to_lead_outreach_and_mute)."""
    prompt_template = prompts.get_prompt("speed_to_lead_outreach", SPEED_TO_LEAD_OUTREACH_PROMPT)
    project = (card.get("project") or "").strip()
    project_line = f" בנוגע ל{project}" if project else ""
    return prompt_template.format(
        customer_name=card.get("customer_name") or "לקוח/ה יקר/ה",
        project_line=project_line,
    )


# בירור צרכים קצר (Short Qualification Flow, ראו sales_agent.run_qualification_
# turn/server._run_lead_qualification_turn) - משפט הסיום **חייב** להישאר קבוע-
# מילולית בדיוק כמו שהוגדר ("מעולה, העברתי את הפרטים...") - לא ניסוח-AI
# משתנה, מאותה סיבה בדיוק כמו הודעת אישור הפגישה/הפתיחה היזומה: ברגע שהנציג
# האנושי מקבל ליד "מועבר", הניסוח המדויק שהלקוח קיבל חשוב. שתי גרסאות - עם/
# בלי בקשת שם - כדי לא "לסתור את עצמו" (לכתוב "העברתי את הפרטים" כשעדיין חסר
# הפרט הכי בסיסי, השם).
QUALIFICATION_HANDOFF_KNOWN_NAME_PROMPT = (
    "מעולה, העברתי את הפרטים ישירות למנהל הפרויקט והוא יתקשר אליך כבר בדקות הקרובות."
)
QUALIFICATION_HANDOFF_NEED_NAME_PROMPT = (
    "מעולה! רק לפני שאני מעביר את הפרטים למנהל הפרויקט - מה השם שלך? "
    "הוא יתקשר אליך כבר בדקות הקרובות."
)


def generate_qualification_handoff_message(card: dict) -> str:
    """הודעת הסיום של בירור הצרכים הקצר - נשלחת כש-run_qualification_turn
    מסמנת is_complete=True, או כשהגענו למקסימום תגובות AI (ראו server._run_
    lead_qualification_turn) - תבנית קבועה (לא AI free-form), בדיוק כמו
    generate_meeting_booking_confirmation_message/generate_speed_to_lead_
    outreach_message. אחריה ה-AI משתתק אוטומטית (ai_enabled=False) - זו
    ההודעה האחרונה בשיחה היזומה."""
    if (card.get("customer_name") or "").strip():
        return prompts.get_prompt("qualification_handoff_known_name", QUALIFICATION_HANDOFF_KNOWN_NAME_PROMPT)
    return prompts.get_prompt("qualification_handoff_need_name", QUALIFICATION_HANDOFF_NEED_NAME_PROMPT)


def apply_qualification_turn_result(
    phone: str,
    reply_text: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
    purpose: str | None = None,
    property_type: str | None = None,
    customer_name: str | None = None,
    mute: bool = False,
) -> dict:
    """מיישמת את **כל** תוצאת תור בירור הצרכים הקצר (ראו server._run_lead_
    qualification_turn/sales_agent.run_qualification_turn) במחזור load_
    customers→save_customers **אחד בלבד** (היום: update_card אחד) - לא ארבע קריאות נפרדות (מונה
    התורות, update_lead_fields, רישום ההודעה היוצאת, ואולי update_lead_
    ai_enabled), שכל אחת מהן קודם עשתה load+save משלה.

    ⚠️ זה קריטי לעמידות בפני קריסה/אתחול שרת (Postgres, DATABASE_URL מוגדר):
    update_card→db.update_customer_row כותבת את השורה הזו בלבד בטרנזקציה
    אחת (SELECT FOR UPDATE + INSERT...ON CONFLICT DO UPDATE + commit יחיד) - ולכן מספר התורות,
    השדות שנאספו (purpose/property_type/customer_name), ההודעה שנרשמה
    ו-ai_enabled **כולם מתעדכנים יחד, אטומית, או לא מתעדכנים כלל** אם השרת
    קרס/התאפס בדיוק באמצע התור הזה. ארבע קריאות נפרדות (המודל הקודם) היו
    חושפות חלון-קריסה אמיתי: למשל qualification_turns עולה ל-2 (ומגבלת
    ה-2-תורות הקשיחה "נעולה"), אבל ai_enabled נשאר True וההודעה שסוכמה
    בפועל אף פעם לא נרשמה - מצב-ביניים לא-עקבי שהמערכת לא הייתה "זוכרת"
    נכון בהמשך.

    מחזירה את הכרטיס המעודכן."""
    def _mutate(card: dict, _is_new: bool) -> None:
        card["qualification_turns"] = int(card.get("qualification_turns") or 0) + 1

        def _set(field: str, value: str | None) -> None:
            if value:
                card[field] = value.strip()

        _set("purpose", purpose)
        _set("property_type", property_type)
        _set("customer_name", customer_name)

        _append_history(card, source_channel, reply_text, direction="out")
        if card.get("lead_status") in (None, "", "new", "follow_up", "no_answer"):
            card["lead_status"] = "contacted"
            card["status_changed_at"] = datetime.now(timezone.utc).isoformat()

        if mute:
            card["ai_enabled"] = False

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def mark_speed_to_lead_outreach_sent(phone: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מסמנת שהפתיחה היזומה (Speed-to-Lead, ראו server._send_speed_to_lead_
    outreach) נשלחה לליד הזה - **לא** משתיקה את ה-AI (בניגוד למה שקרה קודם
    כאן, ראו ההיסטוריה ב-server.py): המטרה המדויקת של הדגל הזה היא לפתוח
    חלון-זמן שבו בירור הצרכים הקצר (ראו server._process_incoming_message_job/
    _run_lead_qualification_turn) **כן** יופעל על התשובה הראשונה של הליד,
    למרות ש-import_source מוגדר על הכרטיס (ליד שנכנס דרך POST /api/leads -
    ראו api_create_lead - הוא בהגדרה "ליד חדש מקמפיין", לא "ליד קיים
    במאגר" שההגנה הקשיחה ב-import_source נועדה לחסום). בלי הדגל הזה, תגובת
    הליד להודעת הפתיחה הייתה נחסמת בטעות כאילו הוא לקוח קיים."""
    return update_card(
        _customer_key(tenant_id, phone),
        lambda card, _is_new: card.__setitem__("speed_to_lead_outreach_sent", True),
        default=_new_card(phone, tenant_id),
    )


def mark_meeting_booked(phone: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מסמנת meeting_booked=True על כרטיס הליד - שדה גלוי-ישירות על הכרטיס
    (לא רק נגזר מ-calendar_tasks.task_type="meeting", ראו db._init_schema)
    לתצוגה/סינון מהירים ברשימת לידים בלי join לטבלת המשימות. נקראת **רק**
    מתוך db.confirm_task_proposal שהצליחה בפועל ל-intent "new"/"reschedule"
    (ראו server.api_task_confirm/_maybe_auto_confirm_scheduling_proposal) -
    כלומר יש כבר פגישה אמיתית מאושרת, לא רק הצעה שמחכה לאישור. דגל חד-כיווני
    בכוונה (לא מתאפס אוטומטית בביטול פגישה) - "נקבעה פגישה בשלב מסוים בתהליך
    מול הליד הזה" הוא עובדה היסטורית, גם אם המועד הספציפי השתנה/בוטל מאוחר
    יותר; מי שרוצה את המועד/הסטטוס העדכני המדויק ימשיך להשתמש ב-calendar_
    tasks (ראו GET /api/calendar/agenda)."""
    return update_card(
        _customer_key(tenant_id, phone),
        lambda card, _is_new: card.__setitem__("meeting_booked", True),
        default=_new_card(phone, tenant_id),
    )


def update_activity_summary(phone: str, summary: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מעדכן תקציר פעילות קצר (activity_summary) שמתעדכן אוטומטית אחרי כל הודעה
    נכנסת (ראו server.py._process_incoming_message_job, מבוסס extract.
    generate_executive_summary) - מטא-דאטה בלבד, בדיוק כמו update_lead_voice_
    extraction: לא נרשם ב-history בנפרד ולא נוגע ב-lead_status."""
    return update_card(
        _customer_key(tenant_id, phone),
        lambda card, _is_new: card.__setitem__("activity_summary", summary),
        default=_new_card(phone, tenant_id),
    )


def log_manual_reply(
    phone: str,
    message: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
    simulated: bool = False,
) -> dict:
    """רושם הודעה יוצאת שנשלחה ידנית ע"י נציג מהדשבורד. בכוונה לא נוגע ב-lead_status -
    בניגוד ל-update_lead_status, זו לא בהכרח פעולת חימום/מעקב שמשנה את שלב הליד.
    simulated=True: ההודעה נרשמה כתיעוד, אבל Twilio חסם את השליחה בפועל (חשבון Trial)."""
    def _mutate(card: dict, _is_new: bool) -> None:
        card.setdefault("source_channel", source_channel)
        _append_history(card, source_channel, message, direction="out", simulated=simulated)

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def log_call_summary(
    phone: str,
    summary: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    simulated: bool = False,
) -> dict:
    """רושם תקציר שיחת טלפון (מיוצר ע"י Claude מתוך הערות חופשיות שהקליד הנציג -
    ראו generate_call_summary) בהיסטוריית הכרטיס, בערוץ channel="voice". בכוונה לא
    נוגע ב-lead_status - כמו log_manual_reply."""
    def _mutate(card: dict, _is_new: bool) -> None:
        card.setdefault("source_channel", "whatsapp")  # לא דורסים את הערוץ המקורי שהליד הגיע ממנו
        _append_history(card, "voice", summary, direction="out", simulated=simulated)

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def import_lead(
    phone: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    customer_name: str | None = None,
    business_name: str | None = None,
    location: str | None = None,
    lead_status: str | None = None,
    import_source: str | None = None,
    category: str | None = None,
    agent: str | None = None,
    notes: str | None = None,
    client_id: str | None = None,
    email: str | None = None,
    address: str | None = None,
    property_type: str | None = None,
    budget: str | None = None,
    requirements: str | None = None,
    rooms: str | None = None,
    project: str | None = None,
) -> tuple[dict, bool]:
    """יוצר/מעדכן כרטיס לקוח - מייבוא CSV/Excel (ראו import_leads.py/
    /api/leads/import) או מהוספת ליד בודד ידנית (POST /api/leads) ב-server.py.
    לא הודעה - אין רישום ב-history. Upsert לפי phone+tenant_id (מונע כפילויות,
    ראו resolve_existing_phone לפתרון התאמת-פורמט). מחזיר (card, is_new) -
    is_new=True אם זה ליד חדש שלא היה קיים קודם. client_id (שלב 5, אופציונלי) -
    ראו update_lead_fields; ai_enabled לא מוגדר כאן בכוונה (ברירת המחדל True
    נאכפת ב-server.py._ai_enabled_for בקריאה, לא צריך לכתוב אותה מפורשות בכל
    כרטיס).

    כל הפרמטרים כאן הם upsert תוסף-בלבד (בניגוד ל-update_lead_fields, שם ""
    מפורש מוחק שדה): ערך ריק/None פשוט לא נוגע בשדה הקיים - ייבוא חזרה של
    אותו קובץ, או קובץ עם עמודות חסרות לחלק מהשורות, לא ימחק נתונים שכבר
    קיימים בכרטיס מהיסטוריה/עריכה ידנית קודמת."""
    fields = {
        "customer_name": customer_name,
        "business_name": business_name,
        "location": location,
        "lead_status": lead_status,
        "import_source": import_source,
        "category": category,
        "agent": agent,
        "notes": notes,
        "email": email,
        "address": address,
        "property_type": property_type,
        "budget": budget,
        "requirements": requirements,
        "rooms": rooms,
        "project": project,
        "client_id": client_id,
    }
    created = {"is_new": False}

    def _mutate(card: dict, is_new: bool) -> None:
        created["is_new"] = is_new  # נקבע בתוך הנעילה - נכון גם תחת יצירה מקבילית
        for field, value in fields.items():
            if value:
                card[field] = value

    card = update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))
    return card, created["is_new"]


def append_imported_history(
    phone: str, entries: list[dict], tenant_id: str = DEFAULT_TENANT_ID
) -> tuple[int, int]:
    """מוסיף לסוף history רשומות שיובאו ממערכת חיצונית (למשל הערות/פעילויות
    מבמבי - migrate_bmby.py) עם חותמת הזמן **המקורית** שלהן. append-only (חוק
    בטיחות 6): לא משנה/ממיין/מוחק רשומה קיימת - רק מוסיף בסוף.

    אידמפוטנטי: כל רשומה חייבת "import_ref" יציב (ValueError אחרת); רשומה
    שה-import_ref שלה כבר קיים בכרטיס (או כפול בתוך entries עצמו) מדולגת -
    הרצה חוזרת לעולם לא מכפילה. timestamp חייב להיות ISO מודע-timezone
    (last_contact_at משווה חותמות - ערבוב naive/aware היה מפיל TypeError).
    הכרטיס חייב להתקיים (KeyError אחרת - לא יוצר כרטיס). אטומי פר-כרטיס
    (update_card). מחזיר (appended, skipped_as_duplicate)."""
    for entry in entries:
        if not entry.get("import_ref"):
            raise ValueError("append_imported_history: כל רשומה חייבת import_ref")
        ts = datetime.fromisoformat(entry["timestamp"])
        if ts.tzinfo is None:
            raise ValueError(f"append_imported_history: timestamp בלי timezone: {entry['timestamp']}")
    counts = {"appended": 0, "skipped": 0}

    def _mutate(card: dict, _is_new: bool) -> None:
        history = card.setdefault("history", [])
        seen = {h.get("import_ref") for h in history if h.get("import_ref")}
        for entry in entries:
            if entry["import_ref"] in seen:
                counts["skipped"] += 1
                continue
            history.append(dict(entry))
            seen.add(entry["import_ref"])
            counts["appended"] += 1

    update_card(_customer_key(tenant_id, phone), _mutate)
    return counts["appended"], counts["skipped"]


def update_lead_category(phone: str, category: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מעדכן קטגוריה חופשית לליד (למשל "נדל\"ן"/"פרטי"/"משפחה") - שדה סיווג ידני,
    לא קשור ל-lead_status. category ריק ("") מנקה את השדה. לא נרשם ב-history - זה
    מטא-דאטה כמו import_source, לא אינטראקציה עם הלקוח."""
    def _mutate(card: dict, _is_new: bool) -> None:
        if category:
            card["category"] = category
        else:
            card.pop("category", None)

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def update_lead_agent(phone: str, agent: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מעדכן "סוכן מטפל" (agent) - שדה טקסט חופשי, מטא-דאטה בלבד (בדיוק כמו
    update_lead_category - לא נרשם ב-history, לא קשור ל-lead_status). agent ריק
    ("") מנקה את השדה."""
    def _mutate(card: dict, _is_new: bool) -> None:
        if agent:
            card["agent"] = agent
        else:
            card.pop("agent", None)

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def update_lead_ai_enabled(phone: str, ai_enabled: bool, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מפעיל/מכבה מענה AI אוטומטי לליד ספציפי (שלב 5 - Enterprise Production
    Readiness). ai_enabled=False: הודעות נכנסות ימשיכו להירשם ולחלץ פרטים
    כרגיל (ראו process_message ב-server.py._ai_enabled_for) - רק לא תיווצר/
    תישלח תשובה אוטומטית; מענה ייעשה ידנית בלבד דרך /api/messages/send.
    שדה חסר על כרטיס קיים נחשב True (ברירת המחדל) - ראו _ai_enabled_for,
    כך שאין צורך במיגרציה ל-378 הלידים הקיימים."""
    return update_card(
        _customer_key(tenant_id, phone),
        lambda card, _is_new: card.__setitem__("ai_enabled", bool(ai_enabled)),
        default=_new_card(phone, tenant_id),
    )


def update_lead_voice_extraction(
    phone: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    customer_name: str | None = None,
    property_type: str | None = None,
    budget: str | None = None,
) -> dict:
    """מעדכן כרטיס לקוח משדות שחולצו אוטומטית מהודעה קולית נכנסת
    (transcription.extract_voice_message_fields, gpt-4o-mini) - property_type/
    budget (שדות ממוקדי נדל"ן) תמיד מתעדכנים לערך האחרון שנאמר (לא מצטברים);
    customer_name מתעדכן **רק אם עדיין לא ידוע** - לא דורס שם שכבר קיים בכרטיס
    בגלל חילוץ פחות אמין מתמלול. מטא-דאטה בלבד - לא נרשם ב-history בנפרד (התמלול
    עצמו כבר נרשם כהודעה רגילה דרך db.log_message) ולא נוגע ב-lead_status."""
    def _mutate(card: dict, _is_new: bool) -> None:
        if customer_name and not card.get("customer_name"):
            card["customer_name"] = customer_name
        if property_type:
            card["property_type"] = property_type
        if budget:
            card["budget"] = budget

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def update_lead_status(
    phone: str,
    status: str,
    extra: dict | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
    note: str | None = None,
    direction: str = "out",
    simulated: bool = False,
) -> dict:
    """simulated=True: ה-note (אם יש) מסומן בהיסטוריה כסימולציה (ראו _append_history) -
    לשימוש כשחסימת Twilio Trial מנעה שליחה אמיתית (whatsapp_send.is_trial_restriction),
    אבל עדיין רוצים לתעד את הכוונה ולעדכן סטטוס לצורך בדיקה (ראו reactivate.py).
    status_changed_at מתעדכן בכל קריאה (גם אם הסטטוס בפועל לא השתנה - "מתי עודכן
    לאחרונה", לא "מתי השתנה בפעם הראשונה") - זה הבסיס לטיימר הפולו-אפ האוטומטי
    (scheduler.check_pending_followups: "לא נוצר קשר תוך X שעות מאז שסומן 'נוצר
    קשר'") - שדה חדש, תואם-לאחור: כרטיסים ישנים בלי השדה פשוט לא ייכנסו לטיימר
    עד העדכון הבא שלהם."""
    def _mutate(card: dict, _is_new: bool) -> None:
        card.setdefault("source_channel", source_channel)
        card["lead_status"] = status
        card["status_changed_at"] = datetime.now(timezone.utc).isoformat()
        for k, value in (extra or {}).items():
            if value:
                card[k] = value
        if note:
            _append_history(card, source_channel, note, direction=direction, simulated=simulated)

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def update_lead_fields(
    phone: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    customer_name: str | None = None,
    business_name: str | None = None,
    location: str | None = None,
    lead_status: str | None = None,
    agent: str | None = None,
    category: str | None = None,
    notes: str | None = None,
    client_id: str | None = None,
    email: str | None = None,
    address: str | None = None,
    property_type: str | None = None,
    budget: str | None = None,
    requirements: str | None = None,
    rooms: str | None = None,
    project: str | None = None,
    purpose: str | None = None,
) -> dict:
    """שמירה מלאה מפאנל עריכת ליד (#editLeadPanel) או מהגריד הנגיש-לעריכה
    בתצוגת פרטי הליד המלאה (Lead Details, BMBY-style - ראו index.html) -
    בניגוד ל-import_lead/upsert_customer (שאף פעם לא מוחקים שדה, רק מוסיפים/
    דורסים ערך לא-ריק), זו פונקציית טופס-עריכה מפורש: None = השדה לא נשלח
    בכלל (לא נוגעים בו), "" = המשתמש ניקה את השדה בפועל בטופס - השדה נמחק
    מהכרטיס (מראה update_lead_category/update_lead_agent). לא נוגעת ב-phone
    עצמו - לשינוי מספר טלפון ראו rekey_lead (rekey מלא, לא רק דריסת ערך).
    client_id (שלב 5): מזהה "בעלים" הליד (user_id של משתמש RBAC בתפקיד client)
    לבידוד קשיח - ראו server.py._effective_client_id. הקורא (server.py) אחראי
    להגביל מי מותר לו בכלל להעביר ערך לפרמטר הזה (admin/partner בלבד) - הפונקציה
    כאן לא בודקת הרשאות, כמו כל שאר extract.py.

    email/address/requirements הם שדות חדשים (לא קיימים בשום מקום אחר בפרויקט
    עד כה) - נוספו ללא מיגרציית DB כלשהי כי כרטיס הליד הוא JSON blob גמיש
    (עמודת customers.data) ולא עמודות SQL נפרדות; שדה חסר בכרטיס ישן פשוט
    None/undefined בתגובת ה-API, בלי שום "ברירת מחדל" מטעה. property_type/
    budget היו קיימים כבר (ממולאים אוטומטית ע"י extract_voice_message_fields),
    אבל עד כה לא היו ניתנים לעריכה ידנית בשום UI - כאן זו הדרך הראשונה.

    purpose: "investment" (השקעה) / "residential" (מגורים) - נאסף ע"י בירור
    הצרכים הקצר (sales_agent.run_qualification_turn, ראו server._run_lead_
    qualification_turn) - אותה בדיוק שיטת "" מוחקת/None לא נוגע."""
    fields = (
        ("customer_name", customer_name),
        ("business_name", business_name),
        ("location", location),
        ("agent", agent),
        ("category", category),
        ("notes", notes),
        ("client_id", client_id),
        ("email", email),
        ("address", address),
        ("property_type", property_type),
        ("budget", budget),
        ("requirements", requirements),
        ("rooms", rooms),
        ("project", project),
        ("purpose", purpose),
    )

    def _mutate(card: dict, _is_new: bool) -> None:
        for field, value in fields:
            if value is None:
                continue
            value = value.strip()
            if value:
                card[field] = value
            else:
                card.pop(field, None)
        if lead_status:
            card["lead_status"] = lead_status

    return update_card(_customer_key(tenant_id, phone), _mutate, default=_new_card(phone, tenant_id))


def rekey_lead(old_phone: str, new_phone: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """עורך את מספר הטלפון של ליד קיים - הפעולה היחידה במערכת שמשנה בפועל את
    המפתח הראשי של הליד (_customer_key), ולכן חייבת "לרדוף" אחרי כל מקום ש-
    phone משמש בו כמזהה: מפתח הכרטיס ב-customers.json, וגם עמודת phone בכל
    שלוש טבלאות ה-SQLite (messages/calls/calendar_tasks - ראו db.rekey_phone) -
    אין מזהה ליד מספרי חלופי בשום מקום. דוחה (ValueError) אם new_phone כבר
    שייך לליד *אחר* קיים תחת אותו tenant - מניעת מיזוג-בטעות של שני לידים
    שונים; מיזוג מכוון אינו נתמך כאן."""
    old_phone = old_phone.strip()
    new_phone = new_phone.strip()
    if not new_phone:
        raise ValueError("מספר טלפון חדש לא יכול להיות ריק")

    old_key = _customer_key(tenant_id, old_phone)
    if new_phone == old_phone:
        card = load_customers().get(old_key)
        if card is None:
            raise ValueError("הליד המקורי לא נמצא")
        return card

    new_key = _customer_key(tenant_id, new_phone)

    def _mutate(card: dict) -> dict:
        card["phone"] = new_phone
        return card

    # העברה אטומית של הכרטיס הזה בלבד (בדיקת קיום/התנגשות + מחיקת המפתח הישן
    # + הוספת החדש - באותה נעילה/טרנזקציה), ורק אחריה עמודת phone בטבלאות הלוג
    # (חוק בטיחות 5 - שני החלקים, לא UPDATE חלקי)
    try:
        card = _move_card(old_key, new_key, _mutate)
    except KeyError:
        raise ValueError("הליד המקורי לא נמצא") from None
    except db.CustomerKeyConflict:
        raise ValueError(f"המספר {new_phone} כבר שייך לליד אחר קיים - לא ניתן למזג לידים כאן") from None
    db.rekey_phone(old_phone, new_phone, tenant_id=tenant_id)
    return card


def delete_lead(phone: str, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
    """מוחק ליד לחלוטין - הכרטיס מ-customers.json וגם כל הפעילות הטכנית הנלווית
    (messages/calls/calendar_tasks, ראו db.delete_lead_activity). פעולה הרסנית
    ובלתי הפיכה - האישור האנושי (מודאל אישור) הוא באחריות ה-UI (index.html),
    לא כאן. מחזיר True אם הליד נמצא ונמחק, False אם לא היה קיים מלכתחילה."""
    if not _delete_card(_customer_key(tenant_id, phone)):
        return False
    db.delete_lead_activity(phone, tenant_id=tenant_id)
    return True


def _tenant_cards(tenant_id: str) -> list[tuple[str, dict]]:
    """(מפתח, כרטיס) של tenant אחד בלבד - קידומת המפתח "<tenant>::" וגם
    tenant_id בכרטיס (אם קיים) תואמים. קריאה בלבד דרך load_customers."""
    prefix = f"{tenant_id}::"
    out = []
    for key, card in load_customers().items():
        if not isinstance(card, dict) or not str(key).startswith(prefix):
            continue
        if card.get("tenant_id") not in (None, "", tenant_id):
            continue
        out.append((key, card))
    return out


def _card_status(card: dict) -> str:
    """lead_status של כרטיס, כש-חסר/None/"" = "new" (ליד חדש שלא טופל)."""
    return card.get("lead_status") or "new"


def select_campaign_leads(
    tenant_id: str,
    statuses: list[str] | None = None,
    project: str | None = None,
) -> list[dict]:
    """בחירת לידים קיימים מה-CRM לקמפיין Outbound (במקום העלאת CSV). קריאה
    בלבד - בלי שום כתיבה/תופעת לוואי.

    - רק כרטיסי ה-tenant הזה; כרטיס בלי phone מדולג.
    - statuses: None/ריק = כל הסטטוסים. "new" תואם גם lead_status חסר/None/"".
    - project: None/"" = בלי סינון; אחרת השוואה case-insensitive אחרי strip.
    - phone מוחזר **בדיוק כפי שנשמר בכרטיס** (בלי נרמול) - כדי שהקמפיין
      ירשום חזרה לאותו כרטיס.
    - כפילויות 05x/+9725x (ראו CLAUDE.md): מנורמלים ל-E.164, ונשאר רק הכרטיס
      עם הכי הרבה history - אדם לא יקבל את הקמפיין פעמיים.
    - ממוין לפי status_changed_at יורד (חסר - בסוף)."""
    wanted = {s for s in (statuses or []) if s}
    project_norm = (project or "").strip().casefold()

    best: dict[str, tuple[str, dict]] = {}
    for key, card in _tenant_cards(tenant_id):
        phone = card.get("phone")
        if not phone or not str(phone).strip():
            continue
        if wanted and _card_status(card) not in wanted:
            continue
        if project_norm and str(card.get("project") or "").strip().casefold() != project_norm:
            continue
        try:
            norm = _to_e164(str(phone)) or str(phone)
        except Exception:
            norm = str(phone)
        prev = best.get(norm)
        if prev is None or len(card.get("history") or []) > len(prev[1].get("history") or []):
            best[norm] = (key, card)

    leads = [
        {
            "key": key,
            "phone": card.get("phone"),
            "tenant_id": card.get("tenant_id") or tenant_id,
            "name": card.get("customer_name"),
            "company": card.get("business_name"),
            "project": card.get("project"),
            "lead_status": _card_status(card),
            "ai_enabled": card.get("ai_enabled", True),
            "_changed": card.get("status_changed_at") or "",
        }
        for key, card in best.values()
    ]
    # מיון יציב בשני שלבים: יורד לפי תאריך, ואז חסרי-תאריך לסוף
    leads.sort(key=lambda d: d["_changed"], reverse=True)
    leads.sort(key=lambda d: d["_changed"] == "")
    for d in leads:
        del d["_changed"]
    return leads


def campaign_filter_options(tenant_id: str) -> dict:
    """אפשרויות סינון לדרופדאונים של יצירת קמפיין מה-CRM: ספירה לפי סטטוס
    (חסר = "new"), לפי project (רק ערכים לא ריקים), וסה"כ כרטיסי ה-tenant.
    קריאה בלבד."""
    statuses: dict[str, int] = {}
    projects: dict[str, int] = {}
    project_label: dict[str, str] = {}  # casefold → הכתיב הראשון שנראה ("Nexus"/"nexus" = אפשרות אחת)
    cards = _tenant_cards(tenant_id)
    for _key, card in cards:
        st = _card_status(card)
        statuses[st] = statuses.get(st, 0) + 1
        proj = str(card.get("project") or "").strip()
        if proj:
            label = project_label.setdefault(proj.casefold(), proj)
            projects[label] = projects.get(label, 0) + 1
    return {"statuses": statuses, "projects": projects, "total": len(cards)}


if __name__ == "__main__":
    demo_messages = [
        ("0501234567", "היי, קוראים לי דני, יש לי חנות פרחים בחיפה"),
        ("0501234567", "אגב שכחתי להגיד, החנות נקראת 'פרחי הכרמל'"),
        ("0529876543", "שלום, אני מיכל מהרצליה, מעניין אותי לשמוע פרטים"),
    ]

    for phone, text in demo_messages:
        print(f"\nהודעה נכנסת מ-{phone}: {text}")
        card = process_message(phone, text)
        print("כרטיס לקוח מעודכן:", card)
