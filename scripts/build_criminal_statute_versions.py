#!/usr/bin/env python3
"""Build observed criminal statute version metadata from official sources.

This generator has two jobs:

* inventory the statute sections that actually appear in criminal index / portal
  charge rows; and
* attach official current-version metadata from California Legislative
  Information while preserving any manually verified historical originals that
  are stored as GitHub Release assets.

Historical originals are intentionally conservative. If a filing date predates
the current LegInfo version and no release-backed historical original covers
that date, importer/viewer code should keep the citation and expose the current
LegInfo page only as a current-law reference, not as the filing-date statute.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from import_scanner_cases import CODE_NAMES, iso_date, leginfo_url, parse_charge_rows


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_REPO_CRI = ROOT / ".tmp" / "sfsc-data-repo" / "archive" / "case-directory" / "CRI"
DEFAULT_PRODUCT_CRI = ROOT / "archive" / "case-directory" / "CRI"
DEFAULT_CACHE = ROOT / ".tmp" / "leginfo-sections"
DEFAULT_JSON = ROOT / "assets" / "data" / "criminal-statute-current-versions.json"
DEFAULT_JS = ROOT / "assets" / "js" / "criminal-statute-current-versions.js"
DEFAULT_MANIFEST = ROOT / "assets" / "sources" / "criminal-statutes" / "manifest.json"
SCHEMA = "sfsc-criminal-statute-current-versions-v1"

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
HISTORY_START_RE = re.compile(
    r"^(?:Amended|Added|Repealed|Renumbered|Repealed and added|Repealed and reenacted|"
    r"Repealed, added|Transferred|Enacted|Formerly)\b",
    re.I,
)
ITALIC_RE = re.compile(r"<i[^>]*>\s*\((?P<body>.*?)\)\s*</i>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})\b",
    re.I,
)
YEAR_RE = re.compile(r"\b(18|19|20)\d{2}\b")


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def base_section(section: object) -> str:
    return re.sub(r"\(.*$", "", clean(section)).strip()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as fh:
        tmp = Path(fh.name)
        fh.write(text)
        fh.flush()
    tmp.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def existing_sections(path: Path) -> dict[str, dict[str, Any]]:
    payload = load_json(path)
    sections = payload.get("sections")
    return sections if isinstance(sections, dict) else {}


def source_dir(path: Path | None) -> Path:
    if path:
        return path
    if DEFAULT_DATA_REPO_CRI.exists():
        return DEFAULT_DATA_REPO_CRI
    return DEFAULT_PRODUCT_CRI


def row_filing_date(row: dict[str, Any], fallback_year: str) -> str:
    for key in ("filing_date", "filed_date", "date_filed"):
        value = iso_date(row.get(key))
        if value:
            return value
    year = clean(row.get("year") or fallback_year)
    return f"{year}-07-01" if re.fullmatch(r"\d{4}", year) else ""


def iter_criminal_rows(cri_dir: Path):
    for path in sorted(cri_dir.glob("*.ndjson")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield path.stem, row


def inventory_sections(cri_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    observed: dict[str, dict[str, Any]] = {}
    totals = Counter()
    years: dict[str, Counter] = defaultdict(Counter)
    dates: dict[str, list[str]] = defaultdict(list)
    examples: dict[str, str] = {}

    for shard_year, row in iter_criminal_rows(cri_dir):
        totals["rows"] += 1
        filing_date = row_filing_date(row, shard_year)
        parsed = row.get("charges_parsed")
        if not isinstance(parsed, list) or not parsed:
            parsed = parse_charge_rows(row.get("charges") or "", filing_date)
        if parsed:
            totals["rows_with_parsed_charges"] += 1
        for charge in parsed:
            if not isinstance(charge, dict):
                continue
            code_system = clean(charge.get("code_system")).upper()
            section = base_section(charge.get("section"))
            if not code_system or not section or code_system not in CODE_NAMES:
                continue
            key = f"{code_system} {section}"
            totals["parsed_statute_charges"] += 1
            years[key][filing_date[:4] or shard_year] += 1
            if filing_date:
                dates[key].append(filing_date)
            examples.setdefault(key, clean(row.get("case_number")))

    for key, year_counts in years.items():
        code_system, section = key.split(" ", 1)
        date_values = sorted(value for value in dates.get(key, []) if value)
        observed[key] = {
            "code_system": code_system,
            "section": section,
            "citation": f"{CODE_NAMES[code_system][0]} \u00a7 {section}",
            "observed_charge_count": sum(year_counts.values()),
            "observed_years": dict(sorted(year_counts.items())),
            "observed_first_filing_date": date_values[0] if date_values else "",
            "observed_last_filing_date": date_values[-1] if date_values else "",
            "example_case_number": examples.get(key, ""),
        }
    totals["observed_section_count"] = len(observed)
    totals["observed_code_year_count"] = sum(len(row["observed_years"]) for row in observed.values())
    return dict(sorted(observed.items())), dict(totals)


def cache_name(code_system: str, section: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]+", "_", f"{code_system}_{section}")
    return f"{safe}.html"


def fetch_leginfo_html(
    code_system: str,
    section: str,
    *,
    cache_dir: Path,
    refresh: bool,
    sleep_seconds: float,
) -> tuple[str, str, str]:
    url = leginfo_url(code_system, section)
    if not url:
        return "", "", "unmapped_code_family"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / cache_name(code_system, section)
    if cache_path.exists() and not refresh:
        return url, cache_path.read_text(encoding="utf-8", errors="replace"), "cache"
    req = urllib.request.Request(url, headers={"User-Agent": "sfsc-criminal-statute-version-builder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        if cache_path.exists():
            return url, cache_path.read_text(encoding="utf-8", errors="replace"), f"cache_after_fetch_error:{type(exc).__name__}"
        return url, "", f"fetch_error:{type(exc).__name__}"
    atomic_write_text(cache_path, body)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    return url, body, "fetched"


def strip_tags(value: str) -> str:
    return clean(html.unescape(TAG_RE.sub(" ", value)))


def current_history_from_html(body: str) -> str:
    if not body:
        return ""
    histories: list[str] = []
    for match in ITALIC_RE.finditer(body):
        text = strip_tags(match.group("body"))
        if HISTORY_START_RE.search(text):
            histories.append(text)
    return histories[-1] if histories else ""


def parse_named_date(text: str) -> str:
    match = DATE_RE.search(text)
    if not match:
        return ""
    month = MONTHS[match.group("month").lower()]
    return date(int(match.group("year")), month, int(match.group("day"))).isoformat()


def parse_version_dates(history: str) -> dict[str, str]:
    out: dict[str, str] = {}
    effective = ""
    operative = ""
    eff_match = re.search(r"\bEffective\s+([^.;]+)", history, re.I)
    if eff_match:
        effective = parse_named_date(eff_match.group(1))
    op_match = re.search(r"\bOperative\s+([^.;]+)", history, re.I)
    if op_match:
        operative = parse_named_date(op_match.group(1))
    if effective:
        out["effective_date"] = effective
    if operative:
        out["operative_date"] = operative
    if operative:
        out["current_version_start_date"] = operative
        out["current_version_start_date_basis"] = "operative_date"
    elif effective:
        out["current_version_start_date"] = effective
        out["current_version_start_date_basis"] = "effective_date"
    else:
        year_values = [m.group(0) for m in YEAR_RE.finditer(history)]
        if year_values and re.search(r"\b(?:Enacted|Added)\b", history, re.I):
            out["current_version_start_date"] = f"{year_values[-1]}-01-01"
            out["current_version_start_date_basis"] = "history_year_only"
    return out


def historical_covers_version(version: dict[str, Any], filing_date: str) -> bool:
    start = clean(version.get("effective_from"))
    end = clean(version.get("effective_to"))
    if not start or not filing_date or filing_date < start:
        return False
    return not end or filing_date <= end


def coverage_status(record: dict[str, Any], filing_date: str) -> str:
    historical = record.get("historical_versions")
    if isinstance(historical, list):
        for version in historical:
            if isinstance(version, dict) and historical_covers_version(version, filing_date):
                return "historical_original"
    current_from = clean(record.get("current_version_start_date"))
    if filing_date and current_from:
        if filing_date < current_from:
            return "needs_historical_original"
        return "current_version"
    return "current_version_date_unknown"


def merge_record(
    key: str,
    observed: dict[str, Any],
    existing: dict[str, Any],
    *,
    cache_dir: Path,
    refresh: bool,
    sleep_seconds: float,
    fetch: bool,
) -> dict[str, Any]:
    code_system = observed["code_system"]
    section = observed["section"]
    current_url = leginfo_url(code_system, section)
    record: dict[str, Any] = {
        "law_code": CODE_NAMES[code_system][1],
        "section": section,
        "source_url": current_url,
        "current_source": "california_legislative_information",
        "current_source_status": "not_fetched",
        **observed,
    }
    if existing:
        for field in ("historical_versions", "notes", "manual_review"):
            if existing.get(field):
                record[field] = existing[field]
    if fetch:
        url, body, status = fetch_leginfo_html(
            code_system,
            section,
            cache_dir=cache_dir,
            refresh=refresh,
            sleep_seconds=sleep_seconds,
        )
        record["source_url"] = url or current_url
        record["current_source_status"] = status
        if body:
            history = current_history_from_html(body)
            if history:
                record["history"] = history
                record.update(parse_version_dates(history))
                record["current_source_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
            else:
                record["current_source_status"] = f"{status}:history_not_found"
    else:
        for field in ("history", "effective_date", "operative_date", "current_version_start_date", "current_version_start_date_basis"):
            if existing.get(field):
                record[field] = existing[field]
    status_counts = Counter()
    for year, count in record.get("observed_years", {}).items():
        filing_date = f"{year}-07-01" if re.fullmatch(r"\d{4}", str(year)) else ""
        status_counts[coverage_status(record, filing_date)] += int(count)
    record["observed_charge_count_by_version_status"] = dict(sorted(status_counts.items()))
    return {key: value for key, value in record.items() if value not in ("", {}, [])}


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    cri_dir = source_dir(args.cri_dir)
    observed, totals = inventory_sections(cri_dir)
    existing = existing_sections(args.json_out)
    sections = {}
    for key, item in observed.items():
        sections[key] = merge_record(
            key,
            item,
            existing.get(key, {}),
            cache_dir=args.cache_dir,
            refresh=args.refresh,
            sleep_seconds=args.sleep,
            fetch=not args.no_fetch,
        )
    historical_versions = sum(len(row.get("historical_versions") or []) for row in sections.values())
    version_status_counts = Counter()
    for record in sections.values():
        for status, count in (record.get("observed_charge_count_by_version_status") or {}).items():
            version_status_counts[status] += int(count)
    return {
        "schema": SCHEMA,
        "source_note": (
            "Observed criminal charge sections are generated from the case-directory CRI rows. "
            "Current text metadata comes from official California Legislative Information pages. "
            "Historical filing-date links are used only when a verified official original is "
            "preserved as an SFSC GitHub Release asset; otherwise importer/viewer code keeps "
            "today's LegInfo page as a current-law reference, not as the filing-date statute."
        ),
        "generated_from": {
            "criminal_case_directory": str(cri_dir),
            "current_source": "https://leginfo.legislature.ca.gov/faces/codes.xhtml",
            "historical_original_manifest": str(DEFAULT_MANIFEST.relative_to(ROOT)).replace("\\", "/"),
        },
        "coverage": {
            **totals,
            "historical_version_count": historical_versions,
            "observed_charge_count_by_version_status": dict(sorted(version_status_counts.items())),
        },
        "sections": dict(sorted(sections.items())),
    }


def write_outputs(payload: dict[str, Any], json_path: Path, js_path: Path) -> None:
    atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    js = (
        "// Generated by scripts/build_criminal_statute_versions.py. Do not hand-edit.\n"
        "export const CRIMINAL_STATUTE_CURRENT_VERSION_LOOKUP = "
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + ";\n"
    )
    atomic_write_text(js_path, js)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cri-dir", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--js-out", type=Path, default=DEFAULT_JS)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--no-fetch", action="store_true", help="Only inventory observed sections and preserve cached/existing metadata.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Polite delay after fetched LegInfo pages.")
    args = parser.parse_args(argv)
    payload = build_payload(args)
    write_outputs(payload, args.json_out, args.js_out)
    print(json.dumps(payload["coverage"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
