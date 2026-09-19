"""Provider-neutral plumbing for model-generated hypotheses.

The prompt, the schema and — most importantly — the parsing live here rather
than in any one provider's module, so every model tier is held to the same
validation. That matters: parsing is the security-relevant step. A model's
output is untrusted input, and an op or argument that was never allow-listed
must not reach the policy pipeline no matter which vendor produced it.
"""

from __future__ import annotations

import difflib
import json
import re
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


# A real contract can run to hundreds of fields, and sending all of it cost about
# 2,750 tokens per case -- 195k for a 71-case benchmark run, which does not fit in
# a free tier's daily allowance at all. Nearly all of that is fields the task has
# no bearing on.
RELATED_CUTOFF = 0.55
MAX_RELATED = 12


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) > 2}


def _relevant_fields(signals: list[Signal], ctx: Any) -> tuple[dict[str, Any], list[str]]:
    """Full detail for the fields that bear on this decision; names for the rest.

    Keeping the remaining names in the prompt matters: a rename destination has to
    be discoverable, and a model cannot propose a field it was never shown. What
    it does not need is the type, description and constraints of 200 unrelated
    ones.
    """
    fields: dict[str, Any] = ctx.model.spec.get("fields", {}) or {}
    focus = {s.detail.get("field") for s in signals if s.detail.get("field")}
    keep: set[str] = {name for name in focus if name in fields}
    keep |= {name for name in fields if name in (ctx.canonical or {})}
    keep |= {name for name, facts in fields.items() if facts.get("required")}

    # Plausible destinations for anything that moved: a shared name token, or a
    # close spelling. Both are how a rename is actually spotted.
    focus_tokens = set().union(*(_tokens(f) for f in focus)) if focus else set()
    related: list[str] = []
    for name in fields:
        if name in keep:
            continue
        if focus_tokens & _tokens(name):
            related.append(name)
    for name in focus:
        related += difflib.get_close_matches(name, [n for n in fields if n not in keep],
                                            n=3, cutoff=RELATED_CUTOFF)
    for name in related[:MAX_RELATED]:
        keep.add(name)

    detailed = {name: fields[name] for name in sorted(keep) if name in fields}
    remaining = sorted(n for n in fields if n not in detailed)
    return detailed, remaining


# Detail keys worth sending. Everything else a signal carries is either redundant
# with the contract section (`spec`, `known_fields`) or prose that does not change
# the decision.
_SIGNAL_KEYS = ("field", "impact", "was", "now", "added", "removed", "allowed",
                "got", "expected_type", "pattern", "max_length", "operation")
_TEXT_KEYS = ("message", "note", "was_text", "now_text")
_TEXT_LIMIT = 90


def _signal_for_prompt(signal: Signal) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": signal.kind, "source": signal.source}
    for key in _SIGNAL_KEYS:
        if signal.detail.get(key) is not None:
            out[key] = signal.detail[key]
    for key in _TEXT_KEYS:
        value = signal.detail.get(key)
        if value:
            out[key] = str(value)[:_TEXT_LIMIT]
    # `spec` repeats the field's facts, which the contract section already carries
    # in full, and `known_fields` repeats the whole field list once per signal.
    required = (signal.detail.get("spec") or {}).get("required")
    if required:
        out["newly_required"] = True
    return out


def build_prompt(signals: list[Signal], ctx: Any) -> str:
    """The task, as data. Identical across providers so tiers stay comparable."""
    detailed, remaining = _relevant_fields(signals, ctx)
    contract: dict[str, Any] = {
        "operation": ctx.model.spec.get("operation"),
        "version": ctx.model.spec.get("version"),
        "fields": detailed,
    }
    if remaining:
        contract["other_accepted_field_names"] = remaining
        contract["note"] = ("Fields bearing on these signals are described in "
                            "full. The rest are listed by name only; ask for "
                            "nothing outside both lists.")
    return json.dumps({
        "signals": [_signal_for_prompt(s) for s in signals],
        "observed_contract": contract,
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
