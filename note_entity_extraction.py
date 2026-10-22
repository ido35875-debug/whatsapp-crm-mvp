"""
NLP Entity Extraction + Auto-Scheduling עבור הערות פנימיות (channel="note") על
ליד - ממיר משפט חופשי שנציג כותב (למשל "מתעניין בפרויקט בהוד השרון בזכות
לארבעה חדרים, גר בנס ציונה עם אשתו, להתקשר ביום רביעי בארבע") לשדות מבניים
על הכרטיס (rooms/project/city) + משימת follow-up אוטומטית (תאריך+שעה) - ראו
server.py: POST /api/messages/note קורא לזה ברקע (thread נפרד, לא חוסם את
תגובת ה-HTTP) אחרי שההערה עצמה נשמרה.

⚠️ מודל הבטיחות: extract_note_entities הוא ניתוח-בלבד (קריאת Claude אחת,
read-only) - זהה בדיוק לכל שאר סוכני ה-AI בפרויקט (ראו scheduling_agent.py/
sales_agent.py). "שדות מבניים שמתעדכנים אוטומטית על הכרטיס" היא אותה
מדיניות בדיוק כמו extract.update_lead_voice_extraction (חילוץ-AI קיים
מהודעות קוליות, שגם הוא כותב לכרטיס בלי אישור ידני נקודתי) - זו פעולת-רישום
פנימית (metadata), לא פעולה כלפי הלקוח. משימת ה-follow-up שנוצרת היא
תזכורת-לעצמנו (status="pending" רגיל, לא "pending_confirmation") - היא לא
"פגישה שנקבעת מול הלקוח בלי אישור" (מה שחוק הבטיחות #1 אוסר): היא תזכורת
פנימית לנציג להתקשר, בדיוק כמו "⏰ חזור בהמשך" (openCallbackPanel) שקיים
כבר בדשבורד - ההבדל היחיד הוא שכאן היא נוצרת אוטומטית מניתוח ה-AI על ההערה,
לא מטופס שמולא ביד. היא עדיין לא שולחת שום דבר ללקוח ולא "סוגרת" שום פגישה.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

import prompts

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

_HEBREW_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

NOTE_EXTRACTION_PROMPT = """\
היום התאריך {today_date} ({today_weekday}).

נתח את ההערה הפנימית הבאה שנציג מכירות כתב על ליד נדל"ן (לא הודעה מהלקוח -
תיעוד פנימי של הנציג על השיחה/המצב), וחלץ ממנה פרטים מבניים, בעברית או
אנגלית:

הערה: "{note_text}"

החזר JSON עם השדות הבאים בדיוק - null בכל שדה שלא מוזכר/לא ברור, אל תמציאו
ערך:
- "rooms": מספר חדרים כמחרוזת מספר (למשל "4") - רק אם מוזכר במפורש כמות
  חדרים הרצויה ללקוח
- "project": שם פרויקט/בניין נדל"ני *ספציפי* שהוזכר בשם - רק אם צוין שם
  ספציפי (לא רק עיר/אזור כלליים - "פרויקט בהוד השרון" בלי שם ספציפי לא נחשב)
- "city": העיר/האזור שבו הלקוח *מעוניין לרכוש/לשכור* - אם מוזכרות כמה ערים
  (למשל עיר-התעניינות מול עיר-מגורים נוכחית של הלקוח) - קחו את עיר
  ה*התעניינות* (איפה מחפש נכס), לא את עיר המגורים הנוכחית שלו
- "has_followup": true אם יש בהערה בקשה/כוונה מפורשת לחזור/להתקשר/לקבוע
  (זמן עתידי קונקרטי) - אחרת false
- "followup_date": YYYY-MM-DD אם has_followup - חשבו לפי התאריך של היום אם
  כתוב ביטוי יחסי ("יום רביעי", "מחר", "בעוד שבוע" וכו'); null אם לא ברור
  מספיק לחשב
- "followup_time": HH:MM בפורמט 24 שעות אם צוינה שעה מפורשת ("בארבע" = 16:00
  בהקשר עסקי של שעות עבודה, אלא אם ברור אחרת מההקשר); null אם לא צוינה שעה

אל תמציאו תאריך/שעה/שם-פרויקט שלא ניתן להסיק בבירור מהטקסט - עדיף null
מתשובה מנוחשת. החזרו אך ורק JSON תקין, בלי טקסט נוסף לפני/אחרי.
"""


def extract_note_entities(note_text: str) -> dict:
    """מנתחת הערה חופשית אחת. לא כותבת שום דבר, לא נוגעת ב-DB - הקורא
    (server.py) מחליט מה לעשות עם התוצאה (עדכון שדות/יצירת משימה).

    מחזירה תמיד dict עם rooms/project/city (כל אחד str או None) ו-
    has_followup/followup_date/followup_time. כשל כלשהו (AI/JSON לא-תקין)
    מחזיר dict "ריק" (הכל None/False) במקום להעלות חריגה - קריאה אחת
    שנכשלת לא אמורה למנוע את שמירת ההערה עצמה (שכבר קרתה קודם, ראו
    server.api_add_note) או להפיל את thread הרקע שקורא לפונקציה הזו."""
    note_text = (note_text or "").strip()
    empty_result = {
        "rooms": None, "project": None, "city": None,
        "has_followup": False, "followup_date": None, "followup_time": None,
    }
    if not note_text:
        return empty_result

    now = datetime.now()
    prompt_template = prompts.get_prompt("note_entity_extraction", NOTE_EXTRACTION_PROMPT)
    prompt = prompt_template.format(
        today_date=now.strftime("%Y-%m-%d"),
        today_weekday=_HEBREW_WEEKDAYS[now.weekday()],
        note_text=note_text,
    )
    try:
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=300,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = next(block.text for block in response.content if block.type == "text").strip()
        parsed = json.loads(raw_text)
    except Exception:
        return empty_result

    if not isinstance(parsed, dict):
        return empty_result

    def _clean_str(value) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    rooms = _clean_str(parsed.get("rooms"))
    # rooms חייב "להרגיש" כמו ספרה (1-2 תווים, תווים מספריים) - לא מחרוזת
    # חופשית שה-AI בטעות שם כאן (למשל "כמה" או משפט שלם) - עדיף None מערך
    # חשוד, כדי לא ללכלך שדה מבני עם טקסט לא-מספרי.
    if rooms and not any(ch.isdigit() for ch in rooms):
        rooms = None

    has_followup = bool(parsed.get("has_followup"))
    followup_date = _clean_str(parsed.get("followup_date")) if has_followup else None
    if followup_date:
        try:
            datetime.strptime(followup_date, "%Y-%m-%d")
        except ValueError:
            followup_date = None
    followup_time = _clean_str(parsed.get("followup_time")) if (has_followup and followup_date) else None

    return {
        "rooms": rooms,
        "project": _clean_str(parsed.get("project")),
        "city": _clean_str(parsed.get("city")),
        "has_followup": has_followup and bool(followup_date),
        "followup_date": followup_date,
        "followup_time": followup_time,
    }
