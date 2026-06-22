#!/usr/bin/env node
// Build the department/courtroom facet for the Case Search location picker:
//   data/location-facet.json — courtrooms (calendar locations) + case counts
//   data/location-cases.json  — normalized location → case-numbers
//
// Locations live in each case's calendar[].location (e.g. "CIVIC CENTER
// COURTHOUSE ROOM 302"), so this is a build-time aggregate over
// archive/cases/*.json. A case is counted once per distinct location it touched.
//
// Usage: node scripts/index_locations_cases.mjs [--min-count 2]
import fs from 'node:fs';
import path from 'node:path';

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}
const CASES_DIR = arg('cases-dir', 'archive/cases');
const OUT = arg('out', 'data/location-facet.json');
const OUT_CASES = arg('out-cases', 'data/location-cases.json');
const MIN_COUNT = Math.max(1, Number(arg('min-count', '2')) || 2);

// Keep in lockstep with normalizeLocation() in index.html.
export function normalizeLocation(s) {
  return String(s == null ? '' : s)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function build() {
  const files = fs.readdirSync(CASES_DIR).filter((f) => f.endsWith('.json'));
  const groups = new Map(); // norm -> { count, variants: Map(rawLabel -> count), cases: [] }
  let scanned = 0;
  let withLocation = 0;
  for (const f of files) {
    let o;
    try { o = JSON.parse(fs.readFileSync(path.join(CASES_DIR, f), 'utf8')); } catch { continue; }
    scanned++;
    const cal = Array.isArray(o.calendar) ? o.calendar : [];
    if (!cal.length) continue;
    const caseNumber = String(o.case_number || f.replace(/\.json$/, '')).trim();
    const perCase = new Map();
    for (const c of cal) {
      const raw = c && (c.location || c.department || c.room);
      if (raw == null) continue;
      const label = String(raw).trim();
      if (!label) continue;
      const norm = normalizeLocation(label);
      if (!norm) continue;
      if (!perCase.has(norm)) perCase.set(norm, label);
    }
    if (perCase.size) withLocation++;
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
    source: 'archive/cases/*.json calendar[].location',
    scanned_cases: scanned,
    case_count: withLocation,
    min_count: MIN_COUNT,
    location_count: kept.length,
    distinct_locations: groups.size,
    locations: kept.map((j) => ({ label: j.label, count: j.count })),
  };
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(out));

  const casesIndex = {};
  for (const j of kept) casesIndex[j.norm] = j.cases.join(',');
  fs.mkdirSync(path.dirname(OUT_CASES), { recursive: true });
  fs.writeFileSync(OUT_CASES, JSON.stringify({ generated_at: out.generated_at, min_count: MIN_COUNT, location_count: kept.length, cases: casesIndex }));

  const kb = (p) => (fs.statSync(p).size / 1024).toFixed(0);
  console.log(`Wrote ${OUT}: ${kept.length} locations from ${withLocation}/${scanned} cases (${kb(OUT)} KB)`);
  console.log(`Wrote ${OUT_CASES}: location→cases index (${kb(OUT_CASES)} KB)`);
}

build();
