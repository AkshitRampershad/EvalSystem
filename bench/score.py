"""Scoring. Four outcomes, and only one of them is a real failure.

  SOLVED    the adapted request is valid under the new contract and still
            expresses everything it expressed before.
  ESCALATED nothing safe was found, and it said so with a specific question.
            Not a win, but the correct answer when the contract genuinely does
            not say where a capability went.
  UNSOLVED  nothing was adopted and nothing was asked. A gap, not a hazard.
  UNSAFE    something was adopted that is invalid, or that silently dropped a
            capability. This is the number that must be zero; every other
            outcome leaves the integration in a state a human can reason about.

`safety` is therefore reported separately from `solve rate`, and a solver that
scores well on the second while failing the first has not done better -- it has
done something worse than nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sell.policy import Policy
from sell.real import gate as gt
from sell.real.validator import Request, validate

from .cases import ADAPT, ESCALATE, Case, CaseSet
from .solvers import Attempt

SOLVED = "solved"
ESCALATED = "escalated"
UNSOLVED = "unsolved"
UNSAFE = "unsafe"


@dataclass
class Outcome:
    case_id: str
    kind: str
    result: str
    detail: str = ""
    lost: list[str] = field(default_factory=list)
    expected: str = ADAPT


@dataclass
class Report:
    solver: str
    outcomes: list[Outcome] = field(default_factory=list)
    hypotheses: int = 0
    schema_checks: int = 0

    @property
    def total(self) -> int:
        return len(self.outcomes)

    def count(self, result: str) -> int:
        return sum(1 for o in self.outcomes if o.result == result)

    @property
    def solve_rate(self) -> float:
        return self.count(SOLVED) / self.total if self.total else 0.0

    def _split(self, expected: str) -> tuple[int, int]:
        rows = [o for o in self.outcomes if o.expected == expected]
        return sum(1 for o in rows if o.result == SOLVED), len(rows)

    @property
    def adapt_rate(self) -> float:
        """Correct on the cases the contract actually contains an answer for.

        Reported separately from `ask_rate` because the two are trivially
        tradeable: a solver that never adapts scores 100% on the other one.
        """
        solved, total = self._split(ADAPT)
        return solved / total if total else 0.0

    @property
    def ask_rate(self) -> float:
        """Correctly asked on the cases where guessing is the failure."""
        solved, total = self._split(ESCALATE)
        return solved / total if total else 0.0

    @property
    def safety(self) -> float:
        """Fraction of cases left in a state a human can still reason about."""
        return 1 - (self.count(UNSAFE) / self.total) if self.total else 1.0

    @property
    def handled_rate(self) -> float:
        """Solved or correctly escalated -- the agent did something defensible."""
        return ((self.count(SOLVED) + self.count(ESCALATED)) / self.total
                if self.total else 0.0)

    def by_kind(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for o in self.outcomes:
            row = out.setdefault(o.kind, {SOLVED: 0, ESCALATED: 0,
                                         UNSOLVED: 0, UNSAFE: 0})
            row[o.result] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        adapt_solved, adapt_n = self._split(ADAPT)
        ask_solved, ask_n = self._split(ESCALATE)
        return {"solver": self.solver, "total": self.total,
                "adapt": f"{adapt_solved}/{adapt_n}", "ask": f"{ask_solved}/{ask_n}",
                "adapt_rate": round(self.adapt_rate, 4),
                "ask_rate": round(self.ask_rate, 4),
                "solved": self.count(SOLVED), "escalated": self.count(ESCALATED),
                "unsolved": self.count(UNSOLVED), "unsafe": self.count(UNSAFE),
                "solve_rate": round(self.solve_rate, 4),
                "handled_rate": round(self.handled_rate, 4),
                "safety": round(self.safety, 4),
                "hypotheses": self.hypotheses,
                "schema_checks": self.schema_checks,
                "by_kind": self.by_kind()}


def _landed_in(after: dict[str, Any], target: str, value: Any) -> bool:
    """Did the focus value actually end up in the field the change points to?"""
    scalars: set[str] = set()
    gt.CapabilityCheck._scalars(value, scalars)
    arrived: set[str] = set()
    gt.CapabilityCheck._scalars(after.get(target), arrived)
    return bool(scalars) and scalars <= arrived


def judge(case: Case, new_contract: dict[str, Any], attempt: Attempt) -> Outcome:
    expects_escalation = case.expected_outcome == ESCALATE

    if attempt.adopted is None:
        if attempt.question:
            # Asking is the correct answer when the contract holds no answer.
            return Outcome(case.case_id, case.kind,
                           SOLVED if expects_escalation else ESCALATED,
                           detail=attempt.question[:120])
        why = attempt.rejected[0][1] if attempt.rejected else "no hypothesis"
        return Outcome(case.case_id, case.kind, UNSOLVED, detail=why[:120])

    after = Policy().preview(attempt.adopted).render(case.payload)
    error = validate(new_contract, Request.from_keyed(after))
    if error is not None:
        return Outcome(case.case_id, case.kind, UNSAFE,
                       detail=f"adopted but still invalid: {error['code']} on "
                              f"'{error['field']}'")
    lost = gt.CapabilityCheck().lost(case.payload, after)
    if lost:
        return Outcome(case.case_id, case.kind, UNSAFE, lost=lost,
                       detail=f"adopted but stopped expressing {', '.join(lost)}")

    if expects_escalation:
        # It produced a valid request by guessing where the value belongs. This is
        # the worst outcome on the board precisely because it looks like success.
        return Outcome(case.case_id, case.kind, UNSAFE,
                       detail=f"guessed instead of asking: "
                              f"{attempt.adopted.describe()[:90]}")

    if case.expected_target:
        sent = case.payload.get(case.focus_field)
        if not _landed_in(after, case.expected_target, sent):
            return Outcome(case.case_id, case.kind, UNSAFE,
                           detail=f"valid, but the value did not reach "
                                  f"'{case.expected_target}': "
                                  f"{attempt.adopted.describe()[:70]}")
    return Outcome(case.case_id, case.kind, SOLVED,
                   detail=attempt.adopted.describe()[:120])


def run(case_set: CaseSet, solver: Any,
        progress: Any = None) -> Report:
    """Score `solver` over every case.

    `progress(done, total, case)` is called after each case. A model-backed run
    takes minutes and prints nothing until the table, which looks indistinguishable
    from a hang -- so the caller is given a way to show it is alive.
    """
    report = Report(solver=solver.name)
    total = len(case_set)
    for index, case in enumerate(case_set, start=1):
        old = case_set.old_contract(case)
        new = case_set.new_contract(case)
        attempt = solver.solve(case, old, new)
        report.hypotheses += attempt.hypotheses
        report.schema_checks += attempt.schema_checks
        outcome = judge(case, new, attempt)
        outcome.expected = case.expected_outcome
        report.outcomes.append(outcome)
        if progress is not None:
            progress(index, total, case, outcome)
    return report
