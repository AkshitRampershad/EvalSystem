# Self-learning integration agent

A working prototype of an agent that detects changes in its environment, works
out how to handle them, verifies the fix against the environment itself, and
adapts — with no human evaluating it, correcting it, or telling it what changed.

It runs offline with no dependencies and no API key:

```bash
python3 run_demo.py                  # full narrated run
python3 run_demo.py --quiet          # metrics only
python3 -m unittest discover -s tests -t .
```

## What the demo shows

A partner API drifts five times over thirty ticks while the agent keeps
submitting real invoices against it.

```
drift                                  lands fixed   lag       detected by  bad writes
renames customer_email -> contact_email   t5    t5    0t   changelog+spec*           0
makes 'currency' a required field         t9    t9    0t    changelog+spec           0
changes the 'terms' value set            t13   t13    0t    changelog+spec           0
requires RFC3339 timestamps              t16   t16    0t    changelog+spec           0
switches amount to cents, SILENTLY       t19   t22    3t            ledger           6

  tasks completed                 59
  tasks failed                     0
  policy versions adopted          5
  production writes rolled back    6
  HUMAN INTERVENTIONS              0
```

The first four drifts cost nothing: they are visible in the published contract,
so the agent sees them before a task can fail. The `*` on the first one means
the fix was verified against the partner's staged version a tick *before* the
change went live, and applied the moment it landed.

The fifth drift is the one that matters. The partner starts reading `amount` as
cents instead of dollars and **says nothing** — no version bump, no changelog
entry, no schema change. Every submission returns `200 OK`. A spec diff is
empty. Nothing an agent can introspect looks wrong, and for three ticks six
invoices are silently booked at 1/100th of their value.

Then the partner's ledger disagrees. The agent:

1. attributes the finding back to the decision that caused it, three ticks late;
2. rolls the bad write back;
3. converts the finding into a **permanent local assertion** — from now on any
   candidate policy that renders that record differently is rejected in
   microseconds instead of on the next three-tick round trip;
4. derives the correction from the ratio the ledger reported;
5. verifies it in the sandbox against all 61 accumulated regression cases;
6. adopts it, and re-submits the repaired records.

Then it recognises the five *later* findings still in flight as stale writes
from the old policy rather than fresh evidence — so it repairs them without
touching the policy again. An agent that skips that check oscillates.

## Why it is built this way

Five decisions carry the whole design.

**Detection never consults the agent's confidence.** Every signal is either
something the environment said or a check that evaluates deterministically. A
model's self-reported certainty is least reliable exactly on the novel inputs
this system exists to catch, so it is not permitted to gate anything. The
sensors run cheapest-first: changelog, spec diff, local pre-flight invariant,
rejection, ledger.

**Learning is data, not weights.** A policy is an ordered pipeline of small
named transforms. Every rule is inspectable, revertible, and carries provenance
back to the signal that justified it — which is also what makes the behaviour
auditable without extra machinery.

**A proposal is a hypothesis until the environment says otherwise.** The
reasoner (heuristics, or Claude) only proposes. Adoption requires passing the
accumulated assertions *and* being accepted by the partner's sandbox for this
task *and* for every case that ever worked before. That last clause is the
catastrophic-forgetting guard, and it is why a language model can be allowed to
write the agent's behaviour.

**Exploration only happens where actions leave no mark.** `experiment.py`
classifies every action FREE / REVERSIBLE / IRREVERSIBLE, unknown actions
default to irreversible, and experiments assert they are running against a FREE
action. Autonomy is defensible only because the undo path exists first.

**Autonomy is per input cluster, not per agent.** A new counterparty starts in
shadow and graduates on its own evidence; one unfamiliar cluster does not
demote the agent everywhere. The error budget is explicit — with no human
evaluator the agent learns by being wrong in production sometimes, so the
operator sets the rate rather than discovering it. In the run above both
clusters are demoted at t24 by the silent drift's rollbacks and re-graduate at
t27.

## Sensors against a real API

`sell/real/` points the same detection layer at contracts a real provider
actually shipped, so the drift is whatever really happened between two releases
rather than something the simulation invented. Stripe publishes its OpenAPI
document in a public git repo with 2,506 tagged versions, which means real drift
is available now instead of after waiting for some to occur.

```bash
python3 -m sell.real.scan --list-versions
python3 -m sell.real.scan --from v1500 --to v2000 --operation "POST /v1/customers" --propose
python3 -m sell.real.scan --from v500 --to v2506            # whole API
```

`openapi.contract()` returns exactly the shape `PartnerAPI.get_spec()` returns,
so `EnvironmentModel.diff()`, the invariant sensor and the reasoner read real
contracts with no changes.

### What real drift actually looks like

Every number below is measured, not estimated — 589 operations and ~6,500
contract fields per version.

| window | span | ops changed | ops with breaking | total changes | breaking |
|---|---|---|---|---|---|
| v500 → v1000 | Aug 2023 → Apr 2024 | 121 | 11 | 529 | 14 |
| v1000 → v1500 | → Feb 2025 | 109 | 2 | 459 | 3 |
| v1500 → v2000 | → Aug 2025 | 91 | 18 | 364 | 35 |
| v2000 → v2506 | → Aug 2026 | 214 | 7 | 1042 | 10 |
| v2400 → v2506 | one month | 12 | 0 | 19 | 0 |
| **v500 → v2506** | **three years** | **214** | **25** | **1716** | **44** |

Four findings, all of which change how you would build this:

**Breaking changes are 2.6% of spec changes.** Over three years, 1,716
field-level changes contained 44 that could break an existing caller. The rest
were 1,033 field additions and 577 description edits. A sensor that alerts on
"the spec changed" pages you roughly 39 times per real problem, which is how
drift detection gets switched off. The impact classifier is not a nicety; it is
the thing that makes the sensor usable.

**A well-run provider barely drifts.** Across one month and 106 releases, zero
breaking changes. Stripe versions by date and holds old behaviour, so the pain
this system addresses is concentrated in providers without that discipline, and
in internal APIs where nobody is guarding compatibility at all. That is where
to point it, and it is worth knowing before building a business on the premise.

**The published version string is not a drift signal.** Releases v2502 through
v2506 all report `info.version: 2026-08-26.dahlia` while their contents differ.
A sensor that polls the version number and diffs only on a bump sees nothing.
Structural diffing is not the expensive alternative to version watching; it is
the only one that works.

**Half the contract is not in the request body.** The first version of this
adapter read only `requestBody` and reported zero fields for 290 of 594
operations, because a `GET` takes its input as query parameters. Real breaking
changes were invisible until that was fixed — Stripe removing the `?refund`
query parameter from `GET /v1/credit_notes/preview` is one of them. Parameters
now share the field map, distinguished by sigil: `?name` query, `{name}` path,
`~name` header.

### Real drift the heuristics cannot solve

`--propose` runs the reasoner on the breaking signals. On the real removal of
`coupon` and `promotion_code` from `POST /v1/customers` it proposes dropping
both, because string distance cannot discover that Stripe moved that capability
into a `discounts` array. Getting from the signal to the right answer needs
semantic knowledge of the provider, which is exactly the gap `--reasoner claude`
exists to close.

Running against real specs also surfaced a gap in the core reasoner: it had a
handler for a field the *API rejected* but none for a field the *contract
dropped*, so a spec-visible removal produced no hypothesis at all. Fixed, and
covered by `TestReasonerOnRealShapes`.

### What is still missing for production

The sensors are real. The verification gate is not: adopting a fix requires the
sandbox check in `experiment.py`, and running that against a real provider needs
real sandbox credentials. `--propose` therefore prints hypotheses and labels them
UNVERIFIED, because nothing has tested them and nothing should adopt them. That
boundary is deliberate — the gate is what makes the loop safe, so it is better
to be visibly absent than quietly skipped.

## Layout

| file | role |
|---|---|
| `sell/environment.py` | the simulated partner: drifts, sandbox, staging, ledger |
| `sell/sensors.py` | detection, ordered by how early each signal fires |
| `sell/store.py` | environment model, trajectory log, growing regression suite |
| `sell/reasoner.py` | hypothesis generation — heuristic and Claude-backed |
| `sell/experiment.py` | the two gates, and the reversibility classifier |
| `sell/policy.py` | versioned transform pipeline with rollback |
| `sell/governor.py` | per-cluster autonomy ladder and error budget |
| `sell/agent.py` | the loop |
| `sell/real/openapi.py` | real OpenAPI 3 -> the same contract shape the sensors read |
| `sell/real/diff.py` | structural diff with breaking / additive / cosmetic verdicts |
| `sell/real/sources.py` | version discovery, fetch and cache for real specs |
| `sell/real/scan.py` | CLI: measure real drift between two shipped versions |

## Using Claude for hypothesis generation

The default reasoner pattern-matches failure shapes that were anticipated in
advance, which is exactly its limitation. `--reasoner claude` hands hypothesis
generation to the model, which handles drift the heuristics were never taught —
including deprecation notices written in prose nobody parsed for:

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python3 run_demo.py --reasoner claude
```

Its proposals go through the identical gates; nothing is trusted because a model
said it. Without credentials the agent falls back to heuristics and says so.

## What this does not do

- **It cannot learn a rule that exists only in someone's head.** Nothing here
  infers a new business policy that produces no observable signal. That is an
  information limit, not an engineering one, and no amount of model capability
  changes it.
- **It does not learn irreversible actions.** Anything classified IRREVERSIBLE
  is excluded from exploration by construction. Extending autonomy there needs a
  simulator, not a bigger error budget.
- **The environment is simulated.** The signals it emits are the ones a real
  integration emits, but a real one has to be *instrumented* to emit them, and
  that instrumentation is the larger share of the work in production. The loop is
  the easy part; the fuel line is not.
- **It does not reach zero human involvement**, and does not claim to. It makes
  each new change cheaper to absorb, and it turns the residue into one question
  the agent asks on its own initiative instead of days of supervision.
