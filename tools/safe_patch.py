import os

def safe_patch(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Update Schema in _create_tables
    if "output_pydantic TEXT" not in content:
        content = content.replace(
            "max_output_tokens INTEGER DEFAULT 0,",
            "max_output_tokens INTEGER DEFAULT 0,\n                output_pydantic TEXT,"
        )

    # 2. Add Migration to _create_tables safely
    migration_block = """
            # Migration to add output_pydantic to tasks
            try:
                self.cursor.execute("ALTER TABLE tasks ADD COLUMN output_pydantic TEXT;")
                self.conn.commit()
            except BaseException:
                pass
"""
    if "ALTER TABLE tasks ADD COLUMN output_pydantic" not in content:
        # We find the end of the _create_tables migrations which is before def create_app
        # Or before "        # ARCHITECT'S NOTE: Methods for interacting with Master AI App ecosystem"
        # We can just insert it right after the migration for max_output_tokens
        
        target = 'self.cursor.execute("ALTER TABLE tasks ADD COLUMN max_output_tokens INTEGER DEFAULT 0;")\n                self.conn.commit()\n            except '
        
        # Determine whether it's sqlite or psycopg2 error
        err_type = "sqlite3.OperationalError" if "sqlite_manager" in filepath else "psycopg2.Error"
        
        full_target = target + err_type + ':\n                pass'
        
        if full_target in content:
            content = content.replace(full_target, full_target + "\n" + migration_block)
        else:
            print(f"Warning: Could not find max_output_tokens migration in {filepath}")

    # 3. Update create_task definition
    content = content.replace(
        "human_validation: bool = False, max_input_context: int = 0, max_output_tokens: int = 0) -> int:",
        "human_validation: bool = False, max_input_context: int = 0, max_output_tokens: int = 0, output_pydantic: Optional[str] = None) -> int:"
    )
    
    # 3a. SQLite vs Postgres placeholders
    placeholder = "?" if "sqlite_manager" in filepath else "%s"
    
    # Update INSERT query
    content = content.replace(
        f"max_input_context, max_output_tokens) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
        f"max_input_context, max_output_tokens, output_pydantic) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})"
    )
    # Update INSERT values
    content = content.replace(
        "int(human_validation), max_input_context, max_output_tokens))",
        "int(human_validation), max_input_context, max_output_tokens, output_pydantic))"
    )

    # 4. Update update_task definition and logic
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
    print(f"Successfully patched {filepath}")

sqlite_path = "C:/Users/Pietro/Documents/GitHub/Alfredo/core/sqlite_manager.py"
postgres_path = "C:/Users/Pietro/Documents/GitHub/Alfredo/core/postgres_manager.py"
safe_patch(sqlite_path)
safe_patch(postgres_path)
