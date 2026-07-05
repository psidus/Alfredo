import os
import sys
import subprocess
import logging
import json
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def get_base_dir():
    # The root of the Alfredo repository
    return Path(__file__).resolve().parent.parent

def run_command(command, cwd=None, error_msg="Command failed"):
    logging.info(f"Running: {command}")
    try:
        result = subprocess.run(command, cwd=cwd, shell=True, check=True, text=True, capture_output=True)
        logging.info(f"Output: {result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"{error_msg}. Exit code: {e.returncode}")
        logging.error(f"Error output: {e.stderr}")
        return False

def setup_venv_and_marker():
    base_dir = get_base_dir()
    venv_dir = base_dir / "venv_rag"
    
    logging.info("1. Creating virtual environment 'venv_rag'...")
    if not venv_dir.exists():
        success = run_command(f"\"{sys.executable}\" -m venv \"{venv_dir}\"", cwd=base_dir, error_msg="Failed to create venv")
        if not success:
            return False
    else:
        logging.info("'venv_rag' already exists.")

    # Determine pip executable inside venv
    if os.name == 'nt':
        pip_exe = venv_dir / "Scripts" / "pip.exe"
    else:
        pip_exe = venv_dir / "bin" / "pip"

    if not pip_exe.exists():
        logging.error("Pip executable not found in venv.")
        return False

    logging.info("2. Installing PyTorch with CUDA 12.8 nightly...")
    # Uninstall existing torch just in case, as per user's batch script
    run_command(f"\"{pip_exe}\" uninstall torch torchvision torchaudio -y", cwd=base_dir, error_msg="Failed to uninstall torch")
    
    torch_cmd = f"\"{pip_exe}\" install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128"
    if not run_command(torch_cmd, cwd=base_dir, error_msg="Failed to install PyTorch Nightly"):
        return False

    logging.info("3. Installing Marker-PDF and API dependencies...")
    if not run_command(f"\"{pip_exe}\" install marker-pdf fastapi uvicorn python-multipart", cwd=base_dir, error_msg="Failed to install Marker-PDF and API dependencies"):
        return False
        
    logging.info("3.1 Generating start_marker_gpu.bat...")
    bat_content = f"""@echo off
echo Starting Marker GPU API Server...
call "{venv_dir}\\Scripts\\activate.bat"
python tools\\marker_api_server.py
pause
"""
    bat_path = base_dir / "start_marker_gpu.bat"
    try:
        with open(bat_path, "w") as f:
            f.write(bat_content)
        logging.info("start_marker_gpu.bat generated successfully.")
    except Exception as e:
        logging.error(f"Failed to generate start_marker_gpu.bat: {e}")
        return False

    return True

def setup_docker_qdrant():
    logging.info("4. Setting up Qdrant Docker container...")
    # Check if docker is installed
    try:
        subprocess.run("docker --version", shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        logging.warning("Docker is not installed or not running. Qdrant will run in local-disk mode.")
        return True # Fallback to local disk mode is acceptable

    # Check if container exists
    result = subprocess.run("docker ps -a --format \"{{.Names}}\"", shell=True, capture_output=True, text=True)
    if "qdrant_server" in result.stdout:
        logging.info("Container 'qdrant_server' already exists. Starting it...")
        run_command("docker start qdrant_server", error_msg="Failed to start qdrant_server")
    else:
        logging.info("Creating and running new 'qdrant_server' container...")
        cmd = "docker run -d -p 6333:6333 -p 6334:6334 --name qdrant_server -v qdrant_storage:/qdrant/storage --restart unless-stopped qdrant/qdrant"
        if not run_command(cmd, error_msg="Failed to create Qdrant container"):
            logging.warning("Failed to setup Qdrant container. It will run in local-disk mode.")
            return True

    # Attempt to open Firewall port on Windows
    if os.name == 'nt':
        logging.info("5. Configuring Windows Firewall for Qdrant (port 6333)...")
        ps_cmd = "powershell -Command \"New-NetFirewallRule -DisplayName 'Docker Qdrant 6333' -Direction Inbound -Protocol TCP -LocalPort 6333 -Action Allow -Profile Private -ErrorAction SilentlyContinue\""
        run_command(ps_cmd, error_msg="Failed to configure firewall (Admin rights might be required, but continuing anyway)")

    return True

def main():
    print("Starting Advanced RAG Setup (Marker GPU + Qdrant)...")
    success_marker = setup_venv_and_marker()
    success_qdrant = setup_docker_qdrant()
    
    config_dir = get_base_dir() / "config"
    config_dir.mkdir(exist_ok=True)
    flag_file = config_dir / "advanced_rag_setup.json"
    
    if success_marker:
        with open(flag_file, "w") as f:
            json.dump({"setup_complete": True, "marker_ready": True}, f)
        print("Setup completed successfully.")
        sys.exit(0)
    else:
        print("Setup encountered errors.")
        sys.exit(1)

if __name__ == "__main__":
    main()
