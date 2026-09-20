"""The browser engine must agree with the Python engine.

`docs/playground.js` is a port of `sell/real/`, and a port that drifts is worse
than no port at all: the playground would show a visitor a question the tool
would not actually ask. So the same inputs go through both implementations and
any disagreement fails here.

Skipped when node is unavailable, so the suite stays runnable without it.
"""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.cases import CaseSet                       # noqa: E402
from bench.run import DEFAULT_CASES                   # noqa: E402
from sell.policy import Policy                        # noqa: E402
from sell.real import diff as differ                  # noqa: E402
from sell.real import gate as gt                      # noqa: E402
from sell.real import openapi as oa                   # noqa: E402
from sell.reasoner import Context, HeuristicReasoner  # noqa: E402
from sell.store import EnvironmentModel               # noqa: E402

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "conformance_driver.js"
FIX = HERE / "fixtures"

# Field-name pairs whose similarity decides whether a rename is proposed at all.
RATIO_PAIRS = [
    ("coupon", "phone"), ("coupon", "discounts"), ("rendering_options", "rendering"),
    ("unit_amount", "amount"), ("?refund", "?refunds"), ("legacy_code", "note"),
    ("promotion_code", "address.postal_code"), ("price", "pricing"),
    ("documents.proof_of_registration", "documents.proof_of_address"),
    ("", ""), ("a", "a"), ("abc", "xyz"),
]


def node() -> str | None:
    return shutil.which("node")


def _ungated(candidates, payload, new):
    """What `bench.solvers.AgentSolver(gated=False)` would ship, and what it costs.

    The page shows this next to the gated verdict, so the two implementations
    have to agree on it as well.
    """
    if not candidates:
        return {"patch": None, "outcome": "none", "lost": []}
    patch = candidates[0]
    after = Policy().preview(patch).render(payload)
    why = gt.SchemaOracle(new).check(after)
    lost = [] if why else gt.CapabilityCheck().lost(payload, after)
    return {"patch": patch.describe(),
            "outcome": "rejected" if why else ("silent" if lost else "safe"),
            "lost": list(lost)}


class TestBrowserEngineMatchesPython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if node() is None:
            raise unittest.SkipTest("node is not available")
        cls.cases = CaseSet.load(DEFAULT_CASES)
        cls.py, payload = cls._python_side(cls.cases)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(payload, fh)
            cls.input_path = fh.name
        proc = subprocess.run([node(), str(DRIVER), cls.input_path],
                              capture_output=True, text=True, cwd=str(HERE))
        if proc.returncode != 0:
            raise AssertionError(f"driver failed: {proc.stderr[:800]}")
        cls.js = json.loads(proc.stdout)

    @staticmethod
    def _python_side(cases):
        """Run the real Python pipeline, and build the same input for node."""
        py, payload = {}, {"pipeline": {}, "extraction": {}, "ratios": RATIO_PAIRS}

        for case in cases:
            old, new = cases.old_contract(case), cases.new_contract(case)
            signals = differ.diff_contracts(old, new)
            model = EnvironmentModel()
            model.adopt(new)
            ctx = Context(policy=Policy(), model=model, canonical=case.payload, tick=0)
            candidates = HeuristicReasoner().propose(signals, ctx)
            verdict = gt.RealGate(new).evaluate(Policy(), candidates, case.payload)
            py[case.case_id] = {
                "ungated": _ungated(candidates, case.payload, new),
                "signals": [[s.kind, s.detail.get("field"), s.detail.get("impact")]
                            for s in signals],
                "candidates": [p.describe() for p in candidates],
                "tier": verdict.tier,
                "question": verdict.question,
                "lost": verdict.lost_capability,
                "adopted": verdict.patch.describe() if verdict.adoptable else None,
            }
            payload["pipeline"][case.case_id] = {
                "old": old, "new": new, "payload": case.payload}

        for name, method, path, depth in [
            ("widgets-v1-post", "post", "/v1/widgets", 2),
            ("widgets-v2-post", "post", "/v1/widgets", 2),
            ("widgets-v1-get", "get", "/v1/widgets", 1),
            ("widgets-v2-get", "get", "/v1/widgets", 1),
            ("widgets-v1-path", "post", "/v1/widgets/{id}", 1),
        ]:
            spec = oa.load(FIX / f"widgets-{name.split('-')[1]}.json")
            payload["extraction"][name] = {"spec": spec, "method": method,
                                           "path": path, "max_depth": depth}
            py.setdefault("_extraction", {})[name] = oa.contract(
                spec, method, path, max_depth=depth)
        return py, payload

    # ---- the comparisons ------------------------------------------------

    def test_similarity_matches_difflib_exactly(self):
        """Rename proposals hinge on a 0.6 cutoff, so an approximation here would
        silently change which repairs the playground offers."""
        for a, b, js_ratio in self.js["ratios"]:
            expected = difflib.SequenceMatcher(None, a, b).ratio()
            self.assertAlmostEqual(js_ratio, expected, places=9,
                                   msg=f"ratio({a!r},{b!r})")

    def test_contract_extraction_matches(self):
        for name, expected in self.py["_extraction"].items():
            got = self.js["extraction"][name]
            self.assertEqual(set(got["fields"]), set(expected["fields"]),
                             f"{name}: different field sets")
            for field, facts in expected["fields"].items():
                for key in ("type", "required", "required_if_present", "allowed",
                            "pattern", "max_length", "location"):
                    self.assertEqual(got["fields"][field].get(key), facts.get(key),
                                     f"{name}: {field}.{key}")

    def test_signals_match_on_every_benchmark_case(self):
        for case_id, expected in self.py.items():
            if case_id == "_extraction":
                continue
            self.assertEqual(self.js["pipeline"][case_id]["signals"], expected["signals"],
                             f"{case_id}: signals differ")

    def test_proposals_match_on_every_benchmark_case(self):
        for case_id, expected in self.py.items():
            if case_id == "_extraction":
                continue
            self.assertEqual(self.js["pipeline"][case_id]["candidates"], expected["candidates"],
                             f"{case_id}: proposals differ")

    def test_verdict_and_question_match_on_every_benchmark_case(self):
        """The question is what the playground shows a visitor. If it differs from
        what the tool would ask, the demo is lying."""
        for case_id, expected in self.py.items():
            if case_id == "_extraction":
                continue
            got = self.js["pipeline"][case_id]
            self.assertEqual(got["tier"], expected["tier"], f"{case_id}: tier")
            self.assertEqual(got["question"], expected["question"], f"{case_id}: question")
            self.assertEqual(got["lost"], expected["lost"], f"{case_id}: lost capability")
            self.assertEqual(got["adopted"], expected["adopted"], f"{case_id}: adopted")
            self.assertEqual(got["ungated"], expected["ungated"], f"{case_id}: ungated")

    def test_the_comparison_actually_covered_the_whole_benchmark(self):
        compared = [k for k in self.py if k != "_extraction"]
        self.assertGreaterEqual(len(compared), 70)
        self.assertEqual(len(compared), len(self.js["pipeline"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
