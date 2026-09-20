"""The page ships its own copy of the benchmark, so it must be the same data.

A visitor stepping through the 71 cases in the browser is being told these are
the cases the published table was measured on. If docs/cases.json drifts from
data/stripe-drift-v1.json.gz, that claim quietly stops being true, and nothing
else in the suite would notice.

Regenerate it with:

    python3 -c "import gzip,json,io; \
      d=json.loads(gzip.open('data/stripe-drift-v1.json.gz').read()); \
      io.open('docs/cases.json','w').write( \
        json.dumps(d, sort_keys=True, separators=(',',':')))"
"""

from __future__ import annotations

import gzip
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "stripe-drift-v1.json.gz"
SHIPPED = ROOT / "docs" / "cases.json"


class TestShippedCasesMatchTheBenchmark(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = json.loads(gzip.open(SOURCE).read())
        cls.shipped = json.loads(SHIPPED.read_text(encoding="utf-8"))

    def test_the_page_ships_the_same_data_the_benchmark_runs(self) -> None:
        self.assertEqual(self.shipped, self.source,
                         "docs/cases.json has drifted from the benchmark data; "
                         "regenerate it rather than editing it by hand")

    def test_every_case_can_be_resolved_to_both_contracts(self) -> None:
        contracts = self.shipped["contracts"]
        for case in self.shipped["cases"]:
            for version in (case["from_version"], case["to_version"]):
                key = f"{version}|{case['operation']}"
                self.assertIn(key, contracts, f"{case['case_id']}: missing {key}")

    def test_the_shipped_file_stays_small_enough_to_fetch(self) -> None:
        # It is fetched only on demand, but a page asset that grows without
        # anyone noticing is how a fast page stops being one.
        size = len(gzip.compress(SHIPPED.read_bytes()))
        self.assertLess(size, 120_000, f"gzipped to {size} bytes")


if __name__ == "__main__":
    unittest.main()


class TestPublishedFiguresMatchAStoredRun(unittest.TestCase):
    """Nine copies of the same figures across two files is how a page drifts.

    Every number quoted for a solver is tied back to the run that produced it,
    so changing one and forgetting the rest fails here rather than shipping.
    """

    RUN = ROOT / "data" / "runs" / "2026-09-20-groq-gpt-oss-120b.json"
    PAGE = ROOT / "docs" / "index.html"
    README = ROOT / "README.md"

    @classmethod
    def setUpClass(cls) -> None:
        reports = json.loads(cls.RUN.read_text(encoding="utf-8"))["reports"]
        cls.rows = {r["solver"]: r for r in reports}
        cls.page = cls.PAGE.read_text(encoding="utf-8")
        cls.readme = cls.README.read_text(encoding="utf-8")

    def test_the_page_table_quotes_the_run(self) -> None:
        for solver, label in [("agent-heuristic", "agent, gated"),
                              ("agent-groq:openai/gpt-oss-120b", "agent + model, gated *")]:
            r = self.rows[solver]
            row = (f'<tr><td class="solver">{label}</td>'
                   f'<td class="mval">{r["adapt"]}</td>'
                   f'<td class="mval">{r["ask"]}</td>'
                   f'<td><span class="unsafe-n" data-zero="false">{r["unsafe"]}</span></td></tr>')
            self.assertIn(row, self.page, f"{solver}: page row does not match the run")

    def test_the_readme_tables_quote_the_run(self) -> None:
        for solver in ("agent-heuristic", "agent-groq:openai/gpt-oss-120b"):
            r = self.rows[solver]
            pattern = (rf"^{re.escape(solver)}\s+{re.escape(r['adapt'])}\s+"
                       rf"{re.escape(r['ask'])}\s+{r['unsafe']}\s+\d+%")
            found = re.findall(pattern, self.readme, re.M)
            self.assertTrue(found, f"{solver}: no README row matches the run")
            self.assertEqual(len(found), 2 if solver == "agent-heuristic" else 2,
                             f"{solver}: expected both result tables to agree")

    def test_the_footnote_states_how_many_the_model_actually_answered(self) -> None:
        r = self.rows["agent-groq:openai/gpt-oss-120b"]
        answered = r["total"] - r["fallbacks"]
        self.assertGreater(r["fallbacks"], 0, "a clean run should drop the caveat entirely")
        self.assertIn(f"{answered} of the {r['total']} cases", self.page)
        self.assertIn(f"{answered}/{r['total']} answered", self.readme)
        self.assertIn(str(r["rate_limit_waits"]), self.page)

    def test_no_superseded_figures_survive(self) -> None:
        """The previous, dirtier run reported 14/27 and 5 unsafe."""
        for text, name in ((self.page, "docs/index.html"), (self.readme, "README.md")):
            for stale in ("14/27", "* partial"):
                if stale in text and "much dirtier run" in text:
                    continue          # the README cites the old run deliberately
                self.assertNotIn(stale, text, f"{name}: stale figure {stale!r}")
