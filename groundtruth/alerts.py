"""Alerts from the unattended jobs, put where a person will see them.

The refresh host (``./gt host``) runs jobs nobody watches. Every problem they hit
goes through ``notify()`` into ``logs/host/alerts.log`` on that machine, which
nobody reads either. So every five minutes the watcher copies new lines into
``ALERTS.md`` in the project and pushes it (``alerts: ...`` commits), and
``./gt watchdog --repo`` (daily in GitHub Actions) fails while ``ALERTS.md``
lists anything, so GitHub emails you. Fix the cause, then delete the alert's
line and push.

    ./gt alerts          # what's open, and the latest lines from this machine's log

An open alert isn't added twice, even when its numbers or commit hashes change,
so a reminder that repeats every run stays one line.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from groundtruth.project import Project

ALERTS_FILE = "ALERTS.md"
HEADER = """# Alerts

Open alerts from the refresh host (`./gt host`). The watcher adds them; the
daily `./gt watchdog --repo` fails while any are listed. Fix the cause, then
delete the alert's line and push.
"""
ENTRY_RE = re.compile(r"^- (\S+) (.+)$")
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+) (.+)$")


def state_dir(project: Project) -> Path:
    """Where the host keeps its logs, locks, and state: the checkout's, even when a job runs in a worktree."""
    override = os.environ.get("GT_HOST_STATE", "").strip()
    return Path(override) if override else project.logs_dir / "host"


def log_path(project: Project) -> Path:
    return state_dir(project) / "alerts.log"


def notify(project: Project, title: str, body: str) -> None:
    """Record an alert. Also runs GT_ALERT_COMMAND and mails GT_ALERT_EMAIL when they're set."""
    body = " ".join(body.split())
    print(f"ALERT: {title} — {body}", file=sys.stderr)
    path = log_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {title} — {body}\n")
    host = socket.gethostname().split(".")[0]
    command = os.environ.get("GT_ALERT_COMMAND", "").strip()
    if command:
        env = {**os.environ, "GT_ALERT_TITLE": title, "GT_ALERT_MESSAGE": body, "GT_ALERT_HOST": host}
        subprocess.run(["bash", "-lc", command], env=env, check=False, capture_output=True)
    email = os.environ.get("GT_ALERT_EMAIL", "").strip()
    if email and shutil.which("mail"):
        subprocess.run(["mail", "-s", f"{title} on {host}", email], input=f"host: {host}\n\n{body}\n",
                       text=True, check=False, capture_output=True)


def normalize(message: str) -> str:
    """An alert's identity: its text with hashes and numbers blanked out."""
    text = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", message.strip())
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).lower()


def parse_log(text: str) -> list[tuple[str, str]]:
    """(timestamp, message) for each alerts.log line."""
    return [(m.group(1), m.group(2).strip()) for line in text.splitlines() if (m := LINE_RE.match(line.strip()))]


def open_entries(text: str) -> list[tuple[str, str]]:
    """(timestamp, message) for each alert listed in an ALERTS.md."""
    return [(m.group(1), m.group(2).strip()) for line in text.splitlines() if (m := ENTRY_RE.match(line.strip()))]


def open_alerts(project: Project) -> list[tuple[str, str]]:
    path = project.root / ALERTS_FILE
    return open_entries(path.read_text(encoding="utf-8")) if path.exists() else []


def merge(current: str, new: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """ALERTS.md with the new alerts that aren't already open, and those messages."""
    text = current if current.strip() else HEADER
    seen = {normalize(message) for _, message in open_entries(text)}
    added, lines = [], []
    for stamp, message in new:
        key = normalize(message)
        if key in seen:
            continue
        seen.add(key)
        added.append(message)
        lines.append(f"- {stamp} {' '.join(message.split())}")
    if not lines:
        return current, []
    if not text.endswith("\n"):
        text += "\n"
    if not open_entries(text):
        text += "\n"
    return text + "\n".join(lines) + "\n", added


def pending(project: Project) -> tuple[list[tuple[str, str]], int]:
    """Alerts logged since the last publish, and the log offset to record once they're published.

    The first call only records where the log ends: older alerts predate publishing and aren't news.
    """
    log = log_path(project)
    offset_file = state_dir(project) / "alerts-published-bytes"
    size = log.stat().st_size if log.exists() else 0
    try:
        offset = int(offset_file.read_text().strip())
    except (OSError, ValueError):
        offset_file.parent.mkdir(parents=True, exist_ok=True)
        offset_file.write_text(f"{size}\n")
        return [], size
    if size < offset:  # the log was truncated or replaced
        offset = 0
    if size == offset:
        return [], offset
    with open(log, "rb") as fh:
        fh.seek(offset)
        raw = fh.read(size - offset)
    complete = raw[: raw.rfind(b"\n") + 1]  # a line still being written waits for the next run
    return parse_log(complete.decode("utf-8", errors="replace")), offset + len(complete)


def mark_published(project: Project, offset: int) -> None:
    (state_dir(project) / "alerts-published-bytes").write_text(f"{offset}\n")
