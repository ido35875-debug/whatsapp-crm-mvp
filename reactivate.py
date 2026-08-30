"""
מודול "חימום לידים קרים": מכין הודעת פתיחה מותאמת אישית לכל איש קשר, שולח אותה
בפועל בוואטסאפ דרך Twilio, ויודע לסווג תשובה נכנסת כ"ליד חם" או "לא רלוונטי".

הערה חשובה - מדיניות מטא: הודעה ראשונה שעסק יוזם ביוזמתו בוואטסאפ (לא מענה על הודעה
שהלקוח פתח) נחשבת "business-initiated conversation". מחוץ ל-Twilio WhatsApp Sandbox
(כלומר במספר production אמיתי), מטא דורשת שהודעה כזו תהיה "הודעת תבנית" (template)
שאושרה מראש - טקסט חופשי לא יעבור. ב-Sandbox אפשר לשלוח טקסט חופשי, אבל רק לנמענים
שהצטרפו אליו בעצמם (שלחו "join <קוד>"). לכן ברירת המחדל של הסקריפט הזה היא dry-run
(מציג תצוגה מקדימה בלבד, לא שולח כלום) - שליחה בפועל דורשת גם דגל --send וגם אישור
אנושי מפורש, וגם פרטי חיבור Twilio אמיתיים ב-.env.
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

import db
import prompts
from extract import DEFAULT_TENANT_ID, _customer_key, last_contact_at, load_customers, update_lead_status
from whatsapp_send import is_trial_restriction, send_whatsapp_message

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

CONTACTS_FILE = Path(__file__).parent / "contacts.csv"
ALREADY_HANDLED_STATUSES = {"contacted", "hot", "not_relevant"}
DEFAULT_COLD_DAYS = 30  # סף ברירת המחדל להחייאה ידנית מהדשבורד - "לא נוצר קשר מעל 30 יום"
FOLLOW_UP_DAYS_AHEAD = 3  # בעוד כמה ימים תיקבע משימת המעקב שנוצרת אוטומטית אחרי שליחה

OUTREACH_PROMPT = """\
אתה כותב הודעת וואטסאפ קצרה, חמה וטבעית (לא שיווקית מדי) לפנייה מחודשת ללקוח פוטנציאלי
שהיה בקשר בעבר ולא המשיך.
שם הלקוח: {name}
שם העסק שלו (אם רלוונטי): {business}
כתוב הודעה קצרה בעברית (2-3 משפטים), בגוף ראשון, כאילו אתה בעל העסק שפונה מחדש.
אל תמציא פרטים שלא ניתנו. החזר רק את טקסט ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""

CLASSIFY_PROMPT = """\
אתה מסווג תשובת וואטסאפ שהתקבלה מליד קר לאחר הודעת פתיחה מחודשת.
סווג את התשובה לקטגוריה אחת בלבד: hot (מעוניין, שואל שאלה, רוצה לשמוע עוד) או
not_relevant (לא מעוניין, מבקש הסרה, לא רלוונטי).
החזר מילה אחת בלבד: hot או not_relevant.

תשובת הלקוח:
"""


def generate_outreach_message(name: str, business: str, vertical: str | None = None) -> str:
    """vertical (אופציונלי, למשל contact["vertical"] מ-contacts.csv): אם יש
    override מותאם-ענף מוגדר ל-vertical הזה ב-prompts.json, הוא ינוצח את
    התבנית הבסיסית - ראו prompts.get_prompt."""
    prompt_template = prompts.get_prompt("reactivation_outreach", OUTREACH_PROMPT, vertical=vertical)
    prompt = prompt_template.format(name=name, business=business or "לא ידוע")
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=300,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(block.text for block in response.content if block.type == "text").strip()


def classify_reply(message_text: str) -> str:
    prompt_template = prompts.get_prompt("reactivation_classify", CLASSIFY_PROMPT)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=10,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt_template + message_text}],
    )
    label = next(block.text for block in response.content if block.type == "text").strip()
    return label if label in ("hot", "not_relevant") else "not_relevant"


def load_contacts() -> list[dict]:
    with CONTACTS_FILE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def get_cold_leads(
    contacts: list[dict], tenant_id: str = DEFAULT_TENANT_ID, days: int | None = None
) -> list[dict]:
    """מסנן מתוך רשימת אנשי הקשר רק את מי שעדיין לא טופל (אין לו כרטיס, או שהכרטיס
    לא מסומן contacted/hot/not_relevant) - כלומר ליד קר שטרם בוצעה אליו פנייה.
    days=None (ברירת מחדל): מסנן לפי סטטוס בלבד - זו ההתנהגות המקורית, ונשמרת
    בכוונה כברירת מחדל כי scheduler.py כבר עושה בדיקת-זמן משלו פר-ורטיקל (_is_due)
    *אחרי* הקריאה הזו - הוספת סף ימים כללי כאן היה שובר את הסף המהיר (3 ימים)
    של ורטיקל ecommerce. days=<מספר>: מוסיף גם סינון "לא נוצר קשר מעל X ימים" -
    ליד עם היסטוריה עדכנית (גם אם הסטטוס עדיין 'new') לא ייחשב קר מספיק - לשימוש
    ההחייאה הידנית מהדשבורד (/api/reactivate, ברירת מחדל DEFAULT_COLD_DAYS)."""
    customers = load_customers()
    now = datetime.now(timezone.utc)
    cold = []
    for contact in contacts:
        card = customers.get(_customer_key(tenant_id, contact["phone"]))
        status = card.get("lead_status") if card else None
        if status in ALREADY_HANDLED_STATUSES:
            continue
        if days is not None:
            last_contact = last_contact_at(card)
            if last_contact is not None and (now - last_contact).days < days:
                continue  # יצר קשר לאחרונה - עדיין לא "קר" מספיק לפי הסף שנבחר
        cold.append(contact)
    return cold


def run_reactivation_campaign(
    tenant_id: str = DEFAULT_TENANT_ID,
    send: bool = False,
    contacts: list[dict] | None = None,
    days: int | None = None,
) -> list[dict]:
    """מריץ את קמפיין החימום ומחזיר תוצאה מובנית לכל ליד קר (לשימוש ב-CLI, ב-API של
    הדשבורד - /api/reactivate, וגם ב-scheduler.py לסריקה אוטומטית). לא שולח כלום אם
    send=False (ברירת מחדל). contacts מאפשר להעביר רשימה מסוננת מראש (למשל ע"י
    scheduler.py לפי ורטיקל+זמן) במקום לקרוא את כל contacts.csv. days מועבר ל-
    get_cold_leads (ראו שם למה ברירת המחדל היא None ולא DEFAULT_COLD_DAYS - זה
    מונע מ-scheduler.py הקיים "לרשת" סף כללי בטעות)."""
    if contacts is None:
        contacts = load_contacts()
    cold_leads = get_cold_leads(contacts, tenant_id=tenant_id, days=days)

    results = []
    for contact in cold_leads:
        message = generate_outreach_message(contact["name"], contact.get("business", ""), vertical=contact.get("vertical"))
        entry = {
            "name": contact["name"],
            "phone": contact["phone"],
            "business": contact.get("business"),
            "message": message,
            "sent": False,
            "simulated": False,
            "error": None,
            "sid": None,
            "task_id": None,
        }

        if send:
            try:
                entry["sid"] = send_whatsapp_message(contact["phone"], message)
                entry["sent"] = True
            except Exception as exc:
                if is_trial_restriction(exc):
                    # חשבון Twilio מסוג Trial חסם את השליחה בפועל (ראו whatsapp_send.
                    # is_trial_restriction - נמען לא מאומת, גם אחרי הצטרפות ל-Sandbox) -
                    # זו לא שגיאת קוד; ממשיכים לעדכן סטטוס/היסטוריה/משימת מעקב בדיוק
                    # כמו שליחה מוצלחת (מסומן simulated=True בכל מקום), כדי לאפשר לבדוק
                    # שכל שאר הצינור - שליפת נתונים מה-CRM, ניסוח ההודעה, עדכון הסטטוס,
                    # המשימה האוטומטית - מתפקד באופן מלא גם בלי לצאת מ-Trial.
                    entry["simulated"] = True
                else:
                    entry["error"] = str(exc)

            if entry["sent"] or entry["simulated"]:
                note = (
                    f"הודעת חימום נשלחה: {message}" if entry["sent"]
                    else f"הודעת חימום (סימולציה - חשבון Twilio Trial חסם שליחה בפועל): {message}"
                )
                update_lead_status(
                    contact["phone"],
                    status="contacted",
                    extra={"customer_name": contact["name"], "business_name": contact.get("business")},
                    tenant_id=tenant_id,
                    note=note,
                    direction="out",
                    simulated=entry["simulated"],
                )
                db.log_message(
                    contact["phone"], message, direction="out", tenant_id=tenant_id,
                    channel="whatsapp", simulated=entry["simulated"],
                )

            # פעילות מעקב מהירה (calendar_tasks) - נוצרת בכל הרצת send=True, גם אם
            # השליחה בפועל מול Twilio נכשלה (למשל חשבון Trial) - כי ההחלטה "לפנות
            # מחדש לליד הזה" כבר התקבלה ע"י הנציג ברגע שהריץ עם send=True, ולא
            # אמורה להיעלם רק כי הערוץ הטכני נכשל; הנציג עדיין עשוי לרצות לעקוב
            # (למשל להתקשר ידנית) גם אם הוואטסאפ לא יצא.
            due_date = (datetime.now(timezone.utc) + timedelta(days=FOLLOW_UP_DAYS_AHEAD)).date().isoformat()
            entry["task_id"] = db.create_task(
                contact["phone"],
                tenant_id=tenant_id,
                title=f"מעקב אחרי חימום: {contact['name']}",
                due_date=due_date,
                notes=f"נוצר אוטומטית ע\"י מנוע החייאת לידים קרים. הודעה שנוסחה: {message}",
            )

        results.append(entry)

    return results


def _print_campaign_results(results: list[dict], send: bool) -> None:
    if not results:
        print("אין לידים קרים חדשים לפנייה - כל אנשי הקשר ב-contacts.csv כבר טופלו.")
        return

    print(f"נמצאו {len(results)} לידים קרים לפנייה:")
    for r in results:
        print(f"\n→ {r['name']} ({r['phone']}):\n{r['message']}")
        if not send:
            continue
        if r["sent"]:
            print(f"  ✅ נשלח בפועל (Twilio SID: {r['sid']})")
        elif r["simulated"]:
            print("  🧪 סימולציה (Trial) - Twilio חסם שליחה אמיתית, אך הסטטוס/היסטוריה/משימת המעקב עודכנו ב-CRM בדיוק כמו בשליחה מוצלחת")
        else:
            print(f"  ❌ שליחה נכשלה: {r['error']}")


if __name__ == "__main__":
    send_mode = "--send" in sys.argv

    if send_mode:
        print("⚠️  מצב שליחה בפועל: הודעות אמיתיות יישלחו ללידים ב-contacts.csv דרך WhatsApp/Twilio.\n")
    else:
        print("מצב תצוגה מקדימה (dry-run) - לא נשלחת אף הודעה. הרץ עם --send כדי לשלוח בפועל.\n")

    _print_campaign_results(run_reactivation_campaign(send=send_mode), send=send_mode)

    print("\n=== דוגמת סיווג תשובות נכנסות ===")
    sample_replies = [
        "וואו איזה כיף שחזרתם אליי! כן בהחלט מעוניין, אפשר לשמוע פרטים?",
        "תודה אבל לא מעוניין כרגע, תורידו אותי מהרשימה בבקשה",
    ]
    for reply in sample_replies:
        label = classify_reply(reply)
        print(f"\nתשובה: {reply}\nסיווג: {label}")
