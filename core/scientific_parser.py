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
        buffered = BytesIO()
        pil_img.save(buffered, format="PNG")
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
                            "url": f"data:image/png;base64,{base64_img}"
                        }
                    }
                ]
            }
        ]

        try:
            # LiteLLM automatically routes to the right provider based on the model name prefix
            response = completion(
                model=model,
                messages=messages,
                temperature=0.0
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Error calling VLM '{model}' for role '{role}': {e}")
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
            "into a clean, well-formatted Markdown table. Ensure all headers and data cells are accurate. "
            "Do not include any extra conversational text, just the Markdown table."
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

    def parse_pdf(self, file_path: str) -> List[Document]:
        """
        Parses a PDF, extracting text and images/tables in reading order.
        Replaces complex objects with VLM-generated semantic descriptions.
        Returns a list of LangChain Documents (one per page or major chunk).
        """
        logger.info(f"Starting Scientific RAG parsing for {file_path} using {self.parser_type}")
        
        documents = []
        skip_text_extraction = False
        
        if self.parser_type in ["marker", "marker_vlm"]:
            try:
                from core.marker_parser import MarkerParser
                marker_parser = MarkerParser(db_path=self.db_path)
                docs = marker_parser.parse_pdf(file_path)
                if docs:
                    if self.parser_type == "marker":
                        return docs
                    else:
                        documents.extend(docs)
                        skip_text_extraction = True
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
                    
                    if skip_text_extraction:
                        enriched_text = "" # Text already extracted by Marker
                    else:
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
                                metadata={"source": file_path, "page": page_num, "scientific_rag": True, "type": "vlm_extraction" if skip_text_extraction else "full_page"}
                            )
                        )
                        
        except Exception as e:
            logger.error(f"Error parsing PDF with pdfplumber: {e}")
            
        return documents
