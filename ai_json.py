"""
עוזר משותף לכל סוכני ה-AI בפרויקט (extract.py/scheduling_agent.py/sales_agent.py/
command_copilot.py/note_entity_extraction.py) - מפענח תשובת JSON מ-Claude תוך
הגנה מפני עטיפת markdown code fence (```json ... ``` או ``` ... ```) שהמודל
מוסיף לפעמים למרות הנחיה מפורשת בפרומפט ("החזירו אך ורק JSON, בלי טקסט נוסף")
לא להוסיף טקסט נוסף.

⚠️ זה לא תקלה תיאורטית - נמדד בפועל: מתוך 6 קריאות חזרתיות לאותו prompt
(note_entity_extraction, בבדיקה ש-session זה ביצע), 4 חזרו עטופות ב-```json
ו-2 לא - קצב כשל-שקט של כ-66% לפרומפט הזה (JSONDecodeError שנתפס ב-except
Exception הגנרי של כל סוכן ונחזר כ"לא נמצאה תוצאה", בלי שגיאה גלויה בלוג).
כל שבעת מקומות הקריאה ל-json.loads(raw_text) בפרויקט (ראו git grep) היו
חשופים לאותו דבר - כולל extract.py, צינור-החילוץ המרכזי שרץ על *כל* הודעת
וואטסאפ נכנסת, לא רק על התכונה החדשה הזו.
"""

import json


def parse_json_response(raw_text: str):
    """מנקה code fence מתחילת/סוף הטקסט (אם קיים) ואז json.loads. מעלה
    json.JSONDecodeError כרגיל אם אחרי הניקוי עדיין לא JSON תקין - הקוראים
    ממשיכים לתפוס את זה ב-except שלהם כמו קודם (fail-soft, לא חריגה
    שמפילה את השרת) - רק שכעת הם לא נכשלים *בטעות* על תשובה שבפועל הייתה
    JSON תקין, רק עטוף ב-```."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.lstrip("\n")
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
    return json.loads(cleaned)
