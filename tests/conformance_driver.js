/* Runs the browser engine over cases exported by the Python side and prints the
   result as JSON, so test_conformance.py can compare the two implementations. */
const fs = require("fs");
const pp = require("../docs/playground.js");

const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = {};
const slim = o => ({ result: o.result, detail: o.detail, lost: o.lost });

for (const [id, c] of Object.entries(input.pipeline || {})) {
  const signals = pp.diffContracts(c.old, c.new);
  const candidates = pp.propose(signals, { contract: c.new, canonical: c.payload });
  const verdict = pp.evaluate(c.new, candidates, c.payload);
  const u = pp.ungated(candidates, c.payload, c.new);
  out[id] = {
    // What ships with no gate, as the page shows it beside the gated verdict.
    ungated: { patch: u.patch ? pp.describePatch(u.patch) : null,
               outcome: u.outcome, lost: u.lost },
    // Both rows of the published table, scored the way bench/score.py does.
    gatedOutcome: slim(pp.judge(c.case, c.new, {
      adopted: verdict.adoptable ? verdict.patch : null,
      question: verdict.question, rejected: verdict.rejected })),
    ungatedOutcome: slim(pp.judge(c.case, c.new, {
      adopted: candidates.length ? candidates[0] : null,
      question: null, rejected: [] })),
    signals: signals.map(s => [s.kind, s.detail.field, s.detail.impact]),
    candidates: candidates.map(p => pp.describePatch(p)),
    tier: verdict.tier,
    question: verdict.question,
    lost: verdict.lost,
    // An escalation still carries the offending patch, for the question. It was
    // not adopted, so report it the way the Python side does.
    adopted: verdict.adoptable ? pp.describePatch(verdict.patch) : null,
  };
}

const extraction = {};
for (const [id, e] of Object.entries(input.extraction || {})) {
  extraction[id] = pp.contract(e.spec, e.method, e.path, e.max_depth);
}

let scan = null;
if (input.scan) {
  const oldCs = pp.contracts(input.scan.old), newCs = pp.contracts(input.scan.new);
  const all = pp.diffAll(oldCs, newCs);
  scan = {
    operations_old: pp.operations(input.scan.old),
    operations_new: pp.operations(input.scan.new),
    contracts_old: oldCs, contracts_new: newCs,
    drifts: all.drifts.map(d => ({
      operation: d.operation,
      signals: d.signals.map(s => [s.kind, s.detail.field, s.detail.impact]),
      breaking: d.breaking.length, additive: d.additive.length,
      cosmetic: d.cosmetic.length })),
    added: all.added, removed: all.removed,
    summary: pp.summarise(all.drifts),
  };
}

const ratios = (input.ratios || []).map(([a, b]) => [a, b, Number(pp.ratio(a, b).toFixed(10))]);

process.stdout.write(JSON.stringify({ pipeline: out, extraction, ratios, scan }));
