import os
import subprocess
import sys

def create_docker_shortcut():
    # 1. Configuration
    project_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Dynamically locate the Desktop folder (handling OneDrive if present)
    try:
        desktop = subprocess.check_output(['powershell', '-Command', '[Environment]::GetFolderPath("Desktop")'], text=True).strip()
    except:
        desktop = os.path.join(os.environ['USERPROFILE'], 'Desktop')
        
    shortcut_path = os.path.join(desktop, "Alfredo (Docker).lnk")
    bat_path = os.path.join(project_dir, "run_docker_alfredo.bat")
    
    if not os.path.exists(bat_path):
        print(f"Error: {bat_path} not found. Make sure you have downloaded the entire project.")
        return

    # 2. Create the Shortcut on Desktop via PowerShell
    icon_path = os.path.join(project_dir, "ui", "favicon.ico")
    if not os.path.exists(icon_path):
        icon_path = "" # Fallback to default system icon if icon doesn't exist

    ps_script = f"""
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
    $Shortcut.TargetPath = "{bat_path}"
    $Shortcut.WorkingDirectory = "{project_dir}"
    $Shortcut.Description = "Start Alfredo AI OS in Docker"
    """
    
    if icon_path:
        ps_script += f'\n$Shortcut.IconLocation = "{icon_path}"'
        
    ps_script += "\n$Shortcut.Save()"

    try:
        subprocess.run(["powershell", "-Command", ps_script], check=True)
        print(f"Success! Desktop shortcut created at: {shortcut_path}")
    except Exception as e:
        print(f"Error creating desktop shortcut: {e}")

if __name__ == "__main__":
    create_docker_shortcut()
