# AI CRM - WhatsApp Multi-Tenant System Architecture

מוצר: AI CRM שמתחבר לוואטסאפ, מחלץ אוטומטית פרטי לקוח מהודעות, ומנהל את מחזור החיים של הליד (Speed-to-Lead, שמירת 100% לידים, Reactivation, תמלול קולי+חילוץ שדות, חיוג רציף).

## ⚠️ קרא לפני כל שינוי/ייבוא נתונים

**מקור האמת ללידים תלוי ב-`DATABASE_URL`** (`extract._use_database()`), אבל תמיד באותו פורמט כרטיס (ראו "מבנה נתונים" למטה) ותמיד דרך `extract.load_customers`/`save_customers`:
- **`DATABASE_URL` = Postgres (פרודקשן ב-Render):** הכרטיסים נקראים/נכתבים לטבלת Postgres `customers` (`cust_key` = `tenant_id::phone`, `data` = כרטיס JSON זהה לפורמט `customers.json`) דרך `db.load_customers_rows`/`db.replace_customers_rows`. **בפרודקשן זו מקור האמת.**
- **`DATABASE_URL` ריק (מקומי):** הכרטיסים נקראים/נכתבים ל-`DATA_DIR/customers.json`.

מפתח: `tenant_id::phone` (אין ID מספרי בשום מקום במערכת).

**`crm_data.db` (SQLite מקומי) הוא לוג טכני משני בלבד** - `messages`/`calls`/`calendar_tasks`/`scheduler_runs`/`reactivation_batches` (כולם לפי `phone`+`tenant_id`). בפרודקשן הטבלאות האלה יושבות באותו Postgres.

**🪤 מלכודת שקרתה בפועל פעמיים (2026-09-09, 2026-09-16):** ל-`crm_data.db` **המקומי** יש גם טבלת `customers` (`phone,name,location,status`, **בלי** `tenant_id`) שהיא **זרה לגמרי לארכיטקטורה** (**לא לבלבל** עם טבלת `customers` (`cust_key,data`) של Postgres בפרודקשן, שהיא כן מקור האמת שם) - נוצרה ע"י סקריפטי ייבוא חיצוניים (`sqlite3` ישיר) שכתבו לשם בטעות. `server.py`/ה-UI **אף פעם** לא קוראים ממנה - כל ייבוא שנחת שם היה בלתי-נראה לגמרי בדשבורד. **כל ייבוא לידים (יחיד או בכמות) חייב לעבור אך ורק דרך `extract.import_lead`/`resolve_existing_phone`** (ישירות, או `POST /api/leads/import`/`POST /api/leads`) - **לא** SQL ישיר ל-`crm_data.db` ולא ל-Postgres.

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
- **`history`** - append-only, `direction`: `"in"`(מהלקוח)/`"out"`(מהמערכת), `channel`: `whatsapp`/`voice`/`note`/`followup`/`followup_2`/`followup_3`/`followup_final`. שדה `simulated:true` מסמן הודעה שלא נשלחה בפועל (Trial/כשל ספק).
- **`category`/`agent`/`notes`** - מטא-דאטה חופשית, לא נוגעים ב-`history`/`lead_status`.
- **`property_type`/`budget`** - מחולצים אוטומטית מהודעה קולית (gpt-4o-mini), רלוונטי בעיקר לנדל"ן.
- **קריאה/כתיבה בלעדית דרך `extract.py`** (`load_customers`/`save_customers`/`import_lead`/`update_lead_fields`/`update_lead_status` וכו') - ראו אזהרת המלכודת למעלה.

## חזון המוצר וסטטוס יכולות

| יכולת | סטטוס |
|---|---|
| Speed-to-Lead (מענה אוטומטי מיידי ב-`/webhook`) | ✅ בנוי; אין מדידת SLA מפורשת |
| שמירת 100% אנשי קשר (כל הודעה → כרטיס במאגר הלידים - `customers.json`/Postgres) | ✅ בנוי |
| CRM אוטונומי (`index.html`: טבלה/קנבן/Inbox/יומן/משוב/חיפוש/ציון חום) | ✅ בנוי |
| מנוע Reactivation (סריקה + פנייה מותאמת) | ✅ בנוי (`reactivate.py`+`scheduler.py`) |
| תמלול קולי + חילוץ שדות אוטומטי (Whisper + gpt-4o-mini) | ✅ בנוי ונבדק E2E עם קריאות אמיתיות |
| פנייה אוטומטית ב-WhatsApp על שדות חסרים בכרטיס | ❌ לא ממומש |
| חיוג רציף (power dialer) + Click-to-Call (גישור אמיתי) | ✅ תור/UI בנוי; ⚠️ גישור Voice אמיתי לא נבדק (חסר `AGENT_PHONE_NUMBER`/`PUBLIC_BASE_URL`) |

## מפת קבצים

| קובץ | תפקיד |
|---|---|
| `extract.py` | מנוע CRM - כל קריאה/כתיבה לכרטיסים (`customers.json` מקומי / Postgres `customers` בפרודקשן) עוברת דרכו בלבד |
| `server.py` | Webhook רב-ערוצי (WhatsApp אמיתי; IG/FB placeholder) + API + דשבורד ב-`/` |
| `index.html` | דשבורד (טבלה/קנבן/Inbox/פאנלים) |
| `whatsapp_provider.py` | Provider pattern - `WHATSAPP_PROVIDER` ב-`.env` בוחר Twilio/GreenAPI/UltraMsg/Mock |
| `reactivate.py` | ניסוח+שליחת הודעות חימום, rate limiting 15-30s, batch tracking |
| `scheduler.py` / `scheduler_worker.py` | סריקה תקופתית ללידים קרים + follow-up אוטומטי ב-4 שלבים (24/48/96/168 שעות, ערוצי `followup`/`followup_2`/`followup_3`/`followup_final`; כפוף ל-`SCHEDULER_AUTO_FOLLOWUP`) (thread מקומי / process נפרד ל-production) |
| `db.py` | שכבת גישה ל-SQLite `crm_data.db` מקומית או ל-Postgres כש-`DATABASE_URL` מוגדר (`IS_POSTGRES`); לוג טכני + בפרודקשן גם טבלת הכרטיסים `customers` (`cust_key,data`) - ראו אזהרה למעלה |
| `export_realestate_from_render.py` | ייצוא read-only של נתוני הנדל"ן מ-Postgres בפרודקשן אל `data_realestate/` - **רק המשתמש מריץ** |
| `paths.py` | `DATA_DIR` משותף לקבצי state (`customers.json`/`crm_data.db`/`chat_history.txt`) |
| `voice_call.py` | Click-to-Call (גישור Twilio Voice) |
| `transcription.py` | Whisper STT + חילוץ שדות gpt-4o-mini |
| `prompts.py`/`prompts.json` | תבניות פרומפט לכל סוכני ה-AI, ניתנות לעריכה חיה מה-UI |
| `tenant_settings.py`/`tenant_settings.json` | הגדרות פר-tenant (למשל סף ימים ל-Reactivation) |

## ספק WhatsApp פעיל: Green API

- **`WHATSAPP_PROVIDER=green_api`** - הספק המוגדר כרגע ב-`.env` (נכון ל-2026-11-11). (בעבר נרשמה כאן בעיית `401` עם Green API - כבר לא רלוונטי לתצורה הנוכחית.)
- UltraMsg: הספק הקודם, נבדק end-to-end; הקוד נשאר בפרויקט. `UltraMsgProvider` מנרמל טלפון ישראלי דרך `whatsapp_send._to_e164` לפני שליחה (בלי זה, מספרים מקומיים `0XXXXXXXXX` מסומנים `Invalid` בלוח הבקרה של UltraMsg).
- Twilio: תקוע ב-Trial (כל שליחה יזומה נחסמת/`simulated` אלא אם הנמען Verified).
- **⚠️ אין "רשת ביטחון" של Trial חוסם.** הספק הפעיל שולח הודעות אמיתיות בפועל (אלא אם `DRY_RUN=true`) - `SCHEDULER_AUTO_SEND`/`SCHEDULER_AUTO_FOLLOWUP` (כרגע `false`/לא מוגדרים) וכל קריאה עם `send:true` ישלחו אמיתית ללידים ב-`contacts.csv`. **חלק מהלידים ב-`contacts.csv` הם עדיין נתוני-דמה** (לא מספרי WhatsApp אמיתיים) - להחליף לפני קמפיין production.

## נתוני לידים

- **נתוני הנדל"ן האמיתיים** (פרויקט נקסוס / "שביל התפוחים", ~12 לידים) נמצאים **רק** ב-Postgres בפרודקשן (Render). `export_realestate_from_render.py` (read-only) מייצא אותם אל `data_realestate/` (`realestate_customers.json` + `customers.json`; התיקייה ב-`.gitignore`). **רק המשתמש מריץ את הייצוא - סוכנים לא מתחברים לפרודקשן.**
- **`customers.json` בתיקיית הפרויקט** (~381 כרטיסים, tenants: `default`/`business_a`/`business_b`/`qa_autoconfirm_test`) = **שאריות וייפים**, לא נתוני נדל"ן (`import_source`: `cleaned_leads.xlsx` ×196, `vape_vcf_import_2026-09-16` ×169). לעסק הוויפים יש עכשיו פרויקט עצמאי נפרד: `C:\Users\limorgy\Desktop\CODE\vape-wholesale-crm` (venv משלו, `vape_crm.db`, פורט 5050, לא ב-git) - לא לגעת בו מכאן.
- **הרצה מקומית בטוחה (על נתוני הנדל"ן):** `DATA_DIR=data_realestate DATABASE_URL= DRY_RUN=true venv/Scripts/python.exe server.py` (פורט 5000). בלי `DATA_DIR` הדשבורד מציג את שאריות הוויפים; עם `DATABASE_URL` מה-`.env` השרת מתחבר ל-Postgres בפרודקשן.
- **פורמטים לא-עקביים היסטורית** - חלק מהכרטיסים במפתח טלפון מקומי (`0501234567`), חלק ב-E.164 (`+972501234567`) לאותו מספר אמיתי → כרטיסים כפולים ישנים שלא אוחדו אוטומטית. `resolve_existing_phone` מונע כפילות *חדשה* בלבד; ניקוי כפילויות קיימות עדיין ידני.
- `crm_data.db`'s `customers` table (הטבלה הזרה) מכילה כ-305 שורות יתומות משיירי ייבוא ישן - לא בשימוש, לא נמחקות אוטומטית.

## מגבלות ידועות

- אין Multi-Tenant אמיתי - מאגר כרטיסים יחיד גלובלי (`customers.json` / טבלת Postgres אחת), בלי בידוד עסקים.
- Omnichannel חלקי - רק WhatsApp מחובר בפועל; IG/FB placeholder בלבד.
- `customers.json` כקובץ שטוח (מצב מקומי) - לא בטוח לכתיבה מקבילית תחת עומס. גם במצב Postgres, `save_customers` מסנכרן את כל המילון (`replace_customers_rows` - כולל מחיקת שורות שחסרות במילון), כך שכתיבות מקבילות עלולות לדרוס זו את זו.
- `/api/*` מוגן במפתח (`API_SECRET_KEY` / משתמשי RBAC, `X-API-Key` או `Bearer`) - בלי מפתח תקין מוחזר 401. אבל המפתח הראשי מוזרק לדשבורד (`window.__API_KEY__`), כך שכל מי שפותח את הדף יכול לשלוף אותו. `/webhook` עצמו לא מאומת ב-Green API (נשען על סודיות הכתובת).
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
3. **`SCHEDULER_AUTO_SEND`/`SCHEDULER_AUTO_FOLLOWUP`/`SCHEDULER_AUTO_CALL_FOLLOWUP`/`SCHEDULER_AUTO_MEETING_REMINDER`/`SCHEDULER_AUTO_MEETING_INVITE`** - כבויים כברירת מחדל (dry-run). לא להדליק בלי הודעות-תבנית מאושרות מול מטא לכל ורטיקל + החלטה עסקית מודעת. **חריג מכוון**: יצירת משימת "📞 אישור הגעה" הפנימית למשווק (`scheduler.check_pending_marketer_reminders`) **אין** לה דגל AUTO_* - היא לא פנייה ללקוח, רק משימה פנימית בדשבורד (מותר בלי אישור, בדיוק כמו הצעות `scheduling_agent.py`).
4. **ייבוא לידים תמיד דרך `extract.import_lead`/`resolve_existing_phone`** - לעולם לא SQL ישיר ל-`crm_data.db` (ראו אזהרת המלכודת למעלה).
5. **שינוי טלפון לליד קיים (rekey)** חייב לעדכן גם מפתח `customers.json` וגם `phone` בשלוש טבלאות ה-SQLite (`db.rekey_phone`) - לא לבצע `UPDATE` חלקי.
6. **היסטוריית הודעות (`history`/`messages`) היא append-only** - אף עריכה (כולל עריכת שיחה) לא נוגעת בלוג ההודעות שכבר הוצג ללקוח/בצ'אט.
7. **`BLOCKED_CONTACTS`/Takeover אנושי** - מספרים ב-`BLOCKED_CONTACTS` (server.py) לעולם לא עוברים דרך pipeline הלידים (אין API לקריאת אנשי הקשר השמורים בטלפון - זו הדרך המעשית היחידה למנוע מענה בטעות לאנשי קשר פרטיים/פנימיים על אותו מספר WhatsApp). הודעה יוצאת שנשלחה ידנית מהמכשיר המקושר (`outgoingMessageReceived` ב-Green API, לא `outgoingAPIMessageReceived`) מכבה אוטומטית `ai_enabled`+מסמנת `lead_status="human_handling"` לליד קיים (`_handle_human_takeover`) - **לא** יוצרת כרטיס חדש למספר שלא היה ליד כבר.

## סוכני הפרויקט (`.claude/agents/`)

ההגדרות המלאות בקבצי `.claude/agents/<name>.md`; זו הרשימה היחידה (מחליפה את טבלת ה-roles הישנה).

| סוכן | תחום | אסור (בנוסף לחוקי הבטיחות) |
|---|---|---|
| `devops` | הרצת שרת מקומי, `.env`/`DATA_DIR`/`DATABASE_URL`/`DRY_RUN`, פריסה ל-Render, Dockerfile/Procfile/`requirements.txt`, git | לגעת בכרטיסים ישירות (רק דרך `extract.py`) |
| `qa` | בדיקת שינויים לפני commit, לוגים, שחזור באגים, `test_suite.py`, בדיקת עמידה בחוקי הבטיחות | מדווח בלבד - לא משנה לוגיקה עסקית |
| `data` | כרטיסי לידים: `extract.py` (load/save/import/update), ייבוא/ייצוא, ניקוי כפילויות, הפרדת שאריות וייפים מנדל"ן, `DATA_DIR` | SQL ישיר, מחיקת לידים בלי אישור, חיבור לפרודקשן, נגיעה ב-`vape-wholesale-crm` |
| `messaging` | הודעות יוצאות ואוטומציה: `reactivate.py`, `scheduler.py`/`scheduler_worker.py` (פולו-אפ 4 שלבים, תזכורות פגישה), `outbound_engine.py`, ספקי WhatsApp, `voice_call.py` | `--send`/`send:true`/הדלקת `SCHEDULER_AUTO_*` בלי תבנית מאושרת ואישור; למחוק/לשנות פגישות קיימות בלי אישור (מותר רק הצעה `pending_confirmation`) |
| `sales-ai` | סוכני AI: `sales_agent.py` (מכירות/התנגדויות/Hot Lead Alert), `scheduling_agent.py`, `prompts.py`/`prompts.json`, `transcription.py`, `note_entity_extraction.py`, `command_copilot.py` | לסגור עסקה/להתחייב על מחיר. Hot Lead Alert (`detect_hot_lead_signal`) מותר לסמן `lead_status="hot"`+הערה פנימית - אסור לשלוח הודעה ללקוח בעצמו |

BI & Analytics (`analytics.py`, read-only) - roadmap בלבד, אין סוכן/קוד.
