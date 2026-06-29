import re

file_path = "c:/Users/pietr/OneDrive/Documenti/GitHub/Alfredo/core/crew_builder.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

before = content[:content.find("def execute_run_with_resume")]
after = content[content.find("def build_dynamic_crew"):]

new_func = """def execute_run_with_resume(run_id: int, status_callback=None, accumulated_context: str = None, chat_id: str = None) -> str:
    \"\"\"
    Executes a workflow run task-by-task with MEMORY-CENTRIC communication.
    Now using a DAG Scheduler with VRAM-awareness.
    \"\"\"
    import concurrent.futures
    import threading
    import os
    import json
    import re
    from dotenv import dotenv_values, find_dotenv

    if chat_id:
        ABORT_FLAGS[str(chat_id)] = False

    run = db.read_run(run_id)
    if not run:
        raise ValueError(f"Run ID {run_id} not found.")

    workflow_id = run['workflow_id']
    workflow_record = db.read_workflow(workflow_id)
    if not workflow_record:
        raise ValueError(f"Workflow ID {workflow_id} not found.")

    task_ids = workflow_record.get('task_ids')
    if task_ids is None:
        if 'task_ids_json' in workflow_record:
            task_ids = json.loads(workflow_record['task_ids_json'])
        else:
            task_ids = []

    if not task_ids:
        raise ValueError(f"Workflow ID {workflow_id} has no tasks defined.")

    inputs = {}
    if run.get('inputs'):
        try:
            inputs = json.loads(run['inputs'])
        except Exception:
            pass

    task_outputs = {}
    if run.get('task_outputs'):
        try:
            task_outputs = json.loads(run['task_outputs'])
        except Exception:
            pass

    start_idx = run.get('current_task_idx', 0)
    db.update_run(run_id, status='running', current_task_idx=start_idx, task_outputs=task_outputs)

    # --- DAG NORMALIZATION ---
    dag_nodes = {}
    for i, step_def in enumerate(task_ids):
        node_id = f"node_{i}"
        depends_on = []
        is_batch = False
        task_id = None
        batch_tasks = []
        batch_size = 5
        source_variable = ""
        
        if isinstance(step_def, int):
            task_id = step_def
            if i > 0: depends_on = [f"node_{i-1}"]
        elif isinstance(step_def, dict):
            node_id = step_def.get("id", f"node_{i}")
            depends_on = step_def.get("depends_on", [])
            if "id" not in step_def: step_def["id"] = node_id
            
            if step_def.get("type") == "batch_loop":
                is_batch = True
                batch_tasks = step_def.get("task_ids", [])
                batch_size = step_def.get("batch_size", 5)
                source_variable = step_def.get("source_variable", "")
            else:
                task_id = step_def.get("task_id")
                
        dag_nodes[node_id] = {
            "step_def": step_def,
            "task_id": task_id,
            "is_batch": is_batch,
            "batch_tasks": batch_tasks,
            "batch_size": batch_size,
            "source_variable": source_variable,
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

    # Memory init
    memory_manager = EphemeralMemoryManager(run_id=run_id)
    if accumulated_context:
        memory_manager.load_from_dump(accumulated_context)
    read_memory_tool = ReadAtomicMemoryTool(memory_manager=memory_manager)
    write_memory_tool = WriteAtomicMemoryTool(memory_manager=memory_manager)

    completed_nodes = set()
    node_outputs = {}
    for n_id, data in dag_nodes.items():
        if data["is_batch"]:
            last_inner = data["batch_tasks"][-1] if data["batch_tasks"] else None
            if last_inner and str(last_inner) in task_outputs:
                completed_nodes.add(n_id)
                node_outputs[n_id] = task_outputs[str(last_inner)]
        else:
            if str(data["task_id"]) in task_outputs:
                completed_nodes.add(n_id)
                node_outputs[n_id] = task_outputs[str(data["task_id"])]

    for n_id in completed_nodes:
        data = dag_nodes[n_id]
        if data["is_batch"]:
            for b_tid in data["batch_tasks"]:
                if str(b_tid) in task_outputs:
                    _auto_save_to_memory(memory_manager, b_tid, task_outputs[str(b_tid)], "Unknown")
        else:
            tid = data["task_id"]
            if str(tid) in task_outputs:
                _auto_save_to_memory(memory_manager, tid, task_outputs[str(tid)], "Unknown")
                
        for dep in dependents[n_id]:
            in_degree[dep] -= 1

    agents_cache = {}
    task_outputs_lock = threading.Lock()
    
    def _execute_task_instance(task_id, current_inputs, log_msg, task_idx=None, parent_output=""):
        task_obj = _build_task(task_id, agents_cache)
        _inject_memory_tools(task_obj.agent, task_obj, read_memory_tool, write_memory_tool)

        def normalize_name(s: str) -> str:
            if not s: return ""
            s_norm = s.lower().replace('_', ' ').replace('-', ' ')
            return " ".join(s_norm.split())

        workflow_tasks = []
        for step in task_ids:
            tids = step.get('task_ids', []) if isinstance(step, dict) and step.get('type') == 'batch_loop' else [step if isinstance(step, int) else step.get('task_id')]
            for tid in tids:
                try:
                    t_rec = db.read_task(tid)
                    if t_rec: workflow_tasks.append(t_rec)
                    else: workflow_tasks.append({'id': tid, 'name': None, 'description': '', 'agent_id': None})
                except Exception:
                    workflow_tasks.append({'id': tid, 'name': None, 'description': '', 'agent_id': None})

        lookup = {}
        with task_outputs_lock:
            for t_rec in workflow_tasks:
                tid = t_rec['id']
                t_name = t_rec.get('name')
                t_out = task_outputs.get(str(tid), "")
                if t_out:
                    if t_name:
                        norm = normalize_name(t_name)
                        if norm: lookup[norm] = t_out
                    lookup[f"task {tid}"] = t_out
                    lookup[f"task_{tid}"] = t_out
                    lookup[str(tid)] = t_out

        lookup["previous task"] = parent_output
        lookup["previous_task"] = parent_output
        lookup["previous"] = parent_output
        lookup["task precedente"] = parent_output
        lookup["task_precedente"] = parent_output

        pattern = re.compile(r'\{task:([^\}]+)\}|\{([^\}]+)\}')
        def repl(match):
            g1 = match.group(1)
            g2 = match.group(2)
            key = g1 if g1 is not None else g2
            if not key: return match.group(0)
            norm_key = normalize_name(key)
            if norm_key in lookup: return lookup[norm_key]
            lower_key = key.strip().lower()
            if lower_key in lookup: return lookup[lower_key]
            return match.group(0)

        def apply_interpolation(text: str) -> str:
            if not text: return text
            for k, v in current_inputs.items():
                text = text.replace(f"{{{k}}}", str(v))
            text = text.replace("{user_input}", current_inputs.get('user_input', ''))
            text = text.replace("{previous_result}", parent_output)
            text = text.replace("{context}", parent_output)
            text = text.replace("{flexible_input}", current_inputs.get('user_input', parent_output))
            text = pattern.sub(repl, text)
            text = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', text)
            return text

        original_desc = apply_interpolation(task_obj.description)
        original_expected = apply_interpolation(task_obj.expected_output)

        index_table = memory_manager.get_memory_index_table()
        if "EPHEMERAL WORKSPACE MEMORY INDEX" not in index_table:
            context_str = (
                "\\n\\n--- [EPHEMERAL WORKSPACE MEMORY INDEX] ---\\n"
                "Results from previous steps are stored in the ephemeral in-memory database.\\n"
                "Use the 'read_atomic_memory' tool with the exact 'key' column value to retrieve data.\\n"
                "Use 'write_atomic_memory' to store YOUR output for downstream agents.\\n\\n"
                f"{index_table}\\n"
                "--- [END MEMORY INDEX] ---\\n"
            )
            original_desc += context_str

        task_obj.description = original_desc
        task_obj.expected_output = original_expected

        if status_callback and task_idx is not None:
            try:
                agent_role = task_obj.agent.role if task_obj.agent else "Unknown"
                status_callback(task_idx, len(task_ids), agent_role, "running")
            except Exception:
                pass

        single_task_crew = Crew(agents=[task_obj.agent], tasks=[task_obj], verbose=True, process='sequential')

        try:
            result = single_task_crew.kickoff()
        except Exception as e:
            err_str = str(e).lower()
            if "503" in err_str or "unavailable" in err_str or "rate" in err_str:
                logging.warning(f"Rate limit / 503 encountered: {e}")
                raise RateLimitError(f"Model high demand error (503): {e}", 0, task_outputs)
            elif "none or empty" in err_str:
                raise RuntimeError(f"Agent failed to generate a valid output: {e}")
            else:
                raise e
        
        task_out = str(result)
        agent_role = task_obj.agent.role if task_obj.agent else "Unknown"
        _auto_save_to_memory(memory_manager, task_id, task_out, agent_role)

        t_rec = db.read_task(task_id)
        if t_rec and chat_id and t_rec.get('human_validation'):
            from core.master_ai import MasterAI
            from core.human_in_the_loop import request_human_input
            master_ai = MasterAI()
            question, options = master_ai.format_validation_request(
                task_out, 
                task_description=t_rec.get('description', ''), 
                expected_output=t_rec.get('expected_output', '')
            )
            user_feedback = request_human_input(chat_id, question, options=options)
            if user_feedback and user_feedback != "SYSTEM_ABORT":
                task_out = master_ai.process_validation_feedback(task_out, user_feedback)
                _auto_save_to_memory(memory_manager, task_id, task_out, f"{agent_role} (Human Edited)")

        with task_outputs_lock:
            task_outputs[str(task_id)] = task_out
            db.update_run(run_id, status='running', result=task_out, current_task_idx=task_idx + 1 if task_idx is not None else 0, task_outputs=task_outputs)
            
        return task_out

    # --- VRAM Management ---
    env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
    env_vars = dotenv_values(env_path)
    try:
        MAX_VRAM_GB = float(env_vars.get("MAX_VRAM_GB", 24.0))
    except Exception:
        MAX_VRAM_GB = 24.0

    current_vram_usage = 0.0
    vram_lock = threading.Lock()
    vram_condition = threading.Condition(vram_lock)
    
    def get_task_vram_cost(t_id):
        try:
            t = db.read_task(t_id)
            if not t: return 0.0
            m_id = t.get('model_id')
            if not m_id:
                a = db.read_agent(t.get('agent_id'))
                if a: m_id = a.get('model_id')
            if m_id:
                m = db.read_model(m_id)
                if m: return float(m.get('vram_gb') or 0.0)
        except Exception:
            pass
        return 0.0

    ready_queue = [n for n, deg in in_degree.items() if deg == 0 and n not in completed_nodes]
    in_flight = set()
    
    def execute_node(n_id):
        nonlocal current_vram_usage
        data = dag_nodes[n_id]
        
        cost = 0.0
        if data["is_batch"]:
            cost = max([get_task_vram_cost(tid) for tid in data["batch_tasks"]], default=0.0)
        else:
            cost = get_task_vram_cost(data["task_id"])
            
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
                
            if not data["is_batch"]:
                log_msg = f"Executing Node {n_id} (Task {data['task_id']})..."
                out = _execute_task_instance(data["task_id"], inputs, log_msg, task_idx=data["original_index"], parent_output=parent_output)
                node_outputs[n_id] = out
                return out
            else:
                batch_tasks = data["batch_tasks"]
                batch_size = int(data.get('batch_size', 5))
                
                data_str = parent_output
                if data["source_variable"]:
                    # placeholder logic if source is a variable
                    pass
                
                try:
                    json_match = re.search(r'\[.*\]', data_str, re.DOTALL)
                    if json_match:
                        items = json.loads(json_match.group(0))
                    else:
                        items = json.loads(data_str)
                    if not isinstance(items, list):
                        raise ValueError("Extracted data is not a JSON array")
                except Exception as e:
                    logging.error(f"Failed to parse source data for batch loop: {e}")
                    items = [{"raw_data": data_str}]
                
                batch_out = ""
                for batch_start in range(0, len(items), batch_size):
                    batch_chunk = items[batch_start:batch_start+batch_size]
                    chunk_str = json.dumps(batch_chunk)
                    
                    batch_inputs = inputs.copy()
                    batch_inputs['current_batch'] = chunk_str
                    
                    memory_manager.clear_memory()
                    
                    for b_idx, inner_task_id in enumerate(batch_tasks):
                        log_msg = f"Executing Batch Loop (Node {n_id}) - Chunk {batch_start//batch_size + 1} - Inner Task {b_idx+1}/{len(batch_tasks)} (ID: {inner_task_id})"
                        batch_out = _execute_task_instance(inner_task_id, batch_inputs, log_msg, task_idx=data["original_index"], parent_output=batch_out)
                
                node_outputs[n_id] = batch_out
                return batch_out
        finally:
            with vram_condition:
                current_vram_usage -= cost
                vram_condition.notify_all()

    last_output_overall = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        
        while ready_queue or in_flight:
            for n_id in ready_queue:
                futures[executor.submit(execute_node, n_id)] = n_id
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
    f.write(before + new_func + after)
