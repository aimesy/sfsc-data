#!/usr/bin/env python3
"""Reject CI patterns that materialize full docket/case corpora unnecessarily."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    path = ROOT / rel
    if path.exists():
        return path.read_text(encoding="utf-8")
    return subprocess.check_output(
        ["git", "show", f"HEAD:{rel}"],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
    )


REQUIRED_SUBSTRINGS = [
    (
        ".github/workflows/case-tables.yml",
        "--skip-docket-entries",
        "case-tables CI must not rebuild the monolithic docket_entries parquet",
    ),
]

FORBIDDEN_PATTERNS = [
    (
        "scripts/build_case_tables.py",
        r"case_files\s*=\s*list\s*\(\s*iter_case_files",
        "build_case_tables must stream archive/cases paths",
    ),
    (
        "scripts/derive_metrics.py",
        r"case_files\s*=\s*list\s*\(",
        "derive_metrics must not materialize the archive/cases path list",
    ),
    (
        "scripts/cause_extract.py",
        r"files\s*=\s*sorted\s*\(\s*glob\.glob",
        "cause_extract must stream archive/cases paths",
    ),
    (
        "scripts/classify_case_status.py",
        r"sorted\s*\(\s*glob\.glob\s*\(",
        "classify_case_status must stream archive/cases paths",
    ),
    (
        "scripts/index_litigants.py",
        r"sorted\s*\(\s*glob\.glob\s*\(\s*ARCHIVE_GLOB",
        "index_litigants must stream archive/cases paths",
    ),
    (
        "scripts/index_clerk_categories.mjs",
        r"fs\.readdirSync\s*\(\s*CASES_DIR\s*\)\s*\.filter",
        "clerk category facets must stream archive/cases paths",
    ),
    (
        "scripts/index_judges_cases.mjs",
        r"fs\.readdirSync\s*\(\s*CASES_DIR\s*\)\s*\.filter",
        "judge facets must stream archive/cases paths",
    ),
    (
        "scripts/index_locations_cases.mjs",
        r"fs\.readdirSync\s*\(\s*CASES_DIR\s*\)\s*\.filter",
        "location facets must stream archive/cases paths",
    ),
]


def main() -> int:
    failures: list[str] = []

    for rel, needle, message in REQUIRED_SUBSTRINGS:
        text = read(rel)
        if needle not in text:
            failures.append(f"{rel}: missing {needle!r}: {message}")

    for rel, pattern, message in FORBIDDEN_PATTERNS:
        try:
            text = read(rel)
        except subprocess.CalledProcessError:
            failures.append(f"{rel}: missing file for CI materialization guard")
            continue
        if re.search(pattern, text):
            failures.append(f"{rel}: forbidden materialization pattern: {message}")

    if failures:
        print("CI materialization guard failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("CI materialization guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
