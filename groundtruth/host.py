"""``./gt host``: one machine keeps main current, unattended.

GitHub Actions can run the weekly refresh and open a pull request for a person
to merge (.github/workflows/refresh.yml). A refresh host goes further. One
always-on machine, named in groundtruth.yaml, re-captures your sources every
night, has headless Claude Code rewrite the wiki pages they made stale, and
pushes main itself whenever every gate passes:

    ./gt host check       is this machine ready? (git push, the host name, claude, cron)
    ./gt host install     add the schedule to this machine's crontab (--at 03:00, --no-rewrite)
    ./gt host print       show that crontab block without installing it
    ./gt host uninstall   remove it

The schedule:

    nightly        ./gt host run refresh    ingest, build, check, eval; push `refresh: ...`
    2h later       ./gt host run repair     the live eval, then ./gt repair on what it got wrong
    every 5 min    ./gt host watch          publish alerts to ALERTS.md; a quick refresh of the
                                            sources facts cite every 2 hours by day; a full
                                            refresh when a person pushes main
    every 30 min   ./gt host run rewrite    ./gt rewrite on stale pages; push `wiki: rewrite ...`

Why it's safe to leave alone:
  * One pusher. Every job stands down unless main's groundtruth.yaml names this
    machine (``host: my-server``). Move the host by changing that line.
  * Main's code and content. Each job runs in its own git worktree reset to
    origin/main, never in your checkout, so a stale or dirty checkout can't leak in.
  * One writer at a time. A lock keeps the refresh, the alerts publisher, and
    rewrite pushes apart, and a push replays onto a moved main instead of failing.
  * Gates first. Nothing is pushed unless ./gt check finds nothing new and ./gt eval
    passes, and every unattended change was checked against its sources first.
  * Problems become alerts. Each job writes to logs/host/alerts.log; the watcher
    copies new ones into ALERTS.md on main, and ./gt watchdog --repo fails until
    someone fixes the cause and deletes the line.

Settings come from the environment or the checkout's .env (never committed):
GT_QUICK_REFRESH_SECONDS (default 7200; 0 turns quick refreshes off),
GT_HOST_WORKDIR (default ~/.cache/groundtruth), GT_WATCHDOG_URL (alert when the
deployed server runs old code), GT_ALERT_COMMAND / GT_ALERT_EMAIL, and the
GT_REWRITE_* knobs in rewrite.py.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

from groundtruth import alerts
from groundtruth.project import Project

BOT_PREFIXES = ("refresh:", "wiki: rewrite", "wiki: cover", "facts: update", "alerts:")  # the host's own commits
CODE_PATHS = ("groundtruth", "Dockerfile", "pyproject.toml")  # what a server image runs; data ships in bundles
JOBS = ("refresh", "rewrite", "repair")
JOB_ARGS = {"refresh": ["refresh", "--write", "--push"], "rewrite": ["rewrite"], "repair": ["repair", "--eval"]}
PUSH_TRIES = 3
REWRITE_PUSH_WAIT_SECONDS = 30 * 60
ALERTS_PUSH_WAIT_SECONDS = 60
DAY_HOURS = (5, 22)  # quick refreshes run between these hours, inclusive
CRON_TAG = "groundtruth host"


class HostError(Exception):
    pass


def log(message: str) -> None:
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{stamp} {message}", flush=True)


# ===================================================================== git


def _git_env() -> dict[str, str] | None:
    """With GT_GIT_TOKEN (the autopilot's GitHub token), git authenticates from the environment of
    this process alone, so the token is never written to .git/config where a model could read it."""
    token = os.environ.get("GT_GIT_TOKEN", "").strip()
    if not token:
        return None
    import base64
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, check=check,
                          env=_git_env())


def git_root(project: Project) -> Path:
    out = git(project.root, "rev-parse", "--show-toplevel", check=False)
    if out.returncode != 0:
        raise HostError(f"{project.root} isn't in a git checkout")
    return Path(out.stdout.strip()).resolve()


def project_path(project: Project, root: Path | None = None) -> str:
    """The project folder relative to the repo root ('.' when the project is the repo)."""
    rel = project.root.resolve().relative_to(root or git_root(project)).as_posix()
    return rel or "."


def ahead(wd: Path) -> int:
    out = git(wd, "rev-list", "--count", "origin/main..HEAD", check=False).stdout.strip()
    return int(out or 0)


def head_subject(root: Path, ref: str = "origin/main") -> str:
    return git(root, "log", "-1", "--format=%s", ref, check=False).stdout.strip()


# ===================================================================== settings


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines from a .env file. Comments, blanks, and malformed lines are skipped."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key and not key.startswith("#") and " " not in key.strip():
            values[key.strip()] = value.strip().strip("'\"")
    return values


def load_env(project: Project) -> None:
    """Fill in settings from the checkout's .env. What's already in the environment wins."""
    for key, value in read_env_file(git_root(project) / ".env").items():
        os.environ.setdefault(key, value)


def parse_quiet(value: str) -> tuple[dt.time, dt.time] | None:
    value = value.strip()
    if not value:
        return None
    start, _, end = value.partition("-")
    return dt.time.fromisoformat(start.strip()), dt.time.fromisoformat(end.strip())


def in_quiet_hours(quiet: tuple[dt.time, dt.time] | None, now: dt.datetime) -> bool:
    if quiet is None:
        return False
    start, end = quiet
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def quiet_hours() -> tuple[dt.time, dt.time] | None:
    """When rewrites stay out of the way of the nightly refresh: GT_REWRITE_QUIET_HOURS, or
    from 20 minutes before GT_REFRESH_AT (default 03:00) to 50 minutes after it."""
    explicit = os.environ.get("GT_REWRITE_QUIET_HOURS")
    if explicit is not None:
        return parse_quiet(explicit)
    at = dt.datetime.combine(dt.date.today(), dt.time.fromisoformat(os.environ.get("GT_REFRESH_AT") or "03:00"))
    return (at - dt.timedelta(minutes=20)).time(), (at + dt.timedelta(minutes=50)).time()


# ===================================================================== the one host


def this_host() -> str:
    return socket.gethostname().split(".")[0]


def configured_name(config: dict) -> str:
    """``host: my-server`` or ``host: {name: my-server}`` in groundtruth.yaml."""
    value = config.get("host") if isinstance(config, dict) else None
    if isinstance(value, dict):
        value = value.get("name")
    return str(value or "").strip()


def named_host(project: Project) -> str:
    """The machine main names as the refresh host ('' if none). Read from origin/main, not the checkout."""
    root = git_root(project)
    rel = project_path(project, root)
    shown = git(root, "show", f"origin/main:{'' if rel == '.' else rel + '/'}groundtruth.yaml", check=False)
    if shown.returncode == 0:
        try:
            return configured_name(yaml.safe_load(shown.stdout) or {})
        except yaml.YAMLError:
            pass
    return configured_name(project.config)


def stand_down(project: Project) -> str | None:
    """Why this machine must not run the jobs, or None when it may. Fetches first: a moved host
    takes effect on the next run, not whenever someone pulls this checkout."""
    git(git_root(project), "fetch", "origin", "--quiet", check=False)
    want, have = named_host(project), this_host()
    if want and want != have:
        return f"standing down: main names {want} as the refresh host, not {have}"
    return None


@contextlib.contextmanager
def lock(path: Path, wait: float = 0.0, poll: float = 5.0):
    """An exclusive lock on a file. Yields False if another process still holds it after ``wait`` seconds."""
    try:
        import fcntl
    except ImportError as exc:
        raise HostError("the refresh host needs Linux or macOS") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(poll)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def refresh_lock(project: Project) -> Path:
    """Held by a refresh for its whole run, and by anything else while it pushes main."""
    return alerts.state_dir(project) / "refresh.lock"


# ===================================================================== worktrees and pushes


def workdir(project: Project, job: str) -> Path:
    """One worktree per checkout, project, and job: two assistants, or two repos with the same name,
    never share one."""
    base = Path(os.environ.get("GT_HOST_WORKDIR") or Path.home() / ".cache" / "groundtruth")
    root = git_root(project)
    rel = project_path(project, root)
    slug = root.name if rel == "." else f"{root.name}-{re.sub(r'[^A-Za-z0-9]+', '-', rel).strip('-')}"
    ident = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return base / f"{slug}-{ident}-{job}"


def cron_marker(project: Project) -> str:
    """What the crontab block is named by: the checkout and the project, so each project has its own."""
    root = git_root(project)
    return f"{CRON_TAG} {root} {project_path(project, root)}"


def push(project: Project, wd: Path, *, wait: float = 0.0, locked: bool = False) -> str:
    """Replay the worktree's commits onto origin/main and push them.

    Returns 'nothing', 'pushed', 'busy' (the refresh lock is held), 'conflict', or 'rejected'.
    """
    if not ahead(wd):
        return "nothing"
    if not locked:
        with lock(refresh_lock(project), wait) as got:
            return push(project, wd, locked=True) if got else "busy"
    for _ in range(PUSH_TRIES):
        before = git(wd, "rev-parse", "HEAD~" + str(ahead(wd))).stdout.strip()
        git(wd, "fetch", "origin", "--quiet")
        if git(wd, "rebase", "origin/main", check=False).returncode != 0:
            git(wd, "rebase", "--abort", check=False)
            return "conflict"
        if git(wd, "rev-parse", "origin/main").stdout.strip() != before:  # main moved: re-run the gates
            why = still_green(project, wd)
            if why:
                log(why)
                return "conflict"
        out = git(wd, "push", "origin", "HEAD:main", check=False)
        if out.returncode == 0:
            log(f"pushed {git(wd, 'rev-parse', '--short', 'HEAD').stdout.strip()} to main")
            return "pushed"
        log(f"push rejected, retrying: {out.stderr.strip()[-200:]}")
    return "rejected"


def reset(wd: Path) -> None:
    git(wd, "checkout", "--quiet", "--force", "--detach", "origin/main")
    git(wd, "reset", "--hard", "--quiet", "origin/main")
    git(wd, "clean", "-fdq")


def prepare(project: Project, wd: Path, *, locked: bool = False) -> bool:
    """Point a job's worktree at a fresh origin/main.

    Commits a previous run couldn't push are pushed first. False means they're
    still waiting for the lock, so this run should leave them and stop.
    """
    root = git_root(project)
    git(root, "fetch", "origin", "--quiet")
    if not (wd / ".git").exists():
        git(root, "worktree", "prune")
        wd.parent.mkdir(parents=True, exist_ok=True)
        git(root, "worktree", "add", "--quiet", "--detach", str(wd), "origin/main")
        return True
    git(wd, "reset", "--hard", "--quiet", "HEAD")
    git(wd, "clean", "-fdq")
    if ahead(wd):
        result = push(project, wd, locked=locked)
        if result == "busy":
            return False
        if result in ("conflict", "rejected"):
            log(f"dropped {ahead(wd)} leftover commit(s) that no longer apply; this run redoes the work")
    reset(wd)
    return True


# ===================================================================== jobs


def _job_env(project: Project, wd: Path | None = None) -> dict[str, str]:
    env = {**os.environ, "GT_HOST_STATE": str(alerts.state_dir(project)), "GT_HOST_JOB": "1", "GT_LOG": "0"}
    if wd is not None:  # main's code from the worktree, never the checkout's install
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(wd), os.environ.get("PYTHONPATH", "")]))
    return env


def still_green(project: Project, wd: Path) -> str | None:
    """After replaying onto a moved main: why the combination fails the gates (None if it passes).

    Changed sources waiting for review don't count; they're expected between a refresh and a rewrite.
    """
    target = str(wd / project_path(project))
    for args in (["check", "--ignore-review"], ["eval"]):
        out = subprocess.run([sys.executable, "-m", "groundtruth", "-p", target, *args], cwd=wd,
                             env=_job_env(project, wd), text=True, capture_output=True, check=False)
        if out.returncode != 0:
            lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip().startswith(("✗", "facts", "wiki"))]
            return f"./gt {args[0]} fails after replaying onto main: {(lines or [out.stdout.strip()[-200:]])[0]}"
    return None


def run(project: Project, job: str, extra: list[str] | None = None) -> int:
    """Run one job in its own worktree at origin/main, with main's code, then push what it committed."""
    if job not in JOBS:
        raise HostError(f"unknown job {job!r} (choose from {', '.join(JOBS)})")
    why = stand_down(project)
    if why:
        log(why)
        return 0
    load_env(project)
    state = alerts.state_dir(project)
    with lock(state / f"{job}-job.lock") as got:  # one of each job at a time
        if not got:
            log(f"another {job} is still running; skipping this one")
            return 0
        if job == "refresh":  # a refresh holds the push lock throughout: nothing lands on main mid-run
            with lock(refresh_lock(project)) as free:
                if not free:
                    log("something else holds the refresh lock; skipping this run")
                    return 0
                return _run(project, job, extra or [], locked=True)
        return _run(project, job, extra or [], locked=False)


def _run(project: Project, job: str, extra: list[str], *, locked: bool) -> int:
    wd = workdir(project, job)
    try:
        if not prepare(project, wd, locked=locked):
            log("earlier commits are still waiting to be pushed; trying again next run")
            return 0
    except subprocess.CalledProcessError as exc:
        alerts.notify(project, f"the {job} job couldn't update its worktree",
                      f"git {' '.join(exc.cmd[3:])} failed: {(exc.stderr or '').strip()[-300:]}")
        return 1
    target = wd / project_path(project)
    cmd = [sys.executable, "-m", "groundtruth", "-p", str(target), *JOB_ARGS[job], *extra]
    at = git(wd, "rev-parse", "--short", "HEAD").stdout.strip()
    log(f"{job}: {' '.join(JOB_ARGS[job] + extra)} at origin/main {at}")
    rc = subprocess.run(cmd, cwd=wd, env=_job_env(project, wd), check=False).returncode
    if job == "refresh" and rc != 0:
        if rc != 1:  # 1 means the refresh already explained itself in an alert
            alerts.notify(project, "the refresh crashed", f"./gt refresh exited {rc} on {this_host()}. "
                          f"See {alerts.state_dir(project) / 'refresh.log'}.")
        reset(wd)  # nothing from a failed refresh reaches main
        return rc
    if job == "rewrite" and not locked and in_quiet_hours(quiet_hours(), dt.datetime.now()):
        log("quiet hours: the nightly refresh is near, so rewrites wait for the next run")
        return rc
    wait = 0.0 if locked else REWRITE_PUSH_WAIT_SECONDS
    result = push(project, wd, wait=wait, locked=locked)
    if result == "busy":
        log("a refresh holds the lock; the commits wait for the next run")
    elif result in ("conflict", "rejected"):
        reset(wd)
        log(f"couldn't push ({result}); dropped this run's commits, the next run redoes them")
    return rc


# ===================================================================== the watcher


def watch(project: Project) -> int:
    """Every five minutes: quick refresh, react to new commits on main, then publish alerts."""
    why = stand_down(project)
    if why:
        log(why)
        return 0
    load_env(project)
    with lock(alerts.state_dir(project) / "watch.lock") as got:
        if not got:
            return 0
        try:
            _watch(project)
        finally:  # however this run ends, new alerts reach ALERTS.md
            publish_alerts(project)
    return 0


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _watch(project: Project) -> None:
    state = alerts.state_dir(project)
    root = git_root(project)

    # Quick refresh: re-capture just the sources your facts cite, so a price or
    # date change reaches main within hours. The nightly refresh still does it all.
    interval = int(os.environ.get("GT_QUICK_REFRESH_SECONDS") or 7200)
    last = state / "last-quick-refresh"
    now = time.time()
    if interval > 0 and DAY_HOURS[0] <= dt.datetime.now().hour <= DAY_HOURS[1] \
            and now - _read_int(last) >= interval:
        last.write_text(f"{int(now)}\n")  # recorded first, so a failing one waits its turn
        run(project, "refresh", ["--quick"])

    git(root, "fetch", "origin", "--quiet")
    head = git(root, "rev-parse", "origin/main").stdout.strip()
    seen = state / "last-seen-main"
    previous = seen.read_text().strip() if seen.exists() else ""
    if head == previous:
        return
    subject = head_subject(root)
    log(f"main moved: {previous[:7] or 'none'} -> {head[:7]} ({subject})")
    if not subject.startswith(BOT_PREFIXES):
        log("a person pushed main: running a full refresh against it")
        run(project, "refresh")
        git(root, "fetch", "origin", "--quiet")
        head = git(root, "rev-parse", "origin/main").stdout.strip()
    seen.write_text(head + "\n")
    _stale_alert(project, head)
    _deploy_alert(project, head)


def _stale_alert(project: Project, head: str) -> None:
    """Without the rewrite job, tell a person when pages fall behind their sources."""
    if os.environ.get("GT_WIKI_AUTO_REWRITE") == "1":
        return  # the rewrite job clears stale pages and alerts on the ones it can't
    wd = workdir(project, "watch")
    prepare(project, wd)
    out = subprocess.run([sys.executable, "-m", "groundtruth", "-p", str(wd / project_path(project)),
                          "rewrite", "--list"], cwd=wd, env=_job_env(project, wd), text=True,
                         capture_output=True, check=False).stdout
    pages = [line for line in out.splitlines() if line.strip() and not line[0].isspace()
             and not line.startswith("nothing")]
    state = alerts.state_dir(project) / "last-stale-alert"
    fingerprint = hashlib.sha256(out.encode()).hexdigest()
    if not pages or (state.exists() and state.read_text().strip() == fingerprint):
        return
    state.write_text(fingerprint + "\n")
    alerts.notify(project, "wiki pages are behind their sources",
                  f"{len(pages)} page(s) at main {head[:7]}, starting with {pages[0]}. Run ./gt compile and hand "
                  f"build/compile-packet.md to a coding agent, or schedule ./gt rewrite (./gt host install).")


def code_commits_since(root: Path, deployed: str, head: str) -> int | None:
    """Commits after ``deployed`` up to ``head`` that change what a server image runs (None: not in history)."""
    if git(root, "merge-base", "--is-ancestor", deployed, head, check=False).returncode != 0:
        return None
    out = git(root, "rev-list", "--count", f"{deployed}..{head}", "--", *CODE_PATHS, check=False).stdout
    return int(out.strip() or 0)


def _deploy_alert(project: Project, head: str) -> None:
    """When GT_WATCHDOG_URL is set: alert if the deployed server runs code older than main's."""
    url = os.environ.get("GT_WATCHDOG_URL", "").strip() or project.url
    if not url:
        return
    from groundtruth import watchdog
    deployed = watchdog.deployed_commit(url)
    if not deployed:
        return  # unreachable or unstamped: the daily watchdog reports those
    behind = code_commits_since(git_root(project), deployed, head)
    if behind == 0:
        alerts.resolve(project, "the deployed server needs a redeploy")
        return
    reason = (f"it runs {deployed[:12]}, which isn't in main's history" if behind is None
              else f"{behind} code commit(s) on main since its build {deployed[:12]}")
    alerts.notify(project, "the deployed server needs a redeploy",
                  f"{url}: {reason}. Data updates ship in bundles; code needs a new image (docs/deploy.md).")


def publish_alerts(project: Project) -> None:
    """Copy new alerts from logs/host/alerts.log into ALERTS.md on main, and drop the resolved ones."""
    new, offset = alerts.pending(project)
    fixed = alerts.resolved(project)
    if not new and not fixed:
        return
    wd = workdir(project, "alerts")
    rel = project_path(project)
    with lock(refresh_lock(project), ALERTS_PUSH_WAIT_SECONDS) as got:
        if not got:
            log("a refresh holds the lock; alerts are published next time")
            return
        for _ in range(PUSH_TRIES):
            prepare(project, wd, locked=True)
            path = wd / rel / alerts.ALERTS_FILE
            text, added, removed = alerts.update(path.read_text(encoding="utf-8") if path.exists() else "", new, fixed)
            if not added and not removed:
                alerts.mark_published(project, offset)
                alerts.clear_resolved(project, fixed)
                return
            path.write_text(text, encoding="utf-8")
            git(wd, "add", "--", str(path))
            git(wd, "commit", "--quiet", "-m", alerts.commit_message(added, removed))
            if git(wd, "push", "origin", "HEAD:main", check=False).returncode == 0:
                log(f"ALERTS.md: {len(added)} new, {len(removed)} resolved")
                alerts.mark_published(project, offset)
                alerts.clear_resolved(project, fixed)
                return


# ===================================================================== GitHub Actions as the host


ACTIONS_HOST = "github-actions"


def claude_ready() -> str | None:
    """Why ./gt rewrite can't run here (None when it can): the claude CLI and a credential."""
    if not (os.environ.get("GT_CLAUDE") or shutil.which("claude")):
        return "the claude CLI isn't installed"
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
            or (Path.home() / ".claude").exists()):
        return "no Claude credential: set the CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY secret"
    return None


def publish_alerts_here(project: Project) -> None:
    """Merge new alerts into ALERTS.md in this checkout and push (the autopilot's version of the watcher's)."""
    new, offset = alerts.pending(project)
    fixed = alerts.resolved(project)
    if not new and not fixed:
        return
    root = git_root(project)
    path = project.root / alerts.ALERTS_FILE
    text, added, removed = alerts.update(path.read_text(encoding="utf-8") if path.exists() else "", new, fixed)
    if added or removed:
        path.write_text(text, encoding="utf-8")
        git(root, "add", "--", str(path))
        git(root, "commit", "--quiet", "-m", alerts.commit_message(added, removed))
        if push(project, root, locked=True) != "pushed":
            return  # the next run publishes them
    alerts.mark_published(project, offset)
    alerts.clear_resolved(project, fixed)


def autopilot(project: Project, *, quick: bool = False) -> int:
    """One unattended pass in this checkout, for a CI runner that is the refresh host.

    refresh (and push); ./gt rewrite; then, nightly with an ANTHROPIC_API_KEY, the live eval,
    which holds the rewrites back if the assistant now answers worse than the last run; push;
    publish alerts (and clear the ones that recovered). Returns 1 when it raised an alert, so
    the run shows red and GitHub emails about it. .github/workflows/autopilot.yml runs it;
    ``host: github-actions`` in groundtruth.yaml makes it the one pusher.
    """
    want = named_host(project)
    if want != ACTIONS_HOST:
        log(f"standing down: main names {want or 'no'} refresh host; set `host: {ACTIONS_HOST}` to use this")
        return 0
    root = git_root(project)
    env = _job_env(project, root)
    env.pop("GT_HOST_STATE", None)  # the runner's state lives in the project's logs/, which the workflow caches
    target = str(project.root)
    log_file = alerts.log_path(project)
    alerted_before = log_file.stat().st_size if log_file.exists() else 0

    def step(*args: str) -> int:
        log("+ ./gt " + " ".join(args))
        return subprocess.run([sys.executable, "-m", "groundtruth", "-p", target, *args], cwd=root, env=env,
                              check=False).returncode

    def publish() -> None:
        result = push(project, root, locked=True)
        if result == "rejected":  # e.g. branch protection on main: say so instead of losing work quietly
            alerts.notify(project, "the autopilot can't push main",
                          "its pushes are rejected. Let GitHub Actions push to main (branch protection, and "
                          "Settings > Actions > General > Workflow permissions: read and write).")
        elif result == "pushed":
            alerts.resolve(project, "the autopilot can't push main")
        if result in ("conflict", "rejected"):
            reset(root)

    rc = step("refresh", "--write", "--push", *(["--quick"] if quick else []))
    if rc not in (0, 1):
        alerts.notify(project, "the refresh crashed", f"./gt refresh exited {rc} in GitHub Actions; see the run log.")
    publish()
    why = claude_ready()
    if why is None:
        alerts.resolve(project, "pages and facts wait for a rewrite that can't run")
        if step("rewrite") not in (0, 1):
            alerts.notify(project, "the wiki rewrite job crashed", "./gt rewrite crashed in GitHub Actions; see the run log.")
    else:
        listing = subprocess.run([sys.executable, "-m", "groundtruth", "-p", target, "rewrite", "--list"], cwd=root,
                                 env=env, text=True, capture_output=True, check=False).stdout
        if listing.strip() and not listing.startswith("nothing"):
            alerts.notify(project, "pages and facts wait for a rewrite that can't run",
                          f"{why}. Until then, changed sources stay unreviewed and ./gt check stays red.")
    can_grade = (os.environ.get("ANTHROPIC_API_KEY") or why is None) and os.environ.get("GT_LIVE_EVAL", "1") != "0"
    if can_grade and quick and ahead(root):
        # Every two hours: just the golden questions this run's changes touch must stay right.
        touched = affected_questions(project, root)
        if touched and live_gate(project, step, only=touched) == "worse":
            log(f"the questions these changes touch got worse ({', '.join(touched[:5])}); holding them back for "
                f"tonight's run, which repairs or reports it")
            reset(root)
    elif can_grade and not quick:
        verdict = live_gate(project, step)
        if verdict == "worse" and why is None and ahead(root):  # fix what got worse before giving up on the night
            step("repair")
            verdict = live_gate(project, step)
        if verdict == "worse" and ahead(root):
            alerts.notify(project, "the live eval got worse",
                          f"after this run's rewrites, the assistant answers golden questions it used to get right "
                          f"(see the run log). The {ahead(root)} rewrite commit(s) were held back; nothing changed "
                          f"for users. Run ./gt eval --tier live to see which.")
            reset(root)
        elif verdict == "failing":
            from groundtruth import repair
            if why is None:  # it didn't get worse, it was already wrong: try to fix the cause
                step("repair")
            remaining, _ = repair.failing(project)
            if remaining:
                alerts.notify(project, "the live eval failed",
                              f"the assistant answers {len(remaining)} golden question(s) wrong on the production "
                              f"model, and ./gt repair couldn't fix them ({', '.join(str(r['id']) for r in remaining[:3])}). "
                              f"Run ./gt eval --tier live to see why.")
            else:
                alerts.resolve(project, "the live eval failed")
        elif verdict == "pass":
            alerts.resolve(project, "the live eval failed")
            alerts.resolve(project, "the live eval got worse")
    publish()
    publish_alerts_here(project)
    return 1 if (log_file.stat().st_size if log_file.exists() else 0) > alerted_before else 0


def affected_questions(project: Project, root: Path) -> list[str]:
    """Golden questions the unpushed commits could change the answer to: ones that call a changed facts
    file's tools, or cite a changed page."""
    from groundtruth import checks, facts
    pre = "" if project_path(project, root) == "." else project_path(project, root) + "/"
    changed = {c[len(pre):] for c in git(root, "diff", "--name-only", "origin/main..HEAD").stdout.split()
               if c.startswith(pre)}
    try:
        tools = {t.name for c in facts.load(project) if project.rel(c.file) in changed for t in c.tools}
    except facts.FactsError:
        tools = set()
    pages = {Path(c).stem for c in changed if c.startswith("wiki/")}
    return [str(i["id"]) for i in checks.load_golden(project) if isinstance(i, dict) and i.get("id")
            and (i.get("must_call_tool") in tools or Path(str(i.get("must_cite_page") or "")).stem in pages)]


def live_gate(project: Project, step, only: list[str] | None = None) -> str:
    """Run the live eval against the last full run kept in the host's state: 'pass', 'failing' (as before),
    or 'worse' (questions it used to answer right now fail). ``only``: just these questions."""
    from groundtruth import evals
    keep = alerts.state_dir(project) / "last-live.json"
    project.runs_dir.mkdir(parents=True, exist_ok=True)
    if keep.exists():  # it must be the newest run on disk, which is what ./gt eval compares against
        stamp = (dt.datetime.now() - dt.timedelta(seconds=1)).strftime("%Y%m%dT%H%M%S")
        shutil.copyfile(keep, project.runs_dir / f"{stamp}.json")
    rc = step("eval", "--tier", "live", *(["--only", ",".join(only)] if only else []))
    latest = evals.latest_live_run(project)
    if latest is None:
        return "failing"
    import json
    run = json.loads(latest.read_text())
    keep.parent.mkdir(parents=True, exist_ok=True)
    if not run.get("regressions") and not only:
        shutil.copyfile(latest, keep)  # a held-back or partial run doesn't become the new baseline
    if run.get("regressions"):
        return "worse"
    return "pass" if rc == 0 else "failing"


# ===================================================================== scheduling


def _cron_paths() -> str:
    found = shutil.which("claude")
    dirs = [str(Path(found).parent)] if found else []
    dirs += [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


def cron_block(project: Project, *, at: str = "03:00", rewrite: bool = True) -> str:
    root = git_root(project)
    hour, minute = (int(x) for x in dt.time.fromisoformat(at).strftime("%H %M").split())
    logs = alerts.state_dir(project).resolve()
    gt = f"cd {shlex.quote(str(root))} && ./gt -p {shlex.quote(project_path(project, root))}"
    lines = [
        f"# BEGIN {cron_marker(project)}",
        "SHELL=/bin/bash",
        f"PATH={_cron_paths()}",
        f"GT_REFRESH_AT={hour:02d}:{minute:02d}",
        f"GT_WIKI_AUTO_REWRITE={1 if rewrite else 0}",
        f"{minute} {hour} * * * {gt} host run refresh >> {shlex.quote(str(logs / 'refresh.log'))} 2>&1",
        f"*/5 * * * * {gt} host watch >> {shlex.quote(str(logs / 'watch.log'))} 2>&1",
    ]
    if rewrite:
        lines.append(f"*/30 * * * * {gt} host run rewrite >> {shlex.quote(str(logs / 'rewrite.log'))} 2>&1")
        repair_hour = (hour + 2) % 24  # after the night's refresh and its first rewrites
        lines.append(f"{minute} {repair_hour} * * * {gt} host run repair >> {shlex.quote(str(logs / 'repair.log'))} 2>&1")
    lines.append(f"# END {cron_marker(project)}")
    return "\n".join(lines) + "\n"


def strip_block(crontab: str, marker: str) -> str:
    begin, end = f"# BEGIN {marker}", f"# END {marker}"
    out, skip = [], False
    for line in crontab.splitlines():
        if line == begin:
            skip = True
        elif line == end:
            skip = False
        elif not skip:
            out.append(line)
    text = "\n".join(out).rstrip("\n")
    return text + "\n" if text else ""


def _crontab() -> str:
    if not shutil.which("crontab"):
        raise HostError("no crontab command on this machine; use `./gt host print` and your own scheduler")
    out = subprocess.run(["crontab", "-l"], text=True, capture_output=True, check=False)
    return out.stdout if out.returncode == 0 else ""


def _write_crontab(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def install(project: Project, *, at: str = "03:00", rewrite: bool = True) -> str:
    want, have = named_host(project), this_host()
    if want != have:
        named = f"names {want}" if want else "names no refresh host"
        raise HostError(f"main's groundtruth.yaml {named}. Only one machine may push main: add `host: {have}` "
                        f"to {project_path(project)}/groundtruth.yaml, commit and push it, then install again")
    root = git_root(project)
    alerts.state_dir(project).mkdir(parents=True, exist_ok=True)
    git(root, "fetch", "origin", "--quiet", check=False)
    head = git(root, "rev-parse", "origin/main", check=False).stdout.strip()
    if head:  # the watcher's first run shouldn't treat all of history as news
        (alerts.state_dir(project) / "last-seen-main").write_text(head + "\n")
    alerts.start_publishing(project)
    current = strip_block(_crontab(), cron_marker(project))
    _write_crontab(current + ("\n" if current else "") + cron_block(project, at=at, rewrite=rewrite))
    return f"installed the {CRON_TAG} schedule for {root} in this machine's crontab"


def uninstall(project: Project) -> str:
    root = git_root(project)
    _write_crontab(strip_block(_crontab(), cron_marker(project)))
    return f"removed the {CRON_TAG} schedule for {root}"


def check(project: Project) -> int:
    """Is this machine ready to be the refresh host? Prints each requirement."""
    bad = 0

    def line(ok: bool | None, msg: str) -> None:
        nonlocal bad
        bad += ok is False
        print(f"  {'✓' if ok else '·' if ok is None else '✗'} {msg}")

    print(f"host check: {this_host()}")
    try:
        root = git_root(project)
    except HostError as exc:
        line(False, str(exc))
        return 1
    load_env(project)
    line(git(root, "fetch", "origin", "--quiet", check=False).returncode == 0, "git fetch origin")
    line(git(root, "rev-parse", "--verify", "--quiet", "origin/main", check=False).returncode == 0,
         "origin/main exists")
    can_push = git(root, "push", "--dry-run", "origin", "refs/remotes/origin/main:refs/heads/main",
                   check=False).returncode == 0
    line(can_push, "can push main (dry run)" if can_push else "can't push main: store git credentials for origin")
    email = git(root, "config", "user.email", check=False).stdout.strip()
    line(bool(email), f"git user.email {email}" if email else "git user.email isn't set (commits will fail)")
    want = named_host(project)
    line(want == this_host(), f"main names this machine as the refresh host ({want})" if want == this_host()
         else f"main's groundtruth.yaml names {want or 'no host'}: add `host: {this_host()}` and push it")
    claude = os.environ.get("GT_CLAUDE") or shutil.which("claude")
    line(bool(claude) or None, f"claude CLI {claude}" if claude
         else "claude CLI not found: needed for ./gt rewrite (https://claude.com/claude-code)")
    token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    line(True if token else None, "CLAUDE_CODE_OAUTH_TOKEN is set" if token else
         "no CLAUDE_CODE_OAUTH_TOKEN: run `claude setup-token` and put it in .env (gitignored), or "
         "rewrites stop when the interactive login expires")
    hook = os.environ.get("GT_ALERT_COMMAND") or os.environ.get("GT_ALERT_EMAIL")
    line(True if hook else None, "alert hook configured" if hook else
         "no GT_ALERT_COMMAND or GT_ALERT_EMAIL: alerts reach you through ALERTS.md and the watchdog only")
    try:
        installed = f"# BEGIN {cron_marker(project)}" in _crontab()
        line(True if installed else None,
             "schedule installed" if installed else "schedule not installed: ./gt host install")
    except HostError as exc:
        line(None, str(exc))
    print("ready" if not bad else f"{bad} problem(s) above")
    return 0 if not bad else 1
