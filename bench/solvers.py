"""Things that attempt the benchmark, including ones that should do badly.

Baselines are not decoration. `NoopSolver` proves the cases are not free, and
`DropSolver` run without the gate measures exactly what the gate is preventing:
dropping the offending field is schema-valid surprisingly often, and silently
stops doing the job every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sell.policy import Patch, Policy, Rule
from sell.real import diff as differ
from sell.real import gate as gt
from sell.reasoner import Context, build_reasoner
from sell.sensors import sense_invariants
from sell.store import EnvironmentModel

from .cases import Case


@dataclass
class Attempt:
    case_id: str
    solver: str
    adopted: Patch | None = None
    tier: str = gt.REJECTED
    question: str | None = None
    rejected: list[tuple[str, str]] = field(default_factory=list)
    hypotheses: int = 0
    schema_checks: int = 0


def _signals(case: Case, old: dict[str, Any], new: dict[str, Any]) -> list[Any]:
    """Exactly what the running agent would see: the spec diff, plus its own
    pre-flight check of the request it was about to send."""
    model = EnvironmentModel()
    model.adopt(new)
    return (differ.diff_contracts(old, new)
            + sense_invariants(case.payload, model, tick=0))


class NoopSolver:
    """Changes nothing. Any case it 'solves' was never broken."""

    name = "noop"

    def solve(self, case: Case, old: dict[str, Any], new: dict[str, Any]) -> Attempt:
        return Attempt(case.case_id, self.name)


class DropSolver:
    """Always stops sending the field that caused the problem.

    The laziest fix that passes a schema check, and the exact failure the
    capability tier exists to catch. Run with `gated=False` to see what would
    ship without it.
    """

    def __init__(self, gated: bool = True) -> None:
        self.gated = gated
        self.name = "drop-gated" if gated else "drop-ungated"

    def solve(self, case: Case, old: dict[str, Any], new: dict[str, Any]) -> Attempt:
        patch = Patch(add=[Rule("drop", {"field": case.focus_field})],
                      rationale="stop sending the offending field")
        if not self.gated:
            return Attempt(case.case_id, self.name, adopted=patch,
                           tier=gt.SCHEMA, hypotheses=1)
        verdict = gt.RealGate(new).evaluate(Policy(), [patch], case.payload)
        return Attempt(case.case_id, self.name,
                       adopted=verdict.patch if verdict.adoptable else None,
                       tier=verdict.tier, question=verdict.question,
                       rejected=verdict.rejected, hypotheses=1,
                       schema_checks=verdict.schema_checks)


class AlwaysEscalateSolver:
    """Never adapts; always asks. Adapts nothing, breaks nothing.

    Included because the case mix is not balanced: roughly six in ten real drift
    events carry no local evidence of where a capability went, so escalating is
    the correct answer for most of them. Without this row on the board, a solver
    with no intelligence at all posts a respectable-looking score and nobody
    notices.
    """

    name = "escalate-always"

    def solve(self, case: Case, old: dict[str, Any], new: dict[str, Any]) -> Attempt:
        return Attempt(case.case_id, self.name, tier=gt.NEEDS_HUMAN,
                       question=f"'{case.focus_field}' changed; how should the "
                                f"request be adjusted?")


class AgentSolver:
    """The system under test: sense, hypothesise, verify, adopt."""

    def __init__(self, reasoner: str = "heuristic", gated: bool = True) -> None:
        self.reasoner = build_reasoner(reasoner)
        self.gated = gated
        self.name = f"agent-{self.reasoner.name}" + ("" if gated else "-ungated")

    def solve(self, case: Case, old: dict[str, Any], new: dict[str, Any]) -> Attempt:
        model = EnvironmentModel()
        model.adopt(new)
        policy = Policy()
        ctx = Context(policy=policy, model=model, canonical=case.payload, tick=0)
        patches = self.reasoner.propose(_signals(case, old, new), ctx)

        if not self.gated:
            return Attempt(case.case_id, self.name,
                           adopted=patches[0] if patches else None,
                           tier=gt.SCHEMA if patches else gt.REJECTED,
                           hypotheses=len(patches))

        verdict = gt.RealGate(new).evaluate(policy, patches, case.payload)
        return Attempt(case.case_id, self.name,
                       adopted=verdict.patch if verdict.adoptable else None,
                       tier=verdict.tier, question=verdict.question,
                       rejected=verdict.rejected, hypotheses=len(patches),
                       schema_checks=verdict.schema_checks)


def build_solver(spec: str) -> Any:
    """'noop' | 'drop' | 'drop-ungated' | 'agent' | 'agent-ungated' | 'claude'"""
    table = {
        "noop": lambda: NoopSolver(),
        "escalate-always": lambda: AlwaysEscalateSolver(),
        "drop": lambda: DropSolver(gated=True),
        "drop-ungated": lambda: DropSolver(gated=False),
        "agent": lambda: AgentSolver("heuristic", gated=True),
        "agent-ungated": lambda: AgentSolver("heuristic", gated=False),
        "claude": lambda: AgentSolver("claude", gated=True),
    }
    if spec not in table:
        raise KeyError(f"unknown solver {spec!r}; choose from {sorted(table)}")
    return table[spec]()
