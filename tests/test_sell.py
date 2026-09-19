"""Tests for the self-learning loop.

The interesting assertions are not "does it adapt" but the properties that make
autonomous adaptation safe: that a fix which would regress a past case is
rejected, that an undocumented change is still caught, that a stale write does
not trigger a spurious policy change, and that no path in the system asks a
human.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sell.agent import Agent                                    # noqa: E402
from sell.environment import PartnerAPI                          # noqa: E402
from sell.experiment import FREE, IRREVERSIBLE, REVERSIBLE, classify  # noqa: E402
from sell.policy import Patch, Rule, initial_policy              # noqa: E402
from sell.reasoner import HeuristicReasoner                      # noqa: E402
from sell.store import GoldenSet                                 # noqa: E402

QUIET = lambda _s: None       # noqa: E731


def record(i: int, cluster: str = "acme", cents: int = 500_000) -> dict:
    return {"invoice_ref": f"INV-{i}", "customer_email": f"ap@{cluster}.example",
            "amount_cents": cents, "issued_on": "2026-09-01",
            "terms": "net30", "currency": "USD"}


def run(drifts: dict[int, str], ticks: int = 12, *, stage: dict[int, str] | None = None,
        clusters=("acme",)) -> tuple[Agent, PartnerAPI]:
    api = PartnerAPI()
    agent = Agent(api, HeuristicReasoner(), log=QUIET, seeded_clusters=clusters)
    rng = random.Random(1)
    n = 0
    for tick in range(1, ticks + 1):
        if stage and tick in stage:
            api.stage_drift(stage[tick], tick)
        if tick in drifts:
            if api.has_staged():
                api.promote_staged(tick)
            else:
                api.apply_drift(drifts[tick], tick)
        tasks = []
        for cluster in clusters:
            n += 1
            tasks.append({"task_id": f"T{n:03d}", "cluster": cluster,
                          "canonical": record(n, cluster, rng.randrange(100, 9000) * 100)})
        agent.tick(tick, tasks)
    return agent, api


class TestSteadyState(unittest.TestCase):
    def test_no_drift_means_no_learning(self):
        """An agent that rewrites its policy when nothing changed is broken."""
        agent, _ = run({}, ticks=8)
        self.assertEqual(agent.policy.version, 1)
        self.assertEqual(agent.metrics.adaptations, [])
        self.assertEqual(agent.metrics.completed, 8)
        self.assertEqual(len(agent.metrics.failed_tasks), 0)


class TestReactiveAdaptation(unittest.TestCase):
    def _adapted(self, drift: str, tick: int = 4, ticks: int = 10):
        agent, api = run({tick: drift}, ticks=ticks)
        self.assertTrue(agent.metrics.adaptations,
                        f"agent never adapted to {drift}")
        self.assertEqual(agent.metrics.human_interventions, 0)
        return agent, api

    def test_field_rename(self):
        agent, _ = self._adapted("rename_email")
        self.assertTrue(any(r.op == "rename" and r.args["to"] == "contact_email"
                            for r in agent.policy.rules))

    def test_new_required_field(self):
        agent, _ = self._adapted("require_currency")
        # The right fix is to stop dropping the field, not to invent a value.
        self.assertFalse(any(r.op == "drop" and r.args["field"] == "currency"
                             for r in agent.policy.rules))

    def test_enum_value_set_change(self):
        agent, _ = self._adapted("terms_enum")
        mapped = [r for r in agent.policy.rules if r.op == "map_value"]
        self.assertTrue(mapped)
        self.assertEqual(mapped[0].args["mapping"].get("net30"), "NET_30")

    def test_date_format_change(self):
        agent, _ = self._adapted("iso_dates")
        self.assertTrue(any(r.op == "suffix" for r in agent.policy.rules))

    def test_documented_unit_change_is_caught_from_the_spec(self):
        """When the change is documented, the ledger never has to get involved."""
        agent, _ = self._adapted("amount_to_cents_documented", tick=4, ticks=10)
        self.assertEqual(agent.metrics.silent_errors_caught, 0)
        self.assertFalse(any(r.op == "divide_int" for r in agent.policy.rules))

    def test_every_adopted_rule_is_environment_verified(self):
        agent, _ = run({3: "rename_email", 6: "terms_enum", 9: "iso_dates"}, ticks=14)
        for rule in agent.policy.rules:
            self.assertTrue(rule.verified, f"{rule.id} entered policy unverified")
            self.assertTrue(rule.provenance, f"{rule.id} has no provenance")


class TestSilentDrift(unittest.TestCase):
    """The case that decides whether any of this is worth building."""

    def setUp(self):
        self.agent, self.api = run({6: "amount_to_cents_silent"}, ticks=16)

    def test_caught_without_any_error_response(self):
        self.assertGreater(self.agent.metrics.silent_errors_caught, 0)
        self.assertEqual(len(self.agent.metrics.failed_tasks), 0,
                         "the partner never rejected anything -- that is the point")

    def test_bad_writes_are_rolled_back(self):
        self.assertGreater(self.agent.metrics.rollbacks, 0)
        rolled = [t for t in self.agent.store.rows if t.rolled_back]
        for t in rolled:
            self.assertNotIn(t.record_id, self.api.records)

    def test_policy_ends_up_correct(self):
        payload = self.agent.policy.render(record(99, cents=123_400))
        self.assertEqual(payload["amount"], 123_400)

    def test_stale_writes_do_not_cause_policy_churn(self):
        """Several bad writes are in flight when the first finding lands. Each
        later finding must be recognised as stale, not re-fixed."""
        self.assertGreater(self.agent.metrics.stale_writes_repaired, 0)
        unit_changes = [a for a in self.agent.metrics.adaptations
                        if "ledger" in a["detected_by"]]
        self.assertEqual(len(unit_changes), 1,
                         f"policy oscillated: {unit_changes}")

    def test_finding_became_a_permanent_assertion(self):
        asserted = [c for c in self.agent.golden.cases.values() if c.assertions]
        self.assertTrue(asserted)


class TestPreVerification(unittest.TestCase):
    def test_staged_change_is_verified_before_it_lands(self):
        agent, _ = run({5: "rename_email"}, ticks=10, stage={4: "rename_email"})
        self.assertEqual(agent.metrics.pre_verified, 1)
        self.assertEqual(len(agent.metrics.failed_tasks), 0)
        landed = [a for a in agent.metrics.adaptations if a["pre_verified"]]
        self.assertEqual(landed[0]["tick"], 5, "should apply the tick it lands")

    def test_a_staged_fix_is_not_deployed_early(self):
        """Deploying a fix for the next version against the current one is a
        self-inflicted outage. It must be held, not applied."""
        api = PartnerAPI()
        agent = Agent(api, HeuristicReasoner(), log=QUIET, seeded_clusters=("acme",))
        api.stage_drift("rename_email", 1)
        agent.tick(1, [{"task_id": "T1", "cluster": "acme", "canonical": record(1)}])
        self.assertEqual(agent.policy.version, 1)
        self.assertEqual(len(agent.staged_patches), 1)
        self.assertEqual(len(agent.metrics.failed_tasks), 0)


class TestRegressionGuard(unittest.TestCase):
    def test_a_fix_that_breaks_a_past_case_is_rejected(self):
        from sell.experiment import Experimenter
        api = PartnerAPI()
        golden = GoldenSet()
        golden.record_success("case1", record(1, cents=500_000))
        golden.add_assertion("case1", {"field": "amount", "equals": 500_000})
        policy = initial_policy()
        policy.commit(Patch(remove=[r.id for r in policy.rules
                                    if r.op == "divide_int"]))   # now correct in cents
        exp = Experimenter(api, golden)
        bad = Patch(add=[Rule("multiply", {"field": "amount", "by": 100})],
                    rationale="double-applies the correction")
        self.assertIsNone(exp.evaluate(policy, [bad], record(2), tick=1),
                          "a patch violating an accumulated assertion was adopted")

    def test_sandbox_rejection_blocks_adoption(self):
        from sell.experiment import Experimenter
        api = PartnerAPI()
        exp = Experimenter(api, GoldenSet())
        nonsense = Patch(add=[Rule("set_const", {"field": "not_a_field", "value": 1})])
        self.assertIsNone(exp.evaluate(initial_policy(), [nonsense], record(1), tick=1))


class TestSafety(unittest.TestCase):
    def test_unknown_actions_are_irreversible_by_default(self):
        self.assertEqual(classify("wire_the_money"), IRREVERSIBLE)
        self.assertEqual(classify("sandbox_submit"), FREE)
        self.assertEqual(classify("submit"), REVERSIBLE)

    def test_policy_is_revertible_to_any_prior_version(self):
        policy = initial_policy()
        before = policy.render(record(1))
        policy.commit(Patch(add=[Rule("drop", {"field": "terms"})]))
        self.assertNotIn("terms", policy.render(record(1)))
        policy.revert()
        self.assertEqual(policy.render(record(1)), before)

    def test_full_scenario_never_needs_a_human(self):
        agent, _ = run({3: "rename_email", 6: "require_currency", 9: "terms_enum",
                        12: "iso_dates", 15: "amount_to_cents_silent"},
                       ticks=24, clusters=("acme", "globex"))
        self.assertEqual(agent.metrics.human_interventions, 0)
        self.assertEqual(agent.metrics.unresolved, [])
        self.assertGreaterEqual(len(agent.metrics.adaptations), 5)

    def test_a_new_cluster_starts_in_shadow_and_graduates_itself(self):
        agent, _ = run({}, ticks=10, clusters=("acme", "newco"))
        self.assertEqual(agent.governor.state("newco").level, "autonomous")
        self.assertTrue(any("promote newco" in e for e in agent.governor.events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
