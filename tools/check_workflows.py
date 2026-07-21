import sys, psycopg2, json
sys.path.append('.')
from core.db_manager import DBManager
db = DBManager()
cur = db.cursor
cur.execute("SELECT task_ids_json FROM workflows WHERE name = 'Thermo Explorer (Autonomous)'")
print(dict(cur.fetchone()))
