import asyncio
import json
from core.master_ai import MasterAI
from core.db_manager import DBManager
from core.crew_builder import execute_dynamic_crew_with_memory

async def main():
    db = DBManager()
    wf = db.read_workflow(1) # Biomass Model Generator
    task_ids = wf['task_ids']
    wf_agents = []
    wf_tasks = []
    seen = set()
    for tid in task_ids:
        t = db.read_task(tid)
        a = db.read_agent(t['agent_id'])
        arole = a['role']
        if arole not in seen:
            wf_agents.append({"role": arole, "goal": a.get('goal'), "backstory": a.get('backstory'), "tools": a.get('tools') or []})
            seen.add(arole)
        wf_tasks.append({
            "name": t['name'], "description": t['description'], "expected_output": t['expected_output'],
            "agent_role": arole, "agent_specialization": t.get('agent_specialization'),
            "required_inputs": t.get('required_inputs'),
            "tools": t.get('tools') or [],
            "vector_dbs": t.get('vector_dbs') or []
        })
    plan = {"agents": wf_agents, "tasks": wf_tasks}
    
    ai = MasterAI()
    print("Testing decompose_workflow_plan...")
    optimized = ai.decompose_workflow_plan(plan)
    
    print("Starting execution of Dynamic Workflow...")
    execution_context = {"idea": "Test idea for biomass", "chemical_name": "Lignin"}
    try:
        result_tuple = await asyncio.to_thread(
            execute_dynamic_crew_with_memory, optimized, execution_context, None, None, 0, {}, "", "12345"
        )
        print("SUCCESS:")
        print(result_tuple[0])
    except Exception as e:
        print(f"FAILED WITH EXCEPTION: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
