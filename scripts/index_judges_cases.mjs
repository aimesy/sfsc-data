#!/usr/bin/env node
// Build the judge/officer facet for the Case Search judge picker:
//   data/judge-facet.json — judges (calendar officers) + per-judge case counts
//   data/judge-cases.json  — normalized judge → case-numbers (for instant render)
//
// Judges live in each case's calendar[].judge, so this is a build-time aggregate
// over archive/cases/*.json. A case is counted once per distinct judge that ever
// sat on it. Near-identical spellings (case, punctuation, and the honorifics
// Hon./Judge/Justice/Commissioner/pro tem) fold onto one normalized key; the
// most common raw spelling becomes the canonical label.
//
// Usage: node scripts/index_judges_cases.mjs [--min-count 2]
import fs from 'node:fs';
import path from 'node:path';

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}
const CASES_DIR = arg('cases-dir', 'archive/cases');
const OUT = arg('out', 'data/judge-facet.json');
const OUT_CASES = arg('out-cases', 'data/judge-cases.json');
const MIN_COUNT = Math.max(1, Number(arg('min-count', '2')) || 2);

// Keep in lockstep with normalizeJudge() in index.html.
export function normalizeJudge(s) {
  return String(s == null ? '' : s)
    .toLowerCase()
    .replace(/\b(?:hon|honorable|judge|justice|commissioner|comm|pro\s*tem|dept|department)\b\.?/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function build() {
  const files = fs.readdirSync(CASES_DIR).filter((f) => f.endsWith('.json'));
  const groups = new Map(); // norm -> { count, variants: Map(rawLabel -> count), cases: [] }
  let scanned = 0;
  let withJudge = 0;
  for (const f of files) {
    let o;
    try { o = JSON.parse(fs.readFileSync(path.join(CASES_DIR, f), 'utf8')); } catch { continue; }
    scanned++;
    const cal = Array.isArray(o.calendar) ? o.calendar : [];
    if (!cal.length) continue;
    const caseNumber = String(o.case_number || f.replace(/\.json$/, '')).trim();
    // distinct judges on this case, keyed by norm → best raw label seen
    const perCase = new Map();
    for (const c of cal) {
      const raw = c && (c.judge || c.judicial_officer || c.officer);
      if (raw == null) continue;
      const label = String(raw).trim();
      if (!label) continue;
      const norm = normalizeJudge(label);
      if (!norm) continue;
      if (!perCase.has(norm)) perCase.set(norm, label);
    }
    if (perCase.size) withJudge++;
    for (const [norm, label] of perCase) {
      let g = groups.get(norm);
      if (!g) { g = { count: 0, variants: new Map(), cases: [] }; groups.set(norm, g); }
      g.count++;
      g.variants.set(label, (g.variants.get(label) || 0) + 1);
      if (caseNumber) g.cases.push(caseNumber);
    }
  }
  const kept = [...groups.entries()]
    .filter(([, g]) => g.count >= MIN_COUNT)
    .map(([norm, g]) => {
      const canonical = [...g.variants.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0];
      return { norm, label: canonical, count: g.count, cases: g.cases };
    })
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));

  const out = {
    generated_at: new Date().toISOString(),
    source: 'archive/cases/*.json calendar[].judge',
    scanned_cases: scanned,
    case_count: withJudge,
    min_count: MIN_COUNT,
    judge_count: kept.length,
    distinct_judges: groups.size,
    judges: kept.map((j) => ({ label: j.label, count: j.count })),
  };
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(out));

  const casesIndex = {};
  for (const j of kept) casesIndex[j.norm] = j.cases.join(',');
  fs.mkdirSync(path.dirname(OUT_CASES), { recursive: true });
  fs.writeFileSync(OUT_CASES, JSON.stringify({ generated_at: out.generated_at, min_count: MIN_COUNT, judge_count: kept.length, cases: casesIndex }));

  const kb = (p) => (fs.statSync(p).size / 1024).toFixed(0);
  console.log(`Wrote ${OUT}: ${kept.length} judges from ${withJudge}/${scanned} cases (${kb(OUT)} KB)`);
  console.log(`Wrote ${OUT_CASES}: judge→cases index (${kb(OUT_CASES)} KB)`);
}

build();
