import sqlite3
import os

db_path = r"c:\Users\Pietro\Documents\GitHub\Alfredo\storage\vector_dbs\perry_8th\chroma.sqlite3"
if not os.path.exists(db_path):
    print("Database does not exist")
else:
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
    print(tables)
    
    if "embeddings" in tables:
        print("Embeddings:", conn.execute("SELECT count(*) FROM embeddings").fetchone())
    if "embedding" in tables:
        print("Embedding:", conn.execute("SELECT count(*) FROM embedding").fetchone())
    if "documents" in tables:
        print("Documents:", conn.execute("SELECT count(*) FROM documents").fetchone())
