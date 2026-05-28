import asyncio
import json
from core.master_ai import MasterAI
from core.db_manager import DBManager

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
            wf_agents.append({"role": arole, "goal": a.get('goal'), "backstory": a.get('backstory')})
            seen.add(arole)
        wf_tasks.append({
            "name": t['name'], "description": t['description'], "expected_output": t['expected_output'],
            "agent_role": arole, "agent_specialization": t.get('agent_specialization'),
            "required_inputs": t.get('required_inputs')
        })
    plan = {"agents": wf_agents, "tasks": wf_tasks}
    
    ai = MasterAI()
    print("Testing decompose_workflow_plan...")
    optimized = ai.decompose_workflow_plan(plan)
    
    available_roles = [a['role'] for a in optimized['agents']]
    print("Available roles:", available_roles)
    for t in optimized['tasks']:
        print(f"Task '{t.get('name')}' assigned to: '{t.get('agent_role')}'")
        if t.get('agent_role') not in available_roles:
            print(f"  --> MISMATCH! '{t.get('agent_role')}' not in available roles.")

if __name__ == '__main__':
    asyncio.run(main())
