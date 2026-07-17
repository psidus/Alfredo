import sys
import os
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.db_manager import DBManager

def setup_thermo_explorer():
    db = DBManager()
    
    # 0. Clean up previous versions of THIS specific workflow to avoid duplicates
    try:
        db.cursor.execute("DELETE FROM workflows WHERE name = 'Thermo Explorer (Autonomous)'")
        db.conn.commit()
    except Exception as e:
        print("Cleanup warning:", e)
        db.conn.rollback()

    # 1. Fetch Agents (Clausius and Dora)
    # We assume they exist from the first thermo setup script. If not, create them.
    db.cursor.execute("SELECT id FROM agents WHERE name = 'Clausius Clapairon'")
    clausius_res = db.cursor.fetchone()
    
    db.cursor.execute("SELECT id FROM agents WHERE name = 'Dora'")
    dora_res = db.cursor.fetchone()
    
    if not clausius_res or not dora_res:
        print("Error: Clausius Clapairon or Dora agents not found. Please run setup_thermo_workflow.py first.")
        return
        
    clausius_agent_id = clausius_res['id']
    dora_agent_id = dora_res['id']

    # 2. Create Tasks
    
    # LEVEL 1: Paginator
    task_1_id = db.create_task(
        name="Generate Target List (Chunked)",
        description="""Start by reading the RAG database linearly using 'read_rag_chunks'.
You MUST use the db_name: '{db_name}', offset: {offset}, and limit: {limit}.
After reading the chunks, extract a comprehensive list of all chemical components mentioned.
Output a strictly formatted JSON containing: {"chemicals": ["Substance 1", "Substance 2", ...]}.
If the tool returns 'END_OF_DOCUMENT', output {"chemicals": [], "end_reached": true}.""",
        expected_output="A strict JSON object with the key 'chemicals'.",
        agent_id=clausius_agent_id,
        tools=["read_rag_chunks"],
        required_inputs=[
            {"key": "db_name", "prompt": "Nome del RAG (es. Electrolite, Perry, CR_chemical)"},
            {"key": "offset", "prompt": "Offset di partenza (default 0)"},
            {"key": "limit", "prompt": "Numero di chunk da esplorare (default 50)"}
        ],
        agent_specialization="You are a librarian. You must read linearly and extract all substances mentioned.",
        human_validation=False
    )
    
    # LEVEL 2: Batch Loop Tasks
    task_2a_id = db.create_task(
        name="Filter Target in Excel",
        description="Check if '{previous_result}' is already mapped in the master Excel database using 'check_excel_db'. If it returns SKIP, output 'SKIP'. If PROCEED, output 'PROCEED'.",
        expected_output="Strictly 'SKIP' or 'PROCEED'.",
        agent_id=clausius_agent_id,
        tools=["check_excel_db"],
        agent_specialization="Database fast checker.",
        human_validation=False
    )
    
    task_2b_id = db.create_task(
        name="[ASYNC] Extract Properties Semantically",
        description="If the previous result is 'SKIP', output 'SKIP'. Otherwise, deeply semantic-search your databases for T-dependent properties, BIPs, and eNRTL for the substance '{previous_result}'. Extract coefficients, references, and ranges.",
        expected_output="Raw text/markdown with extracted values and references, or 'SKIP'.",
        agent_id=dora_agent_id,
        tools=["vector_search", "tabular_query"],
        agent_specialization="Semantic researcher for thermodynamic properties.",
        human_validation=False
    )
    
    task_2c_id = db.create_task(
        name="Reconcile & Validate Data",
        description="If the previous result is 'SKIP', output 'SKIP'. Otherwise, review the raw markdown extracted for '{previous_result}', format it strictly according to the ExtractionOutput Pydantic schema.",
        expected_output="A valid JSON matching the ExtractionOutput schema, or 'SKIP'.",
        agent_id=clausius_agent_id,
        tools=[],
        agent_specialization="Strict Data Validator.",
        output_pydantic="ExtractionOutput",
        human_validation=False
    )
    
    task_2d_id = db.create_task(
        name="Write to Master Excel DB",
        description="If '{previous_result}' is 'SKIP', output 'Skipped'. Otherwise, use 'merge_and_save_data' tool to append the validated ExtractionOutput JSON for '{previous_result}' into the master Excel database.",
        expected_output="Confirmation string.",
        agent_id=clausius_agent_id,
        tools=["merge_and_save_data"],
        agent_specialization="Excel writer.",
        human_validation=False
    )

    # LEVEL 3: Auto-Trigger
    task_3_id = db.create_task(
        name="Trigger Next Batch",
        description="""Review the result of the first task.
If 'end_reached' is true or the document is finished, output 'EXPLORATION FINISHED'.
Otherwise, use the 'trigger_next_batch' tool. Pass 'Thermo Explorer (Autonomous)' as workflow_name, and calculate new_offset as '{offset}' + '{limit}'.
Output the success message from the tool.""",
        expected_output="Trigger success message or EXPLORATION FINISHED.",
        agent_id=clausius_agent_id,
        tools=["trigger_next_batch"],
        agent_specialization="System automation orchestrator.",
        human_validation=False
    )

    # 3. Create the Workflow
    batch_node = {
        "type": "batch_loop",
        "batch_size": 2, # Process 2 chemicals at a time
        "source_variable": "chemicals",
        "task_ids": [task_2a_id, task_2b_id, task_2c_id, task_2d_id]
    }
    
    task_ids_json = json.dumps([task_1_id, batch_node, task_3_id])

    db.create_workflow(
        name="Thermo Explorer (Autonomous)",
        task_ids=[task_1_id, batch_node, task_3_id],
        requires_human_check=False
    )
    print("✅ Successfully set up the Thermo Explorer (Autonomous) workflow!")
    print("It uses a 3-level architecture: Linear Paginator -> Map/Reduce Extractor -> Auto-Trigger.")

if __name__ == "__main__":
    setup_thermo_explorer()
