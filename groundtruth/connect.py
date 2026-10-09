"""``./gt connect``: copy-paste setup for every common MCP client.

    ./gt connect                      # all clients, with your real paths filled in
    ./gt connect claude-desktop       # just one
    ./gt connect claude-desktop --install   # write it into the client's config (backs up first)
    ./gt connect copilot-studio --url https://mcp.example.org/mcp

Local clients launch the server over stdio, so there's nothing to keep running.
Hosted platforms (Copilot Studio, ChatGPT, claude.ai) need the HTTP server deployed
somewhere public: see docs/deploy.md.
"""
from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import sys
import time
from pathlib import Path

from groundtruth.project import REPO_ROOT, Project

CLIENTS = ["claude-desktop", "claude-code", "codex", "cursor", "vscode", "windsurf",
           "copilot-studio", "chatgpt", "claude-ai", "http"]


def python_exe() -> str:
    for candidate in (REPO_ROOT / ".venv" / "bin" / "python", REPO_ROOT / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def server_name(project: Project) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in project.name.lower()).strip("-")
    return (slug[:40] or "groundtruth").replace("--", "-")


def stdio_command(project: Project) -> list[str]:
    return [python_exe(), "-m", "groundtruth", "-p", str(project.root), "serve", "--stdio"]


def _json_entry(project: Project) -> dict:
    cmd = stdio_command(project)
    return {"command": cmd[0], "args": cmd[1:]}


def config_path(client: str) -> Path | None:
    home = Path.home()
    system = platform.system()
    if client == "claude-desktop":
        if system == "Darwin":
            return home / "Library/Application Support/Claude/claude_desktop_config.json"
        if system == "Windows":
            return Path(os.environ.get("APPDATA", home)) / "Claude/claude_desktop_config.json"
        return home / ".config/Claude/claude_desktop_config.json"
    if client == "cursor":
        return home / ".cursor/mcp.json"
    if client == "windsurf":
        return home / ".codeium/windsurf/mcp_config.json"
    return None


def snippet(client: str, project: Project, url: str | None = None) -> str:
    name = server_name(project)
    cmd = stdio_command(project)
    shell_cmd = " ".join(shlex.quote(c) for c in cmd)
    http_url = url or "https://YOUR-HOST/mcp"
    entry = json.dumps({"mcpServers": {name: _json_entry(project)}}, indent=2)

    if client == "claude-desktop":
        return (f"Add to {config_path('claude-desktop')} (or run with --install), then restart Claude Desktop:\n\n"
                f"{entry}")
    if client == "claude-code":
        return (f"Run once (add --scope project to share it with your repo via .mcp.json):\n\n"
                f"claude mcp add {name} -- {shell_cmd}")
    if client == "codex":
        return f"Run once:\n\ncodex mcp add {name} -- {shell_cmd}"
    if client in ("cursor", "windsurf"):
        return f"Add to {config_path(client)} (or run with --install):\n\n{entry}"
    if client == "vscode":
        vs = json.dumps({"servers": {name: {"type": "stdio", **_json_entry(project)}}}, indent=2)
        return f"Add to .vscode/mcp.json in your workspace (Copilot agent mode picks it up):\n\n{vs}"
    if client == "copilot-studio":
        return ("Deploy the HTTP server first (docs/deploy.md), with GT_MCP_API_KEY set. Then in Copilot Studio,\n"
                "add a Model Context Protocol (MCP) tool/connector to your agent:\n"
                f"  Server URL:      {http_url}\n"
                "  Authentication:  API key sent as the header  Authorization: Bearer <GT_MCP_API_KEY>\n"
                "                   (x-api-key: <key> and x-ms-apikey: <key> also work)\n"
                "Paste prompts/system_prompt.md into the agent's instructions so the agent and your evals agree.")
    if client in ("chatgpt", "claude-ai"):
        where = ("In ChatGPT, add a custom connector (developer mode)"
                 if client == "chatgpt" else "In claude.ai, add a custom connector (Settings > Connectors)")
        return (f"{where}, pointing at:\n  {http_url}\n"
                "These connectors need a public HTTPS URL and support OAuth or no auth, not an API-key "
                "header. groundtruth serves public knowledge, so a common setup is: no GT_MCP_API_KEY, "
                "GT_ALLOWED_HOSTS=<your host>, behind HTTPS. Check the platform's current connector docs.")
    if client == "http":
        return (f"Streamable HTTP endpoint (start it with ./gt serve, or deploy it):\n  {url or 'http://127.0.0.1:8765/mcp'}\n"
                "Header (when GT_MCP_API_KEY is set): Authorization: Bearer <key>\n"
                "Health check: GET /healthz")
    raise ValueError(f"unknown client {client!r}; choose from {', '.join(CLIENTS)}")


def install(client: str, project: Project) -> str:
    path = config_path(client)
    if path is None:
        raise ValueError(f"--install supports claude-desktop, cursor, windsurf; for {client} run the printed command")
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} isn't valid JSON ({exc}); fix it or paste the snippet by hand") from exc
        backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    data.setdefault("mcpServers", {})[server_name(project)] = _json_entry(project)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return str(path)
