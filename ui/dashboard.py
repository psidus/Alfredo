# ui/dashboard.py

import streamlit as st
from core.db_manager import DBManager
from core.data_manager import DataManager
import yaml
import json
import re
from datetime import datetime
import os
import hashlib
from dotenv import dotenv_values, set_key, find_dotenv
from core.master_ai import MasterAI
from core.crew_builder import build_crew
from core.notification_manager import NotificationManager

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

def get_agent_avatar_url(agent):
    """Generates a consistent avatar URL for an agent based on their metadata."""
    # Combine name, role and backstory for a unique and consistent seed
    raw_seed = f"{agent.get('name', '')}-{agent.get('role', '')}-{agent.get('backstory', '')[:50]}"
    safe_seed = hashlib.md5(raw_seed.encode('utf-8')).hexdigest()
    
    # DiceBear 9.x avataaars - professional parameters
    avatar_params = "&".join([
        f"seed={safe_seed}",
        "style=circle",
        "accessoriesProbability=0",
        "facialHairProbability=5",
        # Mouth: friendly expressions
        "mouth=smile", "mouth=twinkle", "mouth=default",
        # Eyes: friendly
        "eyes=default", "eyes=happy", "eyes=wink",
        # Eyebrows: natural
        "eyebrows=defaultNatural", "eyebrows=flatNatural", "eyebrows=raisedExcitedNatural",
        # Clothing: professional/business
        "clothing=blazerAndShirt", "clothing=blazerAndSweater", "clothing=collarAndSweater", "clothing=shirtCrewNeck",
        # Hair: traditional cuts
        "top=shortFlat", "top=shortRound", "top=shortWaved", "top=bob", "top=straight01", "top=straight02", "top=theCaesar", "top=theCaesarAndSidePart",
        # Professional background colors
        "backgroundColor=b6e3f4,c0aede,d1d4f9,ffd5dc",
    ])
    return f"https://api.dicebear.com/9.x/avataaars/svg?{avatar_params}"

# --- UI Rendering Functions for Each Tab ---

def render_api_vault():
    """Renders Tab 1: API Vault & Model Registry (as per M1_T1)."""
    db = get_db_manager()
    st.header("API Vault & Model Registry")
    
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
                            avatar_url = get_agent_avatar_url(agent)
                            
                            # st.image handles external URLs
                            
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

    agent_options = {f"{agent['name']} ({agent['role']})": agent['id'] for agent in agents}

    # --- Edit/Create Form ---
    editing_task = None
    if 'editing_task_id' in st.session_state and st.session_state.editing_task_id:
        task_id = st.session_state.editing_task_id
        editing_task = next((t for t in tasks if t['id'] == task_id), None)

    form_title = "Edit Task" if editing_task else "Create a New Task"
    submit_label = "Update Task" if editing_task else "Add Task"

    # --- Dynamic Inputs Logic ---
    if 'temp_required_inputs' not in st.session_state:
        if editing_task:
            st.session_state.temp_required_inputs = editing_task.get('required_inputs', [])
        else:
            st.session_state.temp_required_inputs = [{"key": "", "prompt": ""}]

    def add_row():
        st.session_state.temp_required_inputs.append({"key": "", "prompt": ""})

    def remove_specific_row(index):
        if 0 <= index < len(st.session_state.temp_required_inputs):
            st.session_state.temp_required_inputs.pop(index)

    with st.container(border=True):
        st.subheader(form_title)
        
        default_desc = editing_task['description'] if editing_task else ""
        default_output = editing_task['expected_output'] if editing_task else ""
        default_tools = editing_task.get('tools', []) if editing_task else []
        
        default_agent_id = editing_task['agent_id'] if editing_task else None
        agent_names = list(agent_options.keys())
        agent_ids = list(agent_options.values())
        default_index = agent_ids.index(default_agent_id) if default_agent_id in agent_ids else 0

        description = st.text_area("Task Description", value=default_desc, height=100, key="task_desc_area",
                                    help="Use `{variable_name}` to insert dynamic values from Required Inputs. E.g. `Crea un logo con sfumature {colore}`")
        expected_output = st.text_area("Expected Output", value=default_output, height=150, key="task_output_area",
                                       help="You can also use `{variable_name}` placeholders here.")
        
        # --- Agent Selection with Avatar Preview ---
        # We need to peek at the session state to know which agent is selected for the preview
        current_sel_name = st.session_state.task_agent_sel if "task_agent_sel" in st.session_state else agent_names[default_index]
        current_agent = next((a for a in agents if f"{a['name']} ({a['role']})" == current_sel_name), None)
        
        col_av, col_sel = st.columns([1.5, 8.5])
        with col_av:
            if current_agent:
                # Alignment for larger avatar
                st.markdown("<div style='margin-top: 20px;'></div>", unsafe_allow_html=True)
                # Small spacer to push it slightly to the right
                _, avatar_col = st.columns([1, 5])
                with avatar_col:
                    st.image(get_agent_avatar_url(current_agent), width=80)
        with col_sel:
            selected_agent_name = st.selectbox("Assign to Agent", options=agent_names, index=default_index, key="task_agent_sel")
        
        st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)
        selected_tools = st.multiselect("Assign Tools (Optional)", options=AVAILABLE_TOOLS, default=default_tools, key="task_tools_sel")
        
        # --- Guided Required Inputs ---
        st.markdown("---")
        st.markdown("**🔑 Required Inputs** — *Variables asked in chat before execution*")
        st.caption("The answer will replace `{variable_name}` in the prompts.")
        
        # Table Header
        if st.session_state.temp_required_inputs:
            col_h1, col_h2, col_h3 = st.columns([1, 2, 0.4])
            col_h1.markdown("**Variable Name**")
            col_h2.markdown("**Chat Prompt**")
            col_h3.markdown("") # Empty for delete icon
        
        # Dynamic Rows
        input_rows = []
        for idx, item in enumerate(st.session_state.temp_required_inputs):
            col_var, col_prompt, col_del = st.columns([1, 2, 0.4])
            with col_var:
                v_key = st.text_input(f"Var {idx}", value=item['key'], key=f"ri_key_{idx}", placeholder="e.g. colors", label_visibility="collapsed")
            with col_prompt:
                v_prompt = st.text_input(f"Prompt {idx}", value=item['prompt'], key=f"ri_prompt_{idx}", placeholder="e.g. What color do you want?", label_visibility="collapsed")
            with col_del:
                st.button("🗑️", key=f"del_ri_{idx}", on_click=remove_specific_row, args=(idx,), help="Remove this variable")
            
            # Update state immediately so it's not lost on rerun
            st.session_state.temp_required_inputs[idx] = {"key": v_key, "prompt": v_prompt}
            if v_key.strip() and v_prompt.strip():
                input_rows.append({"key": v_key.strip(), "prompt": v_prompt.strip()})

        col_btn1, _ = st.columns([1, 3])
        with col_btn1:
            st.button("➕ Add Variable", on_click=add_row, use_container_width=True)

        st.markdown("---")
        if st.button(submit_label, type="primary", use_container_width=True):
            # ARCHITECTURAL MANDATE M1_T3-A1: Sanitize all text inputs
            sane_description = sanitize_input(description)
            sane_expected_output = sanitize_input(expected_output)

            if not sane_description or not sane_expected_output or not selected_agent_name:
                st.error("All fields are required.")
            else:
                agent_id = agent_options[selected_agent_name]
                if editing_task:
                    db.update_task(editing_task['id'], sane_description, sane_expected_output, agent_id, selected_tools, input_rows)
                    st.success(f"Task updated successfully!")
                    if 'temp_required_inputs' in st.session_state: del st.session_state.temp_required_inputs
                    clear_editing_state('editing_task_id')
                else:
                    db.create_task(sane_description, sane_expected_output, agent_id, selected_tools, input_rows)
                    st.success(f"Task added successfully!")
                    if 'temp_required_inputs' in st.session_state: del st.session_state.temp_required_inputs
                    st.rerun()

    if editing_task:
        if st.button("Cancel Edit", key="cancel_task_edit", use_container_width=True):
            if 'temp_required_inputs' in st.session_state:
                del st.session_state.temp_required_inputs
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
            
            # Use avatar and columns for a premium look
            avatar_url = get_agent_avatar_url(agent)
            col_avatar, col_expander = st.columns([1, 18])
            with col_avatar:
                st.image(avatar_url, use_container_width=True)
            with col_expander:
                with st.expander(f"**{agent['name']}** ({num_tasks} tasks)"):
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
                                
                                if task.get('required_inputs'):
                                    st.markdown("**🔑 Required Inputs:**")
                                    for ri in task['required_inputs']:
                                        st.markdown(f"- `{{{ri['key']}}}` → *{ri['prompt']}*")
                                
                                col1, col2 = st.columns([1, 1])
                                with col1:
                                    st.button("Edit", key=f"edit_{task['id']}", on_click=set_editing_state, args=('editing_task_id', task['id']), use_container_width=True)
                                with col2:
                                    if st.button("Delete", key=f"delete_{task['id']}", type="primary", use_container_width=True):
                                        db.delete_task(task['id'])
                                        if 'wf_selected_task_ids' in st.session_state:
                                            del st.session_state.wf_selected_task_ids
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
                                if 'wf_selected_task_ids' in st.session_state:
                                    del st.session_state.wf_selected_task_ids
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

    task_id_map = {task['id']: task for task in tasks}
    agents = db.read_all_agents()
    agent_id_map = {agent['id']: agent for agent in agents}
    
    # Build tasks grouped by agent
    tasks_by_agent = {agent['id']: [] for agent in agents}
    for task in tasks:
        a_id = task.get('agent_id')
        if a_id in tasks_by_agent:
            tasks_by_agent[a_id].append(task)

    # --- Session state for selected tasks order ---
    if 'wf_selected_task_ids' not in st.session_state:
        st.session_state.wf_selected_task_ids = []

    def remove_wf_task(task_id):
        if task_id in st.session_state.wf_selected_task_ids:
            st.session_state.wf_selected_task_ids.remove(task_id)

    def move_wf_task_up(idx):
        lst = st.session_state.wf_selected_task_ids
        if idx > 0:
            lst[idx], lst[idx - 1] = lst[idx - 1], lst[idx]

    def move_wf_task_down(idx):
        lst = st.session_state.wf_selected_task_ids
        if idx < len(lst) - 1:
            lst[idx], lst[idx + 1] = lst[idx + 1], lst[idx]

    with st.container(border=True):
        st.subheader("Create a New Workflow")
        workflow_name = st.text_input("Workflow Name", key="wf_name_input")
        requires_human_check = st.checkbox("Requires Human Check", key="wf_human_check")
        
        st.markdown("---")
        st.markdown("**📋 Select Tasks — Grouped by Agent**")
        
        if not agents:
            st.info("No agents available.")
        else:
            # Determine currently selected agent for the UI
            agent_options = [f"{a['name']} - {a['role']}" for a in agents]
            current_agent = agents[0]
            if "wf_agent_selectbox" in st.session_state:
                sel_val = st.session_state.wf_agent_selectbox
                current_agent = next((a for a in agents if f"{a['name']} - {a['role']}" == sel_val), agents[0])
            
            col_agent, col_tasks = st.columns([1, 2])
            
            with col_agent:
                # Avatar above
                avatar_url = get_agent_avatar_url(current_agent)
                st.markdown("<div style='display: flex; justify-content: center; margin-bottom: 10px;'>", unsafe_allow_html=True)
                st.image(avatar_url, width=100)
                st.markdown("</div>", unsafe_allow_html=True)
                
                # Dropdown below
                st.selectbox("Select Agent", options=agent_options, key="wf_agent_selectbox")
            
            with col_tasks:
                st.markdown(f"**Tasks for {current_agent['name']}**")
                agent_tasks = tasks_by_agent.get(current_agent['id'], [])
                if not agent_tasks:
                    st.info("No tasks assigned to this agent.")
                else:
                    # Scrollable container for tasks
                    with st.container(height=300, border=True):
                        for task in agent_tasks:
                            is_selected = task['id'] in st.session_state.wf_selected_task_ids
                            label = f"**Task #{task['id']}:** {task['description'][:80]}{'...' if len(task['description']) > 80 else ''}"
                            if st.checkbox(label, value=is_selected, key=f"wf_check_{task['id']}"):
                                if task['id'] not in st.session_state.wf_selected_task_ids:
                                    st.session_state.wf_selected_task_ids.append(task['id'])
                            else:
                                if task['id'] in st.session_state.wf_selected_task_ids:
                                    st.session_state.wf_selected_task_ids.remove(task['id'])
        
        # --- Ordered Task Preview ---
        st.markdown("---")
        st.markdown("**⚙️ Workflow Steps (in order)**")
        
        if not st.session_state.wf_selected_task_ids:
            st.info("No tasks selected yet. Expand an agent above to add tasks.")
        else:
            for i, t_id in enumerate(st.session_state.wf_selected_task_ids):
                task = task_id_map.get(int(t_id))
                agent = agent_id_map.get(task['agent_id']) if task else None
                
                with st.container(border=True):
                    col_num, col_av, col_txt, col_up, col_down, col_rem = st.columns([0.5, 1, 8, 0.7, 0.7, 0.7])
                    col_num.markdown(f"**{i+1}**")
                    if agent:
                        col_av.image(get_agent_avatar_url(agent), width=36)
                    if task:
                        col_txt.markdown(f"**{task['description'][:70]}{'...' if len(task['description']) > 70 else ''}**")
                    col_up.button("🔼", key=f"wf_up_{i}", on_click=move_wf_task_up, args=(i,), help="Move up")
                    col_down.button("🔽", key=f"wf_down_{i}", on_click=move_wf_task_down, args=(i,), help="Move down")
                    col_rem.button("✖️", key=f"wf_rem_{i}", on_click=remove_wf_task, args=(t_id,), help="Remove")
        
        st.markdown("---")
        if st.button("💾 Save Workflow", type="primary", use_container_width=True):
            sane_workflow_name = sanitize_input(workflow_name)
            if not sane_workflow_name or not st.session_state.wf_selected_task_ids:
                st.error("Workflow Name and at least one Task are required.")
            else:
                db.create_workflow(sane_workflow_name, st.session_state.wf_selected_task_ids, requires_human_check)
                st.success(f"Workflow '{sane_workflow_name}' created successfully!")
                del st.session_state.wf_selected_task_ids
                st.rerun()

    st.divider()
    st.subheader("Saved Workflows")
    if not workflows:
        st.info("No workflows created yet. Use the form above to add one.")
    else:
        agents = db.read_all_agents()
        agent_id_map = {agent['id']: agent for agent in agents}
        
        for workflow in workflows:
            wf_label = f"**Workflow:** {workflow['name']}"
            if workflow.get('has_deletion_warning'):
                wf_label += " ⚠️ **(one or more task deleted)**"
            
            with st.expander(wf_label):
                if workflow.get('has_deletion_warning'):
                    st.warning("One or more tasks previously associated with this workflow have been deleted. The workflow has been updated to remove them.")
                    if st.button("Dismiss Warning", key=f"dismiss_wf_warn_{workflow['id']}", use_container_width=True):
                        db.dismiss_workflow_warning(workflow['id'])
                        st.rerun()
                
                # The db_manager processes task_ids_json into task_ids list automatically
                task_ids = workflow.get('task_ids', [])
                st.markdown(f"**Requires Human Check:** {'Yes' if workflow['requires_human_check'] else 'No'}")
                for i, task_id in enumerate(task_ids):
                    task = task_id_map.get(int(task_id))
                    if task:
                        agent = agent_id_map.get(task['agent_id'])
                        
                        col_avatar, col_text = st.columns([1, 25])
                        with col_avatar:
                            if agent:
                                avatar_url = get_agent_avatar_url(agent)
                                st.image(avatar_url, use_container_width=True)
                            else:
                                st.markdown("<h4 style='margin:0'>❓</h4>", unsafe_allow_html=True)
                        with col_text:
                            st.markdown(f"**Step {i+1}:** {task['description']}")
                    else:
                        st.error(f"Step {i+1}: Task ID {task_id} not found")
                
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

    st.divider()
    st.subheader("🧪 Live System Test")
    st.markdown("Test the integration between **Master AI**, **Crew Builder**, and **Tools**.")
    
    test_prompt = st.text_input("Enter a natural language request", placeholder="e.g. Analizza i file e scrivi un report...")
    
    if st.button("🚀 Execute Integration Test", type="primary"):
        if not test_prompt:
            st.error("Please enter a prompt.")
        else:
            with st.status("Running Integration Test...", expanded=True) as status:
                try:
                    # 1. Master AI Routing
                    st.write("🧠 **Master AI** is analyzing the intent...")
                    master = MasterAI()
                    routing = master.evaluate_intent(test_prompt)
                    st.json(routing)
                    
                    if routing.get('status') == 'success' and routing.get('workflow_id'):
                        wf_id = routing['workflow_id']
                        st.write(f"✅ Route found: **Workflow ID {wf_id}**")
                        
                        # 2. Crew Building
                        st.write("🛠️ **Crew Builder** is assembling the agents...")
                        crew = build_crew(wf_id)
                        
                        # 3. Execution
                        st.write("⚡ **Executing Workflow...**")
                        # Pass extracted params if available, otherwise raw prompt
                        inputs = routing.get('extracted_params', {})
                        # Create a run record
                        run_id = db.create_run(wf_id, status='running')
                        
                        try:
                            result = crew.kickoff(inputs=inputs)
                            
                            # Update run record
                            db.update_run(run_id, status='completed', result=str(result))

                            # Send notification
                            notifier = NotificationManager()
                            workflow = db.read_workflow(wf_id)
                            wf_name = workflow["name"] if workflow else f"Workflow {wf_id}"
                            notifier.notify_workflow_completion(wf_name, result)

                            st.success("✅ Execution Complete!")
                            st.markdown("### Final Output")
                            st.markdown(str(result))
                        except Exception as e:
                            db.update_run(run_id, status='failed', result=str(e))
                            raise e
                    else:
                        st.warning(f"⚠️ Master AI could not route this request. Reason: {routing.get('message', 'No matching workflow')}")
                    
                    status.update(label="Test Finished", state="complete")
                except Exception as e:
                    st.error(f"❌ Error during test: {e}")
                    import traceback
                    st.code(traceback.format_exc())
                    status.update(label="Test Failed", state="error")




def render_history_monitoring():
    """Renders Tab 5: Workflow Run History."""
    db = get_db_manager()
    st.header("History & Monitoring")
    st.markdown("Track the execution status and results of your workflows.")

    runs = db.read_all_runs(limit=50)
    workflows = db.read_all_workflows()
    wf_map = {wf['id']: wf['name'] for wf in workflows}

    if not runs:
        st.info("No workflow runs recorded yet.")
        return

    for run in runs:
        wf_name = wf_map.get(run['workflow_id'], f"Workflow {run['workflow_id']}")
        status = run['status']
        
        status_colors = {
            'running': '🔵 Running',
            'completed': '🟢 Completed',
            'failed': '🔴 Failed'
        }
        status_display = status_colors.get(status, status)
        
        with st.expander(f"{status_display} | {wf_name} | {run['started_at']}"):
            st.markdown(f"**Started At:** {run['started_at']}")
            if run['finished_at']:
                st.markdown(f"**Finished At:** {run['finished_at']}")
            
            st.markdown("**Result / Error:**")
            if run['result']:
                st.code(run['result'], language=None)
            else:
                st.write("No output yet.")


def main():
    """Main function to run the Streamlit dashboard."""
    st.set_page_config(page_title="AI Workflow Configurator", layout="wide")
    # --- Bot Process Management ---
    import subprocess
    import sys
    import os
    
    bot_pid_file = "bot.pid"
    
    def is_bot_running():
        if not os.path.exists(bot_pid_file):
            return False
        with open(bot_pid_file, 'r') as f:
            try:
                pid = int(f.read().strip())
            except ValueError:
                return False
        try:
            output = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"]).decode()
            return str(pid) in output
        except Exception:
            return False

    def toggle_bot():
        if is_bot_running():
            # Stop bot
            with open(bot_pid_file, 'r') as f:
                pid = int(f.read().strip())
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            if os.path.exists(bot_pid_file):
                os.remove(bot_pid_file)
        else:
            # Start bot
            flags = 0x08000000 # CREATE_NO_WINDOW on Windows
            with open("bot.log", "w") as log_file:
                p = subprocess.Popen(
                    [sys.executable, "bot.py"], 
                    creationflags=flags,
                    stdout=log_file,
                    stderr=subprocess.STDOUT
                )
            with open(bot_pid_file, 'w') as f:
                f.write(str(p.pid))

    # --- Header with Right Popovers ---
    col_title, col_tools = st.columns([7, 3])
    with col_title:
        st.title("🤖 AI Workflow Configurator")
        st.caption("A secure dashboard for building and managing AI agent workflows.")
        
        bot_running = is_bot_running()
        btn_label = "🔴 Stop Bot Telegram" if bot_running else "🟢 Start Bot Telegram"
        
        col_btn, _ = st.columns([3, 7])
        with col_btn:
            if st.button(btn_label, use_container_width=True):
                toggle_bot()
                st.rerun()
    
    with col_tools:
        st.write("") # Spacer
        st.write("") # Spacer
        
        # --- Popover 1: Manage Tools ---
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

        # --- Popover 2: Telegram Config ---
        with st.popover("🤖 Telegram Bot Config", use_container_width=True):
            st.markdown("### Telegram Vault")
            st.markdown("Configure your bot credentials for remote control.")
            
            env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
            current_env = dotenv_values(env_path)
            
            tg_token = current_env.get("TELEGRAM_BOT_TOKEN", "")
            tg_ids = current_env.get("TELEGRAM_ALLOWED_USER_IDS", "")
            
            # Status Indicators
            token_status = "🟢" if tg_token.strip() else "🔴"
            ids_status = "🟢" if tg_ids.strip() else "🔴"
            
            st.markdown(f"{token_status} **Bot Token**")
            st.markdown(f"{ids_status} **Allowed User IDs**")
            
            with st.form("telegram_vault_form_standalone"):
                new_tg_token = st.text_input("Telegram Bot Token", type="password", value=tg_token, placeholder="123456789:ABCDEF...")
                new_tg_ids = st.text_input("Allowed User IDs", value=tg_ids, placeholder="e.g. 123456789, 987654321")
                st.caption("IDs must be comma-separated integers.")
                
                if st.form_submit_button("Save Telegram Config"):
                    if new_tg_token:
                        set_key(env_path, "TELEGRAM_BOT_TOKEN", new_tg_token.strip())
                    if new_tg_ids:
                        set_key(env_path, "TELEGRAM_ALLOWED_USER_IDS", new_tg_ids.strip())
                    st.success("Telegram configuration saved to .env!")
                    st.rerun()

    # --- Sidebar Guide ---
    with st.sidebar:
        st.header("🚀 Guide & Placeholders")
        st.markdown("""
        Customize your **Task Descriptions** using these dynamic placeholders:
        
        - **`{variable_name}`**: 
          **Dynamic Input**: Define custom variables in the 'Required Inputs' section. Alfredo will ask for them in chat before execution.
          *Tip: If multiple tasks share the same `{variable_name}`, Alfredo will ask only once!*

        - **`{user_input}`**: 
          Inserts the full initial message you sent to trigger the bot.
          
        - **`{previous_result}`**: 
          Inserts the output of the *last* executed workflow session.
          
        ---
        *Alfredo (Master AI) handles all conversations and will automatically replace these placeholders during the planning phase.*
        """)
        st.divider()
        st.info("The configuration is saved directly to your SQLite database.")

    # Initialize session state for editing
    if 'editing_task_id' not in st.session_state:
        st.session_state.editing_task_id = None
    if 'editing_agent_id' not in st.session_state:
        st.session_state.editing_agent_id = None

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Tab 1: API Vault & Model Registry",
        "Tab 2: Agent Caserma",
        "Tab 3: Task Builder",
        "Tab 4: Workflow Assembler",
        "Tab 5: History & Monitoring"
    ])

    with tab1:
        render_api_vault()

    with tab2:
        render_agent_caserma()
    
    with tab3:
        render_task_builder()

    with tab4:
        render_workflow_assembler()

    with tab5:
        render_history_monitoring()


if __name__ == "__main__":
    main()