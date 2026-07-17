import os
import json
from core.db_manager import DBManager

def recover():
    db = DBManager()
    base_dir = os.path.join("storage", "vector_dbs")
    
    if not os.path.exists(base_dir):
        print(f"Directory {base_dir} does not exist.")
        return

    # First, let's read existing ones to avoid duplicates
    existing = {db_obj['name'] for db_obj in db.read_all_vector_dbs()}
    
    count = 0
    for folder in os.listdir(base_dir):
        path = os.path.join(base_dir, folder)
        if os.path.isdir(path):
            if folder in existing:
                print(f"Skipping '{folder}', already in DB.")
                continue
                
            config_path = os.path.join(path, "config.json")
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    
                    provider = config.get("provider", "unknown")
                    model_name = config.get("model_name", "unknown")
                    
                    db.create_vector_db(name=folder, path=path, provider=provider, model_name=model_name)
                    print(f"Recovered '{folder}' into database.")
                    count += 1
                except Exception as e:
                    print(f"Failed to recover '{folder}': {e}")
            else:
                # If no config, try generic fallback
                db.create_vector_db(name=folder, path=path, provider="unknown", model_name="unknown")
                print(f"Recovered '{folder}' (no config.json) into database.")
                count += 1

    print(f"Successfully recovered {count} vector databases.")

if __name__ == "__main__":
    recover()
