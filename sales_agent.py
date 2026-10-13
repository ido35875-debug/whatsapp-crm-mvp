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


# ---- Hot Lead Alert - זיהוי כוונת רכישה/בקשת שיחה/בקשת תיאום ביקור, לסימון
# מיידי של הליד כ-"hot" בכרטיס (ראו server.py._process_incoming_message_job,
# שקורא ל-detect_hot_lead_signal ברקע אחרי כל הודעה נכנסת ומעדכן lead_status
# מיידית אם is_hot=true - התראה/חימום אוטומטי, לא רק אינדיקציה פסיבית).
# ניתוח-בלבד (read-only, קריאת Claude זולה אחת) - בדיוק כמו scheduling_agent.
# detect_scheduling_intent: לא כותבת שום דבר בעצמה, הקורא מחליט מה לעשות
# עם התוצאה. ----

HOT_LEAD_PROMPT = """\
נתח את ההודעה הבאה שהתקבלה מלקוח פוטנציאלי בוואטסאפ, ובדוק אם היא מראה כוונת
קנייה ברורה, בקשה מפורשת לשיחת טלפון, או בקשה לתאם ביקור/פגישה בנכס/בעסק.

הקשר קודם לשיחה (אם יש, מהישן לחדש):
{history_text}

הודעת הלקוח:
"{message}"

אם ההודעה היא שאלה כללית/בירור ראשוני/תגובה סתמית בלי אחד מהסימנים למעלה -
החזר אך ורק: {{"is_hot": false}}

אם יש בהודעה סימן ברור (כוונת קנייה/רכישה מפורשת, בקשה לשיחת טלפון, או בקשה
לתאם ביקור/פגישה) - החזר JSON עם השדות הבאים בדיוק:
- "is_hot": true
- "reason": משפט אחד קצר בעברית שמסביר מה בהודעה הצביע על זה

היה שמרן - עדיף false אם לא בטוח, לא לנחש. החזר אך ורק JSON תקין, בלי טקסט
נוסף לפני/אחרי.
"""


def detect_hot_lead_signal(message: str, history_text: str = "") -> dict:
    """מנתח הודעה אחת ומחזיר {is_hot, reason}. לא כותבת שום דבר, לא ניגשת ל-DB.
    "is_hot": false הוא המקרה הנפוץ (רוב ההודעות הנכנסות אינן סימן חם) - לא שגיאה.
    נופלת בעדינות ל-{"is_hot": false} על כל כשל פענוח - זיהוי-חום שהתפספס הוא
    הרבה פחות גרוע מחריגה שמפילה את כל עיבוד ההודעה."""
    prompt_template = prompts.get_prompt("hot_lead_detection", HOT_LEAD_PROMPT)
    prompt = prompt_template.format(history_text=history_text or "(אין הקשר נוסף)", message=message)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=150,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = next(block.text for block in response.content if block.type == "text").strip()
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"is_hot": False}
    if not isinstance(parsed, dict) or not parsed.get("is_hot"):
        return {"is_hot": False}
    return {"is_hot": True, "reason": parsed.get("reason") or ""}


# ---- סינון שיחת-חולין אישית (לא עסקית) מאנשי קשר חדשים/לא-מזוהים - ראו
# server.py._process_incoming_message_job: מופעל **רק** לפני המענה האוטומטי
# הראשון לכרטיס שאין לו עדיין שום הקשר נדל"ן/מכירתי מתועד (property_type/
# budget/lead_status מעבר ל-"new") - ברגע שליד הראה הקשר עסקי כלשהו, לא נבדק
# שוב אף פעם, כדי לא "לשתוק" באמצע שיחת מכירה אמיתית רק כי הודעה בודדת
# הייתה קז'ואלית. שמרן בכוונה (עדיף true/לענות מאשר false שגוי שמפספס ליד
# אמיתי - Speed-to-Lead) - ראו RELEVANCE_PROMPT. ----

RELEVANCE_PROMPT = """\
אתה מסנן הודעות וואטסאפ נכנסות לבוט מכירות אוטומטי **של עסק נדל"ן** (סוכנות/
יזם נדל"ן - חיפוש/מכירת/השכרת נכסים), כדי להחליט אם ההודעה הבאה היא פנייה
אפשרית הקשורה לנדל"ן (ליד פוטנציאלי - חיפוש נכס, מחיר, אזור, פגישה/ביקור בנכס -
גם אם מנוסחת בעקיפין או בקיצור), או שיחת חולין פרטית שאין לה שום קשר לנדל"ן
(למשל עם חבר/משפחה - שוק ההון/דוחות כספיים של חברה, ספורט, עדכונים אישיים,
חדשות כלליות, בדיחות). שים לב: עצם זה שההודעה "עוסקת בכסף/מספרים" (כמו דוחות
כספיים, מניות) **אינו** הופך אותה לרלוונטית - זה עסק נדל"ן, לא ייעוץ השקעות.

זהו איש קשר חדש/לא-מזוהה - אין עדיין היסטוריית שיחה עסקית מתועדת איתו.

הודעת הלקוח:
"{message}"

היה שמרן: אם יש סיכוי סביר שזו פנייה שקשורה בפועל לנדל"ן (גם אם לא מפורשת) -
סמן is_relevant=true. סמן is_relevant=false אם ברור שזו שיחת חולין פרטית בלי
שום זיקה אפשרית לנדל"ן - גם אם היא "נשמעת עסקית" בתחום אחר (פיננסים, מניות
וכו').

החזר אך ורק JSON תקין בפורמט הבא, בלי טקסט נוסף:
{{"is_relevant": true/false, "reason": "משפט קצר בעברית שמסביר את ההחלטה"}}
"""


def detect_real_estate_relevance(message: str) -> dict:
    """מנתח הודעה אחת ומחזיר {is_relevant, reason} - האם זו פנייה אפשרית
    הקשורה לנדל"ן, או שיחת חולין פרטית בלי שום קשר לנדל"ן (גם אם היא "נשמעת
    עסקית" בתחום אחר, כמו שוק ההון - ראו ההערה ב-RELEVANCE_PROMPT). נופלת
    בעדינות ל-{"is_relevant": True} (לא חוסמת) על כל כשל פענוח/JSON - עדיף
    לענות ללקוח אמיתי בטעות מאשר לפספס ליד בגלל כשל טכני בסיווג."""
    prompt_template = prompts.get_prompt("real_estate_relevance_detection", RELEVANCE_PROMPT)
    prompt = prompt_template.format(message=message)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=120,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = next(block.text for block in response.content if block.type == "text").strip()
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"is_relevant": True, "reason": "כשל פענוח - ברירת מחדל: לא חוסמים"}
    if not isinstance(parsed, dict) or "is_relevant" not in parsed:
        return {"is_relevant": True, "reason": "תשובה לא תקינה - ברירת מחדל: לא חוסמים"}
    return {"is_relevant": bool(parsed["is_relevant"]), "reason": parsed.get("reason") or ""}


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
