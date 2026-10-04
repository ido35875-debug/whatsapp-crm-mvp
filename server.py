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
import json
import logging
import os
import queue as queue_module
import random
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from openpyxl import load_workbook
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Dial, VoiceResponse
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה
# חייב לרוץ לפני ה-import-ים הבאים - db/reactivate/scheduler/extract קוראים os.environ בזמן טעינה

try:
    # כל הייבואים האלה - כולל db/reactivate/scheduler, לא רק extract/whatsapp_send
    # ישירות - תלויים ב-extract.py, שדורש ANTHROPIC_API_KEY בזמן טעינה (os.environ[...]).
    # לכן כל השרשרת חייבת להיות בתוך אותו try/except - אחרת ה-KeyError קורה כבר
    # ב-"import reactivate" למשל, לפני שמגיעים בכלל לבלוק שתופס אותו.
    import db
    import outbound_engine
    import reactivate
    import sales_agent
    import scheduler
    import scheduling_agent
    import prompts
    import tenant_settings
    import transcription
    import voice_call
    from extract import (
        DEFAULT_TENANT_ID,
        delete_lead,
        generate_call_summary,
        generate_executive_summary,
        generate_missing_fields_message,
        get_missing_fields,
        import_lead,
        load_customers,
        log_call_summary,
        log_manual_reply,
        process_message,
        process_message_with_reply,
        rekey_lead,
        resolve_existing_phone,
        update_lead_agent,
        update_lead_ai_enabled,
        update_lead_category,
        update_lead_fields,
        update_lead_status,
        update_lead_voice_extraction,
    )
    # is_trial_restriction נשאר בשימוש ישיר כאן רק עבור /api/calls/start (שיחות
    # קוליות, voice_call.py) - Voice נשאר קשיח מול Twilio תמיד, ללא קשר ל-
    # WHATSAPP_PROVIDER (ראו ההערה בראש whatsapp_provider.py).
    from whatsapp_send import TWILIO_AUTH_TOKEN, is_trial_restriction
    from whatsapp_provider import TwilioProvider, get_provider
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
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "").strip()  # שער אימות ל-/api/* - ראו _require_api_key למטה
# מתג "מצב צל" גלובלי (שלב 6 - סגירת פיילוט, ברירת מחדל false - לא משנה התנהגות
# קיימת). whatsapp_provider.get_provider() כבר עוטף בעצמו את השליחה היוצאת
# (UltraMsg/Green API/Twilio-לא-TwiML) בסימולציה כש-DRY_RUN=true - ה-קבוע כאן
# נחוץ **רק** למסלול Twilio TwiML הסינכרוני (webhook() למטה), ששם "השליחה" היא
# תגובת ה-HTTP עצמה ולא קריאת send_message נפרדת שאפשר לעטוף באותה נקודה.
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"


def _normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    return f"+{digits}" if digits else ""


# רשימת שולחים מאושרים (VIP) - מופרדת בפסיקים, מוגדרת ב-.env/Render (לא בסורס, כי אלה מספרים אישיים).
# ריקה = בלי סינון (ברירת מחדל, ההתנהגות הקיימת). כשמוגדרת - רק השולחים ברשימה מעובדים ומקבלים מענה.
APPROVED_SENDERS = {_normalize_phone(p) for p in os.environ.get("APPROVED_SENDERS", "").split(",") if p.strip()}

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

logger.info(
    "🚀 שרת עולה: WHATSAPP_PROVIDER=%s | DRY_RUN=%s | DATA_DIR=%s | customers.json=%s",
    os.environ.get("WHATSAPP_PROVIDER", "twilio"), DRY_RUN, DATA_DIR, DATA_DIR / "customers.json",
)

if not API_SECRET_KEY:
    # תקלה נפוצה מאוד בפריסה ראשונה בענן (Render/Railway): .env לא מועלה
    # ל-git בכוונה (סודות), כך שמשתני הסביבה חייבים הגדרה ידנית בלוח הבקרה של
    # הפלטפורמה - בלעדיה כל /api/* נחסם (401) והדשבורד מציג "אין חיבור לשרת"
    # (מטעה - השרת חי, ראו index.html:checkServerStatus). אזהרה חד-פעמית כאן,
    # בזמן עליית השרת, כדי שהתקלה תיראה מיד בלוגים של הפלטפורמה - לא רק
    # תתגלה בשקט כשמישהו כבר מדווח "המערכת לא עובדת".
    logger.warning(
        "⚠️ API_SECRET_KEY אינו מוגדר - כל בקשות ה-/api/* ייחסמו (401)! "
        "הגדירו את משתנה הסביבה API_SECRET_KEY (לדוגמה, python -c \"import secrets; print(secrets.token_urlsafe(32))\") "
        "בלוח הבקרה של הפלטפורמה (Render/Railway וכו') ופרסו מחדש."
    )

app = Flask(__name__)
# מאחורי ngrok/reverse-proxy: משקף את ה-scheme/host/port הציבוריים האמיתיים מתוך
# X-Forwarded-* במקום 127.0.0.1 המקומי - קריטי גם לאימות חתימת Twilio (_verify_twilio_request)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


def _rate_limit_key() -> str:
    """מזהה להגבלת קצב - מפתח API אם סופק (מדויק יותר מ-IP: כמה משתמשים/
    שותפים יכולים לשבת מאחורי אותה כתובת, למשל משרד/NAT משותף - לא רוצים
    שאחד "ישרוף" את המכסה של כולם), אחרת כתובת ה-IP האמיתית של הלקוח (דרך
    ProxyFix מעל - קריטי מאחורי ngrok/reverse-proxy, אחרת כל הבקשות היו
    נראות מגיעות מאותה כתובת פנימית). לא קורא ל-_resolve_user (לא רוצה לשלם
    שאילתת DB על כל בקשה רק בשביל מפתח ה-rate-limit - גם מפתח לא-תקין
    מספיק ככינוי ייחודי-לרוב למגביל)."""
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            key = auth_header[len("Bearer "):].strip()
    return key or get_remote_address()


# ---- הגבלת קצב (Rate Limiting - שלב 4) - הגנה מפני DDoS/ניצול לרעה ----
# ברירת מחדל: memory:// (בתוך-process) - כמו תור ה-webhook/מנויי ה-SSE (שלב 3),
# תחת Gunicorn מרובה-workers כל process סופר בנפרד (מכפיל בפועל את המכסה
# האפקטיבית פי מספר ה-workers) - טרייד-אוף מתועד, לא מוסתר. ל-production אמיתי
# מרובה-workers: מגדירים RATELIMIT_STORAGE_URI=redis://... (דורש גם
# `pip install redis`) כדי שכל ה-workers ישתפו מונה אחד אמיתי.
RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "200 per minute")

limiter = Limiter(
    key_func=_rate_limit_key,
    app=app,
    default_limits=[RATELIMIT_DEFAULT],
    storage_uri=RATELIMIT_STORAGE_URI,
    headers_enabled=True,  # X-RateLimit-* בכל תגובה - עוזר ללקוח (ה-UI/אינטגרציות) לדעת כמה נשאר לפני שהוא נחסם
    swallow_errors=True,  # אם ה-storage (Redis) נופל - עדיף לתת לבקשה לעבור (fail-open) מלהפיל את כל השרת בגלל תשתית ניטור
)

_twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None


@app.route("/health")
@limiter.exempt
def api_health():
    """בדיקת תקינות סטנדרטית (שלב 4) - ל-Load Balancer/Orchestrator (Docker
    healthcheck, Render/Railway וכו'). **לא** תחת /api/ בכוונה - _require_api_key
    מדלג במפורש על נתיבים שלא מתחילים ב-/api/ (ראו שם), כך שכלי ניטור חיצוניים
    לא צריכים מפתח כדי לדעת אם השרת חי. פטורה מ-rate limiting (limiter.exempt) -
    פינג תכוף מה-orchestrator (כל כמה שניות) לא אמור להיחסם ולגרום ל"בריא"
    להיראות "מת". לא חושפת נתון עסקי כלשהו - רק סטטוסים טכניים.

    בודקת בפועל (לא רק "התהליך חי"): (1) DB - שאילתה אמיתית (db.health_check,
    לא רק "החיבור נפתח") נגד sqlite/postgres לפי DATABASE_URL; (2) תור עיבוד
    ה-webhook (שלב 3) - שה-worker thread עדיין חי וגודל התור הנוכחי. 200 אם
    הכל תקין, 503 (Service Unavailable) אחרת - כדי ש-orchestrator/LB אמיתי
    ידע להוציא את המופע הזה ממחזור התעבורה, לא רק לוג שקט."""
    db_ok, db_error = True, None
    try:
        db.health_check()
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    healthy = db_ok

    payload = {
        "status": "ok" if healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "db": {
            "ok": db_ok,
            "backend": "postgresql" if db.IS_POSTGRES else "sqlite",
            "error": db_error,
        },
        "async_queue": {
            "processing_now": _job_processing_lock.locked(),
            "queue_size": _webhook_job_queue.qsize(),
        },
    }
    return jsonify(payload), 200 if healthy else 503


def _verify_twilio_request(req) -> bool:
    """מאמת שבקשת ה-webhook הגיעה באמת מ-Twilio, לפי חתימת X-Twilio-Signature (HMAC
    עם ה-Auth Token) - https://www.twilio.com/docs/usage/webhooks/webhooks-security.
    בלי זה, כל אחד שמנחש את כתובת ה-webhook יכול לשלוח בקשות מזויפות."""
    if _twilio_validator is None:
        logger.warning("אימות Twilio מבוקש אבל TWILIO_AUTH_TOKEN לא מוגדר ב-.env - דוחה את הבקשה")
        return False
    signature = req.headers.get("X-Twilio-Signature", "")
    return _twilio_validator.validate(req.url, req.form, signature)


ADMIN_USER = {"user_id": "admin", "name": "מנהל מערכת (מפתח ראשי)", "role": "admin", "tenant_ids": []}


def _resolve_user(provided_key: str | None) -> dict | None:
    """מזהה את המשתמש ששייך למפתח שסופק - שני מקורות: (1) API_SECRET_KEY הראשי
    מ-.env (שלב 6, נשאר כמו שהיה) → "אדמין מובלע" תמיד, לא תלוי בטבלת users
    בכלל (תאימות לאחור מוחלטת); (2) טבלת db.users (שלב 2, RBAC) - partner/
    client/admin נוספים, כל אחד עם מפתח משלו. None אם המפתח לא תואם אף אחד
    מהשניים. השוואת המפתח הראשי ב-secrets.compare_digest (constant-time) -
    לא `==` רגיל, כדי לא לחשוף את אורכו/תוכנו דרך הבדל בזמן תגובה."""
    if not provided_key:
        return None
    if API_SECRET_KEY and secrets.compare_digest(provided_key, API_SECRET_KEY):
        return ADMIN_USER
    return db.get_user_by_api_key(provided_key)


@app.before_request
def _require_api_key():
    """שער אימות + זיהוי-משתמש ל-כל נתיבי /api/* (שלב 6 + שלב 2 - RBAC).
    before_request יחיד לכל הנתיבים - לא decorator בכל endpoint בנפרד, כדי
    שאף נתיב /api/* חדש בעתיד לא "יישכח" בטעות בלי אימות.

    **לא** נוגע ב-GET / (הגשת ה-UI) או ב-/webhook*/voice/* (Twilio/UltraMsg -
    יש להם אימות חתימה משלהם) - אלו כלל לא מתחילים ב-/api/, כך שהבדיקה למטה
    מדלגת עליהם מאליה.

    מקבל את המפתח דרך X-API-Key **או** Authorization: Bearer <key>. המשתמש
    שזוהה נשמר ב-g.current_user לכל משך הבקשה - endpoints שצריכים לסנן לפי
    tenant/role (ראו _effective_tenant_ids) קוראים אותו משם, לא בודקים בעצמם.
    (עד שלב 8: הייתה כאן גם נפילה-חזרה ל-?api_key= ב-query string, נחוצה
    ל-EventSource של ה-SSE הישן (GET /api/events, בוטל לגמרי לטובת polling
    פשוט - ראו pollDashboardUpdates ב-index.html) - כל שאר ה-UI תמיד שולח
    header, לא היה תלוי בה.)

    ⚠️ אם אף מפתח לא זוהה (לא API_SECRET_KEY, לא משתמש RBAC) - חוסם הכל
    (fail-closed), לא פותח את השער בשקט - "אימות שדולג עליו בטעות" הוא בדיוק
    התרחיש שגרם לפער התיעודי המקורי."""
    if not request.path.startswith("/api/"):
        return None

    provided = request.headers.get("X-API-Key")
    if not provided:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer "):].strip()

    user = _resolve_user(provided)
    if not user:
        return jsonify({"error": "Unauthorized - נדרש X-API-Key או Authorization: Bearer <key> תקין"}), 401
    g.current_user = user
    return None


def _require_admin():
    """בודק שהמשתמש הנוכחי הוא admin - endpoints ניהוליים (ראו /api/admin/users)
    קוראים לזה כשורה ראשונה. מחזיר תגובת 403 מוכנה אם לא, אחרת None (המשך רגיל)."""
    if g.current_user["role"] != "admin":
        return jsonify({"error": "הפעולה הזו מוגבלת למנהלי מערכת (admin) בלבד"}), 403
    return None


def _effective_tenant_ids(requested_tenant_id: str | None) -> list[str] | None:
    """קובעת אילו tenant_ids מותר למשתמש הנוכחי לראות בפועל - לב הבידוד
    (Multi-Tenant Isolation, שלב 2). admin: None = בלי הגבלה בכלל (מכבד את מה
    שביקשו, כולל "הראה הכל" כברירת מחדל - זהה להתנהגות הקודמת). partner/
    client: **תמיד** מצטמצם ל-tenant_ids המוקצים למשתמש ב-DB - גם אם ה-query
    param ביקש tenant אחר (או כלום) - כדי שסינון tenant לא יהיה "נוחות UI"
    בלבד שאפשר לעקוף, אלא גבול אמיתי שנאכף בשרת בכל בקשה."""
    user = g.current_user
    if user["role"] == "admin":
        return [requested_tenant_id] if requested_tenant_id else None
    allowed = user.get("tenant_ids") or []
    if requested_tenant_id:
        return [requested_tenant_id] if requested_tenant_id in allowed else []
    return allowed


def _effective_client_id() -> str | None:
    """מזהה client_id לאכיפת בידוד קשיח לתפקיד client (שלב 5 - Enterprise
    Production Readiness) - **בנוסף** לבידוד tenant_id הקיים (שלב 2, ראו
    _effective_tenant_ids) ולא במקומו: tenant_id ממשיך לבטא איזה עסק/ורטיקל
    מדובר; client_id מבטא איזה משתמש RBAC ספציפי הוא "הבעלים" של הליד (הוקצה
    לו דרך /api/leads/update או ביבוא, admin/partner בלבד - ראו שם). None =
    בלי הגבלה נוספת (admin/partner ממשיכים להיות מוגבלים רק לפי tenant_ids
    כבעבר) - client מוגבל **תמיד** ל-client_id == user_id שלו עצמו, בלי יוצא
    מן הכלל ובלי אפשרות לבקש אחרת (כמו tenant_ids, זה גבול שרת, לא נוחות UI)."""
    user = g.current_user
    if user["role"] != "client":
        return None
    return user["user_id"]


def _authorize_lead_access(phone: str, tenant_id: str):
    """בודקת שהמשתמש הנוכחי רשאי לגעת בליד הזה - tenant_id (שלב 2) **וגם**
    client_id (שלב 5) - לא סומכת על כך ש-phone/tenant_id שהתקבלו בבקשה "בטח
    שייכים" לקורא, כמו שנקודות קצה ותיקות יותר נהגו לעשות. שלב 6 (סגירת
    פיילוט): מיושמת גם על /api/leads/status,/category,/agent - לא רק על
    ai-toggle החדש יותר (שלב 5), שממנו הועבר הקוד לכאן במקום להישאר משוכפל.

    מחזירה (card, None) אם מותר - card הוא זה שכבר נטען מ-customers.json, כדי
    שהקורא לא יצטרך לטעון שוב; או (None, response) עם תגובת שגיאה מוכנה
    (404 אם הליד לא קיים בכלל, 403 אם קיים אבל לא בבעלות המשתמש) - הקורא
    בודק `if err: return err` ומחזיר אותה כמו שהיא."""
    card = load_customers().get(f"{tenant_id}::{phone}")
    if not card:
        return None, (jsonify({"error": "ליד לא נמצא"}), 404)
    resolved_tenant_id = card.get("tenant_id", tenant_id)
    allowed_tenant_ids = _effective_tenant_ids(resolved_tenant_id)
    if allowed_tenant_ids is not None and resolved_tenant_id not in allowed_tenant_ids:
        return None, (jsonify({"error": "אין הרשאה לליד הזה"}), 403)
    required_client_id = _effective_client_id()
    if required_client_id is not None and card.get("client_id") != required_client_id:
        return None, (jsonify({"error": "אין הרשאה לליד הזה"}), 403)
    return card, None


def _ai_enabled_for(phone: str, tenant_id: str) -> bool:
    """בודקת אם מענה AI אוטומטי פעיל לליד (שלב 5, ברירת מחדל: True) - נבדקת
    לפני כל החלטה להריץ process_message_with_reply (webhook הסינכרוני של
    Twilio, ו-_process_incoming_message_job ברקע). ליד חדש שאין לו כרטיס
    עדיין (הודעה ראשונה אי-פעם ממנו) מקבל True - "AI פעיל" היא ברירת המחדל
    הרצויה לליד חדש, לא "לא ידוע = כבוי"."""
    card = load_customers().get(f"{tenant_id}::{phone}")
    return bool(card.get("ai_enabled", True)) if card else True


@app.route("/api/me")
def api_me():
    """זהות המשתמש הנוכחי (לפי מפתח ה-API שסופק) - ה-UI קורא לזה בעליית העמוד
    כדי להתאים את עצמו לתפקיד (partner/client לא רואים לשונית B2B Campaigns/
    ניהול-אדמין, למשל) ולמלא את מחשבון העמלות של שותף."""
    user = g.current_user
    return jsonify({
        "user_id": user["user_id"], "name": user["name"], "role": user["role"],
        "tenant_ids": user.get("tenant_ids") or [],
        "commission_rate": user.get("commission_rate"),
        "avg_deal_value": user.get("avg_deal_value"),
    })


@app.route("/api/admin/users", methods=["GET"])
def api_admin_list_users():
    """רשימת משתמשי RBAC (partner/client/admin נוספים) - אדמין-בלבד. לא כולל
    את מפתח האדמין הראשי מ-.env (ADMIN_USER) - הוא לא שורה ב-DB בכלל."""
    denied = _require_admin()
    if denied:
        return denied
    return jsonify(db.get_users())


@app.route("/api/admin/users", methods=["POST"])
def api_admin_create_user():
    """יוצר משתמש RBAC חדש (partner/client/admin נוסף) עם מפתח API ייחודי -
    אדמין-בלבד. מייצר את המפתח בשרת (secrets.token_urlsafe) - לא מקבל מפתח
    מהקליינט, כדי שאי אפשר יהיה "לבחור" מפתח חלש/צפוי. partner/client חייבים
    tenant_ids לא-ריק - אחרת המשתמש נוצר אבל לא יראה שום ליד (_effective_
    tenant_ids מחזיר רשימה ריקה), שזו כמעט תמיד טעות, לא כוונה."""
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip()
    name = (data.get("name") or "").strip()
    role = data.get("role")
    tenant_ids = data.get("tenant_ids") or []
    commission_rate = data.get("commission_rate")
    avg_deal_value = data.get("avg_deal_value")

    if not user_id or not name or role not in ("admin", "partner", "client"):
        return jsonify({"error": "חסר user_id/name, או role לא תקין (admin/partner/client)"}), 400
    if role in ("partner", "client") and not tenant_ids:
        return jsonify({"error": "partner/client חייבים tenant_ids לפחות אחד - אחרת לא יראו שום ליד"}), 400

    api_key = secrets.token_urlsafe(24)
    try:
        db.create_user(
            api_key, user_id, name, role,
            tenant_ids=tenant_ids, commission_rate=commission_rate, avg_deal_value=avg_deal_value,
        )
    except db.IntegrityError:
        return jsonify({"error": f"משתמש עם user_id '{user_id}' כבר קיים"}), 409
    # api_key מוחזר פעם אחת בלבד כאן (תגובת היצירה) - GET /api/admin/users כן
    # מחזיר אותו גם הוא בפועל (ראו db.get_users) כדי שאדמין יוכל לשחזר/למסור
    # אותו שוב מאוחר יותר בלי לאבד גישה - זה נתיב אדמין-בלבד ממילא.
    return jsonify({"ok": True, "user_id": user_id, "api_key": api_key})


@app.route("/api/admin/users/<user_id>", methods=["DELETE"])
def api_admin_delete_user(user_id):
    """מבטלת משתמש RBAC - אדמין-בלבד. המפתח שלו מפסיק לעבוד מיד (get_user_by_
    api_key לא ימצא אותו יותר)."""
    denied = _require_admin()
    if denied:
        return denied
    if not db.delete_user(user_id):
        return jsonify({"error": "משתמש לא נמצא"}), 404
    return jsonify({"ok": True})


def _compute_commission_for_user(user: dict) -> dict:
    """הליבה המשותפת ל-/api/partner/commissions (self) ול-/api/admin/revenue-
    forecast (סכימה על פני כל השותפים) - סופרת לידים שהומרו (contacted/hot/
    reactivation) בקרב ה-tenant_ids המוקצים למשתמש, ומכפילה במחיר-עסקה ממוצע
    ובשיעור העמלה שהוגדרו לו. נוסחה שקופה ופשוטה, לא הערכה חשבונאית אמיתית."""
    allowed_tenant_ids = set(user.get("tenant_ids") or [])
    conversions = 0
    total_leads = 0
    engaged_statuses = {"contacted", "hot", "reactivation"}
    for key, card in load_customers().items():
        tenant_id, _, key_phone = key.partition("::")
        resolved_tenant_id = card.get("tenant_id", tenant_id)
        if resolved_tenant_id not in allowed_tenant_ids:
            continue
        total_leads += 1
        if card.get("lead_status") in engaged_statuses:
            conversions += 1

    commission_rate = user.get("commission_rate") or 0
    avg_deal_value = user.get("avg_deal_value") or 0
    estimated_commission = conversions * avg_deal_value * commission_rate

    return {
        "user_id": user["user_id"], "name": user["name"],
        "tenant_ids": sorted(allowed_tenant_ids),
        "total_leads": total_leads,
        "conversions": conversions,
        "conversion_rate": round((conversions / total_leads) * 100, 1) if total_leads else 0,
        "commission_rate": commission_rate,
        "avg_deal_value": avg_deal_value,
        "estimated_commission": round(estimated_commission, 2),
    }


@app.route("/api/partner/commissions")
def api_partner_commissions():
    """מחשבון עמלות לשותף (partner) - "מחשבון עמלות אישי" שהתבקש. admin/client
    מקבלים 404 - זו יכולת ספציפית לתפקיד partner."""
    user = g.current_user
    if user["role"] != "partner":
        return jsonify({"error": "מחשבון עמלות זמין רק למשתמשי partner"}), 404
    return jsonify(_compute_commission_for_user(user))


@app.route("/api/admin/revenue-forecast")
def api_admin_revenue_forecast():
    """תחזית הכנסה כוללת מעמלות שותפים - אדמין-בלבד. סוכם את _compute_commission_
    for_user על פני כל משתמשי ה-partner הקיימים (ראו לשונית Analytics)."""
    denied = _require_admin()
    if denied:
        return denied
    partners = [u for u in db.get_users() if u["role"] == "partner"]
    breakdown = [_compute_commission_for_user(p) for p in partners]
    total_forecast = round(sum(p["estimated_commission"] for p in breakdown), 2)
    return jsonify({"partners": breakdown, "total_estimated_commission": total_forecast})


@app.route("/api/analytics/timeseries")
def api_analytics_timeseries():
    """נתוני סדרת-זמן יומית ל-Chart.js (לשונית Analytics): נפח שיחות, פגישות/
    משימות שנוצרו, והמרות (לידים שעברו לסטטוס contacted/hot/reactivation) -
    כל אחד סופר לפי היום שבו קרה, לחלון ?days= האחרון (ברירת מחדל 14, מקסימום
    90). מכבד בידוד Multi-Tenant (שלב 2) - partner/client רואים רק את
    ה-tenant_ids שהוקצו להם, בדיוק כמו /api/leads."""
    try:
        days = max(1, min(int(request.args.get("days", 14)), 90))
    except (TypeError, ValueError):
        days = 14
    allowed_tenant_ids = _effective_tenant_ids(request.args.get("tenant_id") or None)

    today = datetime.now(timezone.utc).date()
    date_labels = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    since_date = date_labels[0]

    calls_by_day = db.analytics_daily_counts("calls", allowed_tenant_ids, since_date)
    tasks_by_day = db.analytics_daily_counts("calendar_tasks", allowed_tenant_ids, since_date)

    # המרות - אין לוג-אירועים ייעודי לשינויי סטטוס (רק status_changed_at אחרון
    # על הכרטיס עצמו) - סורקים customers.json וסופרים לפי היום של status_
    # changed_at לכל ליד שהגיע לאחד מסטטוסי המעורבות. קירוב סביר, לא לוג מדויק
    # של כל שינוי היסטורי (אם ליד עבר סטטוס כמה פעמים, רק האחרון נספר).
    conversions_by_day: dict[str, int] = {}
    engaged_statuses = {"contacted", "hot", "reactivation"}
    for key, card in load_customers().items():
        if card.get("lead_status") not in engaged_statuses:
            continue
        tenant_id, _, _ = key.partition("::")
        resolved_tenant_id = card.get("tenant_id", tenant_id)
        if allowed_tenant_ids is not None and resolved_tenant_id not in allowed_tenant_ids:
            continue
        changed_at = card.get("status_changed_at")
        if not changed_at:
            continue
        day = changed_at[:10]
        if day >= since_date:
            conversions_by_day[day] = conversions_by_day.get(day, 0) + 1

    return jsonify({
        "labels": date_labels,
        "calls": [calls_by_day.get(d, 0) for d in date_labels],
        "meetings": [tasks_by_day.get(d, 0) for d in date_labels],
        "conversions": [conversions_by_day.get(d, 0) for d in date_labels],
    })


@app.errorhandler(Exception)
def handle_uncaught_exception(exc):
    # חריגות HTTP רגילות (404/405 וכו') הן זרימה תקינה - למשל הדפדפן מבקש
    # /favicon.ico שלא קיים, או קליינט פוגע בנתיב שגוי. אלו לא "שגיאה לא מטופלת"
    # ולא אמורות להיראות ב-ERROR log/traceback (זה בדיוק מה שהופך את הלוג ללא-
    # שימושי לאיתור באגים אמיתיים) ולא אמורות להפוך ל-500 - מוחזרות כמו שהן,
    # עם קוד הסטטוס וההודעה האמיתיים שלהן.
    if isinstance(exc, HTTPException):
        return jsonify({"error": exc.description}), exc.code
    logger.error("שגיאה לא מטופלת בבקשה ל-%s: %s", request.path, exc, exc_info=True)
    return jsonify({"error": "שגיאת שרת פנימית"}), 500


SUPPORTED_SOURCES = {"whatsapp", "instagram", "facebook"}
CHAT_HISTORY_FILE = DATA_DIR / "chat_history.txt"


def parse_incoming() -> tuple[str, str, str]:
    """מנרמל הודעה נכנסת מכל ערוץ לפורמט אחיד: (contact_id, message_text, source).
    whatsapp (form-encoded של Twilio, או JSON של ספקים כמו Green API) מפוענח דרך
    ה-Provider הפעיל (ראו whatsapp_provider.get_provider) - כל ספק יודע לפענח את
    הפורמט הגולמי שלו, כדי ששאר האפליקציה לא תצטרך לדעת עליו כלום. instagram/
    facebook (PLACEHOLDER גנרי, לא תעבורת API אמיתית) ממשיכים בדיוק כמו קודם -
    JSON עם שדה source מפורש, לא קשור ל-WhatsApp Provider בכלל."""
    source = request.args.get("source", "whatsapp")
    if source not in SUPPORTED_SOURCES:
        source = "whatsapp"

    if request.is_json:
        data = request.get_json(silent=True) or {}
        declared_source = data.get("source", source)
        if declared_source in SUPPORTED_SOURCES and declared_source != "whatsapp":
            # PLACEHOLDER: פורמט גנרי, לא הפורמט האמיתי של Meta Graph API
            return str(data.get("contact_id", "")), data.get("text", ""), declared_source
        source = "whatsapp"  # JSON בלי source אחר מוצהר = הודעת WhatsApp מבוססת-JSON (למשל Green API)

    if source == "whatsapp":
        contact_id, message_text = get_provider().parse_webhook(request)
        return contact_id, message_text, source

    return "", "", source


def _log_incoming_message(tenant_id: str, source: str, contact_id: str, message_text: str) -> None:
    """מוסיף רשומה מסודרת של הודעה נכנסת מליד ל-chat_history.txt (לוג טקסטואלי,
    בנוסף לרישום המובנה בהיסטוריית הכרטיס בתוך customers.json)."""
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] tenant={tenant_id} source={source} from={contact_id}: {message_text}\n"
    with CHAT_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


@app.route("/")
def index():
    """מגיש את הדשבורד, ומזריק את מפתח ה-API הפעיל כקבוע JS (window.__API_KEY__)
    בזמן ההגשה - **לא** שמור בתוך index.html עצמו (קובץ שנמצא ב-git, ראו
    .gitignore) כדי שהמפתח לא ידלוף להיסטוריית ה-repo. עדיין גלוי בפועל לכל
    מי שטוען את הדף (view-source/DevTools) - זו מגבלה מובנית של מפתח סטטי
    בפרונט משותף, לא כשל במימוש: זה חוסם רק בקשות API "עיוורות" (בוטים/
    כלי-סריקה שלא טענו את העמוד כלל), לא תוקף ממוקד שכבר פתח את האתר."""
    html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    injected = f'<script>window.__API_KEY__={json.dumps(API_SECRET_KEY)};</script>'
    html = html.replace("</head>", injected + "</head>", 1)
    return Response(html, mimetype="text/html")


@app.route("/api/leads")
def api_leads():
    """רשימת כל הלידים מ-customers.json (מקור האמת) - לשימוש הדשבורד ב-index.html.
    ?include_last_message=1 (משמש את תצוגת ה-Unified Inbox): מוסיף לכל ליד את ההודעה
    האחרונה מטבלת messages (תצוגה מקדימה + מיון לפי פעילות) - כבוי כברירת מחדל כדי
    לא להאט את טעינת הטבלה הרגילה עם שאילתת DB נוספת לכל שורה.
    ?include_score=1 (משמש את הטבלה הראשית + מסנן טווח הציון): מוסיף "ציון חום"
    מחושב (1-100, ראו db.compute_lead_score) - גם הוא כבוי כברירת מחדל מאותה סיבה
    (עוד כמה שאילתות DB לכל שורה).
    missing_fields (חדש) - תמיד מחושב, ללא opt-in: בניגוד ל-score/last_message
    זו בדיקת dict בזיכרון בלבד (get_missing_fields), בלי שאילתת DB - אין עלות
    ביצועים שמצדיקה כבוי-כברירת-מחדל. משמש לאינדיקטור "פרטים חסרים" בטבלה.

    בידוד Multi-Tenant (שלב 2, RBAC): partner/client מוגבלים בשרת ל-tenant_ids
    שהוקצו להם (ראו _effective_tenant_ids) - לא רק סינון-UI שאפשר לעקוף. admin
    ממשיך לראות הכל, בדיוק כמו קודם.
    בידוד client_id (שלב 5, נוסף על תנאי tenant_id למעלה, לא במקומו): תפקיד
    client מוגבל בנוסף ל-card["client_id"] == user_id שלו עצמו (ראו
    _effective_client_id) - ליד בלי client_id בכלל לא ייחשב "שלו" (לא ברירת
    מחדל פתוחה) אלא אם צוין לו במפורש."""
    include_last_message = request.args.get("include_last_message") == "1"
    include_score = request.args.get("include_score") == "1"
    allowed_tenant_ids = _effective_tenant_ids(request.args.get("tenant_id") or None)
    required_client_id = _effective_client_id()

    leads = []
    for key, card in load_customers().items():
        tenant_id, _, key_phone = key.partition("::")
        phone = card.get("phone", key_phone)
        resolved_tenant_id = card.get("tenant_id", tenant_id)
        if allowed_tenant_ids is not None and resolved_tenant_id not in allowed_tenant_ids:
            continue
        if required_client_id is not None and card.get("client_id") != required_client_id:
            continue
        lead = {
            "tenant_id": resolved_tenant_id,
            "phone": phone,
            "customer_name": card.get("customer_name"),
            "business_name": card.get("business_name"),
            "location": card.get("location"),
            "source_channel": card.get("source_channel"),
            "import_source": card.get("import_source"),
            "lead_status": card.get("lead_status"),
            "category": card.get("category"),
            "agent": card.get("agent"),
            "property_type": card.get("property_type"),
            "budget": card.get("budget"),
            "notes": card.get("notes"),
            "client_id": card.get("client_id"),
            "ai_enabled": card.get("ai_enabled", True),
            "missing_fields": get_missing_fields(card),
        }
        if include_last_message:
            last = db.get_last_message(phone, tenant_id=resolved_tenant_id)
            lead["last_message"] = last["message"] if last else None
            lead["last_message_at"] = last["timestamp"] if last else None
            lead["last_message_direction"] = last["direction"] if last else None
            lead["last_message_simulated"] = bool(last["simulated"]) if last else False
        if include_score:
            lead["score"] = db.compute_lead_score(phone, tenant_id=resolved_tenant_id)
        leads.append(lead)
    return jsonify(leads)


def _requested_client_id(data: dict) -> str | None:
    """שלב 5 (Enterprise Production Readiness): מחלצת client_id מבקשת יצירה/
    עריכת ליד - admin/partner **בלבד** רשאים לקבוע/לנקות אותו. client שמנסה
    לשלוח את השדה הזה בבקשה נחסם בשקט (מתעלמים מהשדה, לא 403 לכל הבקשה - שאר
    השדות עדיין נשמרים כרגיל) - זה בדיוק העיקוף שהבידוד ב-_effective_client_id
    נועד למנוע: client לא יכול "להעביר" ליד לעצמו/לאחר בעצמו.
    None = השדה לא נשלח בבקשה, או שהמשתמש הנוכחי לא מורשה - update_lead_fields
    מתייחס לשניהם כ"לא נוגעים בשדה" (זהה ל-None של כל שדה אחר שם). "" (מחרוזת
    ריקה) מפורשת מ-admin/partner = ניקוי מכוון של השיוך - שונה מ-None בכוונה."""
    if "client_id" not in data:
        return None
    if g.current_user["role"] not in ("admin", "partner"):
        return None
    return (data.get("client_id") or "").strip()


@app.route("/api/leads", methods=["POST"])
def api_create_lead():
    """הוספת ליד בודד ידנית מהדשבורד ("➕ הוסף ליד") - בניגוד ל-/api/leads/import
    (קובץ שלם), כאן שורה אחת. עוטף את import_lead בדיוק כמו הייבוא - אותה לוגיקת
    upsert, בלי כפילות קוד. הטלפון עובר resolve_existing_phone (לא _to_e164 ישירות)
    כדי לא ליצור כרטיס כפול אם הליד כבר קיים בפורמט מקומי - ראו התיעוד שם."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400

    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    status = data.get("lead_status")
    if status not in VALID_LEAD_STATUSES:
        status = None

    card, is_new = import_lead(
        resolve_existing_phone(phone, tenant_id=tenant_id),
        tenant_id=tenant_id,
        customer_name=(data.get("customer_name") or "").strip() or None,
        business_name=(data.get("business_name") or "").strip() or None,
        location=(data.get("location") or "").strip() or None,
        lead_status=status,
        category=(data.get("category") or "").strip() or None,
        agent=(data.get("agent") or "").strip() or None,
        client_id=_requested_client_id(data) or None,
    )
    return jsonify({"ok": True, "card": card, "is_new": is_new})


@app.route("/api/leads/update", methods=["POST"])
def api_update_lead():
    """עריכה מלאה של ליד קיים מפאנל #editLeadPanel - כל השדות בבקשה אחת (בניגוד
    ל-/api/leads/status,/category,/agent הקיימים, ששומרים שדה בודד ב-inline edit).
    תומך גם בשינוי מספר טלפון (new_phone, אופציונלי - ראו extract.rekey_lead) -
    זו הפעולה היחידה שמשנה את מפתח הזיהוי של הליד, ולכן מתבצעת קודם, לפני עדכון
    שאר השדות (שיישמרו תחת המפתח החדש)."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400

    new_phone = (data.get("new_phone") or "").strip()
    if new_phone and new_phone != phone:
        try:
            rekey_lead(phone, new_phone, tenant_id=tenant_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        phone = new_phone

    lead_status = data.get("lead_status")
    if lead_status is not None and lead_status not in VALID_LEAD_STATUSES:
        return jsonify({"error": "סטטוס לא תקין"}), 400

    card = update_lead_fields(
        phone,
        tenant_id=tenant_id,
        customer_name=data.get("customer_name"),
        business_name=data.get("business_name"),
        location=data.get("location"),
        lead_status=lead_status,
        agent=data.get("agent"),
        category=data.get("category"),
        notes=data.get("notes"),
        client_id=_requested_client_id(data),
    )
    return jsonify({"ok": True, "card": card, "phone": phone})


@app.route("/api/leads/delete", methods=["POST"])
def api_delete_lead():
    """מחיקת ליד לצמיתות (extract.delete_lead) - כולל כל ההיסטוריה/הודעות/שיחות/
    משימות. פעולה הרסנית ובלתי הפיכה - אישור אנושי (מודאל) הוא באחריות ה-UI
    (index.html) לפני הקריאה לנתיב הזה."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400

    found = delete_lead(phone, tenant_id=tenant_id)
    if not found:
        return jsonify({"error": "ליד לא נמצא"}), 404
    return jsonify({"ok": True})


@app.route("/api/search")
def api_search():
    """חיפוש חופשי רב-שדות: קודם בודק התאמה ישירה בשדות הכרטיס (שם/עסק/מיקום/טלפון/
    קטגוריה, מ-customers.json - בזיכרון, זול), ובנוסף מחפש בתוכן פעילות (הודעות
    וואטסאפ, הערות/תקציר שיחות, כותרת/הערות משימות - db.search_activity). מחזיר
    איחוד (union) של שתי ההתאמות כרשימת {phone, tenant_id} - הדשבורד מסנן לפיה את
    allLeads הטעון כבר, בלי לשלוף מחדש את כל רשימת הלידים.

    בידוד Multi-Tenant (שלב 2): שתי ההתאמות (כרטיס + db.search_activity) מסוננות
    ל-tenant_ids המותרים למשתמש - אחרת חיפוש היה יכול "לדלוף" קיום/תוכן של ליד
    מ-tenant אחר, גם אם /api/leads הרגיל כבר לא מציג אותו."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    q_lower = q.lower()
    allowed_tenant_ids = _effective_tenant_ids(None)

    matches = set()
    for key, card in load_customers().items():
        tenant_id, _, key_phone = key.partition("::")
        phone = card.get("phone", key_phone)
        resolved_tenant_id = card.get("tenant_id", tenant_id)
        if allowed_tenant_ids is not None and resolved_tenant_id not in allowed_tenant_ids:
            continue
        haystack = " ".join(str(card.get(f) or "") for f in
                             ("customer_name", "business_name", "location", "phone", "category", "agent")).lower()
        if q_lower in haystack:
            matches.add((phone, resolved_tenant_id))

    for phone, tenant_id in db.search_activity(q):
        if allowed_tenant_ids is None or tenant_id in allowed_tenant_ids:
            matches.add((phone, tenant_id))

    return jsonify([{"phone": phone, "tenant_id": tenant_id} for phone, tenant_id in matches])


VALID_LEAD_STATUSES = {"new", "contacted", "hot", "not_relevant", "reactivation"}


@app.route("/api/leads/status", methods=["POST"])
def api_update_lead_status():
    """מעדכן lead_status ידנית מהדשבורד (שורת הטבלה או פאנל ההיסטוריה). זו רק עדכון
    סטטוס ב-customers.json - לא הודעה, ולכן לא נרשם בטבלת messages.
    שלב 6 (סגירת פיילוט): אוכף בעלות (tenant_id+client_id) לפני השינוי -
    ראו _authorize_lead_access."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    status = data.get("status")

    if not phone or status not in VALID_LEAD_STATUSES:
        return jsonify({"error": "טלפון או סטטוס לא תקינים"}), 400
    _, err = _authorize_lead_access(phone, tenant_id)
    if err:
        return err

    card = update_lead_status(phone, status, tenant_id=tenant_id)
    return jsonify({"ok": True, "card": card})


@app.route("/api/leads/category", methods=["POST"])
def api_update_lead_category():
    """מעדכן קטגוריה חופשית לליד (לדוגמה: נדל"ן/פרטי/משפחה) - שדה סיווג ידני,
    לא קשור ל-lead_status. category ריק ("") מנקה את השדה.
    שלב 6: אוכף בעלות - ראו _authorize_lead_access."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    category = (data.get("category") or "").strip()

    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400
    _, err = _authorize_lead_access(phone, tenant_id)
    if err:
        return err

    card = update_lead_category(phone, category, tenant_id=tenant_id)
    return jsonify({"ok": True, "card": card})


@app.route("/api/leads/agent", methods=["POST"])
def api_update_lead_agent():
    """מעדכן "סוכן מטפל" - שדה טקסט חופשי, מטא-דאטה בלבד (כמו /api/leads/category).
    שלב 6: אוכף בעלות - ראו _authorize_lead_access."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    agent = (data.get("agent") or "").strip()

    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400
    _, err = _authorize_lead_access(phone, tenant_id)
    if err:
        return err

    card = update_lead_agent(phone, agent, tenant_id=tenant_id)
    return jsonify({"ok": True, "card": card})


@app.route("/api/leads/ai-toggle", methods=["POST"])
def api_update_lead_ai_enabled():
    """מפעיל/מכבה מענה AI אוטומטי לליד ספציפי (שלב 5 - Enterprise Production
    Readiness) - המתג ב-#panelChat/#inboxChat ב-index.html. כשכבוי: הודעות
    נכנסות ממשיכות להירשם ולחלץ פרטים כרגיל (extract.process_message), אבל
    לא מיוצרת/נשלחת תשובה אוטומטית - ראו _ai_enabled_for וההערה המלאה ב-
    _process_incoming_message_job/webhook. מענה חוזר לפעול ידנית בלבד דרך
    /api/messages/send, בדיוק כמו לפני שהייתה בכלל תשובה אוטומטית.
    אוכף בעלות (tenant_id+client_id) - ראו _authorize_lead_access, שמאז שלב 6
    משמשת גם את /api/leads/status,/category,/agent למעלה, לא רק את הנתיב הזה."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    ai_enabled = data.get("ai_enabled")

    if not phone or not isinstance(ai_enabled, bool):
        return jsonify({"error": "חסר טלפון, או ai_enabled לא תקין (בוליאני)"}), 400
    _, err = _authorize_lead_access(phone, tenant_id)
    if err:
        return err

    card = update_lead_ai_enabled(phone, ai_enabled, tenant_id=tenant_id)
    return jsonify({"ok": True, "card": card})


# ייבוא לידים מ-CSV/Excel - מיפוי אוטומטי של עמודות (בעברית או באנגלית) לשדות הסטנדרטיים
ALLOWED_IMPORT_EXTENSIONS = {".csv", ".xlsx"}
IMPORT_FIELD_ALIASES = {
    "customer_name": {"name", "customer_name", "full name", "שם", "שם לקוח", "שם מלא"},
    "phone": {"phone", "phone_number", "mobile", "טלפון", "מספר טלפון", "נייד", "מס' טלפון"},
    "business_name": {"business", "business_name", "company", "עסק", "שם עסק", "חברה"},
    "location": {"location", "city", "מיקום", "עיר", "אזור"},
    "import_source": {"source", "lead_source", "מקור", "מקור ליד", "ערוץ מקור"},
    "lead_status": {"status", "lead_status", "סטטוס"},
    "category": {"category", "vertical", "קטגוריה", "סיווג"},
    "agent": {"agent", "assigned_agent", "owner", "סוכן", "סוכן מטפל", "נציג"},
    "notes": {"notes", "note", "comments", "הערות", "הערה"},
}

# ייבוא קמפייני B2B (POST /api/campaigns/import, ראו outbound_engine.py) - מיפוי
# נפרד מ-IMPORT_FIELD_ALIASES למעלה: שדות שונים (company/category, לא business_
# name/lead_status/notes וכו' שרלוונטיים ספציפית לכרטיס ליד ב-customers.json).
CAMPAIGN_FIELD_ALIASES = {
    "phone": {"phone", "phone_number", "mobile", "טלפון", "מספר טלפון", "נייד", "מס' טלפון"},
    "name": {"name", "full name", "contact", "שם", "שם איש קשר", "איש קשר"},
    "company": {"company", "business", "organization", "חברה", "עסק", "ארגון"},
    "category": {"category", "vertical", "industry", "קטגוריה", "ענף", "תחום"},
}


def _normalize_header(h) -> str:
    return str(h or "").strip().lower()


def _map_row(raw_row: dict, field_aliases: dict = IMPORT_FIELD_ALIASES) -> dict:
    """ממפה שורת CSV/Excel גולמית (עמודות בכל שם סביר, עברית או אנגלית) לשדות
    התקניים, לפי מילון aliases נתון - משותף לייבוא לידים (IMPORT_FIELD_ALIASES)
    וגם לייבוא קמפייני B2B (CAMPAIGN_FIELD_ALIASES, ראו api_import_campaign)."""
    normalized = {_normalize_header(k): v for k, v in raw_row.items()}
    mapped = {}
    for field, aliases in field_aliases.items():
        for alias in aliases:
            value = normalized.get(_normalize_header(alias))
            if value not in (None, ""):
                mapped[field] = str(value).strip()
                break
    return mapped


def _map_import_row(raw_row: dict) -> dict:
    return _map_row(raw_row, IMPORT_FIELD_ALIASES)


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
            resolve_existing_phone(phone, tenant_id=tenant_id),
            tenant_id=tenant_id,
            customer_name=mapped.get("customer_name"),
            business_name=mapped.get("business_name"),
            location=mapped.get("location"),
            lead_status=status,
            import_source=mapped.get("import_source"),
            category=mapped.get("category"),
            agent=mapped.get("agent"),
            notes=mapped.get("notes"),
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


EXPORT_COLUMNS = [
    "customer_name", "phone", "business_name", "location", "agent", "category",
    "lead_status", "score", "source_channel", "import_source", "tenant_id", "notes",
]


@app.route("/api/leads/export")
def api_export_leads():
    """מייצא את כל הלידים (כולל ציון חום מחושב) ל-CSV נקי - "📤 ייצוא לידים" בדשבורד.
    העמודות תואמות בכוונה למה שנתמך גם בייבוא חזרה (IMPORT_FIELD_ALIASES) - חוץ
    מ-score, שהוא שדה מחושב-נגזר (db.compute_lead_score) ולעולם לא נשמר/מיובא.
    בידוד Multi-Tenant (שלב 2): partner/client מייצאים רק את ה-tenant_ids
    שהוקצו להם - ייצוא הוא בדיוק סוג "דליפת נתונים" שהבידוד נועד למנוע."""
    allowed_tenant_ids = _effective_tenant_ids(request.args.get("tenant_id") or None)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(EXPORT_COLUMNS)
    for key, card in load_customers().items():
        tenant_id, _, key_phone = key.partition("::")
        phone = card.get("phone", key_phone)
        resolved_tenant_id = card.get("tenant_id", tenant_id)
        if allowed_tenant_ids is not None and resolved_tenant_id not in allowed_tenant_ids:
            continue
        score = db.compute_lead_score(phone, tenant_id=resolved_tenant_id)
        writer.writerow([
            card.get("customer_name") or "", phone, card.get("business_name") or "",
            card.get("location") or "", card.get("agent") or "", card.get("category") or "",
            card.get("lead_status") or "", score, card.get("source_channel") or "",
            card.get("import_source") or "", resolved_tenant_id, card.get("notes") or "",
        ])
    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM כדי ש-Excel יפתח עברית נכון
    return Response(
        csv_bytes, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"},
    )


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


@app.route("/api/messages/note", methods=["POST"])
def api_add_note():
    """הערה פנימית של הצוות על הליד - מופיעה בפיד הכרונולוגי (channel="note") לצד
    הודעות הוואטסאפ ותקצירי השיחות, אבל **לעולם לא נשלחת ללקוח** - לא עוברת דרך
    Twilio/send_whatsapp_message בכלל, בניגוד ל-/api/messages/send. משתמשת באותה
    טבלת messages (לא טבלה נפרדת) - ההערה היא עוד סוג רשומה בציר הזמן של הליד,
    לא ישות נפרדת; ה-polling הקיים (pollForNewMessages) מרים אותה אוטומטית."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    text = (data.get("text") or "").strip()
    if not phone or not text:
        return jsonify({"error": "חסר טלפון או תוכן הערה"}), 400
    db.log_message(phone, text, direction="out", tenant_id=tenant_id, channel="note")
    return jsonify({"ok": True})


@app.route("/api/sales/suggest-reply", methods=["POST"])
def api_sales_suggest_reply():
    """סוכן מכירות והתנגדויות (sales_agent.py) - מציע עד 3 ניסוחי מענה + קטגוריית
    התנגדות שזוהתה, להודעה האחרונה **שהתקבלה מהלקוח** (direction="in" - לא סתם
    ההודעה הכרונולוגית האחרונה, שיכולה להיות תגובה אוטומטית שלנו עצמנו). מחליף
    את /api/leads/objection-response הקודם (הצעה בודדת, בלי סיווג) - ראו הערת
    ה-deprecation ב-extract.py.
    **חוק בטיחות #1 (CLAUDE.md):** לא שולח כלום, לא כותב כלום ל-DB, לא סוגר
    עסקה ולא מתחייב על מחיר (נאכף גם בתוך הפרומפט עצמו - ראו sales_agent.py) -
    רק מחזיר טקסטים מוצעים; השליחה בפועל תמיד דרך POST /api/messages/send
    הקיים, בלחיצה אנושית מפורשת על "✅ אישור ושליחה" בדשבורד."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400

    messages = db.get_messages(phone, tenant_id=tenant_id)
    last_inbound = next((m for m in reversed(messages) if m["direction"] == "in"), None)
    if not last_inbound:
        return jsonify({"error": "אין עדיין הודעה נכנסת מהלקוח להתייחס אליה"}), 400

    # הקשר לניסוח - עד 10 ההודעות/אינטראקציות האחרונות (לא ההיסטוריה המלאה כמו
    # בסיכום המנהלים; כאן צריך הקשר קרוב-לרגע, לא סקירה כוללת, ופחות טוקנים
    # לקריאה שקורית תוך כדי שיחה חיה, לא פעם אחת בסופה).
    recent = messages[-10:]
    history_lines = [
        f"[{'לקוח' if m['direction'] == 'in' else 'אנחנו'} · {m['channel']}] {m['message']}"
        for m in recent
    ]

    card = load_customers().get(f"{tenant_id}::{phone}", {})
    result = sales_agent.suggest_replies(
        last_inbound["message"], card, history_text="\n".join(history_lines)
    )
    return jsonify({
        "ok": True,
        "category": result["category"],
        "category_label": result["category_label"],
        "replies": result["replies"],
        "based_on": last_inbound["message"],
    })


@app.route("/api/leads/executive-summary", methods=["POST"])
def api_executive_summary():
    """AI Sales Copilot - סיכום מנהלים ב-3 בולטים של כל ההיסטוריה מול הליד (הודעות
    וואטסאפ + תקצירי שיחות - כל channel, לא רק whatsapp - כדי שסיכום השיחות הטלפוניות
    ייכלל גם הוא)."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400

    messages = db.get_messages(phone, tenant_id=tenant_id)
    if not messages:
        return jsonify({"error": "אין עדיין היסטוריה מתועדת לליד הזה"}), 400

    history_lines = [
        f"[{'לקוח' if m['direction'] == 'in' else 'אנחנו'} · {m['channel']}] {m['message']}"
        for m in messages
    ]
    card = load_customers().get(f"{tenant_id}::{phone}", {})
    summary_text = generate_executive_summary("\n".join(history_lines), card)
    return jsonify({"ok": True, "summary": summary_text})


@app.route("/api/leads/missing-fields/suggest", methods=["POST"])
def api_missing_fields_suggest():
    """מציע הודעת פנייה עדינה לבקשת השלמת פרטים חסרים בכרטיס (ראו extract.
    get_missing_fields/generate_missing_fields_message) - כפתור "🧩 הצע הודעת
    השלמה" בפאנל ה-Timeline. דורש שכבר הייתה אינטראקציה עם הליד (היסטוריה
    לא-ריקה) - אין טעם/זה לא אמור לפנות ליד קר שמעולם לא יצרנו איתו קשר בכלל.
    **לא שולח כלום** - רק מחזיר טקסט מוצע; השליחה בפועל היא אך ורק דרך לחיצה
    אנושית מפורשת על "✅ אישור ושליחה" בדשבורד, שקוראת ל-POST /api/messages/send
    הקיים (אין נתיב שליחה נפרד/מקביל - חוק בטיחות #1 ב-CLAUDE.md)."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400

    card = load_customers().get(f"{tenant_id}::{phone}")
    if not card:
        return jsonify({"error": "הליד לא נמצא"}), 404
    if not card.get("history"):
        return jsonify({"error": "אין עדיין אינטראקציה עם הליד הזה - אין טעם לבקש השלמת פרטים לפני יצירת קשר ראשוני"}), 400

    missing_fields = get_missing_fields(card)
    if not missing_fields:
        return jsonify({"error": "אין שדות חסרים בכרטיס הליד הזה"}), 400

    message_text = generate_missing_fields_message(card, missing_fields)
    return jsonify({"ok": True, "missing_fields": missing_fields, "message": message_text})


@app.route("/api/reactivate", methods=["POST"])
def api_reactivate():
    """מפעיל את קמפיין חימום הלידים הקרים (reactivate.py) מתוך הדשבורד. ברירת המחדל
    היא preview בלבד (send=False, כברירת המחדל של reactivate.py) - לא נשלח שום דבר,
    ומוחזר סינכרונית (מהיר - רק ניסוח הודעות, בלי קריאות Twilio/השהיות).
    שליחה בפועל (send:true) היא האישור האנושי הנדרש (קליק מפורש בממשק אחרי צפייה
    בתצוגה המקדימה) - ראו מדיניות הבטיחות ב-CLAUDE.md. **רצה ברקע (thread נפרד)**,
    לא סינכרונית בתוך הבקשה עצמה: מנוע השליחה המבוקר (reactivate.RATE_LIMIT_*)
    משהה 15-30 שניות רנדומליות בין הודעה להודעה כדי לא להיראות כספאם/בוט מול
    Twilio/WhatsApp - עבור כמה לידים זה יכול לארוך דקות, יותר מדי זמן להחזיק
    בקשת HTTP פתוחה. הנתיב מחזיר מיד `batch_id` - הדשבורד עוקב אחרי ההתקדמות
    דרך GET /api/reactivate/batches/<id> (polling) עד שה-status אינו 'running'.
    days (אופציונלי): סף "לא נוצר קשר מעל X ימים" - אם לא סופק, נופל חזרה למרווח
    המוגדר ל-tenant הזה ב-tenant_settings (ברירת מחדל כללית 30 יום, אבל כל עסק
    יכול להגדיר לעצמו ערך שונה - ראו /api/tenant-settings) - ראו גם
    reactivate.get_cold_leads למה זה לא ברירת המחדל הגלובלית של reactivate.py."""
    data = request.get_json(silent=True) or {}
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    send = bool(data.get("send", False))
    tenant_default_days = tenant_settings.get_reactivation_days(tenant_id)
    try:
        days = int(data.get("days", tenant_default_days))
    except (TypeError, ValueError):
        days = tenant_default_days

    if not send:
        results = reactivate.run_reactivation_campaign(tenant_id=tenant_id, send=False, days=days)
        return jsonify({"send": False, "days": days, "count": len(results), "results": results})

    # send=True: קודם קובעים כמה לידים קרים יש בפועל (בלי לשלוח כלום עדיין) כדי
    # שה-batch יידע מראש את total_leads, ואז מריצים את השליחה עצמה ב-thread נפרד.
    contacts = reactivate.load_contacts()
    cold_leads = reactivate.get_cold_leads(contacts, tenant_id=tenant_id, days=days)
    batch_id = db.create_reactivation_batch(tenant_id, total_leads=len(cold_leads))

    def _run_in_background():
        try:
            reactivate.run_reactivation_campaign(
                tenant_id=tenant_id, send=True, contacts=contacts, days=days, batch_id=batch_id,
            )
        except Exception:
            logger.exception("קמפיין החייאה (batch_id=%s) נכשל ברקע", batch_id)

    threading.Thread(target=_run_in_background, daemon=True, name=f"reactivate-batch-{batch_id}").start()
    return jsonify({"send": True, "days": days, "batch_id": batch_id, "total_leads": len(cold_leads), "status": "started"})


@app.route("/api/reactivate/batches/<int:batch_id>")
def api_get_reactivate_batch(batch_id):
    """מצב חי (polling) של batch שליחה שרץ/רץ ברקע - כולל כל הפריטים שכבר עובדו
    (result: sent/simulated/error) - ראו api_reactivate למעלה."""
    batch = db.get_reactivation_batch(batch_id)
    if not batch:
        return jsonify({"error": "batch לא נמצא"}), 404
    return jsonify(batch)


@app.route("/api/reactivate/batches")
def api_get_reactivate_batches():
    """היסטוריית ה-batches האחרונים (בלי הפריטים המלאים) - לתצוגת "הרצות קודמות"."""
    tenant_id = request.args.get("tenant_id") or None
    return jsonify(db.get_reactivation_batches(tenant_id=tenant_id))


@app.route("/api/reactivate/eligible-count")
def api_reactivate_eligible_count():
    """כמות הלידים הקרים הזמינים כרגע להחייאה, לפי מרווח הימים המוגדר ל-tenant
    הזה (tenant_settings) - לכרטיס "🔥 החייאת לידים" בולט במרכז הבקרה בדשבורד.
    **בכוונה לא קורא ל-run_reactivation_campaign/generate_outreach_message** -
    זה היה מייצר הודעת Claude אמיתית לכל ליד קר רק כדי להציג מספר, על כל טעינת
    דשבורד; get_cold_leads לבדה זולה (בדיקת סטטוס/תאריך בלבד, בלי קריאת AI)."""
    tenant_id = request.args.get("tenant_id") or DEFAULT_TENANT_ID
    days = tenant_settings.get_reactivation_days(tenant_id)
    contacts = reactivate.load_contacts()
    cold_leads = reactivate.get_cold_leads(contacts, tenant_id=tenant_id, days=days)
    return jsonify({"tenant_id": tenant_id, "days": days, "eligible_count": len(cold_leads)})


# ---- קמפייני B2B Outbound (outbound_engine.py) - ייבוא + תור שליחה מבוקר-קצב ----

@app.route("/api/campaigns/import", methods=["POST"])
def api_campaigns_import():
    """מייבא קובץ CSV/XLSX של אנשי קשר B2B ל-outbound_queue (ראו outbound_engine.
    import_campaign_rows) - ממפה עמודות אוטומטית (עברית/אנגלית, CAMPAIGN_FIELD_
    ALIASES) ומנרמל טלפון ל-E.164. **לא שולח שום דבר** - רק ממלא את התור
    (status='queued'); השליחה בפועל דורשת קליק נפרד על POST /api/campaigns/
    <id>/start (חוק בטיחות #1 ב-CLAUDE.md - פנייה יזומה ראשונה תמיד דורשת
    אישור אנושי מפורש, לא רק ייבוא-אוטומטי-ואז-שליחה)."""
    if "file" not in request.files:
        return jsonify({"error": "לא צורף קובץ (שדה 'file')"}), 400

    upload = request.files["file"]
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        return jsonify({"error": f"סוג קובץ לא נתמך: '{ext or 'ללא סיומת'}'. נתמכים: CSV, XLSX"}), 400

    tenant_id = request.form.get("tenant_id") or DEFAULT_TENANT_ID
    message_template = request.form.get("message_template") or None

    try:
        raw_rows = _parse_import_csv(upload.stream) if ext == ".csv" else _parse_import_excel(upload.stream)
    except Exception as exc:
        return jsonify({"error": f"שגיאה בקריאת הקובץ: {exc}"}), 400

    mapped_rows = [_map_row(raw_row, CAMPAIGN_FIELD_ALIASES) for raw_row in raw_rows]
    campaign_id = uuid.uuid4().hex[:12]
    result = outbound_engine.import_campaign_rows(
        mapped_rows, tenant_id=tenant_id, campaign_id=campaign_id, message_template=message_template
    )
    return jsonify({
        "ok": True,
        "campaign_id": campaign_id,
        "total_rows": len(raw_rows),
        "queued": result["queued"],
        "skipped": result["skipped"],
    })


@app.route("/api/campaigns")
def api_campaigns_list():
    """רשימת קמפיינים מקובצת עם ספירות לפי סטטוס (queued/sent/simulated/failed/
    skipped_quota) - ללשונית B2B Campaigns בדשבורד."""
    tenant_id = request.args.get("tenant_id") or None
    return jsonify(db.get_outbound_campaigns(tenant_id=tenant_id))


@app.route("/api/campaigns/<campaign_id>/queue")
def api_campaign_queue(campaign_id):
    """תור השליחה החי של קמפיין ספציפי (כל הפריטים, כל הסטטוסים) - polling
    מה-UI בזמן שהשליחה רצה ברקע, בדיוק כמו GET /api/reactivate/batches/<id>."""
    tenant_id = request.args.get("tenant_id") or None
    return jsonify(db.get_outbound_queue(tenant_id=tenant_id, campaign_id=campaign_id))


@app.route("/api/campaigns/<campaign_id>/start", methods=["POST"])
def api_campaign_start(campaign_id):
    """מפעילה שליחה בפועל של קמפיין - **קליק אנושי מפורש הוא האישור הנדרש**
    (חוק בטיחות #1). רצה ברקע (thread נפרד), לא סינכרונית בתוך הבקשה - מנוע
    ה-Anti-Ban (outbound_engine.MIN/MAX_DELAY_SECONDS) משהה 45-90 שניות
    רנדומליות בין הודעה להודעה, שיכול לארוך דקות/שעות לקמפיין גדול - יותר
    מדי זמן להחזיק בקשת HTTP פתוחה. הנתיב מחזיר מיד; הדשבורד עוקב אחרי
    ההתקדמות דרך GET /api/campaigns/<id>/queue (polling)."""
    data = request.get_json(silent=True) or {}
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID

    pending = db.get_outbound_queue(tenant_id=tenant_id, campaign_id=campaign_id, status="queued")
    if not pending:
        return jsonify({"error": "אין פריטים בסטטוס 'queued' לקמפיין הזה"}), 400

    def _run_in_background():
        try:
            outbound_engine.run_campaign(tenant_id=tenant_id, campaign_id=campaign_id)
        except Exception:
            logger.exception("קמפיין B2B outbound (campaign_id=%s) נכשל ברקע", campaign_id)

    threading.Thread(target=_run_in_background, daemon=True, name=f"outbound-campaign-{campaign_id}").start()
    return jsonify({"ok": True, "campaign_id": campaign_id, "total_queued": len(pending), "status": "started"})


@app.route("/api/tenant-settings")
def api_get_tenant_settings():
    """הגדרות פר-tenant (כרגע רק reactivation_days) - קריאה בלבד, לשימוש פאנל
    ההחייאה בדשבורד (טעינת הערך השמור כברירת מחדל בתיבת "לא נוצר קשר מעל __ ימים")."""
    tenant_id = request.args.get("tenant_id") or DEFAULT_TENANT_ID
    return jsonify({"tenant_id": tenant_id, "reactivation_days": tenant_settings.get_reactivation_days(tenant_id)})


@app.route("/api/tenant-settings", methods=["POST"])
def api_set_tenant_settings():
    """שומר מרווח החייאה מותאם ל-tenant נתון - "כל עסק דורש חוקיות עסקית שונה
    (קמעונאות מול נדל\"ן)": קמעונאות עשוי לרצות סף קצר (למשל 7 ימים), נדל"ן סף
    ארוך יותר (למשל 30-60 יום). לא משפיע על scheduler.py (הסף האוטומטי הקיים
    שם הוא פר-ורטיקל, לא פר-tenant - נשאר נפרד במכוון)."""
    data = request.get_json(silent=True) or {}
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    try:
        days = int(data.get("reactivation_days"))
        if days < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "reactivation_days חייב להיות מספר שלם לא-שלילי"}), 400
    tenant_settings.set_reactivation_days(tenant_id, days)
    return jsonify({"ok": True, "tenant_id": tenant_id, "reactivation_days": days})


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
    provider = get_provider()
    try:
        sid = provider.send_message(phone, message)
    except Exception as exc:
        if not provider.is_soft_failure(exc):
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


@app.route("/api/calls/config-status")
def api_calls_config_status():
    """בדיקת-תקינות מפורשת לתצורת Voice (AGENT_PHONE_NUMBER/PUBLIC_BASE_URL וכו',
    ראו voice_call.get_missing_voice_config) - לפני שמנסים בכלל לחייג, לא רק
    כתגובה לכישלון אחרי ניסיון. ה-UI קורא לזה כדי להציג לנציג מראש אם השיחה
    הקרובה תהיה חיה או תסומן כסימולציה (ראו callConfigBadge ב-index.html) -
    בדיקה קריאה-בלבד, לא נוגעת בכלום."""
    missing = voice_call.get_missing_voice_config()
    return jsonify({"configured": not missing, "missing": missing})


@app.route("/api/calls/start", methods=["POST"])
def api_calls_start():
    """יוזם Click-to-Call (גישור נציג→לקוח). אישור אנושי = הקליק על '📞 שיחה' בדשבורד.
    אם תצורת Voice חסרה ב-.env (ראו voice_call.get_missing_voice_config) - זה ייתפס כאן
    ויתנהג בדיוק כמו is_trial_restriction ב-/api/messages/send: נרשם כ-simulated,
    מוחזר 200 (לא 502), כדי לאפשר להמשיך ולבדוק את שאר הזרימה (הערות → תקציר → כרטיס)."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    if not phone:
        return jsonify({"error": "חסר טלפון"}), 400

    simulated, call_sid, status = False, None, "initiated"
    try:
        call_sid = voice_call.start_bridge_call(phone, tenant_id=tenant_id)
    except RuntimeError as exc:
        logger.warning("שיחת Voice לא הופעלה (תצורה חסרה): %s", exc)
        simulated, status = True, "simulated_no_config"
    except Exception as exc:
        if not is_trial_restriction(exc):
            return jsonify({"error": str(exc)}), 502
        simulated, status = True, "simulated_trial_restriction"

    call_id = db.create_call(phone, tenant_id=tenant_id, call_sid=call_sid, status=status, simulated=simulated)
    response = {"ok": True, "call_id": call_id, "call_sid": call_sid, "simulated": simulated, "status": status}
    if simulated:
        response["warning"] = (
            "שיחה אמיתית לא בוצעה (חסרה תצורת Voice ב-.env, או שחשבון Trial חוסם) - "
            "נרשמה כסימולציה כדי לאפשר להמשיך למילוי הערות ותקציר."
        )
    return jsonify(response)


@app.route("/api/calls/<int:call_id>")
def api_call_get(call_id):
    """מצב שיחה בודדת לפי id - לשימוש polling חי מה-UI (callPanel) בזמן שהגישור
    בעיצומו: status מתעדכן אסינכרונית ע"י Twilio (POST /voice/status, ראו
    db.update_call_status) - הפולינג הזה הוא הדרך שבה הדשבורד "רואה" מעברים
    כמו ringing->in-progress->completed בזמן אמת, לא רק בהנחה אופטימית."""
    call = db.get_call(call_id)
    if not call:
        return jsonify({"error": "שיחה לא נמצאה"}), 404
    return jsonify(call)


@app.route("/api/calls/<int:call_id>/notes", methods=["POST"])
def api_call_notes(call_id):
    """הנציג שולח הערות חופשיות שהקליד אחרי השיחה → Claude מייצר תקציר
    (extract.generate_call_summary) → נשמר ב-calls (audit trail: הערות + תקציר) וגם
    משוקף להיסטוריה/messages (extract.log_call_summary + db.log_message, channel=
    "voice") - כך שהוא מופיע אוטומטית בצ'אט הקיים (פאנל/Inbox) דרך אותו polling
    שכבר קיים, בלי קוד רינדור נוסף. עובד גם על call_id שנוצר במצב simulated - ולכן
    ניתן לבדיקה מלאה גם בלי שיחת Twilio אמיתית."""
    data = request.get_json(silent=True) or {}
    notes = (data.get("notes") or "").strip()
    if not notes:
        return jsonify({"error": "חסרות הערות"}), 400

    call = db.get_call(call_id)
    if not call:
        return jsonify({"error": "שיחה לא נמצאה"}), 404

    phone, tenant_id, simulated = call["phone"], call["tenant_id"], call["simulated"]
    card = load_customers().get(f"{tenant_id}::{phone}", {})
    summary = generate_call_summary(notes, card)

    updated_call = db.save_call_notes_and_summary(call_id, notes, summary)
    log_call_summary(phone, summary, tenant_id=tenant_id, simulated=simulated)
    db.log_message(phone, summary, direction="out", tenant_id=tenant_id, channel="voice", simulated=simulated)

    return jsonify({"ok": True, "call": updated_call, "summary": summary})


@app.route("/api/calls/<int:call_id>/update", methods=["POST"])
def api_call_update(call_id):
    """עריכה ידנית של שיחה קיימת - status/notes/summary, כל שדה אופציונלי בנפרד
    (רק מה שסופק בבקשה מתעדכן, ראו db.update_call). **לא** משוקף להיסטוריית
    הצ'אט (messages/customers.json) - זו תיקון לרשומת השיחה עצמה (מקור האמת
    לפרטי השיחה), לא לתמלול שכבר הוצג/נשלח בזמנו - ראו התיעוד ב-db.update_call."""
    if not db.get_call(call_id):
        return jsonify({"error": "שיחה לא נמצאה"}), 404

    data = request.get_json(silent=True) or {}
    status = data.get("status")
    notes = data.get("notes")
    summary = data.get("summary")
    if status is None and notes is None and summary is None:
        return jsonify({"error": "לא סופק שדה לעדכון (status/notes/summary)"}), 400

    updated = db.update_call(call_id, status=status, notes=notes, summary=summary)
    return jsonify({"ok": True, "call": updated})


@app.route("/api/calls/<int:call_id>/transcribe", methods=["POST"])
def api_call_transcribe(call_id):
    """מתמלל קובץ אודיו שהועלה (transcription.transcribe_audio, OpenAI Whisper API)
    ומחזיר את הטקסט - **לא** שומר כלום בעצמו. ה-UI ממלא את טקסט התמלול לתוך אותה
    תיבת הערות שממנה ייצור התקציר (POST /api/calls/<id>/notes) - כך שהתמלול הוא
    רק דרך חלופית למלא את ההערות, וכל שאר הצינור (שמירה בכרטיס + חותמת זמן +
    היסטוריה) זהה לגמרי לזרימת ההקלדה הידנית הקיימת, בלי קוד כפול. אם
    OPENAI_API_KEY לא מוגדר - מחזיר 400 עם הודעה ברורה, וה-UI חוזר להקלדה ידנית."""
    if not db.get_call(call_id):
        return jsonify({"error": "שיחה לא נמצאה"}), 404

    if "audio" not in request.files:
        return jsonify({"error": "לא צורף קובץ אודיו (שדה 'audio')"}), 400

    upload = request.files["audio"]
    try:
        text = transcription.transcribe_audio(upload.read(), upload.filename or "recording.webm")
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.error("תמלול אודיו נכשל (call_id=%s): %s", call_id, exc, exc_info=True)
        return jsonify({"error": f"תמלול נכשל: {exc}"}), 502

    return jsonify({"ok": True, "text": text})


@app.route("/api/calls/<int:call_id>/transcribe-recording", methods=["POST"])
def api_call_transcribe_recording(call_id):
    """מתמלל את ההקלטה **האמיתית** של השיחה (call.recording_url, ראו /voice/
    recording-status) עם חותמות-זמן פר-משפט - לנגן התמליל האינטראקטיבי בפאנל
    היסטוריית השיחות (קליק על משפט קופץ לנקודת הזמן המתאימה בהקלטה). שונה
    מ-/api/calls/<id>/transcribe (זו לא הכתבה ידנית של קובץ שהועלה - זו ההקלטה
    שכבר קיימת על השיחה עצמה). 400 אם לשיחה הזו אין recording_url בכלל (עדיין
    לא נתמכה - למשל שיחה simulated, או שהוקלטה עדיין לא הגיעה).

    שלב 6 (סגירת פיילוט): מלבד חותמות-הזמן (transcript_segments, לנגן האינטראקטיבי),
    התמליל **גם** משתקף להיסטוריית הליד - תקציר קצר (generate_call_summary, כמו
    ב-/api/calls/<id>/notes) נכתב ל-messages/customers.json (channel="voice") -
    כדי ששיחה שתומללה אוטומטית תופיע בצ'אט/Timeline הרגיל, לא רק בנגן התמליל.
    בכוונה **לא** נוגע ב-calls.notes/summary עצמם (בניגוד ל-/notes) - אלה שדות
    בבעלות זרימת ההקלדה הידנית; אם נציג כבר הקליד הערות לשיחה הזו, לא רוצים
    לדרוס אותן בשקט רק כי מישהו לחץ "תמלל" אחר כך. כשל בשיקוף להיסטוריה (למשל
    Claude API) לא מפיל את הבקשה - התמליל/חותמות הזמן כבר נשמרו בהצלחה למעלה."""
    call = db.get_call(call_id)
    if not call:
        return jsonify({"error": "שיחה לא נמצאה"}), 404
    recording_url = call.get("recording_url")
    if not recording_url:
        return jsonify({"error": "לשיחה הזו אין הקלטה שמורה (recording_url) - אין מה לתמלל"}), 400

    try:
        audio_bytes = transcription.download_twilio_media(recording_url)
        result = transcription.transcribe_audio_with_segments(audio_bytes, "recording.mp3")
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.error("תמלול הקלטה נכשל (call_id=%s): %s", call_id, exc, exc_info=True)
        return jsonify({"error": f"תמלול נכשל: {exc}"}), 502

    updated_call = db.save_transcript_segments(call_id, result["segments"])

    phone, tenant_id, simulated = call["phone"], call["tenant_id"], call["simulated"]
    try:
        card = load_customers().get(f"{tenant_id}::{phone}", {})
        summary = generate_call_summary(result["text"], card)
        log_call_summary(phone, summary, tenant_id=tenant_id, simulated=simulated)
        db.log_message(phone, summary, direction="out", tenant_id=tenant_id, channel="voice", simulated=simulated)
    except Exception as exc:
        logger.warning(
            "שיקוף תמליל שיחה (call_id=%s) להיסטוריית הליד נכשל - התמליל/חותמות הזמן עצמם נשמרו בהצלחה: %s",
            call_id, exc,
        )

    return jsonify({"ok": True, "text": result["text"], "call": updated_call})


VALID_PROMPT_VERTICALS = {"ecommerce", "services", "real_estate"}


@app.route("/api/prompts")
def api_get_prompts():
    """כל תבניות/פרומפטים הסוכנים (Speed-to-Lead, החייאה מותאם-ענף, תיאום פולו-אפ,
    חילוץ פרטים) - לתצוגה/עריכה מה-CRM ("🤖 תבניות וסוכנים"). ראו prompts.py."""
    return jsonify(prompts.get_all_prompts())


@app.route("/api/prompts/<key>", methods=["POST"])
def api_update_prompt(key):
    """מעדכן תבנית פרומפט. vertical (אופציונלי, רק ל-reactivation_outreach -
    ecommerce/services/real_estate): עורך override מותאם-ענף במקום התבנית
    הבסיסית - template ריק עם vertical מוחק את ה-override (חזרה לברירת המחדל)."""
    data = request.get_json(silent=True) or {}
    template = (data.get("template") or "").strip()
    vertical = data.get("vertical") or None
    if vertical and vertical not in VALID_PROMPT_VERTICALS:
        return jsonify({"error": "ורטיקל לא תקין"}), 400
    if not template and not vertical:
        return jsonify({"error": "חסרה תבנית"}), 400

    try:
        entry = prompts.update_prompt(key, template, vertical=vertical)
    except KeyError:
        return jsonify({"error": "תבנית לא נמצאה"}), 404

    return jsonify({"ok": True, "prompt": entry})


@app.route("/api/calls")
def api_calls():
    """יומן שיחות Voice של ליד - בדומה ל-/api/messages, אבל לטבלת calls (שיחה = שורה,
    לא הודעה בודדת)."""
    phone = request.args.get("phone", "")
    tenant_id = request.args.get("tenant_id", DEFAULT_TENANT_ID)
    if not phone:
        return Response(status=400)
    return jsonify(db.get_calls(phone, tenant_id=tenant_id))


VALID_TASK_STATUSES = {"pending", "done", "cancelled"}


@app.route("/api/tasks", methods=["GET", "POST"])
def api_tasks():
    """משימות מעקב/תזכורות (follow-up) - לא קשור להודעות. GET עם ?phone=&tenant_id=
    מחזיר את המשימות של ליד ספציפי (לפאנל "📅 משימות"); GET בלי phone מחזיר את כל
    המשימות (אופציונלית מסונן ב-?tenant_id=&status=, לתצוגת "📅 יומן" הגלובלית).
    POST יוצר משימה חדשה.

    בידוד (שלב 5 - Enterprise Production Readiness, ראו api_leads לאותו רעיון
    בדיוק): db.get_tasks עצמה לא יודעת כלום על RBAC - מסננים כאן, אחרי השליפה,
    לפי tenant_id (שלב 2) וגם client_id (שלב 5, לפי הכרטיס ב-customers.json של
    הליד שהמשימה שייכת לו - למשימות אין client_id משלהן). tenant_id ריק ("כל
    הטננטים") לא נחסם ב-partner/client - מסונן בפועל לפי tenant_ids/client_id
    שהוקצו להם, בדיוק כמו GET /api/leads."""
    if request.method == "GET":
        phone = request.args.get("phone") or None
        tenant_id = request.args.get("tenant_id") or None
        status = request.args.get("status") or None
        tasks = db.get_tasks(phone=phone, tenant_id=tenant_id, status=status)

        allowed_tenant_ids = _effective_tenant_ids(tenant_id)
        if allowed_tenant_ids is not None:
            tasks = [t for t in tasks if t.get("tenant_id") in allowed_tenant_ids]
        required_client_id = _effective_client_id()
        if required_client_id is not None:
            customers = load_customers()
            tasks = [
                t for t in tasks
                if (customers.get(f"{t.get('tenant_id', DEFAULT_TENANT_ID)}::{t.get('phone')}") or {}).get("client_id")
                == required_client_id
            ]
        return jsonify(tasks)

    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    tenant_id = data.get("tenant_id") or DEFAULT_TENANT_ID
    title = (data.get("title") or "").strip()
    due_date = (data.get("due_date") or "").strip()
    due_time = (data.get("due_time") or "").strip() or None
    notes = (data.get("notes") or "").strip() or None

    if not phone or not title or not due_date:
        return jsonify({"error": "חסר טלפון, כותרת או תאריך יעד"}), 400

    task_id = db.create_task(phone, tenant_id=tenant_id, title=title, due_date=due_date, due_time=due_time, notes=notes)
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/tasks/<int:task_id>/status", methods=["POST"])
def api_task_status(task_id):
    """מעדכן סטטוס משימה (pending/done/cancelled) - מהתגית "✓ בוצע"/"✗ בטל" בפאנל
    המשימות או ביומן הגלובלי. **לא** לשימוש על הצעות אוטומטיות (status=
    'pending_confirmation', ראו scheduling_agent.py) - נחסם במפורש (400) כדי
    שלא לעקוף בטעות דרך הכפתור הכללי את הטיפול בפגישה המקורית (related_task_id)
    שקורה בפועל רק דרך /confirm או /reject למטה."""
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in VALID_TASK_STATUSES:
        return jsonify({"error": "סטטוס לא תקין"}), 400

    existing = db.get_task(task_id)
    if existing and existing["status"] == "pending_confirmation":
        return jsonify({"error": "זו הצעה אוטומטית - יש לאשר (/confirm) או לדחות (/reject) אותה, לא לשנות סטטוס ישירות"}), 400

    task = db.update_task_status(task_id, status)
    if not task:
        return jsonify({"error": "משימה לא נמצאה"}), 404
    return jsonify({"ok": True, "task": task})


@app.route("/api/tasks/<int:task_id>/confirm", methods=["POST"])
def api_task_confirm(task_id):
    """מאשרת הצעת פגישה שזוהתה אוטומטית ע"י scheduling_agent - כפתור "✅ אשר"
    ביומן/בפאנל המשימות. זו הפעולה היחידה שמפעילה בפועל הצעה: הופכת אותה
    לפגישה אמיתית (intent="new"), או מבצעת את הביטול/הזזת המועד המבוקשים על
    הפגישה המקורית (intent="cancel"/"reschedule", ראו db.confirm_task_proposal) -
    **תמיד** מקליק נציג מפורש כאן, אף פעם לא אוטומטית (חוק בטיחות #1)."""
    task = db.confirm_task_proposal(task_id)
    if not task:
        return jsonify({"error": "הצעה לא נמצאה, או שכבר טופלה"}), 404
    return jsonify({"ok": True, "task": task})


@app.route("/api/tasks/<int:task_id>/reject", methods=["POST"])
def api_task_reject(task_id):
    """דוחה הצעת פגישה שזוהתה אוטומטית - כפתור "❌ דחה". לא נוגעת בשום פגישה
    קיימת שההצעה הפנתה אליה (related_task_id, אם יש) - רק מסמנת את ההצעה
    עצמה כמבוטלת."""
    task = db.reject_task_proposal(task_id)
    if not task:
        return jsonify({"error": "הצעה לא נמצאה, או שכבר טופלה"}), 404
    return jsonify({"ok": True, "task": task})


VALID_FEEDBACK_TYPES = {"bug", "idea"}
VALID_FEEDBACK_STATUSES = {"new", "in_progress", "done", "wontfix"}


@app.route("/api/feedback", methods=["GET", "POST"])
def api_feedback():
    """משוב על המערכת עצמה (באג/רעיון) - לא קשור ללידים בכלל, פנימי לצוות שמפעיל
    את ה-CRM. GET מחזיר את כל הרשומות (החדש ביותר קודם); POST יוצר רשומה חדשה."""
    if request.method == "GET":
        return jsonify(db.get_feedback())

    data = request.get_json(silent=True) or {}
    feedback_type = data.get("feedback_type")
    description = (data.get("description") or "").strip()
    if feedback_type not in VALID_FEEDBACK_TYPES or not description:
        return jsonify({"error": "סוג משוב או תיאור לא תקינים"}), 400

    feedback_id = db.create_feedback(feedback_type, description)
    return jsonify({"ok": True, "feedback_id": feedback_id})


@app.route("/api/feedback/<int:feedback_id>/status", methods=["POST"])
def api_feedback_status(feedback_id):
    """מעדכן סטטוס טיפול במשוב (new/in_progress/done/wontfix)."""
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in VALID_FEEDBACK_STATUSES:
        return jsonify({"error": "סטטוס לא תקין"}), 400

    feedback = db.update_feedback_status(feedback_id, status)
    if not feedback:
        return jsonify({"error": "משוב לא נמצא"}), 404
    return jsonify({"ok": True, "feedback": feedback})


@app.route("/voice/connect", methods=["POST"])
def voice_connect():
    """TwiML: Twilio מגיע לכאן ברגע שהנציג (הרגל הראשונה של הגישור) עונה. מגשר
    (<Dial>) למספר הלקוח, עם הקלטה (record="record-from-answer") ו-callback כשההקלטה
    מוכנה. אין תמלול אוטומטי - הוחלט במפורש (ראו voice_call.py)."""
    if VERIFY_TWILIO_SIGNATURE and not _verify_twilio_request(request):
        logger.warning("בקשת /voice/connect נדחתה - חתימת Twilio לא תקינה/חסרה")
        return Response(status=403)

    customer_phone = request.args.get("customer_phone", "")
    if not customer_phone:
        return Response(status=400)

    response = VoiceResponse()
    dial = Dial(
        record="record-from-answer",
        recording_status_callback=f"{voice_call.PUBLIC_BASE_URL}/voice/recording-status",
        recording_status_callback_event=["completed"],
    )
    dial.number(customer_phone)
    response.append(dial)
    return Response(str(response), mimetype="text/xml")


@app.route("/voice/status", methods=["POST"])
def voice_status():
    """Status callback לרגל הראשונה של הגשר (הנציג) - CallSid/CallStatus/CallDuration."""
    if VERIFY_TWILIO_SIGNATURE and not _verify_twilio_request(request):
        return Response(status=403)
    call_sid = request.form.get("CallSid", "")
    status = request.form.get("CallStatus", "")
    duration = request.form.get("CallDuration")
    if call_sid and status:
        db.update_call_status(call_sid, status, duration_seconds=int(duration) if duration else None)
    return Response(status=204)


@app.route("/voice/recording-status", methods=["POST"])
def voice_recording_status():
    """הקלטת השיחה המגושרת מוכנה - שומר רק RecordingUrl. אין תמלול/STT אוטומטי
    (הוחלט במפורש - ראו "Recording + הערות ידניות" ב-CLAUDE.md)."""
    if VERIFY_TWILIO_SIGNATURE and not _verify_twilio_request(request):
        return Response(status=403)
    call_sid = request.form.get("CallSid", "")
    recording_url = request.form.get("RecordingUrl", "")
    if call_sid and recording_url:
        db.save_recording_url(call_sid, recording_url)
    return Response(status=204)


# ---- תור עיבוד AI אסינכרוני (שלב 3) ----
# מפריד בין קבלת ה-webhook (מהיר: פענוח + רישום ההודעה הנכנסת + 200 מיידי)
# לבין עיבוד ה-AI (חילוץ פרטים/מענה אוטומטי/זיהוי כוונת תזמון - קריאות Claude,
# יכולות לארוך כמה שניות) - כדי שהספק (UltraMsg/Green API) לא יחכה על webhook
# שתלוי בקריאת AI, ובלי טעם. **לא** חל על Twilio (TwiML): שם התשובה האוטומטית
# חייבת להיכלל בגוף אותה תגובת HTTP עצמה (חוזה ה-webhook של Twilio) - אין דרך
# "להחזיר קודם ולהשלים אחר כך" בלי לשנות את הפרוטוקול לגמרי, אז שם ממשיכים
# סינכרוני בדיוק כמו קודם (ראו requires_twiml_reply למטה).
#
# worker יחיד (לא thread-pool) בכוונה: process_message_with_reply/extract.py
# עושים read-modify-write גולמי על customers.json (בלי נעילת קובץ) - כמה
# threads שכותבים בו-זמנית עלולים "לבלוע" עדכון אחד של השני (lost update).
# תור FIFO עם צרכן יחיד משמר בדיוק את אותה סריאליזציה שכבר קיימת היום (בקשה
# אחרי בקשה) - רק בלי לעכב את תגובת ה-webhook עצמה.
#
# ⚠️ worker תוך-process (thread), לא broker חיצוני (Redis/Celery) - תחת
# Gunicorn עם כמה workers, לכל process יהיה תור/worker נפרדים משלו. זה *לא*
# בעיה כאן (בשונה מ-scheduler.py, שהוא job מחזורי שהיה רץ כפול על כל worker) -
# כל בקשת webhook מטופלת במלואה בתוך אותו worker process שקיבל אותה מלכתחילה,
# אין צורך בתיאום בין processes בכלל.
_webhook_job_queue: "queue_module.Queue" = queue_module.Queue()

# ---- השהיה אקראית לפני שליחת מענה AI אוטומטי (שלב 5) ----
# אנטי-זיהוי-אוטומציה: תשובה שחוזרת תוך שניות בודדות "נראית" בוט מובהק בעיני
# WhatsApp/הלקוח - לא קשור למנגנון ה-Anti-Ban של outbound_engine.py (שם ההשהיה
# היא בין הודעה להודעה ב*קמפיין* יזום ללקוחות חדשים; כאן זו השהיה לפני מענה
# חוזר ל*אותו* לקוח שכבר כתב אלינו - שני מנגנונים נפרדים בכוונה, לא כפילות).
AUTO_REPLY_MIN_DELAY_SECONDS = int(os.environ.get("AUTO_REPLY_MIN_DELAY_SECONDS", "3"))
AUTO_REPLY_MAX_DELAY_SECONDS = int(os.environ.get("AUTO_REPLY_MAX_DELAY_SECONDS", "7"))


def _send_auto_reply_after_delay(provider, contact_id: str, reply_text: str, tenant_id: str) -> None:
    """שולחת תשובה אוטומטית אחרי השהיה רנדומלית (שלב 5) - רצה ב-thread נפרד
    משלה (daemon, חד-פעמי) שנוצר ונשכח לכל הודעה - **לא** על ה-worker היחיד
    של תור עיבוד ה-webhook (ראו הערת head-of-line blocking בקריאה למעלה).
    ה-reply_text כבר נוצר, נרשם ב-customers.json/messages ופורסם ל-UI לפני
    הקריאה הזו (ראו _process_incoming_message_job) - זו רק פעולת ה-API
    החיצונית בפועל מול הספק, שמותר לה להתעכב בלי להשפיע על שום דבר אחר."""
    time.sleep(random.randint(AUTO_REPLY_MIN_DELAY_SECONDS, AUTO_REPLY_MAX_DELAY_SECONDS))
    try:
        provider.send_message(contact_id, reply_text)
    except Exception as exc:
        logger.warning(
            "שליחת תשובה אוטומטית נכשלה אחרי השהיה (ספק=%s, tenant=%s): %s",
            type(provider).__name__, tenant_id, exc,
        )


def _process_incoming_message_job(contact_id: str, message_text: str, tenant_id: str, source: str) -> None:
    """מעבד הודעה נכנסת אחת ברקע - חילוץ פרטים/מענה אוטומטי, זיהוי כוונת
    תזמון, ושליחת התשובה בפועל (לספקים בלי TwiML). רץ בתוך ה-worker thread
    (ראו _drain_webhook_queue) - **לא** תלוי ב-Flask request context (אין כאן
    גישה ל-request/g), רק בפרמטרים שהועברו במפורש בזמן ה-enqueue. הלוגיקה
    עצמה זהה לגמרי למה שהיה סינכרוני בתוך webhook() לפני שלב 3 - הופרדה
    לפונקציה, לא שוכתבה."""
    provider = get_provider() if source == "whatsapp" else None
    reply_text = None
    try:
        if source == "whatsapp" and _ai_enabled_for(contact_id, tenant_id):
            # תגובה בתוך חלון השיחה שהלקוח פתח - לא הודעה יזומה - ולכן אינה
            # דורשת הודעת-תבנית מאושרת מול מטא.
            card, reply_text = process_message_with_reply(
                contact_id, message_text, tenant_id=tenant_id, source_channel=source
            )
            db.log_message(contact_id, reply_text, direction="out", tenant_id=tenant_id, channel=source)
        else:
            # ai_enabled=False (שלב 5) - עדיין מחלצים פרטים/מעדכנים כרטיס, אבל
            # בלי לייצר/לשלוח תשובה אוטומטית (reply_text נשאר None) - מענה
            # ידני בלבד דרך /api/messages/send. אותה פונקציה בדיוק שכבר
            # משמשת לערוצים בלי מענה אוטומטי בכלל (instagram/facebook) - לא
            # קוד כפול.
            card = process_message(contact_id, message_text, tenant_id=tenant_id, source_channel=source)
        logger.info(
            "🧠 חילוץ נתונים הושלם [תור רקע, tenant=%s]: טלפון=%s | שם=%r | עסק=%r | מיקום=%r",
            tenant_id, contact_id, card.get("customer_name"), card.get("business_name"), card.get("location"),
        )
        if reply_text:
            logger.info("💬 מענה AI נוצר (ייתרכב/יישלח אחרי השהייה): %r", reply_text[:300])
        logger.info("[תור רקע][tenant=%s] [%s] עודכן כרטיס לקוח: %s", tenant_id, source, card)
    except Exception as exc:
        logger.error("שגיאה בעיבוד הודעה ברקע מ-%s (tenant=%s): %s", source, tenant_id, exc, exc_info=True)

    # סוכן יומן (scheduling_agent.py) - מנתח את ההודעה ומזהה בקשת תיאום/ביטול/
    # הזזת פגישה. try/except צר משלו, נפרד מהבלוק למעלה - כשל כאן לעולם לא
    # אמור להשפיע על עיבוד ההודעה/התשובה האוטומטית. כותב לכל היותר שורת
    # **הצעה** ל-calendar_tasks (status="pending_confirmation") - אף פעם לא
    # משנה פגישה קיימת ישירות (ראו אזהרת הבטיחות ב-scheduling_agent.py).
    try:
        prior_messages = db.get_messages(contact_id, tenant_id=tenant_id)[:-1][-6:]
        history_text = "\n".join(
            f"[{'לקוח' if m['direction'] == 'in' else 'אנחנו'} · {m['channel']}] {m['message']}"
            for m in prior_messages
        )
        intent_result = scheduling_agent.detect_scheduling_intent(message_text, history_text=history_text)
        if intent_result["has_intent"]:
            related_task = None
            if intent_result["intent"] in ("cancel", "reschedule"):
                related_task = scheduling_agent.find_related_task(contact_id, tenant_id)
            scheduling_agent.create_scheduling_proposal(
                contact_id, tenant_id, intent_result,
                related_task_id=related_task["id"] if related_task else None,
            )
    except Exception as exc:
        logger.warning("זיהוי כוונת תזמון (scheduling_agent, רקע) נכשל: %s", exc)

    # ספקים בלי TwiML (UltraMsg/Green API/Mock) - התשובה האוטומטית נשלחת
    # כקריאת API יזומה נפרדת, לא inline בתגובת ה-webhook (זו כבר נשלחה מזמן).
    # כשל בשליחה נרשם כאזהרה - ההודעה הנכנסת כבר נרשמה בכל מקרה לפני ה-enqueue.
    # ההשהיה האקראית (שלב 5, אנטי-זיהוי-אוטומציה - תשובה שמגיעה תוך אלפיות
    # שנייה "נראית" רובוטית) רצה ב-thread נפרד משלה, **לא** כאן על ה-worker
    # של תור העיבוד (_drain_webhook_queue) - אחרת השהיה של עד 75
    # שניות הייתה חוסמת מאחוריה כל הודעה אחרת שממתינה בתור, בדיוק ה"ראש תור"
    # (head-of-line blocking) שהתור האסינכרוני כולו (שלב 3) נועד למנוע.
    if source == "whatsapp" and reply_text and provider is not None:
        threading.Thread(
            target=_send_auto_reply_after_delay,
            args=(provider, contact_id, reply_text, tenant_id),
            daemon=True, name=f"auto-reply-delay-{contact_id}",
        ).start()


_job_processing_lock = threading.Lock()


def _drain_webhook_queue() -> None:
    """מעבד את תור ה-webhook עד שהוא ריק, ואז מסיים. נקרא מ-thread קצר שנוצר לכל
    הודעה נכנסת - אין worker קבוע שיכול למות בשקט. הנעילה מבטיחה שעיבוד אחד בכל
    פעם (כמו קודם), כך ש-customers.json לא ייכתב בו-זמנית."""
    while True:
        try:
            job = _webhook_job_queue.get_nowait()
        except queue_module.Empty:
            return
        with _job_processing_lock:
            try:
                _process_incoming_message_job(*job)
            except BaseException:
                logger.exception("job בתור עיבוד ה-webhook נכשל באופן בלתי-צפוי")
            finally:
                _webhook_job_queue.task_done()


def _kick_webhook_drain() -> None:
    threading.Thread(target=_drain_webhook_queue, daemon=True, name="webhook-drain").start()


@app.route("/api/system/queue-status")
def api_system_queue_status():
    """גודל תור עיבוד ה-webhook כרגע + האם ה-worker thread חי - אדמין-בלבד,
    לניטור/דיבוג (ראו _webhook_job_queue/_drain_webhook_queue)."""
    denied = _require_admin()
    if denied:
        return denied
    return jsonify({
        "queue_size": _webhook_job_queue.qsize(),
        "processing_now": _job_processing_lock.locked(),
    })


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<tenant_id>", methods=["POST"])
# מכסה נפרדת ומחמירה יותר מברירת המחדל הגלובלית (RATELIMIT_DEFAULT): זה
# הנתיב החשוף ביותר במערכת - ספקים בלי חתימת HMAC (UltraMsg/Green API, ראו
# ההערה על _verify_twilio_request למטה) סומכים אך ורק על כך שכתובת ה-webhook
# עצמה סודית, ולא על מפתח API (הנתיב הזה לא תחת /api/). כל בקשה שעוברת גם
# עולה כסף בפועל (קריאת Claude ברקע, ראו _webhook_job_queue) - לא רק עומס.
@limiter.limit(os.environ.get("RATELIMIT_WEBHOOK", "60 per minute"))
def webhook(tenant_id: str = DEFAULT_TENANT_ID):
    """⚠️ חוזה חובה כלפי הספק (שלב 8 - הקשחת webhook מפני עומס/backoff): כל
    מה שבתוך ה-try למטה **תמיד** מחזיר 2xx, גם כשאין מה לעבד (קבוצה, סוג
    webhook לא-הודעה) וגם בכשל בלתי-צפוי - ה-except התחתון תופס הכל. ספקים
    כמו Green API/UltraMsg נכנסים ל-exponential backoff על כל תגובה שאינה
    2xx; עם נפח הודעות אמיתי (כולל webhook-ים שאינם הודעה - סטטוסים וכו',
    וכולל רעש מקבוצות וואטסאפ) 4xx/5xx תכופים הם בדיוק מה שגורם ל"עומס
    קריטי"/קריסות - לא עצם קבלת ההודעות. 403 (חתימת Twilio) הוא היוצא מן
    הכלל המודע היחיד - דחייה אבטחתית מכוונת, לא "שגיאה" שצריך להסתיר."""
    try:
        # כל עסק (tenant) מקבל כתובת webhook משלו עם ה-tenant_id שלו בנתיב, למשל:
        # https://<ngrok-url>/webhook/business_a - כך שהנתונים של כל עסק מבודדים זה מזה.

        # parse_incoming קובע את ה-source הסופי (כולל override מגוף ה-JSON, ראו שם) -
        # רק אחריו יודעים אם בכלל רלוונטי לבדוק Provider/חתימת Twilio. parse_incoming
        # עצמו לא כותב כלום (לא DB, לא לוג) - בטוח לקרוא לו לפני אימות החתימה.
        raw_json = request.get_json(silent=True)
        logger.info(
            "📥 webhook הגיע: WHATSAPP_PROVIDER=%s | content-type=%s | JSON גולמי: %s",
            os.environ.get("WHATSAPP_PROVIDER", "twilio"), request.content_type,
            json.dumps(raw_json, ensure_ascii=False)[:2000] if raw_json is not None else repr(request.get_data()[:500]),
        )

        contact_id, message_text, source = parse_incoming()
        if APPROVED_SENDERS and contact_id and _normalize_phone(contact_id) not in APPROVED_SENDERS:
            logger.info("🚷 שולח לא מאושר (APPROVED_SENDERS) - דולג בלי מענה: %s", f"{contact_id[:4]}…{contact_id[-4:]}")
            return Response(status=200)
        logger.info(
            "🔎 חילוץ מההודעה: מספר/מזהה שולח=%r | טקסט=%r | source=%s | typeWebhook=%r",
            contact_id, (message_text or "")[:200], source,
            (raw_json or {}).get("typeWebhook") if isinstance(raw_json, dict) else None,
        )
        provider = get_provider() if source == "whatsapp" else None

        # אימות Twilio חל רק כש-source==whatsapp וה-Provider הפעיל הוא בפועל Twilio,
        # על בקשות בפורמט Twilio האמיתי (form-encoded) - לא על ה-JSON הגנרי של
        # instagram/facebook (PLACEHOLDER) ולא על ספקים אחרים (Green API/Mock, שאין
        # להם מנגנון חתימת HMAC כזה - הם סומכים על כך שכתובת ה-webhook עצמה סודית,
        # ראו WhatsAppProvider.verify_webhook).
        if isinstance(provider, TwilioProvider) and VERIFY_TWILIO_SIGNATURE and not request.is_json:
            if not _verify_twilio_request(request):
                logger.warning(
                    "בקשת webhook נדחתה - חתימת X-Twilio-Signature לא תקינה/חסרה (מ-%s, tenant=%s)",
                    request.remote_addr, tenant_id,
                )
                return Response(status=403)

        # הודעה קולית נכנסת (WhatsApp voice note): מגיעה בלי טקסט, רק מדיה - בלי
        # הטיפול הזה message_text היה נשאר ריק וההודעה כולה נדחית למטה, בלי
        # להירשם בשום מקום ("נעלמת" מה-Inbox). provider.extract_voice_media מנרמל
        # את הפורמט הגולמי הספציפי-ספק (form-encoded אצל Twilio, JSON אצל Green API)
        # למבנה אחיד {"media_url","content_type"} - ראו whatsapp_provider.py; מכאן
        # transcription.transcribe_incoming_voice_message מתמלל אוטומטית (OpenAI
        # Whisper) ומשתמש בטקסט המתומלל בדיוק כמו הודעת טקסט רגילה מכאן והלאה - אותו
        # צינור process_message_with_reply/db.log_message, בלי קוד כפול, לכל ספק.
        # אם התמלול עצמו נכשל (OPENAI_API_KEY לא מוגדר/לא תקין, שגיאת רשת/API) -
        # עדיין מקבלים טקסט placeholder ברור (⚠️) במקום None, כדי שההודעה עדיין
        # תירשם ותופיע ב-Inbox עם סימון שקרה כשל, לא תיעלם בשקט.
        if not message_text and provider is not None:
            voice_media = provider.extract_voice_media(request)
            voice_result = transcription.transcribe_incoming_voice_message(voice_media, provider.download_media)
            if voice_result:
                if voice_result["success"]:
                    message_text = f"🎙️ {voice_result['text']}"
                    # חילוץ שדות אוטומטי (שם/סוג נכס/תקציב - ממוקד נדל"ן, ראו "חזון
                    # המוצר" ב-CLAUDE.md) מהתמלול, עם gpt-4o-mini - רק אחרי תמלול
                    # מוצלח (אין טעם לנתח טקסט placeholder של כישלון). כשל בשלב הזה
                    # לא אמור להפיל את כל הטיפול בהודעה - היא כבר תירשם כרגיל בהמשך
                    # גם אם חילוץ השדות נכשל.
                    if contact_id:
                        try:
                            fields = transcription.extract_voice_message_fields(voice_result["text"])
                            update_lead_voice_extraction(
                                contact_id, tenant_id=tenant_id,
                                customer_name=fields.get("customer_name"),
                                property_type=fields.get("property_type"),
                                budget=fields.get("budget"),
                            )
                        except Exception as exc:
                            logger.warning(
                                "חילוץ שדות אוטומטי מהודעה קולית נכשל (התמלול עצמו הצליח): %s", exc,
                            )
                else:
                    message_text = voice_result["text"]

        if not contact_id or not message_text:
            logger.warning(
                "⏭️ webhook דולג (לא נוצר ליד): contact_id=%r, טקסט ריק=%s, typeWebhook=%r. "
                "אם זו הודעה אמיתית מלקוח - ה-WHATSAPP_PROVIDER או מבנה ה-JSON לא תואמים לספק הפעיל.",
                contact_id, not message_text,
                (raw_json or {}).get("typeWebhook") if isinstance(raw_json, dict) else None,
            )
            # אין מה לעבד - לא רק קלט ריק/שגוי: גם webhook-ים שאינם הודעה נכנסת
            # בכלל (סטטוס שליחה וכו', ראו GreenAPIProvider/UltraMsgProvider.
            # parse_webhook) וגם הודעות קבוצה שסוננו שם בכוונה (@g.us). 200, לא
            # 400 - ראו docstring הפונקציה: תגובה שאינה 2xx היא בדיוק מה שמכניס
            # את הספק ל-backoff, וזה נפוץ-לגמרי, לא מקרה שגיאה.
            return Response(status=200)

        _log_incoming_message(tenant_id, source, contact_id, message_text)
        db.log_message(contact_id, message_text, direction="in", tenant_id=tenant_id, channel=source)

        # Twilio (TwiML): התשובה האוטומטית *חייבת* להיכלל בגוף אותה תגובת HTTP -
        # אין אפשרות להחזיר 200 מיידי ולהשלים ברקע בלי לשבור את חוזה ה-webhook של
        # Twilio (ראו ההערה המלאה מעל _webhook_job_queue). ממשיכים סינכרוני, בדיוק
        # כמו לפני שלב 3 - שום שינוי התנהגות בנתיב הזה.
        if source == "whatsapp" and provider.requires_twiml_reply():
            reply_text = None
            try:
                if _ai_enabled_for(contact_id, tenant_id) and not DRY_RUN:
                    card, reply_text = process_message_with_reply(
                        contact_id, message_text, tenant_id=tenant_id, source_channel=source
                    )
                    db.log_message(contact_id, reply_text, direction="out", tenant_id=tenant_id, channel=source)
                else:
                    # ai_enabled=False (שלב 5) או DRY_RUN=true (שלב 6) - בלי מענה
                    # אוטומטי; ה-TwiML למטה יחזור ריק (בלי <Message>), בדיוק כמו
                    # שהיה קורה אם לא הייתה תשובה בכלל. ל-Twilio אין "שלח אבל אל
                    # תשלח באמת" - התגובה הסינכרונית עצמה היא השליחה, ולכן ב-
                    # DRY_RUN פשוט לא מייצרים תשובה בכלל (בניגוד ל-UltraMsg/Green
                    # API, ששם DRY_RUN עדיין מייצר תשובה מדומה שנראית בהיסטוריה -
                    # ראו DryRunProviderProxy ב-whatsapp_provider.py). מענה ידני
                    # בלבד דרך /api/messages/send.
                    card = process_message(contact_id, message_text, tenant_id=tenant_id, source_channel=source)
                logger.info("[tenant=%s] [%s] עודכן כרטיס לקוח: %s", tenant_id, source, card)
            except Exception as exc:
                logger.error("שגיאה בעיבוד הודעה מ-%s (tenant=%s): %s", source, tenant_id, exc, exc_info=True)

            try:
                prior_messages = db.get_messages(contact_id, tenant_id=tenant_id)[:-1][-6:]
                history_text = "\n".join(
                    f"[{'לקוח' if m['direction'] == 'in' else 'אנחנו'} · {m['channel']}] {m['message']}"
                    for m in prior_messages
                )
                intent_result = scheduling_agent.detect_scheduling_intent(message_text, history_text=history_text)
                if intent_result["has_intent"]:
                    related_task = None
                    if intent_result["intent"] in ("cancel", "reschedule"):
                        related_task = scheduling_agent.find_related_task(contact_id, tenant_id)
                    scheduling_agent.create_scheduling_proposal(
                        contact_id, tenant_id, intent_result,
                        related_task_id=related_task["id"] if related_task else None,
                    )
            except Exception as exc:
                logger.warning("זיהוי כוונת תזמון (scheduling_agent) נכשל: %s", exc)

            body = f"<Message>{escape(reply_text)}</Message>" if reply_text else ""
            twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"
            return Response(twiml, mimetype="text/xml")

        # כל שאר הספקים/הערוצים (UltraMsg/Green API/Mock, instagram/facebook
        # placeholder) - עיבוד ה-AI לא צריך להיכלל בתגובה הזו בכלל, אז מעבירים
        # ל-worker ברקע (שלב 3) ומחזירים 200 מיידית. ההודעה הנכנסת כבר נרשמה
        # למעלה בכל מקרה - גם אם ה-job ברקע ייכשל, שום דבר לא "נעלם".
        _kick_webhook_drain()
        _webhook_job_queue.put((contact_id, message_text, tenant_id, source))
        return Response(status=200)
    except Exception as exc:
        # רשת ביטחון אחרונה (שלב 8): כל חריגה בלתי-צפויה שלא נתפסה למעלה (באג
        # בפענוח, DB זמנית לא זמינה וכו') עדיין מחזירה 200, לא 500 - ה-
        # errorhandler הגלובלי (handle_uncaught_exception למעלה בקובץ) היה
        # מחזיר 500 כאן, וזה בדיוק מה שמכניס ספקים ל-backoff. ה-exc_info=True
        # מבטיח שהחריגה האמיתית עדיין נראית בלוגים לאבחון - "להחזיר 200"
        # אף פעם לא אומר "להסתיר את השגיאה", רק "לא להעניש את הספק עליה".
        logger.error("שגיאה בלתי-צפויה ב-webhook (tenant=%s): %s", tenant_id, exc, exc_info=True)
        return Response(status=200)


if __name__ == "__main__":
    logger.info(
        "מפעיל שרת על %s:%s (debug=%s, verify_twilio_signature=%s, log_level=%s)",
        HOST, PORT, FLASK_DEBUG, VERIFY_TWILIO_SIGNATURE, LOG_LEVEL,
    )
    scheduler.start()
    app.run(host=HOST, port=PORT, debug=FLASK_DEBUG, use_reloader=False)
