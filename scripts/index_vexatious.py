#!/usr/bin/env python3
"""Ingest the California Judicial Council Vexatious Litigant List (authoritative).

Vexatious-litigant status is applied SOLELY from authoritative sources, NEVER
inferred from tentative-ruling text (a tentative merely mentioning a
vexatious-litigant motion — which may be denied — must never flag anyone):

  1. On the CURRENT Judicial Council list  -> source=judicial_council, SOLID
     (tentative=False). The list is the live "in effect" roster (CCP 391.7,
     updated monthly) and carries the issuing court + case number + order date,
     so San Francisco Superior Court rows match OUR cases by case_number.
  2. A docket order declaring someone vexatious but NOT on the current list ->
     rendered TENTATIVE/DOTTED (it may have been vacated/expired/unreported and
     may no longer be in effect). That path is docket-capture-gated; this script
     only does (1).

Outputs:
  * data/vexatious.parquet  — one row per SF Superior Court vexatious entry,
    stamped with judicial_council provenance from tag_registry.json. Same core
    columns as data/tags.parquet (case_number, department, ns, name, parent,
    tier, source, authority, tentative) so the viewer can union it, plus
    provenance extras (litigant_name, order_date, issuing_court, list_case_no,
    comments, in_tentatives).
  * data/scan_targets.txt   — the SF vexatious case numbers (hyphenless CaseNum
    form), the prioritized FRONT of the capture scan list (the ~cases we do not
    yet have are the highest-value targets).

The list is fetched live (https://courts.ca.gov/system/files/file/vexlit.pdf)
or read from a local --pdf path. NO secrets, no court-site access.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.request

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG_REGISTRY_PATH = os.path.join(REPO_ROOT, "tag_registry.json")
VEXLIT_URL = "https://courts.ca.gov/system/files/file/vexlit.pdf"

# A San Francisco Superior Court row, with the case number captured between the
# court phrase and the MM/DD/YY[YY] order date. Court of Appeal rows (1st Dist
# sits in SF) are deliberately excluded — we index trial cases.
SF_ROW_RE = re.compile(
    r"^(?P<name>.+?)\s+San Francisco Superior Court\s+"
    r"(?P<caseno>[A-Za-z0-9\-]+)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{2,4})\s*(?P<comments>.*)$"
)


def write_text_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
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


def write_bytes_atomic(path: str, body: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_parquet_atomic(path: str, df: "pd.DataFrame") -> None:
    directory = os.path.dirname(path) or "."
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


def norm_case(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def load_registry_source(name: str = "judicial_council") -> dict:
    with open(TAG_REGISTRY_PATH, encoding="utf-8") as fh:
        reg = json.load(fh)
    return reg["sources"][name]


def pdf_lines(pdf_path: str) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    out: list[str] = []
    for page in reader.pages:
        for ln in (page.extract_text() or "").split("\n"):
            ln = ln.strip()
            if ln:
                out.append(ln)
    return out


def fetch_pdf(dest: str) -> str:
    req = urllib.request.Request(VEXLIT_URL, headers={"User-Agent": "sfsc/vexatious-index"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    if not body[:1024].lstrip().startswith(b"%PDF"):
        raise ValueError(f"{VEXLIT_URL} did not return PDF bytes")
    write_bytes_atomic(dest, body)
    return dest


def parse_sf_rows(lines: list[str]) -> list[dict]:
    rows = []
    for ln in lines:
        if "San Francisco Superior Court" not in ln:
            continue
        m = SF_ROW_RE.match(ln)
        if not m:
            continue
        rows.append({
            "litigant_name": re.sub(r"\s+", " ", m.group("name")).strip(),
            "list_case_no": m.group("caseno").strip(),
            "order_date": m.group("date").strip(),
            "issuing_court": "San Francisco Superior Court",
            "comments": re.sub(r"\s+", " ", m.group("comments")).strip() or None,
        })
    return rows


def load_tentative_index() -> dict[str, str]:
    """Map normalized case_number -> department for every tentative case."""
    idx: dict[str, str] = {}
    import glob
    for f in glob.glob(os.path.join(REPO_ROOT, "data", "tentatives-*.parquet")):
        if "extras" in f:
            continue
        df = pd.read_parquet(f, columns=["case_number", "department"])
        for cn, dept in zip(df["case_number"], df["department"]):
            if cn is None:
                continue
            idx.setdefault(norm_case(str(cn)), str(dept) if dept is not None else None)
    return idx


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", default=None,
                    help="Local vexlit.pdf path (otherwise downloaded from courts.ca.gov)")
    ap.add_argument("--out-parquet", default=os.path.join(REPO_ROOT, "data", "vexatious.parquet"))
    ap.add_argument("--out-scan", default=os.path.join(REPO_ROOT, "data", "scan_targets.txt"))
    args = ap.parse_args(argv)

    pdf_path = args.pdf
    if not pdf_path:
        pdf_path = os.path.join(REPO_ROOT, "data", "_vexlit.pdf")
        print(f"Downloading {VEXLIT_URL} -> {pdf_path}")
        fetch_pdf(pdf_path)

    lines = pdf_lines(pdf_path)
    sf_rows = parse_sf_rows(lines)
    if not sf_rows:
        print("No SF Superior Court rows parsed — aborting (PDF layout changed?)", file=sys.stderr)
        return 1

    src = load_registry_source("judicial_council")
    tindex = load_tentative_index()

    # One emitted tag row per DISTINCT case number (aliases share a case number).
    seen: dict[str, dict] = {}
    for r in sf_rows:
        key = norm_case(r["list_case_no"])
        if not key:
            continue
        dept = tindex.get(key)
        rec = seen.get(key)
        if rec is None:
            seen[key] = {
                "case_number": key,
                "department": dept,
                "ns": "status",
                "name": "vexatious-litigant",
                "parent": None,
                "tier": src["tier"],
                "source": "judicial_council",
                "authority": src["authority"],
                "tentative": bool(src["tentative"]),   # False -> SOLID pill
                "litigant_name": r["litigant_name"],
                "order_date": r["order_date"],
                "issuing_court": r["issuing_court"],
                "list_case_no": r["list_case_no"],
                "comments": r["comments"],
                "in_tentatives": key in tindex,
            }
        else:
            # Same case, alias row — keep the longest litigant_name, merge comments.
            if len(r["litigant_name"]) > len(rec["litigant_name"]):
                rec["litigant_name"] = r["litigant_name"]

    out = list(seen.values())
    if not out:
        print("No SF vexatious rows parsed (every row's case number normalized empty?) — "
              "aborting without writing.", file=sys.stderr)
        return 1
    df = pd.DataFrame(out)
    df["tier"] = df["tier"].astype("int64")
    df["authority"] = df["authority"].astype("int64")
    write_parquet_atomic(args.out_parquet, df)

    matched = sum(1 for r in out if r["in_tentatives"])
    print(f"Wrote {len(out)} SF vexatious case rows ({matched} match our tentatives, "
          f"{len(out) - matched} not yet captured) to {args.out_parquet}")

    # Scan-target list: vexatious cases at the FRONT. Cases we do NOT yet have
    # come first (highest-value capture targets), then the ones we already have.
    not_have = [r["case_number"] for r in out if not r["in_tentatives"]]
    have = [r["case_number"] for r in out if r["in_tentatives"]]
    scan_text = (
        "# Capture scan targets — PRIORITY FRONT OF THE LIST.\n"
        "# Source: Judicial Council Vexatious Litigant List (SF Superior Court rows).\n"
        f"# Generated by scripts/index_vexatious.py. {len(out)} distinct SF vexatious cases.\n"
        f"# First {len(not_have)} are NOT yet in our tentatives (capture these first); "
        f"remaining {len(have)} we already have.\n"
        "# Hyphenless CaseNum form — paste into the Android app's Bulk dialog.\n"
        "\n# --- vexatious: not yet captured ---\n"
        + "".join(cn + "\n" for cn in not_have)
        + "\n# --- vexatious: already in tentatives ---\n"
        + "".join(cn + "\n" for cn in have)
    )
    write_text_atomic(args.out_scan, scan_text)
    print(f"Wrote scan targets ({len(not_have)} new + {len(have)} known) to {args.out_scan}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
