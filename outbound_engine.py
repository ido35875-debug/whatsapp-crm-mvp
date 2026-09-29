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

import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import db
from whatsapp_provider import get_provider
from whatsapp_send import _to_e164

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

# Anti-Ban rate limiter - השהיה רנדומלית בין הודעה להודעה. איטי יותר מ-
# reactivate.RATE_LIMIT_* (15-30 שניות) בכוונה: אלו אנשי קשר חדשים לגמרי
# שמעולם לא היה איתם קשר - הסיכון לתיוג כספאם גבוה משמעותית מהחייאת ליד קיים.
#
# ⚠️ לא לבלבל עם AUTO_REPLY_MIN/MAX_DELAY_SECONDS ב-server.py (שלב 5): זה כאן
# הוא קצב *בין הודעה להודעה בתוך קמפיין יזום* ללקוחות חדשים (outbound קר);
# ה-delay ב-server.py הוא השהיה לפני *מענה חוזר לאותו לקוח* שכבר כתב אלינו
# (inbound). שני מנגנונים נפרדים בכוונה, למטרות שונות - לא כפילות/טעות.
MIN_DELAY_SECONDS = int(os.environ.get("OUTBOUND_MIN_DELAY_SECONDS", "45"))
MAX_DELAY_SECONDS = int(os.environ.get("OUTBOUND_MAX_DELAY_SECONDS", "90"))

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


def render_message(template: str, name: str | None, company: str | None) -> str:
    """ממלא {name}/{company} בתבנית ההודעה. נפילה חזרה לתבנית ברירת המחדל אם
    בתבנית מותאמת-אישית יש placeholder לא-מוכר (למשל טעות הקלדה) - עדיף
    הודעת ברירת-מחדל תקינה מאשר שגיאת format גולמית שנשלחת/נחסמת."""
    safe_name = name or "שם"
    safe_company = company or "העסק שלכם"
    try:
        return template.format(name=safe_name, company=safe_company)
    except (KeyError, IndexError):
        return DEFAULT_MESSAGE_TEMPLATE.format(name=safe_name, company=safe_company)


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


def run_campaign(tenant_id: str, campaign_id: str) -> dict:
    """שולח בפועל את כל הפריטים 'queued' בקמפיין הזה, בקצב מבוקר (Anti-Ban)
    ותחת מכסה יומית - נקראת **רק** מ-thread נפרד שהתחיל ב-POST /api/campaigns/
    <id>/start (server.py, אחרי קליק אנושי מפורש) - לעולם לא אוטומטית.

    עוצרת (לא נכשלת) ברגע שהמכסה היומית מתמלאת - שאר השורות מסומנות
    'skipped_quota' (לא 'failed') ונשארות ניתנות-לשליחה בהרצה הבאה (מחר, או
    קליק חוזר על הקמפיין - run_campaign תמיד שולף מחדש 'queued' בלבד, אז
    שורות 'skipped_quota' צריכות איפוס ידני לחזור ל-queued אם רוצים לנסות
    שוב - לא קורה אוטומטית כאן, כדי לא ליצור לולאת ניסיונות אינסופית סמויה).
    מחזיר סיכום {sent, simulated, failed, skipped_quota}."""
    items = db.get_outbound_queue(tenant_id=tenant_id, campaign_id=campaign_id, status="queued")
    provider = get_provider()
    summary = {"sent": 0, "simulated": 0, "failed": 0, "skipped_quota": 0}

    for i, item in enumerate(items):
        if db.count_sent_today(tenant_id) >= DAILY_QUOTA:
            db.update_outbound_status(item["id"], "skipped_quota")
            summary["skipped_quota"] += 1
            continue

        db.update_outbound_status(item["id"], "sending")
        try:
            provider.send_message(item["phone"], item["message"])
            db.update_outbound_status(item["id"], "sent", sent_at=datetime.now(timezone.utc).isoformat())
            summary["sent"] += 1
        except Exception as exc:
            if provider.is_soft_failure(exc):
                # כשל "ידוע ותקין" של הספק הפעיל (Trial/נמען לא מאומת וכו') - לא
                # שגיאת קוד, ראו reactivate.py לאותו עיקרון בדיוק.
                db.update_outbound_status(item["id"], "simulated", sent_at=datetime.now(timezone.utc).isoformat())
                summary["simulated"] += 1
            else:
                db.update_outbound_status(item["id"], "failed", error=str(exc))
                summary["failed"] += 1

        # מנוע ה-Anti-Ban: השהיה רנדומלית לפני הפריט הבא (לא אחרי האחרון) - קצב
        # קבוע בין ניסיונות בלי קשר לתוצאה (נשלח/סימולציה/כשל), בדיוק כמו
        # reactivate.py - אחרת כשל חוזר היה יכול "לברוח" מהמגבלה.
        if i < len(items) - 1:
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

    return summary
