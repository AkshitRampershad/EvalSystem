/* PatchProof — browser engine.
 *
 * A port of the Python in sell/real/, not a mock of it. The distinction matters:
 * a demo that merely resembles the tool would be the exact failure this project
 * exists to catch, so tests/test_conformance.py runs the same inputs through both
 * implementations and fails if they disagree.
 *
 * Ported: contract extraction (openapi.py), the structural diff and its impact
 * verdicts (diff.py), request validation (validator.py), the heuristic proposals
 * (reasoner.py) and the gate with its capability check (gate.py).
 */
(function (root) {
  "use strict";

  // ---- difflib.SequenceMatcher.ratio, ported exactly ---------------------
  // Rename decisions turn on a 0.6 similarity cutoff, so an approximation here
  // would silently change which repairs get proposed.
  function matchingBlocks(a, b) {
    const b2j = new Map();
    for (let i = 0; i < b.length; i++) {
      const ch = b[i];
      if (!b2j.has(ch)) b2j.set(ch, []);
      b2j.get(ch).push(i);
    }
    function findLongest(alo, ahi, blo, bhi) {
      let besti = alo, bestj = blo, bestsize = 0;
      let j2len = new Map();
      for (let i = alo; i < ahi; i++) {
        const newj2len = new Map();
        for (const j of (b2j.get(a[i]) || [])) {
          if (j < blo) continue;
          if (j >= bhi) break;
          const k = (j2len.get(j - 1) || 0) + 1;
          newj2len.set(j, k);
          if (k > bestsize) { besti = i - k + 1; bestj = j - k + 1; bestsize = k; }
        }
        j2len = newj2len;
      }
      return [besti, bestj, bestsize];
    }
    const queue = [[0, a.length, 0, b.length]];
    const blocks = [];
    while (queue.length) {
      const [alo, ahi, blo, bhi] = queue.pop();
      const [i, j, k] = findLongest(alo, ahi, blo, bhi);
      if (k) {
        blocks.push([i, j, k]);
        if (alo < i && blo < j) queue.push([alo, i, blo, j]);
        if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
      }
    }
    return blocks;
  }

  function ratio(a, b) {
    if (!a.length && !b.length) return 1.0;
    let matches = 0;
    for (const [, , size] of matchingBlocks(a, b)) matches += size;
    return (2.0 * matches) / (a.length + b.length);
  }

  function getCloseMatches(word, possibilities, n, cutoff) {
    n = n || 3; cutoff = cutoff === undefined ? 0.6 : cutoff;
    const scored = [];
    for (const x of possibilities) {
      // difflib sets seq1 to the candidate and seq2 to the word, and ratio() is
      // NOT symmetric -- the matching-block search depends on which sequence is
      // which. Calling it the other way round picks different renames.
      const r = ratio(x, word);
      if (r >= cutoff) scored.push([r, x]);
    }
    // heapq.nlargest on (score, value) tuples: descending by score, then value.
    scored.sort((p, q) => (q[0] - p[0]) || (p[1] < q[1] ? 1 : p[1] > q[1] ? -1 : 0));
    return scored.slice(0, n).map(p => p[1]);
  }

  // ---- openapi.py --------------------------------------------------------
  const LOC_SIGIL = { query: "?", path: "{", header: "~", cookie: "&" };
  const FORM = "application/x-www-form-urlencoded";
  const JSON_CT = "application/json";
  const DESC_PREFIX = 140;

  function digest(text) {  // stand-in for the sha256 prefix; only equality matters
    let h1 = 0x811c9dc5, h2 = 0x01000193;
    for (let i = 0; i < text.length; i++) {
      h1 = (h1 ^ text.charCodeAt(i)) >>> 0; h1 = Math.imul(h1, 16777619) >>> 0;
      h2 = (h2 + text.charCodeAt(i) * (i + 1)) >>> 0;
    }
    return (h1.toString(16) + h2.toString(16)).slice(0, 12);
  }

  function resolveRef(schema, spec, depth) {
    depth = depth || 0;
    const ref = schema && schema.$ref;
    if (!ref || depth > 8 || ref.indexOf("#/") !== 0) return schema;
    let node = spec;
    for (const part of ref.slice(2).split("/")) {
      if (!node || typeof node !== "object" || !(part in node)) return schema;
      node = node[part];
    }
    const merged = {}; for (const k in schema) if (k !== "$ref") merged[k] = schema[k];
    return resolveRef(Object.assign({}, node, merged), spec, depth + 1);
  }

  function isUnsetSentinel(branch) {
    const e = branch.enum;
    if (!Array.isArray(e)) return false;
    const set = new Set(e);
    if (set.size === 1 && set.has("")) return true;
    return set.size === 2 && set.has("") && set.has(null);
  }

  function mergeAnyOf(schema, spec) {
    const branches = schema.anyOf || schema.oneOf;
    if (!branches) return schema;
    const real = [];
    for (let br of branches) {
      br = resolveRef(br, spec);
      if (isUnsetSentinel(br)) continue;
      real.push(br);
    }
    if (!real.length) return schema;
    const merged = {};
    for (const k in schema) if (k !== "anyOf" && k !== "oneOf") merged[k] = schema[k];
    const types = [], enums = [];
    for (const br of real) {
      if (br.type && types.indexOf(br.type) === -1) types.push(br.type);
      for (const v of (br.enum || [])) if (enums.indexOf(v) === -1) enums.push(v);
      for (const key of ["properties", "pattern", "maxLength", "items"])
        if (key in br && !(key in merged)) merged[key] = br[key];
    }
    if (types.length) merged.type = types.length === 1 ? types[0] : types.slice().sort().join("|");
    if (enums.length) merged.enum = enums;
    return merged;
  }

  function facts(schema, required, requiredIfPresent) {
    const desc = schema.description || "";
    const out = { type: schema.type || "unknown", required: !!required };
    if (requiredIfPresent && !required) out.required_if_present = true;
    if ("enum" in schema) out.allowed = schema.enum.slice();
    if ("pattern" in schema) out.pattern = schema.pattern;
    if ("maxLength" in schema) out.max_length = schema.maxLength;
    if (desc) { out.description = desc.slice(0, DESC_PREFIX); out.description_digest = digest(desc); }
    return out;
  }

  function walk(schema, spec, prefix, out, depth, maxDepth, ancestorRequired) {
    schema = mergeAnyOf(resolveRef(schema, spec), spec);
    const props = schema.properties || {};
    const requiredHere = new Set(schema.required || []);
    for (const name in props) {
      const child = mergeAnyOf(resolveRef(props[name], spec), spec);
      const path = prefix + name;
      const named = requiredHere.has(name);
      const isReq = ancestorRequired && named;
      out[path] = facts(child, isReq, named);
      if (child.properties && depth < maxDepth)
        walk(child, spec, path + ".", out, depth + 1, maxDepth, isReq);
    }
  }

  function parameters(operation, pathItem, spec) {
    const merged = new Map();
    const all = (pathItem.parameters || []).concat(operation.parameters || []);
    for (let raw of all) {
      const param = resolveRef(raw, spec);
      if (!param.name || !param.in) continue;
      merged.set(param.name + "\u0000" + param.in, param);
    }
    const out = {};
    for (const param of merged.values()) {
      const schema = mergeAnyOf(resolveRef(param.schema || {}, spec), spec);
      const required = !!param.required || param.in === "path";
      const withDesc = Object.assign({}, schema,
        { description: param.description || schema.description || "" });
      const f = facts(withDesc, required, false);
      f.location = param.in;
      const key = param.in === "path" ? "{" + param.name + "}"
        : (LOC_SIGIL[param.in] || "?") + param.name;
      out[key] = f;
    }
    return out;
  }

  function requestSchema(operation) {
    const content = (operation.requestBody || {}).content || {};
    for (const ct of [FORM, JSON_CT]) if (content[ct] && content[ct].schema) return content[ct].schema;
    for (const k in content) if (content[k].schema) return content[k].schema;
    return null;
  }

  function contract(spec, method, path, maxDepth) {
    maxDepth = maxDepth === undefined ? 2 : maxDepth;
    const pathItem = ((spec.paths || {})[path]) || {};
    const operation = pathItem[method.toLowerCase()];
    if (!operation) throw new Error(method.toUpperCase() + " " + path + " not in this spec");
    const fields = parameters(operation, pathItem, spec);
    const schema = requestSchema(operation);
    if (schema) walk(schema, spec, "", fields, 0, maxDepth, true);
    const sorted = {};
    for (const k of Object.keys(fields).sort()) sorted[k] = fields[k];
    return {
      version: (spec.info || {}).version || "unknown",
      operation: method.toUpperCase() + " " + path,
      fields: sorted
    };
  }

  // ---- diff.py -----------------------------------------------------------
  const BREAKING = "breaking", ADDITIVE = "additive", COSMETIC = "cosmetic";
  const COMPARED = ["type", "required", "required_if_present", "allowed", "pattern",
                    "max_length", "description_digest"];

  function verdict(kind, was, now) {
    if (kind === "field_removed") return BREAKING;
    if (kind === "field_added") return (now || {}).required ? BREAKING : ADDITIVE;
    if (kind === "type_changed") return BREAKING;
    if (kind === "required_changed" || kind === "required_if_present_changed")
      return now ? BREAKING : ADDITIVE;
    if (kind === "allowed_changed") {
      const lost = (was || []).filter(v => (now || []).indexOf(v) === -1);
      return lost.length ? BREAKING : ADDITIVE;
    }
    if (kind === "max_length_changed")
      return (now != null && was != null && now < was) ? BREAKING : ADDITIVE;
    if (kind === "pattern_changed") return BREAKING;
    if (kind === "description_changed") return COSMETIC;
    return ADDITIVE;
  }

  const same = (a, b) => JSON.stringify(a === undefined ? null : a) ===
                         JSON.stringify(b === undefined ? null : b);

  function diffContracts(oldC, newC) {
    const o = oldC.fields || {}, n = newC.fields || {}, signals = [];
    const emit = (kind, field, detail, was, now) => {
      detail = Object.assign({ field: field, operation: newC.operation || "" }, detail);
      detail.impact = verdict(kind, was, now);
      signals.push({ kind: kind, source: "spec", detail: detail });
    };
    for (const f of Object.keys(n).filter(k => !(k in o)).sort())
      emit("field_added", f, { spec: n[f] }, null, n[f]);
    for (const f of Object.keys(o).filter(k => !(k in n)).sort())
      emit("field_removed", f, { spec: o[f] }, o[f], null);
    for (const f of Object.keys(o).filter(k => k in n).sort()) {
      const a = o[f], b = n[f];
      for (const key of COMPARED) {
        if (same(a[key], b[key])) continue;
        const kind = key === "description_digest" ? "description_changed" : key + "_changed";
        const detail = { was: a[key] === undefined ? null : a[key],
                         now: b[key] === undefined ? null : b[key] };
        if (key === "description_digest") {
          detail.was_text = a.description || null; detail.now_text = b.description || null;
        }
        if (key === "allowed") {
          detail.added = (b.allowed || []).filter(v => (a.allowed || []).indexOf(v) === -1).sort();
          detail.removed = (a.allowed || []).filter(v => (b.allowed || []).indexOf(v) === -1).sort();
        }
        emit(kind, f, detail, a[key], b[key]);
      }
    }
    return signals;
  }

  // ---- validator.py ------------------------------------------------------
  const TYPE_CHECKS = {
    string: v => typeof v === "string", integer: v => Number.isInteger(v) && typeof v !== "boolean",
    number: v => typeof v === "number", boolean: v => typeof v === "boolean",
    array: v => Array.isArray(v), object: v => v && typeof v === "object" && !Array.isArray(v),
    unknown: () => true
  };

  function typeOk(value, declared) {
    for (const part of String(declared).split("|")) {
      if (!(part in TYPE_CHECKS)) return true;
      if (TYPE_CHECKS[part](value)) return true;
    }
    return false;
  }

  function parentPresent(keyed, field) {
    if (field.indexOf(".") === -1) return true;
    const parent = field.slice(0, field.lastIndexOf("."));
    if (parent in keyed) return true;
    return Object.keys(keyed).some(k => k.indexOf(parent + ".") === 0);
  }

  function validate(contractObj, keyed, allowUnknown) {
    const fields = contractObj.fields || {};
    if (!allowUnknown) {
      for (const name of Object.keys(keyed).sort()) {
        if (name in fields) continue;
        const parent = name.indexOf(".") > -1 ? name.slice(0, name.lastIndexOf(".")) : null;
        const pt = parent && fields[parent] && fields[parent].type;
        if (parent && (pt === "object" || pt === "unknown")) continue;
        return { code: "unknown_field", field: name,
                 message: "'" + name + "' is not in the published contract",
                 known_fields: Object.keys(fields).sort() };
      }
    }
    for (const name of Object.keys(fields).sort()) {
      const f = fields[name], supplied = name in keyed;
      if (!supplied) {
        if (f.required) return { code: "missing_required_field", field: name,
          message: "'" + name + "' is required", expected_type: f.type, allowed: f.allowed || null };
        if (f.required_if_present && parentPresent(keyed, name))
          return { code: "missing_required_field", field: name,
            message: "'" + name + "' is required because its parent object was supplied",
            expected_type: f.type, allowed: f.allowed || null };
        continue;
      }
      const v = keyed[name];
      if (v === null || v === undefined) continue;
      if (!typeOk(v, f.type || "unknown"))
        return { code: "type_mismatch", field: name,
          message: "'" + name + "' must be " + f.type, expected_type: f.type };
      if (f.allowed && f.allowed.indexOf(v) === -1)
        return { code: "value_not_allowed", field: name,
          message: JSON.stringify(v) + " is not an accepted value for '" + name + "'",
          allowed: f.allowed.slice() };
      if (f.pattern && typeof v === "string" && !(new RegExp(f.pattern).test(v)))
        return { code: "format_invalid", field: name,
          message: "'" + name + "' does not match " + f.pattern, pattern: f.pattern };
      if (f.max_length != null && typeof v === "string" && v.length > f.max_length)
        return { code: "too_long", field: name,
          message: "'" + name + "' exceeds maxLength " + f.max_length, max_length: f.max_length };
    }
    return null;
  }

  // ---- reasoner.py (heuristic tier) --------------------------------------
  const RENAME_SIMILARITY = 0.6;
  const renameTargets = names => names.filter(n => !(n.indexOf("{") === 0 && n.slice(-1) === "}"));
  const norm = s => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");

  // Python's repr(), so a patch description on the page is byte-identical to the
  // one the CLI prints. Conformance-tested rather than assumed.
  function pyRepr(v) {
    if (v === null || v === undefined) return "None";
    if (typeof v === "boolean") return v ? "True" : "False";
    if (typeof v === "number") return String(v);
    if (typeof v === "string")
      return "'" + v.replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
    if (Array.isArray(v)) return "[" + v.map(pyRepr).join(", ") + "]";
    return "{" + Object.keys(v).map(k => pyRepr(k) + ": " + pyRepr(v[k])).join(", ") + "}";
  }

  function describe(rule) {
    const a = rule.args;
    if (rule.op === "rename") return "rename " + a.from + " -> " + a.to;
    if (rule.op === "drop") return "drop " + a.field;
    if (rule.op === "set_const") return "set " + a.field + " = " + pyRepr(a.value);
    if (rule.op === "map_value") return "map " + a.field + " values " + pyRepr(a.mapping);
    if (rule.op === "suffix") return a.field + " += " + pyRepr(a.suffix);
    return rule.op;
  }
  const describePatch = p => (p.add.map(r => "+" + describe(r))
    .concat(p.remove.map(r => "-" + r))).join(", ") || "(no-op)";

  function propose(signals, ctx) {
    const patches = [];
    const push = (add, remove, rationale) => patches.push({ add: add || [], remove: remove || [], rationale: rationale || "" });
    const known = () => Object.keys(ctx.contract.fields || {});

    const removed = signals.filter(s => s.kind === "field_removed");
    const added = signals.filter(s => s.kind === "field_added");
    for (const r of removed) {
      const names = renameTargets(added.map(a => a.detail.field));
      const best = getCloseMatches(r.detail.field, names, 1, RENAME_SIMILARITY);
      if (best.length) push([{ op: "rename", args: { from: r.detail.field, to: best[0] } }], [],
        "'" + r.detail.field + "' appears to have become '" + best[0] + "'");
    }

    for (const s of signals) {
      const d = s.detail;
      if (s.kind === "field_removed") {
        const fld = d.field;
        const targets = renameTargets(known().filter(n => n !== fld));
        for (const t of getCloseMatches(fld, targets, 2, RENAME_SIMILARITY))
          push([{ op: "rename", args: { from: fld, to: t } }], [],
            "'" + fld + "' appears to have become '" + t + "'");
        push([{ op: "drop", args: { field: fld } }], [],
          "'" + fld + "' was removed from the contract");
      } else if (s.kind === "field_added" && (d.spec || {}).required) {
        const fld = d.field;
        if (ctx.canonical && fld in ctx.canonical)
          push([{ op: "set_const", args: { field: fld, value: ctx.canonical[fld] } }], [],
            "supply '" + fld + "' from the canonical record");
        const allowed = (d.spec || {}).allowed;
        if (allowed && allowed.length)
          push([{ op: "set_const", args: { field: fld, value: allowed[0] } }], [],
            "supply '" + fld + "' from the allowed set");
      } else if (s.kind === "allowed_changed" || s.kind === "value_not_allowed") {
        const fld = d.field;
        const newAllowed = d.allowed || d.now || [];
        const oldAllowed = d.was || [];
        const mapping = {};
        const candidates = oldAllowed.concat("got" in d ? [d.got] : []);
        for (const oldV of candidates) {
          let match = newAllowed.find(nv => norm(nv) === norm(oldV));
          if (match === undefined) {
            const near = getCloseMatches(norm(oldV), newAllowed.map(norm), 1, 0.6);
            if (near.length) match = newAllowed.find(nv => norm(nv) === near[0]);
          }
          if (match !== undefined && match !== oldV) mapping[oldV] = match;
        }
        if (Object.keys(mapping).length)
          push([{ op: "map_value", args: { field: fld, mapping: mapping } }], [],
            "remap '" + fld + "' onto the new value set");
      } else if (s.kind === "format_invalid" || s.kind === "pattern_changed") {
        const pattern = d.pattern || d.now || "";
        if (pattern.indexOf("T") > -1 && pattern.indexOf("Z") > -1)
          push([{ op: "suffix", args: { field: d.field, suffix: "T00:00:00Z" } }], [],
            "'" + d.field + "' now wants a full timestamp");
      }
    }
    // dedupe, mirroring HeuristicReasoner._dedupe
    const seen = new Set(), out = [];
    for (const p of patches) {
      const key = JSON.stringify([p.remove.slice().sort(),
        p.add.map(r => [r.op, JSON.stringify(r.args)]).sort()]);
      if (seen.has(key)) continue;
      seen.add(key); out.push(p);
    }
    return out;
  }

  // ---- policy render -----------------------------------------------------
  function applyRule(rec, rule) {
    const out = Object.assign({}, rec), a = rule.args;
    if (rule.op === "rename") { if (a.from in out) { out[a.to] = out[a.from]; delete out[a.from]; } }
    else if (rule.op === "drop") delete out[a.field];
    else if (rule.op === "set_const") out[a.field] = a.value;
    else if (rule.op === "map_value") { if (a.field in out && a.mapping[out[a.field]] !== undefined) out[a.field] = a.mapping[out[a.field]]; }
    else if (rule.op === "suffix") { const v = out[a.field]; if (typeof v === "string" && v.slice(-a.suffix.length) !== a.suffix) out[a.field] = v + a.suffix; }
    else if (rule.op === "divide_int") { if (Number.isInteger(out[a.field])) out[a.field] = Math.floor(out[a.field] / a.by); }
    else if (rule.op === "multiply") { if (Number.isInteger(out[a.field])) out[a.field] = out[a.field] * a.by; }
    return out;
  }
  const render = (rec, patch) => (patch ? patch.add : []).reduce(applyRule, Object.assign({}, rec));

  // ---- gate.py -----------------------------------------------------------
  const REJECTED = "rejected", NEEDS_HUMAN = "needs_human", SCHEMA = "schema";
  const EMPTY = v => v === null || v === undefined || v === "" ||
    (Array.isArray(v) && !v.length) || (v && typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length);

  function scalars(value, out, depth) {
    depth = depth || 0; if (depth > 6) return;
    if (value && typeof value === "object" && !Array.isArray(value))
      for (const k in value) scalars(value[k], out, depth + 1);
    else if (Array.isArray(value)) for (const v of value) scalars(v, out, depth + 1);
    else if (!EMPTY(value)) out.add(JSON.stringify(value));
  }

  function lostCapability(before, after) {
    const surviving = new Set();
    for (const k in after) scalars(after[k], surviving);
    const gone = [];
    for (const key in before) {
      const value = before[key];
      if (EMPTY(value)) continue;
      const wanted = new Set(); scalars(value, wanted);
      if (!EMPTY(after[key])) {
        const held = new Set(); scalars(after[key], held);
        const inter = [...wanted].filter(v => held.has(v));
        if (inter.length && ![...wanted].every(v => held.has(v))) gone.push(key);
        continue;
      }
      if (wanted.size && ![...wanted].every(v => surviving.has(v))) gone.push(key);
    }
    return gone.sort();
  }

  function capabilityQuestion(fields, contractObj) {
    const names = fields.map(f => "'" + f + "'").join(", ");
    return names + " no longer appears in the request and its value is not carried " +
      "by any other field. The published contract does not say where it moved. Was " +
      "this capability removed, or does it now go somewhere else in " +
      (contractObj.operation || "this operation") + "?";
  }

  function stuckQuestion(rejected, contractObj, payload) {
    const counts = {};
    for (const [, why] of rejected) {
      if (why.indexOf("'") === -1) continue;
      const name = why.split("'")[1];
      counts[name] = (counts[name] || 0) + 1;
    }
    const names = Object.keys(counts);
    if (names.length !== 1) return null;
    const name = names[0];
    const f = (contractObj.fields || {})[name];
    const op = contractObj.operation || "this operation";
    if (!f) return "'" + name + "' is no longer part of " + op +
      " and nothing tried produced a valid request. Where should its value go?";
    if (f.allowed && f.allowed.length)
      return "'" + name + "' was sent as " + pyRepr(payload[name]) + ", which " + op +
        " no longer accepts. The contract now allows " + pyRepr(f.allowed) +
        ", and none of them resembles the old value closely enough to map safely. Which of " +
        "them corresponds to " + pyRepr(payload[name]) + "?";
    return "No valid request could be produced for '" + name + "' in " + op +
      ". The contract now expects type " + pyRepr(f.type) +
      (f.max_length != null ? " with maxLength " + f.max_length : "") +
      "; we are sending " + pyRepr(payload[name]) + ". How should it be converted?";
  }

  function evaluate(newContract, candidates, payload) {
    const before = payload, rejected = [];
    let escalation = null, checks = 0;
    for (const patch of candidates) {
      const after = render(payload, patch);
      checks += 1;
      const err = validate(newContract, after);
      if (err) { rejected.push([describePatch(patch), "schema: " + err.code + " on '" + err.field + "'"]); continue; }
      const lost = lostCapability(before, after);
      if (lost.length) {
        rejected.push([describePatch(patch), "would stop expressing " + lost.join(", ")]);
        if (!escalation) escalation = { patch: patch, tier: NEEDS_HUMAN, lost: lost,
          question: capabilityQuestion(lost, newContract), checks: checks, after: after };
        continue;
      }
      return { patch: patch, tier: SCHEMA, rejected: rejected, checks: checks,
               after: after, adoptable: true, question: null, lost: [] };
    }
    if (escalation) { escalation.rejected = rejected; escalation.adoptable = false; return escalation; }
    const stuck = rejected.length ? stuckQuestion(rejected, newContract, before) : null;
    return { patch: null, tier: stuck ? NEEDS_HUMAN : REJECTED, rejected: rejected,
             question: stuck, checks: checks, adoptable: false, lost: [], after: null };
  }

  // ---- one-call pipeline for the page ------------------------------------
  function analyse(oldSpec, newSpec, method, path, payload) {
    const oldC = contract(oldSpec, method, path);
    const newC = contract(newSpec, method, path);
    const signals = diffContracts(oldC, newC);
    if (!payload) {
      payload = {};
      for (const name in oldC.fields) {
        const f = oldC.fields[name];
        if (f.location === "path" || f.required || signals.some(s => s.detail.field === name)) {
          payload[name] = f.allowed && f.allowed.length ? f.allowed.find(v => !EMPTY(v))
            : f.type && f.type.indexOf("integer") === 0 ? 424242
            : f.type === "boolean" ? true
            : f.type === "array" ? ["CAP-" + name] : "CAP-" + name;
        }
      }
    }
    const candidates = propose(signals, { contract: newC, canonical: payload });
    const preflight = validate(newC, payload);
    const result = evaluate(newC, candidates, payload);
    return { oldContract: oldC, newContract: newC, signals: signals, payload: payload,
             preflight: preflight, candidates: candidates, result: result,
             describePatch: describePatch };
  }

  const api = { contract, pyRepr, diffContracts, validate, propose, evaluate, analyse, render,
                lostCapability, ratio, getCloseMatches, describePatch,
                BREAKING, ADDITIVE, COSMETIC, SCHEMA, NEEDS_HUMAN, REJECTED };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PatchProof = api;
})(typeof self !== "undefined" ? self : this);
