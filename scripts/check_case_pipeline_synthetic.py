#!/usr/bin/env python3
"""Synthetic checks for compact case-table and case-directory invariants."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_case_directory as bcd  # noqa: E402
import build_case_tables as bct  # noqa: E402
import import_scanner_cases as importer  # noqa: E402


FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_index(path: Path, cases: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for case in cases:
        row = {
            "case_number": bcd.norm_case(case.get("case_number")),
            "case_title": case.get("case_title"),
            "captured_at": case.get("captured_at"),
            "n_entries": len(case.get("docket_entries") or []),
            "n_documents": len(case.get("documents") or []),
            "source_url": case.get("source_url"),
        }
        for key in ("case_type", "criminal_case_number", "portal_case_id", "source"):
            if case.get(key):
                row[key] = case[key]
        lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_compact_criminal_directory() -> None:
    print("\ncompact criminal case directory")
    criminal = {
        "case_number": "CRI400001",
        "case_title": "San Francisco criminal case 24000001",
        "case_type": "criminal",
        "criminal_case_number": "24000001",
        "portal_case_id": "CR-PORTAL-1",
        "source": "sfsc-criminal-portal",
        "captured_at": "2026-06-20T00:00:00Z",
        "source_url": "https://webapps.sftc.org/crimportal/crimportal.dll?name=CR-PORTAL-1",
        "docket_entries": [{"description": "Complaint filed"}],
        "calendar": [
            {
                "court_date": "2026-07-01",
                "hearing_time": "09:00 AM",
                "hearing_type": "Arraignment",
                "department": "16",
                "location": "Hall of Justice",
            }
        ],
        "parties": [{"name": "PEOPLE OF THE STATE OF CALIFORNIA", "party_type": "Plaintiff"}],
        "attorneys": [],
        "documents": [],
        "document_bytes_captured": True,
        "document_byte_capture_scope": "criminal-portal-no-documents",
    }
    civil = {
        "case_number": "CGC-24-000001",
        "case_title": "Acme v. Roe",
        "filing_date": "2024-01-02",
        "captured_at": "2026-06-20T00:00:00Z",
        "docket_entries": [{"date_filed": "2024-01-02", "description": "Complaint filed"}],
        "documents": [{"sha256": "abc"}],
        "document_bytes_captured": True,
    }
    with tempfile.TemporaryDirectory(prefix="sfsc-case-pipeline-") as tmp:
        root = Path(tmp)
        case_dir = root / "archive" / "cases"
        for case in (criminal, civil):
            write_json(case_dir / f"{bcd.norm_case(case['case_number'])}.json", case)

        tables = bct.rows_from_cases(case_dir)
        cases = {row["case_number"]: row for row in tables["cases"]}
        check("criminal case_type preserved in cases table", cases["CRI400001"]["case_type"] == "criminal")
        check("criminal number preserved in cases table", cases["CRI400001"]["criminal_case_number"] == "24000001")
        check("portal id preserved in cases table", cases["CRI400001"]["portal_case_id"] == "CR-PORTAL-1")
        check("criminal source preserved in cases table", cases["CRI400001"]["source"] == "sfsc-criminal-portal")
        cal = [row for row in tables["calendar"] if row["case_number"] == "CRI400001"][0]
        check("criminal hearing time normalized", cal["hearing_time"] == "09:00 AM")
        check("criminal department normalized", cal["department"] == "16")
        check(
            "criminal filed_date feeds compact filing date",
            bct.filing_date_for_case({"case_type": "criminal", "filed_date": "06/20/2024"}) == "2024-06-20",
        )

        import pandas as pd

        table_path = root / "data" / "cases.parquet"
        bct.write_parquet_atomic(table_path, pd.DataFrame(tables["cases"]))
        index_path = root / "archive" / "cases-index.ndjson"
        write_index(index_path, [criminal, civil])

        index_rows = bcd.latest_index_rows(index_path)
        table_rows = bcd.case_table_rows(table_path)
        bcd.require_case_directory_sources(
            mode="none",
            json_rows={},
            table_rows=table_rows,
            index_rows=index_rows,
            case_dir=case_dir,
            case_table=table_path,
            allow_missing=False,
        )
        rows = bcd.merge_rows({}, table_rows, index_rows, {})
        source_counts = {
            "case_json_mode": "none",
            "case_json_rows": 0,
            "case_table_rows": len(table_rows),
            "case_index_rows": len(index_rows),
            "case_json_fingerprint": "",
            "case_table_fingerprint": bcd.case_set_fingerprint(table_rows),
            "case_index_fingerprint": bcd.case_set_fingerprint(index_rows),
            "discovery_rows": 0,
            "display_rows": len(rows),
        }
        out_dir = root / "archive" / "case-directory"
        manifest = bcd.build_directory(rows, out_dir, ["data/cases.parquet", "archive/cases-index.ndjson"], source_counts)
        check("directory manifest has compact fingerprint", bool(manifest["source_counts"]["case_table_fingerprint"]))
        generated = {row["case_number"]: row for row in rows}
        criminal_row = generated["CRI400001"]
        check("criminal id survives compact directory", criminal_row.get("criminal_case_number") == "24000001")
        check("portal id survives compact directory", criminal_row.get("portal_case_id") == "CR-PORTAL-1")
        check("case_type survives compact directory", criminal_row.get("case_type") == "criminal")
        check("source survives compact directory", criminal_row.get("source") == "sfsc-criminal-portal")
        check("CRI case without filing date uses unknown shard", criminal_row.get("year") == "unknown", str(criminal_row))

        result = __import__("check_case_directory").check_case_directory(
            out_dir,
            discovery_feeds=[],
            case_index=index_path,
            case_dir=None,
        )
        check("generated compact directory validates", not result["failures"], "\n".join(result["failures"]))


def test_stale_compact_table_rejected() -> None:
    print("\nstale compact table guard")
    try:
        bcd.require_case_directory_sources(
            mode="none",
            json_rows={},
            table_rows={"CGC24000001": {"case_number": "CGC24000001"}},
            index_rows={"CGC24000002": {"case_number": "CGC24000002"}},
            case_dir=Path("__missing_synthetic_case_dir__"),
            case_table=Path("data/cases.parquet"),
            allow_missing=False,
        )
    except SystemExit as exc:
        check("same-count set mismatch rejected", "different case_number sets" in str(exc), str(exc))
    else:
        check("same-count set mismatch rejected", False)


def test_criminal_importer_guards() -> None:
    print("\ncriminal importer guards")
    ambiguous = {
        "schema": "sfsc-criminal-portal-case-v1",
        "case_type": "criminal",
        "case_number": "CRI24000004",
        "criminal_case_number": "24000004",
        "status": "search_rows_without_case_id",
        "search": {"rows": [{"name": "SMITH"}]},
        "docket_entries": [],
        "documents": [],
    }
    check(
        "search rows without CaseId are not restriction proof",
        importer.criminal_unavailable_text(ambiguous) == "",
        importer.criminal_unavailable_text(ambiguous),
    )
    check("ambiguous criminal search rows fail schema", importer.schema_error(ambiguous) == "criminal_case_not_found")

    deduped = importer.normalize_case_data({
        "schema": "sfsc-criminal-portal-case-v1",
        "case_type": "criminal",
        "case_number": "CRI24000005",
        "criminal_case_number": "24000005",
        "portal_case_id": "abc",
        "roa": [{"docketEntryComment": "Complaint filed PC 459"}],
        "docket_entries": [{"description": "Complaint filed PC 459"}],
        "documents": [],
    })
    statute = next((row for row in deduped.get("criminal", {}).get("statutes", []) if row.get("code") == "PC 459"), {})
    check("duplicate scanner ROA/docket statute line counted once", statute.get("count") == 1, str(statute))

    headered = importer.normalize_case_data({
        "schema": "sfsc-criminal-portal-case-v1",
        "case_type": "criminal",
        "case_number": "CRI24000006",
        "criminal_case_number": "24000006",
        "portal_case_id": "def",
        "case_title": "San Francisco criminal case 24000006",
        "case_header": {
            "case_number": "24000006",
            "defendant": "DOE, JANE",
            "case_type": "Felony",
            "filed_date": "06/20/2024",
        },
        "roa": [],
        "documents": [],
    })
    check("criminal header defendant makes People v title", headered.get("case_title") == "People v. DOE, JANE")
    check("criminal header filed_date preserved", headered.get("filed_date") == "06/20/2024")
    check("criminal header-only record is unavailable", headered.get("status") == "unavailable", str(headered))
    check(
        "criminal header-only reason is no public entries",
        headered.get("unavailable_reason") == "criminal_portal_no_public_entries",
        headered.get("unavailable_reason"),
    )
    check(
        "criminal header-only text names available facts",
        headered.get("unavailable_text") == "No information available besides the name of the defendant, DOE, JANE, and date of filing 06/20/2024.",
        headered.get("unavailable_text"),
    )
    check("criminal header-only record passes schema", importer.schema_error(headered) == "", importer.schema_error(headered))
    check(
        "criminal header defendant becomes party",
        any(
            row.get("name") == "DOE, JANE" and row.get("party_type") == "Defendant"
            for row in headered.get("parties", [])
        ),
    )


def main() -> int:
    test_compact_criminal_directory()
    test_stale_compact_table_rejected()
    test_criminal_importer_guards()
    if FAILURES:
        print(f"\nRESULT: FAIL ({len(FAILURES)} failure(s))")
        return 1
    print("\ncase pipeline synthetic checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
