import sys
import os
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.postgres_manager import PostgresManager

def setup_thermo_workflow():
    db = PostgresManager()
    
    # 0. Clean up excess agents and old tasks
    try:
        db.cursor.execute("DELETE FROM workflows WHERE name LIKE '%Thermo%'")
        db.cursor.execute("DELETE FROM tasks WHERE name LIKE '%[ASYNC]%' OR name LIKE '%Thermo%' OR name IN ('Generate Target List', 'Reconcile & Validate Data', 'Write to Master Excel DB')")
        # Delete the old 5 agents we created
        db.cursor.execute("DELETE FROM agents WHERE name IN ('Lead Thermodynamic Analyst', 'Electrolyte Specialist', 'Perry Handbook Expert', 'CR Design Expert', 'Data Validator', 'Database Manager')")
        
        # Also clean up Dora and Clausius Clapairon if they exist to avoid unique constraint issues, or update them
        db.cursor.execute("DELETE FROM agents WHERE name IN ('Dora', 'Clausius Clapairon')")
        db.conn.commit()
    except Exception as e:
        print("Cleanup warning:", e)
        db.conn.rollback()

    # 1. Create the 2 Core Agents
    clausius_agent_id = db.create_agent(
        name="Clausius Clapairon",
        role="Master Thermodynamic Engineer",
        backstory="You are an elite thermodynamics expert. You analyze user requests to identify chemical components, reconcile raw data extracted by researchers, ensure physical consistency, rigorously enforce JSON schemas, and manage database writes.",
        model_id=None,
        tools=["merge_and_save_data"]
    )
    
    dora_agent_id = db.create_agent(
        name="Dora",
        role="RAG Data Explorer",
        backstory="You are an expert data retriever and reader. You navigate complex engineering handbooks and databases to extract precise numerical coefficients, references, and tables. You follow your task specialization strictly.",
        model_id=None,
        tools=["vector_search", "tabular_query"]
    )
    
    # Get Vector DB IDs
    def get_vdb_id(name_like):
        db.cursor.execute("SELECT id FROM vector_databases WHERE name LIKE %s", (f"%{name_like}%",))
        vdb = db.cursor.fetchone()
        return str(vdb['id']) if vdb else None

    vdb_electrolyte = get_vdb_id("Electrolite")
    vdb_perry = get_vdb_id("Perry")
    vdb_cr = get_vdb_id("CR_chemical")
    
    missing_vdbs = []
    if not vdb_electrolyte: missing_vdbs.append("Electrolite")
    if not vdb_perry: missing_vdbs.append("Perry")
    if not vdb_cr: missing_vdbs.append("CR_chemical")
    
    if missing_vdbs:
        raise ValueError(f"CRITICAL ERROR: Required Vector DBs not found in the database: {', '.join(missing_vdbs)}. Please upload them via UI first.")
    
    # 2. Create Tasks and assign Specializations
    task_1_id = db.create_task(
        name="Generate Target List",
        description="Analyze the user prompt. First, identify the required thermodynamic property (e.g., 'eNRTL', 'BIPs', 'Pure Component'). Second, extract the list of pure components or pairs. Output a strictly formatted JSON containing: {\"property_type\": \"...\", \"chemicals\": [\"CH4\", \"Water\"]}.",
        expected_output="A strict JSON object with keys 'property_type' and 'chemicals'.",
        agent_id=clausius_agent_id,
        tools=[],
        required_inputs=[{"key": "prompt", "prompt": "User Prompt describing what to search"}],
        agent_specialization="Focus ONLY on parsing the user intent and outputting the strictly valid JSON list. Do not attempt to search or extract data.",
        human_validation=False
    )
    
    task_2a_id = db.create_task(
        name="[ASYNC] Extract Electrolyte Data",
        description="The user needs the property '{property_type}' for '{previous_result}'. Search your assigned database deeply if it's eNRTL/Electrolyte related. Otherwise do a quick check. Extract coefficients, references, and T ranges. Return 'No relevant data' if empty.",
        expected_output="Raw text/markdown with extracted values and references.",
        agent_id=dora_agent_id,
        tools=["vector_search", "tabular_query"],
        vector_dbs=[vdb_electrolyte] if vdb_electrolyte else [],
        agent_specialization="You are assigned to the Aqueous Electrolyte DB. Focus heavily on eNRTL coefficients and ionic interaction parameters.",
        human_validation=False
    )

    task_2b_id = db.create_task(
        name="[ASYNC] Extract Perry Data",
        description="The user needs the property '{property_type}' for '{previous_result}'. Search your assigned database. Extract coefficients, references, and T ranges. Return 'No relevant data' if empty.",
        expected_output="Raw text/markdown with extracted values and references.",
        agent_id=dora_agent_id,
        tools=["vector_search", "tabular_query"],
        vector_dbs=[vdb_perry] if vdb_perry else [],
        agent_specialization="You are assigned to Perry's Handbook. Focus on extracting accurate pure component T-dependent properties (Cp, Vapor Pressure, etc).",
        human_validation=False
    )

    task_2c_id = db.create_task(
        name="[ASYNC] Extract CR Data",
        description="The user needs the property '{property_type}' for '{previous_result}'. Search your assigned database. Extract coefficients, references, and T ranges. Return 'No relevant data' if empty.",
        expected_output="Raw text/markdown with extracted values and references.",
        agent_id=dora_agent_id,
        tools=["vector_search", "tabular_query"],
        vector_dbs=[vdb_cr] if vdb_cr else [],
        agent_specialization="You are assigned to Coulson & Richardson's Design Manual. Focus on cross-referencing pure physical properties and coefficients.",
        human_validation=False
    )
    
    task_3_id = db.create_task(
        name="Reconcile & Validate Data",
        description="Review the extracted data from Dora's parallel searches for '{previous_result}'. Reconcile synonyms, merge data logically, eliminate duplicates, and format strictly according to the ExtractionOutput Pydantic schema.",
        expected_output="A strictly valid JSON matching the ExtractionOutput schema.",
        agent_id=clausius_agent_id,
        tools=[],
        agent_specialization="Act as a strict Data Validator. You receive messy markdown from Dora. Your sole purpose is to clean, reconcile, and format it into the Pydantic JSON.",
        human_validation=False,
        output_pydantic="ExtractionOutput"
    )

    task_4_id = db.create_task(
        name="Write to Master Excel DB",
        description="Receive the validated ExtractionOutput JSON for '{previous_result}'. Use your 'merge_and_save_data' tool to append this data into the master Excel database.",
        expected_output="A confirmation string indicating the write success.",
        agent_id=clausius_agent_id,
        tools=["merge_and_save_data"],
        agent_specialization="Act as a strict Database Manager. You receive a validated JSON. Your sole purpose is to trigger the write tool.",
        human_validation=False
    )
    
    # 3. Assemble Workflow
    level_1_step = {
        "id": "node_1",
        "type": "sequential",
        "task_ids": [task_1_id],
        "depends_on": [],
        "execution_level": 1
    }

    batch_loop_step = {
        "id": "batch_node_1",
        "type": "batch_loop",
        "task_ids": [task_2a_id, task_2b_id, task_2c_id, task_3_id, task_4_id],
        "batch_size": 1,
        "source_variable": "chemicals", 
        "depends_on": ["node_1"],
        "execution_level": 2
    }
    
    wf_name = "Thermo Data Miner V3 (1 Agent, 3 Specs)"
    db.cursor.execute("SELECT id FROM workflows WHERE name = %s", (wf_name,))
    existing = db.cursor.fetchone()
    if existing:
        db.update_workflow(existing['id'], wf_name, [level_1_step, batch_loop_step], False, [], "No export needed. Written to Excel directly.")
        workflow_id = existing['id']
    else:
        workflow_id = db.create_workflow(
            name=wf_name,
            task_ids=[level_1_step, batch_loop_step],
            requires_human_check=False,
            expected_exports=[],
            export_instructions="No export needed. Written to Excel directly."
        )
    
    print(f"Successfully updated {wf_name} (ID: {workflow_id})")
    print(f"Assigned Agents: Dora (ID: {dora_agent_id}), Clausius Clapairon (ID: {clausius_agent_id})")

if __name__ == "__main__":
    setup_thermo_workflow()
