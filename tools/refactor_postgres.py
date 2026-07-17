import re
import os

filepath = "C:/Users/Pietro/Documents/GitHub/Alfredo/core/sqlite_manager.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Imports and Class Name
content = content.replace("import sqlite3", "import psycopg2\nfrom psycopg2.extras import RealDictCursor")
content = content.replace("class SQLiteManager:", "class PostgresManager:")
content = content.replace("def __init__(self, db_path: str = 'db/database.sqlite'):", 'def __init__(self, db_url: str = None):\n        if not db_url:\n            db_url = os.environ.get("DATABASE_URL")\n        if not db_url:\n            raise ValueError("DATABASE_URL environment variable is required for PostgresManager.")\n        self.db_url = db_url')
content = re.sub(r'self\.db_path = db_path', '', content)
content = content.replace("self.conn: Optional[sqlite3.Connection]", "self.conn")
content = content.replace("self.cursor: Optional[sqlite3.Cursor]", "self.cursor")

# 2. Connection Logic
connect_method = """    def _connect(self):
        try:
            self.conn = psycopg2.connect(self.db_url)
            self.cursor = self.conn.cursor(cursor_factory=RealDictCursor)
        except psycopg2.Error as e:
            print(f"Database connection error: {e}")
            raise"""
content = re.sub(r'    def _connect\(self\):.*?raise\n', connect_method + '\n', content, flags=re.DOTALL)

# 3. Schema types
content = content.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
content = content.replace("sqlite3.Error", "psycopg2.Error")
content = content.replace("sqlite3.OperationalError", "psycopg2.Error")
content = re.sub(r'except psycopg2\.Error:\s+pass', 'except psycopg2.Error:\n                self.conn.rollback()', content)
content = re.sub(r'except BaseException:\s+pass', 'except BaseException:\n                self.conn.rollback()', content)
content = content.replace("sqlite3.Row", "dict")
content = content.replace("return dict(row) if row else None", "return dict(row) if row else None")
content = content.replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE")
content = content.replace("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE")

# 4. Fix INSERT and lastrowid
# We need to find all INSERT statements and append RETURNING id
content = re.sub(r'(sql = "INSERT INTO [^"]+)\"', r'\1 RETURNING id"', content)
content = content.replace("self.cursor.lastrowid", "self.cursor.fetchone()['id']")

# 5. Placeholders ? -> %s
lines = content.split('\n')
for i, line in enumerate(lines):
    if '?' in line and ('sql' in line or 'execute' in line or 'updates.append' in line or 'placeholders' in line):
        # Don't replace in comments
        if '#' in line and line.find('?') > line.find('#'):
            pass
        else:
            lines[i] = line.replace('?', '%s')
content = '\n'.join(lines)

# 5a. Fix boolean inserts
content = content.replace(", 1 if is_local else 0", ", bool(is_local)")
content = content.replace(", 1 if supports_tools else 0", ", bool(supports_tools)")

# 6. Delete task logic
# Delete temp tables logic is very sqlite specific. Postgres sequence reset requires SETVAL
content = content.replace("UPDATE sqlite_sequence SET seq = %s WHERE name = 'tasks'", "SELECT setval(pg_get_serial_sequence('tasks', 'id'), COALESCE((SELECT MAX(id) FROM tasks), 1))")
content = content.replace("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'tasks'", "SELECT setval(pg_get_serial_sequence('tasks', 'id'), 1, false)")

content = content.replace("UPDATE sqlite_sequence SET seq = %s WHERE name = 'workflows'", "SELECT setval(pg_get_serial_sequence('workflows', 'id'), COALESCE((SELECT MAX(id) FROM workflows), 1))")
content = content.replace("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'workflows'", "SELECT setval(pg_get_serial_sequence('workflows', 'id'), 1, false)")

# Postgres doesn't have PRAGMA table_info, it uses information_schema
content = content.replace("PRAGMA table_info(tasks)", "SELECT column_name as name FROM information_schema.columns WHERE table_name = 'tasks'")
content = content.replace("PRAGMA table_info(workflows)", "SELECT column_name as name FROM information_schema.columns WHERE table_name = 'workflows'")

with open("C:/Users/Pietro/Documents/GitHub/Alfredo/core/postgres_manager.py", "w", encoding="utf-8") as f:
    f.write(content)
