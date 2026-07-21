import sys, psycopg2, json, time
sys.path.append('.')
from core.db_manager import DBManager
db = DBManager()
cur = db.cursor

run_inputs = {'db_name': 'Basic_properties', 'limit': 1, 'offset': 0, 'context': 'N/A', 'document_type': 'Pure Components'}
run_id = db.create_run(
    workflow_id=10,
    status='running',
    inputs=run_inputs,
    source='test-trigger'
)
print(f'Started sync run {run_id}...')

from core.crew_builder import execute_run_with_resume

# Run offset 0
print("Running offset 0...")
execute_run_with_resume(run_id)
db.update_run(run_id, status='completed')

# Find next run (offset 1)
cur.execute('SELECT id FROM workflow_runs ORDER BY id DESC LIMIT 1')
next_id = cur.fetchone()[0]
print(f"Running offset 1 (Run {next_id})...")
execute_run_with_resume(next_id)

# Find next run (offset 2 - ARGON)
cur.execute('SELECT id FROM workflow_runs ORDER BY id DESC LIMIT 1')
next_next_id = cur.fetchone()[0]
print(f"Running offset 2 (Run {next_next_id})...")
execute_run_with_resume(next_next_id)

print("Done processing chunks 0, 1, and 2.")
