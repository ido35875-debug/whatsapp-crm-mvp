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
