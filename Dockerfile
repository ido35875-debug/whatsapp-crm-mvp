# AI CRM (whatsapp-crm-mvp) - Production image.
#
# שני תהליכים נפרדים (ראו Procfile/CLAUDE.md) חולקים את אותה תמונה: השרת עצמו
# (Gunicorn, כמה workers) ו-"clock" (scheduler_worker.py, instance יחיד) - חובה
# process נפרד ל-scheduler כי thread-בתוך-Gunicorn-worker היה רץ כמה עותקים
# מקבילים (אחד לכל worker) ושולח הודעות כפולות/משולשות אם SCHEDULER_AUTO_SEND
# מופעל. ה-CMD למטה מריץ את ה-web process כברירת מחדל - ה-worker מופעל בנפרד
# דרך docker-compose.yml (command: python scheduler_worker.py).
#
# --- שלב 4 (Cloud Production Ready) ---
# DATABASE_URL (postgres://... / postgresql://...) - כשמוגדר, db.py מתחבר
# ל-PostgreSQL במקום ל-sqlite המקומי תחת /data (ראו db.py למעלה). לא מוגדר =
# בדיוק ההתנהגות הקיימת (sqlite, קובץ יחיד על ה-volume). RATELIMIT_STORAGE_URI
# (ברירת מחדל memory://, ראו server.py) - ל-production מרובה-workers/instances
# אמיתי, מגדירים redis://... כדי שכל ה-workers ישתפו מונה rate-limit אחד
# (אותו טרייד-אוף בדיוק כמו SSE/תור ה-webhook, ראו server.py).
FROM python:3.11-slim

WORKDIR /app

# תלויות Python בשכבה נפרדת מהקוד - cache יעיל יותר (שינוי בקוד לא מפיל את
# שכבת ה-pip install). כל התלויות הישירות הן חבילות Python טהורות (ראו
# requirements.txt) - כולל psycopg2-**binary** (לא psycopg2 הרגיל) שמארז את
# libpq בתוך ה-wheel עצמו, ולכן עדיין אין צורך בכלי build/apt נוספים כאן.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=5000 \
    DATA_DIR=/data

# משתמש לא-root (best practice בטיחותי סטנדרטי) - /data הוא ה-mount point
# ל-volume הקבוע (customers.json/crm_data.db/chat_history.txt, ראו paths.py) -
# חייב להיות בבעלות אותו משתמש שהתהליך רץ תחתיו, אחרת כתיבה אליו תיכשל.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 5000

# GET /health (שלב 4) - בודק DB אמיתי (SELECT 1) + תור עיבוד ה-webhook, לא רק
# "התהליך עונה". פטור מ-rate limiting (ראו server.py) - פינג תכוף מה-
# orchestrator לא ייחסם ויגרום ל-instance בריא להיראות "מת". קורא את PORT
# בתוך ה-Python עצמו (os.environ.get) ולא כ-${PORT:-5000} של המעטפת - כדי
# להימנע מהתנגשות escaping בין הגרשיים הכפולים של ה-shell ושל ה-f-string.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"5000\")}/health', timeout=4).status == 200 else 1)"

# shell form (לא JSON exec form) - כדי ש-${PORT:-5000} יתפרש בזמן ריצה. תואם גם
# פלטפורמות שמזריקות PORT דינמי (Cloud Run/Heroku-style), וגם docker-compose
# הרגיל (מגדיר PORT=5000 במפורש). WEB_CONCURRENCY שקול ל-"--workers 2" הקבוע
# ב-Procfile (מוסכמת Heroku לכמות ה-workers) - ניתן לדריסה בלי לגעת בתמונה.
CMD gunicorn server:app --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-2} --timeout 60
