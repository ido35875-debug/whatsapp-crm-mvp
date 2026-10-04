"""
סנכרון שיחות קיימות מ-Green API (getChats) - בלי מענה, בלי שליחה.

--target local  (ברירת מחדל): כותב ל-customers.json המקומי, אחרי גיבוי אוטומטי.
--target render: שולח את הלידים ל-Production דרך POST /api/leads של Render.
                 דורש RENDER_API_KEY (= API_SECRET_KEY של Render) ב-.env או במשתני סביבה.
                 RENDER_BASE_URL אופציונלי (ברירת מחדל: הכתובת של השירות ב-Render).

ברירת מחדל לכל מצב: dry-run - מציג מה ייובא, לא כותב כלום. --apply כותב בפועל.
לידים שכבר קיימים (ב-customers.json או ב-Render) נדלגים, כדי לא לדרוס שם קיים.
קבוצות (@g.us) לא נכללות.

שימוש:
  python sync_existing_chats.py                          # תצוגה מקדימה מקומית, 3 שיחות
  python sync_existing_chats.py --limit 3 --apply        # כתיבה מקומית
  python sync_existing_chats.py --target render          # תצוגה מקדימה ל-Render
  python sync_existing_chats.py --target render --limit 3 --apply
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

TENANT_ID = "default"
IMPORT_SOURCE = "green_api_sync"
DEFAULT_RENDER_URL = "https://whatsapp-crm-mvp.onrender.com"

# chatIds של השיחות שנבחרו ידנית - נשמרים ב-.env (SYNC_PINNED_CHAT_IDS) ולא בסורס, כי הם מספרי טלפון אישיים
PINNED_CHAT_IDS = [i.strip() for i in os.environ.get("SYNC_PINNED_CHAT_IDS", "").split(",") if i.strip()]


def fetch_private_chats(limit: int) -> list[dict]:
    base = os.environ["GREEN_API_URL"].strip()
    instance = os.environ["GREEN_API_INSTANCE_ID"].strip()
    token = os.environ["GREEN_API_TOKEN"].strip()
    resp = requests.get(f"{base}/waInstance{instance}/getChats/{token}", timeout=60)
    resp.raise_for_status()
    chats = resp.json()
    private = [c for c in chats if str(c.get("id", "")).endswith("@c.us")]
    return private[:limit]


def pinned_chats(chats: list[dict]) -> list[dict]:
    wanted = set(PINNED_CHAT_IDS)
    return [c for c in chats if c.get("id") in wanted]


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


def existing_phones_local() -> set[str]:
    import extract  # noqa: E402 - דורש ANTHROPIC_API_KEY בזמן import
    return {k.split("::", 1)[1] for k in extract.load_customers() if k.startswith(f"{TENANT_ID}::")}


def existing_phones_render(base_url: str, key: str) -> set[str]:
    resp = requests.get(f"{base_url}/api/leads", headers={"X-API-Key": key}, timeout=120)
    resp.raise_for_status()
    return {lead["phone"] for lead in resp.json() if lead.get("tenant_id", TENANT_ID) == TENANT_ID}


def write_local(to_import: list[tuple[str, str | None]]) -> None:
    import extract  # noqa: E402
    customers_file = extract.CUSTOMERS_FILE
    if customers_file.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = customers_file.with_name(f"customers.backup_sync_{stamp}.json")
        shutil.copy2(customers_file, backup)
        print(f"גיבוי נשמר: {backup.name}")
    for phone, name in to_import:
        extract.import_lead(phone, tenant_id=TENANT_ID, customer_name=name, import_source=IMPORT_SOURCE)


def write_render(to_import: list[tuple[str, str | None]], base_url: str, key: str) -> None:
    failed = []
    for phone, name in to_import:
        resp = requests.post(
            f"{base_url}/api/leads",
            json={"phone": phone, "tenant_id": TENANT_ID, "customer_name": name or ""},
            headers={"X-API-Key": key},
            timeout=60,
        )
        if resp.status_code != 200:
            failed.append((phone, f"HTTP {resp.status_code}"))
    if failed:
        for phone, reason in failed:
            print(f"  ✗ {mask(phone)}  {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ייבוא שיחות קיימות מ-Green API ללידים")
    parser.add_argument("--target", choices=["local", "render"], default="local")
    parser.add_argument("--limit", type=int, default=3, help="כמה שיחות פרטיות לייבא (ברירת מחדל 3)")
    parser.add_argument("--pinned", action="store_true", help="לייבא רק את PINNED_CHAT_IDS")
    parser.add_argument("--apply", action="store_true", help="לכתוב בפועל (ברירת מחדל: תצוגה מקדימה בלבד)")
    args = parser.parse_args()

    base_url, key = None, None
    if args.target == "render":
        key = os.environ.get("RENDER_API_KEY", "").strip()
        if not key:
            raise SystemExit("חסר RENDER_API_KEY - הוסף אותו ל-.env המקומי (ראו ההוראות).")
        base_url = os.environ.get("RENDER_BASE_URL", DEFAULT_RENDER_URL).strip().rstrip("/")

    if args.pinned:
        chats = pinned_chats(fetch_private_chats(10**6))
    else:
        chats = fetch_private_chats(args.limit)
    already = existing_phones_render(base_url, key) if args.target == "render" else existing_phones_local()

    to_import, skipped = [], []
    for chat in chats:
        phone = chat_to_phone(chat["id"])
        name = usable_name(chat.get("name"), phone)
        if phone in already:
            skipped.append((phone, "כבר קיים ביעד"))
        else:
            to_import.append((phone, name))

    mode = "כתיבה בפועל" if args.apply else "תצוגה מקדימה בלבד (dry-run)"
    print(f"יעד: {args.target} | מצב: {mode} | שיחות פרטיות: {len(chats)} | ייובאו: {len(to_import)} | ידולגו: {len(skipped)}")
    for phone, name in to_import:
        print(f"  + {mask(phone)}  שם={name or '(ללא שם)'}")
    for phone, reason in skipped:
        print(f"  - {mask(phone)}  {reason}")

    if not args.apply:
        print("\nלא נכתב דבר. להריץ עם --apply כדי לייבא בפועל.")
        return
    if not to_import:
        print("אין מה לייבא.")
        return

    if args.target == "render":
        write_render(to_import, base_url, key)
        print(f"נשלחו {len(to_import)} לידים ל-Render (ללא מענה, ללא שליחה).")
    else:
        write_local(to_import)
        print(f"יובאו {len(to_import)} לידים ל-customers.json המקומי (ללא מענה, ללא שליחה).")


if __name__ == "__main__":
    main()
