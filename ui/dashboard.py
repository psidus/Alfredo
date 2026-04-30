# ui/dashboard.py

import streamlit as st
from core.db_manager import DBManager
from core.data_manager import DataManager
import yaml
import json
import re
from datetime import datetime

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
    
    with st.form("agent_form"):
        st.subheader("Create a New Agent")
        name = st.text_input("Name")
        role = st.text_input("Role")
        goal = st.text_area("Goal")
        backstory = st.text_area("Backstory")
        
        selected_model_str = st.selectbox("Select Model", options=list(model_options.keys()))
        
        submitted = st.form_submit_button("Add Agent")
        if submitted and name and role and goal and backstory and selected_model_str:
            model_id = model_options[selected_model_str]
            # DB schema accepts name, role, backstory. We merge goal into backstory.
            combined_backstory = f"Goal: {goal}\n\nBackstory: {backstory}"
            db.create_agent(name, role, combined_backstory, model_id, [])
            st.success(f"Agent '{name}' has been recruited!")
            st.rerun()

    st.divider()
    st.subheader("Registered Agents")
    agents = db.read_all_agents()
    if agents:
        for agent in agents:
            with st.expander(f"**Agent:** {agent['name']} ({agent['role']})"):
                st.markdown(f"**Backstory:** {agent['backstory']}")
                model = next((m for m in models if m['id'] == agent['model_id']), None)
                if model:
                    st.markdown(f"**Model:** {model['provider']} / {model['model_name']}")
                
                if st.button("Discharge Agent", key=f"del_agent_{agent['id']}", type="primary"):
                    db.delete_agent(agent['id'])
                    st.toast(f"Discharged agent {agent['name']}", icon="🗑️")
                    st.rerun()

def render_task_builder():
    """Renders Tab 3: UI for creating, viewing, updating, and deleting tasks."""
    db = get_db_manager()
    st.header("Task Builder")
    st.markdown("Define individual tasks and assign them to specific agents.")

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
        
        default_agent_id = editing_task['agent_id'] if editing_task else None
        agent_names = list(agent_options.keys())
        agent_ids = list(agent_options.values())
        default_index = agent_ids.index(default_agent_id) if default_agent_id in agent_ids else 0

        description = st.text_area("Task Description", value=default_desc, height=100)
        expected_output = st.text_area("Expected Output", value=default_output, height=150)
        selected_agent_name = st.selectbox("Assign to Agent", options=agent_names, index=default_index)
        
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
                    db.update_task(editing_task['id'], sane_description, sane_expected_output, agent_id)
                    st.success(f"Task updated successfully!")
                    clear_editing_state('editing_task_id') # This will trigger a rerun
                else:
                    db.create_task(sane_description, sane_expected_output, agent_id)
                    st.success(f"Task added successfully!")
                    st.rerun()

    if editing_task:
        if st.button("Cancel Edit"):
            clear_editing_state('editing_task_id')

    st.divider()
    st.subheader("Existing Tasks")
    if not tasks:
        st.info("No tasks created yet. Use the form above to add one.")
    else:
        for task in tasks:
            agent_name = next((name for name, id in agent_options.items() if id == task['agent_id']), "Unknown Agent")
            with st.expander(f"**Task:** {task['description'][:80]}"):
                st.markdown(f"**Assigned Agent:** `{agent_name}`")
                st.markdown("**Expected Output:**")
                st.code(task['expected_output'], language=None)
                
                col1, col2 = st.columns([1, 1])
                with col1:
                    st.button("Edit", key=f"edit_{task['id']}", on_click=set_editing_state, args=('editing_task_id', task['id']), use_container_width=True)
                with col2:
                    if st.button("Delete", key=f"delete_{task['id']}", type="primary", use_container_width=True):
                        db.delete_task(task['id'])
                        st.toast(f"Deleted task {task['id']}", icon="🗑️")
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
    st.title("🤖 AI Workflow Configurator")
    st.caption("A secure dashboard for building and managing AI agent workflows.")

    # Initialize session state for editing
    if 'editing_task_id' not in st.session_state:
        st.session_state.editing_task_id = None

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