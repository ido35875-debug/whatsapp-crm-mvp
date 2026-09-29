"""
עטיפה דקה לשליחת הודעת וואטסאפ בפועל דרך Twilio. משותפת בין reactivate.py (קמפיין
חימום לידים קרים) ל-server.py (מענה ידני מהדשבורד) - כדי לא לשכפל את החיבור ל-Twilio.

הערה חשובה - מדיניות מטא: הודעה שעסק יוזם ביוזמתו בוואטסאפ, מחוץ לחלון השיחה שהלקוח
פתח (יותר מ-24 שעות מאז הודעתו האחרונה), נחשבת "business-initiated conversation" -
ומחוץ ל-Twilio WhatsApp Sandbox מטא דורשת עבורה "הודעת תבנית" מאושרת מראש; טקסט חופשי
לא יעבור. הפונקציה כאן היא רק שכבת שידור - היא לא בודקת חלון-24-שעות או מדיניות תבניות;
זו אחריות הקורא (אנושי בדשבורד, או קמפיין עם --send מפורש).
"""

import itertools
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

logger = logging.getLogger("whatsapp_crm")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # לדוגמה: whatsapp:+14155238886

# TESTING=True (.env) - נבדק ב*זמן קריאה* ל-send_whatsapp_message (לא נשמר במשתנה
# קבוע ב-import time) כדי שסקריפטים כמו simulate_webhook.py יוכלו להדליק/לכבות את
# זה באותו תהליך פייתון (os.environ["TESTING"] = "true") בלי לדרוש restart - זהה
# בעיקרון ל-SCHEDULER_AUTO_SEND/SCHEDULER_AUTO_FOLLOWUP (דגל סביבה נקרא בכל קריאה).
def _is_testing_mode() -> bool:
    return os.environ.get("TESTING", "false").strip().lower() == "true"


class MockMessenger:
    """מחליף מלא ל-Twilio Client כש-TESTING=True - לא פונה לרשת/Twilio בכלל.
    נועד לבדיקות הרמטיות של שאר הצינור (עיבוד הודעה, DB, UI) בלי תלות בחשבון
    Twilio אמיתי (מגבלות Trial, קצב שליחה, עלות אמיתית). כל שליחה מודפסת ללוג
    ומחזירה SID מזויף עקבי (MOCK-...) - מבחינת הקוד הקורא (reactivate.py/server.py)
    זו הצלחה מלאה (HTTP 200 מדומה), בדיוק כמו SID אמיתי מ-Twilio - לא נדרש טיפול
    מיוחד בשום מקום אחר."""

    _counter = itertools.count(1)

    @classmethod
    def send(cls, from_: str, to: str, body: str) -> str:
        fake_sid = f"MOCK-SID-{next(cls._counter):08d}"
        logger.info(
            "[MockMessenger] שליחה מדומה (אין פנייה לספק אמיתי): from=%s to=%s body=%s -> sid=%s (HTTP 200 מדומה)",
            from_, to, body, fake_sid,
        )
        return fake_sid


def _to_e164(phone: str) -> str:
    """ממיר מספר טלפון ל-E.164 (לדוגמה +972501111111). מספרים רבים ב-customers.json
    שמורים בפורמט מקומי ישראלי (05XXXXXXXX, בלי קידומת מדינה) - Twilio דוחה כתובת
    whatsapp: שאינה E.164 תקין עם שגיאת 400 ("Invalid or disallowed parameters"),
    וזה מה שגרם בפועל לכשל שנצפה - לא פרמטר מיותר שנשלח ל-API."""
    digits = phone.strip()
    if digits.startswith("+"):
        return digits
    if digits.startswith("0"):
        return "+972" + digits[1:]  # הנחת מספר ישראלי מקומי - תואם לנתוני הפרויקט
    return "+" + digits


TRIAL_RESTRICTION_STATUSES = {400, 422}


def is_trial_restriction(exc: Exception) -> bool:
    """מזהה שגיאת חסימה אופיינית לחשבון Twilio מסוג Trial (למשל נמען לא מאומת -
    "Please add the 'to' number as a verified recipient", גם אחרי הצטרפות ל-WhatsApp
    Sandbox - אלו שתי דרישות נפרדות של Trial) - בניגוד לשגיאה אמיתית אחרת (פרטי
    חיבור שגויים וכו'). משותף בין server.py (/api/messages/send, /api/calls/start)
    ל-reactivate.py (--send) - כדי שכל נקודות השליחה יתייחסו לחסימת Trial באותה
    צורה בדיוק, בלי לשכפל את הזיהוי."""
    return (
        isinstance(exc, TwilioRestException)
        and exc.status in TRIAL_RESTRICTION_STATUSES
        and "trial" in str(exc).lower()
    )


def send_whatsapp_message(to_phone: str, body: str) -> str:
    raw = to_phone[len("whatsapp:"):] if to_phone.startswith("whatsapp:") else to_phone
    to = f"whatsapp:{_to_e164(raw)}"

    if _is_testing_mode():
        # בכוונה *לא* בודקים כאן פרטי חיבור Twilio (TWILIO_ACCOUNT_SID וכו') - כל
        # הרעיון של מצב TESTING הוא לבדוק את הצינור גם כשאין בכלל פרטי Twilio
        # מוגדרים/תקינים.
        return MockMessenger.send(from_=TWILIO_WHATSAPP_FROM or "whatsapp:+10000000000", to=to, body=body)

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        raise RuntimeError(
            "חסרים פרטי חיבור ל-Twilio ב-.env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM"
        )
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    # רק שלושת הפרמטרים הנדרשים - Twilio Sandbox (חשבון Trial) דוחה כל פרמטר נוסף/לא-חוקי
    message = client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to, body=body)
    return message.sid
