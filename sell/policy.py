"""The agent's learned behaviour, as data rather than weights.

A policy is an ordered pipeline of small, named transforms that turn an
internal canonical record into a payload the partner accepts. Learning means
adding, removing or altering these rules.

Keeping the learned behaviour in this form buys three things that fine-tuning
does not: it is inspectable (a reviewer can read what the agent believes), it
is revertible (every version is retained), and every rule carries provenance
back to the signal that justified it.
"""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field
from typing import Any

_ids = itertools.count(1)


@dataclass
class Rule:
    op: str
    args: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    # True once the environment itself has confirmed this rule, as opposed to
    # it merely having been asserted by the reasoner or by a human.
    verified: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"r{next(_ids)}"

    def describe(self) -> str:
        a = self.args
        if self.op == "rename":
            return f"rename {a['from']} -> {a['to']}"
        if self.op == "drop":
            return f"drop {a['field']}"
        if self.op == "set_const":
            return f"set {a['field']} = {a['value']!r}"
        if self.op == "divide_int":
            return f"{a['field']} /= {a['by']} (integer)"
        if self.op == "multiply":
            return f"{a['field']} *= {a['by']}"
        if self.op == "map_value":
            return f"map {a['field']} values {a['mapping']}"
        if self.op == "suffix":
            return f"{a['field']} += {a['suffix']!r}"
        return f"{self.op} {a}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "op": self.op, "args": self.args,
                "verified": self.verified, "provenance": self.provenance}


def _apply_rule(rec: dict[str, Any], rule: Rule) -> dict[str, Any]:
    a = rule.args
    out = dict(rec)
    if rule.op == "rename":
        if a["from"] in out:
            out[a["to"]] = out.pop(a["from"])
    elif rule.op == "drop":
        out.pop(a["field"], None)
    elif rule.op == "set_const":
        out[a["field"]] = a["value"]
    elif rule.op == "divide_int":
        if a["field"] in out and isinstance(out[a["field"]], int):
            out[a["field"]] = out[a["field"]] // a["by"]
    elif rule.op == "multiply":
        if a["field"] in out and isinstance(out[a["field"]], int):
            out[a["field"]] = out[a["field"]] * a["by"]
    elif rule.op == "map_value":
        if a["field"] in out:
            out[a["field"]] = a["mapping"].get(out[a["field"]], out[a["field"]])
    elif rule.op == "suffix":
        v = out.get(a["field"])
        if isinstance(v, str) and not v.endswith(a["suffix"]):
            out[a["field"]] = v + a["suffix"]
    else:
        raise ValueError(f"unknown op {rule.op}")
    return out


@dataclass
class Patch:
    """A proposed change to the policy: what to add, what to take away."""
    add: list[Rule] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    rationale: str = ""

    def describe(self) -> str:
        bits = [f"+{r.describe()}" for r in self.add]
        bits += [f"-{rid}" for rid in self.remove]
        return ", ".join(bits) or "(no-op)"


class Policy:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules: list[Rule] = rules or []
        self.version = 1
        self.history: list[tuple[int, list[Rule], str]] = []

    # ---- evaluation ----

    def render(self, canonical: dict[str, Any]) -> dict[str, Any]:
        payload = dict(canonical)
        for rule in self.rules:
            payload = _apply_rule(payload, rule)
        return payload

    def preview(self, patch: Patch) -> "Policy":
        """A copy with the patch applied, for use in an experiment."""
        p = Policy(copy.deepcopy(self.rules))
        p.rules = [r for r in p.rules if r.id not in patch.remove]
        p.rules.extend(copy.deepcopy(patch.add))
        p.version = self.version
        return p

    # ---- mutation (always versioned, always revertible) ----

    def commit(self, patch: Patch, note: str = "") -> int:
        self.history.append((self.version, copy.deepcopy(self.rules), note))
        self.rules = [r for r in self.rules if r.id not in patch.remove]
        self.rules.extend(patch.add)
        self.version += 1
        return self.version

    def revert(self) -> int:
        if not self.history:
            return self.version
        version, rules, _ = self.history.pop()
        self.rules = rules
        self.version = version
        return self.version

    def to_json(self) -> str:
        return json.dumps({"version": self.version,
                           "rules": [r.to_dict() for r in self.rules]}, indent=2)


def initial_policy() -> Policy:
    """What a human shipped on day one, against spec version 1.

    Everything after this point the agent works out for itself.
    """
    seed = {"source": "initial_implementation", "tick": 0}
    return Policy([
        Rule("rename", {"from": "amount_cents", "to": "amount"},
             provenance=seed, verified=True),
        Rule("divide_int", {"field": "amount", "by": 100},
             provenance=seed, verified=True),
        Rule("drop", {"field": "currency"}, provenance=seed, verified=True),
    ])
