"""Validate a request against a provider's own published contract.

This is a real oracle, not a simulation: every rule it enforces was written by
the provider in their OpenAPI document. It needs no credentials and no network,
so it can run on every candidate in an experiment.

What it can decide: unknown fields, missing required fields, conditionally
required fields whose parent is present, wrong types, values outside an enum,
pattern and length violations. That is the whole class of drift that shows up as
a 400 from the provider.

What it cannot decide: anything the schema does not express. A payload can be
perfectly valid and still mean the wrong thing -- the same blind spot the
simulated partner's silent unit change exploits. `gate.py` is where that gap is
handled rather than papered over.

Errors are returned in the same shape `PartnerAPI._validate` returns, so the
existing reasoner reads them with no special case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .openapi import LOC_SIGIL

_TYPE_CHECKS: dict[str, tuple[type, ...] | None] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "unknown": None,          # the spec did not say; do not invent a rule
}


@dataclass
class Request:
    """A request split the way a real API splits it."""
    body: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    path: dict[str, Any] = field(default_factory=dict)
    header: dict[str, Any] = field(default_factory=dict)

    def keyed(self) -> dict[str, Any]:
        """Flatten to the sigil'd keys a contract uses."""
        out: dict[str, Any] = dict(self.body)
        for name, value in self.query.items():
            out[f"{LOC_SIGIL['query']}{name}"] = value
        for name, value in self.path.items():
            out[f"{{{name}}}"] = value
        for name, value in self.header.items():
            out[f"{LOC_SIGIL['header']}{name}"] = value
        return out

    @classmethod
    def from_keyed(cls, keyed: dict[str, Any]) -> "Request":
        req = cls()
        for key, value in keyed.items():
            if key.startswith("?"):
                req.query[key[1:]] = value
            elif key.startswith("{") and key.endswith("}"):
                req.path[key[1:-1]] = value
            elif key.startswith("~"):
                req.header[key[1:]] = value
            else:
                req.body[key] = value
        return req

    def is_empty(self) -> bool:
        return not (self.body or self.query or self.path or self.header)


def _type_ok(value: Any, declared: str) -> bool:
    # anyOf merging can produce a union like "integer|object"; any branch passing
    # is enough, and an unknown type means the spec declined to constrain it.
    for part in str(declared).split("|"):
        expected = _TYPE_CHECKS.get(part, None)
        if part not in _TYPE_CHECKS:
            return True
        if expected is None:
            return True
        if part == "integer" and isinstance(value, bool):
            continue          # a bool is an int in Python but not in an API
        if isinstance(value, expected):
            return True
    return False


def _parent_present(keyed: dict[str, Any], field_name: str) -> bool:
    """Was the object that owns this nested field supplied at all?"""
    if "." not in field_name:
        return True
    parent = field_name.rsplit(".", 1)[0]
    if parent in keyed:
        return True
    return any(k.startswith(parent + ".") for k in keyed)


def validate(contract: dict[str, Any], request: Request | dict[str, Any], *,
             allow_unknown: bool = False) -> dict[str, Any] | None:
    """First violation, or None. Deterministic; no model, no network."""
    keyed = request.keyed() if isinstance(request, Request) else dict(request)
    fields: dict[str, dict[str, Any]] = contract.get("fields", {})

    if not allow_unknown:
        for name in sorted(keyed):
            if name in fields:
                continue
            # A nested key whose parent is a declared free-form object is fine.
            parent = name.rsplit(".", 1)[0] if "." in name else None
            if parent and fields.get(parent, {}).get("type") in ("object", "unknown"):
                continue
            return {"code": "unknown_field", "field": name,
                    "message": f"'{name}' is not in the published contract",
                    "known_fields": sorted(fields)}

    for name, facts in sorted(fields.items()):
        supplied = name in keyed
        if not supplied:
            if facts.get("required"):
                return {"code": "missing_required_field", "field": name,
                        "message": f"'{name}' is required",
                        "expected_type": facts.get("type"),
                        "allowed": facts.get("allowed")}
            if facts.get("required_if_present") and _parent_present(keyed, name):
                return {"code": "missing_required_field", "field": name,
                        "message": (f"'{name}' is required because its parent "
                                    f"object was supplied"),
                        "expected_type": facts.get("type"),
                        "allowed": facts.get("allowed")}
            continue

        value = keyed[name]
        if value is None:
            continue
        declared = facts.get("type", "unknown")
        if not _type_ok(value, declared):
            return {"code": "type_mismatch", "field": name,
                    "message": f"'{name}' must be {declared}, got "
                               f"{type(value).__name__}",
                    "expected_type": declared}
        allowed = facts.get("allowed")
        if allowed is not None and value not in allowed:
            return {"code": "value_not_allowed", "field": name,
                    "message": f"{value!r} is not an accepted value for '{name}'",
                    "allowed": list(allowed)}
        pattern = facts.get("pattern")
        if pattern and isinstance(value, str) and not re.match(pattern, value):
            return {"code": "format_invalid", "field": name,
                    "message": f"'{name}' does not match {pattern}",
                    "pattern": pattern}
        max_length = facts.get("max_length")
        if max_length is not None and isinstance(value, str) and len(value) > max_length:
            return {"code": "too_long", "field": name,
                    "message": f"'{name}' exceeds maxLength {max_length}",
                    "max_length": max_length}
    return None


def validate_all(contract: dict[str, Any], request: Request | dict[str, Any], *,
                 allow_unknown: bool = False) -> list[dict[str, Any]]:
    """Every violation, not just the first. Useful for a report; the gate only
    needs to know whether there was one."""
    keyed = request.keyed() if isinstance(request, Request) else dict(request)
    found: list[dict[str, Any]] = []
    remaining = dict(keyed)
    seen: set[str] = set()
    while True:
        error = validate(contract, remaining, allow_unknown=allow_unknown)
        if error is None or error["field"] in seen:
            break
        seen.add(error["field"])
        found.append(error)
        # Neutralise the field just reported so the next violation surfaces.
        if error["code"] in ("missing_required_field",):
            facts = contract.get("fields", {}).get(error["field"], {})
            allowed = facts.get("allowed")
            remaining[error["field"]] = (allowed[0] if allowed else
                                         _placeholder(facts.get("type", "string")))
        else:
            remaining.pop(error["field"], None)
    return found


def _placeholder(declared: str) -> Any:
    head = str(declared).split("|")[0]
    return {"integer": 0, "number": 0, "boolean": False, "array": [],
            "object": {}}.get(head, "x")


def encode_form(body: dict[str, Any]) -> list[tuple[str, str]]:
    """Dotted keys -> the bracket notation form-encoded APIs expect.

    `shipping.method` becomes `shipping[method]`, which is how Stripe and others
    receive nested objects over x-www-form-urlencoded.
    """
    pairs: list[tuple[str, str]] = []
    for key, value in body.items():
        parts = key.split(".")
        name = parts[0] + "".join(f"[{p}]" for p in parts[1:])
        if isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                pairs.append((f"{name}[{i}]", str(item)))
        elif isinstance(value, dict):
            for sub, item in value.items():
                pairs.append((f"{name}[{sub}]", str(item)))
        elif value is None:
            pairs.append((name, ""))
        else:
            pairs.append((name, str(value)))
    return pairs
