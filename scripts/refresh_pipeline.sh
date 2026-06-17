#!/usr/bin/env bash
# The weekly agent: a governed, multi-step refresh of the knowledge base.
#
#   scrape → rebuild facts → reindex → recompile stale prose → gate → eval → PR
#
# It is autonomous and safe to schedule (see scripts/com.groundtruth.refresh.plist
# and the cron note in docs/02-weekly-agent.md). The design choices that make it
# safe to leave running unattended:
#
#   * STATE lives in git. Work happens on a refresh/<date> branch, never main.
#   * IDEMPOTENT + change-gated. If scraping produced no upstream change, the run
#     makes no commits, opens no PR, and (this is the point) spends $0 on the LLM.
#   * FAILURE IS LEGIBLE. On any gate failure the script stops ON the refresh
#     branch so a human can inspect exactly what it saw. On success it returns
#     you to the branch you started on.
#   * EVALS AUTO-ESCALATE. The free deterministic replay runs every night; the
#     paid release-tier Golden Set runs only when the run actually changed content.
#
# Usage:
#   ./scripts/refresh_pipeline.sh                 # dry run: audit + gates, no writes
#   ./scripts/refresh_pipeline.sh --write --open-pr   # the scheduled invocation

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
VENV="$REPO/.venv/bin/python"
[[ -x "$VENV" ]] || VENV="python3"

DATE="$(date +%Y-%m-%d)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BRANCH="refresh/$DATE"
LOG_DIR="$REPO/logs/refresh"
PR_BODY="$LOG_DIR/$STAMP-pr-body.md"
mkdir -p "$LOG_DIR"

WRITE=0; OPEN_PR=0; LIVE_EVALS=0
CONTENT_COMMITS=0
EVAL_TIER="fast"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --write) WRITE=1 ;;
    --open-pr) OPEN_PR=1 ;;
    --live-evals) LIVE_EVALS=1 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac; shift
done

[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

log() { echo; echo "## $*"; }
run() { echo "+ $*"; "$@"; }

run_gates() {
  log "Validation gates"
  run "$VENV" scripts/check_wiki.py
  run "$VENV" scripts/lint_no_hardfacts.py
  run "$VENV" -m unittest discover -s tests -q
}

run_eval_gate() {
  log "Eval gate"
  if (( LIVE_EVALS == 1 || CONTENT_COMMITS > 0 )); then
    EVAL_TIER="release"
    (( LIVE_EVALS == 0 )) && echo "Auto-escalating to release tier: $CONTENT_COMMITS content change(s) this run."
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
      echo "FATAL: ANTHROPIC_API_KEY required for release-tier evals." >&2; exit 1
    fi
    run "$VENV" scripts/run_evals.py --tier release
  else
    run "$VENV" scripts/run_evals.py --tier fast
  fi
}

commit_if_dirty() {
  local msg="$1"; shift
  if [[ -n "$(git status --porcelain -- "$@")" ]]; then
    run git add "$@"; run git commit -q -m "$msg"
  else
    echo "no changes: $msg"
  fi
}

# -------------------------------------------------------------- dry run
if (( WRITE == 0 )); then
  log "groundtruth refresh — DRY RUN ($DATE)"
  run "$VENV" scripts/recompile.py --list-stale
  run_gates
  run_eval_gate
  echo; echo "Dry run complete. Use --write to scrape, rebuild, recompile, and open a PR."
  exit 0
fi

# -------------------------------------------------------------- write mode
log "groundtruth refresh — WRITE ($DATE)"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "FATAL: write-mode refresh requires a clean worktree." >&2
  git status --short >&2; exit 1
fi
ORIGINAL_BRANCH="$(git branch --show-current)"
git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null \
  && run git checkout -q "$BRANCH" || run git checkout -q -b "$BRANCH"
START_SHA="$(git rev-parse HEAD)"
return_to_start() { [[ "$ORIGINAL_BRANCH" != "$BRANCH" ]] && run git checkout -q "$ORIGINAL_BRANCH" || true; }

log "Scrape upstream sources"
if [[ -f scripts/scrape.py ]]; then
  run "$VENV" scripts/scrape.py
  commit_if_dirty "refresh: sources ($DATE)" sources
else
  echo "No scripts/scrape.py — using the committed sources/ as the evidence layer."
fi

log "Rebuild fact projection + search index"
run "$VENV" scripts/build_db.py
run "$VENV" scripts/build_index.py
commit_if_dirty "refresh: rebuild db + index ($DATE)" db

log "Recompile stale prose (review-gated)"
if [[ "$("$VENV" scripts/recompile.py --list-stale)" != "No stale pages." ]]; then
  run "$VENV" scripts/recompile.py --stale --write --branch "$BRANCH"
  commit_if_dirty "refresh: recompile stale wiki ($DATE)" wiki
else
  echo "No stale pages."
fi

CONTENT_COMMITS="$(git rev-list --count "$START_SHA"..HEAD)"
echo; echo "content commits this run: $CONTENT_COMMITS"

run_gates
run_eval_gate

if (( CONTENT_COMMITS == 0 )); then
  log "No upstream changes"
  echo "Nothing changed. No branch push, no PR, no API spend. (This is most nights.)"
  return_to_start
  exit 0
fi

log "Draft PR"
{
  echo "# groundtruth refresh $DATE"; echo
  echo "Automated governed refresh from \`$BRANCH\`. Eval tier run: **$EVAL_TIER**."; echo
  echo "## Commits"; git log --oneline "$START_SHA"..HEAD | sed 's/^/- /'; echo
  echo "## Diff stat"; echo '```'; git diff --stat "$START_SHA"..HEAD; echo '```'
} > "$PR_BODY"

if (( OPEN_PR == 1 )) && command -v gh >/dev/null 2>&1; then
  run git push -u origin "$BRANCH"
  run gh pr create --draft --base main --head "$BRANCH" --title "refresh: $DATE" --body-file "$PR_BODY" \
    || echo "WARN: gh pr create failed; branch is pushed for manual PR."
else
  echo "PR body written to $PR_BODY"
  echo "Open it with: gh pr create --draft --base main --head $BRANCH --body-file $PR_BODY"
fi

return_to_start
echo; echo "Refresh complete on $BRANCH."
