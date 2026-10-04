"""
whatsapp_provider.py — שכבת הפשטה (Provider Pattern) לשליחה/קבלה של הודעות
WhatsApp, כדי לתמוך במספר ספקים (Twilio, Green API, Mock לבדיקות) מאחורי ממשק
אחיד - שאר האפליקציה (server.py) לא צריכה לדעת איזה ספק פעיל, רק לקרוא ל-
get_provider(). הספק הפעיל נבחר לפי WHATSAPP_PROVIDER ב-.env (ברירת מחדל
"twilio" - לא משנה התנהגות קיימת אצל מי שלא הגדיר את המשתנה בכלל).

היקף מכוון (לא נשכח, לא ממומש בכוונה): שיחות קוליות (Twilio Voice, voice_call.py,
מסלולי /voice/*) לא עוברות דרך השכבה הזו בכלל ונשארות קשיחות מול Twilio - Green
API הוא ספק הודעות טקסט/מדיה, לא ספק שיחות.

הודעות קוליות נכנסות (WhatsApp voice notes, לא Twilio Voice call) כן נתמכות
בצורה אחידה לכל ספק: extract_voice_media מנרמל את ה-payload הגולמי (form-encoded
NumMedia/MediaContentType0/MediaUrl0 אצל Twilio, JSON typeMessage="audioMessage"
אצל Green API) למבנה משותף {"media_url", "content_type"}; download_media מוריד
את הקובץ (Twilio דורש Basic Auth, Green API לא); ומשם transcription.
transcribe_incoming_voice_message (ראו שם) מתמלל בדיוק כמו קודם, ללא שינוי -
ולכן גם ה-AI Copilot (שקורא רק מהיסטוריית ההודעות המאוחדת ב-db/customers.json,
לא ממבנה גולמי כלשהו) ממשיך לעבוד ללא שום שינוי משלו.
"""
from __future__ import annotations

import abc
import logging
import os

import requests

logger = logging.getLogger("whatsapp_crm")

def _default_green_api_url(instance_id: str) -> str:
    # Green API (מ-2024 ואילך) מקצה לכל instance host ייעודי (לא שער גנרי
    # משותף) - הפורמט: https://<4 הספרות הראשונות של idInstance>.api.greenapi.com
    # (אומת ישירות מול לוח הבקרה של המשתמש: idInstance=710722735544 -> apiUrl
    # https://7107.api.greenapi.com). זו ברירת מחדל *מחושבת*, לא hardcoded לחשבון
    # ספציפי אחד - כך שספק אחר עם idInstance שונה יקבל את ה-host הנכון שלו
    # אוטומטית. GREEN_API_URL ב-.env (אופציונלי) דורס את זה במפורש אם צריך -
    # למשל אם התבנית הזו משתנה אצל Green API בעתיד, בלי לחכות לתיקון קוד.
    return f"https://{instance_id[:4]}.api.greenapi.com" if len(instance_id) >= 4 else "https://api.greenapi.com"


class WhatsAppProvider(abc.ABC):
    """ממשק אחיד לכל ספק WhatsApp - send_message לשליחה יזומה, parse_webhook
    לפענוח בקשת webhook נכנסת גולמית (Flask request) לזוג (contact_id, message_text)
    מנורמל, ללא תלות במבנה ה-payload הספציפי לספק."""

    @abc.abstractmethod
    def send_message(self, to: str, text: str) -> str:
        """שולח הודעה, מחזיר מזהה הודעה (SID/idMessage/מזהה מדומה - תלוי ספק)."""

    @abc.abstractmethod
    def parse_webhook(self, request) -> tuple[str, str]:
        """מחזיר (contact_id, message_text) - ("", "") אם הבקשה לא הודעה נכנסת
        תקינה (למשל webhook מסוג status ולא הודעה, אצל Green API)."""

    def verify_webhook(self, request) -> bool:
        """True כברירת מחדל - ספקים שאין להם מנגנון אימות חתימה ייעודי (Green
        API/Mock) סומכים על כך שכתובת ה-webhook עצמה סודית. TwilioProvider דורס
        עם אימות HMAC אמיתי (ראו server._verify_twilio_request)."""
        return True

    def requires_twiml_reply(self) -> bool:
        """True רק עבור Twilio - שם תגובת ה-webhook עצמה מכילה TwiML עם ההודעה
        היוצאת (בתוך אותה תגובת HTTP). ספקים אחרים שולחים תשובה כקריאת API יזומה
        נפרדת (send_message), לא inline."""
        return False

    def is_soft_failure(self, exc: Exception) -> bool:
        """True אם exc הוא כשל 'ידוע ותקין' (למשל חסימת Trial ב-Twilio) שאמור
        להירשם כ-simulated ולא כשגיאת שרת אמיתית (502 ב-/api/messages/send).
        ברירת מחדל False - כל שגיאה נחשבת אמיתית, אלא אם ספק ספציפי יודע אחרת."""
        return False

    def extract_voice_media(self, request) -> dict | None:
        """אם בקשת ה-webhook הנוכחית היא הודעה קולית נכנסת (voice note), מחזיר
        מבנה נתונים אחיד {"media_url": str, "content_type": str} - ללא קשר
        למבנה ה-payload הגולמי הספציפי-ספק (form-encoded אצל Twilio, JSON אצל
        Green API). זה בדיוק ה"נורמליזציה" שמאפשרת ל-transcription.py ו-AI
        Copilot להמשיך לעבוד מול מבנה אחיד בלי לדעת משהו על הספק. None אם זו
        לא הודעה קולית, או שהספק לא תומך בזיהוי הודעות קוליות (ברירת מחדל)."""
        return None

    def download_media(self, media_url: str) -> bytes:
        """מוריד את קובץ המדיה מ-media_url שהוחזר ע"י extract_voice_media.
        ברירת מחדל: הורדה פשוטה בלי אימות מיוחד (מתאים ל-Green API - ה-downloadUrl
        שהיא מחזירה כבר נגיש ציבורית). TwilioProvider דורס עם Basic Auth
        (Account SID + Auth Token) - כתובות המדיה של Twilio מוגנות ודורשות את זה."""
        resp = requests.get(media_url, timeout=30)
        resp.raise_for_status()
        return resp.content


class TwilioProvider(WhatsAppProvider):
    """עוטף את whatsapp_send.py הקיים - send_whatsapp_message כבר תומך במצב
    TESTING=True (MockMessenger) ובזיהוי חסימת Trial (is_trial_restriction), ולא
    משוכפל כאן. אימות החתימה (verify_webhook) נשאר ב-server.py (_verify_twilio_request/
    VERIFY_TWILIO_SIGNATURE) כי הוא משותף גם למסלולי /voice/* שלא עוברים דרך
    ה-Provider בכלל - ראו server.webhook לאופן שבו זה מחובר יחד בלי circular import."""

    def send_message(self, to: str, text: str) -> str:
        from whatsapp_send import send_whatsapp_message
        return send_whatsapp_message(to, text)

    def parse_webhook(self, request) -> tuple[str, str]:
        # Twilio (WhatsApp) שולח form-encoded עם השדות From ו-Body
        contact_id = request.form.get("From", "").replace("whatsapp:", "")
        message_text = request.form.get("Body", "")
        return contact_id, message_text

    def requires_twiml_reply(self) -> bool:
        return True

    def is_soft_failure(self, exc: Exception) -> bool:
        from whatsapp_send import is_trial_restriction
        return is_trial_restriction(exc)

    def extract_voice_media(self, request) -> dict | None:
        # WhatsApp voice notes מגיעים מ-Twilio עם Body ריק ורק מדיה: NumMedia>=1
        # ו-MediaContentType0 מתחיל ב-"audio/".
        try:
            num_media = int(request.form.get("NumMedia", "0") or "0")
        except ValueError:
            num_media = 0
        if num_media < 1:
            return None
        content_type = request.form.get("MediaContentType0", "")
        if not content_type.startswith("audio/"):
            return None
        media_url = request.form.get("MediaUrl0", "")
        if not media_url:
            return None
        return {"media_url": media_url, "content_type": content_type}

    def download_media(self, media_url: str) -> bytes:
        from transcription import download_twilio_media
        return download_twilio_media(media_url)


class MockProvider(WhatsAppProvider):
    """ספק בדיקות טהור - WHATSAPP_PROVIDER=mock בוחר את הספק הזה כליל (לא רק
    מדמה שליחה בתוך ספק אמיתי, כמו TESTING=True עושה ל-Twilio - זו בחירת ספק
    שלמה). לא פונה לשום API חיצוני. פענוח ה-webhook משתמש באותו פורמט From/Body
    כמו Twilio בכוונה - כדי שסקריפטי הבדיקה הקיימים (test_suite.py/
    simulate_webhook.py, שכבר בנויים סביב הפורמט הזה) ימשיכו לעבוד ללא שינוי."""

    def send_message(self, to: str, text: str) -> str:
        from whatsapp_send import MockMessenger
        return MockMessenger.send(from_="mock:crm", to=f"mock:{to}", body=text)

    def parse_webhook(self, request) -> tuple[str, str]:
        contact_id = request.form.get("From", "").replace("whatsapp:", "")
        message_text = request.form.get("Body", "")
        return contact_id, message_text


_GREEN_TEXT_KEYS = ("textMessage", "text", "caption")


def _green_text_from_message_data(message_data: dict) -> str:
    """טקסט של הודעה נכנסת מ-Green API - מכסה את המבנים הנפוצים (textMessageData,
    extendedTextMessageData, caption של מדיה) ואז חיפוש רקורסיבי כגיבוי, כדי שגרסה
    אחרת של המבנה לא תחזיר טקסט ריק בשקט."""
    for key in ("textMessageData", "extendedTextMessageData"):
        block = message_data.get(key) or {}
        for field in _GREEN_TEXT_KEYS:
            value = block.get(field)
            if isinstance(value, str) and value.strip():
                return value

    for value in message_data.values():
        if isinstance(value, dict):
            for field in _GREEN_TEXT_KEYS:
                text = value.get(field)
                if isinstance(text, str) and text.strip():
                    return text
    return ""


class GreenAPIProvider(WhatsAppProvider):
    """מתחבר ל-Green API (https://green-api.com) - שירות WhatsApp API חיצוני
    שמתחבר למספר WhatsApp אמיתי (לא Sandbox) דרך סריקת QR, ללא צורך באישור Meta
    Business/הודעות-תבנית. GREEN_API_INSTANCE_ID + GREEN_API_TOKEN נדרשים ב-.env -
    נלקחים מלוח הבקרה של Green API אחרי יצירת instance.

    פורמט webhook נכנס (typeWebhook="incomingMessageReceived") ופורמט שליחה
    (sendMessage) מתועדים ב-https://green-api.com/docs/ - לא אומתו כאן מול חשבון
    Green API אמיתי (ראו test_green_api.py לבדיקת חיבור/QR בנפרד); ה-shape
    שממומש כאן הוא הפורמט המתועד הרשמי, לא ניחוש."""

    def __init__(self):
        self.instance_id = os.environ.get("GREEN_API_INSTANCE_ID", "").strip()
        self.token = os.environ.get("GREEN_API_TOKEN", "").strip()
        if not (self.instance_id and self.token):
            raise RuntimeError(
                "חסרים פרטי חיבור ל-Green API ב-.env: GREEN_API_INSTANCE_ID, GREEN_API_TOKEN"
            )
        # GREEN_API_URL אופציונלי ב-.env - עוקף את הניחוש המחושב אם מוגדר (ראו
        # _default_green_api_url) - להעתקה ישירה מ"apiUrl" בלוח הבקרה, בלי תלות
        # בכך שדפוס-הפרפיקס ימשיך להיות תקף.
        self.api_url = os.environ.get("GREEN_API_URL", "").strip() or _default_green_api_url(self.instance_id)

    def _url(self, method: str) -> str:
        return f"{self.api_url}/waInstance{self.instance_id}/{method}/{self.token}"

    def send_message(self, to: str, text: str) -> str:
        digits = to.replace("whatsapp:", "").replace("+", "").strip()
        chat_id = f"{digits}@c.us"
        resp = requests.post(self._url("sendMessage"), json={"chatId": chat_id, "message": text}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "idMessage" not in data:
            raise RuntimeError(f"Green API לא החזיר idMessage בתגובה: {data}")
        return data["idMessage"]

    def parse_webhook(self, request) -> tuple[str, str]:
        data = request.get_json(silent=True) or {}
        type_webhook = data.get("typeWebhook")
        if type_webhook == "quotaExceeded":
            logger.error("🚫 Green API: המכסה נגמרה (quotaExceeded) - לא מתקבלות הודעות ולא נשלחים מענים עד חידוש/שדרוג המסלול")
        elif type_webhook == "stateInstanceChanged":
            logger.warning("⚠️ Green API: סטטוס ה-Instance השתנה: %s", data.get("stateInstance"))
        if type_webhook != "incomingMessageReceived":
            # סוגי webhook אחרים של Green API (סטטוס שליחה, שינוי מצב instance וכו') -
            # לא הודעה נכנסת, לא רלוונטי לצינור העיבוד - ("", "") גורם ל-200 שקט
            # ב-server.webhook (לא 400 - ראו שם; Green API יכול להיכנס ל-backoff
            # על תגובות שאינן 2xx), בלי לרשום אותו כהודעה ריקה/שגויה.
            return "", ""

        sender = ((data.get("senderData") or {}).get("chatId") or "")
        if sender.endswith("@g.us"):
            # הודעה מקבוצת וואטסאפ, לא משיחה פרטית - שלב 8 (הקשחת webhook): בלי
            # הסינון הזה, כל "רעש" קבוצתי (קבוצות משפחה/חברים) היה עובר דרך
            # pipeline הלידים המלא (חילוץ פרטים + מענה AI אוטומטי ב-Claude) -
            # עומס/עלות מיותרים לגמרי, ובעיקר מסוכן: תשובה אוטומטית שנשלחת
            # *לתוך קבוצה* נראית כמו ספאם בוטי, לא שירות לקוחות. ("", "") - אותו
            # נתיב "אין מה לעבד" כמו למעלה, 200 שקט.
            return "", ""
        digits = sender.split("@")[0] if "@" in sender else sender
        contact_id = f"+{digits}" if digits.isdigit() else digits

        message_data = data.get("messageData") or {}
        msg_type = message_data.get("typeMessage")
        if msg_type == "audioMessage":
            # הודעה קולית - אין כאן טקסט ישיר; server.webhook יפנה בנפרד ל-
            # extract_voice_media+transcribe_incoming_voice_message (בדיוק כמו
            # אצל Twilio) - כאן מחזירים מחרוזת ריקה, לא מנסים לתמלל בתוך parse_webhook.
            message_text = ""
        else:
            # מדיה אחרת (תמונה/מסמך/מיקום וכו') - לא ממומשת (ראו הערת ההיקף בראש הקובץ)
            message_text = _green_text_from_message_data(message_data)
            if not message_text:
                logger.info("[GreenAPIProvider] הודעה נכנסת מסוג %s ללא טקסט - התעלמות", msg_type)

        return contact_id, message_text

    def extract_voice_media(self, request) -> dict | None:
        """נורמליזציה של payload הודעה קולית של Green API (typeMessage=
        "audioMessage", עם fileMessageData.downloadUrl/mimeType) למבנה האחיד
        {"media_url", "content_type"} - אותו מבנה בדיוק ש-TwilioProvider מחזיר.
        בניגוד ל-Twilio, ה-downloadUrl של Green API כבר נגיש ציבורית בלי אימות -
        ראו download_media (ברירת המחדל ב-WhatsAppProvider, לא נדרס כאן)."""
        data = request.get_json(silent=True) or {}
        if data.get("typeWebhook") != "incomingMessageReceived":
            return None
        message_data = data.get("messageData") or {}
        if message_data.get("typeMessage") != "audioMessage":
            return None
        file_data = message_data.get("fileMessageData") or {}
        media_url = file_data.get("downloadUrl", "")
        content_type = file_data.get("mimeType", "audio/ogg")
        if not media_url:
            return None
        return {"media_url": media_url, "content_type": content_type}


class UltraMsgProvider(WhatsAppProvider):
    """מתחבר ל-UltraMsg (https://ultramsg.com) - שירות WhatsApp API חיצוני נוסף,
    חלופי ל-Green API/Twilio - נבחר אחרי שחיבור Green API נתקע ב-401 בלתי-פתור
    מהסביבה הזו ספציפית (ראו הדיון המלא ב-CLAUDE.md/היסטוריית השיחה), בזמן
    שה-instance ב-UltraMsg כבר מקושר ופעיל בפועל אצל המשתמש.
    ULTRAMSG_INSTANCE_ID + ULTRAMSG_TOKEN נדרשים ב-.env (מלוח הבקרה של UltraMsg).

    send_message מבוסס על מפרט מדויק שנמסר: POST https://api.ultramsg.com/
    {instance}/messages/chat עם payload form-encoded {token, to, body} - זה
    *נבדק בפועל* מול שליחה אמיתית (ראו test_ultramsg_send.py). parse_webhook
    מבוסס על הפורמט המתועד של UltraMsg - **לא אומת** כאן מול webhook אמיתי
    (רק שליחה נבדקה בפועל עד כה) - ראו ההיקף המכוון בראש הקובץ."""

    def __init__(self):
        self.instance_id = os.environ.get("ULTRAMSG_INSTANCE_ID", "").strip()
        self.token = os.environ.get("ULTRAMSG_TOKEN", "").strip()
        if not (self.instance_id and self.token):
            raise RuntimeError(
                "חסרים פרטי חיבור ל-UltraMsg ב-.env: ULTRAMSG_INSTANCE_ID, ULTRAMSG_TOKEN"
            )
        self.base_url = os.environ.get("ULTRAMSG_URL", "").strip() or "https://api.ultramsg.com"

    def send_message(self, to: str, text: str) -> str:
        # UltraMsg דורש מספר בפורמט בינלאומי מלא בלי "+" (למשל "972502222222") -
        # contacts.csv מכיל גם מספרים בפורמט מקומי ישראלי ("0502222222", 0 מוביל
        # בלי קידומת מדינה) - בלי נרמול, ההודעה מתקבלת ב-API (200 + idMessage)
        # אבל נכשלת בפועל אצל UltraMsg ("Invalid" בלוח הבקרה שלהם) כי זה לא מספר
        # תקין. משתמשים ב-_to_e164 המשותף (whatsapp_send.py, אותה לוגיקה בדיוק
        # ש-Twilio כבר משתמש בה - לא כפילות) ומסירים את ה-"+" בסוף, כי זה ספציפי
        # לפורמט ש-UltraMsg דורש.
        from whatsapp_send import _to_e164
        raw = to.replace("whatsapp:", "").strip()
        digits = _to_e164(raw).lstrip("+")
        url = f"{self.base_url}/{self.instance_id}/messages/chat"
        # לוג מלא של הבקשה בפועל - לא רק ה-token (לעולם לא נדפס) - כדי שכל שליחה
        # אמיתית תהיה שקופה ל-100% (נדרש במפורש אחרי חוסר-התאמה בין "הצליח ב-API"
        # ל"Invalid בלוח הבקרה של UltraMsg" - ראו CLAUDE.md).
        logger.info("[UltraMsgProvider] POST %s | to=%r (len=%d) | body_len=%d", url, digits, len(digits), len(text))
        resp = requests.post(
            url,
            data={"token": self.token, "to": digits, "body": text},
            timeout=15,
        )
        logger.info("[UltraMsgProvider] response status=%s body=%s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
        # UltraMsg מחזיר לפעמים HTTP 200 עם error מפורש בגוף (לא רק סטטוס לא-200) -
        # למשל instance לא מקושר/quota - זה כן כשל אמיתי, לא הצלחה.
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"UltraMsg דחה את השליחה: {data['error']}")
        message_id = data.get("id") if isinstance(data, dict) else None
        if message_id is None and isinstance(data, dict):
            nested = data.get("message")
            if isinstance(nested, dict):
                message_id = nested.get("id")
        return str(message_id) if message_id is not None else str(data)

    def parse_webhook(self, request) -> tuple[str, str]:
        # פורמט מתועד (webhook_url בהגדרות ה-instance) - לא אומת מול webhook
        # אמיתי בסביבה הזו (ראו docstring המחלקה).
        data = request.get_json(silent=True) or {}
        payload = data.get("data") or {}
        if payload.get("fromMe"):
            return "", ""  # הודעה יוצאת מאיתנו (עקבנו אחריה כבר), לא נכנסת מהלקוח
        sender = payload.get("from", "")
        if sender.endswith("@g.us"):
            return "", ""  # הודעת קבוצה - ראו ההערה המלאה ב-GreenAPIProvider.parse_webhook
        digits = sender.split("@")[0] if "@" in sender else sender
        contact_id = f"+{digits}" if digits.isdigit() else digits
        message_text = payload.get("body", "") if payload.get("type") == "chat" else ""
        return contact_id, message_text


class DryRunProviderProxy(WhatsAppProvider):
    """עוטפת את ה-Provider האמיתי הפעיל כש-DRY_RUN=true ב-.env (שלב 6 - סגירת
    פיילוט) - "מצב צל": כל מתודות ה**קבלה**/פענוח (parse_webhook, verify_webhook,
    requires_twiml_reply, is_soft_failure, extract_voice_media, download_media)
    מואצלות לספק האמיתי בלי שום שינוי - webhooks נכנסים אמיתיים (UltraMsg/
    Green API אמיתיים, לבדיקה נגד תעבורה אמיתית) ממשיכים להתפענח נכון. רק
    send_message (השליחה **החוצה**) מוחלפת בסימולציה (MockMessenger, כמו
    WHATSAPP_PROVIDER=mock) - שום הודעת WhatsApp אמיתית לא יוצאת ללקוח, בלי
    לאבד את היכולת לבדוק קליטת הודעות אמיתיות/AI/UI.

    שונה במכוון מ-WHATSAPP_PROVIDER=mock (מחליף הכל, כולל פענוח נכנס - ספק
    בדיקות טהור, לא "מצב צל" על ספק אמיתי). לא מכסה את מסלול Twilio TwiML -
    שם ה"שליחה" היא התגובה הסינכרונית עצמה לבקשת ה-webhook (אין send_message
    נפרד לעטוף), ראו הטיפול הנפרד ב-DRY_RUN בתוך server.webhook."""

    def __init__(self, real: WhatsAppProvider):
        self._real = real

    def send_message(self, to: str, text: str) -> str:
        from whatsapp_send import MockMessenger
        logger.info("[DRY_RUN] מדמה שליחת הודעה (לא נשלח בפועל) אל %s: %s", to, text)
        return MockMessenger.send(from_="dry-run:crm", to=f"dry-run:{to}", body=text)

    def parse_webhook(self, request) -> tuple[str, str]:
        return self._real.parse_webhook(request)

    def verify_webhook(self, request) -> bool:
        return self._real.verify_webhook(request)

    def requires_twiml_reply(self) -> bool:
        return self._real.requires_twiml_reply()

    def is_soft_failure(self, exc: Exception) -> bool:
        return self._real.is_soft_failure(exc)

    def extract_voice_media(self, request) -> dict | None:
        return self._real.extract_voice_media(request)

    def download_media(self, media_url: str) -> bytes:
        return self._real.download_media(media_url)


_PROVIDER_INSTANCES: dict[str, WhatsAppProvider] = {}


def get_provider(name: str | None = None) -> WhatsAppProvider:
    """מחזיר את מופע ה-Provider הפעיל (נבנה פעם אחת ונשמר במטמון per-name - כדי
    לא לבנות מחדש RequestValidator/session בכל בקשה). name=None (ברירת מחדל)
    קורא את WHATSAPP_PROVIDER מה-.env *בזמן הקריאה* - לא נשמר כקבוע ב-import
    time - כדי שסקריפטים (test_green_api.py) יוכלו לבחור ספק מפורש בלי restart.

    DRY_RUN=true ב-.env (שלב 6, ברירת מחדל false - לא משנה התנהגות קיימת למי
    שלא הגדיר) עוטפת את הספק האמיתי שנבחר ב-DryRunProviderProxy - כל השליחות
    היוצאות (auto-reply/reactivate.py/outbound_engine.py - כולן קוראות
    get_provider() ואז .send_message, ולא בונות provider משלהן) הופכות
    לסימולציה במכה אחת, בלי לגעת בכל אחד מהקבצים האלה בנפרד. נבדק בזמן קריאה
    (לא נשמר במטמון per-name כמו הספק עצמו) - עקבי עם WHATSAPP_PROVIDER מעל."""
    name = (name or os.environ.get("WHATSAPP_PROVIDER", "twilio")).strip().lower()
    if name not in _PROVIDER_INSTANCES:
        if name == "twilio":
            _PROVIDER_INSTANCES[name] = TwilioProvider()
        elif name == "green_api":
            _PROVIDER_INSTANCES[name] = GreenAPIProvider()
        elif name == "mock":
            _PROVIDER_INSTANCES[name] = MockProvider()
        elif name == "ultramsg":
            _PROVIDER_INSTANCES[name] = UltraMsgProvider()
        else:
            raise ValueError(f"WHATSAPP_PROVIDER לא מוכר: {name!r} (נתמך: mock / green_api / twilio / ultramsg)")
    provider = _PROVIDER_INSTANCES[name]
    if os.environ.get("DRY_RUN", "false").strip().lower() == "true":
        return DryRunProviderProxy(provider)
    return provider
