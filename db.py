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
from datetime import datetime, timedelta, timezone

from paths import DATA_DIR

DB_FILE = DATA_DIR / "crm_data.db"

# מקור-אמת יחיד לערוצי 4 שלבי הפולו-אפ האוטומטי (scheduler._FOLLOWUP_CHANNELS
# מפנה לכאן - db לא יכול לייבא את scheduler, מעגליות). ה-SQL נבנה מהקבוע, לא
# מקלט משתמש - בטוח כליטרל.
FOLLOWUP_CHANNELS = ("followup", "followup_2", "followup_3", "followup_final")
_FOLLOWUP_CHANNELS_SQL = "(" + ", ".join(f"'{c}'" for c in FOLLOWUP_CHANNELS) + ")"

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
    # ThreadedConnectionPool זורק PoolError מיד כשהוא מלא, ולא ממתין. תחת עומס
    # (threads שממתינים לנעילת כרטיס FOR UPDATE מחזיקים חיבור) זה הפיל פעולות.
    # הסמפור גורם לממתין לחכות לחיבור פנוי, עד DB_POOL_WAIT_SECONDS.
    _PG_POOL_WAIT_SECONDS = float(os.environ.get("DB_POOL_WAIT_SECONDS", "30"))
    _pg_pool_slots = threading.BoundedSemaphore(_PG_POOL_MAX)

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

# ברירת המחדל עבור tenants.features - גם ל-DDL (DEFAULT בעמודה) וגם לקוד הפייתון
# (get_tenant_features נופל לזה כש-tenant לא רשום בטבלה בכלל - ראו שם).
_DEFAULT_FEATURES = {
    "whatsapp_ai": True,
    "telephony_queue": True,
    "auto_not_relevant": True,
}
_DEFAULT_FEATURES_JSON = json.dumps(_DEFAULT_FEATURES)
# ציבורי בכוונה (בלי _) - server.py מייבא את זה לאימות שה-flags שמתקבלים מ-
# PUT /api/tenant/features הם בדיוק אחד מהשמות הידועים, לא מפתח שרירותי כלשהו.
KNOWN_FEATURE_FLAGS = set(_DEFAULT_FEATURES.keys())

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
        self._returned = False

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
        if self._returned:
            return  # close() כפול לא מחזיר את החיבור/הסמפור פעמיים
        self._returned = True
        try:
            _pg_pool.putconn(self._raw)
        finally:
            _pg_pool_slots.release()


def _connect_raw():
    if IS_POSTGRES:
        if not _pg_pool_slots.acquire(timeout=_PG_POOL_WAIT_SECONDS):
            raise psycopg2.pool.PoolError(
                f"אין חיבור פנוי ל-Postgres אחרי {_PG_POOL_WAIT_SECONDS:.0f} שניות (DB_POOL_MAX={_PG_POOL_MAX})"
            )
        try:
            return _PGConnection(_pg_pool.getconn())
        except BaseException:
            _pg_pool_slots.release()
            raise
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
        # תזכורת פגישה אוטומטית בוואטסאפ (בוקר/ערב, לפי שעת הפגישה - ראו
        # scheduler.compute_meeting_reminder_time/check_pending_meeting_reminders) -
        # דגל חד-פעמי, בדיוק כמו calls.followup_sent.
        ("reminder_sent", "ALTER TABLE calendar_tasks ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0"),
        # Meeting Confirmation & No-Show Prevention (ראו scheduler.check_pending_
        # meeting_invites/check_pending_marketer_reminders): invite_sent - הודעת
        # זימון (כתובת+מי לפגוש) נשלחה ליום לפני. marketer_reminder_status -
        # "created" ברגע שנוצרה משימת "📞 אישור הגעה" פנימית למשווק (ראו למטה),
        # null לפני כן. attendance_status - התוצאה שהמשווק מסמן בפועל אחרי
        # השיחה (confirmed/no_answer/declined) - null = עדיין לא טופל.
        ("invite_sent", "ALTER TABLE calendar_tasks ADD COLUMN invite_sent INTEGER NOT NULL DEFAULT 0"),
        ("marketer_reminder_status", "ALTER TABLE calendar_tasks ADD COLUMN marketer_reminder_status TEXT"),
        ("attendance_status", "ALTER TABLE calendar_tasks ADD COLUMN attendance_status TEXT"),
        # Automated Meeting Booking Confirmation (ראו server.api_task_confirm) -
        # דגל חד-פעמי, בדיוק כמו invite_sent/reminder_sent - מונע שליחה כפולה אם
        # /confirm נקרא פעמיים (למשל קליק כפול בדשבורד).
        ("booking_confirmation_sent", "ALTER TABLE calendar_tasks ADD COLUMN booking_confirmation_sent INTEGER NOT NULL DEFAULT 0"),
        # הפרדה מלאה בין "פולו-אפ להתקשרות" (תזכורת-לעצמנו, לא מול הלקוח) לבין
        # "פגישה רשמית" (מול הלקוח בפועל) - עד כאן כל השורות ב-calendar_tasks
        # היו שוות-מעמד (אותם due_date/due_time/status="pending"), מה שגרם
        # לבאג אמיתי: scheduler.check_pending_meeting_reminders/invites סרקו
        # *כל* משימה עם status="pending" (כולל פולו-אפ ידני/NLP רגיל) ויכלו
        # לשלוח ללקוח "תזכורת פגישה"/"זימון" על משימה שמעולם לא הייתה פגישה
        # אמיתית. task_type הוא ה-discriminator: 'meeting' נקבע **רק** על-ידי
        # db.confirm_task_proposal (אישור בפועל של הצעת scheduling_agent, ראו
        # שם) - לעולם לא ברירת המחדל של יצירה ידנית/NLP. בכוונה **לא** שתי
        # עמודות due_date נפרדות (follow_up_date/meeting_date) - אותה סיבה
        # בדיוק שכבר תועדה ב-api_tasks (meeting_time הנגזר): due_date/due_time
        # נשארים מקור-האמת היחיד לכל סוג, ו-task_type רק מתאר למה הם מתייחסים;
        # ה-API/ה-UI כבר מציגים אותם תחת שמות נפרדים (ראו api_calendar_agenda).
        ("task_type", "ALTER TABLE calendar_tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'followup'"),
        # מזהה חיצוני יציב למשימה שיובאה ממערכת אחרת (למשל "bmby:task:123" -
        # ראו migrate_bmby.py) - בסיס לאידמפוטנטיות של ייבוא חוזר (find_task_
        # by_external_ref). null לכל משימה שנוצרה במערכת עצמה.
        ("external_ref", "ALTER TABLE calendar_tasks ADD COLUMN external_ref TEXT"),
    ):
        if column not in existing_task_columns:
            conn.execute(ddl)
            if column == "task_type":
                # Backfill חד-פעמי לשורות היסטוריות: intent="new"/"reschedule"
                # הוא הסימן הקיים היחיד ל"זו הייתה הצעת פגישה שאושרה" (ראו
                # scheduling_agent.create_scheduling_proposal) - "cancel"
                # במכוון לא נכלל (ההצעה עצמה, אחרי אישור, היא רשומת-בוטל, לא
                # פגישה). כל שאר השורות (manual/callback/note_nlp) כבר קיבלו
                # את ברירת המחדל 'followup' מה-ALTER עצמו.
                conn.execute(
                    "UPDATE calendar_tasks SET task_type = 'meeting' WHERE intent IN ('new', 'reschedule')"
                )
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
    # מיגרציה קלה נוספת: No-Answer Call Log & Dynamic Follow-Up - תוצאת הניתוב
    # ללקוח עצמו (dial_status, ראו /voice/dial-status) נבדלת בכוונה מ-status
    # הקיימת (זו משקפת את הרגל הראשונה - הנציג - ראו /voice/status; status
    # הופכת "completed" ברגע שהנציג מנתק, בלי קשר אם הלקוח בכלל ענה). שילוב
    # שתיהן בעמודה אחת היה גורם ל-race מול /voice/status (שני callbacks של
    # Twilio על אותה שיחה, בלי סדר מובטח בין ה-UPDATE-ים).
    for column, ddl in (
        ("dial_status", "ALTER TABLE calls ADD COLUMN dial_status TEXT"),
        ("followup_scheduled_at", "ALTER TABLE calls ADD COLUMN followup_scheduled_at TEXT"),
        ("followup_sent", "ALTER TABLE calls ADD COLUMN followup_sent INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in existing_call_columns:
            conn.execute(ddl)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS blocked_contacts (
            id {_PK},
            tenant_id TEXT NOT NULL DEFAULT 'default',
            phone TEXT NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tenant_id, phone)
        )
        """
    )
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
    # נעילת runner של קמפיינים - שורה אחת לכל tenant (lock_key), כדי שרק runner אחד
    # ישלח מכל מספר WhatsApp בכל רגע, גם בין workers שונים של gunicorn (ראו
    # acquire_outbound_lock). heartbeat_at נשמר כ-TEXT ISO ב-UTC בשני הבאקאנדים.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS outbound_locks (
            id {_PK},
            lock_key TEXT NOT NULL UNIQUE,
            owner TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        )
        """
    )
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
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS customers (
            id {_PK},
            cust_key TEXT NOT NULL UNIQUE,
            data TEXT NOT NULL
        )
        """
    )
    # ---- Tenants & Feature Flags (ראו check_feature/is_feature_enabled ב-server.py/
    # db.py) - "subdomain" הוא בפועל אותו מחרוזת tenant_id שכבר בשימוש בכל שאר
    # הטבלאות (customers/messages/calls וכו') - לא דומיין DNS אמיתי בהכרח, רק
    # מפתח ה-tenant הקיים, כדי לא לדרוש שינוי בשום מקום אחר באפליקציה.
    # ⚠️ features הוא TEXT (JSON מסודרר), **לא** JSONB - בכוונה, לא השמטה: ההערה
    # בראש הקובץ הזה כבר קובעת במפורש שכל טיפוסי העמודות זהים בדיוק בין שני
    # הבאקאנדים (היחיד שונה הוא _PK) - וגם customers.data (JSON דומה) הוא TEXT,
    # לא JSONB, מאותה סיבה. JSONB אמיתי היה דורש טיפול א-סימטרי (psycopg2
    # מפענח JSONB לdict אוטומטית בקריאה, אבל *לא* מקודד dict לJSONB בכתיבה בלי
    # cast/adapter מפורש) - בדיוק אותה מחלקת באג (str/native type לפי backend)
    # שתוקנה הרגע ב-compute_lead_score (last_activity, TIMESTAMP). TEXT+json.
    # dumps/loads נשאר סימטרי ופשוט בשני הבסיסים - ראו _parse_features למטה.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS tenants (
            id {_PK},
            name TEXT,
            subdomain TEXT UNIQUE,
            features TEXT NOT NULL DEFAULT '{_DEFAULT_FEATURES_JSON}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # "default" הוא ה-tenant_id הקיים בכל שאר הפרויקט (DEFAULT_TENANT_ID ב-
    # extract.py) - נוצר כאן אוטומטית כדי שיהיה תמיד tenant אחד אמיתי וניתן-
    # לעריכה בטבלה, לא רק ברירת-המחדל ההתנהגותית (ראו get_tenant_features
    # שעדיין נופלת בעדינות לכל התכונות=True גם ל-tenant שלא קיים כלל בטבלה -
    # לא שינוי התנהגות רטרואקטיבי ל-tenant-ים קיימים שלא נרשמו).
    conn.execute(
        "INSERT INTO tenants (name, subdomain, features) VALUES (?, ?, ?) "
        "ON CONFLICT (subdomain) DO NOTHING",
        ("Default", "default", _DEFAULT_FEATURES_JSON),
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
                "SELECT id, channel, direction, message, timestamp, simulated FROM messages "
                "WHERE phone = ? AND tenant_id = ? AND timestamp > ? ORDER BY timestamp ASC, id ASC",
                (phone, tenant_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, channel, direction, message, timestamp, simulated FROM messages "
                "WHERE phone = ? AND tenant_id = ? ORDER BY timestamp ASC, id ASC",
                (phone, tenant_id),
            ).fetchall()
        # id נוסף כאן (שלב חדש) - מזהה יחיד ויציב לכל שורה, בשימוש בצד הלקוח
        # למניעת רינדור כפול (ראו renderedMessageIds ב-index.html createChatController) -
        # לפני כן ה-polling הסתמך רק על timestamp, שיכול להיות זהה בין כמה
        # שורות שנכתבו באותה מילישנייה, ולגרום להצגה כפולה/חסרה.
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


# שאילתות bulk בונות תנאי OR לכל זוג (phone, tenant_id). ב-SQLite יש תקרת עומק
# ביטוי (1000), כך שמעל ~500 לידים השאילתה נכשלה ב-"Expression tree is too large".
# מפצלים למנות - עדיין מעט round-trips, בלי תלות בגודל ה-CRM.
_BULK_PAIR_CHUNK = 300


def get_last_messages_bulk(phone_tenant_pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """כמו get_last_message, לכל הלידים *בבת-אחת* (שאילתת window function
    אחת, לא N שאילתות נפרדות) - אותה בעיית N+1 בדיוק כמו compute_lead_
    scores_bulk (ראו ההערה המלאה שם), GET /api/leads?include_last_message=1
    קרא ל-get_last_message בלולאה, ליד-ליד."""
    if not phone_tenant_pairs:
        return {}
    if len(phone_tenant_pairs) > _BULK_PAIR_CHUNK:
        merged: dict[tuple[str, str], dict] = {}
        for i in range(0, len(phone_tenant_pairs), _BULK_PAIR_CHUNK):
            merged.update(get_last_messages_bulk(phone_tenant_pairs[i:i + _BULK_PAIR_CHUNK]))
        return merged
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conditions = " OR ".join("(phone = ? AND tenant_id = ?)" for _ in phone_tenant_pairs)
        params = [v for pair in phone_tenant_pairs for v in pair]
        rows = conn.execute(
            f"SELECT phone, tenant_id, channel, direction, message, timestamp, simulated FROM ("
            f"SELECT phone, tenant_id, channel, direction, message, timestamp, simulated, "
            f"ROW_NUMBER() OVER (PARTITION BY phone, tenant_id ORDER BY timestamp DESC) AS rn "
            f"FROM messages WHERE ({conditions})"
            f") AS ranked WHERE rn = 1",
            params,
        ).fetchall()
    finally:
        conn.close()
    return {
        (r["phone"], r["tenant_id"]): {
            "channel": r["channel"], "direction": r["direction"], "message": r["message"],
            "timestamp": r["timestamp"], "simulated": bool(r["simulated"]),
        }
        for r in rows
    }


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


def mark_call_dial_outcome(call_sid: str, dial_status: str, followup_scheduled_at: str | None = None) -> dict | None:
    """מעדכן את תוצאת הניתוב בפועל ללקוח (dial_status - ראו POST /voice/dial-status,
    הערה ב-_init_schema) + מתזמן פולו-אפ בוואטסאפ אם רלוונטי (followup_scheduled_at,
    ISO UTC - ראו scheduler.compute_call_followup_time). מחזיר את השורה המעודכנת
    (phone/tenant_id - הקורא (server.py) צריך אותם כדי לעדכן lead_status באותו
    רגע) או None אם call_sid לא נמצא (קרה בעבר/שיחת בדיקה בלי שורה תואמת ב-DB)."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE calls SET dial_status = ?, followup_scheduled_at = ?, updated_at = ? WHERE call_sid = ?",
            (dial_status, followup_scheduled_at, datetime.now(timezone.utc).isoformat(), call_sid),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM calls WHERE call_sid = ?", (call_sid,)).fetchone()
        return _call_row_to_dict(row) if row else None
    finally:
        conn.close()


def get_pending_call_followups() -> list[dict]:
    """שיחות שסומנו dial_status="no_answer" (ראו mark_call_dial_outcome - הערך
    היחיד שהקוד הזה בפועל כותב ל-dial_status כש"לא נענה"; "completed" הוא הערך
    היחיד האחר) עם זמן פולו-אפ מתוזמן שעדיין לא נשלח (followup_sent=0) - לא
    מסננת לפי זמן כאן (הקורא, scheduler.check_pending_call_followups, בודק
    followup_scheduled_at מול "עכשיו" בפייתון - כך ששני הבסיסים (sqlite/postgres)
    לא צריכים להשוות מחרוזות ISO ב-SQL, שבירה בין הבסיסים)."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM calls WHERE followup_sent = 0 AND followup_scheduled_at IS NOT NULL "
            "AND dial_status = 'no_answer'"
        ).fetchall()
        return [_call_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def mark_call_followup_sent(call_id: int) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calls SET followup_sent = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), call_id),
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


# ---- רשימת אנשי קשר חסומים (Ignore List) - DB, לא env var ----
# מחליפה/משלימה את BLOCKED_CONTACTS (env var ב-server.py, גלובלי לכל ה-tenants,
# דורש redeploy לעדכן) עם רשימה פר-tenant שניתנת לניהול חי מהדשבורד (כפתור
# "🚫 חסום איש קשר" בפאנל הליד) - בלי לגעת ב-Render בכלל. שני המקורות נבדקים
# יחד ב-webhook() (ראו server.py) - לא מחליפים זה את זה, משלימים.
def add_blocked_contact(phone: str, tenant_id: str = "default", reason: str | None = None) -> None:
    """חוסם איש קשר - Upsert לפי (tenant_id, phone): קריאה חוזרת עם reason חדש
    מעדכנת את ה-reason הקיים, לא יוצרת שורה כפולה."""
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO blocked_contacts (tenant_id, phone, reason) VALUES (?, ?, ?) "
            "ON CONFLICT (tenant_id, phone) DO UPDATE SET reason = EXCLUDED.reason",
            (tenant_id, phone, reason),
        )
        conn.commit()
    finally:
        conn.close()


def remove_blocked_contact(phone: str, tenant_id: str = "default") -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "DELETE FROM blocked_contacts WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone),
        )
        conn.commit()
    finally:
        conn.close()


def get_blocked_contacts(tenant_id: str | None = None) -> list[dict]:
    """כל אנשי הקשר החסומים - בלי tenant_id מחזיר את כולם (לתצוגת אדמין),
    עם tenant_id מסנן לעסק ספציפי - בדיוק כמו get_tasks."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM blocked_contacts WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM blocked_contacts ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def is_contact_blocked(phone: str, tenant_id: str = "default") -> bool:
    """נבדק ב-webhook() לכל הודעה נכנסת (ראו server.py) - לפני כל עיבוד אחר,
    בדיוק כמו BLOCKED_CONTACTS (env var). שאילתה קלה (אינדקס UNIQUE על
    tenant_id+phone כבר קיים מה-constraint בטבלה)."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM blocked_contacts WHERE tenant_id = ? AND phone = ?", (tenant_id, phone)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_tenant_features(tenant_id: str) -> dict:
    """מחזיר את dict התכונות עבור tenant (מפתח tenants.subdomain, לא tenants.id -
    זה אותו tenant_id string שכל שאר הטבלאות משתמשות בו). tenant שלא רשום בטבלת
    tenants בכלל (כל ה-tenant_id-ים הקיימים טרום הפיצ'ר הזה) מקבל בחזרה את
    ברירת המחדל המלאה (הכל True) - כדי לא לשנות בדיעבד התנהגות לאף tenant
    שלא נרשם במפורש, רק tenant שכן רשום וכן הוגדר עם features=false מפסיק
    לקבל את ההתנהגות הזו."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT features FROM tenants WHERE subdomain = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            return dict(_DEFAULT_FEATURES)
        try:
            features = json.loads(row["features"])
        except (TypeError, ValueError):
            return dict(_DEFAULT_FEATURES)
        merged = dict(_DEFAULT_FEATURES)
        merged.update(features)
        return merged
    finally:
        conn.close()


def is_feature_enabled(tenant_id: str, feature_name: str) -> bool:
    """נבדק ב-check_feature (server.py) ובwebhook() לבדיקת whatsapp_ai - ברירת
    מחדל True עבור feature_name שלא קיים כלל ב-dict (עקבי עם get_tenant_features)."""
    return get_tenant_features(tenant_id).get(feature_name, True)


def set_tenant_feature(tenant_id: str, feature_name: str, enabled: bool) -> dict:
    """מעדכן תכונה בודדת עבור tenant (יוצר את שורת ה-tenant אם לא קיימת עדיין -
    upsert, בדיוק כמו set_blocked_contact) ומחזיר את ה-dict המלא המעודכן."""
    features = get_tenant_features(tenant_id)
    features[feature_name] = enabled
    features_json = json.dumps(features)
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO tenants (name, subdomain, features) VALUES (?, ?, ?) "
            "ON CONFLICT (subdomain) DO UPDATE SET features = EXCLUDED.features, "
            "updated_at = CURRENT_TIMESTAMP",
            (tenant_id, tenant_id, features_json),
        )
        conn.commit()
        return features
    finally:
        conn.close()


def set_tenant_features(tenant_id: str, updates: dict) -> dict:
    """כמו set_tenant_feature, אבל מעדכנת כמה דגלים בבת-אחת בכתיבה אחת (לא
    לולאת set_tenant_feature שהייתה עושה read-modify-write נפרד לכל מפתח) -
    ל-PUT /api/tenant/features (server.py), שמקבל dict שלם של שינויים מה-UI
    בבקשה אחת. updates עובר merge על גבי הדגלים הקיימים, לא מחליף את כולם -
    tenant ששינה רק whatsapp_ai לא מאבד בטעות את telephony_queue הקיים שלו."""
    features = get_tenant_features(tenant_id)
    features.update(updates)
    features_json = json.dumps(features)
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO tenants (name, subdomain, features) VALUES (?, ?, ?) "
            "ON CONFLICT (subdomain) DO UPDATE SET features = EXCLUDED.features, "
            "updated_at = CURRENT_TIMESTAMP",
            (tenant_id, tenant_id, features_json),
        )
        conn.commit()
        return features
    finally:
        conn.close()


def get_all_tenants() -> list[dict]:
    """כל שורות tenants (id/name/subdomain/features-מפוענח) - ל-GET
    /api/admin/tenants (אדמין-בלבד, בדיוק כמו get_users) - מאפשר לאדמין לבחור
    איזה tenant לנהל בלשונית ההגדרות, בלי להכיר מראש את כל ה-subdomain-ים."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, name, subdomain, features FROM tenants ORDER BY subdomain").fetchall()
        result = []
        for row in rows:
            try:
                features = json.loads(row["features"])
            except (TypeError, ValueError):
                features = dict(_DEFAULT_FEATURES)
            result.append({"id": row["id"], "name": row["name"], "subdomain": row["subdomain"], "features": features})
        return result
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
    task_type: str = "followup",
    external_ref: str | None = None,
) -> int:
    """יוצר משימת מעקב/תזכורת (follow-up) לליד, או הצעת-פגישה אוטומטית (ראו
    scheduling_agent.create_scheduling_proposal - status="pending_confirmation",
    source="scheduling_agent"). מוחזר ה-id. ברירות המחדל (status="pending",
    source="manual", task_type="followup") שומרות על ההתנהגות המקורית המדויקת
    לכל קורא קיים (POST /api/tasks) - לא נדרש שינוי בקוד שכבר קורא לפונקציה
    הזו. task_type="meeting" נקבע בפועל **רק** דרך confirm_task_proposal
    (אישור בפועל) - לא כאן ביצירת ההצעה עצמה (שעדיין pending_confirmation,
    לא פגישה אמיתית)."""
    conn = _get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO calendar_tasks "
            "(tenant_id, phone, title, due_date, due_time, notes, status, source, intent, related_task_id, task_type, "
            "external_ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, phone, title, due_date, due_time, notes, status, source, intent, related_task_id, task_type,
             external_ref, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def find_task_by_external_ref(tenant_id: str, external_ref: str) -> dict | None:
    """משימה לפי (tenant_id, external_ref) - לאידמפוטנטיות של ייבוא ממערכת
    חיצונית (migrate_bmby.py). **קריאה בלבד ובלי שום שינוי סכימה**: בכוונה
    _connect_raw ולא _get_connection (שמריץ _init_schema = CREATE/ALTER) -
    כדי ש-dry-run של מיגרציה לא ייצור את crm_data.db/את העמודה. אם הקובץ/
    הטבלה/העמודה עוד לא קיימים - אין משימה כזו (None)."""
    if not external_ref:
        return None
    if not IS_POSTGRES and not DB_FILE.exists():
        return None
    conn = _connect_raw()
    try:
        if "external_ref" not in _existing_columns(conn, "calendar_tasks"):
            return None
        if not IS_POSTGRES:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM calendar_tasks WHERE tenant_id = ? AND external_ref = ? ORDER BY id ASC",
            (tenant_id, external_ref),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_task_type(task_id: int, task_type: str) -> None:
    """מסמנת משימה כ-'meeting' (פגישה רשמית מול הלקוח) לעומת 'followup'
    (ברירת המחדל - תזכורת פנימית לעצמנו). נקראת **רק** מתוך confirm_task_
    proposal, ברגע שהצעת scheduling_agent הופכת בפועל לפגישה אמיתית/מעדכנת
    פגישה קיימת - לעולם לא על יצירה ידנית/NLP (ראו ההערה המלאה ב-_init_schema
    ליד task_type)."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calendar_tasks SET task_type = ?, updated_at = ? WHERE id = ?",
            (task_type, datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
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
    - "reschedule", או "new" כש-related_task_id קיים (ראו ההערה למטה): מעדכנת
      את due_date/due_time של הפגישה המקורית לערכים שהוצעו (ששמורים על שורת
      ההצעה עצמה) - ורק אז מסמנת את ההצעה כ-'done'. "new" מטופל כאן **בדיוק**
      כמו "reschedule" ברגע שיש related_task_id: לליד אחד יש לכל היותר פגישה
      פתוחה אחת בכל רגע נתון (ראו find_related_task/scheduling_agent.py) -
      אם הודעה חדשה מכריזה על פגישה כש-*כבר* יש פגישה פתוחה לליד הזה, מדובר
      בהגדרה בעדכון אותה פגישה, לא בפגישה נוספת בלתי-תלויה, גם אם הניסוח
      בהודעה עצמה לא הזכיר במפורש "הזזה"/"שינוי" (למשל "נקבעה פגישה ל-16:00"
      כשכבר קיימת פגישה פתוחה ל-11:00 אותו ליד) - אחרת נוצרת פגישה כפולה:
      הישנה (השגויה) נשארת פתוחה במקביל לחדשה (הנכונה), ושתיהן מוצגות
      ב-UI/ביומן - ה-UI המציג "הפגישה הקרובה" (ממוין due_date/due_time עולה,
      ראו db.get_tasks) מציג בפועל את הישנה/השגויה כי היא כרונולוגית קודמת
      באותו יום, בזמן שהודעת האישור בוואטסאפ שנשלחה בפועל (built from the
      proposal row עצמה) כבר הציגה את הזמן הנכון - בדיוק התקלה שדווחה.
    - אחרת ("new" בלי related_task_id תקין, או הצעת cancel/reschedule בלי
      related_task_id תקין) - שורת ההצעה עצמה הופכת לפגישה אמיתית (status=
      'pending' רגיל)."""
    proposal = get_task(task_id)
    if not proposal or proposal["status"] != "pending_confirmation":
        return None

    intent = proposal.get("intent")
    related_id = proposal.get("related_task_id")
    if intent == "cancel" and related_id and get_task(related_id):
        update_task_status(related_id, "cancelled")
        update_task_status(task_id, "done")
    elif intent in ("reschedule", "new") and related_id and get_task(related_id):
        update_task_datetime(related_id, proposal["due_date"], proposal["due_time"])
        # task_type="meeting" גם אם הפגישה המקורית נוצרה ידנית לפני התוספת הזו
        # (ללא task_type מפורש, default="followup") - ברגע שהיא מאושרת/מוזזת
        # דרך זרימת scheduling_agent היא בהגדרה פגישה רשמית מול הלקוח.
        set_task_type(related_id, "meeting")
        update_task_status(task_id, "done")
    else:
        update_task_status(task_id, "pending")
        set_task_type(task_id, "meeting")

    return get_task(task_id)


def reject_task_proposal(task_id: int) -> dict | None:
    """דוחה הצעת פגישה שזוהתה אוטומטית - לא נוגעת בשום פגישה קיימת
    (related_task_id, אם יש, נשאר בדיוק כפי שהיה); רק מסמנת את שורת ההצעה
    עצמה כ-'cancelled'. מחזיר None אם ההצעה לא נמצאה/כבר לא pending_confirmation."""
    proposal = get_task(task_id)
    if not proposal or proposal["status"] != "pending_confirmation":
        return None
    return update_task_status(task_id, "cancelled")


def mark_task_reminder_sent(task_id: int) -> None:
    """מסמן ששליחת תזכורת הפגישה בוואטסאפ בוצעה (dry-run או בפועל) - ראו
    scheduler.check_pending_meeting_reminders. דגל חד-פעמי, לא נבדק מול זמן
    (בניגוד ל-followup_sent ב-calls, שם יש followup_scheduled_at נפרד - כאן
    הקורא מחשב reminder time בכל סריקה מ-due_date/due_time, אין צורך לשמור
    זמן מתוזמן נפרד)."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calendar_tasks SET reminder_sent = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_invite_sent(task_id: int) -> None:
    """מסמן שהודעת הזימון (invite - כתובת/מיקום + מי לפגוש, יום לפני הפגישה)
    נשלחה - ראו scheduler.check_pending_meeting_invites. דגל חד-פעמי."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calendar_tasks SET invite_sent = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_marketer_reminder_created(task_id: int) -> None:
    """מסמן שמשימת "📞 אישור הגעה" הפנימית למשווק נוצרה בפועל - ראו
    scheduler.check_pending_marketer_reminders (היא היוצרת גם את שורת המשימה
    החדשה וגם קוראת לפונקציה הזו, על שורת הפגישה המקורית)."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calendar_tasks SET marketer_reminder_status = 'created', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


_VALID_ATTENDANCE_STATUSES = ("confirmed", "no_answer", "declined")


def set_attendance_status(task_id: int, status: str) -> dict | None:
    """מסמן את תוצאת שיחת אישור ההגעה (confirmed/no_answer/declined) על שורת
    **הפגישה המקורית** - נקרא מ-POST /api/tasks/<id>/attendance (server.py),
    שמפנה לכאן את related_task_id אם הקריאה בפועל הגיעה משורת משימת האישור
    (לא מהפגישה עצמה - ראו שם). מחזיר את השורה המעודכנת, או None אם status
    לא תקין/המשימה לא נמצאה - לא כותב כלום במקרה הזה."""
    if status not in _VALID_ATTENDANCE_STATUSES:
        return None
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE calendar_tasks SET attendance_status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM calendar_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_booking_confirmation_sent(task_id: int) -> None:
    """מסמן שהודעת אישור קביעת הפגישה (Automated Meeting Booking Confirmation)
    נשלחה - ראו server.api_task_confirm/_send_meeting_confirmation_after_delay.
    דגל חד-פעמי, בדיוק כמו invite_sent."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE calendar_tasks SET booking_confirmation_sent = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_tasks(
    phone: str | None = None, tenant_id: str | None = None, status: str | None = None,
    task_type: str | None = None,
) -> list[dict]:
    """משימות מעקב. עם phone - רק המשימות של הליד הזה (לפאנל "📅 משימות" - כל סטטוס).
    בלי phone - כל המשימות (לתצוגת "📅 יומן" הגלובלית), עם סינון אופציונלי לפי
    tenant_id/status/task_type ('meeting'/'followup' - ראו ההערה ב-_init_schema).
    ממוין לפי due_date/due_time עולה (הקרוב ביותר קודם) - בכוונה שונה מכל שאר
    הטבלאות בקובץ הזה (שממוינות לפי זמן יצירה): כאן מה שחשוב הוא מתי המשימה
    אמורה לקרות, לא מתי היא נוצרה."""
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
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
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


def compute_lead_scores_bulk(phone_tenant_pairs: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """מחשב "ציון חום" (1-100) לכל הלידים הנתונים *בבת-אחת* - 4 שאילתות
    סה"כ, לא 4×N. ⚠️ תיקון ביצועים קריטי (Performance Sprint): לפני זה היה
    כאן compute_lead_score שחושב ציון ל*ליד בודד* וקראה ל-4 שאילתות משל
    עצמה - GET /api/leads?include_score=1 (ראו server.py) קרא לה בלולאה,
    ליד-ליד. עם עשרות לידים ולזמן-תגובה אמיתי מרשת ל-Postgres production
    (~0.7-1.5 שניות *לכל שאילתה בודדת*, נמדד בפועל - ראו ההיסטוריה), 4
    שאילתות × N לידים הפכו את טעינת טבלת הלידים לכמה *עשרות שניות ועד כמה
    דקות* - נראה בדיוק כמו "תקוע בלופ" בפרונט, אבל זה N+1 queries אמיתי
    בבקאנד, לא באג בלוגיקת ה-polling. compute_lead_score הוסרה כליל - אין
    עוד קורא יחיד-ליד, רק זו.

    משקלים: הודעות פולו-אפ אוטומטיות (channel IN FOLLOWUP_CHANNELS -
    'followup'/'followup_2'/'followup_3'/'followup_final', 4 השלבים - ראו
    scheduler.check_pending_followups) מוחרגות בכוונה גם ממניין ההודעות וגם
    מ"עדכניות" (recency): אלו פניות **שלנו** יזמנו כלפי ליד ששקט, לא פעילות/
    מעורבות אמיתית שלו. הרשימה מוגדרת כאן (FOLLOWUP_CHANNELS בראש הקובץ)
    ו-scheduler._FOLLOWUP_CHANNELS מפנה אליה - מקור-אמת יחיד, בלי מעגליות.

    בונה תנאי OR לכל זוג (phone, tenant_id) - לא IN-על-זוגות (לא נתמך
    בפשטות בשני הבאקאנדים כאחד), אבל עדיין 4 round-trips בלבד בלי קשר
    לכמות הלידים. סביר-בבירור לגודל CRM אמיתי (עשרות-מאות לידים), לא
    מיועד לאלפים."""
    if not phone_tenant_pairs:
        return {}
    if len(phone_tenant_pairs) > _BULK_PAIR_CHUNK:
        merged: dict[tuple[str, str], int] = {}
        for i in range(0, len(phone_tenant_pairs), _BULK_PAIR_CHUNK):
            merged.update(compute_lead_scores_bulk(phone_tenant_pairs[i:i + _BULK_PAIR_CHUNK]))
        return merged
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conditions = " OR ".join("(phone = ? AND tenant_id = ?)" for _ in phone_tenant_pairs)
        params = [v for pair in phone_tenant_pairs for v in pair]

        message_rows = conn.execute(
            f"SELECT phone, tenant_id, COUNT(*) AS cnt FROM messages "
            f"WHERE ({conditions}) AND channel NOT IN {_FOLLOWUP_CHANNELS_SQL} "
            f"GROUP BY phone, tenant_id",
            params,
        ).fetchall()
        call_rows = conn.execute(
            f"SELECT phone, tenant_id, COUNT(*) AS cnt FROM calls WHERE ({conditions}) GROUP BY phone, tenant_id",
            params,
        ).fetchall()
        task_rows = conn.execute(
            f"SELECT phone, tenant_id, COUNT(*) AS cnt FROM calendar_tasks "
            f"WHERE ({conditions}) AND status = 'done' GROUP BY phone, tenant_id",
            params,
        ).fetchall()
        last_activity_rows = conn.execute(
            f"SELECT phone, tenant_id, MAX(ts) AS last_ts FROM ("
            f"SELECT phone, tenant_id, timestamp AS ts FROM messages "
            f"WHERE ({conditions}) AND channel NOT IN {_FOLLOWUP_CHANNELS_SQL} "
            f"UNION ALL "
            f"SELECT phone, tenant_id, created_at AS ts FROM calls WHERE ({conditions}) "
            f"UNION ALL "
            f"SELECT phone, tenant_id, updated_at AS ts FROM calendar_tasks WHERE ({conditions})"
            f") AS combined GROUP BY phone, tenant_id",
            params * 3,
        ).fetchall()
    finally:
        conn.close()

    message_counts = {(r["phone"], r["tenant_id"]): r["cnt"] for r in message_rows}
    call_counts = {(r["phone"], r["tenant_id"]): r["cnt"] for r in call_rows}
    task_counts = {(r["phone"], r["tenant_id"]): r["cnt"] for r in task_rows}
    last_activities = {(r["phone"], r["tenant_id"]): r["last_ts"] for r in last_activity_rows}

    scores: dict[tuple[str, str], int] = {}
    for pair in phone_tenant_pairs:
        score = (
            min(message_counts.get(pair, 0) * _SCORE_MESSAGE_POINTS, _SCORE_MESSAGE_CAP)
            + min(call_counts.get(pair, 0) * _SCORE_CALL_POINTS, _SCORE_CALL_CAP)
            + min(task_counts.get(pair, 0) * _SCORE_TASK_POINTS, _SCORE_TASK_CAP)
        )
        last_activity = last_activities.get(pair)
        if last_activity:
            last_activity_dt = last_activity if isinstance(last_activity, datetime) else datetime.fromisoformat(last_activity)
            if last_activity_dt.tzinfo is None:
                last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - last_activity_dt).days
            for max_age, points in _SCORE_RECENCY_BANDS:
                if age_days <= max_age:
                    score += points
                    break
        scores[pair] = max(1, min(100, score))
    return scores


def search_activity(query: str) -> list[tuple[str, str]]:
    """מחפש טקסט חופשי בתוכן פעילות (לא בשדות הכרטיס עצמו - אלה נבדקים בנפרד ב-
    server.py מ-customers.json) - טקסט הודעות, הערות/תקציר שיחות, וכותרת/הערות
    משימות. מחזיר רשימת (phone, tenant_id) ייחודיים שיש בהם התאמה - לשימוש
    GET /api/search, כחלק מהחיפוש החופשי הרב-שדות בדשבורד."""
    like = f"%{query}%"
    conn = _get_connection()
    try:
        # row_factory מפורש + גישה לפי שם עמודה - בלעדיו [0]/[1] מעלה KeyError
        # על Postgres (RealDictCursor) - ראו ההערה המלאה ב-compute_lead_score,
        # אותו באג בדיוק (לא נתפס קודם כי /api/search לא נבדק מול Postgres אמיתי).
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT phone, tenant_id FROM messages WHERE message LIKE ? "
            "UNION "
            "SELECT DISTINCT phone, tenant_id FROM calls WHERE notes LIKE ? OR summary LIKE ? "
            "UNION "
            "SELECT DISTINCT phone, tenant_id FROM calendar_tasks WHERE title LIKE ? OR notes LIKE ?",
            (like, like, like, like, like),
        ).fetchall()
        return [(row["phone"], row["tenant_id"]) for row in rows]
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
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
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


def claim_outbound_item(item_id: int, to_status: str = "sending", from_status: str = "queued") -> bool:
    """מעבר סטטוס אטומי (compare-and-set) לפריט בתור: UPDATE ... WHERE id=? AND
    status=from_status. מחזיר True רק אם בדיוק שורה אחת השתנתה - כלומר הקורא
    הזה "זכה" בפריט. מונע שליחה כפולה כששני runners (קליק כפול על start, טאב
    נוסף, שני workers של gunicorn) מעבדים את אותו קמפיין במקביל: רק אחד מהם
    יצליח להעביר queued→sending. עובד זהה ב-SQLite וב-Postgres (rowcount של
    UPDATE; _PGConnection מוסיף RETURNING רק ל-INSERT)."""
    conn = _get_connection()
    try:
        cur = conn.execute(
            "UPDATE outbound_queue SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (to_status, datetime.now(timezone.utc).isoformat(), item_id, from_status),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def campaign_is_running(tenant_id: str, campaign_id: str) -> bool:
    """True אם יש בקמפיין פריט כלשהו בסטטוס 'sending' (runner פעיל באמצע שליחה) -
    לשימוש ה-server כדי לא להתחיל runner נוסף. ההגנה האמיתית מפני שליחה כפולה
    היא claim_outbound_item; זו רק בדיקה ל-UX. פריט 'sending' שלא עודכן יותר מ-
    STALE_SENDING_SECONDS (runner שקרס באמצע) לא נחשב "רץ" - אחרת הקמפיין היה
    נחסם ב-409 לתמיד. הפריט עצמו נשאר 'sending' ולא יישלח שוב (בכוונה)."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT status, updated_at FROM outbound_queue WHERE tenant_id = ? AND campaign_id = ?",
            (tenant_id, campaign_id),
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc)

    def _age_seconds(value) -> float | None:
        try:
            ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()

    has_pending = any(r["status"] in ("queued", "skipped_quota") for r in rows)
    for row in rows:
        age = _age_seconds(row["updated_at"])
        if row["status"] == "sending":
            if age is None or age < STALE_SENDING_SECONDS:
                return True  # גיל לא ידוע - עדיף "רץ" (409) מאשר להתיר runner נוסף
        elif has_pending and row["status"] != "queued" and age is not None and age < RUN_IDLE_SECONDS:
            # runner בהשהיית Anti-Ban בין פריטים (אין כרגע פריט ב-sending): פריט טופל
            # לאחרונה ועדיין יש פריטים ממתינים. בלי זה, worker שני של gunicorn היה
            # מתחיל runner מקביל ומקצר את המרווח בין הודעות.
            return True
    return False


# runner תקין מעדכן כל פריט תוך שניות (ההשהיה של 45-90 שנ' היא *בין* פריטים, כשאין פריט ב-sending)
STALE_SENDING_SECONDS = int(os.environ.get("OUTBOUND_STALE_SENDING_SECONDS", "600"))
# runner נחשב פעיל עד דקה אחרי ההשהיה המקסימלית בין פריטים (OUTBOUND_MAX_DELAY_SECONDS)
RUN_IDLE_SECONDS = int(os.environ.get("OUTBOUND_MAX_DELAY_SECONDS", "90")) + 60


def acquire_outbound_lock(lock_key: str, owner: str, stale_seconds: int) -> bool:
    """נעילה בין-תהליכית (גם בין workers של gunicorn) ל-runner של קמפיין. מחזיר
    True אם owner מחזיק בנעילה: נוצרה עכשיו, כבר שלו, או שהקודמת "מתה" (אין
    heartbeat יותר מ-stale_seconds - runner שקרס). אחרת False."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    stale_before = (now - timedelta(seconds=stale_seconds)).isoformat()
    conn = _get_connection()
    try:
        cur = conn.execute(
            "UPDATE outbound_locks SET owner = ?, heartbeat_at = ? "
            "WHERE lock_key = ? AND (owner = ? OR heartbeat_at < ?)",
            (owner, now_iso, lock_key, owner, stale_before),
        )
        conn.commit()
        if cur.rowcount == 1:
            return True
        try:
            conn.execute(
                "INSERT INTO outbound_locks (lock_key, owner, heartbeat_at) VALUES (?, ?, ?)",
                (lock_key, owner, now_iso),
            )
            conn.commit()
            return True
        except IntegrityError:
            return False  # runner אחר חי ומחזיק בנעילה
    finally:
        conn.close()


def heartbeat_outbound_lock(lock_key: str, owner: str) -> bool:
    """מרענן את הנעילה (נקרא לאורך ההשהיות). False = הנעילה כבר לא שלנו."""
    conn = _get_connection()
    try:
        cur = conn.execute(
            "UPDATE outbound_locks SET heartbeat_at = ? WHERE lock_key = ? AND owner = ?",
            (datetime.now(timezone.utc).isoformat(), lock_key, owner),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def release_outbound_lock(lock_key: str, owner: str) -> None:
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM outbound_locks WHERE lock_key = ? AND owner = ?", (lock_key, owner))
        conn.commit()
    finally:
        conn.close()


def outbound_lock_active(lock_key: str, stale_seconds: int) -> bool:
    """True אם runner חי מחזיק בנעילה (heartbeat טרי) - לבדיקת ה-server לפני start."""
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM outbound_locks WHERE lock_key = ? AND heartbeat_at >= ?",
            (lock_key, stale_before),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def last_outbound_sent_at(tenant_id: str) -> datetime | None:
    """זמן השליחה האחרונה בפועל של קמפיין מה-tenant הזה (כל הקמפיינים) - בסיס
    לאכיפת המרווח Anti-Ban לפני כל הודעה, גם מעבר לגבולות runner/קמפיין."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(sent_at) AS last FROM outbound_queue WHERE tenant_id = ? AND status IN ('sent', 'simulated')",
            (tenant_id,),
        ).fetchone()
    finally:
        conn.close()
    value = row["last"] if row else None
    if not value:
        return None
    try:
        ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def count_sent_today(tenant_id: str) -> int:
    """כמה הודעות outbound נשלחו בפועל היום (UTC) עבור ה-tenant הזה - sent+
    simulated נספרים יחד (שתיהן "ניסיון שליחה אמיתי" מבחינת קצב/מכסה, בדיוק
    כמו שההחייאה כבר עושה - simulated ספציפית כשל-ספק ידוע, לא כשל-קוד).
    outbound_engine.DAILY_QUOTA נאכף מול המספר הזה, לא מול total_rows בתור."""
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM outbound_queue "
            # CAST: ב-Postgres sent_at הוא TIMESTAMP, ו-LIKE ישיר עליו נכשל
            # ("operator does not exist: timestamp ~~ unknown") - הפיל כל קמפיין
            # כבר בפריט הראשון. ב-SQLite זה TEXT ממילא - ה-CAST לא משנה דבר.
            "WHERE tenant_id = ? AND status IN ('sent', 'simulated') AND CAST(sent_at AS TEXT) LIKE ?",
            (tenant_id, f"{today}%"),
        ).fetchone()
        return row["cnt"] if row else 0
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


# ---- customers (כרטיסי לידים) - חלופה ל-customers.json כש-DATABASE_URL מוגדר ----
# כל שורה = כרטיס אחד (מפתח tenant::phone + JSON של הכרטיס). כתיבה שוטפת היא
# פר-כרטיס ואטומית (update_customer_row/move_customer_row/delete_customer_row -
# ראו extract.update_card): gunicorn מריץ כמה workers + threads ברקע, וסנכרון של
# כל המילון (load הכל → שינוי כרטיס אחד → save הכל) דרס עדכונים מקביליים לכרטיסים
# אחרים ומחק כרטיסים שנוצרו בינתיים. upsert_customers_rows (bulk) נשאר לסקריפטים
# בלבד, ולעולם לא מוחק.

def load_customers_rows() -> dict:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT cust_key, data FROM customers").fetchall()
        return {row["cust_key"]: json.loads(row["data"]) for row in rows}
    finally:
        conn.close()


def _card_payload(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False, sort_keys=True)


def _begin_card_transaction(conn) -> None:
    """פותח טרנזקציה לכתיבה פר-כרטיס. Postgres: psycopg2 פותח טרנזקציה אוטומטית
    בשאילתה הראשונה (autocommit כבוי) - הנעילה עצמה היא SELECT ... FOR UPDATE.
    SQLite (רק אם DATABASE_URL מוגדר לא-postgres - לא תצורה רגילה): BEGIN IMMEDIATE
    נועל את הקובץ לכתיבה מראש, כי ל-SQLite אין FOR UPDATE."""
    if not IS_POSTGRES:
        conn.execute("BEGIN IMMEDIATE")


def _select_card_for_update(conn, cust_key: str):
    lock_clause = " FOR UPDATE" if IS_POSTGRES else ""
    return conn.execute(
        f"SELECT data FROM customers WHERE cust_key = ?{lock_clause}", (cust_key,)
    ).fetchone()


def update_customer_row(cust_key: str, mutate_fn, create: bool = False):
    """עדכון אטומי של כרטיס **אחד** בלבד (שורה אחת), בטרנזקציה אחת:
    SELECT ... FOR UPDATE (נעילת השורה עד commit) → mutate_fn(card) → כתיבה חזרה
    של אותה שורה בלבד. שום שורה אחרת בטבלה לא נקראת/נכתבת/נמחקת - שני workers/
    threads שמעדכנים כרטיסים *שונים* לא דורסים זה את זה, ושניים שמעדכנים את
    *אותו* כרטיס מסודרים בתור (השני מחכה לנעילה ורואה את הגרסה שהראשון כתב).

    mutate_fn(card: dict | None) -> dict | None: מקבל את הכרטיס הנוכחי (None אם
    לא קיים) ומחזיר את הכרטיס לכתיבה, או None = לא לכתוב כלום. חריגה מתוך
    mutate_fn = rollback, שום שינוי. מחזיר את מה ש-mutate_fn החזיר.

    create=True ושורה לא קיימת: מכניסים קודם שורת placeholder (INSERT ... ON
    CONFLICT DO NOTHING) כדי לנעול גם את *יצירת* הכרטיס - אם thread/worker אחר
    יוצר את אותו מפתח במקביל, ה-INSERT שלנו מחכה ל-commit שלו, ואז אנחנו קוראים
    מחדש (FOR UPDATE) ומפעילים את mutate_fn על הכרטיס שהוא יצר, במקום לדרוס אותו.
    ה-placeholder לעולם לא נראה לקוראים אחרים (לא committed עד הכתיבה הסופית,
    ונעלם ב-rollback)."""
    conn = _get_connection()
    try:
        _begin_card_transaction(conn)
        row = _select_card_for_update(conn, cust_key)
        if row is None and create:
            cur = conn.execute(
                "INSERT INTO customers (cust_key, data) VALUES (?, ?) ON CONFLICT (cust_key) DO NOTHING",
                (cust_key, "{}"),
            )
            if cur.rowcount == 0:
                # מישהו אחר יצר את הכרטיס בינתיים (ו-commit) - עובדים על הגרסה שלו
                row = _select_card_for_update(conn, cust_key)
        current_payload = row["data"] if row is not None else None
        current = json.loads(current_payload) if current_payload is not None else None
        result = mutate_fn(current)
        if result is None:
            return None  # בלי commit - close() עושה rollback (כולל ה-placeholder אם נוצר)
        payload = _card_payload(result)
        if payload != current_payload:
            conn.execute(
                "INSERT INTO customers (cust_key, data) VALUES (?, ?) "
                "ON CONFLICT (cust_key) DO UPDATE SET data = EXCLUDED.data",
                (cust_key, payload),
            )
        conn.commit()
        return result
    finally:
        conn.close()  # בלי commit (חריגה) = rollback (ראו _PGConnection.close / sqlite3)


class CustomerKeyConflict(Exception):
    """move_customer_row: מפתח היעד כבר שייך לכרטיס קיים אחר."""


def move_customer_row(old_key: str, new_key: str, mutate_fn) -> dict:
    """העברת כרטיס בודד ממפתח למפתח (rekey) בטרנזקציה אחת: נועל את השורה הישנה,
    mutate_fn(card) -> card (למשל עדכון phone), מכניס תחת new_key ומוחק את old_key.
    KeyError אם old_key לא קיים; CustomerKeyConflict אם new_key כבר קיים (כולל
    יצירה מקבילית - ה-INSERT הרגיל (בלי ON CONFLICT) נכשל על ה-UNIQUE). לא נוגע
    בשום שורה אחרת. עמודת phone בטבלאות הלוג - באחריות הקורא (rekey_phone)."""
    conn = _get_connection()
    try:
        _begin_card_transaction(conn)
        row = _select_card_for_update(conn, old_key)
        if row is None:
            raise KeyError(old_key)
        if _select_card_for_update(conn, new_key) is not None:
            raise CustomerKeyConflict(new_key)
        card = mutate_fn(json.loads(row["data"]))
        try:
            conn.execute(
                "INSERT INTO customers (cust_key, data) VALUES (?, ?)", (new_key, _card_payload(card))
            )
        except IntegrityError as exc:
            raise CustomerKeyConflict(new_key) from exc
        conn.execute("DELETE FROM customers WHERE cust_key = ?", (old_key,))
        conn.commit()
        return card
    finally:
        conn.close()


def delete_customer_row(cust_key: str) -> bool:
    """מוחק שורת כרטיס אחת בלבד. משמש רק את extract.delete_lead (המחיקה המפורשת
    הקיימת) - לא נתיב מחיקה חדש. True אם נמחקה שורה בפועל."""
    conn = _get_connection()
    try:
        cur = conn.execute("DELETE FROM customers WHERE cust_key = ?", (cust_key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def upsert_customers_rows(customers: dict) -> dict:
    """שמירה בכמות (bulk) - מעדכן/מוסיף רק שורות שהתוכן שלהן שונה מהמסד.
    **לעולם לא מוחק** שורות שחסרות במילון (בעבר replace_customers_rows כן מחק -
    כרטיס שנוצר ע"י worker אחר בין load ל-save היה נמחק). עדיין חשוף ל-lost
    update על כרטיסים שבמילון (ערך ישן דורס חדש) - לקוד שוטף להשתמש ב-
    update_customer_row. מחזיר {upserted, deleted=0}."""
    conn = _get_connection()
    try:
        existing = {
            row["cust_key"]: row["data"]
            for row in conn.execute("SELECT cust_key, data FROM customers").fetchall()
        }
        upserted = 0
        for key, card in customers.items():
            payload = _card_payload(card)
            if existing.get(key) == payload:
                continue
            conn.execute(
                "INSERT INTO customers (cust_key, data) VALUES (?, ?) "
                "ON CONFLICT (cust_key) DO UPDATE SET data = EXCLUDED.data",
                (key, payload),
            )
            upserted += 1
        conn.commit()
        return {"upserted": upserted, "deleted": 0}
    finally:
        conn.close()


def replace_customers_rows(customers: dict) -> dict:
    """תאימות לאחור בלבד - **כבר לא מוחק** שורות (ראו upsert_customers_rows).
    השם נשמר כדי שסקריפטים ישנים לא יישברו; מחיקה רק דרך delete_customer_row."""
    return upsert_customers_rows(customers)
