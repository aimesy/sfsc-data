#!/usr/bin/env python3
"""Classify each captured case's terminal status from its ROA + write the
satisfied-judgment skip list the bulk capture consults.

Reads archive/cases/<case>.json (captured per-case records) and scans the
docket_entries descriptions for terminal-disposition signals. The KEY output for
bulk capture is the JUDGMENT-SATISFIED set: a satisfied judgment is terminal (the
docket will not grow), so bulk re-scans skip it BY DEFAULT. Settled / dismissed
cases are deliberately NOT in the skip set — they can still draw fee motions or
appeals, so they remain worth re-scanning.

Outputs:
  data/case_status.parquet — case_number, case_status, satisfied, settled,
    dismissed, judgment_entered, n_entries, signals, classified_at
  data/satisfied_cases.txt — one normalized case number per line (the bulk skip
    list the Android app fetches via GitHubClient.fetchFileText)
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import tempfile
from collections import Counter

import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_text_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_parquet_atomic(path: str, df: "pd.DataFrame") -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".parquet", dir=directory)
    os.close(fd)
    try:
        df.to_parquet(tmp, index=False)
        if os.path.getsize(tmp) <= 0:
            raise RuntimeError(f"refusing to replace {path}: temporary parquet is empty")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

# Terminal-disposition signals (matched against ROA entry descriptions, upper).
# Keep these as auditable constants rather than inline expressions: only a full
# satisfaction entry can place a case on the skip list. Settlement, judgment,
# and dismissal patterns are descriptive status flags for researchers and must
# not become capture-stop conditions.
RE_SATISFIED = re.compile(r"\bSATISFACTION OF JUDGMENT\b")
RE_PARTIAL   = re.compile(r"\bPARTIAL SATISFACTION\b")
# Entries that MENTION satisfaction but do NOT establish it — a denied/vacated/
# opposed/withdrawn satisfaction (or a motion to set one aside) leaves the case in
# the active post-judgment phase. Such an entry must NOT mark the case satisfied,
# or bulk capture would wrongly skip a still-live case. Errs toward re-scanning.
RE_SATISFIED_NEG = re.compile(
    r"\b(?:DENY|DENIED|DENYING|VACAT\w*|OPPOS\w*|WITHDRAW\w*|SET ASIDE|OBJECTION\w*|"
    r"MOTION TO|REVOK\w*|RESCIND\w*)\b")
RE_SETTLED   = re.compile(r"NOTICE OF SETTLEMENT|CONDITIONAL SETTLEMENT|\bSETTLED\b|DISMISS\w*[^.]{0,40}\bSETTLE")
RE_JUDGMENT  = re.compile(r"NOTICE OF ENTRY OF JUDGMENT|DEFAULT JUDGMENT|JUDGMENT OF DISMISSAL|ENTRY OF JUDGMENT|JUDGMENT ENTERED|\bCONSENT JUDGMENT\b")
RE_DISMISSED = re.compile(r"DISMISSAL OF (THE )?ENTIRE ACTION|REQUEST FOR DISMISSAL|ORDER (OF|FOR) DISMISSAL|\bDISMISSED\b|VOLUNTARY DISMISSAL|INVOLUNTARY DISMISSAL")

STATUS_LABELS = {
    "judgment_satisfied": "Judgment satisfied",
    "dismissed_settled": "Case dismissed; settled",
    "dismissed": "Case dismissed",
    "judgment_entered": "Judgment entered",
    "settled": "Case settled",
    "open": "Open / no terminal disposition detected",
}


def norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " "))


def classify(entries: list[dict]):
    descs = [(e.get("description") or "").upper() for e in entries]
    # SATISFIED only on a full satisfaction entry — a "PARTIAL SATISFACTION" does
    # NOT close the case, so it must not land in the skip set.
    satisfied = any(RE_SATISFIED.search(d) and not RE_PARTIAL.search(d)
                    and not RE_SATISFIED_NEG.search(d) for d in descs)
    blob = "  ".join(descs)
    settled = bool(RE_SETTLED.search(blob))
    judgment = bool(RE_JUDGMENT.search(blob))
    dismissed = bool(RE_DISMISSED.search(blob))
    # Terminal precedence (most-final first). IMPORTANT: only judgment_satisfied
    # is treated as terminal/skippable. A judgment_entered case is NOT done — it
    # enters the POST-JUDGMENT phase (enforcement, abstracts, wage garnishment,
    # renewals of judgment), which routinely runs for YEARS or DECADES before
    # satisfaction, generating new docket entries the whole time. So it must keep
    # being re-scanned until a SATISFACTION OF JUDGMENT finally appears.
    status = ("judgment_satisfied" if satisfied else
              "settled" if settled else
              "judgment_entered" if judgment else      # post-judgment active — re-scan
              "dismissed" if dismissed else
              "open")
    if dismissed and settled and not satisfied:
        status = "dismissed_settled"
    elif dismissed and not satisfied:
        status = "dismissed"
    signals = [d[:90] for d in descs
               if RE_SATISFIED.search(d) or RE_SETTLED.search(d)
               or RE_JUDGMENT.search(d) or RE_DISMISSED.search(d)][:6]
    return status, satisfied, settled, judgment, dismissed, signals


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases-dir", default=os.path.join(REPO, "archive", "cases"))
    ap.add_argument("--out-parquet", default=os.path.join(REPO, "data", "case_status.parquet"))
    ap.add_argument("--out-satisfied", default=os.path.join(REPO, "data", "satisfied_cases.txt"))
    args = ap.parse_args(argv)

    rows = []
    skipped_bad = 0
    for f in glob.iglob(os.path.join(args.cases_dir, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            skipped_bad += 1
            print(f"  ! skipped unreadable {os.path.basename(f)}: {e}")
            continue
        cn = d.get("case_number") or os.path.splitext(os.path.basename(f))[0]
        entries = d.get("docket_entries", []) or []
        status, satisfied, settled, judgment, dismissed, signals = classify(entries)
        rows.append({
            "case_number": norm(cn), "case_status": status,
            "case_status_label": status_label(status),
            "satisfied": satisfied, "settled": settled, "dismissed": dismissed,
            "judgment_entered": judgment, "n_entries": len(entries),
            "signals": " | ".join(signals),
            "classified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    rows.sort(key=lambda r: r["case_number"])
    status_df = pd.DataFrame(rows, columns=[
        "case_number", "case_status", "case_status_label", "satisfied",
        "settled", "dismissed", "judgment_entered", "n_entries", "signals",
        "classified_at",
    ])
    write_parquet_atomic(args.out_parquet, status_df)

    satisfied_cases = sorted({r["case_number"] for r in rows if r["satisfied"]})
    with open(args.out_satisfied, "w", encoding="utf-8") as fh:
        fh.write("# Satisfied-judgment case numbers — bulk capture SKIPS these by default\n")
        fh.write("# (a satisfied judgment is terminal; the docket won't grow).\n")
        fh.write("# NOT settled/dismissed cases (fee motions / appeals can still land), and\n")
        fh.write("# NOT merely judgment-ENTERED cases: those stay in the POST-JUDGMENT phase\n")
        fh.write("# (enforcement, renewals) for years/decades until satisfaction, so they keep\n")
        fh.write("# being re-scanned. Normalized (hyphenless). From classify_case_status.py.\n")
        for c in satisfied_cases:
            fh.write(c + "\n")

    if skipped_bad:
        print(f"  ! {skipped_bad} unreadable case file(s) skipped")
    print("cases:", len(rows), "| status:", dict(Counter(r["case_status"] for r in rows)))
    print("satisfied skip-list:", len(satisfied_cases), "->", args.out_satisfied)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
