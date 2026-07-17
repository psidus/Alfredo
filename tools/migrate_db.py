import os
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor

def migrate_data():
    # SQLite Setup
    sqlite_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "db", "database.sqlite"))
    if not os.path.exists(sqlite_path):
        print(f"SQLite DB not found at {sqlite_path}", flush=True)
        return
    
    print(f"Connecting to SQLite: {sqlite_path}", flush=True)
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    # Postgres Setup
    pg_url = os.environ.get("DATABASE_URL", "postgresql://alfredo_user:alfredo_password@localhost:5432/alfredo_db")
    print(f"Connecting to Postgres...", flush=True)
    try:
        # connect_timeout=5 to prevent hanging forever
        pg_conn = psycopg2.connect(pg_url, connect_timeout=5)
        pg_cursor = pg_conn.cursor(cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"Could not connect to Postgres: {e}", flush=True)
        return

    tables = ["models", "agents", "tasks", "workflows", "context_memory", "workflow_runs"]

    for table in tables:
        print(f"Migrating table: {table}", flush=True)
        
        # Read from SQLite
        sqlite_cursor.execute(f"SELECT * FROM {table}")
        rows = sqlite_cursor.fetchall()
        
        if not rows:
            print(f"  No data to migrate for {table}", flush=True)
            continue
            
        columns = list(rows[0].keys())
        col_names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        
        try:
            pg_cursor.execute(f"TRUNCATE TABLE {table} CASCADE;")
        except Exception as e:
            pg_conn.rollback()
            print(f"Failed to truncate {table}: {e}")
            
        insert_sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
            
        count = 0
        for row in rows:
            values = list(row)
            if table == "models":
                for i, col in enumerate(columns):
                    if col in ("is_local", "supports_tools"):
                        values[i] = bool(values[i])
            
            try:
                pg_cursor.execute(insert_sql, tuple(values))
                count += 1
            except Exception as e:
                print(f"  Error inserting row {dict(row)}: {e}", flush=True)
                pg_conn.rollback()
        
        print(f"  Migrated {count} rows for {table}.", flush=True)
        
        if table != "context_memory":
            try:
                pg_cursor.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1));")
            except Exception as e:
                print(f"  Could not reset sequence for {table}: {e}", flush=True)
                pg_conn.rollback()

    pg_conn.commit()
    sqlite_conn.close()
    pg_conn.close()
    print("Migration complete!", flush=True)

if __name__ == "__main__":
    migrate_data()
