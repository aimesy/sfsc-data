#!/usr/bin/env python3
"""Build Case Search facet indexes from the derived Parquet tables, so the
Advanced filter pickers can offer status / outcome / vexatious filters with
counts and instant results (the viewer is JSON-based and does not read Parquet).

Writes, for each dimension, a {label,count} facet + a normalized-label → case
numbers index (the same shape the JS facet builders emit):
    data/status-facet.json   / data/status-cases.json     (case_status.parquet)
    data/outcome-facet.json   / data/outcome-cases.json     (case_outcomes.parquet)
    data/vexatious-facet.json / data/vexatious-cases.json   (vexatious.parquet)

The normalize() here must stay in lockstep with normalizeLocation() in
index.html (these derived facet fields reuse it).
"""
import json
import re
import datetime
from pathlib import Path

import pyarrow.parquet as pq


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(s).lower())).strip()


NOW = datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_facet(out_facet, out_cases, list_key, groups, min_count=1):
    # groups: dict[label] -> iterable of case_numbers (deduped here)
    entries = []
    for label, cases in groups.items():
        cs = sorted({str(c).strip() for c in cases if str(c).strip()})
        if len(cs) >= min_count and norm(label):
            entries.append((str(label), cs))
    entries.sort(key=lambda e: (-len(e[1]), e[0]))
    facet = {
        "generated_at": NOW,
        "min_count": min_count,
        f"{list_key}_count": len(entries),
        list_key: [{"label": l, "count": len(cs)} for l, cs in entries],
    }
    cases = {
        "generated_at": NOW,
        "min_count": min_count,
        f"{list_key}_count": len(entries),
        "cases": {norm(l): ",".join(cs) for l, cs in entries},
    }
    Path(out_facet).parent.mkdir(parents=True, exist_ok=True)
    Path(out_facet).write_text(json.dumps(facet), encoding="utf-8")
    Path(out_cases).write_text(json.dumps(cases), encoding="utf-8")
    fkb = Path(out_facet).stat().st_size / 1024
    ckb = Path(out_cases).stat().st_size / 1024
    print(f"Wrote {out_facet}: {len(entries)} {list_key} ({fkb:.0f} KB); {out_cases} ({ckb:.0f} KB)")


def column(table, name):
    return table.column(name).to_pylist() if name in table.schema.names else [None] * table.num_rows


def build_status():
    t = pq.read_table("data/case_status.parquet", columns=["case_number", "case_status_label"])
    groups = {}
    for cn, lbl in zip(column(t, "case_number"), column(t, "case_status_label")):
        if not lbl or not cn:
            continue
        groups.setdefault(str(lbl), []).append(cn)
    write_facet("data/status-facet.json", "data/status-cases.json", "statuses", groups, min_count=1)


def build_outcomes():
    t = pq.read_table("data/case_outcomes.parquet", columns=["case_number", "signal"])
    groups = {}
    for cn, sig in zip(column(t, "case_number"), column(t, "signal")):
        if not sig or not cn:
            continue
        label = str(sig).replace("_", " ").strip()
        groups.setdefault(label, set()).add(cn)
    write_facet("data/outcome-facet.json", "data/outcome-cases.json", "outcomes", groups, min_count=2)


def build_vexatious():
    t = pq.read_table("data/vexatious.parquet")
    cns = column(t, "case_number")
    names = column(t, "litigant_name")
    groups = {}
    for cn, name in zip(cns, names):
        if not cn:
            continue
        label = str(name).strip() if name else "vexatious-litigant"
        groups.setdefault(label, set()).add(cn)
    write_facet("data/vexatious-facet.json", "data/vexatious-cases.json", "litigants", groups, min_count=1)


if __name__ == "__main__":
    build_status()
    build_outcomes()
    build_vexatious()
