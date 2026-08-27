"""
שכבת גישה לטבלת messages ב-crm_data.db (SQLite).

חשוב: זו שכבת רישום טכנית נוספת (לוג של הודעות נכנסות/יוצאות) - לא מקור האמת של
המערכת. מקור האמת ליישות "לקוח" נשאר customers.json (ראו extract.py, וה"עקרונות
משותפים" ב-CLAUDE.md). אין כאן כפילות לוגיקת עסקים - רק רישום.
"""

import sqlite3
from datetime import datetime, timezone

from paths import DATA_DIR

DB_FILE = DATA_DIR / "crm_data.db"

_MIGRATIONS = {
    "tenant_id": "ALTER TABLE messages ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    "channel": "ALTER TABLE messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'whatsapp'",
    "direction": "ALTER TABLE messages ADD COLUMN direction TEXT NOT NULL DEFAULT 'in'",
    "simulated": "ALTER TABLE messages ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0",
}


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            phone TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'whatsapp',
            direction TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # מיגרציה קלה: אם messages כבר קיימת מ-create_db.py הישן (בלי tenant_id/channel/direction) - מוסיפים את העמודות החסרות
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    for column, ddl in _MIGRATIONS.items():
        if column not in existing_columns:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            leads_scanned INTEGER NOT NULL DEFAULT 0,
            leads_due INTEGER NOT NULL DEFAULT 0,
            auto_send INTEGER NOT NULL DEFAULT 0,
            summary TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            phone TEXT NOT NULL,
            call_sid TEXT,
            status TEXT NOT NULL DEFAULT 'initiated',
            direction TEXT NOT NULL DEFAULT 'outbound',
            duration_seconds INTEGER,
            recording_url TEXT,
            notes TEXT,
            summary TEXT,
            simulated INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def log_message(
    phone: str,
    message: str,
    direction: str,
    tenant_id: str = "default",
    channel: str = "whatsapp",
    simulated: bool = False,
) -> None:
    """רושם הודעה נכנסת (direction='in') או יוצאת (direction='out') בטבלת messages.
    simulated=True מסמן הודעה שנרשמה אבל לא נשלחה בפועל בפועל (למשל כשחשבון Twilio
    מסוג Trial חוסם שליחה לנמען לא-מאומת) - ראו _is_trial_restriction ב-server.py."""
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO messages (tenant_id, phone, channel, direction, message, timestamp, simulated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, phone, channel, direction, message, datetime.now(timezone.utc).isoformat(), int(simulated)),
        )
        conn.commit()
    finally:
        conn.close()


def get_messages(phone: str, tenant_id: str = "default", since: str | None = None) -> list[dict]:
    """מחזיר את היסטוריית ההודעות (נכנסות/יוצאות) של ליד מסוים מטבלת messages, ממוינות
    לפי זמן. since (אופציונלי, ISO timestamp): מחזיר רק הודעות מאוחרות ממנו - לשימוש
    ה-polling של חלון הצ'אט החי בדשבורד, כדי לא לשלוף את כל ההיסטוריה בכל בדיקה."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        if since:
            rows = conn.execute(
                "SELECT channel, direction, message, timestamp, simulated FROM messages "
                "WHERE phone = ? AND tenant_id = ? AND timestamp > ? ORDER BY timestamp ASC",
                (phone, tenant_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT channel, direction, message, timestamp, simulated FROM messages "
                "WHERE phone = ? AND tenant_id = ? ORDER BY timestamp ASC",
                (phone, tenant_id),
            ).fetchall()
        return [dict(row) | {"simulated": bool(row["simulated"])} for row in rows]
    finally:
        conn.close()


def get_last_message(phone: str, tenant_id: str = "default") -> dict | None:
    """מחזיר את ההודעה האחרונה (נכנסת או יוצאת) של ליד, או None אם אין בכלל - לשימוש
    תצוגת ה-Unified Inbox (תצוגה מקדימה + מיון לפי פעילות אחרונה ברשימת השיחות)."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT channel, direction, message, timestamp, simulated FROM messages "
            "WHERE phone = ? AND tenant_id = ? ORDER BY timestamp DESC LIMIT 1",
            (phone, tenant_id),
        ).fetchone()
        return dict(row) | {"simulated": bool(row["simulated"])} if row else None
    finally:
        conn.close()


def log_scheduler_run(
    leads_scanned: int, leads_due: int, auto_send: bool, summary: str, tenant_id: str = "default"
) -> None:
    """רושם מחזור סריקה אחד של מנוע התזמון (scheduler.py) בטבלת scheduler_runs."""
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO scheduler_runs (tenant_id, timestamp, leads_scanned, leads_due, auto_send, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_id, datetime.now(timezone.utc).isoformat(), leads_scanned, leads_due, int(auto_send), summary),
        )
        conn.commit()
    finally:
        conn.close()


def get_scheduler_runs(limit: int = 20) -> list[dict]:
    """מחזיר את מחזורי הסריקה האחרונים (החדש ביותר קודם) - לשימוש הדשבורד/דיווח."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT tenant_id, timestamp, leads_scanned, leads_due, auto_send, summary "
            "FROM scheduler_runs ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) | {"auto_send": bool(row["auto_send"])} for row in rows]
    finally:
        conn.close()


def create_call(
    phone: str,
    tenant_id: str = "default",
    call_sid: str | None = None,
    status: str = "initiated",
    direction: str = "outbound",
    simulated: bool = False,
) -> int:
    """יוצר שורת שיחה חדשה בטבלת calls (Click-to-Call). מוחזר ה-id (INTEGER PRIMARY
    KEY) - זה מה שהדשבורד שולח חזרה ב-POST /api/calls/<id>/notes אחרי שהשיחה נגמרה."""
    conn = _get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO calls (tenant_id, phone, call_sid, status, direction, simulated, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, phone, call_sid, status, direction, int(simulated), now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_call_status(call_sid: str, status: str, duration_seconds: int | None = None) -> None:
    """מעדכן סטטוס שיחה לפי call_sid - קורא ל-POST /voice/status (status callback של
    הרגל הראשונה בגישור, הנציג, מ-Twilio)."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calls SET status = ?, duration_seconds = COALESCE(?, duration_seconds), "
            "updated_at = ? WHERE call_sid = ?",
            (status, duration_seconds, datetime.now(timezone.utc).isoformat(), call_sid),
        )
        conn.commit()
    finally:
        conn.close()


def save_recording_url(call_sid: str, recording_url: str) -> None:
    """שומר קישור להקלטת השיחה לפי call_sid - קורא ל-POST /voice/recording-status.
    לא מוריד/מתמלל את התוכן - רק שומר את ה-URL (הוחלט במפורש: תמלול אוטומטי מחוץ
    לסקופ הגרסה הזו - ראו "Recording + הערות ידניות" ב-CLAUDE.md)."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calls SET recording_url = ?, updated_at = ? WHERE call_sid = ?",
            (recording_url, datetime.now(timezone.utc).isoformat(), call_sid),
        )
        conn.commit()
    finally:
        conn.close()


def save_call_notes_and_summary(call_id: int, notes: str, summary: str) -> dict | None:
    """שומר הערות חופשיות שהקליד הנציג + תקציר שנוצר ע"י Claude על שורת שיחה קיימת
    (לפי id, לא call_sid). מחזיר את השורה המעודכנת המלאה."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calls SET notes = ?, summary = ?, updated_at = ? WHERE id = ?",
            (notes, summary, datetime.now(timezone.utc).isoformat(), call_id),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return dict(row) | {"simulated": bool(row["simulated"])} if row else None
    finally:
        conn.close()


def get_calls(phone: str, tenant_id: str = "default") -> list[dict]:
    """כל השיחות של ליד, החדשה ביותר קודם - לשימוש GET /api/calls (יומן שיחות מיני)."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM calls WHERE phone = ? AND tenant_id = ? ORDER BY created_at DESC",
            (phone, tenant_id),
        ).fetchall()
        return [dict(row) | {"simulated": bool(row["simulated"])} for row in rows]
    finally:
        conn.close()


def get_call(call_id: int) -> dict | None:
    """שיחה בודדת לפי id - קורא ל-POST /api/calls/<id>/notes (צריך phone/tenant_id/
    simulated לפני שקוראים ל-Claude ומשקפים את התקציר להיסטוריה)."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return dict(row) | {"simulated": bool(row["simulated"])} if row else None
    finally:
        conn.close()
