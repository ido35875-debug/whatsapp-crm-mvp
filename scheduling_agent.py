"""
סוכן ניהול פגישות/יומן (Scheduling & Follow-Up Agent) - ראו "חלוקת סוכנים"
ב-CLAUDE.md. מנתח הודעה נכנסת מהלקוח (טקסט + היסטוריית שיחה - שני-מקורות,
נשלפים ב-server.py דרך extract.py/db.py ומועברים לכאן כפרמטרים; המודול הזה
עצמו לא פותח customers.json) ומזהה כוונת תיאום פגישה חדשה, ביטול, או הזזת
מועד של פגישה קיימת.

⚠️ חוק בטיחות קשיח (CLAUDE.md #1 + הרחבה מפורשת שהוגדרה לסוכן הזה): "אף
פגישה לא תימחק, תבוטל או תתוזמן מחדש מול הלקוח בלי אישור אנושי ב-UI."

איך זה נאכף בפועל, לא רק בתיעוד:
- detect_scheduling_intent הוא ניתוח-בלבד (read-only, קריאת Claude אחת) -
  לא כותב שום דבר, לא נוגע ב-DB.
- create_scheduling_proposal **כן** כותבת ל-calendar_tasks (דרך db.py) - אבל
  היא **תמיד** יוצרת שורת "הצעה" חדשה עם status="pending_confirmation",
  **אף פעם** לא משנה/מוחקת ישירות שורה קיימת. שורה כזו אינה פגישה אמיתית -
  היא לא מוצגת ללקוח, לא נשלחת, ולא משפיעה על שום פגישה קיימת - רק "מחכה"
  ביומן לסקירת נציג. זו בדיוק אותה מדיניות כמו extract.update_lead_
  voice_extraction (שכבר כותב שדות לכרטיס אוטומטית מכל הודעה קולית נכנסת
  בלי אישור) - כי גם זו פעולת-רישום פנימית, לא פעולה כלפי הלקוח.
- הביטול/הזזת המועד בפועל של פגישה **קיימת** קורים אך ורק בתוך
  db.confirm_task_proposal - ופונקציה זו נקראת **רק** מ-POST /api/tasks/
  <id>/confirm ב-server.py, כלומר רק מקליק נציג מפורש על "✅ אשר" ב-UI.
  scheduling_agent.py עצמו אף פעם לא קורא ל-confirm_task_proposal/
  reject_task_proposal - האוטומציה נעצרת ביצירת ההצעה בלבד.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

import db
import prompts

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

_HEBREW_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

VALID_INTENTS = {"new", "cancel", "reschedule"}

_PROPOSAL_TITLE_PREFIX = {
    "new": "🤖 הצעת פגישה חדשה",
    "cancel": "🤖 בקשת ביטול פגישה קיימת",
    "reschedule": "🤖 בקשת שינוי מועד פגישה קיימת",
}


SCHEDULING_INTENT_PROMPT = """\
היום התאריך {today_date} ({today_weekday}).
נתח את ההודעה הבאה שהתקבלה מלקוח פוטנציאלי בוואטסאפ, ובדוק אם יש בה בקשה
לתיאום פגישה חדשה, ביטול פגישה קיימת, או הזזת מועד פגישה קיימת.

הקשר קודם לשיחה (אם יש, מהישן לחדש):
{history_text}

הודעת הלקוח:
"{message}"

אם אין בהודעה שום בקשה מהסוג הזה (שאלה כללית, תשובה לא-קשורה, וכו') - החזר
אך ורק: {{"has_intent": false}}

אם יש בקשה כזו - החזר JSON עם השדות הבאים בדיוק:
- "has_intent": true
- "intent": אחד מ- "new" (פגישה חדשה) / "cancel" (ביטול פגישה קיימת) /
  "reschedule" (הזזת מועד פגישה קיימת)
- "date": התאריך המבוקש בפורמט YYYY-MM-DD - חשב לפי התאריך של היום שניתן
  למעלה אם נכתב ביטוי יחסי ("מחר", "יום שלישי הבא" וכו'); null אם לא ברור
- "time": השעה המבוקשת בפורמט HH:MM (24 שעות); null אם לא צוינה
- "summary": משפט אחד קצר בעברית שמסכם את הבקשה בפועל, לשימוש הנציג בתצוגה

אל תמציא תאריך/שעה שלא ניתן להסיק בבירור מההודעה - עדיף null מתשובה מנוחשת.
החזר אך ורק JSON תקין, בלי טקסט נוסף לפני/אחרי.
"""


def detect_scheduling_intent(message: str, history_text: str = "") -> dict:
    """מנתח הודעה אחת ומחזיר {has_intent, intent, date, time, summary}. לא
    כותב שום דבר, לא ניגש ל-DB. "has_intent": false הוא המקרה הנפוץ (רוב
    ההודעות הנכנסות אינן בקשות תזמון) - לא שגיאה."""
    now = datetime.now()
    prompt_template = prompts.get_prompt("scheduling_intent_detection", SCHEDULING_INTENT_PROMPT)
    prompt = prompt_template.format(
        today_date=now.strftime("%Y-%m-%d"),
        today_weekday=_HEBREW_WEEKDAYS[now.weekday()],
        history_text=history_text or "(אין הקשר נוסף)",
        message=message,
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=200,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = next(block.text for block in response.content if block.type == "text").strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"has_intent": False}

    if not isinstance(parsed, dict) or not parsed.get("has_intent"):
        return {"has_intent": False}
    if parsed.get("intent") not in VALID_INTENTS:
        return {"has_intent": False}

    return {
        "has_intent": True,
        "intent": parsed["intent"],
        "date": parsed.get("date") or None,
        "time": parsed.get("time") or None,
        "summary": parsed.get("summary") or "",
    }


def find_related_task(phone: str, tenant_id: str) -> dict | None:
    """מוצאת את הפגישה הקיימת (status='pending') הכי קרובה בזמן לליד הזה -
    השערה סבירה ל"על איזו פגישה הלקוח מדבר" בבקשת ביטול/הזזת מועד
    (db.get_tasks כבר ממיינת לפי due_date/due_time עולה - פגישה 1 היא
    הבאה בתור). None אם אין אף פגישה פתוחה בכלל לליד הזה - הקורא
    (create_scheduling_proposal) עדיין יוצר הצעה, רק בלי related_task_id,
    כדי שהנציג ידע לבדוק ידנית איזו פגישה מדובר, במקום שההודעה "תיעלם"."""
    pending = db.get_tasks(phone=phone, tenant_id=tenant_id, status="pending")
    return pending[0] if pending else None


def create_scheduling_proposal(
    phone: str, tenant_id: str, intent_result: dict, related_task_id: int | None = None
) -> int:
    """יוצרת שורת **הצעה** ב-calendar_tasks (status="pending_confirmation",
    source="scheduling_agent") מתוך תוצאת detect_scheduling_intent - לא
    פגישה אמיתית עד אישור נציג מפורש (ראו אזהרת הבטיחות בראש הקובץ). מוחזר
    ה-id של שורת ההצעה.

    due_date/due_time על שורת ההצעה: לתאריך/שעה שהתבקשו במפורש (new/
    reschedule); ל-"cancel" בלי תאריך מפורש בהודעה, נלקחים במקום זאת מהפגישה
    המקורית (related_task_id) - כדי שיהיה ברור לנציג בתצוגה על איזו פגישה
    בדיוק מדובר, בלי לפתוח את הפגישה המקורית בנפרד."""
    intent = intent_result["intent"]
    due_date = intent_result.get("date")
    due_time = intent_result.get("time")

    if not due_date and related_task_id:
        original = db.get_task(related_task_id)
        if original:
            due_date = due_date or original["due_date"]
            due_time = due_time or original["due_time"]

    title_prefix = _PROPOSAL_TITLE_PREFIX.get(intent, "🤖 בקשת תזמון")
    summary = intent_result.get("summary") or ""
    title = f"{title_prefix}: {summary}" if summary else title_prefix

    notes_parts = ['זוהה אוטומטית ע"י סוכן היומן (scheduling_agent) - ממתין לאישור נציג.']
    if related_task_id:
        notes_parts.append(f"מתייחס לפגישה קיימת #{related_task_id}.")
    elif intent in ("cancel", "reschedule"):
        notes_parts.append("⚠️ לא נמצאה פגישה פתוחה תואמת אצל הליד הזה - נדרשת בדיקה ידנית.")

    return db.create_task(
        phone,
        tenant_id=tenant_id,
        title=title,
        due_date=due_date or datetime.now().strftime("%Y-%m-%d"),
        due_time=due_time,
        notes=" ".join(notes_parts),
        status="pending_confirmation",
        source="scheduling_agent",
        intent=intent,
        related_task_id=related_task_id,
    )
