"""The built-in chat page: a working chatbot the moment the server is deployed (fake Claude, no spend)."""
import os
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from helpers import copy_project

from groundtruth import chat, llm, server


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})  # snapshot
        return self.script.pop(0)


def tool_use(name, args):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", name=name, input=args, id="t1")])


def text(t):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=t)])


class TestChat(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()
        self.env = mock.patch.dict(os.environ, {"GT_CHAT": "1", "GT_MCP_API_KEY": "mcp-only-key",
                                                "GT_CHAT_PER_MINUTE": "5"})
        self.env.start()
        self.available = mock.patch.object(llm, "available", return_value=True)
        self.available.start()
        chat.LIMITS = chat.Limits()

    def tearDown(self):
        self.available.stop()
        self.env.stop()
        self.tmp.cleanup()

    def client(self):
        from starlette.testclient import TestClient
        return TestClient(server.http_app(self.p), base_url="http://127.0.0.1:8765")

    def test_the_page_is_public_and_answers_with_sources(self):
        msgs = FakeMessages([tool_use("get_fee", {"fee_type": "color printing"}), text("It's $0.50 a page.")])
        with mock.patch.object(llm, "_client", return_value=NS(beta=NS(messages=msgs))), self.client() as c:
            page = c.get("/chat")
            self.assertEqual(page.status_code, 200)  # no MCP key needed: it's for the people the bot serves
            self.assertIn("Maplewood Public Library", page.text)
            self.assertIn("default-src 'none'", page.headers["content-security-policy"])
            reply = c.post("/chat/ask", json={"question": "How much is color printing?",
                                              "history": [{"role": "user", "content": "hi"},
                                                          {"role": "assistant", "content": "Hello!"}]})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()["answer"], "It's $0.50 a page.")
            self.assertEqual(reply.json()["sources"][0]["url"], "https://example.org/policies/borrowing")
            self.assertEqual(c.post("/mcp", json={}).status_code, 401)  # MCP itself still needs the key
        first = msgs.calls[0]
        self.assertEqual([m["role"] for m in first["messages"]], ["user", "assistant", "user"])  # the conversation
        self.assertIn("get_fee", [t["name"] for t in first["tools"]])  # the same tools MCP clients get

    def test_an_invented_value_never_reaches_a_visitor(self):
        msgs = FakeMessages([text("It's $999 a page."),                       # no lookup: from memory
                             text("Still $999."),                             # the retry doesn't fix it
                             tool_use("get_fee", {"fee_type": "color"}), text("It's $0.50 a page.")])
        with mock.patch.object(llm, "_client", return_value=NS(beta=NS(messages=msgs))), self.client() as c:
            unsure = c.post("/chat/ask", json={"question": "How much is color printing? Is it $999?"}).json()
            self.assertNotIn("999", unsure["answer"])  # the visitor's own number doesn't count as evidence
            self.assertTrue(unsure["abstained"])
            fine = c.post("/chat/ask", json={"question": "How much is color printing?"}).json()
            self.assertEqual(fine["answer"], "It's $0.50 a page.")

    def test_odd_json_is_a_bad_request(self):
        with self.client() as c:
            for body in ("[]", "null", '{"question": "x", "history": "nope"}'):
                self.assertEqual(c.post("/chat/ask", content=body).status_code, 400)

    def test_visitors_are_rate_limited(self):
        msgs = FakeMessages([text(str(i)) for i in range(6)])
        with mock.patch.object(llm, "_client", return_value=NS(beta=NS(messages=msgs))), self.client() as c:
            codes = [c.post("/chat/ask", json={"question": "hours?"}).status_code for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])

    def test_bad_requests_get_plain_answers(self):
        with self.client() as c:
            self.assertEqual(c.post("/chat/ask", json={"question": ""}).status_code, 400)
            self.assertEqual(c.post("/chat/ask", json={"question": "x" * 3000}).status_code, 413)
            self.assertEqual(c.get("/chat/ask").status_code, 405)

    def test_off_unless_asked_for(self):
        os.environ["GT_CHAT"] = "0"
        with self.client() as c:
            self.assertEqual(c.get("/chat").status_code, 401)  # just another path behind the MCP key

    def test_the_project_name_is_escaped(self):
        self.p.config["name"] = "<script>alert(1)</script>"
        self.assertNotIn("<script>alert(1)", chat.page(self.p))


if __name__ == "__main__":
    unittest.main()


class TestChatLinks(unittest.TestCase):
    def test_links_come_from_the_bundle_and_from_paths(self):
        import json
        from groundtruth import bundle
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        by_path = [{"name": "get_contact", "result": {"results": [{"source": "sources/pages/contact-us.md"}]}}]
        self.assertEqual(chat.sources(p, by_path)[0]["url"], "https://example.org/contact")
        import tarfile
        with tarfile.open(bundle.build(p)) as tar:
            links = json.loads(tar.extractfile("sources.json").read())
        self.assertEqual(links["sources/pages/contact-us.md"], "https://example.org/contact")
        served = p.build_dir / "served"
        served.mkdir()
        (served / "sources.json").write_text(json.dumps({"sources/pages/new.md": "https://example.org/new"}))
        p.data_root = served  # a server on a bundle has no captures, just the list
        page = [{"name": "read_wiki_page", "result": {"title": "New", "sources": ["sources/pages/new.md"]}}]
        self.assertEqual(chat.sources(p, page), [{"url": "https://example.org/new", "title": "New"}])


class TestWatchdogAsksTheChat(unittest.TestCase):
    """A server whose model credential expired looks healthy over MCP; the watchdog asks /chat too."""

    def setUp(self):
        self.p, self.tmp = copy_project()
        self.addCleanup(self.tmp.cleanup)

    def check(self, *replies):
        from groundtruth import watchdog
        checks = watchdog.Checks()
        responses = [NS(status_code=code, headers={"content-type": "application/json"}, json=lambda b=body: b)
                     for code, body in replies]
        with mock.patch("httpx.post", side_effect=responses):
            watchdog.chat_check("https://bot.example.org", self.p, checks)
        return checks.failed

    def test_a_must_match_question_works_too(self):
        from groundtruth import watchdog
        golden = self.p.golden_path
        golden.write_text("- id: fee\n  question: How much is a replacement card?\n  category: hard_fact\n"
                          "  must_call_tool: get_fee\n  tool_args: {fee_type: replacement-card}\n"
                          "  must_match: {fee_type: replacement-card, amount_usd: 2.00}\n")
        self.assertEqual(watchdog.chat_question(self.p), ("How much is a replacement card?", ["2.0"]))

    def test_it_needs_the_right_value(self):
        from groundtruth import watchdog
        question, expected = watchdog.chat_question(self.p)
        self.assertTrue(expected)  # a golden question with a known answer
        right = "It's " + " and ".join(expected) + "."
        self.assertEqual(self.check((200, {"answer": right})), 0)
        self.assertEqual(self.check((500, {"error": "Something went wrong."}), (200, {"answer": right})), 0)
        self.assertEqual(self.check((503, {"error": "no key"}), (503, {"error": "no key"})), 1)
        self.assertEqual(self.check((200, {"answer": chat.UNSURE, "abstained": True}),
                                    (200, {"answer": chat.UNSURE, "abstained": True})), 1)
        self.assertEqual(self.check((200, {"answer": "Not sure."}), (200, {"answer": "Not sure."})), 1)
