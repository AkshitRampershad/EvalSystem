"""Durable memory: what the agent believes, what it did, and what came back.

Nothing above this layer works without it. The environment model is what makes
change detectable (you can only diff against something you recorded); the
trajectory log is what makes a signal arriving three ticks late attributable to
the decision that caused it; the golden set is what stops a fix for today's
drift from quietly undoing last week's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# What the agent believes about the environment
# --------------------------------------------------------------------------

class EnvironmentModel:
    def __init__(self) -> None:
        self.spec: dict[str, Any] = {}
        self.spec_version: int | None = None
        self.seen_changelog: set[tuple[int, str]] = set()

    def diff(self, observed: dict[str, Any]) -> list[dict[str, Any]]:
        """Structural changes between what we believed and what we just saw."""
        if not self.spec:
            return []
        old, new = self.spec.get("fields", {}), observed.get("fields", {})
        changes: list[dict[str, Any]] = []
        for name in sorted(set(new) - set(old)):
            changes.append({"kind": "field_added", "field": name, "spec": new[name]})
        for name in sorted(set(old) - set(new)):
            changes.append({"kind": "field_removed", "field": name, "spec": old[name]})
        for name in sorted(set(old) & set(new)):
            o, n = old[name], new[name]
            for key in ("type", "required", "allowed", "pattern", "description"):
                if o.get(key) != n.get(key):
                    changes.append({"kind": f"{key}_changed", "field": name,
                                    "was": o.get(key), "now": n.get(key)})
        return changes

    def adopt(self, observed: dict[str, Any]) -> None:
        self.spec = json.loads(json.dumps(observed))
        self.spec_version = observed.get("version")

    def required_fields(self) -> list[str]:
        return [n for n, s in self.spec.get("fields", {}).items() if s.get("required")]

    def field(self, name: str) -> dict[str, Any] | None:
        return self.spec.get("fields", {}).get(name)


# --------------------------------------------------------------------------
# What the agent did, and what happened afterwards
# --------------------------------------------------------------------------

@dataclass
class Trajectory:
    task_id: str
    tick: int
    cluster: str
    canonical: dict[str, Any]
    payload: dict[str, Any]
    policy_version: int
    ok: bool
    record_id: str | None = None
    error: dict[str, Any] | None = None
    # filled in later, possibly much later
    late_outcome: str | None = None
    rolled_back: bool = False


class TrajectoryStore:
    def __init__(self) -> None:
        self.rows: list[Trajectory] = []
        self._by_record: dict[str, Trajectory] = {}

    def log(self, t: Trajectory) -> None:
        self.rows.append(t)
        if t.record_id and t.record_id != "sandbox":
            self._by_record[t.record_id] = t

    def attribute(self, record_id: str) -> Trajectory | None:
        """Map a late signal back to the decision that produced it.

        This is the bookkeeping that makes delayed credit assignment work. An
        agent asked to reason its way back to the cause will guess; a lookup
        table will not.
        """
        return self._by_record.get(record_id)

    def replay(self, task_id: str) -> Trajectory | None:
        return next((t for t in self.rows if t.task_id == task_id), None)


# --------------------------------------------------------------------------
# The regression suite, which only ever grows
# --------------------------------------------------------------------------

@dataclass
class GoldenCase:
    case_id: str
    canonical: dict[str, Any]
    # semantic assertions on the rendered payload, learned from late signals
    assertions: list[dict[str, Any]] = field(default_factory=list)
    origin: str = ""


class GoldenSet:
    def __init__(self) -> None:
        self.cases: dict[str, GoldenCase] = {}

    def record_success(self, case_id: str, canonical: dict[str, Any],
                       origin: str = "") -> None:
        if case_id not in self.cases:
            self.cases[case_id] = GoldenCase(case_id, json.loads(json.dumps(canonical)),
                                             origin=origin)

    def add_assertion(self, case_id: str, assertion: dict[str, Any]) -> None:
        """A late signal became a permanent, locally checkable test.

        The reconciliation told us what the ledger *should* have read; from here
        on, any candidate policy that renders this case differently is wrong,
        and we find that out in milliseconds instead of in three ticks.
        """
        case = self.cases.get(case_id)
        if case is None:
            return
        case.assertions = [a for a in case.assertions
                           if a.get("field") != assertion.get("field")]
        case.assertions.append(assertion)

    def check_assertions(self, policy, case: GoldenCase) -> str | None:
        payload = policy.render(case.canonical)
        for a in case.assertions:
            got = payload.get(a["field"])
            if got != a["equals"]:
                return (f"{case.case_id}: {a['field']} rendered {got!r}, "
                        f"expected {a['equals']!r}")
        return None

    def __len__(self) -> int:
        return len(self.cases)


def dump_state(path: Path, policy, model: EnvironmentModel,
               store: TrajectoryStore, golden: GoldenSet) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "policy": json.loads(policy.to_json()),
        "believed_spec_version": model.spec_version,
        "tasks": len(store.rows),
        "golden_cases": {c.case_id: {"assertions": c.assertions, "origin": c.origin}
                         for c in golden.cases.values()},
    }, indent=2))
