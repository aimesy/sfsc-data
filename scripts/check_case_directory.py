#!/usr/bin/env python3
"""Validate the generated Case Archive display directory."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "archive" / "case-directory"
DEFAULT_CASE_DIR = ROOT / "archive" / "cases"
DEFAULT_INDEX = ROOT / "archive" / "cases-index.ndjson"
DEFAULT_DISCOVERY_FEEDS = [
    ROOT / "archive" / "discovered-cases.ndjson",
    ROOT / "archive" / "new-filings-cases",
    ROOT / "archive" / "new-filings-cases.ndjson",
]
ALLOWED_SCAN_STATES = {
    "complete",
    "core_docs",
    "discovered",
    "indexed",
    "no_docs",
    "partial_docs",
    "restricted",
    "summary_only",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def manifest_int(value: object, default: int = -1) -> int:
    if value is None:
        return default
    return int(value)


def norm_case(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def year_from_civil_case_number(case_number: object) -> str:
    text = norm_case(case_number)
    if text.startswith("CRI"):
        return ""
    m = re.match(r"^[A-Z]+(\d{2})", text)
    if not m:
        m = re.match(r"^(\d{2})[A-Z]+", text)
    if not m:
        return ""
    yy = int(m.group(1))
    pivot = (datetime.now(timezone.utc).year + 1) % 100
    return str(2000 + yy if yy <= pivot else 1900 + yy)


def implausible_civil_year(case_number: object, year: object) -> bool:
    expected = year_from_civil_case_number(case_number)
    actual = str(year or "").strip()
    if not expected or not actual.isdigit():
        return False
    return abs(int(actual) - int(expected)) > 5


def filing_date_year(value: object) -> str:
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})", text)
    return m.group(1) if m else ""


def parse_ndjson(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{lineno}: row is not a JSON object")
            rows.append(row)
    return rows


def index_case_numbers(path: Path) -> tuple[list[str], list[str]]:
    cases: list[str] = []
    failures: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                failures.append(f"{path}:{lineno}: invalid JSON: {exc}")
                continue
            if not isinstance(row, dict):
                failures.append(f"{path}:{lineno}: row is not a JSON object")
                continue
            case_number = norm_case(row.get("case_number"))
            if not case_number:
                failures.append(f"{path}:{lineno}: row missing case_number")
                continue
            cases.append(case_number)
    return cases, failures


def case_set_fingerprint(cases: set[str]) -> str:
    digest = hashlib.sha256()
    for case_number in sorted(cases):
        digest.update(case_number.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def case_sample(cases: set[str], limit: int = 10) -> str:
    ordered = sorted(cases)
    sample = ", ".join(ordered[:limit])
    suffix = "" if len(ordered) <= limit else f", ... {len(ordered) - limit} more"
    return f"{sample}{suffix}"


def case_json_numbers(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {norm_case(child.stem) for child in path.glob("*.json") if norm_case(child.stem)}


def discovery_files(paths: list[Path]) -> list[Path]:
    files = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            files.extend(child for child in sorted(path.rglob("*.ndjson")) if child.is_file())
        else:
            files.append(path)
    return files


def inside(base: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def check_case_directory(
    base: Path,
    discovery_feeds: list[Path] | None = None,
    *,
    case_index: Path | None = DEFAULT_INDEX,
    case_dir: Path | None = DEFAULT_CASE_DIR,
) -> dict[str, Any]:
    failures: list[str] = []
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    manifest = load_json(manifest_path)
    source_counts = manifest.get("source_counts")
    if not isinstance(source_counts, dict):
        failures.append("manifest.source_counts is not an object")
        source_counts = {}
    case_json_rows = manifest_int(source_counts.get("case_json_rows"), 0)
    case_table_rows = manifest_int(source_counts.get("case_table_rows"), 0)
    case_index_rows = manifest_int(source_counts.get("case_index_rows"), 0)
    case_json_fingerprint = str(source_counts.get("case_json_fingerprint") or "").strip()
    case_table_fingerprint = str(source_counts.get("case_table_fingerprint") or "").strip()
    case_index_fingerprint = str(source_counts.get("case_index_fingerprint") or "").strip()
    if case_index_rows > 0 and max(case_json_rows, case_table_rows) <= 0:
        failures.append(
            "manifest source counts show no full case JSON rows or compact case-table rows; "
            "case directory cannot preserve restricted-case state"
        )
    if case_index_fingerprint:
        if (
            case_table_rows > 0
            and case_table_rows == case_index_rows
            and case_table_fingerprint
            and case_table_fingerprint != case_index_fingerprint
        ):
            failures.append("manifest compact case-table fingerprint differs from case-index fingerprint")
        if (
            case_json_rows > 0
            and case_json_rows == case_index_rows
            and case_json_fingerprint
            and case_json_fingerprint != case_index_fingerprint
        ):
            failures.append("manifest case-JSON fingerprint differs from case-index fingerprint")
    index_cases: list[str] = []
    index_case_set: set[str] = set()
    if case_index and case_index.exists():
        index_cases, index_failures = index_case_numbers(case_index)
        failures.extend(index_failures)
        duplicate_count = len(index_cases) - len(set(index_cases))
        if duplicate_count:
            failures.append(f"{case_index}: contains {duplicate_count} duplicate case_number row(s)")
        index_case_set = set(index_cases)
        actual_index_rows = len(index_cases)
        if case_index_rows < actual_index_rows:
            failures.append(
                "manifest.source_counts.case_index_rows is "
                f"{case_index_rows}, below {case_index}'s {actual_index_rows} row(s)"
            )
        if case_dir and case_dir.exists() and case_table_rows <= 0:
            json_cases = case_json_numbers(case_dir)
            missing_json = sorted(set(index_cases) - json_cases)
            missing_index = sorted(json_cases - set(index_cases))
            if missing_json:
                sample = ", ".join(missing_json[:10])
                suffix = "" if len(missing_json) <= 10 else f", ... {len(missing_json) - 10} more"
                failures.append(
                    f"{case_index}: {len(missing_json)} row(s) have no matching "
                    f"{case_dir}/<case>.json ({sample}{suffix})"
                )
            if missing_index:
                sample = ", ".join(missing_index[:10])
                suffix = "" if len(missing_index) <= 10 else f", ... {len(missing_index) - 10} more"
                failures.append(
                    f"{case_dir}: {len(missing_index)} JSON file(s) have no matching "
                    f"{case_index} row ({sample}{suffix})"
                )
    discovered_cases = None
    discovery_label = ""
    if discovery_feeds is None:
        discovery_feeds = DEFAULT_DISCOVERY_FEEDS
    feeds = discovery_files(discovery_feeds)
    if feeds:
        discovered_cases = set()
        discovery_label = ", ".join(str(path) for path in discovery_feeds if path.exists())
        for feed in feeds:
            discovered_cases.update(
                str(row.get("case_number") or "").strip().upper()
                for row in parse_ndjson(feed)
                if row.get("case_number")
            )

    seen_cases: set[str] = set()
    captured_case_set: set[str] = set()
    state_counts: dict[str, int] = {}
    total_rows = 0
    total_cases = 0
    total_indexed = 0
    total_restricted = 0
    total_shards = 0
    indexed_discovered_count = 0
    indexed_discovered_samples: list[str] = []

    for pdf in base.rglob("*.pdf"):
        failures.append(f"unexpected PDF in generated directory: {pdf}")

    prefixes = manifest.get("prefixes")
    if not isinstance(prefixes, list):
        failures.append("manifest.prefixes is not an array")
        prefixes = []

    for prefix in prefixes:
        if not isinstance(prefix, dict):
            failures.append("manifest.prefixes contains a non-object row")
            continue
        prefix_manifest_rel = prefix.get("manifest")
        if not isinstance(prefix_manifest_rel, str) or not prefix_manifest_rel:
            failures.append(f"prefix {prefix.get('prefix')!r} has no manifest path")
            continue
        prefix_manifest_path = base / prefix_manifest_rel
        if not inside(base, prefix_manifest_path) or not prefix_manifest_path.exists():
            failures.append(f"missing prefix manifest: {prefix_manifest_rel}")
            continue
        prefix_manifest = load_json(prefix_manifest_path)
        years = prefix_manifest.get("years")
        if not isinstance(years, list):
            failures.append(f"{prefix_manifest_rel}: years is not an array")
            years = []
        prefix_count = 0
        prefix_case_count = 0
        prefix_discovered_count = 0
        prefix_indexed_count = 0
        prefix_restricted_count = 0
        for year in years:
            if not isinstance(year, dict):
                failures.append(f"{prefix_manifest_rel}: years contains a non-object row")
                continue
            shard_rel = year.get("path")
            if not isinstance(shard_rel, str) or not shard_rel:
                failures.append(f"{prefix_manifest_rel}: year {year.get('year')!r} has no shard path")
                continue
            shard_path = base / shard_rel
            if not inside(base, shard_path) or not shard_path.exists():
                failures.append(f"missing year shard: {shard_rel}")
                continue
            shard_rows = parse_ndjson(shard_path)
            total_shards += 1
            if len(shard_rows) != manifest_int(year.get("count")):
                failures.append(f"{shard_rel}: row count {len(shard_rows)} != manifest {year.get('count')}")
            prefix_count += len(shard_rows)
            total_rows += len(shard_rows)
            year_case_count = 0
            year_discovered_count = 0
            year_indexed_count = 0
            year_restricted_count = 0
            for row in shard_rows:
                case_number = str(row.get("case_number") or "").strip()
                if not case_number:
                    failures.append(f"{shard_rel}: row missing case_number")
                    continue
                row_year = str(row.get("year") or year.get("year") or "").strip()
                filed_year = filing_date_year(row.get("filing_date"))
                if filed_year and row_year != filed_year:
                    failures.append(
                        f"{case_number}: case-directory year {row_year!r} "
                        f"does not match filing_date year {filed_year!r}"
                    )
                elif not filed_year and implausible_civil_year(case_number, row_year):
                    failures.append(
                        f"{case_number}: implausible case-directory year {row_year!r}; "
                        f"case number implies {year_from_civil_case_number(case_number)}"
                    )
                if case_number in seen_cases:
                    failures.append(f"duplicate case row: {case_number}")
                seen_cases.add(case_number)
                state = str(row.get("scan_state") or "").strip()
                if state not in ALLOWED_SCAN_STATES:
                    failures.append(f"{case_number}: invalid scan_state {state!r}")
                state_counts[state] = state_counts.get(state, 0) + 1
                discovered = state == "discovered"
                indexed = state == "indexed"
                restricted = state == "restricted"
                if discovered and norm_case(case_number) in index_case_set:
                    indexed_discovered_count += 1
                    if len(indexed_discovered_samples) < 10:
                        indexed_discovered_samples.append(case_number)
                if discovered or indexed:
                    year_discovered_count += 1
                    if indexed:
                        year_indexed_count += 1
                        captured_case_set.add(norm_case(case_number))
                elif restricted:
                    year_restricted_count += 1
                    captured_case_set.add(norm_case(case_number))
                else:
                    year_case_count += 1
                    captured_case_set.add(norm_case(case_number))
                if discovered and row.get("case_json"):
                    failures.append(f"{case_number}: discovered-only row should not point at a case JSON")
                if discovered and discovered_cases is not None and case_number.upper() not in discovered_cases:
                    failures.append(f"{case_number}: discovered row is absent from discovery feeds ({discovery_label})")
                if row.get("archive_status") == "discovered" and row.get("captured_at"):
                    failures.append(f"{case_number}: discovered archive_status on captured row")
            prefix_case_count += year_case_count
            prefix_discovered_count += year_discovered_count
            prefix_indexed_count += year_indexed_count
            prefix_restricted_count += year_restricted_count
            total_cases += year_case_count
            total_indexed += year_indexed_count
            total_restricted += year_restricted_count
            if year_case_count != manifest_int(year.get("case_count")):
                failures.append(f"{shard_rel}: case count {year_case_count} != manifest {year.get('case_count')}")
            if year_discovered_count != manifest_int(year.get("discovered_count"), 0):
                failures.append(f"{shard_rel}: discovered count {year_discovered_count} != manifest {year.get('discovered_count')}")
            if year_indexed_count != manifest_int(year.get("indexed_count"), 0):
                failures.append(f"{shard_rel}: indexed count {year_indexed_count} != manifest {year.get('indexed_count')}")
            if year_restricted_count != manifest_int(year.get("restricted_count"), 0):
                failures.append(f"{shard_rel}: restricted count {year_restricted_count} != manifest {year.get('restricted_count')}")

        expected_prefix_count = manifest_int(prefix_manifest.get("count"))
        if prefix_count != expected_prefix_count:
            failures.append(f"{prefix_manifest_rel}: row count {prefix_count} != prefix manifest {expected_prefix_count}")
        if prefix_case_count != manifest_int(prefix_manifest.get("case_count")):
            failures.append(f"{prefix_manifest_rel}: case count {prefix_case_count} != prefix manifest {prefix_manifest.get('case_count')}")
        if prefix_discovered_count != manifest_int(prefix_manifest.get("discovered_count"), 0):
            failures.append(f"{prefix_manifest_rel}: discovered count {prefix_discovered_count} != prefix manifest {prefix_manifest.get('discovered_count')}")
        if prefix_indexed_count != manifest_int(prefix_manifest.get("indexed_count"), 0):
            failures.append(f"{prefix_manifest_rel}: indexed count {prefix_indexed_count} != prefix manifest {prefix_manifest.get('indexed_count')}")
        if prefix_restricted_count != manifest_int(prefix_manifest.get("restricted_count"), 0):
            failures.append(f"{prefix_manifest_rel}: restricted count {prefix_restricted_count} != prefix manifest {prefix_manifest.get('restricted_count')}")
        expected_top_count = manifest_int(prefix.get("count"))
        if prefix_count != expected_top_count:
            failures.append(f"{prefix_manifest_rel}: row count {prefix_count} != top manifest {expected_top_count}")
        if prefix_case_count != manifest_int(prefix.get("case_count")):
            failures.append(f"{prefix_manifest_rel}: case count {prefix_case_count} != top manifest {prefix.get('case_count')}")
        if prefix_discovered_count != manifest_int(prefix.get("discovered_count"), 0):
            failures.append(f"{prefix_manifest_rel}: discovered count {prefix_discovered_count} != top manifest {prefix.get('discovered_count')}")
        if prefix_indexed_count != manifest_int(prefix.get("indexed_count"), 0):
            failures.append(f"{prefix_manifest_rel}: indexed count {prefix_indexed_count} != top manifest {prefix.get('indexed_count')}")
        if prefix_restricted_count != manifest_int(prefix.get("restricted_count"), 0):
            failures.append(f"{prefix_manifest_rel}: restricted count {prefix_restricted_count} != top manifest {prefix.get('restricted_count')}")

    expected_display_rows = manifest_int(manifest.get("display_row_count", manifest.get("case_count")))
    if total_rows != expected_display_rows:
        failures.append(f"total row count {total_rows} != manifest display_row_count {manifest.get('display_row_count')}")
    if total_cases != manifest_int(manifest.get("case_count")):
        failures.append(f"captured docket count {total_cases} != manifest case_count {manifest.get('case_count')}")
    if indexed_discovered_count:
        sample = ", ".join(indexed_discovered_samples)
        suffix = "" if indexed_discovered_count <= len(indexed_discovered_samples) else f", ... {indexed_discovered_count - len(indexed_discovered_samples)} more"
        failures.append(
            f"{indexed_discovered_count} case-index row(s) are classified as discovered-only "
            f"in the generated directory ({sample}{suffix})"
        )
    if case_index_rows > 0 and case_json_rows == case_index_rows and total_cases + total_restricted + total_indexed != case_index_rows:
        failures.append(
            "captured + restricted + indexed directory count "
            f"{total_cases + total_restricted + total_indexed} != case-index rows {case_index_rows}"
        )
    if index_case_set and max(case_json_rows, case_table_rows) > 0 and case_table_rows <= 0:
        missing_from_directory = index_case_set - captured_case_set
        extra_in_directory = captured_case_set - index_case_set
        if missing_from_directory:
            failures.append(
                f"{len(missing_from_directory)} case-index case(s) missing from captured directory rows: "
                f"{case_sample(missing_from_directory)}"
            )
        if extra_in_directory:
            failures.append(
                f"{len(extra_in_directory)} captured directory case(s) absent from case index: "
                f"{case_sample(extra_in_directory)}"
            )
        if case_index_fingerprint:
            actual_fingerprint = case_set_fingerprint(captured_case_set)
            if actual_fingerprint != case_index_fingerprint:
                failures.append("captured directory fingerprint differs from manifest case-index fingerprint")
    discovered_like_count = state_counts.get("discovered", 0) + state_counts.get("indexed", 0)
    if discovered_like_count != manifest_int(manifest.get("discovered_count"), 0):
        failures.append(f"discovered/indexed count {discovered_like_count} != manifest {manifest.get('discovered_count')}")
    if total_indexed != manifest_int(manifest.get("indexed_count"), 0):
        failures.append(f"indexed count {total_indexed} != manifest {manifest.get('indexed_count')}")
    if total_restricted != manifest_int(manifest.get("restricted_count"), 0):
        failures.append(f"restricted count {total_restricted} != manifest {manifest.get('restricted_count')}")
    if total_shards != manifest_int(manifest.get("year_shard_count")):
        failures.append(f"year shard count {total_shards} != manifest {manifest.get('year_shard_count')}")
    if state_counts != dict(manifest.get("scan_state_counts") or {}):
        failures.append(f"state counts {state_counts} != manifest {manifest.get('scan_state_counts')}")

    return {
        "display_row_count": total_rows,
        "case_count": total_cases,
        "discovered_count": state_counts.get("discovered", 0) + state_counts.get("indexed", 0),
        "indexed_count": total_indexed,
        "restricted_count": total_restricted,
        "prefix_count": len(prefixes),
        "year_shard_count": total_shards,
        "scan_state_counts": dict(sorted(state_counts.items())),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument(
        "--case-index",
        type=Path,
        default=DEFAULT_INDEX,
        help="Canonical archive/cases-index.ndjson to compare against the generated directory manifest.",
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=DEFAULT_CASE_DIR,
        help=(
            "Canonical archive/cases directory. Strict index/JSON parity is checked "
            "only when no compact case table is available."
        ),
    )
    parser.add_argument(
        "--discovery-feed",
        type=Path,
        action="append",
        default=[],
        help="Discovery feed file or directory. Can be repeated. Defaults to archive/discovered-cases and archive/new-filings-cases.",
    )
    args = parser.parse_args()
    result = check_case_directory(
        args.dir,
        args.discovery_feed or DEFAULT_DISCOVERY_FEEDS,
        case_index=args.case_index,
        case_dir=args.case_dir,
    )
    if result["failures"]:
        for failure in result["failures"]:
            print(f"FAIL: {failure}")
        return 1
    print(
        "case-directory ok: "
        f"{result['case_count']} dockets, "
        f"{result['restricted_count']} restricted, "
        f"{result['discovered_count']} discovered, "
        f"{result['indexed_count']} indexed, "
        f"{result['display_row_count']} display rows, "
        f"{result['prefix_count']} prefixes, "
        f"{result['year_shard_count']} year shards, "
        f"states={result['scan_state_counts']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
