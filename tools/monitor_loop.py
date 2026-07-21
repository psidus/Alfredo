import sys, psycopg2, time, json
sys.path.append('.')
from core.db_manager import DBManager
db = DBManager()
cur = db.cursor

for i in range(120):
    cur.execute('SELECT id, inputs, status FROM workflow_runs ORDER BY id DESC LIMIT 5')
    runs = cur.fetchall()
    latest_run = runs[0]
    inputs = latest_run[1]
    status = latest_run[2]
    # Check if run with offset 2 completed or if offset 3 started
    has_offset_2_completed = False
    has_offset_3_started = False
    
    for r in runs:
        inp = json.loads(r[1])
        if inp.get('offset') == 2 and r[2] == 'completed':
            has_offset_2_completed = True
        if inp.get('offset') == 3:
            has_offset_3_started = True
            
    if has_offset_2_completed or has_offset_3_started:
        print('Offset 2 chunk has been processed!')
        break
    
    time.sleep(5)
    print(f"Waiting... Latest run is offset {json.loads(inputs).get('offset')} ({status})")
