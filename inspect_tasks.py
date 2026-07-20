import os, sys, json
sys.path.append(os.getcwd())
from core.db_manager import DBManager

def inspect_tasks():
    db = DBManager()
    agents = db.read_all_agents()
    clausius = next((a for a in agents if 'Clausius' in a['name']), None)
    if not clausius:
        print("Clausius not found.")
        return

    print(f"Clausius Agent ID: {clausius['id']}")
    
    tasks = db.read_all_tasks()
    clausius_tasks = [t for t in tasks if t.get('agent_id') == clausius['id']]
    
    print("\n--- CLAUSIUS TASKS ---")
    for t in clausius_tasks:
        print(f"ID: {t['id']} | Name: {t.get('name')} | Tools: {t.get('tools')} | Desc: {t['description'][:60]}...")

    workflows = db.read_all_workflows()
    print("\n--- WORKFLOWS ---")
    active_task_ids = set()
    for w in workflows:
        try:
            t_ids = json.loads(w['task_ids_json'])
            print(f"Workflow ID: {w['id']} | Name: {w['name']}")
            print(f"Task IDs JSON: {t_ids}")
            
            def extract_ids(node):
                if isinstance(node, int):
                    active_task_ids.add(node)
                elif isinstance(node, dict):
                    if 'task_id' in node:
                        active_task_ids.add(int(node['task_id']))
                    if 'task_ids' in node:
                        for inner in node['task_ids']:
                            extract_ids(inner)
            
            for tid in t_ids:
                extract_ids(tid)
        except Exception as e:
            print(f"Error parsing workflow {w['id']}: {e}")

    print("\n--- ACTIVE TASK IDS IN WORKFLOWS ---")
    print(active_task_ids)

if __name__ == "__main__":
    inspect_tasks()
