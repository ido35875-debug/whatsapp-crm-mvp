"""
מנוע קמפייני B2B Outbound - פנייה ראשונה יזומה לאנשי קשר *חדשים* שיובאו
מקובץ CSV/Excel (ראו POST /api/campaigns/import ב-server.py), עם מנגנון
Anti-Ban: השהיה רנדומלית בין הודעה להודעה + מכסה יומית פר-tenant.

שונה במכוון מ-reactivate.py: זו לא "החייאת ליד קיים ב-customers.json" - אלו
אנשי קשר חדשים לגמרי, אולי אף פעם לא היה איתם קשר. outbound_queue (db.py)
היא טבלה עצמאית, לא reactivation_batches/reactivation_batch_items.

⚠️ חוק בטיחות קשיח (CLAUDE.md #1): import_campaign_rows רק *ממלא את התור*
(status="queued") - לא שולח שום דבר, לא ניגש ל-provider בכלל. שליחה בפועל
קורית אך ורק דרך run_campaign, שנקראת רק מ-thread נפרד שמתחיל ב-POST
/api/campaigns/<id>/start (server.py) - קליק אנושי מפורש, בדיוק כמו קמפיין
ההחייאה (preview-then-send דו-שלבי). פנייה יזומה ראשונה ב-WhatsApp מחוץ
ל-Sandbox עשויה לדרוש הודעת-תבנית מאושרת מול מטא ("business-initiated
conversation") - זו אחריות המפעיל, ראו "עקרונות משותפים" ב-CLAUDE.md.
"""

import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import db
import extract  # extract.py לא מייבא את outbound_engine - אין circular import
from whatsapp_provider import get_provider
from whatsapp_send import _to_e164

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

# ילד של "whatsapp_crm" (server.py) - בלי זה שורות INFO מכאן לא הגיעו ללוג בכלל (אין handler)
logger = logging.getLogger("whatsapp_crm.outbound")

# Anti-Ban rate limiter - השהיה רנדומלית בין הודעה להודעה. איטי יותר מ-
# reactivate.RATE_LIMIT_* (15-30 שניות) בכוונה: אלו אנשי קשר חדשים לגמרי
# שמעולם לא היה איתם קשר - הסיכון לתיוג כספאם גבוה משמעותית מהחייאת ליד קיים.
#
# ⚠️ לא לבלבל עם AUTO_REPLY_MIN/MAX_DELAY_SECONDS ב-server.py (שלב 5): זה כאן
# הוא קצב *בין הודעה להודעה בתוך קמפיין יזום* ללקוחות חדשים (outbound קר);
# ה-delay ב-server.py הוא השהיה לפני *מענה חוזר לאותו לקוח* שכבר כתב אלינו
# (inbound). שני מנגנונים נפרדים בכוונה, למטרות שונות - לא כפילות/טעות.
# רצפה קשיחה: משתנה סביבה שגוי (0/ריק) לא יכול לבטל את המרווח בפרודקשן.
ANTI_BAN_FLOOR_SECONDS = 45
MIN_DELAY_SECONDS = max(int(os.environ.get("OUTBOUND_MIN_DELAY_SECONDS", "45")), ANTI_BAN_FLOOR_SECONDS)
MAX_DELAY_SECONDS = max(int(os.environ.get("OUTBOUND_MAX_DELAY_SECONDS", "90")), MIN_DELAY_SECONDS)
# runner נחשב "מת" אם לא רענן את הנעילה כך וכך שניות (מרענן כל ~5 שניות גם בזמן השהיה)
RUN_LOCK_STALE_SECONDS = 120


def outbound_lock_key(tenant_id: str) -> str:
    """נעילה אחת לכל tenant (= מספר WhatsApp אחד) - גם שני קמפיינים שונים לא ירוצו במקביל."""
    return f"outbound:{tenant_id}"


def _wait_for_anti_ban_gap(
    tenant_id: str, lock_key: str, owner: str, position: str, phone: str,
    last_attempt_at: datetime | None = None,
) -> bool:
    """חוסם עד שעבר מרווח אקראי של MIN-MAX שניות מהשליחה האחרונה של ה-tenant -
    המאוחר מבין: השליחה האחרונה בפועל ב-DB (כל runner/קמפיין) וניסיון השליחה
    האחרון של ה-runner הזה (כולל ניסיון שנכשל). רושם ללוג כמה ממתינים, ומרענן את
    הנעילה תוך כדי. מחזיר False אם הנעילה אבדה (runner אחר השתלט) - אז אסור לשלוח."""
    gap = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
    candidates = [t for t in (db.last_outbound_sent_at(tenant_id), last_attempt_at) if t is not None]
    last_sent = max(candidates) if candidates else None
    if last_sent is None:
        logger.info("📤 [קמפיין %s] %s: שליחה ראשונה של ה-tenant - שולח עכשיו אל %s", tenant_id, position, phone)
        return True
    elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
    remaining = gap - elapsed
    if remaining <= 0:
        logger.info(
            "📤 [קמפיין %s] %s: עברו %.0f שנ' מהשליחה הקודמת (מרווח נדרש %.0f) - שולח עכשיו אל %s",
            tenant_id, position, elapsed, gap, phone,
        )
        return True
    logger.info(
        "⏳ [קמפיין %s] %s: Anti-Ban - ממתין %.0f שנ' לפני שליחה אל %s (מרווח %.0f שנ' מהשליחה הקודמת)",
        tenant_id, position, remaining, phone, gap,
    )
    deadline = time.monotonic() + remaining
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(5.0, left))
        if not db.heartbeat_outbound_lock(lock_key, owner):
            logger.error("🚨 [קמפיין %s] הנעילה אבדה בזמן ההמתנה - עוצר בלי לשלוח", tenant_id)
            return False
    return True

# מכסה יומית פר-tenant - עצירה מוחלטת (לא רק השהיה) כשמגיעים אליה, כדי לא
# לחצות סף שנראה כמו bulk-spam בעיני WhatsApp/UltraMsg. נאכפת מול
# db.count_sent_today (sent+simulated בפועל היום), לא מול גודל התור הכולל.
DAILY_QUOTA = int(os.environ.get("OUTBOUND_DAILY_QUOTA", "150"))

DEFAULT_MESSAGE_TEMPLATE = (
    "היי {name}, מה שלומך? 🙂 הגעתי אליך דרך {company} וחשבתי שיהיה שווה "
    "להכיר - אשמח לספר בקצרה במה אנחנו יכולים לעזור, ולשמוע אם זה רלוונטי אצלכם."
)


def normalize_campaign_phone(raw: str | None) -> str | None:
    """מנרמל מספר טלפון גולמי מקובץ B2B ל-E.164 - אותה פונקציית נרמול בדיוק
    שכל שאר הפרויקט (Twilio/UltraMsg) כבר משתמש בה (whatsapp_send._to_e164),
    לא לוגיקה כפולה. מחזיר None אם אחרי ניקוי לא נשארו ספרות בכלל - הקורא
    (import_campaign_rows) מדלג על שורה כזו במקום להזריק ערך שבור לתור."""
    if not raw:
        return None
    cleaned = "".join(ch for ch in str(raw).strip() if ch.isdigit() or ch == "+")
    if not cleaned:
        return None
    return _to_e164(cleaned)


def render_message(
    template: str, name: str | None, company: str | None, project: str | None = None,
    fallback_template: str | None = None,
) -> str:
    """ממלא {name}/{company}/{project} בתבנית ההודעה. נפילה חזרה לתבנית ברירת
    המחדל אם בתבנית מותאמת-אישית יש placeholder לא-מוכר (למשל טעות הקלדה) - עדיף
    הודעת ברירת-מחדל תקינה מאשר שגיאת format גולמית שנשלחת/נחסמת. תבנית שבורה
    תחבירית (סוגר מסולסל יתום וכו') / לא-str - מוחזרת כמו שהיא. לעולם לא זורקת
    (אותו עיקרון כמו scheduler._render_tenant_custom_message)."""
    safe_name = name or "שם"
    safe_company = company or "העסק שלכם"
    safe_project = project or "הפרויקט"
    try:
        return template.format(name=safe_name, company=safe_company, project=safe_project)
    except (KeyError, IndexError):
        # fallback_template: קמפיין CRM נופל לתבנית ה-CRM, לא לתבנית ה-B2B ("הגעתי אליך דרך...")
        fallback = fallback_template or DEFAULT_MESSAGE_TEMPLATE
        return fallback.format(name=safe_name, company=safe_company, project=safe_project)
    except (ValueError, AttributeError):
        return template


def render_crm_message(template: str | None, name: str | None, company: str | None, project: str | None = None) -> str:
    """ההודעה שליד CRM יקבל בפועל - תבנית ריקה או שגויה נופלות לתבנית ה-CRM.
    משמשת גם את התצוגה המקדימה (server.py) וגם את import_crm_leads, כך שמה שרואים = מה שנשלח."""
    return render_message(
        (template or "").strip() or DEFAULT_CRM_MESSAGE_TEMPLATE, name, company, project,
        fallback_template=DEFAULT_CRM_MESSAGE_TEMPLATE,
    )


def import_campaign_rows(
    rows: list[dict], tenant_id: str, campaign_id: str, message_template: str | None = None
) -> dict:
    """ממלא את outbound_queue משורות שכבר מופו (phone/name/company/category,
    ראו _map_campaign_row ב-server.py) - **לא שולח שום דבר**, רק רושם
    status="queued" לכל שורה תקינה. מחזיר {queued, skipped} - skipped כולל
    את מספר השורה והסיבה לכל שורה שנפלה (לא רק מונה כולל), כדי שהנציג יראה
    בדיוק מה יובא ומה לא לפני שמפעילים שליחה."""
    template = (message_template or "").strip() or DEFAULT_MESSAGE_TEMPLATE
    queued = 0
    skipped = []
    for row_num, row in enumerate(rows, start=2):  # שורה 1 = כותרות בקובץ המקורי
        phone = normalize_campaign_phone(row.get("phone"))
        if not phone:
            skipped.append({"row": row_num, "reason": "מספר טלפון חסר/לא תקין", "raw": row.get("phone")})
            continue
        name = row.get("name")
        company = row.get("company")
        message = render_message(template, name, company)
        db.add_outbound_queue_item(
            tenant_id, campaign_id, phone,
            name=name, company=company, category=row.get("category"), message=message,
        )
        queued += 1
    return {"queued": queued, "skipped": skipped}


CRM_LEAD_CATEGORY = "crm_lead"

DEFAULT_CRM_MESSAGE_TEMPLATE = (
    "היי {name}, מה שלומך? 🙂 רציתי לעדכן אותך לגבי {project} - "
    "יש לנו חדשות שיכולות לעניין אותך. אשמח לשמוע אם זה עדיין רלוונטי עבורך."
)


def import_crm_leads(
    leads: list[dict], tenant_id: str, campaign_id: str, message_template: str | None = None
) -> dict:
    """ממלא את outbound_queue מלידים *קיימים* ב-CRM (הפלט של
    extract.select_campaign_leads: key/phone/tenant_id/name/company/project/
    lead_status/ai_enabled) - **לא שולח שום דבר**, לא ניגש ל-provider (חוק
    בטיחות #1, בדיוק כמו import_campaign_rows). שליחה רק דרך run_campaign.

    הטלפון נשמר *בדיוק* כפי שהוא בכרטיס (בלי נרמול E.164): נתיב השליחה מנרמל
    בעצמו, והרישום אחרי השליחה (log_manual_reply) חייב לפגוע באותו מפתח כרטיס
    tenant::phone - נרמול כאן היה יוצר כרטיס כפול. category="crm_lead" מסמן
    ל-run_campaign שזה פריט CRM (בדיקה מחדש לפני שליחה + רישום בכרטיס)."""
    queued = 0
    skipped = []
    for lead in leads or []:
        lead = lead or {}
        phone = str(lead.get("phone") or "").strip()
        if not phone:
            skipped.append({"phone": lead.get("phone"), "reason": "מספר טלפון חסר"})
            continue
        if lead.get("tenant_id") != tenant_id:
            skipped.append({"phone": phone, "reason": "tenant לא תואם"})
            continue
        name = lead.get("name")
        company = lead.get("company")
        message = render_crm_message(message_template, name, company, lead.get("project"))
        db.add_outbound_queue_item(
            tenant_id, campaign_id, phone,
            name=name, company=company, category=CRM_LEAD_CATEGORY, message=message,
        )
        queued += 1
    return {"queued": queued, "skipped": skipped}


def _phone_digits_e164(raw: str | None) -> str:
    """נרמול להשוואה בלבד (לא לשליחה) - _to_e164 על מחרוזת שאינה ריקה."""
    raw = str(raw or "").strip()
    return _to_e164(raw) if raw else ""


def _server_style_normalize(raw: str) -> str:
    """אותו נרמול בדיוק כמו server._normalize_phone (ספרות בלבד, 0→972, '+'
    בהתחלה) - משוכפל כאן כי ייבוא server.py מתוך outbound_engine היה יוצר
    circular import (server מייבא את המודול הזה)."""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    return f"+{digits}" if digits else ""


def _env_blocked_contacts() -> set[str]:
    """BLOCKED_CONTACTS מה-env, מפורסר כמו ב-server.py (פסיקים + _normalize_phone).
    נקרא בכל בדיקה (זול) - כך שגם שינוי env בבדיקות נקלט."""
    return {
        _server_style_normalize(p) for p in os.environ.get("BLOCKED_CONTACTS", "").split(",") if p.strip()
    }


def _is_blocked(tenant_id: str, phone: str) -> bool:
    """חסימה (CLAUDE.md חוק 7): BLOCKED_CONTACTS (env) או blocked_contacts ב-DB
    פר-tenant - אותה בדיקה כמו בבחירת הלידים לקמפיין ב-server.py: הפורמט
    המנורמל, ובנוסף המספר הגולמי כפי שנשמר בכרטיס."""
    raw = str(phone or "").strip()
    if not raw:
        return False
    normalized = _server_style_normalize(raw)
    if normalized and normalized in _env_blocked_contacts():
        return True
    e164 = _to_e164(raw)
    candidates = [c for c in dict.fromkeys([normalized, e164, raw]) if c]
    return any(db.is_contact_blocked(c, tenant_id=tenant_id) for c in candidates)


def _resolve_crm_card(tenant_id: str, phone: str) -> tuple[str | None, dict | None]:
    """מאתר את כרטיס הליד לפי tenant + טלפון מנורמל - לא רק לפי המפתח המדויק
    tenant::phone. כרטיס שה-"phone" השמור בו שונה מהמפתח שלו (למשל מפתח
    +9725..., phone 05...) היה מסומן קודם בטעות "לא קיים". סדר עדיפות: התאמה
    מדויקת של המפתח, אחר כך מפתח שהספרות שלו (E.164) תואמות, ואחר כך שדה phone
    של הכרטיס שתואם. מחזיר (key, card) או (None, None)."""
    customers = extract.load_customers()
    exact_key = extract._customer_key(tenant_id, phone)
    if exact_key in customers:
        return exact_key, customers[exact_key]
    target = _phone_digits_e164(phone)
    if not target:
        return None, None
    prefix = f"{tenant_id}::"
    by_field = None
    for key, card in customers.items():
        if not key.startswith(prefix) or not isinstance(card, dict):
            continue
        if card.get("tenant_id") not in (None, tenant_id):
            continue
        if _phone_digits_e164(key[len(prefix):]) == target:
            return key, card
        if by_field is None and _phone_digits_e164(card.get("phone")) == target:
            by_field = (key, card)
    return by_field or (None, None)


def _crm_lead_skip_reason(tenant_id: str, phone: str) -> tuple[str | None, str | None]:
    """בדיקה מחדש רגע לפני שליחה לפריט crm_lead. מחזיר (skip_reason, card_key):
    skip_reason=None אם מותר לשלוח, ו-card_key הוא מפתח הכרטיס שנמצא (לרישום
    אחרי השליחה). מבטל כשהכרטיס נמחק, כשהליד סומן not_relevant, כשנציג
    אנושי השתלט על השיחה (lead_status="human_handling", CLAUDE.md חוק 7),
    או כשהמספר חסום. **לא** מבטל רק כי ai_enabled=False - הבוט משתיק את
    עצמו אחרי הכשרה, ואלה לידים רגילים לגמרי."""
    key, card = _resolve_crm_card(tenant_id, phone)
    if card is None:
        return "הכרטיס כבר לא קיים ב-CRM", None
    status = card.get("lead_status")
    if status == "not_relevant":
        return "הליד סומן not_relevant אחרי ההכנסה לתור", key
    if status == "human_handling":
        return "נציג אנושי השתלט על השיחה (human_handling) אחרי ההכנסה לתור", key
    key_phone = key.split("::", 1)[1] if "::" in key else phone
    if any(_is_blocked(tenant_id, p) for p in dict.fromkeys([phone, key_phone, card.get("phone")]) if p):
        return "המספר חסום (BLOCKED_CONTACTS/רשימת חסימה)", key
    return None, key


def _log_crm_send(tenant_id: str, card_key: str, message: str, simulated: bool) -> None:
    """רישום ההודעה שיצאה בכרטיס הליד (history) ובטבלת messages, בערוץ
    "campaign" - כדי שלשיחה יהיה הקשר. נקרא *אחרי* שהפריט כבר סומן
    sent/simulated, כך שכשל כאן לא גורם לשליחה חוזרת - רק נרשם בלוג.

    כותב רק לכרטיס *קיים* (update_card בלי default): כרטיס שנמחק בינתיים לא
    נוצר מחדש - רק warning, והפריט נשאר sent."""
    phone = card_key.split("::", 1)[1] if "::" in card_key else card_key

    def _mutate(card: dict, _is_new: bool) -> None:
        card.setdefault("source_channel", "campaign")
        extract._append_history(card, "campaign", message, direction="out", simulated=simulated)

    try:
        extract.update_card(card_key, _mutate)
    except KeyError:
        logger.warning(
            "outbound campaign: lead card disappeared before logging the send (tenant=%s) - not recreated",
            tenant_id,
        )
    except Exception:
        logger.exception("outbound campaign: failed to log send on lead card (tenant=%s)", tenant_id)
    try:
        db.log_message(
            phone, message, direction="out", tenant_id=tenant_id, channel="campaign", simulated=simulated,
        )
    except Exception:
        logger.exception("outbound campaign: failed to log send in messages (tenant=%s)", tenant_id)


def run_campaign(tenant_id: str, campaign_id: str) -> dict:
    """שולח בפועל את כל הפריטים 'queued' בקמפיין הזה, בקצב מבוקר (Anti-Ban)
    ותחת מכסה יומית - נקראת **רק** מ-thread נפרד שהתחיל ב-POST /api/campaigns/
    <id>/start (server.py, אחרי קליק אנושי מפורש) - לעולם לא אוטומטית.

    עוצרת (לא נכשלת) ברגע שהמכסה היומית מתמלאת - שאר השורות מסומנות
    'skipped_quota' (לא 'failed') ונשארות ניתנות-לשליחה בהרצה הבאה (מחר, או
    קליק חוזר על הקמפיין - run_campaign תמיד שולף מחדש 'queued' בלבד, אז
    שורות 'skipped_quota' צריכות איפוס ידני לחזור ל-queued אם רוצים לנסות
    שוב - לא קורה אוטומטית כאן, כדי לא ליצור לולאת ניסיונות אינסופית סמויה).

    בטוחה להרצה מקבילית על אותו קמפיין: כל פריט נתפס אטומית (db.claim_outbound_item,
    queued→sending) רגע לפני הטיפול בו - פריט שנתפס ע"י runner אחר מדולג בשקט.

    פריטי category="crm_lead" (import_crm_leads): לפני שליחה נבדק מחדש שהכרטיס
    קיים (לפי tenant + טלפון מנורמל), שהליד לא הפך not_relevant/human_handling
    ושהמספר לא חסום - אחרת הפריט מסומן 'cancelled' (הסיבה בשדה error) ולא נשלח.
    אחרי sent/simulated ההודעה נרשמת בכרטיס שנמצא (רק אם עדיין קיים) ובטבלת
    messages בערוץ "campaign". פריטי CSV רגילים - רק התפיסה האטומית נוספה.
    מחזיר סיכום {sent, simulated, failed, skipped_quota, cancelled}."""
    # גם פריטי skipped_quota מהרצות קודמות - קליק חוזר על "התחל" (למחרת) ממשיך
    # מאיפה שהמכסה עצרה. אין לולאה סמויה: כל הרצה עוברת על הרשימה פעם אחת.
    items = [
        item
        for status in ("queued", "skipped_quota")
        for item in db.get_outbound_queue(tenant_id=tenant_id, campaign_id=campaign_id, status=status)
    ]
    summary = {"sent": 0, "simulated": 0, "failed": 0, "skipped_quota": 0, "cancelled": 0}
    if not items:
        return summary

    # נעילה אחת לכל tenant, בין כל ה-workers: רק runner אחד שולח מהמספר בכל רגע.
    # בלעדיה, כל worker של gunicorn שקיבל "התחל" הריץ runner משלו, וכל runner תפס
    # פריט אחר ושלח מיד - כך יצאו כמה הודעות בבת אחת בלי מרווח.
    lock_key = outbound_lock_key(tenant_id)
    owner = f"{campaign_id}:{uuid.uuid4().hex[:8]}"
    if not db.acquire_outbound_lock(lock_key, owner, RUN_LOCK_STALE_SECONDS):
        logger.warning("⛔ [קמפיין %s] runner אחר כבר שולח מה-tenant הזה - לא מתחיל runner נוסף (campaign=%s)", tenant_id, campaign_id)
        summary["locked"] = True
        return summary
    logger.info(
        "▶️ [קמפיין %s] התחלת שליחה טורית: campaign=%s, %d פריטים, מרווח %d-%d שנ' בין הודעות",
        tenant_id, campaign_id, len(items), MIN_DELAY_SECONDS, MAX_DELAY_SECONDS,
    )
    try:
        _run_campaign_items(tenant_id, items, summary, lock_key, owner)
    finally:
        db.release_outbound_lock(lock_key, owner)
        logger.info("⏹️ [קמפיין %s] סיום שליחה: campaign=%s, סיכום=%s", tenant_id, campaign_id, summary)
    return summary


def _run_campaign_items(tenant_id: str, items: list[dict], summary: dict, lock_key: str, owner: str) -> None:
    """לולאת השליחה הטורית - רצה רק כשה-runner מחזיק בנעילת ה-tenant (ראו run_campaign)."""
    provider = get_provider()
    last_attempt_at: datetime | None = None
    total = len(items)

    for i, item in enumerate(items):
        db.heartbeat_outbound_lock(lock_key, owner)
        if db.count_sent_today(tenant_id) >= DAILY_QUOTA:
            # מעבר מותנה ל-skipped_quota: runner מקביל שכבר תפס/שלח את הפריט לא
            # יידרס. פריט שכבר skipped_quota פשוט נשאר כך.
            if item["status"] == "skipped_quota" or db.claim_outbound_item(item["id"], to_status="skipped_quota"):
                summary["skipped_quota"] += 1
            continue

        # מנוע ה-Anti-Ban: חוסם *לפני* כל שליחה עד שעבר מרווח אקראי של 45-90 שנ'
        # מהשליחה הקודמת של ה-tenant. ההמתנה לפני התפיסה והבדיקה מחדש - כך שמצב
        # הליד נבדק ממש לפני השליחה, לא דקה וחצי קודם.
        position = f"הודעה {i + 1}/{total}"
        if not _wait_for_anti_ban_gap(tenant_id, lock_key, owner, position, item["phone"], last_attempt_at):
            return

        # תפיסה אטומית (queued/skipped_quota)→sending - הגנה נוספת מפני שליחה כפולה
        # (הנעילה כבר מבטיחה runner יחיד). נכשל = הפריט טופל/בוטל בינתיים - דילוג.
        if not db.claim_outbound_item(item["id"], from_status=item["status"]):
            continue

        is_crm_lead = item.get("category") == CRM_LEAD_CATEGORY
        card_key = None
        if is_crm_lead:
            try:
                skip_reason, card_key = _crm_lead_skip_reason(tenant_id, item["phone"])
            except Exception as exc:
                # לא מצליחים לאמת את מצב הליד - עדיף לא לשלוח מאשר לשלוח בעיוורון
                skip_reason = f"בדיקת הכרטיס נכשלה: {exc}"
            if skip_reason:
                # הפריט כבר שלנו (sending) - מעבר ישיר ל-cancelled
                db.update_outbound_status(item["id"], "cancelled", error=skip_reason)
                summary["cancelled"] += 1
                continue  # לא נשלח כלום - אין צורך בהשהיית Anti-Ban

        outcome = None  # "sent"/"simulated" - לרישום בכרטיס אחרי שהסטטוס כבר עודכן
        last_attempt_at = datetime.now(timezone.utc)  # גם ניסיון שנכשל נספר למרווח
        logger.info("📨 [קמפיין %s] %s: שולח עכשיו אל %s", tenant_id, position, item["phone"])
        try:
            provider.send_message(item["phone"], item["message"])
            db.update_outbound_status(item["id"], "sent", sent_at=datetime.now(timezone.utc).isoformat())
            summary["sent"] += 1
            outcome = "sent"
        except Exception as exc:
            if provider.is_soft_failure(exc):
                # כשל "ידוע ותקין" של הספק הפעיל (Trial/נמען לא מאומת וכו') - לא
                # שגיאת קוד, ראו reactivate.py לאותו עיקרון בדיוק.
                db.update_outbound_status(item["id"], "simulated", sent_at=datetime.now(timezone.utc).isoformat())
                summary["simulated"] += 1
                outcome = "simulated"
            else:
                db.update_outbound_status(item["id"], "failed", error=str(exc))
                summary["failed"] += 1

        if is_crm_lead and outcome and card_key:
            # הסטטוס כבר sent/simulated ב-DB - כשל רישום לא יגרום לשליחה חוזרת.
            # card_key = הכרטיס שנמצא בבדיקה מחדש (גם כשהמפתח שונה מ-item.phone)
            _log_crm_send(tenant_id, card_key, item["message"], simulated=(outcome == "simulated"))
