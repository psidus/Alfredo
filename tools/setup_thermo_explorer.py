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
    
    # TASK 1: Context Analyzer (Paginator)
    task_1_id = db.create_task(
        name="Context Analyzer (Chunk Reader)",
        description="""Read the RAG database linearly using the 'read_rag_chunks' tool. 
You MUST use db_name: '{db_name}', offset: {offset}, and limit: {limit}.
Analyze the returned chunk text to determine if it contains a table of thermodynamic properties, an index mapping, or just spare text. 
Use the provided '{document_type}' to help identify the properties shown.
Also consider the '{context}' which may contain the ID/Name of a component that was cut in half in the previous chunk.
If it is a useful table/index, output a JSON string containing:
{"status": "TABLE", "context": "Detailed instructions on what properties map to what fields. If a component was cut in half, include its ID/Name from the input {context} here so the extractor knows what component the dangling numbers belong to.", "raw_chunk": "The exact raw text you read"}
If it is spare text without useful data, or if the tool returns END_OF_DOCUMENT, output strictly:
{"status": "SKIP"}""",
        expected_output="A strict JSON object with status, context, and raw_chunk.",
        agent_id=clausius_agent_id,
        tools=["read_rag_chunks"],
        required_inputs=[
            {"key": "db_name", "prompt": "Nome del RAG (es. Basic_properties, Perry_cap_2)"},
            {"key": "offset", "prompt": "Offset di partenza (default 0)"},
            {"key": "limit", "prompt": "Numero di chunk da esplorare (default 50)"},
            {"key": "document_type", "prompt": "Tipo di documento (es. Pure Components, BIPs, eNRTL)"},
            {"key": "context", "prompt": "Contesto (opzionale, default N/A)"}
        ],
        agent_specialization="Context analyzer and chunk reader.",
        human_validation=False
    )
    
    # TASK 2: Data Extractor
    task_2_id = db.create_task(
        name="Data Extractor",
        description="""Analyze the JSON output from the previous task: '{previous_result}'.
If the status is 'SKIP', you MUST output 'SKIP'.
Otherwise, use the 'raw_chunk' and the 'context' instructions provided in the JSON to extract the thermodynamic data row by row.
CRITICAL: Prioritize extracting the numerical ID into the 'id_no' field if present (e.g., '1', '2'). Leave 'component_name' empty if only an ID is given.
If the 'context' tells you that the first numbers belong to a component from the previous chunk, assign them to that component's ID/Name!
Format all the extracted data STRICTLY according to the ExtractionOutput Pydantic schema.""",
        expected_output="A valid JSON matching the ExtractionOutput schema, or 'SKIP'.",
        agent_id=dora_agent_id,
        tools=[],
        agent_specialization="Data extractor for thermodynamic properties.",
        output_pydantic="ExtractionOutput",
        human_validation=False
    )
    
    # TASK 3: Writer & Merge Checker
    task_3_id = db.create_task(
        name="Write to Master Excel DB",
        description="""If '{previous_result}' is 'SKIP', output 'Skipped'. 
Otherwise, use the 'merge_and_save_data' tool to append the validated ExtractionOutput JSON (which is '{previous_result}') into the master Excel database.
The merge_and_save_data tool will automatically check for identical IDs/names between lines and safely merge or split the rows.""",
        expected_output="Confirmation string from the merge_and_save_data tool.",
        agent_id=clausius_agent_id,
        tools=["merge_and_save_data"],
        agent_specialization="Excel writer and merge conflict handler.",
        human_validation=False
    )

    # TASK 4: Auto-Trigger Next Chunk
    task_4_id = db.create_task(
        name="Trigger Next Batch",
        description="""If the first task returned END_OF_DOCUMENT or we are done, output 'EXPLORATION FINISHED'.
Otherwise, use the 'trigger_next_batch' tool. Pass 'Thermo Explorer (Autonomous)' as workflow_name. 
Calculate new_offset as {offset} + {limit}.
Pass '{document_type}' for document_type.
For the 'context' variable, you MUST check the ephemeral memory (e.g. from Data Extractor) and extract the ID and Name of the LAST component that was processed. Pass this ID and Name as the context so the next batch knows what component was cut off!
Output the success message from the tool.""",
        expected_output="Trigger success message or EXPLORATION FINISHED.",
        agent_id=clausius_agent_id,
        tools=["trigger_next_batch"],
        agent_specialization="System automation orchestrator.",
        human_validation=False
    )

    # 3. Create the Workflow
    # Flat linear sequence of tasks (no batch loop)
    task_ids = [task_1_id, task_2_id, task_3_id, task_4_id]
    
    db.create_workflow(
        name="Thermo Explorer (Autonomous)",
        task_ids=task_ids,
        requires_human_check=False
    )
    print("✅ Successfully set up the Thermo Explorer (Autonomous) workflow!")
    print("It uses a Direct Linear Extraction architecture: Context Analyzer -> Data Extractor -> Writer -> Auto-Trigger.")

if __name__ == "__main__":
    setup_thermo_explorer()
