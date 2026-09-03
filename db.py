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
        CREATE TABLE IF NOT EXISTS calendar_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            phone TEXT NOT NULL,
            title TEXT NOT NULL,
            due_date TEXT NOT NULL,
            due_time TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_type TEXT NOT NULL DEFAULT 'idea',
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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


def create_task(
    phone: str,
    tenant_id: str = "default",
    title: str = "",
    due_date: str = "",
    due_time: str | None = None,
    notes: str | None = None,
) -> int:
    """יוצר משימת מעקב/תזכורת (follow-up) לליד. מוחזר ה-id."""
    conn = _get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO calendar_tasks (tenant_id, phone, title, due_date, due_time, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, phone, title, due_date, due_time, notes, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_task_status(task_id: int, status: str) -> dict | None:
    """מעדכן סטטוס משימה (pending/done/cancelled). מחזיר את השורה המעודכנת."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calendar_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calendar_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_tasks(phone: str | None = None, tenant_id: str | None = None, status: str | None = None) -> list[dict]:
    """משימות מעקב. עם phone - רק המשימות של הליד הזה (לפאנל "📅 משימות" - כל סטטוס).
    בלי phone - כל המשימות (לתצוגת "📅 יומן" הגלובלית), עם סינון אופציונלי לפי
    tenant_id/status. ממוין לפי due_date/due_time עולה (הקרוב ביותר קודם) - בכוונה
    שונה מכל שאר הטבלאות בקובץ הזה (שממוינות לפי זמן יצירה): כאן מה שחשוב הוא מתי
    המשימה אמורה לקרות, לא מתי היא נוצרה."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        clauses, params = [], []
        if phone:
            clauses.append("phone = ?")
            params.append(phone)
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM calendar_tasks {where} ORDER BY due_date ASC, due_time ASC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_task(task_id: int) -> dict | None:
    """משימה בודדת לפי id."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calendar_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_feedback(feedback_type: str, description: str) -> int:
    """יוצר רשומת משוב על המערכת עצמה (באג/רעיון) - לא קשור ללידים. מוחזר ה-id."""
    conn = _get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO system_feedback (feedback_type, description, created_at) VALUES (?, ?, ?)",
            (feedback_type, description, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_feedback_status(feedback_id: int, status: str) -> dict | None:
    """מעדכן סטטוס טיפול במשוב (new/in_progress/done/wontfix). מחזיר את השורה המעודכנת."""
    conn = _get_connection()
    try:
        conn.execute("UPDATE system_feedback SET status = ? WHERE id = ?", (status, feedback_id))
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM system_feedback WHERE id = ?", (feedback_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_feedback() -> list[dict]:
    """כל רשומות המשוב, החדשה ביותר קודם."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM system_feedback ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---- ציון חום לליד (Lead Scoring) ----
# רכיבי הציון (סה"כ מקסימלי = 100, מובטח רצפה של 1):
#   הודעות וואטסאפ  - עד 35 נק' (4 נק' להודעה, נחסם ב-35)
#   שיחות (calls)    - עד 25 נק' (10 נק' לשיחה, נחסם ב-25)
#   משימות שהושלמו  - עד 15 נק' (5 נק' למשימה, נחסם ב-15)
#   עדכניות (recency)- עד 25 נק' (לפי כמה זמן עבר מאז הפעילות האחרונה - הודעה/שיחה/משימה)
# זהו חישוב נגזר (derived) בלבד - לא נשמר בשום מקום, מחושב לפי דרישה (כמו
# get_last_message) כדי שלא יידרש invalidation בכל פעם שמתווספת פעילות חדשה.
_SCORE_MESSAGE_POINTS, _SCORE_MESSAGE_CAP = 4, 35
_SCORE_CALL_POINTS, _SCORE_CALL_CAP = 10, 25
_SCORE_TASK_POINTS, _SCORE_TASK_CAP = 5, 15
_SCORE_RECENCY_BANDS = [(1, 25), (3, 18), (7, 10), (30, 4)]  # (גיל מקסימלי בימים, נקודות)


def compute_lead_score(phone: str, tenant_id: str = "default") -> int:
    """מחשב "ציון חום" (1-100) לליד על בסיס פעילות בפועל בטבלאות messages/calls/
    calendar_tasks - ראו פירוט המשקלים מעל. משמש את GET /api/leads?include_score=1."""
    conn = _get_connection()
    try:
        message_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE phone = ? AND tenant_id = ?", (phone, tenant_id)
        ).fetchone()[0]
        call_count = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE phone = ? AND tenant_id = ?", (phone, tenant_id)
        ).fetchone()[0]
        done_task_count = conn.execute(
            "SELECT COUNT(*) FROM calendar_tasks WHERE phone = ? AND tenant_id = ? AND status = 'done'",
            (phone, tenant_id),
        ).fetchone()[0]
        last_activity = conn.execute(
            "SELECT MAX(ts) FROM ("
            "SELECT MAX(timestamp) AS ts FROM messages WHERE phone = ? AND tenant_id = ? "
            "UNION ALL "
            "SELECT MAX(created_at) AS ts FROM calls WHERE phone = ? AND tenant_id = ? "
            "UNION ALL "
            "SELECT MAX(updated_at) AS ts FROM calendar_tasks WHERE phone = ? AND tenant_id = ?"
            ")",
            (phone, tenant_id, phone, tenant_id, phone, tenant_id),
        ).fetchone()[0]
    finally:
        conn.close()

    score = (
        min(message_count * _SCORE_MESSAGE_POINTS, _SCORE_MESSAGE_CAP)
        + min(call_count * _SCORE_CALL_POINTS, _SCORE_CALL_CAP)
        + min(done_task_count * _SCORE_TASK_POINTS, _SCORE_TASK_CAP)
    )

    if last_activity:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last_activity)).days
        for max_age, points in _SCORE_RECENCY_BANDS:
            if age_days <= max_age:
                score += points
                break

    return max(1, min(100, score))


def search_activity(query: str) -> list[tuple[str, str]]:
    """מחפש טקסט חופשי בתוכן פעילות (לא בשדות הכרטיס עצמו - אלה נבדקים בנפרד ב-
    server.py מ-customers.json) - טקסט הודעות, הערות/תקציר שיחות, וכותרת/הערות
    משימות. מחזיר רשימת (phone, tenant_id) ייחודיים שיש בהם התאמה - לשימוש
    GET /api/search, כחלק מהחיפוש החופשי הרב-שדות בדשבורד."""
    like = f"%{query}%"
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT phone, tenant_id FROM messages WHERE message LIKE ? "
            "UNION "
            "SELECT DISTINCT phone, tenant_id FROM calls WHERE notes LIKE ? OR summary LIKE ? "
            "UNION "
            "SELECT DISTINCT phone, tenant_id FROM calendar_tasks WHERE title LIKE ? OR notes LIKE ?",
            (like, like, like, like, like),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]
    finally:
        conn.close()


def rekey_phone(old_phone: str, new_phone: str, tenant_id: str = "default") -> None:
    """מעדכן את עמודת phone בכל שלוש הטבלאות (messages/calls/calendar_tasks) בבת
    אחת (חיבור/commit יחיד) - חלק מ-extract.rekey_lead (עריכת מספר טלפון לליד
    קיים). אין מזהה ליד מספרי בשום מקום במערכת - phone+tenant_id הוא המפתח
    היחיד, גם כאן וגם ב-customers.json - ולכן שינוי טלפון חייב "לרדוף" אחרי כל
    שלוש הטבלאות, לא רק אחרי הכרטיס."""
    conn = _get_connection()
    try:
        for table in ("messages", "calls", "calendar_tasks"):
            conn.execute(
                f"UPDATE {table} SET phone = ? WHERE phone = ? AND tenant_id = ?",
                (new_phone, old_phone, tenant_id),
            )
        conn.commit()
    finally:
        conn.close()


def delete_lead_activity(phone: str, tenant_id: str = "default") -> None:
    """מוחק את כל שורות הפעילות (messages/calls/calendar_tasks) של ליד - חלק מ-
    extract.delete_lead. לא מוחק את הכרטיס עצמו (זה ב-customers.json, מטופל
    בנפרד ב-extract.py) - רק את הלוג הטכני הנלווה."""
    conn = _get_connection()
    try:
        for table in ("messages", "calls", "calendar_tasks"):
            conn.execute(f"DELETE FROM {table} WHERE phone = ? AND tenant_id = ?", (phone, tenant_id))
        conn.commit()
    finally:
        conn.close()
