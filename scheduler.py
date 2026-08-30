"""
Background Scheduler Worker: תהליך רקע (daemon thread) שרץ בתוך תהליך ה-Flask (server.py),
סורק תקופתית את הלידים הקרים ב-contacts.csv/customers.json, ומפעיל את לוגיקת ההחייאה
(reactivate.py) על מי שעבר את סף הזמן המתאים לוורטיקל שלו (איקומרס מול נדל"ן).

⚠️ החלטת בטיחות מכוונת - חשוב לקרוא לפני שמפעילים בסביבה אמיתית:
זהו תהליך אוטונומי לגמרי - הוא רץ בלי אישור אנושי בכל מחזור. זה מנוגד לעיקרון "אין
פנייה יזומה ללקוח בלי אישור אנושי מפורש" שמוגדר ב-CLAUDE.md עבור reactivate.py/--send
ו-/api/reactivate ("לא אוטומציה מתוזמנת"). לכן, בברירת המחדל, מנוע התזמון **תמיד
dry-run בלבד** (סורק, מייצר הודעות לדוגמה, רושם ללוג - אבל לא שולח בפועל). שליחה
אוטומטית אמיתית דורשת הגדרה מפורשת ומכוונת: SCHEDULER_AUTO_SEND=true ב-.env.
אם תדליקו את זה - המערכת תשלח הודעות WhatsApp אמיתיות אוטומטית, ללא אישור נקודתי
לכל הודעה/קמפיין, כל עוד השרת רץ. אל תדליקו את זה על חשבון production בלי הודעות-
תבנית מאושרות מול מטא (ראו ההערה המקבילה ב-reactivate.py ו-whatsapp_send.py).
"""

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # מפורש - לא תלוי בסדר import של מודולים אחרים

import db
import reactivate
from extract import DEFAULT_TENANT_ID, _customer_key, last_contact_at, load_customers
from paths import DATA_DIR

CHAT_HISTORY_FILE = DATA_DIR / "chat_history.txt"

SCAN_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "3600"))
AUTO_SEND = os.environ.get("SCHEDULER_AUTO_SEND", "false").strip().lower() == "true"

# ורטיקל -> כמה ימים בלי שום קשר (נכנס/יוצא) בהיסטוריית הכרטיס, לפני שליד קר נחשב
# "בשל" להחייאה חוזרת. איקומרס: מחזור מכירה קצר - חימום מהיר. נדל"ן: מחזור מכירה
# ארוך יותר, ליד צריך יותר זמן להבשיל לפני פנייה חוזרת.
VERTICAL_COLD_THRESHOLDS_DAYS = {
    "ecommerce": 3,
    "real_estate": 14,
}
DEFAULT_VERTICAL = "ecommerce"

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_run_at: str | None = None


def _log(line: str) -> None:
    """רושם שורת לוג של מנוע התזמון גם ל-chat_history.txt וגם למסך (stdout)."""
    timestamp = datetime.now(timezone.utc).isoformat()
    with CHAT_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [scheduler] {line}\n")
    print(f"[scheduler] {line}")


def _is_due(contact: dict, customers: dict, tenant_id: str, now: datetime) -> bool:
    """בודק אם ליד קר עבר את סף הזמן להחייאה, לפי הוורטיקל שלו (contact['vertical'])."""
    vertical = (contact.get("vertical") or DEFAULT_VERTICAL).strip().lower()
    threshold_days = VERTICAL_COLD_THRESHOLDS_DAYS.get(vertical, VERTICAL_COLD_THRESHOLDS_DAYS[DEFAULT_VERTICAL])

    card = customers.get(_customer_key(tenant_id, contact["phone"]))
    last_contact = last_contact_at(card)  # פונקציה משותפת ב-extract.py - ראו שם
    if last_contact is None:
        return True  # אין היסטוריה בכלל - בשל מיד

    days_since = (now - last_contact).days
    return days_since >= threshold_days


def run_scan(tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """מריץ מחזור סריקה אחד: מסנן לפי סטטוס (reactivate.get_cold_leads) וזמן+ורטיקל
    (_is_due), ואז מפעיל reactivate.run_reactivation_campaign רק על מי שבשל. רושם
    את התוצאה ל-chat_history.txt (דרך _log) ולטבלת scheduler_runs ב-DB. ניתן לקרוא
    לפונקציה הזו ישירות (למשל מ-/api/scheduler/run-now) בלי לחכות למחזור התזמון."""
    global _last_run_at
    now = datetime.now(timezone.utc)
    customers = load_customers()
    contacts = reactivate.load_contacts()

    cold_leads = reactivate.get_cold_leads(contacts, tenant_id=tenant_id)
    due_leads = [c for c in cold_leads if _is_due(c, customers, tenant_id, now)]
    _last_run_at = now.isoformat()

    if not due_leads:
        summary = f"{len(cold_leads)} לידים קרים נמצאו, אף אחד לא עבר עדיין את סף הזמן לוורטיקל שלו."
        _log(f"סריקה: {summary}")
        db.log_scheduler_run(len(cold_leads), 0, AUTO_SEND, summary, tenant_id=tenant_id)
        return []

    _log(f"סריקה: {len(due_leads)} מתוך {len(cold_leads)} לידים קרים עברו את סף הזמן - מפעיל reactivate (auto_send={AUTO_SEND}).")

    results = reactivate.run_reactivation_campaign(tenant_id=tenant_id, send=AUTO_SEND, contacts=due_leads)

    lines = []
    for r in results:
        if not AUTO_SEND:
            status = "תצוגה מקדימה בלבד (dry-run - לא נשלח)"
        elif r.get("sent"):
            status = f"נשלח בפועל (SID: {r.get('sid')})"
        else:
            status = f"נכשל: {r.get('error')}"
        line = f"  → {r['name']} ({r['phone']}): {status}"
        _log(line)
        lines.append(line)

    db.log_scheduler_run(len(cold_leads), len(due_leads), AUTO_SEND, "\n".join(lines), tenant_id=tenant_id)
    return results


def _loop() -> None:
    _log(
        f"מנוע התזמון הופעל. מרווח סריקה: {SCAN_INTERVAL_SECONDS} שניות. "
        f"auto_send={AUTO_SEND}" + ("" if AUTO_SEND else " (dry-run בלבד - לא שולח הודעות אמיתיות)") + "."
    )
    while not _stop_event.is_set():
        try:
            run_scan()
        except Exception as exc:
            _log(f"שגיאה במחזור סריקה: {exc}")
        _stop_event.wait(SCAN_INTERVAL_SECONDS)
    _log("מנוע התזמון נעצר.")


def start() -> None:
    """מפעיל את מנוע התזמון כ-daemon thread (לא חוסם את שרת ה-Flask, ונעצר אוטומטית
    כשהתהליך הראשי יוצא). לא עושה כלום אם הוא כבר רץ."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_loop, daemon=True, name="reactivation-scheduler")
    _scheduler_thread.start()


def stop() -> None:
    _stop_event.set()


def status() -> dict:
    return {
        "running": bool(_scheduler_thread and _scheduler_thread.is_alive()),
        "auto_send": AUTO_SEND,
        "interval_seconds": SCAN_INTERVAL_SECONDS,
        "last_run_at": _last_run_at,
    }
