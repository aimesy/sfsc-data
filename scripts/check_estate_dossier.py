#!/usr/bin/env python3
"""Checks for the estate / probate dossier extraction layer.

Covers, with no court access and synthetic fixtures only:
  * scripts/estate_dossier.py     — role / event classifiers + probate gate
  * scripts/build_case_tables.py  — the estate_roles / estate_events tables
                                    produced from case JSON, gated to probate

Run: python scripts/check_estate_dossier.py   (exits non-zero on first failure).
The parquet round-trip is skipped with a notice if pandas/pyarrow are absent, so
the pure-classifier checks still run anywhere.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estate_dossier as ed

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------------------
# 1. Roles
# ---------------------------------------------------------------------------
def test_roles() -> None:
    section("estate roles")
    cases = [
        ("Executor", "JANE ROE", "executor", "party_type"),
        ("Administrator", "JOHN DOE", "administrator", "party_type"),
        ("Special Administrator", "X", "special_administrator", "party_type"),
        ("Personal Representative", "X", "personal_representative", "party_type"),
        ("Conservatee", "ELDER PERSON", "conservatee", "party_type"),
        ("Conservator", "CARE GIVER", "conservator", "party_type"),
        ("Decedent", "DEAD PERSON", "decedent", "party_type"),
        ("Creditor", "BANK NA", "creditor", "party_type"),
        ("Claimant", "SOME CLAIMANT", "claimant", "party_type"),
        ("Heir", "AN HEIR", "heir", "party_type"),
        ("Beneficiary", "A BENEFICIARY", "beneficiary", "party_type"),
        ("Trustee", "TRUST CO", "trustee", "party_type"),
        ("Petitioner", "A PETITIONER", "petitioner", "party_type"),
    ]
    for party_type, name, want_role, want_basis in cases:
        roles = ed.estate_roles_for_party(name, party_type)
        got = {r["role"] for r in roles}
        check(f"party_type '{party_type}' -> {want_role}", want_role in got, str(got))
        if roles:
            check(f"  basis for '{party_type}'",
                  any(r["role"] == want_role and r["role_basis"] == want_basis for r in roles))

    # Capacity embedded in the name, separate from a generic party_type.
    roles = ed.estate_roles_for_party(
        "SMITH, JANE, AS EXECUTOR OF THE ESTATE OF JOHN SMITH", "Petitioner")
    got = {(r["role"], r["role_basis"]) for r in roles}
    check("capacity 'as executor' parsed from name",
          ("executor", "capacity") in got, str(got))
    check("party_type 'Petitioner' kept alongside capacity",
          ("petitioner", "party_type") in got, str(got))

    # "administrator with will annexed" beats bare "administrator".
    roles = ed.estate_roles_for_party("X", "Administrator With Will Annexed")
    check("administrator w/ will annexed wins specificity",
          roles and roles[0]["role"] == "administrator_with_will_annexed",
          str(roles))

    # A plain civil party_type yields no estate role.
    check("non-estate party_type -> no role",
          ed.estate_roles_for_party("ACME CORP", "Cross-Defendant") == [])


# ---------------------------------------------------------------------------
# 2. Events
# ---------------------------------------------------------------------------
def test_events() -> None:
    section("estate events")
    cases = [
        ("Petition for Probate of Will and for Letters Testamentary", "petition_for_probate"),
        ("Petition for Letters of Administration", "petition_for_letters_of_administration"),
        ("Letters Testamentary Issued", "letters_issued"),
        ("Inventory and Appraisal filed - $1,250,000.00", "inventory_and_appraisal"),
        ("Final Inventory and Appraisal", "supplemental_inventory_and_appraisal"),
        ("Creditor's Claim filed by Bank", "creditors_claim"),
        ("Order Allowing Creditor's Claim", "creditors_claim_allowed"),
        ("First and Final Account and Report", "account_and_report"),
        ("Petition for Final Distribution", "petition_for_final_distribution"),
        ("Decree of Final Distribution", "decree_of_distribution"),
        ("Bond filed in the amount of $50,000", "bond"),
        ("Notice to Creditors", "notice_to_creditors"),
        ("Order for Final Discharge", "order_for_discharge"),
    ]
    for desc, want in cases:
        events = ed.detect_estate_events(desc)
        got = {e["event_type"] for e in events}
        check(f"'{desc[:48]}' -> {want}", want in got, str(got))

    # Amount captured on the inventory line.
    inv = ed.detect_estate_events("Inventory and Appraisal filed - $1,250,000.00")
    check("inventory amount parsed", any(e["amount"] == 1250000.0 for e in inv),
          str([e["amount"] for e in inv]))

    # Generic letters petition suppressed when a specific one matched (one family).
    ev = ed.detect_estate_events("Petition for Letters of Administration")
    fams = [e["event_family"] for e in ev]
    check("one row per event family", fams.count("letters") == 1, str(fams))

    # Plain docket noise -> nothing.
    check("non-estate docket text -> no events",
          ed.detect_estate_events("Proof of Service of Summons filed") == [])

    # Money helper.
    check("parse_amount $1.2 million", ed.parse_amount("valued at $1.2 million") == 1200000.0)
    check("parse_amount ignores bare numbers", ed.parse_amount("filed 2021 dept 204") is None)


# ---------------------------------------------------------------------------
# 3. build_case_tables integration (no pandas needed for rows_from_cases)
# ---------------------------------------------------------------------------
SYNTHETIC_CASES = {
    "PES21000001": {
        "case_number": "PES-21-000001",
        "case_title": "Estate of Margaret Holloway, Deceased",
        "cause_of_action": "Petition for Probate",
        "parties": [
            {"name": "HOLLOWAY, MARGARET", "party_type": "Decedent"},
            {"name": "HOLLOWAY, ROBERT, AS EXECUTOR OF THE ESTATE",
             "party_type": "Petitioner", "attorneys": "DOE, JANE"},
            {"name": "FIRST BANK NA", "party_type": "Creditor"},
            {"name": "HOLLOWAY, SUSAN", "party_type": "Heir"},
        ],
        "docket_entries": [
            {"date_filed": "2021-03-01", "description":
             "Petition for Probate of Will and for Letters Testamentary"},
            {"date_filed": "2021-04-15", "description":
             "Letters Testamentary Issued to Robert Holloway"},
            {"date_filed": "2021-06-01", "description":
             "Inventory and Appraisal filed - total $2,400,000.00"},
            {"date_filed": "2021-08-01", "description": "Creditor's Claim filed by First Bank"},
            {"date_filed": "2022-01-10", "description":
             "Petition for Final Distribution and First and Final Account"},
        ],
    },
    # Civil case: mentions a "bond" and has a "Petitioner" — must NOT produce any
    # estate_roles / estate_events (gating keeps the estate dossier probate-only).
    "CGC23123456": {
        "case_number": "CGC-23-123456",
        "case_title": "Acme LLC vs. Roe Corp",
        "cause_of_action": "Breach of Contract",
        "parties": [
            {"name": "ACME LLC", "party_type": "Petitioner"},
            {"name": "ROE CORP", "party_type": "Defendant"},
        ],
        "docket_entries": [
            {"date_filed": "2023-01-01", "description":
             "Motion to require plaintiff to furnish security (undertaking/bond)"},
            {"date_filed": "2023-02-01", "description": "Final accounting of damages filed"},
        ],
    },
    # Old numeric (pre-prefix) probate case: no P prefix, no cause text — must be
    # gated IN via a strong estate party_type (Conservatee).
    "259017": {
        "case_number": "259017",
        "case_title": "Conservatorship of Jane Elder",
        "parties": [
            {"name": "ELDER, JANE", "party_type": "Conservatee"},
            {"name": "ELDER, ROBERT", "party_type": "Conservator"},
        ],
        "docket_entries": [
            {"date_filed": "2011-01-01", "description": "First Account and Report filed"},
        ],
    },
}


def write_synthetic_cases(case_dir):
    from pathlib import Path
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    for stem, payload in SYNTHETIC_CASES.items():
        (case_dir / f"{stem}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_build_tables() -> None:
    section("build_case_tables estate tables")
    import build_case_tables as bct
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        case_dir = Path(tmp) / "cases"
        write_synthetic_cases(case_dir)
        tables = bct.rows_from_cases(case_dir)

        roles = tables["estate_roles"]
        role_set = {(r["case_number"], r["role"]) for r in roles}
        check("decedent role row", ("PES21000001", "decedent") in role_set)
        check("executor capacity role row", ("PES21000001", "executor") in role_set)
        check("creditor role row", ("PES21000001", "creditor") in role_set)
        check("heir role row", ("PES21000001", "heir") in role_set)

        events = tables["estate_events"]
        ev_set = {(e["case_number"], e["event_type"]) for e in events}
        check("probate petition event", ("PES21000001", "petition_for_probate") in ev_set)
        check("letters issued event", ("PES21000001", "letters_issued") in ev_set)
        check("inventory event", ("PES21000001", "inventory_and_appraisal") in ev_set)
        check("final distribution event",
              ("PES21000001", "petition_for_final_distribution") in ev_set)
        inv_amounts = [e["amount"] for e in events if e["event_type"] == "inventory_and_appraisal"]
        check("inventory event carries amount", 2400000.0 in inv_amounts, str(inv_amounts))

        # Gating: civil case must produce NO estate roles/events.
        check("civil case yields no estate_roles",
              all(r["case_number"] != "CGC23123456" for r in roles))
        check("civil case yields no estate_events",
              all(e["case_number"] != "CGC23123456" for e in events))
        # Old numeric probate case gated IN via Conservatee party_type.
        check("numeric probate case gated in (roles)",
              any(r["case_number"] == "259017" and r["role"] == "conservatee" for r in roles))
        check("numeric probate case gated in (events)",
              any(e["case_number"] == "259017" for e in events))

        # The full writer must round-trip these to parquet (needs pandas).
        try:
            import pandas  # noqa: F401
        except ImportError:
            print("  [skip] parquet round-trip (pandas not installed)")
            return
        out_dir = Path(tmp) / "out"
        bct.write_tables(tables, out_dir)
        for name in ("estate_roles", "estate_events"):
            check(f"wrote {name}.parquet", (out_dir / f"{name}.parquet").exists())


def test_probate_gate() -> None:
    section("probate gate")
    check("P-prefix is probate", ed.is_probate_case("PES21000001"))
    check("PCN is probate", ed.is_probate_case("PCN20000001"))
    check("CGC civil is not probate (no signals)", not ed.is_probate_case("CGC23123456"))
    check("CUD civil is not probate", not ed.is_probate_case("CUD23000001"))
    check("probate cause_of_action gates in",
          ed.is_probate_case("259017", cause_of_action="Probate - Decedent's Estate"))
    check("strong party_type gates in",
          ed.is_probate_case("259017", parties=[{"party_type": "Conservatee"}]))
    check("generic petitioner party_type does NOT gate in",
          not ed.is_probate_case("CGC1", parties=[{"party_type": "Petitioner"}]))
    check("civil 'bond' docket case stays out",
          not ed.is_probate_case("CGC1", "Breach of Contract",
                                 [{"party_type": "Plaintiff"}, {"party_type": "Defendant"}]))


def main() -> int:
    test_roles()
    test_events()
    test_probate_gate()
    test_build_tables()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("estate dossier checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
