"""Tests for the OpenAI-compatible reasoner tier (Groq and friends).

Two concerns. The plumbing: does it shape a request correctly, degrade when an
endpoint rejects a structured-output mode, and fall back rather than crash when
the endpoint is unreachable. And the security-relevant one: a model response is
untrusted input, so no op outside the allow-list may reach the policy pipeline
however the response is shaped.

The transport is injected throughout, so the code that talks to a provider is
covered without a key and without network access.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sell import hypotheses                              # noqa: E402
from sell.openai_compat import PRESETS, OpenAICompatReasoner  # noqa: E402
from sell.policy import Policy, Rule                     # noqa: E402
from sell.reasoner import Context, HeuristicReasoner, build_reasoner  # noqa: E402
from sell.sensors import Signal                          # noqa: E402
from sell.store import EnvironmentModel                  # noqa: E402

CONTRACT = {"operation": "POST /x",
            "fields": {"new_name": {"type": "string", "required": False},
                       "keep": {"type": "string", "required": False}}}


def ctx() -> Context:
    model = EnvironmentModel()
    model.adopt(CONTRACT)
    return Context(policy=Policy(), model=model,
                   canonical={"old_name": "VALUE", "keep": "K"}, tick=0)


SIGNALS = [Signal("field_removed", "spec", {"field": "old_name"}, 0)]


def reply(content: str) -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": content}}]})


GOOD = json.dumps({"candidates": [
    {"rationale": "it was renamed", "remove_rule_ids": [],
     "add_rules": [{"op": "rename",
                    "args_json": json.dumps({"from": "old_name", "to": "new_name"})}]}]})


class Recorder:
    """A stand-in endpoint that records what it was sent."""

    def __init__(self, *responses: tuple[int, str]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, payload, headers):
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        return self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]


class TestAvailability(unittest.TestCase):
    def test_absent_key_is_reported_not_guessed_at(self):
        r = OpenAICompatReasoner("groq", api_key="", transport=Recorder())
        self.assertIn("GROQ_API_KEY", r.available() or "")

    def test_a_key_makes_it_available(self):
        r = OpenAICompatReasoner("groq", api_key="gsk_x", transport=Recorder())
        self.assertIsNone(r.available())

    def test_without_a_key_it_falls_back_instead_of_calling_out(self):
        rec = Recorder(reply(GOOD))
        r = OpenAICompatReasoner("groq", api_key="", transport=rec)
        r.propose(SIGNALS, ctx())
        self.assertEqual(rec.requests, [], "no key must mean no request")

    def test_unknown_endpoint_is_refused(self):
        with self.assertRaises(KeyError):
            OpenAICompatReasoner("nonesuch")

    def test_presets_cover_the_documented_endpoints(self):
        self.assertIn("groq", PRESETS)
        self.assertTrue(PRESETS["groq"].url().endswith("/chat/completions"))


class TestRequestShape(unittest.TestCase):
    def test_it_sends_model_auth_and_a_structured_output_request(self):
        rec = Recorder(reply(GOOD))
        r = OpenAICompatReasoner("groq", api_key="gsk_x", model="m1", transport=rec)
        r.propose(SIGNALS, ctx())
        sent = rec.requests[0]
        self.assertTrue(sent["url"].endswith("/chat/completions"))
        self.assertEqual(sent["headers"]["Authorization"], "Bearer gsk_x")
        self.assertEqual(sent["payload"]["model"], "m1")
        self.assertEqual(sent["payload"]["response_format"]["type"], "json_schema")
        roles = [m["role"] for m in sent["payload"]["messages"]]
        self.assertEqual(roles, ["system", "user"])

    def test_temperature_defaults_to_zero_for_repeatability(self):
        rec = Recorder(reply(GOOD))
        OpenAICompatReasoner("groq", api_key="k", transport=rec).propose(SIGNALS, ctx())
        self.assertEqual(rec.requests[0]["payload"]["temperature"], 0.0)

    def test_a_custom_base_url_is_honoured(self):
        rec = Recorder(reply(GOOD))
        r = OpenAICompatReasoner("groq", api_key="k", base_url="http://localhost:8000/v1",
                                 transport=rec)
        r.propose(SIGNALS, ctx())
        self.assertTrue(rec.requests[0]["url"].startswith("http://localhost:8000/v1"))

    def test_model_can_come_from_the_environment(self):
        saved = os.environ.get("GROQ_MODEL")
        os.environ["GROQ_MODEL"] = "env-model"
        try:
            r = OpenAICompatReasoner("groq", api_key="k", transport=Recorder())
            self.assertEqual(r.model, "env-model")
        finally:
            os.environ.pop("GROQ_MODEL", None)
            if saved is not None:
                os.environ["GROQ_MODEL"] = saved


class TestDegradation(unittest.TestCase):
    def test_it_drops_to_json_object_when_a_schema_is_rejected(self):
        """Not every model supports json_schema. Refusing to adapt would record a
        formatting limitation as a reasoning failure."""
        rec = Recorder((400, json.dumps({"error": {"message": "json_schema unsupported"}})),
                       reply(GOOD))
        r = OpenAICompatReasoner("groq", api_key="k", transport=rec)
        patches = r.propose(SIGNALS, ctx())
        self.assertEqual(len(rec.requests), 2)
        self.assertEqual(rec.requests[0]["payload"]["response_format"]["type"],
                         "json_schema")
        self.assertEqual(rec.requests[1]["payload"]["response_format"]["type"],
                         "json_object")
        self.assertTrue(any(ru.op == "rename" for p in patches for ru in p.add))

    def test_the_degraded_mode_adds_the_format_instruction_to_the_prompt(self):
        rec = Recorder((400, "{}"), reply(GOOD))
        r = OpenAICompatReasoner("groq", api_key="k", transport=rec)
        r.propose(SIGNALS, ctx())
        system = rec.requests[1]["payload"]["messages"][0]["content"]
        self.assertIn("single JSON object", system)

    def test_it_stops_retrying_on_a_non_format_error(self):
        rec = Recorder((401, json.dumps({"error": {"message": "invalid key"}})))
        r = OpenAICompatReasoner("groq", api_key="k", transport=rec)
        r.propose(SIGNALS, ctx())
        self.assertEqual(len(rec.requests), 1, "an auth failure is not retryable")
        self.assertIn("401", r.last_error or "")

    def test_an_unreachable_endpoint_degrades_to_heuristics(self):
        """What a blocked egress policy looks like from inside the process."""
        rec = Recorder((0, json.dumps({"error": {"message": "OSError: denied"}})))
        r = OpenAICompatReasoner("groq", api_key="k", transport=rec)
        patches = r.propose(SIGNALS, ctx())
        self.assertIn("denied", r.last_error or "")
        self.assertIsInstance(patches, list)


class TestResponseHandling(unittest.TestCase):
    def _propose(self, content: str):
        r = OpenAICompatReasoner("groq", api_key="k", transport=Recorder(reply(content)))
        return r, r.propose(SIGNALS, ctx())

    def test_a_good_response_becomes_a_patch(self):
        _r, patches = self._propose(GOOD)
        self.assertTrue(any(ru.op == "rename" and ru.args["to"] == "new_name"
                            for p in patches for ru in p.add))

    def test_fenced_json_is_recovered(self):
        _r, patches = self._propose(f"```json\n{GOOD}\n```")
        self.assertTrue(any(ru.op == "rename" for p in patches for ru in p.add))

    def test_json_after_preamble_is_recovered(self):
        _r, patches = self._propose(f"Here is my answer:\n{GOOD}")
        self.assertTrue(any(ru.op == "rename" for p in patches for ru in p.add))

    def test_an_empty_candidate_list_is_treated_as_declining_to_guess(self):
        """The prompt asks for no candidates rather than an invented destination,
        so an empty list is an answer, not a malfunction."""
        r, _patches = self._propose('{"candidates": []}')
        self.assertIsNone(r.last_error)

    def test_unusable_output_is_recorded_and_degraded(self):
        r, patches = self._propose("I cannot help with that.")
        self.assertIn("not usable JSON", r.last_error or "")
        self.assertIsInstance(patches, list)

    def test_heuristic_candidates_are_appended_as_a_backstop(self):
        model = EnvironmentModel()
        model.adopt(CONTRACT)
        c = Context(policy=Policy(), model=model,
                    canonical={"old_name": "V"}, tick=0)
        baseline = len(HeuristicReasoner().propose(SIGNALS, c))
        r = OpenAICompatReasoner("groq", api_key="k", transport=Recorder(reply(GOOD)))
        self.assertGreater(len(r.propose(SIGNALS, c)), baseline - 1)


class _NoFallback:
    """Proposes nothing, so an assertion sees only the model's contribution.

    Needed because the heuristic backstop independently proposes a valid rename
    for these signals: with it in place, a test cannot tell a rejected model rule
    from an accepted one.
    """

    name = "none"

    def propose(self, signals, ctx):
        return []


class TestUntrustedResponses(unittest.TestCase):
    """A model response is input, not instruction. None of these may get through."""

    def _patches(self, payload: dict):
        r = OpenAICompatReasoner("groq", api_key="k",
                                 transport=Recorder(reply(json.dumps(payload))),
                                 fallback=_NoFallback())
        return r.propose(SIGNALS, ctx())

    def _ops(self, payload: dict) -> set[str]:
        return {ru.op for p in self._patches(payload) for ru in p.add}

    def test_an_op_outside_the_allow_list_is_discarded(self):
        ops = self._ops({"candidates": [
            {"rationale": "x", "remove_rule_ids": [],
             "add_rules": [{"op": "exec_shell", "args_json": '{"cmd": "rm -rf /"}'}]}]})
        self.assertNotIn("exec_shell", ops)

    def test_a_rule_missing_required_args_is_discarded(self):
        ops = self._ops({"candidates": [
            {"rationale": "x", "remove_rule_ids": [],
             "add_rules": [{"op": "rename", "args_json": '{"from": "a"}'}]}]})
        self.assertNotIn("rename", ops)

    def test_malformed_args_json_is_discarded(self):
        ops = self._ops({"candidates": [
            {"rationale": "x", "remove_rule_ids": [],
             "add_rules": [{"op": "rename", "args_json": "not json"}]}]})
        self.assertNotIn("rename", ops)

    def test_a_removal_of_a_rule_that_does_not_exist_is_ignored(self):
        patches = self._patches({"candidates": [
            {"rationale": "x", "remove_rule_ids": ["r999"], "add_rules": []}]})
        self.assertTrue(all("r999" not in p.remove for p in patches))

    def test_a_well_formed_rule_still_gets_through(self):
        """The counterpart to the rejections above: the filter is not simply
        refusing everything."""
        self.assertIn("rename", self._ops(json.loads(GOOD)))

    def test_structurally_wrong_responses_do_not_raise(self):
        for payload in ({"candidates": "nope"}, {"candidates": [None]},
                        {"candidates": [{"add_rules": "x"}]}, {}, {"candidates": [[]]}):
            self.assertIsInstance(self._patches(payload), list)


class TestSharedPlumbing(unittest.TestCase):
    def test_the_prompt_forbids_writing_into_a_path_parameter(self):
        self.assertIn("path parameter", hypotheses.SYSTEM)

    def test_the_prompt_asks_for_no_candidates_rather_than_a_guess(self):
        self.assertIn("rather than inventing a destination", hypotheses.SYSTEM)

    def test_extract_json_handles_the_shapes_models_actually_emit(self):
        self.assertEqual(hypotheses.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(hypotheses.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(hypotheses.extract_json('sure:\n{"a": 1}\nhope that helps'),
                         {"a": 1})
        self.assertIsNone(hypotheses.extract_json(""))
        self.assertIsNone(hypotheses.extract_json("no json here"))
        self.assertIsNone(hypotheses.extract_json("[1,2,3]"))

    def test_every_tier_shares_one_allow_list(self):
        from sell.reasoner import KNOWN_OPS as from_reasoner
        self.assertIs(from_reasoner, hypotheses.KNOWN_OPS)

    def test_build_reasoner_exposes_the_endpoint_tiers(self):
        r = build_reasoner("groq")
        self.assertTrue(r.name.startswith("groq:"))

    def test_build_reasoner_rejects_an_unknown_tier(self):
        with self.assertRaises(KeyError):
            build_reasoner("definitely-not-a-tier")

    def test_auto_does_not_claim_a_tier_it_cannot_reach(self):
        saved = {k: os.environ.pop(k, None) for k in
                 ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GROQ_API_KEY",
                  "TOGETHER_API_KEY", "OPENROUTER_API_KEY", "LOCAL_API_KEY")}
        try:
            self.assertEqual(build_reasoner("auto").name, "heuristic")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
