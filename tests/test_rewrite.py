"""./gt rewrite: the headless Claude Code loop, end to end with a fake `claude` CLI."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from helpers import LIBRARY, write

from groundtruth import alerts, compile as comp, cover, host, project, reconcile, review, rewrite

NOW = 1_800_000_000.0


def event(**fields) -> str:
    return json.dumps(fields)


def rate_limit(status: str, resets: int, five_hour: float = 0.1, seven_day: float = 0.1) -> str:
    return event(type="rate_limit_event", rate_limit_info={
        "status": status, "resetsAt": resets, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": five_hour, "resetsAt": resets},
                           "seven_day": {"utilization": seven_day, "resetsAt": resets + 86400}}})


def result(text: str = "done", is_error: bool = False) -> str:
    return event(type="result", subtype="success", is_error=is_error, result=text,
                 total_cost_usd=0.02, modelUsage={"claude-haiku-5-5": {}})


INIT = event(type="system", subtype="init", model="claude-haiku-5-5")


class ParseStreamTests(unittest.TestCase):
    def test_success_reads_model_and_text(self):
        res = rewrite.parse_stream([INIT, rate_limit("allowed", int(NOW) + 3600), result("Rewrote it.")], 0.8, NOW)
        self.assertEqual((res.model_id, res.text), ("claude-haiku-5-5", "Rewrote it."))
        self.assertFalse(res.is_error or res.limited or res.auth_error)
        self.assertIsNone(res.pause_until)

    def test_rejected_limit_pauses_until_reset(self):
        resets = int(NOW) + 7200
        res = rewrite.parse_stream([INIT, rate_limit("rejected", resets), result("limit", True)], 0.8, NOW)
        self.assertTrue(res.limited)
        self.assertEqual(res.pause_until, resets)

    def test_limit_text_without_event_retries_soon(self):
        lines = [INIT, rate_limit("allowed", int(NOW) + 4 * 3600), result("You've hit your limit · resets 9pm", True)]
        res = rewrite.parse_stream(lines, 0.8, NOW)
        self.assertTrue(res.limited)
        self.assertEqual(res.pause_until, int(NOW + rewrite.DEFAULT_PAUSE_SECONDS))

    def test_window_past_threshold_pauses_after_the_page(self):
        resets = int(NOW) + 3600
        res = rewrite.parse_stream([INIT, rate_limit("allowed", resets, five_hour=0.85), result()], 0.8, NOW)
        self.assertFalse(res.limited)
        self.assertEqual(res.pause_until, resets)
        self.assertIn("five_hour", res.pause_reason)

    def test_later_window_wins_when_both_are_past_threshold(self):
        resets = int(NOW) + 3600
        res = rewrite.parse_stream([INIT, rate_limit("allowed", resets, 0.9, 0.95), result()], 0.8, NOW)
        self.assertEqual(res.pause_until, resets + 86400)

    def test_logged_out_is_an_auth_error(self):
        res = rewrite.parse_stream([result("Not logged in · Please run /login", True)], 0.8, NOW)
        self.assertTrue(res.auth_error)
        self.assertFalse(res.limited)

    def test_missing_result_is_an_error(self):
        self.assertTrue(rewrite.parse_stream([INIT, "not json"], 0.8, NOW).is_error)


class SmallPartsTests(unittest.TestCase):
    def test_model_versions(self):
        self.assertEqual(rewrite.model_version("claude-haiku-5-5"), (5, 5))
        self.assertEqual(rewrite.model_version("claude-haiku-4-5-20251001"), (4, 5))
        self.assertEqual(rewrite.model_version("claude-sonnet-5"), (5, 0))
        self.assertIsNone(rewrite.model_version("haiku"))
        self.assertLess(rewrite.model_version("claude-haiku-4-5"), rewrite.HAIKU_MIN_VERSION)
        self.assertEqual(rewrite.display_name("claude-haiku-5-5"), "Claude Haiku 5.5")

    def test_stamp_touches_only_frontmatter(self):
        page = "---\ntitle: X\nlast_compiled: 2026-06-04\ncompiled_by: old\n---\n\nBody says last_compiled: keep\n"
        out = rewrite.stamp_frontmatter(page, "2026-10-11", "claude-haiku-5-5")
        self.assertIn("last_compiled: 2026-10-11\ncompiled_by: claude-haiku-5-5\n---", out)
        self.assertIn("Body says last_compiled: keep", out)
        added = rewrite.stamp_frontmatter("---\ntitle: X\n---\nBody\n", "2026-10-11", "m")
        self.assertTrue(added.startswith("---\ntitle: X\nlast_compiled: 2026-10-11\ncompiled_by: m\n---"))

    def test_verdicts(self):
        self.assertEqual(rewrite.parse_verdict('{"verdict": "pass", "unsupported": []}'), ("pass", []))
        verdict, problems = rewrite.parse_verdict(
            'Sure:\n```json\n{"verdict": "fail", "unsupported": [{"claim": "Open on Mondays", "why": "closed"}]}\n```')
        self.assertEqual(verdict, "fail")
        self.assertIn("Open on Mondays", problems[0])
        self.assertEqual(rewrite.parse_verdict('{"verdict": "pass", "unsupported": ["x"]}')[0], "fail")
        self.assertTrue(rewrite.parse_verdict('{"verdict": "fail", "unsupported": []}')[1])
        self.assertIsNone(rewrite.parse_verdict("Looks fine to me!")[0])
        self.assertIsNone(rewrite.parse_verdict('{"verdict": "maybe"}')[0])

    def test_the_checker_can_only_read(self):
        self.assertNotIn("Edit", rewrite.CHECK_TOOLS)
        self.assertIn("Edit", rewrite.CHECK_DENIED)
        self.assertIn("Bash", rewrite.DENIED_TOOLS)
        self.assertIn("Read(./.env)", rewrite.DENIED_TOOLS)
        self.assertEqual(rewrite.writer_tools(["wiki/a.md"]), "Read Glob Grep Edit(wiki/a.md) Write(wiki/a.md)")

    def test_claude_gets_only_what_it_needs_from_the_environment(self):
        with mock.patch.dict(os.environ, {"GT_MCP_API_KEY": "s3cret", "GITHUB_TOKEN": "t", "HOME": "/h",
                                          "CLAUDE_CODE_OAUTH_TOKEN": "c", "ANTHROPIC_API_KEY": "a"}):
            env = rewrite.claude_env()
        self.assertNotIn("GT_MCP_API_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual((env["HOME"], env["CLAUDE_CODE_OAUTH_TOKEN"], env["ANTHROPIC_API_KEY"]), ("/h", "c", "a"))

    def test_commits_skip_the_watchers_rescrape(self):
        self.assertTrue(f"{rewrite.COMMIT_PREFIX} wiki/x.md".startswith(host.BOT_PREFIXES))


# A stand-in for `claude -p`: probes answer OK, the writer edits the page named in
# its prompt, the checker answers with FAKE_VERDICT. FAKE_STRAY / FAKE_FACT misbehave.
FAKE_CLAUDE = r'''
import json, os, re, sys
args = sys.argv[1:]
alias = args[args.index("--model") + 1]
model = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5"}.get(alias, alias)
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"alias": alias, "first": prompt.splitlines()[0], "tools": args[args.index("--allowedTools") + 1]}) + "\n")
print(json.dumps({"type": "system", "subtype": "init", "model": model}))
if prompt.startswith("Reply with just OK"):
    text = "OK"
elif prompt.startswith("Check"):
    text = os.environ.get("FAKE_VERDICT", '{"verdict": "pass", "unsupported": []}')
elif prompt.startswith("You maintain this repo's knowledge base"):
    src = re.search(r"captured: `([^`]+)`", prompt).group(1)
    page = re.search(r"at exactly `([^`]+)`", prompt).group(1)
    mode = os.environ.get("FAKE_COVER", "")
    if mode == "skip":
        text = "SKIP: it's a navigation page"
    else:
        rel_src = src.split("/", 1)[1]
        open(page, "w").write(
            "---\ntitle: Gift cards\npage_type: entity\nlast_compiled: 2026-10-12\ncompiled_by: m\n"
            f"sources:\n  - {rel_src}\nstatus: published\ntags: [gift, card]\n---\n\n# Gift cards\n\n"
            "The library sells gift cards at the circulation desk.\n\n## Sources & Lineage\n\n"
            f"| Source | Captured | URL |\n|---|---|---|\n| [[{rel_src}]] | 2026-10-12 | https://example.org/gift |\n")
        if mode != "noq":
            golden = "assistant/evals/golden.yaml"
            stem = page.rsplit("/", 1)[1][:-3]
            body = open(golden).read()
            open(golden, "w").write(body + f"\n- id: gift-cards\n  question: \"Does the library sell gift cards?\"\n"
                                    f"  category: narrative\n  must_cite_page: {stem}\n")
        text = "Added a page about gift cards."
elif prompt.startswith("You maintain this repo's facts"):
    for path, old, new in json.loads(os.environ.get("FAKE_EDITS", "[]")):  # [file, from, to]
        text = open(path).read()
        open(path, "w").write(text.replace(old, new))
    text = "Updated the changed values."
else:
    page = re.search(r"Rewrite one page: `([^`]+)`", prompt).group(1)
    body = open(page).read()
    extra = "You can renew most items online." + (" It costs $5." if os.environ.get("FAKE_FACT") else "")
    body = body.replace("## Sources & Lineage", extra + "\n\n## Sources & Lineage", 1)
    open(page, "w").write(body)
    if os.environ.get("FAKE_STRAY"):
        open("stray.txt", "w").write("oops")
    text = "Added a line about renewing online."
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text, "modelUsage": {model: {}}}))
'''


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


class RewriteLoopTests(unittest.TestCase):
    """A real git repo with the library in a subfolder, a changed source, and the fake claude."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        shutil.copytree(LIBRARY, self.root / "assistant",
                        ignore=shutil.ignore_patterns("build", "logs", "reports", "*-tools.json"))
        write(self.root / ".gitignore", "build/\nlogs/\nreports/\n**/evals/runs/*-tools.json\n")
        self.p = project.load(self.root / "assistant")
        # A source only one wiki page cites (no facts), so a checked rewrite can settle its review.
        self.app = "sources/pages/app-guide.md"
        write(self.p.root / self.app, "---\nsource_url: https://example.org/app\nscraped_at: 2026-06-01\n---\n\n"
                                      "# The app\n\nDownload the app and sign in with your card.\n")
        self.page = "wiki/journeys/using-the-elibrary-app.md"
        text = (self.p.root / self.page).read_text()
        (self.p.root / self.page).write_text(text.replace("sources:\n", f"sources:\n  - {self.app}\n", 1))
        review.mark_reviewed(self.p)
        sh(self.root, "init", "-q", "-b", "main")
        sh(self.root, "config", "user.email", "t@example.org")
        sh(self.root, "config", "user.name", "t")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "start")
        fake = Path(self.tmp.name) / "claude"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        fake.chmod(0o755)
        self.calls = Path(self.tmp.name) / "calls.jsonl"
        self.env = mock.patch.dict(os.environ, {"GT_CLAUDE": str(fake), "FAKE_LOG": str(self.calls),
                                                "GT_HOST_STATE": str(Path(self.tmp.name) / "state")})
        self.env.start()
        # the fake claude reads its script from FAKE_* variables; real runs pass only what claude needs
        self.prefixes = mock.patch.object(rewrite, "CLAUDE_ENV_PREFIXES", rewrite.CLAUDE_ENV_PREFIXES + ("FAKE_",))
        self.prefixes.start()
        for key in ("GT_REWRITE_MODEL", "GT_REWRITE_CHECK_MODEL", "GT_HOST_JOB", "FAKE_VERDICT", "FAKE_FACT",
                    "FAKE_STRAY", "FAKE_EDITS", "FAKE_COVER", "CLAUDE_CODE_OAUTH_TOKEN"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.prefixes.stop()
        self.env.stop()
        self.tmp.cleanup()

    def change(self, rel: str, extra: str) -> None:
        path = self.p.root / rel
        path.write_text(path.read_text().replace("scraped_at: 2026-06-01", f"scraped_at: {dt.date.today()}")
                        + f"\n{extra}\n")
        sh(self.root, "commit", "-qam", f"refresh: {rel}")

    def calls_made(self) -> list[dict]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def subjects(self) -> list[str]:
        return sh(self.root, "log", "--format=%s").splitlines()

    def test_a_checked_rewrite_commits_and_settles_a_wiki_only_source(self):
        self.change(self.app, "You can also renew from the app.")
        self.assertIn(self.page, rewrite.todo(self.p, {}))
        self.assertEqual(rewrite.run(self.p), 0)
        self.assertEqual(self.subjects()[0], f"wiki: rewrite assistant/{self.page} from current sources")
        body = sh(self.root, "log", "-1", "--format=%b")
        self.assertIn("Claims checked by: claude-sonnet-5-5", body)
        self.assertIn(f"Recorded as reviewed: {self.app}", body)
        self.assertNotIn(self.app, [c.source for c in review.changes(self.p)])  # the review is recorded
        page = (self.p.root / self.page).read_text()
        self.assertIn(f"last_compiled: {dt.date.today()}", page)
        self.assertIn("compiled_by: claude-haiku-5-5", page)
        self.assertEqual(sh(self.root, "status", "--porcelain"), "")
        calls = self.calls_made()
        writer = next(c for c in calls if c["first"].startswith("You maintain"))
        self.assertEqual(writer["alias"], "haiku")
        self.assertEqual(writer["tools"], f"Read Glob Grep Edit(assistant/{self.page}) Write(assistant/{self.page})")
        checker = next(c for c in calls if c["first"].startswith("Check"))
        self.assertEqual((checker["alias"], checker["tools"]), ("sonnet", rewrite.CHECK_TOOLS))
        self.assertEqual(rewrite.todo(self.p, rewrite.load_state(rewrite.state_path(self.p))), {})

    def fees_change(self, edits):
        policy = self.p.root / "sources/pages/borrowing-policy.md"
        policy.write_text(policy.read_text().replace("$2.00 per card", "$3.00 per card"))
        sh(self.root, "commit", "-qam", "refresh: card fee went up")
        os.environ["FAKE_EDITS"] = json.dumps(edits)

    def test_a_changed_fact_is_updated_checked_and_reviewed(self):
        self.fees_change([["assistant/facts/fees.yaml", "amount_usd: 2.00", "amount_usd: 3.00"],
                          ["assistant/evals/golden.yaml", 'must_contain: ["2"]', 'must_contain: ["3"]']])
        self.assertIn("sources/pages/borrowing-policy.md", reconcile.todo(self.p, {}))
        self.assertEqual(rewrite.run(self.p), 0)
        subjects = self.subjects()
        self.assertIn("facts: update fees.yaml from sources/pages/borrowing-policy.md", subjects)
        body = sh(self.root, "log", "--format=%b", "-1", "--grep=^facts: update")
        self.assertIn("amount_usd: 3.0", body)
        self.assertIn("Rows checked by: claude-sonnet-5-5", body)
        self.assertEqual(review.changes(self.p), [])  # facts and all four pages checked: reviewed
        self.assertIn("amount_usd: 3.00", (self.p.root / "facts/fees.yaml").read_text())
        self.assertEqual(sh(self.root, "status", "--porcelain"), "")

    def test_a_value_the_source_doesnt_state_never_ships(self):
        self.fees_change([["assistant/facts/fees.yaml", "amount_usd: 2.00", "amount_usd: 4.00"]])
        rewrite.run(self.p)
        self.assertNotIn("facts: update", " ".join(self.subjects()))
        self.assertIn("amount_usd: 2.00", (self.p.root / "facts/fees.yaml").read_text())
        state = rewrite.load_state(rewrite.state_path(self.p))
        self.assertIn("'4.0'", state["facts_failed"]["sources/pages/borrowing-policy.md"]["reason"])
        self.assertIn("facts would not update", alerts.log_path(self.p).read_text())

    def test_leaving_a_stale_fact_alone_isnt_an_update(self):
        self.fees_change([])  # the writer changes nothing, though the source now says $3.00
        rewrite.run(self.p)
        self.assertIn("sources/pages/borrowing-policy.md", [c.source for c in review.changes(self.p)])
        state = rewrite.load_state(rewrite.state_path(self.p))
        self.assertIn("2.0", state["facts_failed"]["sources/pages/borrowing-policy.md"]["reason"])

    def new_source(self):
        write(self.p.root / "sources/pages/gift-cards.md",
              "---\nsource_url: https://example.org/gift\ncaptured: 2026-10-12\n---\n\n# Gift cards\n\n"
              "Gift cards are sold at the circulation desk at Central.\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-qm", "refresh: a new page appeared")

    def test_a_new_source_gets_a_page_a_question_and_a_review(self):
        self.new_source()
        comp.write_drafts(self.p)  # what ./gt compile leaves behind
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-qm", "drafts")
        self.assertIn("sources/pages/gift-cards.md", cover.todo(self.p, {}))
        self.assertEqual(rewrite.run(self.p), 0)
        self.assertIn("wiki: cover sources/pages/gift-cards.md", self.subjects())
        self.assertTrue((self.p.root / "wiki/gift-cards.md").exists())
        self.assertIn("gift-cards", self.p.golden_path.read_text())
        self.assertFalse([d for d in (self.p.wiki_dir / "drafts").glob("*.md")])  # the draft gave way
        self.assertIn("sources/pages/gift-cards.md", review.load_lock(self.p))  # its review is recorded
        self.assertEqual(cover.todo(self.p, rewrite.load_state(rewrite.state_path(self.p))), [])
        self.assertEqual(sh(self.root, "status", "--porcelain"), "")

    def test_a_source_with_nothing_to_say_is_skipped_until_it_changes(self):
        os.environ["FAKE_COVER"] = "skip"
        self.new_source()
        head = sh(self.root, "rev-parse", "HEAD")
        rewrite.run(self.p)
        self.assertEqual(sh(self.root, "rev-parse", "HEAD"), head)
        state = rewrite.load_state(rewrite.state_path(self.p))
        self.assertIn("navigation", state["cover_skipped"]["sources/pages/gift-cards.md"]["why"])
        self.assertEqual(cover.todo(self.p, state), [])

    def test_a_page_without_a_question_doesnt_ship(self):
        os.environ["FAKE_COVER"] = "noq"
        self.new_source()
        rewrite.run(self.p)
        self.assertNotIn("wiki: cover", " ".join(self.subjects()))
        self.assertFalse((self.p.root / "wiki/gift-cards.md").exists())
        self.assertIn("a new source would not get a page", alerts.log_path(self.p).read_text())

    def test_a_person_can_keep_facts_for_themselves(self):
        cfg = self.p.root / "groundtruth.yaml"
        cfg.write_text(cfg.read_text() + "\nfacts_updates: human\n")
        sh(self.root, "commit", "-qam", "facts stay with a person")
        self.p = project.load(self.p.root)
        self.change("sources/pages/programs.md", "Story time moved to the east room.")
        self.assertEqual(rewrite.run(self.p), 0)
        rewritten = [s for s in self.subjects() if s.startswith("wiki: rewrite assistant/")]
        self.assertEqual(len(rewritten), 2)  # both pages that cite it
        self.assertIn("sources/pages/programs.md", [c.source for c in review.changes(self.p)])
        state = rewrite.load_state(rewrite.state_path(self.p))
        self.assertEqual(rewrite.todo(self.p, state), {})  # checked pages aren't redone while facts wait

    def test_an_unsupported_claim_sends_the_page_back_then_alerts(self):
        os.environ["FAKE_VERDICT"] = '{"verdict": "fail", "unsupported": [{"claim": "renew online", "why": "no"}]}'
        self.change(self.app, "Changed.")
        head = sh(self.root, "rev-parse", "HEAD")
        self.assertEqual(rewrite.run(self.p), 0)
        self.assertEqual(sh(self.root, "rev-parse", "HEAD"), head)  # nothing kept
        self.assertEqual(sh(self.root, "status", "--porcelain"), "")
        writers = [c["alias"] for c in self.calls_made() if c["first"].startswith("You maintain")]
        self.assertEqual(writers, ["haiku", "sonnet"])  # one retry on sonnet
        state = rewrite.load_state(rewrite.state_path(self.p))
        self.assertIn(self.page, state["failed"])
        self.assertIn("renew online", alerts.log_path(self.p).read_text())

    def test_facts_in_prose_and_stray_edits_never_land(self):
        os.environ.update({"FAKE_FACT": "1", "FAKE_STRAY": "1"})
        self.change(self.app, "Changed.")
        head = sh(self.root, "rev-parse", "HEAD")
        rewrite.run(self.p)
        self.assertEqual(sh(self.root, "rev-parse", "HEAD"), head)
        self.assertFalse((self.root / "stray.txt").exists())
        state = rewrite.load_state(rewrite.state_path(self.p))
        self.assertIn("money", state["failed"][self.page]["reason"])
        self.assertFalse([c for c in self.calls_made() if c["first"].startswith("Check")])  # gates come first

    def test_dry_run_keeps_nothing(self):
        self.change(self.app, "Changed.")
        head = sh(self.root, "rev-parse", "HEAD")
        self.assertEqual(rewrite.run(self.p, dry_run=True), 0)
        self.assertEqual(sh(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(sh(self.root, "status", "--porcelain"), "")
        self.assertIn(self.app, [c.source for c in review.changes(self.p)])

    def test_refuses_a_dirty_checkout(self):
        write(self.root / "notes.txt", "mine")
        self.assertEqual(rewrite.run(self.p), 2)
        self.assertTrue((self.root / "notes.txt").exists())

    def test_a_usage_pause_holds_until_the_window_resets(self):
        self.change(self.app, "Changed.")
        path = rewrite.state_path(self.p)
        rewrite.save_state(path, {"paused_until": int(1e10), "pause_reason": "five_hour window at 90%"})
        self.assertEqual(rewrite.run(self.p), 0)
        self.assertFalse(self.calls_made())
        self.assertEqual(rewrite.run(self.p, force=True), 0)
        self.assertTrue(self.calls_made())

    def test_prompt_names_the_page_its_sources_and_the_tools(self):
        prompt = rewrite.build_prompt(self.p, self.page, ["it changed"], "claude-haiku-5-5", "2026-10-11",
                                      ["broken link"])
        self.assertIn(f"Edit only `assistant/{self.page}`", prompt)
        self.assertIn(f"`assistant/{self.app}`", prompt)
        self.assertIn("compiled_by: claude-haiku-5-5", prompt)
        self.assertIn("`get_program`", prompt)
        self.assertIn("- broken link", prompt)
        self.assertIn("- it changed", prompt)


class QuietHoursTests(unittest.TestCase):
    def test_windows(self):
        quiet = host.parse_quiet("02:40-03:50")
        self.assertTrue(host.in_quiet_hours(quiet, dt.datetime(2026, 10, 11, 3, 0)))
        self.assertFalse(host.in_quiet_hours(quiet, dt.datetime(2026, 10, 11, 3, 50)))
        overnight = host.parse_quiet("23:00-01:00")
        self.assertTrue(host.in_quiet_hours(overnight, dt.datetime(2026, 10, 11, 0, 30)))
        self.assertFalse(host.in_quiet_hours(host.parse_quiet(""), dt.datetime(2026, 10, 11, 3, 0)))

    def test_default_follows_the_refresh_time(self):
        with mock.patch.dict(os.environ, {"GT_REFRESH_AT": "04:30"}):
            os.environ.pop("GT_REWRITE_QUIET_HOURS", None)
            self.assertEqual(host.quiet_hours(), (dt.time(4, 10), dt.time(5, 20)))


class EnvFileTests(unittest.TestCase):
    def test_reads_the_token_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp) / ".env", textwrap.dedent("""\
                # comment
                GT_DATA_TOKEN=
                export CLAUDE_CODE_OAUTH_TOKEN='tok-123'
                BAD LINE
                """))
            values = host.read_env_file(path)
        self.assertEqual(values["CLAUDE_CODE_OAUTH_TOKEN"], "tok-123")
        self.assertEqual(values["GT_DATA_TOKEN"], "")
        self.assertEqual(host.read_env_file(Path("/nonexistent/.env")), {})


if __name__ == "__main__":
    unittest.main()
