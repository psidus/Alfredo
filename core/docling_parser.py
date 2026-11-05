"""
core/docling_parser.py

Enterprise Document Intelligence & Layout-Aware Table Parser for Alfredo AI OS.
Integrates `docling-project/docling` (https://github.com/docling-project/docling)
to convert complex PDFs, multi-column documents, Word (.docx), PowerPoint (.pptx),
and Excel (.xlsx) into clean Markdown with tables preserved.
Includes an automatic layout-and-table-aware fallback (`pdfplumber` + `docx` + `openpyxl`).
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional
from crewai.tools import tool

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORKSPACE_DIR = os.path.join(PROJECT_ROOT, "workspace")


def convert_document_to_markdown(file_path: str, max_chars: int = 35000) -> str:
    """
    Converts a PDF, DOCX, PPTX, XLSX, CSV, or TXT file into structured Markdown
    preserving tables and section headers.
    Tries IBM Docling first, then falls back to table-aware pdfplumber/docx/openpyxl.
    """
    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        # Try relative to workspace/
        ws_candidate = os.path.abspath(os.path.join(WORKSPACE_DIR, file_path))
        if os.path.isfile(ws_candidate):
            abs_path = ws_candidate
        else:
            return f"Error: File not found at '{file_path}'."

    ext = os.path.splitext(abs_path)[1].lower()

    # 1. Try IBM Docling first for PDF / DOCX / PPTX / HTML / Images
    if ext in (".pdf", ".docx", ".pptx", ".html", ".png", ".jpg", ".jpeg"):
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
            converter = DocumentConverter()
            conv_res = converter.convert(abs_path)
            md = conv_res.document.export_to_markdown()
            if md and len(md.strip()) > 20:
                return md.strip()[:max_chars]
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[DoclingParser] Docling fallback triggered for {abs_path}: {e}")

    # 2. High-precision table-aware fallback per format
    if ext == ".pdf":
        try:
            import pdfplumber
            blocks: List[str] = [f"# Document: {os.path.basename(abs_path)}"]
            with pdfplumber.open(abs_path) as pdf:
                for p_idx, page in enumerate(pdf.pages):
                    blocks.append(f"\n## Page {p_idx + 1}")
                    txt = (page.extract_text() or "").strip()
                    if txt:
                        blocks.append(txt)
                    tables = page.extract_tables() or []
                    for t_idx, tbl in enumerate(tables):
                        if not tbl:
                            continue
                        rows_md = []
                        for r_i, row in enumerate(tbl):
                            clean_cells = [
                                (str(c).replace("\n", " ").replace("|", "/").strip() if c is not None else "")
                                for c in row
                            ]
                            if any(clean_cells):
                                rows_md.append("| " + " | ".join(clean_cells) + " |")
                                if r_i == 0:
                                    rows_md.append("| " + " | ".join(["---"] * len(clean_cells)) + " |")
                        if rows_md:
                            blocks.append(f"\n### Table {p_idx + 1}.{t_idx + 1}\n" + "\n".join(rows_md))
            return "\n\n".join(blocks)[:max_chars]
        except Exception as e:
            logger.warning(f"[DoclingParser] pdfplumber error on {abs_path}: {e}")

    if ext == ".docx":
        try:
            import docx
            doc = docx.Document(abs_path)
            lines = [f"# Document: {os.path.basename(abs_path)}"]
            for p in doc.paragraphs:
                t = p.text.strip()
                if not t:
                    continue
                style_name = (p.style.name if p.style else "").lower()
                if "heading 1" in style_name:
                    lines.append(f"\n# {t}")
                elif "heading 2" in style_name:
                    lines.append(f"\n## {t}")
                elif "heading 3" in style_name:
                    lines.append(f"\n### {t}")
                else:
                    lines.append(t)
            for idx, table in enumerate(doc.tables):
                lines.append(f"\n### Table {idx + 1}")
                for r_i, row in enumerate(table.rows):
                    cells = [c.text.strip().replace("\n", " ").replace("|", "/") for c in row.cells]
                    lines.append("| " + " | ".join(cells) + " |")
                    if r_i == 0:
                        lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
            return "\n".join(lines)[:max_chars]
        except Exception as e:
            return f"Error parsing DOCX '{abs_path}': {e}"

    if ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(abs_path, data_only=True)
            lines = [f"# Workbook: {os.path.basename(abs_path)}"]
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                lines.append(f"\n## Sheet: {sheet}")
                rows = list(ws.iter_rows(values_only=True))[:250]
                for r_i, row in enumerate(rows):
                    if not any(c is not None for c in row):
                        continue
                    cells = ["" if c is None else str(c).replace("\n", " ").replace("|", "/") for c in row]
                    lines.append("| " + " | ".join(cells) + " |")
                    if r_i == 0:
                        lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
            return "\n".join(lines)[:max_chars]
        except Exception as e:
            return f"Error parsing Excel '{abs_path}': {e}"

    # Generic text fallback
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as e:
        return f"Error reading document '{abs_path}': {e}"


@tool
def parse_complex_document(file_path: str) -> str:
    """
    Converte documenti complessi (PDF scientifici/industriali a più colonne, tabelle, Word .docx,
    Excel .xlsx, PowerPoint .pptx) in Markdown strutturato ad alta precisione usando IBM Docling
    (con estrazione tabelle integrata).
    Args:
        file_path: Percorso del file (relativo a workspace/ oppure percorso completo).
    """
    return convert_document_to_markdown(file_path)
