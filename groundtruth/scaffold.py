"""``./gt init`` and ``./gt use``: start your own assistant and switch between projects.

    ./gt init my-assistant                     # a blank, working project
    ./gt init acme-help --template saas        # start from a template
    ./gt init --list                           # see the templates
    ./gt use examples/library                  # point ./gt at another project

A new project is a folder you own: facts/, wiki/, sources/, prompts/, evals/ and
a groundtruth.yaml. It's born passing ``./gt check`` and ``./gt eval``, so every
change you make from there is one you can test.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from groundtruth.project import DEMO_PROJECT, REPO_ROOT, set_current

TEMPLATES_DIR = REPO_ROOT / "templates"
TEXT_SUFFIXES = {".yaml", ".yml", ".md", ".txt"}


def templates() -> dict[str, str]:
    """name -> one-line description."""
    out = {}
    for d in sorted(TEMPLATES_DIR.iterdir()) if TEMPLATES_DIR.exists() else []:
        cfg = d / "groundtruth.yaml"
        if cfg.exists():
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            out[d.name] = str(data.get("template_description") or data.get("about") or "").strip()
    out["library"] = "The full demo: a public library with hours, events, fees, and a 15-question eval set."
    return out


def _source(template: str) -> Path:
    if template == "library":
        return DEMO_PROJECT
    path = TEMPLATES_DIR / template
    if not (path / "groundtruth.yaml").exists():
        raise ValueError(f"no template {template!r}. Choose from: {', '.join(templates())}")
    return path


def init(dest: str | Path, template: str = "blank", name: str | None = None) -> Path:
    src = _source(template)
    target = Path(dest).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"{target} already exists and isn't empty")
    shutil.copytree(src, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("build", "logs", "reports", "*-tools.json", "*-replay.json",
                                                  "__pycache__", ".DS_Store"))
    display = name or target.name.replace("-", " ").replace("_", " ").title()
    for path in target.rglob("*"):
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8")
            if "{{NAME}}" in text:
                path.write_text(text.replace("{{NAME}}", display), encoding="utf-8")
    cfg = target / "groundtruth.yaml"
    text = cfg.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^template_description:.*\n", "", text)
    if name:
        text = re.sub(r"(?m)^name:.*$", f"name: {display}", text, count=1)
    cfg.write_text(text, encoding="utf-8")
    set_current(target)
    return target


def use(path: str | Path) -> Path:
    return set_current(path)
