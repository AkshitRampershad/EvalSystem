"""Structural diff between two real contract versions, as sensor signals.

Emits the same `Signal` kinds the simulated sensors emit, so the reasoner needs
no special case for real data. Adds two things the simulation did not need:

  * a breaking/non-breaking verdict, because at real scale most changes are
    additive noise and only some can break a caller;
  * a `blast` field naming who is affected -- a removed field only matters if
    you were sending it, and that is what turns a diff into a work queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..sensors import Signal

# A change that can break a caller who was already working, versus one that
# only matters to a caller who wants the new capability.
BREAKING = "breaking"
ADDITIVE = "additive"
COSMETIC = "cosmetic"

_COMPARED = ("type", "required", "required_if_present", "allowed", "pattern",
             "max_length", "description_digest")


def _verdict(kind: str, was: Any, now: Any) -> str:
    if kind == "field_removed":
        return BREAKING
    if kind == "field_added":
        # A newly required field breaks every existing caller; an optional one
        # breaks nobody.
        return BREAKING if (now or {}).get("required") else ADDITIVE
    if kind == "type_changed":
        return BREAKING
    if kind in ("required_changed", "required_if_present_changed"):
        return BREAKING if now else ADDITIVE
    if kind == "allowed_changed":
        lost = set(was or []) - set(now or [])
        return BREAKING if lost else ADDITIVE
    if kind == "max_length_changed":
        try:
            return BREAKING if (now is not None and was is not None
                                and now < was) else ADDITIVE
        except TypeError:
            return ADDITIVE
    if kind == "pattern_changed":
        # A pattern that was absent and now exists, or that changed at all, can
        # reject input that previously passed. Not safely decidable statically.
        return BREAKING
    if kind == "description_changed":
        # Cosmetic by default -- but this is the class the silent unit change
        # hid behind, so it is reported, never dropped.
        return COSMETIC
    return ADDITIVE


def diff_contracts(old: dict[str, Any], new: dict[str, Any], *,
                   tick: int = 0, source: str = "spec") -> list[Signal]:
    o, n = old.get("fields", {}), new.get("fields", {})
    signals: list[Signal] = []

    def emit(kind: str, field: str, detail: dict[str, Any], was=None, now=None) -> None:
        detail = {"field": field, "operation": new.get("operation", ""), **detail}
        detail["impact"] = _verdict(kind, was, now)
        signals.append(Signal(kind, source, detail, tick))

    for field in sorted(set(n) - set(o)):
        emit("field_added", field, {"spec": n[field]}, now=n[field])
    for field in sorted(set(o) - set(n)):
        emit("field_removed", field, {"spec": o[field]}, was=o[field])
    for field in sorted(set(o) & set(n)):
        a, b = o[field], n[field]
        for key in _COMPARED:
            if a.get(key) == b.get(key):
                continue
            kind = "description_changed" if key == "description_digest" else f"{key}_changed"
            detail: dict[str, Any] = {"was": a.get(key), "now": b.get(key)}
            if key == "description_digest":
                detail["was_text"] = a.get("description")
                detail["now_text"] = b.get("description")
            if key == "allowed":
                detail["added"] = sorted(set(b.get("allowed") or []) -
                                         set(a.get("allowed") or []))
                detail["removed"] = sorted(set(a.get("allowed") or []) -
                                           set(b.get("allowed") or []))
            emit(kind, field, detail, was=a.get(key), now=b.get(key))
    return signals


@dataclass
class OperationDrift:
    operation: str
    signals: list[Signal]

    @property
    def breaking(self) -> list[Signal]:
        return [s for s in self.signals if s.detail.get("impact") == BREAKING]

    @property
    def additive(self) -> list[Signal]:
        return [s for s in self.signals if s.detail.get("impact") == ADDITIVE]

    @property
    def cosmetic(self) -> list[Signal]:
        return [s for s in self.signals if s.detail.get("impact") == COSMETIC]


def diff_all(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]], *,
             tick: int = 0) -> tuple[list[OperationDrift], list[str], list[str]]:
    """Diff every operation present in both. Also reports operations that
    appeared or vanished entirely, which no per-field diff can see."""
    drifts: list[OperationDrift] = []
    for op in sorted(set(old) & set(new)):
        signals = diff_contracts(old[op], new[op], tick=tick)
        if signals:
            drifts.append(OperationDrift(op, signals))
    return (drifts, sorted(set(new) - set(old)), sorted(set(old) - set(new)))


def summarise(drifts: list[OperationDrift]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_impact: dict[str, int] = {BREAKING: 0, ADDITIVE: 0, COSMETIC: 0}
    for d in drifts:
        for s in d.signals:
            by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
            by_impact[s.detail.get("impact", ADDITIVE)] += 1
    return {
        "operations_changed": len(drifts),
        "operations_with_breaking_changes": len([d for d in drifts if d.breaking]),
        "signals": sum(len(d.signals) for d in drifts),
        "by_impact": by_impact,
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
    }
