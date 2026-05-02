# ui/dashboard.py

import streamlit as st
from core.db_manager import DBManager
from core.data_manager import DataManager
import yaml
import json
import re
from datetime import datetime
import os

@st.cache_resource
def get_db_manager():
    return DBManager()

# --- ARCHITECTURAL MANDATE M1_T3-A1 & M1_T3-A3: Sanitization Utilities ---

def sanitize_input(text: str) -> str:
    """
    Sanitizes user input to prevent injection attacks.
    Strips shell metacharacters, basic SQL special characters, and script tags.
    This is a critical security measure.
    """
    if not isinstance(text, str):
        return ""
    # Remove script tags
    text = re.sub(r'<\s*script[^>]*>.*?<\s*/\s*script\s*>', '', text, flags=re.IGNORECASE | re.DOTALL)
    # Remove common shell metacharacters and SQL injection attempts
    dangerous_chars = r'[;&|`\'"]'
    text = re.sub(dangerous_chars, '', text)
    # Basic attempt to neutralize SQL comments
    text = text.replace('--', '')
    return text.strip()

def sanitize_filename(name: str) -> str:
    """
    Creates a safe filename from a user-provided string.
    Converts to lowercase, replaces spaces with underscores, and removes all
    non-alphanumeric characters except for underscores and hyphens.
    This prevents Path Traversal vulnerabilities.
    """
    if not isinstance(name, str):
        return "invalid_name"
    name = name.lower().replace(' ', '_')
    name = re.sub(r'[^a-z0-9_-]', '', name)
    return name or "unnamed_workflow"

# --- Helper Functions for Callbacks ---

def set_editing_state(key, value):
    """Generic callback to set a value in session state."""
    st.session_state[key] = value

def clear_editing_state(key):
    """Generic callback to clear a key from session state."""
    if key in st.session_state:
        del st.session_state[key]
    st.rerun()

# --- UI Rendering Functions for Each Tab ---

def render_api_vault():
    """Renders Tab 1: API Vault & Model Registry (as per M1_T1)."""
    db = get_db_manager()
    st.header("API Vault & Model Registry")
    
    from dotenv import dotenv_values, set_key, find_dotenv
    import os

    env_path = find_dotenv()
    if not env_path:
        env_path = os.path.join(os.getcwd(), '.env')
        open(env_path, 'a').close()

    current_env = dotenv_values(env_path)
    
    suggested_keys = [
        "OPENAI_API_KEY", 
        "GROQ_API_KEY", 
        "ANTHROPIC_API_KEY", 
        "GEMINI_API_KEY"
    ]
    
    # Combine suggested keys and any other keys already in .env
    all_keys = sorted(list(set(suggested_keys + list(current_env.keys()))))
    
    st.subheader("API Keys")
    st.markdown("Monitor and manage API keys saved securely in your `.env` file.")
    
    # Display current status in columns
    col1, col2 = st.columns(2)
    for i, key in enumerate(all_keys):
        is_set = key in current_env and bool(current_env[key].strip())
        status_icon = "🟢" if is_set else "🔴"
        with (col1 if i % 2 == 0 else col2):
            st.markdown(f"{status_icon} **{key}**")
            
    with st.form("api_key_form"):
        st.markdown("Add or Update an API Key")
        form_col1, form_col2 = st.columns(2)
        with form_col1:
            selected_key = st.selectbox("Select Key", options=["Custom..."] + suggested_keys)
            custom_key = st.text_input("Custom Key Name (if selected)", placeholder="e.g. MIO_MODELLO_KEY")
        with form_col2:
            key_value = st.text_input("API Key Value", type="password", placeholder="Enter key here...")
            
        submitted_key = st.form_submit_button("Save to .env")
        if submitted_key:
            final_key_name = custom_key.strip() if selected_key == "Custom..." else selected_key
            if final_key_name and key_value:
                # Sanitize key name (uppercase, no spaces)
                final_key_name = final_key_name.upper().replace(' ', '_')
                set_key(env_path, final_key_name, key_value.strip())
                st.success(f"Key '{final_key_name}' saved securely!")
                st.rerun()
            else:
                st.error("Please provide both a valid Key Name and a Value.")

    st.divider()

    st.subheader("Model Registry")
    
    # Load model config
    model_map_path = os.path.join(os.getcwd(), 'config', 'models_map.yaml')
    model_config = DataManager.load_yaml(model_map_path)
    PROVIDER_MAP = model_config.get('provider_map', {})
    LOCAL_MODELS = model_config.get('local_models', [])
    
    st.markdown("Add or update a model available for agents.")
    
    # --- NEW: Local vs Cloud Toggle ---
    is_local = st.toggle("Local Model (Ollama)", help="Enable if running model locally via Ollama")
    
    if is_local:
        provider = "Ollama"
        env_var_name = ""
        selected_local = st.selectbox("Local Model Name", options=LOCAL_MODELS + ["Other (Manual)..."])
        if selected_local == "Other (Manual)...":
            model_name = st.text_input("Type Custom Local Model Name", placeholder="e.g., my-custom-model")
        else:
            model_name = selected_local
    else:
        col1, col2, col3 = st.columns(3)
        with col1:
            # --- NEW: Dropdown of keys found in .env ---
            env_options = sorted([k for k, v in current_env.items() if v and str(v).strip()])
            if not env_options:
                env_options = ["(No keys found in .env)"]
            env_var_name = st.selectbox("Link to API Key", options=env_options, help="Select the variable from your .env that contains the API key for this model.")
        
        # Default values
        inferred_provider = "Other"
        model_options = ["Other (Manual)..."]
        
        # Lookup in dictionary
        if env_var_name in PROVIDER_MAP:
            inferred_provider = PROVIDER_MAP[env_var_name].get("provider", "Other")
            model_options = PROVIDER_MAP[env_var_name].get("models", []) + ["Other (Manual)..."]
        elif env_var_name != "(No keys found in .env)":
            # Fallback simple heuristic for unknown keys
            upper_key = env_var_name.upper()
            if "OPENAI" in upper_key: inferred_provider = "OpenAI"
            elif "GROQ" in upper_key: inferred_provider = "Groq"
            elif "ANTHROPIC" in upper_key: inferred_provider = "Anthropic"
            elif "GEMINI" in upper_key: inferred_provider = "Gemini"
            elif "COHERE" in upper_key: inferred_provider = "Cohere"
        
        with col2:
            provider = st.text_input("Provider", value=inferred_provider, disabled=(env_var_name in PROVIDER_MAP))
        with col3:
            selected_model = st.selectbox("Model Name", options=model_options)
            if selected_model == "Other (Manual)...":
                model_name = st.text_input("Type Custom Model Name", placeholder="e.g., gpt-4-turbo")
            else:
                model_name = selected_model

    submitted = st.button("Add Model", type="primary")
    if submitted and provider and model_name:
        if not is_local and env_var_name == "(No keys found in .env)":
            st.error("Please add an API Key in the Vault first.")
        else:
            db.create_model(provider, model_name, "" if is_local else env_var_name, is_local)
            st.success(f"Added {'local' if is_local else 'cloud'} model '{model_name}'.")
            st.rerun()

    models = db.read_all_models()
    if models:
        st.write("Registered Models:")
        for model in models:
            col1, col2, col3, col4, col5 = st.columns([0.5, 2, 2, 2, 1])
            type_icon = "🏠" if model.get('is_local') else "☁️"
            col1.markdown(type_icon)
            col2.text(f"P: {model['provider']}")
            col3.text(f"M: {model['model_name']}")
            key_display = "---" if model.get('is_local') else (model.get('env_var_name') or "N/A")
            col4.text(f"Key: {key_display}")
            if col5.button("Delete", key=f"del_model_{model['id']}", use_container_width=True):
                db.delete_model(model['id'])
                st.toast(f"Deleted model {model['model_name']}", icon="🗑️")
                st.rerun()
    else:
        st.info("No models registered yet.")


def render_agent_caserma():
    """Renders Tab 2: Agent Caserma (Barracks) (as per M1_T2)."""
    db = get_db_manager()
    st.header("Agent Caserma")
    st.markdown("Create, view, and manage your AI agents.")

    models = db.read_all_models()
    if not models:
        st.warning("No models found. Please register a model in 'Tab 1' first.")
        return

    model_options = {f"{m['provider']} / {m['model_name']}": m['id'] for m in models}
    
    agents = db.read_all_agents()
    
    # --- Edit/Create Form Logic ---
    editing_agent = None
    if 'editing_agent_id' in st.session_state and st.session_state.editing_agent_id:
        agent_id = st.session_state.editing_agent_id
        editing_agent = next((a for a in agents if a['id'] == agent_id), None)

    form_title = "Edit Agent" if editing_agent else "Create a New Agent"
    submit_label = "Update Agent" if editing_agent else "Add Agent"

    with st.form("agent_form"):
        st.subheader(form_title)
        
        default_name = editing_agent['name'] if editing_agent else ""
        default_role = editing_agent['role'] if editing_agent else ""
        
        # Logic to split Goal and Backstory if they follow the format
        raw_backstory = editing_agent['backstory'] if editing_agent else ""
        default_goal = ""
        default_backstory = raw_backstory
        
        if editing_agent and "Goal: " in raw_backstory and "\n\nBackstory: " in raw_backstory:
            try:
                parts = raw_backstory.split("\n\nBackstory: ")
                default_goal = parts[0].replace("Goal: ", "")
                default_backstory = parts[1]
            except Exception:
                pass # Fallback to showing everything in backstory field

        name = st.text_input("Name", value=default_name)
        role = st.text_input("Role", value=default_role)
        goal = st.text_area("Goal", value=default_goal)
        backstory = st.text_area("Backstory", value=default_backstory)
        
        # Select model
        default_model_id = editing_agent['model_id'] if editing_agent else None
        model_names = list(model_options.keys())
        model_ids = list(model_options.values())
        default_model_index = 0
        if default_model_id in model_ids:
            default_model_index = model_ids.index(default_model_id)
            
        selected_model_str = st.selectbox("Select Model", options=model_names, index=default_model_index)
        
        submitted = st.form_submit_button(submit_label)
        if submitted and name and role and goal and backstory and selected_model_str:
            # ARCHITECTURAL MANDATE M1_T3-A1: Sanitize all text inputs
            sane_name = sanitize_input(name)
            sane_role = sanitize_input(role)
            sane_goal = sanitize_input(goal)
            sane_backstory = sanitize_input(backstory)
            
            model_id = model_options[selected_model_str]
            combined_backstory = f"Goal: {sane_goal}\n\nBackstory: {sane_backstory}"
            
            if editing_agent:
                db.update_agent(editing_agent['id'], sane_name, sane_role, combined_backstory, model_id, [])
                st.success(f"Agent '{sane_name}' updated successfully!")
                clear_editing_state('editing_agent_id')
            else:
                db.create_agent(sane_name, sane_role, combined_backstory, model_id, [])
                st.success(f"Agent '{sane_name}' has been recruited!")
                st.rerun()

    if editing_agent:
        if st.button("Cancel Edit", key="cancel_agent_edit"):
            clear_editing_state('editing_agent_id')

    st.divider()
    st.subheader("Registered Agents")
    if agents:
        import urllib.parse
        cols_per_row = 4
        for i in range(0, len(agents), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(agents):
                    agent = agents[i + j]
                    with cols[j]:
                        with st.container(border=True):
                            import hashlib
                            # Combiniamo nome, ruolo e backstory per un seed univoco e coerente
                            raw_seed = f"{agent.get('name', '')}-{agent.get('role', '')}-{agent.get('backstory', '')[:50]}"
                            safe_seed = hashlib.md5(raw_seed.encode('utf-8')).hexdigest()
                            
                            # DiceBear 9.x avataaars - parametri professionali verificati via schema.json
                            avatar_params = "&".join([
                                f"seed={safe_seed}",
                                "style=circle",
                                "accessoriesProbability=0",
                                "facialHairProbability=5",
                                # Bocca: solo espressioni sorridenti/affabili
                                "mouth=smile", "mouth=twinkle", "mouth=default",
                                # Occhi: amichevoli
                                "eyes=default", "eyes=happy", "eyes=wink",
                                # Sopracciglia: naturali e rilassate
                                "eyebrows=defaultNatural", "eyebrows=flatNatural", "eyebrows=raisedExcitedNatural",
                                # Vestiario: solo business/professionale
                                "clothing=blazerAndShirt", "clothing=blazerAndSweater", "clothing=collarAndSweater", "clothing=shirtCrewNeck",
                                # Capelli: tagli tradizionali, no cappelli/turbanti
                                "top=shortFlat", "top=shortRound", "top=shortWaved", "top=bob", "top=straight01", "top=straight02", "top=theCaesar", "top=theCaesarAndSidePart",
                                # Sfondo chiaro professionale
                                "backgroundColor=b6e3f4,c0aede,d1d4f9,ffd5dc",
                            ])
                            avatar_url = f"https://api.dicebear.com/9.x/avataaars/svg?{avatar_params}"
                            
                            # st.image gestisce nativamente URL esterni (no blocchi CSP)
                            col_img = st.columns([1, 2, 1])[1]
                            with col_img:
                                st.image(avatar_url, width=100)
                            
                            st.markdown(f"<h4 style='text-align: center; margin-bottom: 0px; font-size: 18px;'>{agent['name']}</h4>", unsafe_allow_html=True)
                            st.markdown(f"<p style='text-align: center; color: gray; font-size: 14px; margin-top: 0px;'>{agent['role']}</p>", unsafe_allow_html=True)
                            
                            with st.expander("Details"):
                                st.markdown(f"**Backstory:** {agent['backstory']}")
                                model = next((m for m in models if m['id'] == agent['model_id']), None)
                                if model:
                                    st.markdown(f"**Model:** {model['provider']} / {model['model_name']}")
                            
                            col_mod, col_del = st.columns([1, 1])
                            with col_mod:
                                st.button("Edit", key=f"mod_agent_{agent['id']}", on_click=set_editing_state, args=('editing_agent_id', agent['id']), use_container_width=True)
                            with col_del:
                                if st.button("Del", key=f"del_agent_{agent['id']}", type="primary", use_container_width=True):
                                    db.delete_agent(agent['id'])
                                    st.toast(f"Discharged agent {agent['name']}", icon="🗑️")
                                    st.rerun()

def render_task_builder():
    """Renders Tab 3: UI for creating, viewing, updating, and deleting tasks."""
    db = get_db_manager()
    st.header("Task Builder")
    st.markdown("Define individual tasks and assign them to specific agents.")
    
    tools_map_path = os.path.join(os.getcwd(), 'config', 'tools_map.yaml')
    AVAILABLE_TOOLS = []
    try:
        tools_config = DataManager.load_yaml(tools_map_path)
        tools_registry = tools_config.get('tools_registry', {})
        AVAILABLE_TOOLS = list(tools_registry.keys())
    except Exception:
        pass

    agents = db.read_all_agents()
    tasks = db.read_all_tasks()

    if not agents:
        st.warning("No agents found. Please create an agent in 'Tab 2: Agent Caserma' first.")
        return

    agent_options = {agent['name']: agent['id'] for agent in agents}

    # --- Edit/Create Form ---
    editing_task = None
    if 'editing_task_id' in st.session_state and st.session_state.editing_task_id:
        task_id = st.session_state.editing_task_id
        editing_task = next((t for t in tasks if t['id'] == task_id), None)

    form_title = "Edit Task" if editing_task else "Create a New Task"
    submit_label = "Update Task" if editing_task else "Add Task"

    with st.form(key="task_form"):
        st.subheader(form_title)
        
        default_desc = editing_task['description'] if editing_task else ""
        default_output = editing_task['expected_output'] if editing_task else ""
        default_tools = editing_task.get('tools', []) if editing_task else []
        
        default_agent_id = editing_task['agent_id'] if editing_task else None
        agent_names = list(agent_options.keys())
        agent_ids = list(agent_options.values())
        default_index = agent_ids.index(default_agent_id) if default_agent_id in agent_ids else 0

        description = st.text_area("Task Description", value=default_desc, height=100)
        expected_output = st.text_area("Expected Output", value=default_output, height=150)
        selected_agent_name = st.selectbox("Assign to Agent", options=agent_names, index=default_index)
        selected_tools = st.multiselect("Assign Tools (Optional)", options=AVAILABLE_TOOLS, default=default_tools)
        
        submitted = st.form_submit_button(submit_label)
        if submitted:
            # ARCHITECTURAL MANDATE M1_T3-A1: Sanitize all text inputs
            sane_description = sanitize_input(description)
            sane_expected_output = sanitize_input(expected_output)

            if not sane_description or not sane_expected_output or not selected_agent_name:
                st.error("All fields are required.")
            else:
                agent_id = agent_options[selected_agent_name]
                if editing_task:
                    db.update_task(editing_task['id'], sane_description, sane_expected_output, agent_id, selected_tools)
                    st.success(f"Task updated successfully!")
                    clear_editing_state('editing_task_id') # This will trigger a rerun
                else:
                    db.create_task(sane_description, sane_expected_output, agent_id, selected_tools)
                    st.success(f"Task added successfully!")
                    st.rerun()

    if editing_task:
        if st.button("Cancel Edit"):
            clear_editing_state('editing_task_id')

    st.divider()
    st.subheader("Existing Tasks")
    if not tasks and not agents:
        st.info("No tasks or agents created yet. Use the forms above to add them.")
    else:
        # Create a mapping for easy lookup: {agent_id: [task_list]}
        tasks_by_agent = {agent['id']: [] for agent in agents}
        for task in tasks:
            a_id = task['agent_id']
            if a_id in tasks_by_agent:
                tasks_by_agent[a_id].append(task)
            else:
                # Handle tasks assigned to agents that might have been deleted (rare)
                if "unknown" not in tasks_by_agent:
                    tasks_by_agent["unknown"] = []
                tasks_by_agent["unknown"].append(task)

        # Iterate through agents to create grouped dropdowns (expanders)
        for agent in agents:
            a_id = agent['id']
            agent_tasks = tasks_by_agent.get(a_id, [])
            num_tasks = len(agent_tasks)
            
            # Requested format: Agente (numero task)
            with st.expander(f"👤 **{agent['name']}** ({num_tasks} tasks)"):
                if not agent_tasks:
                    st.info(f"No tasks currently assigned to {agent['name']}.")
                else:
                    for task in agent_tasks:
                        # Use a container with border for visual grouping of individual tasks
                        with st.container(border=True):
                            st.markdown(f"**Task ID:** `{task['id']}`")
                            st.markdown(f"**Description:** {task['description']}")
                            st.markdown("**Expected Output:**")
                            st.code(task['expected_output'], language=None)
                            if task.get('tools'):
                                st.markdown(f"**Assigned Tools:** `{', '.join(task['tools'])}`")
                            
                            col1, col2 = st.columns([1, 1])
                            with col1:
                                st.button("Edit", key=f"edit_{task['id']}", on_click=set_editing_state, args=('editing_task_id', task['id']), use_container_width=True)
                            with col2:
                                if st.button("Delete", key=f"delete_{task['id']}", type="primary", use_container_width=True):
                                    db.delete_task(task['id'])
                                    st.toast(f"Deleted task {task['id']}", icon="🗑️")
                                    st.rerun()

        # Handle tasks with no valid agent (orphaned tasks)
        if "unknown" in tasks_by_agent and tasks_by_agent["unknown"]:
            with st.expander(f"❓ **Orphaned Tasks** ({len(tasks_by_agent['unknown'])} tasks)"):
                st.warning("These tasks are assigned to an agent that no longer exists.")
                for task in tasks_by_agent["unknown"]:
                    with st.container(border=True):
                        st.markdown(f"**Description:** {task['description']}")
                        st.markdown("**Expected Output:**")
                        st.code(task['expected_output'], language=None)
                        
                        col1, col2 = st.columns([1, 1])
                        with col1:
                            st.button("Edit", key=f"edit_unk_{task['id']}", on_click=set_editing_state, args=('editing_task_id', task['id']), use_container_width=True)
                        with col2:
                            if st.button("Delete", key=f"delete_unk_{task['id']}", type="primary", use_container_width=True):
                                db.delete_task(task['id'])
                                st.toast(f"Deleted orphaned task {task['id']}", icon="🗑️")
                                st.rerun()

def render_workflow_assembler():
    """Renders Tab 4: UI for creating, viewing, and exporting workflows."""
    db = get_db_manager()
    st.header("Workflow Assembler")
    st.markdown("Combine individual tasks into a sequential workflow.")

    tasks = db.read_all_tasks()
    workflows = db.read_all_workflows()

    if not tasks:
        st.warning("No tasks found. Please create tasks in 'Tab 3: Task Builder' first.")
        return

    task_options = {task['description']: task['id'] for task in tasks}
    task_id_map = {task['id']: task for task in tasks}

    with st.form(key="workflow_form"):
        st.subheader("Create a New Workflow")
        workflow_name = st.text_input("Workflow Name")
        selected_task_descs = st.multiselect("Select and Order Tasks", options=list(task_options.keys()))
        requires_human_check = st.checkbox("Requires Human Check")

        submitted = st.form_submit_button("Add Workflow")
        if submitted:
            # ARCHITECTURAL MANDATE M1_T3-A1: Sanitize workflow name
            sane_workflow_name = sanitize_input(workflow_name)
            if not sane_workflow_name or not selected_task_descs:
                st.error("Workflow Name and at least one Task are required.")
            else:
                ordered_task_ids = [task_options[desc] for desc in selected_task_descs]
                db.create_workflow(sane_workflow_name, ordered_task_ids, requires_human_check)
                st.success(f"Workflow '{sane_workflow_name}' created successfully!")
                st.rerun()

    st.divider()
    st.subheader("Saved Workflows")
    if not workflows:
        st.info("No workflows created yet. Use the form above to add one.")
    else:
        agents = db.read_all_agents()
        agent_id_map = {agent['id']: agent for agent in agents}
        
        for workflow in workflows:
            with st.expander(f"**Workflow:** {workflow['name']}"):
                # The db_manager processes task_ids_json into task_ids list automatically
                task_ids = workflow.get('task_ids', [])
                st.markdown(f"**Requires Human Check:** {'Yes' if workflow['requires_human_check'] else 'No'}")
                st.markdown("**Task Sequence:**")
                for i, task_id in enumerate(task_ids):
                    task = task_id_map.get(task_id)
                    st.write(f"&nbsp;&nbsp;&nbsp;{i+1}. {task['description'] if task else f'Task ID {task_id} not found'}")
                
                # --- ARCHITECTURAL MANDATE M1_T3-A2 & M1_T3-A3: Secure Export Logic ---
                # 1. Construct the data structure for YAML in-memory
                export_data = {'workflow': {'name': workflow['name'], 'tasks': []}}
                for task_id in task_ids:
                    task = task_id_map.get(task_id)
                    if task:
                        agent = agent_id_map.get(task['agent_id'])
                        task_data = {
                            'description': task['description'],
                            'expected_output': task['expected_output'],
                            'agent': agent['name'] if agent else 'Unknown Agent'
                        }
                        export_data['workflow']['tasks'].append(task_data)
                
                # 2. Generate YAML string in-memory. NOTE: yaml.dump is safe for exporting.
                yaml_string = yaml.dump(export_data, sort_keys=False, indent=2)
                
                # 3. Sanitize filename and provide download button
                safe_filename = f"{sanitize_filename(workflow['name'])}.yaml"
                
                col1, col2 = st.columns([1, 2])
                with col1:
                    if st.button("Delete", key=f"del_wf_{workflow['id']}", type="primary", use_container_width=True):
                        db.delete_workflow(workflow['id'])
                        st.toast(f"Deleted workflow {workflow['name']}", icon="🗑️")
                        st.rerun()
                with col2:
                    st.download_button(
                        label="Export to YAML",
                        data=yaml_string.encode('utf-8'),
                        file_name=safe_filename,
                        mime="application/x-yaml",
                        key=f"export_wf_{workflow['id']}",
                        use_container_width=True
                    )




def main():
    """Main function to run the Streamlit dashboard."""
    st.set_page_config(page_title="AI Workflow Configurator", layout="wide")
    # --- Header with Right Popover ---
    col_title, col_tools = st.columns([7, 3])
    with col_title:
        st.title("🤖 AI Workflow Configurator")
        st.caption("A secure dashboard for building and managing AI agent workflows.")
    
    with col_tools:
        st.write("") # Spacer
        st.write("") # Spacer
        with st.popover("🛠️ Manage Tools Registry", use_container_width=True):
            st.markdown("### Tool Registry")
            st.markdown("Add new tools so they can be assigned to tasks.")
            
            tools_map_path = os.path.join(os.getcwd(), 'config', 'tools_map.yaml')
            
            with st.form("add_tool_form"):
                new_tool_id = st.text_input("Tool ID (Function Name)", placeholder="e.g. read_pdf")
                new_tool_name = st.text_input("Display Name", placeholder="e.g. PDF Reader")
                new_tool_desc = st.text_input("Description")
                new_tool_secrets = st.text_input("Required Secrets (comma separated)", placeholder="e.g. API_KEY, OTHER_KEY")
                
                if st.form_submit_button("Add Tool"):
                    if new_tool_id and new_tool_name:
                        try:
                            # Load existing
                            tools_config = DataManager.load_yaml(tools_map_path) if os.path.exists(tools_map_path) else {}
                            if 'tools_registry' not in tools_config:
                                tools_config['tools_registry'] = {}
                            
                            secrets_list = [s.strip() for s in new_tool_secrets.split(",")] if new_tool_secrets else []
                            
                            # Add new
                            tools_config['tools_registry'][new_tool_id] = {
                                'display_name': new_tool_name,
                                'description': new_tool_desc,
                                'required_secrets': secrets_list
                            }
                            
                            # Save
                            with open(tools_map_path, 'w', encoding='utf-8') as f:
                                yaml.dump(tools_config, f, sort_keys=False, indent=2)
                            
                            st.success(f"Tool {new_tool_name} added!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error saving tool: {e}")
                    else:
                        st.error("Tool ID and Display Name are required.")
            
            # Show existing
            try:
                tools_config = DataManager.load_yaml(tools_map_path)
                registry = tools_config.get('tools_registry', {})
                if registry:
                    st.divider()
                    st.markdown("**Registered Tools:**")
                    for t_id, t_data in registry.items():
                        secrets_str = ", ".join(t_data.get('required_secrets', [])) or "None"
                        st.caption(f"**{t_data.get('display_name')}** (`{t_id}`) | Secrets: {secrets_str}")
            except Exception:
                pass

    # --- Sidebar Guide ---
    with st.sidebar:
        st.header("🚀 Guide & Placeholders")
        st.markdown("""
        Use these placeholders in your **Task Descriptions** to create dynamic and sequential workflows:
        
        - **`{user_input}`**: 
          Inserts the text you send directly (e.g., via Telegram).
          
        - **`{previous_result}`**: 
          Inserts the output of the *last* executed workflow. Perfect for chaining.
          
        - **`{flexible_input}`**: 
          **Smart Fallback**: Uses your new input if provided, otherwise uses the previous result automatically.
          
        ---
        *Tip: You can combine them!*
        """)
        st.divider()
        st.info("The configuration is saved directly to your SQLite database.")

    # Initialize session state for editing
    if 'editing_task_id' not in st.session_state:
        st.session_state.editing_task_id = None
    if 'editing_agent_id' not in st.session_state:
        st.session_state.editing_agent_id = None

    tab1, tab2, tab3, tab4 = st.tabs([
        "Tab 1: API Vault & Model Registry",
        "Tab 2: Agent Caserma",
        "Tab 3: Task Builder",
        "Tab 4: Workflow Assembler"
    ])

    with tab1:
        render_api_vault()

    with tab2:
        render_agent_caserma()
    
    with tab3:
        render_task_builder()

    with tab4:
        render_workflow_assembler()


if __name__ == "__main__":
    main()