#!/usr/bin/env python3
"""Build the lightweight sharded Case Archive display directory.

The canonical capture remains ``archive/cases/<case_number>.json``. This script
builds the small viewer-facing directory used by the Case Archive landing page:
one top manifest, one manifest per case prefix, and one NDJSON row shard per
prefix/year. It is intentionally display-first and does not store document
bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from import_scanner_cases import parse_charge_rows


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_DIR = ROOT / "archive" / "cases"
DEFAULT_INDEX = ROOT / "archive" / "cases-index.ndjson"
DEFAULT_INDEX_ENRICHMENTS = [ROOT / "archive" / "criminal-index-enrichment.ndjson"]
DEFAULT_CASE_TABLE = ROOT / "data" / "cases.parquet"
DEFAULT_OUT_DIR = ROOT / "archive" / "case-directory"
DEFAULT_DISCOVERY_PATHS = [
    ROOT / "archive" / "discovered-cases.ndjson",
    ROOT / "archive" / "new-filings-cases",
    ROOT / "archive" / "new-filings-cases.ndjson",
    ROOT / "data" / "new-filings-cases.ndjson",
    ROOT / "data" / "new_filings_cases.ndjson",
]

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("<br>", " ")).strip()


def norm_case(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", clean(value)).upper()


def parse_ndjson(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def discovery_input_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*.ndjson")):
                if child.is_file():
                    yield child
            continue
        yield path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    tmp.replace(path)


def remove_tree(path: Path) -> None:
    def retry_writable(func: Any, file_path: str, _exc_info: Any) -> None:
        try:
            os.chmod(file_path, 0o700)
            func(file_path)
        except FileNotFoundError:
            return

    shutil.rmtree(path, onerror=retry_writable)


def make_tree_writable(path: Path) -> None:
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        os.chmod(root, 0o700)
        for name in dirs:
            os.chmod(Path(root) / name, 0o700)
        for name in files:
            os.chmod(Path(root) / name, 0o600)


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def parse_date(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000 if year <= 30 else 1900
        return f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b([A-Za-z]{3,4})[- ](\d{1,2})[-, ]+(\d{2,4})\b", text)
    if m:
        month = MONTHS.get(m.group(1).lower())
        if month:
            year = int(m.group(3))
            if year < 100:
                year += 2000 if year <= 30 else 1900
            return f"{year:04d}-{month:02d}-{int(m.group(2)):02d}"
    return ""


def first_date(values: Iterable[Any]) -> str:
    dates = [parse_date(v) for v in values]
    dates = [d for d in dates if d]
    return min(dates) if dates else ""


def first_docket_date(case: dict[str, Any]) -> str:
    entries = case.get("docket_entries")
    if not isinstance(entries, list):
        return ""
    values = []
    for entry in entries:
        if isinstance(entry, dict):
            values.extend([
                entry.get("date_filed"),
                entry.get("filed"),
                entry.get("date"),
                entry.get("FILEDATE"),
            ])
    return first_date(values)


def filing_date_for_case(case: dict[str, Any]) -> str:
    direct = first_date([
        case.get("filing_date"),
        case.get("filed"),
        case.get("date_filed"),
        case.get("file_date"),
        case.get("created"),
    ])
    return direct or first_docket_date(case)


def charge_text_from_case(case: dict[str, Any]) -> str:
    candidates: list[Any] = [case.get("charges")]
    criminal_index = case.get("criminal_index") if isinstance(case.get("criminal_index"), dict) else {}
    candidates.append(criminal_index.get("charges"))
    rows = criminal_index.get("rows") if isinstance(criminal_index.get("rows"), list) else []
    for row in rows:
        if isinstance(row, dict):
            candidates.append(row.get("charges") or row.get("CHARGES"))
    for value in candidates:
        text = clean(value)
        if text:
            return text
    return ""


def charge_rows_from_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    rows = case.get("charges_parsed")
    if isinstance(rows, list):
        parsed_rows = [row for row in rows if isinstance(row, dict)]
        if parsed_rows:
            return parsed_rows
    criminal = case.get("criminal") if isinstance(case.get("criminal"), dict) else {}
    rows = criminal.get("charge_rows")
    if isinstance(rows, list):
        parsed_rows = [row for row in rows if isinstance(row, dict)]
        if parsed_rows:
            return parsed_rows
    charges = charge_text_from_case(case)
    if charges:
        return parse_charge_rows(charges, filing_date_for_case(case))
    return []


def charge_text_from_row(row: dict[str, Any]) -> str:
    for key in ("charges", "criminal_charges", "charge", "CHARGES"):
        text = clean(row.get(key))
        if text:
            return text
    return ""


def charge_rows_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    direct = row.get("charges_parsed")
    if isinstance(direct, list):
        parsed_rows = [item for item in direct if isinstance(item, dict)]
        if parsed_rows:
            return parsed_rows
    raw = row.get("charges_json")
    if raw is not None:
        try:
            parsed = json.loads(clean(raw))
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            parsed_rows = [item for item in parsed if isinstance(item, dict)]
            if parsed_rows:
                return parsed_rows
    charges = charge_text_from_row(row)
    if charges:
        filing_date = parse_date(
            row.get("filing_date")
            or row.get("filed")
            or row.get("date_filed")
            or row.get("date")
        )
        return parse_charge_rows(charges, filing_date)
    return []


def prefix_for_case(case_number: str) -> str:
    m = re.match(r"^([A-Za-z]+)", case_number)
    if m:
        return m.group(1).upper()
    m = re.match(r"^\d{2}([A-Za-z]+)", case_number)
    if m:
        return m.group(1).upper()
    return "(none)"


def year_from_case_number(case_number: str) -> str:
    m = re.match(r"^[A-Za-z]+[-_\s]*(\d{2})", case_number)
    if not m:
        m = re.match(r"^(\d{2})[A-Za-z]+", case_number)
    if not m:
        return ""
    yy = int(m.group(1))
    pivot = (datetime.now(timezone.utc).year + 1) % 100
    return str(2000 + yy if yy <= pivot else 1900 + yy)


def year_for_row(row: dict[str, Any]) -> str:
    date = clean(row.get("filing_date"))
    if date:
        m = re.match(r"^(\d{4})", date)
        if m:
            return m.group(1)
    case_number = clean(row.get("case_number"))
    case_type = clean(row.get("case_type")).lower()
    source = clean(row.get("source")).lower()
    if case_type == "criminal" or source == "sfsc-criminal-portal" or case_number.upper().startswith("CRI"):
        return "unknown"
    return year_from_case_number(case_number) or "unknown"


def slug_for_prefix(prefix: str) -> str:
    if prefix == "(none)":
        return "_none"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix).strip("_")
    return slug or "_none"


def numeric_tail(case_number: str) -> int:
    m = re.search(r"(\d+)(?!.*\d)", case_number)
    return int(m.group(1)) if m else 10**18


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def latest_index_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}

    def completeness(row: dict[str, Any]) -> int:
        return int(row.get("n_entries") or 0) + int(row.get("n_documents") or 0)

    for row in parse_ndjson(path):
        case_number = norm_case(row.get("case_number"))
        if not case_number:
            continue
        row = dict(row)
        row["case_number"] = case_number
        prev = rows.get(case_number)
        if (
            prev is None
            or completeness(row) > completeness(prev)
            or (
                completeness(row) == completeness(prev)
                and clean(row.get("captured_at")) > clean(prev.get("captured_at"))
            )
        ):
            rows[case_number] = row
    return rows


def apply_index_enrichments(
    rows: dict[str, dict[str, Any]],
    paths: Iterable[Path],
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    counts: dict[str, int] = {}
    enrichable = {
        "case_title",
        "charges",
        "charges_json",
        "filing_date",
        "filed_date",
        "case_type",
        "source",
        "source_detail",
        "criminal_case_number",
        "portal_case_id",
    }
    for path in paths:
        count = 0
        for raw in parse_ndjson(path):
            case_number = norm_case(raw.get("case_number"))
            if not case_number:
                continue
            count += 1
            base = rows.setdefault(case_number, {"case_number": case_number})
            for key in enrichable:
                value = raw.get(key)
                if value not in (None, "", []):
                    if key in {"filing_date", "filed_date"}:
                        if not clean(base.get("filing_date")) and not clean(base.get("filed_date")):
                            base["filing_date"] = clean(value)
                            base["filed_date"] = clean(value)
                        continue
                    if key == "case_type" and norm_case(base.get("case_number")).startswith("CRI"):
                        base.setdefault("case_type", "criminal")
                    if base.get(key) in (None, "", []):
                        base[key] = value
            if norm_case(base.get("case_number")).startswith("CRI") and not clean(base.get("case_type")):
                base["case_type"] = "criminal"
        if path.exists():
            counts[rel(path)] = count
    return rows, counts


def case_json_file_numbers(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {norm_case(child.stem) for child in path.glob("*.json") if norm_case(child.stem)}


def int_or_zero(value: Any) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(value)
    except Exception:
        return 0


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return clean(value).lower() in {"1", "true", "yes"}


NON_RESTRICTED_UNAVAILABLE_TOKENS = (
    "no_public_entries",
    "no public entries",
    "no-public-entries",
    "no_entries",
    "no entries",
    "no-entries",
    "no_public_records",
    "no public records",
    "no-public-records",
)


def is_restricted_unavailable(status: Any, reason: Any) -> bool:
    status_text = clean(status).lower()
    if status_text not in {"unavailable", "restricted", "not_public", "not_publicly_available"}:
        return False
    reason_text = clean(reason).lower()
    if no_public_entries_reason(reason_text):
        return False
    return True


def no_public_entries_reason(reason: Any) -> bool:
    reason_text = clean(reason).lower()
    return any(token in reason_text for token in NON_RESTRICTED_UNAVAILABLE_TOKENS)


def criminal_row(row: dict[str, Any]) -> bool:
    return clean(row.get("case_type")).lower() == "criminal" or norm_case(row.get("case_number")).startswith("CRI")


def scanned_empty_criminal_roa(row: dict[str, Any], entries_n: int) -> bool:
    return (
        criminal_row(row)
        and entries_n == 0
        and no_public_entries_reason(row.get("unavailable_reason"))
        and bool(clean(row.get("portal_case_id")))
    )


def case_table_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit(
            f"ERROR: {path} exists but pyarrow is not installed; install pyarrow "
            "or build with full archive/cases JSON rows."
        ) from exc

    parquet_file = pq.ParquetFile(path)
    wanted = [
        "case_number",
        "criminal_case_number",
        "portal_case_id",
        "case_title",
        "charges",
        "charges_json",
        "filing_date",
        "case_type",
        "source",
        "source_detail",
        "captured_at",
        "source_url",
        "case_path",
        "status",
        "unavailable_reason",
        "document_bytes_captured",
        "document_byte_capture_scope",
        "documents_total",
        "documents_bytes_count",
        "documents_unavailable_count",
        "documents_deferred_count",
        "docket_entry_count",
    ]
    columns = [name for name in wanted if name in parquet_file.schema_arrow.names]
    if "case_number" not in columns:
        raise SystemExit(f"ERROR: {path} does not include case_number")

    rows: dict[str, dict[str, Any]] = {}
    for raw in parquet_file.read(columns=columns).to_pylist():
        if not isinstance(raw, dict):
            continue
        case_number = norm_case(raw.get("case_number"))
        if not case_number:
            continue
        unavailable_reason = clean(raw.get("unavailable_reason"))
        status = clean(raw.get("status")).lower()
        case_json = clean(raw.get("case_path")) or f"archive/cases/{case_number}.json"
        rows[case_number] = {
            "case_number": case_number,
            "criminal_case_number": clean(raw.get("criminal_case_number")),
            "portal_case_id": clean(raw.get("portal_case_id")),
            "case_title": clean(raw.get("case_title")),
            "charges": charge_text_from_row(raw),
            "charges_parsed": charge_rows_from_row(raw),
            "filing_date": clean(raw.get("filing_date")),
            "case_type": clean(raw.get("case_type")),
            "source": clean(raw.get("source")),
            "source_detail": clean(raw.get("source_detail")),
            "captured_at": clean(raw.get("captured_at")),
            "status": status,
            "unavailable_reason": unavailable_reason,
            "n_entries": int_or_zero(raw.get("docket_entry_count")),
            "n_documents": int_or_zero(raw.get("documents_total")),
            "documents_bytes_count": int_or_zero(raw.get("documents_bytes_count")),
            "documents_unavailable_count": int_or_zero(raw.get("documents_unavailable_count")),
            "documents_deferred_count": int_or_zero(raw.get("documents_deferred_count")),
            "document_bytes_captured": bool_value(raw.get("document_bytes_captured")),
            "document_byte_capture_scope": clean(raw.get("document_byte_capture_scope")),
            "source_url": clean(raw.get("source_url")),
            "restricted": is_restricted_unavailable(status, unavailable_reason)
            and unavailable_reason != "sealed_or_unavailable_tentative_stub",
            "tentative_stub": unavailable_reason == "sealed_or_unavailable_tentative_stub",
            "case_json": case_json,
        }
    return rows


def case_set_fingerprint(rows: dict[str, dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for case_number in sorted(rows):
        digest.update(case_number.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def case_sample(cases: Iterable[str], limit: int = 10) -> str:
    ordered = sorted(cases)
    sample = ", ".join(ordered[:limit])
    suffix = "" if len(ordered) <= limit else f", ... {len(ordered) - limit} more"
    return f"{sample}{suffix}"


def discovery_rows(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in discovery_input_files(paths):
        if path.suffix.lower() in {".txt", ".list"}:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                case_number = norm_case(line)
                if not case_number or case_number in rows:
                    continue
                rows[case_number] = {
                    "case_number": case_number,
                    "archive_status": "discovered",
                    "source": rel(path),
                }
            continue
        for raw in parse_ndjson(path):
            case_number = norm_case(
                raw.get("case_number")
                or raw.get("caseNumber")
                or raw.get("CASE_NUMBER")
                or raw.get("CaseNum")
                or raw.get("case")
            )
            if not case_number or case_number in rows:
                continue
            row = dict(raw)
            row["case_number"] = case_number
            row["case_title"] = clean(
                raw.get("case_title")
                or raw.get("caseTitle")
                or raw.get("CASE_TITLE")
                or raw.get("title")
                or raw.get("name")
            )
            row["charges"] = charge_text_from_row(raw)
            row["archive_status"] = clean(raw.get("archive_status") or raw.get("status")) or "discovered"
            row["filing_date"] = parse_date(
                raw.get("filing_date")
                or raw.get("filed")
                or raw.get("date_filed")
                or raw.get("date")
            )
            row["charges_parsed"] = charge_rows_from_row(row)
            rows[case_number] = row
    return rows


def case_json_rows(case_dir: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(case_dir.glob("*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(case, dict):
            continue
        case_number = norm_case(case.get("case_number") or path.stem)
        if not case_number:
            continue
        documents = case.get("documents") if isinstance(case.get("documents"), list) else []
        docket_entries = case.get("docket_entries") if isinstance(case.get("docket_entries"), list) else []
        criminal_meta = case.get("criminal") if isinstance(case.get("criminal"), dict) else {}
        source_detail = clean(case.get("source_detail"))
        unavailable_reason = clean(case.get("unavailable_reason"))
        row = {
            "case_number": case_number,
            "case_title": clean(case.get("case_title")),
            "filing_date": filing_date_for_case(case),
            "captured_at": clean(case.get("captured_at")),
            "case_type": clean(case.get("case_type")),
            "criminal_case_number": clean(case.get("criminal_case_number")),
            "portal_case_id": clean(case.get("portal_case_id") or criminal_meta.get("portal_case_id")),
            "charges": charge_text_from_case(case),
            "charges_parsed": charge_rows_from_case(case),
            "source": clean(case.get("source")),
            "source_detail": source_detail,
            "status": clean(case.get("status")),
            "unavailable_reason": unavailable_reason,
            "n_entries": len(docket_entries),
            "n_documents": len(documents),
            "documents_bytes_count": int(case.get("documents_bytes_count") or sum(1 for d in documents if isinstance(d, dict) and d.get("sha256"))),
            "documents_unavailable_count": int(case.get("documents_unavailable_count") or sum(1 for d in documents if isinstance(d, dict) and d.get("is_available") is False)),
            "documents_deferred_count": int(case.get("documents_deferred_count") or sum(1 for d in documents if isinstance(d, dict) and d.get("byte_capture_deferred") is True)),
            "document_bytes_captured": case.get("document_bytes_captured") is True,
            "source_url": clean(case.get("source_url")),
            "restricted": is_restricted_unavailable(case.get("status"), unavailable_reason)
            and unavailable_reason != "sealed_or_unavailable_tentative_stub",
            "tentative_stub": unavailable_reason == "sealed_or_unavailable_tentative_stub",
            "case_json": path.relative_to(ROOT).as_posix(),
        }
        rows[case_number] = row
    return rows


def merge_rows(
    json_rows: dict[str, dict[str, Any]],
    table_rows: dict[str, dict[str, Any]],
    index_rows: dict[str, dict[str, Any]],
    discovered: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for case_number, row in discovered.items():
        merged[case_number] = {
            "case_number": case_number,
            "case_title": clean(row.get("case_title")),
            "charges": charge_text_from_row(row),
            "charges_parsed": charge_rows_from_row(row),
            "filing_date": clean(row.get("filing_date")),
            "archive_status": "discovered",
            "discovered_at": clean(row.get("discovered_at")),
            "discovery_source": clean(row.get("source") or row.get("source_system") or row.get("discovery_source")),
            "source_paths": row.get("source_paths") if isinstance(row.get("source_paths"), list) else [],
        }
    for case_number, row in index_rows.items():
        base = merged.get(case_number, {"case_number": case_number})
        base.update({k: v for k, v in row.items() if v not in (None, "")})
        base.setdefault("case_json", f"archive/cases/{case_number}.json")
        base.pop("archive_status", None)
        merged[case_number] = base
    for case_number, row in table_rows.items():
        base = merged.get(case_number, {"case_number": case_number})
        if base.get("case_title") and not row.get("case_title"):
            title = base.get("case_title")
        else:
            title = row.get("case_title")
        if base.get("filing_date") and not row.get("filing_date"):
            filing_date = base.get("filing_date")
        else:
            filing_date = row.get("filing_date")
        if base.get("charges") and not row.get("charges"):
            charges = base.get("charges")
        else:
            charges = row.get("charges")
        if base.get("charges_parsed") and not row.get("charges_parsed"):
            charge_rows = base.get("charges_parsed")
        else:
            charge_rows = row.get("charges_parsed")
        base.update(row)
        base["case_title"] = clean(title)
        base["filing_date"] = clean(filing_date)
        base["charges"] = clean(charges)
        base["charges_parsed"] = charge_rows if isinstance(charge_rows, list) else []
        base.pop("archive_status", None)
        merged[case_number] = base
    for case_number, row in json_rows.items():
        base = merged.get(case_number, {"case_number": case_number})
        if base.get("case_title") and not row.get("case_title"):
            title = base.get("case_title")
        else:
            title = row.get("case_title")
        if base.get("filing_date") and not row.get("filing_date"):
            filing_date = base.get("filing_date")
        else:
            filing_date = row.get("filing_date")
        if base.get("charges") and not row.get("charges"):
            charges = base.get("charges")
        else:
            charges = row.get("charges")
        if base.get("charges_parsed") and not row.get("charges_parsed"):
            charge_rows = base.get("charges_parsed")
        else:
            charge_rows = row.get("charges_parsed")
        base.update(row)
        base["case_title"] = clean(title)
        base["filing_date"] = clean(filing_date)
        base["charges"] = clean(charges)
        base["charges_parsed"] = charge_rows if isinstance(charge_rows, list) else []
        base.pop("archive_status", None)
        merged[case_number] = base

    out = []
    for row in merged.values():
        case_number = norm_case(row.get("case_number"))
        if not case_number:
            continue
        row["case_number"] = case_number
        row["prefix"] = clean(row.get("prefix")) or prefix_for_case(case_number)
        row["year"] = year_for_row(row)
        row["scan_state"] = scan_state(row)
        out.append(display_row(row))
    return out


def require_case_directory_sources(
    *,
    mode: str,
    json_rows: dict[str, dict[str, Any]],
    table_rows: dict[str, dict[str, Any]],
    index_rows: dict[str, dict[str, Any]],
    case_dir: Path,
    case_table: Path,
    allow_missing: bool,
) -> None:
    if allow_missing:
        return
    if index_rows and case_dir.exists():
        case_files = case_json_file_numbers(case_dir)
        if case_files:
            missing_json = sorted(set(index_rows) - case_files)
            if missing_json:
                sample = ", ".join(missing_json[:10])
                suffix = "" if len(missing_json) <= 10 else f", ... {len(missing_json) - 10} more"
                raise SystemExit(
                    "ERROR: found "
                    f"{len(missing_json)} case-index row(s) without matching tracked case JSON under {case_dir}: "
                    f"{sample}{suffix}. Repair archive/cases before rebuilding the website case directory."
                )
    if table_rows and len(table_rows) < len(index_rows):
        raise SystemExit(
            "ERROR: found "
            f"{len(index_rows)} case-index rows but only {len(table_rows)} rows in {case_table}. "
            "Refusing to build the website case directory from stale compact case data."
        )
    if table_rows and index_rows:
        missing_from_table = set(index_rows) - set(table_rows)
        extra_in_table = set(table_rows) - set(index_rows)
        if missing_from_table or extra_in_table:
            details = []
            if missing_from_table:
                details.append(
                    f"{len(missing_from_table)} case-index case(s) missing from {case_table}: "
                    f"{case_sample(missing_from_table)}"
                )
            if extra_in_table:
                details.append(
                    f"{len(extra_in_table)} stale compact-table case(s) absent from the case index: "
                    f"{case_sample(extra_in_table)}"
                )
            raise SystemExit(
                "ERROR: compact case table and case index contain different case_number sets. "
                + "; ".join(details)
            )
    if table_rows:
        return
    if mode == "none":
        raise SystemExit(
            "ERROR: refusing to build the website case directory with "
            "--case-json-mode none unless a current data/cases.parquet is "
            "available. Full archive/cases/*.json rows or compact case-table "
            "status fields are required so restricted cases are not erased; "
            "pass --allow-missing-case-jsons only for ad hoc debugging."
        )
    if index_rows and not json_rows:
        raise SystemExit(
            "ERROR: found "
            f"{len(index_rows)} case-index rows but 0 full case JSON rows under {case_dir}. "
            f"No current compact case table was available at {case_table}. "
            "Full archive/cases/*.json rows or compact case-table status fields are required "
            "so restricted cases are preserved."
        )


def indexed_fact_row(row: dict[str, Any], docs_n: int, entries_n: int) -> bool:
    if not criminal_row(row):
        return False
    if docs_n != 0 or entries_n != 0:
        return False
    source_text = " ".join(
        clean(row.get(key)).lower()
        for key in (
            "source",
            "source_detail",
            "document_byte_capture_scope",
            "discovery_source",
            "unavailable_reason",
        )
    )
    if clean(row.get("portal_case_id")):
        return False
    index_like_source = "index" in source_text or (
        "criminal-portal-no-documents" in source_text
        and any(token in source_text for token in NON_RESTRICTED_UNAVAILABLE_TOKENS)
    )
    if not index_like_source:
        return False
    return any(
        row.get(key)
        for key in ("case_title", "filing_date", "charges", "charges_parsed", "case_type")
    )


def scan_state(row: dict[str, Any]) -> str:
    if clean(row.get("archive_status")).lower() == "discovered" and not clean(row.get("captured_at")):
        return "discovered"
    if row.get("tentative_stub") and not clean(row.get("captured_at")):
        return "discovered"
    docs = row.get("n_documents")
    try:
        docs_n = int(docs)
    except Exception:
        docs_n = -1
    try:
        entries_n = int(row.get("n_entries") or 0)
    except Exception:
        entries_n = 0
    try:
        bytes_n = int(row.get("documents_bytes_count") or 0)
    except Exception:
        bytes_n = 0
    try:
        deferred_n = int(row.get("documents_deferred_count") or 0)
    except Exception:
        deferred_n = 0
    restricted = bool(row.get("restricted"))
    if clean(row.get("status")) or clean(row.get("unavailable_reason")):
        restricted = is_restricted_unavailable(row.get("status"), row.get("unavailable_reason"))
    if scanned_empty_criminal_roa(row, entries_n):
        return "restricted"
    if indexed_fact_row(row, docs_n, entries_n):
        return "indexed"
    if restricted:
        return "restricted"
    if docs_n == 0:
        if row.get("document_bytes_captured") is True:
            return "no_docs"
        return "summary_only"
    if clean(row.get("document_byte_capture_scope")).lower() == "docket-only" or (docs_n > 0 and bytes_n == 0):
        return "summary_only"
    if deferred_n > 0 and bytes_n == 0:
        return "summary_only"
    if row.get("document_bytes_captured") is True and docs_n > 0 and bytes_n >= docs_n:
        return "complete"
    if (
        row.get("essential_documents_captured") is True
        or clean(row.get("document_coverage")) == "essential"
        or (row.get("has_complaint") is True and row.get("has_orders") is True and row.get("has_appellate_orders") is True)
    ):
        return "core_docs"
    if docs_n > 0 and bytes_n > 0:
        return "partial_docs"
    if clean(row.get("captured_at")):
        return "summary_only"
    return "discovered"


def display_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "case_number",
        "criminal_case_number",
        "portal_case_id",
        "case_title",
        "charges",
        "charges_parsed",
        "filing_date",
        "case_type",
        "source",
        "source_url",
        "year",
        "prefix",
        "scan_state",
    ]
    return {key: row[key] for key in keep if row.get(key) not in (None, "")}


def build_directory(
    rows: list[dict[str, Any]],
    out_dir: Path,
    source_paths: list[str],
    source_counts: dict[str, Any],
) -> dict[str, Any]:
    tmp_dir = out_dir.with_name(out_dir.name + ".tmp")
    if tmp_dir.exists():
        remove_tree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    by_prefix: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_prefix.setdefault(clean(row.get("prefix")) or "(none)", []).append(row)

    prefixes = []
    state_counts: dict[str, int] = {}
    year_total = 0
    case_total = 0
    for prefix, prefix_rows in sorted(by_prefix.items(), key=lambda item: (-len(item[1]), item[0])):
        slug = slug_for_prefix(prefix)
        prefix_dir = tmp_dir / slug
        by_year: dict[str, list[dict[str, Any]]] = {}
        prefix_case_count = 0
        prefix_discovered_count = 0
        prefix_indexed_count = 0
        prefix_restricted_count = 0
        for row in prefix_rows:
            state = clean(row.get("scan_state")) or "indexed"
            state_counts[state] = state_counts.get(state, 0) + 1
            if state in {"discovered", "indexed"}:
                prefix_discovered_count += 1
                if state == "indexed":
                    prefix_indexed_count += 1
            elif state == "restricted":
                prefix_restricted_count += 1
            else:
                prefix_case_count += 1
                case_total += 1
            by_year.setdefault(clean(row.get("year")) or "unknown", []).append(row)

        years = []
        for year, year_rows in sorted(by_year.items(), key=lambda item: (item[0] == "unknown", -int(item[0]) if item[0].isdigit() else 0)):
            year_rows.sort(key=lambda r: (numeric_tail(clean(r.get("case_number"))), clean(r.get("case_number"))))
            year_indexed_count = sum(1 for row in year_rows if clean(row.get("scan_state")) == "indexed")
            year_discovered_count = sum(1 for row in year_rows if clean(row.get("scan_state")) in {"discovered", "indexed"})
            year_restricted_count = sum(1 for row in year_rows if clean(row.get("scan_state")) == "restricted")
            year_case_count = len(year_rows) - year_discovered_count - year_restricted_count
            shard_path = f"{slug}/{year}.ndjson"
            atomic_write_text(
                tmp_dir / shard_path,
                "".join(compact_json(row) + "\n" for row in year_rows),
            )
            years.append({
                "year": year,
                "count": len(year_rows),
                "case_count": year_case_count,
                "discovered_count": year_discovered_count,
                "indexed_count": year_indexed_count,
                "restricted_count": year_restricted_count,
                "path": shard_path,
            })
            year_total += 1

        prefix_manifest = {
            "schema_version": 1,
            "prefix": prefix,
            "slug": slug,
            "count": len(prefix_rows),
            "case_count": prefix_case_count,
            "discovered_count": prefix_discovered_count,
            "indexed_count": prefix_indexed_count,
            "restricted_count": prefix_restricted_count,
            "years": years,
        }
        atomic_write_text(prefix_dir / "manifest.json", json.dumps(prefix_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        prefixes.append({
            "prefix": prefix,
            "slug": slug,
            "count": len(prefix_rows),
            "case_count": prefix_case_count,
            "discovered_count": prefix_discovered_count,
            "indexed_count": prefix_indexed_count,
            "restricted_count": prefix_restricted_count,
            "manifest": f"{slug}/manifest.json",
            "year_count": len(years),
        })

    manifest = {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_commit": git_head(),
        "source_paths": source_paths,
        "source_counts": source_counts,
        "display_row_count": len(rows),
        "case_count": case_total,
        "discovered_count": state_counts.get("discovered", 0) + state_counts.get("indexed", 0),
        "indexed_count": state_counts.get("indexed", 0),
        "restricted_count": state_counts.get("restricted", 0),
        "prefix_count": len(prefixes),
        "year_shard_count": year_total,
        "scan_state_counts": dict(sorted(state_counts.items())),
        "prefixes": prefixes,
    }
    atomic_write_text(tmp_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    make_tree_writable(tmp_dir)
    if out_dir.exists():
        make_tree_writable(out_dir)
        remove_tree(out_dir)
    try:
        tmp_dir.replace(out_dir)
    except PermissionError:
        shutil.copytree(tmp_dir, out_dir)
        remove_tree(tmp_dir)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", type=Path, default=DEFAULT_CASE_DIR)
    parser.add_argument("--case-table", type=Path, default=DEFAULT_CASE_TABLE)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument(
        "--index-enrichment",
        type=Path,
        action="append",
        default=None,
        help=(
            "Additional compact NDJSON case-index facts to merge after "
            "archive/cases-index.ndjson. Defaults to archive/criminal-index-enrichment.ndjson when present."
        ),
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--discovery", type=Path, action="append", default=[])
    parser.add_argument(
        "--case-json-mode",
        choices=("all", "none"),
        default="all",
        help="Read full case JSONs for titles and exact counts, or build only from indexes/discovery.",
    )
    parser.add_argument(
        "--allow-missing-case-jsons",
        action="store_true",
        help=(
            "Allow a build with --case-json-mode none or zero full case JSON rows. "
            "Intended only for ad hoc debugging; normal website builds must not use this."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Build into a temporary directory and discard it after reporting the manifest summary.",
    )
    args = parser.parse_args()

    discovery_paths = args.discovery or DEFAULT_DISCOVERY_PATHS
    index_enrichments = args.index_enrichment if args.index_enrichment is not None else DEFAULT_INDEX_ENRICHMENTS
    json_rows = {} if args.case_json_mode == "none" else case_json_rows(args.case_dir)
    index_rows = latest_index_rows(args.index)
    index_rows, index_enrichment_counts = apply_index_enrichments(index_rows, index_enrichments)
    table_rows = case_table_rows(args.case_table) if args.case_json_mode == "none" or (index_rows and not json_rows) else {}
    require_case_directory_sources(
        mode=args.case_json_mode,
        json_rows=json_rows,
        table_rows=table_rows,
        index_rows=index_rows,
        case_dir=args.case_dir,
        case_table=args.case_table,
        allow_missing=args.allow_missing_case_jsons,
    )
    discovered = discovery_rows(discovery_paths)
    rows = merge_rows(json_rows, table_rows, index_rows, discovered)
    source_paths = []
    if json_rows:
        source_paths.append(rel(args.case_dir / "*.json"))
    if table_rows:
        source_paths.append(rel(args.case_table))
    if args.index.exists():
        source_paths.append(rel(args.index))
    for path in index_enrichments:
        if path.exists():
            source_paths.append(rel(path))
    for path in discovery_paths:
        if path.exists():
            source_paths.append(rel(path))
    source_counts = {
        "case_json_mode": args.case_json_mode,
        "case_json_rows": len(json_rows),
        "case_table_rows": len(table_rows),
        "case_index_rows": len(index_rows),
        "case_index_enrichment_rows": index_enrichment_counts,
        "case_json_fingerprint": case_set_fingerprint(json_rows) if json_rows else "",
        "case_table_fingerprint": case_set_fingerprint(table_rows) if table_rows else "",
        "case_index_fingerprint": case_set_fingerprint(index_rows) if index_rows else "",
        "discovery_rows": len(discovered),
        "display_rows": len(rows),
    }
    if args.check:
        with tempfile.TemporaryDirectory(prefix="sfsc-case-directory-") as tmp:
            manifest = build_directory(rows, Path(tmp) / "case-directory", source_paths, source_counts)
    else:
        manifest = build_directory(rows, args.out_dir, source_paths, source_counts)
    print(
        f"built {manifest['display_row_count']} case-directory rows "
        f"({manifest['case_count']} captured dockets, "
        f"{manifest['restricted_count']} restricted, "
        f"{manifest['indexed_count']} indexed, "
        f"{manifest['discovered_count']} discovered) across "
        f"{manifest['prefix_count']} prefixes and {manifest['year_shard_count']} year shards"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
