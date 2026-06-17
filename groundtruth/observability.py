"""Minimal tool-call observability.

Every tool the model calls is logged here: what it asked for, whether it
succeeded, and how long it took. In production you want this in a real store;
for the blueprint it is a JSON-lines file (``logs/tool_calls.jsonl``) plus an
optional stderr echo. Two reasons it matters:

  * **Evals grade the call, not just the text** (see groundtruth/evals.py), and
    this is the same signal at runtime — you can see whether the live bot is
    actually routing hard facts through tools.
  * **It is your dataset.** The questions real users ask, and the tools the model
    chose, are exactly what you mine to write next month's eval cases.

Arguments are shallow-redacted so a stray token in a query never lands in a log.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "tool_calls.jsonl"
REDACT_KEYS = {"api_key", "authorization", "token", "secret", "password"}


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


def log_tool_call(name: str, args: dict, *, ok: bool, ms: float,
                  result_count: int | None = None, request_id: str | None = None) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "request_id": request_id or new_request_id(),
        "tool": name,
        "args": _redact(args),
        "ok": ok,
        "ms": round(ms, 1),
        "result_count": result_count,
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass  # never let logging break a tool call
    if os.environ.get("GT_LOG_STDERR") == "1":
        import sys
        print(f"tool {name} ok={ok} {record['ms']}ms", file=sys.stderr)


def timed(name: str):
    """Decorator: time a tool, log the call, count results, swallow nothing."""
    def deco(fn):
        def wrapper(**kwargs):
            rid = new_request_id()
            t0 = time.perf_counter()
            ok = True
            result = None
            try:
                result = fn(**kwargs)
                return result
            except Exception:
                ok = False
                raise
            finally:
                count = len(result) if isinstance(result, (list, dict)) else None
                log_tool_call(name, kwargs, ok=ok, ms=(time.perf_counter() - t0) * 1000,
                              result_count=count, request_id=rid)
        wrapper.__name__ = getattr(fn, "__name__", name)
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco
