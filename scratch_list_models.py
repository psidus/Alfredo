from core.sqlite_manager import SQLiteManager

db = SQLiteManager()
models = db.read_all_models()
for m in models:
    print(f"ID={m['id']} provider={m['provider']} name={m['model_name']} local={m.get('is_local', False)}")
