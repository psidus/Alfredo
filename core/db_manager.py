import os
from typing import Union
from core.sqlite_manager import SQLiteManager
from core.postgres_manager import PostgresManager

def DBManager() -> Union[SQLiteManager, PostgresManager]:
    """
    Factory function that returns either a PostgresManager or SQLiteManager
    depending on the presence of the DATABASE_URL environment variable.
    """
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        print("🚀 DBManager Router: Connecting to PostgreSQL database...")
        return PostgresManager(db_url=db_url)
    else:
        print("⚠️ DBManager Router: DATABASE_URL not found. Falling back to lightweight SQLite local database...")
        return SQLiteManager()
