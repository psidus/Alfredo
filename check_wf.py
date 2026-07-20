import sys, os, json
sys.path.append(os.getcwd())
from core.db_manager import DBManager
db = DBManager()
wfs = db.read_all_workflows()
wf = next((w for w in wfs if w['name'] == 'Thermo Explorer (Autonomous)'), None)
if not wf:
    print('WF not found')
    sys.exit()

tasks = db.read_all_tasks()
task_map = {t['id']: t for t in tasks}

t_ids_field = wf.get('task_ids_json') or wf.get('task_ids')
t_ids = t_ids_field if isinstance(t_ids_field, list) else json.loads(t_ids_field)

print('--- WF STRUCTURE ---')
for step in t_ids:
    if step.get('type') == 'batch_loop':
        print(f"Level {step.get('execution_level')}: BATCH LOOP (source: {step.get('source_variable')}, size: {step.get('batch_size')})")
        for tid in step.get('task_ids', []):
            t = task_map[tid]
            print(f"  -> {t['name']} (Tools: {t.get('tools')}, Pydantic: {t.get('output_pydantic')})")
    else:
        tid = step.get('task_id')
        t = task_map[tid]
        print(f"Level {step.get('execution_level')}: {t['name']} (Tools: {t.get('tools')}, Pydantic: {t.get('output_pydantic')})")
print('--------------------')
