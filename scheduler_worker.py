"""
נקודת כניסה עצמאית להרצת מנוע התזמון (scheduler.py) כתהליך נפרד לגמרי מתהליך ה-web.

למה זה נחוץ: ב-server.py, scheduler.start() מריץ thread רקע יחיד בתוך תהליך ה-Python
עצמו - זה עובד מצוין להרצה מקומית (python server.py, תהליך אחד). אבל בפריסה אמיתית
ל-production מריצים את server.py דרך Gunicorn עם כמה worker processes (ראו Procfile:
`--workers 2`); Gunicorn *לא* מריץ את הבלוק `if __name__ == "__main__":`, ולכן ה-thread
הזה ממילא לא מופעל שם. גם אם כן היה מופעל, כל worker היה מריץ עותק נפרד משלו - ואם
SCHEDULER_AUTO_SEND=true, זה אומר שליחה כפולה/משולשת של אותה הודעה.

הפתרון: תהליך `clock` נפרד לגמרי ב-Procfile (instance יחיד, לא מוכפל), עם APScheduler
- מריץ סריקה אחת מיד בהפעלה (כמו ההתנהגות הקודמת של ה-thread), ואז לפי המרווח שמוגדר
ב-SCHEDULER_INTERVAL_SECONDS. משתמש באותה לוגיקה בדיוק (scheduler.run_scan) - אין שכפול.

הרצה: python scheduler_worker.py
"""

from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

import scheduler


def main() -> None:
    scheduler._log(
        f"clock process (APScheduler) הופעל - מרווח {scheduler.SCAN_INTERVAL_SECONDS} שניות, "
        f"auto_send={scheduler.AUTO_SEND}" + ("" if scheduler.AUTO_SEND else " (dry-run בלבד)") + "."
    )

    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(
        scheduler.run_scan,
        trigger="interval",
        seconds=scheduler.SCAN_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),  # סריקה ראשונה מיידית, כמו ה-thread הקודם
        id="reactivation_scan",
        max_instances=1,  # לא מריצים סריקה חדשה אם הקודמת עוד לא הסתיימה
    )

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler._log("clock process נעצר.")


if __name__ == "__main__":
    main()
