import os
import unittest

import anyio
from helpers import copy_project

from groundtruth import server


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p, cls.tmp = copy_project()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_tools_are_generated_from_yaml(self):
        srv = server.build_server(self.p)
        tools = {t.name: t for t in anyio.run(srv.list_tools)}
        self.assertEqual(len(tools), 10)
        props = tools["get_hours"].inputSchema["properties"]
        self.assertIn("branch", props)
        self.assertIn("day", props)
        self.assertTrue(tools["get_fee"].annotations.readOnlyHint)

    def test_call_over_mcp(self):
        srv = server.build_server(self.p)
        res = anyio.run(lambda: srv.call_tool("get_fee", {"fee_type": "color printing"}))
        self.assertIn("printing-color", str(res))

    def test_instructions_name_every_fact_tool(self):
        text = server.instructions(self.p)
        for tool in ("get_fee", "get_hours", "get_event"):
            self.assertIn(tool, text)

    def test_http_auth_and_health(self):
        from starlette.testclient import TestClient
        os.environ["GT_MCP_API_KEY"] = "secret-key"
        try:
            client = TestClient(server.http_app(self.p))
            health = client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(set(health.json()), set(server.PUBLIC_HEALTH) | {"checks"})  # no detail without the key
            self.assertNotIn("probe", health.json())
            full = client.get("/healthz", headers={"Authorization": "Bearer secret-key"}).json()
            self.assertEqual(full["facts"]["hours"], 21)
            self.assertTrue(full["probe"])
            self.assertEqual(client.post("/mcp", json={}).status_code, 401)
            self.assertEqual(client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code, 401)
        finally:
            del os.environ["GT_MCP_API_KEY"]

    def test_transport_security_policy(self):
        os.environ.pop("GT_MCP_API_KEY", None)
        os.environ.pop("GT_ALLOWED_HOSTS", None)
        self.assertTrue(server._transport_security().enable_dns_rebinding_protection)
        os.environ["GT_ALLOWED_HOSTS"] = "mcp.example.org"
        try:
            settings = server._transport_security()
            self.assertIn("mcp.example.org", settings.allowed_hosts)
        finally:
            del os.environ["GT_ALLOWED_HOSTS"]
        os.environ["GT_MCP_API_KEY"] = "k"
        try:
            self.assertFalse(server._transport_security().enable_dns_rebinding_protection)
        finally:
            del os.environ["GT_MCP_API_KEY"]


class TestWatchdog(unittest.TestCase):
    def test_stdio_round_trip(self):
        from groundtruth import watchdog
        p, tmp = copy_project()
        try:
            self.assertEqual(watchdog.run(p), 0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
