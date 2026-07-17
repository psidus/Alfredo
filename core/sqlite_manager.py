# core/db_manager.py

import sqlite3
import json
import os
import threading
from typing import List, Dict, Any, Optional

class SQLiteManager:
    """
    A class to manage all interactions with the SQLite database for the AI OS.

    This class encapsulates all SQL operations, including connection management,
    table creation, and full CRUD (Create, Read, Update, Delete) functionality
    for models, agents, tasks, and workflows.

    ARCHITECT'S NOTE: To ensure robust connection handling and prevent resource
    leaks, this class now implements the context manager protocol.
    Usage:
        with DBManager() as db:
            agents = db.read_all_agents()

    In long-running applications like Streamlit, manage the instance carefully
    to avoid creating new connections on every script rerun. Use Streamlit's
    resource caching:
        @st.cache_resource
        def get_db_manager():
            return DBManager()
        db = get_db_manager()
    """

    def __init__(self, db_path: str = 'db/database.sqlite'):
        """
        Initializes the DBManager, connects to the database, and creates tables.

        Args:
            db_path (str): The path to the SQLite database file.
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.cursor: Optional[sqlite3.Cursor] = None
        self.lock = threading.Lock()
        self._connect()
        self._create_tables()

    def _connect(self):
        """Establishes the database connection and cursor."""
        try:
            # Ensure the directory exists. This is a safe local file operation.
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            
            # ARCHITECT'S NOTE: Increased timeout to 10 seconds. This is critical.
            # Both Streamlit and the Telegram bot will access this file.
            # Without a timeout, concurrent write attempts would immediately fail
            # with a "database is locked" error. This timeout forces the second
            # process to wait, preventing crashes and improving stability.
            self.conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            
            # Use Row factory to get dictionary-like results
            self.conn.row_factory = sqlite3.Row
            self.cursor = self.conn.cursor()
            
            # Enable foreign key support, crucial for data integrity.
            self.cursor.execute("PRAGMA foreign_keys = ON;")
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"Database connection error: {e}")
            raise

    def close(self):
        """Closes the database connection gracefully."""
        if self.conn:
            self.conn.close()
            self.conn = None
            self.cursor = None
            
    # ARCHITECT'S NOTE: Added context manager methods for robust resource management.
    # This guarantees that the database connection is closed automatically,
    # preventing resource leaks even if errors occur.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _create_tables(self):
        """Creates all necessary tables if they don't already exist."""
        if not self.cursor or not self.conn:
            raise ConnectionError("Database is not connected.")

        # ARCHITECT'S NOTE: Schema is well-designed.
        # - UNIQUE constraints prevent duplicate logical entities (e.g., two models with the same name).
        # - `ON DELETE SET NULL` for foreign keys is a safe choice. It prevents cascading deletes
        #   that could lead to unintentional data loss while maintaining relational integrity.
        #   For example, deleting a Model won't delete the Agent that used it.
        sql_statements = [
            """
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                model_name TEXT NOT NULL UNIQUE,
                env_var_name TEXT,
                is_local BOOLEAN DEFAULT FALSE,
                vram_gb REAL DEFAULT 0.0
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                backstory TEXT,
                model_id INTEGER,
                tools_json TEXT,
                FOREIGN KEY (model_id) REFERENCES models (id) ON DELETE SET NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                expected_output TEXT NOT NULL,
                agent_id INTEGER,
                tools_json TEXT, -- List of tool names as JSON
                required_inputs_json TEXT, -- List of {key, prompt} objects as JSON
                agent_specialization TEXT, -- Optional task-level agent specialization
                name TEXT,
                model_id INTEGER,
                human_validation INTEGER DEFAULT 0, -- Indicates if task pauses for human feedback
                max_input_context INTEGER DEFAULT 0,
                max_output_tokens INTEGER DEFAULT 0,
                output_pydantic TEXT,
                FOREIGN KEY (agent_id) REFERENCES agents (id) ON DELETE SET NULL,
                FOREIGN KEY (model_id) REFERENCES models (id) ON DELETE SET NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                task_ids_json TEXT NOT NULL, -- List of task IDs as JSON
                expected_exports TEXT DEFAULT '[]', -- List of output formats as JSON
                requires_human_check INTEGER DEFAULT 0,
                has_deletion_warning INTEGER DEFAULT 0,
                export_instructions TEXT DEFAULT '' -- Optional guidance for Master AI export generation
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS context_memory (
                session_id TEXT PRIMARY KEY,
                last_output TEXT,
                accumulated_context TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id INTEGER,
                status TEXT, -- 'running', 'completed', 'failed'
                result TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                current_task_idx INTEGER DEFAULT 0,
                task_outputs TEXT,
                inputs TEXT,
                source TEXT,
                in_flight_tasks TEXT DEFAULT '[]',
                FOREIGN KEY (workflow_id) REFERENCES workflows (id) ON DELETE SET NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS vector_databases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS hitl_requests (
                chat_id TEXT PRIMARY KEY,
                question TEXT,
                answer TEXT,
                status TEXT DEFAULT 'pending', -- 'pending', 'replied'
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT,
                description TEXT,
                api_key TEXT NOT NULL UNIQUE,
                db_env_key TEXT,
                api_env_key TEXT,
                api_base_url TEXT,
                db_type TEXT DEFAULT 'sqlite',
                app_root_path TEXT,
                config JSON DEFAULT '{}',
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        ]
        try:
            for statement in sql_statements:
                self.cursor.execute(statement)
            self.conn.commit()
            
            # Migration to add env_var_name if upgrading from older version
            try:
                self.cursor.execute("ALTER TABLE models ADD COLUMN env_var_name TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass 

            # Migration to add is_local if upgrading
            try:
                self.cursor.execute("ALTER TABLE models ADD COLUMN is_local BOOLEAN DEFAULT FALSE;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass 
                
            # Migration to add vram_gb if upgrading
            try:
                self.cursor.execute("ALTER TABLE models ADD COLUMN vram_gb REAL DEFAULT 0.0;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass
                
            # Migration to add supports_tools to models if upgrading
            try:
                self.cursor.execute("ALTER TABLE models ADD COLUMN supports_tools BOOLEAN DEFAULT TRUE;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass
                
            # Migration to add tools_json to tasks if upgrading
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN tools_json TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add required_inputs_json to tasks if upgrading
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN required_inputs_json TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add has_deletion_warning to workflows if upgrading
            try:
                self.cursor.execute("ALTER TABLE workflows ADD COLUMN has_deletion_warning INTEGER DEFAULT 0;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add vector_dbs_json to tasks if upgrading
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN vector_dbs_json TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add max_input_context to tasks if upgrading
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN max_input_context INTEGER DEFAULT 0;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add max_output_tokens to tasks if upgrading
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN max_output_tokens INTEGER DEFAULT 0;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add output_pydantic to tasks
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN output_pydantic TEXT;")
                self.conn.commit()
            except BaseException:
                pass


            # Migration to add current_task_idx, task_outputs, and inputs to workflow_runs
            try:
                self.cursor.execute("ALTER TABLE workflow_runs ADD COLUMN current_task_idx INTEGER DEFAULT 0;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add in_flight_tasks to workflow_runs
            try:
                self.cursor.execute("ALTER TABLE workflow_runs ADD COLUMN in_flight_tasks TEXT DEFAULT '[]';")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add human_validation to tasks if upgrading
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN human_validation INTEGER DEFAULT 0;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                self.cursor.execute("ALTER TABLE workflow_runs ADD COLUMN task_outputs TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                self.cursor.execute("ALTER TABLE workflow_runs ADD COLUMN inputs TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add agent_specialization to tasks if upgrading from older version
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN agent_specialization TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add name to tasks if upgrading from older version
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN name TEXT;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass
 
            # Migration to add model_id to tasks if upgrading from older version
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN model_id INTEGER REFERENCES models(id) ON DELETE SET NULL;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add export_instructions to workflows (for Master AI guidance)
            try:
                self.cursor.execute("ALTER TABLE workflows ADD COLUMN export_instructions TEXT DEFAULT '';")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add app_id to workflows (for external app integration)
            try:
                self.cursor.execute("ALTER TABLE workflows ADD COLUMN app_id INTEGER DEFAULT NULL REFERENCES apps(id) ON DELETE SET NULL;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration to add source to workflow_runs (to track API vs Telegram origin)
            try:
                self.cursor.execute("ALTER TABLE workflow_runs ADD COLUMN source TEXT DEFAULT 'telegram';")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

        except sqlite3.Error as e:
            print(f"Error creating tables: {e}")
            self.conn.rollback()
            raise

    # --- Helper Methods ---
    def _to_dict(self, row: sqlite3.Row) -> Optional[Dict[str, Any]]:
        """Converts a sqlite3.Row object to a dictionary."""
        return dict(row) if row else None
    
    def _process_json_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Deserializes JSON fields in a dictionary."""
        # ARCHITECT'S NOTE: Storing complex types as JSON strings is acceptable for this
        # project's scope. It avoids complex table joins for simple lists.
        # This implementation is safe and correct.
        if 'tools_json' in data and data['tools_json']:
            try:
                data['tools'] = json.loads(data['tools_json'])
            except json.JSONDecodeError:
                data['tools'] = [] 
            del data['tools_json']
        if 'task_ids_json' in data and data['task_ids_json']:
            try:
                data['task_ids'] = json.loads(data['task_ids_json'])
            except json.JSONDecodeError:
                data['task_ids'] = [] 
            del data['task_ids_json']
        if 'required_inputs_json' in data and data['required_inputs_json']:
            try:
                data['required_inputs'] = json.loads(data['required_inputs_json'])
            except json.JSONDecodeError:
                data['required_inputs'] = [] 
            del data['required_inputs_json']
        if 'vector_dbs_json' in data and data['vector_dbs_json']:
            try:
                data['vector_dbs'] = json.loads(data['vector_dbs_json'])
            except json.JSONDecodeError:
                data['vector_dbs'] = [] 
            del data['vector_dbs_json']
        if 'expected_exports' in data and isinstance(data['expected_exports'], str):
            try:
                data['expected_exports'] = json.loads(data['expected_exports'])
            except json.JSONDecodeError:
                data['expected_exports'] = [data['expected_exports']] if data['expected_exports'] else []
        return data

    # --- Vector Databases CRUD ---
    def create_vector_db(self, name: str, path: str, provider: str, model_name: str) -> int:
        normalized_path = path.replace('\\', '/')
        sql = "INSERT INTO vector_databases (name, path, provider, model_name) VALUES (?, ?, ?, ?)"
        self.cursor.execute(sql, (name, normalized_path, provider, model_name))
        self.conn.commit()
        return self.cursor.lastrowid

    def read_all_vector_dbs(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM vector_databases ORDER BY created_at DESC"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        dbs = [self._to_dict(row) for row in rows]
        for db in dbs:
            if db.get('path'):
                db['path'] = db['path'].replace('\\', '/')
        return dbs

    def delete_vector_db(self, db_id: int) -> int:
        sql = "DELETE FROM vector_databases WHERE id = ?"
        self.cursor.execute(sql, (db_id,))
        self.conn.commit()
        return self.cursor.rowcount

    # --- Models CRUD ---
    # ARCHITECT'S NOTE: All CRUD methods correctly use parameterized queries (`?`),
    # which is the standard and effective way to prevent SQL injection attacks.
    # The logic is sound and maps directly to the defined schema.
    
    def create_model(self, provider: str, model_name: str, env_var_name: str = "", is_local: bool = False, vram_gb: float = 0.0, supports_tools: bool = True) -> int:
        sql = "INSERT INTO models (provider, model_name, env_var_name, is_local, vram_gb, supports_tools) VALUES (?, ?, ?, ?, ?, ?)"
        self.cursor.execute(sql, (provider, model_name, env_var_name, 1 if is_local else 0, vram_gb, 1 if supports_tools else 0))
        self.conn.commit()
        return self.cursor.lastrowid

    def read_model(self, model_id: int) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM models WHERE id = ?"
        self.cursor.execute(sql, (model_id,))
        row = self.cursor.fetchone()
        return self._to_dict(row)

    def read_model_by_name(self, model_name: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM models WHERE model_name = ? LIMIT 1"
        self.cursor.execute(sql, (model_name,))
        row = self.cursor.fetchone()
        return self._to_dict(row)

    def read_all_models(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM models ORDER BY provider, model_name"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        return [self._to_dict(row) for row in rows]

    def update_model(self, model_id: int, provider: str, model_name: str, env_var_name: str = "", is_local: bool = False, vram_gb: float = 0.0, supports_tools: bool = True) -> int:
        sql = "UPDATE models SET provider = ?, model_name = ?, env_var_name = ?, is_local = ?, vram_gb = ?, supports_tools = ? WHERE id = ?"
        self.cursor.execute(sql, (provider, model_name, env_var_name, 1 if is_local else 0, vram_gb, 1 if supports_tools else 0, model_id))
        self.conn.commit()
        return self.cursor.rowcount

    def delete_model(self, model_id: int) -> int:
        sql = "DELETE FROM models WHERE id = ?"
        self.cursor.execute(sql, (model_id,))
        self.conn.commit()
        return self.cursor.rowcount

    # --- Agents CRUD ---
    def create_agent(self, name: str, role: str, backstory: str, model_id: Optional[int], tools: List[str]) -> int:
        tools_json = json.dumps(tools)
        sql = "INSERT INTO agents (name, role, backstory, model_id, tools_json) VALUES (?, ?, ?, ?, ?)"
        self.cursor.execute(sql, (name, role, backstory, model_id, tools_json))
        self.conn.commit()
        return self.cursor.lastrowid

    def read_agent(self, agent_id: int) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM agents WHERE id = ?"
        self.cursor.execute(sql, (agent_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        agent_dict = self._to_dict(row)
        return self._process_json_fields(agent_dict)

    def read_all_agents(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM agents ORDER BY name"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        processed_rows = []
        for row in rows:
            agent_dict = self._to_dict(row)
            processed_rows.append(self._process_json_fields(agent_dict))
        return processed_rows

    def update_agent(self, agent_id: int, name: str, role: str, backstory: str, model_id: Optional[int], tools: List[str]) -> int:
        tools_json = json.dumps(tools)
        sql = """
        UPDATE agents 
        SET name = ?, role = ?, backstory = ?, model_id = ?, tools_json = ? 
        WHERE id = ?
        """
        self.cursor.execute(sql, (name, role, backstory, model_id, tools_json, agent_id))
        self.conn.commit()
        return self.cursor.rowcount

    def delete_agent(self, agent_id: int) -> int:
        sql = "DELETE FROM agents WHERE id = ?"
        self.cursor.execute(sql, (agent_id,))
        self.conn.commit()
        return self.cursor.rowcount

    # --- Tasks CRUD ---
    def create_task(self, description: str, expected_output: str, agent_id: Optional[int], tools: List[str] = None, required_inputs: List[Dict[str, str]] = None, vector_dbs: List[str] = None, agent_specialization: Optional[str] = None, name: Optional[str] = None, model_id: Optional[int] = None, human_validation: bool = False, max_input_context: int = 0, max_output_tokens: int = 0, output_pydantic: Optional[str] = None) -> int:
        tools = tools or []
        required_inputs = required_inputs or []
        vector_dbs = vector_dbs or []
        tools_json = json.dumps(tools)
        required_inputs_json = json.dumps(required_inputs)
        vector_dbs_json = json.dumps(vector_dbs)
        sql = "INSERT INTO tasks (description, expected_output, agent_id, tools_json, required_inputs_json, vector_dbs_json, agent_specialization, name, model_id, human_validation, max_input_context, max_output_tokens, output_pydantic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        self.cursor.execute(sql, (description, expected_output, agent_id, tools_json, required_inputs_json, vector_dbs_json, agent_specialization or None, name or None, model_id, int(human_validation), max_input_context, max_output_tokens, output_pydantic))
        self.conn.commit()
        return self.cursor.lastrowid

    def read_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE id = ?"
        self.cursor.execute(sql, (task_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        task_dict = self._to_dict(row)
        return self._process_json_fields(task_dict)

    def read_all_tasks(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks ORDER BY id"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        processed_rows = []
        for row in rows:
            task_dict = self._to_dict(row)
            processed_rows.append(self._process_json_fields(task_dict))
        return processed_rows

    def update_task(self, task_id: int, description: str, expected_output: str, agent_id: Optional[int], tools: List[str] = None, required_inputs: List[Dict[str, str]] = None, vector_dbs: List[str] = None, agent_specialization: Optional[str] = None, name: Optional[str] = None, model_id: Optional[int] = None, human_validation: bool = False, max_input_context: int = 0, max_output_tokens: int = 0, output_pydantic: Optional[str] = None) -> int:
        tools = tools or []
        required_inputs = required_inputs or []
        vector_dbs = vector_dbs or []
        tools_json = json.dumps(tools)
        required_inputs_json = json.dumps(required_inputs)
        vector_dbs_json = json.dumps(vector_dbs)
        sql = """
        UPDATE tasks 
        SET description = ?, expected_output = ?, agent_id = ?, tools_json = ?, required_inputs_json = ?, vector_dbs_json = ?, agent_specialization = ?, name = ?, model_id = ?, human_validation = ?, max_input_context = ?, max_output_tokens = ?, output_pydantic = ?
        WHERE id = ?
        """
        self.cursor.execute(sql, (description, expected_output, agent_id, tools_json, required_inputs_json, vector_dbs_json, agent_specialization or None, name or None, model_id, int(human_validation), max_input_context, max_output_tokens, output_pydantic, task_id))
        self.conn.commit()
        return self.cursor.rowcount

    def delete_task(self, task_id: int) -> int:
        """
        Deletes a task, removes it from any workflows, re-indexes remaining tasks,
        and sets a warning flag on affected workflows.
        """
        task_id = int(task_id)
        
        # 1. Identify all workflows that contain this task and remove it
        self.cursor.execute("SELECT id, task_ids_json FROM workflows")
        workflows = self.cursor.fetchall()
        
        for wf in workflows:
            try:
                task_ids = json.loads(wf['task_ids_json'])
            except (json.JSONDecodeError, TypeError):
                task_ids = []
                
            # If the deleted task is in the workflow, remove it and set warning
            def get_tid(t):
                return int(t.get('task_id', 0)) if isinstance(t, dict) else int(t)

            if any(get_tid(tid) == task_id for tid in task_ids):
                new_task_ids = [tid for tid in task_ids if get_tid(tid) != task_id]
                self.cursor.execute(
                    "UPDATE workflows SET task_ids_json = ?, has_deletion_warning = 1 WHERE id = ?",
                    (json.dumps(new_task_ids), wf['id'])
                )

        # 2. Delete the specific task from the tasks table
        self.cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        
        # 3. Re-index remaining tasks to ensure sequential IDs (1, 2, 3...)
        self.cursor.execute("SELECT id FROM tasks ORDER BY id")
        remaining_tasks = self.cursor.fetchall()
        
        if remaining_tasks:
            # Create a mapping from OLD_ID to NEW_ID (1-based index)
            mapping = {int(row['id']): i + 1 for i, row in enumerate(remaining_tasks)}
            
            # Use a temporary table to safely update task IDs without PK collisions
            self.cursor.execute("CREATE TEMP TABLE tasks_backup AS SELECT * FROM tasks")
            self.cursor.execute("DELETE FROM tasks")
            
            # Get column names to ensure we re-insert correctly
            self.cursor.execute("PRAGMA table_info(tasks)")
            columns = [col['name'] for col in self.cursor.fetchall()]
            cols_str = ", ".join(columns)
            placeholders = ", ".join(["?"] * len(columns))
            
            self.cursor.execute("SELECT * FROM tasks_backup ORDER BY id")
            all_task_data = self.cursor.fetchall()
            
            for i, task_data in enumerate(all_task_data):
                task_dict = dict(task_data)
                task_dict['id'] = i + 1 # Assign new sequential ID
                
                vals = [task_dict[col] for col in columns]
                self.cursor.execute(f"INSERT INTO tasks ({cols_str}) VALUES ({placeholders})", vals)
            
            self.cursor.execute("DROP TABLE tasks_backup")
            
            # 4. Update all workflows: apply the mapping AND remove any orphaned IDs
            self.cursor.execute("SELECT id, task_ids_json FROM workflows")
            current_workflows = self.cursor.fetchall()
            for wf in current_workflows:
                try:
                    t_ids = json.loads(wf['task_ids_json'])
                    # Filter: keep only IDs that still exist in our mapping, and update them
                    new_t_ids = []
                    for tid in t_ids:
                        actual_tid = int(tid.get('task_id', 0)) if isinstance(tid, dict) else int(tid)
                        if actual_tid in mapping:
                            if isinstance(tid, dict):
                                new_dict = dict(tid)
                                new_dict['task_id'] = mapping[actual_tid]
                                new_t_ids.append(new_dict)
                            else:
                                new_t_ids.append(mapping[actual_tid])
                    
                    self.cursor.execute("UPDATE workflows SET task_ids_json = ? WHERE id = ?", 
                                        (json.dumps(new_t_ids), wf['id']))
                except (json.JSONDecodeError, TypeError):
                    continue
            
            # Reset SQLite autoincrement sequence
            self.cursor.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'tasks'", (len(all_task_data),))
        else:
            # If no tasks left, reset sequence
            self.cursor.execute("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'tasks'")

        self.conn.commit()
        return 1

    def dismiss_workflow_warning(self, workflow_id: int) -> None:
        """Clears the deletion warning flag for a workflow."""
        self.cursor.execute("UPDATE workflows SET has_deletion_warning = 0 WHERE id = ?", (workflow_id,))
        self.conn.commit()

    # --- Workflows CRUD ---
    def create_workflow(self, name: str, task_ids: list, requires_human_check: bool, expected_exports: List[str] = None, export_instructions: str = None, app_id: Optional[int] = None) -> int:
        task_ids_json = json.dumps(task_ids)
        expected_exports_json = json.dumps(expected_exports or [])
        export_instructions_str = export_instructions or ""
        sql = "INSERT INTO workflows (name, task_ids_json, expected_exports, requires_human_check, export_instructions, app_id) VALUES (?, ?, ?, ?, ?, ?)"
        self.cursor.execute(sql, (name, task_ids_json, expected_exports_json, requires_human_check, export_instructions_str, app_id))
        self.conn.commit()
        return self.cursor.lastrowid

    def read_workflow(self, workflow_id: int) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM workflows WHERE id = ?"
        self.cursor.execute(sql, (workflow_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        workflow_dict = self._to_dict(row)
        return self._process_json_fields(workflow_dict)

    def read_all_workflows(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM workflows ORDER BY name"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        processed_rows = []
        for row in rows:
            workflow_dict = self._to_dict(row)
            processed_rows.append(self._process_json_fields(workflow_dict))
        return processed_rows

    def update_workflow(self, workflow_id: int, name: str, task_ids: list, requires_human_check: bool, expected_exports: List[str] = None, export_instructions: str = None) -> int:
        task_ids_json = json.dumps(task_ids)
        expected_exports_json = json.dumps(expected_exports or [])
        export_instructions_str = export_instructions or ""
        sql = """
        UPDATE workflows 
        SET name = ?, task_ids_json = ?, expected_exports = ?, requires_human_check = ?, export_instructions = ? 
        WHERE id = ?
        """
        self.cursor.execute(sql, (name, task_ids_json, expected_exports_json, requires_human_check, export_instructions_str, workflow_id))
        self.conn.commit()
        return self.cursor.rowcount

    def delete_workflow(self, workflow_id: int) -> int:
        """
        Deletes a workflow and re-indexes remaining workflows to ensure sequential IDs.
        """
        workflow_id = int(workflow_id)
        self.cursor.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        
        # Re-index remaining workflows
        self.cursor.execute("SELECT id FROM workflows ORDER BY id")
        remaining = self.cursor.fetchall()
        
        if remaining:
            # Temporary table strategy for safe ID updates
            self.cursor.execute("CREATE TEMP TABLE workflows_backup AS SELECT * FROM workflows")
            self.cursor.execute("DELETE FROM workflows")
            
            self.cursor.execute("PRAGMA table_info(workflows)")
            columns = [col['name'] for col in self.cursor.fetchall()]
            cols_str = ", ".join(columns)
            placeholders = ", ".join(["?"] * len(columns))
            
            self.cursor.execute("SELECT * FROM workflows_backup ORDER BY id")
            all_wf_data = self.cursor.fetchall()
            
            for i, wf_data in enumerate(all_wf_data):
                wf_dict = dict(wf_data)
                wf_dict['id'] = i + 1
                
                vals = [wf_dict[col] for col in columns]
                self.cursor.execute(f"INSERT INTO workflows ({cols_str}) VALUES ({placeholders})", vals)
            
            self.cursor.execute("DROP TABLE workflows_backup")
            self.cursor.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'workflows'", (len(all_wf_data),))
        else:
            self.cursor.execute("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'workflows'")
            
        self.conn.commit()
        return 1

    # --- Context Memory CRUD ---
    def update_context(self, session_id: str, last_output: str, accumulated_context: str = "") -> None:
        sql = """
        INSERT INTO context_memory (session_id, last_output, accumulated_context, updated_at) 
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(session_id) DO UPDATE SET 
            last_output=excluded.last_output, 
            accumulated_context=excluded.accumulated_context,
            updated_at=CURRENT_TIMESTAMP
        """
        self.cursor.execute(sql, (session_id, last_output, accumulated_context))
        self.conn.commit()

    def get_context(self, session_id: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM context_memory WHERE session_id = ?"
        self.cursor.execute(sql, (session_id,))
        row = self.cursor.fetchone()
        return self._to_dict(row)

    def clear_context(self, session_id: str) -> int:
        sql = "DELETE FROM context_memory WHERE session_id = ?"
        self.cursor.execute(sql, (session_id,))
        self.conn.commit()
        return self.cursor.rowcount
        
    def read_all_contexts(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM context_memory ORDER BY updated_at DESC"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        return [self._to_dict(row) for row in rows]

    # --- Workflow Runs CRUD ---
    def create_run(self, workflow_id: int, status: str = 'running', inputs: dict = None, source: str = 'telegram') -> int:
        inputs_json = json.dumps(inputs) if inputs else None
        sql = "INSERT INTO workflow_runs (workflow_id, status, current_task_idx, task_outputs, inputs, source, in_flight_tasks) VALUES (?, ?, 0, '{}', ?, ?, '[]')"
        self.cursor.execute(sql, (workflow_id, status, inputs_json, source))
        self.conn.commit()
        return self.cursor.lastrowid

    def update_run(self, run_id: int, status: str, result: str = "", current_task_idx: int = None, task_outputs: dict = None, in_flight_tasks: list = None) -> None:
        updates = ["status = ?", "result = ?", "finished_at = (CASE WHEN ? IN ('completed', 'failed') THEN CURRENT_TIMESTAMP ELSE finished_at END)"]
        params = [status, result, status]

        if current_task_idx is not None:
            updates.append("current_task_idx = ?")
            params.append(current_task_idx)
            
        if task_outputs is not None:
            updates.append("task_outputs = ?")
            params.append(json.dumps(task_outputs))
            
        if in_flight_tasks is not None:
            updates.append("in_flight_tasks = ?")
            params.append(json.dumps(in_flight_tasks))
            
        sql = f"UPDATE workflow_runs SET {', '.join(updates)} WHERE id = ?"
        params.append(run_id)
        
        with self.lock:
            self.cursor.execute(sql, tuple(params))
            self.conn.commit()

    def read_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM workflow_runs WHERE id = ?"
        self.cursor.execute(sql, (run_id,))
        row = self.cursor.fetchone()
        return self._to_dict(row)

    def read_all_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?"
        self.cursor.execute(sql, (limit,))
        rows = self.cursor.fetchall()
        return [self._to_dict(row) for row in rows]

    def delete_run(self, run_id: int) -> int:
        """Deletes a specific workflow run record."""
        sql = "DELETE FROM workflow_runs WHERE id = ?"
        self.cursor.execute(sql, (run_id,))
        self.conn.commit()
        return self.cursor.rowcount

    def clear_all_runs(self) -> int:
        """Clears all history from the workflow_runs table."""
        sql = "DELETE FROM workflow_runs"
        self.cursor.execute(sql)
        # Reset the autoincrement counter for this table
        self.cursor.execute("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'workflow_runs'")
        self.conn.commit()
        return self.cursor.rowcount

    # --- HITL Requests CRUD ---
    def create_hitl_request(self, chat_id: str, question: str) -> None:
        sql = """
        INSERT INTO hitl_requests (chat_id, question, status, answer, updated_at) 
        VALUES (?, ?, 'pending', NULL, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET 
            question=excluded.question, 
            status='pending', 
            answer=NULL,
            updated_at=CURRENT_TIMESTAMP
        """
        self.cursor.execute(sql, (chat_id, question))
        self.conn.commit()

    def set_hitl_answer(self, chat_id: str, answer: str) -> bool:
        sql = "UPDATE hitl_requests SET answer = ?, status = 'replied', updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?"
        self.cursor.execute(sql, (answer, chat_id))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def get_hitl_request(self, chat_id: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM hitl_requests WHERE chat_id = ?"
        self.cursor.execute(sql, (chat_id,))
        row = self.cursor.fetchone()
        return self._to_dict(row)

    def delete_hitl_request(self, chat_id: str) -> None:
        sql = "DELETE FROM hitl_requests WHERE chat_id = ?"
        self.cursor.execute(sql, (chat_id,))
        self.conn.commit()

    # --- External Apps CRUD ---
    def create_app(self, name: str, display_name: str = None, description: str = None,
                   db_env_key: str = None, api_env_key: str = None, api_base_url: str = None,
                   db_type: str = 'sqlite', app_root_path: str = None, config: dict = None) -> int:
        """Creates a new external app registration and generates a UUID API key."""
        import uuid
        api_key = str(uuid.uuid4())
        config_json = json.dumps(config or {})
        sql = """INSERT INTO apps (name, display_name, description, api_key, db_env_key, 
                 api_env_key, api_base_url, db_type, app_root_path, config) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        self.cursor.execute(sql, (name, display_name or name, description or '',
                                  api_key, db_env_key, api_env_key, api_base_url,
                                  db_type, app_root_path, config_json))
        self.conn.commit()
        return self.cursor.lastrowid

    def get_app(self, app_id: int) -> Optional[Dict[str, Any]]:
        """Reads a single app by ID."""
        sql = "SELECT * FROM apps WHERE id = ?"
        self.cursor.execute(sql, (app_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        app_dict = self._to_dict(row)
        # Parse config JSON
        if app_dict.get('config'):
            try:
                app_dict['config'] = json.loads(app_dict['config'])
            except json.JSONDecodeError:
                app_dict['config'] = {}
        return app_dict

    def get_app_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Reads a single app by its unique slug name."""
        sql = "SELECT * FROM apps WHERE name = ?"
        self.cursor.execute(sql, (name,))
        row = self.cursor.fetchone()
        if not row:
            return None
        app_dict = self._to_dict(row)
        if app_dict.get('config'):
            try:
                app_dict['config'] = json.loads(app_dict['config'])
            except json.JSONDecodeError:
                app_dict['config'] = {}
        return app_dict

    def get_app_by_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        """Reads a single app by its API key (used for SDK authentication)."""
        sql = "SELECT * FROM apps WHERE api_key = ?"
        self.cursor.execute(sql, (api_key,))
        row = self.cursor.fetchone()
        if not row:
            return None
        app_dict = self._to_dict(row)
        if app_dict.get('config'):
            try:
                app_dict['config'] = json.loads(app_dict['config'])
            except json.JSONDecodeError:
                app_dict['config'] = {}
        return app_dict

    def get_all_apps(self) -> List[Dict[str, Any]]:
        """Returns all registered external apps."""
        sql = "SELECT * FROM apps ORDER BY created_at DESC"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        result = []
        for row in rows:
            app_dict = self._to_dict(row)
            if app_dict.get('config'):
                try:
                    app_dict['config'] = json.loads(app_dict['config'])
                except json.JSONDecodeError:
                    app_dict['config'] = {}
            result.append(app_dict)
        return result

    def update_app(self, app_id: int, **kwargs) -> int:
        """Updates an app record. Accepts any column as keyword argument."""
        if not kwargs:
            return 0
        # Handle config serialization
        if 'config' in kwargs and isinstance(kwargs['config'], dict):
            kwargs['config'] = json.dumps(kwargs['config'])
        set_clauses = [f"{key} = ?" for key in kwargs.keys()]
        values = list(kwargs.values()) + [app_id]
        sql = f"UPDATE apps SET {', '.join(set_clauses)} WHERE id = ?"
        self.cursor.execute(sql, values)
        self.conn.commit()
        return self.cursor.rowcount

    def delete_app(self, app_id: int) -> int:
        """Deletes an app and unlinks any workflows associated with it."""
        # Unlink workflows first (set app_id to NULL)
        self.cursor.execute("UPDATE workflows SET app_id = NULL WHERE app_id = ?", (app_id,))
        sql = "DELETE FROM apps WHERE id = ?"
        self.cursor.execute(sql, (app_id,))
        self.conn.commit()
        return self.cursor.rowcount

    def regenerate_app_api_key(self, app_id: int) -> str:
        """Generates a new API key for an app and returns it."""
        import uuid
        new_key = str(uuid.uuid4())
        self.cursor.execute("UPDATE apps SET api_key = ? WHERE id = ?", (new_key, app_id))
        self.conn.commit()
        return new_key

    def get_app_workflows(self, app_id: int) -> List[Dict[str, Any]]:
        """Returns all workflows linked to a specific app."""
        sql = "SELECT * FROM workflows WHERE app_id = ? ORDER BY name"
        self.cursor.execute(sql, (app_id,))
        rows = self.cursor.fetchall()
        processed = []
        for row in rows:
            wf_dict = self._to_dict(row)
            processed.append(self._process_json_fields(wf_dict))
        return processed

    def get_app_runs(self, app_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns recent runs triggered via API for workflows belonging to an app."""
        sql = """
        SELECT wr.* FROM workflow_runs wr
        JOIN workflows w ON wr.workflow_id = w.id
        WHERE w.app_id = ? AND wr.source = 'api'
        ORDER BY wr.started_at DESC LIMIT ?
        """
        self.cursor.execute(sql, (app_id, limit))
        rows = self.cursor.fetchall()
        return [self._to_dict(row) for row in rows]
