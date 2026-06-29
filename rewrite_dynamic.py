import re

file_path = "c:/Users/pietr/OneDrive/Documenti/GitHub/Alfredo/core/crew_builder.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

before = content[:content.find("def execute_dynamic_crew_with_memory")]
after = content[content.find("if __name__ == '__main__':"):]

new_func = """def execute_dynamic_crew_with_memory(plan: dict, execution_context: dict = None, default_model_id=None, run_id: int = None, start_idx: int = 0, initial_task_outputs: dict = None, accumulated_context: str = None, chat_id: str = None, progress_callback=None) -> str:
    if not plan or 'agents' not in plan or 'tasks' not in plan:
        raise ValueError("Invalid plan format. Must contain 'agents' and 'tasks'.")

    import concurrent.futures
    import threading
    import os
    import json
    import re
    import time
    from dotenv import dotenv_values, find_dotenv

    if chat_id:
        ABORT_FLAGS[str(chat_id)] = False

    execution_context = execution_context or {}

    if not default_model_id:
        DataManager.load_env()
        env_model_id = os.getenv("DEFAULT_AGENT_MODEL_ID")
        if env_model_id:
            try:
                default_model_id = int(env_model_id)
            except ValueError:
                pass

    if not default_model_id:
        models = db.read_all_models()
        if models:
            default_model_id = models[0]['id']
        else:
            raise ValueError("No models found in the database.")

    llm_instance = _instantiate_llm(default_model_id)
    model_record = db.read_model(default_model_id)
    is_local_model = False
    if model_record:
        is_local_model = bool(model_record.get('is_local')) or model_record.get('provider', '').lower() == 'ollama'

    agents_data_by_role = {a['role']: a for a in plan.get('agents', [])}
    conciseness_trait = ("\\n\\nCRITICAL TRAIT: You are extremely concise and data-driven. "
                         "You never ramble. You output structured bullet points, not essays. "
                         "Every sentence must carry unique, actionable information.")

    dynamic_run_id = int(time.time()) % 1_000_000
    memory_manager = EphemeralMemoryManager(run_id=dynamic_run_id)
    if accumulated_context:
        memory_manager.load_from_dump(accumulated_context)
    read_memory_tool = ReadAtomicMemoryTool(memory_manager=memory_manager)
    write_memory_tool = WriteAtomicMemoryTool(memory_manager=memory_manager)

    agents_cache = {}
    task_outputs = initial_task_outputs or {}

    # Normalize DAG for dynamic plan
    dag_nodes = {}
    tasks = plan['tasks']
    for i, task_data in enumerate(tasks):
        node_id = task_data.get("id", f"node_{i}")
        depends_on = task_data.get("depends_on", [])
        if "id" not in task_data:
            if i > 0 and not depends_on:
                depends_on = [f"node_{i-1}"]
            
        dag_nodes[node_id] = {
            "task_data": task_data,
            "depends_on": depends_on,
            "original_index": i
        }

    in_degree = {n: 0 for n in dag_nodes}
    dependents = {n: [] for n in dag_nodes}
    for n_id, data in dag_nodes.items():
        for d in data["depends_on"]:
            if d in dag_nodes:
                in_degree[n_id] += 1
                dependents[d].append(n_id)

    completed_nodes = set()
    node_outputs = {}
    for n_id, data in dag_nodes.items():
        idx = data["original_index"]
        if str(idx) in task_outputs:
            completed_nodes.add(n_id)
            node_outputs[n_id] = task_outputs[str(idx)]
            
    for n_id in completed_nodes:
        for dep in dependents[n_id]:
            in_degree[dep] -= 1

    env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
    env_vars = dotenv_values(env_path)
    try:
        MAX_VRAM_GB = float(env_vars.get("MAX_VRAM_GB", 24.0))
    except Exception:
        MAX_VRAM_GB = 24.0

    current_vram_usage = 0.0
    vram_lock = threading.Lock()
    vram_condition = threading.Condition(vram_lock)
    task_outputs_lock = threading.Lock()
    
    def get_dynamic_task_vram_cost():
        # Dynamic agents usually share the default model
        if model_record:
            return float(model_record.get('vram_gb') or 0.0)
        return 0.0

    ready_queue = [n for n, deg in in_degree.items() if deg == 0 and n not in completed_nodes]
    in_flight = set()

    def execute_dynamic_node(n_id):
        nonlocal current_vram_usage
        data = dag_nodes[n_id]
        task_data = data["task_data"]
        task_idx = data["original_index"]
        
        cost = get_dynamic_task_vram_cost()
        
        with vram_condition:
            while current_vram_usage + cost > MAX_VRAM_GB and (current_vram_usage > 0 or cost > MAX_VRAM_GB):
                if current_vram_usage == 0 and cost > MAX_VRAM_GB:
                    break
                vram_condition.wait()
            current_vram_usage += cost

        try:
            parent_output = ""
            if data["depends_on"]:
                parent_id = data["depends_on"][-1]
                parent_output = node_outputs.get(parent_id, "")

            if run_id:
                db.update_run(run_id, status='running', current_task_idx=task_idx, task_outputs=task_outputs)

            agent_role = task_data.get('agent_role')
            specialization = task_data.get('agent_specialization')

            if agent_role not in agents_data_by_role:
                logging.warning(f"Task specifies unknown agent role '{agent_role}'. Skipping.")
                return ""

            agent_info = agents_data_by_role[agent_role]
            base_role = agent_info['role']
            base_backstory = agent_info.get('backstory', '') or ""
            base_goal = agent_info.get('goal', '') or ""

            if specialization:
                if "{specialization}" in base_role:
                    effective_role = base_role.replace("{specialization}", specialization)
                else:
                    effective_role = f"{base_role} specialized in {specialization}"
                
                if "{specialization}" in base_backstory:
                    effective_backstory = base_backstory.replace("{specialization}", specialization)
                else:
                    effective_backstory = base_backstory + f"\\n\\nCRITICAL CONTEXT: Your expertise is focused on **{specialization}**."

                if "{specialization}" in base_goal:
                    effective_goal = base_goal.replace("{specialization}", specialization)
                else:
                    effective_goal = f"{base_goal} (specialized in {specialization})"
            else:
                effective_role = base_role.replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
                effective_backstory = base_backstory.replace("{specialization}", "").strip()
                effective_goal = base_goal.replace("{specialization}", "").strip()

            effective_role = re.sub(r'\\{([a-zA-Z0-9_]+)\\}', r'<\\1>', effective_role)
            effective_backstory = re.sub(r'\\{([a-zA-Z0-9_]+)\\}', r'<\\1>', effective_backstory)
            effective_goal = re.sub(r'\\{([a-zA-Z0-9_]+)\\}', r'<\\1>', effective_goal)

            if not specialization and agent_role in agents_cache:
                agent_instance = agents_cache[agent_role]
            else:
                agent_tools = [] if is_local_model else _map_tools(agent_info.get('tools', []))
                enhanced_backstory = effective_backstory + conciseness_trait
                agent_instance = Agent(
                    role=effective_role,
                    backstory=enhanced_backstory,
                    goal=effective_goal,
                    llm=llm_instance,
                    tools=agent_tools,
                    verbose=True,
                    allow_delegation=False,
                    max_iter=5,
                    step_callback=check_abort
                )
                if not specialization:
                    agents_cache[agent_role] = agent_instance

            combined_tools = list(set((agent_info.get('tools') or []) + (task_data.get('tools') or [])))
            task_tools = _get_task_tools(combined_tools, task_data.get('vector_dbs') or [], is_local_model)
            task_description = task_data['description'] + AGENT_COMMS_DIRECTIVE

            for k, v in execution_context.items():
                task_description = task_description.replace(f"{{{k}}}", str(v))
            task_description = task_description.replace("{user_input}", execution_context.get('user_input', ''))
            task_description = task_description.replace("{previous_result}", parent_output)
            task_description = task_description.replace("{context}", parent_output)

            index_table = memory_manager.get_memory_index_table()
            task_description += (
                "\\n\\n--- [EPHEMERAL WORKSPACE MEMORY INDEX] ---\\n"
                "Results from previous steps are stored in the ephemeral in-memory database.\\n"
                "Use the 'read_atomic_memory' tool with the exact 'key' to retrieve data.\\n"
                "Use 'write_atomic_memory' to store YOUR output for downstream agents.\\n\\n"
                f"{index_table}\\n"
                "--- [END MEMORY INDEX] ---\\n"
            )

            base_expected = task_data.get('expected_output', 'Task Output')
            vector_format_directive = " FORMAT CRITERIA (For Vector DB): Begin with a clear '# Topic: <Subject>' header, a 1-line summary, a '[KEYWORDS: ...]' block, and then self-contained, noun-heavy bullet points."
            if "vector" not in base_expected.lower() and "header" not in base_expected.lower():
                base_expected += vector_format_directive

            for k, v in execution_context.items():
                base_expected = base_expected.replace(f"{{{k}}}", str(v))
            base_expected = base_expected.replace("{user_input}", execution_context.get('user_input', ''))
            base_expected = base_expected.replace("{previous_result}", parent_output)
            base_expected = base_expected.replace("{context}", parent_output)

            task_description = re.sub(r'\\{([a-zA-Z0-9_]+)\\}', r'<\\1>', task_description)
            base_expected = re.sub(r'\\{([a-zA-Z0-9_]+)\\}', r'<\\1>', base_expected)

            task_obj = Task(
                description=task_description,
                expected_output=base_expected,
                agent=agent_instance,
                tools=task_tools,
                async_execution=False
            )

            _inject_memory_tools(agent_instance, task_obj, read_memory_tool, write_memory_tool)

            single_crew = Crew(
                agents=[agent_instance],
                tasks=[task_obj],
                verbose=True,
                process='sequential'
            )

            if progress_callback:
                try:
                    progress_callback(task_idx, len(plan['tasks']), effective_role, "running")
                except Exception:
                    pass

            max_retries = 2
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        time.sleep(10 * attempt)
                        single_crew = Crew(agents=[agent_instance], tasks=[task_obj], verbose=True, process='sequential')
                    result = single_crew.kickoff(inputs=execution_context)
                    last_exception = None
                    break
                except Exception as e:
                    last_exception = e
                    err_str = str(e).lower()
                    is_transient = "503" in err_str or "unavailable" in err_str or "rate" in err_str or "empty" in err_str or "none" in err_str or "model output" in err_str or "resource" in err_str or "overloaded" in err_str
                    if is_transient and attempt < max_retries:
                        continue
                    elif is_transient:
                        if run_id: db.update_run(run_id, status='paused', result=str(e), current_task_idx=task_idx, task_outputs=task_outputs)
                        raise RateLimitError(f"Model transient error after retries: {e}", task_idx, task_outputs)
                    else:
                        raise e

            if last_exception is not None:
                raise last_exception
                    
            task_out = str(result)
            key_name = f"dynamic_task_{task_idx}"
            summary_text = f"Output of dynamic task {task_idx + 1} by agent '{effective_role}': {task_out[:500]}"
            memory_manager.write_record(key=key_name, content_summary=summary_text, structured_data={"raw_output": task_out}, agent_role=effective_role)

            if chat_id and task_data.get('human_validation'):
                from core.master_ai import MasterAI
                from core.human_in_the_loop import request_human_input
                master_ai = MasterAI()
                question, options = master_ai.format_validation_request(task_out, task_description=task_data.get('description', ''), expected_output=task_data.get('expected_output', ''))
                user_feedback = request_human_input(chat_id, question, options=options)
                
                if user_feedback and user_feedback != "SYSTEM_ABORT":
                    task_out = master_ai.process_validation_feedback(task_out, user_feedback)
                    memory_manager.write_record(key=key_name, content_summary=f"Output of dynamic task {task_idx + 1} by agent '{effective_role} (Human Edited)': {task_out[:500]}", structured_data={"raw_output": task_out}, agent_role=f"{effective_role} (Human Edited)")
            
            if progress_callback:
                try:
                    progress_callback(task_idx, len(plan['tasks']), effective_role, "completed")
                except Exception:
                    pass
            
            with task_outputs_lock:
                task_outputs[str(task_idx)] = task_out
                if run_id:
                    db.update_run(run_id, status='running', result=task_out, current_task_idx=task_idx + 1, task_outputs=task_outputs)

            node_outputs[n_id] = task_out
            return task_out
        finally:
            with vram_condition:
                current_vram_usage -= cost
                vram_condition.notify_all()

    last_output_overall = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        
        while ready_queue or in_flight:
            for n_id in ready_queue:
                futures[executor.submit(execute_dynamic_node, n_id)] = n_id
                in_flight.add(n_id)
            ready_queue.clear()
            
            if not in_flight:
                break
                
            done, not_done = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            
            for f in done:
                n_id = futures.pop(f)
                in_flight.remove(n_id)
                
                try:
                    res = f.result()
                    if res:
                        last_output_overall = res
                except Exception as e:
                    logging.error(f"Node {n_id} failed: {e}")
                    raise
                    
                completed_nodes.add(n_id)
                for dep in dependents[n_id]:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        ready_queue.append(dep)

    all_records = memory_manager.dump_all_records()
    global_context = json.dumps(all_records, indent=2, ensure_ascii=False)

    return last_output_overall, global_context

"""

with open(file_path, "w", encoding="utf-8") as f:
    f.write(before + new_func + "\n\n" + after)
