import os
import sys
import subprocess
import logging
import json
import tempfile
import requests
from pathlib import Path
from typing import List

from langchain_core.documents import Document

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Add file handler to capture logs
if not logger.handlers:
    os.makedirs("storage", exist_ok=True)
    fh = logging.FileHandler("storage/app_debug.log")
    fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(fh)

class MarkerParser:
    def __init__(self, db_path: str = None):
        self.db_path = db_path
        self.base_dir = Path(__file__).resolve().parent.parent
        self.venv_dir = self.base_dir / "venv_rag"
        
        if os.name == 'nt':
            self.marker_exe = self.venv_dir / "Scripts" / "marker_single.exe"
        else:
            self.marker_exe = self.venv_dir / "bin" / "marker_single"

        self.api_url = None
        # Try to find a running API (Docker host or Localhost)
        for host in ["host.docker.internal", "127.0.0.1"]:
            try:
                url = f"http://{host}:8001"
                resp = requests.get(f"{url}/health", timeout=1.0)
                if resp.status_code == 200:
                    self.api_url = url
                    break
            except Exception:
                pass

    def is_available(self) -> bool:
        if self.api_url is not None:
            return True
        return self.marker_exe.exists()

    def parse_pdf(self, file_path: str) -> List[Document]:
        if not self.is_available():
            logger.error(f"Marker executable not found at {self.marker_exe} and API is not reachable.")
            return []

        logger.info(f"Starting Marker GPU parsing for {file_path}")
        documents = []

        # 1. Try API execution (if running)
        if self.api_url:
            logger.info(f"Using Marker API at {self.api_url}")
            try:
                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f, "application/pdf")}
                    resp = requests.post(f"{self.api_url}/parse_pdf", files=files, timeout=(10, 7200))
                
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("markdown", "")
                    if content.strip():
                        images = data.get("images", {})
                        documents.append(
                            Document(
                                page_content=content,
                                metadata={
                                    "source": file_path, 
                                    "scientific_rag": True, 
                                    "parser": "marker_gpu_api",
                                    "images": images
                                }
                            )
                        )
                        return documents
                else:
                    logger.error(f"Marker API returned error {resp.status_code}: {resp.text}")
            except Exception as e:
                logger.error(f"Error calling Marker API: {e}")

        # 2. Fallback to Local Subprocess execution (if running locally on Windows)
        if not self.marker_exe.exists():
            logger.error("API failed and local Marker executable not found.")
            return []

        logger.info("Using local Marker executable.")
        with tempfile.TemporaryDirectory() as temp_dir:
            cmd = f"\"{self.marker_exe}\" \"{file_path}\" --output_dir \"{temp_dir}\""
            logger.info(f"Running command: {cmd}")
            try:
                result = subprocess.run(cmd, shell=True, check=True, text=True, capture_output=True)
                logger.info("Marker process finished successfully.")
            except subprocess.CalledProcessError as e:
                logger.error(f"Marker parsing failed with exit code {e.returncode}: {e.stderr}")
                return []

            base_name = os.path.splitext(os.path.basename(file_path))[0]
            output_folder = os.path.join(temp_dir, base_name)
            
            if not os.path.exists(output_folder):
                for root, dirs, files in os.walk(temp_dir):
                    if any(f.endswith(".md") for f in files):
                        output_folder = root
                        break

            if not os.path.exists(output_folder):
                logger.error(f"Marker output folder not found after processing {file_path}")
                return []

            md_files = [f for f in os.listdir(output_folder) if f.endswith(".md")]
            if not md_files:
                logger.error(f"No markdown files found in {output_folder}")
                return []
                
            md_path = os.path.join(output_folder, md_files[0])
            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    
                if content.strip():
                    import base64
                    images = {}
                    for root_dir, _, files_list in os.walk(output_folder):
                        for img_file in files_list:
                            if img_file.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                                img_path = os.path.join(root_dir, img_file)
                                try:
                                    with open(img_path, "rb") as img_f:
                                        images[img_file] = base64.b64encode(img_f.read()).decode('utf-8')
                                except Exception as e:
                                    logger.error(f"Failed to read image {img_file}: {e}")
                                
                    documents.append(
                        Document(
                            page_content=content,
                            metadata={
                                "source": file_path, 
                                "scientific_rag": True, 
                                "parser": "marker_gpu_local",
                                "images": images
                            }
                        )
                    )
            except Exception as e:
                logger.error(f"Error reading Marker output markdown: {e}")

        return documents
