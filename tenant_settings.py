"""
הגדרות פר-Tenant (עסק) - כרגע רק מרווח הזמן הנדרש להחייאת לידים קרים
(reactivation_days), אבל בנוי כמפתח-ערך גנרי פר-tenant כדי לאפשר הגדרות
נוספות בעתיד בלי לשנות מבנה. אותו דפוס בדיוק כמו prompts.py/prompts.json:
JSON הוא מקור האמת בזמן ריצה, קבוע בקוד (DEFAULT_REACTIVATION_DAYS) הוא
רק רשת ביטחון לתenant שעדיין לא הוגדר לו כלום - "כל עסק דורש חוקיות עסקית
שונה (קמעונאות מול נדל"ן)" כפי שהתבקש.
"""

import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent / "tenant_settings.json"
DEFAULT_REACTIVATION_DAYS = 30  # "לא נוצר קשר מעל X ימים" - ברירת המחדל לכל tenant שלא הגדיר בעצמו


def _load() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    with SETTINGS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_reactivation_days(tenant_id: str) -> int:
    """מרווח הזמן (בימים) שאחריו ליד נחשב "קר מספיק" להחייאה עבור ה-tenant הזה -
    ברירת מחדל DEFAULT_REACTIVATION_DAYS אם ה-tenant לא הגדיר ערך משלו."""
    days = _load().get(tenant_id, {}).get("reactivation_days")
    return days if isinstance(days, int) and days >= 0 else DEFAULT_REACTIVATION_DAYS


def set_reactivation_days(tenant_id: str, days: int) -> None:
    if not isinstance(days, int) or days < 0:
        raise ValueError("מספר ימים חייב להיות מספר שלם לא-שלילי")
    data = _load()
    data.setdefault(tenant_id, {})["reactivation_days"] = days
    _save(data)
