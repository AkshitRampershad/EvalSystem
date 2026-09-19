#!/usr/bin/env python3
"""Scan real drift between two shipped versions of a real API.

    # what changed in POST /v1/invoices between two Stripe releases
    python3 -m sell.real.scan --from v2502 --to v2506 --operation "POST /v1/invoices"

    # across the whole API, with a breaking-change work queue
    python3 -m sell.real.scan --from v2400 --to v2506

    # what versions exist
    python3 -m sell.real.scan --list-versions

The output answers the question that decides whether a self-learning agent is
worth building for a given integration: of the drift that really happens, how
much is visible in the published contract, and how much of it can break you.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..policy import Policy
from ..reasoner import Context, build_reasoner
from ..store import EnvironmentModel
from . import diff as differ
from . import sources

IMPACT_ORDER = {differ.BREAKING: 0, differ.ADDITIVE: 1, differ.COSMETIC: 2}
MARK = {differ.BREAKING: "BREAKING", differ.ADDITIVE: "additive",
        differ.COSMETIC: "cosmetic"}


def _print_signals(signals, limit: int) -> None:
    ordered = sorted(signals, key=lambda s: (IMPACT_ORDER.get(s.detail.get("impact"), 3),
                                             s.kind, s.detail.get("field", "")))
    for s in ordered[:limit]:
        impact = MARK.get(s.detail.get("impact"), "?")
        field = s.detail.get("field", "")
        line = f"    [{impact:<8}] {s.kind:<26} {field}"
        d = s.detail
        if s.kind == "allowed_changed":
            if d.get("removed"):
                line += f"  -{d['removed']}"
            if d.get("added"):
                line += f"  +{d['added']}"
        elif s.kind == "description_changed":
            line += "  (prose changed)"
        elif "was" in d or "now" in d:
            line += f"  {d.get('was')!r} -> {d.get('now')!r}"
        elif s.kind in ("field_added", "field_removed"):
            spec = d.get("spec") or {}
            line += f"  ({spec.get('type')}{', required' if spec.get('required') else ''})"
        print(line)
    if len(ordered) > limit:
        print(f"    ... {len(ordered) - limit} more")


def _synthetic_record(contract: dict[str, Any]) -> dict[str, Any]:
    """A stand-in for a real caller's payload, built from the old contract.

    The reasoner needs to know what we were sending in order to propose how to
    send it now. In production this comes from the trajectory store; here it is
    reconstructed from the version of the contract we were working against.
    """
    sample: dict[str, Any] = {}
    for name, facts in contract.get("fields", {}).items():
        if facts.get("allowed"):
            sample[name] = facts["allowed"][0]
        elif facts["type"].startswith("integer"):
            sample[name] = 1
        elif facts["type"].startswith("boolean"):
            sample[name] = True
        else:
            sample[name] = f"<{name}>"
    return sample


def _propose(signals, old: dict[str, Any], new: dict[str, Any], kind: str) -> None:
    reasoner = build_reasoner(kind)
    model = EnvironmentModel()
    model.adopt(new)
    ctx = Context(policy=Policy(), model=model,
                  canonical=_synthetic_record(old), tick=0)
    patches = reasoner.propose(list(signals), ctx)
    print(f"\n{reasoner.name} reasoner proposes {len(patches)} hypothesis(es):")
    for patch in patches:
        print(f"    {patch.describe()}")
        if patch.rationale:
            print(f"      {patch.rationale}")
    print("\n  These are UNVERIFIED. Nothing here has been tested against the")
    print("  provider, so none of it would be adopted: the sandbox gate in")
    print("  experiment.py is what turns a hypothesis into policy, and running it")
    print("  needs real sandbox credentials for this provider.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sell.real.scan")
    ap.add_argument("--provider", default="stripe", choices=sorted(sources.PROVIDERS))
    ap.add_argument("--from", dest="old", help="git ref, URL or local path")
    ap.add_argument("--to", dest="new", help="git ref, URL or local path")
    ap.add_argument("--operation", help='e.g. "POST /v1/invoices"; omit to scan all')
    ap.add_argument("--depth", type=int, default=None,
                    help="nesting depth to flatten (default 2 single-op, 1 full scan)")
    ap.add_argument("--limit", type=int, default=25, help="signals shown per operation")
    ap.add_argument("--top", type=int, default=12, help="operations shown in a full scan")
    ap.add_argument("--list-versions", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--propose", action="store_true",
                    help="run the reasoner on the breaking signals and print the "
                         "hypotheses it would test (UNVERIFIED -- see --help notes)")
    ap.add_argument("--reasoner", default="auto",
                    choices=["auto", "heuristic", "claude"])
    args = ap.parse_args(argv)

    if args.list_versions:
        versions = sources.list_versions(args.provider)
        print(f"{args.provider}: {len(versions)} most recent shipped versions")
        print("  " + " ".join(versions))
        return 0

    if not (args.old and args.new):
        ap.error("--from and --to are required unless --list-versions")

    # ---- one operation -------------------------------------------------
    if args.operation:
        method, path = args.operation.split(None, 1)
        depth = 2 if args.depth is None else args.depth
        old = sources.load_contract(args.old, method, path, provider=args.provider,
                                    max_depth=depth, refresh=args.refresh)
        new = sources.load_contract(args.new, method, path, provider=args.provider,
                                    max_depth=depth, refresh=args.refresh)
        signals = differ.diff_contracts(old, new)
        drift = differ.OperationDrift(new["operation"], signals)
        if args.json:
            print(json.dumps({"operation": new["operation"],
                              "summary": differ.summarise([drift]),
                              "signals": [{"kind": s.kind, **s.detail} for s in signals]},
                             indent=2, default=str))
            return 0
        print(f"{new['operation']}   {args.old} ({old['version']}) -> "
              f"{args.new} ({new['version']})")
        print(f"fields: {len(old['fields'])} -> {len(new['fields'])}")
        if not signals:
            print("\nno change in this operation's request contract")
            return 0
        print(f"\n{len(signals)} change(s): {len(drift.breaking)} breaking, "
              f"{len(drift.additive)} additive, {len(drift.cosmetic)} cosmetic")
        _print_signals(signals, args.limit)
        if args.propose and drift.breaking:
            _propose(drift.breaking, old, new, args.reasoner)
        return 0

    # ---- whole API -----------------------------------------------------
    depth = 1 if args.depth is None else args.depth
    print(f"extracting contracts ({args.provider} {args.old} -> {args.new}, "
          f"depth {depth})...", file=sys.stderr)
    old_all = sources.load_contracts(args.old, provider=args.provider,
                                     max_depth=depth, refresh=args.refresh)
    new_all = sources.load_contracts(args.new, provider=args.provider,
                                     max_depth=depth, refresh=args.refresh)
    drifts, added_ops, removed_ops = differ.diff_all(old_all, new_all)
    summary = differ.summarise(drifts)

    if args.json:
        print(json.dumps({"from": args.old, "to": args.new, "summary": summary,
                          "operations_added": added_ops,
                          "operations_removed": removed_ops,
                          "breaking_by_operation": {
                              d.operation: [{"kind": s.kind, **s.detail}
                                            for s in d.breaking]
                              for d in drifts if d.breaking}},
                         indent=2, default=str))
        return 0

    print(f"\n{args.provider}  {args.old} -> {args.new}")
    print(f"operations compared        {len(set(old_all) & set(new_all))}")
    print(f"operations added           {len(added_ops)}")
    print(f"operations removed         {len(removed_ops)}")
    print(f"operations changed         {summary['operations_changed']}")
    print(f"  ...with breaking changes {summary['operations_with_breaking_changes']}")
    print(f"total field-level changes  {summary['signals']}")
    by = summary["by_impact"]
    print(f"  breaking {by[differ.BREAKING]}   additive {by[differ.ADDITIVE]}   "
          f"cosmetic {by[differ.COSMETIC]}")
    print("\nby kind:")
    for kind, count in summary["by_kind"].items():
        print(f"  {count:>6}  {kind}")

    breaking = sorted((d for d in drifts if d.breaking),
                      key=lambda d: -len(d.breaking))
    if breaking:
        print(f"\nwork queue -- {len(breaking)} operation(s) with breaking changes:")
        for d in breaking[:args.top]:
            print(f"\n  {d.operation}  ({len(d.breaking)} breaking)")
            _print_signals(d.breaking, args.limit)
        if len(breaking) > args.top:
            print(f"\n  ... {len(breaking) - args.top} more operations")
    else:
        print("\nno breaking changes between these versions")
    if removed_ops:
        print(f"\nremoved operations: {', '.join(removed_ops[:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
