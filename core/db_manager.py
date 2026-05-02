# core/db_manager.py

import sqlite3
import json
import os
from typing import List, Dict, Any, Optional

class DBManager:
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
                is_local BOOLEAN DEFAULT 0
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
                tools_json TEXT,
                FOREIGN KEY (agent_id) REFERENCES agents (id) ON DELETE SET NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                task_ids_json TEXT,
                requires_human_check BOOLEAN NOT NULL DEFAULT 0 CHECK (requires_human_check IN (0, 1))
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS context_memory (
                session_id TEXT PRIMARY KEY,
                last_output TEXT,
                accumulated_context TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                self.cursor.execute("ALTER TABLE models ADD COLUMN is_local BOOLEAN DEFAULT 0;")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass 
                
            # Migration to add tools_json to tasks if upgrading
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN tools_json TEXT;")
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
                data['tools'] = [] # Gracefully handle corrupt JSON data
            del data['tools_json']
        if 'task_ids_json' in data and data['task_ids_json']:
            try:
                data['task_ids'] = json.loads(data['task_ids_json'])
            except json.JSONDecodeError:
                data['task_ids'] = [] # Gracefully handle corrupt JSON data
            del data['task_ids_json']
        return data

    # --- Models CRUD ---
    # ARCHITECT'S NOTE: All CRUD methods correctly use parameterized queries (`?`),
    # which is the standard and effective way to prevent SQL injection attacks.
    # The logic is sound and maps directly to the defined schema.
    
    def create_model(self, provider: str, model_name: str, env_var_name: str = "", is_local: bool = False) -> int:
        sql = "INSERT INTO models (provider, model_name, env_var_name, is_local) VALUES (?, ?, ?, ?)"
        self.cursor.execute(sql, (provider, model_name, env_var_name, 1 if is_local else 0))
        self.conn.commit()
        return self.cursor.lastrowid

    def read_model(self, model_id: int) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM models WHERE id = ?"
        self.cursor.execute(sql, (model_id,))
        row = self.cursor.fetchone()
        return self._to_dict(row)

    def read_all_models(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM models ORDER BY provider, model_name"
        self.cursor.execute(sql)
        rows = self.cursor.fetchall()
        return [self._to_dict(row) for row in rows]

    def update_model(self, model_id: int, provider: str, model_name: str, env_var_name: str = "", is_local: bool = False) -> int:
        sql = "UPDATE models SET provider = ?, model_name = ?, env_var_name = ?, is_local = ? WHERE id = ?"
        self.cursor.execute(sql, (provider, model_name, env_var_name, 1 if is_local else 0, model_id))
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
    def create_task(self, description: str, expected_output: str, agent_id: Optional[int], tools: List[str] = None) -> int:
        tools = tools or []
        tools_json = json.dumps(tools)
        sql = "INSERT INTO tasks (description, expected_output, agent_id, tools_json) VALUES (?, ?, ?, ?)"
        self.cursor.execute(sql, (description, expected_output, agent_id, tools_json))
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

    def update_task(self, task_id: int, description: str, expected_output: str, agent_id: Optional[int], tools: List[str] = None) -> int:
        tools = tools or []
        tools_json = json.dumps(tools)
        sql = """
        UPDATE tasks 
        SET description = ?, expected_output = ?, agent_id = ?, tools_json = ? 
        WHERE id = ?
        """
        self.cursor.execute(sql, (description, expected_output, agent_id, tools_json, task_id))
        self.conn.commit()
        return self.cursor.rowcount

    def delete_task(self, task_id: int) -> int:
        sql = "DELETE FROM tasks WHERE id = ?"
        self.cursor.execute(sql, (task_id,))
        self.conn.commit()
        return self.cursor.rowcount

    # --- Workflows CRUD ---
    def create_workflow(self, name: str, task_ids: List[int], requires_human_check: bool) -> int:
        task_ids_json = json.dumps(task_ids)
        sql = "INSERT INTO workflows (name, task_ids_json, requires_human_check) VALUES (?, ?, ?)"
        self.cursor.execute(sql, (name, task_ids_json, requires_human_check))
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

    def update_workflow(self, workflow_id: int, name: str, task_ids: List[int], requires_human_check: bool) -> int:
        task_ids_json = json.dumps(task_ids)
        sql = """
        UPDATE workflows 
        SET name = ?, task_ids_json = ?, requires_human_check = ? 
        WHERE id = ?
        """
        self.cursor.execute(sql, (name, task_ids_json, requires_human_check, workflow_id))
        self.conn.commit()
        return self.cursor.rowcount

    def delete_workflow(self, workflow_id: int) -> int:
        sql = "DELETE FROM workflows WHERE id = ?"
        self.cursor.execute(sql, (workflow_id,))
        self.conn.commit()
        return self.cursor.rowcount

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