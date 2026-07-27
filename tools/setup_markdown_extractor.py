import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.db_manager import DBManager

def setup_markdown_extractor():
    db = DBManager()
    
    # 0. Clean up previous versions
    try:
        db.cursor.execute("DELETE FROM workflows WHERE name = 'Markdown Table Extractor (Loop)'")
        db.conn.commit()
    except Exception as e:
        print("Cleanup warning:", e)
        db.conn.rollback()

    # 1. Fetch Agents (Clausius and Dora)
    db.cursor.execute("SELECT id FROM agents WHERE name = 'Clausius Clapairon'")
    clausius_res = db.cursor.fetchone()
    if not clausius_res:
        clausius_agent_id = db.create_agent(
            name="Clausius Clapairon",
            role="Master Thermodynamic Engineer",
            backstory="You are an elite thermodynamics expert. You manage database writes and orchestration.",
            model_id=None,
            tools=["save_markdown_data", "trigger_next_batch"]
        )
    else:
        clausius_agent_id = clausius_res['id']
        
    db.cursor.execute("SELECT id FROM agents WHERE name = 'Dora'")
    dora_res = db.cursor.fetchone()
    if not dora_res:
        dora_agent_id = db.create_agent(
            name="Dora",
            role="RAG Data Explorer",
            backstory="You are an expert data retriever and reader.",
            model_id=None,
            tools=[]
        )
    else:
        dora_agent_id = dora_res['id']

    # 2. Create Tasks
    
    # TASK 1: Context Analyzer (Paginator)
    task_1_id = db.create_task(
        name="Read Markdown Chunk",
        description="""Read the vector database linearly using the 'read_rag_chunks' tool. 
You MUST use db_name: '{db_name}', offset: {offset}, and limit: {limit}.
Analyze the returned chunk text to determine if it contains parts of the split Markdown table. 
If it is a useful table/index, output a JSON string containing:
{"status": "TABLE", "raw_chunk": "The exact raw text you read"}
If it is spare text without useful data, output strictly:
{"status": "SKIP"}
If the tool returns END_OF_DOCUMENT (meaning there is no more data to read), output strictly:
{"status": "FINISHED"}""",
        expected_output="A strict JSON object with status (TABLE, SKIP, or FINISHED).",
        agent_id=clausius_agent_id,
        tools=["read_rag_chunks"],
        required_inputs=[
            {"key": "db_name", "prompt": "Nome del Vector DB (es. MarkdownThermal)"},
            {"key": "offset", "prompt": "Offset di partenza (default 0)"},
            {"key": "limit", "prompt": "Numero di chunk da esplorare (default 5)"}
        ],
        agent_specialization="Context analyzer and chunk reader. You ONLY output JSON.",
        human_validation=False
    )
    
    # TASK 2: Data Extractor
    task_2_id = db.create_task(
        name="Extract Markdown Data",
        description="""Analyze the JSON output from the previous task: '{previous_result}'.
If the status in the JSON is 'SKIP' or 'FINISHED', you MUST output a valid JSON matching MarkdownThermalExtraction with an empty 'rows' list.
Otherwise, use the 'raw_chunk' to extract the thermodynamic data. Correlate the rows that have the same 'NO' (ID) so that all properties for a single ID are together.
Format all the extracted data STRICTLY according to the MarkdownThermalExtraction Pydantic schema.""",
        expected_output="A valid JSON matching the MarkdownThermalExtraction schema.",
        agent_id=dora_agent_id,
        tools=[],
        agent_specialization="Data extractor for markdown properties.",
        output_pydantic="MarkdownThermalExtraction",
        human_validation=False
    )
    
    # TASK 3: Writer
    task_3_id = db.create_task(
        name="Save to Excel DB",
        description="""The '{previous_result}' is a JSON string containing extracted data.
If the JSON has no rows, YOU MUST STILL EXECUTE THE 'save_markdown_data' TOOL! 
Pass the exact string '{previous_result}' as the 'validated_data_json' argument to the tool.
Output the exact confirmation string returned by the tool.""",
        expected_output="Confirmation string from the save_markdown_data tool.",
        agent_id=clausius_agent_id,
        tools=["save_markdown_data"],
        agent_specialization="Excel writer. You MUST use the save_markdown_data tool.",
        human_validation=False
    )

    # TASK 4: Auto-Trigger Next Chunk
    task_4_id = db.create_task(
        name="Trigger Next Markdown Batch",
        description="""Analyze the result of the previous task. 
If the output indicates FINISHED or END_OF_DOCUMENT in earlier tasks, you MUST output 'EXPLORATION FINISHED' and STOP. Do not call any tools.
Otherwise, you MUST use the 'trigger_next_batch' tool to continue the loop!
CRITICAL RULE: You MUST pass exactly "Markdown Table Extractor (Loop)" as the workflow_name argument.
Pass exactly "{db_name}" as the db_name argument.
Pass exactly {limit} as the limit argument.
Calculate new_offset as {offset} + {limit} and pass it as the new_offset argument.
Output the exact success message from the trigger tool.""",
        expected_output="Trigger success message or EXPLORATION FINISHED.",
        agent_id=clausius_agent_id,
        tools=["trigger_next_batch"],
        agent_specialization="System automation orchestrator. Your only job is to trigger the next batch.",
        human_validation=False
    )

    # 3. Create the Workflow
    task_ids = [task_1_id, task_2_id, task_3_id, task_4_id]
    
    db.create_workflow(
        name="Markdown Table Extractor (Loop)",
        task_ids=task_ids,
        requires_human_check=False
    )
    print("✅ Successfully set up the Markdown Table Extractor workflow!")

if __name__ == "__main__":
    setup_markdown_extractor()
