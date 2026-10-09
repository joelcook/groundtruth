import functools
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import copy_project, write

from groundtruth import ingest

SITE = {
    "index.html": "<html><head><title>Home</title></head><body><nav><a href='/a.html'>A</a></nav>"
                  "<main><h1>Welcome</h1><p>" + "Plenty of real words about our help center. " * 4 +
                  "</p><a href='/a.html?utm_source=x'>a</a> <a href='/b.html'>b</a> "
                  "<a href='/private/secret.html'>s</a></main><footer>footer junk</footer></body></html>",
    "a.html": "<html><body><main><h1>Page A</h1><p>" + "Alpha content for page A. " * 6 + "</p></main></body></html>",
    "b.html": "<html><body><main><h1>Page B</h1><p>" + "Bravo content for page B. " * 6 + "</p></main></body></html>",
    "private/secret.html": "<html><body><main><p>" + "secret " * 30 + "</p></main></body></html>",
    "robots.txt": "User-agent: *\nDisallow: /private/\n",
}


class TestHtml(unittest.TestCase):
    def test_strips_chrome(self):
        title, md = ingest.html_to_markdown(SITE["index.html"], "https://x.test/")
        self.assertEqual(title, "Welcome")
        self.assertNotIn("footer junk", md)
        self.assertIn("help center", md)

    def test_slug(self):
        self.assertEqual(ingest.slug_for("https://x.test/help/Reset-Password.html"), "help-reset-password")
        self.assertEqual(ingest.slug_for("https://x.test/"), "index")


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_folder_ingest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d) / "faq.md", "# FAQ\n\nAnswers to common questions about everything.\n")
            write(Path(d) / "page.html", "<html><body><main><h1>Hi</h1><p>Hello there, friend.</p></main></body></html>")
            write(Path(d) / "image.png", "x")
            first = ingest.run(self.p, [d])
            self.assertEqual(len(first.new), 2)
            self.assertEqual(len(first.skipped), 1)
            second = ingest.run(self.p, [d])
            self.assertEqual(len(second.unchanged), 2)
            self.assertFalse(second.changed)
            self.assertIn(d, self.p.ingest_sources)  # remembered in groundtruth.yaml

    def test_crawl_respects_robots_and_dedupes(self):
        with tempfile.TemporaryDirectory() as site:
            for name, html in SITE.items():
                write(Path(site) / name, html)
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=site)
            handler.log_message = lambda *a, **k: None
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}/"
                result = ingest.run(self.p, [url], delay=0, save=False)
            finally:
                httpd.shutdown()
        names = sorted(Path(n).name for n in result.new)
        self.assertEqual(names, ["a.md", "b.md", "index.md"])
        self.assertTrue(any("robots" in s for s in result.skipped))
        text = (self.p.pages_dir / "a.md").read_text()
        self.assertIn("source_url:", text)
        self.assertIn("sha256:", text)


if __name__ == "__main__":
    unittest.main()
