import os
import pandas as pd
from pydantic import BaseModel
from typing import Optional
from core.schemas.thermo_schemas import ExtractionOutput
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
                "ID_No", "Component Name", "CAS", "Equation Form", "T_min", "T_max", "Physical State",
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
            if "ID_No" not in df_pure.columns:
                df_pure.insert(0, "ID_No", None)
            df_bip = pd.read_excel(DB_PATH, sheet_name="BIPs")
            df_enrtl = pd.read_excel(DB_PATH, sheet_name="eNRTL_Coeffs")
            
            # Helper functions for matching
            def normalize_name(name):
                if pd.isna(name) or not isinstance(name, str): return ""
                name = name.lower()
                import re
                name = re.sub(r'\[\d+\]', '', name)
                return name.strip()

            def are_substances_same(row_name, row_cas, item_name, item_cas):
                if pd.notna(row_cas) and item_cas:
                    if str(row_cas).strip() == str(item_cas).strip():
                        return True
                n_row = normalize_name(row_name)
                n_item = normalize_name(item_name)
                if n_row and n_item and (n_row in n_item or n_item in n_row):
                    return True
                return False

            def check_conditions_match(row_phys, row_tmin, row_tmax, item_phys, item_tmin, item_tmax):
                if pd.notna(row_phys) and item_phys and str(row_phys).strip().lower() != str(item_phys).strip().lower():
                    return False
                if pd.notna(row_tmin) and item_tmin and float(row_tmin) != float(item_tmin): return False
                if pd.notna(row_tmax) and item_tmax and float(row_tmax) != float(item_tmax): return False
                return True

            def has_conflict(row, item_dict, col_map):
                for dict_key, excel_col in col_map.items():
                    if dict_key in ['reference', 'component_name', 'cas']: continue
                    new_val = item_dict.get(dict_key)
                    if new_val is not None:
                        old_val = row.get(excel_col)
                        if pd.notna(old_val):
                            if str(old_val).strip().lower() != str(new_val).strip().lower():
                                return True
                return False

            # Merge Pure Components
            for item in validated_data.pure_components:
                item_dict = item.dict()
                col_map = {
                    "id_no": "ID_No", "component_name": "Component Name", "cas": "CAS", "equation_form": "Equation Form",
                    "t_min": "T_min", "t_max": "T_max", "physical_state": "Physical State",
                    "HeatCapacityGas_coeffs": "HeatCapacityGas_coeffs", "HeatCapacityLiquid_coeffs": "HeatCapacityLiquid_coeffs",
                    "VaporPressure_coeffs": "VaporPressure_coeffs", "VolumeLiquid_coeffs": "VolumeLiquid_coeffs",
                    "ViscosityLiquid_coeffs": "ViscosityLiquid_coeffs", "ViscosityGas_coeffs": "ViscosityGas_coeffs",
                    "ThermalConductivityLiquid_coeffs": "ThermalConductivityLiquid_coeffs", "ThermalConductivityGas_coeffs": "ThermalConductivityGas_coeffs",
                    "sigma_e_coeffs": "sigma_e_coeffs", "reference": "Reference"
                }

                matched_idx = None
                conflict_found = False
                
                for idx, row in df_pure.iterrows():
                    match = False
                    if item.id_no and pd.notna(row.get("ID_No")) and str(row.get("ID_No")).strip() == str(item.id_no).strip():
                        match = True
                    elif are_substances_same(row["Component Name"], row.get("CAS"), item.component_name, item.cas):
                        match = True

                    if match:
                        if check_conditions_match(row.get("Physical State"), row.get("T_min"), row.get("T_max"), 
                                                  item.physical_state, item.t_min, item.t_max):
                            if has_conflict(row, item_dict, col_map):
                                conflict_found = True
                            else:
                                matched_idx = idx
                                conflict_found = False # Reset if we find a perfect non-conflicting match
                                break
                
                if matched_idx is not None:
                    # Merge into existing row
                    for dict_key, excel_col in col_map.items():
                        new_val = item_dict.get(dict_key)
                        if new_val is not None:
                            if dict_key == 'reference':
                                old_ref = df_pure.at[matched_idx, excel_col]
                                if pd.notna(old_ref):
                                    if str(new_val) not in str(old_ref):
                                        df_pure.at[matched_idx, excel_col] = str(old_ref) + "; " + str(new_val)
                                else:
                                    df_pure.at[matched_idx, excel_col] = new_val
                            elif dict_key != 'component_name':
                                if pd.isna(df_pure.at[matched_idx, excel_col]):
                                    df_pure.at[matched_idx, excel_col] = new_val
                else:
                    # Add new row
                    final_name = item.component_name
                    if conflict_found:
                        count = sum(1 for _, r in df_pure.iterrows() if are_substances_same(r["Component Name"], r.get("CAS"), item.component_name, item.cas))
                        if count > 0:
                            final_name = f"{item.component_name} [{count}]"
                            
                    new_row = pd.DataFrame([{
                        "ID_No": item.id_no, "Component Name": final_name, "CAS": item.cas, "Equation Form": item.equation_form,
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
                item_dict = item.dict()
                col_map = {
                    "component_1": "Component 1", "component_2": "Component 2", "phase": "Phase", "t_range": "T_range",
                    "k_ij": "k_ij", "A_mm": "A_mm", "B_mm": "B_mm", "alpha_mm": "alpha_mm", "reference": "Reference"
                }

                matched_idx = None
                conflict_found = False
                
                for idx, row in df_bip.iterrows():
                    if are_substances_same(row["Component 1"], None, item.component_1, None) and \
                       are_substances_same(row["Component 2"], None, item.component_2, None):
                        
                        # Check conditions
                        cond_match = True
                        if pd.notna(row.get("Phase")) and item.phase and str(row.get("Phase")).strip().lower() != str(item.phase).strip().lower():
                            cond_match = False
                        if pd.notna(row.get("T_range")) and item.t_range and str(row.get("T_range")).strip().lower() != str(item.t_range).strip().lower():
                            cond_match = False
                            
                        if cond_match:
                            if has_conflict(row, item_dict, col_map):
                                conflict_found = True
                            else:
                                matched_idx = idx
                                conflict_found = False
                                break
                                
                if matched_idx is not None:
                    for dict_key, excel_col in col_map.items():
                        new_val = item_dict.get(dict_key)
                        if new_val is not None:
                            if dict_key == 'reference':
                                old_ref = df_bip.at[matched_idx, excel_col]
                                if pd.notna(old_ref):
                                    if str(new_val) not in str(old_ref):
                                        df_bip.at[matched_idx, excel_col] = str(old_ref) + "; " + str(new_val)
                                else:
                                    df_bip.at[matched_idx, excel_col] = new_val
                            elif dict_key not in ['component_1', 'component_2']:
                                if pd.isna(df_bip.at[matched_idx, excel_col]):
                                    df_bip.at[matched_idx, excel_col] = new_val
                else:
                    final_name1 = item.component_1
                    if conflict_found:
                        count = sum(1 for _, r in df_bip.iterrows() if are_substances_same(r["Component 1"], None, item.component_1, None) and are_substances_same(r["Component 2"], None, item.component_2, None))
                        if count > 0:
                            final_name1 = f"{item.component_1} [{count}]"
                            
                    new_row = pd.DataFrame([{
                        "Component 1": final_name1, "Component 2": item.component_2, "Phase": item.phase,
                        "T_range": item.t_range, "k_ij": item.k_ij, "A_mm": item.A_mm, "B_mm": item.B_mm,
                        "alpha_mm": item.alpha_mm, "Reference": item.reference, "Confidence": validated_data.confidence_score
                    }])
                    df_bip = pd.concat([df_bip, new_row], ignore_index=True)

            # Merge eNRTL
            for item in validated_data.enrtl:
                item_dict = item.dict()
                col_map = {
                    "molecule_m": "Molecule (m)", "cation_c": "Cation (c)", "anion_a": "Anion (a)", "t_range": "T_range",
                    "alpha_mca": "alpha_mca", "tau_mca_A": "tau_mca_A", "tau_mca_B": "tau_mca_B", 
                    "tau_cam_A": "tau_cam_A", "tau_cam_B": "tau_cam_B", "tau_caca": "tau_caca", "reference": "Reference"
                }

                matched_idx = None
                conflict_found = False
                
                for idx, row in df_enrtl.iterrows():
                    if are_substances_same(row["Molecule (m)"], None, item.molecule_m, None) and \
                       str(row["Cation (c)"]).strip().lower() == str(item.cation_c).strip().lower() and \
                       str(row["Anion (a)"]).strip().lower() == str(item.anion_a).strip().lower():
                        
                        cond_match = True
                        if pd.notna(row.get("T_range")) and item.t_range and str(row.get("T_range")).strip().lower() != str(item.t_range).strip().lower():
                            cond_match = False
                            
                        if cond_match:
                            # Adjust has_conflict slightly since keys are different
                            is_conflict = False
                            for dict_key, excel_col in col_map.items():
                                if dict_key in ['reference', 'molecule_m', 'cation_c', 'anion_a']: continue
                                new_val = item_dict.get(dict_key)
                                if new_val is not None:
                                    old_val = row.get(excel_col)
                                    if pd.notna(old_val):
                                        if str(old_val).strip().lower() != str(new_val).strip().lower():
                                            is_conflict = True
                                            break
                            if is_conflict:
                                conflict_found = True
                            else:
                                matched_idx = idx
                                conflict_found = False
                                break
                                
                if matched_idx is not None:
                    for dict_key, excel_col in col_map.items():
                        new_val = item_dict.get(dict_key)
                        if new_val is not None:
                            if dict_key == 'reference':
                                old_ref = df_enrtl.at[matched_idx, excel_col]
                                if pd.notna(old_ref):
                                    if str(new_val) not in str(old_ref):
                                        df_enrtl.at[matched_idx, excel_col] = str(old_ref) + "; " + str(new_val)
                                else:
                                    df_enrtl.at[matched_idx, excel_col] = new_val
                            elif dict_key not in ['molecule_m', 'cation_c', 'anion_a']:
                                if pd.isna(df_enrtl.at[matched_idx, excel_col]):
                                    df_enrtl.at[matched_idx, excel_col] = new_val
                else:
                    final_name_m = item.molecule_m
                    if conflict_found:
                        count = sum(1 for _, r in df_enrtl.iterrows() if are_substances_same(r["Molecule (m)"], None, item.molecule_m, None) and str(r["Cation (c)"]).strip().lower() == str(item.cation_c).strip().lower() and str(r["Anion (a)"]).strip().lower() == str(item.anion_a).strip().lower())
                        if count > 0:
                            final_name_m = f"{item.molecule_m} [{count}]"
                            
                    new_row = pd.DataFrame([{
                        "Molecule (m)": final_name_m, "Cation (c)": item.cation_c, "Anion (a)": item.anion_a,
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
