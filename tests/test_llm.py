"""The live path, exercised with a fake Claude client (no network, no spend)."""
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from helpers import copy_project

from groundtruth import evals, llm, server


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})  # snapshot
        return self.script.pop(0)


def fake_client(script):
    msgs = FakeMessages(script)
    return NS(beta=NS(messages=msgs)), msgs


def tool_use(name, args, id_="t1"):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", name=name, input=args, id=id_)])


def text(t):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=t)])


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tool_loop_and_request_shape(self):
        client, msgs = fake_client([tool_use("get_fee", {"fee_type": "color printing"}), text("It's $0.50 a page.")])
        with mock.patch.object(llm, "_client", return_value=client):
            out = llm.agent("color printing cost?", "sys", server.tool_specs(self.p),
                            lambda n, a: server.call_tool(self.p, n, a, log=False))
        self.assertEqual(out["response"], "It's $0.50 a page.")
        self.assertEqual([{k: c[k] for k in ("name", "input")} for c in out["tool_calls"]],
                         [{"name": "get_fee", "input": {"fee_type": "color printing"}}])
        self.assertEqual(out["tool_calls"][0]["result"]["count"], 1)  # graders check what came back
        first, second = msgs.calls
        self.assertEqual(first["model"], llm.model())
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral"})
        result_block = second["messages"][-1]["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertIn("printing-color", result_block["content"])

    def test_refusal_is_reported(self):
        client, _ = fake_client([NS(stop_reason="refusal", content=[])])
        with mock.patch.object(llm, "_client", return_value=client):
            out = llm.agent("q", "sys", [], lambda n, a: {})
        self.assertEqual(out["stop_reason"], "refusal")

    def test_live_tier_grades_with_fake_model(self):
        script = []
        for item in evals.checks.load_golden(self.p):
            if item.get("must_call_tool"):
                script.append(tool_use(item["must_call_tool"], item.get("tool_args") or {}))
            if item.get("must_cite_page"):
                script.append(tool_use("read_wiki_page", {"path_or_slug": item["must_cite_page"]}))
            script.append(text(" ".join(item.get("must_contain", [])) + " (from the tools)"))
        client, _ = fake_client(script)
        with mock.patch.object(llm, "_client", return_value=client), \
                mock.patch.object(llm, "available", return_value=True):
            self.assertEqual(evals.run(self.p, "live", judge=False, quiet=True), 0)


if __name__ == "__main__":
    unittest.main()
