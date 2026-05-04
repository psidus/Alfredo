# core/data_manager.py

import os
import yaml
from dotenv import load_dotenv, find_dotenv
import logging
import tempfile
import shutil

# --- Architect's Note ---
# Set up a logger for this module to provide visibility into file operations,
# which is crucial for debugging in a system with multiple processes.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class DataManager:
    """
    Manages loading and saving of configuration data.
    Supports .env files for environment variables and YAML files for configurations and exports.
    This class is designed to be stateless (using static methods) for safe concurrent use
    by different system components (e.g., Streamlit UI, Telegram Bot).
    """

    @staticmethod
    def load_env():
        """
        Loads the .env file into the environment.
        """
        env_path = find_dotenv()
        if env_path:
            load_dotenv(dotenv_path=env_path)
            return True
        return False

    @staticmethod
    def load_api_key(api_name: str) -> str | None:
        """
        Loads a specific API key from the .env file.
        Looks for a .env file in the project root and loads it into the environment.
        
        Args:
            api_name (str): The name of the environment variable (e.g., 'OPENAI_API_KEY').
            
        Returns:
            str | None: The value of the API key or None if not found.
        """
        # --- Architect's Note ---
        # Using find_dotenv() makes the location of the .env file independent of the
        # script's working directory. This is robust. `load_dotenv` will only load
        # variables that are not already present in the environment, which is safe.
        env_path = find_dotenv()
        if not env_path:
            logging.warning(".env file not found. API keys will not be loaded.")
            return None
            
        load_dotenv(dotenv_path=env_path)
        
        api_key = os.getenv(api_name)
        if not api_key:
            logging.warning(f"'{api_name}' not found in the environment or .env file.")
        
        return api_key

    get_api_key = load_api_key

    @staticmethod
    def load_yaml(file_path: str) -> dict:
        """
        Safely loads data from a specified YAML file.
        
        Args:
            file_path (str): The full path to the YAML file.
            
        Returns:
            dict: The data loaded from the YAML file. Returns an empty dict if the file
                  is not found or is malformed.
        """
        # --- Architect's Note ---
        # The file existence check and `try...except` block are critical for resilience.
        # The system must not crash due to a missing or corrupted config file.
        # Most importantly, using yaml.safe_load() is a security requirement to prevent
        # arbitrary code execution from a compromised YAML file.
        if not os.path.exists(file_path):
            logging.warning(f"YAML file not found at: {file_path}. Returning empty dictionary.")
            return {}
            
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            logging.error(f"Error parsing YAML file {file_path}: {e}. Returning empty dictionary.")
            return {}
        except Exception as e:
            logging.error(f"An unexpected error occurred while reading {file_path}: {e}")
            return {}

    @staticmethod
    def save_yaml(file_path: str, data: dict) -> None:
        """
        Saves a dictionary to a specified YAML file using an atomic write operation
        to prevent file corruption.
        
        Args:
            file_path (str): The full path to the destination YAML file.
            data (dict): The dictionary to save.
        """
        # --- Architect's Note (CRITICAL) ---
        # This implements an atomic write. Writing directly to the destination file ('w' mode)
        # is dangerous. If the application crashes mid-write, the file will be corrupted
        # (empty or incomplete). By writing to a temporary file and then renaming it,
        # we guarantee that the destination file is only ever replaced by a complete,
        # valid new version. This prevents data loss.
        
        # 1. Ensure the target directory exists.
        dir_name = os.path.dirname(file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        # 2. Create a temporary file in the same directory to ensure atomic move is possible.
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile('w', delete=False, dir=dir_name, encoding='utf-8', suffix='.tmp') as temp_f:
                yaml.dump(data, temp_f, default_flow_style=False, sort_keys=False)
                temp_path = temp_f.name
            
            # 3. If write is successful, atomically move/rename the temp file to the final destination.
            shutil.move(temp_path, file_path)
            logging.info(f"Successfully saved data to {file_path}")

        except Exception as e:
            logging.error(f"Failed to save data to {file_path}: {e}")
            # Clean up the temporary file if it still exists after an error
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

# Export for module-level access
load_env = DataManager.load_env
get_api_key = DataManager.load_api_key