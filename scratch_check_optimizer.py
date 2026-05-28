import asyncio
import json
from core.master_ai import MasterAI

async def main():
    ai = MasterAI()
    plan = {
        "agents": [{"role": "Senior Chemist", "goal": "Analyze chemicals", "backstory": "A great chemist"}],
        "tasks": [
            {
                "name": "Full analysis and coding",
                "description": "Search the web for {chemical_name}. Calculate its properties. Audit the python source code in the database and write the final report to a file.",
                "expected_output": "A final report file and validated code.",
                "agent_role": "Senior Chemist",
                "required_inputs": [{"key": "chemical_name", "prompt": "chemical?"}],
                "tools": [],
                "vector_dbs": []
            }
        ]
    }
    
    print("Testing decompose_workflow_plan (AI Optimizer)...")
    optimized = ai.decompose_workflow_plan(plan)
    print(json.dumps(optimized, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
