#!/usr/bin/env python3
"""Drive the agent through a drifting environment with no human in the loop.

Usage:
    python3 run_demo.py                      # heuristic reasoner, no API key needed
    python3 run_demo.py --reasoner claude    # Claude generates the hypotheses
    python3 run_demo.py --quiet              # metrics only

What to watch for, in order of how much each one matters:

  t4/t5  an announced rename is absorbed *before* the change lands -- zero
         failed tasks, because the changelog was read rather than waited on.
  t6     a new cluster appears, starts in shadow, and graduates on its own.
  t19    an undocumented change to amount handling. Every submission returns
         200. The spec diff is empty. Nothing looks wrong.
  t22    the partner's ledger disagrees. The agent attributes the finding back
         to the decision that caused it, rolls the write back, converts the
         finding into a permanent regression assertion, derives the fix from the
         ratio, verifies it in the sandbox, and re-submits.

No step in any of that asks a person, and no step consults the agent's own
confidence.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from sell.agent import Agent
from sell.environment import PartnerAPI
from sell.reasoner import build_reasoner
from sell.store import dump_state

# (tick, drift, human-readable label). An entry with announce_at publishes a
# deprecation notice first, which is what makes pre-emptive adaptation possible.
SCHEDULE = [
    {"at": 5,  "drift": "rename_email",           "stage_at": 4,
     "label": "renames customer_email -> contact_email"},
    {"at": 9,  "drift": "require_currency",       "stage_at": None,
     "label": "makes 'currency' a required field"},
    {"at": 13, "drift": "terms_enum",             "stage_at": None,
     "label": "changes the 'terms' value set"},
    {"at": 16, "drift": "iso_dates",              "stage_at": None,
     "label": "requires RFC3339 timestamps"},
    {"at": 19, "drift": "amount_to_cents_silent", "stage_at": None,
     "label": "switches amount to cents, SILENTLY"},
]


def make_record(task_id: str, cluster: str, rng: random.Random) -> dict:
    # Whole-dollar amounts: the day-one policy divides by 100 losslessly, so the
    # later unit change is the only thing that can corrupt the figure.
    return {
        "invoice_ref": f"INV-{task_id}",
        "customer_email": f"ap@{cluster}.example",
        "amount_cents": rng.randrange(100, 9000) * 100,
        "issued_on": "2026-09-01",
        "terms": rng.choice(["net15", "net30", "net60"]),
        "currency": "USD",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reasoner", default="auto", choices=["auto", "heuristic", "claude"])
    ap.add_argument("--ticks", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--budget", type=float, default=0.25)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--state", default="state/run.json")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    api = PartnerAPI()
    reasoner = build_reasoner(args.reasoner)
    log = (lambda _s: None) if args.quiet else print
    agent = Agent(api, reasoner, log=log, error_budget=args.budget,
                  seeded_clusters=("acme",))

    live = reasoner.name != "heuristic" and reasoner._client_or_none() is not None
    if reasoner.name == "heuristic":
        print("reasoner: heuristic (offline pattern matching)")
        if args.reasoner == "auto":
            print("  no Anthropic credentials found -- set ANTHROPIC_API_KEY and pass")
            print("  --reasoner claude to have the model generate the hypotheses")
    elif live:
        print(f"reasoner: claude ({reasoner.model})")
    else:
        print(f"reasoner: claude requested but unavailable ({reasoner.last_error});")
        print("  every proposal below came from the heuristic fallback")
    print(f"error budget: {args.budget:.0%} per cluster    "
          f"initial policy: v{agent.policy.version}\n")

    landed: list[dict] = []
    seq = 0
    for tick in range(1, args.ticks + 1):
        for entry in SCHEDULE:
            if entry["stage_at"] == tick:
                note = api.stage_drift(entry["drift"], tick)
                log(f"\n=== t{tick} :: partner stages its next version ===")
                log(f"    announcement: {note}")
            elif entry["at"] == tick:
                note = (api.promote_staged(tick) if api.has_staged()
                        else api.apply_drift(entry["drift"], tick))
                landed.append({"tick": tick, **entry})
                log(f"\n=== t{tick} :: DRIFT LANDS -- partner {entry['label']} ===")
                log(f"    partner says: {note}")

        clusters = ["acme"] if tick < 6 else ["acme", "globex"]
        tasks = []
        for cluster in clusters:
            seq += 1
            tid = f"T{seq:03d}"
            tasks.append({"task_id": tid, "cluster": cluster,
                          "canonical": make_record(tid, cluster, rng)})
        if not args.quiet:
            print(f"\n-- t{tick} --")
        agent.tick(tick, tasks)

    # ---------------- report ----------------
    m = agent.metrics
    print("\n" + "=" * 72)
    print("ADAPTATION LEDGER")
    print("=" * 72)
    print(f"{'drift':<38}{'lands':>6}{'fixed':>6}{'lag':>6}"
          f"{'detected by':>18}{'bad writes':>12}")
    for row in landed:
        tick = row["tick"]
        after = [a for a in m.adaptations if a["tick"] >= tick]
        rejected = len([f for f in m.failed_tasks
                        if tick <= f["tick"] <= (after[0]["tick"] if after else tick)])
        # Scope the damage to this drift's own window, or every later rollback
        # gets blamed on the first drift that happened to precede it.
        upto = fixed["tick"] if (fixed := (after[0] if after else None)) else tick
        bad = len([t for t in agent.store.rows
                   if t.rolled_back and tick <= t.tick <= upto])
        if fixed is None:
            print(f"{row['label'][:37]:<38}{'t'+str(tick):>6}{'-':>6}{'--':>6}"
                  f"{'not detected':>18}{bad:>12}")
            continue
        src = fixed["detected_by"] + ("*" if fixed["pre_verified"] else "")
        lag = f"{fixed['tick'] - tick}t"
        print(f"{row['label'][:37]:<38}{'t'+str(tick):>6}{'t'+str(fixed['tick']):>6}"
              f"{lag:>6}{src:>18}{bad or rejected:>12}")
    print("\n  * fix was verified against the staged version before the change landed,")
    print("    so it applied on the first tick with no failed task and no rejection.")
    print("  'bad writes' for the silent drift are writes that were accepted and")
    print("  looked correct; only the ledger revealed them, and all were rolled back.")

    print("\n" + "=" * 72)
    print("OUTCOMES")
    print("=" * 72)
    print(f"  tasks completed                 {m.completed}")
    print(f"  tasks failed (cost of learning) {len(m.failed_tasks)}")
    print(f"  policy versions adopted         {len(m.adaptations)} "
          f"(now v{agent.policy.version})")
    print(f"  silent errors caught by ledger  {m.silent_errors_caught}")
    print(f"  stale writes repaired, no churn {m.stale_writes_repaired}")
    print(f"  fixes pre-verified before drift {m.pre_verified}")
    print(f"  production writes rolled back   {m.rollbacks}")
    print(f"  sandbox experiments run         {agent.experimenter.experiments}")
    print(f"  regression cases accumulated    {len(agent.golden)}")
    print(f"  unresolved signals              {len(m.unresolved)}")
    print(f"  HUMAN INTERVENTIONS             {m.human_interventions}")
    if getattr(reasoner, "last_error", None) and reasoner.name == "claude":
        print(f"  (reasoner fell back at least once: {reasoner.last_error})")

    print("\nautonomy ladder:")
    for name, st in sorted(agent.governor.clusters.items()):
        print(f"  {name:<10} {st.level:<11} ok={st.successes} fail={st.failures} "
              f"rollbacks={st.rollbacks} recent-error-rate={st.error_rate():.0%}")
    for ev in agent.governor.events:
        print(f"    {ev}")

    print("\nlearned policy (every rule traceable to the signal that justified it):")
    for r in agent.policy.rules:
        src = r.provenance.get("source") or r.provenance.get("reasoner") or "seed"
        origin = r.provenance.get("signal", r.provenance.get("source", "initial"))
        print(f"  {r.id:<5} {r.describe():<46} "
              f"{'verified' if r.verified else 'UNVERIFIED':<11} via {src}/{origin}")

    out = Path(args.state)
    dump_state(out, agent.policy, agent.model, agent.store, agent.golden)
    print(f"\nstate written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
