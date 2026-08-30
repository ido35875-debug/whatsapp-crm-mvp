"""
תשתית תמלול קולי (Speech-to-Text) - עטיפה דקה ל-OpenAI Whisper API, באותו דפוס
בדיוק כמו whatsapp_send.py/voice_call.py: בודקת תצורה בזמן קריאה (לא בזמן import),
ומעלה RuntimeError ידידותי אם לא מוגדרת - כדי שהשרת ימשיך לעלות ולשרת את שאר
המערכת גם בלי ספק STT מוגדר. הקורא (server.py: POST /api/calls/<id>/transcribe)
תופס את זה ומחזיר שגיאה ברורה ל-UI, שחוזר לזרימת הקלדת הערות ידנית הקיימת.

למה קריאת HTTP ישירה עם requests ולא ה-package הרשמי openai: נמנעים מתלות pip
כבדה נוספת ב-requirements.txt - requests כבר מותקן טרנזיטיבית (twilio/anthropic
תלויים בו), אז זו קריאת REST פשוטה בלי ספרייה חדשה.
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

from whatsapp_send import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN  # noqa: E402 - להורדת מדיה מ-Twilio

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_API_URL = "https://api.openai.com/v1/audio/transcriptions"


def transcribe_audio(file_bytes: bytes, filename: str) -> str:
    """שולח קובץ אודיו ל-OpenAI Whisper API (language="he" - עברית) ומחזיר את
    הטקסט המתומלל. מעלה RuntimeError אם OPENAI_API_KEY לא מוגדר ב-.env, או
    requests.HTTPError אם OpenAI עצמו דחה את הבקשה (מפתח לא תקין, קובץ פגום וכו')."""
    if not OPENAI_API_KEY:
        raise RuntimeError("תמלול אוטומטי לא מוגדר: חסר OPENAI_API_KEY ב-.env")

    response = requests.post(
        WHISPER_API_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        files={"file": (filename, file_bytes)},
        data={"model": "whisper-1", "language": "he"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["text"].strip()


def download_twilio_media(media_url: str) -> bytes:
    """מוריד קובץ מדיה (הודעה קולית נכנסת וכו') מכתובת Twilio - כתובות מדיה של
    Twilio מוגנות ודורשות Basic Auth עם Account SID + Auth Token, בדיוק כמו כל
    קריאת REST API אחרת ל-Twilio (לא ציבורי/פתוח)."""
    response = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
    response.raise_for_status()
    return response.content


def transcribe_incoming_voice_message(form) -> str | None:
    """בודק אם form-data נכנס מ-Twilio (הודעת webhook) הוא הודעה קולית -
    NumMedia>=1 ו-MediaContentType0 מתחיל ב-"audio/" (כך WhatsApp voice notes
    מגיעים - Body ריק, רק מדיה). אם כן - מוריד את המדיה (זמנית, לזיכרון בלבד -
    לא נכתב לדיסק, אין צורך: transcribe_audio שולח את הבייטים ישירות ל-OpenAI)
    ומתמלל אותה. מחזיר:
    - None אם זו בכלל לא הודעה קולית (אין מדיה, או שהמדיה אינה audio/) - הקורא
      (server.py /webhook) יודע להבדיל בין "לא היה מה לתמלל" ל"תמלול נכשל".
    - "🎙️ <טקסט מתומלל>" בהצלחה.
    - טקסט placeholder ברור (⚠️) אם ההורדה/התמלול נכשלו - **לא** None וגם לא
      מעלה חריגה - כדי שההודעה עדיין תירשם ב-Inbox/DB במקום "להיעלם" בשקט אם
      OPENAI_API_KEY לא מוגדר, המדיה לא זמינה, או שגיאת רשת/API כלשהי."""
    try:
        num_media = int(form.get("NumMedia", "0") or "0")
    except ValueError:
        num_media = 0
    if num_media < 1:
        return None

    content_type = form.get("MediaContentType0", "")
    if not content_type.startswith("audio/"):
        return None

    media_url = form.get("MediaUrl0", "")
    try:
        audio_bytes = download_twilio_media(media_url)
        filename = "voice_note." + (content_type.split("/")[-1] or "ogg")
        text = transcribe_audio(audio_bytes, filename)
        return f"🎙️ {text}"
    except Exception as exc:
        return f"⚠️ [הודעה קולית נכנסת - תמלול אוטומטי נכשל: {exc}]"
