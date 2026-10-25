"""
Command Copilot (Quick Action Bar) - ממיר הוראה חופשית בעברית/אנגלית (למשל
"עדכן את יאיר כהן לחם, הוסף הערה שקבענו פגישה, ותזמן פולו-אפ ליום שלישי")
לרשימת פעולות מובנות על ליד *יחיד* קיים - בלי לכתוב שום דבר ל-DB בעצמו.

⚠️ מודל הבטיחות (זהה בדיוק לעיקרון של scheduling_agent.py - ראו שם): parse_
command הוא read-only - קריאת Claude אחת, לא נוגע ב-DB בכלל. הביצוע בפועל
קורה רק ב-server.py (POST /api/copilot/execute), ורק אחרי שהנציג אישר
במפורש את תצוגת-התצוגה-המקדימה ב-UI (Command Bar -> preview -> confirm) -
אין "שלב שלישי שקט" שמבצע משהו בלי לעבור קודם את שני השלבים האלה.

זיהוי הליד: ה-AI מקבל רשימת לידים *מועמדים* (phone/tenant_id/customer_name/
business_name/lead_status) שכבר סוננה בשרת (ראו server._copilot_candidate_
leads) לפי הרשאות המשתמש המחובר (_effective_tenant_ids/_effective_client_id) -
לא יכול "לנחש" או לגעת בליד שהמשתמש לא מורשה לראות מלכתחילה, כי הליד פשוט
לא ברשימה שקיבל. בנוסף, parse_command **לא בוטח** ב-phone/tenant_id שה-AI
מחזיר כפשוטם - מאמת אותם מול הרשימה המקורית שוב (ראו הבדיקה בסוף הפונקציה),
כהגנה מפני הזיה (hallucination) של מספר טלפון שלא ממש ברשימה.
"""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

import ai_json
import prompts

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

_HEBREW_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

VALID_LEAD_STATUSES = {
    "new", "contacted", "follow_up", "hot", "not_relevant", "reactivation", "human_handling", "no_answer",
}
_STATUS_HINTS = {
    "new": "חדש - עדיין לא טופל",
    "contacted": "נוצר קשר",
    "follow_up": "פולו-אפ נשלח, מחכים לתשובה",
    "hot": "חם - מעוניין ברור, כולל הצלחה/פגישה שנקבעה",
    "human_handling": "בטיפול אנושי ידני",
    "no_answer": "לא ענה לשיחה",
    "not_relevant": "לא רלוונטי",
    "reactivation": "ליד קר שהוחיה",
}
_VALID_ACTION_TYPES = {"status_update", "add_note", "schedule_task"}
_MAX_CANDIDATES = 300  # הגנה על גודל ה-prompt במאגר לידים גדול מאוד - ראו server.py
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

COMMAND_PARSE_PROMPT = """\
היום התאריך {today_date} ({today_weekday}).

להלן רשימת הלידים שהמשתמש המחובר מורשה לנהל כרגע (JSON, עד {max_candidates} לידים):
{candidates_json}

להלן הוראה חופשית שהמשתמש הקליד או הקליט בקולו, בעברית או באנגלית:
"{command}"

המשימה שלך: לזהות **ליד יחיד** מתוך הרשימה שמתאים לשם/לפרטים שהוזכרו בהוראה
(יכול להיות כתיב חלקי/שגוי/כינוי/אנגלית-מול-עברית - זה הייעוד שלך, לא התאמת
מחרוזת מדויקת), ולמפות את ההוראה לרשימת פעולות מובנות משלושה סוגים בלבד:

1. status_update - שינוי סטטוס הליד. "status" חייב להיות אחד מהערכים הבאים
   בדיוק (לא דבר אחר, לא תרגום, לא המצאה): {status_values}
   מדריך קצר לכל ערך: {status_hints}
   אם ההוראה מרמזת על הצלחה/פגישה שנקבעה בלי שום ערך אחר שמתאים טוב יותר -
   "hot" הוא ברירת המחדל הסבירה. אם אין בהוראה שום רמז לשינוי סטטוס - אל
   תחזיר פעולה מהסוג הזה בכלל.

2. add_note - הערה פנימית קצרה (לעולם לא נשלחת ללקוח). "text": אם ההוראה
   כוללת תוכן מפורש להערה - השתמש בו כפי שהוא (מתורגם לעברית אם נדרש). אם
   ההוראה רק אומרת "הוסף הערה"/"add a note" בלי תוכן מפורש - נסח הערה
   קצרה (משפט אחד) שמסכמת מה קרה, לפי שאר ההוראה.

3. schedule_task - משימת פולו-אפ/פגישה ביומן. שדות: "due_date" (YYYY-MM-DD -
   חשב לפי התאריך של היום אם כתוב ביטוי יחסי כמו "מחר"/"יום שלישי הבא"/
   "tomorrow"/"next Tuesday"; אם לא צוין תאריך בכלל אבל כן התבקש פולו-אפ/
   תזכורת/פגישה - קבע את התאריך של מחר כברירת מחדל), "due_time" (HH:MM
   בפורמט 24 שעות, או null אם לא צוינה שעה), ו-"title" (כמה מילים בעברית
   שמתארות את המשימה, למשל "פולו-אפ טלפוני" או "פגישה במשרד").

כללים נוספים:
- "label" בכל פעולה הוא משפט אחד בעברית, קריא לנציג, שמתאר מה הפעולה הזו
  תעשה בפועל (לתצוגת אישור לפני ביצוע) - למשל 'עדכון סטטוס ל"חם"'.
- אם ההוראה לא מזכירה אף ליד שמתאים לרשימה, או שמתאימה בבירור ליותר מליד
  אחד (למשל שם נפוץ שחוזר כמה פעמים ברשימה) - אל תמציא/תנחש ליד; דווח על
  זה בשדה lead_match (ראו הפורמט למטה) במקום.
- אם ההוראה לא מבקשת אף אחת משלוש הפעולות האלה (שאלה כללית, לא הוראת-ביצוע
  אמיתית) - החזר actions כרשימה ריקה, עם clarification שמסביר זאת.

החזר **אך ורק** JSON תקין בפורמט הזה, בלי טקסט נוסף לפני/אחרי, בלי הערות:
{{
  "lead_match": "found" | "ambiguous" | "not_found",
  "phone": "<בדיוק כפי שמופיע ברשימה, רק אם found>",
  "tenant_id": "<בדיוק כפי שמופיע ברשימה, רק אם found>",
  "matched_candidates": [<עד 5 אובייקטים {{"phone","tenant_id","customer_name"}} מהרשימה - רק אם ambiguous>],
  "actions": [<0 או יותר אובייקטי פעולה מהסוגים שלמעלה, כל אחד עם "type" ו-"label">],
  "clarification": "<הודעה קצרה בעברית, רק אם יש בעיה (not_found/ambiguous/אין פעולה מתאימה) - אחרת null>"
}}
"""


def _build_prompt(command_text: str, candidate_leads: list[dict]) -> str:
    now = datetime.now()
    prompt_template = prompts.get_prompt("command_copilot_parse", COMMAND_PARSE_PROMPT)
    candidates = candidate_leads[:_MAX_CANDIDATES]
    return prompt_template.format(
        today_date=now.strftime("%Y-%m-%d"),
        today_weekday=_HEBREW_WEEKDAYS[now.weekday()],
        max_candidates=_MAX_CANDIDATES,
        candidates_json=json.dumps(candidates, ensure_ascii=False),
        command=command_text,
        status_values=", ".join(sorted(VALID_LEAD_STATUSES)),
        status_hints="; ".join(f'{k}="{v}"' for k, v in _STATUS_HINTS.items()),
    )


def _default_tomorrow() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def sanitize_action(raw: dict) -> dict | None:
    """מאמתת פעולה בודדת - מחזירה None (ולא מעלה חריגה) על כל סטייה מהפורמט
    הצפוי. ציבורית בכוונה (לא _sanitize_action): server.py/POST /api/copilot/
    execute קוראת לה **שוב**, בנפרד מ-parse_command, על הפעולות שחוזרות
    מהלקוח ב-execute - אסור לבטוח בפעולות "שאושרו ב-preview" כפשוטן; לקוח
    יכול לשלוח ל-execute כל JSON, לא רק מה ש-parse_command החזיר במקור.
    שימוש באותה פונקציית אימות בשני המקומות מונע סטייה (drift) בין מה
    שמוצג ב-preview לבין מה שבאמת נאכף ב-execute."""
    if not isinstance(raw, dict):
        return None
    action_type = raw.get("type")
    label = str(raw.get("label") or "").strip()
    if action_type not in _VALID_ACTION_TYPES or not label:
        return None

    if action_type == "status_update":
        status = raw.get("status")
        if status not in VALID_LEAD_STATUSES:
            return None
        return {"type": "status_update", "status": status, "label": label}

    if action_type == "add_note":
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        return {"type": "add_note", "text": text, "label": label}

    # schedule_task
    due_date = str(raw.get("due_date") or "").strip()
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        due_date = _default_tomorrow()  # עדיף תאריך סביר-כברירת-מחדל מאשר לזרוק את כל הפעולה
    due_time = raw.get("due_time")
    due_time = due_time if (isinstance(due_time, str) and _TIME_RE.match(due_time)) else None
    title = str(raw.get("title") or "").strip() or "פולו-אפ"
    return {"type": "schedule_task", "due_date": due_date, "due_time": due_time, "title": title, "label": label}


def parse_command(command_text: str, candidate_leads: list[dict]) -> dict:
    """מנתחת הוראה חופשית אחת. candidate_leads: רשימת dict-ים עם phone/
    tenant_id/customer_name (ראו server._copilot_candidate_leads) - כבר
    מסוננת-הרשאות לפני שהיא מגיעה לכאן; הפונקציה הזו לא יודעת כלום על RBAC.

    מחזירה תמיד dict עם lead_match ("found"/"ambiguous"/"not_found"),
    actions (רשימה מסוננת-ומאומתת של פעולות תקינות בלבד), ו-clarification
    (הודעה לנציג, או None). קריסה/JSON לא-תקין מה-AI -> "not_found" עם
    הודעת clarification גנרית, לא חריגה שמפילה את הבקשה."""
    command_text = (command_text or "").strip()
    if not command_text:
        return {"lead_match": "not_found", "phone": None, "tenant_id": None,
                "matched_candidates": [], "actions": [], "clarification": "לא הוזנה הוראה"}

    prompt = _build_prompt(command_text, candidate_leads)
    # תכונה אינטראקטיבית (לא עיבוד-רקע עם catch-all משלו כמו ב-scheduling_agent) -
    # כל כשל (רשת/JSON לא-תקין/שגיאת ספק) חוזר כתשובה ידידותית למשתמש ב-UI,
    # לא כ-500 גולמי. נבדק במפורש ולא "except Exception" שקוף על כל הבלוק,
    # כדי שבאגים אמיתיים בקוד הסניטציה למטה (אחרי ה-try) לא יוסתרו בטעות.
    try:
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=800,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = next(block.text for block in response.content if block.type == "text").strip()
        parsed = ai_json.parse_json_response(raw_text)
    except Exception:
        return {"lead_match": "not_found", "phone": None, "tenant_id": None, "matched_candidates": [],
                "actions": [], "clarification": "לא הצלחתי לנתח את ההוראה - ניסו לנסח אחרת"}

    if not isinstance(parsed, dict):
        return {"lead_match": "not_found", "phone": None, "tenant_id": None, "matched_candidates": [],
                "actions": [], "clarification": "לא הצלחתי לנתח את ההוראה - ניסו לנסח אחרת"}

    lead_match = parsed.get("lead_match")
    if lead_match not in ("found", "ambiguous", "not_found"):
        lead_match = "not_found"

    phone = parsed.get("phone")
    tenant_id = parsed.get("tenant_id")
    if lead_match == "found":
        # ⚠️ לא בוטחים ב-AI - מאמתים שהצמד (phone, tenant_id) שהוא "בחר" אכן
        # קיים *מילה-במילה* ברשימת המועמדים המקורית, לא רק "נשמע סביר". זה
        # סוגר הזיית-מספר-טלפון (hallucination), לא רק טעות-ניסוח.
        is_real_candidate = any(
            c.get("phone") == phone and c.get("tenant_id") == tenant_id for c in candidate_leads
        )
        if not is_real_candidate:
            lead_match = "not_found"
            phone = None
            tenant_id = None

    actions = []
    if lead_match == "found":
        for raw_action in (parsed.get("actions") or []):
            sanitized = sanitize_action(raw_action)
            if sanitized:
                actions.append(sanitized)

    matched_candidates = []
    if lead_match == "ambiguous":
        for c in (parsed.get("matched_candidates") or [])[:5]:
            if isinstance(c, dict) and any(c.get("phone") == x.get("phone") and c.get("tenant_id") == x.get("tenant_id") for x in candidate_leads):
                matched_candidates.append({
                    "phone": c.get("phone"), "tenant_id": c.get("tenant_id"), "customer_name": c.get("customer_name"),
                })

    clarification = parsed.get("clarification")
    clarification = str(clarification).strip() if clarification else None
    if lead_match == "found" and not actions and not clarification:
        clarification = "לא זוהתה פעולה מתאימה בהוראה - ניסו לציין במפורש שינוי סטטוס, הערה, או תזמון פולו-אפ"

    return {
        "lead_match": lead_match,
        "phone": phone if lead_match == "found" else None,
        "tenant_id": tenant_id if lead_match == "found" else None,
        "matched_candidates": matched_candidates,
        "actions": actions,
        "clarification": clarification,
    }
