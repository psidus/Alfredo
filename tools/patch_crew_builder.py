import os

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
crew_builder_path = os.path.join(base_dir, "core", "crew_builder.py")
with open(crew_builder_path, "r", encoding="utf-8") as f:
    content = f.read()

if "from core.schema_loader import get_schema_class" not in content:
    content = content.replace("from core.thermo_schemas import ExtractionOutput", "from core.schema_loader import get_schema_class")

old_logic_1 = """        kwargs = {}
        if task_data.get('name', '').startswith("Extract Thermo Coefficients"):
            kwargs['output_pydantic'] = ExtractionOutput"""
new_logic_1 = """        kwargs = {}
        if task_data.get('output_pydantic'):
            cls = get_schema_class(task_data.get('output_pydantic'))
            if cls:
                kwargs['output_pydantic'] = cls"""

content = content.replace(old_logic_1, new_logic_1)

old_logic_2 = """                    if isinstance(schemas, list) and len(schemas) > 0:
                        kwargs['output_pydantic'] = schemas[0]"""
new_logic_2 = """                    if isinstance(schemas, list) and len(schemas) > 0:
                        cls = get_schema_class(schemas[0])
                        if cls:
                            kwargs['output_pydantic'] = cls"""

content = content.replace(old_logic_2, new_logic_2)

with open(crew_builder_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Patch applied successfully to crew_builder.py")
