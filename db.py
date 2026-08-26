"""
שכבת גישה לטבלת messages ב-crm_data.db (SQLite).

חשוב: זו שכבת רישום טכנית נוספת (לוג של הודעות נכנסות/יוצאות) - לא מקור האמת של
המערכת. מקור האמת ליישות "לקוח" נשאר customers.json (ראו extract.py, וה"עקרונות
משותפים" ב-CLAUDE.md). אין כאן כפילות לוגיקת עסקים - רק רישום.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_FILE = Path(__file__).parent / "crm_data.db"

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
