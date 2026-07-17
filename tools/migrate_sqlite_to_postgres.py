import os
import sys
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.postgres_manager import PostgresManager

def migrate():
    print("Starting migration from SQLite to PostgreSQL...")
    sqlite_path = os.path.join(os.path.dirname(__file__), '..', 'db', 'database.sqlite')
    if not os.path.exists(sqlite_path):
        sqlite_path = os.path.join(os.path.dirname(__file__), '..', 'db', 'alfredo.db')
        if not os.path.exists(sqlite_path):
            print(f"Could not find SQLite DB at {sqlite_path}")
            return

    postgres_url = os.environ.get("DATABASE_URL")
    if not postgres_url:
        print("DATABASE_URL environment variable is missing. E.g. postgresql://user:pass@localhost:5432/db")
        return

    # Initialize Postgres DB (this creates tables if they don't exist)
    print("Initializing Postgres schema...")
    pg_manager = PostgresManager(db_url=postgres_url)
    
    try:
        sqlite_conn = sqlite3.connect(sqlite_path)
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_cursor = sqlite_conn.cursor()

        pg_conn = psycopg2.connect(postgres_url)
        pg_cursor = pg_conn.cursor()

        tables_to_migrate = [
            "models",
            "agents",
            "tasks",
            "workflows",
            "context_memory",
            "vector_databases",
            "apps",
            "hitl_requests",
            "workflow_runs"
        ]

        for table in tables_to_migrate:
            print(f"Migrating table: {table}...")
            sqlite_cursor.execute(f"SELECT * FROM {table}")
            rows = sqlite_cursor.fetchall()
            
            if not rows:
                print(f"  Table {table} is empty, skipping.")
                continue

            # Get columns from the first row
            columns = rows[0].keys()
            cols_str = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            
            # Clear existing PG table
            pg_cursor.execute(f"TRUNCATE {table} CASCADE")

            insert_sql = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})"
            
            inserted = 0
            for row in rows:
                values = [row[col] for col in columns]
                pg_cursor.execute(insert_sql, values)
                inserted += 1
                
            print(f"  Inserted {inserted} rows into {table}.")
            
            # Update sequence if the table has an 'id' column
            if 'id' in columns:
                pg_cursor.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1))")
        
        pg_conn.commit()
        print("Migration completed successfully!")
        
    except Exception as e:
        print(f"Migration failed: {e}")
        if 'pg_conn' in locals():
            pg_conn.rollback()
    finally:
        if 'sqlite_conn' in locals():
            sqlite_conn.close()
        if 'pg_conn' in locals():
            pg_conn.close()

if __name__ == "__main__":
    migrate()
