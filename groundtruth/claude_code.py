"""Live evals through Claude Code, for setups with a Claude plan and no API key.

``./gt eval --tier live`` normally asks Claude through the API (llm.py), which
needs an ANTHROPIC_API_KEY. A refresh host that runs on a Claude plan
(CLAUDE_CODE_OAUTH_TOKEN) would then never grade the live model. This runs the
same questions through headless Claude Code instead. It's connected to this
project's MCP server over stdio, with the server's own instructions as the system
prompt and no other tools, so it reaches the facts exactly the way an MCP client
does. The tool calls and results come back from its stream, in the shape the
graders expect, and the narrative judge runs the same way.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from groundtruth.project import Project

PREFIX = "mcp__groundtruth__"
TIMEOUT_SECONDS = 600


def cli() -> str | None:
    return os.environ.get("GT_CLAUDE") or shutil.which("claude")


def available() -> bool:
    """The claude CLI, and a Claude login it can use unattended."""
    return bool(cli()) and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
                                or (Path.home() / ".claude").exists())


def _env() -> dict[str, str]:
    from groundtruth import rewrite
    return rewrite.claude_env()


def _run(args: list[str], prompt: str, cwd: str) -> list[dict]:
    out = subprocess.run([cli() or "claude", "-p", *args, "--output-format", "stream-json", "--verbose",
                          "--no-session-persistence", "--permission-mode", "dontAsk"],
                         input=prompt, text=True, capture_output=True, cwd=cwd, env=_env(),
                         timeout=TIMEOUT_SECONDS, check=False)
    events = []
    for line in out.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _payload(content) -> object:
    """A tool result's content (text blocks of JSON) as the object the tool returned."""
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


def parse(events: list[dict]) -> dict:
    """Claude Code's stream -> {"response", "tool_calls": [{"name", "input", "result"}], "stop_reason"}."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    response, stop = "", "error"
    for event in events:
        message = event.get("message") or {}
        for block in message.get("content") or [] if isinstance(message.get("content"), list) else []:
            if block.get("type") == "tool_use" and str(block.get("name", "")).startswith(PREFIX):
                calls[block["id"]] = {"name": block["name"][len(PREFIX):], "input": block.get("input") or {}}
                order.append(block["id"])
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                calls[block["tool_use_id"]]["result"] = _payload(block.get("content"))
        if event.get("type") == "result":
            response = str(event.get("result") or "")
            stop = "error" if event.get("is_error") else "end_turn"
    return {"response": response, "tool_calls": [calls[i] for i in order], "stop_reason": stop}


def agent(project: Project, question: str, system: str, *, model: str) -> dict:
    """One question through Claude Code with only this project's MCP tools."""
    with tempfile.TemporaryDirectory() as tmp:  # an empty folder: no CLAUDE.md or settings leak into the answer
        config = Path(tmp) / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"groundtruth": {
            "command": sys.executable,
            "args": ["-m", "groundtruth", "-p", str(project.root), "serve", "--stdio"],
            "env": {"GT_LOG": "0", "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        }}}))
        events = _run(["--model", model, "--system-prompt", system, "--tools", "",
                       "--mcp-config", str(config), "--strict-mcp-config",
                       "--allowedTools", "mcp__groundtruth", "--max-turns", "12"], question, tmp)
    return parse(events)


def judge(system: str, content: str, *, model: str) -> str:
    """The judge's reply text (llm.judge parses it)."""
    with tempfile.TemporaryDirectory() as tmp:
        events = _run(["--model", model, "--system-prompt", system, "--tools", "", "--max-turns", "1"],
                      content, tmp)
    return next((str(e.get("result") or "") for e in events if e.get("type") == "result"), "")
