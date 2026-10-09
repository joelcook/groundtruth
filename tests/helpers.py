"""Shared test helpers: work on a throwaway copy of a project, never the real one."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["GT_LOG"] = "0"

from groundtruth import project  # noqa: E402

LIBRARY = REPO / "examples" / "library"
TEMPLATES = REPO / "templates"


def copy_project(src: Path = LIBRARY) -> tuple[project.Project, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    dest = Path(tmp.name) / src.name
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("build", "logs", "reports", "*-tools.json",
                                                             "*-replay.json"))
    return project.load(dest), tmp


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
