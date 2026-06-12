import re
import json
import sqlite3
from typing import Type

from pydantic import BaseModel, Field
from crewai.tools import BaseTool


class AppDatabaseQueryInput(BaseModel):
    """Input schema for AppDatabaseQueryTool."""
    action: str = Field(
        ...,
        description=(
            "The action to perform. Must be one of: "
            "'list_tables' (list all tables in the database), "
            "'describe_table' (show columns, types, nullable for a table), "
            "'query' (execute a SELECT-only SQL query), "
            "'insert' (insert a row from JSON data), "
            "'update' (update rows from JSON data)."
        )
    )
    sql: str = Field(
        default="",
        description="SQL query string. Required when action='query'. Must be a SELECT statement."
    )
    table: str = Field(
        default="",
        description="Table name. Required for 'describe_table', 'insert', and 'update' actions."
    )
    data: str = Field(
        default="",
        description=(
            "JSON string with data for 'insert' or 'update' actions. "
            "For insert: {\"col1\": \"val1\", \"col2\": \"val2\"}. "
            "For update: {\"set\": {\"col\": \"val\"}, \"where\": {\"id\": 1}}."
        )
    )


# Regex pattern to detect destructive SQL keywords in query action
_DESTRUCTIVE_SQL_PATTERN = re.compile(
    r'\b(DROP|DELETE|ALTER|TRUNCATE|INSERT|UPDATE|CREATE|REPLACE|GRANT|REVOKE)\b',
    re.IGNORECASE
)


class AppDatabaseQueryTool(BaseTool):
    """
    A CrewAI tool that allows agents to query the database of a connected
    external application. Supports SQLite and PostgreSQL backends.

    Use this tool when you need to:
    - List tables in an app's database
    - Inspect table schemas (columns, types)
    - Run read-only SELECT queries
    - Insert or update records via structured JSON data
    """

    name: str = "app_database_query"
    description: str = (
        "Query the database of a connected external app. "
        "Use action='list_tables' to discover tables, 'describe_table' for schema, "
        "'query' for SELECT queries, or 'insert'/'update' to modify data."
    )
    args_schema: Type[BaseModel] = AppDatabaseQueryInput

    # Custom fields needed for execution
    connection_string: str = Field(..., description="Database connection string.")
    db_type: str = Field(default="sqlite", description="Database type: 'sqlite' or 'postgresql'.")
    app_name: str = Field(default="", description="Name of the connected app.")

    def _get_connection(self):
        """Creates and returns a database connection based on db_type."""
        if self.db_type == "postgresql":
            try:
                import psycopg2
                import psycopg2.extras
            except ImportError:
                raise ImportError(
                    "psycopg2 is not installed. Install it with: pip install psycopg2-binary"
                )
            conn = psycopg2.connect(self.connection_string)
            return conn
        else:
            conn = sqlite3.connect(self.connection_string)
            conn.row_factory = sqlite3.Row
            return conn

    def _list_tables(self, conn) -> str:
        """Lists all table names in the database."""
        cursor = conn.cursor()
        if self.db_type == "postgresql":
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name;"
            )
            tables = [row[0] for row in cursor.fetchall()]
        else:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
            )
            tables = [row[0] for row in cursor.fetchall()]

        if not tables:
            return f"No tables found in the '{self.app_name}' database."
        return f"Tables in '{self.app_name}' database:\n" + "\n".join(f"  - {t}" for t in tables)

    def _describe_table(self, conn, table: str) -> str:
        """Shows column names, types, and nullable info for a table."""
        cursor = conn.cursor()
        if self.db_type == "postgresql":
            cursor.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position;",
                (table,)
            )
            columns = cursor.fetchall()
            if not columns:
                return f"Table '{table}' not found or has no columns."
            lines = [f"Schema of '{table}':"]
            for col_name, data_type, nullable in columns:
                lines.append(f"  - {col_name} ({data_type}, nullable={nullable})")
            return "\n".join(lines)
        else:
            cursor.execute(f"PRAGMA table_info('{table}');")
            columns = cursor.fetchall()
            if not columns:
                return f"Table '{table}' not found or has no columns."
            lines = [f"Schema of '{table}':"]
            for col in columns:
                col_name = col[1] if isinstance(col, tuple) else col['name']
                col_type = col[2] if isinstance(col, tuple) else col['type']
                not_null = col[3] if isinstance(col, tuple) else col['notnull']
                nullable = "NO" if not_null else "YES"
                lines.append(f"  - {col_name} ({col_type}, nullable={nullable})")
            return "\n".join(lines)

    def _execute_query(self, conn, sql: str) -> str:
        """Executes a read-only SELECT query and returns formatted results."""
        # Security: block destructive SQL statements
        if _DESTRUCTIVE_SQL_PATTERN.search(sql):
            return (
                "Error: Only SELECT queries are allowed in 'query' mode. "
                "Destructive statements (DROP, DELETE, ALTER, TRUNCATE, INSERT, UPDATE) are blocked."
            )

        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()

        if not rows:
            return "Query returned 0 rows."

        # Extract column names from cursor description
        col_names = [desc[0] for desc in cursor.description]

        # Format as a text table
        lines = [" | ".join(col_names)]
        lines.append("-" * len(lines[0]))
        for row in rows:
            lines.append(" | ".join(str(val) for val in row))

        return f"Query returned {len(rows)} row(s):\n" + "\n".join(lines)

    def _execute_insert(self, conn, table: str, data_str: str) -> str:
        """Parses JSON data and inserts a row into the specified table."""
        data = json.loads(data_str)
        columns = list(data.keys())
        values = list(data.values())

        if self.db_type == "postgresql":
            placeholders = ", ".join(["%s"] * len(values))
        else:
            placeholders = ", ".join(["?"] * len(values))

        col_str = ", ".join(columns)
        sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"

        cursor = conn.cursor()
        cursor.execute(sql, values)
        conn.commit()
        return f"Successfully inserted 1 row into '{table}'."

    def _execute_update(self, conn, table: str, data_str: str) -> str:
        """Parses JSON data and updates rows in the specified table."""
        data = json.loads(data_str)
        set_data = data.get("set", {})
        where_data = data.get("where", {})

        if not set_data:
            return "Error: 'data' JSON must contain a 'set' key with columns to update."
        if not where_data:
            return "Error: 'data' JSON must contain a 'where' key with filter conditions."

        if self.db_type == "postgresql":
            placeholder = "%s"
        else:
            placeholder = "?"

        set_parts = [f"{col} = {placeholder}" for col in set_data.keys()]
        where_parts = [f"{col} = {placeholder}" for col in where_data.keys()]
        values = list(set_data.values()) + list(where_data.values())

        sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}"

        cursor = conn.cursor()
        cursor.execute(sql, values)
        conn.commit()
        affected = cursor.rowcount
        return f"Successfully updated {affected} row(s) in '{table}'."

    def _run(self, action: str, sql: str = "", table: str = "", data: str = "") -> str:
        try:
            conn = self._get_connection()
        except ImportError as e:
            return str(e)
        except Exception as e:
            return f"Error connecting to database: {e}"

        try:
            action = action.lower().strip()

            if action == "list_tables":
                return self._list_tables(conn)

            elif action == "describe_table":
                if not table:
                    return "Error: 'table' parameter is required for 'describe_table' action."
                return self._describe_table(conn, table)

            elif action == "query":
                if not sql:
                    return "Error: 'sql' parameter is required for 'query' action."
                return self._execute_query(conn, sql)

            elif action == "insert":
                if not table:
                    return "Error: 'table' parameter is required for 'insert' action."
                if not data:
                    return "Error: 'data' parameter is required for 'insert' action."
                return self._execute_insert(conn, table, data)

            elif action == "update":
                if not table:
                    return "Error: 'table' parameter is required for 'update' action."
                if not data:
                    return "Error: 'data' parameter is required for 'update' action."
                return self._execute_update(conn, table, data)

            else:
                return (
                    f"Unknown action '{action}'. "
                    "Valid actions: 'list_tables', 'describe_table', 'query', 'insert', 'update'."
                )

        except Exception as e:
            return f"Error executing '{action}' on '{self.app_name}' database: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass
