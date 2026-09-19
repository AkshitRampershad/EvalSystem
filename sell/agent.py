"""The loop. Sense, hypothesise, experiment, adopt, act -- with nobody watching.

The ordering matters more than any single component:

  1. sense the contract before running any work, so an announced change is
     handled before it can cost a task;
  2. process late signals next, because an outcome that arrived from three ticks
     ago invalidates decisions we are about to repeat;
  3. pre-flight each task locally, so drift is caught before transmission;
  4. only then act, and treat a rejection as one more signal.

No step consults the agent's opinion of itself. Every adaptation is decided by
the environment, in a sandbox, against an accumulated regression suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .experiment import REVERSIBLE, Experimenter, classify
from .governor import Governor
from .policy import initial_policy
from .reasoner import Context
from .sensors import (sense_changelog, sense_invariants, sense_ledger,
                      sense_rejection, sense_spec, sense_staging)
from .store import EnvironmentModel, GoldenSet, Trajectory, TrajectoryStore

MAX_ADAPT_ATTEMPTS = 3


@dataclass
class Metrics:
    adaptations: list[dict[str, Any]] = field(default_factory=list)
    failed_tasks: list[dict[str, Any]] = field(default_factory=list)
    completed: int = 0
    rollbacks: int = 0
    silent_errors_caught: int = 0
    stale_writes_repaired: int = 0
    pre_verified: int = 0
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    # Kept to make a claim falsifiable rather than rhetorical: if the agent ever
    # needs a person, it increments this and the demo's headline number is wrong.
    human_interventions: int = 0


class Agent:
    def __init__(self, api, reasoner, *, log: Callable[[str], None] = print,
                 error_budget: float = 0.25, seeded_clusters: tuple[str, ...] = ()) -> None:
        self.api = api
        self.reasoner = reasoner
        self.log = log
        self.model = EnvironmentModel()
        self.policy = initial_policy()
        self.store = TrajectoryStore()
        self.golden = GoldenSet()
        self.experimenter = Experimenter(api, self.golden)
        self.governor = Governor(error_budget, seeded=seeded_clusters)
        self.metrics = Metrics()
        # Fixes verified against the partner's staged version, held until the
        # change they answer actually lands.
        self.staged_patches: list[Any] = []
        self.model.adopt(api.get_spec())
        for entry in api.get_changelog():
            self.model.seen_changelog.add((entry["version"], entry["note"]))

    # ------------------------------------------------------------------
    # adaptation
    # ------------------------------------------------------------------

    def adapt(self, signals: list[str], tick: int, canonical: dict[str, Any],
              why: str) -> bool:
        for s in signals:
            self.log(f"    detected  {s.summary()}")
        ctx = Context(policy=self.policy, model=self.model, canonical=canonical, tick=tick)
        # A patch already verified against the staged version is tried first: it
        # costs nothing to retry and it is the only candidate with evidence
        # gathered before the change landed.
        candidates = list(self.staged_patches) + self.reasoner.propose(signals, ctx)
        if not candidates:
            self.log(f"    no hypothesis for {why}")
            return False
        self.log(f"    {len(candidates)} hypothes{'is' if len(candidates) == 1 else 'es'} "
                 f"from {self.reasoner.name}; testing in sandbox")
        evidence = self.experimenter.evaluate(self.policy, candidates, canonical, tick)
        if evidence is None:
            self.log("    every candidate rejected by the sandbox or the regression suite")
            self.metrics.unresolved.append({"tick": tick, "why": why})
            return False
        for desc, reason in evidence.rejected_candidates:
            self.log(f"      rejected  {desc}  ({reason})")
        pre_verified = any(evidence.patch is sp for sp in self.staged_patches)
        if pre_verified:
            self.staged_patches = [sp for sp in self.staged_patches
                                   if sp is not evidence.patch]
            self.metrics.pre_verified += 1
        version = self.policy.commit(evidence.patch, note=why)
        self.log(f"    ADOPTED   {evidence.patch.describe()}  -> policy v{version}"
                 + ("  (pre-verified against the staged version)" if pre_verified else ""))
        self.log(f"              verified by {evidence.summary()}")
        self.metrics.adaptations.append({
            "tick": tick, "version": version, "why": why,
            "detected_by": "+".join(sorted({sig.source for sig in signals})),
            "pre_verified": pre_verified,
            "patch": evidence.patch.describe(),
            "rationale": evidence.patch.rationale,
            "sandbox_calls": evidence.sandbox_calls,
        })
        return True

    # ------------------------------------------------------------------
    # per-tick
    # ------------------------------------------------------------------

    def tick(self, tick: int, tasks: list[dict[str, Any]]) -> None:
        sample = tasks[0]["canonical"] if tasks else self._any_canonical()

        # 1. active sensing -- cheapest possible adaptation, before any task runs
        observed = sense_changelog(self.api, self.model, tick) + \
            sense_spec(self.api, self.model, tick)
        # An announcement of a change that has not landed is not something to act
        # on against the live contract -- it belongs to the pre-verify path.
        upcoming = [s for s in observed if s.detail.get("upcoming")]
        signals = [s for s in observed if not s.detail.get("upcoming")]
        if signals and sample:
            self.log("  sensing:")
            self.adapt(signals, tick, sample, "contract change observed proactively")

        # Earliest sensor of all: verify a fix against the partner's staged
        # version, then hold it. Deploying it now would break the live contract.
        upcoming += sense_staging(self.api, self.model, tick)
        if upcoming and sample:
            self.log("  staged next version detected:")
            self._pre_verify(upcoming, tick, sample)

        # 2. delayed outcomes -- must precede new work, since they invalidate it
        for finding in self.api.reconcile(tick):
            self._handle_late(finding, tick)

        # 3. the tick's work
        for task in tasks:
            self._execute(task, tick)

    def _pre_verify(self, signals: list[Any], tick: int,
                    canonical: dict[str, Any]) -> None:
        for sig in signals:
            self.log(f"    detected  {sig.summary()}")
        ctx = Context(policy=self.policy, model=self.model, canonical=canonical, tick=tick)
        candidates = self.reasoner.propose(signals, ctx)
        if not candidates:
            self.log("    no hypothesis for the staged change; will handle it on landing")
            return
        evidence = self.experimenter.evaluate(self.policy, candidates, canonical, tick,
                                              staging=True)
        if evidence is None:
            self.log("    no candidate satisfied the staged contract; "
                     "will handle it on landing")
            return
        self.staged_patches.append(evidence.patch)
        self.log(f"    PRE-VERIFIED {evidence.patch.describe()} against the staged "
                 f"version; holding until it lands")

    def _any_canonical(self) -> dict[str, Any] | None:
        return next((c.canonical for c in self.golden.cases.values()), None)

    # ------------------------------------------------------------------
    # acting
    # ------------------------------------------------------------------

    def _execute(self, task: dict[str, Any], tick: int) -> None:
        cluster, canonical = task["cluster"], task["canonical"]
        tid = task["task_id"]
        self.log(f"  task {tid} ({cluster}, autonomy={self.governor.state(cluster).level})")

        for attempt in range(1, MAX_ADAPT_ATTEMPTS + 1):
            payload = self.policy.render(canonical)

            # pre-flight: catch drift locally, before spending a real submission
            problems = sense_invariants(payload, self.model, tick)
            if problems:
                self.log("    pre-flight failed:")
                if not self.adapt(problems, tick, canonical, "pre-flight invariant failure"):
                    break
                continue

            if not self.governor.may_write(cluster):
                # Shadow mode: prove it in the sandbox, take nothing on faith,
                # and let the ladder decide when this cluster gets to act.
                err = self.experimenter._sandbox_ok(self.policy, canonical, tick)
                ok = err is None
                self.log(f"    shadow submit -> {'accepted' if ok else err}")
                self.governor.record(cluster, ok, tick)
                if ok:
                    self.golden.record_success(tid, canonical, origin=f"shadow t{tick}")
                return

            armed = self.governor.needs_rollback_armed(cluster)
            assert classify("submit") == REVERSIBLE, "production writes need an undo path"
            response = self.api.submit(payload, tick=tick,
                                       expected_cents=canonical["amount_cents"])
            traj = Trajectory(task_id=tid, tick=tick, cluster=cluster, canonical=canonical,
                              payload=payload, policy_version=self.policy.version,
                              ok=response.ok, record_id=response.record_id,
                              error=response.error)
            self.store.log(traj)

            if response.ok:
                self.log(f"    submitted -> {response.record_id}"
                         + ("  (rollback armed)" if armed else ""))
                self.golden.record_success(tid, canonical, origin=f"accepted t{tick}")
                self.governor.record(cluster, True, tick)
                self.metrics.completed += 1
                return

            self.log(f"    rejected: {response.error['code']} on "
                     f"'{response.error.get('field')}'")
            self.metrics.failed_tasks.append({"tick": tick, "task_id": tid,
                                              "error": response.error["code"]})
            self.governor.record(cluster, False, tick)
            if not self.adapt([sense_rejection(response.error, tick)], tick, canonical,
                              f"rejection of {tid}"):
                break

        self.log(f"    giving up on {tid} this tick; will retry as the contract is relearned")

    # ------------------------------------------------------------------
    # delayed credit assignment
    # ------------------------------------------------------------------

    def _handle_late(self, finding: dict[str, Any], tick: int) -> None:
        signal = sense_ledger(finding, tick)
        rid = finding.get("record_id")
        traj = self.store.attribute(rid) if rid else None
        self.log(f"  LATE SIGNAL (t{tick}): {signal.summary()}")
        if traj is None:
            self.log("    could not attribute to a decision -- nothing to learn from")
            return
        self.log(f"    attributed to {traj.task_id}, submitted at t{traj.tick} "
                 f"under policy v{traj.policy_version}")
        self.metrics.silent_errors_caught += 1

        # The finding says what the ledger should have read. Turn that into a
        # permanent local assertion so the fix is verifiable in milliseconds
        # instead of on the next three-tick round trip, and so no future patch
        # can silently undo it.
        self.golden.record_success(traj.task_id, traj.canonical, origin="late signal")
        self.golden.add_assertion(traj.task_id, {"field": finding["field"],
                                                 "equals": finding["expected_cents"]})
        self.log(f"    regression suite now asserts "
                 f"{finding['field']} == {finding['expected_cents']} for {traj.task_id}")

        # Reversibility is what makes unsupervised action defensible: undo the
        # bad write before reasoning about the cause.
        if rid and self.api.rollback(rid):
            traj.rolled_back = True
            self.metrics.rollbacks += 1
            self.governor.record_rollback(traj.cluster)
            self.log(f"    rolled back {rid}")

        self.governor.record(traj.cluster, False, tick)

        # Critical distinction, and the one a naive loop gets wrong: is the
        # policy still wrong, or is this a write made under a policy we have
        # already fixed? Re-deriving a "fix" for an already-correct policy is how
        # a self-modifying agent oscillates. Ask the assertion, then decide.
        case = self.golden.cases.get(traj.task_id)
        if case is not None and self.golden.check_assertions(self.policy, case) is None:
            self.metrics.stale_writes_repaired += 1
            self.log(f"    policy v{self.policy.version} already renders this correctly "
                     f"-- stale write from v{traj.policy_version}, no adaptation needed")
            self._execute({"task_id": traj.task_id + "-repair", "cluster": traj.cluster,
                           "canonical": traj.canonical}, tick)
            return

        if self.adapt([signal], tick, traj.canonical, "ledger disagreed with our write"):
            self._execute({"task_id": traj.task_id + "-repair", "cluster": traj.cluster,
                           "canonical": traj.canonical}, tick)
