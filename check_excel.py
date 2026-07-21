import pandas as pd
import os

db_path = "storage/thermo_database.xlsx"
if os.path.exists(db_path):
    print("Excel DB found!")
    try:
        df = pd.read_excel(db_path, sheet_name="T_Dependent_Properties")
        print(f"Total rows in T_Dependent_Properties: {len(df)}")
        if len(df) > 0:
            print(df.tail(3)[['ID_No', 'Component Name', 'HeatCapacityLiquid_coeffs', 'Confidence']])
    except Exception as e:
        print("Error reading T_Dependent_Properties:", e)
        
    try:
        df2 = pd.read_excel(db_path, sheet_name="BIPs")
        print(f"Total rows in BIPs: {len(df2)}")
    except Exception as e:
        pass
else:
    print("Excel DB not found.")
