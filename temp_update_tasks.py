import sys, os, json
sys.path.append(os.getcwd())
from core.db_manager import DBManager
db = DBManager()

t25 = db.read_task(25)
if t25:
    new_desc_25 = t25['description'].replace("output 'SKIP'", "output a JSON with status='SKIP'")
    db.cursor.execute("UPDATE tasks SET description = %s WHERE id = 25", (new_desc_25,))

t26 = db.read_task(26)
if t26:
    new_desc_26 = t26['description'].replace("If '{previous_result}' is 'SKIP'", "If '{previous_result}' is 'SKIP' or its JSON status is 'SKIP'")
    db.cursor.execute("UPDATE tasks SET description = %s WHERE id = 26", (new_desc_26,))

db.conn.commit()
print('Tasks updated for better SKIP handling.')
