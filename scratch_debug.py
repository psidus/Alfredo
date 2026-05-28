"""Debug script: shows model config, agent config, workflow tasks, and tries a minimal kickoff."""
import os
import sys
import io
import json
import traceback

# Force UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from core.db_manager import DBManager
from core.data_manager import DataManager
from core.crew_builder import _instantiate_llm, _map_tools, _get_task_tools, execute_dynamic_crew_with_memory, RateLimitError
from core.master_ai import MasterAI
from crewai import Agent, Task, Crew

db = DBManager()

print("=" * 60)
print("1. ALL MODELS IN DATABASE")
print("=" * 60)
models = db.read_all_models()
for m in models:
    print(f"  ID={m['id']}  provider={m['provider']}  model_name={m['model_name']}  is_local={m.get('is_local', 0)}")

print()
DataManager.load_env()
env_model = os.getenv("DEFAULT_AGENT_MODEL_ID")
print(f"  DEFAULT_AGENT_MODEL_ID from .env: {env_model}")

print()
print("=" * 60)
print("2. ALL AGENTS IN DATABASE")
print("=" * 60)
for m in models:
    agents = db.cursor.execute("SELECT * FROM agents WHERE model_id = ?", (m['id'],)).fetchall()
    for a in agents:
        adict = dict(a)
        print(f"  Agent ID={adict['id']} role={adict['role'][:60]}  model_id={adict['model_id']}  tools={adict.get('tools')}")

print()
print("=" * 60)
print("3. WORKFLOW 1 TASK DETAILS")
print("=" * 60)
wf = db.read_workflow(1)
print(f"  Workflow: {wf['name']}")
for tid in wf['task_ids']:
    t = db.read_task(tid)
    a = db.read_agent(t['agent_id'])
    print(f"  Task ID={tid}  name={t.get('name')}  agent_role={a['role'][:50]}  spec={t.get('agent_specialization')}")
    print(f"    tools={t.get('tools')}  vector_dbs={t.get('vector_dbs')}")
    print(f"    required_inputs={t.get('required_inputs')}")
    print()

print("=" * 60)
print("4. ATTEMPTING MINIMAL CREW KICKOFF (one agent, one task)")
print("=" * 60)

# Use the first model
model_id = models[0]['id']
try:
    llm = _instantiate_llm(model_id)
    print(f"  LLM instantiated: {llm}")
except Exception as e:
    print(f"  FAILED to instantiate LLM: {e}")
    traceback.print_exc()
    sys.exit(1)

# Build a minimal agent and task
try:
    test_agent = Agent(
        role="Test Agent",
        backstory="You are a simple test agent.",
        goal="Answer a simple question.",
        llm=llm,
        tools=[],
        verbose=True,
        allow_delegation=False,
        max_iter=3
    )
    test_task = Task(
        description="What is 2 + 2? Answer with just the number.",
        expected_output="A single number",
        agent=test_agent,
        async_execution=False
    )
    test_crew = Crew(
        agents=[test_agent],
        tasks=[test_task],
        verbose=True,
        process='sequential'
    )
    print("  Crew built. Kicking off...")
    result = test_crew.kickoff()
    print(f"  SUCCESS! Result: {result}")
except Exception as e:
    print(f"  FAILED during kickoff: {type(e).__name__}: {e}")
    traceback.print_exc()

print()
print("=" * 60)
print("5. ATTEMPTING FULL WORKFLOW EXECUTION (with decompose)")
print("=" * 60)

# Build plan from DB
wf_agents = []
wf_tasks = []
seen = set()
for tid in wf['task_ids']:
    t = db.read_task(tid)
    a = db.read_agent(t['agent_id'])
    arole = a['role']
    if arole not in seen:
        wf_agents.append({"role": arole, "goal": a.get('goal'), "backstory": a.get('backstory'), "tools": a.get('tools') or []})
        seen.add(arole)
    wf_tasks.append({
        "name": t.get('name'), "description": t['description'], "expected_output": t['expected_output'],
        "agent_role": arole, "agent_specialization": t.get('agent_specialization'),
        "required_inputs": t.get('required_inputs'),
        "tools": t.get('tools') or [],
        "vector_dbs": t.get('vector_dbs') or []
    })
plan = {"agents": wf_agents, "tasks": wf_tasks}

ai = MasterAI()
print("  Decomposing workflow plan...")
try:
    optimized = ai.decompose_workflow_plan(plan)
    print(f"  Decomposed into {len(optimized['tasks'])} tasks with {len(optimized['agents'])} agents.")
    for i, t in enumerate(optimized['tasks']):
        print(f"    [{i}] {t.get('name', 'Unnamed')} -> agent_role='{t.get('agent_role', '?')}'  tools={t.get('tools')}  vector_dbs={t.get('vector_dbs')}")
except Exception as e:
    print(f"  FAILED to decompose: {e}")
    traceback.print_exc()
    sys.exit(1)

print()
print("  Starting execute_dynamic_crew_with_memory...")
execution_context = {"idea": "A biomass gasification system for rural areas"}
try:
    result_tuple = execute_dynamic_crew_with_memory(
        optimized, execution_context, None, None, 0, {}, "", "12345"
    )
    print(f"  SUCCESS! Final output length: {len(str(result_tuple[0]))}")
except RateLimitError as rle:
    print(f"  RATE LIMIT ERROR at task {rle.current_task_idx}: {rle}")
except Exception as e:
    print(f"  FAILED: {type(e).__name__}: {e}")
    traceback.print_exc()

print("\n=== DEBUG COMPLETE ===")
