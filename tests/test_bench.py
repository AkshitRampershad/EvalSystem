"""Tests for the benchmark.

Two jobs here. The usual one -- does the scorer classify outcomes correctly --
and a less usual one: does the shipped case file still hold the properties that
make it worth scoring against? A benchmark whose data quietly rots measures
nothing while continuing to print numbers, so those properties are asserted on
the committed cases themselves.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import score as scoring                       # noqa: E402
from bench.cases import ADAPT, ESCALATE, Case, CaseSet   # noqa: E402
from bench.run import DEFAULT_CASES                      # noqa: E402
from bench.solvers import Attempt, build_solver          # noqa: E402
from sell.policy import Patch, Rule                      # noqa: E402
from sell.real import gate as gt                         # noqa: E402
from sell.real.validator import Request, validate        # noqa: E402

CASES = CaseSet.load(DEFAULT_CASES)


class TestShippedCasesAreWorthScoring(unittest.TestCase):
    """The properties that stop this from being a benchmark in name only."""

    def test_there_are_cases_of_several_kinds(self):
        self.assertGreater(len(CASES), 40)
        self.assertGreaterEqual(len(CASES.by_kind()), 4)

    def test_every_request_was_valid_before_the_drift(self):
        """Otherwise the case tests the miner, not the solver."""
        for case in CASES:
            error = validate(CASES.old_contract(case),
                             Request.from_keyed(case.payload))
            self.assertIsNone(error, f"{case.case_id} was already invalid: {error}")

    def test_every_request_is_broken_by_the_drift(self):
        """Otherwise the case is free and inflates every score."""
        for case in CASES:
            self.assertIsNotNone(
                validate(CASES.new_contract(case), Request.from_keyed(case.payload)),
                f"{case.case_id} still validates; nothing to adapt")

    def test_every_focus_value_is_traceable(self):
        """An empty focus value cannot be followed, so a correct adaptation would
        be scored as a failure. This bit once, on nullable enums listing ''."""
        for case in CASES:
            if case.kind == "newly_required":
                continue
            value = case.payload.get(case.focus_field)
            self.assertNotIn(value, (None, "", [], {}),
                             f"{case.case_id} has an untraceable focus value")

    def test_rename_cases_name_where_the_value_should_go(self):
        for case in CASES:
            if case.kind == "rename":
                self.assertEqual(case.expected_outcome, ADAPT)
                self.assertIsNotNone(case.expected_target, case.case_id)

    def test_removal_cases_expect_a_question_not_a_guess(self):
        for case in CASES:
            if case.kind == "removed":
                self.assertEqual(case.expected_outcome, ESCALATE, case.case_id)

    def test_both_expectation_classes_are_represented(self):
        expectations = {c.expected_outcome for c in CASES}
        self.assertEqual(expectations, {ADAPT, ESCALATE},
                         "a benchmark with one expectation class is gameable")

    def test_contracts_are_present_for_every_case(self):
        for case in CASES:
            self.assertIn(case.old_key, CASES.contracts)
            self.assertIn(case.new_key, CASES.contracts)


class TestPersistence(unittest.TestCase):
    def test_round_trip_through_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json.gz"
            CASES.save(path)
            back = CaseSet.load(path)
        self.assertEqual(len(back), len(CASES))
        self.assertEqual(back.cases[0].case_id, CASES.cases[0].case_id)
        self.assertEqual(back.contracts.keys(), CASES.contracts.keys())

    def test_round_trip_uncompressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            CASES.save(path)
            self.assertEqual(len(CaseSet.load(path)), len(CASES))


class TestJudge(unittest.TestCase):
    def _case(self, **kw) -> Case:
        base = dict(case_id="t", provider="p", operation="POST /x",
                    from_version="a", to_version="b", kind="rename",
                    focus_field="old", payload={"old": "VALUE"},
                    expected_outcome=ADAPT, expected_target="new")
        base.update(kw)
        return Case(**base)

    CONTRACT = {"operation": "POST /x",
                "fields": {"new": {"type": "string", "required": False},
                           "other": {"type": "string", "required": False}}}

    def test_a_correct_rename_is_solved(self):
        case = self._case()
        attempt = Attempt("t", "s", adopted=Patch(
            add=[Rule("rename", {"from": "old", "to": "new"})]), tier=gt.SCHEMA)
        self.assertEqual(scoring.judge(case, self.CONTRACT, attempt).result,
                         scoring.SOLVED)

    def test_a_valid_rename_to_the_wrong_field_is_unsafe(self):
        """Validating is not the same as being right, and this is the difference."""
        case = self._case()
        attempt = Attempt("t", "s", adopted=Patch(
            add=[Rule("rename", {"from": "old", "to": "other"})]), tier=gt.SCHEMA)
        outcome = scoring.judge(case, self.CONTRACT, attempt)
        self.assertEqual(outcome.result, scoring.UNSAFE)
        self.assertIn("did not reach", outcome.detail)

    def test_dropping_the_value_is_unsafe(self):
        case = self._case()
        attempt = Attempt("t", "s", adopted=Patch(
            add=[Rule("drop", {"field": "old"})]), tier=gt.SCHEMA)
        outcome = scoring.judge(case, self.CONTRACT, attempt)
        self.assertEqual(outcome.result, scoring.UNSAFE)
        self.assertEqual(outcome.lost, ["old"])

    def test_an_invalid_result_is_unsafe(self):
        case = self._case()
        attempt = Attempt("t", "s", adopted=Patch(
            add=[Rule("set_const", {"field": "nope", "value": 1})]), tier=gt.SCHEMA)
        self.assertEqual(scoring.judge(case, self.CONTRACT, attempt).result,
                         scoring.UNSAFE)

    def test_asking_when_a_fix_existed_is_escalated_not_solved(self):
        case = self._case()
        attempt = Attempt("t", "s", tier=gt.NEEDS_HUMAN, question="where?")
        self.assertEqual(scoring.judge(case, self.CONTRACT, attempt).result,
                         scoring.ESCALATED)

    def test_asking_when_no_fix_existed_is_solved(self):
        case = self._case(kind="removed", expected_outcome=ESCALATE,
                          expected_target=None)
        attempt = Attempt("t", "s", tier=gt.NEEDS_HUMAN, question="where?")
        self.assertEqual(scoring.judge(case, self.CONTRACT, attempt).result,
                         scoring.SOLVED)

    def test_guessing_when_no_fix_existed_is_unsafe(self):
        """The outcome that looks most like success and is the most dangerous."""
        case = self._case(kind="removed", expected_outcome=ESCALATE,
                          expected_target=None)
        attempt = Attempt("t", "s", adopted=Patch(
            add=[Rule("rename", {"from": "old", "to": "other"})]), tier=gt.SCHEMA)
        outcome = scoring.judge(case, self.CONTRACT, attempt)
        self.assertEqual(outcome.result, scoring.UNSAFE)
        self.assertIn("guessed instead of asking", outcome.detail)

    def test_silence_is_unsolved(self):
        attempt = Attempt("t", "s", rejected=[("x", "schema: no")])
        self.assertEqual(scoring.judge(self._case(), self.CONTRACT, attempt).result,
                         scoring.UNSOLVED)


class TestBaselines(unittest.TestCase):
    def test_noop_solves_nothing(self):
        report = scoring.run(CASES, build_solver("noop"))
        self.assertEqual(report.count(scoring.SOLVED), 0,
                         "a case a do-nothing solver 'solves' was never broken")
        self.assertEqual(report.count(scoring.UNSAFE), 0)

    def test_always_escalating_is_perfectly_safe_and_adapts_nothing(self):
        """This row is the floor. A solver that does not beat it on `adapt` has
        contributed nothing, however good its overall percentage looks."""
        report = scoring.run(CASES, build_solver("escalate-always"))
        self.assertEqual(report.adapt_rate, 0.0)
        self.assertEqual(report.ask_rate, 1.0)
        self.assertEqual(report.safety, 1.0)

    def test_the_naive_drop_is_unsafe_on_nearly_every_case(self):
        report = scoring.run(CASES, build_solver("drop-ungated"))
        self.assertGreater(report.count(scoring.UNSAFE), 0.9 * report.total)

    def test_the_gate_removes_every_unsafe_outcome_from_that_baseline(self):
        """The gate's whole justification, as a number."""
        ungated = scoring.run(CASES, build_solver("drop-ungated"))
        gated = scoring.run(CASES, build_solver("drop"))
        self.assertGreater(ungated.count(scoring.UNSAFE), 50)
        self.assertEqual(gated.count(scoring.UNSAFE), 0)

    def test_the_gate_also_protects_the_real_agent(self):
        ungated = scoring.run(CASES, build_solver("agent-ungated"))
        gated = scoring.run(CASES, build_solver("agent"))
        self.assertGreater(ungated.count(scoring.UNSAFE),
                           10 * max(gated.count(scoring.UNSAFE), 1))

    def test_the_agent_beats_the_floor_on_adaptation(self):
        floor = scoring.run(CASES, build_solver("escalate-always"))
        agent = scoring.run(CASES, build_solver("agent"))
        self.assertGreater(agent.adapt_rate, floor.adapt_rate,
                           "the agent adapts nothing the trivial baseline cannot")

    def test_scoring_is_deterministic(self):
        a = scoring.run(CASES, build_solver("agent")).as_dict()
        b = scoring.run(CASES, build_solver("agent")).as_dict()
        self.assertEqual(a, b, "a benchmark that varies run to run cannot track change")


if __name__ == "__main__":
    unittest.main(verbosity=2)
