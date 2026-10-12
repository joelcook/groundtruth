"""The refresh host: one pusher, worktrees at main, pushes that replay, alerts that reach ALERTS.md."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import LIBRARY, copy_project, write

from groundtruth import alerts, host, ingest, project, refresh, watchdog

DEPLOY_A = ("2026-10-12T03:25:03-04:00", "the deployed server needs a redeploy — 3 code commit(s) since 43abd47c9f66")
DEPLOY_B = ("2026-10-13T03:25:03-04:00", "the deployed server needs a redeploy — 5 code commit(s) since 7075d7a1b2c3")
PAGE_X = ("2026-10-12T18:01:00-04:00", "a wiki page would not rewrite — wiki/x.md failed")
PAGE_Y = ("2026-10-12T18:02:00-04:00", "a wiki page would not rewrite — wiki/y.md failed")


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class AlertsTests(unittest.TestCase):
    def test_parse_and_normalize(self):
        self.assertEqual(alerts.parse_log(f"{DEPLOY_A[0]} {DEPLOY_A[1]}\nnot an alert\n"), [DEPLOY_A])
        self.assertEqual(alerts.normalize(DEPLOY_A[1]), alerts.normalize(DEPLOY_B[1]))
        self.assertNotEqual(alerts.normalize(PAGE_X[1]), alerts.normalize(PAGE_Y[1]))

    def test_merge(self):
        text, added = alerts.merge("", [DEPLOY_A])
        self.assertTrue(text.startswith("# Alerts\n"))
        self.assertEqual((added, alerts.open_entries(text)), ([DEPLOY_A[1]], [DEPLOY_A]))
        self.assertEqual(alerts.merge(text, [DEPLOY_B]), (text, []))  # already open, numbers aside
        both, added = alerts.merge(alerts.HEADER, [PAGE_X, PAGE_Y, PAGE_X])
        self.assertEqual(added, [PAGE_X[1], PAGE_Y[1]])
        cleared = "\n".join(line for line in text.splitlines() if not line.startswith("- ")) + "\n"
        self.assertEqual(alerts.merge(cleared, [DEPLOY_B])[1], [DEPLOY_B[1]])  # a cleared alert can come back

    def test_pending_skips_history_and_waits_for_whole_lines(self):
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"GT_HOST_STATE": str(Path(tmp.name) / "state")}):
            log = alerts.log_path(p)
            write(log, f"{PAGE_X[0]} {PAGE_X[1]}\n")
            self.assertEqual(alerts.pending(p)[0], [PAGE_X])  # nothing logged is ever dropped silently
            alerts.start_publishing(p)  # what ./gt host install does: only what comes next is news
            self.assertEqual(alerts.pending(p)[0], [])
            alerts.notify(p, "the refresh failed", "./gt check fails")
            with open(log, "a") as fh:
                fh.write("2026-10-12T19:00:00-04:00 half a li")
            new, offset = alerts.pending(p)
            self.assertEqual([m for _, m in new], ["the refresh failed — ./gt check fails"])
            alerts.mark_published(p, offset)
            self.assertEqual(alerts.pending(p)[0], [])


class RemoteRepo(unittest.TestCase):
    """origin (bare) and a host checkout with the library in assistant/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        seed = base / "seed"
        shutil.copytree(LIBRARY, seed / "assistant",
                        ignore=shutil.ignore_patterns("build", "logs", "reports", "*-tools.json"))
        write(seed / ".gitignore", "build/\nlogs/\nreports/\n**/evals/runs/*-tools.json\n")
        write(seed / "docs" / "opening.md", "# Opening\n\nThe new branch opens next spring with a maker space.\n")
        cfg = seed / "assistant" / "groundtruth.yaml"
        cfg.write_text(cfg.read_text().replace("ingest:\n  sources: []", "ingest:\n  sources: [../docs]")
                       + f"\nhost: {host.this_host()}\n")
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                     ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "start"]):
            sh(seed, *args)
        self.origin = base / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(self.origin)], check=True)
        self.checkout = base / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)], check=True)
        sh(self.checkout, "config", "user.email", "host@example.org")
        sh(self.checkout, "config", "user.name", "host")
        self.p = project.load(self.checkout / "assistant")
        self.env = mock.patch.dict(os.environ, {"GT_HOST_WORKDIR": str(base / "work"),
                                                "GT_HOST_STATE": str(base / "state")})
        self.env.start()
        for key in ("GT_ALERT_COMMAND", "GT_ALERT_EMAIL", "GT_WATCHDOG_URL", "GT_HOST_JOB"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def origin_subject(self) -> str:
        return sh(self.origin, "log", "-1", "--format=%s", "main")


class HostTests(RemoteRepo):
    def test_only_the_named_host_runs(self):
        self.assertIsNone(host.stand_down(self.p))
        cfg = self.p.root / "groundtruth.yaml"
        cfg.write_text(cfg.read_text().replace(f"host: {host.this_host()}", "host: some-other-box"))
        self.assertIsNone(host.stand_down(self.p))  # the checkout's copy doesn't count; main's does
        sh(self.checkout, "commit", "-qam", "move the host")
        sh(self.checkout, "push", "-q", "origin", "HEAD:main")
        sh(self.checkout, "fetch", "-q", "origin")
        self.assertIn("some-other-box", host.stand_down(self.p))
        self.assertEqual(host.run(self.p, "refresh"), 0)
        self.assertEqual(self.origin_subject(), "move the host")  # stood down: nothing pushed

    def test_push_replays_onto_a_moved_main(self):
        wd = host.workdir(self.p, "rewrite")
        self.assertTrue(host.prepare(self.p, wd))
        write(wd / "assistant" / "notes.txt", "x")
        sh(wd, "add", "-A")
        sh(wd, "commit", "-qm", "wiki: rewrite a page")
        write(self.checkout / "README.md", "someone else\n")  # main moves meanwhile
        sh(self.checkout, "add", "-A")
        sh(self.checkout, "commit", "-qm", "a person's commit")
        sh(self.checkout, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(host.push(self.p, wd), "pushed")
        self.assertEqual(sh(self.origin, "log", "-2", "--format=%s", "main").splitlines(),
                         ["wiki: rewrite a page", "a person's commit"])

    def test_a_replay_that_breaks_the_gates_isnt_pushed(self):
        wd = host.workdir(self.p, "rewrite")
        host.prepare(self.p, wd)
        write(wd / "assistant" / "wiki" / "broken.md", "no frontmatter")  # fails ./gt check
        sh(wd, "add", "-A")
        sh(wd, "commit", "-qm", "wiki: rewrite broken")
        write(self.checkout / "README.md", "main moves\n")
        sh(self.checkout, "add", "-A")
        sh(self.checkout, "commit", "-qm", "a person's commit")
        sh(self.checkout, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(host.push(self.p, wd), "conflict")
        self.assertEqual(self.origin_subject(), "a person's commit")

    def test_a_held_lock_makes_pushes_wait(self):
        wd = host.workdir(self.p, "rewrite")
        host.prepare(self.p, wd)
        write(wd / "x.txt", "x")
        sh(wd, "add", "-A")
        sh(wd, "commit", "-qm", "wiki: rewrite x")
        with host.lock(host.refresh_lock(self.p)):
            self.assertEqual(host.push(self.p, wd), "busy")
            self.assertFalse(host.prepare(self.p, wd))  # leftovers wait for the lock instead of vanishing
        self.assertTrue(host.prepare(self.p, wd))
        self.assertEqual(self.origin_subject(), "wiki: rewrite x")

    def test_host_refresh_ingests_in_a_worktree_and_pushes_main(self):
        self.assertEqual(host.run(self.p, "refresh"), 0)
        self.assertTrue(self.origin_subject().startswith("refresh: sources ("))
        self.assertEqual(sh(self.checkout, "status", "--porcelain"), "")  # the checkout is untouched
        files = sh(self.origin, "ls-tree", "-r", "--name-only", "main", "assistant/sources/pages")
        self.assertIn("opening", files)
        # A quiet night still leaves a trace, so the watchdog can tell it from a dead host.
        self.assertEqual(host.run(self.p, "refresh"), 0)
        self.assertTrue(self.origin_subject().startswith("refresh: no source changes ("))

    def test_a_quiet_quick_refresh_leaves_no_trace(self):
        head = sh(self.checkout, "rev-parse", "HEAD")
        with mock.patch.object(ingest, "recapture", return_value=ingest.IngestResult()):  # no network in tests
            self.assertEqual(refresh.run(self.p, push=True, quick=True), 0)
        self.assertEqual(sh(self.checkout, "rev-parse", "HEAD"), head)

    def test_autopilot_is_the_one_pusher_in_ci(self):
        self.assertEqual(host.autopilot(self.p), 0)  # main names this machine, not github-actions
        self.assertEqual(self.origin_subject(), "start")
        cfg = self.p.root / "groundtruth.yaml"
        cfg.write_text(cfg.read_text().replace(f"host: {host.this_host()}", "host: github-actions"))
        sh(self.checkout, "commit", "-qam", "the autopilot is the host")
        sh(self.checkout, "push", "-q", "origin", "HEAD:main")
        self.assertIn("github-actions", host.stand_down(self.p))  # machines step aside
        with mock.patch.object(host, "claude_ready", return_value="no Claude credential (test)"):
            self.assertEqual(host.autopilot(self.p), 1)  # it raised an alert: the run shows red
        log = sh(self.origin, "log", "-3", "--format=%s", "main").splitlines()
        self.assertTrue(log[1].startswith("refresh: sources ("), log)
        self.assertTrue(log[0].startswith("alerts: pages and facts wait for a rewrite"), log)  # no silent stall
        self.assertEqual(sh(self.checkout, "status", "--porcelain"), "")

    def test_the_token_never_lands_in_git_config(self):
        with mock.patch.dict(os.environ, {"GT_GIT_TOKEN": "ghs_test_token_value"}):
            env = host._git_env()
            host.git(self.checkout, "status")
        self.assertIn("GIT_CONFIG_VALUE_0", env)
        self.assertNotIn("ghs_test", (self.checkout / ".git" / "config").read_text())

    def test_a_fixed_alert_leaves_alerts_md(self):
        alerts.start_publishing(self.p)
        alerts.notify(self.p, "the refresh failed", "./gt check fails after the refresh (x)")
        host.publish_alerts(self.p)
        self.assertIn("the refresh failed", sh(self.origin, "show", "main:assistant/ALERTS.md"))
        alerts.resolve(self.p, "the refresh failed")
        host.publish_alerts(self.p)
        self.assertNotIn("the refresh failed", sh(self.origin, "show", "main:assistant/ALERTS.md"))
        self.assertTrue(self.origin_subject().startswith("alerts: resolved"))

    def test_the_live_gate_holds_back_only_what_got_worse(self):
        import json

        def step(regressions, rc):
            def run(*args):
                self.p.runs_dir.mkdir(parents=True, exist_ok=True)
                (self.p.runs_dir / "20991231T000000.json").write_text(json.dumps({"regressions": regressions}))
                return rc
            return run
        self.assertEqual(host.live_gate(self.p, step([], 0)), "pass")
        self.assertTrue((alerts.state_dir(self.p) / "last-live.json").exists())  # the next run compares to it
        self.assertEqual(host.live_gate(self.p, step(["card-fee"], 1)), "worse")
        self.assertEqual(json.loads((alerts.state_dir(self.p) / "last-live.json").read_text())["regressions"], [])
        self.assertEqual(host.live_gate(self.p, step([], 1)), "failing")  # wrong before too: not a regression
        before = (alerts.state_dir(self.p) / "last-live.json").read_text()
        host.live_gate(self.p, step([], 0), only=["q1"])
        self.assertEqual((alerts.state_dir(self.p) / "last-live.json").read_text(), before)  # partial: not a baseline

    def test_quick_runs_grade_only_what_their_changes_touch(self):
        wd = host.git_root(self.p)
        fees = self.p.root / "facts" / "fees.yaml"
        fees.write_text(fees.read_text().replace("aliases: [room, study room]", "aliases: [room, study room, rooms]"))
        sh(wd, "commit", "-qam", "facts: update fees.yaml")
        touched = host.affected_questions(self.p, wd)
        self.assertIn("printing-bw-cost", touched)  # it calls get_fee
        self.assertNotIn("central-saturday-open", touched)

    def test_alerts_reach_alerts_md_on_main_once(self):
        alerts.start_publishing(self.p)
        alerts.notify(self.p, "the refresh failed", "./gt check fails after the refresh (facts: x)")
        host.publish_alerts(self.p)
        self.assertTrue(self.origin_subject().startswith("alerts: the refresh failed"))
        text = sh(self.origin, "show", "main:assistant/ALERTS.md")
        self.assertIn("./gt check fails after the refresh", text)
        alerts.notify(self.p, "the refresh failed", "./gt check fails after the refresh (facts: x)")
        host.publish_alerts(self.p)
        self.assertEqual(sh(self.origin, "show", "main:assistant/ALERTS.md"), text)  # still one line
        sh(self.checkout, "pull", "-q", "origin", "main")
        self.assertEqual(len(alerts.open_alerts(self.p)), 1)

    def test_cron_block_installs_three_jobs_and_strips_cleanly(self):
        block = host.cron_block(self.p, at="04:15")
        self.assertIn("15 4 * * * cd", block)
        self.assertIn("host run refresh", block)
        self.assertIn("*/5 * * * *", block)
        self.assertIn("*/30 * * * *", block)
        self.assertIn("GT_REFRESH_AT=04:15", block)
        self.assertIn("15 6 * * * cd", block)  # the live eval and repair, two hours after the refresh
        self.assertIn("host run repair", block)
        self.assertNotIn("host run rewrite", host.cron_block(self.p, rewrite=False))
        mine = "0 1 * * * echo keep me\n"
        self.assertEqual(host.strip_block(mine + block, host.cron_marker(self.p)), mine)
        # A second assistant in the same repo gets its own block and its own worktrees.
        other = project.load(shutil.copytree(self.p.root, self.p.root.parent / "second"))
        both = mine + block + host.cron_block(other)
        self.assertEqual(host.strip_block(both, host.cron_marker(other)), mine + block)
        self.assertNotEqual(host.workdir(self.p, "refresh"), host.workdir(other, "refresh"))

    def test_code_commits_since(self):
        root = host.git_root(self.p)
        start = sh(root, "rev-parse", "HEAD")
        write(root / "groundtruth" / "x.py", "x = 1\n")
        sh(root, "add", "-A")
        sh(root, "commit", "-qm", "code")
        write(root / "assistant" / "wiki" / "y.md", "y")
        sh(root, "add", "-A")
        sh(root, "commit", "-qm", "content")
        head = sh(root, "rev-parse", "HEAD")
        self.assertEqual(host.code_commits_since(root, start, head), 1)
        self.assertEqual(host.code_commits_since(root, head, head), 0)
        self.assertIsNone(host.code_commits_since(root, "0" * 40, head))


class RefreshTests(unittest.TestCase):
    def test_recapture_rereads_local_sources_and_flags_deleted_ones(self):
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        doc = write(Path(tmp.name) / "docs" / "hours.md", "# Hours\n\nOpen every day from 9am.\n")
        ingest.run(p, [str(doc.parent)], save=False)
        rel = next(s for s in (p.rel(x) for x in p.pages_dir.glob("hours*.md")))
        doc.write_text("# Hours\n\nOpen every day from 10am.\n")
        self.assertEqual(ingest.recapture(p, [rel]).updated, [rel])
        self.assertIn("10am", (p.root / rel).read_text())
        doc.unlink()
        self.assertEqual(ingest.recapture(p, [rel]).removed, [rel])

    def test_an_outage_isnt_mistaken_for_removals(self):
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        res = ingest.IngestResult(removed=["a", "b", "c", "d"])
        self.assertIn("outage", refresh._outage(p, res))
        self.assertIsNone(refresh._outage(p, ingest.IngestResult(removed=["a"])))

    def test_quick_targets_are_the_sources_facts_cite(self):
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        self.assertIn("sources/pages/locations-and-hours.md", refresh.fact_sources(p))
        self.assertNotIn("sources/pages/old-fees-flyer.md", refresh.fact_sources(p))


class WatchdogRepoTests(unittest.TestCase):
    def test_results(self):
        self.assertTrue(watchdog.alerts_result([])[0])
        ok, msg = watchdog.alerts_result([PAGE_X])
        self.assertFalse(ok)
        self.assertIn("wiki/x.md", msg)
        now = time.time()
        self.assertTrue(watchdog.refresh_result(now, int(now - 3600), "abc", 30)[0])
        self.assertFalse(watchdog.refresh_result(now, int(now - 40 * 3600), "abc", 30)[0])
        self.assertFalse(watchdog.refresh_result(now, None, "", 30)[0])
        self.assertIsNone(watchdog.ci_result([{"status": "in_progress"}], "abc1234")[0])
        self.assertTrue(watchdog.ci_result([{"status": "completed", "conclusion": "success"}], "abc1234")[0])
        self.assertFalse(watchdog.ci_result([{"status": "completed", "conclusion": "failure"}], "abc1234")[0])
        self.assertFalse(watchdog.ci_result([], "abc1234")[0])
        self.assertTrue(watchdog.code_lag_result(0, None, "abc1234", 72)[0])
        self.assertTrue(watchdog.code_lag_result(2, 10, "abc1234", 72)[0])
        self.assertFalse(watchdog.code_lag_result(2, 100, "abc1234", 72)[0])
        self.assertFalse(watchdog.code_lag_result(None, None, "abc1234", 72)[0])

    def test_health_reports_the_build_commit(self):
        from groundtruth import server
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"GT_BUILD_COMMIT": "abc1234"}):
            self.assertEqual(server.health(p)["commit"], "abc1234")


class LaunchStepsTests(unittest.TestCase):
    def test_only_the_host_can_be_checked_from_the_files(self):
        from groundtruth import progress
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        steps = progress.launch_steps(p)
        self.assertEqual([s.done for s in steps], [None, None, None, False, None])
        p.config["host"] = "box"
        self.assertTrue(progress.launch_steps(p)[3].done)


if __name__ == "__main__":
    unittest.main()
