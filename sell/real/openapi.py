"""Adapter: a real OpenAPI 3 document -> the contract shape our sensors read.

The point of this module is that nothing downstream has to know it is looking at
real data. `contract()` returns exactly the shape `PartnerAPI.get_spec()`
returns, so `EnvironmentModel.diff()`, the invariant sensor and the reasoner all
work against a provider's published schema with no changes.

Real specs are messier than a simulation in three ways that matter:

  * request bodies nest, so a flat field name is not enough -- fields are
    addressed by dotted path, and a nested field is only genuinely required if
    every ancestor is too;
  * `anyOf` is used both for real unions and as an idiom for "or empty string to
    unset", and folding the sentinel branch into the enum would invent values
    that do not exist;
  * descriptions are long prose and change constantly, so they are stored as a
    hash plus a readable prefix rather than compared in full.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Parameters live in a different part of the document from the request body, but
# they are just as much part of the contract: a renamed query parameter or a
# newly required filter breaks a caller exactly as hard as a body field does.
# They share one field map, distinguished by sigil so they cannot collide with a
# JSON property name: "?name" query, "{name}" path, "~name" header.
LOC_SIGIL = {"query": "?", "path": "{", "header": "~", "cookie": "&"}

FORM = "application/x-www-form-urlencoded"
JSON_CT = "application/json"
DESC_PREFIX = 140

# Stripe (and others) spell "or unset this field" as an enum of the empty
# string. Treating that as a real allowed value pollutes every enum diff.
_UNSET_SENTINELS = ({""}, {"", None})


def load(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _resolve(schema: dict[str, Any], spec: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Follow a local $ref. Bounded, because real specs contain cycles."""
    ref = schema.get("$ref")
    if not ref or depth > 8:
        return schema
    if not ref.startswith("#/"):
        return schema
    node: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return schema
        node = node[part]
    merged = {k: v for k, v in schema.items() if k != "$ref"}
    return _resolve({**node, **merged}, spec, depth + 1)


def _merge_any_of(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Flatten anyOf/oneOf into one description of the field.

    The empty-string 'unset' branch is dropped rather than merged: it is an API
    idiom, not a value the caller is choosing between.
    """
    branches = schema.get("anyOf") or schema.get("oneOf")
    if not branches:
        return schema
    real: list[dict[str, Any]] = []
    for branch in branches:
        branch = _resolve(branch, spec)
        if set(branch.get("enum", [])) in _UNSET_SENTINELS:
            continue
        real.append(branch)
    if not real:
        return schema
    merged = {k: v for k, v in schema.items() if k not in ("anyOf", "oneOf")}
    types, enums = [], []
    for branch in real:
        if branch.get("type") and branch["type"] not in types:
            types.append(branch["type"])
        for v in branch.get("enum", []) or []:
            if v not in enums:
                enums.append(v)
        for key in ("properties", "pattern", "maxLength", "items"):
            if key in branch and key not in merged:
                merged[key] = branch[key]
    if types:
        merged["type"] = types[0] if len(types) == 1 else "|".join(sorted(types))
    if enums:
        merged["enum"] = enums
    return merged


def _facts(schema: dict[str, Any], required: bool,
           required_if_present: bool = False) -> dict[str, Any]:
    desc = schema.get("description") or ""
    out: dict[str, Any] = {"type": schema.get("type", "unknown"), "required": required}
    if required_if_present and not required:
        # "Mandatory only if you send the parent object." Flattening this to
        # `required: False` would hide a real breaking change from anyone who
        # does send it, so both facts are kept.
        out["required_if_present"] = True
    if "enum" in schema:
        out["allowed"] = list(schema["enum"])
    if "pattern" in schema:
        out["pattern"] = schema["pattern"]
    if "maxLength" in schema:
        out["max_length"] = schema["maxLength"]
    if desc:
        # Full prose is too noisy to diff and too long to print; a hash detects
        # any change, the prefix makes the report readable.
        out["description"] = desc[:DESC_PREFIX]
        out["description_digest"] = _digest(desc)
    return out


def _walk(schema: dict[str, Any], spec: dict[str, Any], prefix: str,
          out: dict[str, dict[str, Any]], depth: int, max_depth: int,
          ancestor_required: bool) -> None:
    schema = _merge_any_of(_resolve(schema, spec), spec)
    props = schema.get("properties") or {}
    required_here = set(schema.get("required") or [])
    for name, child_raw in props.items():
        child = _merge_any_of(_resolve(child_raw, spec), spec)
        path = f"{prefix}{name}"
        # A nested field is only really required if everything above it is.
        named_required = name in required_here
        is_required = ancestor_required and named_required
        out[path] = _facts(child, is_required, required_if_present=named_required)
        if child.get("properties") and depth < max_depth:
            _walk(child, spec, f"{path}.", out, depth + 1, max_depth, is_required)


def _parameters(operation: dict[str, Any], path_item: dict[str, Any],
                spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Path-level and operation-level parameters, operation-level winning.

    The OpenAPI rule is that a parameter is identified by (name, in), and an
    operation-level entry overrides a path-level one with the same identity.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in list(path_item.get("parameters") or []) + list(operation.get("parameters") or []):
        param = _resolve(raw, spec)
        name, location = param.get("name"), param.get("in")
        if not name or not location:
            continue
        merged[(name, location)] = param

    out: dict[str, dict[str, Any]] = {}
    for (name, location), param in merged.items():
        schema = _merge_any_of(_resolve(param.get("schema") or {}, spec), spec)
        # A path parameter is always required, whatever the document says.
        required = bool(param.get("required")) or location == "path"
        facts = _facts({**schema, "description": param.get("description")
                        or schema.get("description") or ""}, required)
        facts["location"] = location
        key = f"{LOC_SIGIL.get(location, '?')}{name}"
        if location == "path":
            key = f"{{{name}}}"
        out[key] = facts
    return out


def _request_schema(operation: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    content = (operation.get("requestBody") or {}).get("content") or {}
    for ct in (FORM, JSON_CT):
        if ct in content and "schema" in content[ct]:
            return content[ct]["schema"]
    for body in content.values():
        if "schema" in body:
            return body["schema"]
    return None


def operations(spec: dict[str, Any], *, with_contract_only: bool = True) -> list[tuple[str, str]]:
    """Every operation, or only those that actually accept input.

    An operation with neither a request body nor parameters has no contract a
    caller can drift against, so it is skipped by default.
    """
    found: list[tuple[str, str]] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(operation, dict):
                continue
            if with_contract_only and _request_schema(operation, spec) is None \
                    and not _parameters(operation, path_item, spec):
                continue
            found.append((method.lower(), path))
    return sorted(found)


def contract(spec: dict[str, Any], method: str, path: str, *,
             max_depth: int = 2) -> dict[str, Any]:
    """Return the request contract for one operation, in EnvironmentModel shape.

    Deliberately identical to what the simulated partner returns, so the same
    sensors read both.
    """
    path_item = (spec.get("paths") or {}).get(path) or {}
    operation = path_item.get(method.lower())
    if operation is None:
        raise KeyError(f"{method.upper()} {path} not in this spec")
    fields: dict[str, dict[str, Any]] = dict(_parameters(operation, path_item, spec))
    schema = _request_schema(operation, spec)
    if schema is not None:
        # Top-level body fields have no ancestors, so required is decided here.
        _walk(schema, spec, "", fields, 0, max_depth, ancestor_required=True)
    return {
        "version": (spec.get("info") or {}).get("version", "unknown"),
        "operation": f"{method.upper()} {path}",
        "fields": dict(sorted(fields.items())),
    }


def contracts(spec: dict[str, Any], ops: list[tuple[str, str]] | None = None, *,
              max_depth: int = 1) -> dict[str, dict[str, Any]]:
    """Every operation's contract, keyed by 'METHOD /path'.

    Depth defaults lower than `contract()`: at repo scale the nested expansion
    dominates runtime, and one level already covers the fields a caller sets.
    """
    out: dict[str, dict[str, Any]] = {}
    for method, path in (ops if ops is not None else operations(spec)):
        try:
            c = contract(spec, method, path, max_depth=max_depth)
        except KeyError:
            continue
        out[c["operation"]] = c
    return out
