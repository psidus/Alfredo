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
import html
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

def start_editing_task(task_id):
    st.session_state.editing_task_id = task_id
    if 'last_editing_task_id' in st.session_state:
        del st.session_state.last_editing_task_id
    # Reset tool checkboxes to prevent leakage
    for k in list(st.session_state.keys()):
        if k.startswith("cb_task_"):
            del st.session_state[k]

def start_editing_agent(agent_id):
    st.session_state.editing_agent_id = agent_id
    if 'last_editing_agent_id' in st.session_state:
        del st.session_state.last_editing_agent_id

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
    
    if "vdb_success_msg" in st.session_state:
        st.success(st.session_state.vdb_success_msg)
        del st.session_state.vdb_success_msg
    
    col_list, col_main = st.columns([1, 2.5])
    
    with col_list:
        st.subheader("Existing Databases")
        vector_dbs = db.read_all_vector_dbs()
        vm = VectorManager()
        storage_dir = vm.storage_dir  # default: storage/vector_dbs

        if not vector_dbs:
            st.info("No databases created yet.")
        else:
            # Display cards in a 2-column grid
            grid_cols = st.columns(2)
            for i, vdb in enumerate(vector_dbs):
                folder_exists = os.path.isdir(vdb['path'])
                with grid_cols[i % 2]:
                    with st.container(height=350, border=True):
                        short_name = vdb['name'][:25] + "..." if len(vdb['name']) > 25 else vdb['name']
                        if not folder_exists:
                            st.markdown(f"**{short_name}** ⚠️")
                            st.caption("Missing on disk — folder was deleted externally.")
                        else:
                            # Count structured CSVs if present
                            structured_dir = os.path.join(vdb['path'], "structured")
                            csv_count = len([f for f in os.listdir(structured_dir) if f.endswith('.csv')]) if os.path.isdir(structured_dir) else 0
                            label = f"**{short_name}**"
                            if csv_count:
                                label += f" 📊 ({csv_count})"
                            st.markdown(label)
                        
                        if folder_exists:
                            db_config = vm.get_database_config(vdb['path'])
                            st.caption(f"Provider: **{vdb['provider']}** | Model: **{vdb['model_name']}**\n\nChunk Size: {db_config.get('chunk_size', 'N/A')} | Overlap: {db_config.get('chunk_overlap', 'N/A')}")
                        else:
                            st.caption(f"Provider: **{vdb['provider']}** | Model: **{vdb['model_name']}**")
                            
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

                    # Read model and provider from config.json if available
                    disc_config = vm.get_database_config(disc_path)
                    disc_provider = disc_config.get("provider")
                    disc_model = disc_config.get("model_name")
                    matched_key = None

                    if disc_provider and disc_model:
                        st.markdown(f"✨ **Detected Embedding:** `{disc_provider} / {disc_model}`")
                        # Try to match detected provider/model with options in imp_models
                        for k, v in imp_models.items():
                            if v["provider"] == disc_provider.lower() and v["model_name"] == disc_model:
                                matched_key = k
                                break
                        
                        if not matched_key:
                            custom_key = f"Detected: {disc_provider.capitalize()} / {disc_model}"
                            imp_models = {custom_key: {"provider": disc_provider.lower(), "model_name": disc_model}, **imp_models}
                            matched_key = custom_key

                    options = list(imp_models.keys())
                    default_idx = options.index(matched_key) if matched_key in options else 0

                    sel = st.selectbox(
                        "Embedding model used to create this DB",
                        options=options,
                        index=default_idx,
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
            db_config = vm.get_database_config(vdb['path'])
            st.caption(f"Files will be vectorized using **{vdb['provider']} / {vdb['model_name']}** with Chunk Size: **{db_config['chunk_size']}** and Overlap: **{db_config['chunk_overlap']}**.")
            
            with st.form("add_files_to_db_form"):
                new_files = st.file_uploader(
                    "Select documents/spreadsheets to add",
                    accept_multiple_files=True,
                    type=['pdf', 'txt', 'md', 'docx', 'csv', 'xlsx', 'xls', 'js', 'py', 'json', 'html', 'css'],
                    key="manage_upload_files"
                )
                
                col_add_btn, _ = st.columns([1, 4])
                with col_add_btn:
                    submitted = st.form_submit_button("Add Files", type="primary")
                    
                if submitted:
                    if not new_files:
                        st.warning("Please upload at least one file.")
                    else:
                        # --- Deduplication: get existing file names ---
                        existing_files = vm.get_database_files(vdb['path'], vdb['provider'], vdb['model_name'])
                        existing_vectorized_basenames = {os.path.basename(f) for f in existing_files.get('vectorized', [])}
                        existing_structured_basenames = set(existing_files.get('structured', []))
                        
                        # Save uploaded files preserving original names (not random temp names)
                        temp_dir_obj = tempfile.mkdtemp()
                        temp_paths = []
                        skipped_duplicates = []
                        
                        for f in new_files:
                            original_name = f.name
                            ext = os.path.splitext(original_name)[1].lower()
                            
                            # Check for duplicates
                            if ext in ('.csv', '.xlsx', '.xls'):
                                # For tabular: check sanitized name against structured/
                                safe_base = re.sub(r'[^a-z0-9_-]', '_', os.path.splitext(original_name)[0].lower()).strip('_')
                                if ext == '.csv':
                                    check_name = f"{safe_base}.csv"
                                else:
                                    check_name = safe_base  # Excel sheets get appended, partial match
                                if any(check_name in s for s in existing_structured_basenames):
                                    skipped_duplicates.append(original_name)
                                    continue
                            else:
                                # For vectorized: check original filename against source basenames
                                if original_name in existing_vectorized_basenames:
                                    skipped_duplicates.append(original_name)
                                    continue
                            
                            temp_path = os.path.join(temp_dir_obj, original_name)
                            with open(temp_path, "wb") as out_f:
                                out_f.write(f.read())
                            temp_paths.append(temp_path)
                        
                        if skipped_duplicates:
                            st.warning(f"⏭️ Skipped {len(skipped_duplicates)} duplicate(s) already in DB: {', '.join(skipped_duplicates)}")
                        
                        if not temp_paths:
                            st.info("All uploaded files are already in the database. Nothing to add.")
                        else:
                            with st.status(f"Adding {len(temp_paths)} new file(s) to database...", expanded=True) as add_status:
                                st.write("Extracting text and generating embeddings...")
                                add_progress = st.empty()
                                
                                def add_progress_callback(current_batch, total_batches, message):
                                    """Live progress feedback for Add Files."""
                                    if total_batches > 0:
                                        pct = int((current_batch / total_batches) * 100)
                                        add_progress.markdown(f"**Progress: {pct}%** — {message}")
                                    else:
                                        add_progress.markdown(f"**{message}**")
                                
                                try:
                                    res = vm.add_files_to_database(
                                        db_path=vdb['path'],
                                        provider=vdb['provider'],
                                        model_name=vdb['model_name'],
                                        file_paths=temp_paths,
                                        chunk_size=db_config['chunk_size'],
                                        chunk_overlap=db_config['chunk_overlap'],
                                        progress_callback=add_progress_callback
                                    )
                                    # Clean up temp files
                                    for p in temp_paths:
                                        try:
                                            os.remove(p)
                                        except Exception:
                                            pass
                                    try:
                                        os.rmdir(temp_dir_obj)
                                    except Exception:
                                        pass
                                            
                                    if res.get('status') == 'success':
                                        add_status.update(label="Files added successfully!", state="complete")
                                        st.success(res.get('message'))
                                        st.toast("Files added successfully!", icon="✅")
                                        st.rerun()
                                    else:
                                        add_status.update(label="Error adding files", state="error")
                                        st.error(res.get('message'))
                                except Exception as add_err:
                                    add_status.update(label="Error adding files", state="error")
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
            db_name = st.text_input("Database Name", placeholder="e.g. project_docs_2026", key="create_vdb_name")
            
            # File Uploader supports drag & drop inherently
            uploaded_files = st.file_uploader(
                "Drag & Drop Documents (PDF, TXT, DOCX, CSV, Excel, Code)",
                accept_multiple_files=True,
                type=['pdf', 'txt', 'md', 'docx', 'csv', 'xlsx', 'xls', 'js', 'py', 'json', 'html', 'css'],
                key="create_vdb_files",
                help="📄 PDF/TXT/DOCX/MD/Code → vectorized for semantic search.  📊 CSV/XLSX/XLS → cleaned and saved as structured tables, queryable by agents via the tabular_query tool."
            )
            
            # Model Selection
            from dotenv import dotenv_values, find_dotenv
            env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
            current_env = dotenv_values(env_path)
            
            # Build embedding model dropdown: cloud models FIRST (best default), then local
            available_embedding_models = {}
            
            # 1. Dynamically load CLOUD models first (so they become the default when available)
            model_map_path = os.path.join(os.getcwd(), 'config', 'models_map.yaml')
            if os.path.exists(model_map_path):
                model_config = DataManager.load_yaml(model_map_path)
                provider_map = model_config.get('provider_map', {})
                for env_key, data in provider_map.items():
                    # Only add if the key is actually in .env
                    if current_env.get(env_key) and str(current_env.get(env_key)).strip():
                        prov_name = data.get('provider', 'Other')
                        embed_list = data.get('embed_models', [])
                        # For retro-compatibility with older yamls, if embed_list is empty but we know some hardcoded ones
                        if not embed_list:
                            if "OPENAI" in env_key: embed_list = ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"]
                            if "GEMINI" in env_key or "GOOGLE" in env_key: embed_list = ["models/gemini-embedding-2", "models/gemini-embedding-001"]
                        
                        for em in embed_list:
                            display_name = f"{prov_name} / {em}"
                            # Standardize provider names for the vector store
                            v_prov = prov_name.lower()
                            if "openai" in v_prov: v_prov = "openai"
                            elif "google" in v_prov or "gemini" in v_prov: v_prov = "gemini"
                            
                            available_embedding_models[display_name] = {"provider": v_prov, "model_name": em}
            
            # 2. Then add local Ollama models as fallback options
            available_embedding_models["Local (Ollama) / nomic-embed-text"] = {"provider": "ollama", "model_name": "nomic-embed-text"}
            available_embedding_models["Local (Ollama) / mxbai-embed-large"] = {"provider": "ollama", "model_name": "mxbai-embed-large"}
            available_embedding_models["Local (Ollama) / all-minilm"] = {"provider": "ollama", "model_name": "all-minilm"}
                
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
                            st.markdown(f"**🧠 Model in use:** `{selected_provider} / {selected_model_name}`")
                            st.info("💡 **How to Stop/Pause:** To stop the embedding process, click the 'Stop' (🛑) button at the top right of the screen. Your progress is saved after every batch, so you won't lose data. You can resume later by clicking 'Manage Files' -> 'Add Files'.")
                            
                            st.write("Extracting text and generating embeddings...")
                            progress_placeholder = st.empty()
                            vm = VectorManager()
                            
                            def ui_progress_callback(current_batch, total_batches, message):
                                """Live progress feedback inside the st.status widget."""
                                if total_batches > 0:
                                    pct = int((current_batch / total_batches) * 100)
                                    progress_placeholder.markdown(
                                        f"**Progress: {pct}%** — {message}"
                                    )
                                else:
                                    progress_placeholder.markdown(f"**{message}**")
                            
                            result = vm.create_database(
                                db_name=safe_db_name,
                                file_paths=file_paths,
                                provider=selected_provider,
                                model_name=selected_model_name,
                                chunk_size=chunk_size,
                                chunk_overlap=chunk_overlap,
                                progress_callback=ui_progress_callback
                            )
                            
                            if result["status"] in ("success", "partial"):
                                # Save to metadata DB — even partial is usable
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
                                if result["status"] == "partial":
                                    st.session_state.vdb_success_msg = f"⚠️ Database '{safe_db_name}' created with partial data. You can add the missing files later via 'Manage Files'."
                                else:
                                    st.session_state.vdb_success_msg = f"✅ Database '{safe_db_name}' created successfully!"
                                
                                # We don't manually clear the form inputs here to avoid StreamlitAPIException.
                                # The user can manually clear the form if they want to create another DB.
                                
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
        "GEMINI_API_KEY",
        "OLLAMA_API_KEY"
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
                final_key_name = final_key_name.upper().replace(' ', '_')
                if "TELEGRAM" in final_key_name:
                    st.error("Telegram bot tokens must be managed in the Telegram Bot Config at the top right, not here.")
                else:
                    from core.api_verifier import verify_and_fetch_models
                    with st.spinner("Verifying API Key and fetching models..."):
                        result = verify_and_fetch_models(final_key_name, key_value.strip())
                        
                    if not result.get("success"):
                        st.error(f"Verification failed: {result.get('error')}")
                    else:
                        set_key(env_path, final_key_name, key_value.strip())
                        
                        # Update models_map.yaml
                        model_map_path = os.path.join(os.getcwd(), 'config', 'models_map.yaml')
                        model_config = DataManager.load_yaml(model_map_path)
                        provider_map = model_config.get('provider_map', {})
                        
                        if final_key_name not in provider_map:
                            # Infer provider name
                            prov_name = "Other"
                            if "OPENAI" in final_key_name: prov_name = "OpenAI"
                            elif "GROQ" in final_key_name: prov_name = "Groq"
                            elif "ANTHROPIC" in final_key_name: prov_name = "Anthropic"
                            elif "GEMINI" in final_key_name or "GOOGLE" in final_key_name: prov_name = "Google"
                            elif "OLLAMA" in final_key_name: prov_name = "Ollama"
                            
                            provider_map[final_key_name] = {"provider": prov_name, "models": [], "embed_models": []}
                            
                        # Update models in yaml
                        provider_map[final_key_name]["models"] = result.get("chat_models", [])
                        provider_map[final_key_name]["embed_models"] = result.get("embed_models", [])
                        model_config['provider_map'] = provider_map
                        
                        # Save yaml
                        with open(model_map_path, 'w') as f:
                            yaml.dump(model_config, f, default_flow_style=False)
                            
                        # Sync to SQLite DB (only chat models)
                        prov = provider_map[final_key_name]["provider"]
                        all_db_models = db.read_all_models()
                        
                        # Find existing models in DB for this key
                        existing_db_models = [m for m in all_db_models if m.get("env_var_name") == final_key_name]
                        existing_model_names = {m["model_name"]: m["id"] for m in existing_db_models}
                        
                        fetched_chat_models = result.get("chat_models", [])
                        
                        # Add new models
                        for m_name in fetched_chat_models:
                            if m_name not in existing_model_names:
                                db.create_model(prov, m_name, final_key_name, False)
                                
                        # Remove deleted models
                        for m_name, m_id in existing_model_names.items():
                            if m_name not in fetched_chat_models:
                                db.delete_model(m_id)
                                
                        st.success(f"Key '{final_key_name}' verified and models synced securely!")
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
    
    # --- Only Local Models can be added manually now ---
    provider = "Ollama"
    env_var_name = ""
    is_local = True
    selected_local = st.selectbox("Local Model Name", options=LOCAL_MODELS + ["Other (Manual)..."])
    if selected_local == "Other (Manual)...":
        model_name = st.text_input("Type Custom Local Model Name", placeholder="e.g., my-custom-model")
    else:
        model_name = selected_local

    submitted = st.button("Add Local Model", type="primary")
    if submitted and provider and model_name:
        import sqlite3
        try:
            db.create_model(provider, model_name, "", True)
            st.success(f"Added local model '{model_name}'.")
            st.rerun()
        except sqlite3.IntegrityError:
            st.error(f"Il modello '{model_name}' è già presente nel database.")
        except Exception as e:
            st.error(f"Errore durante l'aggiunta del modello: {e}")

    models = db.read_all_models()
    if models:
        st.write("Registered Models:")
        
        # Group by provider
        providers_dict = {}
        for model in models:
            p = model.get('provider', 'Other')
            if p not in providers_dict:
                providers_dict[p] = []
            providers_dict[p].append(model)
            
        for provider_name, p_models in providers_dict.items():
            st.markdown(f"**{provider_name}**")
            with st.container(height=250, border=True):
                for model in p_models:
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
            
    # Read current Default Agent model selection from .env
    current_default_agent_model_id = current_env.get("DEFAULT_AGENT_MODEL_ID", "")
    default_agent_index = 0
    try:
        if current_default_agent_model_id:
            agent_model_id_int = int(current_default_agent_model_id)
            if agent_model_id_int in model_ids:
                default_agent_index = model_ids.index(agent_model_id_int)
    except ValueError:
        pass
        
    col_agent_model, col_agent_save = st.columns([4, 1])
    with col_agent_model:
        selected_default_agent_model_str = st.selectbox(
            "Select Default Model for Agents", 
            options=model_names, 
            index=default_agent_index,
            key="default_agent_model_select",
            help="Select the default model that all agents will use if a task doesn't override it."
        )
    with col_agent_save:
        st.write("") # Spacer
        st.write("") # Spacer
        if st.button("💾 Save Default Model", use_container_width=True, key="save_default_agent_model_btn"):
            chosen_model_id = model_options[selected_default_agent_model_str]
            set_key(env_path, "DEFAULT_AGENT_MODEL_ID", str(chosen_model_id))
            st.toast("Default Agent Model saved successfully!", icon="✅")
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

    with st.container(border=True):
        st.subheader(form_title)
        
        default_name = (editing_agent['name'] or "") if editing_agent else ""
        default_role = (editing_agent['role'] or "") if editing_agent else ""
        
        # Logic to split Goal and Backstory if they follow the format
        raw_backstory = (editing_agent['backstory'] or "") if editing_agent else ""
        default_goal = ""
        default_backstory = raw_backstory
        
        if editing_agent and "Goal: " in raw_backstory and "\n\nBackstory: " in raw_backstory:
            try:
                parts = raw_backstory.split("\n\nBackstory: ")
                default_goal = parts[0].replace("Goal: ", "")
                default_backstory = parts[1]
            except Exception:
                pass # Fallback to showing everything in backstory field

        # Ensure session state variables for agent editing exist and are in sync
        current_editing_id = editing_agent['id'] if editing_agent else None

        if "optimized_agent_data" in st.session_state:
            opt_data = st.session_state.pop("optimized_agent_data")
            st.session_state.agent_role_input = opt_data["role"]
            st.session_state.agent_goal_input = opt_data["goal"]
            st.session_state.agent_backstory_input = opt_data["backstory"]
            st.toast("Prompts optimized successfully! Review and save the agent.", icon="✨")

        if "last_editing_agent_id" not in st.session_state:
            st.session_state.last_editing_agent_id = current_editing_id
            st.session_state.agent_name_input = default_name
            st.session_state.agent_role_input = default_role
            st.session_state.agent_goal_input = default_goal
            st.session_state.agent_backstory_input = default_backstory
        elif st.session_state.last_editing_agent_id != current_editing_id:
            st.session_state.last_editing_agent_id = current_editing_id
            st.session_state.agent_name_input = default_name
            st.session_state.agent_role_input = default_role
            st.session_state.agent_goal_input = default_goal
            st.session_state.agent_backstory_input = default_backstory

        # Sanitize session state keys before rendering widgets to avoid TypeError: bad argument type for built-in operation
        for k in ["agent_name_input", "agent_role_input", "agent_goal_input", "agent_backstory_input"]:
            if k in st.session_state:
                if st.session_state[k] is None:
                    st.session_state[k] = ""
                elif not isinstance(st.session_state[k], str):
                    st.session_state[k] = str(st.session_state[k])

        name = st.text_input("Name", key="agent_name_input")
        role = st.text_input("Role", key="agent_role_input")
        goal = st.text_area("Goal", key="agent_goal_input")
        backstory = st.text_area("Backstory", key="agent_backstory_input")
        
        col_opt, col_sub = st.columns([1, 1])
        with col_opt:
            if st.button("✨ Optimize Prompts with AI", use_container_width=True, key="opt_agent_prompts_btn"):
                current_role = st.session_state.get("agent_role_input", "").strip()
                current_goal = st.session_state.get("agent_goal_input", "").strip()
                current_backstory = st.session_state.get("agent_backstory_input", "").strip()
                
                if not current_role and not current_goal and not current_backstory:
                    st.warning("Please fill in at least one field (Role, Goal, or Backstory) to optimize.")
                else:
                    with st.spinner("Optimizing prompts..."):
                        try:
                            master_ai = MasterAI()
                            optimized = master_ai.optimize_agent_fields(
                                role=current_role,
                                goal=current_goal,
                                backstory=current_backstory
                            )
                            st.session_state.optimized_agent_data = {
                                "role": optimized["role"],
                                "goal": optimized["goal"],
                                "backstory": optimized["backstory"]
                            }
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error during optimization: {e}")
                            
        with col_sub:
            submitted = st.button(submit_label, type="primary", use_container_width=True, key="save_agent_btn")
            
        if submitted and name and role and goal and backstory:
            # ARCHITECTURAL MANDATE M1_T3-A1: Sanitize all text inputs
            sane_name = sanitize_input(name)
            sane_role = sanitize_input(role)
            sane_goal = sanitize_input(goal)
            sane_backstory = sanitize_input(backstory)
            
            combined_backstory = f"Goal: {sane_goal}\n\nBackstory: {sane_backstory}"
            
            if editing_agent:
                model_id = editing_agent.get('model_id')
                db.update_agent(editing_agent['id'], sane_name, sane_role, combined_backstory, model_id, [])
                st.success(f"Agent '{sane_name}' updated successfully!")
                if 'last_editing_agent_id' in st.session_state: del st.session_state.last_editing_agent_id
                clear_editing_state('editing_agent_id')
            else:
                db.create_agent(sane_name, sane_role, combined_backstory, None, [])
                st.success(f"Agent '{sane_name}' has been recruited!")
                if 'last_editing_agent_id' in st.session_state: del st.session_state.last_editing_agent_id
                st.rerun()

    if editing_agent:
        if st.button("Cancel Edit", key="cancel_agent_edit", use_container_width=True):
            if 'last_editing_agent_id' in st.session_state: del st.session_state.last_editing_agent_id
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
                        with st.container(height=380, border=True):
                            avatar_url = get_agent_avatar_url(agent)
                            
                            # st.image handles external URLs
                            
                            # st.image gestisce nativamente URL esterni (no blocchi CSP)
                            col_img = st.columns([1, 2, 1])[1]
                            with col_img:
                                st.image(avatar_url, width=100)
                            
                            short_name = agent['name'][:20] + "..." if len(agent['name']) > 20 else agent['name']
                            short_role = agent['role'][:30] + "..." if len(agent['role']) > 30 else agent['role']
                            
                            st.markdown(f"<h4 style='text-align: center; margin-bottom: 0px; font-size: 18px;' title='{html.escape(agent['name'])}'>{short_name}</h4>", unsafe_allow_html=True)
                            st.markdown(f"<p style='text-align: center; color: gray; font-size: 14px; margin-top: 0px;' title='{html.escape(agent['role'])}'>{short_role}</p>", unsafe_allow_html=True)
                            
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
                                st.button("Edit", key=f"mod_agent_{agent['id']}", on_click=start_editing_agent, args=(agent['id'],), use_container_width=True)
                            with col_del:
                                with st.popover("Del", use_container_width=True):
                                    st.markdown("Are you sure?")
                                    if st.button("Yes", key=f"yes_del_agent_{agent['id']}", type="primary", use_container_width=True):
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
    tools_registry = {}
    try:
        tools_config = DataManager.load_yaml(tools_map_path)
        tools_registry = tools_config.get('tools_registry', {})
        AVAILABLE_TOOLS = list(tools_registry.keys())
    except Exception:
        pass

    TOOL_EMOJIS = {
        "read_file": "📄",
        "write_file": "✏️",
        "read_file_anywhere": "🔍",
        "search_files": "📂",
        "search_web": "🌐",
        "execute_shell_command": "💻",
        "ask_operator": "💬",
        "create_word_document": "📝",
        "edit_word_document": "🖊️",
        "create_excel_document": "📊",
        "take_screenshot": "📸",
        "manage_email": "✉️",
        "vector_search": "🗄️",
        "calculator": "🧮",
        "file_read_tool": "📖",
        "file_write_tool": "💾",
        "python_repl_tool": "🐍"
    }

    agents = db.read_all_agents()
    tasks = db.read_all_tasks()

    if not agents:
        st.warning("No agents found. Please create an agent in 'Tab 2: Agent Caserma' first.")
        return

    agent_options = {f"{agent['name']} ({agent['role']})": agent['id'] for agent in agents}

    # Fetch models for task-level LLM overrides
    models = db.read_all_models()
    model_options = {f"{m['provider']} / {m['model_name']}": m['id'] for m in models}
    model_names = ["None / Use Agent Default"] + list(model_options.keys())
    model_ids = [None] + list(model_options.values())

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
            # Shift session state keys to match the new list
            num_elements = len(st.session_state.temp_required_inputs) + 1
            keys = [st.session_state.get(f"ri_key_{i}", "") for i in range(num_elements)]
            prompts = [st.session_state.get(f"ri_prompt_{i}", "") for i in range(num_elements)]
            
            if index < len(keys):
                keys.pop(index)
            if index < len(prompts):
                prompts.pop(index)
                
            for i in range(num_elements):
                st.session_state.pop(f"ri_key_{i}", None)
                st.session_state.pop(f"ri_prompt_{i}", None)
                
            for i, (k, p) in enumerate(zip(keys, prompts)):
                st.session_state[f"ri_key_{i}"] = k
                st.session_state[f"ri_prompt_{i}"] = p

    with st.container(border=True):
        st.subheader(form_title)
        
        default_name = (editing_task['name'] or "") if editing_task else ""
        default_desc = (editing_task['description'] or "") if editing_task else ""
        default_output = (editing_task['expected_output'] or "") if editing_task else ""
        default_tools = editing_task.get('tools', []) if editing_task else []
        default_specialization = editing_task.get('agent_specialization', '') or '' if editing_task else ''
        
        default_agent_id = editing_task['agent_id'] if editing_task else None
        agent_names = list(agent_options.keys())
        agent_ids = list(agent_options.values())
        default_index = agent_ids.index(default_agent_id) if default_agent_id in agent_ids else 0
        default_agent_name = agent_names[default_index] if agent_names else None
        
        # Calculate default vector DB names
        vector_dbs = db.read_all_vector_dbs()
        default_vdb_names = []
        if vector_dbs and editing_task:
            db_options = {f"{vdb['name']} ({vdb['provider']})": vdb['id'] for vdb in vector_dbs}
            default_vdb_ids = editing_task.get('vector_dbs', []) if editing_task else []
            default_vdb_names = [name for name, v_id in db_options.items() if str(v_id) in default_vdb_ids or int(v_id) in default_vdb_ids]

        # Ensure session state variables for task editing exist and are in sync
        current_editing_task_id = editing_task['id'] if editing_task else None
        
        if "optimized_task_data" in st.session_state:
            opt_data = st.session_state.pop("optimized_task_data")
            st.session_state.task_desc_area = opt_data["description"]
            st.session_state.task_output_area = opt_data["expected_output"]
            st.toast("Task optimized successfully! Review and save changes.", icon="✨")

        if "last_editing_task_id" not in st.session_state:
            st.session_state.last_editing_task_id = current_editing_task_id
            st.session_state.task_name_input = default_name
            st.session_state.task_desc_area = default_desc
            st.session_state.task_output_area = default_output
            st.session_state.temp_required_inputs = editing_task.get('required_inputs', [{"key": "", "prompt": ""}]) if editing_task else [{"key": "", "prompt": ""}]
            st.session_state.task_specialization_input = default_specialization
            st.session_state.task_agent_sel = default_agent_name
            st.session_state.task_tools_sel = default_tools
            st.session_state.task_vdb_sel = default_vdb_names
            
            # Default model override selection
            default_model_id = editing_task.get('model_id') if editing_task else None
            default_model_index = 0
            if default_model_id in model_ids:
                default_model_index = model_ids.index(default_model_id)
            st.session_state.task_model_sel = model_names[default_model_index] if model_names else None
            
            # Clear old dynamic input keys from session state
            for k in list(st.session_state.keys()):
                if k.startswith("ri_key_") or k.startswith("ri_prompt_"):
                    del st.session_state[k]
            # Reset tool checkboxes to match newly loaded default_tools
            for k in list(st.session_state.keys()):
                if k.startswith("cb_task_"):
                    del st.session_state[k]
            # Set the new ones
            for idx, item in enumerate(st.session_state.temp_required_inputs):
                st.session_state[f"ri_key_{idx}"] = item.get("key", "") or ""
                st.session_state[f"ri_prompt_{idx}"] = item.get("prompt", "") or ""
                
        elif st.session_state.last_editing_task_id != current_editing_task_id:
            st.session_state.last_editing_task_id = current_editing_task_id
            st.session_state.task_name_input = default_name
            st.session_state.task_desc_area = default_desc
            st.session_state.task_output_area = default_output
            st.session_state.temp_required_inputs = editing_task.get('required_inputs', [{"key": "", "prompt": ""}]) if editing_task else [{"key": "", "prompt": ""}]
            st.session_state.task_specialization_input = default_specialization
            st.session_state.task_agent_sel = default_agent_name
            st.session_state.task_tools_sel = default_tools
            st.session_state.task_vdb_sel = default_vdb_names
            
            # Default model override selection
            default_model_id = editing_task.get('model_id') if editing_task else None
            default_model_index = 0
            if default_model_id in model_ids:
                default_model_index = model_ids.index(default_model_id)
            st.session_state.task_model_sel = model_names[default_model_index] if model_names else None
            
            # Clear old dynamic input keys from session state
            for k in list(st.session_state.keys()):
                if k.startswith("ri_key_") or k.startswith("ri_prompt_"):
                    del st.session_state[k]
            # Reset tool checkboxes to match newly loaded default_tools
            for k in list(st.session_state.keys()):
                if k.startswith("cb_task_"):
                    del st.session_state[k]
            # Set the new ones
            for idx, item in enumerate(st.session_state.temp_required_inputs):
                st.session_state[f"ri_key_{idx}"] = item.get("key", "") or ""
                st.session_state[f"ri_prompt_{idx}"] = item.get("prompt", "") or ""

        # Sanitize session state keys before rendering widgets to avoid TypeError: bad argument type for built-in operation
        for k in ["task_name_input", "task_desc_area", "task_output_area", "task_specialization_input"]:
            if k in st.session_state:
                if st.session_state[k] is None:
                    st.session_state[k] = ""
                elif not isinstance(st.session_state[k], str):
                    st.session_state[k] = str(st.session_state[k])
                    
        for idx in range(len(st.session_state.get('temp_required_inputs', []))):
            for pfx in ["ri_key_", "ri_prompt_"]:
                k = f"{pfx}{idx}"
                if k in st.session_state:
                    if st.session_state[k] is None:
                        st.session_state[k] = ""
                    elif not isinstance(st.session_state[k], str):
                        st.session_state[k] = str(st.session_state[k])

        task_name = st.text_input(
            "Task Name / Label (Optional)",
            placeholder="e.g. analisi_database",
            help="Give this task a unique name to reference its output in other tasks (e.g. `{task:analisi_database}`). Leave empty to use Task ID instead.",
            key="task_name_input"
        )
        description = st.text_area("Task Description", height=100, key="task_desc_area",
                                    help="Use `{variable_name}` to insert dynamic values from Required Inputs. E.g. `Crea un logo con sfumature {colore}`")
        expected_output = st.text_area("Expected Output", height=150, key="task_output_area",
                                       help="You can also use `{variable_name}` placeholders here.")

        # --- Task AI Optimizer Button ---
        if st.button("✨ Optimize Description & Output with AI", key="opt_task_prompts_btn", use_container_width=True):
            current_desc = st.session_state.get("task_desc_area", "").strip()
            current_out = st.session_state.get("task_output_area", "").strip()
            
            if not current_desc and not current_out:
                st.warning("Please fill in at least one field (Description or Expected Output) to optimize.")
            else:
                with st.spinner("Optimizing task..."):
                    try:
                        master_ai = MasterAI()
                        optimized = master_ai.optimize_task_fields(
                            description=current_desc,
                            expected_output=current_out
                        )
                        st.session_state.optimized_task_data = {
                            "description": optimized["description"],
                            "expected_output": optimized["expected_output"]
                        }
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error during optimization: {e}")
        
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
            
            # Retrieve currently selected index for key-based selectbox alignment
            default_task_model_idx = 0
            if "task_model_sel" in st.session_state and st.session_state.task_model_sel in model_names:
                default_task_model_idx = model_names.index(st.session_state.task_model_sel)
                
            selected_model_str = st.selectbox(
                "Model Override for this Task (Optional)",
                options=model_names,
                index=default_task_model_idx,
                key="task_model_sel",
                help="Select a specific LLM model to execute this task. If set to 'None / Use Agent Default', it will fallback to the agent's default model."
            )
            
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
        
        # --- Human Validation (HITL) ---
        human_validation = st.checkbox(
            "Pause for Human Validation",
            key="task_human_validation_cb",
            value=bool(editing_task.get('human_validation')) if editing_task else False,
            help="If enabled, execution will pause after this task and ask the user via chat to review the agent's output and provide feedback to change/filter the output before proceeding."
        )
        
        st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)
        
        # Display Tools inside a collapsible expander containing Premium Cards
        with st.expander("🛠️ Assign Tools (Optional)", expanded=False):
            st.caption("Select the tools this task's agent is authorized to use:")
            
            # 1. Initialize all tool checkboxes in session state so selection is preserved even when filtered out
            for tool_id in AVAILABLE_TOOLS:
                cb_key = f"cb_task_{tool_id}"
                if cb_key not in st.session_state:
                    st.session_state[cb_key] = tool_id in default_tools
            
            # 2. Search box for filtering tools
            search_query = st.text_input("🔍 Cerca Tool", placeholder="Cerca tool per nome o descrizione...", key="tool_search_input", label_visibility="collapsed")
            st.markdown("<div style='margin-top: 8px;'></div>", unsafe_allow_html=True)
            
            # Filter tools list based on search query
            filtered_tools = AVAILABLE_TOOLS
            if search_query:
                q = search_query.strip().lower()
                filtered_tools = [
                    t for t in AVAILABLE_TOOLS
                    if q in t.lower() or q in t.replace("_", " ").lower() or q in tools_registry.get(t, {}).get("description", "").lower()
                ]

            # 3. Fixed height container with a scrollbar
            with st.container(height=380):
                st.markdown("""
<style>
/* Center checkbox vertically inside the tool card */
div[data-testid="column"] div[data-testid="stCheckbox"] {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    height: 100% !important;
    min-height: 42px !important;
    margin: 0 !important;
    padding: 0 !important;
}
/* Ensure the border container itself has proper spacing */
div[data-testid="column"] div[data-testid="stVerticalBlockBorderWrapper"] {
    padding: 0.6rem 0.8rem !important;
}
</style>
""", unsafe_allow_html=True)
                if filtered_tools:
                    cols_per_row = 4
                    rows = [filtered_tools[i:i + cols_per_row] for i in range(0, len(filtered_tools), cols_per_row)]
                    
                    for row_tools in rows:
                        cols = st.columns(cols_per_row)
                        for idx, tool_id in enumerate(row_tools):
                            with cols[idx]:
                                emoji = TOOL_EMOJIS.get(tool_id, "🛠️")
                                pretty_name = tool_id.replace("_", " ").title()
                                tool_desc = tools_registry.get(tool_id, {}).get("description", "")
                                escaped_desc = html.escape(tool_desc)
                                
                                cb_key = f"cb_task_{tool_id}"
                                with st.container(border=True):
                                    c_cb, c_name = st.columns([1, 5], gap="small")
                                    with c_cb:
                                        st.checkbox("Select", key=cb_key, label_visibility="collapsed", help=tool_desc)
                                    with c_name:
                                        st.markdown(
                                            f"<div style='font-size: 13.5px; font-weight: 600; display: flex; align-items: center; min-height: 42px; line-height: 1.2; margin-left: -5px;'>"
                                            f"<span style='margin-right: 8px; font-size: 19px;'>{emoji}</span>"
                                            f"<span>{pretty_name}</span>"
                                            f"<span title='{escaped_desc}' style='cursor: help; margin-left: 6px; font-size: 15px; color: #4A90E2; font-weight: bold;'>ⓘ</span>"
                                            f"</div>",
                                            unsafe_allow_html=True
                                        )
                else:
                    st.info("Nessun tool corrisponde alla ricerca.")

            # Compile selected tools from all checkboxes state
            selected_tools = [t for t in AVAILABLE_TOOLS if st.session_state.get(f"cb_task_{t}", False)]
            st.session_state.task_tools_sel = selected_tools
        st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)
        
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
                
                # Resolve task model_id
                selected_model_str = st.session_state.get("task_model_sel", "None / Use Agent Default")
                task_model_id = None
                if selected_model_str and selected_model_str != "None / Use Agent Default":
                    task_model_id = model_options.get(selected_model_str)
                
                if editing_task:
                    db.update_task(editing_task['id'], sane_description, sane_expected_output, agent_id, selected_tools, input_rows, selected_vector_dbs, agent_specialization.strip() or None, task_name.strip() or None, task_model_id, human_validation)
                    st.success(f"Task updated successfully!")
                    if 'temp_required_inputs' in st.session_state: del st.session_state.temp_required_inputs
                    if 'last_editing_task_id' in st.session_state: del st.session_state.last_editing_task_id
                    if 'task_name_input' in st.session_state: del st.session_state.task_name_input
                    if 'task_model_sel' in st.session_state: del st.session_state.task_model_sel
                    for k in list(st.session_state.keys()):
                        if k.startswith("cb_task_"):
                            del st.session_state[k]
                    clear_editing_state('editing_task_id')
                else:
                    db.create_task(sane_description, sane_expected_output, agent_id, selected_tools, input_rows, selected_vector_dbs, agent_specialization.strip() or None, task_name.strip() or None, task_model_id, human_validation)
                    st.success(f"Task added successfully!")
                    if 'temp_required_inputs' in st.session_state: del st.session_state.temp_required_inputs
                    if 'last_editing_task_id' in st.session_state: del st.session_state.last_editing_task_id
                    if 'task_name_input' in st.session_state: del st.session_state.task_name_input
                    if 'task_model_sel' in st.session_state: del st.session_state.task_model_sel
                    for k in list(st.session_state.keys()):
                        if k.startswith("cb_task_"):
                            del st.session_state[k]
                    st.rerun()

    if editing_task:
        if st.button("Cancel Edit", key="cancel_task_edit", use_container_width=True):
            if 'temp_required_inputs' in st.session_state:
                del st.session_state.temp_required_inputs
            if 'last_editing_task_id' in st.session_state:
                del st.session_state.last_editing_task_id
            if 'task_name_input' in st.session_state:
                del st.session_state.task_name_input
            if 'task_model_sel' in st.session_state:
                del st.session_state.task_model_sel
            for k in list(st.session_state.keys()):
                if k.startswith("cb_task_"):
                    del st.session_state[k]
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
                                if task.get('name'):
                                    st.markdown(f"**Task Name:** `{task['name']}` (ID: `{task['id']}`)")
                                else:
                                    st.markdown(f"**Task ID:** `{task['id']}`")
                                st.markdown(f"**Description:** {task['description']}")
                                st.markdown("**Expected Output:**")
                                st.code(task['expected_output'], language=None)
                                if task.get('tools'):
                                    pretty_tools = [f"{TOOL_EMOJIS.get(t, '🛠️')} {t.replace('_', ' ').title()}" for t in task['tools']]
                                    st.markdown(f"**Assigned Tools:** {', '.join(pretty_tools)}")
                                
                                if task.get('agent_specialization'):
                                    st.markdown(f"**🎯 Specialization:** *{task['agent_specialization']}*")
                                    
                                if task.get('model_id'):
                                    task_model = next((m for m in models if m['id'] == task['model_id']), None)
                                    if task_model:
                                        st.markdown(f"**🤖 Model Override:** `{task_model['provider']} / {task_model['model_name']}`")
                                
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
                                    st.button("Edit", key=f"edit_{task['id']}", on_click=start_editing_task, args=(task['id'],), use_container_width=True)
                                with col2:
                                    with st.popover("Delete", use_container_width=True):
                                        st.markdown("Are you sure?")
                                        if st.button("Yes", key=f"yes_del_{task['id']}", type="primary", use_container_width=True):
                                            db.delete_task(task['id'])
                                            if "last_editing_task_id" in st.session_state and st.session_state.last_editing_task_id == task['id']:
                                                del st.session_state.last_editing_task_id
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
                            st.button("Edit", key=f"edit_unk_{task['id']}", on_click=start_editing_task, args=(task['id'],), use_container_width=True)
                        with col2:
                            with st.popover("Delete", use_container_width=True):
                                st.markdown("Are you sure?")
                                if st.button("Yes", key=f"yes_del_unk_{task['id']}", type="primary", use_container_width=True):
                                    db.delete_task(task['id'])
                                    if "last_editing_task_id" in st.session_state and st.session_state.last_editing_task_id == task['id']:
                                        del st.session_state.last_editing_task_id
                                    st.toast(f"Deleted orphaned task {task['id']}", icon="🗑️")
                                    st.rerun()

def render_workflow_assembler():
    """Renders Tab 4: UI for creating, viewing, and exporting workflows."""
    db = get_db_manager()
    st.header("Workflow Assembler")
    st.markdown("Combine individual tasks into a sequential workflow.")

    # Inject CSS/JS to style task buttons to look like normal clickable text
    st.markdown("""
<style>
/* CSS to style task buttons inside the task scroll container to look like plain text */
div[data-testid="stElementContainer"]:has(.task-text-marker) + div[data-testid="stElementContainer"] button {
    background-color: transparent !important;
    border: none !important;
    padding: 0 !important;
    margin: 0 !important;
    color: inherit !important;
    text-align: left !important;
    font-weight: normal !important;
    box-shadow: none !important;
    height: auto !important;
    min-height: 0 !important;
    width: 100% !important;
    justify-content: flex-start !important;
    display: inline-block !important;
    cursor: pointer !important;
    white-space: normal !important;
    line-height: inherit !important;
}
div[data-testid="stElementContainer"]:has(.task-text-marker) + div[data-testid="stElementContainer"] button:hover {
    color: #29B6F6 !important;
    background-color: transparent !important;
    text-decoration: underline !important;
}
div[data-testid="stElementContainer"]:has(.task-text-marker) + div[data-testid="stElementContainer"] button:focus {
    background-color: transparent !important;
    box-shadow: none !important;
    color: inherit !important;
    outline: none !important;
}
div[data-testid="stElementContainer"]:has(.task-text-marker) + div[data-testid="stElementContainer"] button:active {
    background-color: transparent !important;
    color: #29B6F6 !important;
}
</style>

<script>
(function() {
    function styleTaskButtons() {
        const markers = document.querySelectorAll('.task-text-marker');
        markers.forEach(marker => {
            const container = marker.closest('div[data-testid="stElementContainer"]');
            if (container) {
                const nextContainer = container.nextElementSibling;
                if (nextContainer) {
                    const btn = nextContainer.querySelector('button');
                    if (btn) {
                        btn.style.backgroundColor = 'transparent';
                        btn.style.border = 'none';
                        btn.style.padding = '0';
                        btn.style.margin = '0';
                        btn.style.boxShadow = 'none';
                        btn.style.color = 'inherit';
                        btn.style.textAlign = 'left';
                        btn.style.justifyContent = 'flex-start';
                        btn.style.display = 'inline-block';
                        btn.style.width = '100%';
                        btn.style.height = 'auto';
                        btn.style.minHeight = '0';
                        btn.style.fontWeight = 'normal';
                        btn.style.cursor = 'pointer';
                        btn.style.whiteSpace = 'normal';
                        
                        btn.onmouseover = () => {
                            btn.style.color = '#29B6F6';
                            btn.style.textDecoration = 'underline';
                            btn.style.backgroundColor = 'transparent';
                        };
                        btn.onmouseout = () => {
                            btn.style.color = 'inherit';
                            btn.style.textDecoration = 'none';
                            btn.style.backgroundColor = 'transparent';
                        };
                        btn.onfocus = () => {
                            btn.style.backgroundColor = 'transparent';
                            btn.style.outline = 'none';
                            btn.style.boxShadow = 'none';
                        };
                    }
                }
            }
        });
    }
    if (!window.taskButtonsIntervalID) {
        window.taskButtonsIntervalID = setInterval(styleTaskButtons, 300);
    }
    styleTaskButtons();
})();
</script>
""", unsafe_allow_html=True)

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

    # --- Edit/Create Workflow State Management ---
    editing_workflow = None
    if 'editing_workflow_id' in st.session_state and st.session_state.editing_workflow_id:
        wf_id = st.session_state.editing_workflow_id
        editing_workflow = next((w for w in workflows if w['id'] == wf_id), None)
        
    current_editing_wf_id = editing_workflow['id'] if editing_workflow else None
    
    default_wf_name = (editing_workflow['name'] or "") if editing_workflow else ""
    default_wf_human_check = editing_workflow['requires_human_check'] if editing_workflow else False
    default_wf_task_ids = list(editing_workflow.get('task_ids', [])) if editing_workflow else []
    
    raw_exports = editing_workflow.get('expected_exports', []) if editing_workflow else []
    default_wf_expected_exports = raw_exports if isinstance(raw_exports, list) else []

    if "last_editing_workflow_id" not in st.session_state:
        st.session_state.last_editing_workflow_id = current_editing_wf_id
        st.session_state.wf_name_input = default_wf_name
        st.session_state.wf_human_check = default_wf_human_check
        st.session_state.wf_expected_exports = default_wf_expected_exports
        st.session_state.wf_selected_task_ids = default_wf_task_ids
        for k in list(st.session_state.keys()):
            if k.startswith("wf_check_"):
                del st.session_state[k]
    elif st.session_state.last_editing_workflow_id != current_editing_wf_id:
        st.session_state.last_editing_workflow_id = current_editing_wf_id
        st.session_state.wf_name_input = default_wf_name
        st.session_state.wf_human_check = default_wf_human_check
        st.session_state.wf_expected_exports = default_wf_expected_exports
        st.session_state.wf_selected_task_ids = default_wf_task_ids
        for k in list(st.session_state.keys()):
            if k.startswith("wf_check_"):
                del st.session_state[k]

    def remove_wf_task(task_id):
        if task_id in st.session_state.wf_selected_task_ids:
            st.session_state.wf_selected_task_ids.remove(task_id)
            chk_key = f"wf_check_{task_id}"
            if chk_key in st.session_state:
                del st.session_state[chk_key]

    def move_wf_task_up(idx):
        lst = st.session_state.wf_selected_task_ids
        if idx > 0:
            lst[idx], lst[idx - 1] = lst[idx - 1], lst[idx]

    def move_wf_task_down(idx):
        lst = st.session_state.wf_selected_task_ids
        if idx < len(lst) - 1:
            lst[idx], lst[idx + 1] = lst[idx + 1], lst[idx]

    with st.container(border=True):
        if editing_workflow:
            st.subheader(f"Edit Workflow: {editing_workflow['name']}")
        else:
            st.subheader("Create a New Workflow")
            
        workflow_name = st.text_input("Workflow Name", key="wf_name_input")
        requires_human_check = st.checkbox("Requires Human Check", key="wf_human_check")
        
        try:
            from core.export_tools import EXPORT_TOOL_MAP
            available_exports = list(EXPORT_TOOL_MAP.keys())
        except ImportError:
            available_exports = ["python", "json", "markdown", "text", "word", "excel"]

        # Ensure default values are valid options
        current_defaults = st.session_state.get("wf_expected_exports", [])
        valid_defaults = [x for x in current_defaults if x in available_exports]

        parsed_exports = st.multiselect(
            "Expected File Outputs (Generated by Master AI)",
            options=available_exports,
            default=valid_defaults,
            key="wf_expected_exports_multiselect"
        )
        
        # Keep session state updated with the selected list so if validation fails it persists
        st.session_state.wf_expected_exports = parsed_exports

        # --- Export Instructions (optional guidance for the Master AI) ---
        default_export_instructions = ""
        if editing_workflow:
            default_export_instructions = editing_workflow.get("export_instructions", "") or ""
        
        if "wf_export_instructions" not in st.session_state or st.session_state.get("last_editing_workflow_id") != current_editing_wf_id:
            st.session_state.wf_export_instructions = default_export_instructions

        export_instructions = st.text_area(
            "📝 Export Instructions (Optional)",
            key="wf_export_instructions_input",
            value=st.session_state.wf_export_instructions,
            placeholder="e.g., For Python: extract the simulation model from the Developer agent. For Excel: use the metrics from the Analyst agent. For Word: write a full report.",
            help="Guide the Master AI on what to extract from each agent's output for each file format. Leave empty to let the AI decide automatically.",
            height=100
        )
        st.session_state.wf_export_instructions = export_instructions

        
        st.markdown("---")
        st.markdown("**📋 Add Workflow Blocks**")
        
        block_type = st.radio("Block Type to Add", ["Single Task", "Batch Loop"], horizontal=True)

        if block_type == "Single Task":
            if not agents:
                st.info("No agents available.")
            else:
                agent_options = {f"{a['name']} - {a['role']}": a['id'] for a in agents}
                sel_agent_str = st.selectbox("Select Agent", list(agent_options.keys()), key="wf_single_agent")
                agent_id = agent_options[sel_agent_str]
                agent_tasks = tasks_by_agent.get(agent_id, [])
                if agent_tasks:
                    task_options = {f"{t.get('name') or 'Task #'+str(t['id'])}: {t['description'][:50]}": t['id'] for t in agent_tasks}
                    sel_task_str = st.selectbox("Select Task", list(task_options.keys()), key="wf_single_task")
                    if st.button("➕ Add Single Task"):
                        st.session_state.wf_selected_task_ids.append(task_options[sel_task_str])
                        st.rerun()
                else:
                    st.info("No tasks assigned to this agent.")

        elif block_type == "Batch Loop":
            with st.container(border=True):
                st.markdown("**Batch Loop Properties**")
                b_size = st.number_input("Batch Size (items per chunk)", min_value=1, value=5)
                b_source = st.text_input("Source Variable", value="{previous_result}", help="The variable or output holding the JSON array to iterate over.")
                
                st.markdown("**Select inner loop tasks (in order):**")
                if tasks:
                    all_task_options = {f"{t.get('name') or 'Task #'+str(t['id'])} ({agent_id_map.get(t.get('agent_id'), {}).get('name', 'Unknown')}): {t['description'][:50]}": t['id'] for t in tasks}
                    sel_inner_tasks_str = st.multiselect("Inner Tasks", list(all_task_options.keys()))
                    if st.button("➕ Add Batch Loop"):
                        inner_ids = [all_task_options[s] for s in sel_inner_tasks_str]
                        if inner_ids:
                            new_block = {
                                "type": "batch_loop",
                                "task_ids": inner_ids,
                                "batch_size": b_size,
                                "source_variable": b_source
                            }
                            st.session_state.wf_selected_task_ids.append(new_block)
                            st.rerun()
                        else:
                            st.error("Select at least one inner task.")
                else:
                    st.info("No tasks available.")

        # --- Ordered Task Preview ---
        st.markdown("---")
        st.markdown("**⚙️ Workflow Steps (in order)**")
        
        if not st.session_state.wf_selected_task_ids:
            st.info("No tasks selected yet.")
        else:
            def rem_step(idx):
                st.session_state.wf_selected_task_ids.pop(idx)

            for i, step in enumerate(st.session_state.wf_selected_task_ids):
                with st.container(border=True):
                    col_num, col_main, col_up, col_down, col_rem = st.columns([0.5, 8, 0.7, 0.7, 0.7])
                    col_num.markdown(f"**{i+1}**")
                    
                    is_batch = isinstance(step, dict) and step.get("type") == "batch_loop"
                    if not is_batch:
                        task_id = step if isinstance(step, int) else step.get("task_id")
                        task = task_id_map.get(int(task_id))
                        if task:
                            agent = agent_id_map.get(task['agent_id'])
                            a_name = agent['name'] if agent else "Unknown Agent"
                            t_name = task.get('name') or f"Task #{task['id']}"
                            col_main.markdown(f"👤 **{a_name}** ➔ **{t_name}**: {task['description'][:70]}...")
                        else:
                            col_main.markdown(f"❌ Unknown Task ID: {task_id}")
                    else:
                        inner_ids = step.get("task_ids", [])
                        b_size = step.get("batch_size")
                        col_main.markdown(f"🔄 **Batch Loop** (Size: {b_size}, Source: `{step.get('source_variable')}`)")
                        for inner_id in inner_ids:
                            itask = task_id_map.get(int(inner_id))
                            if itask:
                                iagent = agent_id_map.get(itask['agent_id'])
                                i_aname = iagent['name'] if iagent else "Unknown Agent"
                                i_tname = itask.get('name') or f"Task #{itask['id']}"
                                col_main.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ 👤 **{i_aname}** ➔ **{i_tname}**")
                            else:
                                col_main.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ ❌ Unknown Task ID: {inner_id}")
                    
                    col_up.button("🔼", key=f"wf_up_{i}", on_click=move_wf_task_up, args=(i,), help="Move up")
                    col_down.button("🔽", key=f"wf_down_{i}", on_click=move_wf_task_down, args=(i,), help="Move down")
                    col_rem.button("✖️", key=f"wf_rem_{i}", on_click=rem_step, args=(i,), help="Remove")
        st.markdown("---")
        if editing_workflow:
            col_save, col_cancel = st.columns([1, 1])
            with col_save:
                if st.button("💾 Update Workflow", type="primary", use_container_width=True):
                    sane_workflow_name = sanitize_input(workflow_name)
                    if not sane_workflow_name or not st.session_state.wf_selected_task_ids:
                        st.error("Workflow Name and at least one Task are required.")
                    else:
                        db.update_workflow(editing_workflow['id'], sane_workflow_name, st.session_state.wf_selected_task_ids, requires_human_check, parsed_exports, export_instructions)
                        st.success(f"Workflow '{sane_workflow_name}' updated successfully!")
                        st.session_state.editing_workflow_id = None
                        if 'last_editing_workflow_id' in st.session_state:
                            del st.session_state.last_editing_workflow_id
                        if 'wf_selected_task_ids' in st.session_state:
                            del st.session_state.wf_selected_task_ids
                        if 'wf_name_input' in st.session_state:
                            del st.session_state.wf_name_input
                        if 'wf_human_check' in st.session_state:
                            del st.session_state.wf_human_check
                        if 'wf_expected_exports' in st.session_state:
                            del st.session_state.wf_expected_exports
                        if 'wf_export_instructions' in st.session_state:
                            del st.session_state.wf_export_instructions
                        for k in list(st.session_state.keys()):
                            if k.startswith("wf_check_"):
                                del st.session_state[k]
                        st.rerun()
            with col_cancel:
                if st.button("❌ Cancel Edit", use_container_width=True):
                    st.session_state.editing_workflow_id = None
                    if 'last_editing_workflow_id' in st.session_state:
                        del st.session_state.last_editing_workflow_id
                    if 'wf_selected_task_ids' in st.session_state:
                        del st.session_state.wf_selected_task_ids
                    if 'wf_name_input' in st.session_state:
                        del st.session_state.wf_name_input
                    if 'wf_human_check' in st.session_state:
                        del st.session_state.wf_human_check
                    if 'wf_expected_exports' in st.session_state:
                        del st.session_state.wf_expected_exports
                    if 'wf_export_instructions' in st.session_state:
                        del st.session_state.wf_export_instructions
                    for k in list(st.session_state.keys()):
                        if k.startswith("wf_check_"):
                            del st.session_state[k]
                    st.rerun()
        else:
            if st.button("💾 Save Workflow", type="primary", use_container_width=True):
                sane_workflow_name = sanitize_input(workflow_name)
                if not sane_workflow_name or not st.session_state.wf_selected_task_ids:
                    st.error("Workflow Name and at least one Task are required.")
                else:
                    db.create_workflow(sane_workflow_name, st.session_state.wf_selected_task_ids, requires_human_check, parsed_exports, export_instructions)
                    st.success(f"Workflow '{sane_workflow_name}' created successfully!")
                    if 'wf_selected_task_ids' in st.session_state:
                        del st.session_state.wf_selected_task_ids
                    if 'wf_name_input' in st.session_state:
                        del st.session_state.wf_name_input
                    if 'wf_human_check' in st.session_state:
                        del st.session_state.wf_human_check
                    if 'wf_expected_exports' in st.session_state:
                        del st.session_state.wf_expected_exports
                    if 'wf_export_instructions' in st.session_state:
                        del st.session_state.wf_export_instructions
                    for k in list(st.session_state.keys()):
                        if k.startswith("wf_check_"):
                            del st.session_state[k]
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
                for i, step in enumerate(task_ids):
                    is_batch = isinstance(step, dict) and step.get("type") == "batch_loop"
                    if not is_batch:
                        task_id = step if isinstance(step, int) else step.get("task_id")
                        task = task_id_map.get(int(task_id))
                        if task:
                            agent = agent_id_map.get(task['agent_id'])
                            col_avatar, col_text = st.columns([1, 25])
                            with col_avatar:
                                if agent:
                                    st.image(get_agent_avatar_url(agent), use_container_width=True)
                                else:
                                    st.markdown("<h4 style='margin:0'>❓</h4>", unsafe_allow_html=True)
                            with col_text:
                                t_name_display = task.get('name') or f"Task #{task['id']}"
                                st.markdown(f"**Step {i+1} ({t_name_display}):** {task['description']}")
                        else:
                            st.error(f"Step {i+1}: Task ID {task_id} not found")
                    else:
                        inner_ids = step.get("task_ids", [])
                        b_size = step.get("batch_size")
                        st.markdown(f"**Step {i+1} (🔄 Batch Loop)**: Chunk Size: {b_size}, Source: `{step.get('source_variable')}`")
                        for inner_id in inner_ids:
                            itask = task_id_map.get(int(inner_id))
                            if itask:
                                iagent = agent_id_map.get(itask['agent_id'])
                                i_aname = iagent['name'] if iagent else "Unknown Agent"
                                i_tname = itask.get('name') or f"Task #{itask['id']}"
                                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ 👤 **{i_aname}** ➔ **{i_tname}**")
                            else:
                                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ ❌ Unknown Task ID: {inner_id}")
                
                # --- ARCHITECTURAL MANDATE M1_T3-A2 & M1_T3-A3: Secure Export Logic ---
                # 1. Construct the data structure for YAML in-memory
                export_data = {'workflow': {'name': workflow['name'], 'tasks': []}}
                for step in task_ids:
                    is_batch = isinstance(step, dict) and step.get("type") == "batch_loop"
                    if not is_batch:
                        task_id = step if isinstance(step, int) else step.get("task_id")
                        task = task_id_map.get(int(task_id))
                        if task:
                            agent = agent_id_map.get(task['agent_id'])
                            task_data = {
                                'name': task.get('name') or f"Task #{task['id']}",
                                'description': task['description'],
                                'expected_output': task['expected_output'],
                                'agent': agent['name'] if agent else 'Unknown Agent'
                            }
                            export_data['workflow']['tasks'].append(task_data)
                    else:
                        batch_info = {
                            'type': 'batch_loop',
                            'batch_size': step.get('batch_size'),
                            'source_variable': step.get('source_variable'),
                            'tasks': []
                        }
                        for inner_id in step.get("task_ids", []):
                            itask = task_id_map.get(int(inner_id))
                            if itask:
                                iagent = agent_id_map.get(itask['agent_id'])
                                task_data = {
                                    'name': itask.get('name') or f"Task #{itask['id']}",
                                    'description': itask['description'],
                                    'expected_output': itask['expected_output'],
                                    'agent': iagent['name'] if iagent else 'Unknown Agent'
                                }
                                batch_info['tasks'].append(task_data)
                        export_data['workflow']['tasks'].append(batch_info)
                # 2. Generate YAML string in-memory. NOTE: yaml.dump is safe for exporting.
                yaml_string = yaml.dump(export_data, sort_keys=False, indent=2)
                
                # 3. Sanitize filename and provide download button
                safe_filename = f"{sanitize_filename(workflow['name'])}.yaml"
                
                col_del, col_edit, col_export = st.columns([1, 1, 2])
                with col_edit:
                    if st.button("Edit", key=f"edit_wf_{workflow['id']}", use_container_width=True):
                        st.session_state.editing_workflow_id = workflow['id']
                        if 'last_editing_workflow_id' in st.session_state:
                            del st.session_state.last_editing_workflow_id
                        st.rerun()
                with col_del:
                    with st.popover("Delete", use_container_width=True):
                        st.markdown("Are you sure?")
                        if st.button("Yes", key=f"yes_del_wf_{workflow['id']}", type="primary", use_container_width=True):
                            db.delete_workflow(workflow['id'])
                            st.toast(f"Deleted workflow {workflow['name']}", icon="🗑️")
                            if st.session_state.get('editing_workflow_id') == workflow['id']:
                                st.session_state.editing_workflow_id = None
                                if 'last_editing_workflow_id' in st.session_state:
                                    del st.session_state.last_editing_workflow_id
                            st.rerun()
                with col_export:
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
    with col2:
        if st.button("🔄 Refresh Live Status", type="secondary"):
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
                
                if status == 'running':
                    current_idx = run.get('current_task_idx', 0)
                    st.info(f"⏳ **In Progress:** Executing Step {current_idx + 1}")

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
                                last_output, global_context = execute_run_with_resume(run['id'])
                                db.update_run(run['id'], status='completed', result=last_output)
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


def render_my_apps():
    """Renders the 'Le mie App' tab for managing external app integrations."""
    db = get_db_manager()
    DataManager.load_env()

    st.subheader("🔗 Le mie App — Integrazione App Esterne")
    st.markdown("""
    Collega applicazioni esterne ad Alfredo. Crea workflow specifici per un'app
    e triggera li dall'app stessa tramite il **Widget SDK** o le **API REST**.
    """)

    # --- Session state for app detail view ---
    if 'viewing_app_id' not in st.session_state:
        st.session_state.viewing_app_id = None

    apps = db.get_all_apps()

    # ========= APP LIST VIEW =========
    if st.session_state.viewing_app_id is None:
        st.divider()

        # --- Create New App Form ---
        with st.expander("➕ Collega Nuova App", expanded=len(apps) == 0):
            with st.form("create_app_form"):
                col1, col2 = st.columns(2)
                with col1:
                    app_name = st.text_input("Nome App (slug, senza spazi)", placeholder="mio_gestionale",
                                             help="Usato nelle URL delle API. Solo lettere, numeri e underscore.")
                    app_display = st.text_input("Nome Visualizzato", placeholder="Il Mio Gestionale")
                    app_desc = st.text_area("Descrizione", placeholder="Descrivi brevemente l'app...", max_chars=500)
                with col2:
                    app_db_type = st.selectbox("Tipo Database", ["sqlite", "postgresql"],
                                               help="Il tipo di database dell'app esterna")
                    app_db_env_key = st.text_input("Nome Variabile .env per DB",
                                                   placeholder="GESTIONALE_DB_URL",
                                                   help="Es: GESTIONALE_DB_URL → Alfredo leggerà os.getenv('GESTIONALE_DB_URL')")
                    app_api_env_key = st.text_input("Nome Variabile .env per API Key",
                                                    placeholder="GESTIONALE_API_KEY",
                                                    help="Es: GESTIONALE_API_KEY → usata per autenticarsi verso l'app")
                    app_api_url = st.text_input("Base URL API dell'app",
                                                placeholder="http://localhost:3000/api")
                    app_root = st.text_input("Path Root Progetto App (opzionale)",
                                             placeholder="C:/Users/.../MioProgetto")

                submitted = st.form_submit_button("🚀 Collega App", use_container_width=True)
                if submitted:
                    if not app_name:
                        st.error("Il nome app è obbligatorio.")
                    elif not re.match(r'^[a-zA-Z0-9_]+$', app_name):
                        st.error("Il nome app può contenere solo lettere, numeri e underscore.")
                    else:
                        try:
                            app_id = db.create_app(
                                name=app_name.lower(),
                                display_name=app_display or app_name,
                                description=app_desc,
                                db_env_key=app_db_env_key or None,
                                api_env_key=app_api_env_key or None,
                                api_base_url=app_api_url or None,
                                db_type=app_db_type,
                                app_root_path=app_root or None
                            )
                            st.success(f"✅ App '{app_display or app_name}' collegata con successo! (ID: {app_id})")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Errore nella creazione dell'app: {e}")

        # --- Apps Cards ---
        if not apps:
            st.info("Nessuna app collegata. Usa il form sopra per collegare la tua prima app.")
        else:
            for app_record in apps:
                with st.container(border=True):
                    col1, col2, col3 = st.columns([3, 1, 1])
                    with col1:
                        status_emoji = "🟢" if app_record.get('status') == 'active' else "🔴"
                        st.markdown(f"### {status_emoji} {app_record.get('display_name', app_record['name'])}")
                        st.caption(f"Slug: `{app_record['name']}` | DB: `{app_record.get('db_type', 'N/A')}` | Creata: {app_record.get('created_at', 'N/A')}")
                        if app_record.get('description'):
                            st.markdown(f"*{app_record['description'][:100]}*")

                        # Count workflows
                        app_workflows = db.get_app_workflows(app_record['id'])
                        st.caption(f"📋 {len(app_workflows)} workflow collegati")

                    with col2:
                        if st.button("📂 Apri", key=f"open_app_{app_record['id']}", use_container_width=True):
                            st.session_state.viewing_app_id = app_record['id']
                            st.rerun()

                    with col3:
                        if st.button("🗑️ Elimina", key=f"del_app_{app_record['id']}", use_container_width=True, type="secondary"):
                            db.delete_app(app_record['id'])
                            st.success(f"App '{app_record['name']}' eliminata.")
                            st.rerun()

    # ========= APP DETAIL VIEW =========
    else:
        app_id = st.session_state.viewing_app_id
        app_record = db.get_app(app_id)

        if not app_record:
            st.error("App non trovata.")
            st.session_state.viewing_app_id = None
            st.rerun()
            return

        # Back button
        if st.button("⬅️ Torna alla lista app"):
            st.session_state.viewing_app_id = None
            st.rerun()

        st.markdown(f"## 🔗 {app_record.get('display_name', app_record['name'])}")

        # --- Tabs inside app detail ---
        app_tab1, app_tab2, app_tab3, app_tab4 = st.tabs([
            "🔑 Connessione", "📋 Workflow", "📦 Integrazione", "📊 Log"
        ])

        # --- TAB: Connection & API Key ---
        with app_tab1:
            st.markdown("### Chiave API")
            st.code(app_record['api_key'], language=None)
            col1, col2 = st.columns(2)
            with col1:
                if st.button("🔄 Rigenera API Key", use_container_width=True):
                    new_key = db.regenerate_app_api_key(app_id)
                    st.warning(f"⚠️ Nuova API Key generata. Aggiorna l'SDK nell'app esterna!")
                    st.code(new_key, language=None)
                    st.rerun()
            with col2:
                new_status = "disabled" if app_record.get('status') == 'active' else "active"
                btn_label = "🔴 Disabilita" if app_record.get('status') == 'active' else "🟢 Abilita"
                if st.button(btn_label, use_container_width=True):
                    db.update_app(app_id, status=new_status)
                    st.rerun()

            st.divider()
            st.markdown("### Configurazione Connessione")

            # Check .env variables
            env_vars_status = []
            if app_record.get('db_env_key'):
                val = os.getenv(app_record['db_env_key'])
                status = "✅" if val else "❌ Non trovata nel .env"
                env_vars_status.append(f"**DB** (`{app_record['db_env_key']}`): {status}")
            if app_record.get('api_env_key'):
                val = os.getenv(app_record['api_env_key'])
                status = "✅" if val else "❌ Non trovata nel .env"
                env_vars_status.append(f"**API Key** (`{app_record['api_env_key']}`): {status}")

            if env_vars_status:
                for s in env_vars_status:
                    st.markdown(s)
            else:
                st.info("Nessuna variabile .env configurata per questa app.")

            st.markdown(f"**Base URL API**: `{app_record.get('api_base_url', 'Non configurato')}`")
            st.markdown(f"**Tipo DB**: `{app_record.get('db_type', 'sqlite')}`")
            st.markdown(f"**Root Path**: `{app_record.get('app_root_path', 'Non configurato')}`")

        # --- TAB: Workflows ---
        with app_tab2:
            st.markdown("### Workflow collegati a questa App")
            app_workflows = db.get_app_workflows(app_id)

            if not app_workflows:
                st.info("Nessun workflow collegato. Crea un workflow nel **Workflow Assembler** (Tab 5) e collegalo a questa app.")
            else:
                for wf in app_workflows:
                    with st.container(border=True):
                        task_ids = wf.get('task_ids', [])
                        st.markdown(f"**{wf['name']}** — {len(task_ids)} task")

                        # Show required inputs
                        all_inputs = []
                        for tid in task_ids:
                            if isinstance(tid, int):
                                task_rec = db.read_task(tid)
                                if task_rec:
                                    for inp in task_rec.get('required_inputs', []):
                                        key = inp.get('key', '') if isinstance(inp, dict) else str(inp)
                                        if key and key not in [x['key'] for x in all_inputs if isinstance(x, dict)]:
                                            all_inputs.append(inp)

                        if all_inputs:
                            input_keys = [inp.get('key', str(inp)) if isinstance(inp, dict) else str(inp) for inp in all_inputs]
                            st.caption(f"Input richiesti: `{'`, `'.join(input_keys)}`")

            st.divider()
            st.markdown("### Collega workflow esistente")
            all_workflows = db.read_all_workflows()
            unlinked = [wf for wf in all_workflows if wf.get('app_id') is None]

            if unlinked:
                wf_options = {f"{wf['name']} (ID: {wf['id']})": wf['id'] for wf in unlinked}
                selected_wf = st.selectbox("Seleziona workflow da collegare", list(wf_options.keys()),
                                           key=f"link_wf_{app_id}")
                if st.button("🔗 Collega a questa App", key=f"link_btn_{app_id}"):
                    wf_id = wf_options[selected_wf]
                    # Update workflow to set app_id
                    wf_record = db.read_workflow(wf_id)
                    if wf_record:
                        db.cursor.execute("UPDATE workflows SET app_id = ? WHERE id = ?", (app_id, wf_id))
                        db.conn.commit()
                        st.success(f"Workflow '{wf_record['name']}' collegato a questa app!")
                        st.rerun()
            else:
                st.caption("Tutti i workflow sono già collegati ad un'app o non ne esistono ancora.")

        # --- TAB: Integration Snippet ---
        with app_tab3:
            st.markdown("### Snippet di Integrazione")
            st.markdown("Copia questo codice nella tua app per integrare il widget Alfredo:")

            api_port = os.getenv('ALFREDO_API_PORT', '8000')
            snippet = f"""<!-- Alfredo SDK Widget -->
<script src="http://localhost:{api_port}/sdk/alfredo-sdk.js"></script>
<script>
  AlfredoClient.init({{
    serverUrl: 'http://localhost:{api_port}',
    apiKey: '{app_record["api_key"]}'
  }});
</script>"""
            st.code(snippet, language="html")

            st.divider()
            st.markdown("### API Diretta (per sviluppatori)")
            st.markdown("Se preferisci usare le API REST direttamente senza il widget:")

            api_example = f"""# Lista workflow disponibili
curl http://localhost:{api_port}/api/apps/{app_record['name']}/workflows \\
  -H "X-API-Key: {app_record['api_key']}"

# Trigger workflow
curl -X POST http://localhost:{api_port}/api/apps/{app_record['name']}/trigger \\
  -H "X-API-Key: {app_record['api_key']}" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "workflow_id": 1,
    "inputs": {{"nome_cliente": "Mario Rossi"}}
  }}'

# Poll risultato
curl http://localhost:{api_port}/api/jobs/{{job_id}}"""
            st.code(api_example, language="bash")

        # --- TAB: Run Logs ---
        with app_tab4:
            st.markdown("### Log Esecuzioni API")
            app_runs = db.get_app_runs(app_id, limit=20)

            if not app_runs:
                st.info("Nessuna esecuzione API registrata per questa app.")
            else:
                for run in app_runs:
                    status_emoji = {"running": "🔄", "completed": "✅", "failed": "❌"}.get(run.get('status', ''), "❓")
                    with st.expander(f"{status_emoji} Run #{run['id']} — {run.get('status', 'N/A')} — {run.get('started_at', '')}"):
                        st.markdown(f"**Workflow ID**: {run.get('workflow_id', 'N/A')}")
                        st.markdown(f"**Status**: {run.get('status', 'N/A')}")
                        st.markdown(f"**Avviato**: {run.get('started_at', 'N/A')}")
                        st.markdown(f"**Completato**: {run.get('finished_at', 'N/A')}")
                        if run.get('result'):
                            st.text_area("Risultato", run['result'], height=150,
                                         key=f"run_result_{run['id']}")





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
            if sys.platform.startswith("win"):
                output = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"]).decode()
                return str(pid) in output
            else:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return False
                return True
        except Exception:
            return False

    def toggle_bot():
        if is_bot_running():
            # Stop bot
            with open(bot_pid_file, 'r') as f:
                pid = int(f.read().strip())
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                import signal
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            if os.path.exists(bot_pid_file):
                os.remove(bot_pid_file)
        else:
            # Start bot
            # Force UTF-8 environment for the bot process
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            with open("bot.log", "w", encoding="utf-8") as log_file:
                popen_kwargs = {
                    "stdout": log_file,
                    "stderr": subprocess.STDOUT,
                    "env": env
                }
                if sys.platform.startswith("win"):
                    popen_kwargs["creationflags"] = 0x08000000 # CREATE_NO_WINDOW
                p = subprocess.Popen(
                    [sys.executable, "bot.py"], 
                    **popen_kwargs
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
        ### 🤖 What is Alfredo?
        **Alfredo** is an AI Agentic Orchestrator powered by **CrewAI** and **Master AI**. It allows you to model custom teams of AI agents, organize them into sequential workflows, and execute them dynamically through natural language (via Streamlit or Telegram).

        ### ⚙️ How It Works
        1. **Define Team**: Register models and create agents with unique roles, backstories, and tools in **Agent Caserma**.
        2. **Build Workflows**: Build individual tasks and link them sequentially in **Workflow Assembler**.
        3. **Dynamic Planning**: Send a request. **Master AI** analyzes your intent, selects the workflow, asks for required inputs, and configures the agents.
        4. **Execute**: The crew runs the tasks step-by-step, feeding results from one task to the next.

        ---

        ### 👤 Agent Specialization
        Keep agents generic (e.g. *Researcher*) and specialize them per-task:
        - **`{specialization}`**: Place this in the agent's **Role** or **Backstory** (e.g. `Researcher specialized in {specialization}`). Alfredo will inject the task's custom specialization at runtime.
          *Note: If omitted, the task specialization is automatically appended.*

        ### 📝 Task Inputs
        Format task **Descriptions** or **Expected Outputs** with:
        - **`{variable_name}`**: Define custom parameters in the task's **Required Inputs**. Alfredo will prompt you for them before execution.
          *Tip: Identical variables across tasks are requested only once!*
        - **`{user_input}`**: Inserts your initial message that triggered the workflow.
        - **`{previous_result}`** (or **`{context}`**): Inserts the output of the preceding task.

        ---
        ### 🛠️ Model & Tool Compatibility
        - ✅ **Cloud Models** (OpenAI, Gemini, Anthropic, Groq): Full support for function calling and tools (web search, file access, shell commands).
        - ❌ **Local Models** (Ollama): Do not support tool calling. Tools are automatically disabled for local models.

        *Alfredo resolves all parameters dynamically during planning and execution.*
        """)
        st.divider()
        st.info("The configuration is saved directly to your SQLite database.")

    # Initialize session state for editing
    if 'editing_task_id' not in st.session_state:
        st.session_state.editing_task_id = None
    if 'editing_agent_id' not in st.session_state:
        st.session_state.editing_agent_id = None
    if 'editing_workflow_id' not in st.session_state:
        st.session_state.editing_workflow_id = None

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "Tab 1: Add Database",
        "Tab 2: API Vault & Model Registry",
        "Tab 3: Agent Caserma",
        "Tab 4: Task Builder",
        "Tab 5: Workflow Assembler",
        "Tab 6: History & Monitoring",
        "Tab 7: 🔗 Le mie App"
    ])

    with tab1:
        try:
            render_knowledge_base()
        except Exception as e:
            st.error(f"Errore nel caricamento del Database: {e}")
            st.exception(e)

    with tab2:
        try:
            render_api_vault()
        except Exception as e:
            st.error(f"Errore nel caricamento di API Vault & Model Registry: {e}")
            st.exception(e)

    with tab3:
        try:
            render_agent_caserma()
        except Exception as e:
            st.error(f"Errore nel caricamento di Agent Caserma: {e}")
            st.exception(e)
    
    with tab4:
        try:
            render_task_builder()
        except Exception as e:
            st.error(f"Errore nel caricamento di Task Builder: {e}")
            st.exception(e)

    with tab5:
        try:
            render_workflow_assembler()
        except Exception as e:
            st.error(f"Errore nel caricamento di Workflow Assembler: {e}")
            st.exception(e)

    with tab6:
        try:
            render_history_monitoring()
        except Exception as e:
            st.error(f"Errore nel caricamento di History & Monitoring: {e}")
            st.exception(e)

    with tab7:
        try:
            render_my_apps()
        except Exception as e:
            st.error(f"Errore nel caricamento di Le mie App: {e}")
            st.exception(e)


if __name__ == "__main__":
    main()