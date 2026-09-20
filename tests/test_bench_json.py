"""`--json` has to be pipeable, and has to say when a row cannot be quoted.

The page carries a model-backed row. Whether that row is a measurement of the
model it names, or of the heuristic it silently fell back to, is the difference
between a number and a fabrication -- so it belongs in the machine-readable
output, not only in a warning on stderr that a caller may never see.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bench.run", *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=600)


class TestBenchJson(unittest.TestCase):
    def test_stdout_is_json_and_nothing_else(self) -> None:
        r = run("--solver", "noop", "--json", "--quiet")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        payload = json.loads(r.stdout)          # raises if anything else leaked
        self.assertEqual(payload["cases"], 71)
        self.assertEqual(len(payload["reports"]), 1)

    def test_the_human_header_still_appears_on_stderr(self) -> None:
        r = run("--solver", "noop", "--json", "--quiet")
        self.assertIn("real drift cases", r.stderr)

    def test_an_offline_row_is_marked_clean(self) -> None:
        r = run("--solver", "agent", "--json", "--quiet")
        row = json.loads(r.stdout)["reports"][0]
        self.assertTrue(row["clean"])
        self.assertEqual(row["fallbacks"], 0)
        self.assertEqual((row["adapt"], row["ask"], row["unsafe"]), ("7/27", "35/44", 2))

    def test_a_model_row_without_credentials_is_marked_unquotable(self) -> None:
        """The exact failure the page's '* partial' footnote exists for."""
        r = run("--solver", "groq", "--json", "--quiet")
        row = json.loads(r.stdout)["reports"][0]
        self.assertFalse(row["clean"])
        self.assertEqual(row["fallbacks"], row["total"])
        self.assertIn("GROQ_API_KEY", row["last_error"])
        self.assertIn("WARNING", r.stderr)

    def test_solver_is_repeatable_rather_than_comma_separated(self) -> None:
        r = run("--solver", "noop", "--solver", "agent", "--json", "--quiet")
        names = [x["solver"] for x in json.loads(r.stdout)["reports"]]
        self.assertEqual(names, ["noop", "agent-heuristic"])


if __name__ == "__main__":
    unittest.main()
