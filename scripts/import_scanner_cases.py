#!/usr/bin/env python3
"""Promote local headless-scanner case JSON into the committed archive.

The scanner intentionally writes to .scanner/cases so raw harvesting can run
without GitHub credentials. This script is the explicit bridge from that local
cache into archive/cases plus the append-only cases index.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from tempfile import NamedTemporaryFile

import document_storage as storage


ROOT = Path(__file__).resolve().parents[1]
CRIMINAL_PORTAL_SCHEMA = "sfsc-criminal-portal-case-v1"
CRIMINAL_PORTAL_SOURCE = "sftc-criminal-portal"
CRIMINAL_PORTAL_URL = "https://webapps.sftc.org/crimportal/crimportal.dll"
CRIMINAL_SESSION_RE = re.compile(r"([?&]SessionID=)[^&#]+", re.I)
STATUTE_RE = re.compile(
    r"\b(?:PC|PEN(?:AL)?\s+CODE|HS|VC|BP|CC|GC)\s*(?:§|SECTION|SEC\.)?\s*"
    r"\d+[A-Za-z]?(?:\.\d+)?(?:\([^)]+\))*",
    re.I,
)
PROCEDURAL_STATUTE_RE = re.compile(
    r"\bPC\s*(?:1001\.3[56]|1001\.95|1538\.5|1050|1203\.2|1369|1370|1382|1385|1417|3000\.08|3455|4011(?:\.6)?)\b",
    re.I,
)


def norm_case(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("<br>", " ")).strip()


def is_criminal_portal_case(data: dict) -> bool:
    return (
        clean(data.get("schema")).lower() == CRIMINAL_PORTAL_SCHEMA
        or clean(data.get("source")).lower() == CRIMINAL_PORTAL_SOURCE
        or clean(data.get("case_type")).lower() == "criminal"
        or bool(clean(data.get("criminal_case_number")))
    )


def criminal_raw_number(data: dict, fallback: str = "") -> str:
    direct = clean(data.get("criminal_case_number") or data.get("criminalCaseNumber"))
    if direct:
        return re.sub(r"[^0-9]", "", direct)
    case_number = clean(data.get("case_number") or fallback)
    m = re.match(r"^CRI[-_\s]*(\d{6,})$", case_number, re.I)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{6,}", clean(fallback)):
        return clean(fallback)
    return ""


def criminal_archive_case_number(data: dict, fallback: str = "") -> str:
    existing = norm_case(data.get("case_number"))
    if re.fullmatch(r"CRI\d{6,}", existing):
        return existing
    raw = criminal_raw_number(data, fallback)
    return f"CRI{raw}" if raw else existing


def redact_criminal_portal_url(value: object) -> str:
    raw = clean(value)
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(k, v) for k, v in query if k.lower() != "sessionid"]
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    except Exception:
        return CRIMINAL_SESSION_RE.sub(r"\1[redacted]", raw)


def criminal_source_url(data: dict) -> str:
    for key in ("source_url", "court_url", "url"):
        url = redact_criminal_portal_url(data.get(key))
        if url:
            return url
    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    redirect = redact_criminal_portal_url(search.get("redirect"))
    if redirect:
        return redirect
    portal_id = clean(data.get("portal_case_id") or data.get("portalCaseId"))
    if portal_id:
        return f"{CRIMINAL_PORTAL_URL}?CaseId={urllib.parse.quote(portal_id)}"
    return CRIMINAL_PORTAL_URL


def first_text(row: dict, *keys: str) -> str:
    for key in keys:
        value = clean(row.get(key))
        if value:
            return value
    return ""


def split_criminal_start_time(value: object) -> tuple[str, str]:
    raw = clean(value)
    if not raw:
        return "", ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T\s]+(.+))?$", raw)
    if m:
        return m.group(1), clean(m.group(2))
    m = re.match(r"^(\d{1,2}/\d{1,2}/\d{2,4})(?:\s+(.+))?$", raw)
    if m:
        return m.group(1), clean(m.group(2))
    return raw, ""


def normalize_criminal_docket_rows(data: dict) -> list[dict]:
    rows = data.get("roa") if isinstance(data.get("roa"), list) else []
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["__index"] = index
        item["date_filed"] = first_text(row, "date_filed", "filedDate", "FILEDATE", "filed", "date")
        item["description"] = first_text(row, "description", "docketEntryComment", "RTEXT", "text", "title")
        item["submitter"] = first_text(row, "submitter", "otherSubmitter")
        item["source"] = "criminal_portal_roa"
        out.append(item)
    return out


def normalize_criminal_calendar_rows(data: dict) -> list[dict]:
    rows = data.get("calendar") if isinstance(data.get("calendar"), list) else []
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        court_date, hearing_time = split_criminal_start_time(
            first_text(row, "court_date", "startTime", "date", "start_time")
        )
        item = dict(row)
        item["__index"] = index
        item["court_date"] = first_text(row, "court_date", "date") or court_date
        item["hearing_time"] = first_text(row, "hearing_time", "time") or hearing_time
        item["matters"] = first_text(row, "matters", "hearingType", "calendar_matter", "description")
        item["hearing_type"] = first_text(row, "hearing_type", "hearingType")
        item["location"] = first_text(row, "location", "room")
        item["department"] = first_text(row, "department", "dept")
        item["source"] = "criminal_portal_calendar"
        out.append(item)
    return out


def statute_code(value: str) -> str:
    value = re.sub(r"\bPEN(?:AL)?\s+CODE\b", "PC", value, flags=re.I)
    value = re.sub(r"\bSECTION\b|\bSEC\.\b|§", "", value, flags=re.I)
    return clean(value).upper()


def normalize_criminal_statutes(data: dict, docket_entries: list[dict]) -> list[dict]:
    hits: dict[str, dict] = {}
    seen_lines: set[str] = set()

    def add_text(source: str, value: object) -> None:
        line = clean(value)
        if not line:
            return
        line_key = line.upper()
        if line_key in seen_lines:
            return
        seen_lines.add(line_key)
        for match in STATUTE_RE.finditer(line):
            code = statute_code(match.group(0))
            if not code:
                continue
            prev = hits.setdefault(
                code,
                {
                    "code": code,
                    "count": 0,
                    "sources": [],
                    "classification": "procedural" if PROCEDURAL_STATUTE_RE.search(code) else "unknown",
                },
            )
            prev["count"] += 1
            if source not in prev["sources"]:
                prev["sources"].append(source)
            if prev["classification"] != "procedural" and re.search(
                r"\b(?:complaint|information|indictment|charge|plea)\b", line, re.I
            ):
                prev["classification"] = "charge_candidate"

    raw_roa = data.get("roa") if isinstance(data.get("roa"), list) else []
    for row in raw_roa:
        if isinstance(row, dict):
            add_text("roa", row.get("docketEntryComment") or row.get("description") or row.get("text"))
    for row in docket_entries:
        if isinstance(row, dict):
            add_text("docket_entries", row.get("description") or row.get("text") or row.get("title"))
    return sorted(hits.values(), key=lambda row: row["code"])


def criminal_unavailable_text(data: dict) -> str:
    status = clean(data.get("status")).lower()
    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    messages = [
        data.get("message"),
        data.get("unavailable_text"),
        data.get("unavailable_reason"),
        search.get("message"),
    ]
    rows = search.get("rows")
    if isinstance(rows, list):
        messages.extend(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows if isinstance(row, dict))
    joined = clean(" ".join(clean(value) for value in messages if clean(value)))
    if (
        status in {"unavailable", "restricted", "not_public", "not_publicly_available"}
        or re.search(r"\b(?:not\s+public(?:ly)?\s+available|confidential|sealed|restricted|not\s+available\s+to\s+the\s+public)\b", joined, re.I)
    ):
        return joined or "Criminal portal indicates this case is not publicly available."
    return ""


def criminal_no_information_text(defendant: str = "", filed_date: str = "") -> str:
    defendant = clean(defendant)
    filed_date = clean(filed_date)
    if defendant and filed_date:
        return f"No information available besides the name of the defendant, {defendant}, and date of filing {filed_date}."
    if defendant:
        return f"No information available besides the name of the defendant, {defendant}."
    if filed_date:
        return f"No information available besides date of filing {filed_date}."
    return "No information available."


def criminal_case_exists(data: dict) -> bool:
    if clean(data.get("portal_case_id") or data.get("portalCaseId")):
        return True
    if criminal_unavailable_text(data):
        return True
    status = clean(data.get("status")).lower()
    return status in {"found", "unavailable", "restricted", "not_public", "not_publicly_available"}


def normalize_case_data(data: dict, fallback_stem: str = "") -> dict:
    if not is_criminal_portal_case(data):
        return data
    case_number = criminal_archive_case_number(data, fallback_stem)
    raw_number = criminal_raw_number(data, fallback_stem)
    case_header = data.get("case_header") if isinstance(data.get("case_header"), dict) else {}
    defendant = clean(data.get("defendant") or case_header.get("defendant"))
    filed_date = clean(data.get("filed_date") or case_header.get("filed_date"))
    display_case_number = clean(data.get("display_case_number") or case_header.get("case_number"))
    criminal_case_type = clean(data.get("criminal_case_type") or case_header.get("case_type"))
    criminal_title = clean(data.get("case_title") or data.get("title"))
    if defendant and (
        not criminal_title
        or re.fullmatch(r"San Francisco criminal case\s+\d+", criminal_title, re.I)
        or criminal_title.upper() == defendant.upper()
    ):
        criminal_title = f"People v. {defendant}"
    docket_entries = data.get("docket_entries") if isinstance(data.get("docket_entries"), list) else normalize_criminal_docket_rows(data)
    calendar = normalize_criminal_calendar_rows(data)
    statutes = normalize_criminal_statutes(data, docket_entries)
    unavailable_text = criminal_unavailable_text(data)
    unavailable_reason = clean(data.get("unavailable_reason")) if unavailable_text else ""
    parties = data.get("parties") if isinstance(data.get("parties"), list) else (
        [{"name": defendant, "party_type": "Defendant", "source": "criminal_portal_case_header"}]
        if defendant
        else []
    )
    attorneys = data.get("attorneys") if isinstance(data.get("attorneys"), list) else []
    documents = data.get("documents") if isinstance(data.get("documents"), list) else []
    payments = data.get("payments") if isinstance(data.get("payments"), list) else []
    no_public_rows = not docket_entries and not calendar and not attorneys and not documents and not payments
    if not unavailable_text and no_public_rows and (defendant or filed_date):
        unavailable_text = criminal_no_information_text(defendant, filed_date)
        unavailable_reason = "criminal_portal_no_public_entries"
    criminal = data.get("criminal") if isinstance(data.get("criminal"), dict) else {}
    criminal = {
        **criminal,
        "raw_case_number": raw_number,
        "portal_case_id": clean(data.get("portal_case_id") or data.get("portalCaseId")),
        "display_case_number": display_case_number,
        "defendant": defendant,
        "filed_date": filed_date,
        "case_type": criminal_case_type,
        "case_header": case_header,
        "statutes": statutes,
        "inferred_charges": [
            {**row, "inference": "tentative_from_criminal_docket_text"}
            for row in statutes
            if row.get("classification") == "charge_candidate"
        ],
    }
    out = dict(data)
    search = out.get("search") if isinstance(out.get("search"), dict) else {}
    if search:
        out["search"] = {
            **search,
            "redirect": redact_criminal_portal_url(search.get("redirect")) or search.get("redirect"),
        }
    for url_key in ("source_url", "court_url", "url"):
        if out.get(url_key):
            out[url_key] = redact_criminal_portal_url(out.get(url_key))
    out.update(
        {
            "schema": data.get("schema") or CRIMINAL_PORTAL_SCHEMA,
            "source": CRIMINAL_PORTAL_SOURCE,
            "case_type": "criminal",
            "case_number": case_number,
            "criminal_case_number": raw_number,
            "display_case_number": display_case_number,
            "defendant": defendant,
            "filed_date": filed_date,
            "criminal_case_type": criminal_case_type,
            "case_header": case_header,
            "case_title": criminal_title
            or (f"San Francisco criminal case {raw_number}" if raw_number else "San Francisco criminal case"),
            "court": clean(data.get("court")) or "San Francisco Superior Court - Criminal",
            "cause_of_action": clean(data.get("cause_of_action") or data.get("cause")) or "Criminal",
            "source_url": criminal_source_url(data),
            "docket_entries": docket_entries,
            "calendar": calendar,
            "parties": parties,
            "attorneys": attorneys,
            "documents": documents,
            "payments": payments,
            "document_bytes_captured": data.get("document_bytes_captured") is True
            or not documents,
            "document_byte_capture_scope": "criminal-portal-no-documents"
            if not documents
            else data.get("document_byte_capture_scope"),
            "criminal": criminal,
        }
    )
    if unavailable_text:
        out["status"] = "unavailable"
        out["unavailable_reason"] = unavailable_reason or "criminal_portal_not_publicly_available"
        out["unavailable_text"] = unavailable_text
    return out


def read_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"skip invalid {path}: {exc}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as f:
        tmp = Path(f.name)
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def append_index(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    by_case: dict[str, dict] = {}
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as existing_fh:
            for lineno, line in enumerate(existing_fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{lineno}: invalid JSON in case index: {exc}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{lineno}: row is not an object")
                case_number = norm_case(row.get("case_number"))
                if not case_number:
                    raise ValueError(f"{path}:{lineno}: row missing case_number")
                row["case_number"] = case_number
                by_case.pop(case_number, None)
                by_case[case_number] = row
    for record in records:
        case_number = norm_case(record.get("case_number"))
        if not case_number:
            continue
        row = dict(record)
        row["case_number"] = case_number
        by_case.pop(case_number, None)
        by_case[case_number] = row
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as f:
        tmp = Path(f.name)
        for record in by_case.values():
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def schema_error(data: dict) -> str:
    if is_criminal_portal_case(data) and not criminal_case_exists(data):
        return "criminal_case_not_found"
    if not norm_case(data.get("case_number")):
        return "missing_case_number"
    if data.get("status") == "unavailable":
        return ""
    if not isinstance(data.get("docket_entries"), list):
        return "docket_entries_not_list"
    if not isinstance(data.get("documents"), list):
        return "documents_not_list"
    return ""


def summary_record(data: dict, case_number: str) -> dict:
    docket_entries = data.get("docket_entries")
    documents = data.get("documents")
    docs = documents if isinstance(documents, list) else []
    record = {
        "case_number": case_number,
        "captured_at": data.get("captured_at"),
        "n_entries": len(docket_entries) if isinstance(docket_entries, list) else 0,
        "n_documents": len(docs),
        "documents_bytes_count": sum(1 for doc in docs if isinstance(doc, dict) and doc.get("sha256")),
        "documents_unavailable_count": sum(
            1 for doc in docs if isinstance(doc, dict) and doc.get("is_available") is False
        ),
        "documents_deferred_count": sum(
            1 for doc in docs if isinstance(doc, dict) and storage.doc_byte_deferred(doc)
        ),
        "document_bytes_captured": has_complete_document_assets(data),
        "source_url": data.get("source_url"),
    }
    if data.get("case_type"):
        record["case_type"] = data.get("case_type")
    if data.get("criminal_case_number"):
        record["criminal_case_number"] = data.get("criminal_case_number")
    if data.get("portal_case_id"):
        record["portal_case_id"] = data.get("portal_case_id")
    if data.get("source"):
        record["source"] = data.get("source")
    return record


def docket_indicates_documents(data: dict) -> bool:
    entries = data.get("docket_entries")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("has_document") is True or entry.get("doc_id"):
            return True
        for key in ("document_url", "view_url", "url"):
            value = str(entry.get(key) or "")
            if "CaseInfo.dll" in value or "imgquery" in value:
                return True
    return False


def has_complete_document_assets(data: dict) -> bool:
    if data.get("status") == "unavailable":
        return True
    documents = data.get("documents")
    # Byte-first invariant: a full capture ALWAYS writes a `documents` array (the
    # scanner sets `documents: docs`, possibly empty). A MISSING `documents` key
    # therefore means document enumeration never ran (metadata-only capture) and
    # the case must NOT be treated as complete. An empty list is complete only
    # when byte capture was marked complete and the docket advertises no docs.
    if not isinstance(documents, list):
        return False
    if not documents:
        return data.get("document_bytes_captured") is True and not docket_indicates_documents(data)
    for doc in documents:
        if not isinstance(doc, dict):
            return False
        if doc.get("is_available") is False:
            continue
        if storage.doc_byte_deferred(doc):
            continue
        if not storage.doc_has_archived_object(doc):
            return False
    return True


def existing_archive_is_complete(dest: Path) -> bool:
    data = read_json(dest)
    return bool(data and has_complete_document_assets(data))


def import_cases(args: argparse.Namespace) -> int:
    scanner_dir = args.scanner_dir
    archive_dir = args.archive_dir
    index_path = args.index
    imported: list[dict] = []
    stats = {
        "imported": 0,
        "skipped_existing": 0,
        "skipped_error_files": 0,
        "skipped_invalid": 0,
        "skipped_schema_invalid": 0,
        "skipped_case_mismatch": 0,
        "skipped_incomplete_documents": 0,
    }

    if not scanner_dir.exists():
        raise SystemExit(f"scanner dir does not exist: {scanner_dir}")

    for src in sorted(scanner_dir.glob("*.json")):
        if src.name.endswith(".error.json"):
            stats["skipped_error_files"] += 1
            continue
        data = read_json(src)
        if data is None:
            stats["skipped_invalid"] += 1
            continue
        data = normalize_case_data(data, src.stem)
        case_number = norm_case(data.get("case_number") or src.stem)
        src_case_number = norm_case(src.stem)
        if is_criminal_portal_case(data):
            raw_number = criminal_raw_number(data, src.stem)
            source_matches = src_case_number in {case_number, norm_case(raw_number)}
        else:
            source_matches = case_number == src_case_number
        if not case_number or not source_matches:
            stats["skipped_case_mismatch"] += 1
            print(f"skip case mismatch {src}: {data.get('case_number')!r}", file=sys.stderr)
            continue
        schema_reason = schema_error(data)
        if schema_reason:
            stats["skipped_schema_invalid"] += 1
            print(f"skip schema invalid {src}: {schema_reason}", file=sys.stderr)
            continue
        if not has_complete_document_assets(data):
            stats["skipped_incomplete_documents"] += 1
            print(f"skip incomplete document assets {src}", file=sys.stderr)
            continue
        dest = archive_dir / f"{case_number}.json"
        if dest.exists() and not args.overwrite_existing:
            if existing_archive_is_complete(dest):
                stats["skipped_existing"] += 1
                continue
            stats.setdefault("replaced_incomplete_existing", 0)
            stats["replaced_incomplete_existing"] += 1
        stats["imported"] += 1
        imported.append(summary_record(data, case_number))
        if not args.dry_run:
            write_json_atomic(dest, data)

    if not args.dry_run:
        append_index(index_path, imported)

    for key, value in stats.items():
        print(f"{key}: {value}")
    if args.dry_run:
        print("dry_run: true")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scanner-dir",
        type=Path,
        default=ROOT / ".scanner" / "cases",
        help="Directory containing local_case_scanner JSON output.",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=ROOT / "archive" / "cases",
        help="Committed archive/cases directory.",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=ROOT / "archive" / "cases-index.ndjson",
        help="Append-only archive cases index.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Replace archive/cases files that already exist.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.scanner_dir = args.scanner_dir.resolve()
    args.archive_dir = args.archive_dir.resolve()
    args.index = args.index.resolve()
    return args


if __name__ == "__main__":
    raise SystemExit(import_cases(parse_args(sys.argv[1:])))
