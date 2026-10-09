"""``./gt report``: what is the assistant actually being asked, and is it working?

Reads ``logs/tool_calls.jsonl`` (written by the server on every tool call) and
writes ``reports/observability.md``:

  * volume, error rate, and latency (p50 / p95) per tool
  * the most common questions people search for
  * **gaps**: searches and lookups that found nothing. Each one is a to-do:
    write the page or add the fact, then add the question to evals/golden.yaml.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from groundtruth import observability
from groundtruth.project import Project


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return values[idx]


def _query_text(rec: dict) -> str:
    args = rec.get("args") or {}
    for key in ("query", "path_or_slug"):
        if args.get(key):
            return str(args[key]).strip()
    vals = [f"{k}={v}" for k, v in args.items() if k not in ("limit", "max_chars") and v not in (None, "")]
    return ", ".join(vals) or "(no arguments)"


def build(project: Project, days: int = 30) -> tuple[str, dict]:
    records = observability.read_log(project)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    recent = []
    for r in records:
        try:
            ts = dt.datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts >= cutoff and not r.get("probe"):
            recent.append(r)

    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in recent:
        by_tool[r.get("tool", "?")].append(r)
    errors = [r for r in recent if not r.get("ok", True)]
    gaps = Counter(f"{r.get('tool')}: {_query_text(r)}" for r in recent
                   if r.get("ok", True) and r.get("result_count") == 0)
    searches = Counter(_query_text(r).lower() for r in recent if r.get("tool") == "search_wiki")
    days_active = len({str(r["ts"])[:10] for r in recent})

    stats = {"calls": len(recent), "errors": len(errors), "gaps": sum(gaps.values()),
             "tools": {t: len(v) for t, v in by_tool.items()}, "days_active": days_active}

    lines = [
        f"# {project.name}: assistant report",
        "",
        f"Last {days} days, generated {dt.date.today().isoformat()} by `./gt report`.",
        "",
    ]
    if not recent:
        lines += ["No tool calls logged yet. Start the server (`./gt serve`), connect a chat client",
                  "(`./gt connect`), ask a few questions, then run `./gt report` again.", ""]
        return "\n".join(lines), stats
    err_rate = 100.0 * len(errors) / len(recent)
    lines += [
        "## At a glance",
        "",
        "| Tool calls | Active days | Errors | Lookups that found nothing |",
        "|---:|---:|---:|---:|",
        f"| {len(recent)} | {days_active} | {len(errors)} ({err_rate:.1f}%) | {sum(gaps.values())} |",
        "",
        "## Per tool",
        "",
        "| Tool | Calls | Errors | Found nothing | p50 ms | p95 ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for tool, recs in sorted(by_tool.items(), key=lambda kv: -len(kv[1])):
        ms = [float(r.get("ms") or 0) for r in recs]
        empty = sum(1 for r in recs if r.get("result_count") == 0)
        errs = sum(1 for r in recs if not r.get("ok", True))
        lines.append(f"| `{tool}` | {len(recs)} | {errs} | {empty} | {_pct(ms, 0.5):.0f} | {_pct(ms, 0.95):.0f} |")
    lines += ["", "## Gaps: asked, but nothing found", ""]
    if gaps:
        lines += ["Each line is a to-do: write the wiki page or add the fact, then add the question",
                  "to `evals/golden.yaml` so it stays fixed.", ""]
        lines += [f"- {q} ({n}x)" for q, n in gaps.most_common(25)]
    else:
        lines.append("None. Every lookup found something.")
    lines += ["", "## Top searches", ""]
    lines += [f"- {q} ({n}x)" for q, n in searches.most_common(20)] or ["- (no searches yet)"]
    if errors:
        lines += ["", "## Recent errors", ""]
        lines += [f"- {r.get('ts', '')[:19]} `{r.get('tool')}` {_query_text(r)}: {r.get('error', '')}"
                  for r in errors[-10:]]
    lines.append("")
    return "\n".join(lines), stats


def run(project: Project, days: int = 30) -> tuple[str, dict]:
    text, stats = build(project, days)
    out = project.reports_dir / "observability.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return str(out), stats
