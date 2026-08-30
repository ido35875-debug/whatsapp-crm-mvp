"""
מערכת ניהול תבניות/פרומפטים פשוטה - מאפשרת לשלוף ולעדכן את הפרומפטים של סוכני
ה-AI (Speed-to-Lead, החייאה מותאם-ענף, תיאום פולו-אפ) ישירות מה-CRM, בלי לערוך
קוד Python ולפרוס מחדש.

prompts.json הוא מקור האמת בזמן ריצה. הקבועים הקשיחים ב-extract.py/reactivate.py
(EXTRACTION_PROMPT, REPLY_PROMPT וכו') משמשים רק כברירת מחדל (fallback) שמועברת
לכל קריאה ל-get_prompt - אם הקובץ חסר, פגום, או שהמפתח הספציפי לא קיים בו, המערכת
ממשיכה לעבוד עם הנוסח הקבוע בקוד במקום להתרסק. זה אותו עיקרון בדיוק כמו כל שכבת
"תצורה חסרה -> נפילה חזרה בעדינות" אחרת בפרויקט (Voice/STT/Trial).
"""

import json
from pathlib import Path

PROMPTS_FILE = Path(__file__).parent / "prompts.json"


def load_prompts() -> dict:
    """כל הפרומפטים הגולמיים מ-prompts.json. {} אם הקובץ חסר או לא תקין - לא
    מעלה חריגה, כדי ש-get_prompt תמיד תוכל ליפול חזרה ל-default של הקורא."""
    if not PROMPTS_FILE.exists():
        return {}
    try:
        return json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_prompts(prompts: dict) -> None:
    PROMPTS_FILE.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")


def get_prompt(key: str, default: str, vertical: str | None = None) -> str:
    """מחזיר את הפרומפט הפעיל עבור key - מ-prompts.json אם קיים, אחרת default
    (הקבוע הקשיח ב-extract.py/reactivate.py, כרשת ביטחון). אם vertical ניתן
    ויש override ספציפי לוורטיקל הזה תחת vertical_overrides - הוא מנצח את
    התבנית הבסיסית (משמש ב-reactivation_outreach - "מותאם ענף")."""
    entry = load_prompts().get(key)
    if not entry:
        return default
    if vertical:
        override = (entry.get("vertical_overrides") or {}).get(vertical)
        if override:
            return override
    return entry.get("template") or default


def get_all_prompts() -> dict:
    """כל הפרומפטים לתצוגה/עריכה מה-CRM (GET /api/prompts)."""
    return load_prompts()


def update_prompt(key: str, template: str, vertical: str | None = None) -> dict:
    """מעדכן פרומפט - את התבנית הבסיסית, או override ספציפי לוורטיקל אם
    vertical ניתן (template ריק מוחק את ה-override של הוורטיקל הזה, כלומר
    "חזרה לברירת המחדל"). שומר ל-prompts.json מיד. מעלה KeyError אם ה-key
    לא קיים בכלל - לא יוצר תבניות חדשות דרך העדכון, רק עורך קיימות."""
    prompts = load_prompts()
    if key not in prompts:
        raise KeyError(key)
    if vertical:
        prompts[key].setdefault("vertical_overrides", {})
        if template:
            prompts[key]["vertical_overrides"][vertical] = template
        else:
            prompts[key]["vertical_overrides"].pop(vertical, None)
    else:
        prompts[key]["template"] = template
    save_prompts(prompts)
    return prompts[key]
