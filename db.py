"""
שכבת גישה לטבלת messages ב-crm_data.db - SQLite (ברירת מחדל, פיתוח מקומי) או
PostgreSQL (ענן, שלב 4) לפי משתנה הסביבה DATABASE_URL.

חשוב: זו שכבת רישום טכנית נוספת (לוג של הודעות נכנסות/יוצאות) - לא מקור האמת של
המערכת. מקור האמת ליישות "לקוח" נשאר customers.json (ראו extract.py, וה"עקרונות
משותפים" ב-CLAUDE.md). אין כאן כפילות לוגיקת עסקים - רק רישום.

--- תמיכת PostgreSQL (שלב 4) ---
DATABASE_URL מוגדר (postgres://... / postgresql://...) → מתחברים ל-Postgres דרך
psycopg2, עם pool קטן (ThreadedConnectionPool) - בענן חיבור TCP חדש בכל קריאה
יקר משמעותית מ-sqlite3.connect() המקומי, וממש לא מתאים ל"scale". לא מוגדר
(ברירת המחדל) → בדיוק ההתנהגות הקיימת מאז ומתמיד: sqlite3, קובץ יחיד תחת
DATA_DIR - שום שינוי התנהגות בפיתוח מקומי.

כל שאר הקוד בקובץ הזה (מ-log_message ומטה) **לא יודע ולא צריך לדעת** על ההבדל -
_PGConnection עוטפת psycopg2 בממשק תואם-sqlite3.Connection: placeholders (?)
מתורגמים ל-%s בזמן ריצה, INSERT מקבל RETURNING id אוטומטית כדי ש-cur.lastrowid
יעבוד זהה בשני הבסיסים, וה-cursor נפתח תמיד עם RealDictCursor כדי ששורות יהיו
dict-like בדיוק כמו sqlite3.Row (תומכות גם ב-row["col"] וגם ב-dict(row)) - כל
קריאות ה-`conn.row_factory = sqlite3.Row` הקיימות בהמשך הקובץ נשארות ללא שינוי
ופשוט הופכות ל-no-op מול Postgres. ה-DDL עצמו (CREATE TABLE/מיגרציות) משותף
כמעט מילה-במילה בין שני הבסיסים - ראו _PK/_existing_columns למטה על שני
ההבדלים האמיתיים היחידים (הגדרת המפתח הראשי, ובדיקת עמודות קיימות ל-ALTER
TABLE).

⚠️ **הערה חשובה (שלב 5 - Enterprise Production Readiness):** אין ואף פעם לא
הייתה כאן טבלת SQL בשם "customers" - כרטיס הלקוח/ליד (כולל client_id/
ai_enabled, שלב 5) חי אך ורק ב-customers.json (extract.py, ראו paths.py),
בדיוק כמו שכתוב למעלה. אם חיפשת כאן טבלת customers כדי להוסיף לה עמודה -
זו לא הטבלה הנכונה: client_id/ai_enabled נוספו כשדות רגילים על כרטיס הלקוח
ב-customers.json (ראו extract.update_lead_ai_enabled/update_lead_fields),
לא כעמודות SQL - כדי לא ליצור שני מקורות-אמת מתחרים לאותה ישות "לקוח".
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

from paths import DATA_DIR

DB_FILE = DATA_DIR / "crm_data.db"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

if IS_POSTGRES:
    import psycopg2
    import psycopg2.extensions
    import psycopg2.extras
    import psycopg2.pool

    _PG_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
    _PG_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
    _pg_pool = psycopg2.pool.ThreadedConnectionPool(_PG_POOL_MIN, _PG_POOL_MAX, DATABASE_URL)

    IntegrityError = psycopg2.IntegrityError
else:
    # נחשף כדי ש-server.py יוכל לתפוס כשל UNIQUE (למשל api_key/user_id כפול ב-
    # db.create_user) בלי להכיר בעצמו איזה backend פעיל כרגע - ראו server.py.
    IntegrityError = sqlite3.IntegrityError

# מפתח ראשי: AUTOINCREMENT (sqlite) / SERIAL (postgres) - ההבדל האמיתי היחיד בין
# ה-DDL של שני הבסיסים; כל שאר טיפוסי העמודות (TEXT/INTEGER/REAL/TIMESTAMP)
# ותחביר ה-DEFAULT/ALTER TABLE ADD COLUMN/CREATE INDEX IF NOT EXISTS זהים
# במדויק בשני הבסיסים.
_PK = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

_MIGRATIONS = {
    "tenant_id": "ALTER TABLE messages ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    "channel": "ALTER TABLE messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'whatsapp'",
    "direction": "ALTER TABLE messages ADD COLUMN direction TEXT NOT NULL DEFAULT 'in'",
    "simulated": "ALTER TABLE messages ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0",
}

_QMARK_RE = re.compile(r"\?")


class _Cursor:
    """עוטפת psycopg2 cursor בודד - lastrowid מחושב מ-RETURNING id (ראו
    _PGConnection.execute), לא native כמו sqlite3.Cursor.lastrowid."""

    __slots__ = ("_raw", "lastrowid", "rowcount")

    def __init__(self, raw, lastrowid=None):
        self._raw = raw
        self.lastrowid = lastrowid
        self.rowcount = raw.rowcount

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()


class _PGConnection:
    """עוטפת חיבור psycopg2 בודד (מתוך _pg_pool) בממשק תואם-sqlite3.Connection -
    ראו הסבר מלא בראש הקובץ. close() מחזירה ל-pool, לא סוגרת socket בפועל."""

    def __init__(self, raw):
        self._raw = raw

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _value):
        pass  # no-op בכוונה - ה-cursor כבר נפתח תמיד עם RealDictCursor, ראו execute

    def execute(self, sql, params=()):
        pg_sql = _QMARK_RE.sub("%s", sql)
        is_insert = sql.strip().upper().startswith("INSERT") and "RETURNING" not in sql.upper()
        if is_insert:
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(pg_sql, list(params) if params else None)
        lastrowid = None
        if is_insert:
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
        return _Cursor(cur, lastrowid)

    def commit(self):
        self._raw.commit()

    def close(self):
        # rollback מונע "poisoned connection" בחזרה ל-pool: אם קרתה שגיאה בתוך
        # ה-transaction (למשל IntegrityError ב-create_user) ולא בוצע rollback
        # מפורש, החיבור הבא שישתמש באותו connection מה-pool ייתקל ב-"current
        # transaction is aborted" על כל שאילתה, גם תמימה לגמרי.
        try:
            if self._raw.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                self._raw.rollback()
        except Exception:
            pass
        _pg_pool.putconn(self._raw)


def _connect_raw():
    if IS_POSTGRES:
        return _PGConnection(_pg_pool.getconn())
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _existing_columns(conn, table: str) -> set[str]:
    """שמות העמודות הקיימות בפועל בטבלה - לבדיקת "האם צריך ALTER TABLE ADD
    COLUMN" (מיגרציה קלה, ראו _MIGRATIONS ושלושת השימושים הנוספים ב-
    _init_schema). ההבדל האמיתי היחיד בין הבסיסים: sqlite חושפת את זה דרך
    PRAGMA (שם הטבלה מוזרק ישירות ב-f-string כי PRAGMA לא ניתנת לפרמור עם
    ?/%s - אבל table תמיד מגיע כקבוע-קוד קשיח מ-_init_schema, לא קלט משתמש);
    postgres חושפת דרך information_schema.columns הסטנדרטי (כן ניתן לפרמור)."""
    if IS_POSTGRES:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)
        ).fetchall()
        return {row["column_name"] for row in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


_schema_ready = False
_schema_lock = threading.Lock()


def _init_schema(conn) -> None:
    """כל ה-CREATE TABLE/CREATE INDEX/ALTER TABLE - משותף כמעט לגמרי בין sqlite
    ל-postgres (ראו _PK/_existing_columns לשני ההבדלים האמיתיים). רץ פעם אחת
    בלבד לכל process (ראו _ensure_schema), לא בכל _get_connection() כמו לפני
    שלב 4 - כדי לא "להכביד" על postgres עם 8+ בדיקות סכימה על כל שאילתה
    בודדת (עלות round-trip רשתית אמיתית שלא הייתה קיימת מול קובץ sqlite מקומי)."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS messages (
            id {_PK},
            tenant_id TEXT NOT NULL DEFAULT 'default',
            phone TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'whatsapp',
            direction TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # מיגרציה קלה: אם messages כבר קיימת מ-create_db.py הישן (בלי tenant_id/channel/direction) - מוסיפים את העמודות החסרות
    existing_columns = _existing_columns(conn, "messages")
    for column, ddl in _MIGRATIONS.items():
        if column not in existing_columns:
            conn.execute(ddl)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS scheduler_runs (
            id {_PK},
            tenant_id TEXT NOT NULL DEFAULT 'default',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            leads_scanned INTEGER NOT NULL DEFAULT 0,
            leads_due INTEGER NOT NULL DEFAULT 0,
            auto_send INTEGER NOT NULL DEFAULT 0,
            summary TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS calendar_tasks (
            id {_PK},
            tenant_id TEXT NOT NULL DEFAULT 'default',
            phone TEXT NOT NULL,
            title TEXT NOT NULL,
            due_date TEXT NOT NULL,
            due_time TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            notes TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            intent TEXT,
            related_task_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # מיגרציה קלה: calendar_tasks שכבר קיימת (לפני סוכן היומן, scheduling_agent.py)
    # לא תקבל את שלוש העמודות החדשות מ-CREATE TABLE IF NOT EXISTS לבדו - אותו
    # רעיון בדיוק כמו transcript_segments ב-calls למטה. source מבחין הצעה
    # שזוהתה אוטומטית ('scheduling_agent') ממשימה שנוצרה ידנית ('manual', ברירת
    # המחדל - כל המשימות הקיימות היום); intent/related_task_id משמשים רק
    # להצעות (ראו db.confirm_task_proposal) - null עבור משימות רגילות.
    existing_task_columns = _existing_columns(conn, "calendar_tasks")
    for column, ddl in (
        ("source", "ALTER TABLE calendar_tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"),
        ("intent", "ALTER TABLE calendar_tasks ADD COLUMN intent TEXT"),
        ("related_task_id", "ALTER TABLE calendar_tasks ADD COLUMN related_task_id INTEGER"),
    ):
        if column not in existing_task_columns:
            conn.execute(ddl)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS system_feedback (
            id {_PK},
            feedback_type TEXT NOT NULL DEFAULT 'idea',
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS reactivation_batches (
            id {_PK},
            tenant_id TEXT NOT NULL DEFAULT 'default',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'running',
            total_leads INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            simulated_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS reactivation_batch_items (
            id {_PK},
            batch_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            name TEXT,
            message TEXT,
            result TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS calls (
            id {_PK},
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
            transcript_segments TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # מיגרציה קלה: calls שכבר קיימת (כמו ב-crm_data.db הנוכחי, לפני שנוסף נגן
    # התמלול האינטראקטיבי) לא תקבל את העמודה החדשה מ-CREATE TABLE IF NOT EXISTS
    # לבדו - צריך להוסיף אותה בנפרד אם היא עוד לא שם (אותו רעיון כמו _MIGRATIONS
    # למעלה, שם ספציפית ל-messages).
    existing_call_columns = _existing_columns(conn, "calls")
    if "transcript_segments" not in existing_call_columns:
        conn.execute("ALTER TABLE calls ADD COLUMN transcript_segments TEXT")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS outbound_queue (
            id {_PK},
            tenant_id TEXT NOT NULL DEFAULT 'default',
            campaign_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            name TEXT,
            company TEXT,
            category TEXT,
            message TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            scheduled_at TIMESTAMP,
            sent_at TIMESTAMP,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_queue_campaign ON outbound_queue(campaign_id)")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS users (
            id {_PK},
            api_key TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'client',
            tenant_ids TEXT,
            commission_rate REAL,
            avg_deal_value REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        _init_schema(conn)
        _schema_ready = True


def _get_connection():
    conn = _connect_raw()
    _ensure_schema(conn)
    return conn


def health_check() -> bool:
    """בדיקת חיות מינימלית ל-DB (SELECT 1 בפועל, לא רק "התחברות הצליחה") -
    נקראת מ-GET /health ב-server.py (שלב 4). מעלה חריגה אם משהו לא תקין -
    הקורא (api_health) תופס ומדווח unhealthy, לא בולעת שגיאות כאן."""
    conn = _get_connection()
    try:
        conn.execute("SELECT 1")
        return True
    finally:
        conn.close()


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


# ---- קמפיין החייאה ברקע (reactivate.py, שליחה בפועל דרך /api/reactivate) - מעקב
# התקדמות חי לצורך תיעוד ברור בדשבורד: "batch" אחד = הרצת --send אחת (רשימת לידים
# קרים שנשלחה אליהם הודעה, עם השהיה מבוקרת בין הודעה להודעה - ראו reactivate.py),
# "item" אחד = תוצאת השליחה לליד בודד בתוך ה-batch (pending עד שמגיע תורו).

def create_reactivation_batch(tenant_id: str, total_leads: int) -> int:
    conn = _get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO reactivation_batches (tenant_id, started_at, status, total_leads) "
            "VALUES (?, ?, 'running', ?)",
            (tenant_id, datetime.now(timezone.utc).isoformat(), total_leads),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_batch_item(batch_id: int, phone: str, name: str | None, message: str,
                    result: str, error: str | None = None) -> None:
    """result: 'sent' / 'simulated' / 'error'. מעדכן גם את המונים המצטברים על
    ה-batch עצמו (sent_count/simulated_count/error_count) - כדי שסטטוס חי
    (GET /api/reactivate/batches/<id>) לא יצטרך לספור מחדש את כל השורות בכל בקשה."""
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO reactivation_batch_items (batch_id, phone, name, message, result, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch_id, phone, name, message, result, error, datetime.now(timezone.utc).isoformat()),
        )
        column = {"sent": "sent_count", "simulated": "simulated_count", "error": "error_count"}.get(result)
        if column:
            conn.execute(f"UPDATE reactivation_batches SET {column} = {column} + 1 WHERE id = ?", (batch_id,))
        conn.commit()
    finally:
        conn.close()


def finish_reactivation_batch(batch_id: int, status: str = "completed") -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE reactivation_batches SET status = ?, finished_at = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), batch_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_reactivation_batch(batch_id: int) -> dict | None:
    """מחזיר את ה-batch עצמו + כל הפריטים שכבר עובדו בו (החדש ביותר קודם) - לשימוש
    ה-polling של הדשבורד בזמן שהשליחה עדיין רצה ברקע."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        batch_row = conn.execute(
            "SELECT * FROM reactivation_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch_row:
            return None
        items = conn.execute(
            "SELECT phone, name, message, result, error, created_at FROM reactivation_batch_items "
            "WHERE batch_id = ? ORDER BY id ASC",
            (batch_id,),
        ).fetchall()
        batch = dict(batch_row)
        batch["items"] = [dict(item) for item in items]
        return batch
    finally:
        conn.close()


def get_reactivation_batches(tenant_id: str | None = None, limit: int = 10) -> list[dict]:
    """רשימת ה-batches האחרונים (החדש ביותר קודם), בלי הפריטים המלאים - לתצוגת
    היסטוריה קצרה בדשבורד."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM reactivation_batches WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reactivation_batches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
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


def _call_row_to_dict(row) -> dict:
    # type: (sqlite3.Row | dict) -> dict - sqlite3.Row (מקומי) או RealDictRow
    # (postgres, ראו _PGConnection.execute) - שניהם תומכים ב-row["col"] ו-dict(row).
    """ממיר שורת calls גולמית ל-dict - simulated ל-bool, transcript_segments (JSON
    text, ראו save_transcript_segments) ל-list פייתוני (או None אם לא תומלל)."""
    d = dict(row) | {"simulated": bool(row["simulated"])}
    d["transcript_segments"] = json.loads(row["transcript_segments"]) if row["transcript_segments"] else None
    return d


def save_transcript_segments(call_id: int, segments: list[dict]) -> dict | None:
    """שומר תמלול עם חותמות-זמן פר-משפט (ראו transcription.
    transcribe_audio_with_segments) על שורת שיחה קיימת - לנגן התמליל האינטראקטיבי
    (POST /api/calls/<id>/transcribe-recording). נפרד מ-save_call_notes_and_summary/
    update_call - זה שדה טכני נוסף (JSON), לא הערות/תקציר טקסט חופשי."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calls SET transcript_segments = ?, updated_at = ? WHERE id = ?",
            (json.dumps(segments, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), call_id),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return _call_row_to_dict(row) if row else None
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
        return _call_row_to_dict(row) if row else None
    finally:
        conn.close()


def update_call(call_id: int, status: str | None = None, notes: str | None = None,
                 summary: str | None = None) -> dict | None:
    """עדכון ידני של שורת שיחה קיימת - status/notes/summary כל אחד בנפרד (None =
    השאר ללא שינוי), לשימוש POST /api/calls/<id>/update. בניגוד ל-
    save_call_notes_and_summary (שתמיד קובע notes+summary יחד, כחלק מזרימת
    התקציר האוטומטי המקורית ע"י Claude) - זו עריכה ידנית ישירה, שדה-שדה.
    **בכוונה לא נוגע ב-messages/customers.json history** - אלו לוג היסטורי
    בלתי-ניתן-לשינוי (append-only) בכל שאר המערכת; העריכה כאן מתקנת רק את
    רשומת ה-calls עצמה (מקור האמת לפרטי השיחה בפני עצמה), לא "מזייפת" תיקון
    רטרואקטיבי של מה שכבר הוצג/נשלח בצ'אט בזמנו."""
    fields, values = [], []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
    if summary is not None:
        fields.append("summary = ?")
        values.append(summary)
    if not fields:
        return get_call(call_id)
    fields.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat())
    values.append(call_id)

    conn = _get_connection()
    try:
        conn.execute(f"UPDATE calls SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return _call_row_to_dict(row) if row else None
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
        return [_call_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def get_call(call_id: int) -> dict | None:
    """שיחה בודדת לפי id - קורא ל-POST /api/calls/<id>/notes (צריך phone/tenant_id/
    simulated לפני שקוראים ל-Claude ומשקפים את התקציר להיסטוריה)."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return _call_row_to_dict(row) if row else None
    finally:
        conn.close()


def create_task(
    phone: str,
    tenant_id: str = "default",
    title: str = "",
    due_date: str = "",
    due_time: str | None = None,
    notes: str | None = None,
    status: str = "pending",
    source: str = "manual",
    intent: str | None = None,
    related_task_id: int | None = None,
) -> int:
    """יוצר משימת מעקב/תזכורת (follow-up) לליד, או הצעת-פגישה אוטומטית (ראו
    scheduling_agent.create_scheduling_proposal - status="pending_confirmation",
    source="scheduling_agent"). מוחזר ה-id. ברירות המחדל (status="pending",
    source="manual") שומרות על ההתנהגות המקורית המדויקת לכל קורא קיים
    (POST /api/tasks) - לא נדרש שינוי בקוד שכבר קורא לפונקציה הזו."""
    conn = _get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO calendar_tasks "
            "(tenant_id, phone, title, due_date, due_time, notes, status, source, intent, related_task_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, phone, title, due_date, due_time, notes, status, source, intent, related_task_id, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_task_status(task_id: int, status: str) -> dict | None:
    """מעדכן סטטוס משימה (pending/done/cancelled/pending_confirmation). מחזיר את
    השורה המעודכנת. שימוש ישיר על שורת "pending_confirmation" (הצעה אוטומטית
    שטרם טופלה) חסום ב-API (POST /api/tasks/<id>/status דוחה 400) - יש להשתמש
    ב-confirm_task_proposal/reject_task_proposal למטה, כדי שלא לעקוף בטעות את
    הטיפול בפגישה המקורית (related_task_id) בבקשת ביטול/הזזת מועד."""
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


def update_task_datetime(task_id: int, due_date: str, due_time: str | None) -> dict | None:
    """מעדכן תאריך/שעה של משימה קיימת (הזזת מועד). בשימוש **רק** דרך
    confirm_task_proposal אחרי אישור נציג מפורש - scheduling_agent.py עצמו
    אף פעם לא קורא לפונקציה הזו ישירות (ראו חוק הבטיחות ב-scheduling_agent.py)."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calendar_tasks SET due_date = ?, due_time = ?, updated_at = ? WHERE id = ?",
            (due_date, due_time, datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calendar_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def confirm_task_proposal(task_id: int) -> dict | None:
    """מאשר הצעת פגישה שזוהתה אוטומטית ע"י scheduling_agent (status=
    'pending_confirmation') - הפעולה היחידה שבאמת "מפעילה" הצעה, ותמיד
    מגיעה מקליק נציג מפורש ב-UI (POST /api/tasks/<id>/confirm), אף פעם לא
    אוטומטית מתוך scheduling_agent.py עצמו. מחזיר None אם ההצעה לא נמצאה, או
    כבר לא ב-pending_confirmation (מונע אישור/דחייה כפולים אם שני נציגים
    לחצו כמעט בו-זמנית, או אם ההצעה כבר טופלה).

    לפי intent:
    - "cancel": מבטלת בפועל את הפגישה המקורית (related_task_id, status=
      'cancelled') - ורק אז מסמנת את שורת ההצעה עצמה כ-'done' (טופלה).
    - "reschedule": מעדכנת את due_date/due_time של הפגישה המקורית לערכים
      שהוצעו (ששמורים על שורת ההצעה עצמה) - ורק אז מסמנת את ההצעה כ-'done'.
    - אחרת ("new", או הצעת cancel/reschedule בלי related_task_id תקין) -
      שורת ההצעה עצמה הופכת לפגישה אמיתית (status='pending' רגיל)."""
    proposal = get_task(task_id)
    if not proposal or proposal["status"] != "pending_confirmation":
        return None

    intent = proposal.get("intent")
    related_id = proposal.get("related_task_id")
    if intent == "cancel" and related_id and get_task(related_id):
        update_task_status(related_id, "cancelled")
        update_task_status(task_id, "done")
    elif intent == "reschedule" and related_id and get_task(related_id):
        update_task_datetime(related_id, proposal["due_date"], proposal["due_time"])
        update_task_status(task_id, "done")
    else:
        update_task_status(task_id, "pending")

    return get_task(task_id)


def reject_task_proposal(task_id: int) -> dict | None:
    """דוחה הצעת פגישה שזוהתה אוטומטית - לא נוגעת בשום פגישה קיימת
    (related_task_id, אם יש, נשאר בדיוק כפי שהיה); רק מסמנת את שורת ההצעה
    עצמה כ-'cancelled'. מחזיר None אם ההצעה לא נמצאה/כבר לא pending_confirmation."""
    proposal = get_task(task_id)
    if not proposal or proposal["status"] != "pending_confirmation":
        return None
    return update_task_status(task_id, "cancelled")


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


# ---- outbound_queue (קמפייני B2B outbound - ראו outbound_engine.py) ----
# טבלה עצמאית, לא reactivation_batches/reactivation_batch_items הקיימות: אלו
# משרתות מטרה שונה (החייאת לידים *קיימים* ב-customers.json) - כאן מדובר בפנייה
# ראשונה לאנשי קשר *חדשים* שיובאו מ-CSV/Excel B2B, שעדיין לא בהכרח כרטיס ליד
# בכלל. campaign_id מקבץ שורות מאותו ייבוא יחד (לתצוגת "תור שליחה" בדשבורד).

def add_outbound_queue_item(
    tenant_id: str,
    campaign_id: str,
    phone: str,
    name: str | None = None,
    company: str | None = None,
    category: str | None = None,
    message: str | None = None,
) -> int:
    """מוסיף איש קשר יחיד לתור השליחה (status='queued'). לא שולח כלום - רק רישום;
    השליחה בפועל קורית אך ורק דרך outbound_engine.run_campaign (הפעלה מפורשת,
    לא אוטומטית בזמן ייבוא - ראו חוק בטיחות #1 ב-CLAUDE.md)."""
    conn = _get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO outbound_queue "
            "(tenant_id, campaign_id, phone, name, company, category, message, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (tenant_id, campaign_id, phone, name, company, category, message, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_outbound_queue(
    tenant_id: str | None = None, campaign_id: str | None = None, status: str | None = None
) -> list[dict]:
    """שורות תור ה-outbound, לפי הסינונים שסופקו - ל-GET /api/campaigns/queue
    (polling חי מה-UI) וגם לשימוש הפנימי של outbound_engine (שליפת ה-'queued')."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        clauses, params = [], []
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if campaign_id:
            clauses.append("campaign_id = ?")
            params.append(campaign_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM outbound_queue {where} ORDER BY id ASC", params
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_outbound_campaigns(tenant_id: str | None = None) -> list[dict]:
    """רשימת קמפיינים מקובצת (campaign_id) עם ספירות לפי סטטוס - לתצוגת "קמפיינים"
    ב-UI, בלי לשלוף את כל השורות הבודדות. total/queued/sent/simulated/failed
    מוחזרים כמספרים מוכנים לתצוגה (progress bar וכו')."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        where = "WHERE tenant_id = ?" if tenant_id else ""
        params = [tenant_id] if tenant_id else []
        rows = conn.execute(
            f"""
            SELECT
                campaign_id,
                tenant_id,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status = 'simulated' THEN 1 ELSE 0 END) AS simulated,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'skipped_quota' THEN 1 ELSE 0 END) AS skipped_quota,
                MIN(created_at) AS created_at
            FROM outbound_queue {where}
            GROUP BY campaign_id, tenant_id
            ORDER BY MIN(created_at) DESC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_outbound_status(
    item_id: int, status: str, sent_at: str | None = None, error: str | None = None
) -> dict | None:
    """מעדכן סטטוס פריט בתור (queued/sending/sent/simulated/failed/skipped_quota/
    cancelled) - קורא ל-outbound_engine תוך כדי עיבוד התור. מחזיר השורה המעודכנת."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE outbound_queue SET status = ?, sent_at = COALESCE(?, sent_at), "
            "error = ?, updated_at = ? WHERE id = ?",
            (status, sent_at, error, datetime.now(timezone.utc).isoformat(), item_id),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM outbound_queue WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_sent_today(tenant_id: str) -> int:
    """כמה הודעות outbound נשלחו בפועל היום (UTC) עבור ה-tenant הזה - sent+
    simulated נספרים יחד (שתיהן "ניסיון שליחה אמיתי" מבחינת קצב/מכסה, בדיוק
    כמו שההחייאה כבר עושה - simulated ספציפית כשל-ספק ידוע, לא כשל-קוד).
    outbound_engine.DAILY_QUOTA נאכף מול המספר הזה, לא מול total_rows בתור."""
    conn = _get_connection()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM outbound_queue "
            "WHERE tenant_id = ? AND status IN ('sent', 'simulated') AND sent_at LIKE ?",
            (tenant_id, f"{today}%"),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# ---- users (RBAC & Multi-Tenant Isolation - שלב 2) ----
# מפתח API ראשי אחד (API_SECRET_KEY ב-.env, ראו server.py:_require_api_key) ממשיך
# להתקיים כ"אדמין מובלע" בשביל תאימות לאחור מוחלטת עם שלב 6 (לא שורה כאן, לא
# תלוי בטבלה הזו בכלל). הטבלה הזו מוסיפה משתמשי partner/client *נוספים* - כל
# אחד עם מפתח API נפרד משלו וסט tenant_ids שהוא רשאי לראות.

def _row_to_user(row: dict) -> dict:
    row = dict(row)
    row["tenant_ids"] = json.loads(row["tenant_ids"]) if row.get("tenant_ids") else []
    return row


def create_user(
    api_key: str,
    user_id: str,
    name: str,
    role: str,
    tenant_ids: list[str] | None = None,
    commission_rate: float | None = None,
    avg_deal_value: float | None = None,
) -> int:
    """יוצר משתמש RBAC חדש (partner/client/admin נוסף) עם מפתח API ייחודי משלו.
    tenant_ids: רשימת ה-tenant-ים שהמשתמש הזה רשאי לראות (admin: התעלמות -
    admin תמיד רואה הכל, ראו server.py:_effective_tenant_ids). מעלה
    sqlite3.IntegrityError אם api_key/user_id כבר קיימים - לא דורס בשקט."""
    conn = _get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (api_key, user_id, name, role, tenant_ids, commission_rate, avg_deal_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (api_key, user_id, name, role, json.dumps(tenant_ids or []), commission_rate, avg_deal_value),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_by_api_key(api_key: str) -> dict | None:
    """שולפת משתמש RBAC לפי מפתח API - נקראת בכל בקשת /api/* (ראו server.py:
    _require_api_key) אחרי שנבדק שזה לא המפתח הראשי (API_SECRET_KEY, אדמין
    מובלע). None אם המפתח לא שייך לאף משתמש - הקורא מחזיר 401."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE api_key = ?", (api_key,)).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def get_users() -> list[dict]:
    """כל משתמשי ה-RBAC (לא כולל את מפתח האדמין הראשי מ-.env, שאינו שורה כאן) -
    ל-GET /api/admin/users (אדמין-בלבד). api_key מוחזר מלא בכוונה - זה נתיב
    אדמין-בלבד, וללא זה אי אפשר להציג/למסור לשותף/לקוח את המפתח שהוקצה לו."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [_row_to_user(row) for row in rows]
    finally:
        conn.close()


def analytics_daily_counts(table: str, tenant_ids: list[str] | None, since_date: str) -> dict:
    """סופרת שורות מ-calls/calendar_tasks מקובצות ליום (date(created_at)) מאז
    since_date (כולל) - למחוון Analytics (GET /api/analytics/timeseries,
    server.py). tenant_ids: None = בלי סינון (admin); רשימה = מסונן אליה
    בלבד (RBAC, שלב 2) - רשימה ריקה מחזירה {} (בכוונה, לא כל הטבלה).
    table מגיע תמיד כקבוע מחרוזת קשיח מהקוד הקורא (לא קלט משתמש) - ה-assert
    הוא רק רשת ביטחון, לא סניטציה של קלט חיצוני."""
    assert table in ("calls", "calendar_tasks"), "טבלה לא נתמכת ל-analytics_daily_counts"
    conn = _get_connection()
    try:
        clauses = ["date(created_at) >= ?"]
        params: list = [since_date]
        if tenant_ids is not None:
            if not tenant_ids:
                return {}
            placeholders = ",".join("?" for _ in tenant_ids)
            clauses.append(f"tenant_id IN ({placeholders})")
            params.extend(tenant_ids)
        where = " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT date(created_at) AS d, COUNT(*) FROM {table} WHERE {where} GROUP BY d",
            params,
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def delete_user(user_id: str) -> bool:
    """מוחקת משתמש RBAC לפי user_id (לא api_key - נוח יותר לניהול, לא חושף/
    דורש את המפתח כדי למחוק). מחזירה True אם נמחקה שורה בפועל."""
    conn = _get_connection()
    try:
        cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
