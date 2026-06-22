#!/usr/bin/env python3
"""Refresh judges.json from the live SF Superior Court judicial-assignments page.

judges.json maps a judge CODE (the initials the tentatives data uses, e.g. "CK"
= Curtis E.A. Karnow) -> {name, dept}. The codes come from the tentatives feed,
NOT the roster, so this script does NOT invent codes. It:

  1. Fetches the current judicial-assignments page (HTML table:
     DEPT | LASTNAME, First (role) | designation | phone).
  2. Matches each existing code_map entry to a roster row by last + first name.
  3. Updates the matched entry's `dept` (judges get reassigned) and refreshes
     `updated` / `source`.
  4. Reports — and does NOT silently apply — the structural changes:
       * roster judges with NO code (NEW — need a code assigned when they first
         appear in the tentatives feed),
       * codes NOT in the current roster (departed / retired / assigned-out).
  5. Archives the raw roster HTML (dated) under sources/ for provenance.

Pure stdlib (urllib + re) + the repo's judges.json. No secrets.
Run by .github/workflows/judges.yml monthly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUDGES_JSON = os.path.join(REPO_ROOT, "judges.json")
ROSTER_URL = "https://sf.courts.ca.gov/general-information/judicial-assignments-2026"


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


def write_json_atomic(path: str, data: dict) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def fetch_html(url: str, attempts: int = 3) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "sfsc/judges-refresh"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts - 1:
                raise
            delay = min(30.0, 2.0 * (2 ** attempt))
            print(f"fetch retry {attempt + 1}/{attempts - 1}: {exc}; sleeping {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"unreachable retry state for {url}")


def norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def parse_roster(html_text: str) -> list[dict]:
    """Return [{dept, last, first, designation}] from the assignments table."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, re.S | re.I)
    out = []
    for r in rows:
        cells = [
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)
        ]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        dept, judge = cells[0], cells[1]
        designation = cells[2] if len(cells) > 2 else ""
        if judge.upper() == "JUDGE" or dept.upper() == "DEPT":
            continue  # header
        # "LASTNAME, First Middle (role)" — drop the parenthetical role.
        judge = re.sub(r"\([^)]*\)", "", judge).strip().rstrip(",")
        if "," not in judge:
            continue
        last, first = [p.strip() for p in judge.split(",", 1)]
        if not last or not first:
            continue
        out.append({"dept": dept.strip(), "last": last, "first": first,
                    "designation": designation})
    return out


def name_matches(code_name: str, last: str, first: str) -> bool:
    """A code_map name matches a roster row if it contains the roster last name
    AND the roster first name as substrings (token-ish, punctuation-insensitive)."""
    cn = norm(code_name)
    first_tok = norm(first.split()[0]) if first.split() else ""
    return bool(cn) and norm(last) in cn and first_tok in cn


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", default=None, help="Local roster HTML (else fetch live)")
    ap.add_argument("--out", default=JUDGES_JSON)
    ap.add_argument("--archive-dir", default=os.path.join(REPO_ROOT, "sources"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    raw = open(args.html, encoding="utf-8", errors="replace").read() if args.html else fetch_html(ROSTER_URL)
    roster = parse_roster(raw)
    if len(roster) < 20:
        print(f"Only {len(roster)} roster rows parsed — layout may have changed; aborting.",
              file=sys.stderr)
        return 1

    with open(args.out, encoding="utf-8") as fh:
        data = json.load(fh)
    code_map: dict = data["code_map"]

    matched_codes, reassigned, dept_unchanged = set(), [], 0
    used_roster = set()
    for code, info in code_map.items():
        # Capture the matched index directly; roster.index(hit) would return the
        # FIRST dict equal to hit, so duplicate roster rows could mark the wrong
        # index used and silently drop a real judge.
        hit_idx, hit = next(((i, r) for i, r in enumerate(roster)
                             if i not in used_roster
                             and name_matches(info.get("name", ""), r["last"], r["first"])), (None, None))
        if hit is None:
            continue
        used_roster.add(hit_idx)
        matched_codes.add(code)
        if str(info.get("dept")) != hit["dept"]:
            reassigned.append((code, info.get("name"), info.get("dept"), hit["dept"]))
            info["dept"] = hit["dept"]
        else:
            dept_unchanged += 1

    new_judges = [r for i, r in enumerate(roster) if i not in used_roster]
    departed = [(c, code_map[c].get("name"), code_map[c].get("dept"))
                for c in code_map if c not in matched_codes]

    print(f"Roster rows: {len(roster)} | matched codes: {len(matched_codes)} | "
          f"reassigned: {len(reassigned)} | unchanged: {dept_unchanged}")
    if reassigned:
        print("\n-- dept reassignments (applied) --")
        for c, n, old, new in reassigned:
            print(f"   {c} {n}: {old} -> {new}")
    if new_judges:
        print("\n-- roster judges with NO code (assign a code when seen in tentatives) --")
        for r in new_judges:
            print(f"   {r['last']}, {r['first']} (Dept {r['dept']}) — {r['designation']}")
    if departed:
        print("\n-- codes NOT in current roster (departed / assigned-out / senior) --")
        for c, n, d in departed:
            print(f"   {c} {n} (was Dept {d})")

    data["updated"] = dt.date.today().isoformat()
    data["source"] = ROSTER_URL
    # Record the structural deltas in-file so a reviewer/agent can act on them.
    data["pending_review"] = {
        "checked_at": dt.datetime.utcnow().isoformat() + "Z",
        "new_in_roster_no_code": [f"{r['last']}, {r['first']} (Dept {r['dept']})" for r in new_judges],
        "codes_not_in_roster": [c for c, _, _ in departed],
    }

    if args.dry_run:
        print("\n[dry-run] not writing.")
        return 0

    write_json_atomic(args.out, data)
    os.makedirs(args.archive_dir, exist_ok=True)
    stamp = dt.date.today().isoformat().replace("-", "")
    write_text_atomic(os.path.join(args.archive_dir, f"sftc-judicial-assignments-{stamp}.html"), raw)
    print(f"\nWrote {args.out} + archived roster ({stamp}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
