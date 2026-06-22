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
        "charges": "Robbery PC 211; 245(a)(1) PC/F Assault with a deadly weapon",
        "charges_parsed": [
            {
                "raw": "Robbery PC 211",
                "title": "Robbery",
                "code": "PC 211",
                "code_system": "PC",
                "section": "211",
                "citation": "Penal Code § 211",
                "url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=211.&lawCode=PEN",
            },
            {
                "raw": "245(a)(1) PC/F Assault with a deadly weapon",
                "title": "Assault with a deadly weapon",
                "code": "PC 245(a)(1)",
                "code_system": "PC",
                "section": "245(a)(1)",
                "citation": "Penal Code § 245(a)(1)",
                "url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=245.&lawCode=PEN",
                "classification": "felony",
            },
        ],
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
    criminal_index_stub = {
        "case_number": "CRI400002",
        "case_title": "People v. INDEXED PERSON",
        "case_type": "criminal",
        "criminal_case_number": "24000002",
        "portal_case_id": "",
        "charges": "PC 459/F Burglary",
        "filing_date": "2024-02-03",
        "source": "sftc-criminal-portal",
        "captured_at": "2026-06-20T00:00:00Z",
        "source_url": "https://webapps.sftc.org/crimportal/crimportal.dll",
        "docket_entries": [],
        "documents": [],
    }
    criminal_empty_roa = {
        "case_number": "CRI400003",
        "case_title": "People v. EMPTY ROA",
        "case_type": "criminal",
        "criminal_case_number": "24000003",
        "portal_case_id": "CR-EMPTY-ROA",
        "filing_date": "2024-02-04",
        "source": "sftc-criminal-portal",
        "captured_at": "2026-06-20T00:00:00Z",
        "source_url": "https://webapps.sftc.org/crimportal/crimportal.dll?CaseId=CR-EMPTY-ROA",
        "docket_entries": [],
        "documents": [],
    }
    with tempfile.TemporaryDirectory(prefix="sfsc-case-pipeline-") as tmp:
        root = Path(tmp)
        case_dir = root / "archive" / "cases"
        for case in (criminal, civil, criminal_index_stub, criminal_empty_roa):
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

        tables["cases"].extend([
            {
                "case_number": bcd.norm_case(criminal_index_stub["case_number"]),
                "case_title": criminal_index_stub["case_title"],
                "filing_date": criminal_index_stub["filing_date"],
                "case_type": "criminal",
                "criminal_case_number": criminal_index_stub["criminal_case_number"],
                "portal_case_id": "",
                "charges": criminal_index_stub["charges"],
                "source": "sftc-criminal-portal",
                "captured_at": criminal_index_stub["captured_at"],
                "source_url": criminal_index_stub["source_url"],
                "status": "unavailable",
                "unavailable_reason": "criminal_portal_no_public_entries",
                "document_bytes_captured": True,
                "document_byte_capture_scope": "criminal-portal-no-documents",
                "documents_total": 0,
                "documents_bytes_count": 0,
                "documents_unavailable_count": 0,
                "documents_deferred_count": 0,
                "docket_entry_count": 0,
            },
            {
                "case_number": bcd.norm_case(criminal_empty_roa["case_number"]),
                "case_title": criminal_empty_roa["case_title"],
                "filing_date": criminal_empty_roa["filing_date"],
                "case_type": "criminal",
                "criminal_case_number": criminal_empty_roa["criminal_case_number"],
                "portal_case_id": criminal_empty_roa["portal_case_id"],
                "source": "sftc-criminal-portal",
                "captured_at": criminal_empty_roa["captured_at"],
                "source_url": criminal_empty_roa["source_url"],
                "status": "unavailable",
                "unavailable_reason": "criminal_portal_no_public_entries",
                "document_bytes_captured": True,
                "document_byte_capture_scope": "criminal-portal-no-documents",
                "documents_total": 0,
                "documents_bytes_count": 0,
                "documents_unavailable_count": 0,
                "documents_deferred_count": 0,
                "docket_entry_count": 0,
            },
        ])
        table_path = root / "data" / "cases.parquet"
        bct.write_parquet_atomic(table_path, pd.DataFrame(tables["cases"]))
        index_path = root / "archive" / "cases-index.ndjson"
        write_index(index_path, [criminal, civil, criminal_index_stub, criminal_empty_roa])

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
        check("charges survive compact directory", criminal_row.get("charges") == criminal["charges"], str(criminal_row))
        check("parsed charges survive compact directory", len(criminal_row.get("charges_parsed") or []) == 2, str(criminal_row))
        check("case_type survives compact directory", criminal_row.get("case_type") == "criminal")
        check("source survives compact directory", criminal_row.get("source") == "sfsc-criminal-portal")
        check("CRI case without filing date uses unknown shard", criminal_row.get("year") == "unknown", str(criminal_row))
        check("criminal index stub is indexed", generated["CRI400002"].get("scan_state") == "indexed", str(generated["CRI400002"]))
        check("scanned empty criminal ROA is restricted", generated["CRI400003"].get("scan_state") == "restricted", str(generated["CRI400003"]))
        check("indexed count is reported", manifest.get("indexed_count") == 1, str(manifest))
        check("discovered count includes indexed", manifest.get("discovered_count") == 1, str(manifest))

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
        "charges": "Robbery PC 211; 245(a)(1) PC/F Assault with a deadly weapon",
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
        headered.get("unavailable_text") == "No information available besides the name of the defendant, DOE, JANE, date of filing, 06/20/2024, and charges in the case: Robbery PC 211; 245(a)(1) PC/F Assault with a deadly weapon.",
        headered.get("unavailable_text"),
    )
    parsed_charges = headered.get("charges_parsed") or []
    check("criminal index charges split into rows", len(parsed_charges) == 2, str(parsed_charges))
    check("criminal charge row has citation", parsed_charges[0].get("citation") == "Penal Code § 211", str(parsed_charges))
    check("reverse charge citation parsed", parsed_charges[1].get("citation") == "Penal Code § 245(a)(1)", str(parsed_charges))
    check("reverse charge title parsed", parsed_charges[1].get("title") == "Assault with a deadly weapon", str(parsed_charges))
    real_shape = importer.parse_charge_rows("459 PC/F 459 PC/F 8888888 466 PC/M")
    check("space-separated real charge string dedupes statutes", [row.get("code") for row in real_shape if row.get("code")] == ["PC 459", "PC 466"], str(real_shape))
    check("space-separated real charge string preserves sentinel", any(row.get("raw") == "8888888" and row.get("unparsed") for row in real_shape), str(real_shape))
    mixed_codes = importer.parse_charge_rows("11377 HS/M; VC 23152(a)/M; BP 25658(a)/M; GC 1090/F")
    check("non-penal charge code families parse", [row.get("code") for row in mixed_codes] == ["HS 11377", "VC 23152(a)", "BP 25658(a)", "GC 1090"], str(mixed_codes))
    check("health and safety links use HSC", "lawCode=HSC" in mixed_codes[0].get("url", ""), str(mixed_codes))
    check("vehicle links use VEH", "lawCode=VEH" in mixed_codes[1].get("url", ""), str(mixed_codes))
    check("business and professions links use BPC", "lawCode=BPC" in mixed_codes[2].get("url", ""), str(mixed_codes))
    check("government links use GOV", "lawCode=GOV" in mixed_codes[3].get("url", ""), str(mixed_codes))
    stale = importer.normalize_case_data({
        **headered,
        "unavailable_text": "No information available besides the name of the defendant, DOE, JANE, and date of filing 06/20/2024. criminal_portal_no_public_entries",
        "unavailable_reason": "criminal_portal_no_public_entries",
    })
    check("stale criminal no-public text is regenerated", stale.get("unavailable_text") == headered.get("unavailable_text"), stale.get("unavailable_text"))
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
