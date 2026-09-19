"""The benchmark's data model.

A case is one real drift event plus a request that worked before it. Two
properties are enforced at mining time and are what stop the benchmark from
measuring nothing:

  * the request must be valid under the old contract -- otherwise the case is
    testing the miner, not the solver;
  * the request must be *invalid* under the new contract -- otherwise there is
    nothing to adapt and the case is free.

Contracts are stored once and referenced, because the same operation appears in
many cases and a contract is far larger than a case.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

# What kind of change broke the caller. Drives the per-kind breakdown, which is
# where a solver's real strengths and gaps show up.
RENAME = "rename"                    # the field moved to a differently named one
REMOVED = "removed"                  # the field is gone with no evident successor
NEWLY_REQUIRED = "newly_required"    # a field we never sent is now mandatory
ENUM_NARROWED = "enum_narrowed"      # a value we send is no longer accepted
TYPE_CHANGED = "type_changed"        # the field wants a different type now
LENGTH_TIGHTENED = "length_tightened"  # our value is now too long

KINDS = (RENAME, REMOVED, NEWLY_REQUIRED, ENUM_NARROWED, TYPE_CHANGED,
         LENGTH_TIGHTENED)

# What a correct solver should do. Derived from the shape of the change, never
# from an opinion about the provider's intent.
#
# ADAPT     the contract contains enough information to fix this locally.
# ESCALATE  it does not. Asking is the right answer, and guessing is a failure --
#           a value written into an unrelated field validates perfectly and is
#           worse than no change, because nothing downstream will complain.
ADAPT = "adapt"
ESCALATE = "escalate"


@dataclass
class Case:
    case_id: str
    provider: str
    operation: str
    from_version: str
    to_version: str
    kind: str
    focus_field: str
    payload: dict[str, Any]
    # Whether the new contract plausibly contains somewhere for the value to go.
    # Used to judge whether escalation was the right call, never to solve.
    replacement_candidate: str | None = None
    expected_outcome: str = ADAPT
    # Where the value must end up for an adaptation to count. None means any
    # destination that validates and preserves the value is acceptable.
    expected_target: str | None = None
    note: str = ""

    @property
    def old_key(self) -> str:
        return f"{self.from_version}|{self.operation}"

    @property
    def new_key(self) -> str:
        return f"{self.to_version}|{self.operation}"


@dataclass
class CaseSet:
    provider: str = "stripe"
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    cases: list[Case] = field(default_factory=list)
    generated_by: str = ""

    def old_contract(self, case: Case) -> dict[str, Any]:
        return self.contracts[case.old_key]

    def new_contract(self, case: Case) -> dict[str, Any]:
        return self.contracts[case.new_key]

    def by_kind(self) -> dict[str, list[Case]]:
        out: dict[str, list[Case]] = {}
        for case in self.cases:
            out.setdefault(case.kind, []).append(case)
        return out

    def __iter__(self) -> Iterator[Case]:
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)

    # ---- persistence -------------------------------------------------

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"provider": self.provider,
                           "generated_by": self.generated_by,
                           "contracts": self.contracts,
                           "cases": [asdict(c) for c in self.cases]},
                          indent=1, sort_keys=True).encode("utf-8")
        if path.suffix == ".gz":
            with gzip.open(path, "wb", compresslevel=9) as fh:
                fh.write(blob)
        else:
            path.write_bytes(blob)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "CaseSet":
        path = Path(path)
        raw = (gzip.open(path, "rb").read() if path.suffix == ".gz"
               else path.read_bytes())
        data = json.loads(raw.decode("utf-8"))
        return cls(provider=data.get("provider", "unknown"),
                   generated_by=data.get("generated_by", ""),
                   contracts=data["contracts"],
                   cases=[Case(**c) for c in data["cases"]])
