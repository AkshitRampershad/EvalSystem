"""Provider-neutral plumbing for model-generated hypotheses.

The prompt, the schema and — most importantly — the parsing live here rather
than in any one provider's module, so every model tier is held to the same
validation. That matters: parsing is the security-relevant step. A model's
output is untrusted input, and an op or argument that was never allow-listed
must not reach the policy pipeline no matter which vendor produced it.
"""

from __future__ import annotations

import json
from typing import Any

from .policy import Patch, Rule
from .sensors import Signal

# The complete set of transforms a model may propose. Anything outside this is
# discarded rather than interpreted.
KNOWN_OPS: dict[str, set[str]] = {
    "rename": {"from", "to"},
    "drop": {"field"},
    "set_const": {"field", "value"},
    "divide_int": {"field", "by"},
    "multiply": {"field", "by"},
    "map_value": {"field", "mapping"},
    "suffix": {"field", "suffix"},
}

PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "string"},
                    "remove_rule_ids": {"type": "array", "items": {"type": "string"}},
                    "add_rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": sorted(KNOWN_OPS)},
                                "args_json": {
                                    "type": "string",
                                    "description": "JSON object of arguments for the op",
                                },
                            },
                            "required": ["op", "args_json"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["rationale", "remove_rule_ids", "add_rules"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

SYSTEM = """You maintain an integration between an internal canonical record \
and a partner API whose contract changes without warning.

You are given: the signals that just fired, the contract as currently observed, \
the policy (an ordered transform pipeline) the integration is running, and one \
canonical record.

Propose candidate patches to the policy that would make the integration correct \
again. Order them best-first. Two or three well-reasoned candidates beat ten \
speculative ones.

Available ops and their args:
  rename     {"from": str, "to": str}
  drop       {"field": str}
  set_const  {"field": str, "value": any}
  divide_int {"field": str, "by": int}
  multiply   {"field": str, "by": int}
  map_value  {"field": str, "mapping": {old: new}}
  suffix     {"field": str, "suffix": str}

Rules apply in order to a copy of the canonical record. Removing an existing \
rule is often the right fix -- prefer it to piling a new rule on top of a stale \
one. Field names may carry a location sigil: "?name" is a query parameter, \
"{name}" a path parameter, "~name" a header, and a bare name is a request body \
field. Never move a value into a path parameter: that changes which resource the \
request addresses.

Every candidate will be tested against the partner's published contract and an \
accumulated regression suite before anything is adopted, so propose the \
hypothesis you believe is right rather than the one that is safest to be wrong \
about. If the contract genuinely does not say where a capability went, say so by \
returning no candidates rather than inventing a destination -- a value written \
into an unrelated field validates perfectly and is worse than no change at all."""

RESPONSE_INSTRUCTION = (
    "Reply with a single JSON object of the form "
    '{"candidates": [{"rationale": str, "remove_rule_ids": [str], '
    '"add_rules": [{"op": str, "args_json": str}]}]}. '
    "No prose, no code fences."
)


def build_prompt(signals: list[Signal], ctx: Any) -> str:
    """The task, as data. Identical across providers so tiers stay comparable."""
    return json.dumps({
        "signals": [{"kind": s.kind, "source": s.source, "detail": s.detail}
                    for s in signals],
        "observed_contract": ctx.model.spec,
        "current_policy": json.loads(ctx.policy.to_json()),
        "canonical_record": ctx.canonical,
    }, indent=2, default=str)


def extract_json(text: str) -> dict[str, Any] | None:
    """Recover the JSON object from a response, tolerating fences and preamble.

    Models that lack strict structured output wrap JSON in prose or code fences
    often enough that failing on it would misattribute a formatting quirk to a
    reasoning failure.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, depth = text.find("{"), 0
    if start < 0:
        return None
    for i in range(start, len(text)):
        depth += 1 if text[i] == "{" else (-1 if text[i] == "}" else 0)
        if depth == 0:
            try:
                parsed = json.loads(text[start:i + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def to_patch(candidate: dict[str, Any], signals: list[Signal], ctx: Any,
             reasoner: str) -> Patch | None:
    """One validated patch, or None. Never raises on hostile input."""
    if not isinstance(candidate, dict):
        return None
    provenance = {"reasoner": reasoner, "tick": getattr(ctx, "tick", 0),
                  "signals": [s.kind for s in signals],
                  "rationale": str(candidate.get("rationale", ""))[:400]}
    rules: list[Rule] = []
    for raw in candidate.get("add_rules") or []:
        if not isinstance(raw, dict):
            return None
        op = raw.get("op")
        if op not in KNOWN_OPS:
            return None
        args = raw.get("args_json")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                return None
        if not isinstance(args, dict) or not KNOWN_OPS[op] <= set(args):
            return None
        rules.append(Rule(op, args, provenance=provenance))

    valid_ids = {r.id for r in ctx.policy.rules}
    removes = [rid for rid in (candidate.get("remove_rule_ids") or [])
               if isinstance(rid, str) and rid in valid_ids]
    if not rules and not removes:
        return None
    return Patch(add=rules, remove=removes,
                 rationale=str(candidate.get("rationale", ""))[:400])


def to_patches(data: dict[str, Any], signals: list[Signal], ctx: Any,
               reasoner: str) -> list[Patch]:
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return []
    out = [to_patch(c, signals, ctx, reasoner) for c in candidates]
    return [p for p in out if p is not None]
