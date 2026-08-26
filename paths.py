"""
נתיב תיקיית הנתונים המשותף - customers.json, crm_data.db, chat_history.txt.

ברירת מחדל: תיקיית הפרויקט עצמה (בדיוק ההתנהגות הקיימת מקומית - אין שינוי כלל
בהרצה רגילה בלי DATA_DIR מוגדר). בפריסה בענן על דיסק מתמיד (למשל Render Disk),
מגדירים DATA_DIR לנתיב ה-mount (למשל /data) - כדי שהנתונים ישרדו בין דיפלויים.
בלי זה, מערכת קבצים ephemeral (ברירת המחדל בענן) מאפסת הכל בכל דיפלוי מחדש.

לא כולל את contacts.csv (קובץ קלט/seed שמגיע מהקוד עצמו, לא state שמשתנה בזמן ריצה)
ולא את server_error.log (לוג טכני - Render כבר תופס stdout/stderr בפני עצמו, ואין
צורך לשמר אותו בין דיפלויים כמו נתוני לקוחות).
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
