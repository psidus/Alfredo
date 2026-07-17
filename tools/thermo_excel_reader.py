import os
import pandas as pd
from crewai.tools import tool

DB_PATH = "storage/thermo_database.xlsx"

@tool
def check_excel_db(chemical_name: str) -> str:
    """
    Checks if a chemical component is already present and complete in the master Excel database.
    Input: The chemical name to check (e.g., 'Ethanol').
    Returns 'SKIP' if the component is fully mapped with basic properties.
    Returns 'PROCEED' if the component is missing or incomplete, indicating extraction should happen.
    """
    if not os.path.exists(DB_PATH):
        return "PROCEED"
        
    try:
        df_pure = pd.read_excel(DB_PATH, sheet_name="T_Dependent_Properties")
        
        # Case insensitive match
        mask = df_pure["Component Name"].str.lower() == chemical_name.lower()
        if not mask.any():
            return "PROCEED"
            
        # Get the row
        row = df_pure[mask].iloc[0]
        
        # Check if essential fields are filled
        essential_columns = ["CAS", "Equation Form", "T_min", "T_max", "Physical State"]
        
        for col in essential_columns:
            if col in row.index and pd.isna(row[col]):
                return "PROCEED"  # Missing essential data, we should proceed
                
        # If it reaches here, it's present and essential data is filled
        return "SKIP"
        
    except Exception as e:
        # If any error happens (e.g., missing sheet), proceed to extract
        print(f"Error reading Excel DB: {e}")
        return "PROCEED"
