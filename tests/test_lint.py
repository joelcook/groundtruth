import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load the linter module directly from scripts/ (not an importable package).
_spec = importlib.util.spec_from_file_location("lint_no_hardfacts", ROOT / "scripts" / "lint_no_hardfacts.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _lint_text(body: str) -> list[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("---\ntitle: t\n---\n\n" + body + "\n\n## Sources & Lineage\n\n| x | 2026-01-01 | $9 |\n")
        path = Path(fh.name)
    try:
        return lint.lint_file(path)
    finally:
        path.unlink()


class TestNoHardFacts(unittest.TestCase):
    def test_flags_dollar_amount(self):
        self.assertTrue(_lint_text("Printing costs $0.10 per page."))

    def test_flags_calendar_date(self):
        self.assertTrue(_lint_text("The sale is on July 4, 2026."))

    def test_clean_prose_passes(self):
        self.assertEqual(_lint_text("Ask the assistant for the current price with get_fee."), [])

    def test_frontmatter_and_lineage_ignored(self):
        # Dates/dollars in frontmatter and the lineage table must NOT trip the linter.
        self.assertEqual(_lint_text("This page explains the fine-free policy plainly."), [])

    def test_fact_ok_escape_hatch(self):
        body = "<!-- fact-ok: stable illustrative figure -->\nThe legacy code was $5 in 2019."
        self.assertEqual(_lint_text(body), [])


if __name__ == "__main__":
    unittest.main()
