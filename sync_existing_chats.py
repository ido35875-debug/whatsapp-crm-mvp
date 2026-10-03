"""
סנכרון שיחות קיימות מ-Green API (getChats) אל customers.json - בלי מענה, בלי שליחה.

ברירת מחדל: dry-run - מציג מה ייובא, לא כותב כלום.
--apply: כותב בפועל, אחרי גיבוי אוטומטי של customers.json.
לידים שכבר קיימים ב-customers.json נדלגים (לא דורסים שם/נתונים קיימים).
קבוצות (@g.us) לא נכללות.

שימוש:
  python sync_existing_chats.py                  # תצוגה מקדימה של 3 שיחות
  python sync_existing_chats.py --limit 20       # תצוגה מקדימה של 20
  python sync_existing_chats.py --limit 3 --apply
"""

import argparse
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

import extract  # noqa: E402 - דורש ANTHROPIC_API_KEY בזמן import (ראו extract.py)

TENANT_ID = extract.DEFAULT_TENANT_ID
IMPORT_SOURCE = "green_api_sync"


def fetch_private_chats(limit: int) -> list[dict]:
    base = os.environ["GREEN_API_URL"].strip()
    instance = os.environ["GREEN_API_INSTANCE_ID"].strip()
    token = os.environ["GREEN_API_TOKEN"].strip()
    resp = requests.get(f"{base}/waInstance{instance}/getChats/{token}", timeout=60)
    resp.raise_for_status()
    chats = resp.json()
    private = [c for c in chats if str(c.get("id", "")).endswith("@c.us")]
    return private[:limit]


def chat_to_phone(chat_id: str) -> str:
    digits = chat_id.split("@")[0]
    return f"+{digits}" if digits.isdigit() else digits


def usable_name(name: str | None, phone: str) -> str | None:
    if not name:
        return None
    bare = name.strip().lstrip("+").replace(" ", "").replace("-", "")
    if bare.isdigit() or bare == phone.lstrip("+"):
        return None  # השם הוא מספר טלפון, לא שם אמיתי
    return name.strip()


def mask(phone: str) -> str:
    return f"{phone[:4]}…{phone[-4:]}" if len(phone) > 8 else phone


def main() -> None:
    parser = argparse.ArgumentParser(description="ייבוא שיחות קיימות מ-Green API ל-customers.json")
    parser.add_argument("--limit", type=int, default=3, help="כמה שיחות פרטיות לייבא (ברירת מחדל 3)")
    parser.add_argument("--apply", action="store_true", help="לכתוב בפועל (ברירת מחדל: תצוגה מקדימה בלבד)")
    args = parser.parse_args()

    chats = fetch_private_chats(args.limit)
    existing = extract.load_customers()

    to_import, skipped = [], []
    for chat in chats:
        phone = chat_to_phone(chat["id"])
        key = f"{TENANT_ID}::{phone}"
        name = usable_name(chat.get("name"), phone)
        if key in existing:
            skipped.append((phone, "כבר קיים ב-customers.json"))
        else:
            to_import.append((phone, name))

    mode = "כתיבה בפועל" if args.apply else "תצוגה מקדימה בלבד (dry-run)"
    print(f"מצב: {mode} | שיחות פרטיות שנמשכו: {len(chats)} | ייובאו: {len(to_import)} | ידולגו: {len(skipped)}")
    for phone, name in to_import:
        print(f"  + {mask(phone)}  שם={name or '(ללא שם)'}")
    for phone, reason in skipped:
        print(f"  - {mask(phone)}  {reason}")

    if not args.apply or not to_import:
        if not args.apply:
            print("\nלא נכתב דבר. להריץ עם --apply כדי לייבא בפועל.")
        return

    customers_file = extract.CUSTOMERS_FILE
    if customers_file.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = customers_file.with_name(f"customers.backup_sync_{stamp}.json")
        shutil.copy2(customers_file, backup)
        print(f"גיבוי נשמר: {backup.name}")

    for phone, name in to_import:
        extract.import_lead(
            phone, tenant_id=TENANT_ID, customer_name=name, import_source=IMPORT_SOURCE,
        )
    print(f"יובאו {len(to_import)} לידים ל-customers.json (ללא מענה, ללא שליחה).")


if __name__ == "__main__":
    main()
