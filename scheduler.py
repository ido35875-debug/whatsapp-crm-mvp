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
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # מפורש - לא תלוי בסדר import של מודולים אחרים

import db
import reactivate
from extract import (
    DEFAULT_TENANT_ID,
    _customer_key,
    generate_followup_message,
    generate_meeting_invite_message,
    generate_meeting_reminder_message,
    generate_missed_call_followup_message,
    last_contact_at,
    load_customers,
    log_manual_reply,
    save_customers,
    update_lead_status,
)
from paths import DATA_DIR
from whatsapp_send import is_trial_restriction, send_whatsapp_message

CHAT_HISTORY_FILE = DATA_DIR / "chat_history.txt"

SCAN_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "3600"))
AUTO_SEND = os.environ.get("SCHEDULER_AUTO_SEND", "false").strip().lower() == "true"

# פולו-אפ אוטומטי ללידים שסומנו "נוצר קשר" ולא ענו - טריגר אוטומציה **נפרד**
# מ-AUTO_SEND (החייאת לידים קרים) - אותה מדיניות בטיחות בדיוק (dry-run כברירת
# מחדל, שליחה אמיתית דורשת env var מפורש), אבל דגל משלו כי אלו שתי אוטומציות
# עצמאיות - אין סיבה שהדלקת אחת תפעיל את השנייה בטעות.
FOLLOWUP_HOURS = int(os.environ.get("FOLLOWUP_HOURS", "24"))
AUTO_FOLLOWUP = os.environ.get("SCHEDULER_AUTO_FOLLOWUP", "false").strip().lower() == "true"

# תזכורת פולו-אפ **שנייה/אחרונה** - אם גם היא עברה בלי תשובה, זו הפנייה האוטומטית
# האחרונה בנושא הזה (סגמנטציה: הליד נשאר בסטטוס "follow_up" בלי המשך אוטומטי -
# ראו _FOLLOWUP_CHANNELS/check_pending_followups למטה). אותו דגל בטיחות
# (SCHEDULER_AUTO_FOLLOWUP) שולט גם על השלב הזה - אין דגל נפרד.
FINAL_FOLLOWUP_HOURS = int(os.environ.get("FINAL_FOLLOWUP_HOURS", "72"))

# ---- No-Answer Call Log & Dynamic Follow-Up (ראו server.voice_dial_status) ----
# דגל בטיחות **נפרד** משתי האוטומציות למעלה - שליחת WhatsApp אמיתית אחרי שיחת
# Click-to-Call שלא נענתה היא עוד אוטומציה עצמאית, בלי קשר לטיפול בלידים קרים/
# תזכורות פולו-אפ בטקסט. ברירת מחדל false (dry-run - רק רושם ללוג/מתזמן ב-DB,
# לא שולח בפועל).
AUTO_CALL_FOLLOWUP = os.environ.get("SCHEDULER_AUTO_CALL_FOLLOWUP", "false").strip().lower() == "true"

# ⚠️ חובה שעון מקומי מודע-DST (Asia/Jerusalem), **לא** UTC גולמי - השרת (Render)
# רץ ב-UTC; "16:00"/"08:30-09:30" בבקשת המוצר הם שעון ישראל במובהק (עסק נדל"ן
# ישראלי). טעות נפוצה כאן הייתה גורמת לסף "16:00" לזחול ב-2-3 שעות (קיץ/חורף)
# ולהפוך את כל הלוגיקה לשגויה בלי שגיאה גלויה - בדיוק מה שה-Code Audit המבוקש
# התריע עליו.
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
CALL_FOLLOWUP_CUTOFF_HOUR = int(os.environ.get("CALL_FOLLOWUP_CUTOFF_HOUR", "16"))  # שעון ישראל
CALL_FOLLOWUP_SAME_DAY_HOUR = int(os.environ.get("CALL_FOLLOWUP_SAME_DAY_HOUR", "18"))  # שעון ישראל
CALL_FOLLOWUP_NEXT_DAY_START = (8, 30)  # שעון ישראל
CALL_FOLLOWUP_NEXT_DAY_END = (9, 30)    # שעון ישראל

AUTO_REPLY_MIN_DELAY_SECONDS = int(os.environ.get("AUTO_REPLY_MIN_DELAY_SECONDS", "20"))
AUTO_REPLY_MAX_DELAY_SECONDS = int(os.environ.get("AUTO_REPLY_MAX_DELAY_SECONDS", "60"))

# ---- תזכורות פגישה אוטומטיות בוואטסאפ (בוקר מול ערב, ראו compute_meeting_
# reminder_time/check_pending_meeting_reminders למטה) ----
# דגל בטיחות נפרד נוסף - עוד אוטומציה עצמאית, בלי קשר לשלוש האחרות למעלה.
AUTO_MEETING_REMINDER = os.environ.get("SCHEDULER_AUTO_MEETING_REMINDER", "false").strip().lower() == "true"
# פגישה לפני השעה הזו (שעון ישראל) נחשבת "פגישת בוקר" -> תזכורת נשלחת בערב
# הקודם (MEETING_REMINDER_EVENING_HOUR). פגישה בשעה הזו ואחריה נחשבת "אחה"צ/
# ערב" -> תזכורת נשלחת בבוקר אותו היום (אותו חלון 08:30-09:30 כמו פולו-אפ
# שיחות - CALL_FOLLOWUP_NEXT_DAY_START/END - לא כפל-קוד, שתי הפונקציות חולקות
# את הקבועים האלה בכוונה).
MEETING_REMINDER_MORNING_CUTOFF_HOUR = int(os.environ.get("MEETING_REMINDER_MORNING_CUTOFF_HOUR", "13"))
MEETING_REMINDER_EVENING_HOUR = int(os.environ.get("MEETING_REMINDER_EVENING_HOUR", "19"))  # שעון ישראל

# ---- Meeting Confirmation & No-Show Prevention ----
# דגל בטיחות להודעת הזימון ללקוח (invite - כתובת/מיקום + מי לפגוש, יום לפני) -
# עצמאי מ-AUTO_MEETING_REMINDER (זו הודעה שונה, תוכן שונה - ראו extract.
# generate_meeting_invite_message).
AUTO_MEETING_INVITE = os.environ.get("SCHEDULER_AUTO_MEETING_INVITE", "false").strip().lower() == "true"

# משימת "📞 אישור הגעה" הפנימית למשווק - לא נשלחת ללקוח בכלל (ראו
# check_pending_marketer_reminders), ולכן **אין** דגל AUTO_* - יצירת משימה
# פנימית בדשבורד אינה "פנייה יזומה ללקוח" (ראו CLAUDE.md #1, אותו עיקרון
# שמאפשר ל-scheduling_agent.py ליצור הצעות בלי אישור). תמיד נוצרת בפועל -
# ה"בטיחות" כאן היא שהיא *לא* מתקשרת/שולחת בעצמה, רק מתריעה לאדם לעשות זאת.
#
# סף שונה בכוונה מ-MEETING_REMINDER_MORNING_CUTOFF_HOUR (11:30, לא 13:00) -
# זו דרישת מוצר נפרדת (שיחת אישור-הגעה טלפונית צריכה עדיפות/מרווח זמן אחר
# מהודעת תזכורת בוואטסאפ) - ראו "אישורי הגעה" בבקשת המוצר. שעת ה-11:30 מיוצגת
# כ(שעה,דקה) בכוונה, לא כ-int שעה שלם - חיתוך בחצי-שעה, לא בשעה עגולה.
MARKETER_CALL_CUTOFF = (11, 30)  # שעון ישראל - פגישה לפני השעה הזו = "פגישת בוקר"
MARKETER_CALL_NEXT_DAY_START = (7, 30)  # שעון ישראל - פגישת אחה"צ/ערב -> שיחה בבוקר אותו יום
MARKETER_CALL_NEXT_DAY_END = (8, 30)    # שעון ישראל
# ⚠️ הנחה מפורשת משלי (לא פורט במפורש בבקשת המוצר): "פגישת בוקר -> יום לפני"
# לא קיבלה שעת-יעד מדויקת ("יום לפני" בלבד) - בברירת מחדל קבעתי ערב יום קודם
# (שעות עבודה סבירות), ניתן לשינוי כאן בלי לגעת בלוגיקה.
MARKETER_CALL_DAY_BEFORE_HOUR = int(os.environ.get("MARKETER_CALL_DAY_BEFORE_HOUR", "18"))  # שעון ישראל

# ורטיקל -> כמה ימים בלי שום קשר (נכנס/יוצא) בהיסטוריית הכרטיס, לפני שליד קר נחשב
# "בשל" להחייאה חוזרת. שלושת הענפים ממסמך האפיון (חזון המוצר ב-CLAUDE.md): איקומרס -
# מחזור מכירה קצר, חימום מהיר; שירותים - ביניים; נדל"ן - מחזור מכירה ארוך, ליד צריך
# יותר זמן להבשיל לפני פנייה חוזרת.
VERTICAL_COLD_THRESHOLDS_DAYS = {
    "ecommerce": 3,
    "services": 7,
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


_FOLLOWUP_CHANNELS = ("followup", "followup_final")


def check_pending_followups(tenant_id: str | None = None) -> list[dict]:
    """סורק customers.json לחיפוש לידים שסומנו "נוצר קשר"/"follow_up"
    (lead_status in ("contacted","follow_up")) ולא קיבלו תגובה נכנסת מאז
    status_changed_at (ראו extract.update_lead_status) - ומייצר/שולח עד שתי
    הודעות פולו-אפ בשני שלבים:
    - שלב 1 (FOLLOWUP_HOURS, ברירת מחדל 24ש): תזכורת ראשונה (channel="followup") -
      ואז lead_status עובר אוטומטית ל-"follow_up" (סגמנטציה בדשבורד), בלי לגעת
      ב-status_changed_at (כדי ששעון ה-72ש ימשיך להימדד מרגע "נוצר קשר" המקורי,
      לא מרגע התזכורת).
    - שלב 2 (FINAL_FOLLOWUP_HOURS, ברירת מחדל 72ש): תזכורת אחרונה/סופית
      (channel="followup_final", טון שונה - ראו extract.generate_followup_
      message(final=True)). אחריה אין עוד פולו-אפ אוטומטי - הליד נשאר
      "follow_up" עד שנציג יתערב ידנית או שהלקוח יענה.
    ספירת התזכורות שנשלחו מאז status_changed_at (לפי channel ב-history) קובעת
    באיזה שלב הליד נמצא - בלי צורך בשדה-מצב נפרד.

    tenant_id=None (ברירת מחדל) סורק את כל ה-tenants - בניגוד ל-run_scan, שדורש
    tenant_id ספציפי (ההחייאה תמיד רצה per-tenant דרך contacts.csv); כאן אין
    קובץ contacts.csv מעורב בכלל, רק customers.json שכבר כולל tenant_id בכל
    מפתח, אז אין סיבה להגביל לtenant יחיד כברירת מחדל.

    **שליחה אמיתית רק אם AUTO_FOLLOWUP=true** (SCHEDULER_AUTO_FOLLOWUP ב-.env) -
    בדיוק כמו AUTO_SEND למעלה: ברירת המחדל היא dry-run (מייצר את ההודעה, רושם
    בלוג, לא שולח כלום). כשל Trial (whatsapp_send.is_trial_restriction) נתפס
    ומטופל בדיוק כמו בכל שאר נקודות השליחה בפרויקט - נרשם כ-simulated, לא
    כשגיאה אמיתית."""
    now = datetime.now(timezone.utc)
    customers = load_customers()
    results = []

    for key, card in customers.items():
        card_tenant, _, phone = key.partition("::")
        if tenant_id and card_tenant != tenant_id:
            continue
        if card.get("lead_status") not in ("contacted", "follow_up"):
            continue
        changed_at_str = card.get("status_changed_at")
        if not changed_at_str:
            continue  # כרטיס ישן מלפני שהשדה נוסף - אין בסיס לחשב ממנו טיימר
        try:
            changed_at = datetime.fromisoformat(changed_at_str)
        except ValueError:
            continue

        history = card.get("history") or []
        if any(h.get("direction") == "in" and h.get("timestamp", "") > changed_at_str for h in history):
            continue  # כבר ענה מאז - אין צורך בפולו-אפ

        reminders_sent = sum(
            1 for h in history
            if h.get("channel") in _FOLLOWUP_CHANNELS and h.get("timestamp", "") > changed_at_str
        )
        if reminders_sent >= 2:
            continue  # גם התזכורת הראשונה וגם האחרונה כבר נשלחו - אין עוד פולו-אפ אוטומטי

        is_final = reminders_sent == 1
        threshold_hours = FINAL_FOLLOWUP_HOURS if is_final else FOLLOWUP_HOURS
        if (now - changed_at).total_seconds() < threshold_hours * 3600:
            continue  # עדיין בתוך חלון ההמתנה לשלב הזה

        channel = "followup_final" if is_final else "followup"
        message = generate_followup_message(card, final=is_final)
        entry = {
            "phone": phone, "tenant_id": card_tenant, "name": card.get("customer_name"),
            "message": message, "final": is_final, "sent": False, "simulated": False, "error": None,
        }

        if AUTO_FOLLOWUP:
            try:
                entry["sid"] = send_whatsapp_message(phone, message)
                entry["sent"] = True
            except Exception as exc:
                if is_trial_restriction(exc):
                    entry["simulated"] = True
                else:
                    entry["error"] = str(exc)

            if entry["sent"] or entry["simulated"]:
                # source_channel=channel (לא "whatsapp") - כדי שה-ספירה למעלה
                # תוכל להבדיל שלב פולו-אפ מהודעה רגילה, ולא לשלוח כפול/לדלג שלב
                log_manual_reply(phone, message, tenant_id=card_tenant, source_channel=channel, simulated=entry["simulated"])
                db.log_message(phone, message, direction="out", tenant_id=card_tenant, channel=channel, simulated=entry["simulated"])
            status_label = "נשלח בפועל" if entry["sent"] else ("סימולציה (Trial)" if entry["simulated"] else f"נכשל: {entry['error']}")
        else:
            status_label = "dry-run - לא נשלח"

        if not is_final and card.get("lead_status") != "follow_up":
            # רק בשלב 1 - מקדמים סגמנטציה ל-"follow_up" בלי לגעת ב-status_changed_at
            # (ראו docstring מעל). טעינה/שמירה טריות - לא על ה-customers שנטען
            # בתחילת הפונקציה, כדי לא לדרוס עדכונים מקביליים אחרים.
            fresh_customers = load_customers()
            fresh_card = fresh_customers.get(key)
            if fresh_card is not None:
                fresh_card["lead_status"] = "follow_up"
                save_customers(fresh_customers)

        stage_label = "סופי (72ש)" if is_final else "ראשון (24ש)"
        _log(f"פולו-אפ {stage_label} ({status_label}): {entry['name'] or phone} ({phone}, tenant={card_tenant})")
        results.append(entry)

    if not results:
        _log(f"בדיקת פולו-אפים: אין לידים שממתינים מעל {FOLLOWUP_HOURS}/{FINAL_FOLLOWUP_HOURS} שעות בלי תשובה.")
    return results


def compute_call_followup_time(now_utc: datetime) -> datetime:
    """מחשב מתי לשלוח פולו-אפ בוואטסאפ אחרי שיחת Click-to-Call שלא נענתה - ראו
    server.voice_dial_status (הקורא היחיד בפועל). **שעון ישראל (Asia/Jerusalem),
    לא UTC גולמי** - ראו האזהרה מעל ISRAEL_TZ. חיוג לפני CALL_FOLLOWUP_CUTOFF_HOUR
    (ברירת מחדל 16:00 שעון ישראל) -> היום ב-CALL_FOLLOWUP_SAME_DAY_HOUR (ברירת
    מחדל 18:00, בדיוק). חיוג בשעה הזו ואחריה -> מחר, זמן אקראי בין 08:30-09:30
    (אנושיות - לא "קריר"/אלגוריתמי מדי, בדיוק כמו AUTO_REPLY_MIN/MAX_DELAY_SECONDS
    לתזמון תשובה אוטומטית). מחזיר datetime מודע-timezone ב-UTC (לשמירה ב-DB,
    עקבי עם כל שאר חותמות הזמן בפרויקט - ראו db.mark_call_dial_outcome)."""
    local_now = now_utc.astimezone(ISRAEL_TZ)
    if local_now.hour < CALL_FOLLOWUP_CUTOFF_HOUR:
        target_local = local_now.replace(
            hour=CALL_FOLLOWUP_SAME_DAY_HOUR, minute=0, second=0, microsecond=0
        )
    else:
        start_h, start_m = CALL_FOLLOWUP_NEXT_DAY_START
        end_h, end_m = CALL_FOLLOWUP_NEXT_DAY_END
        window_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        offset_minutes = random.randint(0, max(window_minutes, 0))
        tomorrow_local = local_now + timedelta(days=1)
        target_local = tomorrow_local.replace(
            hour=start_h, minute=start_m, second=0, microsecond=0
        ) + timedelta(minutes=offset_minutes)
    return target_local.astimezone(timezone.utc)


def check_pending_call_followups() -> list[dict]:
    """סורק שיחות Click-to-Call שסומנו no_answer (ראו db.mark_call_dial_outcome,
    server.voice_dial_status) עם followup_scheduled_at שעבר - ומייצר/שולח הודעת
    פולו-אפ קז'ואלית בוואטסאפ (extract.generate_missed_call_followup_message).
    לא שולחת אם הלקוח כבר כתב משהו בעצמו אחרי השיחה - פולו-אפ "התקשרנו ולא
    השגנו אותך" אחרי שהלקוח כבר יזם קשר בעצמו הוא מיושן/מביך, לא מועיל.

    **שליחה אמיתית רק אם SCHEDULER_AUTO_CALL_FOLLOWUP=true** - בדיוק כמו
    AUTO_SEND/AUTO_FOLLOWUP למעלה: ברירת המחדל dry-run. השהיה אנושית רגילה
    (AUTO_REPLY_MIN/MAX_DELAY_SECONDS, אותם משתני סביבה כמו מענה אוטומטי
    ל-webhook) לפני השליחה בפועל - חוסמת (time.sleep) כאן בכוונה: זה thread/
    job מחזורי ברקע, לא תגובת HTTP שמישהו מחכה לה, אז אין בעיית head-of-line
    blocking כמו ב-server._send_auto_reply_after_delay."""
    now = datetime.now(timezone.utc)
    results = []

    for call in db.get_pending_call_followups():
        scheduled_at_str = call.get("followup_scheduled_at")
        if not scheduled_at_str:
            continue
        try:
            scheduled_at = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            continue
        if scheduled_at > now:
            continue  # עדיין לא הגיע הזמן המתוזמן

        phone, tenant_id = call["phone"], call["tenant_id"]
        card = load_customers().get(_customer_key(tenant_id, phone)) or {"phone": phone, "tenant_id": tenant_id}

        call_created_at = call.get("created_at") or ""
        history = card.get("history") or []
        if any(h.get("direction") == "in" and h.get("timestamp", "") > call_created_at for h in history):
            _log(f"פולו-אפ שיחה שלא נענתה דולג (הלקוח כבר כתב): {card.get('customer_name') or phone} ({phone})")
            db.mark_call_followup_sent(call["id"])
            continue

        message = generate_missed_call_followup_message(card)
        entry = {
            "phone": phone, "tenant_id": tenant_id, "name": card.get("customer_name"),
            "message": message, "sent": False, "simulated": False, "error": None,
        }

        if AUTO_CALL_FOLLOWUP:
            time.sleep(random.randint(AUTO_REPLY_MIN_DELAY_SECONDS, AUTO_REPLY_MAX_DELAY_SECONDS))
            try:
                entry["sid"] = send_whatsapp_message(phone, message)
                entry["sent"] = True
            except Exception as exc:
                if is_trial_restriction(exc):
                    entry["simulated"] = True
                else:
                    entry["error"] = str(exc)

            if entry["sent"] or entry["simulated"]:
                log_manual_reply(phone, message, tenant_id=tenant_id, source_channel="call_followup", simulated=entry["simulated"])
                db.log_message(phone, message, direction="out", tenant_id=tenant_id, channel="call_followup", simulated=entry["simulated"])
            status_label = "נשלח בפועל" if entry["sent"] else ("סימולציה (Trial)" if entry["simulated"] else f"נכשל: {entry['error']}")
        else:
            status_label = "dry-run - לא נשלח"

        db.mark_call_followup_sent(call["id"])
        _log(f"פולו-אפ שיחה שלא נענתה ({status_label}): {entry['name'] or phone} ({phone}, tenant={tenant_id})")
        results.append(entry)

    if not results:
        _log("בדיקת פולו-אפים לשיחות שלא נענו: אין פולו-אפים שהגיע זמנם.")
    return results


def compute_meeting_reminder_time(due_date: str, due_time: str) -> datetime | None:
    """מחשב מתי לשלוח תזכורת פגישה בוואטסאפ, לפי שעון ישראל (ראו האזהרה מעל
    ISRAEL_TZ) - פגישת בוקר (לפני MEETING_REMINDER_MORNING_CUTOFF_HOUR, ברירת
    מחדל 13:00) -> תזכורת בערב הקודם (MEETING_REMINDER_EVENING_HOUR, ברירת
    מחדל 19:00, בדיוק). פגישת אחה"צ/ערב (13:00 ואחריה) -> תזכורת בבוקר אותו
    היום, בחלון 08:30-09:30 (CALL_FOLLOWUP_NEXT_DAY_START/END - משותף עם
    compute_call_followup_time בכוונה, לא כפל-קוד). מחזיר None אם due_date/
    due_time לא תקינים (למשל משימה בלי שעה מוגדרת - אין "בוקר מול ערב" להחליט
    ממנו, לא מתזמנים תזכורת בכלל במקרה הזה - עדיף לא לשלוח מאשר לנחש)."""
    try:
        meeting_hour, meeting_minute = (int(p) for p in due_time.split(":")[:2])
        meeting_date = datetime.strptime(due_date, "%Y-%m-%d").date()
    except (ValueError, AttributeError, TypeError):
        return None

    meeting_local = datetime(
        meeting_date.year, meeting_date.month, meeting_date.day,
        meeting_hour, meeting_minute, tzinfo=ISRAEL_TZ,
    )
    if meeting_hour < MEETING_REMINDER_MORNING_CUTOFF_HOUR:
        reminder_local = (meeting_local - timedelta(days=1)).replace(
            hour=MEETING_REMINDER_EVENING_HOUR, minute=0, second=0, microsecond=0
        )
    else:
        start_h, start_m = CALL_FOLLOWUP_NEXT_DAY_START
        end_h, end_m = CALL_FOLLOWUP_NEXT_DAY_END
        window_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        offset_minutes = random.randint(0, max(window_minutes, 0))
        reminder_local = meeting_local.replace(
            hour=start_h, minute=start_m, second=0, microsecond=0
        ) + timedelta(minutes=offset_minutes)
    return reminder_local.astimezone(timezone.utc)


def check_pending_meeting_reminders() -> list[dict]:
    """סורק פגישות מאושרות בפועל (calendar_tasks, status="pending" - **לא**
    "pending_confirmation", שזו הצעה שעדיין לא אושרה ע"י נציג) שעדיין לא נשלחה
    להן תזכורת (reminder_sent=0 - ראו db.mark_task_reminder_sent) וזמן התזכורת
    המחושב (compute_meeting_reminder_time) כבר הגיע - ומייצרת/שולחת תזכורת
    בוואטסאפ (extract.generate_meeting_reminder_message).

    **שליחה אמיתית רק אם SCHEDULER_AUTO_MEETING_REMINDER=true** - בדיוק כמו
    שאר דגלי הבטיחות בקובץ הזה. השהיה אנושית רגילה לפני שליחה בפועל, אותו
    עיקרון כמו check_pending_call_followups."""
    now = datetime.now(timezone.utc)
    results = []

    for task in db.get_tasks(status="pending"):
        if task.get("reminder_sent"):
            continue
        reminder_at = compute_meeting_reminder_time(task.get("due_date"), task.get("due_time"))
        if reminder_at is None or reminder_at > now:
            continue

        phone, tenant_id = task["phone"], task["tenant_id"]
        card = load_customers().get(_customer_key(tenant_id, phone)) or {"phone": phone, "tenant_id": tenant_id}

        message = generate_meeting_reminder_message(card, task)
        entry = {
            "phone": phone, "tenant_id": tenant_id, "name": card.get("customer_name"),
            "message": message, "due_date": task.get("due_date"), "due_time": task.get("due_time"),
            "sent": False, "simulated": False, "error": None,
        }

        if AUTO_MEETING_REMINDER:
            time.sleep(random.randint(AUTO_REPLY_MIN_DELAY_SECONDS, AUTO_REPLY_MAX_DELAY_SECONDS))
            try:
                entry["sid"] = send_whatsapp_message(phone, message)
                entry["sent"] = True
            except Exception as exc:
                if is_trial_restriction(exc):
                    entry["simulated"] = True
                else:
                    entry["error"] = str(exc)

            if entry["sent"] or entry["simulated"]:
                log_manual_reply(phone, message, tenant_id=tenant_id, source_channel="meeting_reminder", simulated=entry["simulated"])
                db.log_message(phone, message, direction="out", tenant_id=tenant_id, channel="meeting_reminder", simulated=entry["simulated"])
            status_label = "נשלח בפועל" if entry["sent"] else ("סימולציה (Trial)" if entry["simulated"] else f"נכשל: {entry['error']}")
        else:
            status_label = "dry-run - לא נשלח"

        db.mark_task_reminder_sent(task["id"])
        _log(
            f"תזכורת פגישה ({status_label}): {entry['name'] or phone} ({phone}, tenant={tenant_id}) "
            f"- {task.get('due_date')} {task.get('due_time')}"
        )
        results.append(entry)

    if not results:
        _log("בדיקת תזכורות פגישה: אין תזכורות שהגיע זמנן.")
    return results


def check_pending_meeting_invites() -> list[dict]:
    """סורק פגישות מאושרות בפועל (status="pending") שעדיין לא נשלחה להן הודעת
    זימון (invite_sent=0 - ראו db.mark_invite_sent) - ושולחת/מייצרת אותה יום
    לפני הפגישה, **באותה שעה ביום** כמו הפגישה עצמה (לא שעת-יעד קבועה - הבקשה
    פירטה שעות קבועות לתזכורת הטלפונית אבל לא לזימון, ראו ההערה ב-
    extract.generate_meeting_invite_message). ללא due_time - לא ניתן לחשב
    "יום לפני, באותה שעה" בכלל, לא מתזמנים זימון (עדיף לא לשלוח מאשר לנחש שעה).

    **שליחה אמיתית רק אם SCHEDULER_AUTO_MEETING_INVITE=true** - בדיוק כמו שאר
    דגלי הבטיחות בקובץ הזה."""
    now = datetime.now(timezone.utc)
    results = []

    for task in db.get_tasks(status="pending"):
        if task.get("invite_sent") or not task.get("due_time"):
            continue
        try:
            meeting_hour, meeting_minute = (int(p) for p in task["due_time"].split(":")[:2])
            meeting_date = datetime.strptime(task["due_date"], "%Y-%m-%d").date()
        except (ValueError, AttributeError, TypeError, KeyError):
            continue
        meeting_local = datetime(
            meeting_date.year, meeting_date.month, meeting_date.day,
            meeting_hour, meeting_minute, tzinfo=ISRAEL_TZ,
        )
        invite_at = (meeting_local - timedelta(days=1)).astimezone(timezone.utc)
        if invite_at > now:
            continue

        phone, tenant_id = task["phone"], task["tenant_id"]
        card = load_customers().get(_customer_key(tenant_id, phone)) or {"phone": phone, "tenant_id": tenant_id}

        message = generate_meeting_invite_message(card, task)
        entry = {
            "phone": phone, "tenant_id": tenant_id, "name": card.get("customer_name"),
            "message": message, "due_date": task.get("due_date"), "due_time": task.get("due_time"),
            "sent": False, "simulated": False, "error": None,
        }

        if AUTO_MEETING_INVITE:
            time.sleep(random.randint(AUTO_REPLY_MIN_DELAY_SECONDS, AUTO_REPLY_MAX_DELAY_SECONDS))
            try:
                entry["sid"] = send_whatsapp_message(phone, message)
                entry["sent"] = True
            except Exception as exc:
                if is_trial_restriction(exc):
                    entry["simulated"] = True
                else:
                    entry["error"] = str(exc)

            if entry["sent"] or entry["simulated"]:
                log_manual_reply(phone, message, tenant_id=tenant_id, source_channel="meeting_invite", simulated=entry["simulated"])
                db.log_message(phone, message, direction="out", tenant_id=tenant_id, channel="meeting_invite", simulated=entry["simulated"])
            status_label = "נשלח בפועל" if entry["sent"] else ("סימולציה (Trial)" if entry["simulated"] else f"נכשל: {entry['error']}")
        else:
            status_label = "dry-run - לא נשלח"

        db.mark_invite_sent(task["id"])
        _log(f"הודעת זימון לפגישה ({status_label}): {entry['name'] or phone} ({phone}, tenant={tenant_id})")
        results.append(entry)

    if not results:
        _log("בדיקת הודעות זימון לפגישה: אין זימונים שהגיע זמנם.")
    return results


def compute_marketer_call_time(due_date: str, due_time: str) -> datetime | None:
    """מחשב מתי לייצר משימת "📞 אישור הגעה" פנימית למשווק (לא נשלח ללקוח
    בכלל) - ראו check_pending_marketer_reminders. פגישת בוקר (לפני
    MARKETER_CALL_CUTOFF, ברירת מחדל 11:30 שעון ישראל) -> המשימה נוצרת יום
    לפני בשעה MARKETER_CALL_DAY_BEFORE_HOUR (⚠️ הנחה שלי - ראו ההערה מעל
    הקבוע). פגישת אחה"צ/ערב (11:30 ואחריה) -> המשימה נוצרת בבוקר אותו היום,
    בחלון 07:30-08:30 אקראי. None אם due_date/due_time לא תקינים."""
    try:
        meeting_hour, meeting_minute = (int(p) for p in due_time.split(":")[:2])
        meeting_date = datetime.strptime(due_date, "%Y-%m-%d").date()
    except (ValueError, AttributeError, TypeError):
        return None

    meeting_local = datetime(
        meeting_date.year, meeting_date.month, meeting_date.day,
        meeting_hour, meeting_minute, tzinfo=ISRAEL_TZ,
    )
    cutoff_hour, cutoff_minute = MARKETER_CALL_CUTOFF
    is_morning_meeting = (meeting_hour, meeting_minute) < (cutoff_hour, cutoff_minute)
    if is_morning_meeting:
        call_local = (meeting_local - timedelta(days=1)).replace(
            hour=MARKETER_CALL_DAY_BEFORE_HOUR, minute=0, second=0, microsecond=0
        )
    else:
        start_h, start_m = MARKETER_CALL_NEXT_DAY_START
        end_h, end_m = MARKETER_CALL_NEXT_DAY_END
        window_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        offset_minutes = random.randint(0, max(window_minutes, 0))
        call_local = meeting_local.replace(
            hour=start_h, minute=start_m, second=0, microsecond=0
        ) + timedelta(minutes=offset_minutes)
    return call_local.astimezone(timezone.utc)


def check_pending_marketer_reminders() -> list[dict]:
    """סורק פגישות מאושרות בפועל (status="pending") שעדיין לא נוצרה להן משימת
    אישור-הגעה פנימית (marketer_reminder_status is None - ראו db.mark_
    marketer_reminder_created) וזמן היצירה המחושב (compute_marketer_call_time)
    הגיע - ויוצרת שורת calendar_tasks **חדשה** (source="marketer_confirmation",
    related_task_id=הפגישה המקורית) - "📞 אישור הגעה" שמופיעה כמשימה רגילה
    ביומן/בפאנל המשימות (ראו renderTaskRow ב-index.html), עם כפתורי תוצאה
    משלה (לא "✓ בוצע"/"✗ בטל" הרגילים - ראו POST /api/tasks/<id>/attendance).

    ⚠️ **אין דגל AUTO_* כאן בכוונה** - זו לא פנייה ללקוח, רק יצירת משימה פנימית
    בדשבורד (ראו ההערה המלאה מעל MARKETER_CALL_CUTOFF) - נוצרת תמיד בפועל,
    בניגוד לשאר הפונקציות בקובץ הזה."""
    now = datetime.now(timezone.utc)
    results = []

    for task in db.get_tasks(status="pending"):
        if task.get("marketer_reminder_status") or not task.get("due_time"):
            continue
        call_at = compute_marketer_call_time(task.get("due_date"), task.get("due_time"))
        if call_at is None or call_at > now:
            continue

        phone, tenant_id = task["phone"], task["tenant_id"]
        card = load_customers().get(_customer_key(tenant_id, phone)) or {}
        name = card.get("customer_name") or phone
        call_local = call_at.astimezone(ISRAEL_TZ)

        new_task_id = db.create_task(
            phone, tenant_id=tenant_id,
            title=f"📞 אישור הגעה טלפוני - {name}",
            due_date=call_local.strftime("%Y-%m-%d"),
            due_time=call_local.strftime("%H:%M"),
            notes=f"פגישה מתוזמנת ל-{task.get('due_date')} {task.get('due_time')} - יש להתקשר ולאשר הגעה.",
            status="pending",
            source="marketer_confirmation",
            related_task_id=task["id"],
        )
        db.mark_marketer_reminder_created(task["id"])
        _log(
            f"נוצרה משימת אישור-הגעה (task_id={new_task_id}) ל-{name} ({phone}, tenant={tenant_id}) "
            f"- להתקשר ב-{call_local.strftime('%Y-%m-%d %H:%M')}"
        )
        results.append({"task_id": new_task_id, "phone": phone, "tenant_id": tenant_id, "name": name, "call_at": call_at.isoformat()})

    if not results:
        _log("בדיקת משימות אישור-הגעה: אין משימות שהגיע זמנן.")
    return results


def _loop() -> None:
    _log(
        f"מנוע התזמון הופעל. מרווח סריקה: {SCAN_INTERVAL_SECONDS} שניות. "
        f"auto_send={AUTO_SEND}" + ("" if AUTO_SEND else " (dry-run בלבד - לא שולח הודעות אמיתיות)") +
        f" | auto_followup={AUTO_FOLLOWUP} (סף {FOLLOWUP_HOURS}/{FINAL_FOLLOWUP_HOURS} שעות)" +
        ("" if AUTO_FOLLOWUP else " (dry-run בלבד)") +
        f" | auto_call_followup={AUTO_CALL_FOLLOWUP}" + ("" if AUTO_CALL_FOLLOWUP else " (dry-run בלבד)") +
        f" | auto_meeting_reminder={AUTO_MEETING_REMINDER}" + ("" if AUTO_MEETING_REMINDER else " (dry-run בלבד)") +
        f" | auto_meeting_invite={AUTO_MEETING_INVITE}" + ("" if AUTO_MEETING_INVITE else " (dry-run בלבד)") + "."
    )
    while not _stop_event.is_set():
        try:
            run_scan()
        except Exception as exc:
            _log(f"שגיאה במחזור סריקה: {exc}")
        try:
            check_pending_followups()
        except Exception as exc:
            _log(f"שגיאה בבדיקת פולו-אפים: {exc}")
        try:
            check_pending_call_followups()
        except Exception as exc:
            _log(f"שגיאה בבדיקת פולו-אפים לשיחות שלא נענו: {exc}")
        try:
            check_pending_meeting_reminders()
        except Exception as exc:
            _log(f"שגיאה בבדיקת תזכורות פגישה: {exc}")
        try:
            check_pending_meeting_invites()
        except Exception as exc:
            _log(f"שגיאה בבדיקת הודעות זימון לפגישה: {exc}")
        try:
            check_pending_marketer_reminders()
        except Exception as exc:
            _log(f"שגיאה בבדיקת משימות אישור-הגעה: {exc}")
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
        "auto_followup": AUTO_FOLLOWUP,
        "followup_hours": FOLLOWUP_HOURS,
        "final_followup_hours": FINAL_FOLLOWUP_HOURS,
        "auto_call_followup": AUTO_CALL_FOLLOWUP,
        "call_followup_cutoff_hour": CALL_FOLLOWUP_CUTOFF_HOUR,
        "auto_meeting_reminder": AUTO_MEETING_REMINDER,
        "meeting_reminder_morning_cutoff_hour": MEETING_REMINDER_MORNING_CUTOFF_HOUR,
        "auto_meeting_invite": AUTO_MEETING_INVITE,
        "marketer_call_cutoff": f"{MARKETER_CALL_CUTOFF[0]:02d}:{MARKETER_CALL_CUTOFF[1]:02d}",
    }
