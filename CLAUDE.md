# AI CRM - WhatsApp Multi-Tenant System Architecture

מוצר: AI CRM שמתחבר לוואטסאפ, מחלץ אוטומטית פרטי לקוח מהודעות, ומנהל את מחזור החיים של הליד (Speed-to-Lead, שמירת 100% לידים, Reactivation, תמלול קולי+חילוץ שדות, חיוג רציף).

## ⚠️ קרא לפני כל שינוי/ייבוא נתונים

**מקור האמת היחיד ללידים הוא `customers.json`.** `server.py` **לעולם לא** קורא לידים מטבלת SQL כלשהי. מפתח: `tenant_id::phone` (אין ID מספרי בשום מקום במערכת).

**`crm_data.db` (SQLite) הוא לוג טכני משני בלבד** - `messages`/`calls`/`calendar_tasks`/`scheduler_runs`/`reactivation_batches` (כולם לפי `phone`+`tenant_id`).

**🪤 מלכודת שקרתה בפועל פעמיים (2026-09-09, 2026-09-16):** ל-`crm_data.db` יש גם טבלת `customers` (`phone,name,location,status`, **בלי** `tenant_id`) שהיא **זרה לגמרי לארכיטקטורה** - נוצרה ע"י סקריפטי ייבוא חיצוניים (`sqlite3` ישיר) שכתבו לשם בטעות. `server.py`/ה-UI **אף פעם** לא קוראים ממנה - כל ייבוא שנחת שם היה בלתי-נראה לגמרי בדשבורד. **כל ייבוא לידים (יחיד או בכמות) חייב לעבור אך ורק דרך `extract.import_lead`/`resolve_existing_phone`** (ישירות, או `POST /api/leads/import`/`POST /api/leads`) - **לא** SQL ישיר ל-`crm_data.db`.

## מבנה נתונים בפועל - `customers.json`

**זהו המבנה האמיתי הקיים בקוד/בנתונים - לא סכמה מוצעת.** כל שדה אופציונלי (מתווסף
רק כשיש ערך); אין שדה `role`/`content`/`status` גנרי, אין ID מספרי.

```json
{
  "default::+972501234567": {
    "phone": "+972501234567",
    "tenant_id": "default",
    "customer_name": "דני",
    "business_name": "פרחי הכרמל",
    "location": "חיפה",
    "source_channel": "whatsapp",
    "lead_status": "contacted",
    "status_changed_at": "2026-09-14T23:24:01.474853+00:00",
    "category": "נדל\"ן",
    "agent": "שם הנציג",
    "notes": "הערות חופשיות מהעריכה הידנית",
    "property_type": "בית פרטי",
    "budget": "עד 3,200,000 ש\"ח",
    "import_source": "vape_vcf_import_2026-09-16",
    "history": [
      {
        "timestamp": "2026-09-14T22:47:54.736817+00:00",
        "channel": "whatsapp",
        "direction": "out",
        "message": "טקסט ההודעה",
        "simulated": false
      }
    ]
  }
}
```

- **מפתח:** `"<tenant_id>::<phone>"` - לא ID מספרי, לעולם.
- **`lead_status`** - אחד מ-`VALID_LEAD_STATUSES` ב-`server.py`: `new`/`contacted`/`hot`/`not_relevant`/`reactivation` (לא ערכי סטטוס בעברית, לא `null` בשקט - `null`/חסר = ליד חדש שלא טופל).
- **`history`** - append-only, `direction`: `"in"`(מהלקוח)/`"out"`(מהמערכת), `channel`: `whatsapp`/`voice`/`note`/`followup`. שדה `simulated:true` מסמן הודעה שלא נשלחה בפועל (Trial/כשל ספק).
- **`category`/`agent`/`notes`** - מטא-דאטה חופשית, לא נוגעים ב-`history`/`lead_status`.
- **`property_type`/`budget`** - מחולצים אוטומטית מהודעה קולית (gpt-4o-mini), רלוונטי בעיקר לנדל"ן.
- **קריאה/כתיבה בלעדית דרך `extract.py`** (`load_customers`/`save_customers`/`import_lead`/`update_lead_fields`/`update_lead_status` וכו') - ראו אזהרת המלכודת למעלה.

## חזון המוצר וסטטוס יכולות

| יכולת | סטטוס |
|---|---|
| Speed-to-Lead (מענה אוטומטי מיידי ב-`/webhook`) | ✅ בנוי; אין מדידת SLA מפורשת |
| שמירת 100% אנשי קשר (כל הודעה → כרטיס ב-`customers.json`) | ✅ בנוי |
| CRM אוטונומי (`index.html`: טבלה/קנבן/Inbox/יומן/משוב/חיפוש/ציון חום) | ✅ בנוי |
| מנוע Reactivation (סריקה + פנייה מותאמת) | ✅ בנוי (`reactivate.py`+`scheduler.py`) |
| תמלול קולי + חילוץ שדות אוטומטי (Whisper + gpt-4o-mini) | ✅ בנוי ונבדק E2E עם קריאות אמיתיות |
| פנייה אוטומטית ב-WhatsApp על שדות חסרים בכרטיס | ❌ לא ממומש |
| חיוג רציף (power dialer) + Click-to-Call (גישור אמיתי) | ✅ תור/UI בנוי; ⚠️ גישור Voice אמיתי לא נבדק (חסר `AGENT_PHONE_NUMBER`/`PUBLIC_BASE_URL`) |

## מפת קבצים

| קובץ | תפקיד |
|---|---|
| `extract.py` | מנוע CRM - כל קריאה/כתיבה ל-`customers.json` עוברת דרכו בלבד |
| `server.py` | Webhook רב-ערוצי (WhatsApp אמיתי; IG/FB placeholder) + API + דשבורד ב-`/` |
| `index.html` | דשבורד (טבלה/קנבן/Inbox/פאנלים) |
| `whatsapp_provider.py` | Provider pattern - `WHATSAPP_PROVIDER` ב-`.env` בוחר Twilio/GreenAPI/UltraMsg/Mock |
| `reactivate.py` | ניסוח+שליחת הודעות חימום, rate limiting 15-30s, batch tracking |
| `scheduler.py` / `scheduler_worker.py` | סריקה תקופתית ללידים קרים + follow-up אוטומטי (thread מקומי / process נפרד ל-production) |
| `db.py` | שכבת גישה ל-`crm_data.db` (לוג טכני בלבד - ראו אזהרה למעלה) |
| `paths.py` | `DATA_DIR` משותף לקבצי state (`customers.json`/`crm_data.db`/`chat_history.txt`) |
| `voice_call.py` | Click-to-Call (גישור Twilio Voice) |
| `transcription.py` | Whisper STT + חילוץ שדות gpt-4o-mini |
| `prompts.py`/`prompts.json` | תבניות פרומפט לכל סוכני ה-AI, ניתנות לעריכה חיה מה-UI |
| `tenant_settings.py`/`tenant_settings.json` | הגדרות פר-tenant (למשל סף ימים ל-Reactivation) |

## ספק WhatsApp פעיל: UltraMsg

- **`WHATSAPP_PROVIDER=ultramsg`** - הספק הפעיל כרגע, נבדק end-to-end עם הודעה אמיתית שהתקבלה בטלפון.
- Twilio: תקוע ב-Trial (כל שליחה יזומה נחסמת/`simulated` אלא אם הנמען Verified). Green API: נוסה, `401` לא-מוסבר בסביבה הזו - קוד נשאר בפרויקט, לא בשימוש.
- `UltraMsgProvider` מנרמל טלפון ישראלי דרך `whatsapp_send._to_e164` לפני שליחה (בלי זה, מספרים מקומיים `0XXXXXXXXX` מסומנים `Invalid` בלוח הבקרה של UltraMsg).
- **⚠️ אין יותר "רשת ביטחון" של Trial חוסם.** UltraMsg שולח הודעות אמיתיות בפועל - `SCHEDULER_AUTO_SEND`/`SCHEDULER_AUTO_FOLLOWUP` (כרגע `false`/לא מוגדרים) וכל קריאה עם `send:true` ישלחו אמיתית ללידים ב-`contacts.csv`. **חלק מהלידים ב-`contacts.csv` הם עדיין נתוני-דמה** (לא מספרי WhatsApp אמיתיים) - להחליף לפני קמפיין production.

## נתוני לידים

- `customers.json`: מקור האמת, ~**382 לידים** (עודכן 2026-09-16, ייבוא רשימת וייפים דרך `extract.import_lead`).
- **פורמטים לא-עקביים היסטורית** - חלק מהכרטיסים במפתח טלפון מקומי (`0501234567`), חלק ב-E.164 (`+972501234567`) לאותו מספר אמיתי → כרטיסים כפולים ישנים שלא אוחדו אוטומטית. `resolve_existing_phone` מונע כפילות *חדשה* בלבד; ניקוי כפילויות קיימות עדיין ידני.
- `crm_data.db`'s `customers` table (הטבלה הזרה) מכילה כ-305 שורות יתומות משיירי ייבוא ישן - לא בשימוש, לא נמחקות אוטומטית.

## מגבלות ידועות

- אין Multi-Tenant אמיתי - `customers.json` קובץ יחיד גלובלי, בלי בידוד עסקים.
- Omnichannel חלקי - רק WhatsApp מחובר בפועל; IG/FB placeholder בלבד.
- `customers.json` כקובץ שטוח - לא בטוח לכתיבה מקבילית תחת עומס production אמיתי.
- `/api/*` ללא אימות/הרשאה בכלל - כל מי שמגיע לכתובת יכול לקרוא/לשנות הכל.
- סודות (`TWILIO_AUTH_TOKEN` וכו') בטקסט גלוי ב-`.env` - אין secret manager.
- אין HTTPS/rate limiting באפליקציה עצמה (מניחים proxy/ngrok/LB מול production).
- Voice Twilio (גישור אמיתי) לא מוגדר/לא נבדק - חסרים `AGENT_PHONE_NUMBER`/`PUBLIC_BASE_URL`.
- Gunicorn לא רץ על Windows (תלוי `fcntl`) - נבדק רק ייבוא `server:app`, לא הרצה מלאה מקומית; יעד production הוא קונטיינר Linux.
- ציון חום (`db.compute_lead_score`) הוא שדה מחושב-בלבד בכוונה - לא ניתן לעריכה ידנית.

## חוקי בטיחות קשיחים (לא לשנות בלי אישור מפורש)

1. **אף סוכן לא סוגר עסקה, מוחק נתונים, או יוזם פנייה ראשונה ללקוח בלי אישור אנושי מפורש.**
   - חריג: מענה אוטומטי חוזר בתוך חלון שיחה שהלקוח פתח (`/webhook`) - מותר, זו לא פנייה יזומה.
   - חריג: `POST /api/messages/send` (קליק "שלח" ידני של נציג) - האישור הוא הקליק עצמו; חלה עדיין מדיניות חלון-24h של מטא (לא נאכפת אוטומטית בקוד - אחריות הנציג).
2. **קמפיין Reactivation (`--send`/`/api/reactivate` עם `send:true`) דורש אישור אנושי לכל הרצה** - ה-UI אוכף תצוגה מקדימה (`send:false`) לפני `confirm()` ורק אז שליחה אמיתית.
3. **`SCHEDULER_AUTO_SEND`/`SCHEDULER_AUTO_FOLLOWUP`** - כבויים כברירת מחדל (dry-run). לא להדליק בלי הודעות-תבנית מאושרות מול מטא לכל ורטיקל + החלטה עסקית מודעת.
4. **ייבוא לידים תמיד דרך `extract.import_lead`/`resolve_existing_phone`** - לעולם לא SQL ישיר ל-`crm_data.db` (ראו אזהרת המלכודת למעלה).
5. **שינוי טלפון לליד קיים (rekey)** חייב לעדכן גם מפתח `customers.json` וגם `phone` בשלוש טבלאות ה-SQLite (`db.rekey_phone`) - לא לבצע `UPDATE` חלקי.
6. **היסטוריית הודעות (`history`/`messages`) היא append-only** - אף עריכה (כולל עריכת שיחה) לא נוגעת בלוג ההודעות שכבר הוצג ללקוח/בצ'אט.

## חלוקת סוכנים (roles - חלקם roadmap בלבד, לא קוד רץ)

| סוכן | קבצים | אסור |
|---|---|---|
| DevOps | `server.py`, `requirements.txt` | לגעת ב-`customers.json` ישירות |
| QA/Bug Finder | `extract.py` (פרומפטים), לוגים | לשנות לוגיקת עסקים בלי אישור |
| Cold Lead Reactivation | `reactivate.py`, `contacts.csv` | לשלוח `--send`/`send:true`/production ללא תבנית מאושרת/אישור |
| Sales & Objection | טרם נוצר (`sales_agent.py`) | לסגור עסקה/להתחייב על מחיר בלי אישור |
| Scheduling & Follow-Up | טרם נוצר (`scheduling_agent.py`) | למחוק/לשנות פגישות בלי אישור |
| BI & Analytics | טרם נוצר (`analytics.py`) | לשנות נתוני לקוחות (read-only) |
