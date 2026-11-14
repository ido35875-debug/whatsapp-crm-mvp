"""
create_user.py - יוצר משתמש RBAC (admin/partner/client) עם מפתח API אישי, בלי
דשבורד. נועד בעיקר ליצירת האדמין הראשון: מאז 2026-11-14 הדשבורד כבר לא מקבל את
API_SECRET_KEY מוזרק מהשרת, ונכנסים אליו רק עם מפתח אישי (מסך התחברות).

משתמש באותה db.create_user בדיוק כמו POST /api/admin/users (ראו server.py) -
והמפתח נוצר באותה דרך (secrets.token_urlsafe(24)).

לאיזה DB זה כותב: לפי DATABASE_URL בסביבה (בדיוק כמו db.py) -
  - DATABASE_URL ריק  → SQLite מקומי ב-DATA_DIR/crm_data.db
  - DATABASE_URL מוגדר → Postgres (למשל מתוך Render Shell של השירות בפרודקשן)
⚠️ הסקריפט **לא** טוען את .env בעצמו - כדי שהרצה מקומית לא תכתוב בטעות ל-
Postgres של הפרודקשן. ליצירת משתמש בפרודקשן: להריץ ב-Render Shell (שם משתני
הסביבה כבר מוגדרים), או להשתמש ב-curl עם API_SECRET_KEY (ראו למטה).

דוגמאות:
  python create_user.py --user-id ido --name "עידו" --role admin
  python create_user.py --user-id agent1 --name "נציג" --role client --tenant-ids default
  python create_user.py --list

חלופה בלי shell (מפתח האדמין הראשי עובד ב-curl):
  curl -X POST https://<app>.onrender.com/api/admin/users \\
       -H "X-API-Key: <API_SECRET_KEY>" -H "Content-Type: application/json" \\
       -d '{"user_id":"ido","name":"עידו","role":"admin"}'
"""
from __future__ import annotations

import argparse
import secrets
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="יצירת משתמש RBAC עם מפתח API אישי")
    parser.add_argument("--user-id", help="מזהה ייחודי (באנגלית, למשל ido)")
    parser.add_argument("--name", help="שם לתצוגה")
    parser.add_argument("--role", choices=["admin", "partner", "client"], default="admin")
    parser.add_argument("--tenant-ids", default="", help="מופרד בפסיקים - חובה ל-partner/client")
    parser.add_argument("--list", action="store_true", help="הצג משתמשים קיימים (בלי מפתחות)")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # עברית בקונסולת Windows
    except Exception:
        pass

    import db  # אחרי argparse - כדי ש---help לא יפתח חיבור DB

    backend = "Postgres (DATABASE_URL)" if db.IS_POSTGRES else "SQLite מקומי (DATA_DIR/crm_data.db)"
    print(f"DB: {backend}")

    if args.list:
        for u in db.get_users():
            print(f"- {u.get('user_id')} | {u.get('name')} | {u.get('role')} | tenants={u.get('tenant_ids')}")
        return 0

    if not args.user_id or not args.name:
        parser.error("--user-id ו---name נדרשים (או --list)")
    tenant_ids = [t.strip() for t in args.tenant_ids.split(",") if t.strip()]
    if args.role in ("partner", "client") and not tenant_ids:
        parser.error("partner/client חייבים --tenant-ids (לפחות אחד) - אחרת לא יראו שום ליד")

    api_key = secrets.token_urlsafe(24)
    try:
        db.create_user(api_key, args.user_id.strip(), args.name.strip(), args.role, tenant_ids=tenant_ids)
    except db.IntegrityError:
        print(f"שגיאה: משתמש עם user_id '{args.user_id}' כבר קיים", file=sys.stderr)
        return 1

    print(f"נוצר משתמש '{args.user_id}' (role={args.role}).")
    print("מפתח API אישי (להדביק במסך ההתחברות של הדשבורד; שמרו אותו במקום בטוח):")
    print(api_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
