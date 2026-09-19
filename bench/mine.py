"""Mine drift cases out of a provider's real version history.

No human annotation. A case's label comes from the structure of the change
itself: a removed field with a closely named successor is a rename, one without
is a removal, an enum that lost a value we send is a narrowing, and so on.

Every candidate is then held to two tests before it becomes a case: the request
must validate under the old contract, and must fail under the new one. Anything
that does not meet both is discarded, because a case that was already broken or
is already fine measures the miner rather than the solver.
"""

from __future__ import annotations

import difflib
from typing import Any

from sell.real import diff as differ
from sell.real import sources
from sell.real.validator import Request, validate

from .cases import (ADAPT, ENUM_NARROWED, ESCALATE, LENGTH_TIGHTENED,
                    NEWLY_REQUIRED, REMOVED, RENAME, TYPE_CHANGED, Case, CaseSet)

# A value distinctive enough that finding it again in the adapted request is
# meaningful rather than coincidental.
CAP_MARKER = "CAP-{field}"
RENAME_CUTOFF = 0.6
MAX_REPAIR_STEPS = 24


def _value_for(facts: dict[str, Any], field_name: str, *,
               traceable: bool = False) -> Any:
    """A value for a field. `traceable` means it must be findable again.

    The focus field's value is how the scorer tells a real adaptation from a
    coincidence, so it can never be empty. Nullable enums routinely list "" as a
    legitimate member, and picking it made correct renames look like failures.
    """
    allowed = facts.get("allowed")
    if allowed:
        usable = [a for a in allowed if a not in (None, "", [], {})]
        if traceable:
            return usable[0] if usable else None
        return (usable or allowed)[0]
    declared = str(facts.get("type", "string")).split("|")[0]
    if declared == "integer":
        return 424242
    if declared == "number":
        return 424242
    if declared == "boolean":
        return True
    if declared == "array":
        return [CAP_MARKER.format(field=field_name)]
    if declared == "object":
        return {}
    marker = CAP_MARKER.format(field=field_name)
    max_length = facts.get("max_length")
    if isinstance(max_length, int) and max_length < len(marker):
        return marker[:max_length] or "x"
    return marker


def _expectation(kind: str, focus: str, focus_value: Any,
                 replacement: str | None,
                 new: dict[str, Any]) -> tuple[str, str | None]:
    """What a correct solver should do with this case, and where the value goes.

    The rule throughout: adaptation is only expected where the new contract
    actually contains the answer. Everywhere else the right behaviour is to ask,
    and adopting a guess counts against the solver.
    """
    if kind == RENAME:
        return ADAPT, replacement
    if kind == REMOVED:
        # No successor in the contract. Anywhere the value could be put would be
        # a guess, and a guess that validates is the dangerous outcome.
        return ESCALATE, None
    if kind == NEWLY_REQUIRED:
        return ADAPT, None
    if kind == ENUM_NARROWED:
        allowed = (new.get("fields", {}).get(focus, {}) or {}).get("allowed") or []
        target = next((a for a in allowed if _norm(a) == _norm(focus_value)), None)
        if target is None:
            near = difflib.get_close_matches(str(focus_value),
                                            [str(a) for a in allowed], n=1,
                                            cutoff=0.7)
            target = near[0] if near else None
        # A value set replaced wholesale carries no local evidence of which new
        # value replaced which old one.
        return (ADAPT, None) if target is not None else (ESCALATE, None)
    if kind == TYPE_CHANGED:
        return ADAPT, None
    if kind == LENGTH_TIGHTENED:
        # Truncating someone's data to fit is a decision, not a conversion.
        return ESCALATE, None
    return ADAPT, None


def _norm(value: Any) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _repair(contract: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """Add whatever the old contract insists on, so the request is genuinely
    valid before the drift. Returns None if it cannot be made valid."""
    payload = dict(payload)
    for _ in range(MAX_REPAIR_STEPS):
        error = validate(contract, Request.from_keyed(payload))
        if error is None:
            return payload
        if error["code"] != "missing_required_field":
            return None
        name = error["field"]
        facts = contract.get("fields", {}).get(name, {})
        payload[name] = _value_for(facts, name)
    return None


def _build_payload(old: dict[str, Any], focus: str | None,
                   focus_value: Any = None) -> dict[str, Any] | None:
    """A request valid under `old` that exercises `focus`, or None if impossible."""
    payload: dict[str, Any] = {}
    fields = old.get("fields", {})
    for name, facts in fields.items():
        # Path parameters are structural: the request cannot be addressed without
        # them, so they are always part of a realistic payload.
        if facts.get("location") == "path" or facts.get("required"):
            payload[name] = _value_for(facts, name)
    if focus is not None and focus in fields:
        value = (focus_value if focus_value is not None
                 else _value_for(fields[focus], focus, traceable=True))
        if value in (None, "", [], {}):
            return None          # nothing traceable to carry; not a usable case
        payload[focus] = value
        # A nested focus field drags in whatever its parent object requires.
        if "." in focus:
            parent = focus.rsplit(".", 1)[0]
            for name, facts in fields.items():
                if name.startswith(parent + ".") and facts.get("required_if_present"):
                    payload.setdefault(name, _value_for(facts, name))
    return _repair(old, payload)


def _classify(signal, old: dict[str, Any], new: dict[str, Any],
              added_fields: list[str]) -> tuple[str, str, Any, str | None] | None:
    """(kind, focus_field, focus_value, replacement_candidate) or None to skip."""
    kind_raw, detail = signal.kind, signal.detail
    field_name = detail.get("field")
    if not field_name:
        return None
    old_facts = old.get("fields", {}).get(field_name, {})
    # An object-typed field has no scalar value of its own, so a payload can only
    # carry it as `{}` -- and dropping an empty object loses nothing a checker can
    # see, which made these cases free. The diff emits the nested leaves as their
    # own signals, so the real content is still covered.
    if kind_raw in ("field_removed", "type_changed") \
            and str(old_facts.get("type", "")).startswith("object"):
        return None

    if kind_raw == "field_removed":
        match = difflib.get_close_matches(field_name, added_fields, n=1,
                                         cutoff=RENAME_CUTOFF)
        value = _value_for(old_facts, field_name, traceable=True)
        if match:
            return RENAME, field_name, value, match[0]
        return REMOVED, field_name, value, None

    if kind_raw == "field_added":
        # Only interesting when it is mandatory: an optional addition breaks
        # nobody and would be a free case.
        if not (detail.get("spec") or {}).get("required"):
            return None
        return NEWLY_REQUIRED, field_name, None, None

    if kind_raw == "allowed_changed":
        removed = detail.get("removed") or []
        if not removed:
            return None
        return ENUM_NARROWED, field_name, removed[0], None

    if kind_raw == "type_changed":
        return TYPE_CHANGED, field_name, \
            _value_for(old_facts, field_name, traceable=True), None

    if kind_raw == "max_length_changed":
        was, now = detail.get("was"), detail.get("now")
        if not (isinstance(was, int) and isinstance(now, int) and now < was):
            return None
        return LENGTH_TIGHTENED, field_name, "x" * min(was, now + 40), None

    return None


def mine_window(old_ref: str, new_ref: str, *, provider: str = "stripe",
                max_depth: int = 1, log=lambda _s: None) -> tuple[list[Case], dict]:
    old_all = sources.load_contracts(old_ref, provider=provider, max_depth=max_depth)
    new_all = sources.load_contracts(new_ref, provider=provider, max_depth=max_depth)
    drifts, _, _ = differ.diff_all(old_all, new_all)

    cases: list[Case] = []
    contracts: dict[str, dict[str, Any]] = {}
    skipped = {"unbuildable": 0, "already_valid": 0, "unclassified": 0}

    for drift in drifts:
        if not drift.breaking:
            continue
        old_c, new_c = old_all[drift.operation], new_all[drift.operation]
        added = [s.detail["field"] for s in drift.signals if s.kind == "field_added"]
        for signal in drift.breaking:
            classified = _classify(signal, old_c, new_c, added)
            if classified is None:
                skipped["unclassified"] += 1
                continue
            kind, focus, focus_value, replacement = classified

            payload = _build_payload(old_c, focus if kind != NEWLY_REQUIRED else None,
                                     focus_value)
            if payload is None:
                skipped["unbuildable"] += 1
                continue
            # The drift must actually break this request, or there is nothing to
            # measure. This is the gate that keeps the benchmark honest.
            if validate(new_c, Request.from_keyed(payload)) is None:
                skipped["already_valid"] += 1
                continue

            expected, target = _expectation(kind, focus, focus_value,
                                            replacement, new_c)
            cases.append(Case(
                case_id=f"{provider}:{old_ref}->{new_ref}:{drift.operation}:{focus}",
                provider=provider, operation=drift.operation,
                from_version=old_ref, to_version=new_ref, kind=kind,
                focus_field=focus, payload=payload,
                replacement_candidate=replacement,
                expected_outcome=expected, expected_target=target,
                note=signal.summary()))
            contracts[f"{old_ref}|{drift.operation}"] = old_c
            contracts[f"{new_ref}|{drift.operation}"] = new_c

    log(f"  {old_ref}->{new_ref}: {len(cases)} cases "
        f"(skipped {skipped['already_valid']} already-valid, "
        f"{skipped['unbuildable']} unbuildable, "
        f"{skipped['unclassified']} unclassified)")
    return cases, contracts


def mine(windows: list[tuple[str, str]], *, provider: str = "stripe",
         max_depth: int = 1, log=lambda _s: None) -> CaseSet:
    result = CaseSet(provider=provider,
                     generated_by=f"bench.mine windows={windows} depth={max_depth}")
    seen: set[str] = set()
    for old_ref, new_ref in windows:
        cases, contracts = mine_window(old_ref, new_ref, provider=provider,
                                       max_depth=max_depth, log=log)
        result.contracts.update(contracts)
        for case in cases:
            if case.case_id in seen:
                continue
            seen.add(case.case_id)
            result.cases.append(case)
    result.cases.sort(key=lambda c: c.case_id)
    return result
