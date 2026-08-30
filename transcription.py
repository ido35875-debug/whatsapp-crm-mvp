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

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")  # נתיב מפורש - עמיד לכל דרך הרצה/פריסה

from whatsapp_send import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN  # noqa: E402 - להורדת מדיה מ-Twilio

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_API_URL = "https://api.openai.com/v1/audio/transcriptions"
CHAT_API_URL = "https://api.openai.com/v1/chat/completions"


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


def transcribe_incoming_voice_message(form) -> dict | None:
    """בודק אם form-data נכנס מ-Twilio (הודעת webhook) הוא הודעה קולית -
    NumMedia>=1 ו-MediaContentType0 מתחיל ב-"audio/" (כך WhatsApp voice notes
    מגיעים - Body ריק, רק מדיה). אם כן - מוריד את המדיה (זמנית, לזיכרון בלבד -
    לא נכתב לדיסק, אין צורך: transcribe_audio שולח את הבייטים ישירות ל-OpenAI)
    ומתמלל אותה. מחזיר:
    - None אם זו בכלל לא הודעה קולית (אין מדיה, או שהמדיה אינה audio/) - הקורא
      (server.py /webhook) יודע להבדיל בין "לא היה מה לתמלל" ל"תמלול נכשל".
    - {"success": True, "text": "<טקסט מתומלל גולמי>"} בהצלחה - בלי קידומת "🎙️";
      זו תוספת תצוגה שהקורא מוסיף, כדי ש-extract_voice_message_fields תקבל טקסט
      נקי לניתוח.
    - {"success": False, "text": "<placeholder ברור עם ⚠️>"} אם ההורדה/התמלול
      נכשלו - **לא** None וגם לא מעלה חריגה - כדי שההודעה עדיין תירשם ב-Inbox/DB
      במקום "להיעלם" בשקט אם OPENAI_API_KEY לא מוגדר/לא תקין, המדיה לא זמינה,
      או שגיאת רשת/API כלשהי. success=False אומר לקורא גם לא לנסות לחלץ שדות
      (extract_voice_message_fields) מ-text - אין טקסט אמיתי לנתח."""
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
        return {"success": True, "text": text}
    except Exception as exc:
        return {"success": False, "text": f"⚠️ [הודעה קולית נכנסת - תמלול אוטומטי נכשל: {exc}]"}


FIELD_EXTRACTION_PROMPT = """\
אתה עוזר שמנתח תמלול של הודעה קולית מלקוח פוטנציאלי בתחום הנדל"ן, ומחלץ ממנה
שלושה שדות: customer_name (שם הלקוח, אם הוזכר), property_type (סוג הנכס המבוקש -
למשל דירה, בית פרטי, משרד, מחסן, אם הוזכר), budget (התקציב שהלקוח ציין, כמחרוזת
כפי שנאמרה - למשל "עד 2 מיליון שקל", אם הוזכר).
אם שדה מסוים לא הוזכר בטקסט, החזר עבורו null. אל תמציא פרטים שלא נאמרו.
החזר אך ורק JSON תקין בפורמט הבא, בלי שום טקסט נוסף:
{"customer_name": "...", "property_type": "...", "budget": "..."}

טקסט התמלול:
"""


def extract_voice_message_fields(text: str) -> dict:
    """מנתח טקסט מתומלל (הודעה קולית נכנסת) עם gpt-4o-mini ומחלץ שם לקוח/סוג
    נכס/תקציב - שדות ממוקדי נדל"ן, ראו "חזון המוצר" ב-CLAUDE.md. מריץ אחרי
    transcribe_incoming_voice_message מצליחה (לא על טקסט placeholder של כישלון).
    מעלה RuntimeError אם OPENAI_API_KEY לא מוגדר. מחזיר תמיד dict עם שלושת
    המפתחות (None לכל שדה שלא הוזכר/לא חולץ) - גם אם ה-JSON שחזר חלקי/לא תקין,
    לא מפיל את הקורא (server.py /webhook), רק מחזיר שדות ריקים."""
    if not OPENAI_API_KEY:
        raise RuntimeError("חילוץ שדות אוטומטי לא מוגדר: חסר OPENAI_API_KEY ב-.env")

    response = requests.post(
        CHAT_API_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": FIELD_EXTRACTION_PROMPT + text}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=30,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}

    return {
        "customer_name": parsed.get("customer_name") or None,
        "property_type": parsed.get("property_type") or None,
        "budget": parsed.get("budget") or None,
    }
