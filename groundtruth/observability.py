"""Tool-call logging: what the model asked for, whether it worked, how long it took.

Every tool call is appended to ``<project>/logs/tool_calls.jsonl``. That file is
two things at once:

  * **A live signal.** Is the bot actually routing hard facts through tools?
    ``./gt report`` turns the log into a dashboard.
  * **Your next eval set.** Questions that found nothing (zero results) are
    gaps: write the wiki page or add the fact, then add the question to
    ``evals/golden.yaml``.

Arguments are shallow-redacted so a stray token in a query never lands in a log.
Logging never breaks a tool call. Set GT_LOG=0 to turn it off, GT_LOG_STDERR=1
to echo each call.
"""
from __future__ import annotations

import contextvars
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from groundtruth.project import Project

REDACT_KEYS = {"api_key", "authorization", "token", "secret", "password", "pin", "ssn"}

# Set while handling a request from ./gt watchdog, so synthetic checks never show up
# as real demand (or fake gaps) in ./gt report.
PROBE: contextvars.ContextVar[bool] = contextvars.ContextVar("groundtruth_probe", default=False)


def log_path(project: Project) -> Path:
    return project.logs_dir / "tool_calls.jsonl"


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _redact(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if k.lower() in REDACT_KEYS:
            out[k] = "***"
        elif isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "…"
        else:
            out[k] = v
    return out


def log_tool_call(project: Project, name: str, args: dict, *, ok: bool, ms: float,
                  result_count: int | None = None, error: str | None = None,
                  request_id: str | None = None) -> None:
    if os.environ.get("GT_LOG", "1") == "0":
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "request_id": request_id or new_request_id(),
        "tool": name,
        "args": _redact(args),
        "ok": ok,
        "ms": round(ms, 1),
        "result_count": result_count,
    }
    if error:
        record["error"] = error[:300]
    if PROBE.get():
        record["probe"] = True
    try:
        path = log_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass
    if os.environ.get("GT_LOG_STDERR") == "1":
        print(f"tool {name} ok={ok} results={result_count} {record['ms']}ms", file=sys.stderr)


def read_log(project: Project) -> list[dict]:
    path = log_path(project)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
