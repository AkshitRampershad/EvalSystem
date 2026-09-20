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
