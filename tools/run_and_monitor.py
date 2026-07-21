import sys, psycopg2, json, time
sys.path.append('.')
from tools.workflow_trigger_tool import trigger_next_batch
from core.db_manager import DBManager

db = DBManager()
cur = db.cursor

print("Starting initial trigger at offset 0...")
trigger_next_batch.func('Thermo Explorer (Autonomous)', 'Basic_properties', 1, 0, 'N/A', 'Pure Components')

print("Waiting for offset 2 to finish...")
for i in range(120):
    cur.execute('SELECT id, inputs, status FROM workflow_runs ORDER BY id DESC LIMIT 5')
    runs = cur.fetchall()
    
    has_offset_2_completed = False
    has_offset_3_started = False
    
    for r in runs:
        inp = json.loads(r['inputs'])
        if inp.get('offset') == 2 and r['status'] == 'completed':
            has_offset_2_completed = True
        if inp.get('offset') == 3:
            has_offset_3_started = True
            
    if has_offset_2_completed or has_offset_3_started:
        print('Offset 2 chunk has been processed successfully!')
        break
    
    latest = runs[0]
    print(f"Waiting... Latest run ID {latest['id']} is offset {json.loads(latest['inputs']).get('offset')} ({latest['status']})")
    time.sleep(10)
