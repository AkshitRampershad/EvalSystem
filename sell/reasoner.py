"""Hypothesis generation: given a signal, what might the new correct behaviour be?

Two implementations, same interface:

  HeuristicReasoner -- pattern-matches the failure shapes we anticipated.
      Fast, free, offline, and completely blind to anything not on its list.

  ClaudeReasoner    -- reads the signal, the contract and the current policy and
      proposes candidate patches. Handles shapes nobody enumerated in advance,
      including prose deprecation notices.

Neither is trusted. A proposal from either is a *hypothesis*, and the only thing
that promotes a hypothesis to a rule is the environment accepting it in an
experiment (see experiment.py). That separation is what makes it safe to let a
language model write the agent's behaviour.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .policy import Patch, Rule
from .sensors import Signal

MODEL = "claude-opus-5"

KNOWN_OPS = {"rename": {"from", "to"}, "drop": {"field"}, "set_const": {"field", "value"},
             "divide_int": {"field", "by"}, "multiply": {"field", "by"},
             "map_value": {"field", "mapping"}, "suffix": {"field", "suffix"}}


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


@dataclass
class Context:
    policy: Any
    model: Any
    canonical: dict[str, Any]
    tick: int = 0
    trajectory: Any = None
    notes: list[str] = field(default_factory=list)


class HeuristicReasoner:
    name = "heuristic"

    def propose(self, signals: list[Signal], ctx: Context) -> list[Patch]:
        patches: list[Patch] = []
        # Removed-and-added in the same observation is almost always a rename;
        # pairing them across the batch is what makes that inference possible.
        removed = [s for s in signals if s.kind == "field_removed"]
        added = [s for s in signals if s.kind == "field_added"]
        for r in removed:
            names = [a.detail["field"] for a in added]
            best = difflib.get_close_matches(r.detail["field"], names, n=1, cutoff=0.3)
            if best:
                patches.append(self._rename(r.detail["field"], best[0], r, ctx))

        for s in signals:
            patches.extend(self._for_signal(s, ctx))
        return self._dedupe(patches)

    # -- per-signal hypotheses -------------------------------------------

    def _for_signal(self, s: Signal, ctx: Context) -> list[Patch]:
        d, out = s.detail, []
        prov = {"signal": s.kind, "source": s.source, "tick": s.tick,
                "reasoner": self.name, "detail": d}

        if s.kind in ("missing_required_field", "field_added"):
            fld = d.get("field")
            if not fld:
                return out
            if d.get("kind") == "field_added" and not (d.get("spec") or {}).get("required"):
                return out
            # If we are actively dropping a field the contract now demands, the
            # fix is to stop dropping it -- not to invent a value for it.
            drops = [r.id for r in ctx.policy.rules
                     if r.op == "drop" and r.args.get("field") == fld]
            if drops:
                out.append(Patch(remove=drops, rationale=f"stop dropping now-required '{fld}'"))
            if fld in ctx.canonical:
                out.append(Patch(add=[Rule("set_const", {"field": fld,
                                                         "value": ctx.canonical[fld]},
                                           provenance=prov)],
                                 rationale=f"supply '{fld}' from the canonical record"))
            allowed = d.get("allowed") or (d.get("spec") or {}).get("allowed")
            if allowed:
                out.append(Patch(add=[Rule("set_const", {"field": fld, "value": allowed[0]},
                                           provenance=prov)],
                                 rationale=f"supply '{fld}' from the allowed set"))

        elif s.kind == "field_removed":
            # A field we send that the contract no longer has. Distinct from
            # `unknown_field`: that arrives as a rejection after we transmit,
            # this arrives from a spec diff before we do. Same two hypotheses --
            # it moved, or it is gone -- but reached a task earlier.
            #
            # Where it moved to something not textually similar (a real example:
            # Stripe replacing `coupon` with a `discounts` array), string
            # distance cannot find it and only `drop` is proposed. That gap is
            # where a model-backed reasoner earns its cost.
            fld = d["field"]
            known = [n for n in ctx.model.spec.get("fields", {}) if n != fld]
            for target in difflib.get_close_matches(fld, known, n=2, cutoff=0.6):
                out.append(self._rename(fld, target, s, ctx))
            out.append(Patch(add=[Rule("drop", {"field": fld}, provenance=prov)],
                             rationale=f"'{fld}' was removed from the contract"))

        elif s.kind == "unknown_field":
            fld = d["field"]
            known = d.get("known_fields") or list(ctx.model.spec.get("fields", {}))
            best = difflib.get_close_matches(fld, known, n=2, cutoff=0.3)
            for target in best:
                out.append(self._rename(fld, target, s, ctx))
            out.append(Patch(add=[Rule("drop", {"field": fld}, provenance=prov)],
                             rationale=f"'{fld}' is no longer part of the contract"))

        elif s.kind in ("value_not_allowed", "allowed_changed"):
            fld = d["field"]
            new_allowed = d.get("allowed") or d.get("now") or []
            old_allowed = d.get("was") or []
            mapping: dict[str, Any] = {}
            candidates = list(old_allowed) + ([d["got"]] if "got" in d else [])
            for old in candidates:
                match = next((n for n in new_allowed if _norm(n) == _norm(old)), None)
                if match is None:
                    near = difflib.get_close_matches(_norm(old), [_norm(n) for n in new_allowed],
                                                     n=1, cutoff=0.6)
                    match = next((n for n in new_allowed if _norm(n) == near[0]), None) if near else None
                if match is not None and match != old:
                    mapping[old] = match
            if mapping:
                out.append(Patch(add=[Rule("map_value", {"field": fld, "mapping": mapping},
                                           provenance=prov)],
                                 rationale=f"remap '{fld}' onto the new value set"))

        elif s.kind in ("format_invalid", "pattern_changed"):
            fld = d["field"]
            pattern = d.get("pattern") or d.get("now") or ""
            if "T" in pattern and "Z" in pattern:
                out.append(Patch(add=[Rule("suffix", {"field": fld, "suffix": "T00:00:00Z"},
                                           provenance=prov)],
                                 rationale=f"'{fld}' now wants a full timestamp"))

        elif s.kind == "description_changed":
            # The silent class: the contract still validates our old value, and
            # only the prose says the meaning moved underneath us.
            now, was = str(d.get("now", "")).lower(), str(d.get("was", "")).lower()
            fld = d["field"]
            if ("cents" in now or "minor unit" in now) and "dollar" in was:
                divs = [r.id for r in ctx.policy.rules
                        if r.op == "divide_int" and r.args.get("field") == fld]
                if divs:
                    out.append(Patch(remove=divs,
                                     rationale=f"'{fld}' is now in minor units; stop dividing"))

        elif s.kind == "published_change":
            out.extend(self._from_prose(d.get("note", ""), s, ctx))

        elif s.kind == "reconciliation_mismatch":
            out.extend(self._from_ledger(d, s, ctx))

        return out

    def _from_prose(self, note: str, s: Signal, ctx: Context) -> list[Patch]:
        """Deprecation notices are free information, if you can parse them.

        This tier only understands wording it was taught. A drift announced in
        unfamiliar prose slips past here and is caught later by a rejection or
        the ledger -- which is precisely the gap ClaudeReasoner closes.
        """
        out: list[Patch] = []
        quoted = re.findall(r"'([^']+)'", note)
        low = note.lower()
        if "renam" in low and len(quoted) >= 2:
            out.append(self._rename(quoted[0], quoted[1], s, ctx))
        if ("minor unit" in low or "cents" in low) and quoted:
            fld = quoted[0]
            divs = [r.id for r in ctx.policy.rules
                    if r.op == "divide_int" and r.args.get("field") == fld]
            if divs:
                out.append(Patch(remove=divs,
                                 rationale=f"announcement: '{fld}' moved to minor units"))
        return out

    def _from_ledger(self, d: dict[str, Any], s: Signal, ctx: Context) -> list[Patch]:
        """The hardest signal to act on, and the most informative.

        The ledger tells us what it booked and what it should have booked. The
        ratio between them *is* the correction -- no search required.
        """
        booked, expected = d.get("booked_cents", 0), d.get("expected_cents", 0)
        fld = d.get("field", "amount")
        prov = {"signal": s.kind, "source": s.source, "tick": s.tick,
                "reasoner": self.name, "detail": d}
        out: list[Patch] = []
        if booked and expected and expected % booked == 0:
            factor = expected // booked
            divs = [r.id for r in ctx.policy.rules
                    if r.op == "divide_int" and r.args.get("field") == fld
                    and r.args.get("by") == factor]
            if divs:
                out.append(Patch(remove=divs,
                                 rationale=f"ledger is {factor}x our value; drop the /{factor}"))
            else:
                out.append(Patch(add=[Rule("multiply", {"field": fld, "by": factor},
                                           provenance=prov)],
                                 rationale=f"scale '{fld}' by {factor} to match the ledger"))
        return out

    def _rename(self, src: str, dst: str, s: Signal, ctx: Context) -> Patch:
        prov = {"signal": s.kind, "source": s.source, "tick": s.tick,
                "reasoner": self.name, "detail": s.detail}
        # A rename of a field we produce via an earlier rename must retarget that
        # rule instead of stacking a second one on top of it.
        for r in ctx.policy.rules:
            if r.op == "rename" and r.args.get("to") == src:
                return Patch(add=[Rule("rename", {"from": r.args["from"], "to": dst},
                                       provenance=prov)],
                             remove=[r.id],
                             rationale=f"retarget rename {r.args['from']} -> {dst}")
        return Patch(add=[Rule("rename", {"from": src, "to": dst}, provenance=prov)],
                     rationale=f"'{src}' appears to have become '{dst}'")

    @staticmethod
    def _dedupe(patches: list[Patch]) -> list[Patch]:
        seen, out = set(), []
        for p in patches:
            key = (tuple(sorted(p.remove)),
                   tuple(sorted((r.op, json.dumps(r.args, sort_keys=True)) for r in p.add)))
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out


# --------------------------------------------------------------------------

_PATCH_SCHEMA = {
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
                                "op": {"type": "string",
                                       "enum": sorted(KNOWN_OPS)},
                                "args_json": {"type": "string",
                                              "description": "JSON object of arguments for the op"},
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

_SYSTEM = """You maintain an integration between an internal canonical record \
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
one. Every candidate will be tested against the partner's sandbox and against \
an accumulated regression suite before anything is adopted, so propose the \
hypothesis you think is right rather than the one that is safest to be wrong \
about."""


class ClaudeReasoner:
    """Hypothesis generation for drift shapes nobody enumerated in advance."""

    name = "claude"

    def __init__(self, model: str = MODEL, fallback: Any = None) -> None:
        self.model = model
        self.fallback = fallback or HeuristicReasoner()
        self._client = None
        self.calls = 0
        self.last_error: str | None = None

    def _client_or_none(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError:
            self.last_error = "anthropic SDK not installed"
            return None
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            self.last_error = "no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in environment"
            return None
        self._client = anthropic.Anthropic()
        return self._client

    def propose(self, signals: list[Signal], ctx: Context) -> list[Patch]:
        client = self._client_or_none()
        if client is None:
            return self.fallback.propose(signals, ctx)
        prompt = json.dumps({
            "signals": [{"kind": s.kind, "source": s.source, "detail": s.detail}
                        for s in signals],
            "observed_contract": ctx.model.spec,
            "current_policy": json.loads(ctx.policy.to_json()),
            "canonical_record": ctx.canonical,
        }, indent=2, default=str)
        try:
            self.calls += 1
            response = client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": "high",
                               "format": {"type": "json_schema", "schema": _PATCH_SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
            if response.stop_reason == "refusal":
                self.last_error = "model declined the request"
                return self.fallback.propose(signals, ctx)
            text = next(b.text for b in response.content if b.type == "text")
            data = json.loads(text)
        except Exception as exc:                      # noqa: BLE001 - degrade, never crash
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.fallback.propose(signals, ctx)

        patches = [p for p in (self._to_patch(c, signals, ctx)
                               for c in data.get("candidates", [])) if p]
        # The heuristics are cheap; keep them as backstop candidates so a poor
        # generation never leaves the agent with nothing to try.
        return patches + self.fallback.propose(signals, ctx)

    def _to_patch(self, cand: dict[str, Any], signals: list[Signal],
                  ctx: Context) -> Patch | None:
        prov = {"reasoner": self.name, "tick": ctx.tick,
                "signals": [s.kind for s in signals],
                "rationale": cand.get("rationale", "")}
        rules: list[Rule] = []
        for raw in cand.get("add_rules", []):
            op = raw.get("op")
            if op not in KNOWN_OPS:
                return None
            try:
                args = json.loads(raw.get("args_json") or "{}")
            except json.JSONDecodeError:
                return None
            if not isinstance(args, dict) or not KNOWN_OPS[op] <= set(args):
                return None
            rules.append(Rule(op, args, provenance=prov))
        valid_ids = {r.id for r in ctx.policy.rules}
        removes = [rid for rid in cand.get("remove_rule_ids", []) if rid in valid_ids]
        if not rules and not removes:
            return None
        return Patch(add=rules, remove=removes, rationale=cand.get("rationale", ""))


def build_reasoner(kind: str = "auto") -> Any:
    if kind == "heuristic":
        return HeuristicReasoner()
    reasoner = ClaudeReasoner()
    if kind == "claude":
        return reasoner
    # auto: use Claude when credentials exist, heuristics otherwise
    return reasoner if reasoner._client_or_none() is not None else HeuristicReasoner()
