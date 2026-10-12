"""Find the project you're working on and everything inside it.

A project is a folder with a ``groundtruth.yaml`` in it. Your facts, wiki,
sources, prompt, and evals all live there, so the code in this repo never needs
editing and ``git pull`` keeps working.

Which project? First match wins:
  1. an explicit path (``./gt -p my-assistant ...``)
  2. the ``GT_PROJECT`` environment variable
  3. a ``groundtruth.yaml`` in the current folder or any parent
  4. the path written in ``groundtruth.project`` at the repo root (``./gt use``)
  5. the bundled demo, ``examples/library``
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
POINTER = REPO_ROOT / "groundtruth.project"
CONFIG_NAME = "groundtruth.yaml"
DEMO_PROJECT = REPO_ROOT / "examples" / "library"


class ProjectError(Exception):
    """The project can't be found or its config is broken."""


@dataclass
class Project:
    root: Path
    config: dict[str, Any]
    data_root: Path | None = None  # set when the server is running a published data bundle

    # ------------------------------------------------------------ identity
    @property
    def name(self) -> str:
        return str(self.config.get("name") or self.root.name)

    @property
    def about(self) -> str:
        return " ".join(str(self.config.get("about") or "").split())

    # ------------------------------------------------------------ inputs (you edit these)
    @property
    def facts_dir(self) -> Path:
        return self.root / "facts"

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def pages_dir(self) -> Path:
        return self.sources_dir / "pages"

    @property
    def wiki_dir(self) -> Path:
        if self.data_root is not None:
            return self.data_root / "wiki"
        return self.root / "wiki"

    @property
    def prompt_path(self) -> Path:
        if self.data_root is not None and (self.data_root / "prompts" / "system_prompt.md").exists():
            return self.data_root / "prompts" / "system_prompt.md"  # a published bundle carries the prompt
        return self.root / "prompts" / "system_prompt.md"

    @property
    def golden_path(self) -> Path:
        return self.root / "evals" / "golden.yaml"

    @property
    def runs_dir(self) -> Path:
        return self.root / "evals" / "runs"

    # ------------------------------------------------------------ outputs (generated, gitignored)
    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def db_path(self) -> Path:
        override = os.environ.get("GT_DB_PATH", "").strip()
        if override:
            return Path(override)
        if self.data_root is not None:
            return self.data_root / "facts.db"
        return self.build_dir / "facts.db"

    @property
    def index_path(self) -> Path:
        if self.data_root is not None:
            return self.data_root / "search-index.db"
        return self.build_dir / "search-index.db"

    @property
    def logs_dir(self) -> Path:
        override = os.environ.get("GT_LOG_DIR", "").strip()
        return Path(override) if override else self.root / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    # ------------------------------------------------------------ settings
    @property
    def synonyms(self) -> dict[str, set[str]]:
        raw = ((self.config.get("search") or {}).get("synonyms")) or {}
        out: dict[str, set[str]] = {}
        for word, alts in raw.items():
            group = {str(word).lower()} | {str(a).lower() for a in (alts or [])}
            for member in group:
                out.setdefault(member, set()).update(group)
        return out

    @property
    def ingest_sources(self) -> list[str]:
        return [str(s) for s in ((self.config.get("ingest") or {}).get("sources") or [])]

    @property
    def ingest_max_pages(self) -> int:
        return int((self.config.get("ingest") or {}).get("max_pages") or 200)

    @property
    def url(self) -> str | None:
        """The deployed server's /mcp URL (``url:`` in groundtruth.yaml). Public, not a secret."""
        return str(self.config.get("url") or "").strip() or None

    def rel(self, path: Path | str) -> str:
        """A path relative to the project, for friendly output."""
        p = Path(path)
        for base in (self.root, REPO_ROOT):
            try:
                return str(p.resolve().relative_to(base.resolve()))
            except ValueError:
                continue
        return str(p)


def load(root: Path | str) -> Project:
    root = Path(root).expanduser().resolve()
    cfg = root / CONFIG_NAME
    if not cfg.exists():
        raise ProjectError(
            f"no {CONFIG_NAME} in {root}. Start one with: ./gt init {root.name}"
        )
    try:
        config = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProjectError(f"{cfg} is not valid YAML: {exc}") from exc
    if not isinstance(config, dict):
        raise ProjectError(f"{cfg} must be a YAML mapping (key: value)")
    return Project(root=root, config=config)


def _find_upwards(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        if (folder / CONFIG_NAME).exists():
            return folder
    return None


def pointer_target() -> Path | None:
    if not POINTER.exists():
        return None
    text = POINTER.read_text(encoding="utf-8").strip()
    if not text:
        return None
    target = Path(text)
    return target if target.is_absolute() else (REPO_ROOT / target)


def resolve(explicit: str | Path | None = None) -> Project:
    """Find the active project (see the module docstring for the order)."""
    if explicit:
        return load(explicit)
    env = os.environ.get("GT_PROJECT", "").strip()
    if env:
        return load(env)
    found = _find_upwards(Path.cwd())
    if found is not None:
        return load(found)
    target = pointer_target()
    if target is not None:
        return load(target)
    return load(DEMO_PROJECT)


def set_current(path: Path | str) -> Path:
    """Point ``./gt`` at a project by writing ``groundtruth.project``."""
    target = Path(path).expanduser().resolve()
    load(target)  # fail loudly if it isn't a project
    try:
        text = str(target.relative_to(REPO_ROOT))
    except ValueError:
        text = str(target)
    POINTER.write_text(text + "\n", encoding="utf-8")
    return target
