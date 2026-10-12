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

import os
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

import scheduler


FOLLOWUP_INTERVAL_SECONDS = int(os.environ.get("FOLLOWUP_INTERVAL_SECONDS", "1800"))  # ברירת מחדל: כל חצי שעה


def main() -> None:
    scheduler._log(
        f"clock process (APScheduler) הופעל - מרווח {scheduler.SCAN_INTERVAL_SECONDS} שניות, "
        f"auto_send={scheduler.AUTO_SEND}" + ("" if scheduler.AUTO_SEND else " (dry-run בלבד)") + "."
    )
    scheduler._log(
        f"job נוסף: בדיקת פולו-אפים - מרווח {FOLLOWUP_INTERVAL_SECONDS} שניות, "
        f"auto_followup={scheduler.AUTO_FOLLOWUP} (סף {scheduler.FOLLOWUP_HOURS}/{scheduler.FINAL_FOLLOWUP_HOURS} שעות)" +
        ("" if scheduler.AUTO_FOLLOWUP else " (dry-run בלבד)") + "."
    )
    scheduler._log(
        f"job נוסף: פולו-אפ לשיחות שלא נענו - מרווח {FOLLOWUP_INTERVAL_SECONDS} שניות, "
        f"auto_call_followup={scheduler.AUTO_CALL_FOLLOWUP} (cutoff {scheduler.CALL_FOLLOWUP_CUTOFF_HOUR}:00 שעון ישראל)" +
        ("" if scheduler.AUTO_CALL_FOLLOWUP else " (dry-run בלבד)") + "."
    )
    scheduler._log(
        f"job נוסף: תזכורות פגישה - מרווח {FOLLOWUP_INTERVAL_SECONDS} שניות, "
        f"auto_meeting_reminder={scheduler.AUTO_MEETING_REMINDER} "
        f"(cutoff {scheduler.MEETING_REMINDER_MORNING_CUTOFF_HOUR}:00 שעון ישראל)" +
        ("" if scheduler.AUTO_MEETING_REMINDER else " (dry-run בלבד)") + "."
    )
    scheduler._log(
        f"job נוסף: הודעות זימון לפגישה (יום לפני) - מרווח {FOLLOWUP_INTERVAL_SECONDS} שניות, "
        f"auto_meeting_invite={scheduler.AUTO_MEETING_INVITE}" +
        ("" if scheduler.AUTO_MEETING_INVITE else " (dry-run בלבד)") + "."
    )
    scheduler._log(
        f"job נוסף: משימות אישור-הגעה למשווק - מרווח {FOLLOWUP_INTERVAL_SECONDS} שניות, "
        f"cutoff {scheduler.MARKETER_CALL_CUTOFF[0]:02d}:{scheduler.MARKETER_CALL_CUTOFF[1]:02d} שעון ישראל "
        "(אין dry-run - משימה פנימית בלבד, לא פנייה ללקוח)."
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
    # job שני, נפרד ועצמאי מסריקת ההחייאה למעלה - מרווח משלו (בדרך כלל קצר יותר,
    # כי חלון הפולו-אפ עצמו נמדד בשעות, לא בימים כמו לידים קרים) - ראו
    # scheduler.check_pending_followups לפרטי הלוגיקה והגנת הבטיחות (AUTO_FOLLOWUP).
    sched.add_job(
        scheduler.check_pending_followups,
        trigger="interval",
        seconds=FOLLOWUP_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id="followup_check",
        max_instances=1,
    )
    # job שלישי - פולו-אפ בוואטסאפ אחרי שיחת Click-to-Call שלא נענתה (No-Answer
    # Call Log). אותו מרווח בדיקה כמו followup_check לעיל (FOLLOWUP_INTERVAL_SECONDS,
    # לא מרווח נפרד) - גרנולריות של עד 30 דקות מספיקה לחלון של 60 דקות (08:30-09:30)
    # בלי להוסיף עוד משתנה סביבה. ראו scheduler.compute_call_followup_time לחישוב
    # הזמן המתוזמן עצמו (שעון ישראל, לא UTC).
    sched.add_job(
        scheduler.check_pending_call_followups,
        trigger="interval",
        seconds=FOLLOWUP_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id="call_followup_check",
        max_instances=1,
    )
    # job רביעי - תזכורות פגישה אוטומטיות בוואטסאפ (בוקר מול ערב, לפי שעת
    # הפגישה). אותו מרווח בדיקה כמו שני ה-jobs הקודמים - ראו scheduler.
    # compute_meeting_reminder_time לחישוב הזמן המתוזמן עצמו.
    sched.add_job(
        scheduler.check_pending_meeting_reminders,
        trigger="interval",
        seconds=FOLLOWUP_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id="meeting_reminder_check",
        max_instances=1,
    )
    # job חמישי - הודעת זימון ללקוח יום לפני הפגישה (Meeting Confirmation &
    # No-Show Prevention).
    sched.add_job(
        scheduler.check_pending_meeting_invites,
        trigger="interval",
        seconds=FOLLOWUP_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id="meeting_invite_check",
        max_instances=1,
    )
    # job שישי - יצירת משימת "📞 אישור הגעה" פנימית למשווק. אין דגל AUTO_* -
    # ראו ההערה המלאה ב-scheduler.MARKETER_CALL_CUTOFF (לא פנייה ללקוח).
    sched.add_job(
        scheduler.check_pending_marketer_reminders,
        trigger="interval",
        seconds=FOLLOWUP_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id="marketer_reminder_check",
        max_instances=1,
    )

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler._log("clock process נעצר.")


if __name__ == "__main__":
    main()
