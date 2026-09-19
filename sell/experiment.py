"""Validation. The part that decides whether learning happened.

Generating a plausible fix is easy; knowing it is correct and that it broke
nothing is the whole problem. Every candidate passes two gates before it can
become policy:

  local gate    -- the accumulated assertions still hold. Free, instant, and it
                   is the only thing that catches a regression on a drift whose
                   evidence arrived days late.
  sandbox gate  -- the partner itself accepts this task *and* every case that
                   ever worked before. The environment is the referee; no
                   self-assessment is consulted.

A candidate that fails either gate is discarded silently. A candidate that
passes both is adopted with the evidence attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .policy import Patch


# Exploration is only permissible where an action cannot leave a mark. This is
# the enforcement point for that rule -- not a comment, a gate.
FREE = "free"                  # sandbox / dry run / read: explore without limit
REVERSIBLE = "reversible"      # real write with a working undo: act, keep rollback armed
IRREVERSIBLE = "irreversible"  # never used for exploration, at any budget


def classify(action: str) -> str:
    return {"sandbox_submit": FREE, "get_spec": FREE, "get_changelog": FREE,
            "submit": REVERSIBLE, "rollback": REVERSIBLE,
            "notify_customer": IRREVERSIBLE, "settle_payment": IRREVERSIBLE}.get(
                action, IRREVERSIBLE)  # unknown actions are irreversible by default


@dataclass
class Evidence:
    patch: Patch
    sandbox_calls: int = 0
    golden_cases_checked: int = 0
    rejected_candidates: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.patch.describe()} "
                f"[{self.sandbox_calls} sandbox calls, "
                f"{self.golden_cases_checked} regression cases]")


class Experimenter:
    def __init__(self, api, golden) -> None:
        self.api = api
        self.golden = golden
        self.sandbox_calls = 0
        self.experiments = 0

    def _sandbox_ok(self, policy, canonical: dict[str, Any], tick: int,
                    staging: bool = False) -> str | None:
        assert classify("sandbox_submit") == FREE, "experiments must be side-effect free"
        self.sandbox_calls += 1
        r = self.api.submit(policy.render(canonical), tick=tick, sandbox=True,
                            staging=staging)
        if r.ok:
            return None
        return f"{r.error['code']} on '{r.error.get('field')}'"

    def evaluate(self, policy, candidates: list[Patch], canonical: dict[str, Any],
                 tick: int, staging: bool = False) -> Evidence | None:
        rejected: list[tuple[str, str]] = []
        spent_before = self.sandbox_calls
        for patch in candidates:
            self.experiments += 1
            trial = policy.preview(patch)

            failure = next((self.golden.check_assertions(trial, c)
                            for c in self.golden.cases.values()
                            if self.golden.check_assertions(trial, c)), None)
            if failure:
                rejected.append((patch.describe(), f"assertion: {failure}"))
                continue

            why = self._sandbox_ok(trial, canonical, tick, staging)
            if why:
                rejected.append((patch.describe(), f"sandbox rejected: {why}"))
                continue

            # Catastrophic-forgetting guard: everything that ever worked must
            # still work. A fix that trades yesterday's cases for today's is
            # not a fix.
            regressed = None
            for case in self.golden.cases.values():
                why = self._sandbox_ok(trial, case.canonical, tick, staging)
                if why:
                    regressed = f"regression on {case.case_id}: {why}"
                    break
            if regressed:
                rejected.append((patch.describe(), regressed))
                continue

            for rule in patch.add:
                rule.verified = True
            return Evidence(patch=patch,
                            sandbox_calls=self.sandbox_calls - spent_before,
                            golden_cases_checked=len(self.golden.cases),
                            rejected_candidates=rejected)
        return None
