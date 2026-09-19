"""Detection. Note what is *not* here: the agent's own confidence.

Every signal below is either something the environment said, or a check that
can be evaluated deterministically. A model's self-reported certainty is
excluded on purpose -- it is least reliable exactly on the novel inputs this
system exists to catch, so it is not permitted to gate anything.

The sensors are ordered by how early they fire:

  1. changelog  -- the change is announced before it lands (cheapest of all)
  2. spec diff  -- the change is visible in the contract before a task fails
  3. invariant  -- a local pre-flight check fails before we transmit
  4. rejection  -- the environment returns an error
  5. ledger     -- nothing looked wrong until the books disagreed, ticks later
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import EnvironmentModel


@dataclass
class Signal:
    kind: str
    source: str            # changelog | spec | invariant | rejection | ledger
    detail: dict[str, Any] = field(default_factory=dict)
    tick: int = 0

    def summary(self) -> str:
        d = self.detail
        f = d.get("field")
        head = f"[{self.source}] {self.kind}"
        if f:
            head += f" on '{f}'"
        if "was" in d or "now" in d:
            head += f": {d.get('was')!r} -> {d.get('now')!r}"
        elif d.get("message"):
            head += f": {d['message']}"
        elif d.get("note"):
            head += f": {d['note']}"
        return head


def sense_changelog(api, model, tick: int) -> list[Signal]:
    """Read what the partner published. The only sensor that can fire before
    any task has failed, which is the difference between adapting and recovering."""
    out: list[Signal] = []
    for entry in api.get_changelog():
        key = (entry["version"], entry["note"])
        if key in model.seen_changelog:
            continue
        model.seen_changelog.add(key)
        out.append(Signal("published_change", "changelog",
                          {"note": entry["note"], "upcoming": entry.get("upcoming", False),
                           "version": entry["version"]}, tick))
    return out


def sense_spec(api, model, tick: int) -> list[Signal]:
    observed = api.get_spec()
    if not model.spec:
        model.adopt(observed)
        return []
    changes = model.diff(observed)
    model.adopt(observed)
    return [Signal(c["kind"], "spec", c, tick) for c in changes]


def sense_staging(api, model, tick: int) -> list[Signal]:
    """Diff production against the partner's staged next version.

    The earliest possible detection. A fix found here can be fully verified
    against the future contract before a single production task meets it -- but
    it cannot be *deployed* early, because it would break the version still
    live. So it is held, pre-verified, and applied the instant the change lands.
    """
    if not getattr(api, "has_staged", lambda: False)():
        return []
    staged = api.get_spec(staged=True)
    key = ("staged", staged["version"])
    if key in model.seen_changelog:
        return []
    model.seen_changelog.add(key)
    live = {"fields": model.spec.get("fields", {})}
    probe = EnvironmentModel()
    probe.spec = live
    return [Signal(c["kind"], "staging", c, tick) for c in probe.diff(staged)]


def sense_invariants(payload: dict[str, Any], model, tick: int) -> list[Signal]:
    """Cheap deterministic checks against the contract we believe we are under,
    run before transmitting. Catches most drift without spending a real task."""
    out: list[Signal] = []
    fields = model.spec.get("fields", {})
    if not fields:
        return out
    for name in payload:
        if name not in fields:
            out.append(Signal("unknown_field", "invariant",
                              {"field": name, "known_fields": sorted(fields),
                               "message": f"'{name}' is not in the current contract"}, tick))
    for name, spec in fields.items():
        if spec.get("required") and name not in payload:
            out.append(Signal("missing_required_field", "invariant",
                              {"field": name, "expected_type": spec.get("type"),
                               "allowed": spec.get("allowed"),
                               "message": f"'{name}' is required and absent"}, tick))
            continue
        if name not in payload:
            continue
        v = payload[name]
        allowed = spec.get("allowed")
        if allowed is not None and v not in allowed:
            out.append(Signal("value_not_allowed", "invariant",
                              {"field": name, "allowed": list(allowed), "got": v,
                               "message": f"{v!r} is not an accepted value"}, tick))
        if spec.get("type") == "integer" and not isinstance(v, int):
            out.append(Signal("type_mismatch", "invariant",
                              {"field": name, "expected_type": "integer",
                               "message": f"'{name}' must be an integer"}, tick))
        pattern = spec.get("pattern")
        if pattern and isinstance(v, str):
            import re
            if not re.match(pattern, v):
                out.append(Signal("format_invalid", "invariant",
                                  {"field": name, "pattern": pattern, "got": v,
                                   "message": f"'{name}' does not match {pattern}"}, tick))
    return out


def sense_rejection(error: dict[str, Any], tick: int) -> Signal:
    return Signal(error["code"], "rejection", dict(error), tick)


def sense_ledger(finding: dict[str, Any], tick: int) -> Signal:
    return Signal(finding["type"], "ledger", dict(finding), tick)
