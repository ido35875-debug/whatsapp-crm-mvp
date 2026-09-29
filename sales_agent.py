"""
סוכן מכירות והתנגדויות (Sales & Objection Agent) - ראו "חלוקת סוכנים" ב-CLAUDE.md.
מנתח את ההודעה האחרונה **שהתקבלה מהלקוח** (direction="in" - נקבע ע"י הקורא, לא
כאן) ומציע עד 3 ניסוחי מענה מוכנים לנציג אנושי, יחד עם קטגוריית ההתנגדות שזוהתה
(מחיר/"אחזור אליך"/הססנות/השוואה למתחרה/אחר) - כדי שהנציג יבין מהר מול מה הוא
עומד, לא רק מה לענות.

**מחליף** את extract.generate_objection_response הקודם (הצעה בודדת, בלי סיווג) -
לא נשמר endpoint/פונקציה מקבילה לאותה יכולת בדיוק (ראו POST /api/sales/suggest-
reply ב-server.py וכפתור "🎯 הצעות תגובה" ב-index.html, שהחליפו את "💬 הצע מענה
להתנגדות"/POST /api/leads/objection-response).

חוק בטיחות קשיח (CLAUDE.md #1) - נאכף גם במבנה הפרומפט עצמו (SALES_OBJECTION_
PROMPT), לא רק בתיעוד: הסוכן הזה **אף פעם** לא סוגר עסקה, לא מוחק נתונים, ולא
מתחייב על מחיר/הנחה/תנאים באופן אוטונומי. אין כאן שום קריאת כתיבה בכלל - לא
ל-customers.json (extract.py), לא ל-crm_data.db (db.py) - כל טקסט שהמודול הזה
מייצר הוא הצעה-בלבד, שהנציג בוחר/עורך/מאשר לשלוח בעצמו (POST /api/messages/send
הקיים). suggest_replies מקבל את היסטוריית השיחה ופרטי הליד כפרמטרים (שנשלפים
ב-server.py דרך extract.load_customers/db.get_messages) - המודול הזה עצמו לא
פותח customers.json/crm_data.db בשום מקום.
"""

import json
import os
import re
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

import prompts

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ארבע קטגוריות ההתנגדות הנפוצות שהוגדרו במפורש (מחיר/דחיית-החלטה/הססנות/מתחרה) +
# "other" - קטגוריית נפילה-רכה לכל הודעה שלא משויכת בבירור לאף אחת מהן, כדי
# ש-classify_objection לא תיאלץ "לדחוף בכוח" הודעה לקטגוריה שלא מתאימה לה.
OBJECTION_CATEGORIES: dict[str, str] = {
    "price": "מחיר/עלות - הלקוח חושב שזה יקר או מתמקח",
    "callback": '"אחזור אליך"/דחיית החלטה - הלקוח רוצה לדחות את זה להמשך',
    "hesitation": "היסוס כללי/חוסר ודאות - לא סירוב מפורש, אבל לא סגור על זה",
    "competitor": "השוואה למתחרה - הלקוח מזכיר אלטרנטיבה/מתחרה",
    "other": "התנגדות/הודעה שלא משויכת בבירור לאף קטגוריה למעלה",
}


CLASSIFY_OBJECTION_PROMPT = """\
סווג את ההודעה הבאה שהתקבלה מלקוח פוטנציאלי לקטגוריית התנגדות אחת בלבד, מתוך
הרשימה: price, callback, hesitation, competitor, other.

הודעת הלקוח:
"{last_message}"

החזר אך ורק את שם הקטגוריה (מילה אחת מהרשימה למעלה, באנגלית, בלי מרכאות/הסברים).
"""


def classify_objection(last_message: str) -> str:
    """מסווג את ההודעה האחרונה לאחת מ-OBJECTION_CATEGORIES - קריאת Claude זולה
    ונפרדת מניסוח ההצעות עצמן (max_tokens נמוך משמעותית). נופל ל-"other" אם
    המודל החזיר משהו שלא ברשימה - לא מעלה חריגה על תשובה לא-צפויה."""
    prompt_template = prompts.get_prompt("sales_objection_classify", CLASSIFY_OBJECTION_PROMPT)
    prompt = prompt_template.format(last_message=last_message)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=10,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(block.text for block in response.content if block.type == "text").strip().lower()
    return text if text in OBJECTION_CATEGORIES else "other"


SALES_OBJECTION_PROMPT = """\
אתה עוזר לנציג מכירות אנושי לנסח מענה להודעה האחרונה שהתקבלה מלקוח פוטנציאלי
בוואטסאפ. קטגוריית ההתנגדות שזוהתה בהודעה: {category_label}.
פרטים ידועים על הלקוח (אם יש): שם - {customer_name}, עסק - {business_name}, מיקום - {location}.

היסטוריית השיחה עד כה (אם יש, מהישנה לחדשה):
{history_text}

ההודעה האחרונה מהלקוח:
"{last_message}"

--- סגנון כתיבה (קריטי - מענה אנושי 100%) ---
כתוב בדיוק כמו שנציג אנושי אמיתי כותב בוואטסאפ - עברית ישראלית טבעית, חמה
וישירה, בגובה העיניים. בלי שפה רשמית/משרדית ("לתשומת ליבך", "הריני", "בברכה"),
בלי ניסוחים רובוטיים, ובלי תפריטים/רשימות בתוך ההודעה עצמה. כל ניסוח - 1-2
משפטים קצרים בלבד. לעולם לא פסקה ארוכה ולא רשימת בולטים.

--- טיפול רך בהתנגדות (קריטי) ---
אל תתווכח עם הלקוח ואל תמכור בכוח. אם ההתנגדות היא סירוב-רך ("יקר", "לא
מעוניין", "אין לי זמן" וכיו"ב) - הגב באמפתיה קצרה וכנה (משפט אחד, לא יותר),
ומיד אחריה שאל שאלה אחת חדה וממוקדת שמפנה את השיחה לערך האמיתי או לתיאום
שיחה קצרה - לא שאלה כללית/פתוחה מדי.

--- הנעה לשלב הבא (קריטי) ---
כל ניסוח חייב לחתור בטבעיות לצעד קונקרטי הבא - קביעת שיחה קצרה (טלפון/וידאו)
ביומן, או השלמת הפרט החסר שהכי מקדם את התהליך - לא להשאיר את ההודעה "תלויה
באוויר" בלי כיוון ברור להמשך.

הצע בדיוק 3 ניסוחי מענה שונים בעברית, כל אחד עומד במלוא ההנחיות מעל, ומתייחס
ישירות למה שהלקוח כתב. שלושת הניסוחים צריכים להיות גישות שונות באמת (למשל:
אחד שמציע לקבוע שיחה קצרה, אחד שמדגיש ערך בקצרה, אחד ששואל שאלת-המשך חדה) -
לא שלוש גרסאות-ניסוח כמעט-זהות לאותו רעיון.

⚠️ הגבלות מחייבות - אל תפר אותן באף אחד משלושת הניסוחים:
- אל תסגור עסקה, אל תאשר הזמנה, ואל תגיד שהעסקה "סגורה"/"מאושרת".
- אל תתחייב על מחיר, הנחה, תנאי תשלום, או תאריך/זמינות ספציפיים - אם המחיר עלה
  כהתנגדות, הפנה לשיחה עם הנציג ("אשמח לדבר איתך על זה ישירות") ואל תציע מספר.
- אל תמציא מבצעים/הבטחות/פרטים שלא ניתנו בהיסטוריה למעלה.

החזר **מערך JSON אחד ויחיד** (לא שלושה מערכים נפרדים, לא טקסט נוסף לפני/אחרי,
לא כותרות/מספור) עם בדיוק 3 מחרוזות, בפורמט המדויק הזה - שורה אחת, סוגר מרובע
אחד בהתחלה וסוגר מרובע אחד בסוף:
["ניסוח ראשון", "ניסוח שני", "ניסוח שלישי"]
"""


def suggest_replies(last_message: str, card: dict, history_text: str = "") -> dict:
    """מנתח הודעה אחרונה שהתקבלה מלקוח ומחזיר {category, category_label, replies}
    - עד 3 הצעות מענה מנוסחות. לא שולח כלום, לא כותב כלום ל-DB (ראו אזהרת
    הבטיחות בראש הקובץ). card: כרטיס הליד (מ-extract.load_customers, נשלף
    ע"י הקורא - server.py). history_text: טקסט היסטוריה מרוכז אופציונלי (אם
    ריק, המודל מקבל רק את ההודעה האחרונה עצמה, בלי הקשר קודם)."""
    category = classify_objection(last_message)
    category_label = OBJECTION_CATEGORIES[category]

    prompt_template = prompts.get_prompt("sales_objection_replies", SALES_OBJECTION_PROMPT)
    prompt = prompt_template.format(
        category_label=category_label,
        customer_name=card.get("customer_name") or "לא ידוע",
        business_name=card.get("business_name") or "לא ידוע",
        location=card.get("location") or "לא ידוע",
        history_text=history_text or "(אין הקשר נוסף מעבר להודעה האחרונה)",
        last_message=last_message,
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=500,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = next(block.text for block in response.content if block.type == "text").strip()
    replies = _parse_replies(raw_text)

    return {"category": category, "category_label": category_label, "replies": replies}


def _parse_replies(raw_text: str) -> list[str]:
    """מפרק את פלט המודל לרשימת מחרוזות - עמיד לשני סוגי סטייה מהפורמט המבוקש
    (מערך JSON אחד): (1) המודל (לעיתים נדירות) מחזיר כמה מערכי JSON נפרדים
    ברצף (אחד לכל הצעה) במקום מערך מאוחד אחד, למרות ההנחיה בפרומפט - נצפה
    בפועל תוך כדי הבדיקה; (2) טקסט חופשי בלי JSON תקין בכלל. בשני המקרים עדיף
    לחלץ הצעות אמיתיות/להציג טקסט גולמי-אחד מאשר להחזיר בלוק-ג'אנק אחד או 500."""
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, list):
            replies = [str(item).strip() for item in parsed if str(item).strip()]
            if replies:
                return replies[:3]
    except json.JSONDecodeError:
        pass

    replies = []
    for match in re.finditer(r"\[[^\[\]]*\]", raw_text, re.DOTALL):
        try:
            chunk = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(chunk, list):
            replies.extend(str(item).strip() for item in chunk if str(item).strip())
        elif isinstance(chunk, str) and chunk.strip():
            replies.append(chunk.strip())
    if replies:
        return replies[:3]

    return [raw_text] if raw_text else []
