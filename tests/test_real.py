"""Tests for the sensors that read real published API contracts.

The fixtures are small, but every shape in them is one a real spec actually
uses and that a naive reader gets wrong: nested objects behind a `$ref`,
`anyOf` used as an "or unset" idiom, parameters living outside the request
body, and path-level parameters inherited by an operation.

An extra test runs against a genuinely downloaded provider spec when one is
cached, and skips otherwise, so the suite stays offline-clean.

Run: python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sell.real import diff as differ           # noqa: E402
from sell.real import openapi as oa             # noqa: E402
from sell.real import sources                   # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
V1 = oa.load(FIX / "widgets-v1.json")
V2 = oa.load(FIX / "widgets-v2.json")


class TestAdapter(unittest.TestCase):
    def setUp(self):
        self.body = oa.contract(V1, "post", "/v1/widgets", max_depth=2)
        self.query = oa.contract(V1, "get", "/v1/widgets")

    def test_shape_matches_the_simulated_partner(self):
        """Nothing downstream should be able to tell real from simulated."""
        self.assertEqual(set(self.body) >= {"version", "fields", "operation"}, True)
        for facts in self.body["fields"].values():
            self.assertIn("type", facts)
            self.assertIn("required", facts)

    def test_ref_is_resolved_and_nested_fields_are_dotted(self):
        self.assertIn("shipping.method", self.body["fields"])
        self.assertEqual(self.body["fields"]["shipping.method"]["allowed"],
                         ["ground", "air"])

    def test_nested_required_is_conditional_not_absolute(self):
        """`shipping.method` is required only if `shipping` is sent. Reporting it
        as flatly required would make every caller look broken."""
        facts = self.body["fields"]["shipping.method"]
        self.assertFalse(facts["required"])
        self.assertTrue(facts["required_if_present"])

    def test_top_level_required_is_honoured(self):
        self.assertTrue(self.body["fields"]["name"]["required"])
        self.assertFalse(self.body["fields"]["amount"]["required"])

    def test_unset_sentinel_is_not_treated_as_an_allowed_value(self):
        """anyOf [..., {enum: [""]}] means 'or clear it', not 'or send empty'."""
        self.assertNotIn("", self.body["fields"]["note"].get("allowed") or [])
        self.assertEqual(self.body["fields"]["note"]["type"], "string")
        self.assertEqual(self.body["fields"]["note"]["max_length"], 50)

    def test_query_parameters_are_part_of_the_contract(self):
        self.assertIn("?limit", self.query["fields"])
        self.assertEqual(self.query["fields"]["?limit"]["location"], "query")
        self.assertEqual(self.query["fields"]["?status"]["allowed"],
                         ["active", "archived"])

    def test_path_parameters_are_inherited_and_always_required(self):
        c = oa.contract(V1, "post", "/v1/widgets/{id}")
        self.assertIn("{id}", c["fields"])
        self.assertTrue(c["fields"]["{id}"]["required"])

    def test_operations_include_parameter_only_endpoints(self):
        ops = oa.operations(V1)
        self.assertIn(("get", "/v1/legacy"), ops,
                      "an endpoint whose only input is a query param still has a "
                      "contract to drift against")

    def test_description_is_hashed_not_compared_in_full(self):
        facts = self.body["fields"]["amount"]
        self.assertIn("description_digest", facts)
        self.assertLessEqual(len(facts["description"]), oa.DESC_PREFIX)

    def test_unknown_operation_raises(self):
        with self.assertRaises(KeyError):
            oa.contract(V1, "post", "/v1/nope")


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.body = differ.diff_contracts(
            oa.contract(V1, "post", "/v1/widgets", max_depth=2),
            oa.contract(V2, "post", "/v1/widgets", max_depth=2))
        self.query = differ.diff_contracts(oa.contract(V1, "get", "/v1/widgets"),
                                           oa.contract(V2, "get", "/v1/widgets"))
        self.by = {(s.kind, s.detail["field"]): s for s in self.body + self.query}

    def _impact(self, kind: str, field: str) -> str:
        self.assertIn((kind, field), self.by, f"{kind} on {field} not detected")
        return self.by[(kind, field)].detail["impact"]

    def test_removed_field_is_breaking(self):
        self.assertEqual(self._impact("field_removed", "legacy_code"), differ.BREAKING)

    def test_type_change_is_breaking(self):
        self.assertEqual(self._impact("type_changed", "amount"), differ.BREAKING)

    def test_tightened_max_length_is_breaking_but_loosened_is_not(self):
        self.assertEqual(self._impact("max_length_changed", "name"), differ.BREAKING)
        self.assertEqual(differ._verdict("max_length_changed", 50, 100), differ.ADDITIVE)

    def test_new_required_parameter_is_breaking_but_optional_is_not(self):
        self.assertEqual(self._impact("field_added", "?account"), differ.BREAKING)
        self.assertEqual(self._impact("field_added", "tags"), differ.ADDITIVE)

    def test_enum_losing_a_value_is_breaking_and_gaining_one_is_not(self):
        s = self.by[("allowed_changed", "?status")]
        self.assertEqual(s.detail["impact"], differ.BREAKING)
        self.assertEqual(s.detail["removed"], ["archived"])
        added = self.by[("allowed_changed", "mode")]
        self.assertEqual(added.detail["impact"], differ.ADDITIVE)
        self.assertEqual(added.detail["added"], ["instant"])

    def test_relaxing_a_conditional_requirement_is_not_breaking(self):
        self.assertEqual(self._impact("required_if_present_changed", "shipping.method"),
                         differ.ADDITIVE)

    def test_description_change_is_cosmetic_but_never_dropped(self):
        """The silent unit change in the simulation hid in exactly this class, so
        it must stay visible even though it cannot break a caller by itself."""
        s = self.by[("description_changed", "amount")]
        self.assertEqual(s.detail["impact"], differ.COSMETIC)
        self.assertIn("cents", s.detail["now_text"])

    def test_signals_are_the_same_type_the_simulated_sensors_emit(self):
        from sell.sensors import Signal
        self.assertTrue(all(isinstance(s, Signal) for s in self.body))
        self.assertTrue(all(s.summary() for s in self.body))

    def test_identical_versions_produce_no_signals(self):
        c = oa.contract(V1, "post", "/v1/widgets")
        self.assertEqual(differ.diff_contracts(c, c), [])

    def test_whole_spec_diff_reports_added_and_removed_operations(self):
        drifts, added, removed = differ.diff_all(oa.contracts(V1), oa.contracts(V2))
        self.assertIn("POST /v1/gadgets", added)
        self.assertIn("GET /v1/legacy", removed)
        summary = differ.summarise(drifts)
        self.assertGreater(summary["by_impact"][differ.BREAKING], 0)
        self.assertEqual(summary["signals"],
                         sum(len(d.signals) for d in drifts))


class TestReasonerOnRealShapes(unittest.TestCase):
    def test_a_removed_field_produces_a_hypothesis(self):
        """Found by running against a real spec: the heuristic reasoner had no
        handler for a field the contract dropped, only for one the API rejected."""
        from sell.policy import Policy
        from sell.reasoner import Context, HeuristicReasoner
        from sell.store import EnvironmentModel
        new = oa.contract(V2, "post", "/v1/widgets", max_depth=2)
        signals = [s for s in differ.diff_contracts(
            oa.contract(V1, "post", "/v1/widgets", max_depth=2), new)
            if s.kind == "field_removed"]
        self.assertTrue(signals)
        model = EnvironmentModel()
        model.adopt(new)
        patches = HeuristicReasoner().propose(
            signals, Context(policy=Policy(), model=model,
                             canonical={"legacy_code": "x"}, tick=0))
        self.assertTrue(any(r.op == "drop" and r.args["field"] == "legacy_code"
                            for p in patches for r in p.add))


class TestSources(unittest.TestCase):
    def test_local_path_resolves_without_network(self):
        self.assertEqual(sources.resolve_spec(str(FIX / "widgets-v1.json")),
                         FIX / "widgets-v1.json")

    def test_known_providers_build_pinned_urls(self):
        url = sources.PROVIDERS["stripe"].url("v2506")
        self.assertIn("/v2506/", url)
        self.assertTrue(url.endswith(".json"))


class TestAgainstRealProviderSpec(unittest.TestCase):
    """Runs only when a real spec has been downloaded; skipped otherwise."""

    @classmethod
    def setUpClass(cls):
        cls.path = sources.cache_dir() / "specs" / "stripe-v2506.json"
        if not cls.path.exists():
            raise unittest.SkipTest(f"no cached provider spec at {cls.path}")
        cls.spec = oa.load(cls.path)

    def test_extracts_a_large_real_contract(self):
        c = oa.contract(self.spec, "post", "/v1/invoices", max_depth=2)
        self.assertGreater(len(c["fields"]), 50)
        self.assertTrue(any("." in n for n in c["fields"]), "nesting was not flattened")

    def test_every_real_operation_has_at_least_one_field(self):
        contracts = oa.contracts(self.spec, max_depth=1)
        empty = [op for op, c in contracts.items() if not c["fields"]]
        self.assertEqual(empty, [], "an operation with no readable contract means "
                                    "the adapter is blind to part of the surface")

    def test_real_query_parameters_are_found(self):
        c = oa.contract(self.spec, "get", "/v1/invoices")
        self.assertTrue([n for n in c["fields"] if n.startswith("?")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
