import sys
import os

# Add Alfredo root to path so we can import core.db_manager
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.db_manager import DBManager

def setup_thermo_workflow():
    db = DBManager()
    
    # 1. Create Agents
    parser_agent_id = db.create_agent(
        name="Thermodynamic Parser",
        role="Thermodynamic Data Parser",
        backstory="You are an expert chemical engineer with a PhD in thermodynamics. You can read complex tables and equations and extract coefficients flawlessly.",
        model_id=None,
        tools=[]
    )
    
    validator_agent_id = db.create_agent(
        name="Thermodynamic Validator",
        role="Thermodynamic Data Validator",
        backstory="You are a rigorous QA engineer for chemical process simulation. You ensure that Cp is positive, fractions sum to 1, and temperatures make sense. You alone have the authority to save data to the Excel DB.",
        model_id=None,
        tools=["merge_and_save_data"]
    )
    
    # 2. Create Tasks
    parsing_task_id = db.create_task(
        name="Extract Thermo Coefficients",
        description="Extract thermodynamic parameters from the provided text block. Ensure you capture all available T-dependent properties, standard BIPs (like k_ij, A_mm), and eNRTL specific properties (like tau_mca). Ensure extreme precision.",
        expected_output="JSON containing extracted Pure Components, BIPs, and eNRTL data strictly matching the ExtractionOutput Pydantic schema structure.",
        agent_id=parser_agent_id,
        tools=[],
        required_inputs=[{"name": "text_chunk", "description": "Markdown text block"}],
        vector_dbs=[],
        agent_specialization=None,
        human_validation=False
    )
    
    validation_task_id = db.create_task(
        name="Validate & Merge Thermo Data",
        description="Review the extracted JSON data. Ensure physical consistency (e.g. positive Cp, correct T_min < T_max). Once verified, you MUST use the 'merge_and_save_data' tool to pass the JSON string and save it to the Excel database.",
        expected_output="A confirmation string indicating whether the merge_and_save_data tool succeeded.",
        agent_id=validator_agent_id,
        tools=["merge_and_save_data"],
        required_inputs=[],
        vector_dbs=[],
        agent_specialization=None,
        human_validation=False
    )
    
    # 3. Assemble Workflow (Batch Loop)
    # The batch loop will wrap the two tasks. 
    # Let's create the workflow dictionary representing the batch loop structure.
    import json
    
    batch_loop_step = {
        "id": "batch_node_1",
        "type": "batch_loop",
        "task_ids": [parsing_task_id, validation_task_id],
        "batch_size": 1,
        "source_variable": "rag_results_or_markdown_chunks",
        "depends_on": [],
        "execution_level": 1
    }
    
    workflow_id = db.create_workflow(
        name="Thermo Data Miner Loop",
        task_ids=[batch_loop_step],
        requires_human_check=False,
        expected_exports=[],
        export_instructions="No export needed, the validator saves directly to Excel."
    )
    
    print(f"Successfully created Thermo Data Miner workflow (ID: {workflow_id})")
    print(f"Parser Agent ID: {parser_agent_id}, Validator Agent ID: {validator_agent_id}")
    print(f"Parsing Task ID: {parsing_task_id}, Validation Task ID: {validation_task_id}")

if __name__ == "__main__":
    setup_thermo_workflow()
