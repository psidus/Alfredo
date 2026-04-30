import json
import os
import re
from crewai import Crew

try:
    from identities import pfc_manager, systems_architect, frontend_dev, backend_dev
    from tasks import create_dynamic_tasks
except ImportError:
    from agents.identities import pfc_manager, systems_architect, frontend_dev, backend_dev
    from agents.tasks import create_dynamic_tasks

def extract_code(text):
    """
    Extracts Python code from agent output. 
    Prioritizes content inside ```python ... ``` blocks.
    """
    # Try to find ```python ... ``` blocks
    code_blocks = re.findall(r'```python\s+(.*?)\s+```', text, re.DOTALL)
    if code_blocks:
        return code_blocks[-1].strip() # Use the last block (likely the final code)
    
    # Try to find any ``` ... ``` blocks
    code_blocks = re.findall(r'```\s+(.*?)\s+```', text, re.DOTALL)
    if code_blocks:
        return code_blocks[-1].strip()
    
    # If no blocks, return the whole text (might be risky but better than nothing)
    return text.strip()

def find_next_task(prd_data):
    """
    Find the first task with status 'todo' whose dependencies are all completed ('done').
    """
    completed_tasks = []
    for module in prd_data['modules']:
        for task in module['tasks']:
            if task['status'] == 'done':
                completed_tasks.append(task['task_id'])

    for module in prd_data['modules']:
        for task in module['tasks']:
            if task['status'] == 'todo':
                if all(dep in completed_tasks for dep in task['dependencies']):
                    return task, module['module_id']

    return None, None

def run_ralph_cycle():
    """
    Executes a single cycle of the Ralph Loop.
    """
    print("\n=== STARTING RALPH CYCLE (GSD EXECUTION) ===")

    prd_path = 'prd.json'
    plan_path = 'Overall_plan.txt'
    progress_path = 'progress.txt'

    if not os.path.exists(prd_path):
        print(f"[ERROR] {prd_path} not found.")
        return

    # 1. READ PRD AND OVERALL PLAN
    with open(prd_path, 'r', encoding='utf-8') as f:
        prd_data = json.load(f)
    
    overall_plan = ""
    if os.path.exists(plan_path):
        with open(plan_path, 'r', encoding='utf-8') as f:
            overall_plan = f.read()

    # 2. SELECT NEXT TASK
    target_task, module_id = find_next_task(prd_data)

    if not target_task:
        print("No executable task found or project is complete.")
        return

    print(f"[INFO] Selected task: {target_task['task_id']} - {target_task['title']}")
    print(f"[INFO] Target file: {target_task['target_file']}")

    # 3. CREATE TASKS WITH CONTEXT
    tasks = create_dynamic_tasks(
        target_task, 
        overall_plan=overall_plan, 
        full_prd=json.dumps(prd_data, indent=2)
    )

    crew = Crew(
        agents=[pfc_manager, systems_architect, frontend_dev, backend_dev],
        tasks=tasks,
        verbose=True,
        max_rpm=20
    )

    # 4. EXECUTE TASK PIPELINE
    result = crew.kickoff()

    # 5. EXTRACT AND SAVE GENERATED CODE
    final_output = str(result)
    cleaned_code = extract_code(final_output)

    if not cleaned_code or len(cleaned_code) < 10:
        print("[WARNING] Extracted code seems too short or empty. Check agent output.")
    
    target_file_path = os.path.join(os.getcwd(), target_task['target_file'])
    os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

    with open(target_file_path, 'w', encoding='utf-8') as f:
        f.write(cleaned_code)

    print(f"[SUCCESS] Code saved to {target_file_path}")

    # 6. UPDATE PROJECT STATE
    target_task['status'] = 'done'

    with open(prd_path, 'w', encoding='utf-8') as f:
        json.dump(prd_data, f, indent=2)

    with open(progress_path, 'a', encoding='utf-8') as f:
        f.write(f"Completed: {target_task['task_id']} - {target_task['title']} saved in {target_task['target_file']}\n")

    print("=== CYCLE COMPLETED SUCCESSFULLY ===\n")

if __name__ == "__main__":
    run_ralph_cycle()
