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
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

import db
from extract import DEFAULT_TENANT_ID, _customer_key, load_customers, update_lead_status
from whatsapp_send import send_whatsapp_message

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

CONTACTS_FILE = Path(__file__).parent / "contacts.csv"
ALREADY_HANDLED_STATUSES = {"contacted", "hot", "not_relevant"}

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


def generate_outreach_message(name: str, business: str) -> str:
    prompt = OUTREACH_PROMPT.format(name=name, business=business or "לא ידוע")
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=300,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(block.text for block in response.content if block.type == "text").strip()


def classify_reply(message_text: str) -> str:
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=10,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": CLASSIFY_PROMPT + message_text}],
    )
    label = next(block.text for block in response.content if block.type == "text").strip()
    return label if label in ("hot", "not_relevant") else "not_relevant"


def load_contacts() -> list[dict]:
    with CONTACTS_FILE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def get_cold_leads(contacts: list[dict], tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """מסנן מתוך רשימת אנשי הקשר רק את מי שעדיין לא טופל (אין לו כרטיס, או שהכרטיס
    לא מסומן contacted/hot/not_relevant) - כלומר ליד קר שטרם בוצעה אליו פנייה."""
    customers = load_customers()
    cold = []
    for contact in contacts:
        card = customers.get(_customer_key(tenant_id, contact["phone"]))
        status = card.get("lead_status") if card else None
        if status in ALREADY_HANDLED_STATUSES:
            continue
        cold.append(contact)
    return cold


def run_reactivation_campaign(
    tenant_id: str = DEFAULT_TENANT_ID, send: bool = False, contacts: list[dict] | None = None
) -> list[dict]:
    """מריץ את קמפיין החימום ומחזיר תוצאה מובנית לכל ליד קר (לשימוש ב-CLI, ב-API של
    הדשבורד - /api/reactivate, וגם ב-scheduler.py לסריקה אוטומטית). לא שולח כלום אם
    send=False (ברירת מחדל). contacts מאפשר להעביר רשימה מסוננת מראש (למשל ע"י
    scheduler.py לפי ורטיקל+זמן) במקום לקרוא את כל contacts.csv."""
    if contacts is None:
        contacts = load_contacts()
    cold_leads = get_cold_leads(contacts, tenant_id=tenant_id)

    results = []
    for contact in cold_leads:
        message = generate_outreach_message(contact["name"], contact.get("business", ""))
        entry = {
            "name": contact["name"],
            "phone": contact["phone"],
            "business": contact.get("business"),
            "message": message,
            "sent": False,
            "error": None,
            "sid": None,
        }

        if send:
            try:
                entry["sid"] = send_whatsapp_message(contact["phone"], message)
                entry["sent"] = True
                update_lead_status(
                    contact["phone"],
                    status="contacted",
                    extra={"customer_name": contact["name"], "business_name": contact.get("business")},
                    tenant_id=tenant_id,
                    note=f"הודעת חימום נשלחה: {message}",
                    direction="out",
                )
                db.log_message(contact["phone"], message, direction="out", tenant_id=tenant_id, channel="whatsapp")
            except Exception as exc:
                entry["error"] = str(exc)

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
