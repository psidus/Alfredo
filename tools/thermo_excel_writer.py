import os
import pandas as pd
from pydantic import BaseModel
from typing import Optional
from core.thermo_schemas import ExtractionOutput
from crewai.tools import tool

DB_PATH = "storage/thermo_database.xlsx"

def init_excel_db():
    """Initializes the Excel database with the 3 required sheets if it doesn't exist."""
    if not os.path.exists("storage"):
        os.makedirs("storage")
    
    if not os.path.exists(DB_PATH):
        with pd.ExcelWriter(DB_PATH, engine='openpyxl') as writer:
            # Sheet 1: T_Dependent_Properties
            pd.DataFrame(columns=[
                "Component Name", "CAS", "Equation Form", "T_min", "T_max", "Physical State",
                "HeatCapacityGas_coeffs", "HeatCapacityLiquid_coeffs", "VaporPressure_coeffs",
                "VolumeLiquid_coeffs", "ViscosityLiquid_coeffs", "ViscosityGas_coeffs",
                "ThermalConductivityLiquid_coeffs", "ThermalConductivityGas_coeffs", "sigma_e_coeffs",
                "Reference", "Confidence"
            ]).to_excel(writer, sheet_name="T_Dependent_Properties", index=False)
            
            # Sheet 2: BIPs
            pd.DataFrame(columns=[
                "Component 1", "Component 2", "Phase", "T_range",
                "k_ij", "A_mm", "B_mm", "alpha_mm",
                "Reference", "Confidence"
            ]).to_excel(writer, sheet_name="BIPs", index=False)
            
            # Sheet 3: eNRTL_Coeffs
            pd.DataFrame(columns=[
                "Molecule (m)", "Cation (c)", "Anion (a)", "T_range",
                "alpha_mca", "tau_mca_A", "tau_mca_B", "tau_cam_A", "tau_cam_B", "tau_caca",
                "Reference", "Confidence"
            ]).to_excel(writer, sheet_name="eNRTL_Coeffs", index=False)
        print(f"Initialized empty Excel database at {DB_PATH}")

@tool
def merge_and_save_data(validated_data_json: str) -> str:
    """
    Reads the existing Excel DB, merges new thermodynamic data incrementally (filling gaps),
    resolves conflicts (T range, physical state, chemical structures), and saves.
    Input must be a JSON string matching ExtractionOutput schema.
    """
    try:
        validated_data = ExtractionOutput.parse_raw(validated_data_json)
    except Exception as e:
        return f"Error parsing JSON against ExtractionOutput schema: {e}"

    if not os.path.exists(DB_PATH):
        init_excel_db()
        
    try:
        with pd.ExcelWriter(DB_PATH, engine='openpyxl', mode='a', if_sheet_exists='overlay') as writer:
            # Load existing sheets
            df_pure = pd.read_excel(DB_PATH, sheet_name="T_Dependent_Properties")
            df_bip = pd.read_excel(DB_PATH, sheet_name="BIPs")
            df_enrtl = pd.read_excel(DB_PATH, sheet_name="eNRTL_Coeffs")
            
            # Merge Pure Components
            for item in validated_data.pure_components:
                # Conflict logic: same name, physical state, and T range
                mask = (df_pure["Component Name"] == item.component_name) & \
                       (df_pure["Physical State"] == item.physical_state) & \
                       (df_pure["T_min"] == item.t_min) & \
                       (df_pure["T_max"] == item.t_max)
                       
                if mask.any():
                    idx = df_pure[mask].index[0]
                    for col, val in item.dict().items():
                        col_map = {
                            "component_name": "Component Name", "cas": "CAS", "equation_form": "Equation Form",
                            "t_min": "T_min", "t_max": "T_max", "physical_state": "Physical State",
                            "HeatCapacityGas_coeffs": "HeatCapacityGas_coeffs", "HeatCapacityLiquid_coeffs": "HeatCapacityLiquid_coeffs",
                            "VaporPressure_coeffs": "VaporPressure_coeffs", "VolumeLiquid_coeffs": "VolumeLiquid_coeffs",
                            "ViscosityLiquid_coeffs": "ViscosityLiquid_coeffs", "ViscosityGas_coeffs": "ViscosityGas_coeffs",
                            "ThermalConductivityLiquid_coeffs": "ThermalConductivityLiquid_coeffs", "ThermalConductivityGas_coeffs": "ThermalConductivityGas_coeffs",
                            "sigma_e_coeffs": "sigma_e_coeffs", "reference": "Reference"
                        }
                        if col in col_map and val is not None:
                            excel_col = col_map[col]
                            if pd.isna(df_pure.at[idx, excel_col]):
                                df_pure.at[idx, excel_col] = val
                else:
                    new_row = pd.DataFrame([{
                        "Component Name": item.component_name, "CAS": item.cas, "Equation Form": item.equation_form,
                        "T_min": item.t_min, "T_max": item.t_max, "Physical State": item.physical_state,
                        "HeatCapacityGas_coeffs": item.HeatCapacityGas_coeffs, "HeatCapacityLiquid_coeffs": item.HeatCapacityLiquid_coeffs,
                        "VaporPressure_coeffs": item.VaporPressure_coeffs, "VolumeLiquid_coeffs": item.VolumeLiquid_coeffs,
                        "ViscosityLiquid_coeffs": item.ViscosityLiquid_coeffs, "ViscosityGas_coeffs": item.ViscosityGas_coeffs,
                        "ThermalConductivityLiquid_coeffs": item.ThermalConductivityLiquid_coeffs, "ThermalConductivityGas_coeffs": item.ThermalConductivityGas_coeffs,
                        "sigma_e_coeffs": item.sigma_e_coeffs, "Reference": item.reference, "Confidence": validated_data.confidence_score
                    }])
                    df_pure = pd.concat([df_pure, new_row], ignore_index=True)

            # Merge BIPs
            for item in validated_data.bips:
                mask = (df_bip["Component 1"] == item.component_1) & \
                       (df_bip["Component 2"] == item.component_2) & \
                       (df_bip["Phase"] == item.phase) & \
                       (df_bip["T_range"] == item.t_range)
                if mask.any():
                    idx = df_bip[mask].index[0]
                    for col, val in item.dict().items():
                        col_map = {
                            "component_1": "Component 1", "component_2": "Component 2", "phase": "Phase", "t_range": "T_range",
                            "k_ij": "k_ij", "A_mm": "A_mm", "B_mm": "B_mm", "alpha_mm": "alpha_mm", "reference": "Reference"
                        }
                        if col in col_map and val is not None:
                            excel_col = col_map[col]
                            if pd.isna(df_bip.at[idx, excel_col]):
                                df_bip.at[idx, excel_col] = val
                else:
                    new_row = pd.DataFrame([{
                        "Component 1": item.component_1, "Component 2": item.component_2, "Phase": item.phase,
                        "T_range": item.t_range, "k_ij": item.k_ij, "A_mm": item.A_mm, "B_mm": item.B_mm,
                        "alpha_mm": item.alpha_mm, "Reference": item.reference, "Confidence": validated_data.confidence_score
                    }])
                    df_bip = pd.concat([df_bip, new_row], ignore_index=True)

            # Merge eNRTL
            for item in validated_data.enrtl:
                mask = (df_enrtl["Molecule (m)"] == item.molecule_m) & \
                       (df_enrtl["Cation (c)"] == item.cation_c) & \
                       (df_enrtl["Anion (a)"] == item.anion_a) & \
                       (df_enrtl["T_range"] == item.t_range)
                if mask.any():
                    idx = df_enrtl[mask].index[0]
                    for col, val in item.dict().items():
                        col_map = {
                            "molecule_m": "Molecule (m)", "cation_c": "Cation (c)", "anion_a": "Anion (a)", "t_range": "T_range",
                            "alpha_mca": "alpha_mca", "tau_mca_A": "tau_mca_A", "tau_mca_B": "tau_mca_B", 
                            "tau_cam_A": "tau_cam_A", "tau_cam_B": "tau_cam_B", "tau_caca": "tau_caca", "reference": "Reference"
                        }
                        if col in col_map and val is not None:
                            excel_col = col_map[col]
                            if pd.isna(df_enrtl.at[idx, excel_col]):
                                df_enrtl.at[idx, excel_col] = val
                else:
                    new_row = pd.DataFrame([{
                        "Molecule (m)": item.molecule_m, "Cation (c)": item.cation_c, "Anion (a)": item.anion_a,
                        "T_range": item.t_range, "alpha_mca": item.alpha_mca, "tau_mca_A": item.tau_mca_A,
                        "tau_mca_B": item.tau_mca_B, "tau_cam_A": item.tau_cam_A, "tau_cam_B": item.tau_cam_B,
                        "tau_caca": item.tau_caca, "Reference": item.reference, "Confidence": validated_data.confidence_score
                    }])
                    df_enrtl = pd.concat([df_enrtl, new_row], ignore_index=True)

            # Write all back
            df_pure.to_excel(writer, sheet_name="T_Dependent_Properties", index=False)
            df_bip.to_excel(writer, sheet_name="BIPs", index=False)
            df_enrtl.to_excel(writer, sheet_name="eNRTL_Coeffs", index=False)
            
        return f"Success. Database incrementally updated and saved to {DB_PATH}"
    except Exception as e:
        return f"Error merging data to Excel: {e}"

if __name__ == "__main__":
    init_excel_db()
