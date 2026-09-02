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
from core.schema_loader import get_available_schemas, get_schema_class
from core.workflow_graph import (
    NODE_TYPE_BATCH,
    NODE_TYPE_EXPORT,
    NODE_TYPE_HITL,
    NODE_TYPE_INPUT,
    NODE_TYPE_TASK,
    canonicalize_graph,
    graph_to_dot,
    new_node_id,
    normalize_graph,
    remove_node,
    sync_header_from_export_nodes,
    validate_graph,
)

def safe_set_key(env_path, key_to_set, value_to_set):
    """
    In-place replacement of dotenv's set_key.
    Docker bind-mounts individual files via inode. os.replace (used by python-dotenv)
    changes the inode, throwing OSError 16 Device or resource busy.
    This function modifies the file in-place preserving the inode.
    """
    if not os.path.exists(env_path):
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(f'{key_to_set}="{value_to_set}"\n')
        os.environ[key_to_set] = str(value_to_set)
        return

    with open(env_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    key_found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key_to_set}="):
            lines[i] = f'{key_to_set}="{value_to_set}"\n'
            key_found = True
            break
            
    if not key_found:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append(f'{key_to_set}="{value_to_set}"\n')
        
    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
        
    os.environ[key_to_set] = str(value_to_set)


def sync_fetched_local_models(db, provider, chat_models, detailed=None, env_var_name=""):
    """Add fetched local runtime models to the registry without deleting existing ones."""
    from core.api_verifier import estimate_supports_tools_from_model_id

    existing = {
        m["model_name"]
        for m in db.read_all_models()
        if (m.get("provider") or "").lower() == provider.lower()
    }
    size_map = {m["name"]: float(m.get("size_gb") or 0) for m in (detailed or [])}
    added = 0
    for name in chat_models or []:
        if not name or name in existing:
            continue
        try:
            db.create_model(
                provider,
                name,
                env_var_name,
                True,
                vram_gb=size_map.get(name) or 0.0,
                supports_tools=estimate_supports_tools_from_model_id(name, provider),
            )
            added += 1
        except Exception:
            continue
    return added


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
    name = name.strip().replace(' ', '_')
    name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    return name or "unnamed_workflow"

def _humanize_field_name(name: str) -> str:
    return str(name).replace("_", " ").strip().capitalize()

def _format_status_cell(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)

def render_status_table(title: str, data, *, kind: str = "success", extra_rows: dict | None = None):
    """Render dict payloads as a status banner plus a key/value table."""
    banner_fn = getattr(st, kind, st.info)
    if not isinstance(data, dict):
        banner_fn(f"{title}: {data}")
        return

    merged = dict(data)
    if extra_rows:
        merged.update(extra_rows)

    rows = [
        {"Field": _humanize_field_name(key), "Value": _format_status_cell(value)}
        for key, value in merged.items()
    ]

    banner_fn(title)
    if rows:
        st.dataframe(
            rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Field": st.column_config.TextColumn("Field", width="medium"),
                "Value": st.column_config.TextColumn("Value", width="large"),
            },
        )

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
                            if db_config.get('use_intelligent_chunking', False):
                                st.caption(f"Provider: **{vdb['provider']}** | Model: **{vdb['model_name']}**\n\n🧠 **Intelligent Markdown Chunking** Enabled")
                            else:
                                st.caption(f"Provider: **{vdb['provider']}** | Model: **{vdb['model_name']}**\n\nChunk Size: {db_config.get('chunk_size', 'N/A')} | Overlap: {db_config.get('chunk_overlap', 'N/A')}")
                        else:
                            st.caption(f"Provider: **{vdb['provider']}** | Model: **{vdb['model_name']}**")
                            
                        if st.button("Delete", key=f"del_vdb_{vdb['id']}", type="primary", use_container_width=True):
                            if folder_exists:
                                vm.delete_database(vdb['name'])
                            db.delete_vector_db(vdb['id'])
                            
                            # Close the modal/query if the deleted database is currently open
                            if "manage_vdb" in st.session_state and st.session_state.manage_vdb['id'] == vdb['id']:
                                del st.session_state.manage_vdb
                            if "query_vdb" in st.session_state and st.session_state.query_vdb['id'] == vdb['id']:
                                del st.session_state.query_vdb
                                
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
                    
                    if len(files_dict['structured']) > 50:
                        st.warning(f"There are {len(files_dict['structured'])} tables. Showing a dropdown for removal to prevent UI lag.")
                        col_sel, col_btn = st.columns([5, 1.2])
                        with col_sel:
                            selected_file = st.selectbox("Select table to remove", files_dict['structured'])
                        with col_btn:
                            if st.button("🗑️ Remove", key=f"rm_struct_bulk_{vdb['id']}", type="secondary", use_container_width=True):
                                success = vm.remove_file_from_database(
                                    db_path=vdb['path'],
                                    provider=vdb['provider'],
                                    model_name=vdb['model_name'],
                                    file_type='structured',
                                    file_identifier=selected_file
                                )
                                if success:
                                    st.toast(f"Removed structured table '{selected_file}'", icon="✅")
                                    st.rerun()
                                else:
                                    st.error(f"Could not remove '{selected_file}'")
                    else:
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
                    if len(filtered_vectorized) > 50:
                        st.warning(f"There are {len(filtered_vectorized)} documents. Showing a dropdown for removal to prevent UI lag.")
                        col_sel, col_btn = st.columns([5, 1.2])
                        with col_sel:
                            selected_vfile = st.selectbox("Select document to remove", filtered_vectorized, key=f"sel_vect_{vdb['id']}")
                        with col_btn:
                            if st.button("🗑️ Remove", key=f"rm_vect_bulk_{vdb['id']}", type="secondary", use_container_width=True):
                                success = vm.remove_file_from_database(
                                    db_path=vdb['path'],
                                    provider=vdb['provider'],
                                    model_name=vdb['model_name'],
                                    file_type='vectorized',
                                    file_identifier=selected_vfile
                                )
                                if success:
                                    st.toast(f"Removed vectorized file '{selected_vfile}'", icon="✅")
                                    st.rerun()
                                else:
                                    st.error(f"Could not remove '{selected_vfile}'")
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
            if db_config.get('use_intelligent_chunking', False):
                st.caption(f"Files will be vectorized using **{vdb['provider']} / {vdb['model_name']}** with **🧠 Intelligent Markdown Chunking**.")
            else:
                st.caption(f"Files will be vectorized using **{vdb['provider']} / {vdb['model_name']}** with Chunk Size: **{db_config.get('chunk_size', 'N/A')}** and Overlap: **{db_config.get('chunk_overlap', 'N/A')}**.")
            
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

        with st.container(border=True):
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
            ui_mem = {}
            memory_path = os.path.join("storage", "ui_memory.json")
            if os.path.exists(memory_path):
                try:
                    import json
                    with open(memory_path, "r", encoding="utf-8") as f:
                        ui_mem = json.load(f)
                except:
                    pass

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
            available_embedding_models["Local (Ollama) / bge-m3"] = {"provider": "ollama", "model_name": "bge-m3"}
            available_embedding_models["Local (Ollama) / snowflake-arctic-embed"] = {"provider": "ollama", "model_name": "snowflake-arctic-embed"}
            available_embedding_models["Local (Ollama) / all-minilm"] = {"provider": "ollama", "model_name": "all-minilm"}
                
            available_embedding_models["Other (Manual Input)"] = {"provider": "custom", "model_name": "custom"}
                
            opts = list(available_embedding_models.keys())
            default_emb = ui_mem.get("embedding_model", opts[0] if opts else "")
            try:
                emb_idx = opts.index(default_emb)
            except ValueError:
                emb_idx = 0
                
            selected_model_str = st.selectbox("Select Embedding Model", options=opts, index=emb_idx)
            
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
                
                use_intelligent_chunking = st.checkbox("🧠 Use Intelligent Markdown Chunking", value=True, help="When enabled, respects Markdown headers and tables during chunking. Recommended for Scientific RAG or .md files.")
            # --- Scientific RAG Parameters ---
            scientific_mode = st.checkbox("🧪 Enable Scientific RAG (Advanced Agentic Ingestion)", help="Use VLMs to extract and describe graphs, tables, and P&ID drawings intelligently.")
            scientific_config = {}
            
            # Check setup status
            marker_setup_done = False
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base_dir, "config", "advanced_rag_setup.json")
            if os.path.exists(config_path):
                try:
                    import json
                    with open(config_path, "r") as f:
                        cfg = json.load(f)
                        marker_setup_done = cfg.get("marker_ready", False)
                except Exception:
                    pass

            if scientific_mode:
                if not marker_setup_done:
                    st.info("🚀 **Advanced RAG Setup (Marker GPU & Qdrant)**\n\n"
                            "Marker provides lightning-fast GPU-accelerated PDF parsing, while Qdrant provides a high-performance scalable vector storage.\n"
                            "These require a one-time setup (downloading PyTorch CUDA and Docker Qdrant).")
                    if st.button("Download & Setup Dependencies"):
                        with st.spinner("Setting up Marker and Qdrant... (this may take several minutes)"):
                            import subprocess
                            import sys
                            setup_script = os.path.join(base_dir, "tools", "setup_advanced_rag.py")
                            try:
                                subprocess.run([sys.executable, setup_script], check=True)
                                st.success("Setup complete! Please interact with the app to refresh.")
                                st.rerun()
                            except subprocess.CalledProcessError:
                                st.error("Setup failed. Check the console for details.")
                    scientific_config["parser"] = "pdfplumber"
                    scientific_config["vectordb"] = "chroma"
                else:
                    st.success("✅ Marker GPU and Qdrant are ready.")
                    col_p, col_v = st.columns(2)
                    with col_p:
                        parser_choice = st.selectbox(
                            "📄 PDF Parser", 
                            [
                                "Marker (GPU Accelerated)", 
                                "Mistral OCR 4 (Cloud API)",
                                "Marker + VLM (Maximum Quality)", 
                                "pdfplumber (Fallback)"
                            ], 
                            index=0,
                            help="Pricing info: Mistral OCR 4 is ~4$/1000 pages. Google Document AI is ~5$/1000 pages."
                        )
                    with col_v:
                        vectordb_choice = st.selectbox("🗄️ Vector Database", ["Qdrant (Scalable Server)", "Chroma (Local File)"], index=0)
                        
                    scientific_config["vectordb"] = "qdrant" if vectordb_choice.startswith("Qdrant") else "chroma"
                    
                    if parser_choice.startswith("Marker + VLM"):
                        scientific_config["parser"] = "marker_vlm"
                    elif parser_choice.startswith("Mistral OCR"):
                        scientific_config["parser"] = "mistral_ocr"
                    elif parser_choice.startswith("Marker"):
                        scientific_config["parser"] = "marker"
                    else:
                        scientific_config["parser"] = "pdfplumber"

                st.markdown("Configure specific **Vision-capable** models for scientific parsing:")
                
                # ── Curated Vision Model Pools (role-specific) ──
                # Each pool is ordered by recommendation strength for that task.
                # Only models with proven vision/multimodal capabilities are listed.
                
                # GRAPHS: Need strong spatial reasoning + coordinate estimation
                graph_models_pool = [
                    # Cloud (best accuracy for digitizing curves)
                    "google/gemini-2.5-pro",       # Best spatial reasoning
                    "google/gemini-2.0-flash",      # Fast, good accuracy
                    "google/gemini-1.5-pro",        # Proven strong on charts
                    "openai/gpt-4o",                # Excellent coordinate estimation
                    "openai/gpt-4o-mini",           # Lighter but capable
                    "anthropic/claude-3-5-sonnet-20240620",
                    "mistral/pixtral-large-latest",
                    "mistral/pixtral-12b-2409",
                    # Local (16GB friendly)
                    "ollama/qwen3-vl:8b",           # SOTA local VLM, best for charts
                    "ollama/gemma3:12b",            # Strong chart/document analysis
                    "ollama/pixtral:12b",           # Mistral, native resolution for fine details
                    "ollama/granite3.2-vision",     # IBM, good on technical charts
                    "ollama/qwen2.5-vl:7b",         # Proven OCR + chart reading
                    "ollama/minicpm-v",             # Lightweight, decent on graphs
                ]
                
                # TABLES: Need strong OCR + structural formatting
                table_models_pool = [
                    # Cloud
                    "google/gemini-2.0-flash",      # Fast OCR, excellent formatting
                    "google/gemini-1.5-flash",      # Very fast, good table OCR
                    "google/gemini-2.5-pro",        # Best accuracy
                    "openai/gpt-4o",
                    "anthropic/claude-3-5-sonnet-20240620",  # Excellent Markdown output
                    "mistral/pixtral-large-latest",
                    "mistral/pixtral-12b-2409",
                    # Local (16GB friendly — specialized)
                    "ollama/glm-ocr",               # 0.9B, SOTA table/OCR specialist
                    "ollama/granite3.2-vision",     # IBM, surgical on table extraction
                    "ollama/qwen3-vl:8b",           # All-round strong
                    "ollama/qwen2.5-vl:7b",
                    "ollama/minicpm-v",
                ]
                
                # DRAWINGS (P&ID, PFD, schematics): Need strong spatial + engineering reasoning
                drawing_models_pool = [
                    # Cloud
                    "google/gemini-2.5-pro",        # Best complex schematic reasoning
                    "google/gemini-1.5-pro",        # Proven on P&ID
                    "google/gemini-2.0-flash",
                    "openai/gpt-4o",                # Strong engineering understanding
                    "anthropic/claude-3-5-sonnet-20240620",
                    "mistral/pixtral-large-latest",
                    "mistral/pixtral-12b-2409",
                    # Local (16GB friendly)
                    "ollama/qwen3-vl:8b",           # Best local for complex images
                    "ollama/pixtral:12b",           # Mistral, native resolution captures tiny P&ID text
                    "ollama/gemma3:12b",            # Good spatial reasoning
                    "ollama/llama3.2-vision:11b",   # Meta, solid general vision
                    "ollama/qwen2.5-vl:7b",
                    "ollama/minicpm-v",
                ]
                
                def _filter_available(pool):
                    """Filter a pool to only include models whose provider API is active."""
                    result = []
                    for m in pool:
                        if m.startswith("google/") and (current_env.get("GEMINI_API_KEY") or current_env.get("GOOGLE_API_KEY")):
                            result.append(m)
                        elif m.startswith("openai/") and current_env.get("OPENAI_API_KEY"):
                            result.append(m)
                        elif m.startswith("anthropic/") and current_env.get("ANTHROPIC_API_KEY"):
                            result.append(m)
                        elif m.startswith("mistral/") and current_env.get("MISTRAL_API_KEY"):
                            result.append(m)
                        elif m.startswith("ollama/"):
                            result.append(m)  # Local models are always available
                    result.append("Other (Manual Input)")
                    return result
                
                avail_graph = _filter_available(graph_models_pool)
                avail_table = _filter_available(table_models_pool)
                avail_drawing = _filter_available(drawing_models_pool)
                
                def get_model_selection(label, key, default_hint, options):
                    default_val = ui_mem.get(key, options[0] if options else "")
                    try:
                        idx = options.index(default_val)
                    except ValueError:
                        idx = 0
                    sel = st.selectbox(label, options=options, index=idx, key=f"sel_{key}", help=default_hint)
                    if sel == "Other (Manual Input)":
                        return st.text_input(f"Custom model (provider/model)", key=f"cust_{key}")
                    return sel

                sc_col1, sc_col2 = st.columns(2)
                with sc_col1:
                    graph_model = get_model_selection(
                        "📈 Model for Graphs (Curve Digitizer)", "graphs", 
                        "Extracts (X,Y) coordinates from curves. Best cloud: Gemini 2.5 Pro. Best local: qwen3-vl:8b, llama3.2-vision:11b, or granite3.2-vision. (DO NOT use text-only models like gemma3).",
                        avail_graph
                    )
                    table_model = get_model_selection(
                        "📊 Model for Tables (OCR → Markdown)", "tables", 
                        "Converts table images to structured Markdown/CSV. Best cloud: Gemini 2.0 Flash. Best local: glm-ocr, qwen3-vl:8b, or granite3.2-vision. (DO NOT use text-only models).",
                        avail_table
                    )
                with sc_col2:
                    drawing_model = get_model_selection(
                        "🏭 Model for Drawings (P&ID / PFD / Schematics)", "drawings", 
                        "Describes technical schematics. Best cloud: Gemini 2.5 Pro. Best local: qwen3-vl:8b or llama3.2-vision:11b. (DO NOT use text-only models).",
                        avail_drawing
                    )
                    default_gp = ui_mem.get("graph_points", 10)
                    graph_points = st.number_input("📐 Graph Extraction Points", min_value=3, max_value=100, value=default_gp, help="How many (X,Y) data points to extract from each curve.")
                
                scientific_config["models_config"] = {
                    "graphs": graph_model,
                    "tables": table_model,
                    "drawings": drawing_model
                }
                scientific_config["graph_points"] = graph_points
            
            submitted = st.button("Start Embedding", type="primary")
            if submitted:
                # Save models to memory
                try:
                    import json
                    ui_mem["embedding_model"] = selected_model_str
                    if scientific_mode:
                        ui_mem["graphs"] = graph_model
                        ui_mem["tables"] = table_model
                        ui_mem["drawings"] = drawing_model
                        ui_mem["graph_points"] = graph_points
                    os.makedirs("storage", exist_ok=True)
                    with open(memory_path, "w", encoding="utf-8") as f:
                        json.dump(ui_mem, f, indent=4)
                except Exception:
                    pass

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
                                progress_callback=ui_progress_callback,
                                scientific_mode=scientific_mode,
                                scientific_config=scientific_config,
                                use_intelligent_chunking=use_intelligent_chunking
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

    # Merge OS environment variables (which Docker Compose populates) with the local .env file
    current_env = {**os.environ, **dotenv_values(env_path)}
    
    suggested_keys = [
        "OPENAI_API_KEY", 
        "GROQ_API_KEY", 
        "ANTHROPIC_API_KEY", 
        "GEMINI_API_KEY",
        "OLLAMA_API_KEY",
        "LMSTUDIO_API_KEY",
        "MISTRAL_API_KEY"
    ]
    
    # To prevent displaying internal Docker OS variables in the UI, we only list keys from the file + suggestions
    file_keys = list(dotenv_values(env_path).keys())
    all_env_keys = sorted([
        k for k in set(suggested_keys + file_keys) 
        if k and str(k).replace('\ufeff', '').strip() and "TELEGRAM" not in k.upper()
    ])
    
    # Categorize keys dynamically
    llm_keywords = ["OPENAI", "GROQ", "ANTHROPIC", "GEMINI", "OLLAMA", "LMSTUDIO", "LM_STUDIO", "MISTRAL"]
    llm_keys = [k for k in all_env_keys if any(kw in k for kw in llm_keywords) and k not in ("OLLAMA_API_BASE", "LMSTUDIO_API_BASE", "LM_STUDIO_API_BASE", "LMSTUDIO_MODELS_DIR")]
    
    connection_suffixes = ["_API_KEY", "_DB_URL", "_TOKEN", "_PASSWORD"]
    conn_keys = [k for k in all_env_keys if k not in llm_keys and any(k.endswith(s) for s in connection_suffixes)]
    
    agent_keys = ["MASTER_AI_MODEL_NAME", "DEFAULT_AGENT_MODEL_NAME"]
    headroom_keys = ["HEADROOM_ENABLED", "HEADROOM_PROXY_URL"]
    system_keys = [k for k in all_env_keys if k not in llm_keys and k not in conn_keys and k not in agent_keys and k not in headroom_keys]
    
    # Tooltip definitions for system/conn keys
    key_tooltips = {
        "ERP_API_KEY": "Password to connect to the external business management system (e.g. Biomass App).",
        "ERP_DB_URL": "Address to access the external app database (e.g. Biomass DB).",
        "MASTER_AI_MODEL_NAME": "The main AI brain Alfredo uses for everyday tasks.",
        "OLLAMA_API_BASE": "The URL of the local or remote Ollama server (e.g. http://192.168.178.105:11434).",
        "LMSTUDIO_API_BASE": "The URL of the LM Studio / Bionic local server (e.g. http://127.0.0.1:1234).",
        "LMSTUDIO_API_KEY": "Credential checked against the LM Studio server (dummy lm-studio is fine locally).",
        "LMSTUDIO_MODELS_DIR": "This computer's GGUF folder (e.g. C:\\Users\\<you>\\.lmstudio\\models).",
    }
    
    tab_sys, tab_models = st.tabs(["System Setting", "Load Models"])

    with tab_sys:
        # 1. System & Integration Settings
        st.subheader("System Settings")
        st.markdown("Database, Headroom, and other global configuration variables.")
        
        with st.expander("View System Settings", expanded=False):
            # Headroom AI — Context Compression
            st.markdown("#### Headroom AI")
            st.markdown("Context compression to reduce token usage by 60-95%.")
            
            hr_enabled_raw = str(current_env.get("HEADROOM_ENABLED", ""))
            hr_enabled = hr_enabled_raw.strip('"\'').lower() == "true"
            hr_proxy = str(current_env.get("HEADROOM_PROXY_URL", "")).strip('"\'').strip()
            
            h_col1, h_col2 = st.columns(2)
            with h_col1:
                st.markdown(f"{'🟢' if hr_enabled else '🔴'} **HEADROOM_ENABLED**")
                st.caption("Active" if hr_enabled else "Inactive")
            with h_col2:
                if hr_enabled and hr_proxy:
                    st.markdown("🟢 **HEADROOM_PROXY_URL**")
                    st.caption(f"Proxy Mode: {hr_proxy}")
                elif hr_enabled:
                    st.markdown("🟡 **HEADROOM_PROXY_URL**")
                    st.caption("Inline Mode (Proxy not set)")
                else:
                    st.markdown("🔴 **HEADROOM_PROXY_URL**")
                    st.caption("Inactive")
                    
            st.divider()

            col3, col4 = st.columns(2)
            for i, key in enumerate(system_keys):
                val = current_env.get(key)
                is_set = bool(val and str(val).strip())
                status_icon = "🟢" if is_set else "🔴"
                tooltip = key_tooltips.get(key, "System configuration parameter.")
                
                with (col3 if i % 2 == 0 else col4):
                    # Using HTML title attribute to create a native tooltip
                    st.markdown(f'<span title="{tooltip}" style="cursor: help;">{status_icon} <b>{key}</b></span>', unsafe_allow_html=True)
                    
        st.divider()
        
        # --- Main Agent (Master AI) Configuration Row ---
        st.markdown("### 🧠 Global Default Models")
        
        models = db.read_all_models()
        model_names = []
        model_ids = []
        model_options = {}
        if models:
            model_options = {f"{m['provider']} / {m['model_name']}": m['id'] for m in models}
            model_names = list(model_options.keys())
            model_ids = list(model_options.values())
        
        agent_tooltips = {
            "MASTER_AI_MODEL_NAME": "Routes incoming requests and acts as the main system orchestrator.",
            "DEFAULT_AGENT_MODEL_NAME": "Automatically used by any agent if you don't explicitly assign a model for a specific task."
        }
        
        st.markdown("**Configured AI Default Variables**")
        col_env1, col_env2 = st.columns(2)
        for idx, env_var in enumerate(["MASTER_AI_MODEL_NAME", "DEFAULT_AGENT_MODEL_NAME"]):
            is_set = env_var in current_env and bool(str(current_env.get(env_var, "")).strip())
            icon = "🟢" if is_set else "🔴"
            tooltip = agent_tooltips.get(env_var, "")
            with [col_env1, col_env2][idx]:
                st.markdown(f'<span title="{tooltip}" style="cursor: help;">{icon} <b>{env_var}</b></span>', unsafe_allow_html=True)
        st.write("") # Spacer

        current_master_model_name = current_env.get("MASTER_AI_MODEL_NAME", "")
        
        default_master_index = 0
        if current_master_model_name:
            for idx, name_str in enumerate(model_names):
                if current_master_model_name in name_str:
                    default_master_index = idx
                    break
            
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
                if selected_master_model_str:
                    chosen_model_name = selected_master_model_str.split(" / ", 1)[-1]
                    safe_set_key(env_path, "MASTER_AI_MODEL_NAME", chosen_model_name)
                    st.toast("Master AI Model saved successfully!", icon="✅")
                    st.rerun()
                
        # Read current Default Agent model selection from .env
        current_default_agent_model_name = current_env.get("DEFAULT_AGENT_MODEL_NAME", "")
        default_agent_index = 0
        if current_default_agent_model_name:
            for idx, name_str in enumerate(model_names):
                if current_default_agent_model_name in name_str:
                    default_agent_index = idx
                    break
            
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
                if selected_default_agent_model_str:
                    chosen_agent_model_name = selected_default_agent_model_str.split(" / ", 1)[-1]
                    safe_set_key(env_path, "DEFAULT_AGENT_MODEL_NAME", chosen_agent_model_name)
                    st.toast("Default Agent Model saved successfully!", icon="✅")
                    st.rerun()
                
        st.divider()
        st.subheader("Global Resource Settings")
        st.markdown("Configure limits for parallel agent execution based on your hardware.")
        with st.form("resource_limits_form"):
            current_max_vram = current_env.get("MAX_VRAM_GB", "16.0")
            try:
                current_max_vram_float = float(current_max_vram)
            except ValueError:
                current_max_vram_float = 16.0
                
            new_max_vram = st.number_input("Maximum Local VRAM (GB)", min_value=1.0, max_value=256.0, value=current_max_vram_float, step=1.0, help="Used by the Task Scheduler to pause or downgrade agents when VRAM is full.")
            submitted_resources = st.form_submit_button("Save Resource Settings")
            if submitted_resources:
                safe_set_key(env_path, "MAX_VRAM_GB", str(new_max_vram))
                st.success("Resource settings saved.")
                st.rerun()

    with tab_models:
        # 2. LLM Provider API Keys
        st.subheader("LLM Provider API Keys")
        st.markdown("Monitor and manage API keys for AI models.")
        
        col1, col2 = st.columns(2)
        for i, key in enumerate(llm_keys):
            is_set = key in current_env and bool(current_env[key].strip())
            status_icon = "🟢" if is_set else "🔴"
            with (col1 if i % 2 == 0 else col2):
                st.markdown(f"{status_icon} **{key}**")
                
        st.divider()
                
        st.subheader("Load cloud models")
        st.markdown(
            "Add or update an API Key for **cloud** providers (Gemini, OpenAI, …). "
            "For **Ollama**, do not paste the URL here as a key — use **Configure Local Providers** below, "
            "or paste `http://192.168.178.105:11434` and Alfredo will save it as Base URL with an empty key."
        )
        form_col1, form_col2 = st.columns(2)
        with form_col1:
            selected_key = st.selectbox("Select Key", options=["Custom..."] + suggested_keys)
            custom_key = st.text_input("Custom Key Name (if selected)", placeholder="e.g. MIO_MODELLO_KEY")
        with form_col2:
            key_value = st.text_input("API Key Value", type="password", placeholder="Enter key here...")
            
        submitted_key = st.button("Save to .env", type="primary", key="save_cloud_model_btn")
        if submitted_key:
            final_key_name = custom_key.strip() if selected_key == "Custom..." else selected_key
            if final_key_name:
                final_key_name = final_key_name.upper().replace(' ', '_')
                raw_value = (key_value or "").strip()
                looks_like_url = raw_value.lower().startswith("http://") or raw_value.lower().startswith("https://")
                # Local Ollama is reached by URL, not by a secret. Pasting the URL as "API key" is the usual mix-up.
                if "OLLAMA" in final_key_name and looks_like_url:
                    safe_set_key(env_path, "OLLAMA_API_BASE", raw_value.rstrip("/"))
                    safe_set_key(env_path, "OLLAMA_API_KEY", "")
                    from core.api_verifier import _fetch_ollama
                    with st.spinner("Checking Ollama at that URL (no API key needed)..."):
                        result = _fetch_ollama(api_key="", base_url_override=raw_value.rstrip("/"))
                    if result.get("success"):
                        added = sync_fetched_local_models(
                            db, "Ollama", result.get("chat_models", []),
                            result.get("chat_models_detailed", []), env_var_name="",
                        )
                        st.success(
                            f"That value is the Ollama Base URL, not a key. Saved as OLLAMA_API_BASE. "
                            f"Found {len(result.get('chat_models', []))} local models"
                            + (f", {added} added to the registry." if added else ".")
                        )
                        st.rerun()
                    else:
                        st.error(f"Ollama URL check failed: {result.get('error')}")
                elif "TELEGRAM" in final_key_name:
                    st.error("Telegram bot tokens must be managed in the Telegram Bot Config at the top right, not here.")
                elif not raw_value:
                    st.error("Please provide both a valid Key Name and a Value.")
                else:
                    from core.api_verifier import verify_and_fetch_models
                    with st.spinner("Verifying API Key and fetching models..."):
                        result = verify_and_fetch_models(final_key_name, key_value.strip())
                        
                    if not result.get("success"):
                        st.error(f"Verification failed: {result.get('error')}")
                    else:
                        safe_set_key(env_path, final_key_name, key_value.strip())
                        
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
                            elif "LMSTUDIO" in final_key_name or "LM_STUDIO" in final_key_name: prov_name = "LMStudio"
                            elif "MISTRAL" in final_key_name: prov_name = "Mistral"
                            
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
                        
                        from core.api_verifier import estimate_supports_tools_from_model_id

                        # Add new models
                        for m_name in fetched_chat_models:
                            if m_name not in existing_model_names:
                                is_local_sync = prov in ("Ollama", "LMStudio")
                                db.create_model(
                                    prov,
                                    m_name,
                                    final_key_name,
                                    is_local_sync,
                                    supports_tools=estimate_supports_tools_from_model_id(m_name, prov) if is_local_sync else True,
                                )
                                
                        # Remove deleted models
                        for m_name, m_id in existing_model_names.items():
                            if m_name not in fetched_chat_models:
                                db.delete_model(m_id)
                                
                        st.success(f"Key '{final_key_name}' verified and models synced securely!")
                        st.rerun()
            else:
                st.error("Please provide both a valid Key Name and a Value.")

        st.divider()

        st.subheader("Local Model registry")
        
        # --- Configure Local Provider ---
        with st.expander("🔌 Configure Local Providers (Ollama / LM Studio)", expanded=False):
            env_path = os.path.join(os.getcwd(), '.env')
            tab_ol, tab_lms, tab_import = st.tabs(["Ollama", "LM Studio / Bionic", "Import GGUF"])

            with tab_ol:
                st.caption(
                    "Ollama on this PC is reached at **http://192.168.178.105:11434** — that is the Base URL, not an API key. "
                    "Leave **Ollama API Key empty**. Put the URL only in **Ollama Base URL**, then Connect."
                )
                current_url = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
                current_key = os.getenv("OLLAMA_API_KEY", "")

                new_url = st.text_input("Ollama Base URL", value=current_url, help="The IP and port of your Ollama server (e.g., http://192.168.178.105:11434/). From Docker use http://host.docker.internal:11434")
                new_key = st.text_input("Ollama API Key (Optional)", value=current_key, type="password")

                if st.button("Connect & Refresh Ollama Models"):
                    safe_set_key(env_path, "OLLAMA_API_BASE", new_url)
                    safe_set_key(env_path, "OLLAMA_API_KEY", new_key)
                    from core.api_verifier import _fetch_ollama
                    res = _fetch_ollama(api_key=new_key, base_url_override=new_url)
                    if res.get("success"):
                        added = sync_fetched_local_models(
                            db, "Ollama", res.get("chat_models", []),
                            res.get("chat_models_detailed", []), env_var_name="",
                        )
                        st.success(
                            f"Ollama verified at {new_url}. "
                            f"Found {len(res.get('chat_models', []))} models"
                            + (f", {added} added to registry." if added else ".")
                        )
                        import time
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"Ollama credential/URL check failed: {res.get('error')}")

            with tab_lms:
                st.caption(
                    "Same flow as Ollama: save **Base URL** + **API key**, then Connect verifies the credential "
                    "against the local server and syncs models. Default GGUF folder is `~/.lmstudio/models` on this PC; "
                    "another computer pastes its own path. Start **LM Studio → Developer → Local Server** first."
                )
                current_lms_url = os.getenv("LMSTUDIO_API_BASE") or os.getenv("LM_STUDIO_API_BASE") or "http://127.0.0.1:1234"
                current_lms_key = os.getenv("LMSTUDIO_API_KEY") or "lm-studio"
                new_lms_url = st.text_input(
                    "LM Studio Base URL",
                    value=current_lms_url,
                    help="Equivalent to Ollama Base URL. Local: http://127.0.0.1:1234. Docker: http://host.docker.internal:1234",
                )
                new_lms_key = st.text_input(
                    "LM Studio API Key",
                    value=current_lms_key,
                    type="password",
                    help="Checked like Ollama's API key. LM Studio accepts a dummy such as lm-studio.",
                )
                from core.api_verifier import default_lmstudio_models_dir
                new_lms_dir = st.text_input(
                    "Local GGUF folder (this computer)",
                    value=default_lmstudio_models_dir(),
                    help="Saved as LMSTUDIO_MODELS_DIR. On another PC set that machine's models folder.",
                )
                if st.button("Connect & Refresh LM Studio Models"):
                    if not new_lms_url.strip():
                        st.error("LM Studio Base URL is required.")
                    elif not (new_lms_key or "").strip():
                        st.error("LM Studio API Key is required (use lm-studio if the server has no auth).")
                    else:
                        key_to_save = new_lms_key.strip()
                        safe_set_key(env_path, "LMSTUDIO_API_BASE", new_lms_url.strip())
                        safe_set_key(env_path, "LM_STUDIO_API_BASE", new_lms_url.strip())
                        safe_set_key(env_path, "LMSTUDIO_API_KEY", key_to_save)
                        safe_set_key(env_path, "LMSTUDIO_MODELS_DIR", new_lms_dir)
                        from core.api_verifier import _fetch_lmstudio
                        res = _fetch_lmstudio(api_key=key_to_save, base_url_override=new_lms_url.strip())
                        if res.get("success"):
                            added = sync_fetched_local_models(
                                db, "LMStudio", res.get("chat_models", []),
                                res.get("chat_models_detailed", []),
                                env_var_name="LMSTUDIO_API_KEY",
                            )
                            st.success(
                                f"LM Studio verified at {new_lms_url.strip()}. "
                                f"Found {len(res.get('chat_models', []))} models"
                                + (f", {added} added to registry." if added else ".")
                            )
                            import time
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error(f"LM Studio credential/URL check failed: {res.get('error')}")

            with tab_import:
                st.caption(
                    "Put a quantized GGUF into Ollama without Hugging Face pull. "
                    "For 16 GB VRAM pick a file **under ~15 GB** (your Qwen3.8 27B Q3_K_S is ~12.6 GB)."
                )
                from core.api_verifier import default_lmstudio_models_dir, list_local_gguf_files, import_gguf_to_ollama
                gguf_root = st.text_input(
                    "Folder to scan",
                    value=default_lmstudio_models_dir(),
                    help="Not hardcoded: defaults to this user's ~/.lmstudio/models. Save a custom path for this PC.",
                )
                if st.button("Save GGUF folder for this computer"):
                    safe_set_key(env_path, "LMSTUDIO_MODELS_DIR", gguf_root)
                    st.success("Saved LMSTUDIO_MODELS_DIR in .env")
                    import time
                    time.sleep(0.5)
                    st.rerun()
                gguf_files = list_local_gguf_files(gguf_root)
                fits_vram = [g for g in gguf_files if g.get("size_gb", 0) <= 15.5]
                if not gguf_files:
                    st.info("No .gguf files found in that folder (mmproj files are skipped).")
                else:
                    if fits_vram:
                        st.caption(f"{len(fits_vram)} file(s) fit under 16 GB VRAM.")
                    recommended = next(
                        (g for g in fits_vram if "qwen3.8" in g["name"].lower() and "q3_k_s" in g["name"].lower()),
                        fits_vram[0] if fits_vram else gguf_files[0],
                    )
                    gguf_labels = {
                        f"{g['rel']} ({g['size_gb']} GB"
                        + (" — fits 16GB" if g.get("size_gb", 0) <= 15.5 else " — too large for 16GB")
                        + ")": g
                        for g in gguf_files
                    }
                    default_label = next(
                        (lab for lab, g in gguf_labels.items() if g["path"] == recommended["path"]),
                        list(gguf_labels.keys())[0],
                    )
                    selected_gguf_label = st.selectbox(
                        "GGUF file",
                        options=list(gguf_labels.keys()),
                        index=list(gguf_labels.keys()).index(default_label),
                    )
                    selected_gguf = gguf_labels[selected_gguf_label]
                    if selected_gguf.get("size_gb", 0) > 15.5:
                        st.warning("This quant is larger than 16 GB VRAM. Prefer Q3_K_S / Q4_K_M smaller than 15 GB.")
                    default_import_name = os.path.splitext(selected_gguf["name"])[0].lower().replace(" ", "-")
                    import_name = st.text_input("Ollama model name", value=default_import_name)
                    if st.button("Import into Ollama (no download)"):
                        with st.spinner("Importing GGUF into Ollama (copying blobs, can take several minutes)..."):
                            res = import_gguf_to_ollama(selected_gguf["path"], import_name)
                        if res.get("success"):
                            try:
                                db.create_model(
                                    "Ollama",
                                    res.get("model_name") or import_name,
                                    "",
                                    True,
                                    vram_gb=float(selected_gguf.get("size_gb") or 13.0),
                                )
                            except Exception:
                                pass
                            st.success(res.get("message") or f"Imported as {import_name}")
                            import time
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error(res.get("error"))
        
        # Load model config
        model_map_path = os.path.join(os.getcwd(), 'config', 'models_map.yaml')
        model_config = DataManager.load_yaml(model_map_path)
        PROVIDER_MAP = model_config.get('provider_map', {})
        LOCAL_MODELS = model_config.get('local_models', [])
        
        from core.api_verifier import _fetch_ollama, _fetch_lmstudio, estimate_supports_tools_from_model_id
        ollama_api_key = os.getenv("OLLAMA_API_KEY", "")
        ollama_models = _fetch_ollama(ollama_api_key)
        LOCAL_MODELS_DETAILED = []
        if ollama_models.get("success"):
            LOCAL_MODELS = ollama_models.get("chat_models", [])
            LOCAL_MODELS_DETAILED = ollama_models.get("chat_models_detailed", [])

        lms_models = {"success": False}
        if os.getenv("LMSTUDIO_API_BASE") or os.getenv("LM_STUDIO_API_BASE"):
            lms_models = _fetch_lmstudio()
        LMS_MODELS = lms_models.get("chat_models", []) if lms_models.get("success") else []
        LMS_MODELS_DETAILED = lms_models.get("chat_models_detailed", []) if lms_models.get("success") else []
        
        st.markdown("Add or update a model available for agents.")
        
        env_var_name = ""
        is_local = True
        local_runtime = st.selectbox("Local runtime", ["Ollama", "LM Studio"])
        provider = "Ollama" if local_runtime == "Ollama" else "LMStudio"
        runtime_models = LOCAL_MODELS if provider == "Ollama" else LMS_MODELS
        runtime_detailed = LOCAL_MODELS_DETAILED if provider == "Ollama" else LMS_MODELS_DETAILED
        if provider == "LMStudio" and not runtime_models:
            st.warning("LM Studio server not reachable. Start it in LM Studio → Developer → Local Server, then Connect.")
        
        local_col1, local_col2 = st.columns(2)
        with local_col1:
            def format_model(name):
                if name == "Other (Manual)...": return name
                for m in runtime_detailed:
                    if m["name"] == name:
                        size = m.get("size_gb") or 0
                        return f"{name} ({size} GB)" if size else name
                return name

            selected_local = st.selectbox("Local Model Name", options=runtime_models + ["Other (Manual)..."], format_func=format_model)
            if selected_local == "Other (Manual)...":
                model_name = st.text_input("Type Custom Local Model Name", placeholder="e.g., qwen3.8-27b-tools or llama-3.1-8b-instruct")
            else:
                model_name = selected_local
        with local_col2:
            default_vram = 4.0
            if selected_local != "Other (Manual)...":
                for m in runtime_detailed:
                    if m["name"] == selected_local:
                        default_vram = float(m.get("size_gb") or 4.0) or 4.0
                        break
            vram_gb = st.number_input("VRAM Required (GB)", min_value=0.0, max_value=256.0, value=default_vram, step=0.5, help="Estimated memory used by this model.")
            auto_supports_tools = estimate_supports_tools_from_model_id(model_name or selected_local, provider)
            support_mode = st.selectbox(
                "Tool-calling support",
                options=["Auto", "Yes", "No"],
                index=0,
                help="Auto infers support from runtime and model family. Override to force-enable or disable tools for this model.",
            )
            supports_tools = auto_supports_tools if support_mode == "Auto" else (support_mode == "Yes")
            st.caption(
                f"Auto guess for `{model_name or selected_local}`: "
                f"{'✅ supports tools' if auto_supports_tools else '❌ no tool-calling'}."
            )

        submitted = st.button("Add Local Model", type="primary")
        if submitted and provider and model_name:
            import sqlite3
            try:
                env_for_model = "LMSTUDIO_API_KEY" if provider == "LMStudio" else ""
                db.create_model(
                    provider,
                    model_name,
                    env_for_model,
                    True,
                    vram_gb=vram_gb,
                    supports_tools=supports_tools,
                )
                st.success(
                    f"Added local model '{model_name}' requiring {vram_gb} GB VRAM "
                    f"({'tools ON' if supports_tools else 'tools OFF'})."
                )
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
                        col1, col2, col3, col4, col5, col6, col7 = st.columns([0.5, 2, 2, 1.3, 2, 1.5, 1])
                        type_icon = "🏠" if model.get('is_local') else "☁️"
                        prov_l = (model.get('provider') or "").lower()
                        if prov_l in ("lmstudio", "lm_studio"):
                            env_key = model.get('env_var_name') or "LMSTUDIO_API_KEY"
                            has_key = bool(str(current_env.get(env_key, "")).strip())
                            has_url = bool(str(current_env.get("LMSTUDIO_API_BASE") or current_env.get("LM_STUDIO_API_BASE") or "").strip())
                            status_sema = "🟢" if (has_key or has_url) else "🔴"
                        elif model.get('is_local'):
                            status_sema = "🟢"
                        else:
                            env_key = model.get('env_var_name', '')
                            status_sema = "🟢" if env_key in current_env and bool(current_env[env_key].strip()) else "🔴"
                        col1.markdown(f"{type_icon} {status_sema}")
                        col2.text(f"P: {model['provider']}")
                        col3.text(f"M: {model['model_name']}")
                        tools_enabled = bool(model.get("supports_tools", 1))
                        col4.text(f"Tools: {'✅' if tools_enabled else '❌'}")
                        if prov_l in ("lmstudio", "lm_studio"):
                            key_display = model.get('env_var_name') or "LMSTUDIO_API_KEY"
                        else:
                            key_display = "---" if model.get('is_local') else (model.get('env_var_name') or "N/A")
                        col5.text(f"Key: {key_display}")
                        vram_val = model.get('vram_gb', 0.0)
                        vram_display = f"{vram_val} GB" if model.get('is_local') else "---"
                        col6.text(f"VRAM: {vram_display}")
                        if col7.button("Delete", key=f"del_model_{model['id']}", use_container_width=True):
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
                    st.markdown("**✨ Optimizing Agent...**")
                    try:
                        master_ai = MasterAI()
                        ph = st.empty()
                        full_text = ""
                        import time
                        last_update = 0
                        for chunk in master_ai.optimize_agent_fields_stream(
                            role=current_role,
                            goal=current_goal,
                            backstory=current_backstory
                        ):
                            full_text += chunk
                            if time.time() - last_update > 0.1:
                                ph.code(full_text, language="json")
                                last_update = time.time()
                        ph.code(full_text, language="json")
                        
                        import json, re
                        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", full_text, re.DOTALL | re.IGNORECASE)
                        if match:
                            full_text = match.group(1).strip()
                            
                        try:
                            optimized = json.loads(full_text)
                            st.session_state.optimized_agent_data = {
                                "role": optimized.get("role", current_role),
                                "goal": optimized.get("goal", current_goal),
                                "backstory": optimized.get("backstory", current_backstory)
                            }
                        except json.JSONDecodeError:
                            st.error("AI returned invalid JSON format. Optimization failed.")
                            
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
        "write_python_file": "📝",
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
            
            # Pre-populate human validation
            st.session_state.task_human_validation_cb = bool(editing_task.get('human_validation')) if editing_task else False
            
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
            
            # Pre-populate human validation
            st.session_state.task_human_validation_cb = bool(editing_task.get('human_validation')) if editing_task else False
            
            # Clear old dynamic input keys from session state
            for k in list(st.session_state.keys()):
                if k.startswith("ri_key_") or k.startswith("ri_prompt_"):
                    del st.session_state[k]
            # Reset tool checkboxes to match newly loaded default_tools
            for k in list(st.session_state.keys()):
                if k.startswith("cb_task_"):
                    del st.session_state[k]
            # Pre-check tools that are already assigned to this task
            if default_tools:
                for t in default_tools:
                    st.session_state[f"cb_task_{t}"] = True

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

        # --- Write Tool Instruction Injection ---
        WRITE_TOOLS_MAP = {
            "write_file": "Save the final text output to a file using the 'write_file' tool. Specify a filename in the workspace.",
            "write_python_file": "Save the generated python code to a .py file using the 'write_python_file' tool.",
            "create_word_document": "Generate a well-formatted Word document using the 'create_word_document' tool.",
            "edit_word_document": "Append or edit an existing Word document using the 'edit_word_document' tool.",
            "create_excel_document": "Generate an Excel spreadsheet with the data using the 'create_excel_document' tool."
        }
        
        def on_write_tool_select():
            selected = st.session_state.get("task_write_tool_sel", "None")
            if selected != "None":
                current_out = st.session_state.get("task_output_area", "")
                instruction = WRITE_TOOLS_MAP[selected]
                if instruction not in current_out:
                    append_str = f"\n\n[TOOL INSTRUCTION]: {instruction}"
                    if current_out.strip():
                        st.session_state.task_output_area = current_out.rstrip() + append_str
                    else:
                        st.session_state.task_output_area = instruction
                
                # Auto-enable the tool checkbox
                st.session_state[f"cb_task_{selected}"] = True
                
                # Reset selectbox
                st.session_state.task_write_tool_sel = "None"
                
        st.selectbox(
            "🪄 Quick Action: Assign Write Tool & Inject Instructions", 
            options=["None"] + list(WRITE_TOOLS_MAP.keys()),
            format_func=lambda x: "Select a tool to inject..." if x == "None" else f"{TOOL_EMOJIS.get(x, '🛠️')} {x.replace('_', ' ').title()}",
            key="task_write_tool_sel",
            on_change=on_write_tool_select,
            help="Select a write tool to automatically add its usage instructions to the Expected Output and enable it for this task."
        )

        # --- Pydantic Schema Injection ---
        available_schemas = list(get_available_schemas().keys())
        
        default_pydantic_str = editing_task.get('output_pydantic') if editing_task else ""
        default_pydantic_list = [s.strip() for s in default_pydantic_str.split(',')] if default_pydantic_str else []
        default_pydantic_list = [s for s in default_pydantic_list if s in available_schemas]

        def on_schema_select():
            sels = st.session_state.task_pydantic_sel
            if sels:
                import json
                if len(sels) == 1:
                    cls = get_schema_class(sels[0])
                    if cls:
                        schema_json = json.dumps(cls.model_json_schema(), indent=2)
                else:
                    from pydantic import create_model
                    fields = {}
                    for s in sels:
                        cls = get_schema_class(s)
                        if cls:
                            fields[s.lower()] = (cls, ...)
                    if fields:
                        DynamicModel = create_model('DynamicOutputSchema', **fields)
                        schema_json = json.dumps(DynamicModel.model_json_schema(), indent=2)
                    else:
                        schema_json = ""
                        
                if schema_json:
                    current_out = st.session_state.get("task_output_area", "")
                    import re
                    current_out = re.sub(r'\n\nMust conform to this JSON schema:\n```json\n.*?\n```', '', current_out, flags=re.DOTALL)
                    append_str = f"\n\nMust conform to this JSON schema:\n```json\n{schema_json}\n```"
                    st.session_state.task_output_area = f"{current_out}{append_str}".strip()

        selected_pydantic = st.multiselect(
            "Enforce Pydantic Schema (Optional)",
            options=available_schemas,
            default=default_pydantic_list,
            key="task_pydantic_sel",
            on_change=on_schema_select,
            help="Select one or more Pydantic schemas to strictly validate the JSON output. Alfredo will inject a combined schema into the Expected Output box."
        )
        selected_pydantic_str = ",".join(selected_pydantic) if selected_pydantic else None


        # --- Task AI Optimizer Button ---
        if st.button("✨ Optimize Description & Output with AI", key="opt_task_prompts_btn", use_container_width=True):
            current_desc = st.session_state.get("task_desc_area", "").strip()
            current_out = st.session_state.get("task_output_area", "").strip()
            
            if not current_desc and not current_out:
                st.warning("Please fill in at least one field (Description or Expected Output) to optimize.")
            else:
                st.markdown("**✨ Optimizing Task...**")
                try:
                    master_ai = MasterAI()
                    ph = st.empty()
                    full_text = ""
                    import time
                    last_update = 0
                    for chunk in master_ai.optimize_task_fields_stream(
                        description=current_desc,
                        expected_output=current_out
                    ):
                        full_text += chunk
                        if time.time() - last_update > 0.1:
                            ph.code(full_text, language="json")
                            last_update = time.time()
                    ph.code(full_text, language="json")
                    
                    import json, re
                    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", full_text, re.DOTALL | re.IGNORECASE)
                    if match:
                        full_text = match.group(1).strip()
                    
                    try:
                        optimized = json.loads(full_text)
                        st.session_state.optimized_task_data = {
                            "description": optimized.get("description", current_desc),
                            "expected_output": optimized.get("expected_output", current_out)
                        }
                    except json.JSONDecodeError:
                        st.error("AI returned invalid JSON format. Optimization failed.")
                        
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
            
            task_max_input_context = 0
            task_max_output_tokens = 0
            
            if selected_model_str and selected_model_str != "None / Use Agent Default":
                selected_model_id = model_options.get(selected_model_str)
                model_record = next((m for m in models if m['id'] == selected_model_id), None)
                if model_record:
                    is_ollama = model_record.get('provider', '').lower() == 'ollama'
                    
                    st.markdown("<div style='margin-top: 8px;'></div>", unsafe_allow_html=True)
                    task_max_output_tokens = st.slider(
                        "Max Output Tokens", 
                        min_value=256, max_value=128000, value=editing_task.get('max_output_tokens') or 4096 if editing_task else 4096, step=256,
                        help="Maximum number of tokens the model is allowed to generate in its response.",
                        key="task_out_tok"
                    )
                    
                    if is_ollama:
                        ollama_max = 128000
                        try:
                            from core.api_verifier import get_ollama_model_info
                            info = get_ollama_model_info(model_record.get('model_name', ''))
                            if info.get('success'):
                                ollama_max = info.get('max_context', 128000)
                        except Exception:
                            pass
                            
                        default_in = editing_task.get('max_input_context') or 8192 if editing_task else 8192
                        if default_in > ollama_max:
                            default_in = ollama_max
                            
                        task_max_input_context = st.slider(
                            "Max Input Context Size",
                            min_value=1024, max_value=max(1024, ollama_max), value=default_in, step=1024,
                            help=f"Allocates memory for context. Max supported by this model is {ollama_max}.",
                            key="task_in_ctx"
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
            help="If enabled, execution will pause after this task and ask the user via chat to review the agent's output and provide feedback to change/filter the output before proceeding."
        )

        st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)

        # --- Tool profile (sandbox ACL) ---
        profile_options = ["", "intermediate", "final_writer"]
        default_profile = (editing_task.get("tool_profile") if editing_task else "") or ""
        if default_profile not in profile_options:
            default_profile = ""
        tool_profile = st.selectbox(
            "Tool Profile",
            options=profile_options,
            index=profile_options.index(default_profile),
            format_func=lambda x: {
                "": "Auto (infer from write tools)",
                "intermediate": "Intermediate — no durable write tools",
                "final_writer": "Final writer — write tools allowed",
            }.get(x, x),
            help="Intermediate agents can only use read/memory tools. Final writers may use sandboxed write tools.",
            key="task_tool_profile_select",
        )
        
        st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)
        
        # Display Tools inside a collapsible expander containing Premium Cards
        with st.expander("🛠️ Assign Tools (Optional)", expanded=False):
            st.caption("Select the tools this task's agent is authorized to use. Tool execution depends on the selected model's `supports_tools` setting in Model Registry.")
            
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
                                safe_set_key(env_path_email, "OUTLOOK_EMAIL", new_email.strip())
                                saved_any = True
                            if new_password.strip():
                                safe_set_key(env_path_email, "OUTLOOK_APP_PASSWORD", new_password.strip())
                                saved_any = True
                            if new_imap.strip():
                                safe_set_key(env_path_email, "OUTLOOK_IMAP_SERVER", new_imap.strip())
                                saved_any = True
                            if new_smtp.strip():
                                safe_set_key(env_path_email, "OUTLOOK_SMTP_SERVER", new_smtp.strip())
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
                    db.update_task(editing_task['id'], sane_description, sane_expected_output, agent_id, selected_tools, input_rows, selected_vector_dbs, agent_specialization.strip() or None, task_name.strip() or None, task_model_id, human_validation, st.session_state.get('task_in_ctx', 0), st.session_state.get('task_out_tok', 0), selected_pydantic_str, tool_profile or "")
                    st.success(f"Task updated successfully!")
                    if 'temp_required_inputs' in st.session_state: del st.session_state.temp_required_inputs
                    if 'last_editing_task_id' in st.session_state: del st.session_state.last_editing_task_id
                    if 'task_name_input' in st.session_state: del st.session_state.task_name_input
                    if 'task_model_sel' in st.session_state: del st.session_state.task_model_sel
                    if 'task_human_validation_cb' in st.session_state: del st.session_state.task_human_validation_cb
                    for k in list(st.session_state.keys()):
                        if k.startswith("cb_task_"):
                            del st.session_state[k]
                    clear_editing_state('editing_task_id')
                else:
                    db.create_task(sane_description, sane_expected_output, agent_id, selected_tools, input_rows, selected_vector_dbs, agent_specialization.strip() or None, task_name.strip() or None, task_model_id, human_validation, st.session_state.get('task_in_ctx', 0), st.session_state.get('task_out_tok', 0), selected_pydantic_str, tool_profile or "")
                    st.success(f"Task added successfully!")
                    if 'temp_required_inputs' in st.session_state: del st.session_state.temp_required_inputs
                    if 'last_editing_task_id' in st.session_state: del st.session_state.last_editing_task_id
                    if 'task_name_input' in st.session_state: del st.session_state.task_name_input
                    if 'task_model_sel' in st.session_state: del st.session_state.task_model_sel
                    if 'task_human_validation_cb' in st.session_state: del st.session_state.task_human_validation_cb
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
            if 'task_human_validation_cb' in st.session_state:
                del st.session_state.task_human_validation_cb
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
                                with st.container(height=150, border=False):
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
                                
                                if task.get('human_validation'):
                                    st.markdown("---")
                                    st.markdown("✋ **Human Validation Checkpoint**")
                                    st.info("The workflow will pause after this task to allow the user to make a selection (Human-in-the-Loop) on Telegram.")
                                
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
                        with st.container(height=150, border=False):
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

def render_workflow_share_hub(db):
    """Share / Hub tab: local package import/export + optional hub publish/browse."""
    import os
    from core.workflow_package import (
        analyze_package_compatibility,
        dumps_package,
        export_workflow,
        import_workflow,
        loads_package,
        validate_package,
    )
    from core.hub_client import HubClient, HubClientError
    from core.hub_setup import (
        build_colleague_env_snippet,
        build_hosts_snippet,
        generate_invite_token,
        get_lan_ip,
        hub_container_status,
        is_running_in_docker,
        run_hub_compose,
        suggest_client_hub_url,
        suggest_colleague_hub_url,
        wait_for_hub_health,
    )

    def _hub_profile_from_mode(mode: str) -> str:
        mode = (mode or "off").strip().lower()
        if mode == "remote":
            return "remote"
        return "company"

    def _hub_mode_from_profile(profile: str) -> str:
        if profile == "remote":
            return "remote"
        return "local"

    st.subheader("Share Workflows")
    st.caption(
        "Packages (`.alfredo.json`) carry **logic only** — never API keys. "
        "Configure the hub in **Connect & account**, then publish or install from **Catalog**."
    )

    _publish_flash = st.session_state.get("hub_publish_flash")
    if _publish_flash:
        pc1, pc2 = st.columns([6, 1])
        with pc1:
            st.success(
                f"✅ **Publish confirmed** — **{_publish_flash.get('title')}** · "
                f"package id **{_publish_flash.get('id')}** · slug `{_publish_flash.get('slug')}` · "
                f"visibility **{_publish_flash.get('visibility')}**"
            )
        with pc2:
            if st.button("OK", key="hub_dismiss_publish_flash_top"):
                del st.session_state["hub_publish_flash"]
                st.rerun()
    _publish_err = st.session_state.get("hub_publish_error")
    if _publish_err:
        st.error(f"Last publish failed: {_publish_err}")

    env_path = find_dotenv() or os.path.join(os.getcwd(), ".env")
    file_env = dotenv_values(env_path) if os.path.exists(env_path) else {}

    def _hub_env(key, default=""):
        return (os.environ.get(key) or file_env.get(key) or default or "").strip()

    def _apply_hub_connection(
        *,
        mode,
        url,
        org,
        username="",
        token="",
        display_name="",
        server_role="",
        update_token=True,
    ):
        safe_set_key(env_path, "HUB_MODE", mode)
        safe_set_key(env_path, "HUB_API_URL", url)
        safe_set_key(env_path, "HUB_ORG", org)
        if username:
            safe_set_key(env_path, "HUB_USERNAME", username)
        if update_token and token:
            safe_set_key(env_path, "HUB_TOKEN", token)
        if display_name:
            safe_set_key(env_path, "HUB_DISPLAY_NAME", display_name)
        if server_role in ("client", "server_admin"):
            safe_set_key(env_path, "HUB_SERVER_ROLE", server_role)
        os.environ["HUB_MODE"] = mode
        os.environ["HUB_API_URL"] = url
        os.environ["HUB_ORG"] = org
        if username:
            os.environ["HUB_USERNAME"] = username
        if update_token and token:
            os.environ["HUB_TOKEN"] = token
        if display_name:
            os.environ["HUB_DISPLAY_NAME"] = display_name
        if server_role in ("client", "server_admin"):
            os.environ["HUB_SERVER_ROLE"] = server_role

    def _sync_hub_form_from_env(*, profile_hint: str = "company"):
        """Pre-fill hub widgets from .env when credentials on disk change."""
        default_url = (
            hub_url
            or suggest_client_hub_url(lan_ip=lan_ip)
            if profile_hint == "company"
            else (hub_url or "https://hub.example.com")
        )
        saved_role = _hub_env("HUB_SERVER_ROLE")
        if saved_role not in ("client", "server_admin"):
            saved_role = "server_admin" if _hub_env("HUB_REQUIRED_ORG") else "client"
        env_sig = "|".join(
            [
                hub_mode,
                hub_url,
                hub_org,
                hub_user,
                hub_token,
                _hub_env("HUB_DISPLAY_NAME"),
                _hub_env("HUB_INVITE_TOKEN"),
                saved_role,
            ]
        )
        if st.session_state.get("_hub_env_sig") == env_sig:
            return
        st.session_state["_hub_env_sig"] = env_sig
        fields = {
            "hub_profile": _hub_profile_from_mode(hub_mode),
            "hub_conn_url": default_url,
            "hub_conn_org": hub_org or "psid",
            "hub_reg_user": hub_user,
            "hub_reg_disp": _hub_env("HUB_DISPLAY_NAME"),
            "hub_reg_org": hub_org or "psid",
            "hub_existing_user": hub_user,
            "hub_existing_token": hub_token,
            "hub_reg_invite": _hub_env("HUB_INVITE_TOKEN"),
            "hub_server_role": saved_role,
        }
        for key, val in fields.items():
            if val:
                st.session_state[key] = val

    lan_ip = get_lan_ip()
    docker_status = hub_container_status()
    hub_mode = _hub_env("HUB_MODE", "off").lower()
    hub_url = _hub_env("HUB_API_URL", "http://localhost:8010")
    hub_user = _hub_env("HUB_USERNAME")
    hub_token = _hub_env("HUB_TOKEN")
    hub_org = _hub_env("HUB_ORG")

    workflows = db.read_all_workflows() or []
    wf_options = {f"{w['name']} (#{w['id']})": w["id"] for w in workflows}

    tab_connect, tab_catalog, tab_files = st.tabs(
        ["Connect & account", "Catalog (publish / install)", "File transfer (.alfredo.json)"]
    )

    # ── Connect & account ──────────────────────────────────────────────
    with tab_connect:
        _sync_hub_form_from_env(profile_hint=_hub_profile_from_mode(hub_mode))

        profile_labels = {
            "company": "Company hub — private LAN/VPN",
            "remote": "Remote hub — public or hosted URL",
        }
        cur_profile = _hub_profile_from_mode(hub_mode)
        profile = st.radio(
            "Connection type",
            options=["company", "remote"],
            index=["company", "remote"].index(cur_profile),
            format_func=lambda k: profile_labels[k],
            horizontal=True,
            key="hub_profile",
            help="Company = hub on your office network. Remote = any public hub URL.",
        )
        selected_mode = _hub_mode_from_profile(profile)

        is_server_admin = False
        if profile == "company":
            server_role = st.radio(
                "Role of this PC",
                options=["client", "server_admin"],
                format_func=lambda r: (
                    "Client — connect to a company hub on the network"
                    if r == "client"
                    else "Server — I run the hub on this PC"
                ),
                horizontal=True,
                key="hub_server_role",
            )
            is_server_admin = server_role == "server_admin"

        conn_url = st.text_input(
            "Hub URL",
            key="hub_conn_url",
            help="Company: http://psid.us:8010 · Remote: https://your-public-hub.example.com",
        )
        conn_org = st.text_input(
            "Organization slug",
            key="hub_conn_org",
            help="Same org slug for all colleagues. Required for org-visible packages.",
        )

        server_role = st.session_state.get("hub_server_role", "client")
        save_col, test_col = st.columns(2)
        with save_col:
            if st.button("Save connection", type="primary", key="hub_save_connection"):
                _apply_hub_connection(
                    mode=selected_mode,
                    url=conn_url,
                    org=conn_org,
                    server_role=server_role if profile == "company" else "",
                    update_token=False,
                )
                st.success(f"Connection saved (`HUB_MODE={selected_mode}`).")
                st.rerun()
        with test_col:
            test_clicked = st.button("Test connection", key="hub_test_conn")

        hub_up = False
        health: dict = {}
        test_url = conn_url if test_clicked else (st.session_state.get("hub_last_health_url") or conn_url)
        if test_clicked:
            st.session_state["hub_last_health_url"] = conn_url
            _apply_hub_connection(
                mode=selected_mode,
                url=conn_url,
                org=conn_org,
                server_role=server_role if profile == "company" else "",
                update_token=False,
            )
            st.rerun()
        try:
            health = HubClient(base_url=test_url).health()
            hub_up = health.get("status") == "ok"
            render_status_table("Hub status", health, kind="success")
        except HubClientError as e:
            st.error(f"Cannot reach `{test_url}`: {e}")

        if hub_mode in ("local", "remote") and hub_url == conn_url:
            st.caption(f"Saved: `{hub_mode}` · `{hub_url}` · org `{hub_org or '—'}`")
        elif hub_mode == "off":
            st.warning("Connection not saved yet — click **Save connection** or **Test connection**.")

        if profile == "company" and is_server_admin:
            with st.expander("Hub server on this PC (Docker + access policy)", expanded=not docker_status.get("hub_running")):
                tip_col, _ = st.columns([20, 1])
                with tip_col:
                    st.caption("Start the registry containers, then set who may register and publish.")
                if is_running_in_docker():
                    st.button(
                        "ℹ️",
                        key="hub_srv_docker_tip",
                        help="Dashboard is inside Docker — start/stop hub from the host terminal: "
                        "docker compose --profile hub up -d, or run_psid_hub_start.bat",
                    )
                elif not docker_status.get("docker_available"):
                    st.warning("Docker Desktop is not running.")
                else:
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Hub", "running" if docker_status.get("hub_running") else "stopped")
                    m2.metric("Database", "running" if docker_status.get("postgres_running") else "stopped")
                    m3.metric("LAN IP", lan_ip or "—")
                    b1, b2 = st.columns(2)
                    with b1:
                        if st.button("Start hub", type="primary", key="hub_srv_start"):
                            ok, msg = run_hub_compose("up")
                            st.success("Started.") if ok else st.error(msg)
                            st.rerun()
                    with b2:
                        if st.button("Stop hub", key="hub_srv_stop"):
                            ok, msg = run_hub_compose("stop")
                            st.success("Stopped.") if ok else st.error(msg)
                            st.rerun()

                p1, p2 = st.columns(2)
                with p1:
                    srv_reg = st.selectbox(
                        "Who can register",
                        ["invite", "open", "closed"],
                        index=["invite", "open", "closed"].index(
                            (_hub_env("HUB_REGISTRATION") or "invite").lower()
                            if (_hub_env("HUB_REGISTRATION") or "invite").lower() in ("invite", "open", "closed")
                            else "invite"
                        ),
                        key="hub_srv_reg",
                    )
                    srv_hostname = st.text_input("LAN hostname (optional)", value="psid.us", key="hub_srv_host")
                with p2:
                    srv_invite_val = _hub_env("HUB_INVITE_TOKEN") or st.session_state.get("hub_srv_invite_gen") or ""
                    ic1, ic2 = st.columns([5, 1], vertical_alignment="bottom")
                    with ic1:
                        srv_invite = st.text_input("Invite token", value=srv_invite_val, key="hub_srv_invite")
                    with ic2:
                        if st.button("Generate", key="hub_srv_gen_invite"):
                            tok = generate_invite_token()
                            st.session_state["hub_srv_invite_gen"] = tok
                            st.session_state["hub_srv_invite"] = tok
                            st.rerun()
                    srv_public = st.checkbox(
                        "Allow public packages on this hub",
                        value=_hub_env("HUB_ALLOW_PUBLIC", "false").lower() in ("1", "true", "yes"),
                        key="hub_srv_public",
                    )
                if st.button("Save server policy", key="hub_srv_save_policy"):
                    _apply_hub_connection(
                        mode=selected_mode,
                        url=conn_url,
                        org=conn_org,
                        server_role=server_role,
                        update_token=False,
                    )
                    safe_set_key(env_path, "HUB_REGISTRATION", srv_reg)
                    safe_set_key(env_path, "HUB_REQUIRED_ORG", conn_org)
                    safe_set_key(env_path, "HUB_ALLOW_PUBLIC", "true" if srv_public else "false")
                    if srv_reg == "invite" and srv_invite:
                        safe_set_key(env_path, "HUB_INVITE_TOKEN", srv_invite)
                    st.success("Server policy and connection saved. Restart hub if it was already running.")
                    st.rerun()
                if lan_ip and srv_hostname.strip():
                    st.markdown("**Colleague onboarding (copy to other PCs)**")
                    st.code(
                        build_colleague_env_snippet(
                            hub_url=suggest_colleague_hub_url(lan_ip=lan_ip, hostname=srv_hostname.strip()),
                            org_slug=conn_org,
                            ollama_url=f"http://{srv_hostname.strip()}:11434",
                        ),
                        language="ini",
                    )
                    st.caption("`hosts` file (all PCs):")
                    st.code(build_hosts_snippet(lan_ip=lan_ip, hostname=srv_hostname.strip()), language="text")

        st.markdown("---")
        st.markdown("#### Your account")

        hub_display = _hub_env("HUB_DISPLAY_NAME")
        if hub_user and hub_token:
            who = f"**@{hub_user}**"
            if hub_display:
                who = f"**{hub_display}** (@{hub_user})"
            st.success(f"Signed in as {who} · org `{hub_org or '—'}` · `{hub_url}`")
            st.caption("Credentials are saved in `.env` on this PC and will pre-fill on next visit.")
            if st.button("Sign out (clear token)", key="hub_disconnect"):
                safe_set_key(env_path, "HUB_TOKEN", "")
                os.environ["HUB_TOKEN"] = ""
                st.session_state["hub_existing_token"] = ""
                st.rerun()
        else:
            st.caption("Create a new hub user, or connect with a token you already received.")
            reg_mode = (health or {}).get("registration_mode") if hub_up else (_hub_env("HUB_REGISTRATION") or "invite")
            r1, r2 = st.columns(2)
            with r1:
                reg_user = st.text_input("Username", key="hub_reg_user")
                reg_disp = st.text_input("Display name", key="hub_reg_disp")
            with r2:
                reg_org = st.text_input("Org slug", key="hub_reg_org")
                reg_invite = ""
                if reg_mode == "invite":
                    reg_invite = st.text_input(
                        "Invite token",
                        key="hub_reg_invite",
                        type="password",
                        help="Ask the hub admin for this token.",
                    )
            if st.button("Register & connect", type="primary", key="hub_register_connect"):
                try:
                    _apply_hub_connection(
                        mode=selected_mode,
                        url=conn_url,
                        org=reg_org or conn_org,
                        display_name=reg_disp,
                        server_role=server_role if profile == "company" else "",
                        update_token=False,
                    )
                    if profile == "company" and is_server_admin:
                        if not docker_status.get("hub_running") and docker_status.get("docker_available") and not is_running_in_docker():
                            ok, msg = run_hub_compose("up")
                            if not ok:
                                st.error(msg)
                                st.stop()
                    ready, _, err = wait_for_hub_health(conn_url, timeout_sec=30.0)
                    if not ready:
                        st.error(f"Hub not ready: {err}")
                        st.stop()
                    res = HubClient(base_url=conn_url).register(
                        reg_user, reg_disp, reg_org or conn_org, invite_token=reg_invite
                    )
                    token = res.get("token") or ""
                    _apply_hub_connection(
                        mode=selected_mode,
                        url=conn_url,
                        org=reg_org or conn_org,
                        username=res.get("username") or reg_user,
                        token=token,
                        display_name=reg_disp or res.get("display_name") or reg_user,
                        server_role=server_role if profile == "company" else "",
                    )
                    st.success("Registered and connected.")
                    st.rerun()
                except HubClientError as e:
                    st.error(str(e))

            st.markdown("##### Already registered?")
            st.caption("Use this if you registered on another PC and kept your token.")
            ex1, ex2 = st.columns(2)
            with ex1:
                existing_user = st.text_input("Username", key="hub_existing_user")
            with ex2:
                existing_token = st.text_input("Token", type="password", key="hub_existing_token")
            if st.button("Connect with existing token", key="hub_existing_connect"):
                if not existing_user or not existing_token:
                    st.error("Enter both username and token.")
                else:
                    _apply_hub_connection(
                        mode=selected_mode,
                        url=conn_url,
                        org=conn_org,
                        username=existing_user,
                        token=existing_token,
                        display_name=_hub_env("HUB_DISPLAY_NAME"),
                        server_role=server_role if profile == "company" else "",
                    )
                    st.success("Connected.")
                    st.rerun()

    # ── Catalog ────────────────────────────────────────────────────────
    with tab_catalog:
        if hub_mode not in ("local", "remote"):
            st.warning(
                "Hub connection not saved. Open **Connect & account**, set URL and org, "
                "then click **Save connection**."
            )
        elif not hub_user or not hub_token:
            st.warning(
                "Account not signed in. In **Connect & account**, click **Register & connect** "
                "(or **Connect with existing token**)."
            )
        else:
            client = HubClient(base_url=hub_url, username=hub_user, token=hub_token)
            try:
                client.health()
            except HubClientError as e:
                st.error(f"Hub unreachable at `{hub_url}`: {e}")
                st.stop()

            def _hub_catalog_cache_key():
                return f"{hub_url}|{hub_user}"

            def _load_hub_catalog(force: bool = False):
                ck = _hub_catalog_cache_key()
                if force or st.session_state.get("hub_catalog_ck") != ck:
                    st.session_state["hub_catalog_ck"] = ck
                    st.session_state["hub_catalog_packages"] = client.search(q="", tag="")
                return st.session_state.get("hub_catalog_packages") or []

            def _filter_hub_packages(packages, query: str, tag_filter: str):
                q = (query or "").strip().lower()
                tag_l = (tag_filter or "").strip().lower()
                out = []
                for pkg in packages:
                    if tag_l:
                        tags = [str(t).lower() for t in (pkg.get("tags") or [])]
                        if tag_l not in tags:
                            continue
                    if q:
                        blob = " ".join(
                            [
                                str(pkg.get("title") or ""),
                                str(pkg.get("description") or ""),
                                str(pkg.get("slug") or ""),
                                str(pkg.get("author_username") or ""),
                                " ".join(str(t) for t in (pkg.get("tags") or [])),
                            ]
                        ).lower()
                        if q not in blob:
                            continue
                    out.append(pkg)
                return out

            flash = st.session_state.get("hub_publish_flash")
            if flash:
                st.success(
                    f"✅ **Publish confirmed** — **{flash.get('title')}** saved on the hub "
                    f"(id **{flash.get('id')}**, slug `{flash.get('slug')}`, {flash.get('visibility')}). "
                    f"Open **Browse & install** to verify."
                )

            pub_tab, browse_tab = st.tabs(["Publish", "Browse & install"])
            with pub_tab:
                st.caption(
                    "**private** — you + named users · **org** — same organization slug · "
                    "**public** — anyone on this hub (if allowed by server policy)"
                )

                def _sync_pub_fields_from_selection():
                    sel = st.session_state.get("hub_pub_wf", "")
                    if not sel:
                        return
                    name = sel.split(" (#")[0]
                    st.session_state["hub_pub_title"] = name
                    slug = name.lower().replace(" ", "-").replace("_", "-")
                    while "--" in slug:
                        slug = slug.replace("--", "-")
                    st.session_state["hub_pub_slug"] = slug.strip("-")[:64]

                if not wf_options:
                    st.warning("No workflows to publish.")
                else:
                    pub_sel = st.selectbox(
                        "Workflow",
                        list(wf_options.keys()),
                        key="hub_pub_wf",
                        on_change=_sync_pub_fields_from_selection,
                    )
                    if st.session_state.get("hub_pub_wf_last") != pub_sel:
                        _sync_pub_fields_from_selection()
                        st.session_state["hub_pub_wf_last"] = pub_sel

                    pub_title = st.text_input("Title", key="hub_pub_title")
                    pub_slug = st.text_input(
                        "Slug",
                        key="hub_pub_slug",
                        help="Unique per author + version. Change this when publishing a different workflow.",
                    )
                    pub_desc = st.text_area("Description", key="hub_pub_desc", height=70)
                    pub_tags = st.text_input("Tags (comma-separated)", key="hub_pub_tags")
                    pub_vis = st.selectbox("Visibility", ["private", "org", "public"], key="hub_pub_vis")
                    pub_ver = st.text_input("Version", value="1.0.0", key="hub_pub_ver")
                    pub_share = st.text_input(
                        "Share with (private only, comma-separated usernames)",
                        key="hub_pub_share",
                    )
                    if st.button("Publish to hub", type="primary", key="hub_pub_btn"):
                        try:
                            st.session_state.pop("hub_publish_error", None)
                            wf_id = wf_options[st.session_state.get("hub_pub_wf") or pub_sel]
                            pub_title_val = st.session_state.get("hub_pub_title", pub_title)
                            pub_slug_val = st.session_state.get("hub_pub_slug", pub_slug)
                            pkg = export_workflow(
                                wf_id,
                                description=pub_desc,
                                tags=[t.strip() for t in pub_tags.split(",") if t.strip()],
                                author=client.username,
                                db=db,
                            )
                            res = client.publish(
                                pkg,
                                slug=pub_slug_val,
                                title=pub_title_val,
                                description=pub_desc,
                                tags=[t.strip() for t in pub_tags.split(",") if t.strip()],
                                visibility=pub_vis,
                                package_version=pub_ver,
                                share_with=[u.strip() for u in pub_share.split(",") if u.strip()],
                            )
                            pkg_id = res.get("id")
                            st.session_state["hub_publish_flash"] = {
                                "id": pkg_id,
                                "title": pub_title_val,
                                "slug": pub_slug_val,
                                "visibility": pub_vis,
                                "workflow": st.session_state.get("hub_pub_wf", pub_sel),
                            }
                            st.session_state.pop("hub_catalog_packages", None)
                            st.session_state.pop("hub_catalog_ck", None)
                            st.toast(f"✅ Published: {pub_title_val} (id={pkg_id})")
                            st.rerun()
                        except Exception as e:
                            st.session_state["hub_publish_error"] = str(e)
                            st.error(f"Publish failed: {e}")

            @st.fragment
            def _hub_browse_live():
                try:
                    all_packages = _load_hub_catalog(force=False)
                except HubClientError as e:
                    st.error(f"Could not load catalog: {e}")
                    all_packages = []

                bc1, bc2 = st.columns([4, 1])
                with bc1:
                    st.text_input(
                        "Filter packages",
                        key="hub_search_q",
                        placeholder="Start typing — list updates live…",
                    )
                with bc2:
                    st.write("")
                    st.write("")
                    if st.button("Refresh catalog", key="hub_search_refresh"):
                        try:
                            _load_hub_catalog(force=True)
                            st.toast("Catalog refreshed")
                        except HubClientError as e:
                            st.error(str(e))

                st.text_input("Tag filter (optional)", key="hub_search_tag")
                q = (st.session_state.get("hub_search_q") or "").strip()
                tag = (st.session_state.get("hub_search_tag") or "").strip()
                results = _filter_hub_packages(all_packages, q, tag)
                if q or tag:
                    parts = []
                    if q:
                        parts.append(f"text `{q}`")
                    if tag:
                        parts.append(f"tag `{tag}`")
                    st.caption(
                        f"Live filter ({', '.join(parts)}) — **{len(results)}** of **{len(all_packages)}** packages."
                    )
                else:
                    st.caption(f"Showing all **{len(all_packages)}** packages visible to you.")

                if results:
                    for pkg in results:
                        with st.container(border=True):
                            st.markdown(
                                f"**{pkg.get('title')}** `v{pkg.get('package_version')}` — "
                                f"@{pkg.get('author_username')} · **{pkg.get('visibility')}** · "
                                f"⬇ {pkg.get('downloads', 0)}"
                            )
                            st.caption(pkg.get("description") or "")
                            pkg_tags = pkg.get("tags") or []
                            if pkg_tags:
                                st.caption("Tags: " + ", ".join(pkg_tags))
                            is_author = (
                                str(pkg.get("author_username") or "").strip().lower()
                                == str(hub_user or "").strip().lower()
                            )
                            if is_author:
                                c_inst, c_share, c_del = st.columns(3)
                            else:
                                c_inst, c_share, c_del = st.columns([2, 2, 1])
                                c_share = None
                                c_del = None
                            with c_inst:
                                if st.button("Install", key=f"hub_inst_{pkg['id']}"):
                                    try:
                                        full = client.download(pkg["id"])
                                        body = full.get("package_json")
                                        if isinstance(body, str):
                                            body = loads_package(body)
                                        result = import_workflow(body, conflict_policy="rename", db=db)
                                        st.success(f"Installed as **{result['workflow_name']}**")
                                        for w in result.get("warnings") or []:
                                            st.warning(w)
                                    except Exception as e:
                                        st.error(str(e))
                            if is_author:
                                with c_share:
                                    share_user = st.text_input("Share with", key=f"hub_share_u_{pkg['id']}")
                                    if st.button("Share", key=f"hub_share_b_{pkg['id']}"):
                                        try:
                                            client.share(pkg["id"], share_user)
                                            st.toast(f"Shared with {share_user}")
                                        except Exception as e:
                                            st.error(str(e))
                                with c_del:
                                    with st.popover("Remove from hub", use_container_width=True):
                                        st.caption(
                                            f"Permanently remove **{pkg.get('title')}** "
                                            f"`v{pkg.get('package_version')}` from the hub. "
                                            "Colleagues will no longer see or install it."
                                        )
                                        if st.button(
                                            "Confirm remove",
                                            key=f"hub_del_{pkg['id']}",
                                            type="primary",
                                        ):
                                            try:
                                                client.delete(pkg["id"])
                                                st.session_state.pop("hub_catalog_packages", None)
                                                st.session_state.pop("hub_catalog_ck", None)
                                                st.toast(f"Removed {pkg.get('title')} from hub")
                                                st.rerun()
                                            except Exception as e:
                                                st.error(str(e))
                elif all_packages:
                    st.info("No packages match your filter. Clear the filter fields to see everything.")
                else:
                    st.info(
                        "No packages visible with your account. Publish one in the **Publish** tab, "
                        "or check that you share the same org slug as the publisher."
                    )

            with browse_tab:
                _hub_browse_live()

    # ── File transfer ────────────────────────────────────────────────────
    with tab_files:
        st.markdown("#### Export")
        if not wf_options:
            st.warning("No workflows to export.")
        else:
            sel = st.selectbox("Workflow", list(wf_options.keys()), key="share_export_wf")
            c1, c2 = st.columns(2)
            with c1:
                desc = st.text_area("Description", key="share_export_desc", height=80)
            with c2:
                tags_raw = st.text_input("Tags (comma-separated)", key="share_export_tags")
                author = st.text_input("Author", value=_hub_env("HUB_USERNAME"), key="share_export_author")
            if st.button("Build package", key="share_build_pkg"):
                try:
                    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
                    pkg = export_workflow(
                        wf_options[sel],
                        description=desc,
                        tags=tags,
                        author=author,
                        db=db,
                    )
                    st.session_state["share_built_package"] = pkg
                    compat = analyze_package_compatibility(pkg)
                    st.success("Package ready.")
                    if any(compat.get(k) for k in ("missing_tools", "custom_tools", "missing_secrets", "missing_schemas")):
                        st.warning(
                            "Compatibility notes (tools listed as custom/missing must exist on the "
                            "target machine — they are still stored in the package)."
                        )
                        st.json(compat)
                    st.download_button(
                        "Download .alfredo.json",
                        data=dumps_package(pkg).encode("utf-8"),
                        file_name=f"{pkg['package'].get('slug') or 'workflow'}.alfredo.json",
                        mime="application/json",
                        key="share_dl_built",
                    )
                    with st.expander("Preview JSON"):
                        st.json(pkg)
                except Exception as e:
                    st.error(f"Export failed: {e}")

        st.markdown("---")
        st.markdown("#### Import")
        uploaded = st.file_uploader("Upload .alfredo.json", type=["json"], key="share_import_file")
        conflict = st.selectbox(
            "If workflow name exists",
            ["rename", "skip", "overwrite"],
            key="share_conflict",
            help="rename = create with suffix; skip = abort; overwrite = delete existing workflow then import",
        )
        if uploaded and st.button("Analyze & import", type="primary", key="share_do_import"):
            try:
                raw = uploaded.read().decode("utf-8")
                pkg = loads_package(raw)
                errs = validate_package(pkg)
                if errs:
                    st.error("Invalid package:\n- " + "\n- ".join(errs))
                else:
                    compat = analyze_package_compatibility(pkg)
                    st.markdown("**Compatibility**")
                    st.json(compat)
                    result = import_workflow(pkg, conflict_policy=conflict, db=db)
                    st.success(
                        f"Imported **{result['workflow_name']}** (id={result['workflow_id']}) — "
                        f"{result['tasks_imported']} tasks, {result['agents_imported']} agents"
                    )
                    for w in result.get("warnings") or []:
                        st.warning(w)
            except Exception as e:
                st.error(f"Import failed: {e}")


def render_workflow_assembler():
    """Renders Tab 4: UI for creating, viewing, and exporting workflows."""
    db = get_db_manager()
    st.header("Workflow Assembler")
    st.markdown("Combine individual tasks into a sequential workflow.")
    tab_wf, tab_share, tab_test = st.tabs(["Workflow + Saved Workflow", "Share / Hub", "Live System Test"])
    with tab_wf:

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
    
        import uuid
        raw_task_ids = list(editing_workflow.get('task_ids', [])) if editing_workflow else []
        default_wf_expected_exports = []
        raw_exports = editing_workflow.get('expected_exports', []) if editing_workflow else []
        default_wf_expected_exports = raw_exports if isinstance(raw_exports, list) else []
        default_export_instructions = (editing_workflow.get("export_instructions", "") or "") if editing_workflow else ""

        default_wf_task_ids = normalize_graph(
            raw_task_ids,
            expected_exports=default_wf_expected_exports,
            export_instructions=default_export_instructions,
            materialize_export=True,
        )

        if "last_editing_workflow_id" not in st.session_state or st.session_state.last_editing_workflow_id != current_editing_wf_id:
            st.session_state.last_editing_workflow_id = current_editing_wf_id
            st.session_state.wf_name_input = default_wf_name
            st.session_state.wf_human_check = default_wf_human_check
            st.session_state.wf_expected_exports = default_wf_expected_exports
            st.session_state.wf_export_instructions = default_export_instructions
            # Rebuild canvas structure (Level columns + stacked cards) for this draft
            try:
                from core.workflow_canvas import hydrate_graph_for_canvas
                _task_map = {t["id"]: t for t in tasks}
                st.session_state.wf_selected_task_ids = hydrate_graph_for_canvas(
                    default_wf_task_ids, task_id_map=_task_map, force_layout=True
                )
            except Exception:
                st.session_state.wf_selected_task_ids = default_wf_task_ids
            for k in (
                "wf_flow_state",
                "wf_loop_select_mode",
                "wf_loop_select_ids",
                "canvas_selected_node",
            ):
                st.session_state.pop(k, None)
            for k in list(st.session_state.keys()):
                if k.startswith("wf_check_"):
                    del st.session_state[k]
                
        # Failsafe check
        if "wf_selected_task_ids" not in st.session_state:
            st.session_state.wf_selected_task_ids = default_wf_task_ids
        else:
            # Keep draft typed / repaired across reruns
            st.session_state.wf_selected_task_ids = normalize_graph(
                st.session_state.wf_selected_task_ids,
                materialize_export=False,
            )

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

            export_instructions = st.text_area(
                "📝 Export Instructions (Optional)",
                key="wf_export_instructions",
                placeholder="e.g., For Python: extract the simulation model from the Developer agent. For Excel: use the metrics from the Analyst agent. For Word: write a full report.",
                help="Guide the Master AI on what to extract from each agent's output for each file format. Leave empty to let the AI decide automatically.",
                height=100
            )

        
            st.markdown("---")
            editor_mode = st.radio(
                "Editor mode",
                ["Canvas (drag & drop)", "Lanes (classic)"],
                horizontal=True,
                key="wf_editor_mode",
                help="Canvas: palette + React Flow board, task write-through, structured loops. Lanes: level columns.",
            )

            if editor_mode.startswith("Canvas"):
                canvas_ok = False
                try:
                    from ui.workflow_canvas_panel import render_canvas_palette_and_board
                    render_canvas_palette_and_board(
                        db=db,
                        tasks=tasks,
                        agents=agents,
                        available_exports=available_exports,
                        sanitize_input=sanitize_input,
                    )
                    canvas_ok = True
                except Exception as canvas_err:
                    import traceback
                    st.error(f"Canvas editor error: {canvas_err}")
                    st.code(traceback.format_exc(), language="text")
                    st.info("Falling back to Lanes mode below. If this is an import error, run: pip install streamlit-flow-component")
                st.markdown("---")
                if canvas_ok:
                    synced_exports, synced_instr = sync_header_from_export_nodes(
                        st.session_state.wf_selected_task_ids,
                        st.session_state.get("wf_expected_exports") or parsed_exports,
                        export_instructions or "",
                    )
                    if synced_exports and set(synced_exports) != set(parsed_exports or []):
                        st.info(
                            f"Export blocks declare: {', '.join(synced_exports)}. "
                            "On save these sync into Expected File Outputs."
                        )
                    st.session_state["_wf_skip_lanes"] = True
                else:
                    st.session_state["_wf_skip_lanes"] = False
            else:
                st.session_state["_wf_skip_lanes"] = False

            if not st.session_state.get("_wf_skip_lanes"):
                st.markdown("**Add workflow blocks** (function-block schema)")
                st.caption(
                    "Primary block = Task (agent is the face). Also: Input, Batch, HITL gate, Export. "
                    "Wire data ports below each card; control deps via Depends On."
                )

                default_block_type_idx = 0
                default_b_size = 5
                default_b_source = "{previous_result}"
                default_inner_tasks = []

                all_task_options = {}
                if tasks:
                    all_task_options = {f"{t.get('name') or 'Task #'+str(t['id'])} ({agent_id_map.get(t.get('agent_id'), {}).get('name', 'Unknown')}): {t['description'][:50]}": t['id'] for t in tasks}
                    for step in st.session_state.wf_selected_task_ids:
                        if isinstance(step, dict) and step.get("type") == NODE_TYPE_BATCH:
                            default_block_type_idx = 1
                            default_b_size = int(step.get("batch_size", 5))
                            default_b_source = step.get("source_variable", "{previous_result}")
                            for tid in step.get("task_ids", []):
                                for k, v in all_task_options.items():
                                    if v == tid:
                                        default_inner_tasks.append(k)
                                        break
                            break

                block_type = st.radio(
                    "Block Type to Add",
                    ["Task", "Batch Loop", "HITL", "Export", "Input"],
                    index=min(default_block_type_idx, 4),
                    horizontal=True,
                )

                if block_type == "Task":
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
                            if st.button("Add Task block"):
                                new_node = {
                                    "id": new_node_id(),
                                    "type": NODE_TYPE_TASK,
                                    "task_id": task_options[sel_task_str],
                                    "depends_on": [],
                                    "execution_level": 1,
                                    "model_tier": "default",
                                    "inputs_map": {},
                                }
                                st.session_state.wf_selected_task_ids.append(new_node)
                                st.rerun()
                        else:
                            st.info("No tasks assigned to this agent.")

                elif block_type == "Batch Loop":
                    with st.container(border=True):
                        st.markdown("**Batch Loop Properties**")
                        b_size = st.number_input("Batch Size (items per chunk)", min_value=1, value=default_b_size)
                        b_source = st.text_input("Source Variable", value=default_b_source, help="The variable or output holding the JSON array to iterate over.")
                        st.markdown("**Select inner loop tasks (in order):**")
                        if tasks:
                            valid_defaults = [x for x in default_inner_tasks if x in all_task_options]
                            sel_inner_tasks_str = st.multiselect("Inner Tasks", list(all_task_options.keys()), default=valid_defaults)
                            if st.button("Add Batch Loop"):
                                inner_ids = [all_task_options[s] for s in sel_inner_tasks_str]
                                if inner_ids:
                                    new_block = {
                                        "id": new_node_id(),
                                        "type": NODE_TYPE_BATCH,
                                        "task_ids": inner_ids,
                                        "batch_size": b_size,
                                        "source_variable": b_source,
                                        "depends_on": [],
                                        "execution_level": 1,
                                        "model_tier": "default",
                                        "inputs_map": {},
                                    }
                                    st.session_state.wf_selected_task_ids.append(new_block)
                                    st.rerun()
                                else:
                                    st.error("Select at least one inner task.")
                        else:
                            st.info("No tasks available.")

                elif block_type == "HITL":
                    hitl_msg = st.text_input(
                        "HITL message",
                        value="Please review and approve before continuing.",
                        key="wf_hitl_msg",
                    )
                    if st.button("Add HITL gate"):
                        st.session_state.wf_selected_task_ids.append({
                            "id": new_node_id("hitl"),
                            "type": NODE_TYPE_HITL,
                            "depends_on": [],
                            "execution_level": 1,
                            "model_tier": "default",
                            "inputs_map": {},
                            "message": hitl_msg,
                            "gate_mode": "after_parents",
                        })
                        st.rerun()

                elif block_type == "Export":
                    ex_opts = st.multiselect(
                        "Export formats",
                        options=available_exports,
                        default=[],
                        key="wf_add_export_formats",
                    )
                    ex_instr = st.text_area("Export instructions", key="wf_add_export_instr", height=80)
                    if st.button("Add Export block"):
                        st.session_state.wf_selected_task_ids.append({
                            "id": new_node_id("export"),
                            "type": NODE_TYPE_EXPORT,
                            "depends_on": [],
                            "execution_level": 1,
                            "model_tier": "default",
                            "inputs_map": {},
                            "exports": ex_opts,
                            "export_instructions": ex_instr or "",
                        })
                        st.rerun()

                elif block_type == "Input":
                    in_keys = st.text_input(
                        "Declared input keys (comma-separated)",
                        value="user_input",
                        key="wf_add_input_keys",
                        help="Logical ports available as from=input in data wiring.",
                    )
                    if st.button("Add Input block"):
                        keys = [k.strip() for k in (in_keys or "").split(",") if k.strip()]
                        st.session_state.wf_selected_task_ids.append({
                            "id": new_node_id("input"),
                            "type": NODE_TYPE_INPUT,
                            "depends_on": [],
                            "execution_level": 1,
                            "model_tier": "default",
                            "inputs_map": {},
                            "keys": keys or ["user_input"],
                        })
                        st.rerun()

                st.markdown("---")
                st.markdown("**Schema (by execution level)**")
                st.markdown("Nodes run in parallel when dependencies allow. Use Data wiring for named ports.")

                if not st.session_state.wf_selected_task_ids:
                    st.info("No blocks yet. Add a Task, Batch, HITL, Export, or Input block above.")
                else:
                    def rem_step(idx):
                        removed_id = st.session_state.wf_selected_task_ids[idx]["id"]
                        st.session_state.wf_selected_task_ids = remove_node(
                            st.session_state.wf_selected_task_ids, removed_id
                        )

                    def get_node_label(step):
                        if not isinstance(step, dict):
                            return str(step)
                        ntype = step.get("type", NODE_TYPE_TASK)
                        if ntype == NODE_TYPE_BATCH:
                            return f"Batch ({step.get('id')})"
                        if ntype == NODE_TYPE_HITL:
                            return f"HITL ({step.get('id')})"
                        if ntype == NODE_TYPE_EXPORT:
                            return f"Export ({step.get('id')})"
                        if ntype == NODE_TYPE_INPUT:
                            return f"Input ({step.get('id')})"
                        task_id = step.get("task_id")
                        task = task_id_map.get(int(task_id)) if task_id is not None else None
                        t_name = (task.get('name') or f"Task #{task_id}") if task else "Unknown"
                        return f"{t_name} ({step.get('id')})"

                    levels = sorted(list(set(step.get("execution_level", 1) for step in st.session_state.wf_selected_task_ids)))
                    if not levels:
                        levels = [1]

                    st.markdown("<div class='workflow-level-marker'></div>", unsafe_allow_html=True)
                    st.markdown("""
                    <style>
                    div[data-testid="stElementContainer"]:has(.workflow-level-marker) + div[data-testid="stHorizontalBlock"] {
                        flex-wrap: nowrap !important;
                        overflow-x: auto !important;
                        padding-bottom: 10px !important;
                    }
                    div[data-testid="stElementContainer"]:has(.workflow-level-marker) + div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
                        min-width: 350px !important;
                    }
                    </style>
                    """, unsafe_allow_html=True)

                    level_columns = st.columns(len(levels))

                    for lvl_idx, lvl in enumerate(levels):
                        with level_columns[lvl_idx]:
                            st.markdown(f"### Level {lvl}")

                            for i, step in enumerate(st.session_state.wf_selected_task_ids):
                                if step.get("execution_level", 1) != lvl:
                                    continue

                                with st.container(border=True):
                                    col_main = st.container()
                                    ntype = step.get("type", NODE_TYPE_TASK)
                                    is_batch = ntype == NODE_TYPE_BATCH
                                    is_hitl = ntype == NODE_TYPE_HITL
                                    is_export = ntype == NODE_TYPE_EXPORT
                                    is_input = ntype == NODE_TYPE_INPUT

                                    if ntype == NODE_TYPE_TASK:
                                        task_id = step.get("task_id")
                                        task = task_id_map.get(int(task_id)) if task_id is not None else None
                                        if task:
                                            agent = agent_id_map.get(task['agent_id'])
                                            a_name = agent['name'] if agent else "Unknown Agent"
                                            t_name = task.get('name') or f"Task #{task['id']}"
                                            av_col, tx_col = col_main.columns([1, 5])
                                            with av_col:
                                                if agent:
                                                    st.image(get_agent_avatar_url(agent), width=56)
                                            with tx_col:
                                                st.markdown(
                                                    f"**TASK** {t_name} (`{step['id']}`)<br>"
                                                    f"<sub>Agent: {a_name}</sub><br>"
                                                    f"<sub>{(task.get('description') or '')[:100]}...</sub>",
                                                    unsafe_allow_html=True,
                                                )
                                                if task.get("human_validation"):
                                                    st.caption("Task flag: human_validation")
                                        else:
                                            col_main.markdown(f"Unknown Task ID: {task_id}")
                                    elif is_batch:
                                        b_size = step.get("batch_size", 1)
                                        col_main.markdown(
                                            f"**BATCH** (`{step['id']}`)<br>"
                                            f"<sub>Size: {b_size}, Source: `{step.get('source_variable')}`</sub>",
                                            unsafe_allow_html=True,
                                        )
                                        for inner_id in step.get("task_ids", []):
                                            itask = task_id_map.get(int(inner_id)) if inner_id is not None else None
                                            if itask:
                                                iagent = agent_id_map.get(itask['agent_id'])
                                                i_tname = itask.get('name') or f"Task #{itask['id']}"
                                                i_aname = iagent['name'] if iagent else "?"
                                                col_main.markdown(f"&nbsp;&nbsp;-> {i_tname} ({i_aname})", unsafe_allow_html=True)
                                    elif is_hitl:
                                        col_main.markdown(f"**HITL gate** `{step['id']}`")
                                        step["message"] = col_main.text_input(
                                            "Message",
                                            value=step.get("message") or "",
                                            key=f"hitl_msg_{step['id']}",
                                        )
                                    elif is_export:
                                        col_main.markdown(f"**EXPORT** `{step['id']}`")
                                        cur_ex = [x for x in (step.get("exports") or []) if x in available_exports]
                                        step["exports"] = col_main.multiselect(
                                            "Formats",
                                            options=available_exports,
                                            default=cur_ex,
                                            key=f"ex_fmt_{step['id']}",
                                        )
                                        step["export_instructions"] = col_main.text_area(
                                            "Instructions",
                                            value=step.get("export_instructions") or "",
                                            key=f"ex_instr_{step['id']}",
                                            height=70,
                                        )
                                    elif is_input:
                                        col_main.markdown(f"**INPUT** `{step['id']}`")
                                        keys_str = ", ".join(step.get("keys") or [])
                                        new_keys = col_main.text_input(
                                            "Keys",
                                            value=keys_str,
                                            key=f"in_keys_{step['id']}",
                                        )
                                        step["keys"] = [k.strip() for k in new_keys.split(",") if k.strip()]

                                    step["execution_level"] = col_main.number_input(
                                        "Level (Parallelism)", min_value=1, value=int(lvl), key=f"lvl_{step['id']}"
                                    )

                                    if ntype in (NODE_TYPE_TASK, NODE_TYPE_BATCH):
                                        tier_opts = ["default", "simple"]
                                        cur_tier = step.get("model_tier", "default")
                                        if cur_tier not in tier_opts:
                                            cur_tier = "default"
                                        step["model_tier"] = col_main.selectbox(
                                            "Model Tier",
                                            options=tier_opts,
                                            index=tier_opts.index(cur_tier),
                                            key=f"tier_{step['id']}",
                                            help="All DAG agents are workers.",
                                        )
                                    step.pop("role", None)

                                    possible_deps = {
                                        get_node_label(prev_step): prev_step["id"]
                                        for prev_step in st.session_state.wf_selected_task_ids
                                        if prev_step["id"] != step["id"]
                                    }
                                    current_deps = step.get("depends_on", [])
                                    valid_default_deps = [label for label, n_id in possible_deps.items() if n_id in current_deps]

                                    selected_labels = col_main.multiselect(
                                        "Depends On (control):",
                                        options=list(possible_deps.keys()),
                                        default=valid_default_deps,
                                        key=f"deps_{step['id']}",
                                        placeholder="No dependencies",
                                    )
                                    step["depends_on"] = [possible_deps[label] for label in selected_labels]

                                    if ntype in (NODE_TYPE_TASK, NODE_TYPE_BATCH):
                                        with col_main.expander("Data wiring", expanded=False):
                                            imap = dict(step.get("inputs_map") or {})
                                            source_opts = {"(run input)": "input"}
                                            source_opts.update({
                                                get_node_label(p): p["id"]
                                                for p in st.session_state.wf_selected_task_ids
                                                if p["id"] != step["id"]
                                            })
                                            port_name = st.text_input(
                                                "Port name (placeholder in task text)",
                                                key=f"wire_port_{step['id']}",
                                                placeholder="e.g. brief",
                                            )
                                            src_label = st.selectbox(
                                                "From",
                                                options=list(source_opts.keys()),
                                                key=f"wire_from_{step['id']}",
                                            )
                                            key_field = st.text_input(
                                                "Key / field (optional)",
                                                key=f"wire_key_{step['id']}",
                                                placeholder="JSON field or empty for full output",
                                            )
                                            if st.button("Set wire", key=f"wire_set_{step['id']}"):
                                                if port_name.strip():
                                                    src_id = source_opts[src_label]
                                                    imap[port_name.strip()] = {
                                                        "from": src_id,
                                                        "key": key_field.strip(),
                                                    }
                                                    if src_id != "input" and src_id not in step["depends_on"]:
                                                        step["depends_on"] = list(step["depends_on"]) + [src_id]
                                                    step["inputs_map"] = imap
                                                    st.rerun()
                                            if imap:
                                                st.markdown("**Current wires:**")
                                                for port, binding in list(imap.items()):
                                                    bfrom = binding.get("from") if isinstance(binding, dict) else "?"
                                                    bkey = (binding.get("key") if isinstance(binding, dict) else "") or "*"
                                                    c1, c2 = st.columns([4, 1])
                                                    c1.code(f"{{{port}}} <- {bfrom}.{bkey}")
                                                    if c2.button("x", key=f"wire_del_{step['id']}_{port}"):
                                                        imap.pop(port, None)
                                                        step["inputs_map"] = imap
                                                        st.rerun()
                                            else:
                                                st.caption("No wires — runtime uses {previous_result} from last parent.")

                                    c_up, c_down, c_rem = col_main.columns(3)
                                    c_up.button("Up", key=f"wf_up_{i}", on_click=move_wf_task_up, args=(i,))
                                    c_down.button("Down", key=f"wf_down_{i}", on_click=move_wf_task_down, args=(i,))
                                    c_rem.button("Remove", key=f"wf_rem_{i}", on_click=rem_step, args=(i,))

                    st.markdown("**Dependency mini-map**")
                    task_labels = {t["id"]: (t.get("name") or f"Task #{t['id']}") for t in tasks}
                    try:
                        dot = graph_to_dot(st.session_state.wf_selected_task_ids, task_labels=task_labels)
                        st.graphviz_chart(dot)
                    except Exception as viz_err:
                        st.caption(f"Graphviz preview unavailable: {viz_err}")
                        wires = []
                        for n in st.session_state.wf_selected_task_ids:
                            for d in n.get("depends_on") or []:
                                wires.append(f"{d} -> {n.get('id')} (control)")
                            for port, binding in (n.get("inputs_map") or {}).items():
                                if isinstance(binding, dict):
                                    wires.append(
                                        f"{binding.get('from')}.{binding.get('key') or '*'} -> {n.get('id')}.{{{port}}}"
                                    )
                        if wires:
                            st.code("\n".join(wires))
                        else:
                            st.caption("No edges yet.")

                synced_exports, synced_instr = sync_header_from_export_nodes(
                    st.session_state.wf_selected_task_ids,
                    st.session_state.get("wf_expected_exports") or parsed_exports,
                    export_instructions or "",
                )
                if synced_exports and set(synced_exports) != set(parsed_exports or []):
                    st.info(
                        f"Export blocks declare: {', '.join(synced_exports)}. "
                        "On save these sync into Expected File Outputs for Master AI compatibility."
                    )

            st.markdown("---")

            def _persist_workflow_draft(name: str, workflow_id=None):
                """Validate → canonicalize → sync export header → create/update."""
                graph = canonicalize_graph(st.session_state.wf_selected_task_ids)
                errors, warnings = validate_graph(graph, task_id_map=task_id_map)
                for w in warnings:
                    st.warning(w)
                if errors:
                    for e in errors:
                        st.error(e)
                    return False
                exports, instr = sync_header_from_export_nodes(
                    graph, parsed_exports, export_instructions or ""
                )
                if not exports:
                    exports = list(parsed_exports or [])
                if not instr:
                    instr = export_instructions or ""
                if workflow_id is not None:
                    db.update_workflow(
                        workflow_id, name, graph, requires_human_check, exports, instr
                    )
                else:
                    db.create_workflow(
                        name, graph, requires_human_check, exports, instr
                    )
                return True

            def _clear_wf_edit_session():
                st.session_state.editing_workflow_id = None
                for key in (
                    "last_editing_workflow_id",
                    "wf_selected_task_ids",
                    "wf_name_input",
                    "wf_human_check",
                    "wf_expected_exports",
                    "wf_export_instructions",
                    "wf_flow_state",
                    "wf_loop_select_mode",
                    "wf_loop_select_ids",
                    "canvas_selected_node",
                ):
                    if key in st.session_state:
                        del st.session_state[key]
                for k in list(st.session_state.keys()):
                    if k.startswith("wf_check_"):
                        del st.session_state[k]

            if editing_workflow:
                col_save, col_cancel = st.columns([1, 1])
                with col_save:
                    if st.button("Update Workflow", type="primary", use_container_width=True):
                        sane_workflow_name = sanitize_input(workflow_name)
                        if not sane_workflow_name:
                            st.error("Workflow Name is required.")
                        elif _persist_workflow_draft(sane_workflow_name, editing_workflow["id"]):
                            st.success(f"Workflow '{sane_workflow_name}' updated successfully!")
                            _clear_wf_edit_session()
                            st.rerun()
                with col_cancel:
                    if st.button("Cancel Edit", use_container_width=True):
                        _clear_wf_edit_session()
                        st.rerun()
            else:
                if st.button("Save Workflow", type="primary", use_container_width=True):
                    sane_workflow_name = sanitize_input(workflow_name)
                    if not sane_workflow_name:
                        st.error("Workflow Name is required.")
                    elif _persist_workflow_draft(sane_workflow_name, None):
                        st.success(f"Workflow '{sane_workflow_name}' created successfully!")
                        _clear_wf_edit_session()
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
                    # Grouping by execution level
                    levels = sorted(list(set(step.get("execution_level", 1) if isinstance(step, dict) else 1 for step in task_ids)))
                    if not levels:
                        levels = [1]
                    
                    st.markdown("<div class='workflow-level-marker'></div>", unsafe_allow_html=True)
                    st.markdown("""
                    <style>
                    div[data-testid="stElementContainer"]:has(.workflow-level-marker) + div[data-testid="stHorizontalBlock"] {
                        flex-wrap: nowrap !important;
                        overflow-x: auto !important;
                        padding-bottom: 10px !important;
                    }
                    div[data-testid="stElementContainer"]:has(.workflow-level-marker) + div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
                        min-width: 350px !important;
                    }
                    </style>
                    """, unsafe_allow_html=True)
                
                    level_columns = st.columns(len(levels))
                
                    for lvl_idx, lvl in enumerate(levels):
                        with level_columns[lvl_idx]:
                            st.markdown(f"### 📍 Livello {lvl}")
                        
                            for i, step in enumerate(task_ids):
                                exec_lvl = step.get("execution_level", 1) if isinstance(step, dict) else 1
                                if exec_lvl != lvl:
                                    continue
                                
                                with st.container(border=True):
                                    ntype = step.get("type") if isinstance(step, dict) else None
                                    is_batch = isinstance(step, dict) and step.get("type") == "batch_loop"
                                    is_seq = isinstance(step, dict) and step.get("type") == "sequential"
                                    is_hitl = ntype == "hitl"
                                    is_export = ntype == "export"
                                    is_input = ntype == "input"
                                    if is_hitl:
                                        st.markdown(f"**HITL** `{step.get('id')}` — {step.get('message') or 'gate'}")
                                    elif is_export:
                                        st.markdown(
                                            f"**EXPORT** `{step.get('id')}` — "
                                            f"{', '.join(step.get('exports') or []) or 'formats TBD'}"
                                        )
                                    elif is_input:
                                        st.markdown(
                                            f"**INPUT** `{step.get('id')}` — keys: "
                                            f"{', '.join(step.get('keys') or []) or 'user_input'}"
                                        )
                                    elif not is_batch and not is_seq:
                                        task_id = step if isinstance(step, int) else step.get("task_id")
                                        if task_id is not None:
                                            task = task_id_map.get(int(task_id))
                                        else:
                                            task = None
                                        if task:
                                            agent = agent_id_map.get(task['agent_id'])
                                            col_avatar, col_text = st.columns([1, 6])
                                            with col_avatar:
                                                if agent:
                                                    st.image(get_agent_avatar_url(agent), use_container_width=True)
                                                else:
                                                    st.markdown("<h4 style='margin:0'>❓</h4>", unsafe_allow_html=True)
                                            with col_text:
                                                t_name_display = task.get('name') or f"Task #{task['id']}"
                                                node_id = step.get('id', 'N/A') if isinstance(step, dict) else 'N/A'
                                                deps = step.get('depends_on', []) if isinstance(step, dict) else []
                                                deps_str = f" **(Depends on: {', '.join(deps)})**" if deps else ""
                                                tooltip_html = f"<span title='Node ID: {node_id}' style='cursor:help;'>ℹ️</span>"
                                                st.markdown(f"🚀 **{t_name_display}** {tooltip_html}{deps_str}<br><sub>{task['description']}</sub>", unsafe_allow_html=True)
                                                if task.get('human_validation'):
                                                    st.markdown("✋ **Human Validation Checkpoint**")
                                        else:
                                            st.error(f"Step {i+1}: Task ID {task_id} not found")
                                    else:
                                        inner_ids = step.get("task_ids", [])
                                        node_id = step.get('id', 'N/A')
                                        tooltip_html = f"<span title='Node ID: {node_id}' style='cursor:help;'>ℹ️</span>"
                                        if is_batch:
                                            b_size = step.get("batch_size")
                                            st.markdown(f"**Step {i+1}** | 🔄 **Batch Loop** {tooltip_html} | Chunk Size: {b_size}, Source: `{step.get('source_variable')}`", unsafe_allow_html=True)
                                        else:
                                            st.markdown(f"**Step {i+1}** | ▶️ **Sequential Tasks** {tooltip_html}", unsafe_allow_html=True)
                                        for inner_id in inner_ids:
                                            if inner_id is not None:
                                                itask = task_id_map.get(int(inner_id))
                                            else:
                                                itask = None
                                            if itask:
                                                iagent = agent_id_map.get(itask['agent_id'])
                                                i_aname = iagent['name'] if iagent else "Unknown Agent"
                                                i_tname = itask.get('name') or f"Task #{itask['id']}"
                                                tooltip_inner = f"<span title='Inner Task ID: {inner_id}' style='cursor:help;'>ℹ️</span>"
                                                i_col_indent, i_col_card = st.columns([0.5, 7.5])
                                                with i_col_card:
                                                    with st.container(border=True):
                                                        i_col_avatar, i_col_text = st.columns([1, 8])
                                                        with i_col_avatar:
                                                            if iagent:
                                                                st.image(get_agent_avatar_url(iagent), width=70)
                                                            else:
                                                                st.markdown("<h4 style='margin:0'>❓</h4>", unsafe_allow_html=True)
                                                        with i_col_text:
                                                            st.markdown(f"🚀 **{i_tname}** {tooltip_inner}<br><sub>{itask.get('description', '')[:100]}...</sub>", unsafe_allow_html=True)
                                            else:
                                                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ ❌ Unknown Task ID: {inner_id}")
                
                    # --- ARCHITECTURAL MANDATE M1_T3-A2 & M1_T3-A3: Secure Export Logic ---
                    # 1. Construct the data structure for YAML in-memory
                    export_data = {'workflow': {'name': workflow['name'], 'tasks': []}}
                    for step in task_ids:
                        is_batch = isinstance(step, dict) and step.get("type") == "batch_loop"
                        is_seq = isinstance(step, dict) and step.get("type") == "sequential"
                        if not is_batch and not is_seq:
                            task_id = step if isinstance(step, int) else step.get("task_id")
                            if task_id is not None:
                                task = task_id_map.get(int(task_id))
                            else:
                                task = None
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
                            group_info = {
                                'type': step.get('type'),
                                'tasks': []
                            }
                            if is_batch:
                                group_info['batch_size'] = step.get('batch_size')
                                group_info['source_variable'] = step.get('source_variable')
                            for inner_id in step.get("task_ids", []):
                                if inner_id is not None:
                                    itask = task_id_map.get(int(inner_id))
                                else:
                                    itask = None
                                if itask:
                                    iagent = agent_id_map.get(itask['agent_id'])
                                    task_data = {
                                        'name': itask.get('name') or f"Task #{itask['id']}",
                                        'description': itask['description'],
                                        'expected_output': itask['expected_output'],
                                        'agent': iagent['name'] if iagent else 'Unknown Agent'
                                    }
                                    group_info['tasks'].append(task_data)
                            export_data['workflow']['tasks'].append(group_info)
                    # 2. Generate YAML string in-memory. NOTE: yaml.dump is safe for exporting.
                    yaml_string = yaml.dump(export_data, sort_keys=False, indent=2)
                
                    # 3. Sanitize filename and provide download button
                    safe_filename = f"{sanitize_filename(workflow['name'])}.yaml"
                
                    col_del, col_edit, col_export, col_json = st.columns([1, 1, 1.2, 1.2])
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
                            label="Export YAML",
                            data=yaml_string.encode('utf-8'),
                            file_name=safe_filename,
                            mime="application/x-yaml",
                            key=f"export_wf_{workflow['id']}",
                            use_container_width=True
                        )
                    with col_json:
                        try:
                            from core.workflow_package import export_workflow, dumps_package
                            pkg = export_workflow(workflow['id'], db=db)
                            st.download_button(
                                label="Export Package",
                                data=dumps_package(pkg).encode("utf-8"),
                                file_name=f"{sanitize_filename(workflow['name'])}.alfredo.json",
                                mime="application/json",
                                key=f"export_pkg_{workflow['id']}",
                                use_container_width=True,
                                help="Portable .alfredo.json (agents, tasks, graph — no secrets)",
                            )
                        except Exception as pkg_err:
                            st.caption(f"Package export unavailable: {pkg_err}")

    with tab_share:
        render_workflow_share_hub(db)

    with tab_test:
        st.subheader("🧪 Run Workflows")
        st.markdown("Test the integration between **Master AI**, **Crew Builder**, and **Tools**, or directly run a specific workflow.")
    
        exec_mode = st.radio("Execution Mode", ["Direct Workflow Execution", "Natural Language Routing (Master AI)"], horizontal=True)
        st.markdown("---")

        if exec_mode == "Natural Language Routing (Master AI)":
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
                                inputs = routing.get('extracted_params', {})
                                run_id = db.create_run(wf_id, status='running', inputs=inputs)
                            
                                try:
                                    from core.crew_builder import execute_run_with_resume
                                    result = execute_run_with_resume(
                                        run_id, 
                                        status_callback=lambda tidx, tot, role, status="completed": st.write(f"Task {tidx}/{tot} ({role}): {status}")
                                    )
                                
                                    db.update_run(run_id, status='completed', result=str(result))
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
        
        else:
            # Direct Workflow Execution
            workflows = db.read_all_workflows()
            if not workflows:
                st.info("No workflows available.")
            else:
                wf_options = {w['id']: w['name'] for w in workflows}
                selected_wf_id = st.selectbox("Select Workflow to Run", options=list(wf_options.keys()), format_func=lambda x: wf_options[x])
                
                if selected_wf_id:
                    selected_wf = next((w for w in workflows if w['id'] == selected_wf_id), None)
                    st.markdown(f"**Executing:** {selected_wf['name']}")
                    
                    # Parse required inputs from task descriptions
                    tasks = db.read_all_tasks()
                    task_map = {t['id']: t for t in tasks}
                    
                    # Extract all task IDs using the previously defined _extract_tids logic
                    def _extract_tids_local(steps):
                        tids = []
                        for step in steps:
                            if isinstance(step, int):
                                tids.append(step)
                            elif isinstance(step, dict):
                                if step.get("type") == "batch_loop":
                                    tids.extend(_extract_tids_local(step.get("task_ids", [])))
                                elif "task_id" in step:
                                    tids.append(step["task_id"])
                        return tids
                    
                    try:
                        t_ids_field = selected_wf.get('task_ids_json') or selected_wf.get('task_ids', '[]')
                        raw_tids = t_ids_field if isinstance(t_ids_field, list) else json.loads(t_ids_field)
                        flat_tids = _extract_tids_local(raw_tids)
                    except Exception as e:
                        flat_tids = []
                        st.error(f"Error parsing workflow tasks: {e}")
                        
                    import re
                    required_vars = set()
                    var_prompts = {}
                    for tid in flat_tids:
                        t = task_map.get(tid)
                        if t and t.get('description'):
                            # Find all {var} patterns
                            matches = re.findall(r'\{([a-zA-Z0-9_]+)\}', t['description'])
                            for m in matches:
                                if m not in ['previous_result']: # Ignore built-in crewai vars
                                    required_vars.add(m)
                        if t and t.get('required_inputs'):
                            for ri in t['required_inputs']:
                                if isinstance(ri, dict) and 'key' in ri and 'prompt' in ri:
                                    var_prompts[ri['key']] = ri['prompt']
                                    
                    st.markdown("### Required Inputs")
                    user_inputs = {}
                    if not required_vars:
                        st.info("This workflow does not require any dynamic inputs.")
                    else:
                        for var in sorted(list(required_vars)):
                            placeholder = var_prompts.get(var, f"e.g. Enter value for {var}")
                            # Give a sensible default placeholder
                            if var == 'context':
                                val = st.text_input(f"Value for `{var}` (Optional)", key=f"input_{var}_{selected_wf_id}", value="N/A", placeholder=placeholder)
                            elif var == 'document_type':
                                val = st.selectbox(f"Value for `{var}` (Optional)", ["Pure Components", "BIPs", "eNRTL", "Unknown/Mixed"], key=f"input_{var}_{selected_wf_id}")
                            else:
                                val = st.text_input(f"Value for `{var}`", key=f"input_{var}_{selected_wf_id}", placeholder=placeholder)
                            user_inputs[var] = val
                            
                    st.markdown("### Paused Runs")
                    from core.crew_builder import PauseExecution
                    
                    runs = db.read_all_runs()
                    paused_runs = [r for r in runs if r.get('status') == 'paused' and r.get('workflow_id') == selected_wf_id]
                    
                    if paused_runs:
                        for pr in paused_runs:
                            st.warning(f"⚠️ Run #{pr['id']} was paused due to rate limits or errors. Progress has been saved.")
                            if st.button(f"▶️ Resume Run #{pr['id']}", key=f"resume_{pr['id']}", type="primary"):
                                with st.status(f"Resuming Run #{pr['id']}...", expanded=True) as status:
                                    try:
                                        from core.crew_builder import execute_run_with_resume
                                        result = execute_run_with_resume(
                                            pr['id'], 
                                            status_callback=lambda tidx, tot, role, status="completed": st.write(f"Task {tidx}/{tot} ({role}): {status}")
                                        )
                                        db.update_run(pr['id'], status='completed', result=str(result))
                                        st.success("✅ Execution Complete!")
                                        st.markdown(str(result))
                                    except PauseExecution as e:
                                        st.warning(f"⚠️ Workflow Paused: {e}")
                                        st.info("You can resume this run later from the 'Paused Runs' section above.")
                                        status.update(label="Execution Paused", state="complete")
                                    except Exception as e:
                                        db.update_run(pr['id'], status='failed', result=str(e))
                                        st.error(f"❌ Error during execution: {e}")
                                        import traceback
                                        st.code(traceback.format_exc())
                                        status.update(label="Execution Failed", state="error")
                                    else:
                                        status.update(label="Execution Finished", state="complete")
                    
                    st.markdown("### Start New Run")
                    if st.button("🚀 Execute Workflow", type="primary"):
                        # Validate inputs
                        missing = [k for k in required_vars if not user_inputs.get(k) and k not in ['context', 'document_type']]
                        if missing:
                            st.error(f"Please provide values for: {', '.join(missing)}")
                        else:
                            with st.status("Queuing Workflow...", expanded=True) as status:
                                try:
                                    # Create Run Record
                                    run_id = db.create_run(selected_wf_id, status='running', inputs=user_inputs)
                                    
                                    import threading
                                    def _run_workflow_bg(r_id, wf_name):
                                        try:
                                            from core.crew_builder import execute_run_with_resume
                                            result = execute_run_with_resume(r_id)
                                            db = DBManager()
                                            current_run = db.read_run(r_id)
                                            if current_run and current_run.get('status') in ['stopped', 'failed', 'cancelled', 'paused']:
                                                db.update_run(r_id, result=str(result))
                                            else:
                                                db.update_run(r_id, status='completed', result=str(result))
                                                from core.notification_manager import NotificationManager
                                                notifier = NotificationManager()
                                                notifier.notify_workflow_completion(wf_name, result)
                                        except Exception as e:
                                            try:
                                                db = DBManager()
                                                current_run = db.read_run(r_id)
                                                if current_run and current_run.get('status') in ['stopped', 'failed', 'cancelled', 'paused']:
                                                    pass
                                                else:
                                                    db.update_run(r_id, status='failed', result=str(e))
                                            except Exception:
                                                pass

                                    thread = threading.Thread(target=_run_workflow_bg, args=(run_id, selected_wf['name']), daemon=True)
                                    thread.start()
                                    
                                    st.success(f"✅ Workflow queued successfully (Run ID: {run_id})!")
                                    st.info("🔄 It is now running in the background. You can safely change tabs to 'History & Monitoring' to view its real-time progress without interrupting it.")
                                except PauseExecution as e:
                                    st.warning(f"⚠️ Workflow Paused: {e}")
                                    st.info("You can resume this run later from the 'Paused Runs' section above.")
                                except Exception as e:
                                    db.update_run(run_id, status='failed', result=str(e))
                                    st.error(f"Failed to queue workflow: {e}")
                    
                    # Check for active running instances of this workflow
                    active_runs = [r for r in db.read_all_runs(limit=10) if r['workflow_id'] == selected_wf_id and r['status'] == 'running']
                    if active_runs:
                        ar = active_runs[0]
                        st.markdown("---")
                        
                        # Extract in-flight tasks for better visibility
                        in_flight = ar.get('in_flight_tasks', '[]')
                        try:
                            if isinstance(in_flight, str):
                                in_flight_list = json.loads(in_flight)
                            else:
                                in_flight_list = in_flight
                            in_flight_str = ", ".join(str(t) for t in in_flight_list if t)
                            if not in_flight_str: in_flight_str = "Initializing..."
                        except Exception:
                            in_flight_str = "Unknown"
                            
                        st.info(f"🔄 **This workflow is currently running in the background** (Run ID: {ar['id']}).\n\n**Currently Executing:** {in_flight_str}")
                        
                        if st.button("🛑 Stop Active Run", type="secondary", key=f"stop_{ar['id']}"):
                            db.update_run(active_runs[0]['id'], status='stopped')
                            st.success("Run stopped successfully. It may take a few seconds to halt.")
                            st.rerun()

def render_tool_factory():
    """Renders the Tool Factory tab."""
    db = get_db_manager()
    st.header("Tool Factory 2.0 🏭")
    st.markdown("Use AI to generate and deploy custom tools for your agents.")
    
    tools_map_path = os.path.join(os.getcwd(), 'config', 'tools_map.yaml')
    custom_tools_path = os.path.join(os.getcwd(), 'tools', 'custom_tools.py')
    
    if "tf_step" not in st.session_state:
        st.session_state.tf_step = 1
        st.session_state.tf_generated_code = ""
        st.session_state.tf_tool_id = ""

    # --- STEP 1: GENERATE ---
    if st.session_state.tf_step == 1:
        st.subheader("Step 1: Describe the Tool")
        
        # Model Selection
        models = db.read_all_models()
        model_options = {m['id']: f"{m['provider'].upper()} - {m['model_name']} ({m.get('description', '')})" for m in models}
        if not model_options:
            st.error("No LLM models found. Please configure a model in Settings.")
            return
            
        selected_model_id = st.selectbox("Select LLM for Code Generation", options=list(model_options.keys()), format_func=lambda x: model_options[x])
        
        tool_prompt = st.text_area("What should this tool do?", placeholder="e.g. A tool that fetches the latest news about a company from a public API.", height=150)
        
        if st.button("Generate Code ⚡", type="primary"):
            if tool_prompt:
                st.markdown("**Generating code...**")
                try:
                    from core.tool_generator import generate_tool_code_stream
                    code_placeholder = st.empty()
                    
                    full_text = ""
                    import time
                    last_update = 0
                    for chunk in generate_tool_code_stream(tool_prompt, selected_model_id):
                        full_text += chunk
                        if time.time() - last_update > 0.1:
                            code_placeholder.code(full_text, language="python")
                            last_update = time.time()
                    code_placeholder.code(full_text, language="python")
                        
                    # Extract code from markdown block if present
                    import re
                    match = re.search(r"```python\s*(.*?)\s*```", full_text, re.DOTALL)
                    if match:
                        full_text = match.group(1).strip()
                    else:
                        match_generic = re.search(r"```\s*(.*?)\s*```", full_text, re.DOTALL)
                        if match_generic:
                            full_text = match_generic.group(1).strip()
                            
                    st.session_state.tf_generated_code = full_text
                    
                    # Try to extract function name
                    match_func = re.search(r'def\s+([a-zA-Z0-9_]+)\s*\(', full_text)
                    if match_func:
                        st.session_state.tf_tool_id = match_func.group(1)
                        
                    st.session_state.tf_step = 2
                    st.rerun()
                except Exception as e:
                    st.error(f"Error generating code: {e}")
            else:
                st.warning("Please provide a description.")
                
    # --- STEP 2: REVIEW & SAVE ---
    elif st.session_state.tf_step == 2:
        st.subheader("Step 2: Review and Deploy")
        
        edited_code = st.text_area("Review / Edit Python Code", value=st.session_state.tf_generated_code, height=400)
        
        st.markdown("---")
        st.markdown("**Tool Metadata**")
        col1, col2 = st.columns(2)
        with col1:
            tool_id = st.text_input("Tool ID (Function Name)", value=st.session_state.tf_tool_id)
        with col2:
            tool_name = st.text_input("Display Name", placeholder="e.g. Custom News Fetcher")
            
        tool_desc = st.text_input("Short Description (for the UI)", placeholder="What does this tool do?")
        tool_secrets = st.text_input("Required Secrets (comma separated)", placeholder="e.g. NEWS_API_KEY")
        
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            if st.button("Deploy Tool 🚀", type="primary"):
                if not tool_id or not tool_name or not tool_desc:
                    st.error("Please fill in Tool ID, Display Name, and Description.")
                elif not edited_code:
                    st.error("Code cannot be empty.")
                else:
                    try:
                        # 1. Append to custom_tools.py
                        with open(custom_tools_path, 'a', encoding='utf-8') as f:
                            f.write(f"\n\n{edited_code}\n")
                            
                        # 2. Update YAML
                        tools_config = DataManager.load_yaml(tools_map_path) if os.path.exists(tools_map_path) else {}
                        if 'tools_registry' not in tools_config:
                            tools_config['tools_registry'] = {}
                            
                        secrets_list = [s.strip() for s in tool_secrets.split(",")] if tool_secrets else []
                        tools_config['tools_registry'][tool_id] = {
                            'display_name': tool_name,
                            'description': tool_desc,
                            'required_secrets': secrets_list
                        }
                        
                        with open(tools_map_path, 'w', encoding='utf-8') as f:
                            import yaml
                            yaml.dump(tools_config, f, sort_keys=False, indent=2)
                            
                        st.success(f"Tool '{tool_name}' deployed successfully!")
                        st.session_state.tf_step = 1
                        st.session_state.tf_generated_code = ""
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error saving tool: {e}")
        with col_s2:
            if st.button("Cancel / Start Over"):
                st.session_state.tf_step = 1
                st.session_state.tf_generated_code = ""
                st.rerun()

    # --- LIST EXISTING TOOLS ---
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



def render_history_monitoring():
    """Renders Tab 5: Workflow Run History."""
    db = get_db_manager()
    st.header("History & Monitoring")
    st.markdown("Track the execution status and results of your workflows.")

    col1, col2, col3 = st.columns([1, 1, 4])
    with col1:
        if st.button("🗑️ Clear All History", type="primary", use_container_width=True):
            count = db.clear_all_runs()
            st.toast(f"History cleared! ({count} runs removed)", icon="🗑️")
            st.rerun()
    with col2:
        if st.button("🧹 Clear Completed", type="secondary", use_container_width=True):
            runs_to_del = [r for r in db.read_all_runs() if r['status'] == 'completed']
            for r in runs_to_del:
                db.delete_run(r['id'])
            st.toast(f"Cleared {len(runs_to_del)} completed runs!", icon="🧹")
            st.rerun()
    with col3:
        if st.button("🔄 Refresh Live Status", type="secondary"):
            st.rerun()
            
    auto_delete = st.checkbox("🧹 Auto-delete completed runs (keeps only failed/running)", value=False)

    runs = db.read_all_runs(limit=50)
    workflows = db.read_all_workflows()
    wf_map = {wf['id']: wf['name'] for wf in workflows}

    if not runs:
        st.info("No workflow runs recorded yet.")
        return

    for run in runs:
        if auto_delete and run['status'] == 'completed':
            db.delete_run(run['id'])
            continue
            
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
                    in_flight_json = run.get('in_flight_tasks')
                    if in_flight_json and in_flight_json != '[]':
                        import json
                        try:
                            in_flight_list = json.loads(in_flight_json)
                            if len(in_flight_list) > 1:
                                tasks_str = ", ".join(in_flight_list)
                                st.info(f"⏳ **In Progress:** {len(in_flight_list)} Agents running in parallel ({tasks_str})")
                            elif len(in_flight_list) == 1:
                                st.info(f"⏳ **In Progress:** Executing {in_flight_list[0]}")
                        except Exception:
                            current_idx = run.get('current_task_idx', 0)
                            st.info(f"⏳ **In Progress:** Executing Step {current_idx + 1}")
                    else:
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
                if status == 'running':
                    if st.button("🛑", key=f"stop_run_{run['id']}", help="Stop this run"):
                        db.update_run(run['id'], status='stopped')
                        st.toast("Run stopped", icon="🛑")
                        st.rerun()
                        
                if st.button("🗑️", key=f"del_run_{run['id']}", help="Delete this run"):
                    db.delete_run(run['id'])
                    st.toast(f"Run {run['id']} deleted")
                    st.rerun()


def render_local_training():
    """Renders Tab: Local Model Training."""
    import core.fine_tuner as ft
    import pandas as pd
    import time

    st.header("Local Model Training 🪖")
    st.markdown(
        "Fine-tune open-source models with Unsloth (QLoRA). "
        "Flusso: prepara dataset → addestra → monitora → testa → esporta in Ollama."
    )

    tab_data, tab_tune, tab_eval, tab_chat, tab_deploy = st.tabs([
        "1. Data Prep 🗃️",
        "2. Fine-Tuning ⚙️",
        "3. Monitoring & Eval 📊",
        "4. Inference Test 💬",
        "5. Export & Deployment 📦",
    ])

    with tab_data:
        st.subheader("Prepare Dataset")
        st.caption(
            "Formati supportati: CSV (`prompt`/`instruction` + `completion`/`response`/`output`), "
            "TXT (righe alternate user/assistant), JSON/JSONL (campo `messages`), PDF (testo estratto in chunk)."
        )
        uploaded_files = st.file_uploader(
            "Upload raw files (CSV, TXT, JSON, JSONL, PDF)",
            type=["csv", "txt", "json", "jsonl", "pdf"],
            accept_multiple_files=True,
        )
        if st.button("Genera Dataset ChatML", type="primary"):
            if uploaded_files:
                st.info("Salvataggio file e conversione in ChatML...")
                saved_paths = []
                for f in uploaded_files:
                    save_dir = ft.RAW_DATASET_DIR
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, f.name)
                    with open(save_path, "wb") as out:
                        out.write(f.getbuffer())
                    saved_paths.append(save_path)
                result = ft.prepare_chatml_dataset(saved_paths)
                if result["total_entries"] > 0:
                    st.success(
                        f"✅ Dataset generato: `{result['path']}` — "
                        f"{result['total_entries']} esempi da {len(result['processed_files'])} file."
                    )
                else:
                    st.error("Nessun esempio generato. Controlla i file e i messaggi sotto.")
                for err in result.get("errors") or []:
                    st.warning(err)
            else:
                st.warning("Carica almeno un file prima.")

        st.markdown("---")
        ok, ds_msg, ds_count = ft.ensure_dataset_ready()
        if ok:
            st.caption(f"Dataset pronto: **{ds_count}** esempi — `{ds_msg}`")
            preview = ft.load_dataset_preview(limit=5)
            if preview:
                st.dataframe(preview, use_container_width=True)
            else:
                st.info("Dataset presente ma anteprima vuota.")
        else:
            st.info(ds_msg)

    with tab_tune:
        st.subheader("Configure Training")
        col1, col2 = st.columns(2)
        with col1:
            base_model = st.selectbox(
                "Base Model",
                [
                    "unsloth/llama-3-8b-Instruct-bnb-4bit",
                    "unsloth/mistral-7b-instruct-v0.3-bnb-4bit",
                    "unsloth/Qwen2.5-7B-Instruct-bnb-4bit",
                ],
            )
            preset = st.radio(
                "Training Preset",
                ["🚀 Fast (Prototype)", "⚖️ Balanced (Recommended)", "🧠 Deep (High Quality)"],
            )
            if "llama" in base_model.lower():
                st.caption(
                    "Llama gated: imposta `HUGGINGFACE_TOKEN` nel `.env` e accetta la licenza su HuggingFace."
                )

        with col2:
            st.markdown("### Hardware Estimation")
            vram_est = ft.estimate_vram_usage(base_model, batch_size=2)
            safe_text = "Safe" if vram_est["is_safe"] else "Warning: May OOM"
            color = "normal" if vram_est["is_safe"] else "inverse"
            st.metric(
                "Estimated VRAM Required",
                f"{vram_est['required_gb']:.1f} GB",
                delta=safe_text,
                delta_color=color,
            )
            est_mins = ft.estimate_training_minutes(preset)
            steps = ft.max_steps_for_preset(preset)
            st.metric("Estimated Time", f"~ {est_mins} mins", delta=f"{steps} steps")

        ok_ds, ds_info, ds_count = ft.ensure_dataset_ready()
        if ok_ds:
            st.info(f"Dataset: {ds_count} esempi pronti.")
        else:
            st.warning(ds_info)

        st.markdown("---")
        if st.button("Avvia Addestramento 🚀", type="primary", use_container_width=True):
            result = ft.start_training_process({"model": base_model, "preset": preset})
            if result.get("ok"):
                st.success(result["message"])
                st.info("Apri il tab Monitoring per seguire progressi e log.")
            else:
                st.error(result.get("message", "Avvio training fallito."))

    with tab_eval:
        st.subheader("Live Monitoring")
        status = ft.read_training_status()
        step = int(status.get("step", 0) or 0)
        total = max(int(status.get("total_steps", 1) or 1), 1)
        loss = float(status.get("loss", 0.0) or 0.0)
        done = bool(status.get("done", False))
        running = ft.is_training_running()

        pct = min(100, int((step / total) * 100))
        label = f"Step {step}/{total} — Loss: {loss:.4f}"
        if done:
            label += " ✅ Completato"
        elif running:
            label += " ⏳ In corso"
        st.progress(pct, text=label)

        col_a, col_b = st.columns(2)
        with col_a:
            auto_refresh = st.checkbox("Auto-refresh (3s)", value=running and not done)
        with col_b:
            if st.button("Aggiorna ora"):
                st.rerun()

        loss_history = ft.read_loss_history()
        if loss_history:
            st.line_chart(pd.DataFrame({"Loss": loss_history}))
        elif running:
            st.info("In attesa dei primi valori di loss...")
        else:
            st.caption("Nessuna curva loss ancora. Avvia un training per vedere i dati reali.")

        with st.expander("Training Logs", expanded=True):
            st.code(ft.read_training_logs(), language="text")

        if auto_refresh and running and not done:
            time.sleep(3)
            st.rerun()

    with tab_chat:
        st.subheader("Inference Test")
        st.markdown("Testa l'adapter LoRA salvato in `storage/adapters/temp` dopo il training.")
        if not os.path.isdir(ft.ADAPTER_DIR):
            st.warning("Nessun adapter trovato. Completa prima un training.")
        prompt = st.chat_input("Di' qualcosa al modello fine-tuned...")
        if prompt:
            st.chat_message("user").write(prompt)
            with st.spinner("Generazione in corso..."):
                resp = ft.run_inference(prompt, ft.ADAPTER_DIR)
            st.chat_message("assistant").write(resp)

    with tab_deploy:
        st.subheader("Export to Ollama")
        st.caption("Esporta GGUF (q4_k_m), scrive un Modelfile e prova `ollama create`.")
        final_name = st.text_input("Final Model Name", placeholder="e.g. Alfredo-Support-Bot-8B")
        if st.button("Export to .gguf and Deploy 📦", type="primary"):
            if final_name:
                with st.spinner("Export GGUF in corso (può richiedere diversi minuti)..."):
                    result = ft.export_to_ollama(ft.ADAPTER_DIR, final_name)
                if result.get("ok"):
                    st.success(result["message"])
                    if result.get("gguf_path"):
                        st.code(result["gguf_path"])
                else:
                    st.error(result.get("message", "Export fallito."))
            else:
                st.error("Inserisci un nome per il modello.")


def render_my_apps():
    """Renders the 'Le mie App' tab for managing external app integrations."""
    db = get_db_manager()
    DataManager.load_env()

    st.subheader("🔗 My Apps — External App Integration (For Developers)")
    st.markdown("""
    **Developer Zone**: Connect external applications to Alfredo. From here, you can create custom workflows and connect them directly to frontend elements of other apps. 
    Trigger these workflows from the external app itself using the **Widget SDK** or **REST APIs**.
    """)

    # --- Session state for app detail view ---
    if 'viewing_app_id' not in st.session_state:
        st.session_state.viewing_app_id = None

    apps = db.get_all_apps()

    # ========= APP LIST VIEW =========
    if st.session_state.viewing_app_id is None:
        st.divider()

        # --- Create New App Form ---
        with st.expander("➕ Connect New App", expanded=len(apps) == 0):
            with st.form("create_app_form"):
                col1, col2 = st.columns(2)
                with col1:
                    app_name = st.text_input("App Name (slug, no spaces)", placeholder="my_erp_system",
                                             help="Used in API URLs. Only letters, numbers, and underscores.")
                    app_display = st.text_input("Display Name", placeholder="My ERP System")
                    app_desc = st.text_area("Description", placeholder="Briefly describe the app...", max_chars=500)
                with col2:
                    app_db_type = st.selectbox("Database Type", ["sqlite", "postgresql"],
                                               help="The external app's database type")
                    app_db_env_key = st.text_input(".env Variable Name for DB",
                                                   placeholder="ERP_DB_URL",
                                                   help="E.g., ERP_DB_URL → Alfredo will read os.getenv('ERP_DB_URL')")
                    app_api_env_key = st.text_input(".env Variable Name for API Key",
                                                    placeholder="ERP_API_KEY",
                                                    help="E.g., ERP_API_KEY → used to authenticate with the app")
                    app_api_url = st.text_input("App's API Base URL",
                                                placeholder="http://localhost:3000/api")
                    app_root = st.text_input("App Project Root Path (optional)",
                                             placeholder="C:/Users/.../MyProject")

                submitted = st.form_submit_button("🚀 Connect App", use_container_width=True)
                if submitted:
                    if not app_name:
                        st.error("App name is required.")
                    elif not re.match(r'^[a-zA-Z0-9_]+$', app_name):
                        st.error("App name can only contain letters, numbers, and underscores.")
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
                            st.success(f"✅ App '{app_display or app_name}' connected successfully! (ID: {app_id})")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error creating app: {e}")

        # --- Apps Cards ---
        if not apps:
            st.info("No apps connected. Use the form above to connect your first app.")
        else:
            for app_record in apps:
                with st.container(border=True):
                    col1, col2, col3 = st.columns([3, 1, 1])
                    with col1:
                        status_emoji = "🟢" if app_record.get('status') == 'active' else "🔴"
                        st.markdown(f"### {status_emoji} {app_record.get('display_name', app_record['name'])}")
                        st.caption(f"Slug: `{app_record['name']}` | DB: `{app_record.get('db_type', 'N/A')}` | Created: {app_record.get('created_at', 'N/A')}")
                        if app_record.get('description'):
                            st.markdown(f"*{app_record['description'][:100]}*")

                        # Count workflows
                        app_workflows = db.get_app_workflows(app_record['id'])
                        st.caption(f"📋 {len(app_workflows)} workflows connected")

                    with col2:
                        if st.button("📂 Open", key=f"open_app_{app_record['id']}", use_container_width=True):
                            st.session_state.viewing_app_id = app_record['id']
                            st.rerun()

                    with col3:
                        if st.button("🗑️ Delete", key=f"del_app_{app_record['id']}", use_container_width=True, type="secondary"):
                            db.delete_app(app_record['id'])
                            st.success(f"App '{app_record['name']}' deleted.")
                            st.rerun()

    # ========= APP DETAIL VIEW =========
    else:
        app_id = st.session_state.viewing_app_id
        app_record = db.get_app(app_id)

        if not app_record:
            st.error("App not found.")
            st.session_state.viewing_app_id = None
            st.rerun()
            return

        # Back button
        if st.button("⬅️ Back to apps list"):
            st.session_state.viewing_app_id = None
            st.rerun()

        st.markdown(f"## 🔗 {app_record.get('display_name', app_record['name'])}")

        # --- Tabs inside app detail ---
        app_tab1, app_tab2, app_tab3, app_tab4 = st.tabs([
            "🔑 Connection", "📋 Workflows", "📦 Integration", "📊 Logs"
        ])

        # --- TAB: Connection & API Key ---
        with app_tab1:
            st.markdown("### API Key")
            st.code(app_record['api_key'], language=None)
            col1, col2 = st.columns(2)
            with col1:
                if st.button("🔄 Regenerate API Key", use_container_width=True):
                    new_key = db.regenerate_app_api_key(app_id)
                    st.warning(f"⚠️ New API Key generated. Remember to update the SDK in your external app!")
                    st.code(new_key, language=None)
                    st.rerun()
            with col2:
                new_status = "disabled" if app_record.get('status') == 'active' else "active"
                btn_label = "🔴 Disable" if app_record.get('status') == 'active' else "🟢 Enable"
                if st.button(btn_label, use_container_width=True):
                    db.update_app(app_id, status=new_status)
                    st.rerun()

            st.divider()
            st.markdown("### Connection Configuration")

            # Check .env variables mapped to this app
            env_vars_status = []
            if app_record.get('db_env_key'):
                env_vars_status.append(app_record['db_env_key'])
            if app_record.get('api_env_key'):
                env_vars_status.append(app_record['api_env_key'])

            if env_vars_status:
                env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
                app_env = dotenv_values(env_path)
                col_a1, col_a2 = st.columns(2)
                for i, key in enumerate(env_vars_status):
                    is_set = key in app_env and bool(app_env[key].strip())
                    status_icon = "🟢" if is_set else "🔴"
                    tooltip = f"Connection parameter for {app_record.get('name')}"
                    with (col_a1 if i % 2 == 0 else col_a2):
                        st.markdown(f'<span title="{tooltip}" style="cursor: help;">{status_icon} <b>{key}</b></span>', unsafe_allow_html=True)
            else:
                st.info("No .env variables configured for this app. Go to 'Edit' (if available) or create a new app to map env variables.")

            st.markdown(f"**Base API URL**: `{app_record.get('api_base_url', 'Not configured')}`")
            st.markdown(f"**DB Type**: `{app_record.get('db_type', 'sqlite')}`")
            st.markdown(f"**Root Path**: `{app_record.get('app_root_path', 'Not configured')}`")

        # --- TAB: Workflows ---
        with app_tab2:
            st.markdown("### Workflows connected to this App")
            app_workflows = db.get_app_workflows(app_id)

            if not app_workflows:
                st.info("No workflows connected. Create a workflow in the **Workflow Assembler** (Tab 5) and connect it to this app.")
            else:
                for wf in app_workflows:
                    with st.container(border=True):
                        task_ids = wf.get('task_ids', [])
                        st.markdown(f"**{wf['name']}** — {len(task_ids)} tasks")

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
                            st.caption(f"Required inputs: `{'`, `'.join(input_keys)}`")

            st.divider()
            st.markdown("### Connect existing workflow")
            all_workflows = db.read_all_workflows()
            unlinked = [wf for wf in all_workflows if wf.get('app_id') is None]

            if unlinked:
                wf_options = {f"{wf['name']} (ID: {wf['id']})": wf['id'] for wf in unlinked}
                selected_wf = st.selectbox("Select workflow to connect", list(wf_options.keys()),
                                           key=f"link_wf_{app_id}")
                if st.button("🔗 Connect to this App", key=f"link_btn_{app_id}"):
                    wf_id = wf_options[selected_wf]
                    # Update workflow to set app_id
                    wf_record = db.read_workflow(wf_id)
                    if wf_record:
                        db.cursor.execute("UPDATE workflows SET app_id = ? WHERE id = ?", (app_id, wf_id))
                        db.conn.commit()
                        st.success(f"Workflow '{wf_record['name']}' connected to this app!")
                        st.rerun()
            else:
                st.caption("All workflows are already connected to an app, or none exist yet.")

        # --- TAB: Integration Snippet ---
        with app_tab3:
            st.markdown("### Integration Snippet")
            st.markdown("Copy this code into your app's frontend to integrate the Alfredo widget:")

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
            st.markdown("### Direct API (For Developers)")
            st.markdown("If you prefer to use the REST APIs directly without the frontend widget:")

            api_example = f"""# List available workflows
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

# Poll result
curl http://localhost:{api_port}/api/jobs/{{job_id}}"""
            st.code(api_example, language="bash")

        # --- TAB: Run Logs ---
        with app_tab4:
            st.markdown("### API Run Logs")
            app_runs = db.get_app_runs(app_id, limit=20)

            if not app_runs:
                st.info("No API runs recorded for this app yet.")
            else:
                for run in app_runs:
                    status_emoji = {"running": "🔄", "completed": "✅", "failed": "❌"}.get(run.get('status', ''), "❓")
                    with st.expander(f"{status_emoji} Run #{run['id']} — {run.get('status', 'N/A')} — {run.get('started_at', '')}"):
                        st.markdown(f"**Workflow ID**: {run.get('workflow_id', 'N/A')}")
                        st.markdown(f"**Status**: {run.get('status', 'N/A')}")
                        st.markdown(f"**Started**: {run.get('started_at', 'N/A')}")
                        st.markdown(f"**Completed**: {run.get('finished_at', 'N/A')}")
                        if run.get('result'):
                            st.text_area("Result", run['result'], height=150,
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

    @st.dialog("🚀 Guide & Placeholders", width="large")
    def show_guide():
        st.markdown("""
### 🤖 What is Alfredo?
**Alfredo** is an AI OS / agent orchestrator. You build a team of agents, wire them into workflows, then run those workflows with natural language from this dashboard or from **Telegram**.

Under the hood: **Master AI** plans and routes; **CrewAI** runs the crew; your data and keys stay in your machine (`.env` + database).

**Golden rule (do this order):**
1. Put API keys → 2. Register models → 3. Create agents → 4. Create tasks → 5. Assemble a workflow → 6. Chat / Telegram.

---

### 🗺️ What is in each section?
Use the left navigation. Click each page once so you know where things live.

| Page | What it is for |
|------|----------------|
| **🔐 API Vault** | Save provider API keys, register LLM models, pick Master AI + default agent model, Ollama URL, VRAM limit. **Start here.** |
| **🧱 Asset Builder** | Three tabs: **Knowledge Base** (RAG / documents), **Agent Caserma** (agents), **Tool Factory** (tools for agents). |
| **📋 Task Builder** | Create tasks assigned to an agent (description, inputs, tools, HITL, specialization). |
| **🧩 Workflow Assembler** | Build the workflow graph: Canvas (drag & drop) or Lanes. Blocks: Level, Task, Batch, HITL, Export, Input, Loop. |
| **📊 History & Monitoring** | Past runs, logs, status of executions. |
| **🪖 Local Model Training** | Optional fine-tuning / local training helpers (advanced). |
| **🔗 My Apps** | Connect external apps (env keys for DB/API) and link workflows to them. |

**Sidebar (top):**
- **🤖** (robot button) → this guide.
- **Main area (top left):** **🟢 Start / 🔴 Stop Bot Telegram** and **🤖 Telegram Bot Config**.

---

### 1️⃣ First setup — credentials & models (step by step)

#### A. File `.env` (once)
1. In the project folder, copy `.env.example` → `.env` (if you do not already have `.env`).
2. Never commit `.env` (it holds secrets).
3. You can fill keys in the file **or** from the UI below — both write the same `.env`.

#### B. Cloud API keys + models — page **API Vault** → tab **Load Models**
1. Open **API Vault** → tab **Load Models**.
2. Under **Load cloud models**:
   - **Select Key** (e.g. `GEMINI_API_KEY`, `OPENAI_API_KEY`, …) or **Custom...**
   - Paste the **API Key Value**
   - Click **Save to .env**
3. Alfredo **verifies the key**, downloads the provider’s model list, and **syncs models into the database**. Wait for success (🟢 on the key).
4. **Do not** put Telegram tokens here — use **Telegram Bot Config** (top left of the main area).

#### C. Set Master AI + default agent model
1. Go to **API Vault → System Setting**.
2. Under **Global Default Models**:
   - pick **Master AI** (orchestrator) → **💾 Save Model**
   - pick **Default Model for Agents** → **💾 Save Default Model**
3. Without these defaults, planning / agents may have nothing to run on.

#### D. Local models (Ollama / LM Studio) — optional
1. **Ollama** (Windows service on the desktop PC, **not** Docker)
   - `localhost:11434` / `http://192.168.178.105:11434` is only the API. Docker is Qdrant.
   - To **update**: run the new Windows installer from [ollama.com](https://ollama.com/download). Do **not** run `sc create` again, do **not** change the system variable `OLLAMA_HOST`, do **not** stop Docker.
   - In **API Vault → Load Models → Configure Local Providers → Ollama**: keep **Ollama Base URL** as `http://192.168.178.105:11434` (or `http://host.docker.internal:11434` if the dashboard itself runs in Docker). Leave the API key empty. **Connect**.
2. **LM Studio / Bionic (reuse GGUF already on disk)**
   - Bionic and LM Studio share `~/.lmstudio/models`. Start **LM Studio → Developer → Local Server** (port 1234).
   - In **Configure Local Providers → LM Studio / Bionic**, set the URL and **Connect**, then add the model as runtime **LM Studio**.
3. **Import GGUF into Ollama** (no re-download): tab **Import GGUF**, pick a file under `.lmstudio/models`, import. Needs a recent Ollama for new architectures (e.g. Qwen3.8). Uses extra disk because Ollama copies blobs.
4. In **Local Model registry**, add the model so agents can use it.
5. Set **Tool-calling support** per local model when adding it to the registry (`Auto/Yes/No`). Alfredo enforces this per-model flag, not just by provider.

#### E. Telegram — step by step
1. In Telegram, open **@BotFather** → `/newbot` → copy the token.
2. Get your numeric user id (e.g. talk to `@userinfobot` or similar) — it looks like `123456789`.
3. In Alfredo main area (top left) → **Telegram Bot Config**:
   - paste **Bot Token**
   - paste **Allowed User IDs** (comma-separated if more people: `111,222`)
   - **Save Telegram Config**
4. On the main toolbar click **🟢 Start Bot Telegram** (turns red when running).
5. Message your bot on Telegram. If the bot does not answer: check token, your id is in the allow-list, and the bot is started.

#### F. Optional extras
- **Outlook email tools**: configure Outlook email + app password where the Task / tool UI asks for them (saved in `.env`).
- **Headroom** (token compression): see **API Vault → System Setting**.
- **MAX_VRAM_GB**: same tab — limits parallel local GPU use.

---

### 2️⃣ Build a working crew (step by step)

#### 1) Agents — **Asset Builder → Agent Caserma**
1. Create an agent: name, role, backstory, tools.
2. Keep agents **generic** (e.g. Researcher, Writer). Specialize per task (see placeholders below).
3. Assign tools only if the agent’s model supports tools (`supports_tools` in Model Registry). This is per-model (cloud and local), not provider-wide.

#### 2) Knowledge (optional) — **Asset Builder → Knowledge Base**
Upload / index documents so agents can retrieve facts (RAG) instead of inventing.

#### 3) Tools (optional) — **Asset Builder → Tool Factory**
Generate or register tools, then attach them to agents in Caserma.

#### 4) Tasks — **Task Builder**
1. Pick an agent.
2. Write **Description** and **Expected Output**.
3. Add **Required Inputs** if the task needs parameters (`topic`, `language`, …).
4. Optional: specialization, HITL (human check), tools override, output schema.
5. Save. Tasks are shared assets: editing a task updates every workflow that uses it.

#### 5) Workflow — **Workflow Assembler**
1. Name the workflow.
2. Prefer **Canvas (drag & drop)**:
   - **Add Level** = columns (execution stages).
   - Drop **Task / Batch / HITL / Export / Input** into a level.
   - Wire outputs → inputs with the dots on task cards (or **Auto-link by levels**).
   - **Loop select**: marquee a group → create a **loop frame** (`while` or `for_each`).
3. Or use **Lanes (classic)** if you prefer form + columns.
4. **Update / Create Workflow** to save.
5. Opening **Edit** on an existing workflow rebuilds the canvas layout automatically.

**Block cheat-sheet**
- **Task** — does the work (runs an agent task).
- **Batch** — for-each over a JSON list inside one block.
- **HITL** — pause for human approve / feedback.
- **Export** — ask Master AI to write files (python, excel, word, …).
- **Input** — declares run keys for `{port}` wiring.
- **Level** — column / stage (not executed by itself).
- **Loop frame** — repeats a body: **`while`** until HITL approve and/or output regex (capped by `max_iterations`); **`for_each`** runs the body once per item in a JSON array from the previous result.

#### 6) Run it
- From Telegram / chat: describe what you want in plain language. Master AI picks the workflow, asks for missing inputs, then runs.
- Watch progress in **History & Monitoring**.

---

### 👤 Agent specialization
Keep one generic agent; change focus per task:

- Put **`{specialization}`** in the agent **Role** or **Backstory**  
  Example: `Researcher specialized in {specialization}`
- In the task, set the specialization field (e.g. `thermochemistry`).
- If you omit `{specialization}` in the agent text, Alfredo still appends the task specialization at the end.

---

### 📝 Placeholders (copy-paste into task text)

Use these in **Description** or **Expected Output**:

| Placeholder | Meaning |
|-------------|---------|
| `{variable_name}` | Any name you listed in **Required Inputs**. Alfredo asks once before run (same name shared across tasks = asked once). |
| `{user_input}` | The first user message that started the run. |
| `{previous_result}` or `{context}` | Output of the previous step (legacy fallback). |
| `{port_name}` | Value from Assembler **data wiring** (`inputs_map`): from a parent node or from a run **Input** block. |
| `{specialization}` | (In agent Role/Backstory) filled from the task specialization. |

**Tip:** Prefer named ports in Canvas (`inputs_map`) for clean graphs; keep `{previous_result}` for simple linear chains.

---

### 🛠️ Model & tool compatibility
- ✅ **Cloud** (OpenAI, Gemini, Anthropic, Groq, …): tools / function-calling OK.
- 🧠 **Local Ollama / LM Studio**: support is **per model** (`supports_tools`), with Auto guess available when you add models.
- ⚙️ In execution, Alfredo enables or strips tools based on that model flag, so you can keep modern local models tool-capable.
- Master AI and agents use the models you registered + set as defaults in API Vault.

---

### 🆘 If something does not work
1. API Vault shows 🔴 on the key? Save the key again.
2. No models in the dropdown? Register a model under **Load Models**.
3. Agent / task empty lists? Create agent before task; create task before workflow.
4. Telegram silent? Token + allowed IDs saved, then **Start Bot**.
5. Canvas empty on Edit? Wait for hydrate / click **Rebuild canvas layout**.
6. Loop never stops? Set HITL exit and/or regex, and a safe `max_iterations`.

Secrets → `.env` · Agents / tasks / workflows → database · This guide → robot button 🤖
        """)
        st.divider()
        st.info(
            "Keys and Telegram settings are stored in your local `.env`. "
            "Agents, tasks, workflows, and models live in the Alfredo database. "
            "Nothing here is shared unless you explicitly publish a workflow package (logic only — no secrets)."
        )

    # --- Sidebar branding (title + guide) ---
    with st.sidebar:
        st.markdown("""
        <style>
        [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
            display: flex;
            flex-direction: column;
        }
        [data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
            order: 0;
            flex-shrink: 0;
            padding-top: 0 !important;
            padding-bottom: 0 !important;
        }
        [data-testid="stSidebar"] [data-testid="stSidebarNav"] {
            order: 1;
            flex: 1;
            padding-top: 0 !important;
            border-top: 1px solid rgba(128, 128, 128, 0.22);
            margin-top: 2rem;
        }
        [data-testid="stSidebar"] [data-testid="stSidebarNav"] ul {
            padding-top: 0 !important;
            margin-top: 0 !important;
        }
        [data-testid="stSidebar"] [data-testid="stSidebarNav"] li:first-child {
            margin-top: 0 !important;
            padding-top: 0 !important;
        }
        [data-testid="stSidebar"] [data-testid="stColumn"]:has(+ [data-testid="stColumn"] .sidebar-brand-marker) .stButton > button {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0.1rem 0.2rem !important;
            min-height: auto !important;
            font-size: 1.55rem !important;
            line-height: 1 !important;
        }
        [data-testid="stSidebar"] [data-testid="stColumn"]:has(+ [data-testid="stColumn"] .sidebar-brand-marker) .stButton > button:hover {
            background: rgba(128, 128, 128, 0.12) !important;
            border-radius: 8px !important;
        }
        [data-testid="stSidebar"] .sidebar-brand-block {
            margin-bottom: 0;
        }
        [data-testid="stSidebar"] .sidebar-brand-title {
            font-size: 1.12rem;
            font-weight: 700;
            line-height: 1.25;
            margin: 0;
            letter-spacing: -0.02em;
        }
        [data-testid="stSidebar"] .sidebar-brand-subtitle {
            font-size: 0.82rem;
            line-height: 1.45;
            margin: 0.35rem 0 0 0;
            padding-bottom: 0;
            opacity: 0.72;
        }
        </style>
        """, unsafe_allow_html=True)

        brand_icon_col, brand_title_col = st.columns([0.16, 0.84], vertical_alignment="center")
        with brand_icon_col:
            if st.button("🤖", key="sidebar_guide_btn", help="Guide & Placeholders", use_container_width=True):
                show_guide()
        with brand_title_col:
            st.markdown(
                '<div class="sidebar-brand-marker"></div>'
                '<div class="sidebar-brand-block">'
                '<p class="sidebar-brand-title">AI Workflow Configurator</p>'
                '<p class="sidebar-brand-subtitle">A secure dashboard for building and managing AI agent workflows.</p>'
                '</div>',
                unsafe_allow_html=True,
            )

    # --- Main toolbar: Telegram actions (left, above page titles) ---
    st.markdown("""
    <style>
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) {
        gap: 0.85rem !important;
        width: 50% !important;
        max-width: 50% !important;
        margin-bottom: 1.25rem !important;
        align-items: center !important;
        justify-content: flex-start !important;
    }
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) > [data-testid="stLayoutWrapper"] {
        flex: 1 1 0 !important;
        width: auto !important;
        min-width: 0 !important;
        max-width: none !important;
        height: auto !important;
        overflow: visible !important;
        margin: 0 !important;
        padding-top: 0.5rem !important;
        padding-bottom: 0.5rem !important;
        box-sizing: border-box !important;
    }
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) .stButton,
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) [data-testid="stPopover"] {
        width: 100% !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) .stButton > button,
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) [data-testid="stPopover"] > button,
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) button[data-testid="stPopoverButton"] {
        width: 100% !important;
        min-width: 100% !important;
        min-height: 2.9rem !important;
        height: 2.9rem !important;
        font-weight: 600 !important;
        font-size: 0.95rem !important;
        padding: 0.55rem 1.25rem !important;
        border-radius: 10px !important;
        white-space: nowrap !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        box-sizing: border-box !important;
        margin: 0 !important;
    }
    </style>
    """, unsafe_allow_html=True)

    bot_running = is_bot_running()
    btn_label = "🔴 Stop Bot Telegram" if bot_running else "🟢 Start Bot Telegram"

    with st.container(horizontal=True, key="telegram_toolbar"):
        if st.button(btn_label, use_container_width=True, key="top_bot_toggle"):
            toggle_bot()
            st.rerun()
        with st.popover("🤖 Telegram Bot Config", use_container_width=True, key="top_tg_config"):
            st.markdown("### Telegram Vault")
            st.markdown("Configure your bot credentials for remote control.")

            env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
            current_env = dotenv_values(env_path)

            tg_token = current_env.get("TELEGRAM_BOT_TOKEN", "")
            tg_ids = current_env.get("TELEGRAM_ALLOWED_USER_IDS", "")

            token_status = "🟢" if tg_token.strip() else "🔴"
            ids_status = "🟢" if tg_ids.strip() else "🔴"

            st.markdown(f"{token_status} **Bot Token**")
            st.markdown(f"{ids_status} **Allowed User IDs**")

            with st.form("telegram_vault_form_standalone"):
                token_placeholder = "Saved (enter new to overwrite)" if tg_token.strip() else "123456789:ABCDEF..."
                ids_placeholder = "Saved (enter new to overwrite)" if tg_ids.strip() else "e.g. 123456789, 987654321"

                new_tg_token = st.text_input("Telegram Bot Token", type="password", value="", placeholder=token_placeholder)
                new_tg_ids = st.text_input("Allowed User IDs", type="password", value="", placeholder=ids_placeholder)
                st.caption("IDs must be comma-separated integers.")

                if st.form_submit_button("Save Telegram Config"):
                    if new_tg_token:
                        safe_set_key(env_path, "TELEGRAM_BOT_TOKEN", new_tg_token.strip())
                    if new_tg_ids:
                        safe_set_key(env_path, "TELEGRAM_ALLOWED_USER_IDS", new_tg_ids.strip())
                    st.success("Telegram configuration saved to .env!")
                    st.rerun()


    # Guide moved to st.dialog

    # Initialize session state for editing
    if 'editing_task_id' not in st.session_state:
        st.session_state.editing_task_id = None
    if 'editing_agent_id' not in st.session_state:
        st.session_state.editing_agent_id = None
    if 'editing_workflow_id' not in st.session_state:
        st.session_state.editing_workflow_id = None

    def page_api_vault():
        try:
            render_api_vault()
        except Exception as e:
            st.error(f"Errore nel caricamento di API Vault & Model Registry: {e}")
            st.exception(e)

    def page_asset_builder():
        tab_db, tab_agents, tab_tools = st.tabs(["Knowledge Base", "Agent Caserma", "Tool Factory"])
        
        with tab_db:
            try:
                render_knowledge_base()
            except Exception as e:
                st.error(f"Errore nel caricamento del Database: {e}")
                st.exception(e)
                
        with tab_agents:
            try:
                render_agent_caserma()
            except Exception as e:
                st.error(f"Errore nel caricamento di Agent Caserma: {e}")
                st.exception(e)

        with tab_tools:
            try:
                render_tool_factory()
            except Exception as e:
                st.error(f"Errore nel caricamento di Tool Factory: {e}")
                st.exception(e)

    def page_task_builder():
        try:
            render_task_builder()
        except Exception as e:
            st.error(f"Errore nel caricamento di Task Builder: {e}")
            st.exception(e)

    def page_workflow_assembler():
        try:
            render_workflow_assembler()
        except Exception as e:
            st.error(f"Errore nel caricamento di Workflow Assembler: {e}")
            st.exception(e)

    def page_history_monitoring():
        try:
            render_history_monitoring()
        except Exception as e:
            st.error(f"Errore nel caricamento di History & Monitoring: {e}")
            st.exception(e)

    def page_my_apps():
        try:
            render_my_apps()
        except Exception as e:
            st.error(f"Error loading My Apps: {e}")
            st.exception(e)

    def page_local_training():
        try:
            render_local_training()
        except Exception as e:
            st.error(f"Error loading Local Training: {e}")
            st.exception(e)

    pages = [
        st.Page(page_api_vault, title="API Vault", icon="🔐"),
        st.Page(page_asset_builder, title="Asset Builder", icon="🧱"),
        st.Page(page_task_builder, title="Task Builder", icon="📋"),
        st.Page(page_workflow_assembler, title="Workflow Assembler", icon="🧩"),
        st.Page(page_history_monitoring, title="History & Monitoring", icon="📊"),
        st.Page(page_local_training, title="Local Model Training", icon="🪖"),
        st.Page(page_my_apps, title="My Apps", icon="🔗")
    ]
    
    pg = st.navigation(pages)
    pg.run()


if __name__ == "__main__":
    main()