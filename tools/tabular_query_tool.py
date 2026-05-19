import os
import glob
from typing import Type, List, Optional

import pandas as pd
from pydantic import BaseModel, Field
from crewai.tools import BaseTool


class TabularQueryInput(BaseModel):
    """Input schema for TabularQueryTool."""
    file_name: str = Field(
        ...,
        description=(
            "The name of the CSV file to query (e.g. 'results.csv' or 'data_Sheet1.csv'). "
            "Must match a file inside the database's structured/ subfolder."
        )
    )
    action: str = Field(
        ...,
        description=(
            "The action to perform on the table. Must be one of: "
            "'list' (list all available CSV files), "
            "'info' (columns, dtypes, non-null counts), "
            "'head' (first 5 rows), "
            "'summary' (descriptive statistics), "
            "'query' (filter rows using a pandas query string)."
        )
    )
    query_string: Optional[str] = Field(
        default=None,
        description=(
            "A pandas .query() expression to filter rows. "
            "Required only when action='query'. "
            "Example: \"Temperature > 800\" or \"`Biomass Type` == 'Wood chips'\"."
        )
    )


class TabularQueryTool(BaseTool):
    """
    A CrewAI tool that allows agents to inspect and query structured CSV/Excel files
    stored in the `structured/` subfolder of a vector database directory.

    Use this tool when you need to:
    - Explore tabular data (column names, data types, statistics)
    - Filter rows by specific conditions using pandas query syntax
    - Read raw rows from CSV files derived from uploaded Excel spreadsheets
    """

    name: str = "tabular_query"
    description: str = (
        "Query tabular data (CSV/Excel) associated with this knowledge base. "
        "Use action='list' to see available tables, 'info' for schema, 'head' for a preview, "
        "'summary' for statistics, or 'query' with a pandas filter expression to find specific rows."
    )
    args_schema: Type[BaseModel] = TabularQueryInput

    # Path to the `structured/` folder for this database
    structured_dir: str = Field(..., description="Absolute path to the structured/ directory of the vector DB.")

    def _get_csv_path(self, file_name: str) -> str:
        """Resolves the full path and validates the file is inside structured_dir."""
        # Ensure only the basename is used to prevent path traversal
        safe_name = os.path.basename(file_name)
        full_path = os.path.join(self.structured_dir, safe_name)
        return full_path

    def _list_files(self) -> str:
        csv_files = glob.glob(os.path.join(self.structured_dir, "*.csv"))
        if not csv_files:
            return "No CSV files found in the structured data folder."
        names = [os.path.basename(f) for f in sorted(csv_files)]
        return "Available CSV tables:\n" + "\n".join(f"  - {n}" for n in names)

    def _run(self, file_name: str, action: str, query_string: Optional[str] = None) -> str:
        # Allow listing without specifying a file
        if action == "list":
            return _list_files_in(self.structured_dir)

        full_path = self._get_csv_path(file_name)

        if not os.path.exists(full_path):
            available = self._list_files()
            return (
                f"File '{file_name}' not found in the structured data folder.\n"
                f"{available}"
            )

        try:
            df = pd.read_csv(full_path)
        except Exception as e:
            return f"Error reading '{file_name}': {e}"

        action = action.lower().strip()

        if action == "info":
            buf = []
            buf.append(f"File: {file_name}")
            buf.append(f"Shape: {df.shape[0]} rows × {df.shape[1]} columns")
            buf.append("\nColumns:")
            for col in df.columns:
                non_null = df[col].notna().sum()
                buf.append(f"  • {col} ({df[col].dtype}) — {non_null}/{len(df)} non-null values")
            return "\n".join(buf)

        elif action == "head":
            return f"First 5 rows of '{file_name}':\n{df.head().to_string(index=False)}"

        elif action == "summary":
            numeric = df.select_dtypes(include="number")
            if numeric.empty:
                return f"No numeric columns found in '{file_name}' to summarize."
            return (
                f"Statistical summary of '{file_name}' (numeric columns):\n"
                f"{numeric.describe().to_string()}"
            )

        elif action == "query":
            if not query_string:
                return "Error: action='query' requires a 'query_string' parameter."
            try:
                result = df.query(query_string)
                if result.empty:
                    return f"Query '{query_string}' returned 0 matching rows."
                return (
                    f"Query '{query_string}' matched {len(result)} row(s):\n"
                    f"{result.to_string(index=False)}"
                )
            except Exception as e:
                return (
                    f"Error executing query '{query_string}': {e}\n"
                    "Tip: Use backticks for column names with spaces, e.g. `Column Name` == 'value'."
                )

        else:
            return (
                f"Unknown action '{action}'. "
                "Valid actions: 'list', 'info', 'head', 'summary', 'query'."
            )


def _list_files_in(structured_dir: str) -> str:
    """Standalone helper for listing CSV files in a structured directory."""
    csv_files = glob.glob(os.path.join(structured_dir, "*.csv"))
    if not csv_files:
        return "No CSV files found in the structured data folder."
    names = [os.path.basename(f) for f in sorted(csv_files)]
    return "Available CSV tables:\n" + "\n".join(f"  - {n}" for n in names)
