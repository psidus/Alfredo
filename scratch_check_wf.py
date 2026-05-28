import sys
import json
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from core.db_manager import DBManager

db = DBManager()
wfs = [w for w in db.read_all_workflows() if 'start' in w['name'].lower()]
if not wfs:
    print("No startup workflow found.")
    sys.exit(0)

wf = wfs[0]
print("WORKFLOW:")
print(json.dumps(wf, indent=2))
print("\nTASKS AND AGENTS:")

for tid in (wf.get('task_ids') or []):
    t = db.read_task(tid)
    if not t:
        continue
    a = db.read_agent(t.get('agent_id'))
    print(f"--- TASK: {t.get('name')} ---")
    print(f"Desc: {t.get('description')}")
    print(f"Agent Spec: {t.get('agent_specialization')}")
    print(f"Agent: {a.get('role') if a else 'None'}")
    print(f"Tools: {t.get('tools')}")
