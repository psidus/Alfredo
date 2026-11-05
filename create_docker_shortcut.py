import os
import subprocess
import sys

def create_docker_shortcut():
    # 1. Configuration
    project_dir = os.path.dirname(os.path.abspath(__file__))
    bat_path = os.path.join(project_dir, "run_docker_alfredo.bat")
    icon_path = os.path.join(project_dir, "ui", "favicon.ico")
    
    if not os.path.exists(bat_path):
        print(f"Error: {bat_path} not found. Make sure you have downloaded the entire project.")
        return

    if not os.path.exists(icon_path):
        print(f"Warning: {icon_path} not found. Using fallback icon.")
        icon_path = ""

    # Dynamically locate all relevant Desktop folders (handling OneDrive and local desktop)
    desktop_dirs = []
    try:
        main_desktop = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command', '[Environment]::GetFolderPath("Desktop")'], 
            text=True
        ).strip()
        if main_desktop and os.path.exists(main_desktop):
            desktop_dirs.append(main_desktop)
    except Exception:
        pass

    user_desktop = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
    if user_desktop and os.path.exists(user_desktop) and user_desktop not in desktop_dirs:
        desktop_dirs.append(user_desktop)

    if not desktop_dirs:
        desktop_dirs.append(os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop'))

    # 2. Create the Shortcut on each Desktop location via PowerShell
    for desktop in desktop_dirs:
        shortcut_path = os.path.join(desktop, "Alfredo (Docker).lnk")
        escaped_shortcut = shortcut_path.replace('"', '`"')
        escaped_bat = bat_path.replace('"', '`"')
        escaped_proj = project_dir.replace('"', '`"')
        escaped_icon = icon_path.replace('"', '`"')

        ps_script = f"""
        $WshShell = New-Object -ComObject WScript.Shell
        $Shortcut = $WshShell.CreateShortcut("{escaped_shortcut}")
        $Shortcut.TargetPath = "{escaped_bat}"
        $Shortcut.WorkingDirectory = "{escaped_proj}"
        $Shortcut.Description = "Start Alfredo AI OS in Docker"
        """
        if icon_path:
            ps_script += f'\n$Shortcut.IconLocation = "{escaped_icon},0"'
        ps_script += "\n$Shortcut.Save()"

        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True)
            print(f"Success! Desktop shortcut created at: {shortcut_path} with icon {icon_path}")
        except Exception as e:
            print(f"Error creating desktop shortcut at {shortcut_path}: {e}")

if __name__ == "__main__":
    create_docker_shortcut()
