import os
import logging
import base64
import json
import csv
from io import BytesIO
from typing import List, Dict, Any, Tuple
import cv2
import numpy as np
from PIL import Image
import pdfplumber
from langchain_core.documents import Document
from litellm import completion

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Add file handler to capture logs
if not logger.handlers:
    os.makedirs("storage", exist_ok=True)
    fh = logging.FileHandler("storage/app_debug.log")
    fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(fh)

class ScientificParser:
    def __init__(self, models_config: Dict[str, str], graph_points: int = 10, db_path: str = None, parser_type: str = "pdfplumber"):
        """
        Args:
            models_config: Dictionary mapping roles to model names.
                           e.g., {"graphs": "gpt-4o", "tables": "claude-3-5-sonnet-20240620", "drawings": "gemini/gemini-1.5-pro"}
            graph_points: Number of data points to extract from graphs.
            db_path: Path to the ChromaDB/Qdrant directory (used to save structured CSVs).
            parser_type: "pdfplumber" or "marker"
        """
        self.models_config = models_config
        self.graph_points = graph_points
        self.db_path = db_path
        self.parser_type = parser_type
        
    def _encode_image(self, pil_img: Image.Image) -> str:
        """Convert PIL Image to base64 string for VLM API."""
        # Resize image if it's too large to prevent Ollama payload limits
        max_size = 1024
        if pil_img.width > max_size or pil_img.height > max_size:
            pil_img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            
        # Ensure image is in a safe format (RGB or L)
        if pil_img.mode not in ("RGB", "L"):
            pil_img = pil_img.convert("RGB")
            
        buffered = BytesIO()
        pil_img.save(buffered, format="JPEG", quality=85)
        return base64.b64encode(buffered.getvalue()).decode('utf-8')
        
    def _preprocess_graph(self, pil_img: Image.Image) -> Image.Image:
        """
        Use OpenCV to preprocess the graph image to enhance lines and axes
        and reduce noise before sending it to the VLM.
        """
        open_cv_image = np.array(pil_img)
        
        # Check if it's already grayscale
        if len(open_cv_image.shape) == 3:
            # Convert RGB to BGR (OpenCV format)
            open_cv_image = open_cv_image[:, :, ::-1].copy()
            gray = cv2.cvtColor(open_cv_image, cv2.COLOR_BGR2GRAY)
        else:
            gray = open_cv_image
            
        # Apply slight blur to reduce noise, then adaptive thresholding to enhance lines
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        thresh = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        
        # Convert back to PIL Image
        return Image.fromarray(thresh)

    def _call_vlm(self, image: Image.Image, prompt: str, role: str) -> str:
        """Call LiteLLM with a base64 image and a prompt based on the role."""
        model = self.models_config.get(role)
        if not model:
            logger.warning(f"No model defined for role '{role}', falling back to default description.")
            return f"[Missing model for {role} extraction]"

        base64_img = self._encode_image(image)
        
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_img}"
                        }
                    }
                ]
            }
        ]

        base_delay = 5.0
        attempt = 1
        
        while True:
            try:
                # LiteLLM automatically routes to the right provider based on the model name prefix
                api_base = os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")
                response = completion(
                    model=model,
                    messages=messages,
                    temperature=0.0,
                    api_base=api_base
                )
                return response.choices[0].message.content
            except Exception as e:
                error_str = str(e).lower()
                retryable_keywords = [
                    "429", "resource_exhausted", "rate limit", "quota",
                    "too many requests", "overloaded", "503", "service unavailable",
                    "timeout", "timed out", "connection", "connect", "temporarily unavailable"
                ]
                is_retryable = any(kw in error_str for kw in retryable_keywords)
                
                if is_retryable:
                    # Exponential backoff capped at 120 seconds
                    wait_time = min(base_delay * (2 ** (attempt - 1)), 120.0)
                    logger.warning(f"VLM error '{e}'. System paused. Retrying in {wait_time}s (Attempt {attempt})...")
                    import time
                    time.sleep(wait_time)
                    attempt += 1
                else:
                    logger.error(f"Non-retryable error calling VLM '{model}' for role '{role}': {e}")
                    return f"[Error extracting {role} with {model}: {e}]"

    def _process_graph(self, image: Image.Image, page_num: int, obj_index: int) -> str:
        # Preprocess graph for better line visibility
        processed_img = self._preprocess_graph(image)
        
        prompt = (
            f"You are an expert chemical engineer and data analyst. "
            f"This is a technical graph/chart. Your task is to digitize it. "
            f"Please identify the X and Y axes, their units, and extract EXACTLY {self.graph_points} "
            f"evenly spaced data points (X, Y) from the main curve shown. "
            f"Return the data STRICTLY as a Markdown table with columns: Point, X_value, Y_value. "
            f"Also, provide a brief 1-2 sentence description of what the graph represents before the table."
        )
        result = self._call_vlm(processed_img, prompt, "graphs")
        
        # Optionally save to CSV if structured path is available
        self._save_to_csv(result, f"graph_p{page_num}_obj{obj_index}.csv")
        return result

    def _process_table(self, image: Image.Image, page_num: int, obj_index: int) -> str:
        prompt = (
            "You are an expert data analyst. Please convert this image of a table "
            "into a clean, well-formatted Markdown table. "
            "CRITICAL INSTRUCTIONS:\n"
            "1. Extract ONLY the essential information and data cells.\n"
            "2. DO NOT include any filler spaces, invisible characters, or ambiguous noise used for visual alignment.\n"
            "3. Ensure all headers and data cells are accurate and concise.\n"
            "4. Ignore watermarks, page numbers, or any text outside the actual table.\n"
            "5. Do not include any extra conversational text, output JUST the Markdown table."
        )
        result = self._call_vlm(image, prompt, "tables")
        
        self._save_to_csv(result, f"table_p{page_num}_obj{obj_index}.csv")
        return result

    def _process_drawing(self, image: Image.Image) -> str:
        prompt = (
            "You are an expert chemical engineer. Describe this technical drawing, P&ID, or PFD "
            "in extreme technical detail. Identify all major equipment (columns, heat exchangers, pumps, vessels), "
            "the flow of materials, and any control loops or critical parameters mentioned."
        )
        return self._call_vlm(image, prompt, "drawings")

    def _save_to_csv(self, markdown_text: str, filename: str):
        """Extract markdown tables from the VLM response and save them as CSV in the structured/ directory."""
        if not self.db_path:
            return
            
        structured_dir = os.path.join(self.db_path, "structured")
        os.makedirs(structured_dir, exist_ok=True)
        csv_path = os.path.join(structured_dir, filename)
        
        # Very simple markdown table parser
        lines = markdown_text.split('\n')
        table_lines = [line.strip() for line in lines if line.strip().startswith('|') and line.strip().endswith('|')]
        
        if not table_lines:
            return
            
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                for i, line in enumerate(table_lines):
                    # Skip separator line like |---|---|
                    if set(line.replace('|', '').replace('-', '').replace(':', '').strip()) == set():
                        continue
                    
                    row = [cell.strip() for cell in line.split('|')[1:-1]]
                    writer.writerow(row)
            logger.info(f"Saved extracted structured data to {csv_path}")
        except Exception as e:
            logger.error(f"Failed to save CSV {filename}: {e}")

    def _extract_markdown_tables(self, markdown_text: str, filename_prefix: str):
        """Finds markdown tables in text and saves them as CSV files."""
        if not self.db_path:
            return
            
        structured_dir = os.path.join(self.db_path, "structured")
        os.makedirs(structured_dir, exist_ok=True)
        
        lines = markdown_text.split('\n')
        current_table = []
        table_count = 0
        
        for line in lines:
            if line.strip().startswith('|') and line.strip().endswith('|'):
                current_table.append(line.strip())
            elif current_table:
                # We reached the end of a table
                self._save_markdown_table_to_csv(current_table, os.path.join(structured_dir, f"{filename_prefix}_table_{table_count}.csv"))
                table_count += 1
                current_table = []
                
        # Handle table at the end of the file
        if current_table:
            self._save_markdown_table_to_csv(current_table, os.path.join(structured_dir, f"{filename_prefix}_table_{table_count}.csv"))

    def _save_markdown_table_to_csv(self, table_lines: List[str], csv_path: str):
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                for line in table_lines:
                    if set(line.replace('|', '').replace('-', '').replace(':', '').strip()) == set():
                        continue # Skip separator line
                    row = [cell.strip() for cell in line.split('|')[1:-1]]
                    writer.writerow(row)
            logger.info(f"Saved extracted structured data to {csv_path}")
        except Exception as e:
            logger.error(f"Failed to save CSV {csv_path}: {e}")

    def _process_marker_images(self, doc: Document) -> Document:
        """Processes images returned by Marker using the VLM."""
        images_dict = doc.metadata.get("images", {})
        if not images_dict:
            return doc
            
        content = doc.page_content
        import re
        import io
        
        # Find all image references in markdown, e.g. ![...](.../filename.png)
        img_pattern = re.compile(r'!\[.*?\]\((.*?([^/]+?\.(?:png|jpg|jpeg|webp)))\)', re.IGNORECASE)
        
        def replace_image(match):
            full_path = match.group(1)
            filename = match.group(2)
            
            # Find the base64 image
            b64_data = None
            for key, val in images_dict.items():
                if filename in key or key in filename:
                    b64_data = val
                    break
                    
            if not b64_data:
                return match.group(0)
                
            try:
                img_bytes = base64.b64decode(b64_data)
                pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                
                # Context heuristic based on surrounding text could be added here, 
                # but for simplicity we assume 'drawings' unless 'table'/'graph' is nearby
                # We can grab 500 chars before the image
                start_idx = max(0, match.start() - 500)
                context_text = content[start_idx:match.start()].lower()
                
                obj_type = "drawings"
                if "figure" in context_text or "fig." in context_text:
                    if "plot" in context_text or "curve" in context_text or "graph" in context_text:
                        obj_type = "graphs"
                elif "table" in context_text:
                    obj_type = "tables"
                    
                logger.info(f"Marker VLM processing image {filename} as {obj_type}")
                
                if obj_type == "graphs":
                    desc = self._process_graph(pil_img, 1, filename)
                elif obj_type == "tables":
                    desc = self._process_table(pil_img, 1, filename)
                else:
                    desc = self._process_drawing(pil_img)
                    
                return f"\n\n--- Start of Extracted {obj_type.capitalize()} ({filename}) ---\n{desc}\n--- End of Extracted {obj_type.capitalize()} ---\n\n"
            except Exception as e:
                logger.error(f"Failed to process Marker image {filename}: {e}")
                return match.group(0)
                
        new_content = img_pattern.sub(replace_image, content)
        doc.page_content = new_content
        return doc

    def parse_pdf(self, file_path: str) -> List[Document]:
        """
        Parses a PDF, extracting text and images/tables in reading order.
        Replaces complex objects with VLM-generated semantic descriptions.
        Returns a list of LangChain Documents (one per page or major chunk).
        """
        logger.info(f"Starting Scientific RAG parsing for {file_path} using {self.parser_type}")
        
        documents = []
        
        if self.parser_type == "mistral_ocr":
            try:
                from core.mistral_parser import MistralOCRParser
                mistral_parser = MistralOCRParser()
                docs = mistral_parser.parse_pdf(file_path)
                if docs:
                    base_name = os.path.splitext(os.path.basename(file_path))[0]
                    self._extract_markdown_tables(docs[0].page_content, base_name)
                    return docs
                else:
                    logger.warning(f"Mistral OCR returned no documents for {file_path}. Falling back to pdfplumber.")
            except Exception as e:
                logger.error(f"Mistral OCR parser failed: {e}. Falling back to pdfplumber.")

        if self.parser_type in ["marker", "marker_vlm"]:
            try:
                from core.marker_parser import MarkerParser
                marker_parser = MarkerParser(db_path=self.db_path)
                docs = marker_parser.parse_pdf(file_path)
                if docs:
                    base_name = os.path.splitext(os.path.basename(file_path))[0]
                    self._extract_markdown_tables(docs[0].page_content, base_name)
                    
                    if self.parser_type == "marker_vlm":
                        logger.info(f"Processing Marker images with VLM for {file_path}")
                        docs[0] = self._process_marker_images(docs[0])
                        
                    return docs
                else:
                    logger.warning(f"Marker returned no documents for {file_path}. Falling back to pdfplumber.")
            except ImportError as e:
                logger.error(f"Could not import MarkerParser: {e}. Falling back to pdfplumber.")
            except Exception as e:
                logger.error(f"Marker parser failed: {e}. Falling back to pdfplumber.")

        try:
            with pdfplumber.open(file_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    page_num = i + 1
                    logger.info(f"Processing page {page_num}/{len(pdf.pages)}...")
                    
                    # 1. Extract raw text
                    text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                    enriched_text = text + "\n\n"
                    
                    # 2. Extract images/figures
                    if page.images:
                        logger.info(f"Found {len(page.images)} images on page {page_num}.")
                        for j, img_obj in enumerate(page.images):
                            try:
                                # Get bounding box of image
                                x0 = img_obj["x0"]
                                top = img_obj["top"]
                                x1 = img_obj["x1"]
                                bottom = img_obj["bottom"]
                                
                                # Crop the image from the page as a PIL Image
                                cropped = page.crop((x0, top, x1, bottom)).to_image(resolution=150).original
                                
                                obj_type = "drawings" # default
                                context_text = text.lower()
                                if "figure" in context_text or "fig." in context_text:
                                    if "plot" in context_text or "curve" in context_text or "graph" in context_text:
                                        obj_type = "graphs"
                                    else:
                                        obj_type = "drawings"
                                elif "table" in context_text:
                                    obj_type = "tables"
                                
                                logger.info(f"Classified object {j} on page {page_num} as {obj_type}")
                                
                                if obj_type == "graphs":
                                    desc = self._process_graph(cropped, page_num, j)
                                elif obj_type == "tables":
                                    desc = self._process_table(cropped, page_num, j)
                                else:
                                    desc = self._process_drawing(cropped)
                                    
                                enriched_text += f"\n\n--- Start of Extracted {obj_type.capitalize()} (Page {page_num}) ---\n"
                                enriched_text += desc
                                enriched_text += f"\n--- End of Extracted {obj_type.capitalize()} ---\n\n"
                                
                            except Exception as e:
                                logger.error(f"Failed to process image {j} on page {page_num}: {e}")
                    
                    # Create a LangChain document for the enriched page
                    if enriched_text.strip():
                        documents.append(
                            Document(
                                page_content=enriched_text,
                                metadata={"source": file_path, "page": page_num, "scientific_rag": True, "type": "full_page"}
                            )
                        )
                        
        except Exception as e:
            logger.error(f"Error parsing PDF with pdfplumber: {e}")
            
        return documents
