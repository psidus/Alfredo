import ast
import json
from core.db_manager import DBManager

db = DBManager()
db.cursor.execute("SELECT id, status, inputs, result FROM workflow_runs ORDER BY id DESC LIMIT 5")
runs = db.cursor.fetchall()

for r in runs:
    print(f"=============================")
    print(f"Run ID: {r['id']}")
    print(f"Status: {r['status']}")
    print(f"Inputs: {r['inputs']}")
    
    if not r['result']:
        print("Result: In Progress or None")
        continue
        
    try:
        # result is likely a string representation of a tuple: (final_output_string, list_of_dicts)
        result_tuple = ast.literal_eval(r['result'])
        if isinstance(result_tuple, tuple) and len(result_tuple) >= 2:
            print("--- Final Output ---")
            print(result_tuple[0])
            
            print("\n--- Memory Logs ---")
            memory = json.loads(result_tuple[1])
            for step in memory:
                print(f">> TASK: {step.get('key', 'Unknown')}")
                if 'data' in step:
                    print(step['data'])
                elif 'summary' in step:
                    print(step['summary'][:500] + "..." if len(step['summary']) > 500 else step['summary'])
        else:
            print("Result format unknown:", r['result'])
    except Exception as e:
        print(f"Failed to parse result: {e}")
        # print raw
        print("Raw result:", r['result'][:500])
