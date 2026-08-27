# WhatsApp CRM MVP — הנחיות פרויקט

מוצר: AI CRM שמתחבר לוואטסאפ (ובעתיד לערוצים נוספים), מחלץ אוטומטית פרטי לקוח מהודעות, ומנהל את מחזור החיים של הליד.

## סטטוס נוכחי — חשוב לקרוא לפני הכל

המסמך הזה מגדיר חלוקת אחריות בין 6 "סוכנים" במודל Multi-Tenant & Omnichannel.
**זו הגדרת תפקידים/roadmap, לא קוד שמפעיל אוטומטית 6 סוכנים עצמאיים.** כתיבת המסמך הזה
לא יוצרת בעצמה תהליכים רצים. מה שקיים בפועל כקוד רץ כרגע:

| קובץ | תפקיד | סטטוס |
|---|---|---|
| `extract.py` | מנוע חילוץ פרטים מטקסט + שכבת CRM (`customers.json`) | ✅ עובד |
| `server.py` | שרת Webhook רב-ערוצי (whatsapp אמיתי + מענה אוטומטי, instagram/facebook placeholder) + דשבורד (`/`) + API (`/api/leads` - עם `?include_last_message=1` אופציונלי לתצוגת Inbox; `/api/messages` קריאה-בלבד; `/api/leads/status` מעדכן `lead_status`; `/api/messages/send` ו-`/api/reactivate` **שולחים בפועל**) | ✅ עובד (whatsapp), ⚠️ placeholder (שאר הערוצים) |
| `index.html` | דשבורד לידים - שתי תצוגות מתחלפות: **טבלה** (סטטוס ניתן לעריכה + סטטיסטיקות + פאנל היסטוריה נגלל + פאנל החייאת לידים קרים) ו-**Unified Inbox** (רשימת שיחות ממוינת לפי פעילות אחרונה + חלון צ'אט קבוע לצידה) - שתיהן חולקות אותה לוגיקת צ'אט חי (`createChatController`, ראו למטה) - נטען ב-`/` | ✅ עובד |
| `whatsapp_send.py` | עטיפת שליחה משותפת ל-Twilio (`send_whatsapp_message`) - בשימוש `reactivate.py` וגם `server.py` | ✅ עובד; דורש פרטי Twilio אמיתיים ב-`.env` |
| `reactivate.py` | ניסוח הודעות חימום ללידים קרים + סיווג תגובות + שליחה בפועל דרך Twilio - גם כ-CLI (`--send`) וגם כפונקציה שקוראים לה מ-`/api/reactivate` ומ-`scheduler.py` | ✅ עובד |
| `scheduler.py` | **Background Scheduler Worker** - סורק תקופתית לידים קרים לפי סף זמן פר-ורטיקל (`ecommerce`/`real_estate`, מ-`contacts.csv`) ומריץ עליהם `reactivate.py`. שני מצבי הרצה: thread בתוך `server.py` (מקומי, `python server.py`), או `scheduler_worker.py` העצמאי (APScheduler, ל-production - ראו למטה) | ✅ עובד; **dry-run בלבד כברירת מחדל** - שליחה אוטומטית אמיתית דורשת `SCHEDULER_AUTO_SEND=true` מפורש ב-`.env` (כרגע לא מוגדר) |
| `Procfile` + `scheduler_worker.py` (2026-08-26) | פריסה לענן: `web` (Gunicorn, כמה workers) + `clock` (מנוע התזמון כתהליך עצמאי יחיד, APScheduler) | ✅ עובד; נבדק מקומית (לא ניתן להריץ Gunicorn עצמו על Windows - ראו הערה למטה) |
| `paths.py` (2026-08-26) | `DATA_DIR` משותף - `customers.json`/`crm_data.db`/`chat_history.txt` יכולים לשבת על דיסק מתמיד בענן (`DATA_DIR` env var); ברירת מחדל = תיקיית הפרויקט, ללא שינוי מקומי | ✅ עובד ונבדק (ברירת מחדל + override) |
| `POST /api/leads/import` (ב-`server.py`) | ייבוא לידים מ-CSV/XLSX - ממפה אוטומטית שם/טלפון/עסק/מקור/סטטוס (עברית או אנגלית), upsert לפי טלפון מנורמל (E.164) + tenant דרך `import_lead` ב-`extract.py`. כפתור "📥 ייבוא לידים" ב-`index.html` | ✅ עובד |
| הקשחת `server.py` (2026-08-25) | משתני סביבה (PORT/HOST/FLASK_DEBUG/LOG_LEVEL/VERIFY_TWILIO_SIGNATURE), לוגים מסודרים (קונסולה + `server_error.log` מתגלגל), אימות חתימת Twilio (`X-Twilio-Signature`) ל-`/webhook`, ProxyFix מאחורי ngrok | ✅ עובד ונבדק דרך ngrok אמיתי - ראו הפירוט תחת "הקשחת production" למטה |
| `voice_call.py` (חדש) + `/api/calls/*`,`/voice/*` ב-`server.py` + טבלת `calls` ב-`db.py` + כפתור "📞 שיחה" ב-`index.html` (2026-08-27) | **Click-to-Call**: גישור שיחה (Twilio מתקשר לנציג, ואז מגשר ללקוח) + הקלטה + הערות ידניות של הנציג → Claude מייצר תקציר שמשוקף גם ל-`customers.json`/`messages` (channel="voice") | ✅ **צינור ה-DB/AI/UI נבדק מקצה-לקצה** (במצב simulated - ראו למטה); ⚠️ שיחת Voice אמיתית (חיוג בפועל, TwiML bridge, הקלטה אמיתית) **לא נבדקה** - אין עדיין מספר Twilio Voice/`AGENT_PHONE_NUMBER`/`PUBLIC_BASE_URL` מוגדרים |

תפקידי QA / Sales / Scheduling / BI שמתוארים למטה **עדיין אין להם קובץ מימוש**. כדי שסוכן
יהפוך מ"תיאור תפקיד" ל"תהליך שרץ בפועל" צריך: (1) לבנות עבורו סקריפט ייעודי, ו-(2) לתזמן
אותו (Windows Task Scheduler / cron / הרצה ידנית) או להריץ אותו כ-subagent דרך Claude Code.

### מגבלות ידועות (חשוב לדעת)

- **אין עדיין Multi-Tenant אמיתי.** `customers.json` הוא קובץ יחיד וגלובלי — אין הפרדה בין
  עסקים שונים שישתמשו במערכת. אם בעתיד כמה עסקים ישתמשו באותה מערכת, צריך להוסיף
  `tenant_id`/`business_id` לכל רשומה ולבודד גישה בין עסקים. זה לא קיים כרגע.
- **Omnichannel חלקי בלבד.** WhatsApp מחובר בפועל דרך Twilio. Instagram ו-Facebook הם
  placeholder בפורמט JSON גנרי — לא מחוברים בפועל ל-Meta Graph API האמיתי.
- **⚠️ פורמטים לא-עקביים של טלפון בנתונים קיימים (התגלה 2026-08-25 בבדיקת ייבוא CSV).**
  חלק מהכרטיסים ב-`customers.json` נשמרו במפתח לפי טלפון מקומי (`0501234567`), חלק לפי
  E.164 (`+972501234567`) - **לאותו מספר אמיתי** יכולים להיות שני כרטיסים נפרדים ושונים
  (למשל דני/פרחי הכרמל תחת `default::0501234567` מול רוני/רוני קייטרינג תחת
  `default::+972501234567` - אותו מספר בדיוק, שני זהויות). `import_lead` (ומכאן גם ייבוא
  CSV/Excel) מנרמל ל-E.164 לפני upsert - כך שאם תייבאו קובץ עם ליד קיים בפורמט מקומי,
  הוא עלול "לפגוש" ולעדכן כרטיס אחר שכבר קיים ב-E.164 עבור אותו מספר, ולא את הכרטיס
  שציפיתם לו. **צריך ניקוי/איחוד נתונים ידני** של הכרטיסים הכפולים לפני שסומכים על
  זיהוי-כפילויות אוטומטי בייבוא בסביבת production; זו לא נבדקה/תוקנה אוטומטית כאן.
- **⚠️ אין עדיין מספר Twilio Voice-capable מוגדר, ואין `PUBLIC_BASE_URL` חי (2026-08-27).**
  Click-to-Call (`voice_call.py`) בנוי ונבדק במלואו במצב `simulated` (בדיוק כמו חסימת
  Trial ב-WhatsApp), אבל שיחת גישור אמיתית - חיוג בפועל לנציג, TwiML ב-`/voice/connect`,
  הקלטה אמיתית, ואימות חתימת Twilio על שלושת ה-webhooks החדשים - **לא נבדקו** כאן. דורש
  מהמשתמש לרכוש מספר Voice בקונסולת Twilio, להגדיר `AGENT_PHONE_NUMBER`/
  `TWILIO_VOICE_FROM`/`PUBLIC_BASE_URL` ב-`.env`, ולחשוף URL ציבורי (ngrok/Render) - אותה
  מגבלה עקרונית כמו Gunicorn-על-Windows ואימות חתימת `/webhook` שנבדק רק דרך ngrok אמיתי.

### הקשחת production ב-server.py (2026-08-25) - מה כן ומה לא

**מה נוסף בפועל, ונבדק:**
- **משתני סביבה מלאים:** `PORT`, `HOST`, `FLASK_DEBUG`, `LOG_LEVEL`, `VERIFY_TWILIO_SIGNATURE`
  נקראים מ-`.env` עם ברירות מחדל בטוחות. `FLASK_DEBUG` בקוד הוא `false` כברירת מחדל
  (production-safe - מצב debug של Flask חושף Werkzeug debugger שמריץ קוד שרירותי) -
  ב-`.env` המקומי הוגדר `FLASK_DEBUG=true` במפורש כדי לשמר את חוויית הפיתוח.
  ייבוא נכשל (`extract.py`/`whatsapp_send.py` דורשים `ANTHROPIC_API_KEY`) נתפס עם הודעה
  ברורה ו-`sys.exit(1)` במקום traceback גולמי.
- **לוגים:** `logging` סטנדרטי - קונסולה + `server_error.log` מתגלגל (`RotatingFileHandler`,
  2MB × 5 קבצי גיבוי) שרושם WARNING/ERROR עם traceback מלא. נפרד מ-`chat_history.txt`
  (שנשאר לוג *עסקי* של תוכן הודעות, לא לוג שגיאות טכני). `@app.errorhandler(Exception)`
  תופס כל חריגה לא-מטופלת, רושם אותה, ומחזיר JSON נקי (`{"error": "..."}`) במקום דף
  שגיאה של Flask/traceback לקליינט.
- **אימות Twilio אמיתי:** `X-Twilio-Signature` מאומת מול `TwilioRequestValidator` (חלק
  מ-SDK הרשמי) לכל בקשה ל-`/webhook` בפורמט Twilio (form-encoded). **`ProxyFix` נדרש
  ונוסף** כי בלעדיו `request.url` משקף את הכתובת הפנימית (`127.0.0.1:5000`) ולא את
  הכתובת הציבורית שטוויליו חתם עליה - האימות תמיד היה נכשל בטעות. **נבדק דרך ngrok
  אמיתי**: בקשה עם חתימה מחושבת נכון על ה-URL הציבורי - התקבלה (200); בקשה בלי חתימה,
  גם דרך אותו tunnel - נדחתה (403). בדיקות ידניות (curl/פיתוח מקומי) בלי חתימת Twilio
  אמיתית יידחו אוטומטית כברירת מחדל - מגדירים `VERIFY_TWILIO_SIGNATURE=false` ב-`.env`
  זמנית אם צריך לבדוק ידנית.

**מה *לא* כוסה - "100% מוכן ל-production" הוא לא מדויק, חשוב לדעת:**
- **`/api/*` (כל נתיבי הדשבורד) עדיין ללא אימות/הרשאה בכלל** - כל מי שמגיע לכתובת יכול
  לקרוא נתוני לקוחות, לשנות סטטוסים, לשלוח הודעות ידניות, ולהריץ קמפיינים. זה לא נדרש
  במפורש בבקשה הזו (שהתמקדה ב-webhook של Twilio), אבל זו פרצה משמעותית לפריסה אמיתית.
- **`customers.json` כקובץ שטוח** לא בטוח לכתיבה מקבילית בעומס production אמיתי (race
  conditions אפשריים בין בקשות webhook/API/scheduler מקבילות) - לא נפתר כאן.
- **סודות (`TWILIO_AUTH_TOKEN` וכו') עדיין בטקסט גלוי ב-`.env`** - ל-production אמיתי
  צריך secret manager (לא מומש כאן).
- **אין HTTPS termination באפליקציה עצמה** - מניחים שהיא רצה מאחורי proxy/ngrok/load
  balancer שמטפל בזה, כמו היום.
- **אין rate limiting** על אף נתיב.

### חלון צ'אט חי ב-index.html (2026-08-26)

**מנגנון:** `pollForNewMessages()` שולפת `GET /api/messages?...&since=<timestamp ההודעה
האחרונה שכבר מוצגת>` כל 3 שניות (`LIVE_POLL_INTERVAL_MS`) כל עוד פאנל ההיסטוריה פתוח
(`startLivePolling`/`stopLivePolling` ב-`openHistory`/`closeHistory`/`closeAllPanels`).
`db.get_messages` ו-`/api/messages` תומכים עכשיו בפרמטר `since` (מחזיר רק הודעות
מאוחרות ממנו) - כדי לא לשלוף את כל ההיסטוריה מחדש בכל בדיקה. שליחה דרך הקומפוזר לא
מוסיפה בועה "אופטימית" מקומית - קוראת ל-`pollForNewMessages()` מיד אחרי שליחה מוצלחת,
ומציגה את ההודעה דרך אותו נתיב בדיוק כמו הודעות נכנסות חיות (מונע כפילות).

**למה polling ולא WebSocket/SSE:** בפריסת production (ראו Procfile למטה) Gunicorn רץ
עם כמה workers - חיבור WebSocket/SSE נעול ל-worker אחד, ואם ההודעה החדשה מגיעה
מ-webhook שנחת על worker *אחר*, לא הייתה דרך פשוטה (בלי Redis pub/sub או דומה - לא
קיים בפרויקט) להודיע לחיבור הפתוח. Polling קורא מ-`crm_data.db` המשותף בכל בקשה בנפרד
- כל worker רואה את אותם נתונים תמיד, בלי בעיית קואורדינציה בין workers (בדיוק אותה
סיבה שבגללה `scheduler_worker.py` הוא process נפרד - ראו למטה).

**נבדק אמיתי, לא רק בקוד:** נשלחה בקשת webhook חתומה אמיתית (Twilio signature תקין)
בזמן שחלון הצ'אט היה פתוח בדפדפן (Playwright) - ההודעה הנכנסת *וגם* התגובה האוטומטית
הופיעו בפאנל **בלי שום אינטראקציה עם הדף** תוך פחות ממחזור polling אחד. אומת גם
שה-polling נעצר לגמרי ברגע שסוגרים את הפאנל (0 בקשות נוספות). **תוך כדי בדיקה נמצא
ותוקן race condition אמיתי**: הקריאה המפורשת שמציגה הודעה שהמשתמש עצמו שלח יכלה
"להיבלע" בשקט אם התנגשה עם טיק polling ברקע (guard `isPolling` פשוט) - במקום להיעלם,
עכשיו היא מסמנת `pollAgainRequested` ומריצה סבב נוסף מיד; ה-fix אומת (בדיקה חוזרת:
בועה אחת בדיוק, בלי כפילות, בלי השהיה נסתרת).

### Unified Inbox (2026-08-26)

**מה נוסף:** תצוגה שנייה ב-`index.html`, לצד הטבלה הקיימת (כפתורי מעבר "📋 טבלה" /
"💬 Inbox" - `switchView`) - רשימת שיחות (`#inboxList`, ממוינת לפי `last_message_at`
יורד - השיחה עם הפעילות האחרונה למעלה, בדיוק כמו כל אפליקציית צ'אט) לצד חלון צ'אט
קבוע (לא slide-over כמו הפאנל הקיים). לכל שורה ברשימה: שם, תצוגה מקדימה של ההודעה
האחרונה (עם קידומת "את/ה: " אם היא יצאה מהמערכת), זמן, ותג סטטוס.

**Backend:** `db.get_last_message(phone, tenant_id)` חדש ב-`db.py` - שולף רק את
ההודעה האחרונה (לא כל ההיסטוריה) לכל ליד. `GET /api/leads?include_last_message=1`
(אופציונלי - ברירת המחדל בלי הפרמטר לא השתנתה, לא פוגע בטבלה הקיימת) מצרף
`last_message`/`last_message_at`/`last_message_direction` לכל ליד ברשימה.

**Frontend - refactor, לא שכפול:** הלוגיקה של "חלון צ'אט חי" (polling, composer,
race-fix `pollAgainRequested`, עדכון סטטוס) חולצה מהקוד הישן (שהיה קשור ישירות ל-
ID-ים של הפאנל הנגלל) לפונקציית מפעל `createChatController(ids)` ב-`index.html`
שמחזירה controller עם `open`/`close`/`syncStatus`. שני מופעים עצמאיים: `panelChat`
(מפעיל את `#panel` הישן - `openHistory`/`closeAllPanels` עכשיו רק wrappers דקים
סביבו) ו-`inboxChat` (מפעיל את חלון הצ'אט ב-Inbox). **אותו קוד מדויק, שתי מופעים** -
אין סיכון לסחף בין הגרסאות כמו שהיה קורה עם copy-paste. `switchView` דואג שרק
controller אחד "חי" בכל רגע - עובר ל-Inbox סוגר את הפאנל הישן (`closeAllPanels`),
ועובר לטבלה עוצר את ה-polling של ה-Inbox (`inboxChat.close()`) - כדי שלא ירוצו שני
מחזורי polling מקבילים על אותו ליד.

**נבדק אמיתי (Playwright), לא רק בקוד:**
1. טעינת שתי התצוגות + מעבר ביניהן - 0 שגיאות קונסולה.
2. פתיחת שיחה ב-Inbox, מעבר לשיחה שנייה - היסטוריה נטענת נכון בכל פעם, כולל מקרה
   "אין הודעות רשומות" לליד בלי היסטוריה.
3. **רגרסיה**: הפאנל הנגלל הישן (`#panel`) עדיין עובד זהה לחלוטין אחרי ה-refactor.
4. שליחת הודעה דרך הקומפוזר של ה-Inbox - עובד קצה-לקצה (כולל הטיפול הקיים ב-
   מגבלת Twilio Trial: הודעה נרשמת כ-`simulated`, מוצגת עם אזהרה מתאימה).
5. **זמן-אמת אמיתי**: webhook חתום (Twilio signature תקין) נשלח בזמן ששיחה פתוחה
   ב-Inbox - ההודעה הנכנסת + התגובה האוטומטית הופיעו בלי שום אינטראקציה עם הדף,
   תוך פחות ממחזור polling אחד (זהה בדיוק להתנהגות שנבדקה על הפאנל הישן).
כל נתוני הבדיקה (הודעות טסט ב-`crm_data.db`, `customers.json`, `chat_history.txt`)
נוקו בסוף.

### Click-to-Call + תקציר שיחה אוטומטי (2026-08-27)

**מנגנון:** כפתור "📞 שיחה" (בפאנל ההיסטוריה הנגלל וב-Inbox, ליד ה-header של הצ'אט)
פותח פאנל ייעודי (`#callPanel`) וקורא ל-`POST /api/calls/start`. זה יוזם **גישור** דרך
Twilio (`voice_call.start_bridge_call` - חדש): Twilio מתקשר קודם לנציג
(`AGENT_PHONE_NUMBER`), וברגע שהוא עונה, TwiML ב-`/voice/connect` מגשר (`<Dial
record="record-from-answer">`) למספר הלקוח - **לא** Softphone מבוסס-WebRTC בדפדפן
(זה היה דורש יצירת TwiML Application ידנית בקונסולת Twilio, לא ניתן לסקריפט + גישה
למיקרופון). ההקלטה נשמרת (URL בלבד, דרך `/voice/recording-status`) - **אין תמלול/STT
אוטומטי** בגרסה הזו (הוחלט במפורש מול המשתמש: "הקלטה + הערות ידניות" עדיף על תמלול
Twilio המובנה - איכות מוטלת בספק בעברית - ועל הוספת ספק STT חדש כמו OpenAI Whisper -
עלות/תלות חדשה). אחרי השיחה, הנציג מקליד הערות חופשיות (`POST
/api/calls/<id>/notes`), ו-Claude מייצר מהן תקציר עברי קצר
(`extract.generate_call_summary` - אותו pattern בדיוק כמו `generate_reply`/
`generate_outreach_message`). התקציר נשמר בטבלת `calls` החדשה ב-`db.py` (audit trail:
`call_sid`/`status`/`duration_seconds`/`recording_url`/`notes`/`summary`) **וגם**
משוקף להיסטוריה הרגילה (`extract.log_call_summary`, בכוונה לא נוגע ב-`lead_status` -
כמו `log_manual_reply`) **וגם** לטבלת `messages` עם `channel="voice"` - כך שהוא מופיע
אוטומטית כבועה בצ'אט הקיים (פאנל/Inbox) דרך אותו polling שכבר קיים ונבדק, בלי קוד
רינדור נוסף (`renderBubble` קיבל רק הסתעפות קטנה ל-`channel==="voice"`).

**Fallback מדומה - בדיוק כמו Trial ב-WhatsApp:** בסביבת הפיתוח הזו **אין** עדיין מספר
Twilio Voice/`AGENT_PHONE_NUMBER`/`PUBLIC_BASE_URL` מוגדרים ב-`.env`. `/api/calls/start`
תופס את זה (`RuntimeError` מ-`voice_call.py`, נבדק כ-`status="simulated_no_config"`)
ומתנהג בדיוק כמו `_is_trial_restriction` ב-`/api/messages/send`: רושם שיחה כ-`simulated`
ומחזיר 200 (לא 502) - כדי לאפשר לבדוק את כל שאר הצינור (הערות → Claude → תקציר → כרטיס)
בלי מספר Voice אמיתי.

**נבדק בפועל (Playwright + קריאות API ישירות), לא רק בקוד:**
- `POST /api/calls/start` → `simulated:true, status:"simulated_no_config"` כצפוי.
- `POST /api/calls/<id>/notes` עם הערות עבריות אמיתיות → תקציר Claude איכותי ותמציתי
  (לא stub - `ANTHROPIC_API_KEY` כבר מוגדר), נשמר גם ב-`calls` וגם ב-`customers.json`
  history (`channel:"voice"`) וגם ב-`messages`; אומת ש-`lead_status` **לא** השתנה.
- Playwright: כפתור "📞 שיחה" מהפאנל הנגלל וגם מה-Inbox - שניהם פותחים את `#callPanel`
  נכון (כולל stacking נכון מעל הפאנל/ה-Inbox, ו-overlay שנשאר פתוח אם `#panel` עדיין
  פתוח מתחתיו). התקציר שנוצר הופיע כבועה חדשה עם תווית "📞 תקציר שיחה" בצ'אט תוך מחזור
  polling אחד אחרי סגירת פאנל השיחה - **בלי רענון ידני**, בדיוק כמו שיחת webhook נכנסת.
  0 שגיאות קונסולה, ורגרסיה מלאה: שליחת הודעה רגילה דרך הקומפוזר הקיים ב-Inbox עדיין
  עובדת אחרי כל השינויים.
- **לא נבדק (ולא ניתן לבדיקה כאן):** חיוג אמיתי לטלפון נציג, `/voice/connect` שנקרא
  בפועל ע"י Twilio ומגשר ללקוח, הקלטה אמיתית + `/voice/recording-status` עם
  `RecordingUrl` אמיתי, מעברי `CallStatus` אמיתיים ב-`/voice/status`, ואימות חתימת
  Twilio אמיתי (`_verify_twilio_request`) על שלושת ה-webhooks החדשים - דורש מספר Voice
  אמיתי + `AGENT_PHONE_NUMBER` + `PUBLIC_BASE_URL` ציבורי (ngrok/Render) שהמשתמש טרם
  סיפק. כל נתוני הבדיקה נוקו בסוף (`calls`, `messages`, `customers.json` history).

### פריסה לענן (2026-08-26) - Procfile, Gunicorn, נתיבים, ומגבלה קריטית

**מה נוסף ונבדק:**
- **`Procfile`** - שני process types (מוסכמת Heroku/Render/Railway):
  `web: gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60` ו-
  `clock: python scheduler_worker.py`. **הפרדה מכוונת, לא שרירותית:** `scheduler.start()`
  (thread בתוך `server.py`) עובד רק בהרצה ישירה (`python server.py`, `__name__ ==
  "__main__"`) - Gunicorn לא מפעיל את הבלוק הזה בכלל. גם אם היה מופעל, כל אחד מה-
  workers של Gunicorn היה מריץ עותק נפרד של הלולאה - ואם `SCHEDULER_AUTO_SEND=true`
  אי פעם, זו הייתה שליחה כפולה/משולשת של אותה הודעה. `scheduler_worker.py` (חדש, עם
  APScheduler - נוסף ל-`requirements.txt`) הוא process יחיד ונפרד ל-production, בלי
  הבעיה הזו. **נבדק ישירות**: `python scheduler_worker.py` בפועל - עלה, הריץ סריקה
  מיידית (זהה להתנהגות ה-thread הקודמת), רשם ל-`chat_history.txt`, נעצר נקי.
  ⚠️ **לא הצלחתי להריץ את Gunicorn עצמו** במכונת הפיתוח הזו - Gunicorn תלוי ב-`fcntl`
  (מודול POSIX-בלבד) ו**לא רץ על Windows בכלל** מבחינה עקרונית, לא רק כאן. זה לא
  משפיע על הפריסה בפועל (יעד הפריסה הוא קונטיינר Linux), אבל המשמעות היא של-Gunicorn
  עצמו (בניגוד לכל שאר הקוד בפרויקט) **לא בוצעה הרצה אמיתית מקצה-לקצה** בסביבה הזו -
  רק ייבוא `server:app` כאובייקט WSGI תקין אומת (ראו למטה).
- **`requirements.txt` מלא:** `anthropic`, `python-dotenv`, `flask`, `twilio`, `openpyxl`,
  `gunicorn`, `apscheduler` - כל התלויות הישירות שבפועל בשימוש בקוד.
- **נתיבים יחסיים - נבדק בפועל, לא רק בקוד:** כל קובץ (`customers.json`, `crm_data.db`,
  `contacts.csv`, `chat_history.txt`, `server_error.log`, `.env`) נטען דרך
  `Path(__file__).parent / "..."` - לא תלוי בתיקיית העבודה (cwd) שממנה מריצים את
  התהליך. **תוך כדי בדיקה נתקלתי בבאג אמיתי וצר**: `load_dotenv()` בלי נתיב מפורש
  משתמש בזיהוי stack-frame שנכשל דווקא כש-`python -c "..."` (הרצת קוד inline) מייבא
  את `server`; כשמייבאים כקובץ אמיתי (כך ש-Gunicorn/`import` רגיל עובדים) זה כן הצליח
  - אבל כדי לא להסתמך על ההתנהגות העדינה הזו בכלל, שיניתי את **כל** קריאות ה-
  `load_dotenv()` (ב-`server.py`, `extract.py`, `reactivate.py`, `whatsapp_send.py`,
  `scheduler.py`) לנתיב מפורש (`dotenv_path=Path(__file__).parent / ".env"`) - וגם
  את `create_db.py` (שהשתמש בנתיב יחסי גולמי `'crm_data.db'`) לאותה גישה. נבדק: ייבוא
  `server:app` מ-cwd שונה לגמרי, וגם קריאה אמיתית מ-`crm_data.db` דרך אותו ייבוא -
  שניהם הצליחו ומצאו את הנתונים האמיתיים, לא קבצים ריקים חדשים.

**✅ עודכן 2026-08-26 - מגבלת ה-ephemeral filesystem טופלה חלקית:** `customers.json`,
`crm_data.db` ו-`chat_history.txt` (State שמשתנה בזמן ריצה) עוברים עכשיו דרך `paths.py`
חדש - `DATA_DIR` (env var, ברירת מחדל: תיקיית הפרויקט - **אין שינוי בהתנהגות מקומית**).
ב-Render, מגדירים Disk עם Mount Path (למשל `/data`) ו-`DATA_DIR=/data` באותו ערך -
כך הנתונים שורדים בין דיפלויים. `contacts.csv` (seed/קלט, לא state) ו-`server_error.log`
(לוג טכני, לא קריטי לשימור) **נשארים** בתיקיית הפרויקט במכוון - ראו הערת הראש של
`paths.py`. **נבדק בפועל**, לא רק בקוד: (1) ברירת מחדל בלי `DATA_DIR` - שלושת הנתיבים
זהים ל-100% למה שהיו לפני השינוי (בדיקת equality ישירה); (2) עם `DATA_DIR` מוצבע
לתיקייה נפרדת - קובץ שנוצר דרך `extract.upsert_customer` נחת שם ולא ב-`customers.json`
האמיתי של הפרויקט (וידאתי את שניהם); (3) הרצה מלאה מחדש של השרת + הבדיקה הפונקציונלית
המלאה (webhook חתום + 7 בדיקות נוספות) - עברה 100%, אפס שגיאות בלוג.
**עדיין לא מומש:** מעבר ל-DB מנוהל אמיתי (Postgres וכו') - `DATA_DIR` פותר את בעיית
ה-ephemeral filesystem, אבל `crm_data.db` נשאר SQLite קובץ-יחיד (לא תומך בכתיבה
מקבילית אמיתית בעומס, גם עם דיסק מתמיד) - זה עדיין מחוץ לסקופ שהתבקש.

**🐛 באג אמיתי שנמצא ותוקן 2026-08-26 - Render deploy נכשל עם "Exit status 1":**
ב-`server.py`, `import db` / `import reactivate` / `import scheduler` רצו **לפני**
בלוק ה-`try/except KeyError` שנועד לתפוס משתנה סביבה חסר (`ANTHROPIC_API_KEY`) בצורה
ברורה - אבל שלושתם תלויים ב-`extract.py` בעצמם, אז ה-`KeyError` קרה כבר ב-`import
reactivate` עצמו, **לפני** שהגיעו בכלל לבלוק שאמור לתפוס אותו. בפועל זה הופיע כ-
traceback גולמי לא ברור בלוג, ולא כהודעת השגיאה הידידותית שהתכוונתי אליה. **תוקן**
ע"י העברת שלושת ה-imports האלה *לתוך* אותו try/except. **נבדק בפועל**: הסתרתי זמנית
את `.env` (מדמה container טרי ב-Render בלי משתני סביבה מוגדרים עדיין) - לפני התיקון
קיבלתי traceback גולמי; אחרי התיקון: `שגיאת הגדרה: משתנה סביבה חסר - 'ANTHROPIC_API_KEY'.
בדקו את משתני הסביבה (.env מקומית / Render Environment)` עם `exit code 1` נקי. שחזרתי
את `.env` מיד אחרי כל בדיקה. **זה ה-hypothesis המוביל להסבר "Deploy failed error 1"
שדווח** - חסרים משתני סביבה בדשבורד של Render (ראו CLAUDE.md/שיחה קודמת לרשימת
המשתנים הנדרשים) - אבל לא אושר מול הלוג האמיתי של Render עצמו, רק שוחזר מקומית.

## עקרונות משותפים לכל הסוכנים

- **מקור אמת יחיד:** `customers.json`. כל הסוכנים קוראים/כותבים דרך הפונקציות ב-`extract.py`
  (`load_customers`, `upsert_customer`, `update_lead_status`, `import_lead`) — לא נוגעים
  בקובץ ישירות. אין להוסיף שדות/לוגיקה שכופלים או עוקפים את הפונקציות הקיימות.
  **לוג טכני נוסף (לא מקור אמת):** כל הודעה נכנסת/יוצאת נרשמת גם בטבלת `messages` ב-
  `crm_data.db` (SQLite) דרך `db.py` (`log_message`), וגם כשורת טקסט ב-`chat_history.txt`
  (דרך `_log_incoming_message` ב-`server.py`). אלו כפילויות מכוונות למעקב/דיבוג בלבד —
  אף לוגיקת עסקים לא קוראת מהן; מקור האמת היחיד להחלטות עדיין `customers.json`.
- **היסטוריה:** כל אינטראקציה עם לקוח (הודעה נכנסת, הודעת חימום שנשלחה) נשמרת במערך
  `history` בכרטיס הלקוח, עם `timestamp`, `channel`, `message`.
- **ערוץ (`source_channel`):** כל כרטיס לקוח נושא את הערוץ שממנו הגיע לראשונה. ערכים תקפים
  כרגע: `whatsapp` (אמיתי), `instagram`, `facebook` (placeholder בלבד — ראו הערה ב-`server.py`).
- **בטיחות:** אף סוכן לא סוגר עסקה, לא מוחק נתונים ולא יוזם פנייה ראשונה ללקוח בלי אישור
  אנושי מפורש. שליחת הודעה יזומה ראשונה בוואטסאפ (כמו חימום לידים קרים ב-`reactivate.py`)
  דורשת הודעת-תבנית מאושרת מול מטא — זה תהליך עסקי נפרד מהקוד, ועדיין לא ממומש.
  **חריגים (שליחה אוטומטית מותרת):**
  1. `server.py` שולח אוטומטית תגובת-מענה חזרה ללקוח בוואטסאפ (דרך TwiML) כשמתקבלת הודעה
     נכנסת — זו תגובה בתוך חלון השיחה שהלקוח פתח, לא פנייה יזומה, ולכן מותרת ללא תבנית.
  2. `POST /api/messages/send` (משמש את כפתור "שלח" בפאנל ההיסטוריה ב-`index.html`) שולח
     הודעה חופשית אמיתית דרך Twilio. האישור האנושי כאן הוא הקליק המפורש של הנציג על
     "שלח" לכל הודעה בנפרד (בניגוד לקמפיין ב-`reactivate.py` שהוא batch) — אבל **גם כאן
     חלה מדיניות חלון-24-שעות של מטא**: אם הליד לא כתב הודעה ב-24 השעות האחרונות, זו
     "business-initiated conversation" שעלולה לדרוש תבנית מאושרת; הקוד לא בודק את זה
     אוטומטית, זו אחריות הנציג השולח.
  ההיסטוריה בכרטיס הלקוח מסמנת כל הודעה עם `direction: "in"` (מהלקוח) או `"out"` (מהמערכת).
  שליחה ידנית דרך `log_manual_reply` (בניגוד ל-`update_lead_status`) **לא** משנה את
  `lead_status` — תגובה לליד "חם" לא אמורה להחזיר אותו ל"נוצר קשר".
  **שדה `simulated`:** אם Twilio חוסם שליחה מ-`/api/messages/send` בגלל מגבלת חשבון Trial
  (סטטוס 400/422 עם "trial" בהודעה - למשל נמען לא מאומת) - זו לא נחשבת שגיאת קוד. השרת
  תופס את זה (`_is_trial_restriction` ב-`server.py`), **רושם את ההודעה בכל זאת** ב-
  `customers.json` וב-`messages` (SQLite) עם `simulated: true`/`simulated=1`, ומחזיר `200`
  לדשבורד (לא `502`) - כדי שאפשר יהיה לבדוק את חלון השיחות בלי שכל בדיקה עם מספר לא-מאומת
  תיכשל. **הלקוח לא קיבל את ההודעה בפועל** במקרה הזה; ה-UI מציג את זה בבירור (בועה עם
  מסגרת מקווקוות כתומה + "⚠️ סימולציה - לא נשלח בפועל"). זה חל **רק** על `/api/messages/send`
  (השליחה הידנית מהדשבורד) - לא על `reactivate.py`/`--send`, ששם כשל שליחה עדיין מדווח
  ככשל אמיתי (`entry["error"]`), כי קמפיין batch לא אמור "להעלים" כשלים בלי שהמפעיל ידע.
- **⚠️ פרטי Twilio אמיתיים מוגדרים ב-`.env`** (`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/
  `TWILIO_WHATSAPP_FROM`) — מאז 2026-08-25 אלה כבר לא ריקים. כל שימוש ב-`send_whatsapp_message`
  (מ-`reactivate.py --send` או מ-`/api/messages/send`) שולח **הודעת WhatsApp אמיתית** דרך
  Twilio. בזמן פיתוח/בדיקה - אל תשלחו למספרים אמיתיים בלי אישור מפורש; מספרים שלא הצטרפו
  ל-Sandbox (לא שלחו `join <קוד>`) יידחו אוטומטית ע"י Twilio כ"unverified" - זו רשת ביטחון
  חלקית לבדיקות, לא תחליף לזהירות.
- **`AGENT_PHONE_NUMBER`/`TWILIO_VOICE_FROM`/`PUBLIC_BASE_URL` (2026-08-27, ל-Click-to-Call
  ב-`voice_call.py`) - עדיין לא מוגדרים ב-`.env`.** בניגוד ל-`ANTHROPIC_API_KEY` (נבדק
  קשיח בזמן import, מפיל את השרת אם חסר), אלה נבדקים **רק בזמן קריאה** (בתוך
  `start_bridge_call`) - Voice הוא feature אופציונלי, והשרת ממשיך לעלות ולשרת WhatsApp
  גם בלעדיהם (`/api/calls/start` פשוט חוזר במצב `simulated`). `PUBLIC_BASE_URL` בלי `/`
  בסוף.

## חלוקת סוכנים

### 1. DevOps Agent
**אחריות:** הרצת הקוד, ניהול השרת (`server.py`), התקנת תלויות, הרצת בדיקות.
**קבצים:** `server.py`, `requirements.txt`, `venv/`
**סמכות:** להפעיל/לכבות את השרת, להתקין חבילות, להריץ בדיקות.
**אסור:** לגעת בנתוני לקוחות (`customers.json`) ישירות — רק דרך הפונקציות ב-`extract.py`.

### 2. QA & Bug Finder Agent
**אחריות:** ניטור לוגים של `server.py`, איתור כשלים בחילוץ AI (JSON לא תקין, שדות חסרים),
שיפור הפרומפט (`EXTRACTION_PROMPT` ב-`extract.py`) בהתאם לכשלים שנצפו בפועל.
**קבצים:** `extract.py` (פרומפטים), לוגים.
**סמכות:** להציע/לערוך פרומפטים.
**אסור:** לשנות לוגיקת עסקים (upsert, מבנה הכרטיס) בלי אישור אנושי.

### 3. Cold Lead Reactivation Agent
**אחריות:** ניהול פנייה ללידים קרים מ-`contacts.csv`, ניסוח הודעות פתיחה מותאמות אישית,
שליחתן בפועל בוואטסאפ דרך Twilio, וסיווג תגובות נכנסות ל-`hot`/`not_relevant`.
**קבצים:** `reactivate.py`, `contacts.csv`, `whatsapp_send.py` (משותף עם `server.py`)
**גם דרך הדשבורד:** `POST /api/reactivate` ב-`server.py` קורא ל-`reactivate.run_reactivation_campaign`
(אותה פונקציה, לא שכפול) - ומופעל מפאנל "🔥 החייאת לידים קרים" ב-`index.html`. ה-UI אוכף
שני שלבים: קודם `{"send": false}` (תצוגה מקדימה חובה - מציגה את כל ההודעות שייווצרו), ורק
אחרי `confirm()` מפורש בדפדפן נשלחת קריאת `{"send": true}` בפועל. **אין** דרך ב-UI לדלג
ישר לשליחה בלי לעבור קודם דרך התצוגה המקדימה.
**סמכות:** לעדכן `lead_status` דרך `update_lead_status`; לשלוח הודעת פתיחה ליד קר **רק**
כשמריצים את הסקריפט עם `--send`, או לוחצים "שלח בפועל" בדשבורד אחרי תצוגה מקדימה
(ברירת המחדל בשני המקומות היא dry-run - תצוגה מקדימה, אין שליחה).
**אסור:** להריץ `--send`/לקרוא ל-`/api/reactivate` עם `send:true` בלי אישור אנושי מפורש
לכל קמפיין ידני; לשלוח למספר production אמיתי (מחוץ ל-Sandbox) בלי הודעת-תבנית מאושרת
מול מטא - שליחת טקסט חופשי כפנייה יזומה שם תיחסם או תסכן את המספר.

**⚠️ עדכון 2026-08-25 - אוטומציה מתוזמנת קיימת עכשיו, אבל כבויה כברירת מחדל:**
`scheduler.py` הוא daemon thread שרץ בתוך `server.py` ומפעיל את `reactivate.run_reactivation_campaign`
**אוטומטית**, בלי קליק אנושי בכל מחזור - זה חריג מכוון לכלל "לא אוטומציה מתוזמנת" למעלה,
והוא בטוח **רק** כי ברירת המחדל שלו היא dry-run תמידי (סורק, מייצר הודעות, רושם ל-
`chat_history.txt` ולטבלת `scheduler_runs`, אבל לא שולח). שליחה אוטומטית אמיתית דורשת
`SCHEDULER_AUTO_SEND=true` מפורש ב-`.env` (כרגע **לא** מוגדר - ראו האזהרה המלאה בראש
`scheduler.py`). **אל תדליקו את זה** בלי לוודא קודם הודעות-תבנית מאושרות מול מטא לכל
ורטיקל, ובלי החלטה מודעת שסבב שליחה ללא אישור נקודתי מקובל עסקית כאן.
הסף לכל ורטיקל (`VERTICAL_COLD_THRESHOLDS_DAYS` ב-`scheduler.py`): `ecommerce`=3 ימים,
`real_estate`=14 ימים בלי קשר. `contacts.csv` מכיל עמודת `vertical` אופציונלית (ברירת
מחדל: `ecommerce`) - זו לוגיקה חדשה, לא הייתה קיימת קודם בנתוני הפרויקט.

### 4. Sales & Objection Agent
**אחריות:** ניהול שיחת מכירה, זיהוי וטיפול בהתנגדויות נפוצות ("יקר לי", "צריך לחשוב",
"לא עכשיו") בהתאם להיסטוריית השיחה בכרטיס הלקוח.
**קבצים:** טרם נוצר — מועמד: `sales_agent.py`
**סמכות:** להציע תסריטי מענה להתנגדויות.
**אסור:** לסגור עסקה, להתחייב על מחיר/הנחה, או לשלוח הודעה בלי אישור אנושי.

### 5. Scheduling & Follow-Up Agent
**אחריות:** ניהול תזכורות מעקב (follow-up) ללידים שלא ענו, קביעת פגישות ביומן.
**קבצים:** טרם נוצר — מועמד: `scheduling_agent.py`
**סמכות:** לקרוא/לכתוב ליומן (כשייבנה חיבור בפועל — Google Calendar API וכו').
**אסור:** למחוק או לשנות פגישות קיימות בלי אישור אנושי.

### 6. BI & Analytics Agent
**אחריות:** הפקת דוחות ביצועים — יחסי המרה לפי ערוץ, זמן תגובה ממוצע, זיהוי צווארי בקבוק
בתהליך המכירה.
**קבצים:** טרם נוצר — מועמד: `analytics.py`, קורא מ-`customers.json`
**סמכות:** קריאה בלבד (read-only).
**אסור:** לשנות נתוני לקוחות בכל צורה שהיא.
