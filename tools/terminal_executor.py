import subprocess
import shlex
import logging
import re
from crewai_tools import tool # Using crewai_tools for the decorator
import os

# Configure logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# --- Security Constants ---
FORBIDDEN_COMMANDS = {
    # Destructive
    "rm", "mv", "dd", "mkfs", "format",
    # Privilege/System
    "sudo", "su", "chmod", "chown", "shutdown", "reboot", "systemctl",
    # Network/Reverse Shells
    "nc", "ncat", "curl", "wget", "ssh", "telnet",
    # Secondary Execution
    "bash", "sh", "zsh", "python", "python3", "perl", "ruby", "php", "node",
    # Package Managers
    "apt", "dpkg", "yum", "pacman", "pip", "npm", "yarn",
    # Other potentially dangerous commands
    "fdisk", "parted", "wipefs", "cp", # cp can be dangerous if used improperly
    "cat", "head", "tail", "less", "more", # Blocked for protected paths, but generally safe, not strictly forbidden
    "vi", "vim", "nano", "emacs", # Editors are blocked to prevent direct file modification outside file tools
    "sed", "awk", "grep", # Can be used to manipulate or extract sensitive data, blocked for protected paths
    "kill", "killall", # Process management
}

FORBIDDEN_OPERATORS = {
    "|", ">", "<", ">>", "<<", "&", ";", "$", "`", "||", "&&",
}

# Regex patterns for protected paths to prevent direct access to critical project files/directories
PROTECTED_PATHS_REGEX = re.compile(
    r"\b(\.sqlite|\.yaml|\.yml|\.env|/db/|/config/|/core/|/ui/|/tools/)\b",
    re.IGNORECASE
)

# --- Security Validation Layer ---
def validate_command(command_str: str) -> tuple[bool, str]:
    """
    Validates a shell command string against a blacklist of commands, operators,
    and protected file paths to ensure safe execution.

    Args:
        command_str: The command string to validate.

    Returns:
        A tuple (bool, str) where bool is True if safe, False otherwise,
        and str is a message indicating the validation result or error.
    """
    # 1. Operator Check
    for op in FORBIDDEN_OPERATORS:
        if op in command_str:
            return False, f"Security Error: Command contains forbidden shell operator '{op}' (chaining/redirection not allowed)."

    # 2. Path Protection Check (before shlex.split to catch complex path attacks)
    if PROTECTED_PATHS_REGEX.search(command_str):
        return False, "Security Error: Direct terminal access to database, configuration, or core project files is strictly prohibited."

    try:
        # 3. Parsing
        args = shlex.split(command_str)
    except ValueError as e:
        return False, f"Security Error: Invalid command string parsing: {e}"

    if not args:
        return False, "Security Error: Empty command provided."

    # 4. Command Extraction
    base_command = args[0].lower()

    # 5. Blacklist Check
    if base_command in FORBIDDEN_COMMANDS:
        return False, f"Security Error: Command '{base_command}' is blacklisted."
    
    # 6. Check for paths in arguments more granularly if needed (already covered by regex, but good for redundancy)
    for arg in args:
        if PROTECTED_PATHS_REGEX.search(arg):
            return False, "Security Error: Command arguments reference protected project paths."

    return True, "Safe"

# --- Core Execution Logic ---
@tool("Terminal Executor")
def execute_shell_command(command_str: str, timeout: int = 30) -> str:
    """
    Executes a safe, read-only or authorized local terminal command after strict validation.
    Chaining commands (e.g., '&&', '|') or redirection (e.g., '>') is blocked.
    Destructive commands (e.g., 'rm', 'sudo') and direct access to database,
    configuration, or core project files are strictly prohibited.

    Use this tool to check system status, read logs *outside* the project directory,
    or perform authorized local system checks.

    Input must be a single, simple command string, e.g., 'ls -l /tmp' or 'df -h'.

    Args:
        command_str: The shell command string to execute.
        timeout: Maximum time in seconds to wait for the command to complete.

    Returns:
        A string containing the command's exit code, standard output, and standard error,
        or an error message if validation fails or an exception occurs.
    """
    is_safe, validation_message = validate_command(command_str)

    if not is_safe:
        logger.warning(f"Terminal Command Rejected: {command_str} - Reason: {validation_message}")
        return validation_message

    logger.info(f"Executing secure command: {command_str}")

    try:
        args = shlex.split(command_str)
        
        # CRITICAL ARCHITECTURE RULE: shell=False to prevent shell injection
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,  # Decode stdout/stderr as text
            timeout=timeout,
            check=False, # Do not raise CalledProcessError for non-zero exit codes
            shell=False
        )

        output = (
            f"Exit Code: {result.returncode}\n"
            f"STDOUT:\n{result.stdout.strip()}\n"
            f"STDERR:\n{result.stderr.strip()}"
        )
        logger.info(f"Command '{command_str}' completed. Output:\n{output}")
        return output

    except subprocess.TimeoutExpired:
        logger.error(f"Command execution timed out after {timeout} seconds: {command_str}")
        return f"Error: Command execution timed out after {timeout} seconds."
    except FileNotFoundError:
        logger.error(f"Command not found on the system: {command_str}")
        return f"Error: Command '{args[0]}' not found on the system. Please check if the command is installed and in the system's PATH."
    except Exception as e:
        logger.error(f"An unexpected error occurred during command execution: {command_str} - Error: {e}")
        return f"Error: An unexpected exception occurred: {type(e).__name__} - {e}"

# Example of how to use this tool (for testing purposes, not part of the final module)
if __name__ == "__main__":
    print("--- Testing Terminal Executor ---")

    # Safe commands
    print("\n--- Safe Commands ---")
    print(execute_shell_command("ls -la"))
    print(execute_shell_command("echo Hello World"))
    print(execute_shell_command("pwd"))
    print(execute_shell_command("date"))
    print(execute_shell_command("hostname"))

    # Blacklisted commands
    print("\n--- Blacklisted Commands ---")
    print(execute_shell_command("rm -rf /"))
    print(execute_shell_command("sudo apt update"))
    print(execute_shell_command("python -c 'import os; os.system(\"ls\")'"))
    print(execute_shell_command("bash -c 'echo test'"))

    # Forbidden operators
    print("\n--- Forbidden Operators ---")
    print(execute_shell_command("ls | grep a"))
    print(execute_shell_command("echo hello > file.txt"))
    print(execute_shell_command("ls && pwd"))

    # Protected paths
    print("\n--- Protected Paths ---")
    print(execute_shell_command("ls /db"))
    print(execute_shell_command("cat /config/test.yaml"))
    print(execute_shell_command("find . -name database.sqlite"))
    print(execute_shell_command("echo $HOME/.env"))
    print(execute_shell_command("ls -la /core/db_manager.py"))

    # Command not found
    print("\n--- Command Not Found ---")
    print(execute_shell_command("nonexistentcommand"))

    # Timeout test (uncomment to test, might hang for 5 seconds)
    # print("\n--- Timeout Test ---")
    # print(execute_shell_command("sleep 5", timeout=2))