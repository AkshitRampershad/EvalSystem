"""The verification gate for a real provider.

Three tiers, with deliberately different authority. Conflating them is how a
self-modifying agent ships a confident mistake.

  1. SCHEMA   -- the provider's own published contract accepts this request.
                 Free, offline, runs on every candidate. Proves only that the
                 provider will not reject it; it says nothing about meaning.
  2. SANDBOX  -- the provider's test environment accepted it for real. Catches
                 undocumented constraints the schema never expressed. Needs
                 credentials, so it is opt-in and off by default.
  3. OUTCOME  -- what actually happened downstream. Not available here at all:
                 it arrives later, and `agent.py` handles it via the ledger
                 sensor. Any claim of semantic correctness belongs to this tier.

Between tiers 1 and 2 sits the check that matters most in practice. A candidate
can satisfy the schema by *dropping* the field it could not work out -- valid
request, silent loss of behaviour. `CapabilityCheck` compares what the request
used to express against what it expresses now, and refuses to let that pass as a
verified fix. It cannot say where the value should have gone, so it escalates
with one specific question instead of guessing.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..policy import Patch, Policy
from .validator import Request, encode_form, validate

# Verdict tiers, weakest first.
REJECTED = "rejected"
NEEDS_HUMAN = "needs_human"
SCHEMA = "schema"
SANDBOX = "sandbox"


@dataclass
class Verdict:
    patch: Patch | None
    tier: str
    rejected: list[tuple[str, str]] = field(default_factory=list)
    question: str | None = None
    schema_checks: int = 0
    sandbox_calls: int = 0
    lost_capability: list[str] = field(default_factory=list)

    @property
    def adoptable(self) -> bool:
        """Only a positively verified candidate may become policy.

        NEEDS_HUMAN is deliberately not adoptable: a request that validates while
        having quietly stopped doing its job is the worst outcome available, and
        it is indistinguishable from success at tiers 1 and 2.
        """
        return self.patch is not None and self.tier in (SCHEMA, SANDBOX)

    def summary(self) -> str:
        if self.patch is None:
            return f"{self.tier}: no candidate survived ({len(self.rejected)} tried)"
        bits = [f"verified at tier '{self.tier}'",
                f"{self.schema_checks} schema checks"]
        if self.sandbox_calls:
            bits.append(f"{self.sandbox_calls} sandbox calls")
        return f"{self.patch.describe()} -- " + ", ".join(bits)


# ---------------------------------------------------------------------------
# Tier 1: the provider's published contract
# ---------------------------------------------------------------------------

class SchemaOracle:
    """Real rules, written by the provider, enforced locally and for free."""

    tier = SCHEMA

    def __init__(self, contract: dict[str, Any]) -> None:
        self.contract = contract
        self.checks = 0

    def check(self, payload: dict[str, Any]) -> str | None:
        self.checks += 1
        error = validate(self.contract, Request.from_keyed(payload))
        if error is None:
            return None
        return f"{error['code']} on '{error['field']}'"


# ---------------------------------------------------------------------------
# The gap between tiers: behaviour the schema cannot see
# ---------------------------------------------------------------------------

@dataclass
class CapabilityCheck:
    """Did this candidate stop expressing something the request used to express?

    Matching is on values, not keys, which is what makes it useful: a rename
    carries the value to a new key and passes, while a drop loses the value and
    does not. That distinction is invisible to any schema.
    """

    ignore: frozenset[str] = frozenset()

    @staticmethod
    def _scalars(value: Any, out: set[str], depth: int = 0) -> None:
        """Every scalar reachable inside a value.

        Nesting matters: a provider that replaces `coupon: "X"` with
        `discounts: [{"coupon": "X"}]` has kept the capability, and comparing
        only top-level values would call that a loss. The value has to be found
        wherever it ended up.
        """
        if depth > 6:
            return
        if isinstance(value, dict):
            for item in value.values():
                CapabilityCheck._scalars(item, out, depth + 1)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                CapabilityCheck._scalars(item, out, depth + 1)
        elif value not in (None, "", [], {}):
            out.add(json.dumps(value, sort_keys=True, default=str))

    def lost(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        surviving: set[str] = set()
        for value in after.values():
            self._scalars(value, surviving)
        gone: list[str] = []
        for key, value in before.items():
            if key in self.ignore or value in (None, "", [], {}):
                continue
            wanted: set[str] = set()
            self._scalars(value, wanted)
            # Kept only if every scalar it carried is still somewhere in the
            # request. A partial carry is a partial loss, and counts as loss.
            if wanted and not wanted <= surviving:
                gone.append(key)
        return sorted(gone)

    @staticmethod
    def question(fields: list[str], contract: dict[str, Any]) -> str:
        names = ", ".join(f"'{f}'" for f in fields)
        return (f"{names} no longer appears in the request and its value is not "
                f"carried by any other field. The published contract does not say "
                f"where it moved. Was this capability removed, or does it now go "
                f"somewhere else in "
                f"{contract.get('operation', 'this operation')}?")


# ---------------------------------------------------------------------------
# Tier 2: the provider's sandbox
# ---------------------------------------------------------------------------

class Transport(Protocol):
    def __call__(self, method: str, url: str, body: list[tuple[str, str]],
                 headers: dict[str, str]) -> tuple[int, str]: ...


def urllib_transport(method: str, url: str, body: list[tuple[str, str]],
                     headers: dict[str, str]) -> tuple[int, str]:
    data = urllib.parse.urlencode(body).encode() if body else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


@dataclass
class SandboxConfig:
    base_url: str
    token: str
    # Off unless explicitly enabled: a sandbox probe is still a real request that
    # creates real (test-mode) objects, which is a side effect, not a read.
    enabled: bool = False
    require_test_credential: bool = True
    test_prefixes: tuple[str, ...] = ("sk_test_", "rk_test_", "test_")

    @classmethod
    def from_env(cls, provider: str = "stripe") -> "SandboxConfig | None":
        token = (os.environ.get("SELL_LIVE_TOKEN")
                 or os.environ.get(f"{provider.upper()}_TEST_KEY", ""))
        base = os.environ.get("SELL_LIVE_BASE_URL",
                              "https://api.stripe.com" if provider == "stripe" else "")
        if not token or not base:
            return None
        return cls(base_url=base.rstrip("/"), token=token,
                   enabled=os.environ.get("SELL_LIVE", "") == "1")

    def refusal(self) -> str | None:
        if not self.enabled:
            return "live sandbox not enabled (set SELL_LIVE=1)"
        if self.require_test_credential and not self.token.startswith(self.test_prefixes):
            # Refusing beats trusting an operator to have set the right key.
            return ("refusing to probe with a credential that is not a test key; "
                    "set SELL_LIVE_ALLOW_UNSAFE=1 only if you are certain")
        return None


class LiveSandbox:
    """Tier 2. Sends the candidate request to the provider's test environment."""

    tier = SANDBOX

    def __init__(self, config: SandboxConfig, method: str, path: str, *,
                 transport: Transport = urllib_transport) -> None:
        self.config = config
        self.method = method.upper()
        self.path = path
        self.transport = transport
        self.calls = 0
        self.last_status: int | None = None

    def available(self) -> str | None:
        if os.environ.get("SELL_LIVE_ALLOW_UNSAFE") == "1":
            self.config.require_test_credential = False
        return self.config.refusal()

    def check(self, payload: dict[str, Any]) -> str | None:
        request = Request.from_keyed(payload)
        path = self.path
        for name, value in request.path.items():
            path = path.replace(f"{{{name}}}", urllib.parse.quote(str(value)))
        url = f"{self.config.base_url}{path}"
        if request.query:
            url += "?" + urllib.parse.urlencode(
                [(k, str(v)) for k, v in request.query.items()])
        headers = {"Authorization": f"Bearer {self.config.token}",
                   "Content-Type": "application/x-www-form-urlencoded",
                   "User-Agent": "sell-engine/0.1"}
        headers.update({k: str(v) for k, v in request.header.items()})
        self.calls += 1
        status, text = self.transport(self.method, url, encode_form(request.body), headers)
        self.last_status = status
        if 200 <= status < 300:
            return None
        return self._explain(status, text)

    @staticmethod
    def _explain(status: int, text: str) -> str:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return f"HTTP {status}"
        err = payload.get("error") or payload
        detail = err.get("message") or err.get("detail") or ""
        param = err.get("param") or err.get("field") or ""
        code = err.get("code") or err.get("type") or f"http_{status}"
        return f"{code}" + (f" on '{param}'" if param else "") + \
               (f": {detail[:120]}" if detail else "")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class RealGate:
    """Mirrors `experiment.Experimenter` against a real provider.

    Order is chosen so that the cheapest check that can reject a candidate runs
    first, and so that nothing reaches the provider until it has already passed
    the provider's own written rules.
    """

    def __init__(self, contract: dict[str, Any], *, golden: Any = None,
                 sandbox: LiveSandbox | None = None,
                 capability: CapabilityCheck | None = None) -> None:
        self.oracle = SchemaOracle(contract)
        self.golden = golden
        self.sandbox = sandbox
        self.capability = capability or CapabilityCheck()
        self.contract = contract

    def evaluate(self, policy: Policy, candidates: list[Patch],
                 canonical: dict[str, Any], *,
                 regression: list[dict[str, Any]] | None = None) -> Verdict:
        before = policy.render(canonical)
        rejected: list[tuple[str, str]] = []
        escalation: Verdict | None = None

        for patch in candidates:
            trial = policy.preview(patch)

            if self.golden is not None:
                failure = next((self.golden.check_assertions(trial, case)
                                for case in self.golden.cases.values()
                                if self.golden.check_assertions(trial, case)), None)
                if failure:
                    rejected.append((patch.describe(), f"assertion: {failure}"))
                    continue

            after = trial.render(canonical)
            why = self.oracle.check(after)
            if why:
                rejected.append((patch.describe(), f"schema: {why}"))
                continue

            # Everything that ever worked must still validate.
            regressed = None
            for case in (regression or []):
                why = self.oracle.check(trial.render(case))
                if why:
                    regressed = f"regression: {why}"
                    break
            if regressed:
                rejected.append((patch.describe(), regressed))
                continue

            lost = self.capability.lost(before, after)
            if lost:
                # Schema-valid but behaviour-losing. Hold the first such candidate
                # as an escalation and keep looking for one that loses nothing.
                rejected.append((patch.describe(),
                                 f"would stop expressing {', '.join(lost)}"))
                if escalation is None:
                    escalation = Verdict(
                        patch=patch, tier=NEEDS_HUMAN, lost_capability=lost,
                        question=self.capability.question(lost, self.contract),
                        schema_checks=self.oracle.checks)
                continue

            tier = SCHEMA
            sandbox_calls = 0
            if self.sandbox is not None and self.sandbox.available() is None:
                why = self.sandbox.check(after)
                sandbox_calls = self.sandbox.calls
                if why:
                    rejected.append((patch.describe(), f"sandbox: {why}"))
                    continue
                tier = SANDBOX

            for rule in patch.add:
                rule.verified = True
            return Verdict(patch=patch, tier=tier, rejected=rejected,
                           schema_checks=self.oracle.checks,
                           sandbox_calls=sandbox_calls)

        if escalation is not None:
            escalation.rejected = rejected
            return escalation
        return Verdict(patch=None, tier=REJECTED, rejected=rejected,
                       schema_checks=self.oracle.checks)
