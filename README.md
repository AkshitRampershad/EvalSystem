# PatchProof

**PatchProof detects API contract drift, generates candidate repairs, and verifies that autonomous changes preserve existing capabilities before they are applied.**

This is three things: an agent that repairs a broken integration by itself, a
gate that refuses fixes which only *look* correct, and a benchmark built from 71
real breaking changes in Stripe's published history that says how well it
actually works.

The short version of the result: the gate matters more than the agent. The
obvious repair — stop sending whatever broke — produces a valid request and
quietly loses money in **71 out of 71** real cases. Behind the gate, **zero** get
through.

```bash
git clone https://github.com/AkshitRampershad/PatchProof && cd PatchProof
python3 run_demo.py                                # watch it adapt, no API key needed
python3 -m bench.run                               # score it against real drift
python3 -m unittest discover -s tests -t .         # 150 tests
```

| | |
|---|---|
| **Proven** | Drift detection against Stripe's real published contracts, across 2,506 versions. The gate eliminating every unsafe fix, measured. A benchmark that scores any solver on 71 real breaking changes. |
| **Not proven** | The end-to-end adaptation loop runs against a simulator, not a live provider. Verification against a real provider sandbox is built but needs credentials and has never been run. The best model score comes from a run in which the model answered 68 of 71 cases. |

---

## The problem

You have software that talks to somebody else's API — a payment processor, a
CRM, a data feed. One day they rename a field, tighten a validation rule, or
change what a number means. Your integration breaks.

Someone has to notice, work out what changed, and fix it. That is unglamorous,
interrupt-driven work, and it never stops, because the other end never stops
changing.

The bad case isn't the one that breaks loudly. It's the one where your request is
still perfectly valid and quietly does the wrong thing — the field you were
sending is simply gone, so nothing errors, nothing alerts, and nobody finds out
until the numbers don't add up.

## How this is handled today

Mostly well enough, and that is worth being honest about. Contract tests catch
some changes. Monitoring catches more. A provider with good discipline announces
breaking changes in advance, and Stripe — measured here — ships almost none.

What the status quo does not solve is the silent class. Every existing safeguard
answers "did the request fail?" When the answer is no and the behaviour is wrong
anyway, nothing in the usual toolkit fires.

## What this does differently

The agent watches the contract, notices a change, and forms a hypothesis about
the new correct behaviour. That part is unremarkable and several tools do it.

The part that matters is what happens next: **a hypothesis is not allowed to
become behaviour until something outside the agent confirms it.** Three tiers,
with deliberately different authority:

1. **The provider's own published contract** accepts the request. Free, offline,
   runs on every candidate — and proves only that the provider will not reject
   it. It says nothing about meaning.
2. **The provider's sandbox** accepted it for real, catching undocumented rules
   the contract never expressed.
3. **What actually happened** downstream, days later. Not available in advance at
   all. Every claim about semantic correctness belongs here and nowhere else.

Between tiers one and two sits the check the whole project exists for. When the
agent cannot work out where a value went, the easiest fix that passes every
check is to **stop sending it**. Valid request, silent loss. So the gate compares
what the request used to accomplish against what it accomplishes now, and refuses.

Then it does the thing that makes autonomy tolerable: **it asks one specific
question instead of guessing.**

> `'coupon' no longer appears in the request and its value is not carried by any
> other field. The published contract does not say where it moved. Was this
> capability removed, or does it now go somewhere else in POST /v1/customers?`

Guessing is treated as a failure, not a partial success. A value written into an
unrelated field validates perfectly and is worse than no change at all, because
nothing downstream will ever complain.

## What's real and what isn't

The section most worth checking, so it is stated plainly rather than implied.

**Real.** The drift sensors read OpenAPI contracts a provider actually published.
Stripe keeps 2,506 tagged versions in public git, so the changes measured here
are ones that really happened to real callers. Over three years: 589 operations,
~6,500 contract fields, 1,716 field-level changes.

**Real.** The benchmark's 71 cases are mined from that history and labelled
without human annotation. Two properties are enforced when mining and re-checked
by the test suite: each request must be valid before the change and broken after
it. A benchmark whose data rots keeps printing numbers while measuring nothing.

**Simulated.** The full sense → hypothesise → verify → act → recover loop runs
against a partner API written for the purpose. That simulator is what lets the
delayed-signal path be exercised end to end; a real provider's ledger does not
disagree with you on demand.

**Built, never run.** Tier 2 — verification against a live provider sandbox —
needs test-mode credentials. The HTTP path is covered by tests with an injected
transport. It has not been pointed at Stripe.

**Near-complete.** The model-backed row below comes from a run where the model
answered 68 of 71 cases; three fell back to the heuristic under free-tier rate
limiting. Because the heuristic is the safer solver, those three make that row
look slightly better than it is.

## What the numbers say

Every row measured on the same 71 real cases. Two rates, reported separately
because they are trivially traded against each other: `adapt` is being correct
where the contract holds the answer, `ask` is correctly asking where it does not.

```
solver                          adapt     ask  UNSAFE  safety
noop                             0/27    0/44       0    100%
escalate-always                  0/27   44/44       0    100%
drop-ungated                     0/27    0/44      71      0%
drop-gated                       0/27   44/44       0    100%
agent-heuristic                  7/27   35/44       2     97%
agent-groq:openai/gpt-oss-120b  13/27   38/44       7     90%   * 68/71 answered
```

**Read `UNSAFE` first.** It counts fixes that were adopted and are invalid,
silently dropped a capability, or guessed where no answer existed. Every other
outcome leaves a human able to reason about the situation. An unsafe one looks
like success.

**The gate is the most valuable component, by a wide margin.** `drop-ungated` is
the obvious fix with no verification: unsafe on 71 of 71. The identical solver
behind the gate: zero. Nothing else here moves a number that far.

**A model nearly doubles adaptation, and costs more safety than it buys.**
7/27 → 13/27, with gains exactly where pattern-matching is blind. It also took
unsafe adoptions from 2 to 7 — three on renames, two on narrowed enums:
plausible destinations that validate and are wrong. That is a gate problem, not
a model problem, and it is the next thing to fix.

**Most real drift is not locally fixable at all.** 44 of the 71 cases carry no
evidence anywhere of where the capability went. Any pitch resting on
fully-unattended repair has to account for that: the realistic ceiling on this
provider is about 38%, and the rest is an agent asking one good question instead
of a human reading a changelog.

**And the least flattering finding came from auditing the benchmark, not the
agent.** An earlier version of the scorer reported 42% solved. Reading the
per-case output showed what it rewarded: `rename coupon -> phone`,
`rename promotion_code -> address.postal_code`, and `rename coupon -> {customer}`
— which writes a coupon code into a path parameter and addresses an entirely
different resource. All three validated. All three preserved the value. All three
scored as successes. A benchmark that flatters the system it measures is worse
than none, because it retires the question.

## Glossary

Terms as this codebase uses them, not in general.

- **Drift** — a change to the contract of a service you depend on, made without
  coordinating with you.
- **Contract** — the machine-readable description of what a request may contain:
  fields, types, required-ness, allowed values. Here, extracted from OpenAPI.
- **Policy** — the agent's learned behaviour, stored as an ordered list of named
  transforms rather than model weights, so every rule is readable, revertible and
  traceable to the signal that justified it.
- **The gate** — the tiered verification that a proposed fix must survive before
  it becomes behaviour.
- **Capability loss** — the request still validates but has stopped doing
  something it used to do. The failure this project is organised around.
- **Escalation** — the agent declining to guess, with one specific question.
  Scored as the correct answer wherever the contract holds no answer.
- **Ledger signal** — ground truth that arrives long after the decision, from the
  provider's own records. The only tier that can catch a silent error.

---

<details>
<summary><b>Design decisions, and why each one is that way</b></summary>

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

</details>

<details>
<summary><b>Sensors against a real API — what real drift measures like</b></summary>

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

</details>

<details>
<summary><b>The benchmark — how cases are mined, labelled and scored</b></summary>

`bench/` is what makes the claims here falsifiable. It mines drift events out of
a provider's real version history, labels them without human annotation, and
scores any solver against them.

```bash
python3 -m bench.run                              # the whole ladder
python3 -m bench.run --solver agent --detail      # per-case results
python3 -m bench.run --solver agent --only unsafe # just the failures
python3 -m bench.run --mine                       # re-mine from history
```

71 cases across nine windows of Stripe's published history, Nov 2022 to Aug 2026.
Each case is one real breaking change plus a request that worked before it. Two
properties are enforced at mining time and re-asserted by the test suite, because
a benchmark whose data rots keeps printing numbers while measuring nothing: the
request must be **valid under the old contract**, and must be **invalid under the
new one**.

### Labels come from structure, not opinion

Each case carries an expected outcome derived from the shape of the change:

- **adapt** (27 cases) — the new contract contains enough to fix this locally: a
  renamed field with an evident successor, a newly required field, an enum value
  with a clear counterpart.
- **ask** (44 cases) — it does not. A field removed with no successor anywhere,
  or a value set replaced wholesale. Here *guessing is the failure*: a value
  written into an unrelated field validates perfectly and is worse than no change
  at all, because nothing downstream will ever complain.

Those two rates are reported separately, because they are trivially tradeable —
a solver that never adapts scores 100% on the second.

### Results

```
solver                          adapt     ask  UNSAFE  safety
noop                             0/27    0/44       0    100%
escalate-always                  0/27   44/44       0    100%
drop-ungated                     0/27    0/44      71      0%
drop-gated                       0/27   44/44       0    100%
agent-heuristic-ungated          3/27    0/44      58     18%
agent-heuristic                  7/27   35/44       2     97%
agent-groq:openai/gpt-oss-120b  13/27   38/44       7     90%   * 68/71 answered
```

`*` — the model answered 68 of the 71 cases; three fell back to the heuristic
after free-tier rate limiting, across 144 waits. Since the heuristic is the
safer solver, those three flatter this row: **7 unsafe is a floor**. An earlier,
much dirtier run (39 of 71 answered) reported 14/27 and 5 unsafe; cutting the
prompt by 56% (see below) is what made a near-complete run fit.

Read `UNSAFE` first. It counts cases where a solver adopted a change that is
invalid, silently dropped a capability, or guessed where the contract held no
answer. Every other outcome leaves the integration in a state a human can reason
about; an unsafe one does not, and looks like success.

Four things this says, including two that are unflattering:

**The gate is the most valuable component by a wide margin.** The naive fix —
stop sending whatever broke — is unsafe on **71 of 71** cases. Behind the gate,
the identical solver is unsafe on zero. The same holds for the real agent: 60
unsafe ungated, 2 gated. Nothing else in this repository moves a number that far.

**The heuristic reasoner barely earns its place.** It adapts correctly on 7 of 27
solvable cases, and it asks correctly less often than a solver that does nothing
but ask (35/44 against 44/44). Its entire contribution over the trivial floor is
7 adaptations — real, but a long way from a system that maintains an integration
by itself.

**A model-backed reasoner nearly doubles adaptation, and costs more safety than
it buys.** Answering 68 of the 71 cases, `gpt-oss-120b` reached 13/27, with gains
concentrated exactly where the heuristic is blind: `rename` 5→10, `type_changed`
0→1, `length_tightened` 0→1, `enum_narrowed` 1→2. It also took unsafe adoptions
from 2 to 7 — three more on `rename`, two on `enum_narrowed` — plausible
destinations and plausible value mappings that validate and are wrong. That is
the predictable cost of a more confident proposer, and it is a gate problem
rather than a reasoner problem: a rename destination currently needs only to
satisfy the schema, when it should have to clear a stronger bar. That is the next
fix, and the benchmark is what will show whether it works.

It is worth stating the trade plainly rather than burying it. Six more cases
adapted, five more left in a state nobody will notice. On these 71 cases the
model tier is not yet worth switching on by default, which is why the page
leads with the gate and not with the model.

**The prompt had to shrink before it could be measured at all.** The first attempt
at a full run died on a free tier's 200,000 tokens-per-day limit, because sending
the whole contract cost about 2,750 tokens per case — 195,000 for one run, before
the system prompt and output. Nearly all of it was fields and signal detail with no
bearing on the decision. Sending full detail only for relevant fields, bare names
for the rest, and dropping signal payloads that duplicate the contract cut it to
87,000 (56% less), which fits. Two tests keep it there: one asserts a full run stays
under budget, another that no trimming ever hides a field a correct answer needs.

**Most real drift is not locally solvable.** 44 of 71 cases carry no evidence of
where a capability went. Any pitch resting on an agent that fixes drift
unattended has to account for that: the realistic ceiling for full autonomy on
this provider is about 38%, and the rest is an agent asking one good question
instead of a human reading a changelog.

**The unflattering part was found by auditing the benchmark, not the agent.** An
earlier version of the scorer reported 42% solved. Reading the per-case output
showed what it was rewarding: `rename coupon -> phone`, `rename promotion_code ->
address.postal_code`, and `rename coupon -> {customer}`, which writes a coupon
code into a **path parameter** and addresses a different resource entirely. All
three validated and all three preserved the value, so all three scored as
successes. The fixes were a stricter rename threshold (0.3 similarity was
coincidence, not evidence), excluding path parameters as rename destinations, and
per-case expected outcomes so that guessing is scored as the failure it is.

A benchmark that flatters the system it measures is worse than none, because it
retires the question.

### Adding a solver

Implement `solve(case, old_contract, new_contract) -> Attempt` and register it in
`bench/solvers.py`. The baselines are there to be beaten: any solver that does
not beat `escalate-always` on the `adapt` column has contributed nothing, however
good its overall percentage looks.

</details>

<details>
<summary><b>Model tiers, credentials and token budget</b></summary>

The reasoner is the one pluggable component, because the gate does not care who
produced a hypothesis — only whether it survives verification. Every tier shares
the same prompt, the same op allow-list and the same parsing in
`sell/hypotheses.py`, so their benchmark rows are comparable, and a model's
response is treated as untrusted input on every one of them.

| tier | endpoint | credential |
|---|---|---|
| `heuristic` | none — offline pattern matching | none (the default) |
| `claude` | Anthropic Messages API | `ANTHROPIC_API_KEY` |
| `groq` | Groq, OpenAI-compatible | `GROQ_API_KEY` |
| `together`, `openrouter` | same shape | `TOGETHER_API_KEY`, `OPENROUTER_API_KEY` |
| `local` | a local vLLM or Ollama server | none by default |

```bash
export GROQ_API_KEY=...            # never paste a key into a chat or a commit
export GROQ_MODEL=openai/gpt-oss-120b   # optional; each tier has a default
python3 -m bench.run --solver agent --solver groq
python3 run_demo.py --reasoner groq
```

No vendor SDK is needed for the OpenAI-compatible tiers — they use stdlib HTTP,
so the project stays dependency-free. Only the `claude` tier needs
`pip install -r requirements.txt`.

Structured-output support varies by endpoint and model, so a request degrades in
three steps: `json_schema`, then `json_object`, then a plain instruction with
tolerant extraction. A model that cannot honour a schema should not have a
formatting limitation recorded as a reasoning failure.

If a tier's credential is missing, the run says so and its hypotheses come from
the heuristic fallback — a row is never allowed to read as a measurement of a
model that was never called.

### Note on network egress

Reaching a model endpoint requires that the environment permit it. In a sandbox
with a restrictive egress policy the call fails at the socket, which surfaces as
`last_error` and a fall back to heuristics rather than a crash — but it also means
the tier cannot be measured there. `api.groq.com` is denied by the policy of the
environment this was developed in, so **the Groq row has not been measured**; the
code path is covered by tests with an injected transport, and the number needs a
run somewhere the endpoint is reachable.

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

</details>

<details>
<summary><b>Code layout</b></summary>

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
| `sell/real/validator.py` | request validation against a provider's own contract |
| `sell/real/gate.py` | the three-tier verification gate and the capability check |
| `bench/mine.py` | mine labelled drift cases from real version history |
| `bench/solvers.py` | the agent under test, plus baselines built to fail |
| `bench/score.py` | four outcomes; only one of them is a real failure |
| `bench/run.py` | CLI: run the ladder and print the table |
| `data/stripe-drift-v1.json.gz` | 71 committed cases, so runs are comparable |

</details>

## Limits

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
