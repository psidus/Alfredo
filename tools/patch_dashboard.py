import os

dashboard_path = "C:/Users/Pietro/Documents/GitHub/Alfredo/ui/dashboard.py"
with open(dashboard_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Imports
if "from core.schema_loader import" not in content:
    content = content.replace(
        "from core.api_verifier import get_ollama_model_info",
        "from core.api_verifier import get_ollama_model_info\nfrom core.schema_loader import get_available_schemas, get_schema_class"
    )
    # in case it missed:
    if "from core.schema_loader import" not in content:
        content = content.replace("from core.db_manager import get_db_manager", "from core.db_manager import get_db_manager\nfrom core.schema_loader import get_available_schemas, get_schema_class")

# 2. Add Pydantic Dropdown in render_task_builder
# Look for:
#        expected_output = st.text_area("Expected Output", height=150, key="task_output_area",
#                                       help="You can also use `{variable_name}` placeholders here.")

pydantic_ui_code = """
        # --- Pydantic Schema Injection ---
        available_schemas = list(get_available_schemas().keys())
        schema_options = ["None"] + available_schemas
        
        default_pydantic = editing_task.get('output_pydantic') if editing_task else None
        default_pydantic_idx = schema_options.index(default_pydantic) if default_pydantic in schema_options else 0

        def on_schema_select():
            sel = st.session_state.task_pydantic_sel
            if sel and sel != "None":
                cls = get_schema_class(sel)
                if cls:
                    import json
                    schema_json = json.dumps(cls.model_json_schema(), indent=2)
                    current_out = st.session_state.get("task_output_area", "")
                    append_str = f"\\n\\nMust conform to this JSON schema:\\n```json\\n{schema_json}\\n```"
                    if append_str not in current_out:
                        st.session_state.task_output_area = f"{current_out}{append_str}".strip()

        selected_pydantic = st.selectbox(
            "Enforce Pydantic Schema (Optional)",
            options=schema_options,
            index=default_pydantic_idx,
            key="task_pydantic_sel",
            on_change=on_schema_select,
            help="Select a Pydantic schema to strictly validate the JSON output of this task. Alfredo will automatically inject the schema definition into the Expected Output box."
        )
"""

if "Enforce Pydantic Schema (Optional)" not in content:
    target = 'expected_output = st.text_area("Expected Output", height=150, key="task_output_area",\n                                       help="You can also use `{variable_name}` placeholders here.")'
    content = content.replace(target, target + "\n" + pydantic_ui_code)

# 3. Update create_task / update_task calls
if "selected_pydantic if selected_pydantic != 'None' else None" not in content:
    # Need to pass output_pydantic to db.update_task and db.create_task
    content = content.replace(
        "st.session_state.get('task_out_tok', 0))",
        "st.session_state.get('task_out_tok', 0), selected_pydantic if selected_pydantic != 'None' else None)"
    )

with open(dashboard_path, "w", encoding="utf-8") as f:
    f.write(content)

print("dashboard patched")
