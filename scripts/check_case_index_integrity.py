#!/usr/bin/env python3
"""Validate that the case index and tracked case JSON files agree."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_DIR = ROOT / "archive" / "cases"
DEFAULT_INDEX = ROOT / "archive" / "cases-index.ndjson"


def norm_case(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def load_index_cases(path: Path) -> tuple[set[str], list[str], list[str]]:
    cases: set[str] = set()
    duplicates: list[str] = []
    bad_rows: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                bad_rows.append(f"{path}:{lineno}: invalid JSON: {exc}")
                continue
            if not isinstance(row, dict):
                bad_rows.append(f"{path}:{lineno}: row is not an object")
                continue
            case_number = norm_case(row.get("case_number"))
            if not case_number:
                bad_rows.append(f"{path}:{lineno}: missing case_number")
                continue
            if case_number in cases:
                duplicates.append(case_number)
            cases.add(case_number)
    return cases, duplicates, bad_rows


def load_json_cases(path: Path) -> set[str]:
    return {
        norm_case(child.stem)
        for child in path.glob("*.json")
        if child.is_file() and norm_case(child.stem)
    }


def summarize(label: str, values: set[str] | list[str], limit: int) -> str:
    ordered = sorted(values)
    shown = ", ".join(ordered[:limit])
    suffix = "" if len(ordered) <= limit else f", ... +{len(ordered) - limit} more"
    return f"{label}: {shown}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASE_DIR)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--sample", type=int, default=25)
    args = parser.parse_args()

    if not args.index.exists():
        raise SystemExit(f"ERROR: missing case index: {args.index}")
    if not args.cases_dir.exists():
        raise SystemExit(f"ERROR: missing case JSON directory: {args.cases_dir}")

    index_cases, duplicates, bad_rows = load_index_cases(args.index)
    json_cases = load_json_cases(args.cases_dir)
    missing_json = index_cases - json_cases
    missing_index = json_cases - index_cases

    failures = []
    if bad_rows:
        failures.append(summarize("bad index rows", bad_rows, args.sample))
    if duplicates:
        failures.append(summarize("duplicate index cases", duplicates, args.sample))
    if missing_json:
        failures.append(summarize("index rows without archive/cases JSON", missing_json, args.sample))
    if missing_index:
        failures.append(summarize("archive/cases JSON missing from index", missing_index, args.sample))

    if failures:
        print(
            "case-index integrity failed: "
            f"{len(index_cases)} unique index cases, {len(json_cases)} case JSON files"
        )
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print(f"case-index integrity ok: {len(index_cases)} index rows, {len(json_cases)} case JSON files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
