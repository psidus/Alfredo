import os
import pandas as pd
from pydantic import BaseModel
from core.schemas.thermo_schemas import MarkdownThermalExtraction
from crewai.tools import tool

DB_PATH = "storage/thermo_database.xlsx"
SHEET_NAME = "C&R_thermo"

@tool
def save_markdown_data(validated_data_json: str) -> str:
    """
    Reads the existing Excel DB, merges new thermodynamic data extracted from markdown,
    resolves conflicts by NO (ID), and saves.
    Input must be a JSON string matching MarkdownThermalExtraction schema.
    """
    if validated_data_json.strip() in ["SKIP", "FINISHED"]:
        return f"Tool bypassed gracefully because input was {validated_data_json.strip()}"

    # Extract JSON if wrapped in markdown code blocks
    import re
    json_str = validated_data_json.strip()
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', json_str, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        start = json_str.find('{')
        end = json_str.rfind('}')
        if start != -1 and end != -1 and end > start:
            json_str = json_str[start:end+1]

    try:
        # In Pydantic v2 use model_validate_json, in v1 parse_raw
        try:
            validated_data = MarkdownThermalExtraction.model_validate_json(json_str)
        except AttributeError:
            validated_data = MarkdownThermalExtraction.parse_raw(json_str)
    except Exception as e:
        return f"Error parsing JSON against MarkdownThermalExtraction schema: {e}"

    if not validated_data.rows:
        return "No rows to save in the provided JSON."

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    try:
        data = [row.dict() for row in validated_data.rows]
        df = pd.DataFrame(data)
        
        if os.path.exists(DB_PATH):
            try:
                existing_df = pd.read_excel(DB_PATH, sheet_name=SHEET_NAME)
                df = pd.concat([existing_df, df], ignore_index=True)
                df.drop_duplicates(subset=['NO'], keep='last', inplace=True)
            except Exception:
                # If the sheet doesn't exist yet, we just write the new df
                pass
                
            with pd.ExcelWriter(DB_PATH, engine='openpyxl', mode='a', if_sheet_exists='replace') as writer:
                df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
        else:
            with pd.ExcelWriter(DB_PATH, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
                
        return f"Successfully saved {len(validated_data.rows)} rows to Excel (Sheet: {SHEET_NAME})."
    except Exception as e:
        return f"Error saving to Excel: {e}"
