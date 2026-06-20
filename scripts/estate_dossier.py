#!/usr/bin/env python3
"""Estate / probate dossier extraction — roles and events.

San Francisco probate matters (Dept 204; case prefixes PES/PTR/PCN/PGN etc.) are
non-adversarial estate proceedings: there is no "X v. Y" caption, the litigants
wear capacities (executor, administrator, conservator, trustee, heir, creditor),
and the docket is a sequence of estate milestones (petition for probate, letters
issued, inventory & appraisal, creditor's claim, accounting, final distribution).
The generic case-table builder flattens the Parties / ROA tabs but does not name
those capacities or milestones, so an estate reads as an undifferentiated party
list. This module is the estate-aware classifier that turns the already-captured
party rows and docket text into two small, audit-friendly "dossier" tables:

  * estate_roles   — one row per (party, estate role): executor, administrator,
                     personal representative, trustee, conservator/conservatee,
                     guardian/ward, decedent, heir, beneficiary, creditor,
                     claimant, petitioner, objector, surviving spouse, ...
  * estate_events  — one row per probate milestone parsed from a ROA entry:
                     petition for probate / letters, letters issued, inventory &
                     appraisal, creditor's claim (allowed/rejected), accounting,
                     petition for / decree of distribution, bond, discharge, ...
                     with the dollar amount when the entry states one (inventory
                     value, distribution amount).

Both tables are gated by ``is_probate_case`` so civil/UD dockets that merely
mention a bond or list a petitioner do not pollute the estate dossier.

Name-change extraction is deferred pending a real corpus of change-of-name
cases. The archive currently carries almost no such cases, and the removed
prototype had poor recall.

Design contract (matches CLAUDE.md "cite, verbatim, or don't assert it"):
this is a CLASSIFIER over verbatim captured text. Every row carries the exact
matched snippet (``matched_text``) so a reviewer can audit the inference. It
makes no network calls, infers nothing it cannot point at in the source string,
and depends only on the standard library so it is importable by
``build_case_tables.py`` and unit-testable on its own.
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Small text helpers (kept local so this module has no project dependencies).
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("<br>", " ")).strip()


_MONEY_RE = re.compile(
    r"\$\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s?(?P<mag>million|billion|thousand|m|k)?\b",
    re.IGNORECASE,
)
_MAGNITUDE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9}


def parse_amount(text: str) -> float | None:
    """First plausible dollar amount in a docket snippet, or None.

    Used to capture an inventory total or a distribution amount when the ROA
    entry states one (e.g. "Inventory and Appraisal ... $1,250,000.00"). Only a
    literal ``$`` amount is read; bare numbers are ignored to avoid false hits on
    dates, code sections, and receipt numbers. NOTE: on petition/letters rows the
    first ``$`` is often the filing fee, not an estate value — consumers should
    trust ``amount`` mainly on inventory/bond/distribution/account events.
    """
    m = _MONEY_RE.search(_clean(text))
    if not m:
        return None
    try:
        value = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    mag = (m.group("mag") or "").lower()
    return round(value * _MAGNITUDE.get(mag, 1.0), 2)


# ---------------------------------------------------------------------------
# Estate roles. Ordered most-specific -> least so "administrator with will
# annexed" wins over "administrator", "special administrator" over both, etc.
# Each pattern is matched against an uppercased candidate string that is either
# the court's party_type or a capacity phrase parsed out of the party name
# ("JANE DOE, AS EXECUTOR OF THE ESTATE OF ...").
# ---------------------------------------------------------------------------
ESTATE_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("decedent", re.compile(r"\bDECEDENT\b|\bDECEASED\b|\bESTATE OF\b")),
    ("special_administrator", re.compile(r"\bSPECIAL\s+ADMINISTRATOR")),
    ("administrator_with_will_annexed",
     re.compile(r"\bADMINISTRATOR\b.*\bWILL\s+ANNEXED\b|\bADMINISTRATOR\s+C\.?T\.?A\.?\b")),
    ("personal_representative", re.compile(r"\bPERSONAL\s+REPRESENTATIVE\b|\bPERS\.?\s*REP\b")),
    ("executor", re.compile(r"\bEXECUT(?:OR|RIX|RICE)\b|\bEXEC\b")),
    ("administrator", re.compile(r"\bADMINISTRAT(?:OR|RIX)\b|\bADMIN(?:ISTRATOR)?\b")),
    ("successor_trustee", re.compile(r"\bSUCCESSOR\s+TRUSTEE\b")),
    ("trustee", re.compile(r"\bTRUSTEE\b")),
    ("trustor", re.compile(r"\bTRUSTOR\b|\bSETTLOR\b|\bGRANTOR\b")),
    ("guardian_ad_litem", re.compile(r"\bGUARDIAN\s+AD\s+LITEM\b|\bGAL\b")),
    ("conservator", re.compile(r"\bCONSERVATOR\b")),
    ("conservatee", re.compile(r"\bCONSERVATEE\b|\bPROPOSED\s+CONSERVATEE\b")),
    ("guardian", re.compile(r"\bGUARDIAN\b")),
    ("ward", re.compile(r"\bWARD\b|\bMINOR\b")),
    ("surviving_spouse", re.compile(r"\bSURVIVING\s+SPOUSE\b|\bSPOUSE\b")),
    ("heir", re.compile(r"\bHEIRS?\b")),
    ("devisee", re.compile(r"\bDEVISEE\b")),
    ("legatee", re.compile(r"\bLEGATEE\b")),
    ("beneficiary", re.compile(r"\bBENEFICIAR(?:Y|IES)\b")),
    ("creditor", re.compile(r"\bCREDITOR\b")),
    ("claimant", re.compile(r"\bCLAIMANT\b")),
    ("objector", re.compile(r"\bOBJECTOR\b|\bCONTESTANT\b")),
    ("petitioner", re.compile(r"\bPETITIONER\b|\bCO-?PETITIONER\b")),
    ("fiduciary", re.compile(r"\bFIDUCIARY\b")),
]

# Capacity phrase inside a party name: "..., AS EXECUTOR OF ...",
# "... AS TRUSTEE OF THE SMITH FAMILY TRUST", "INDIVIDUALLY AND AS ...".
_CAPACITY_RE = re.compile(
    r"\bAS\s+(?:AN?\s+|THE\s+)?(?P<cap>[A-Z][A-Z' .\-]{2,60}?)"
    r"(?=\s+(?:OF|FOR|TO|UNDER)\b|,|;|$)",
    re.IGNORECASE,
)

# Roles that are strong, probate-specific signals (used to gate the estate
# tables). Deliberately excludes the generic civil roles petitioner / objector /
# creditor / claimant / surviving_spouse, which also appear in non-probate cases.
STRONG_ESTATE_ROLES = frozenset({
    "decedent", "executor", "administrator", "administrator_with_will_annexed",
    "special_administrator", "personal_representative", "conservator",
    "conservatee", "guardian", "guardian_ad_litem", "ward", "trustee",
    "successor_trustee", "trustor", "heir", "devisee", "legatee",
    "beneficiary", "fiduciary",
})

# SF Superior probate case numbers start with P (PES estate, PCN conservatorship,
# PTR trust, PGN/PGD guardianship, PDW, PRE, ...). Civil are C*, family F*.
_PROBATE_CASE_NUMBER_RE = re.compile(r"^\s*P[A-Z]", re.IGNORECASE)
_PROBATE_CAUSE_RE = re.compile(
    r"\b(?:PROBATE|CONSERVATOR|GUARDIAN|DECEDENT|ESTATE\s+OF|TESTAMENTARY|"
    r"INTESTATE|\bTRUST\b)\b",
    re.IGNORECASE,
)


def classify_estate_role(text: str) -> tuple[str, str] | None:
    """Map a party_type or capacity phrase to (canonical_role, matched_text)."""
    up = _clean(text).upper()
    if not up:
        return None
    for role, pat in ESTATE_ROLE_PATTERNS:
        m = pat.search(up)
        if m:
            return role, m.group(0).strip()
    return None


def is_probate_case(case_number: str, cause_of_action: str = "", parties: Any = ()) -> bool:
    """True for estate/probate matters; gates the estate_roles/estate_events tables.

    A case qualifies on ANY of: a probate case-number prefix (P...), a probate
    ``cause_of_action``, or a party whose court ``party_type`` is a strong estate
    role (decedent/PR/executor/administrator/conservator/conservatee/guardian/
    ward/trustee/heir/...). The strong-role path is what keeps old numeric-only
    probate case numbers (pre-prefix era) in scope while excluding civil dockets
    that merely mention a bond or a petitioner.
    """
    if _PROBATE_CASE_NUMBER_RE.match(_clean(case_number)):
        return True
    if cause_of_action and _PROBATE_CAUSE_RE.search(_clean(cause_of_action)):
        return True
    for party in parties or ():
        if not isinstance(party, dict):
            continue
        party_type = (party.get("party_type") or party.get("partyType")
                      or party.get("PARTYTYPE") or party.get("type") or "")
        found = classify_estate_role(party_type)
        if found and found[0] in STRONG_ESTATE_ROLES:
            return True
    return False


def estate_roles_for_party(party_name: str, party_type: str) -> list[dict[str, str]]:
    """Estate roles for one party row.

    Primary role comes from the court's ``party_type`` (basis ``party_type``);
    additional capacities embedded in the name (basis ``capacity``) are added
    when they name a different role — this is how the same person shows up as
    both, e.g., "Petitioner" (party_type) and "executor" (capacity in the name).
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    primary = classify_estate_role(party_type)
    if primary:
        role, matched = primary
        seen.add(role)
        out.append({"role": role, "role_basis": "party_type", "matched_text": matched})

    for cap_match in _CAPACITY_RE.finditer(_clean(party_name)):
        capacity = cap_match.group("cap")
        found = classify_estate_role(capacity)
        if found and found[0] not in seen:
            seen.add(found[0])
            out.append({"role": found[0], "role_basis": "capacity",
                        "matched_text": _clean(cap_match.group(0))})
    return out


# ---------------------------------------------------------------------------
# Estate events. Ordered most-specific -> least; ``family`` suppresses a generic
# match once a specific sibling in the same family already fired on the same row
# (e.g. don't also tag generic "petition_for_letters" when
# "petition_for_letters_of_administration" matched).
# ---------------------------------------------------------------------------
ESTATE_EVENT_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("petition_for_probate", "open",
     re.compile(r"\bPETITION\b.*\bPROBATE\s+OF\s+WILL\b|\bPETITION\s+FOR\s+PROBATE\b", re.I)),
    ("petition_for_letters_testamentary", "letters",
     re.compile(r"\bPETITION\b.*\bLETTERS\s+TESTAMENTARY\b", re.I)),
    ("petition_for_letters_of_administration", "letters",
     re.compile(r"\bPETITION\b.*\bLETTERS\s+OF\s+ADMINISTRATION\b", re.I)),
    ("petition_for_letters", "letters",
     re.compile(r"\bPETITION\b.*\bLETTERS\b|\bPETITION\s+FOR\s+(?:SPECIAL\s+)?ADMINISTRATION\b", re.I)),
    ("letters_issued", "letters_issued",
     re.compile(r"\bLETTERS\b.*\bISSUED\b|\bISSUANCE\s+OF\s+LETTERS\b|\bLETTERS\s+(?:TESTAMENTARY|OF\s+ADMINISTRATION|SPECIAL)\b", re.I)),
    ("will_admitted", "will",
     re.compile(r"\bWILL\s+ADMITTED\b|\bADMITT\w*\s+TO\s+PROBATE\b|\bORDER\s+ADMITTING\s+WILL\b", re.I)),
    ("supplemental_inventory_and_appraisal", "inventory",
     re.compile(r"\b(?:SUPP(?:LEMENTAL)?|FINAL|ADDITIONAL)\s+INVENTORY\s+(?:AND|&)\s+APPRAISAL\b", re.I)),
    ("inventory_and_appraisal", "inventory",
     re.compile(r"\bINVENTORY\s+(?:AND|&)\s+APPRAISAL\b|\bINVENTORY\s+AND\s+APPRAISEMENT\b", re.I)),
    ("creditors_claim_allowed", "claim",
     re.compile(r"\bCLAIM\b.*\b(?:ALLOW|APPROV)\w*|\b(?:ALLOW|APPROV)\w*\b.*\bCLAIM\b", re.I)),
    ("creditors_claim_rejected", "claim",
     re.compile(r"\bCLAIM\b.*\b(?:REJECT|DISALLOW|DENI)\w*|\b(?:REJECT|DISALLOW|DENI)\w*\b.*\bCLAIM\b", re.I)),
    ("creditors_claim", "claim",
     re.compile(r"\bCREDITOR'?S?\s+CLAIM\b", re.I)),
    ("petition_for_final_distribution", "distribution",
     re.compile(r"\bPETITION\b.*\bFINAL\s+DISTRIBUTION\b", re.I)),
    ("petition_for_preliminary_distribution", "distribution",
     re.compile(r"\bPETITION\b.*\bPRELIMINARY\s+DISTRIBUTION\b", re.I)),
    ("petition_for_distribution", "distribution",
     re.compile(r"\bPETITION\b.*\bDISTRIBUTION\b", re.I)),
    ("decree_of_distribution", "distribution",
     re.compile(r"\b(?:DECREE|ORDER)\b.*\bDISTRIBUTION\b|\bORDER\s+FOR\s+(?:FINAL|PRELIMINARY)\s+DISTRIBUTION\b", re.I)),
    ("petition_to_determine_distribution_rights", "heirship",
     re.compile(r"\bDETERMIN\w*\b.*\b(?:DISTRIBUTION\s+RIGHTS|ENTITLEMENT|HEIRSHIP|PERSONS\s+ENTITLED)\b", re.I)),
    ("spousal_property_petition", "spousal",
     re.compile(r"\bSPOUSAL\s+(?:OR\s+DOMESTIC\s+PARTNER\s+)?PROPERTY\s+(?:PETITION|ORDER)\b", re.I)),
    ("account_and_report", "account",
     re.compile(r"\b(?:FIRST|SECOND|THIRD|FINAL|ANNUAL|CURRENT)\s+(?:AND\s+FINAL\s+)?ACCOUNT(?:ING)?\b|\bACCOUNT\s+(?:AND|&)\s+REPORT\b|\bACCOUNTING\b", re.I)),
    ("waiver_of_bond", "bond",
     re.compile(r"\bWAIV\w*\s+(?:OF\s+)?BOND\b|\bBOND\s+WAIVED\b", re.I)),
    ("bond", "bond",
     re.compile(r"\bBOND\b", re.I)),
    ("notice_to_creditors", "notice",
     re.compile(r"\bNOTICE\s+(?:TO|OF)\s+CREDITORS\b|\bNOTICE\s+OF\s+ADMINISTRATION\b", re.I)),
    ("order_for_discharge", "discharge",
     re.compile(r"\b(?:ORDER|EX\s+PARTE\s+PETITION)\b.*\bDISCHARGE\b|\bORDER\s+OF\s+FINAL\s+DISCHARGE\b", re.I)),
]


def detect_estate_events(description: str) -> list[dict[str, Any]]:
    """Estate milestones in a single ROA / docket description.

    Returns at most one row per event family (specific beats generic), each with
    the canonical ``event_type``, the matched snippet, and any dollar ``amount``
    stated in the entry. Usually 0 or 1 rows; a compound entry can yield a few.
    """
    text = _clean(description)
    if not text:
        return []
    out: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    amount = parse_amount(text)
    for event_type, family, pat in ESTATE_EVENT_PATTERNS:
        if family in seen_families:
            continue
        m = pat.search(text)
        if m:
            seen_families.add(family)
            out.append({
                "event_type": event_type,
                "event_family": family,
                "matched_text": _clean(m.group(0)),
                "amount": amount,
            })
    return out


__all__ = [
    "parse_amount",
    "is_probate_case",
    "classify_estate_role",
    "estate_roles_for_party",
    "detect_estate_events",
    "ESTATE_ROLE_PATTERNS",
    "ESTATE_EVENT_PATTERNS",
    "STRONG_ESTATE_ROLES",
]
