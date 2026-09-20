/* Runs the browser engine over cases exported by the Python side and prints the
   result as JSON, so test_conformance.py can compare the two implementations. */
const fs = require("fs");
const pp = require("../docs/playground.js");

const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = {};

for (const [id, c] of Object.entries(input.pipeline || {})) {
  const signals = pp.diffContracts(c.old, c.new);
  const candidates = pp.propose(signals, { contract: c.new, canonical: c.payload });
  const verdict = pp.evaluate(c.new, candidates, c.payload);
  out[id] = {
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

const ratios = (input.ratios || []).map(([a, b]) => [a, b, Number(pp.ratio(a, b).toFixed(10))]);

process.stdout.write(JSON.stringify({ pipeline: out, extraction, ratios }));
