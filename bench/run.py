#!/usr/bin/env python3
"""Run the drift benchmark.

    python3 -m bench.run                          # all baselines + the agent
    python3 -m bench.run --solver agent --detail  # per-case results
    python3 -m bench.run --solver claude          # model-backed hypotheses
    python3 -m bench.run --mine                   # re-mine cases from history

Read the safety column first. A solver with a higher solve rate and any unsafe
cases has not done better: an adopted change that silently stops working is
worse than no change at all, because nothing downstream will tell you.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import score as scoring
from .cases import CaseSet
from .mine import mine
from .solvers import build_solver

DEFAULT_CASES = Path(__file__).resolve().parents[1] / "data" / "stripe-drift-v1.json.gz"
DEFAULT_WINDOWS = [("v250", "v500"), ("v500", "v750"), ("v750", "v1000"),
                   ("v1000", "v1250"), ("v1250", "v1500"), ("v1500", "v1750"),
                   ("v1750", "v2000"), ("v2000", "v2250"), ("v2250", "v2506")]
LADDER = ["noop", "escalate-always", "drop-ungated", "drop",
          "agent-ungated", "agent"]


def _bar(report: scoring.Report, width: int = 28) -> str:
    """Solved / escalated / unsolved / unsafe, at a glance."""
    if not report.total:
        return ""
    glyphs = [(scoring.SOLVED, "#"), (scoring.ESCALATED, "?"),
              (scoring.UNSOLVED, "."), (scoring.UNSAFE, "!")]
    out = ""
    for result, glyph in glyphs:
        out += glyph * round(width * report.count(result) / report.total)
    return out[:width].ljust(width)


def _print_table(reports: list[scoring.Report]) -> None:
    print(f"{'solver':<24}{'adapt':>8}{'ask':>8}{'UNSAFE':>8}{'safety':>8}  profile")
    for r in reports:
        a_s, a_n = r._split(scoring.ADAPT)
        k_s, k_n = r._split(scoring.ESCALATE)
        print(f"{r.solver:<24}{f'{a_s}/{a_n}':>8}{f'{k_s}/{k_n}':>8}"
              f"{r.count(scoring.UNSAFE):>8}{r.safety*100:>7.0f}%  {_bar(r)}")
    print("\n  adapt = correct where the contract holds the answer")
    print("  ask   = correctly asked where it does not (guessing there is a failure)")
    print("  # solved   ? asked but a fix existed   . no answer   ! UNSAFE")


def _print_kinds(report: scoring.Report) -> None:
    print(f"\nper drift kind ({report.solver}):")
    print(f"  {'kind':<18}{'n':>4}{'solved':>8}{'esc':>6}{'unsol':>7}{'UNSAFE':>8}")
    for kind, row in sorted(report.by_kind().items(),
                            key=lambda kv: -sum(kv[1].values())):
        n = sum(row.values())
        print(f"  {kind:<18}{n:>4}{row[scoring.SOLVED]:>8}"
              f"{row[scoring.ESCALATED]:>6}{row[scoring.UNSOLVED]:>7}"
              f"{row[scoring.UNSAFE]:>8}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.run")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--solver", action="append",
                    help="repeatable; baselines (noop, escalate-always, drop, "
                         "drop-ungated, agent) or a reasoner tier (claude, groq, "
                         "together, openrouter, local), optionally '-ungated'. "
                         "Default runs the whole offline ladder.")
    ap.add_argument("--detail", action="store_true", help="per-case outcomes")
    ap.add_argument("--only", help="show only this result class in --detail",
                    choices=[scoring.SOLVED, scoring.ESCALATED,
                             scoring.UNSOLVED, scoring.UNSAFE])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mine", action="store_true", help="re-mine, then write --cases")
    args = ap.parse_args(argv)

    if args.mine:
        case_set = mine(DEFAULT_WINDOWS, log=print)
        path = case_set.save(args.cases)
        print(f"\nmined {len(case_set)} cases -> {path}")
    else:
        case_set = CaseSet.load(args.cases)

    kinds = case_set.by_kind()
    print(f"\n{len(case_set)} real drift cases from {case_set.provider}, "
          f"{len(case_set.contracts)} contracts")
    print("  " + "  ".join(f"{k}={len(v)}" for k, v in
                           sorted(kinds.items(), key=lambda kv: -len(kv[1]))))
    print()

    specs = args.solver or LADDER
    solvers = [build_solver(s) for s in specs]
    for solver in solvers:
        # Say so up front when a tier silently degraded, rather than letting its
        # row be read as a measurement of the model it names.
        reasoner = getattr(solver, "reasoner", None)
        unavailable = (reasoner.available() if hasattr(reasoner, "available")
                       else getattr(reasoner, "last_error", None))
        if reasoner is not None and unavailable:
            print(f"  note: {solver.name} is unavailable ({unavailable}); "
                  f"its hypotheses come from the heuristic fallback")
    reports = [scoring.run(case_set, solver) for solver in solvers]

    # A row is only a measurement of the model it names if the model answered for
    # every case. Say loudly when it did not.
    for solver, report in zip(solvers, reports):
        reasoner = getattr(solver, "reasoner", None)
        degraded = getattr(reasoner, "fallbacks", 0)
        if not degraded:
            continue
        waits = getattr(reasoner, "rate_limit_waits", 0)
        print(f"\n  WARNING: {report.solver} fell back to heuristics on "
              f"{degraded}/{report.total} cases"
              + (f" after {waits} rate-limit waits" if waits else "")
              + f" (last error: {getattr(reasoner, 'last_error', None)}).")
        print("  This row is NOT a clean measurement of that model. Re-run with a "
              "higher rate limit,")
        print("  or a paid tier, before quoting the number.")

    if args.json:
        print(json.dumps({"cases": len(case_set),
                          "reports": [r.as_dict() for r in reports]}, indent=2))
        return 0

    _print_table(reports)
    _print_kinds(reports[-1])

    if args.detail:
        target = reports[-1]
        print(f"\nper-case ({target.solver}"
              + (f", {args.only} only" if args.only else "") + "):")
        for o in target.outcomes:
            if args.only and o.result != args.only:
                continue
            print(f"  [{o.result:<9}] {o.kind:<17} {o.case_id.split(':', 2)[-1]}")
            if o.detail:
                print(f"              {o.detail}")

    worst = [r for r in reports if r.count(scoring.UNSAFE)]
    if worst:
        print("\nsolvers that adopted something unsafe: "
              + ", ".join(f"{r.solver} ({r.count(scoring.UNSAFE)})" for r in worst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
