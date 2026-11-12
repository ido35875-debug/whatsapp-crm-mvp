"""
ייצוא חד-פעמי (קריאה בלבד) של כרטיסי הלידים מ-Postgres של הפרודקשן (Render) לתיקייה מקומית נפרדת.

- מתחבר עם DATABASE_URL מ-.env (או --url), בטרנזקציה READ ONLY - לא כותב כלום לפרודקשן.
- קורא את טבלת customers (cust_key, data) - אותו מבנה בדיוק כמו customers.json המקומי.
- שומר ל-data_realestate/realestate_customers.json + עותק customers.json (השרת קורא רק את השם הזה מ-DATA_DIR).
- --tenant מסנן ל-tenant_id מסוים (ברירת מחדל: כל השורות).

הרצה:
    venv\\Scripts\\python.exe export_realestate_from_render.py [--tenant TENANT] [--out data_realestate]
ואז:
    DATA_DIR=data_realestate DATABASE_URL= DRY_RUN=true venv\\Scripts\\python.exe server.py
⚠️ בלי DRY_RUN=true הדשבורד המקומי שולח הודעות WhatsApp אמיתיות ללקוחות אמיתיים דרך ה-Provider שב-.env.
"""

import argparse
import collections
import json
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).parent


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="DATABASE_URL מפורש (ברירת מחדל: מ-.env)")
    parser.add_argument("--tenant", help="לייצא רק tenant_id זה")
    parser.add_argument("--out", default="data_realestate")
    args = parser.parse_args()

    url = (args.url or dotenv_values(PROJECT_DIR / ".env").get("DATABASE_URL") or "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        print("DATABASE_URL של Postgres לא נמצא (.env / --url).", file=sys.stderr)
        return 1

    conn = psycopg2.connect(url, sslmode="require", connect_timeout=20)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SELECT cust_key, data FROM customers")
            rows = cur.fetchall()
    finally:
        conn.close()

    customers = {key: json.loads(data) for key, data in rows}
    by_tenant = collections.Counter(c.get("tenant_id") for c in customers.values())
    print(f"נשלפו {len(customers)} כרטיסים מהפרודקשן. לפי tenant: {dict(by_tenant)}")

    if args.tenant:
        customers = {k: c for k, c in customers.items() if c.get("tenant_id") == args.tenant}
        print(f"אחרי סינון tenant={args.tenant}: {len(customers)} כרטיסים")

    out_dir = (PROJECT_DIR / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(customers, ensure_ascii=False, indent=2)
    for name in ("realestate_customers.json", "customers.json"):
        (out_dir / name).write_text(payload, encoding="utf-8")
    print(f"נשמר ל-{out_dir} (realestate_customers.json + customers.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
