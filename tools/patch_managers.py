import os

def update_manager(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Update Schema
    if "output_pydantic TEXT" not in content:
        content = content.replace(
            "max_output_tokens INTEGER DEFAULT 0,",
            "max_output_tokens INTEGER DEFAULT 0,\n                output_pydantic TEXT,"
        )

    # 2. Add Migration
    migration_block = """
            # Migration to add output_pydantic to tasks
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN output_pydantic TEXT;")
                self.conn.commit()
            except BaseException:
                pass
"""
    if "ALTER TABLE tasks ADD COLUMN output_pydantic" not in content:
        # insert before the last except block in _create_tables
        parts = content.split("except sqlite3.Error as e:" if "sqlite3.Error" in content else "except psycopg2.Error as e:")
        content = parts[0] + migration_block + ("except sqlite3.Error as e:" if "sqlite3.Error" in content else "except psycopg2.Error as e:") + parts[1]

    # 3. Update create_task
    content = content.replace(
        "human_validation: bool = False, max_input_context: int = 0, max_output_tokens: int = 0) -> int:",
        "human_validation: bool = False, max_input_context: int = 0, max_output_tokens: int = 0, output_pydantic: Optional[str] = None) -> int:"
    )
    
    # 3a. SQLite vs Postgres placeholders
    placeholder = "?" if "sqlite_manager" in filepath else "%s"
    
    content = content.replace(
        f"max_input_context, max_output_tokens) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
        f"max_input_context, max_output_tokens, output_pydantic) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})"
    )
    content = content.replace(
        "int(human_validation), max_input_context, max_output_tokens))",
        "int(human_validation), max_input_context, max_output_tokens, output_pydantic))"
    )

    # 4. Update update_task
    content = content.replace(
        "max_output_tokens = " + placeholder,
        "max_output_tokens = " + placeholder + f", output_pydantic = {placeholder}"
    )
    content = content.replace(
        "int(human_validation), max_input_context, max_output_tokens, task_id))",
        "int(human_validation), max_input_context, max_output_tokens, output_pydantic, task_id))"
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

sqlite_path = "C:/Users/Pietro/Documents/GitHub/Alfredo/core/sqlite_manager.py"
postgres_path = "C:/Users/Pietro/Documents/GitHub/Alfredo/core/postgres_manager.py"
update_manager(sqlite_path)
update_manager(postgres_path)
