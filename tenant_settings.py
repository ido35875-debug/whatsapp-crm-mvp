"""
הגדרות פר-Tenant (עסק) - כרגע רק מרווח הזמן הנדרש להחייאת לידים קרים
(reactivation_days), אבל בנוי כמפתח-ערך גנרי פר-tenant כדי לאפשר הגדרות
נוספות בעתיד בלי לשנות מבנה. אותו דפוס בדיוק כמו prompts.py/prompts.json:
JSON הוא מקור האמת בזמן ריצה, קבוע בקוד (DEFAULT_REACTIVATION_DAYS) הוא
רק רשת ביטחון לתenant שעדיין לא הוגדר לו כלום - "כל עסק דורש חוקיות עסקית
שונה (קמעונאות מול נדל"ן)" כפי שהתבקש.
"""

import json

from paths import DATA_DIR

# state שמשתנה בזמן ריצה (נערך חי מפאנל ההחייאה - "💾 שמור כברירת מחדל") - עובר
# דרך DATA_DIR (ראו paths.py) בדיוק כמו customers.json/crm_data.db, כדי לשרוד
# הפעלות מחדש/דיפלויים חדשים ב-production על filesystem ephemeral (Docker/
# Render) - ברירת המחדל בלי DATA_DIR מוגדר היא תיקיית הפרויקט, זהה להתנהגות
# הקודמת בהרצה מקומית.
SETTINGS_FILE = DATA_DIR / "tenant_settings.json"
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


def get_green_api_credentials(tenant_id: str) -> tuple[str | None, str | None]:
    """ה-Green API הייעודי של ה-tenant הזה (instance_id, token) - אם לא הוגדר
    לו אחד משלו, מוחזר (None, None) והקורא (whatsapp_provider.get_tenant_
    provider) נופל חזרה ל-Provider הגלובלי המשותף (WHATSAPP_PROVIDER ב-.env),
    בדיוק כמו get_reactivation_days נופל ל-DEFAULT_REACTIVATION_DAYS.
    ⚠️ אלו אישורי שליחה אמיתיים (לא רק הגדרה עסקית כמו reactivation_days) -
    נשמרים כאן באותה רמת-אמון בדיוק כמו שאר קובץ ההגדרות הזה (JSON רגיל
    ב-DATA_DIR, לא כספת-סודות ייעודית) - החלטה מודעת, לא פיקוח."""
    tenant_data = _load().get(tenant_id, {})
    instance_id = tenant_data.get("green_api_instance_id")
    token = tenant_data.get("green_api_token")
    return (instance_id or None, token or None)


def set_green_api_credentials(tenant_id: str, instance_id: str, token: str) -> None:
    if not instance_id or not token:
        raise ValueError("instance_id/token חייבים להיות ערכים לא-ריקים")
    data = _load()
    data.setdefault(tenant_id, {})["green_api_instance_id"] = instance_id.strip()
    data[tenant_id]["green_api_token"] = token.strip()
    _save(data)


def get_followup_4th_message(tenant_id: str) -> str | None:
    """תבנית הודעה מותאמת-אישית שה-tenant הגדיר לעצמו לשליחה-אוטומטית
    הרביעית/האחרונה בסדרת הפולו-אפ (ראו scheduler.check_pending_followups) -
    None אם ה-tenant לא הגדיר תבנית משלו, ואז הקורא נופל חזרה ל-הודעה
    שנוצרת ע"י AI (extract.generate_followup_message(final=True)), בדיוק
    כמו ההתנהגות המקורית לפני שהתמיכה הזו נוספה."""
    message = _load().get(tenant_id, {}).get("followup_4th_message")
    return message or None


def set_followup_4th_message(tenant_id: str, message: str) -> None:
    if not message or not message.strip():
        raise ValueError("טקסט ההודעה לא יכול להיות ריק")
    data = _load()
    data.setdefault(tenant_id, {})["followup_4th_message"] = message.strip()
    _save(data)
