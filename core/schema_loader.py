import os
import sys
import importlib
import inspect
from pydantic import BaseModel

SCHEMAS_DIR = os.path.join(os.path.dirname(__file__), 'schemas')

def _load_all_schema_modules():
    """Dynamically loads all modules in the schemas directory."""
    modules = []
    if not os.path.exists(SCHEMAS_DIR):
        os.makedirs(SCHEMAS_DIR)
        
    for filename in os.listdir(SCHEMAS_DIR):
        if filename.endswith('.py') and not filename.startswith('__'):
            module_name = f"core.schemas.{filename[:-3]}"
            try:
                # Force reload to pick up changes without restarting the app
                if module_name in sys.modules:
                    module = importlib.reload(sys.modules[module_name])
                else:
                    module = importlib.import_module(module_name)
                modules.append(module)
            except Exception as e:
                print(f"Error loading schema module {module_name}: {e}")
    return modules

def get_available_schemas():
    """
    Returns a dictionary of all available Pydantic schema classes found in core/schemas/.
    Format: {"ClassName": ClassObject}
    """
    schemas = {}
    modules = _load_all_schema_modules()
    for module in modules:
        for name, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
                # Ensure it's defined in this module, not imported from pydantic itself
                if obj.__module__ == module.__name__:
                    schemas[name] = obj
    return schemas

def get_schema_class(schema_name: str):
    """
    Returns the Pydantic class by name, or None if not found.
    """
    if not schema_name:
        return None
    schemas = get_available_schemas()
    return schemas.get(schema_name)
