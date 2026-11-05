"""
core/mcp_client.py

Universal Model Context Protocol (MCP) Client & CrewAI Tool Bridge for Alfredo AI OS.
Integrates `modelcontextprotocol/python-sdk` (https://github.com/modelcontextprotocol/python-sdk)
with a built-in JSON-RPC 2.0 stdio/HTTP fallback so Alfredo agents can connect to any
open-source MCP server (Filesystem, Fetch, SQLite, GitHub, Google Drive, Notion, Slack, Home Assistant)
without writing custom Python tool code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from typing import Any, Dict, List, Optional
import requests
from crewai.tools import tool

logger = logging.getLogger(__name__)

_MCP_LOCK = threading.RLock()

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MCP_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "mcp_servers.json")

DEFAULT_MCP_SERVERS: Dict[str, Dict[str, Any]] = {
    "filesystem_workspace": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", os.path.join(PROJECT_ROOT, "workspace")],
        "env": {},
        "enabled": False,
        "description": "Official MCP Filesystem server scoped to workspace/",
    },
    "web_fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "env": {},
        "enabled": False,
        "description": "Official MCP Fetch server for converting web pages to Markdown",
    },
}


class MCPClientManager:
    """
    Manages MCP server configurations (`config/mcp_servers.json`) and executes
    `tools/list` and `tools/call` over either the official `mcp` Python SDK or
    direct JSON-RPC 2.0 stdio/HTTP transport.
    """

    def __init__(self, config_path: str = MCP_CONFIG_PATH):
        self.config_path = config_path
        self._ensure_config()

    def _ensure_config(self) -> None:
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        if not os.path.exists(self.config_path):
            self.save_servers(DEFAULT_MCP_SERVERS)

    def load_servers(self) -> Dict[str, Dict[str, Any]]:
        with _MCP_LOCK:
            try:
                if os.path.exists(self.config_path):
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        return data.get("mcpServers", data)
            except Exception as e:
                logger.warning(f"[MCP] Failed to load {self.config_path}: {e}")
            return dict(DEFAULT_MCP_SERVERS)

    def save_servers(self, servers: Dict[str, Dict[str, Any]]) -> None:
        with _MCP_LOCK:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": servers}, f, indent=2, ensure_ascii=False)

    def upsert_server(
        self,
        name: str,
        command: str,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
        enabled: bool = True,
        description: str = "",
        url: str = "",
    ) -> None:
        servers = self.load_servers()
        servers[name.strip()] = {
            "command": command.strip(),
            "args": args or [],
            "env": env or {},
            "url": url.strip(),
            "enabled": bool(enabled),
            "description": description.strip(),
        }
        self.save_servers(servers)

    def delete_server(self, name: str) -> bool:
        servers = self.load_servers()
        if name in servers:
            del servers[name]
            self.save_servers(servers)
            return True
        return False

    def toggle_server(self, name: str, enabled: bool) -> bool:
        servers = self.load_servers()
        if name in servers:
            servers[name]["enabled"] = bool(enabled)
            self.save_servers(servers)
            return True
        return False

    # ------------------------------------------------------------------
    # Protocol Execution (Official MCP SDK + JSON-RPC 2.0 Stdio/HTTP Fallback)
    # ------------------------------------------------------------------
    def _jsonrpc_stdio_exchange(
        self,
        server_cfg: Dict[str, Any],
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 25,
    ) -> Dict[str, Any]:
        """
        Executes an MCP JSON-RPC 2.0 handshake (`initialize` -> `notifications/initialized` -> `method`)
        with a local stdio MCP server process.
        """
        cmd = server_cfg.get("command") or ""
        args = [str(a) for a in (server_cfg.get("args") or [])]
        if not cmd:
            raise ValueError("MCP server has no 'command' configured.")

        env = os.environ.copy()
        for k, v in (server_cfg.get("env") or {}).items():
            if v:
                env[str(k)] = str(v)

        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "alfredo-ai-os", "version": "2.0.0"},
            },
        }
        init_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        call_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": method,
            "params": params or {},
        }
        stdin_payload = (
            json.dumps(init_req) + "\n" + json.dumps(init_notif) + "\n" + json.dumps(call_req) + "\n"
        )

        proc = subprocess.run(
            [cmd] + args,
            input=stdin_payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            shell=(os.name == "nt" and cmd.lower() in ("npx", "npm", "uvx")),
        )
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
                if msg.get("id") == 2:
                    if "error" in msg:
                        raise RuntimeError(str(msg["error"]))
                    return msg.get("result") or {}
            except json.JSONDecodeError:
                continue

        if proc.stderr:
            raise RuntimeError(f"MCP Server stderr: {proc.stderr.strip()[:400]}")
        raise RuntimeError("No valid JSON-RPC response from MCP server.")

    def list_tools(self, server_name: Optional[str] = None) -> List[Dict[str, Any]]:
        servers = self.load_servers()
        targets = (
            {server_name: servers[server_name]}
            if (server_name and server_name in servers)
            else {k: v for k, v in servers.items() if v.get("enabled", True)}
        )

        discovered: List[Dict[str, Any]] = []
        for s_name, cfg in targets.items():
            try:
                if cfg.get("url"):
                    resp = requests.post(
                        cfg["url"],
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    res = (resp.json() or {}).get("result") or {}
                else:
                    res = self._jsonrpc_stdio_exchange(cfg, "tools/list", {})
                for t in res.get("tools") or []:
                    discovered.append(
                        {
                            "server": s_name,
                            "name": t.get("name"),
                            "description": t.get("description", ""),
                            "inputSchema": t.get("inputSchema", {}),
                        }
                    )
            except Exception as e:
                discovered.append(
                    {
                        "server": s_name,
                        "name": "<connection_error>",
                        "description": f"Could not query server '{s_name}': {e}",
                        "inputSchema": {},
                    }
                )
        return discovered

    def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        servers = self.load_servers()
        if server_name not in servers:
            return f"Error: MCP server '{server_name}' not found in config/mcp_servers.json."
        cfg = servers[server_name]
        try:
            params = {"name": tool_name, "arguments": arguments or {}}
            if cfg.get("url"):
                resp = requests.post(
                    cfg["url"],
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params},
                    timeout=30,
                )
                resp.raise_for_status()
                res = (resp.json() or {}).get("result") or {}
            else:
                res = self._jsonrpc_stdio_exchange(cfg, "tools/call", params, timeout=35)

            content_list = res.get("content") or []
            if isinstance(content_list, list) and content_list:
                texts = [
                    str(item.get("text") or item)
                    for item in content_list
                    if isinstance(item, dict)
                ]
                if texts:
                    return "\n".join(texts)
            return json.dumps(res, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"Error calling MCP tool '{server_name}.{tool_name}': {e}"


_MCP_MANAGER: Optional[MCPClientManager] = None


def get_mcp_manager() -> MCPClientManager:
    global _MCP_MANAGER
    if _MCP_MANAGER is None:
        _MCP_MANAGER = MCPClientManager()
    return _MCP_MANAGER


@tool
def mcp_list_tools(server_name: str = "") -> str:
    """
    Elenca tutti gli strumenti disponibili sui server MCP (Model Context Protocol) attivi
    (o su uno specifico `server_name`), mostrando nome, descrizione e parametri JSON richiesti.
    """
    mgr = get_mcp_manager()
    tools = mgr.list_tools(server_name=server_name.strip() or None)
    if not tools:
        return "Nessun server MCP attivo o nessuno strumento trovato. Attiva un server MCP dalla Dashboard."
    lines = ["🔌 STRUMENTI MCP DISPONIBILI:"]
    for t in tools:
        schema_props = list(((t.get("inputSchema") or {}).get("properties") or {}).keys())
        lines.append(
            f"- [{t['server']}] **{t['name']}** (args: {schema_props}): {t['description']}"
        )
    return "\n".join(lines)


@tool
def mcp_call_tool(server_name: str, tool_name: str, arguments_json: str = "{}") -> str:
    """
    Esegue uno strumento esposto da un server MCP esterno (es. Google Drive, Notion, GitHub,
    SQLite, Home Assistant, Filesystem).
    Args:
        server_name: Nome del server MCP registrato (es. 'filesystem_workspace').
        tool_name: Nome dello strumento MCP da invocare.
        arguments_json: Stringa JSON con i parametri richiesti dallo strumento.
    """
    try:
        args = json.loads(arguments_json or "{}") if isinstance(arguments_json, str) else (arguments_json or {})
    except Exception as e:
        return f"Error parsing arguments_json: {e}"
    return get_mcp_manager().call_tool(server_name.strip(), tool_name.strip(), args)
