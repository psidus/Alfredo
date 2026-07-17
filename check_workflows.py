import os, sys
sys.path.append(os.getcwd())
from core.db_manager import DBManager

def check_workflows():
    db = DBManager()
    wfs = db.read_all_workflows()
    if not wfs:
        print("No workflows found.")
    for w in wfs:
        print(f"ID: {w['id']} - Name: {w['name']}")

if __name__ == "__main__":
    check_workflows()
