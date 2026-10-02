import sqlite3
import pandas as pd

from src.config import settings

def check_db():
    print("Checking tweets.sqlite...")
    try:
        conn = sqlite3.connect(settings.db_path)
        df = pd.read_sql_query("SELECT * FROM sqlite_master WHERE type='table'", conn)
        print("Tables in tweets.sqlite:", df["name"].tolist())
        conn.close()
    except Exception as e:
        print("Error checking tweets.sqlite:", e)

    print("\nChecking viral_hunter.db...")
    try:
        conn = sqlite3.connect("d:/viral-hunter/viral_hunter.db")
        df = pd.read_sql_query("SELECT * FROM sqlite_master WHERE type='table'", conn)
        print("Tables in viral_hunter.db:", df["name"].tolist())
        
        if "pipeline_runs" in df["name"].tolist():
            runs = pd.read_sql_query("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 10", conn)
            print("\nRecent pipeline_runs:")
            print(runs.to_string())
            
        if "raw_tweets" in df["name"].tolist():
            tweets = pd.read_sql_query("SELECT COUNT(*) as count FROM raw_tweets", conn)
            print("\nTotal raw_tweets:", tweets.iloc[0]["count"])
            
    except Exception as e:
        print("Error checking viral_hunter.db:", e)

if __name__ == "__main__":
    check_db()
