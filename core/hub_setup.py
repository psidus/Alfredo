"""
Helpers for local Alfredo hub setup (Docker, LAN URLs, onboarding snippets).
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple


def find_project_root() -> str:
    """Return repo root (directory containing docker-compose.yml)."""
    candidates = [os.getcwd()]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.dirname(here))
    for root in candidates:
        if os.path.isfile(os.path.join(root, "docker-compose.yml")):
            return root
    return os.getcwd()


def is_running_in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def get_lan_ip() -> Optional[str]:
    """Best LAN IPv4 for colleague onboarding (prefers 192.168/10, skips Docker bridges)."""
    override = (os.getenv("HUB_LAN_IP") or "").strip()
    if override:
        return override
    addrs = collect_ipv4_addresses()
    for ip in addrs:
        if ip.startswith(("192.168.", "10.")):
            return ip
    for ip in addrs:
        if not _is_docker_bridge_ip(ip):
            return ip
    return addrs[0] if addrs else None


def _is_docker_bridge_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 172 and 17 <= b <= 31


def collect_ipv4_addresses() -> List[str]:
    """Collect non-loopback IPv4 addresses for this host."""
    seen: set[str] = set()
    addrs: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in seen and not ip.startswith("127."):
                seen.add(ip)
                addrs.append(ip)
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip not in seen and not ip.startswith("127."):
            addrs.insert(0, ip)
    except OSError:
        pass
    return addrs


def hub_profile_from_mode(mode: str) -> str:
    mode = (mode or "off").strip().lower()
    if mode == "local":
        return "company"
    if mode == "remote":
        return "remote"
    return "files_only"


def hub_mode_from_profile(profile: str) -> str:
    if profile == "company":
        return "local"
    if profile == "remote":
        return "remote"
    return "off"


def docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
            timeout=20,
        )
        return True
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return False


def hub_container_status(project_root: Optional[str] = None) -> Dict[str, Any]:
    """Inspect hub Docker services."""
    root = project_root or find_project_root()
    out: Dict[str, Any] = {
        "docker_available": docker_available(),
        "hub_running": False,
        "postgres_running": False,
        "project_root": root,
    }
    if not out["docker_available"]:
        return out

    try:
        proc = subprocess.run(
            ["docker", "compose", "--profile", "hub", "ps", "--status", "running", "--services"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        running = {s.strip() for s in proc.stdout.splitlines() if s.strip()}
        out["hub_running"] = "hub" in running
        out["postgres_running"] = "hub_postgres" in running
        if proc.returncode != 0 and proc.stderr:
            out["error"] = proc.stderr.strip()
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)
    return out


def run_hub_compose(action: str, project_root: Optional[str] = None) -> Tuple[bool, str]:
    """
    Start or stop hub containers.

    action: ``up`` | ``stop``
    """
    root = project_root or find_project_root()
    if not docker_available():
        return False, "Docker is not available. Start Docker Desktop and retry."

    if action == "up":
        cmd = ["docker", "compose", "--profile", "hub", "up", "-d", "hub_postgres", "hub"]
    elif action == "stop":
        cmd = ["docker", "compose", "--profile", "hub", "stop", "hub", "hub_postgres"]
    else:
        return False, f"Unknown action: {action}"

    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=180)
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return False, output.strip() or f"docker compose failed (exit {proc.returncode})"
        return True, output.strip() or "OK"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def wait_for_hub_health(base_url: str, timeout_sec: float = 45.0) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Poll /hub/health until ok or timeout."""
    from core.hub_client import HubClient, HubClientError

    client = HubClient(base_url=base_url)
    deadline = time.time() + timeout_sec
    last_err = ""
    while time.time() < deadline:
        try:
            health = client.health()
            if health.get("status") == "ok":
                return True, health, ""
        except HubClientError as e:
            last_err = str(e)
        time.sleep(1.5)
    return False, None, last_err or "Hub did not become ready in time"


def generate_invite_token() -> str:
    return secrets.token_urlsafe(24)


def suggest_client_hub_url(*, lan_ip: Optional[str] = None, hostname: str = "") -> str:
    """
    Pick a sensible HUB_API_URL for this Alfredo instance.

    - Dashboard in Docker → host.docker.internal
    - Native Windows/Linux on server → localhost
    """
    hostname = (hostname or "").strip()
    if hostname:
        return f"http://{hostname}:8010"
    if is_running_in_docker():
        return "http://host.docker.internal:8010"
    return "http://localhost:8010"


def suggest_colleague_hub_url(*, lan_ip: Optional[str] = None, hostname: str = "") -> str:
    """URL colleagues on other PCs should use."""
    hostname = (hostname or "").strip()
    if hostname:
        return f"http://{hostname}:8010"
    ip = lan_ip or get_lan_ip()
    if ip:
        return f"http://{ip}:8010"
    return "http://<server-lan-ip>:8010"


def build_colleague_env_snippet(
    *,
    hub_url: str,
    org_slug: str,
    ollama_url: str = "",
) -> str:
    lines = [
        "HUB_MODE=local",
        f"HUB_API_URL={hub_url}",
        f"HUB_ORG={org_slug}",
        "HUB_USERNAME=<your-username>",
        "HUB_TOKEN=<from Connect & account → Register & connect>",
    ]
    if ollama_url:
        lines.append(f"OLLAMA_API_BASE={ollama_url}")
    return "\n".join(lines)


def build_hosts_snippet(*, lan_ip: str, hostname: str) -> str:
    return f"{lan_ip}  {hostname}"


def parse_compose_ps_json(raw: str) -> List[Dict[str, Any]]:
    """Parse newline-delimited JSON from docker compose ps --format json."""
    items: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items
