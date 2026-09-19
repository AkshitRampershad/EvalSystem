"""Tests for the real-provider verification gate.

The three things worth asserting are not "does it validate":

  * the tiers keep their separate authority -- a schema pass is not adoption;
  * a candidate that satisfies the contract by dropping the field it could not
    work out is refused, not adopted, and produces one specific question;
  * the live sandbox path is exercised without credentials, by injecting a
    transport, so the code that talks to a real provider is not untested.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sell.policy import Patch, Policy, Rule           # noqa: E402
from sell.real import gate as gt                       # noqa: E402
from sell.real import openapi as oa                    # noqa: E402
from sell.real import validator as va                  # noqa: E402
from sell.store import GoldenSet                       # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
V1 = oa.load(FIX / "widgets-v1.json")
V2 = oa.load(FIX / "widgets-v2.json")
C1 = oa.contract(V1, "post", "/v1/widgets", max_depth=2)
C2 = oa.contract(V2, "post", "/v1/widgets", max_depth=2)
Q2 = oa.contract(V2, "get", "/v1/widgets")


class TestValidator(unittest.TestCase):
    def test_accepts_a_conforming_request(self):
        self.assertIsNone(va.validate(C2, va.Request(
            body={"name": "W", "mode": "fast", "amount": "100"})))

    def test_rejects_each_violation_class(self):
        cases = {
            "unknown_field": va.Request(body={"name": "W", "legacy_code": "x"}),
            "missing_required_field": va.Request(body={"mode": "fast"}),
            "value_not_allowed": va.Request(body={"name": "W", "mode": "warp"}),
            "type_mismatch": va.Request(body={"name": "W", "amount": 100}),
            "too_long": va.Request(body={"name": "x" * 80}),
        }
        for expected, request in cases.items():
            error = va.validate(C2, request)
            self.assertIsNotNone(error, f"{expected} not detected")
            self.assertEqual(error["code"], expected)

    def test_conditional_requirement_fires_only_when_parent_is_sent(self):
        self.assertIsNone(va.validate(C1, va.Request(body={"name": "W"})))
        error = va.validate(C1, va.Request(body={"name": "W", "shipping.address": "a"}))
        self.assertEqual(error["code"], "missing_required_field")
        self.assertEqual(error["field"], "shipping.method")

    def test_a_bool_is_not_an_integer(self):
        """True is an int in Python and is not in any API."""
        self.assertFalse(va._type_ok(True, "integer"))
        self.assertTrue(va._type_ok(True, "boolean"))

    def test_union_types_from_anyof_accept_either_branch(self):
        self.assertTrue(va._type_ok(5, "integer|object"))
        self.assertTrue(va._type_ok({}, "integer|object"))
        self.assertFalse(va._type_ok("s", "integer|object"))

    def test_unconstrained_type_is_not_given_an_invented_rule(self):
        self.assertTrue(va._type_ok("anything", "unknown"))

    def test_parameters_are_validated_in_their_own_location(self):
        self.assertEqual(va.validate(Q2, va.Request(query={"limit": 10}))["field"],
                         "?account")
        self.assertIsNone(va.validate(Q2, va.Request(query={"limit": 10,
                                                           "account": "acct_1"})))

    def test_request_round_trips_through_keyed_form(self):
        req = va.Request(body={"a": 1}, query={"b": 2}, path={"id": "x"})
        self.assertEqual(va.Request.from_keyed(req.keyed()).keyed(), req.keyed())

    def test_validate_all_reports_more_than_the_first_problem(self):
        errors = va.validate_all(C2, va.Request(body={"mode": "warp"}))
        self.assertGreaterEqual(len(errors), 2)

    def test_form_encoding_uses_bracket_notation(self):
        pairs = dict(va.encode_form({"shipping.method": "air", "ok": True,
                                     "tags": ["a", "b"]}))
        self.assertEqual(pairs["shipping[method]"], "air")
        self.assertEqual(pairs["ok"], "true")
        self.assertEqual(pairs["tags[0]"], "a")


class TestCapabilityCheck(unittest.TestCase):
    def setUp(self):
        self.check = gt.CapabilityCheck()

    def test_a_rename_keeps_the_capability(self):
        self.assertEqual(self.check.lost({"coupon": "SAVE20"}, {"promo": "SAVE20"}), [])

    def test_a_drop_loses_it(self):
        self.assertEqual(self.check.lost({"coupon": "SAVE20"}, {}), ["coupon"])

    def test_a_value_nested_into_a_new_structure_is_still_kept(self):
        """Found while testing against a real spec: providers replace a scalar
        field with an array of objects, and the value survives inside it."""
        self.assertEqual(
            self.check.lost({"coupon": "SAVE20"},
                            {"discounts": [{"coupon": "SAVE20"}]}), [])

    def test_a_partial_carry_counts_as_a_loss(self):
        lost = self.check.lost({"pair": ["a", "b"]}, {"pair": ["a"]})
        self.assertEqual(lost, ["pair"])

    def test_empty_values_are_not_capabilities(self):
        self.assertEqual(self.check.lost({"note": "", "tags": []}, {}), [])

    def test_the_question_names_the_field_and_the_operation(self):
        q = self.check.question(["coupon"], {"operation": "POST /v1/customers"})
        self.assertIn("coupon", q)
        self.assertIn("POST /v1/customers", q)


class TestGateTiers(unittest.TestCase):
    def setUp(self):
        self.policy = Policy([Rule("rename", {"from": "title", "to": "name"},
                                   verified=True)])
        self.canonical = {"title": "Widget", "mode": "fast"}

    def test_a_schema_valid_non_lossy_fix_is_adopted(self):
        policy = Policy()
        canonical = {"name": "W", "mode": "turbo", "legacy_code": "L"}
        patch = Patch(add=[Rule("rename", {"from": "legacy_code", "to": "note"})],
                      rationale="carry it to a field that still exists")
        verdict = gt.RealGate(C2).evaluate(policy, [patch], canonical)
        self.assertEqual(verdict.tier, gt.SCHEMA)
        self.assertTrue(verdict.adoptable)
        self.assertTrue(patch.add[0].verified,
                        "an adopted rule must be marked environment-verified")

    def test_a_schema_invalid_fix_is_never_adoptable(self):
        patch = Patch(add=[Rule("set_const", {"field": "nope", "value": 1})])
        verdict = gt.RealGate(C2).evaluate(Policy(), [patch], {"name": "W"})
        self.assertFalse(verdict.adoptable)
        self.assertIn(verdict.tier, (gt.REJECTED, gt.NEEDS_HUMAN))
        self.assertIn("schema", verdict.rejected[0][1])

    def test_a_lossy_fix_escalates_instead_of_being_adopted(self):
        """The case the whole tier exists for: dropping the field satisfies the
        contract and silently stops doing the job."""
        policy = Policy()
        canonical = {"name": "W", "legacy_code": "L"}
        patch = Patch(add=[Rule("drop", {"field": "legacy_code"})])
        verdict = gt.RealGate(C2).evaluate(policy, [patch], canonical)
        self.assertEqual(verdict.tier, gt.NEEDS_HUMAN)
        self.assertFalse(verdict.adoptable,
                         "a behaviour-losing candidate must never be adoptable")
        self.assertEqual(verdict.lost_capability, ["legacy_code"])
        self.assertIsNotNone(verdict.question)

    def test_a_non_lossy_candidate_is_preferred_over_a_lossy_one(self):
        policy = Policy()
        canonical = {"name": "W", "legacy_code": "L"}
        lossy = Patch(add=[Rule("drop", {"field": "legacy_code"})])
        keeps = Patch(add=[Rule("rename", {"from": "legacy_code", "to": "note"})])
        verdict = gt.RealGate(C2).evaluate(policy, [lossy, keeps], canonical)
        self.assertEqual(verdict.tier, gt.SCHEMA)
        self.assertIs(verdict.patch, keeps)

    def test_a_fix_that_regresses_a_past_case_is_rejected(self):
        policy = Policy()
        good = {"name": "W", "mode": "fast"}
        breaks_old = {"name": "W", "mode": "turbo"}
        patch = Patch(add=[Rule("map_value", {"field": "mode",
                                              "mapping": {"turbo": "warp"}})])
        verdict = gt.RealGate(C2).evaluate(policy, [patch], breaks_old,
                                           regression=[good, breaks_old])
        self.assertFalse(verdict.adoptable)
        self.assertTrue(any("schema" in why for _, why in verdict.rejected))

    def test_accumulated_assertions_gate_before_the_schema(self):
        golden = GoldenSet()
        golden.record_success("c1", {"name": "W", "amount": 100})
        golden.add_assertion("c1", {"field": "amount", "equals": 100})
        patch = Patch(add=[Rule("multiply", {"field": "amount", "by": 100})])
        gate = gt.RealGate(C2, golden=golden)
        verdict = gate.evaluate(Policy(), [patch], {"name": "W", "amount": 100})
        self.assertEqual(verdict.tier, gt.REJECTED)
        self.assertIn("assertion", verdict.rejected[0][1])

    def test_no_candidates_means_no_adoption(self):
        verdict = gt.RealGate(C2).evaluate(Policy(), [], {"name": "W"})
        self.assertIsNone(verdict.patch)
        self.assertFalse(verdict.adoptable)


class TestLiveSandbox(unittest.TestCase):
    """Exercises the real-provider HTTP path with an injected transport, so the
    code that would talk to a provider is covered without credentials."""

    def _sandbox(self, responder, **cfg):
        seen: list[dict] = []

        def transport(method, url, body, headers):
            seen.append({"method": method, "url": url, "body": dict(body),
                         "auth": headers.get("Authorization")})
            return responder(seen[-1])

        config = gt.SandboxConfig(base_url="https://api.example.test",
                                  token="sk_test_abc", enabled=True, **cfg)
        return gt.LiveSandbox(config, "post", "/v1/widgets/{id}",
                              transport=transport), seen

    def test_a_2xx_is_a_pass_and_the_request_is_shaped_correctly(self):
        sandbox, seen = self._sandbox(lambda _r: (200, '{"id":"w_1"}'))
        self.assertIsNone(sandbox.available())
        self.assertIsNone(sandbox.check({"{id}": "w_9", "name": "W",
                                         "shipping.method": "air", "?expand": "x"}))
        call = seen[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/v1/widgets/w_9", call["url"], "path param not substituted")
        self.assertIn("expand=x", call["url"], "query param not sent in the URL")
        self.assertEqual(call["body"]["shipping[method]"], "air")
        self.assertEqual(call["auth"], "Bearer sk_test_abc")

    def test_a_4xx_is_reported_with_the_provider_s_own_reason(self):
        body = '{"error":{"code":"parameter_unknown","param":"nope","message":"Bad."}}'
        sandbox, _ = self._sandbox(lambda _r: (400, body))
        why = sandbox.check({"{id}": "w_1", "nope": 1})
        self.assertIn("parameter_unknown", why)
        self.assertIn("nope", why)

    def test_a_non_json_error_still_produces_a_reason(self):
        sandbox, _ = self._sandbox(lambda _r: (503, "<html>down</html>"))
        self.assertEqual(sandbox.check({"{id}": "w_1"}), "HTTP 503")

    def test_it_refuses_a_credential_that_is_not_a_test_key(self):
        config = gt.SandboxConfig(base_url="https://api.example.test",
                                  token="sk_live_DANGER", enabled=True)
        sandbox = gt.LiveSandbox(config, "post", "/v1/widgets",
                                 transport=lambda *a, **k: (200, "{}"))
        self.assertIn("refusing", sandbox.available() or "")

    def test_it_is_off_unless_explicitly_enabled(self):
        config = gt.SandboxConfig(base_url="https://api.example.test",
                                  token="sk_test_abc", enabled=False)
        sandbox = gt.LiveSandbox(config, "post", "/v1/widgets",
                                 transport=lambda *a, **k: (200, "{}"))
        self.assertIn("not enabled", sandbox.available() or "")

    def test_the_gate_reaches_tier_sandbox_when_the_provider_accepts(self):
        sandbox, seen = self._sandbox(lambda _r: (200, "{}"))
        gate = gt.RealGate(C2, sandbox=sandbox)
        patch = Patch(add=[Rule("rename", {"from": "legacy_code", "to": "note"})])
        verdict = gate.evaluate(Policy(), [patch],
                                {"name": "W", "legacy_code": "L"})
        self.assertEqual(verdict.tier, gt.SANDBOX)
        self.assertTrue(verdict.adoptable)
        self.assertEqual(len(seen), 1, "the provider should be probed exactly once")

    def test_a_sandbox_rejection_overrides_a_schema_pass(self):
        """The reason tier 2 exists: a request the document allows and the
        provider does not."""
        body = '{"error":{"code":"undocumented_constraint","message":"no."}}'
        sandbox, _ = self._sandbox(lambda _r: (402, body))
        gate = gt.RealGate(C2, sandbox=sandbox)
        patch = Patch(add=[Rule("rename", {"from": "legacy_code", "to": "note"})])
        verdict = gate.evaluate(Policy(), [patch], {"name": "W", "legacy_code": "L"})
        self.assertEqual(verdict.tier, gt.REJECTED)
        self.assertTrue(any("sandbox" in why for _, why in verdict.rejected))

    def test_nothing_reaches_the_provider_until_the_schema_passes(self):
        sandbox, seen = self._sandbox(lambda _r: (200, "{}"))
        gate = gt.RealGate(C2, sandbox=sandbox)
        bad = Patch(add=[Rule("set_const", {"field": "nope", "value": 1})])
        gate.evaluate(Policy(), [bad], {"name": "W"})
        self.assertEqual(seen, [], "an invalid candidate must not be transmitted")

    def test_config_from_env_is_absent_without_credentials(self):
        import os
        saved = {k: os.environ.pop(k, None)
                 for k in ("SELL_LIVE_TOKEN", "STRIPE_TEST_KEY", "SELL_LIVE")}
        try:
            self.assertIsNone(gt.SandboxConfig.from_env("stripe"))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
