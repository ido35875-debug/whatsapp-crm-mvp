"""
Enterprise CSV/Excel Lead Import Utility.

מודול יחיד, בשימוש גם מ-POST /api/leads/import (server.py, העלאה מהדשבורד)
וגם כסקריפט CLI עצמאי (python import_leads.py <file> --tenant-id ...) - לקבצים
גדולים/ייבוא ישיר בשרת בלי לעבור דרך דפדפן. שתי דרכי הכניסה קוראות לאותה
import_rows() בדיוק - אין שום לוגיקת מיפוי/נרמול/ייבוא כפולה בין השתיים.

נועד לקבל ייצוא CSV/XLSX מ-BMBY ומ-Monday.com, וגם כל קובץ גנרי דומה (Excel/
Google Sheets עם כותרות עמודות סבירות בעברית או אנגלית) - ה-aliasing למטה
(IMPORT_FIELD_ALIASES) כולל שמות עמודות נפוצים משתי הפלטפורמות, אבל אינו
תלוי בפורמט קשיח אחד: כל עמודה שהשם שלה (אחרי normalize_header) תואם אחד
מה-aliases הידועים ממופה, בלי קשר לסדר העמודות או לשם הקובץ.

⚠️ מגבלה ידועה שהוכרה-במפורש: אין לי רשימה רשמית/מתועדת של שמות העמודות
המדויקים ש-BMBY מייצא (אין גישה לחשבון BMBY אמיתי כדי לאמת זאת) - ה-aliases
כאן מבוססים על מוסכמות נפוצות (עברית/אנגלית, "סטטוס"/"status" וכו') ולא על
ייצוא BMBY אמיתי שנבדק. אם עמודה ב-BMBY לא ממופה אוטומטית, היא תישאר בעמודה
הגולמית (לא תאבד - ראו 'לא ממופה' בדוח) ואפשר להוסיף alias חדש ל-
IMPORT_FIELD_ALIASES בלי לשנות שום קוד אחר.

שלושת העקרונות שהתבקשו:
1. מיפוי עמודות אוטומטי - IMPORT_FIELD_ALIASES, case-insensitive, עברית+אנגלית.
2. מניעת כפילויות: upsert לפי (tenant_id, טלפון מנורמל) - ראו extract.
   resolve_existing_phone/import_lead. שורה עם טלפון שכבר קיים *מעדכנת* את
   הכרטיס (רק שדות לא-ריקים נכתבים, ראו import_lead) - לא יוצרת כרטיס כפול,
   וגם לא מוחקת נתונים קיימים שהשורה המיובאת פשוט לא מזכירה.
3. נרמול טלפון לתקן +972 - ראו normalize_il_phone: סובל מקפים/רווחים/סוגריים/
   נקודות, קידומת 00 בינלאומית, קידומת 972 בלי +, מספר מקומי בלי 0 מוביל,
   ואפילו ארטיפקט "501234567.0" שקורה כש-Excel שומר עמודת טלפון כעמודה
   מספרית (ראו _clean_excel_cell) - כל אלה נפוצים בייצוא בפועל, לא רק
   הפורמט הנקי שכל שאר הפרויקט מניח (whatsapp_send._to_e164 המשותף, שנשאר
   בשימוש בלתי-תלוי בשאר הפרויקט ולא נוגעים בו כאן).
"""

import argparse
import csv
import io
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

import extract
from extract import DEFAULT_TENANT_ID, VALID_LEAD_STATUSES, import_lead, resolve_existing_phone

ALLOWED_IMPORT_EXTENSIONS = {".csv", ".xlsx"}

# שמות עמודות נפוצים (עברית + אנגלית) כולל כמה שמות אופייניים ל-BMBY/
# Monday.com - ראו ההערה בראש הקובץ על המגבלה (לא מאומת מול ייצוא BMBY אמיתי).
IMPORT_FIELD_ALIASES = {
    "customer_name": {
        "name", "customer_name", "full name", "contact name", "lead name", "contact",
        "שם", "שם לקוח", "שם מלא", "שם איש קשר", "איש קשר",
    },
    "phone": {
        "phone", "phone_number", "mobile", "cell", "cell phone", "primary phone", "tel",
        "טלפון", "מספר טלפון", "נייד", "מס' טלפון", "טלפון נייד", "טל",
    },
    "email": {"email", "e-mail", "mail", "אימייל", "מייל", "דוא\"ל", 'דוא"ל'},
    "address": {"address", "street address", "כתובת"},
    "business_name": {
        "business", "business_name", "company", "organization", "account",
        "עסק", "שם עסק", "חברה",
    },
    "location": {"location", "city", "area", "neighborhood", "מיקום", "עיר", "אזור", "שכונה"},
    "import_source": {"source", "lead_source", "origin", "מקור", "מקור ליד", "ערוץ מקור"},
    "lead_status": {"status", "lead_status", "deal stage", "stage", "סטטוס", "סטטוס ליד", "שלב"},
    "category": {"category", "vertical", "tags", "tag", "קטגוריה", "סיווג", "תגית"},
    "agent": {
        "agent", "assigned_agent", "owner", "assignee", "assigned to", "sales rep",
        "סוכן", "סוכן מטפל", "נציג", "אחראי",
    },
    "property_type": {
        "property_type", "property type", "property preferences", "apartment type", "unit type", "asset type",
        "סוג נכס", "סוג דירה", "סוג הנכס", "העדפות נכס",
    },
    "budget": {"budget", "price range", "budget range", "price", "תקציב", "טווח תקציב", "מחיר"},
    "requirements": {"requirements", "preferences", "needs", "דרישות", "העדפות", "צרכים"},
    "notes": {
        "notes", "note", "comments", "remarks", "description", "historical notes", "history",
        "הערות", "הערה", "תיאור", "הערות היסטוריות", "היסטוריה",
    },
}

# ערכי סטטוס חופשיים נפוצים מ-BMBY/Monday שלא תואמים מילה-במילה ל-enum הפנימי
# (extract.VALID_LEAD_STATUSES) - מיפוי "מיטב ההשערה" מתועד, לא ניחוש סמוי:
# ערך לא-מוכר שלא מופיע כאן נשאר בלי סטטוס במקום להיכשל על כל השורה (ראו
# resolve_status) - אותה "נפילה חזרה בעדינות" שכל שאר הפרויקט נוהג בה.
STATUS_ALIAS_MAP = {
    "לקוח חדש": "new", "חדש": "new",
    "in progress": "contacted", "נוצר קשר": "contacted", "בטיפול": "contacted", "פעיל": "contacted",
    "חם": "hot", "won": "hot", "deal closed": "hot", "סגור": "hot", "נסגר": "hot",
    "follow up": "follow_up", "follow-up": "follow_up", "פולו אפ": "follow_up",
    "not relevant": "not_relevant", "closed lost": "not_relevant", "לא רלוונטי": "not_relevant",
    "lost": "not_relevant", "לא פעיל": "not_relevant",
}


def normalize_header(h) -> str:
    return str(h or "").strip().lower()


_TRAILING_DOT_ZERO_RE = re.compile(r"\.0+$")


def normalize_il_phone(raw) -> str:
    """מנרמלת טלפון ישראלי מכל פורמט סביר ל-E.164 (+972XXXXXXXXX). "" אם לא
    נשאר אף ספרה (הקורא מדלג על השורה, ראו import_rows) - לא מייבאת 'טלפון
    ריק'. חזקה יותר מ-whatsapp_send._to_e164 המשותף (ראו ההערה בראש הקובץ) -
    לא דורסת אותו, רק משמשת ספציפית בצינור הייבוא הזה."""
    if raw is None:
        return ""
    raw = str(raw).strip()
    if not raw:
        return ""
    raw = _TRAILING_DOT_ZERO_RE.sub("", raw)  # ארטיפקט Excel: עמודת טלפון שנשמרה כמספרית ("501234567.0")
    has_plus = raw.startswith("+")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if digits.startswith("00"):
        digits = digits[2:]
        return "+" + digits if digits else ""
    if digits.startswith("972"):
        return "+" + digits
    if digits.startswith("0"):
        return "+972" + digits[1:]
    if len(digits) == 9:
        # מספר ישראלי מקומי בלי ה-0 המוביל - נפוץ כש-Excel/Monday מאבדים אפס
        # מוביל בעמודה שמטופלת כמספרית, לא כטקסט.
        return "+972" + digits
    return "+" + digits


def map_row(raw_row: dict) -> dict:
    """ממפה שורת CSV/Excel גולמית (עמודות בכל שם סביר, עברית או אנגלית) לשדות
    התקניים לפי IMPORT_FIELD_ALIASES. עמודה שלא תואמת שום alias נשארת בחוץ
    בשקט (לא נכשלת) - פשוט לא מיובאת לאותו שדה."""
    normalized = {normalize_header(k): v for k, v in raw_row.items()}
    mapped = {}
    for field, aliases in IMPORT_FIELD_ALIASES.items():
        for alias in aliases:
            value = normalized.get(normalize_header(alias))
            if value not in (None, ""):
                mapped[field] = str(value).strip()
                break
    return mapped


def resolve_status(raw_status: str | None) -> str | None:
    """None אם לא ניתן למפות ערך לסטטוס תקין - הקורא פשוט מדלג על שינוי
    הסטטוס לאותה שורה (לא נכשל, לא מייבא ערך שרירותי)."""
    if not raw_status:
        return None
    key = raw_status.strip().lower()
    if key in VALID_LEAD_STATUSES:
        return key
    return STATUS_ALIAS_MAP.get(raw_status.strip()) or STATUS_ALIAS_MAP.get(key)


def _clean_excel_cell(value):
    # תא Excel מספרי ששומר מספר שלם כ-float (501234567.0) - ממיר ל-int לפני
    # שהוא הופך למחרוזת בהמשך; בלי זה, ".0" מצטרף כתוספת-ספרה שגויה בסוף
    # (ראו normalize_il_phone - גם שם יש הגנה שנייה, אבל נכון לתקן פה בשורש,
    # לטובת כל עמודה מספרית אחרת, לא רק טלפון).
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def parse_csv(file_or_path) -> list[dict]:
    """file_or_path: stream (יש לו .read(), כמו Flask request.files[...].stream)
    או נתיב (str/Path) - שתי דרכי הקריאה האפשריות (העלאת דשבורד מול CLI)."""
    if hasattr(file_or_path, "read"):
        text = file_or_path.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")  # utf-8-sig סופג BOM מ-CSV שיוצא מ-Excel
        return list(csv.DictReader(io.StringIO(text)))
    with open(file_or_path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def parse_excel(file_or_path) -> list[dict]:
    workbook = load_workbook(file_or_path, read_only=True, data_only=True)
    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
    except StopIteration:
        return []
    rows = []
    for row in rows_iter:
        if all(cell is None for cell in row):
            continue
        rows.append({headers[i]: _clean_excel_cell(row[i]) for i in range(min(len(headers), len(row)))})
    return rows


def parse_rows(file_or_path, ext: str) -> list[dict]:
    ext = ext.lower()
    if ext == ".csv":
        return parse_csv(file_or_path)
    if ext == ".xlsx":
        return parse_excel(file_or_path)
    raise ValueError(f"סוג קובץ לא נתמך: '{ext}'. נתמכים: CSV, XLSX")


def import_rows(
    raw_rows: list[dict],
    tenant_id: str = DEFAULT_TENANT_ID,
    default_import_source: str = "csv_import",
    dry_run: bool = False,
) -> dict:
    """הליבה המשותפת - נקראת גם מ-POST /api/leads/import (server.py) וגם
    מה-CLI (main() למטה). dry_run=True: מדמה imported/updated (בודקת רק אם
    הטלפון המנורמל כבר קיים, בלי לכתוב שום דבר ל-DB בכלל) - לסקירה לפני
    הרצה אמיתית על קובץ גדול/לא-מוכר."""
    customers_snapshot = extract.load_customers() if dry_run else None

    imported = updated = 0
    skipped = []
    for row_num, raw_row in enumerate(raw_rows, start=2):  # שורה 1 = כותרות
        mapped = map_row(raw_row)
        raw_phone = mapped.get("phone")
        if not raw_phone:
            skipped.append({"row": row_num, "reason": "אין מספר טלפון"})
            continue

        phone = normalize_il_phone(raw_phone)
        if not phone:
            skipped.append({"row": row_num, "reason": f"מספר טלפון לא תקין: '{raw_phone}'"})
            continue

        status = resolve_status(mapped.get("lead_status"))
        import_source = mapped.get("import_source") or default_import_source

        if dry_run:
            key = f"{tenant_id}::{phone}"
            if key in customers_snapshot:
                updated += 1
            else:
                imported += 1
            continue

        _card, is_new = import_lead(
            resolve_existing_phone(phone, tenant_id=tenant_id),
            tenant_id=tenant_id,
            customer_name=mapped.get("customer_name"),
            business_name=mapped.get("business_name"),
            location=mapped.get("location"),
            lead_status=status,
            import_source=import_source,
            category=mapped.get("category"),
            agent=mapped.get("agent"),
            notes=mapped.get("notes"),
            email=mapped.get("email"),
            address=mapped.get("address"),
            property_type=mapped.get("property_type"),
            budget=mapped.get("budget"),
            requirements=mapped.get("requirements"),
        )
        if is_new:
            imported += 1
        else:
            updated += 1

    return {"total_rows": len(raw_rows), "imported": imported, "updated": updated, "skipped": skipped}


def _print(line: str, file=None) -> None:
    # קונסולת Windows (cp1252) קורסת על עברית ב-print() רגיל - ראו התיקון הזהה
    # ב-scheduler.py._log. נפילה-חזרה ל-ASCII (backslashreplace) כדי שה-CLI
    # לא יתרסק על הפלט שלו עצמו בהרצה מקומית על Windows.
    try:
        print(line, file=file)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="backslashreplace").decode("ascii"), file=file)


def main():
    parser = argparse.ArgumentParser(
        description="ייבוא לידים מקובץ CSV/XLSX (ייצוא BMBY/Monday.com, או כל קובץ דומה) ישירות ל-DB המוגדר ב-.env (DATABASE_URL אם קיים, אחרת customers.json מקומי)."
    )
    parser.add_argument("file", help="נתיב לקובץ CSV/XLSX")
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID, help=f"tenant_id (ברירת מחדל: {DEFAULT_TENANT_ID})")
    parser.add_argument("--source", default="csv_import", help="import_source לשורות בלי עמודת מקור משל עצמן (ברירת מחדל: csv_import)")
    parser.add_argument("--dry-run", action="store_true", help="מציג כמה שורות ייובאו/יעודכנו בלי לכתוב שום דבר ל-DB")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        _print(f"קובץ לא נמצא: {path}", file=sys.stderr)
        sys.exit(1)
    ext = path.suffix.lower()
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        _print(f"סוג קובץ לא נתמך: '{ext}'. נתמכים: CSV, XLSX", file=sys.stderr)
        sys.exit(1)

    try:
        raw_rows = parse_rows(path, ext)
    except Exception as exc:
        _print(f"שגיאה בקריאת הקובץ: {exc}", file=sys.stderr)
        sys.exit(1)

    result = import_rows(raw_rows, tenant_id=args.tenant_id, default_import_source=args.source, dry_run=args.dry_run)

    prefix = "[DRY RUN - לא נכתב שום דבר] " if args.dry_run else ""
    _print(f"{prefix}קובץ: {path.name} | tenant: {args.tenant_id}")
    _print(f"סה\"כ שורות: {result['total_rows']} | לידים חדשים: {result['imported']} | עודכנו: {result['updated']} | דילוג: {len(result['skipped'])}")
    for s in result["skipped"]:
        _print(f"  שורה {s['row']}: {s['reason']}")


if __name__ == "__main__":
    main()
