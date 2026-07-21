import sys, psycopg2, json
sys.path.append('.')
from core.db_manager import DBManager
db = DBManager()
cur = db.cursor

task_5_desc = '''Read the RAG database linearly using the 'read_rag_chunks' tool. 
You MUST use db_name: '{db_name}', offset: {offset}, and limit: {limit}.
Analyze the returned chunk text to determine if it contains a table of thermodynamic properties, an index mapping, or just spare text. 
Use the provided '{document_type}' to help identify the properties shown.
Also consider the '{context}' which may contain the ID/Name of a component that was cut in half in the previous chunk.
If it is a useful table/index, output a JSON string containing:
{"status": "TABLE", "context": "Detailed instructions on what properties map to what fields. If a component was cut in half, include its ID/Name from the input {context} here so the extractor knows what component the dangling numbers belong to.", "raw_chunk": "The exact raw text you read"}
If it is spare text without useful data, output strictly:
{"status": "SKIP"}
If the tool returns END_OF_DOCUMENT (meaning there is no more data to read), output strictly:
{"status": "FINISHED"}'''

task_6_desc = '''Analyze the JSON output from the previous task: '{previous_result}'.
If the status in the JSON is 'SKIP', you MUST output a valid JSON matching ExtractionOutput with status set to "SKIP" and all lists empty.
If the status in the JSON is 'FINISHED', you MUST output a valid JSON matching ExtractionOutput with status set to "FINISHED" and all lists empty.
Otherwise, use the 'raw_chunk' and the 'context' instructions provided in the JSON to extract the thermodynamic data row by row.
CRITICAL: You MUST format your JSON EXACTLY with these root keys:
{
  "status": "TABLE",
  "pure_components": [
    {
      "id_no": "1",
      "component_name": "ARGON",
      "molwt": "39.948",
      "tbp": "-185.9"
    }
  ],
  "bips": [],
  "enrtl": []
}
DO NOT use "items" as a key. You MUST use "pure_components".
CRITICAL: Prioritize extracting the numerical ID into the 'id_no' field if present (e.g., '1', '2'). Leave 'component_name' empty if only an ID is given.
If the 'context' tells you that the first numbers belong to a component from the previous chunk, assign them to that component's ID/Name!
Format all the extracted data STRICTLY according to the ExtractionOutput Pydantic schema.'''

task_7_desc = '''The '{previous_result}' is a JSON string containing thermodynamic data (with a 'status' field).
If the JSON has "status": "SKIP" or "status": "FINISHED", YOU MUST STILL EXECUTE THE 'merge_and_save_data' TOOL! 
Pass the exact string '{previous_result}' as the 'validated_data_json' argument to the tool.
The tool will handle it gracefully.
DO NOT explain the JSON. DO NOT rewrite the JSON. JUST CALL THE TOOL.
Output the exact confirmation string returned by the tool.'''

task_8_desc = '''Analyze the result of the previous task: '{previous_result}'.
If the previous tool output indicates that the JSON had status "FINISHED", you MUST output 'EXPLORATION FINISHED' and STOP. Do not call any tools.
Otherwise (even if it was skipped or successfully written), you MUST use the 'trigger_next_batch' tool to continue the loop!
CRITICAL RULE: You MUST pass exactly "Thermo Explorer (Autonomous)" as the workflow_name argument. Do not invent or pick other names!
Pass exactly "{db_name}" as the db_name argument.
Pass exactly {limit} as the limit argument.
Calculate new_offset as {offset} + {limit} and pass it as the new_offset argument.
Pass "{document_type}" for document_type.
For the 'context' variable, if there was valid extracted data in this batch, pass the ID and Name of the LAST component processed. If none or skipped, pass "{context}" (the previous context) to carry it over.
Output the exact success message from the trigger tool.'''

cur.execute("UPDATE tasks SET description = %s, expected_output = %s WHERE id = 5", (task_5_desc, 'A strict JSON object with status (TABLE, SKIP, or FINISHED).'))
cur.execute("UPDATE tasks SET description = %s, expected_output = %s, output_pydantic = %s WHERE id = 6", (task_6_desc, 'A valid JSON matching the ExtractionOutput schema.', 'ExtractionOutput'))
cur.execute("UPDATE tasks SET description = %s, expected_output = %s WHERE id = 7", (task_7_desc, 'Confirmation string from the merge_and_save_data tool.'))
cur.execute("UPDATE tasks SET description = %s, expected_output = %s WHERE id = 8", (task_8_desc, 'Trigger success message or EXPLORATION FINISHED.'))

# Also ensure workflow only uses tasks 5, 6, 7, 8
cur.execute("UPDATE workflows SET task_ids_json = %s WHERE name = 'Thermo Explorer (Autonomous)'", (json.dumps([5, 6, 7, 8]),))

db.conn.commit()
print('Tasks 5, 6, 7, 8 updated successfully!')
