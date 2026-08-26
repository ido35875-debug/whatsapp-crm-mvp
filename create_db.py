import sqlite3
from pathlib import Path

DB_FILE = Path(__file__).parent / "crm_data.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # יצירת טבלת לקוחות
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS customers (
            phone TEXT PRIMARY KEY,
            name TEXT,
            location TEXT,
            status TEXT DEFAULT 'חדש'
        )
    ''')

    # יצירת טבלת הודעות
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (phone) REFERENCES customers(phone)
        )
    ''')

    conn.commit()
    conn.close()
    print("Database initialized successfully!")


if __name__ == "__main__":
    init_db()
