# ui/dashboard.py

import streamlit as st
import importlib
import core.db_manager
# Force reload to ensure new methods like delete_run are picked up
importlib.reload(core.db_manager)
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

def render_knowledge_base():
    """Renders Tab 1: Vector Knowledge Base."""
    import importlib
    import core.vector_manager
    importlib.reload(core.vector_manager)
    from core.vector_manager import VectorManager
    import tempfile
    
    db = get_db_manager()
    st.header("Add Database (Vector Knowledge Base)")
    st.markdown("Create local vector databases from your documents to provide context to agents.")
    
    col_list, col_main = st.columns([1, 2.5])
    
    with col_list:
        st.subheader("Existing Databases")
        vector_dbs = db.read_all_vector_dbs()
        vm = VectorManager()
        storage_dir = vm.storage_dir  # default: storage/vector_dbs

        if not vector_dbs:
            st.info("No databases created yet.")
        else:
            for vdb in vector_dbs:
                folder_exists = os.path.isdir(vdb['path'])
                with st.container(border=True):
                    if not folder_exists:
                        st.markdown(f"**{vdb['name']}** ⚠️")
                        st.caption("Missing on disk — folder was deleted externally.")
                    else:
                        # Count structured CSVs if present
                        structured_dir = os.path.join(vdb['path'], "structured")
                        csv_count = len([f for f in os.listdir(structured_dir) if f.endswith('.csv')]) if os.path.isdir(structured_dir) else 0
                        label = f"**{vdb['name']}**"
                        if csv_count:
                            label += f" 📊 ({csv_count} table{'s' if csv_count > 1 else ''})"
                        st.markdown(label)
                    st.caption(f"Provider: {vdb['provider']} | Model: {vdb['model_name']}")
                    if st.button("Delete", key=f"del_vdb_{vdb['id']}", type="primary", use_container_width=True):
                        if folder_exists:
                            vm.delete_database(vdb['name'])
                        db.delete_vector_db(vdb['id'])
                        st.toast(f"Database {vdb['name']} removed", icon="🗑️")
                        st.rerun()

                    if folder_exists and st.button("Manage Files", key=f"manage_vdb_{vdb['id']}", use_container_width=True):
                        st.session_state.manage_vdb = vdb
                        if "query_vdb" in st.session_state:
                            del st.session_state.query_vdb
                        st.rerun()

                    if folder_exists and st.button("Query", key=f"query_vdb_{vdb['id']}", use_container_width=True):
                        st.session_state.query_vdb = vdb
                        if "manage_vdb" in st.session_state:
                            del st.session_state.manage_vdb
                        st.rerun()

        # --- Auto-Discovery: Detect manually added DB folders not in metadata ---
        registered_names = {vdb['name'] for vdb in vector_dbs}
        discovered = []
        if os.path.isdir(storage_dir):
            for folder_name in sorted(os.listdir(storage_dir)):
                folder_path = os.path.join(storage_dir, folder_name)
                if os.path.isdir(folder_path) and folder_name not in registered_names:
                    # Check it looks like a real DB (has chroma.sqlite3 or structured/ subfolder)
                    has_chroma = os.path.exists(os.path.join(folder_path, "chroma.sqlite3"))
                    has_structured = os.path.isdir(os.path.join(folder_path, "structured"))
                    if has_chroma or has_structured:
                        discovered.append((folder_name, folder_path))

        if discovered:
            st.divider()
            st.markdown("**🔍 Discovered Databases** — *Found on disk but not registered*")
            for disc_name, disc_path in discovered:
                with st.container(border=True):
                    st.markdown(f"📁 `{disc_name}`")
                    st.caption(disc_path)

                    # Quick-import form
                    from dotenv import dotenv_values, find_dotenv
                    env_path_disc = find_dotenv() or os.path.join(os.getcwd(), '.env')
                    current_env_disc = dotenv_values(env_path_disc)

                    imp_models = {
                        "Local (Ollama) / nomic-embed-text": {"provider": "ollama", "model_name": "nomic-embed-text"},
                    }
                    if current_env_disc.get("OPENAI_API_KEY"):
                        imp_models["OpenAI / text-embedding-3-small"] = {"provider": "openai", "model_name": "text-embedding-3-small"}
                    if current_env_disc.get("GEMINI_API_KEY") or current_env_disc.get("GOOGLE_API_KEY"):
                        imp_models["Gemini / gemini-embedding-2"] = {"provider": "gemini", "model_name": "models/gemini-embedding-2"}
                        imp_models["Gemini / gemini-embedding-001"] = {"provider": "gemini", "model_name": "models/gemini-embedding-001"}

                    sel = st.selectbox(
                        "Embedding model used to create this DB",
                        options=list(imp_models.keys()),
                        key=f"disc_model_{disc_name}"
                    )
                    if st.button("Import", key=f"disc_import_{disc_name}", use_container_width=True):
                        chosen = imp_models[sel]
                        db.create_vector_db(
                            name=disc_name,
                            path=disc_path,
                            provider=chosen["provider"],
                            model_name=chosen["model_name"]
                        )
                        st.toast(f"Database '{disc_name}' imported successfully!", icon="✅")
                        st.rerun()

    with col_main:
        if "manage_vdb" in st.session_state:
            vdb = st.session_state.manage_vdb
            st.subheader(f"📂 Manage Files: {vdb['name']}")
            st.caption(f"Path: {vdb['path']} | Provider: {vdb['provider']} | Model: {vdb['model_name']}")
            
            vm = VectorManager()
            files_dict = vm.get_database_files(vdb['path'], vdb['provider'], vdb['model_name'])
            
            col_close, _ = st.columns([1, 4])
            with col_close:
                if st.button("Close Manager", key="close_manage_vdb", use_container_width=True):
                    del st.session_state.manage_vdb
                    st.rerun()
                
            # File list container
            with st.container(border=True):
                # 1. Tabular Files
                st.markdown("### 📊 Structured Tables (Excel/CSV)")
                if not files_dict['structured']:
                    st.info("No structured files inside this database.")
                else:
                    # Scrollable container if there are multiple tables
                    container_ctx = st.container(height=200) if len(files_dict['structured']) > 4 else st.container()
                    with container_ctx:
                        for idx, s_file in enumerate(files_dict['structured']):
                            col_file_name, col_file_action = st.columns([5, 1.2])
                            with col_file_name:
                                st.write(f"📊 `{s_file}`")
                            with col_file_action:
                                if st.button("🗑️ Remove", key=f"rm_struct_{idx}_{vdb['id']}", type="secondary", use_container_width=True):
                                    success = vm.remove_file_from_database(
                                        db_path=vdb['path'],
                                        provider=vdb['provider'],
                                        model_name=vdb['model_name'],
                                        file_type='structured',
                                        file_identifier=s_file
                                    )
                                    if success:
                                        st.toast(f"Removed structured table '{s_file}'", icon="✅")
                                        st.rerun()
                                    else:
                                        st.error(f"Could not remove '{s_file}'")
                
                st.divider()
                
                # 2. Vectorized Files
                st.markdown("### 📖 Vectorized Documents (PDF/TXT/DOCX)")
                
                # Search input for vectorized files
                search_vect = st.text_input(
                    "🔍 Search vectorized documents by name",
                    placeholder="Type to filter documents...",
                    key=f"search_vect_input_{vdb['id']}"
                )
                
                filtered_vectorized = files_dict['vectorized']
                if search_vect:
                    filtered_vectorized = [
                        f for f in files_dict['vectorized']
                        if search_vect.lower() in os.path.basename(f).lower()
                    ]
                
                if not filtered_vectorized:
                    if search_vect:
                        st.info("No documents match your search query.")
                    else:
                        st.info("No vectorized files inside this database.")
                else:
                    # Constrain height to 300px to enable scrollbar
                    with st.container(height=300):
                        for idx, v_file in enumerate(filtered_vectorized):
                            col_file_name, col_file_action = st.columns([5, 1.2])
                            v_base = os.path.basename(v_file)
                            with col_file_name:
                                st.write(f"📄 `{v_base}`")
                                st.caption(v_file)
                            with col_file_action:
                                if st.button("🗑️ Remove", key=f"rm_vect_{idx}_{vdb['id']}", type="secondary", use_container_width=True):
                                    success = vm.remove_file_from_database(
                                        db_path=vdb['path'],
                                        provider=vdb['provider'],
                                        model_name=vdb['model_name'],
                                        file_type='vectorized',
                                        file_identifier=v_file
                                    )
                                    if success:
                                        st.toast(f"Removed '{v_base}' from vector store", icon="✅")
                                        st.rerun()
                                    else:
                                        st.error(f"Could not remove '{v_base}'")
                                    
            # Form to add files to this database
            st.markdown("### ➕ Add Files to this Database")
            with st.form("add_files_to_db_form"):
                new_files = st.file_uploader(
                    "Select documents/spreadsheets to add",
                    accept_multiple_files=True,
                    type=['pdf', 'txt', 'md', 'docx', 'csv', 'xlsx', 'xls'],
                    key="manage_upload_files"
                )
                
                col_add_btn, _ = st.columns([1, 4])
                with col_add_btn:
                    submitted = st.form_submit_button("Add Files", type="primary")
                    
                if submitted:
                    if not new_files:
                        st.warning("Please upload at least one file.")
                    else:
                        with st.spinner("Processing files..."):
                            temp_paths = []
                            for f in new_files:
                                suffix = os.path.splitext(f.name)[1]
                                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_f:
                                    temp_f.write(f.read())
                                    temp_paths.append(temp_f.name)
                                    
                            try:
                                res = vm.add_files_to_database(
                                    db_path=vdb['path'],
                                    provider=vdb['provider'],
                                    model_name=vdb['model_name'],
                                    file_paths=temp_paths
                                )
                                for p in temp_paths:
                                    try:
                                        os.remove(p)
                                    except Exception:
                                        pass
                                        
                                if res.get('status') == 'success':
                                    st.success(res.get('message'))
                                    st.toast("Files added successfully!", icon="✅")
                                    st.rerun()
                                else:
                                    st.error(res.get('message'))
                            except Exception as add_err:
                                st.error(f"Error adding files: {add_err}")
            st.divider()

        if "query_vdb" in st.session_state:
            vdb = st.session_state.query_vdb
            st.subheader(f"Query: {vdb['name']}")
            st.caption(f"Path: {vdb['path']} | Provider: {vdb['provider']} | Model: {vdb['model_name']}")
            
            with st.container(border=True):
                query_text = st.text_input("Ask a question to this database", placeholder="e.g. What is the main conclusion of the research?")
                col_q1, col_q2 = st.columns([1, 4])
                with col_q1:
                    if st.button("Run Query", type="primary"):
                        if query_text:
                            vm = VectorManager()
                            with st.spinner("Searching..."):
                                response = vm.query_database(
                                    db_path=vdb['path'],
                                    provider=vdb['provider'],
                                    model_name=vdb['model_name'],
                                    query=query_text
                                )
                                st.session_state.last_query_result = response
                        else:
                            st.warning("Please enter a query.")
                with col_q2:
                    if st.button("Close Query", use_container_width=False):
                        del st.session_state.query_vdb
                        if "last_query_result" in st.session_state:
                            del st.session_state.last_query_result
                        st.rerun()
            
            if "last_query_result" in st.session_state:
                st.markdown("### Results")
                st.info(st.session_state.last_query_result)
            
            st.divider()

        with st.form("create_vdb_form"):
            st.subheader("Create New Database")
            db_name = st.text_input("Database Name", placeholder="e.g. project_docs_2026")
            
            # File Uploader supports drag & drop inherently
            uploaded_files = st.file_uploader(
                "Drag & Drop Documents (PDF, TXT, DOCX, CSV, Excel)",
                accept_multiple_files=True,
                type=['pdf', 'txt', 'md', 'docx', 'csv', 'xlsx', 'xls'],
                help="📄 PDF/TXT/DOCX/MD → vectorized for semantic search.  📊 CSV/XLSX/XLS → cleaned and saved as structured tables, queryable by agents via the tabular_query tool."
            )
            
            # Model Selection
            from dotenv import dotenv_values, find_dotenv
            env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
            current_env = dotenv_values(env_path)
            
            available_embedding_models = {
                "Local (Ollama) / nomic-embed-text": {"provider": "ollama", "model_name": "nomic-embed-text"},
                "Local (Ollama) / mxbai-embed-large": {"provider": "ollama", "model_name": "mxbai-embed-large"},
                "Local (Ollama) / all-minilm": {"provider": "ollama", "model_name": "all-minilm"}
            }
            
            if current_env.get("OPENAI_API_KEY") and str(current_env["OPENAI_API_KEY"]).strip():
                available_embedding_models.update({
                    "OpenAI / text-embedding-3-small": {"provider": "openai", "model_name": "text-embedding-3-small"},
                    "OpenAI / text-embedding-3-large": {"provider": "openai", "model_name": "text-embedding-3-large"},
                    "OpenAI / text-embedding-ada-002": {"provider": "openai", "model_name": "text-embedding-ada-002"}
                })
                
            if (current_env.get("GEMINI_API_KEY") and str(current_env["GEMINI_API_KEY"]).strip()) or \
               (current_env.get("GOOGLE_API_KEY") and str(current_env["GOOGLE_API_KEY"]).strip()):
                available_embedding_models.update({
                    "Gemini / gemini-embedding-2": {"provider": "gemini", "model_name": "models/gemini-embedding-2"},
                    "Gemini / gemini-embedding-001": {"provider": "gemini", "model_name": "models/gemini-embedding-001"}
                })
                
            available_embedding_models["Other (Manual Input)"] = {"provider": "custom", "model_name": "custom"}
                
            selected_model_str = st.selectbox("Select Embedding Model", options=list(available_embedding_models.keys()))
            
            if selected_model_str == "Other (Manual Input)":
                col_prov, col_mod = st.columns(2)
                with col_prov:
                    custom_provider = st.selectbox("Provider", ["ollama", "openai", "gemini"])
                with col_mod:
                    custom_model_name = st.text_input("Model Name", placeholder="e.g. mxbai-embed-large")
                
                selected_provider = custom_provider
                selected_model_name = custom_model_name
            else:
                selected_provider = available_embedding_models[selected_model_str]["provider"]
                selected_model_name = available_embedding_models[selected_model_str]["model_name"]
            
            # --- Advanced Parameters ---
            with st.expander("🛠️ Advanced Parameters (Chunking & Quality)"):
                col_c1, col_c2 = st.columns(2)
                with col_c1:
                    chunk_size = st.number_input("Chunk Size (Characters)", min_value=100, max_value=10000, value=1000, step=100, 
                                                 help="The maximum number of characters per chunk. Larger chunks provide more context but may exceed model limits.")
                with col_c2:
                    chunk_overlap = st.number_input("Chunk Overlap", min_value=0, max_value=2000, value=200, step=50,
                                                    help="The number of overlapping characters between chunks to maintain context continuity.")
            
            submitted = st.form_submit_button("Start Embedding", type="primary")
            
            if submitted:
                if not db_name or not uploaded_files or not selected_model_name:
                    st.error("Please provide a name, select files, and choose a valid model.")
                else:
                    # Sanitize DB name
                    safe_db_name = sanitize_filename(db_name)
                    
                    # Save files to a temporary directory for processing
                    with tempfile.TemporaryDirectory() as temp_dir:
                        file_paths = []
                        for uploaded_file in uploaded_files:
                            temp_path = os.path.join(temp_dir, uploaded_file.name)
                            with open(temp_path, "wb") as f:
                                f.write(uploaded_file.getbuffer())
                            file_paths.append(temp_path)
                            
                        # Process
                        with st.status(f"Processing {len(file_paths)} files...", expanded=True) as status:
                            st.write("Extracting text and generating embeddings...")
                            vm = VectorManager()
                            
                            result = vm.create_database(
                                db_name=safe_db_name,
                                file_paths=file_paths,
                                provider=selected_provider,
                                model_name=selected_model_name,
                                chunk_size=chunk_size,
                                chunk_overlap=chunk_overlap
                            )
                            
                            if result["status"] == "success":
                                # Save to metadata DB
                                db.create_vector_db(
                                    name=safe_db_name,
                                    path=result['db_path'],
                                    provider=selected_provider,
                                    model_name=selected_model_name
                                )
                                st.write(result["message"])
                                if result.get("skipped_files"):
                                    st.warning("Some files were skipped:")
                                    for skipped in result["skipped_files"]:
                                        st.write(f"- {skipped}")
                                status.update(label="Embedding Complete!", state="complete")
                                st.success(f"Database '{safe_db_name}' created successfully!")
                                st.rerun()
                            else:
                                st.error(result["message"])
                                if result.get("skipped_files"):
                                    st.warning("Skipped files:")
                                    for skipped in result["skipped_files"]:
                                        st.write(f"- {skipped}")
                                status.update(label="Embedding Failed", state="error")


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
    
    # Combine suggested keys and any other keys already in .env, excluding Telegram configs
    all_keys = sorted([k for k in set(suggested_keys + list(current_env.keys())) if "TELEGRAM" not in k.upper()])
    
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
                if "TELEGRAM" in final_key_name:
                    st.error("Telegram bot tokens must be managed in the Telegram Bot Config at the top right, not here.")
                else:
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
            env_options = sorted([k for k, v in current_env.items() if v and str(v).strip() and "TELEGRAM" not in k.upper()])
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
    
    # --- Main Agent (Master AI) Configuration Row ---
    st.markdown("### 🧠 Main Agent (Master AI) Configuration")
    
    # Read current Master AI model selection from .env
    from dotenv import set_key
    env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
    current_env = dotenv_values(env_path)
    current_master_model_id = current_env.get("MASTER_AI_MODEL_ID", "")
    
    # Find matching model index
    model_names = list(model_options.keys())
    model_ids = list(model_options.values())
    
    default_master_index = 0
    try:
        if current_master_model_id:
            master_model_id_int = int(current_master_model_id)
            if master_model_id_int in model_ids:
                default_master_index = model_ids.index(master_model_id_int)
    except ValueError:
        pass
        
    col_master_model, col_master_save = st.columns([4, 1])
    with col_master_model:
        selected_master_model_str = st.selectbox(
            "Select Model for Master AI (System Orchestrator)", 
            options=model_names, 
            index=default_master_index,
            key="master_ai_model_select",
            help="Select the model that Master AI will use for routing intents and refining agent output."
        )
    with col_master_save:
        st.write("") # Spacer
        st.write("") # Spacer
        if st.button("💾 Save Model", use_container_width=True, key="save_master_model_btn"):
            chosen_model_id = model_options[selected_master_model_str]
            set_key(env_path, "MASTER_AI_MODEL_ID", str(chosen_model_id))
            st.toast("Master AI Model saved successfully!", icon="✅")
            st.rerun()
            
    st.divider()
    
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
                                backstory_content = agent['backstory'] or ""
                                goal_val = ""
                                backstory_val = backstory_content
                                
                                if "Goal: " in backstory_content and "\n\nBackstory: " in backstory_content:
                                    try:
                                        parts = backstory_content.split("\n\nBackstory: ")
                                        goal_val = parts[0].replace("Goal: ", "").strip()
                                        backstory_val = parts[1].strip()
                                    except Exception:
                                        pass
                                
                                if goal_val:
                                    st.markdown(f"**Goal:** {goal_val}")
                                    st.markdown(f"**Backstory:** {backstory_val}")
                                else:
                                    st.markdown(f"**Backstory:** {backstory_content}")
                                    
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
        default_specialization = editing_task.get('agent_specialization', '') or '' if editing_task else ''
        
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
        
        col_av, col_sel = st.columns([1.8, 8.2])
        with col_av:
            if current_agent:
                # Alignment for larger avatar, pushed towards the right
                st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
                _, img_col = st.columns([1, 4])
                with img_col:
                    st.image(get_agent_avatar_url(current_agent), width=100)
        with col_sel:
            selected_agent_name = st.selectbox("Assign to Agent", options=agent_names, index=default_index, key="task_agent_sel")
            
        # --- Spacing ---
        st.markdown("<div style='margin-top: 12px;'></div>", unsafe_allow_html=True)
            
        # --- Agent Specialization (Optional) ---
        agent_specialization = st.text_input(
            "Agent Specialization (Optional)",
            value=default_specialization,
            placeholder="e.g. chemical thermodynamics and biofuel combustion properties",
            help="Dynamically narrows the assigned agent's role and backstory for this specific task, without creating a new agent.",
            key="task_specialization_input"
        )
        if agent_specialization:
            st.caption(f"🔍 The agent will act as: **{selected_agent_name.split(' (')[0]} specialized in {agent_specialization}**")
        
        st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)
        selected_tools = st.multiselect("Assign Tools (Optional)", options=AVAILABLE_TOOLS, default=default_tools, key="task_tools_sel")
        
        selected_vector_dbs = []
        if 'vector_search' in selected_tools:
            st.markdown("---")
            st.markdown("**📁 Vector Database Selection** — *Choose which databases this task can read from*")
            vector_dbs = db.read_all_vector_dbs()
            if vector_dbs:
                db_options = {f"{vdb['name']} ({vdb['provider']})": vdb['id'] for vdb in vector_dbs}
                default_vdb_ids = editing_task.get('vector_dbs', []) if editing_task else []
                default_vdb_names = [name for name, v_id in db_options.items() if str(v_id) in default_vdb_ids or int(v_id) in default_vdb_ids]
                
                sel_names = st.multiselect("Select Vector Databases", options=list(db_options.keys()), default=default_vdb_names, key="task_vdb_sel")
                selected_vector_dbs = [str(db_options[name]) for name in sel_names]
            else:
                st.warning("No vector databases available. Create one in the 'Add Database' tab.")

        # --- Outlook Email Credentials Card ---
        if 'manage_email' in selected_tools:
            st.markdown("---")
            st.markdown("**📧 Outlook Configuration** — *SMTP/IMAP credentials for email access*")

            from dotenv import dotenv_values, set_key, find_dotenv
            env_path_email = find_dotenv() or os.path.join(os.getcwd(), '.env')
            current_env_email = dotenv_values(env_path_email)

            # Check which secrets are configured (show status WITHOUT exposing values)
            outlook_keys = {
                "OUTLOOK_EMAIL": "Outlook Email Address",
                "OUTLOOK_APP_PASSWORD": "App Password (NOT your normal password)",
                "OUTLOOK_IMAP_SERVER": "IMAP Server (default: outlook.office365.com)",
                "OUTLOOK_SMTP_SERVER": "SMTP Server (default: smtp.office365.com)",
            }

            with st.container(border=True):
                st.markdown("##### Credentials Status")
                status_col1, status_col2 = st.columns(2)
                for i, (key, label) in enumerate(outlook_keys.items()):
                    is_set = bool(current_env_email.get(key, "").strip())
                    icon = "🟢" if is_set else "🔴"
                    status_text = "Configured" if is_set else "Not configured"
                    with (status_col1 if i % 2 == 0 else status_col2):
                        st.markdown(f"{icon} **{label}**  \n`{status_text}`")

                with st.expander("✏️ Enter / Edit Credentials", expanded=not all(
                    bool(current_env_email.get(k, "").strip()) for k in outlook_keys
                )):
                    st.caption("🔒 Credentials are saved in your local `.env` file. They are never shown or shared.")

                    with st.form("outlook_credentials_form"):
                        col_e1, col_e2 = st.columns(2)
                        with col_e1:
                            new_email = st.text_input(
                                "Outlook Email Address",
                                placeholder="my.email@outlook.com",
                                help="Your Outlook or Office 365 email address"
                            )
                            new_imap = st.text_input(
                                "IMAP Server",
                                value=current_env_email.get("OUTLOOK_IMAP_SERVER", "outlook.office365.com"),
                                help="For Outlook.com and Office365: outlook.office365.com"
                            )
                        with col_e2:
                            new_password = st.text_input(
                                "App Password",
                                type="password",
                                placeholder="xxxx xxxx xxxx xxxx",
                                help="Create an App Password at account.microsoft.com -> Security -> App passwords"
                            )
                            new_smtp = st.text_input(
                                "SMTP Server",
                                value=current_env_email.get("OUTLOOK_SMTP_SERVER", "smtp.office365.com"),
                                help="For Outlook.com and Office365: smtp.office365.com"
                            )

                        save_outlook = st.form_submit_button("💾 Save Outlook Credentials", type="primary")
                        if save_outlook:
                            saved_any = False
                            if new_email.strip():
                                set_key(env_path_email, "OUTLOOK_EMAIL", new_email.strip())
                                saved_any = True
                            if new_password.strip():
                                set_key(env_path_email, "OUTLOOK_APP_PASSWORD", new_password.strip())
                                saved_any = True
                            if new_imap.strip():
                                set_key(env_path_email, "OUTLOOK_IMAP_SERVER", new_imap.strip())
                                saved_any = True
                            if new_smtp.strip():
                                set_key(env_path_email, "OUTLOOK_SMTP_SERVER", new_smtp.strip())
                                saved_any = True

                            if saved_any:
                                st.success("✅ Credentials saved to `.env`. They will not be displayed in plain text.")
                                st.rerun()
                            else:
                                st.warning("No fields filled. Enter at least Email and App Password.")

                st.info(
                    "💡 **How to get a Microsoft App Password:**\n"
                    "1. Go to [account.microsoft.com](https://account.microsoft.com) -> **Security**\n"
                    "2. Click on **Advanced security options**\n"
                    "3. Under *App passwords*, click **Create a new app password**\n"
                    "4. Copy the generated password (16 characters) and paste it above."
                )

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
                    db.update_task(editing_task['id'], sane_description, sane_expected_output, agent_id, selected_tools, input_rows, selected_vector_dbs, agent_specialization.strip() or None)
                    st.success(f"Task updated successfully!")
                    if 'temp_required_inputs' in st.session_state: del st.session_state.temp_required_inputs
                    clear_editing_state('editing_task_id')
                else:
                    db.create_task(sane_description, sane_expected_output, agent_id, selected_tools, input_rows, selected_vector_dbs, agent_specialization.strip() or None)
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
                                
                                if task.get('agent_specialization'):
                                    st.markdown(f"**🎯 Specialization:** *{task['agent_specialization']}*")
                                
                                if task.get('vector_dbs') and 'vector_search' in task.get('tools', []):
                                    # Fetch DB names for display
                                    all_dbs = db.read_all_vector_dbs()
                                    db_id_to_name = {str(d['id']): d['name'] for d in all_dbs}
                                    db_names = [db_id_to_name.get(str(vid), f"Unknown DB (ID {vid})") for vid in task['vector_dbs']]
                                    st.markdown(f"**📁 Vector Databases:** `{', '.join(db_names)}`")
                                
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
                        # Create a run record with inputs
                        run_id = db.create_run(wf_id, status='running', inputs=inputs)
                        
                        try:
                            from core.crew_builder import execute_run_with_resume
                            result = execute_run_with_resume(run_id, status_callback=st.write)
                            
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

    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("🗑️ Clear All History", type="primary", use_container_width=True):
            count = db.clear_all_runs()
            st.toast(f"History cleared! ({count} runs removed)", icon="🗑️")
            st.rerun()

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
            col_data, col_actions = st.columns([8, 1])
            with col_data:
                st.markdown(f"**Started At:** {run['started_at']}")
                if run['finished_at']:
                    st.markdown(f"**Finished At:** {run['finished_at']}")
                
                st.markdown("**Result / Error:**")
                if run['result']:
                    st.code(run['result'], language=None)
                else:
                    st.write("No output yet.")
            
            with col_actions:
                if status in ['failed', 'running']:
                    if st.button("🔄", key=f"res_run_{run['id']}", help="Resume this run"):
                        with st.spinner("Resuming execution..."):
                            try:
                                from core.crew_builder import execute_run_with_resume
                                result = execute_run_with_resume(run['id'])
                                db.update_run(run['id'], status='completed', result=result)
                                st.toast("Run resumed and completed successfully!", icon="✅")
                                st.rerun()
                            except Exception as e:
                                db.update_run(run['id'], status='failed', result=str(e))
                                st.toast(f"Failed to resume run: {e}", icon="❌")
                                st.rerun()
                
                if st.button("🗑️", key=f"del_run_{run['id']}", help="Delete this run"):
                    db.delete_run(run['id'])
                    st.toast(f"Run {run['id']} deleted")
                    st.rerun()


def main():
    """Main function to run the Streamlit dashboard."""
    import os
    from PIL import Image
    logo_path = "logo.png"
    page_icon = None
    if os.path.exists(logo_path):
        try:
            page_icon = Image.open(logo_path)
        except Exception:
            pass
    st.set_page_config(page_title="AI Workflow Configurator", layout="wide", page_icon=page_icon or "🤖")
    # --- Bot Process Management ---
    import subprocess
    import sys
    
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
            # Force UTF-8 environment for the bot process
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            with open("bot.log", "w", encoding="utf-8") as log_file:
                p = subprocess.Popen(
                    [sys.executable, "bot.py"], 
                    creationflags=flags,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env
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
        **🛠️ Tool Support & Models**
        
        If a task requires **Tools** (e.g. searching the web, reading files, executing terminal commands):
        
        - ✅ **Cloud Models**: Use models from **OpenAI**, **Gemini**, **Anthropic**, or **Groq**. These support the "Function Calling" protocol required for tools.
        - ❌ **Local Models (Ollama)**: Models like **phi3**, **llama**, etc., usually **do NOT support tools**. 
          *Note: Alfredo will automatically disable tools if you assign a local model to an agent.*
          
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

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "Tab 1: Add Database",
        "Tab 2: API Vault & Model Registry",
        "Tab 3: Agent Caserma",
        "Tab 4: Task Builder",
        "Tab 5: Workflow Assembler",
        "Tab 6: History & Monitoring"
    ])

    with tab1:
        render_knowledge_base()

    with tab2:
        render_api_vault()

    with tab3:
        render_agent_caserma()
    
    with tab4:
        render_task_builder()

    with tab5:
        render_workflow_assembler()

    with tab6:
        render_history_monitoring()


if __name__ == "__main__":
    main()