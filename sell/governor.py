"""Autonomy granted per input cluster, and the error budget that pays for it.

Two decisions live here.

Granularity: autonomy is a property of a cluster of similar inputs, not of the
agent. One unfamiliar vendor should not demote the agent everywhere, and a
cluster with forty clean runs should not wait on a cluster with two. This is
also what makes graduation statistically reachable -- you are never asked to
demonstrate a global accuracy number on a shifting distribution.

Cost: with no human evaluator, the agent learns by being wrong in production
sometimes. There is no third option, so the budget is explicit and the operator
sets it, per cluster, rather than discovering it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SHADOW = "shadow"          # sandbox only; nothing reaches production
CANARY = "canary"          # real writes, rollback armed, tight budget
AUTONOMOUS = "autonomous"  # real writes, no gate

LADDER = [SHADOW, CANARY, AUTONOMOUS]
PROMOTE_AFTER = {SHADOW: 2, CANARY: 5}


@dataclass
class ClusterState:
    name: str
    level: str = SHADOW
    consecutive_ok: int = 0
    successes: int = 0
    failures: int = 0
    rollbacks: int = 0
    window: list[bool] = field(default_factory=list)

    def error_rate(self, window: int = 10) -> float:
        recent = self.window[-window:]
        return 0.0 if not recent else 1 - (sum(recent) / len(recent))


class Governor:
    def __init__(self, error_budget: float = 0.25, seeded: tuple[str, ...] = ()) -> None:
        self.error_budget = error_budget
        self.clusters: dict[str, ClusterState] = {}
        self.events: list[str] = []
        for name in seeded:
            # A cluster a human shipped and verified starts mid-ladder; one the
            # agent meets for the first time does not.
            self.clusters[name] = ClusterState(name, level=CANARY)

    def state(self, cluster: str) -> ClusterState:
        return self.clusters.setdefault(cluster, ClusterState(cluster))

    def may_write(self, cluster: str) -> bool:
        return self.state(cluster).level in (CANARY, AUTONOMOUS)

    def needs_rollback_armed(self, cluster: str) -> bool:
        return self.state(cluster).level == CANARY

    def record(self, cluster: str, ok: bool, tick: int) -> None:
        st = self.state(cluster)
        st.window.append(ok)
        if ok:
            st.successes += 1
            st.consecutive_ok += 1
            need = PROMOTE_AFTER.get(st.level)
            if need and st.consecutive_ok >= need and st.error_rate() <= self.error_budget:
                st.level = LADDER[LADDER.index(st.level) + 1]
                st.consecutive_ok = 0
                self.events.append(f"t{tick} promote {cluster} -> {st.level}")
        else:
            st.failures += 1
            st.consecutive_ok = 0
            if st.error_rate() > self.error_budget and st.level != SHADOW:
                st.level = LADDER[LADDER.index(st.level) - 1]
                self.events.append(
                    f"t{tick} demote {cluster} -> {st.level} "
                    f"(error rate {st.error_rate():.0%} over budget {self.error_budget:.0%})")

    def record_rollback(self, cluster: str) -> None:
        self.state(cluster).rollbacks += 1
