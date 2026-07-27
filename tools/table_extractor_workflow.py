import os
import sys
import pandas as pd
from typing import List

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.schemas.thermo_schemas import MarkdownThermalExtraction

from crewai import Agent, Task, Crew, Process
from langchain_community.chat_models import ChatOllama

# Configurazione del modello LLM Locale
# Qui impostiamo Qwen3 14b, assumendo che sia ospitato localmente (es. tramite Ollama)
local_llm = ChatOllama(
    model="qwen3:14b",
    base_url="http://localhost:11434"
)

# 1. Definizione degli Agenti
research_agent = Agent(
    role="Data Alignment Specialist",
    goal="Analyze markdown text, identify tables, and logically align split rows using the 'NO' (ID) column.",
    backstory="You are an expert at reading messy scientific documents, finding tables, and preparing data for extraction. You understand that some tables are split into multiple parts and must be correlated by ID.",
    llm=local_llm,
    verbose=True,
    allow_delegation=False
)

thermo_agent = Agent(
    role="Thermodynamic Data Extractor",
    goal="Extract aligned tabular data into a strict JSON format matching the Pydantic schema.",
    backstory="You are a precise data extractor. You take aligned textual data and map it perfectly to rigorous Pydantic schemas, ensuring no data is lost and types are correct.",
    llm=local_llm,
    verbose=True,
    allow_delegation=False
)

def run_extraction(text_chunk: str) -> MarkdownThermalExtraction:
    """Esegue il workflow CrewAI sul blocco di testo fornito."""
    
    task1 = Task(
        description=f"Review the following text chunk. It contains pieces of a split markdown table. Identify all columns and rows. Correlate the rows that have the same 'NO' (ID) so that all properties for a single ID are together. Text: {text_chunk}",
        expected_output="A clean, reunified text representation of the table where each 'NO' has all its properties on one line or logically grouped.",
        agent=research_agent
    )

    task2 = Task(
        description="Take the reunified tabular data from the previous task and extract it into the MarkdownThermalExtraction schema. Ensure all fields like VISA, VISB, TBP, MOLWT, etc., are captured.",
        expected_output="A strict JSON matching the MarkdownThermalExtraction schema.",
        agent=thermo_agent,
        output_pydantic=MarkdownThermalExtraction
    )

    crew = Crew(
        agents=[research_agent, thermo_agent],
        tasks=[task1, task2],
        process=Process.sequential,
        verbose=True
    )

    result = crew.kickoff()
    
    # In CrewAI le ultime versioni restituiscono un oggetto con attributo pydantic
    if hasattr(result, 'pydantic') and result.pydantic:
        return result.pydantic
    elif isinstance(result, MarkdownThermalExtraction):
        return result
    else:
        # Fallback nel caso in cui il parsing automatico non vada a buon fine
        return None

def export_to_excel(extracted_data: MarkdownThermalExtraction, output_path: str = "storage/extracted_thermal_data.xlsx"):
    """Esporta i dati estratti nel file Excel, appendendo o creando un nuovo file."""
    if not extracted_data or not extracted_data.rows:
        print("No rows extracted or object is invalid.")
        return

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Converti gli oggetti Pydantic in dicts tramite model_dump() (Pydantic v2)
    # Se pydantic v1: row.dict()
    try:
        data = [row.model_dump() for row in extracted_data.rows]
    except AttributeError:
        data = [row.dict() for row in extracted_data.rows]
        
    df = pd.DataFrame(data)
    
    if os.path.exists(output_path):
        existing_df = pd.read_excel(output_path)
        # Uniamo e aggiorniamo per NO
        df = pd.concat([existing_df, df], ignore_index=True)
        # Mantieni l'ultimo valore estratto per evitare duplicati
        df.drop_duplicates(subset=['NO'], keep='last', inplace=True)
    
    df.to_excel(output_path, index=False)
    print(f"Data exported successfully to {output_path}")

def chunk_markdown_file(file_path: str, lines_per_chunk: int = 150):
    """Generatore per dividere il file markdown in blocchi da analizzare."""
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    for i in range(0, len(lines), lines_per_chunk):
        yield "".join(lines[i:i + lines_per_chunk])

if __name__ == "__main__":
    file_path = "markdown_thermalProp.md"
    
    print("Starting Data Extraction Workflow...")
    chunks = list(chunk_markdown_file(file_path, lines_per_chunk=200))
    
    for idx, chunk in enumerate(chunks):
        # Eseguiamo solo se ci sono evidenze di una tabella (es. colonna NO)
        if "|  NO |" in chunk:
            print(f"Processing chunk {idx}...")
            try:
                extraction_result = run_extraction(chunk)
                if extraction_result:
                    export_to_excel(extraction_result)
                else:
                    print(f"Extraction failed to return expected Pydantic object for chunk {idx}.")
            except Exception as e:
                print(f"Error processing chunk {idx}: {e}")
                
            # Rimuovere il break per analizzare tutto il file. 
            # Inizialmente lo lasciamo per testare un solo chunk
            break
