"""A simulated partner API that drifts, plus the signals a real one emits.

This stands in for the environment the agent operates against. It is
deliberately unhelpful in the ways real integrations are unhelpful:

  * some drifts announce themselves in the spec (proactively sensable),
  * some only surface as a 400 when a task fails (reactively sensable),
  * and at least one is *silent* -- the write is accepted and looks fine,
    and only a reconciliation several ticks later reveals it was wrong.

That last class is the reason self-evaluation cannot rest on the agent's own
confidence: nothing about the response says anything went wrong.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

MONEY_DOLLARS = "Amount in whole dollars."
MONEY_CENTS = "Amount in minor units (cents)."


@dataclass
class Response:
    ok: bool
    record_id: str | None = None
    error: dict[str, Any] | None = None


@dataclass
class FieldSpec:
    name: str
    type: str                      # string | integer | date | enum
    required: bool = True
    allowed: list[str] | None = None
    pattern: str | None = None
    description: str = ""
    deprecated_for: str | None = None   # set when a rename is announced early

    def as_spec(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "required": self.required,
                             "description": self.description}
        if self.allowed is not None:
            d["allowed"] = list(self.allowed)
        if self.pattern is not None:
            d["pattern"] = self.pattern
        if self.deprecated_for is not None:
            d["deprecated_for"] = self.deprecated_for
        return d


def _initial_fields() -> dict[str, FieldSpec]:
    return {
        "invoice_ref":    FieldSpec("invoice_ref", "string"),
        "customer_email": FieldSpec("customer_email", "string",
                                    pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
        "amount":         FieldSpec("amount", "integer", description=MONEY_DOLLARS),
        "issued_on":      FieldSpec("issued_on", "date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
        "terms":          FieldSpec("terms", "enum", allowed=["net15", "net30", "net60"]),
    }


class PartnerAPI:
    """The environment. Mutates itself when a drift is applied."""

    def __init__(self) -> None:
        self.fields: dict[str, FieldSpec] = _initial_fields()
        self.spec_version = 1
        self.changelog: list[dict[str, Any]] = []
        self.records: dict[str, dict[str, Any]] = {}
        self._seq = 0
        # record_id -> (due_tick, expected_cents, cents_actually_booked)
        # The booked figure is computed at submit time: a later change to how
        # the partner reads `amount` cannot retroactively alter what the ledger
        # already recorded.
        self._pending: dict[str, tuple[int, int, int]] = {}
        self.reconcile_lag = 3
        # How the partner interprets `amount`. Deliberately NOT part of the
        # published contract -- a real integration learns this the hard way.
        self.amount_unit = "dollars"
        # A staged next version, as many partner sandboxes expose. This is what
        # makes pre-emptive verification possible at all: without somewhere to
        # test the future contract, an announcement is only a hint.
        self.staged_fields: dict[str, FieldSpec] | None = None
        self.staged_drift: str | None = None

    # ---------------- observable surface (what the agent may call) ----------

    def get_spec(self, *, staged: bool = False) -> dict[str, Any]:
        fields = self.staged_fields if (staged and self.staged_fields) else self.fields
        return {
            "version": self.spec_version + (1 if staged and self.staged_fields else 0),
            "staged": bool(staged and self.staged_fields),
            "fields": {n: f.as_spec() for n, f in sorted(fields.items())},
        }

    def has_staged(self) -> bool:
        return self.staged_fields is not None

    def get_changelog(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self.changelog]

    def submit(self, payload: dict[str, Any], *, tick: int,
               sandbox: bool = False, staging: bool = False,
               expected_cents: int | None = None) -> Response:
        if staging:
            if self.staged_fields is None:
                return Response(ok=False, error={"code": "no_staged_version",
                                                 "field": None,
                                                 "message": "nothing staged"})
            return (Response(ok=True, record_id="staging")
                    if self._validate(payload, self.staged_fields) is None
                    else Response(ok=False, error=self._validate(payload, self.staged_fields)))
        err = self._validate(payload)
        if err is not None:
            return Response(ok=False, error=err)
        if sandbox:
            return Response(ok=True, record_id="sandbox")
        self._seq += 1
        rid = f"rec_{self._seq:04d}"
        self.records[rid] = dict(payload)
        if expected_cents is not None:
            self._pending[rid] = (tick + self.reconcile_lag, expected_cents,
                                  self._booked_cents(payload))
        return Response(ok=True, record_id=rid)

    def rollback(self, record_id: str) -> bool:
        """Reversibility primitive. Real connectors get this from a void/delete
        endpoint or a compensating write; without it, autonomy is indefensible."""
        self._pending.pop(record_id, None)
        return self.records.pop(record_id, None) is not None

    def reconcile(self, tick: int) -> list[dict[str, Any]]:
        """Delayed ground truth: the partner's ledger, some ticks later.

        This is the signal that catches a drift the 200 OK hid.
        """
        findings: list[dict[str, Any]] = []
        for rid, (due, expected_cents, booked) in list(self._pending.items()):
            if tick < due:
                continue
            del self._pending[rid]
            if rid not in self.records:
                continue
            if booked != expected_cents:
                findings.append({
                    "type": "reconciliation_mismatch",
                    "record_id": rid,
                    "field": "amount",
                    "expected_cents": expected_cents,
                    "booked_cents": booked,
                    "message": (f"ledger booked {booked} cents, source document says "
                                f"{expected_cents} cents"),
                })
        return findings

    # ---------------- internals -------------------------------------------

    def _booked_cents(self, rec: dict[str, Any]) -> int:
        amount = rec.get("amount")
        if not isinstance(amount, int):
            return -1
        return amount if self.amount_unit == "cents" else amount * 100

    def _validate(self, payload: dict[str, Any],
                  fields: dict[str, FieldSpec] | None = None) -> dict[str, Any] | None:
        fields = self.fields if fields is None else fields
        for name in payload:
            if name not in fields:
                return {"code": "unknown_field", "field": name,
                        "message": f"unrecognized field '{name}'",
                        "known_fields": sorted(fields)}
        for name, spec in fields.items():
            if name not in payload:
                if spec.required:
                    return {"code": "missing_required_field", "field": name,
                            "message": f"field '{name}' is required",
                            "expected_type": spec.type,
                            "allowed": spec.allowed}
                continue
            v = payload[name]
            if spec.type == "integer" and not isinstance(v, int):
                return {"code": "type_mismatch", "field": name,
                        "message": f"field '{name}' must be an integer, got "
                                   f"{type(v).__name__}",
                        "expected_type": "integer"}
            if spec.type in ("string", "date") and not isinstance(v, str):
                return {"code": "type_mismatch", "field": name,
                        "message": f"field '{name}' must be a string",
                        "expected_type": "string"}
            if spec.type == "enum" and v not in (spec.allowed or []):
                return {"code": "value_not_allowed", "field": name,
                        "message": f"'{v}' is not an accepted value for '{name}'",
                        "allowed": list(spec.allowed or [])}
            if spec.pattern and isinstance(v, str) and not re.match(spec.pattern, v):
                return {"code": "format_invalid", "field": name,
                        "message": f"field '{name}' does not match required format",
                        "pattern": spec.pattern}
        return None

    # ---------------- drifts ----------------------------------------------

    def apply_drift(self, name: str, tick: int) -> str:
        fn = getattr(self, f"_drift_{name}", None)
        if fn is None:
            raise KeyError(f"no such drift: {name}")
        note = fn()
        if note.startswith("(no announcement"):
            # An undocumented change leaves no trace on the observable surface:
            # no version bump, no changelog entry, nothing to diff against.
            return note
        self.spec_version += 1
        self.changelog.append({"tick": tick, "version": self.spec_version, "note": note})
        return note

    def _drift_rename_email(self) -> str:
        old = self.fields.pop("customer_email")
        self.fields["contact_email"] = FieldSpec(
            "contact_email", old.type, pattern=old.pattern,
            description="Billing contact email address.")
        return "renamed 'customer_email' to 'contact_email'"

    def _drift_require_currency(self) -> str:
        self.fields["currency"] = FieldSpec(
            "currency", "enum", allowed=["USD", "EUR", "GBP"],
            description="ISO 4217 currency code.")
        return "added required field 'currency'"

    def _drift_amount_to_cents_documented(self) -> str:
        """Semantics move, but the prose says so. Sensable before any task fails."""
        self.amount_unit = "cents"
        self.fields["amount"].description = MONEY_CENTS
        return "'amount' is now interpreted in minor units (cents), not dollars"

    def _drift_amount_to_cents_silent(self) -> str:
        """The nastiest real drift: behaviour changes, the contract does not.

        Type stays integer, so a dollars value still validates. Description is
        unchanged, so a spec diff shows nothing. The submission returns 200 and
        looks perfect. Only the partner's ledger, some ticks later, disagrees.

        No amount of introspection catches this. It is the reason self-reported
        confidence cannot be the evaluator.
        """
        self.amount_unit = "cents"
        return "(no announcement -- undocumented change to amount handling)"

    def _drift_terms_enum(self) -> str:
        self.fields["terms"].allowed = ["NET_15", "NET_30", "NET_60", "DUE_ON_RECEIPT"]
        return "'terms' values switched to upper snake case; added DUE_ON_RECEIPT"

    def _drift_iso_dates(self) -> str:
        f = self.fields["issued_on"]
        f.pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        f.description = "Issue timestamp, RFC3339 UTC."
        return "'issued_on' now requires a full RFC3339 UTC timestamp"

    def stage_drift(self, name: str, tick: int) -> str:
        """Publish the next version to the sandbox and announce it, without
        changing production. The agent can now verify a fix a tick early."""
        fn = getattr(self, f"_drift_{name}")
        live = copy.deepcopy(self.fields)
        note = fn()                     # mutates self.fields
        self.staged_fields = self.fields
        self.staged_drift = name
        self.fields = live              # production is untouched
        self.changelog.append({"tick": tick, "version": self.spec_version + 1,
                               "note": note, "upcoming": True})
        return note

    def promote_staged(self, tick: int) -> str:
        assert self.staged_fields is not None, "nothing staged"
        self.fields = self.staged_fields
        self.staged_fields = None
        name, self.staged_drift = self.staged_drift, None
        self.spec_version += 1
        note = f"staged change now live: {name}"
        self.changelog.append({"tick": tick, "version": self.spec_version, "note": note})
        return note

    def announce_upcoming(self, note: str, tick: int) -> None:
        """A deprecation notice published before the change lands.

        This is what makes proactive adaptation possible at all: the agent can
        read it, patch, and verify before a single task fails.
        """
        self.changelog.append({"tick": tick, "version": self.spec_version,
                               "note": note, "upcoming": True})
