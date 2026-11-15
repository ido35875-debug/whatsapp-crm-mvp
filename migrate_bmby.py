"""
מיגרציה מ-BMBY (CRM נדל"ן קודם) למערכת - אנשי קשר/לידים, סטטוסים, משימות
פתוחות והערות/פעילויות היסטוריות. בטוח, אידמפוטנטי, בלי כפילויות.

שימוש (ברירת המחדל היא DRY-RUN - קורא, מאמת, מדווח, **לא משנה כלום**):

    venv/Scripts/python.exe migrate_bmby.py --input bmby_export/ --config bmby_mapping.json
    venv/Scripts/python.exe migrate_bmby.py --input bmby_export/ --config bmby_mapping.json --apply
        [--tenant default] [--report report.json] [--include-done-tasks] [--allow-remote-db]

⚠️ היעד נקבע לפי DATABASE_URL/DATA_DIR (כמו כל הפרויקט - ראו CLAUDE.md): עם
DATABASE_URL מה-.env זה Postgres בפרודקשן. --apply מול Postgres שאינו
localhost דורש --allow-remote-db מפורש. היעד מודפס בתחילת כל הרצה.

עקרונות (ראו CLAUDE.md, חוקים 4/5/6):
- כרטיסים נוצרים/מתעדכנים **רק** דרך extract.import_lead (+ extract.update_card
  פר-כרטיס לשדות-מטא שלא נתמכים ב-import_lead: מזהי BMBY, בלוק הערות, סטטוס).
  טלפון מנורמל ב-import_leads.normalize_il_phone ומאותר מול כרטיס קיים
  (05x מול +972) ב-extract.resolve_existing_phone - מעדכן, לא משכפל.
- קשרים לפי מזהי BMBY בלבד (contact_id/lead_id), לעולם לא לפי שם.
- פעילויות/הערות -> סוף history (append-only) עם חותמת הזמן המקורית ו-import_ref
  יציב (extract.append_imported_history מדלג על import_ref קיים). לא נכתבות
  לטבלת messages - הן לא הודעות WhatsApp.
- משימות -> db.create_task עם external_ref יציב (db.find_task_by_external_ref
  מדלג על קיימת). ברירת מחדל: רק משימות פתוחות.
- status_changed_at **לא** נקבע לליד מיובא (ראו _apply_card_metadata) - כך
  שלידים "contacted" מיובאים לא נכנסים לטיימר הפולו-אפ האוטומטי עד שנציג
  יעדכן אותם בפועל.
- לפני --apply: גיבוי כל הכרטיסים (extract.load_customers) ל-
  DATA_DIR/customers.backup_bmby_<UTC>.json (open "x" - לא דורס גיבוי קיים).
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import db
import extract
from extract import VALID_LEAD_STATUSES
from import_leads import _clean_excel_cell, normalize_il_phone, parse_csv, parse_excel
from paths import DATA_DIR

ENTITIES = ("contacts", "leads", "tasks", "activities")
VALID_TASK_STATUSES = {"pending", "done", "cancelled"}
VALID_TASK_TYPES = {"followup", "meeting"}
# שדות כרטיס שעוברים ל-extract.import_lead (notes/lead_status מטופלים בנפרד - ראו _apply_card_metadata)
CARD_FIELDS = (
    "customer_name", "business_name", "location", "category", "agent", "email", "address",
    "property_type", "budget", "requirements", "rooms", "project",
)
# שדות שהליד העדכני ביותר גובר בהם על איש הקשר (ליד = פנייה ספציפית לפרויקט)
LEAD_PRIORITY_FIELDS = ("project", "agent", "budget", "property_type", "rooms", "requirements", "location", "category")
NOTES_BLOCK_START = "--- ייבוא מבמבי ---"
NOTES_BLOCK_END = "--- סוף ייבוא מבמבי ---"
_NOTES_BLOCK_RE = re.compile(re.escape(NOTES_BLOCK_START) + r".*?" + re.escape(NOTES_BLOCK_END), re.S)
_PHONE_SPLIT_RE = re.compile(r"[,;/|\n]+")


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


def mask_phone(phone: str | None) -> str:
    p = str(phone or "")
    if len(p) <= 6:
        return "***"
    return p[:6] + "*" * (len(p) - 8) + p[-2:]


def mask_key(tenant: str, phone: str) -> str:
    return f"{tenant}::{mask_phone(phone)}"


def stable_hash(*parts) -> str:
    return hashlib.sha1(json.dumps([to_text(p) for p in parts], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def is_valid_phone(e164: str) -> bool:
    if not re.fullmatch(r"\+\d{8,15}", e164 or ""):
        return False
    if e164.startswith("+972"):
        return len(e164) - 4 in (8, 9)  # קווי (8) / נייד (9) אחרי 972
    return len(e164) >= 11


def local_il_format(e164: str) -> str | None:
    return "0" + e164[4:] if e164.startswith("+972") else None


# ---------------------------------------------------------------- config

class ConfigError(Exception):
    pass


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
    cfg.setdefault("date_formats", ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"])
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
            e164 = normalize_il_phone(part)
            if not is_valid_phone(e164):
                invalid.append(part)
                continue
            if e164 not in seen:
                seen.add(e164)
                valid.append((part, e164))
    valid.sort(key=lambda pe: 0 if pe[1].startswith("+9725") else 1)  # stable: נייד לפני קווי
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
        self.history: list = []
        self.contact_ids: set = set()
        self.lead_ids: set = set()


class Resolver:
    """מאתר את הכרטיס של מספר/מזהה BMBY: קודם מזהה BMBY שכבר נשמר בכרטיס קיים
    (הרצה חוזרת, גם אם הטלפון שונה בינתיים), אחרת extract.resolve_existing_phone
    על הפורמט הגולמי ועל הפורמט המקומי (05x) - כך שכרטיס ישן ב-05x או ב-+972
    מתעדכן ולא משוכפל."""

    def __init__(self, tenant: str, snapshot: dict):
        self.tenant = tenant
        self.snapshot = snapshot
        self.by_contact_id, self.by_lead_id = {}, {}
        for key, card in snapshot.items():
            t, _, phone = key.partition("::")
            if t != tenant:
                continue
            for cid in set(card.get("bmby_contact_ids") or []) | ({card["bmby_contact_id"]} if card.get("bmby_contact_id") else set()):
                self.by_contact_id.setdefault(str(cid), phone)
            for lid in card.get("bmby_lead_ids") or []:
                self.by_lead_id.setdefault(str(lid), phone)
        self._cache = {}

    def exists(self, phone: str) -> bool:
        return f"{self.tenant}::{phone}" in self.snapshot

    def resolve(self, raw: str, e164: str) -> str:
        if e164 in self._cache:
            return self._cache[e164]
        result = e164
        for candidate in (raw, local_il_format(e164), e164):
            if not candidate:
                continue
            found = extract.resolve_existing_phone(candidate, tenant_id=self.tenant)
            if self.exists(found):
                result = found
                break
        self._cache[e164] = result
        return result


def build_plans(contacts, leads, resolver: Resolver, report) -> tuple[dict, dict, dict]:
    plans: dict[str, CardPlan] = {}
    contact_plan: dict[str, CardPlan] = {}
    lead_plan: dict[str, CardPlan] = {}
    contacts_by_id = {c["id"]: c for c in contacts if c["id"]}
    stats_c, stats_l = report["contacts"], report["leads"]

    def plan_for(phones, bmby_contact_id=None, bmby_lead_id=None) -> CardPlan:
        phone = None
        if bmby_contact_id and bmby_contact_id in resolver.by_contact_id:
            phone = resolver.by_contact_id[bmby_contact_id]
        elif bmby_lead_id and bmby_lead_id in resolver.by_lead_id:
            phone = resolver.by_lead_id[bmby_lead_id]
        if phone is None:
            raw, e164 = phones[0]
            phone = resolver.resolve(raw, e164)
        if phone not in plans:
            plans[phone] = CardPlan(phone, resolver.exists(phone))
        plan = plans[phone]
        for raw, e164 in phones:
            if e164 != extract._to_e164(plan.phone) and e164 not in plan.extra_phones:
                plan.extra_phones.append(e164)
        return plan

    def attach_contact(c, plan):
        if c in plan.contacts:
            return
        if plan.contacts:
            stats_c["duplicates_merged"] += 1
            report["merged_contacts"].append({"card": mask_key(resolver.tenant, plan.phone),
                                              "bmby_contact_ids": sorted({x["id"] or f"row{x['row']}" for x in plan.contacts + [c]})})
        plan.contacts.append(c)
        if c["id"]:
            plan.contact_ids.add(c["id"])
            contact_plan[c["id"]] = plan

    for c in contacts:
        stats_c["read"] += 1
        if c["phones"]:
            attach_contact(c, plan_for(c["phones"], bmby_contact_id=c["id"]))
        elif c["id"] and c["id"] in resolver.by_contact_id:
            attach_contact(c, plan_for([], bmby_contact_id=c["id"]))

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
            if lead["phones"]:
                for raw, e164 in lead["phones"]:
                    if e164 != extract._to_e164(plan.phone) and e164 not in plan.extra_phones:
                        plan.extra_phones.append(e164)
        elif lead["phones"] or (lead["id"] and lead["id"] in resolver.by_lead_id) or (cid and cid in resolver.by_contact_id):
            plan = plan_for(lead["phones"], bmby_contact_id=cid, bmby_lead_id=lead["id"])
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
        if not any(c in p.contacts for p in plans.values()):
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


def _merge_notes(existing: str | None, block: str | None) -> str | None:
    existing = existing or ""
    if block is None:
        return existing or None
    if _NOTES_BLOCK_RE.search(existing):
        return _NOTES_BLOCK_RE.sub(lambda _m: block, existing, count=1)
    return (existing.rstrip() + "\n\n" + block) if existing.strip() else block


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


def _apply_card_metadata(tenant: str, phone: str, comp: dict, cfg, outcome: dict):
    """update_card פר-כרטיס (אטומי) לשדות ש-import_lead לא מטפל בהם נכון:

    - lead_status: נכתב רק אם לכרטיס אין סטטוס (status_policy=fill_missing) או
      ב-overwrite. **status_changed_at לא נקבע** (וכשמחליפים סטטוס קיים - מוסר):
      scheduler._process_followup_lead מתחיל טיימר פולו-אפ רק לכרטיס עם
      status_changed_at. תאריך עדכון ישן מבמבי היה גורם לשלב 1 (24ש') להיות
      "באיחור" כבר ברגע הייבוא, ו-"now" היה מתחיל פולו-אפ ללקוחות במבי תוך 24ש'.
      התאריך המקורי נשמר ב-bmby_status_updated_at (מידע בלבד).
    - notes: בלוק "ייבוא מבמבי" מוחלף במקום (הרצה חוזרת) או מתווסף - ההערות
      הקיימות בכרטיס נשמרות (import_lead היה דורס את כל השדה).
    - bmby_contact_id/bmby_contact_ids/bmby_lead_ids/bmby_status - לאידמפוטנטיות ולביקורת."""

    def _mutate(card: dict, _is_new: bool) -> None:
        status = comp["status"]
        if status:
            cur = card.get("lead_status")
            if cur == status:
                outcome["status"] = "unchanged"
            elif cur is None or cfg["status_policy"] == "overwrite":
                card["lead_status"] = status
                card.pop("status_changed_at", None)
                outcome["status"] = "set"
            else:
                outcome["status"] = "kept_existing"
        merged = _merge_notes(card.get("notes"), comp["notes_block"])
        if merged:
            card["notes"] = merged
        if comp["contact_ids"]:
            ids = sorted(set(card.get("bmby_contact_ids") or []) | set(comp["contact_ids"]))
            card["bmby_contact_ids"] = ids
            card.setdefault("bmby_contact_id", comp["contact_ids"][0])
        if comp["lead_ids"]:
            card["bmby_lead_ids"] = sorted(set(card.get("bmby_lead_ids") or []) | set(comp["lead_ids"]))
        if comp["status_raw"]:
            card["bmby_status"] = comp["status_raw"]
        if comp["status_updated_at"]:
            card["bmby_status_updated_at"] = comp["status_updated_at"]
        card.setdefault("bmby_migrated_at", datetime.now(timezone.utc).isoformat())

    extract.update_card(f"{tenant}::{phone}", _mutate)


# ---------------------------------------------------------------- tasks / activities

def link_plan(table: Table, row: dict, contact_plan, lead_plan, resolver: Resolver, plans) -> tuple[str | None, str]:
    """(טלפון הכרטיס או None, סיבה). lead_id קודם, אחר כך contact_id; גם מזהים מהרצה קודמת."""
    lid, cid = table.text(row, "lead_id"), table.text(row, "contact_id")
    if lid and lid in lead_plan:
        return lead_plan[lid].phone, ""
    if cid and cid in contact_plan:
        return contact_plan[cid].phone, ""
    if lid and lid in resolver.by_lead_id:
        return resolver.by_lead_id[lid], ""
    if cid and cid in resolver.by_contact_id:
        return resolver.by_contact_id[cid], ""
    if not lid and not cid:
        return None, "אין lead_id/contact_id"
    return None, f"לא מקושר לכרטיס (lead_id={lid or '-'}, contact_id={cid or '-'} - חסר/בלי טלפון)"


def plan_tasks(table: Table | None, cfg, args, contact_plan, lead_plan, resolver, plans, report, today_local: date) -> list[dict]:
    out, seen_refs = [], set()
    if table is None:
        return out
    st = report["tasks"]
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
        phone, reason = link_plan(table, row, contact_plan, lead_plan, resolver, plans)
        if phone is None:
            skip(reason, "skipped_unlinked")
            continue
        task_id = table.text(row, "task_id")
        title = table.text(row, "title")
        desc = table.text(row, "description")
        type_raw = table.text(row, "type")
        due_raw = table.raw(row, "due_date")
        ref = f"bmby:task:{task_id}" if task_id else "bmby:task:h:" + stable_hash(
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
        out.append({
            "row": rownum, "phone": phone, "title": title or type_raw or (desc[:60] if desc else "משימה מבמבי"),
            "due_date": due_date, "due_time": due_time, "notes": "\n".join(note_lines),
            "status": status, "task_type": task_type, "external_ref": ref,
            "past_due": due_date < today_local.isoformat(),
        })
    return out


def plan_activities(table: Table | None, cfg, contact_plan, lead_plan, resolver, plans, report) -> dict:
    by_phone: dict[str, list] = {}
    if table is None:
        return by_phone
    st = report["history"]
    seen = set()
    for idx, row in enumerate(table.rows):
        rownum = idx + 2
        st["read"] += 1

        def skip(reason, counter="skipped_invalid"):
            st[counter] += 1
            report["skipped_rows"].append({"file": "activities", "row": rownum, "reason": reason})

        phone, reason = link_plan(table, row, contact_plan, lead_plan, resolver, plans)
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
        message = cfg["history_message_prefix"] + (f"{type_raw}: " if type_raw else "") + body
        agent = table.text(row, "agent")
        if agent:
            message += f" (נציג: {agent})"
        entry = {
            "timestamp": to_utc_iso(when[0]), "channel": "note", "direction": "out", "message": message,
            "imported_from": "bmby", "import_ref": ref,
        }
        if type_raw:
            entry["bmby_type"] = type_raw
        by_phone.setdefault(phone, []).append(entry)
    for entries in by_phone.values():
        entries.sort(key=lambda e: e["timestamp"])
    return by_phone


# ---------------------------------------------------------------- main

def describe_target() -> tuple[str, bool]:
    """(תיאור היעד, האם מרוחק)."""
    if db.IS_POSTGRES:
        host = urlparse(db.DATABASE_URL).hostname or "?"
        return f"Postgres @ {host} (טבלת customers)", host not in ("localhost", "127.0.0.1", "::1")
    return f"JSON מקומי: {extract.CUSTOMERS_FILE}", False


def write_backup(stamp: str) -> Path:
    path = DATA_DIR / f"customers.backup_bmby_{stamp}.json"
    with open(path, "x", encoding="utf-8") as fh:  # "x" - לעולם לא דורס גיבוי קיים
        json.dump(extract.load_customers(), fh, ensure_ascii=False, indent=2)
    return path


def new_report(args, target) -> dict:
    return {
        "mode": "apply" if args.apply else "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": args.tenant, "target": target, "input_dir": str(args.input), "backup": None,
        "files": {},
        "contacts": {"read": 0, "with_card": 0, "skipped": 0, "duplicates_merged": 0},
        "leads": {"read": 0, "linked": 0, "skipped": 0},
        "cards": {"planned": 0, "created": 0, "updated": 0, "errors": 0,
                  "status_set": 0, "status_unchanged": 0, "status_kept_existing": 0, "status_none": 0},
        "tasks": {"read": 0, "created": 0, "skipped_existing": 0, "skipped_not_open": 0, "skipped_unlinked": 0,
                  "skipped_unmapped_status": 0, "skipped_invalid": 0, "skipped_duplicate_in_file": 0,
                  "past_due_open": 0, "meetings": 0, "errors": 0},
        "history": {"read": 0, "appended": 0, "skipped_duplicate": 0, "skipped_unlinked": 0,
                    "skipped_invalid": 0, "skipped_duplicate_in_file": 0, "errors": 0},
        "unmapped_lead_statuses": {}, "unmapped_task_statuses": {}, "unmapped_task_types": {},
        "multi_lead_cards": [], "merged_contacts": [], "field_conflicts": [],
        "skipped_rows": [], "errors": [], "warnings": [],
        "notes_for_dashboard": (
            "הערות/פעילויות מבמבי נכתבות ל-history של הכרטיס (channel='note'), לא לטבלת messages. "
            "חלון הצ'אט בדשבורד (GET /api/messages) קורא רק מטבלת messages - ולכן הן לא יופיעו בו; "
            "הן כן חלק מהכרטיס (וגם מ-last_contact_at ומהקשר ה-AI ב-_format_history_for_reply)."
        ),
    }


def run(args) -> dict:
    cfg = load_config(args.config)
    target, remote = describe_target()
    report = new_report(args, target)
    print(f"מצב: {'APPLY (כתיבה אמיתית)' if args.apply else 'DRY-RUN (שום דבר לא משתנה)'} | tenant={args.tenant}")
    print(f"יעד: {target}")
    if args.apply and remote and not args.allow_remote_db:
        raise ConfigError("--apply מול Postgres מרוחק (לא localhost) דורש --allow-remote-db מפורש")

    tables = load_tables(Path(args.input), cfg, report)
    snapshot = extract.load_customers()
    resolver = Resolver(args.tenant, snapshot)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_local = datetime.now(cfg["_tz"]).date()

    contacts, leads = read_records(tables, cfg, report)
    plans, contact_plan, lead_plan = build_plans(contacts, leads, resolver, report)
    tasks = plan_tasks(tables.get("tasks"), cfg, args, contact_plan, lead_plan, resolver, plans, report, today_local)
    activities = plan_activities(tables.get("activities"), cfg, contact_plan, lead_plan, resolver, plans, report)

    comps = {phone: compose_plan(plan, cfg, today, report) for phone, plan in plans.items()}
    report["cards"]["planned"] = len(plans)

    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report["backup"] = str(write_backup(stamp))
        print(f"גיבוי כל הכרטיסים: {report['backup']}")

    tenant = args.tenant
    for phone, plan in plans.items():
        comp = comps[phone]
        existing = snapshot.get(f"{tenant}::{phone}")
        fields = fields_for_import(existing, comp, cfg, report, tenant, phone)
        if not args.apply:
            report["cards"]["updated" if plan.exists else "created"] += 1
            cur = (existing or {}).get("lead_status")
            if not comp["status"]:
                report["cards"]["status_none"] += 1
            elif cur == comp["status"]:
                report["cards"]["status_unchanged"] += 1
            elif cur is None or cfg["status_policy"] == "overwrite":
                report["cards"]["status_set"] += 1
            else:
                report["cards"]["status_kept_existing"] += 1
            continue
        try:
            _card, is_new = extract.import_lead(phone, tenant_id=tenant, **fields)
            report["cards"]["created" if is_new else "updated"] += 1
            outcome = {"status": None}
            _apply_card_metadata(tenant, phone, comp, cfg, outcome)
            report["cards"][{"set": "status_set", "unchanged": "status_unchanged",
                             "kept_existing": "status_kept_existing", None: "status_none"}[outcome["status"]]] += 1
        except Exception as exc:  # כרטיס אחד בעייתי לא עוצר את השאר - מדווח
            report["cards"]["errors"] += 1
            report["errors"].append({"card": mask_key(tenant, phone), "stage": "card", "error": repr(exc)})

    for phone, entries in activities.items():
        if not args.apply:
            existing_refs = {h.get("import_ref") for h in (snapshot.get(f"{tenant}::{phone}") or {}).get("history") or []}
            dup = sum(1 for e in entries if e["import_ref"] in existing_refs)
            report["history"]["skipped_duplicate"] += dup
            report["history"]["appended"] += len(entries) - dup
            continue
        try:
            appended, skipped = extract.append_imported_history(phone, entries, tenant_id=tenant)
            report["history"]["appended"] += appended
            report["history"]["skipped_duplicate"] += skipped
        except Exception as exc:
            report["history"]["errors"] += len(entries)
            report["errors"].append({"card": mask_key(tenant, phone), "stage": "history", "error": repr(exc)})

    for task in tasks:
        if db.find_task_by_external_ref(tenant, task["external_ref"]):
            report["tasks"]["skipped_existing"] += 1
            continue
        report["tasks"]["past_due_open"] += int(task["past_due"] and task["status"] == "pending")
        report["tasks"]["meetings"] += int(task["task_type"] == "meeting")
        if not args.apply:
            report["tasks"]["created"] += 1
            continue
        try:
            db.create_task(
                phone=task["phone"], tenant_id=tenant, title=task["title"], due_date=task["due_date"],
                due_time=task["due_time"], notes=task["notes"], status=task["status"], source="bmby_import",
                task_type=task["task_type"], external_ref=task["external_ref"],
            )
            report["tasks"]["created"] += 1
        except Exception as exc:
            report["tasks"]["errors"] += 1
            report["errors"].append({"row": task["row"], "stage": "task", "error": repr(exc)})

    if report["tasks"]["meetings"]:
        report["warnings"].append(
            f"{report['tasks']['meetings']} משימות מסוג 'meeting' - אם SCHEDULER_AUTO_MEETING_REMINDER/INVITE "
            "יודלקו, הן יכולות לשלוח תזכורות/זימונים ללקוחות. לבדוק לפני הדלקה.")
    if report["unmapped_lead_statuses"]:
        report["warnings"].append("יש סטטוסי במבי לא ממופים - הסטטוס בכרטיס לא שונה, הטקסט המקורי נשמר ב-notes/bmby_status.")
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
    c, l, cards, t, h = report["contacts"], report["leads"], report["cards"], report["tasks"], report["history"]
    print(f"\n=== {pre}סיכום ===")
    print(f"  אנשי קשר: נקראו {c['read']}, שויכו לכרטיס {c['with_card']}, דולגו {c['skipped']}, כפולים שמוזגו {c['duplicates_merged']}")
    print(f"  לידים: נקראו {l['read']}, שויכו {l['linked']}, דולגו {l['skipped']}, כרטיסים עם כמה לידים {len(report['multi_lead_cards'])}")
    print(f"  כרטיסים: {cards['planned']} | נוצרו {cards['created']}, עודכנו {cards['updated']}, שגיאות {cards['errors']}")
    print(f"    סטטוס: נקבע {cards['status_set']}, ללא שינוי {cards['status_unchanged']}, "
          f"נשמר קיים {cards['status_kept_existing']}, ללא סטטוס/לא ממופה {cards['status_none']}")
    print(f"    קונפליקטים בשדות (נשמר הערך הקיים): {len(report['field_conflicts'])}")
    print(f"  משימות: נקראו {t['read']}, נוצרו {t['created']}, קיימות כבר {t['skipped_existing']}, לא פתוחות {t['skipped_not_open']}, "
          f"לא מקושרות {t['skipped_unlinked']}, סטטוס לא ממופה {t['skipped_unmapped_status']}, לא תקינות {t['skipped_invalid']}, "
          f"כפולות בקובץ {t['skipped_duplicate_in_file']}; פתוחות-באיחור {t['past_due_open']}, פגישות {t['meetings']}")
    print(f"  היסטוריה: נקראו {h['read']}, נוספו {h['appended']}, כבר קיימות {h['skipped_duplicate']}, לא מקושרות {h['skipped_unlinked']}, "
          f"לא תקינות {h['skipped_invalid']}, כפולות בקובץ {h['skipped_duplicate_in_file']}")
    for title, key in (("סטטוסי ליד לא ממופים", "unmapped_lead_statuses"), ("סטטוסי משימה לא ממופים", "unmapped_task_statuses"),
                       ("סוגי משימה לא ממופים", "unmapped_task_types")):
        if report[key]:
            print(f"  {title}: " + ", ".join(f"'{k}' x{v}" for k, v in report[key].items()))
    for m in report["multi_lead_cards"]:
        print(f"  כמה לידים לכרטיס {m['card']}: נשמר #{m['kept_lead_id']}, נוספים {m['other_lead_ids']} (פרויקטים {m['projects']})")
    if report["skipped_rows"]:
        print(f"\n=== שורות שדולגו ({len(report['skipped_rows'])}) ===")
        for s in report["skipped_rows"][:100]:
            print(f"  {s['file']} שורה {s['row']}: {s['reason']}")
        if len(report["skipped_rows"]) > 100:
            print("  ... (הרשימה המלאה בדוח ה-JSON)")
    for w in report["warnings"][:50]:
        print(f"  ⚠ {w}")
    for e in report["errors"]:
        print(f"  ✗ שגיאה: {e}")
    if report["backup"]:
        print(f"\nגיבוי: {report['backup']}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="מיגרציה מ-BMBY (dry-run כברירת מחדל)")
    ap.add_argument("--input", required=True, help="תיקייה עם קבצי הייצוא (CSV/XLSX)")
    ap.add_argument("--config", required=True, help="קובץ מיפוי (ראו bmby_mapping.example.json)")
    ap.add_argument("--tenant", default=extract.DEFAULT_TENANT_ID)
    ap.add_argument("--apply", action="store_true", help="לבצע בפועל (ברירת מחדל: dry-run)")
    ap.add_argument("--report", help="נתיב לדוח JSON (ברירת מחדל: DATA_DIR/bmby_migration_report_<mode>_<UTC>.json)")
    ap.add_argument("--include-done-tasks", action="store_true", help="לייבא גם משימות שבוצעו/בוטלו")
    ap.add_argument("--allow-remote-db", action="store_true", help="לאפשר --apply מול Postgres שאינו localhost")
    args = ap.parse_args(argv)
    if not Path(args.input).is_dir():
        print(f"שגיאה: --input אינו תיקייה: {args.input}", file=sys.stderr)
        return 2
    try:
        report = run(args)
    except (ConfigError, ValueError, json.JSONDecodeError) as exc:
        print(f"שגיאה: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    report_path = Path(args.report) if args.report else DATA_DIR / (
        f"bmby_migration_report_{report['mode']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nדוח JSON: {report_path}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
