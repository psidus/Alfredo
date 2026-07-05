import os
import sys
import subprocess
import tempfile
import logging
import uvicorn
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Marker GPU API Server", version="1.0")

# Identify the marker executable
base_dir = Path(__file__).resolve().parent.parent
venv_dir = base_dir / "venv_rag"

if os.name == 'nt':
    marker_exe = venv_dir / "Scripts" / "marker_single.exe"
else:
    marker_exe = venv_dir / "bin" / "marker_single"

@app.get("/health")
def health_check():
    return {"status": "ok", "marker_available": marker_exe.exists()}

@app.post("/parse_pdf")
async def parse_pdf(file: UploadFile = File(...)):
    """
    Receives a PDF file, processes it with Marker GPU, and returns the Markdown content.
    """
    if not marker_exe.exists():
        raise HTTPException(status_code=500, detail="Marker executable not found. Make sure setup_advanced_rag.py was run.")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_pdf_path = os.path.join(temp_dir, "temp.pdf")
        with open(temp_pdf_path, "wb") as f:
            content = await file.read()
            f.write(content)

        output_dir = os.path.join(temp_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        
        cmd = [
            str(marker_exe),
            temp_pdf_path,
            "--output_dir", output_dir,
            "--output_format", "markdown",
            "--batch_multiplier", "2"
        ]
        
        logger.info(f"Running marker: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Marker command failed: {e.stderr}")
            raise HTTPException(status_code=500, detail=f"Marker command failed: {e.stderr}")
            
        # marker creates a folder with the same name as the pdf (temp)
        result_dir = os.path.join(output_dir, "temp")
        md_file = os.path.join(result_dir, "temp.md")
        
        if not os.path.exists(md_file):
            logger.error(f"Marker did not produce output at {md_file}")
            raise HTTPException(status_code=500, detail="Marker did not produce expected output.")
            
        with open(md_file, "r", encoding="utf-8") as f:
            markdown_text = f.read()
            
        return JSONResponse(content={"markdown": markdown_text})

if __name__ == "__main__":
    logger.info(f"Starting Marker API Server on port 8001...")
    logger.info(f"Marker Executable Path: {marker_exe}")
    uvicorn.run("marker_api_server:app", host="0.0.0.0", port=8001, reload=False)
