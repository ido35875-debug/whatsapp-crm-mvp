"""
שלב 1 של ה-MVP: "המוח" שמחלץ פרטי לקוח מהודעת טקסט.
בשלב הזה עוד אין חיבור אמיתי לוואטסאפ - אנחנו מדמים הודעה נכנסת כמחרוזת טקסט,
ובודקים שה-AI באמת יודע לחלץ ממנה שם לקוח, שם עסק ומיקום.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# נתיב מפורש (לא הזיהוי-אוטומטי המבוסס-stack של load_dotenv) - עמיד לחלוטין לכל דרך
# הרצה/פריסה (gunicorn, אריזה בענן וכו'), בלי תלות ב-cwd או בשרשרת הקריאות בזמן import
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

from paths import DATA_DIR  # noqa: E402 - חייב אחרי load_dotenv (DATA_DIR עצמו נקרא מ-os.environ)

CUSTOMERS_FILE = DATA_DIR / "customers.json"
DEFAULT_TENANT_ID = "default"  # מזהה העסק כשאין tenant_id מפורש (תאימות לאחור)

EXTRACTION_PROMPT = """\
אתה עוזר שמחלץ פרטי לקוח מהודעת וואטסאפ בעברית או באנגלית.
מתוך ההודעה הבאה, חלץ שלושה שדות: customer_name (שם הלקוח), business_name (שם העסק שלו, אם צוין), location (עיר/אזור, אם צוין).
אם שדה מסוים לא מופיע בהודעה, החזר עבורו null.
החזר אך ורק JSON תקין בפורמט הבא, בלי שום טקסט נוסף:
{"customer_name": "...", "business_name": "...", "location": "..."}

הודעה:
"""


def extract_customer_info(message_text: str) -> dict:
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=500,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": EXTRACTION_PROMPT + message_text}],
    )
    raw_text = next(block.text for block in response.content if block.type == "text").strip()
    return json.loads(raw_text)


def load_customers() -> dict:
    if CUSTOMERS_FILE.exists():
        return json.loads(CUSTOMERS_FILE.read_text(encoding="utf-8"))
    return {}


def save_customers(customers: dict) -> None:
    CUSTOMERS_FILE.write_text(
        json.dumps(customers, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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


def upsert_customer(
    phone: str,
    extracted: dict,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
    raw_message: str | None = None,
) -> dict:
    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    card = customers.get(key, {"phone": phone, "tenant_id": tenant_id})
    card.setdefault("source_channel", source_channel)  # הערוץ שממנו הגיע הלקוח לראשונה
    for field in ("customer_name", "business_name", "location"):
        if extracted.get(field):
            card[field] = extracted[field]
    if raw_message:
        _append_history(card, source_channel, raw_message)
    customers[key] = card
    save_customers(customers)
    return card


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
אתה נציג שירות של עסק קטן, עונה בקצרה ובחום להודעה שהתקבלה מלקוח פוטנציאלי בוואטסאפ.
זו הודעת הלקוח:
"{message}"

פרטים שכבר ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}, מיקום - {location}.

כתוב תגובה קצרה בעברית (1-2 משפטים), חמה וטבעית, שמאשרת שהפנייה התקבלה ושיחזרו אליו בקרוב.
אם יש שם ללקוח, פנה אליו בשמו. אל תמציא פרטים שלא ניתנו, ואל תבטיח מחיר, הנחה או תאריך מדויק.
החזר רק את טקסט ההודעה, בלי מרכאות ובלי הסברים נוספים.
"""


def generate_reply(message_text: str, card: dict) -> str:
    prompt = REPLY_PROMPT.format(
        message=message_text,
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
    return next(block.text for block in response.content if block.type == "text").strip()


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
    prompt = CALL_SUMMARY_PROMPT.format(
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


def process_message_with_reply(
    phone: str,
    message_text: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
) -> tuple[dict, str]:
    """כמו process_message, ובנוסף מייצר תשובה אוטומטית ורושם אותה בהיסטוריה כהודעה יוצאת."""
    card = process_message(phone, message_text, tenant_id=tenant_id, source_channel=source_channel)
    reply_text = generate_reply(message_text, card)

    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    card = customers[key]
    _append_history(card, source_channel, reply_text, direction="out")
    customers[key] = card
    save_customers(customers)

    return card, reply_text


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
    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    card = customers.get(key, {"phone": phone, "tenant_id": tenant_id})
    card.setdefault("source_channel", source_channel)
    _append_history(card, source_channel, message, direction="out", simulated=simulated)
    customers[key] = card
    save_customers(customers)
    return card


def log_call_summary(
    phone: str,
    summary: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    simulated: bool = False,
) -> dict:
    """רושם תקציר שיחת טלפון (מיוצר ע"י Claude מתוך הערות חופשיות שהקליד הנציג -
    ראו generate_call_summary) בהיסטוריית הכרטיס, בערוץ channel="voice". בכוונה לא
    נוגע ב-lead_status - כמו log_manual_reply."""
    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    card = customers.get(key, {"phone": phone, "tenant_id": tenant_id})
    card.setdefault("source_channel", "whatsapp")  # לא דורסים את הערוץ המקורי שהליד הגיע ממנו
    _append_history(card, "voice", summary, direction="out", simulated=simulated)
    customers[key] = card
    save_customers(customers)
    return card


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
) -> tuple[dict, bool]:
    """יוצר/מעדכן כרטיס לקוח - מייבוא CSV/Excel (ראו /api/leads/import) או מהוספת
    ליד בודד ידנית (POST /api/leads) ב-server.py. לא הודעה - אין רישום ב-history.
    Upsert לפי phone+tenant_id (מונע כפילויות). מחזיר (card, is_new) - is_new=True
    אם זה ליד חדש שלא היה קיים קודם."""
    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    is_new = key not in customers
    card = customers.get(key, {"phone": phone, "tenant_id": tenant_id})
    if customer_name:
        card["customer_name"] = customer_name
    if business_name:
        card["business_name"] = business_name
    if location:
        card["location"] = location
    if lead_status:
        card["lead_status"] = lead_status
    if import_source:
        card["import_source"] = import_source
    if category:
        card["category"] = category
    if agent:
        card["agent"] = agent
    customers[key] = card
    save_customers(customers)
    return card, is_new


def update_lead_category(phone: str, category: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מעדכן קטגוריה חופשית לליד (למשל "נדל\"ן"/"פרטי"/"משפחה") - שדה סיווג ידני,
    לא קשור ל-lead_status. category ריק ("") מנקה את השדה. לא נרשם ב-history - זה
    מטא-דאטה כמו import_source, לא אינטראקציה עם הלקוח."""
    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    card = customers.get(key, {"phone": phone, "tenant_id": tenant_id})
    if category:
        card["category"] = category
    else:
        card.pop("category", None)
    customers[key] = card
    save_customers(customers)
    return card


def update_lead_agent(phone: str, agent: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """מעדכן "סוכן מטפל" (agent) - שדה טקסט חופשי, מטא-דאטה בלבד (בדיוק כמו
    update_lead_category - לא נרשם ב-history, לא קשור ל-lead_status). agent ריק
    ("") מנקה את השדה."""
    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    card = customers.get(key, {"phone": phone, "tenant_id": tenant_id})
    if agent:
        card["agent"] = agent
    else:
        card.pop("agent", None)
    customers[key] = card
    save_customers(customers)
    return card


def update_lead_status(
    phone: str,
    status: str,
    extra: dict | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_channel: str = "whatsapp",
    note: str | None = None,
    direction: str = "out",
) -> dict:
    customers = load_customers()
    key = _customer_key(tenant_id, phone)
    card = customers.get(key, {"phone": phone, "tenant_id": tenant_id})
    card.setdefault("source_channel", source_channel)
    card["lead_status"] = status
    for k, value in (extra or {}).items():
        if value:
            card[k] = value
    if note:
        _append_history(card, source_channel, note, direction=direction)
    customers[key] = card
    save_customers(customers)
    return card


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
