"""
מיגרציה מ-BMBY (CRM נדל"ן קודם) למערכת - אנשי קשר/לידים, סטטוסים, משימות
פתוחות והערות/פעילויות היסטוריות. בטוח, אידמפוטנטי, בלי כפילויות.

שימוש (ברירת המחדל היא DRY-RUN - קורא, מאמת, מדווח, **לא משנה כלום**):

    venv/Scripts/python.exe migrate_bmby.py --target local --input bmby_export/ --config bmby_mapping.json
    venv/Scripts/python.exe migrate_bmby.py --target local --input bmby_export/ --config bmby_mapping.json --apply
    venv/Scripts/python.exe migrate_bmby.py --target database --input bmby_export/ --config bmby_mapping.json \
        [--apply --allow-remote-db] [--tenant default] [--report report.json] [--include-done-tasks]

--target (חובה) קובע את היעד במפורש, **לפני** שמודול כלשהו של הפרויקט נטען:
- local: DATABASE_URL מאולץ לריק (גם אם מוגדר ב-.env/בסביבה) -> הכרטיסים ב-
  DATA_DIR/customers.json, הערות ומשימות ב-DATA_DIR/crm_data.db.
- database: נטען .env (dotenv, בלי לדרוס משתנה סביבה קיים) ונדרש DATABASE_URL
  של Postgres. --apply מול host שאינו localhost/127.0.0.1 דורש --allow-remote-db.
  ⚠️ עם ה-.env של הפרויקט זה Postgres של הפרודקשן (Render) - רק המשתמש מריץ.
  dry-run במצב database רק קורא (SELECT) - שלב ה-DDL (db._init_schema) מדולג בו.
היעד (host מוסתר חלקית, DATA_DIR) מודפס בתחילת כל הרצה.

עקרונות (ראו CLAUDE.md, חוקים 4/5/6):
- כרטיסים נוצרים/מתעדכנים **רק** דרך extract (extract.import_leads_batch - אותה
  סמנטיקת upsert כמו import_lead + מטא-דאטה של הייבוא, בכתיבה אחת לכרטיס).
  טלפון מנורמל (normalize_phone, מעל import_leads.normalize_il_phone) ומאותר מול
  כרטיס קיים דרך אינדקס טלפונים בזיכרון (PhoneIndex) שנבנה מ-load_customers()
  אחד בתחילת ההרצה - אותם מועמדים כמו extract.resolve_existing_phone (גולמי,
  E.164, 05x מקומי) - מעדכן, לא משכפל.
- קשרים לפי מזהי BMBY בלבד (contact_id/lead_id), לעולם לא לפי שם.
- פעילויות/הערות -> טבלת messages כהערה פנימית (channel='note', direction='out')
  עם חותמת הזמן המקורית (db.insert_imported_notes) - בדיוק כמו הערה ידנית מ-
  POST /api/messages/note: מוצגות בציר הזמן בדשבורד (GET /api/messages), לא
  נשלחות ללקוח, ו**לא** נכתבות ל-card["history"] (שמוזן כהקשר למענה ה-AI
  האוטומטי - extract._format_history_for_reply). אידמפוטנטיות: הערה שכבר קיימת
  לאותו טלפון באותה חותמת זמן עם אותו תוכן - מדולגת.
- משימות -> db.create_imported_task עם external_ref יציב (אינדקס ייחודי חלקי על
  (tenant_id, external_ref)). ברירת מחדל: רק משימות פתוחות. פגישות - ראו plan_tasks.
- status_changed_at **לא** נקבע לליד מיובא (ראו make_card_mutator).
- לפני --apply: גיבוי כל הכרטיסים ל-DATA_DIR/customers.backup_bmby_<UTC>.json.
"""

import argparse
import copy
import csv
import hashlib
import io
import json
import os
import re
import sys
import time as _time
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# ⚠️ בכוונה אין כאן import של מודולי הפרויקט (db/extract/import_leads/paths):
# `import db` עם DATABASE_URL בסביבה פותח מיד pool חיבורים ל-Postgres, ו-extract
# טוען את ה-.env (שבו DATABASE_URL של הפרודקשן). הם נטענים רק ב-bootstrap(),
# אחרי שהיעד (--target) הוכרע והסביבה הוכנה בהתאם.
db = None
extract = None
VALID_LEAD_STATUSES: set = set()
_clean_excel_cell = normalize_il_phone = parse_csv = parse_excel = None
DATA_DIR: Path | None = None

PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1"}

ENTITIES = ("contacts", "leads", "tasks", "activities")
VALID_TASK_STATUSES = {"pending", "done", "cancelled"}
VALID_TASK_TYPES = {"followup", "meeting"}
TASK_REF_PREFIX = "bmby:task:"
# שדות כרטיס שעוברים ל-import (notes/lead_status מטופלים בנפרד - ראו make_card_mutator)
CARD_FIELDS = (
    "customer_name", "business_name", "location", "category", "agent", "email", "address",
    "property_type", "budget", "requirements", "rooms", "project",
)
# שדות שהליד העדכני ביותר גובר בהם על איש הקשר (ליד = פנייה ספציפית לפרויקט)
LEAD_PRIORITY_FIELDS = ("project", "agent", "budget", "property_type", "rooms", "requirements", "location", "category")
NOTES_BLOCK_START = "--- ייבוא מבמבי (לא לערוך בתוך הבלוק - נדרס בייבוא חוזר) ---"
NOTES_BLOCK_END = "--- סוף ייבוא מבמבי ---"
# תופס גם את הכותרת הישנה ("--- ייבוא מבמבי ---") וגם את החדשה
_NOTES_BLOCK_RE = re.compile(r"--- ייבוא מבמבי[^\n]*?---.*?" + re.escape(NOTES_BLOCK_END), re.S)
_PHONE_SPLIT_RE = re.compile(r"[,;/|\n]+")
_PHONEISH_RE = re.compile(r"\+?\d[\d\- ]{6,}\d")
IL_MOBILE_PREFIX = "+9725"
IL_LANDLINE_AREA_DIGITS = "23489"  # 02/03/04/08/09


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- target / bootstrap

def mask_host(host: str | None) -> str:
    host = host or "?"
    if host in LOCAL_DB_HOSTS:
        return host
    parts = host.split(".")
    tail = ".".join(parts[-2:]) if len(parts) >= 3 else ""
    return (host[:4] + "***" + ("." + tail if tail else "")) if len(host) > 4 else "***"


def _load_dotenv() -> None:
    from dotenv import load_dotenv
    # override=False: משתנה שכבר מוגדר בסביבה (כולל DATABASE_URL="" שאולץ ב-local) לא נדרס
    load_dotenv(dotenv_path=PROJECT_DIR / ".env", override=False)


def resolve_target(target: str, apply: bool, allow_remote_db: bool) -> dict:
    """מכריע את היעד ומכין את os.environ - **לפני** כל import של מודול פרויקט."""
    if target == "local":
        os.environ["DATABASE_URL"] = ""  # לפני load_dotenv - כך ש-.env לא יכניס את הפרודקשן
        _load_dotenv()
        return {"kind": "local", "host": None, "remote": False}
    if target != "database":
        raise ConfigError(f"--target לא מוכר: {target}")
    _load_dotenv()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        raise ConfigError("--target database דורש DATABASE_URL של Postgres (בסביבה או ב-.env) - לא נמצא")
    host = urlparse(url).hostname or "?"
    remote = host not in LOCAL_DB_HOSTS
    if apply and remote and not allow_remote_db:
        raise ConfigError(f"--apply מול Postgres מרוחק ({mask_host(host)}) דורש --allow-remote-db מפורש")
    return {"kind": "database", "host": host, "remote": remote}


def bootstrap(target: dict) -> None:
    """טוען את מודולי הפרויקט אחרי שהסביבה הוכנה, ומוודא שהם באמת על היעד שנבחר."""
    global db, extract, VALID_LEAD_STATUSES, _clean_excel_cell, normalize_il_phone, parse_csv, parse_excel, DATA_DIR
    wanted_url = os.environ.get("DATABASE_URL", "").strip()
    if "db" in sys.modules and sys.modules["db"].DATABASE_URL != wanted_url:
        # רק בהרצה בתוך תהליך שכבר טען את db (בדיקות) - לא נעבור בשקט ליעד אחר
        raise ConfigError("מודול db כבר נטען בתהליך הזה עם DATABASE_URL אחר מהיעד שנבחר - להריץ בתהליך נפרד")
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))
    import db as _db
    import extract as _extract
    import import_leads as _il
    from paths import DATA_DIR as _data_dir

    if target["kind"] == "local" and (_db.DATABASE_URL or _extract._use_database()):
        raise ConfigError("--target local אבל DATABASE_URL לא ריק אחרי הטעינה - עוצר")
    if target["kind"] == "database" and not _db.IS_POSTGRES:
        raise ConfigError("--target database אבל db לא במצב Postgres - עוצר")
    db, extract = _db, _extract
    VALID_LEAD_STATUSES = _extract.VALID_LEAD_STATUSES
    _clean_excel_cell, normalize_il_phone = _il._clean_excel_cell, _il.normalize_il_phone
    parse_csv, parse_excel = _il.parse_csv, _il.parse_excel
    DATA_DIR = _data_dir


def describe_target(target: dict) -> str:
    if target["kind"] == "database":
        where = "מרוחק" if target["remote"] else "מקומי"
        return f"Postgres ({where}) @ {mask_host(target['host'])} - טבלאות customers/messages/calendar_tasks"
    return f"JSON מקומי: {extract.CUSTOMERS_FILE} + SQLite: {db.DB_FILE}"


# ---------------------------------------------------------------- utils

def norm_key(value) -> str:
    """נרמול כותרת עמודה/ערך סטטוס להשוואה: BOM, רווחים, אותיות קטנות, גרשיים עבריים."""
    s = str(value if value is not None else "").replace("﻿", "").replace("‏", "").replace("‎", "")
    s = s.replace("״", '"').replace("׳", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def to_text(value) -> str:
    if value is None:
        return ""
    value = _clean_excel_cell(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def mask_phone(phone) -> str:
    """לכל היותר 3 ספרות ראשונות + 2 אחרונות; מספר קצר (<9 ספרות) מוסתר לגמרי."""
    p = str(phone or "")
    digits = re.sub(r"\D", "", p)
    if len(digits) < 9:
        return "***"
    return ("+" if p.strip().startswith("+") else "") + digits[:3] + "*" * (len(digits) - 5) + digits[-2:]


def mask_key(tenant: str, phone: str) -> str:
    return f"{tenant}::{mask_phone(phone)}"


def sanitize(text) -> str:
    """מסתיר כל רצף דמוי-טלפון (גם בתוך מפתח tenant::phone) - לשגיאות/אזהרות בדוח."""
    return _PHONEISH_RE.sub(lambda m: mask_phone(m.group(0)), str(text))


def stable_hash(*parts) -> str:
    return hashlib.sha1(json.dumps([to_text(p) for p in parts], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def normalize_phone(raw) -> str:
    """import_leads.normalize_il_phone + שני תיקונים שכיחים בייצוא BMBY:
    - "+972 050-1234567" / "972-050..." / "00972 050..." (0 מיותר אחרי 972) -> +97250...
    - קווי ישראלי בן 8 ספרות שאיבד את ה-0 (Excel מספרי), רק כשחד-משמעי: בלי "+"/"00",
      ספרה ראשונה קידומת אזור (2/3/4/8/9) -> +972XXXXXXXX."""
    s = re.sub(r"\.0+$", "", to_text(raw))
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if not s.startswith("+") and len(digits) == 8 and digits[0] in IL_LANDLINE_AREA_DIGITS:
        return "+972" + digits
    e164 = normalize_il_phone(s)
    if e164.startswith("+9720"):
        e164 = "+972" + e164[5:]
    return e164


def is_valid_phone(e164: str) -> bool:
    if not re.fullmatch(r"\+\d{8,15}", e164 or ""):
        return False
    if e164.startswith("+972"):
        rest = e164[4:]
        return (len(rest) == 9 and rest[0] in "57") or (len(rest) == 8 and rest[0] in IL_LANDLINE_AREA_DIGITS)
    return len(e164) >= 11


def local_il_format(e164: str) -> str | None:
    return "0" + e164[4:] if e164.startswith("+972") else None


# ---------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    cfg = _strip_comments(json.loads(Path(path).read_text(encoding="utf-8")))
    cfg.setdefault("files", {})
    cfg.setdefault("columns", {})
    cfg["lead_status_map"] = {norm_key(k): v for k, v in (cfg.get("lead_status_map") or {}).items()}
    cfg["task_status_map"] = {norm_key(k): v for k, v in (cfg.get("task_status_map") or {}).items()}
    cfg["task_type_map"] = {norm_key(k): v for k, v in (cfg.get("task_type_map") or {}).items()}
    for raw, target in cfg["lead_status_map"].items():
        if target is not None and target not in VALID_LEAD_STATUSES:
            raise ConfigError(f"lead_status_map['{raw}'] = '{target}' - לא ב-VALID_LEAD_STATUSES {sorted(VALID_LEAD_STATUSES)}")
    for raw, target in cfg["task_status_map"].items():
        if target not in VALID_TASK_STATUSES:
            raise ConfigError(f"task_status_map['{raw}'] = '{target}' - מותר: {sorted(VALID_TASK_STATUSES)}")
    for raw, target in cfg["task_type_map"].items():
        if target not in VALID_TASK_TYPES:
            raise ConfigError(f"task_type_map['{raw}'] = '{target}' - מותר: {sorted(VALID_TASK_TYPES)}")
    for key in ("task_status_empty", "task_status_default"):
        val = cfg.get(key)
        if val is not None and val not in VALID_TASK_STATUSES:
            raise ConfigError(f"{key} = '{val}' - מותר: {sorted(VALID_TASK_STATUSES)} או null")
    cfg.setdefault("task_status_empty", "pending")
    cfg.setdefault("task_status_default", None)
    if cfg.setdefault("task_type_default", "followup") not in VALID_TASK_TYPES:
        raise ConfigError("task_type_default חייב להיות followup/meeting")
    if cfg.setdefault("task_missing_due_date", "today") not in ("today", "skip"):
        raise ConfigError("task_missing_due_date חייב להיות today/skip")
    if cfg.setdefault("status_policy", "fill_missing") not in ("fill_missing", "overwrite"):
        raise ConfigError("status_policy חייב להיות fill_missing/overwrite")
    if cfg.setdefault("field_policy", "fill_missing") not in ("fill_missing", "overwrite"):
        raise ConfigError("field_policy חייב להיות fill_missing/overwrite")
    for key, default in (("future_meeting_reminder", False), ("future_meeting_marketer_task", True)):
        if not isinstance(cfg.setdefault(key, default), bool):
            raise ConfigError(f"{key} חייב להיות true/false")
    cfg.setdefault("date_formats", ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"])
    day_first = any(re.search(r"%d[/.\-]%m", f) for f in cfg["date_formats"])
    # %Y-%m-%d (ISO, שנה קודם) אינו דו-משמעי - רק %m/%d שלא אחרי %Y
    month_first = any(re.search(r"(?<!%Y[/.\-])%m[/.\-]%d", f) for f in cfg["date_formats"])
    if day_first and month_first:
        raise ConfigError("date_formats מערבב יום-ראשון (%d/%m) וחודש-ראשון (%m/%d) - דו-משמעי (01/02 = ינואר או פברואר?). לבחור אחד.")
    cfg.setdefault("timezone", "Asia/Jerusalem")
    cfg.setdefault("csv_encodings", ["utf-8-sig", "cp1255"])
    cfg.setdefault("history_message_prefix", "[במבי] ")
    cfg.setdefault("default_category", None)
    try:
        cfg["_tz"] = ZoneInfo(cfg["timezone"])
    except Exception as exc:
        raise ConfigError(f"timezone לא תקין: {cfg['timezone']} ({exc})") from exc
    return cfg


# ---------------------------------------------------------------- dates

def parse_local_dt(value, cfg) -> tuple[datetime, bool] | None:
    """(datetime מודע-timezone באזור cfg.timezone, has_time) או None אם ריק/לא ניתן לפענוח."""
    tz = cfg["_tz"]
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=tz)
        return dt.astimezone(tz), value.time() != time(0, 0)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=tz), False
    s = str(value).strip()
    if not s:
        return None
    try:
        iso = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = iso if iso.tzinfo else iso.replace(tzinfo=tz)
        return dt.astimezone(tz), len(s) > 10
    except ValueError:
        pass
    for fmt in cfg["date_formats"]:
        try:
            parsed = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=tz), "%H" in fmt
    return None


def parse_time_of_day(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})", str(value))
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def to_utc_iso(local: datetime) -> str:
    return local.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- files

def read_table(path: Path, cfg) -> list[dict]:
    ext = path.suffix.lower()
    if ext == ".xlsx":
        return parse_excel(path)
    if ext == ".csv":
        last_exc = None
        for enc in cfg["csv_encodings"]:
            if enc.lower().replace("_", "-") in ("utf-8-sig", "utf8-sig"):
                try:
                    return parse_csv(path)  # הקורא המשותף של import_leads (utf-8-sig)
                except UnicodeDecodeError as exc:
                    last_exc = exc
                    continue
            try:
                text = path.read_bytes().decode(enc)
            except (UnicodeDecodeError, LookupError) as exc:
                last_exc = exc
                continue
            return list(csv.DictReader(io.StringIO(text)))
        raise ValueError(f"לא הצלחתי לפענח את קידוד הקובץ {path.name} ({cfg['csv_encodings']}): {last_exc}")
    raise ValueError(f"סוג קובץ לא נתמך: {path.name} (נתמכים CSV/XLSX; קובץ .xls ישן - לשמור מחדש כ-.xlsx)")


def find_file(input_dir: Path, names) -> Path | None:
    if isinstance(names, str):
        names = [names]
    for name in names or []:
        if any(ch in name for ch in "*?["):
            matches = sorted(p for p in input_dir.glob(name) if p.suffix.lower() in (".csv", ".xlsx", ".xls"))
            if matches:
                return matches[0]
        else:
            p = input_dir / name
            if p.exists():
                return p
    return None


def resolve_columns(headers: list[str], aliases_by_field: dict) -> tuple[dict, list[str]]:
    """{field: [כותרות מקוריות קיימות, לפי סדר ה-aliases]}, [כותרות שלא מופו]."""
    by_norm = {}
    for h in headers:
        by_norm.setdefault(norm_key(h), h)
    mapping, used = {}, set()
    for field, aliases in aliases_by_field.items():
        if isinstance(aliases, str):
            aliases = [aliases]
        cols = []
        for alias in aliases:
            h = by_norm.get(norm_key(alias))
            if h is not None and h not in cols:
                cols.append(h)
        if cols:
            mapping[field] = cols
            used.update(cols)
    unmapped = [h for h in headers if h not in used and str(h).strip()]
    return mapping, unmapped


class Table:
    def __init__(self, entity, path, rows, mapping, unmapped):
        self.entity, self.path, self.rows, self.mapping, self.unmapped = entity, path, rows, mapping, unmapped

    def raw(self, row: dict, field: str):
        for col in self.mapping.get(field, []):
            v = row.get(col)
            if v not in (None, "") and to_text(v) != "":
                return v
        return None

    def text(self, row: dict, field: str) -> str:
        return to_text(self.raw(row, field))

    def all_values(self, row: dict, field: str) -> list:
        return [row.get(c) for c in self.mapping.get(field, []) if to_text(row.get(c)) != ""]


def load_tables(input_dir: Path, cfg, report) -> dict:
    tables = {}
    for entity in ENTITIES:
        names = (cfg["files"].get(entity) or {}).get("names") if isinstance(cfg["files"].get(entity), dict) else cfg["files"].get(entity)
        path = find_file(input_dir, names)
        if path is None:
            report["files"][entity] = {"path": None, "note": "לא נמצא קובץ (אופציונלי)"}
            continue
        rows = read_table(path, cfg)
        headers = list(rows[0].keys()) if rows else []
        mapping, unmapped = resolve_columns(headers, cfg["columns"].get(entity) or {})
        tables[entity] = Table(entity, path, rows, mapping, unmapped)
        report["files"][entity] = {
            "path": str(path), "rows": len(rows), "columns_mapped": mapping, "columns_unmapped": unmapped,
        }
    if "contacts" not in tables and "leads" not in tables:
        raise ConfigError("לא נמצא קובץ contacts וגם לא קובץ leads בתיקיית הקלט - אין ממה ליצור כרטיסים")
    return tables


# ---------------------------------------------------------------- model

def extract_phones(table: Table, row: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """([(raw, e164)] תקינים ללא כפילויות - נייד ישראלי קודם, לפי סדר העמודות), [ערכים לא תקינים]."""
    valid, invalid, seen = [], [], set()
    for value in table.all_values(row, "phone"):
        for part in _PHONE_SPLIT_RE.split(to_text(value)):
            part = part.strip()
            if not part:
                continue
            e164 = normalize_phone(part)
            if not is_valid_phone(e164):
                invalid.append(part)
                continue
            if e164 not in seen:
                seen.add(e164)
                valid.append((part, e164))
    valid.sort(key=lambda pe: 0 if pe[1].startswith(IL_MOBILE_PREFIX) else 1)  # stable: נייד לפני קווי
    return valid, invalid


def build_fields(table: Table, row: dict, cfg) -> dict:
    fields = {}
    for f in CARD_FIELDS:
        v = table.text(row, f)
        if v:
            fields[f] = v
    if "customer_name" not in fields:
        name = " ".join(x for x in (table.text(row, "first_name"), table.text(row, "last_name")) if x)
        if name:
            fields["customer_name"] = name
    return fields


def _sort_ts(rec) -> str:
    ts = rec.get("updated_at") or rec.get("created_at")
    return ts.isoformat() if ts else ""


def read_records(tables, cfg, report) -> tuple[list, list]:
    contacts, leads = [], []
    for entity, out in (("contacts", contacts), ("leads", leads)):
        table = tables.get(entity)
        if table is None:
            continue
        for idx, row in enumerate(table.rows):
            rownum = idx + 2
            phones, invalid = extract_phones(table, row)
            upd = parse_local_dt(table.raw(row, "updated_at"), cfg)
            crt = parse_local_dt(table.raw(row, "created_at"), cfg)
            rec = {
                "entity": entity, "row": rownum,
                "id": table.text(row, "contact_id" if entity == "contacts" else "lead_id") or None,
                "contact_id": table.text(row, "contact_id") or None,
                "phones": phones, "invalid_phones": invalid,
                "fields": build_fields(table, row, cfg),
                "notes": table.text(row, "notes"),
                "status_raw": table.text(row, "status"),
                "source": table.text(row, "source"),
                "updated_at": upd[0] if upd else None,
                "created_at": crt[0] if crt else None,
            }
            if invalid:
                report["warnings"].append(f"{entity} שורה {rownum}: {len(invalid)} מספר/ים לא תקינים התעלמו")
            out.append(rec)
    return contacts, leads


class CardPlan:
    def __init__(self, phone: str, exists: bool):
        self.phone = phone          # מחרוזת הטלפון במפתח הכרטיס (קיים, או E.164 לחדש)
        self.exists = exists
        self.contacts: list = []
        self.leads: list = []
        self.extra_phones: list[str] = []
        self.contact_ids: set = set()
        self.lead_ids: set = set()


class PhoneIndex:
    """אינדקס בזיכרון של הכרטיסים הקיימים ב-tenant (load_customers() אחד בתחילת
    ההרצה) - במקום extract.resolve_existing_phone לכל טלפון (שטוען את כל המאגר בכל
    קריאה). מתעדכן בכרטיסים שמתוכננים להיווצר בהרצה הזו (register).

    lookup - אותה סמנטיקה בדיוק כמו Resolver הקודם (resolve_existing_phone על
    הפורמט הגולמי, ואז על ה-05x המקומי, ואז E.164): המועמדים לפי הסדר הם
    raw, _to_e164(raw), 05x, E.164 - וכרטיס נמצא רק אם המפתח שלו שווה בדיוק
    לאחד מהם. רק אם אין התאמה מדויקת: התאמה לפי נרמול (כרטיס ישן שנשמר בפורמט
    שלישי, למשל "972501234567" בלי "+"), שהייתה יוצרת כפילות - מדווחת בדוח."""

    def __init__(self, tenant: str, snapshot: dict):
        self.tenant = tenant
        self.exact: set[str] = set()
        self.by_norm: dict[str, list[str]] = {}
        self.by_contact_id: dict[str, str] = {}
        self.by_lead_id: dict[str, str] = {}
        for key, card in snapshot.items():
            t, _, phone = key.partition("::")
            if t != tenant:
                continue
            self._add(phone)
            for cid in set(card.get("bmby_contact_ids") or []) | ({card["bmby_contact_id"]} if card.get("bmby_contact_id") else set()):
                self.by_contact_id.setdefault(str(cid), phone)
            for lid in card.get("bmby_lead_ids") or []:
                self.by_lead_id.setdefault(str(lid), phone)
        self.existing = set(self.exact)

    def _add(self, phone: str) -> None:
        if phone in self.exact:
            return
        self.exact.add(phone)
        for n in {extract._to_e164(phone), normalize_phone(phone)}:
            if n:
                self.by_norm.setdefault(n, []).append(phone)

    def register(self, phone: str) -> None:
        self._add(phone)

    def exists(self, phone: str) -> bool:
        return phone in self.existing

    def lookup(self, raw: str, e164: str) -> tuple[str | None, str | None, bool]:
        """(טלפון הכרטיס או None, "exact"/"normalized", ambiguous?)."""
        for cand in (raw, extract._to_e164(raw), local_il_format(e164), e164):
            if cand and cand in self.exact:
                return cand, "exact", False
        hits = self.by_norm.get(e164) or []
        if not hits:
            return None, None, False
        if len(hits) > 1:
            pref = [h for h in hits if h == e164] or [h for h in hits if h == local_il_format(e164)] or sorted(hits)
            return pref[0], "normalized", True
        return hits[0], "normalized", False


def build_plans(contacts, leads, index: PhoneIndex, report) -> tuple[dict, dict, dict]:
    plans: dict[str, CardPlan] = {}
    contact_plan: dict[str, CardPlan] = {}
    lead_plan: dict[str, CardPlan] = {}
    attached: set[int] = set()  # id() של רשומות איש קשר ששויכו (במקום סריקת O(C²))
    contacts_by_id = {c["id"]: c for c in contacts if c["id"]}
    stats_c, stats_l, pm = report["contacts"], report["leads"], report["phone_matching"]
    tenant = index.tenant

    def get_plan(phone: str) -> CardPlan:
        if phone not in plans:
            plans[phone] = CardPlan(phone, index.exists(phone))
            index.register(phone)
        return plans[phone]

    def add_extra(plan: CardPlan, phones) -> None:
        key_e164 = normalize_phone(plan.phone)
        for _raw, e164 in phones:
            if e164 != key_e164 and e164 not in plan.extra_phones:
                plan.extra_phones.append(e164)

    def match_phones(phones, label) -> str | None:
        """כל הטלפונים של הרשומה מול האינדקס (לא רק הראשון). טלפון משני נבדק רק אם הוא
        נייד - קווי משותף (בית/משרד) לא ימזג שני אנשים לכרטיס אחד."""
        hits = []
        for i, (raw, e164) in enumerate(phones):
            if i > 0 and not e164.startswith(IL_MOBILE_PREFIX):
                continue
            found, how, ambiguous = index.lookup(raw, e164)
            if found:
                hits.append((i, found, how, ambiguous))
        if not hits:
            return None
        i, found, how, ambiguous = hits[0]
        pm[how] += 1
        if i > 0:
            pm["secondary_phone"].append({"record": label, "card": mask_key(tenant, found)})
        if ambiguous:
            pm["ambiguous_existing_duplicates"].append({"record": label, "card": mask_key(tenant, found)})
        if len({h[1] for h in hits}) > 1:
            pm["phones_match_multiple_cards"].append({"record": label, "used": mask_key(tenant, found),
                                                      "others": sorted({mask_key(tenant, h[1]) for h in hits[1:] if h[1] != found})})
        return found

    def plan_for(phones, label, bmby_contact_id=None, bmby_lead_id=None) -> CardPlan:
        phone = None
        if bmby_contact_id and bmby_contact_id in index.by_contact_id:
            phone = index.by_contact_id[bmby_contact_id]
            pm["by_bmby_id"] += 1
        elif bmby_lead_id and bmby_lead_id in index.by_lead_id:
            phone = index.by_lead_id[bmby_lead_id]
            pm["by_bmby_id"] += 1
        if phone is None:
            phone = match_phones(phones, label)
        if phone is None:
            phone = phones[0][1]
            pm["new"] += 1
        plan = get_plan(phone)
        add_extra(plan, phones)
        return plan

    def attach_contact(c, plan):
        if id(c) in attached:
            return
        if plan.contacts:
            stats_c["duplicates_merged"] += 1
            report["merged_contacts"].append({"card": mask_key(tenant, plan.phone),
                                              "bmby_contact_ids": sorted({x["id"] or f"row{x['row']}" for x in plan.contacts + [c]})})
        attached.add(id(c))
        plan.contacts.append(c)
        if c["id"]:
            plan.contact_ids.add(c["id"])
            contact_plan[c["id"]] = plan

    for c in contacts:
        stats_c["read"] += 1
        label = f"contacts שורה {c['row']}"
        if c["phones"]:
            attach_contact(c, plan_for(c["phones"], label, bmby_contact_id=c["id"]))
        elif c["id"] and c["id"] in index.by_contact_id:
            attach_contact(c, plan_for([], label, bmby_contact_id=c["id"]))

    seen_lead_ids = set()
    for lead in leads:
        stats_l["read"] += 1
        if lead["id"] and lead["id"] in seen_lead_ids:
            stats_l["skipped"] += 1
            report["skipped_rows"].append({"file": "leads", "row": lead["row"], "reason": f"lead_id כפול בקובץ ({lead['id']})"})
            continue
        if lead["id"]:
            seen_lead_ids.add(lead["id"])
        plan = None
        cid = lead["contact_id"]
        if cid and cid in contact_plan:
            plan = contact_plan[cid]
            add_extra(plan, lead["phones"])
        elif lead["phones"] or (lead["id"] and lead["id"] in index.by_lead_id) or (cid and cid in index.by_contact_id):
            plan = plan_for(lead["phones"], f"leads שורה {lead['row']}", bmby_contact_id=cid, bmby_lead_id=lead["id"])
            if cid and cid in contacts_by_id:
                attach_contact(contacts_by_id[cid], plan)
        if plan is None:
            stats_l["skipped"] += 1
            if cid and cid in contacts_by_id:
                reason = "איש הקשר המקושר בלי טלפון תקין"
            elif cid:
                reason = f"contact_id {cid} לא נמצא בקובץ אנשי הקשר ואין לליד טלפון"
            else:
                reason = "אין טלפון תקין ואין contact_id"
            report["skipped_rows"].append({"file": "leads", "row": lead["row"], "reason": reason})
            continue
        plan.leads.append(lead)
        if lead["id"]:
            plan.lead_ids.add(lead["id"])
            lead_plan[lead["id"]] = plan
        stats_l["linked"] += 1

    for c in contacts:
        if id(c) not in attached:
            stats_c["skipped"] += 1
            reason = "אין טלפון תקין" + (f" (ערכים לא תקינים: {len(c['invalid_phones'])})" if c["invalid_phones"] else "")
            report["skipped_rows"].append({"file": "contacts", "row": c["row"], "reason": reason})
        else:
            stats_c["with_card"] += 1
    return plans, contact_plan, lead_plan


def map_lead_status(raw: str, cfg) -> tuple[str | None, bool]:
    """(status פנימי או None, mapped?) - ערך שכבר תקין (למשל 'hot') מתקבל כמו שהוא."""
    if not raw:
        return None, True
    k = norm_key(raw)
    if k in cfg["lead_status_map"]:
        return cfg["lead_status_map"][k], True
    if k in VALID_LEAD_STATUSES:
        return k, True
    return None, False


def compose_plan(plan: CardPlan, cfg, today: str, report) -> dict:
    """מחשב מה ייכתב לכרטיס: שדות, סטטוס, בלוק הערות, מזהי BMBY."""
    contacts = sorted(plan.contacts, key=_sort_ts)  # הישן קודם, העדכני אחרון
    leads = sorted(plan.leads, key=lambda r: (_sort_ts(r), r["row"]))
    latest_lead = leads[-1] if leads else None

    fields = {}
    for rec in contacts:  # העדכני דורס
        fields.update(rec["fields"])
    for lead in leads:  # ליד: מאכלס שדות חסרים מכל הלידים; שדות-ליד מהעדכני ביותר גוברים
        for f, v in lead["fields"].items():
            if f in LEAD_PRIORITY_FIELDS or f not in fields:
                fields[f] = v
    if cfg.get("default_category") and "category" not in fields:
        fields["category"] = cfg["default_category"]

    status_rec = latest_lead if latest_lead and latest_lead["status_raw"] else None
    if status_rec is None:
        for rec in reversed(contacts):
            if rec["status_raw"]:
                status_rec = rec
                break
    status_raw = status_rec["status_raw"] if status_rec else ""
    status, mapped = map_lead_status(status_raw, cfg)
    if not mapped:
        report["unmapped_lead_statuses"][status_raw] = report["unmapped_lead_statuses"].get(status_raw, 0) + 1
    status_updated = (status_rec or {}).get("updated_at") or (status_rec or {}).get("created_at")

    lines = []
    for rec in contacts:
        if rec["notes"]:
            lines.append(rec["notes"])
    for lead in reversed(leads):
        if lead["notes"]:
            prefix = f"(ליד #{lead['id']}) " if len(leads) > 1 and lead["id"] else ""
            lines.append(prefix + lead["notes"])
    sources = list(dict.fromkeys(r["source"] for r in contacts + leads if r["source"]))
    if sources:
        lines.append("מקור בבמבי: " + ", ".join(sources))
    if plan.extra_phones:
        lines.append("טלפונים נוספים: " + ", ".join(plan.extra_phones))
    if status_raw and not mapped:
        lines.append(f"סטטוס בבמבי: {status_raw}")
    if len(leads) > 1:
        others = []
        for lead in leads[:-1]:
            bits = [f"#{lead['id'] or 'שורה ' + str(lead['row'])}"]
            if lead["fields"].get("project"):
                bits.append(f"פרויקט {lead['fields']['project']}")
            if lead["status_raw"]:
                bits.append(f"סטטוס {lead['status_raw']}")
            ts = lead["updated_at"] or lead["created_at"]
            if ts:
                bits.append(f"עודכן {ts.strftime('%d/%m/%Y')}")
            others.append(", ".join(bits))
        lines.append("לידים נוספים בבמבי: " + " | ".join(others))
        report["multi_lead_cards"].append({
            "card": mask_key(report["tenant"], plan.phone),
            "kept_lead_id": latest_lead["id"], "other_lead_ids": [l["id"] for l in leads[:-1]],
            "projects": [l["fields"].get("project") for l in leads],
        })
    notes_block = (NOTES_BLOCK_START + "\n" + "\n".join(lines) + "\n" + NOTES_BLOCK_END) if lines else None

    return {
        "fields": fields, "status": status, "status_raw": status_raw, "status_mapped": mapped,
        "status_updated_at": to_utc_iso(status_updated) if status_updated else None,
        "notes_block": notes_block,
        "contact_ids": sorted(plan.contact_ids), "lead_ids": sorted(plan.lead_ids),
        "import_source": f"bmby_migration_{today}",
    }


def _merge_notes(existing: str | None, block: str | None) -> tuple[str | None, str | None]:
    """(notes חדש, תוכן הבלוק הקודם אם היה). בלוק קיים מוחלף במקומו - כל טקסט שנכתב
    *בתוך* הבלוק (בין שורות הפתיחה והסיום) נדרס; טקסט מחוץ לבלוק נשמר."""
    existing = existing or ""
    if block is None:
        return existing or None, None
    m = _NOTES_BLOCK_RE.search(existing)
    if m:
        return existing[:m.start()] + block + existing[m.end():], m.group(0)
    return ((existing.rstrip() + "\n\n" + block) if existing.strip() else block), None


def _block_body(block: str | None) -> str:
    """תוכן הבלוק בלי שורות הפתיחה/הסיום (הכותרת השתנתה בין גרסאות)."""
    lines = (block or "").splitlines()
    return "\n".join(lines[1:-1]).strip()


def fields_for_import(existing: dict | None, comp: dict, cfg, report, tenant, phone) -> dict:
    """field_policy=fill_missing: לא דורסים ערך קיים שונה בכרטיס שכבר קיים (נרשם כקונפליקט)."""
    out = {}
    for f, v in comp["fields"].items():
        cur = (existing or {}).get(f)
        if existing and cur and cur != v and cfg["field_policy"] == "fill_missing":
            report["field_conflicts"].append({"card": mask_key(tenant, phone), "field": f, "kept": "existing"})
            continue
        out[f] = v
    if not existing or not existing.get("import_source") or cfg["field_policy"] == "overwrite":
        out["import_source"] = comp["import_source"]
    return out


def make_card_mutator(comp: dict, cfg, outcome: dict, now_iso: str):
    """mutate(card, is_new) לשדות ש-import (upsert) לא מטפל בהם נכון. רץ אחרי upsert השדות,
    באותה כתיבה (extract.import_leads_batch), וגם על עותק ב-dry-run (אותה ספירה בדיוק).

    - lead_status: נכתב רק אם לכרטיס אין סטטוס (status_policy=fill_missing) או ב-overwrite.
      status_changed_at: לליד חדש/בלי סטטוס - **לא נקבע** (scheduler._process_followup_lead
      מתחיל טיימר פולו-אפ רק לכרטיס עם status_changed_at; תאריך ישן מבמבי היה גורם לשלב 1
      להיות "באיחור" מיד, ו-"now" היה מתחיל פולו-אפ ללקוחות במבי תוך 24ש'). ב-overwrite של
      ליד חי: אם הסטטוס **זהה** - status_changed_at נשמר (הטיימר ממשיך כרגיל); אם הסטטוס
      **השתנה** - status_changed_at מוסר (טיימר של הסטטוס הקודם לא רלוונטי, וטיימר חדש היה
      שולח פולו-אפ ללקוח בלי החלטה), והערכים הקודמים נשמרים ב-bmby_prev_lead_status/
      bmby_prev_status_changed_at. התאריך המקורי מבמבי נשמר ב-bmby_status_updated_at.
    - notes: בלוק "ייבוא מבמבי" מוחלף במקום (הרצה חוזרת) או מתווסף - הערות מחוץ לבלוק נשמרות.
    - bmby_contact_id/bmby_contact_ids/bmby_lead_ids/bmby_status - לאידמפוטנטיות ולביקורת.
    לא נוגע ב-history."""

    def _mutate(card: dict, _is_new: bool) -> None:
        status = comp["status"]
        outcome["status"] = None
        if status:
            cur = card.get("lead_status")
            if cur == status:
                outcome["status"] = "unchanged"
            elif cur is None or cfg["status_policy"] == "overwrite":
                if cur is not None:
                    card["bmby_prev_lead_status"] = cur
                    if card.get("status_changed_at"):
                        card["bmby_prev_status_changed_at"] = card["status_changed_at"]
                card["lead_status"] = status
                card.pop("status_changed_at", None)
                outcome["status"] = "set"
            else:
                outcome["status"] = "kept_existing"
        merged, old_block = _merge_notes(card.get("notes"), comp["notes_block"])
        outcome["notes_block_changed"] = old_block is not None and _block_body(old_block) != _block_body(comp["notes_block"])
        if merged:
            card["notes"] = merged
        if comp["contact_ids"]:
            card["bmby_contact_ids"] = sorted(set(card.get("bmby_contact_ids") or []) | set(comp["contact_ids"]))
            card.setdefault("bmby_contact_id", comp["contact_ids"][0])
        if comp["lead_ids"]:
            card["bmby_lead_ids"] = sorted(set(card.get("bmby_lead_ids") or []) | set(comp["lead_ids"]))
        if comp["status_raw"]:
            card["bmby_status"] = comp["status_raw"]
        if comp["status_updated_at"]:
            card["bmby_status_updated_at"] = comp["status_updated_at"]
        card.setdefault("bmby_migrated_at", now_iso)

    return _mutate


# ---------------------------------------------------------------- tasks / activities

def link_plan(table: Table, row: dict, contact_plan, lead_plan, index: PhoneIndex) -> tuple[str | None, str]:
    """(טלפון הכרטיס או None, סיבה). lead_id קודם, אחר כך contact_id; גם מזהים מהרצה קודמת."""
    lid, cid = table.text(row, "lead_id"), table.text(row, "contact_id")
    if lid and lid in lead_plan:
        return lead_plan[lid].phone, ""
    if cid and cid in contact_plan:
        return contact_plan[cid].phone, ""
    if lid and lid in index.by_lead_id:
        return index.by_lead_id[lid], ""
    if cid and cid in index.by_contact_id:
        return index.by_contact_id[cid], ""
    if not lid and not cid:
        return None, "אין lead_id/contact_id"
    return None, f"לא מקושר לכרטיס (lead_id={lid or '-'}, contact_id={cid or '-'} - חסר/בלי טלפון)"


def plan_tasks(table: Table | None, cfg, args, contact_plan, lead_plan, index, report, now_local: datetime) -> list[dict]:
    """משימות מבמבי -> calendar_tasks. פגישות (task_type=meeting) - כדי שהסקדולר לא יפעל
    על פגישות מיובאות בלי החלטה:
    - פגישה פתוחה שמועדה **עבר**: מיובאת כמשימת מעקב (task_type=followup, כותרת
      "פגישה שעברה (במבי): ...") - check_pending_marketer_reminders (שאין לו דגל AUTO)
      היה יוצר מיד "📞 אישור הגעה" לכל פגישה ישנה, ותזכורת/זימון היו "באיחור".
    - פגישה **עתידית**: task_type=meeting עם invite_sent=1 (הוזמנו כבר בבמבי - אין זימון
      שני), reminder_sent=1 אלא אם future_meeting_reminder=true, ו-marketer_reminder_status
      ריק (משימת "📞 אישור הגעה" פנימית תיווצר בזמנה) אלא אם future_meeting_marketer_task=false.
    - משימה שבוצעה/בוטלה (--include-done-tasks): כל הדגלים מסומנים (הסקדולר ממילא סורק
      רק pending)."""
    out, seen_refs = [], set()
    if table is None:
        return out
    st = report["tasks"]
    today_local = now_local.date()
    for idx, row in enumerate(table.rows):
        rownum = idx + 2
        st["read"] += 1

        def skip(reason, counter="skipped_invalid"):
            st[counter] += 1
            report["skipped_rows"].append({"file": "tasks", "row": rownum, "reason": reason})

        status_raw = table.text(row, "status")
        if not status_raw:
            status = cfg["task_status_empty"]
        else:
            status = cfg["task_status_map"].get(norm_key(status_raw))
            if status is None:
                report["unmapped_task_statuses"][status_raw] = report["unmapped_task_statuses"].get(status_raw, 0) + 1
                status = cfg["task_status_default"]
        if status is None:
            skip(f"סטטוס משימה לא ממופה: '{status_raw}'", "skipped_unmapped_status")
            continue
        if status != "pending" and not args.include_done_tasks:
            st["skipped_not_open"] += 1
            continue
        phone, reason = link_plan(table, row, contact_plan, lead_plan, index)
        if phone is None:
            skip(reason, "skipped_unlinked")
            continue
        task_id = table.text(row, "task_id")
        title = table.text(row, "title")
        desc = table.text(row, "description")
        type_raw = table.text(row, "type")
        due_raw = table.raw(row, "due_date")
        ref = f"{TASK_REF_PREFIX}{task_id}" if task_id else f"{TASK_REF_PREFIX}h:" + stable_hash(
            table.text(row, "lead_id"), table.text(row, "contact_id"), title, desc, due_raw, type_raw)
        if ref in seen_refs:
            skip(f"משימה כפולה בקובץ ({ref})", "skipped_duplicate_in_file")
            continue
        seen_refs.add(ref)

        due = parse_local_dt(due_raw, cfg)
        if due is None:
            if due_raw not in (None, "") or cfg["task_missing_due_date"] == "skip":
                skip(f"תאריך יעד חסר/לא מפוענח: '{to_text(due_raw)}'")
                continue
            due_date, has_time, due_dt = today_local.isoformat(), False, None
        else:
            due_dt, has_time = due
            due_date = due_dt.date().isoformat()
        due_time = parse_time_of_day(table.raw(row, "due_time")) or (due_dt.strftime("%H:%M") if due_dt and has_time else None)

        task_type = cfg["task_type_map"].get(norm_key(type_raw), None) if type_raw else None
        if type_raw and task_type is None:
            report["unmapped_task_types"][type_raw] = report["unmapped_task_types"].get(type_raw, 0) + 1
        task_type = task_type or cfg["task_type_default"]

        note_lines = [desc] if desc else []
        if table.text(row, "agent"):
            note_lines.append(f"נציג: {table.text(row, 'agent')}")
        if type_raw:
            note_lines.append(f"סוג בבמבי: {type_raw}")
        crt = parse_local_dt(table.raw(row, "created_at"), cfg)
        note_lines.append(f"יובא מבמבי (משימה {'#' + task_id if task_id else 'ללא מזהה'}"
                          + (f", נוצרה {crt[0].strftime('%d/%m/%Y')}" if crt else "") + ")")
        title = title or type_raw or (desc[:60] if desc else "משימה מבמבי")

        flags = {"reminder_sent": False, "invite_sent": False, "marketer_reminder_status": None}
        meeting_kind = None
        if task_type == "meeting":
            hh, mm = (int(x) for x in due_time.split(":")) if due_time else (23, 59)
            d = date.fromisoformat(due_date)
            meeting_at = datetime(d.year, d.month, d.day, hh, mm, tzinfo=cfg["_tz"])
            if status != "pending":
                flags = {"reminder_sent": True, "invite_sent": True, "marketer_reminder_status": "bmby_imported"}
                meeting_kind = "closed"
            elif meeting_at < now_local:
                task_type = "followup"
                title = f"פגישה שעברה (במבי): {title}"
                note_lines.append("פגישה שמועדה עבר בזמן הייבוא - יובאה כמשימת מעקב (לא כפגישה), "
                                  "כדי שלא תיווצר לה תזכורת/זימון/משימת אישור-הגעה אוטומטית. לעדכן תוצאה.")
                flags = {"reminder_sent": True, "invite_sent": True, "marketer_reminder_status": "bmby_imported"}
                meeting_kind = "past_as_followup"
            else:
                flags = {
                    "reminder_sent": not cfg["future_meeting_reminder"],
                    "invite_sent": True,
                    "marketer_reminder_status": None if cfg["future_meeting_marketer_task"] else "bmby_imported",
                }
                meeting_kind = "future"
        out.append({
            "row": rownum, "phone": phone, "title": title,
            "due_date": due_date, "due_time": due_time, "notes": "\n".join(note_lines),
            "status": status, "task_type": task_type, "external_ref": ref,
            "past_due": due_date < today_local.isoformat(), "meeting_kind": meeting_kind, **flags,
        })
    return out


def plan_activities(table: Table | None, cfg, contact_plan, lead_plan, index, report) -> dict:
    by_phone: dict[str, list] = {}
    if table is None:
        return by_phone
    st = report["notes"]
    seen = set()
    prefix = cfg["history_message_prefix"]
    for idx, row in enumerate(table.rows):
        rownum = idx + 2
        st["read"] += 1

        def skip(reason, counter="skipped_invalid"):
            st[counter] += 1
            report["skipped_rows"].append({"file": "activities", "row": rownum, "reason": reason})

        phone, reason = link_plan(table, row, contact_plan, lead_plan, index)
        if phone is None:
            skip(reason, "skipped_unlinked")
            continue
        text, subject, type_raw = table.text(row, "text"), table.text(row, "subject"), table.text(row, "type")
        if not text and not subject:
            skip("פעילות ריקה (אין טקסט/נושא)")
            continue
        when_raw = table.raw(row, "date")
        when = parse_local_dt(when_raw, cfg)
        if when is None:
            skip(f"תאריך פעילות חסר/לא מפוענח: '{to_text(when_raw)}'")
            continue
        act_id = table.text(row, "activity_id")
        ref = f"bmby:activity:{act_id}" if act_id else "bmby:activity:h:" + stable_hash(
            table.text(row, "lead_id"), table.text(row, "contact_id"), when_raw, type_raw, subject, text)
        if ref in seen:
            skip(f"פעילות כפולה בקובץ ({ref})", "skipped_duplicate_in_file")
            continue
        seen.add(ref)
        body = " - ".join(x for x in (subject, text) if x)
        core = (f"{type_raw}: " if type_raw else "") + body
        agent = table.text(row, "agent")
        if agent:
            core += f" (נציג: {agent})"
        by_phone.setdefault(phone, []).append({
            "timestamp": to_utc_iso(when[0]),
            "message": prefix + core, "core": core, "ref": ref,
        })
    for entries in by_phone.values():
        entries.sort(key=lambda e: e["timestamp"])
    return by_phone


def _note_exists(existing_notes: dict, phone: str, entry: dict) -> bool:
    """אותו טלפון + אותה חותמת זמן (UTC, שניות) + אותו תוכן (גם אם הקידומת שונתה בין הרצות)."""
    key = (phone, db._norm_ts(entry["timestamp"]))
    return any(m == entry["message"] or m.endswith(entry["core"]) for m in existing_notes.get(key, ()))


# ---------------------------------------------------------------- main

def load_snapshot(dry_run: bool) -> dict:
    if db.IS_POSTGRES and dry_run and not db.customers_table_exists_readonly():
        return {}  # מסד ריק - אין כרטיסים (ובלי ליצור טבלאות ב-dry-run)
    return extract.load_customers()


def write_backup(stamp: str) -> Path:
    path = DATA_DIR / f"customers.backup_bmby_{stamp}.json"
    with open(path, "x", encoding="utf-8") as fh:  # "x" - לעולם לא דורס גיבוי קיים
        json.dump(extract.load_customers(), fh, ensure_ascii=False, indent=2)
    return path


def new_report(args, target_desc) -> dict:
    return {
        "mode": "apply" if args.apply else "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": args.tenant, "target": target_desc, "data_dir": str(DATA_DIR),
        "input_dir": str(args.input), "backup": None, "elapsed_seconds": None,
        "files": {},
        "contacts": {"read": 0, "with_card": 0, "skipped": 0, "duplicates_merged": 0},
        "leads": {"read": 0, "linked": 0, "skipped": 0},
        "phone_matching": {"by_bmby_id": 0, "exact": 0, "normalized": 0, "new": 0,
                           "secondary_phone": [], "ambiguous_existing_duplicates": [], "phones_match_multiple_cards": []},
        "cards": {"planned": 0, "created": 0, "updated": 0, "errors": 0,
                  "status_set": 0, "status_unchanged": 0, "status_kept_existing": 0, "status_none": 0,
                  "notes_block_changed": 0},
        "tasks": {"read": 0, "created": 0, "skipped_existing": 0, "skipped_not_open": 0, "skipped_unlinked": 0,
                  "skipped_unmapped_status": 0, "skipped_invalid": 0, "skipped_duplicate_in_file": 0,
                  "skipped_card_failed": 0, "past_due_open": 0, "meetings_future": 0,
                  "meetings_past_as_followup": 0, "errors": 0},
        "notes": {"read": 0, "inserted": 0, "skipped_duplicate": 0, "skipped_unlinked": 0,
                  "skipped_invalid": 0, "skipped_duplicate_in_file": 0, "skipped_card_failed": 0, "errors": 0},
        "unmapped_lead_statuses": {}, "unmapped_task_statuses": {}, "unmapped_task_types": {},
        "multi_lead_cards": [], "merged_contacts": [], "field_conflicts": [], "notes_blocks_changed": [],
        "orphans_card_failed": [], "ai_context_risk": [],
        "skipped_rows": [], "errors": [], "warnings": [],
        "notes_for_dashboard": (
            "הערות/פעילויות מבמבי נכתבות לטבלת messages כהערה פנימית (channel='note', direction='out') "
            "עם חותמת הזמן המקורית - מופיעות בציר הזמן של הליד בדשבורד (GET /api/messages), לא נשלחות "
            "ללקוח, ולא נכתבות ל-card['history'] (שמוזן למענה האוטומטי - extract._format_history_for_reply)."
        ),
        "notes_block_warning": (
            f"בלוק ההערות בכרטיס ('{NOTES_BLOCK_START}' ... '{NOTES_BLOCK_END}') נכתב מחדש בכל הרצה: "
            "טקסט שנציג הקליד *בתוך* הבלוק נדרס בייבוא חוזר. טקסט מחוץ לבלוק נשמר. "
            "notes_blocks_changed = כרטיסים שבהם תוכן הבלוק הקיים שונה ממה שייכתב עכשיו."
        ),
    }


def run(args, target: dict) -> dict:
    started = _time.monotonic()
    cfg = load_config(args.config)
    target_desc = describe_target(target)
    report = new_report(args, target_desc)
    dry = not args.apply
    print("=" * 70)
    print(f"מצב: {'APPLY (כתיבה אמיתית)' if args.apply else 'DRY-RUN (שום דבר לא משתנה)'} | tenant={args.tenant}")
    print(f"יעד: {target_desc}")
    print(f"DATA_DIR: {DATA_DIR}")
    if target["kind"] == "database" and target["remote"]:
        print("⚠️  יעד מרוחק" + (" - כתיבה אמיתית!" if args.apply else " - קריאה בלבד (dry-run)"))
    print("=" * 70)
    if dry and db.IS_POSTGRES:
        # dry-run = קריאה בלבד: מדלגים על db._init_schema (CREATE/ALTER) בחיבור הראשון.
        db._schema_ready = True

    tables = load_tables(Path(args.input), cfg, report)
    snapshot = load_snapshot(dry)
    index = PhoneIndex(args.tenant, snapshot)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_local = datetime.now(cfg["_tz"])
    now_iso = datetime.now(timezone.utc).isoformat()
    tenant = args.tenant

    contacts, leads = read_records(tables, cfg, report)
    plans, contact_plan, lead_plan = build_plans(contacts, leads, index, report)
    tasks = plan_tasks(tables.get("tasks"), cfg, args, contact_plan, lead_plan, index, report, now_local)
    activities = plan_activities(tables.get("activities"), cfg, contact_plan, lead_plan, index, report)

    comps = {phone: compose_plan(plan, cfg, today, report) for phone, plan in plans.items()}
    report["cards"]["planned"] = len(plans)

    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report["backup"] = str(write_backup(stamp))
        print(f"גיבוי כל הכרטיסים: {report['backup']}")

    # ---- כרטיסים: upsert + מטא-דאטה בכתיבה אחת לכרטיס
    items = []
    for phone, plan in plans.items():
        comp = comps[phone]
        existing = snapshot.get(f"{tenant}::{phone}")
        outcome: dict = {}
        items.append({
            "phone": phone, "fields": fields_for_import(existing, comp, cfg, report, tenant, phone),
            "mutate": make_card_mutator(comp, cfg, outcome, now_iso), "outcome": outcome, "exists": plan.exists,
        })
    if dry:
        results = []
        for item in items:  # אותה לוגיקה בדיוק על עותק - בלי כתיבה
            current = snapshot.get(f"{tenant}::{item['phone']}")
            card = copy.deepcopy(current) if current is not None else {"phone": item["phone"], "tenant_id": tenant}
            for f, v in item["fields"].items():
                if v:
                    card[f] = v
            item["mutate"](card, current is None)
            results.append({"ok": True, "is_new": current is None, "error": None})
    else:
        results = extract.import_leads_batch(
            [{"phone": i["phone"], "fields": i["fields"], "mutate": i["mutate"]} for i in items], tenant_id=tenant)
    failed_cards: set[str] = set()
    status_counter = {"set": "status_set", "unchanged": "status_unchanged",
                      "kept_existing": "status_kept_existing", None: "status_none"}
    for item, res in zip(items, results):
        if not res["ok"]:
            failed_cards.add(item["phone"])
            report["cards"]["errors"] += 1
            report["errors"].append({"card": mask_key(tenant, item["phone"]), "stage": "card",
                                     "error": sanitize(f"{type(res['error']).__name__}: {res['error']}")})
            continue
        report["cards"]["created" if res["is_new"] else "updated"] += 1
        report["cards"][status_counter[item["outcome"].get("status")]] += 1
        if item["outcome"].get("notes_block_changed"):
            report["cards"]["notes_block_changed"] += 1
            report["notes_blocks_changed"].append(mask_key(tenant, item["phone"]))

    # ---- הערות/פעילויות -> messages (channel='note'), חותמת זמן מקורית
    existing_notes = db.existing_note_messages(tenant) if activities else {}
    for phone, entries in activities.items():
        card = snapshot.get(f"{tenant}::{phone}") or {}
        if card.get("speed_to_lead_outreach_sent") and card.get("ai_enabled", True) is not False:
            # server.py בונה את הקשר בירור-הצרכים (_run_lead_qualification_turn - תשובה ללקוח) מ-6
            # ההודעות האחרונות בטבלת messages, כולל channel='note'. החוסם היחיד לכרטיס מיובא הוא
            # import_source - וכרטיס עם speed_to_lead_outreach_sent עוקף אותו.
            report["ai_context_risk"].append(mask_key(tenant, phone))
        if phone in failed_cards:
            report["notes"]["skipped_card_failed"] += len(entries)
            report["orphans_card_failed"].append({"card": mask_key(tenant, phone), "kind": "notes", "count": len(entries)})
            continue
        new = [e for e in entries if not _note_exists(existing_notes, phone, e)]
        report["notes"]["skipped_duplicate"] += len(entries) - len(new)
        if not new:
            continue
        if dry:
            report["notes"]["inserted"] += len(new)
            continue
        try:
            report["notes"]["inserted"] += db.insert_imported_notes(
                tenant, phone, [{"timestamp": e["timestamp"], "message": e["message"]} for e in new])
        except Exception as exc:
            report["notes"]["errors"] += len(new)
            report["errors"].append({"card": mask_key(tenant, phone), "stage": "notes",
                                     "error": sanitize(f"{type(exc).__name__}: {exc}")})

    # ---- משימות
    existing_refs = db.existing_task_external_refs(tenant, TASK_REF_PREFIX) if tasks else set()
    for task in tasks:
        if task["phone"] in failed_cards:
            report["tasks"]["skipped_card_failed"] += 1
            report["orphans_card_failed"].append({"card": mask_key(tenant, task["phone"]), "kind": "task",
                                                  "row": task["row"], "external_ref": task["external_ref"]})
            continue
        if task["external_ref"] in existing_refs:
            report["tasks"]["skipped_existing"] += 1
            continue
        if not dry:
            try:
                new_id = db.create_imported_task(
                    phone=task["phone"], tenant_id=tenant, title=task["title"], due_date=task["due_date"],
                    due_time=task["due_time"], notes=task["notes"], status=task["status"],
                    task_type=task["task_type"], external_ref=task["external_ref"],
                    reminder_sent=task["reminder_sent"], invite_sent=task["invite_sent"],
                    marketer_reminder_status=task["marketer_reminder_status"],
                )
            except Exception as exc:
                report["tasks"]["errors"] += 1
                report["errors"].append({"row": task["row"], "stage": "task", "error": sanitize(f"{type(exc).__name__}: {exc}")})
                continue
            if new_id is None:
                report["tasks"]["skipped_existing"] += 1
                continue
        report["tasks"]["created"] += 1
        report["tasks"]["past_due_open"] += int(task["past_due"] and task["status"] == "pending")
        report["tasks"]["meetings_future"] += int(task["meeting_kind"] == "future")
        report["tasks"]["meetings_past_as_followup"] += int(task["meeting_kind"] == "past_as_followup")

    if report["tasks"]["meetings_future"]:
        report["warnings"].append(
            f"{report['tasks']['meetings_future']} פגישות עתידיות יובאו כ-meeting: invite_sent=1 (בלי זימון נוסף)"
            + (", reminder_sent=1 (בלי תזכורת WhatsApp אוטומטית)" if not cfg["future_meeting_reminder"] else
               ", ⚠️ תזכורת WhatsApp תישלח אם SCHEDULER_AUTO_MEETING_REMINDER יודלק")
            + ("; משימת '📞 אישור הגעה' פנימית תיווצר בזמנה" if cfg["future_meeting_marketer_task"] else "") + ".")
    if report["tasks"]["meetings_past_as_followup"]:
        report["warnings"].append(
            f"{report['tasks']['meetings_past_as_followup']} פגישות פתוחות שמועדן עבר יובאו כמשימות מעקב (לא כפגישות).")
    if report["unmapped_lead_statuses"]:
        report["warnings"].append("יש סטטוסי במבי לא ממופים - הסטטוס בכרטיס לא שונה, הטקסט המקורי נשמר ב-notes/bmby_status.")
    if report["ai_context_risk"]:
        report["warnings"].append(
            f"{len(report['ai_context_risk'])} כרטיסים עם speed_to_lead_outreach_sent (בירור צרכים AI פעיל) יקבלו הערות "
            "מיובאות - server.py מזין את 6 ההודעות האחרונות בטבלת messages (כולל הערות) להקשר התשובה ללקוח. "
            "לשקול ai_enabled=false להם לפני --apply. רשימה: ai_context_risk.")
    if report["cards"]["notes_block_changed"]:
        report["warnings"].append(
            f"{report['cards']['notes_block_changed']} כרטיסים: בלוק 'ייבוא מבמבי' הקיים שונה ממה שייכתב - "
            "טקסט שהוקלד בתוך הבלוק (אם יש) יידרס. רשימה: notes_blocks_changed.")
    report["elapsed_seconds"] = round(_time.monotonic() - started, 2)
    return report


def print_report(report: dict) -> None:
    pre = "[DRY-RUN - צפוי] " if report["mode"] == "dry-run" else ""
    print("\n=== קבצים ===")
    for entity, info in report["files"].items():
        if not info.get("path"):
            print(f"  {entity}: {info.get('note')}")
            continue
        print(f"  {entity}: {Path(info['path']).name} - {info['rows']} שורות; מופו: {', '.join(info['columns_mapped']) or '-'}")
        if info["columns_unmapped"]:
            print(f"    עמודות לא ממופות: {', '.join(map(str, info['columns_unmapped']))}")
    c, l, cards, t, n, pm = (report["contacts"], report["leads"], report["cards"], report["tasks"], report["notes"],
                             report["phone_matching"])
    print(f"\n=== {pre}סיכום ===")
    print(f"  אנשי קשר: נקראו {c['read']}, שויכו לכרטיס {c['with_card']}, דולגו {c['skipped']}, כפולים שמוזגו {c['duplicates_merged']}")
    print(f"  לידים: נקראו {l['read']}, שויכו {l['linked']}, דולגו {l['skipped']}, כרטיסים עם כמה לידים {len(report['multi_lead_cards'])}")
    print(f"  איתור כרטיס: לפי מזהה במבי {pm['by_bmby_id']}, טלפון מדויק {pm['exact']}, טלפון מנורמל {pm['normalized']}, "
          f"חדש {pm['new']}; טלפון משני {len(pm['secondary_phone'])}, כפילויות קיימות {len(pm['ambiguous_existing_duplicates'])}, "
          f"טלפונים לכמה כרטיסים {len(pm['phones_match_multiple_cards'])}")
    print(f"  כרטיסים: {cards['planned']} | נוצרו {cards['created']}, עודכנו {cards['updated']}, שגיאות {cards['errors']}")
    print(f"    סטטוס: נקבע {cards['status_set']}, ללא שינוי {cards['status_unchanged']}, "
          f"נשמר קיים {cards['status_kept_existing']}, ללא סטטוס/לא ממופה {cards['status_none']}")
    print(f"    קונפליקטים בשדות (נשמר הערך הקיים): {len(report['field_conflicts'])}; בלוק הערות שהשתנה: {cards['notes_block_changed']}")
    print(f"  משימות: נקראו {t['read']}, נוצרו {t['created']}, קיימות כבר {t['skipped_existing']}, לא פתוחות {t['skipped_not_open']}, "
          f"לא מקושרות {t['skipped_unlinked']}, סטטוס לא ממופה {t['skipped_unmapped_status']}, לא תקינות {t['skipped_invalid']}, "
          f"כפולות בקובץ {t['skipped_duplicate_in_file']}, כרטיס נכשל {t['skipped_card_failed']}; "
          f"פתוחות-באיחור {t['past_due_open']}, פגישות עתידיות {t['meetings_future']}, פגישות שעברו->מעקב {t['meetings_past_as_followup']}")
    print(f"  הערות (messages, channel=note): נקראו {n['read']}, נוספו {n['inserted']}, כבר קיימות {n['skipped_duplicate']}, "
          f"לא מקושרות {n['skipped_unlinked']}, לא תקינות {n['skipped_invalid']}, כפולות בקובץ {n['skipped_duplicate_in_file']}, "
          f"כרטיס נכשל {n['skipped_card_failed']}")
    for title, key in (("סטטוסי ליד לא ממופים", "unmapped_lead_statuses"), ("סטטוסי משימה לא ממופים", "unmapped_task_statuses"),
                       ("סוגי משימה לא ממופים", "unmapped_task_types")):
        if report[key]:
            print(f"  {title}: " + ", ".join(f"'{k}' x{v}" for k, v in report[key].items()))
    for m in report["multi_lead_cards"][:50]:
        print(f"  כמה לידים לכרטיס {m['card']}: נשמר #{m['kept_lead_id']}, נוספים {m['other_lead_ids']} (פרויקטים {m['projects']})")
    if report["skipped_rows"]:
        print(f"\n=== שורות שדולגו ({len(report['skipped_rows'])}) ===")
        for s in report["skipped_rows"][:100]:
            print(f"  {s['file']} שורה {s['row']}: {s['reason']}")
        if len(report["skipped_rows"]) > 100:
            print("  ... (הרשימה המלאה בדוח ה-JSON)")
    print(f"\n  ℹ {report['notes_block_warning']}")
    for w in report["warnings"][:50]:
        print(f"  ⚠ {w}")
    for e in report["errors"][:100]:
        print(f"  ✗ שגיאה: {e}")
    if report["backup"]:
        print(f"\nגיבוי: {report['backup']}")
    print(f"זמן ריצה: {report['elapsed_seconds']} שנ'")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="מיגרציה מ-BMBY (dry-run כברירת מחדל)")
    ap.add_argument("--target", required=True, choices=("local", "database"),
                    help="local = DATA_DIR/customers.json (DATABASE_URL מאולץ לריק); database = Postgres מ-DATABASE_URL")
    ap.add_argument("--input", required=True, help="תיקייה עם קבצי הייצוא (CSV/XLSX)")
    ap.add_argument("--config", required=True, help="קובץ מיפוי (ראו bmby_mapping.example.json)")
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--apply", action="store_true", help="לבצע בפועל (ברירת מחדל: dry-run)")
    ap.add_argument("--report", help="נתיב לדוח JSON (ברירת מחדל: DATA_DIR/bmby_migration_report_<mode>_<UTC>.json)")
    ap.add_argument("--include-done-tasks", action="store_true", help="לייבא גם משימות שבוצעו/בוטלו")
    ap.add_argument("--allow-remote-db", action="store_true", help="לאפשר --apply מול Postgres שאינו localhost")
    args = ap.parse_args(argv)
    if not Path(args.input).is_dir():
        print(f"שגיאה: --input אינו תיקייה: {args.input}", file=sys.stderr)
        return 2
    try:
        target = resolve_target(args.target, args.apply, args.allow_remote_db)
        bootstrap(target)
        report = run(args, target)
    except (ConfigError, ValueError, json.JSONDecodeError) as exc:
        print(f"שגיאה: {sanitize(exc)}", file=sys.stderr)
        return 2
    print_report(report)
    report_path = Path(args.report) if args.report else DATA_DIR / (
        f"bmby_migration_report_{report['mode']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nדוח JSON: {report_path}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
