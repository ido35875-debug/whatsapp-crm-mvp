"""
עטיפה דקה ליצירת שיחת "Click-to-Call" (Voice bridge) דרך Twilio: Twilio מתקשר קודם
לנציג (AGENT_PHONE_NUMBER), וברגע שהוא עונה, TwiML ב-/voice/connect (server.py) מגשר
(<Dial>) למספר הלקוח. במכוון לא WebRTC softphone - זה ידרוש יצירת TwiML Application
ידנית בקונסולת Twilio (לא ניתן לסקריפט), ובנוסף גישה למיקרופון בדפדפן. משותף רק
ל-server.py כרגע (routes /api/calls/*), בדומה ל-whatsapp_send.py.

⚠️ בסביבת הפיתוח הזו אין עדיין מספר Twilio Voice-capable מוגדר (רק מספר WhatsApp
Sandbox קיים ב-.env) - AGENT_PHONE_NUMBER/TWILIO_VOICE_FROM/PUBLIC_BASE_URL נבדקים
בזמן קריאה (לא בזמן import, בניגוד ל-ANTHROPIC_API_KEY) כדי שהשרת ימשיך לעלות ולשרת
וואטסאפ גם בלי תצורת Voice - ראו server.py: /api/calls/start מטפל ב-RuntimeError כאן
בדיוק כמו שמטפל ב-TwilioRestException של send_whatsapp_message (רישום כ-simulated).
"""

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

from whatsapp_send import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, _to_e164  # לא לשכפל

TWILIO_VOICE_FROM = os.environ.get("TWILIO_VOICE_FROM")     # מספר Voice-capable של Twilio
AGENT_PHONE_NUMBER = os.environ.get("AGENT_PHONE_NUMBER")   # מספר הנציג - הרגל הראשונה של הגשר
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")         # https://... בלי / בסוף - נדרש כי Calls.create צריך URL ל-TwiML


def start_bridge_call(customer_phone: str, tenant_id: str = "default") -> str:
    """יוזם שיחת גישור: Twilio מתקשר קודם ל-AGENT_PHONE_NUMBER, ולאחר מענה TwiML
    (/voice/connect) מגשר ללקוח. מחזיר call_sid בהצלחה. מעלה RuntimeError אם תצורה
    חסרה, או TwilioRestException אם Twilio עצמו דחה את הבקשה - שני המקרים מטופלים
    בנפרד ב-/api/calls/start (server.py), בדיוק כמו send_whatsapp_message +
    _is_trial_restriction."""
    missing = [
        name for name, value in (
            ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
            ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
            ("TWILIO_VOICE_FROM", TWILIO_VOICE_FROM),
            ("AGENT_PHONE_NUMBER", AGENT_PHONE_NUMBER),
            ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
        ) if not value
    ]
    if missing:
        raise RuntimeError(f"חסרים משתני סביבה לשיחות Voice ב-.env: {', '.join(missing)}")

    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    # + ב-E.164 גולמי מתפרש כרווח ב-query string - חובה quote לפני שמצרפים ל-URL
    connect_url = (
        f"{PUBLIC_BASE_URL}/voice/connect"
        f"?customer_phone={quote(_to_e164(customer_phone))}&tenant_id={quote(tenant_id)}"
    )
    call = client.calls.create(
        to=_to_e164(AGENT_PHONE_NUMBER),
        from_=TWILIO_VOICE_FROM,
        url=connect_url,
        status_callback=f"{PUBLIC_BASE_URL}/voice/status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
    )
    return call.sid
