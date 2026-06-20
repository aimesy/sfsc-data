#!/usr/bin/env python3
"""Build normalized Parquet tables from archived SFSC case JSON.

The scanner and promoter keep ``archive/cases/<case_number>.json`` as the
canonical capture artifact because a case is naturally nested and needs
verbatim salvage/provenance fields. This script is the derived analytics layer:
it flattens those captures into one-row-per-record tables suitable for DuckDB,
aggregate counts, joins, and external research exports.

Outputs, by default:

* data/cases.parquet
* data/docket_entries.parquet
* data/parties.parquet
* data/attorneys.parquet
* data/representation.parquet
* data/calendar.parquet
* data/payments.parquet
* data/estate_roles.parquet
* data/estate_events.parquet
* data/entity-profiles-manifest.json
* data/entity-profiles-<kind>-NNN.json
* data/case-representation-manifest.json
* data/case-representation/<prefix>/<case_number>.json

The estate_roles / estate_events tables are the probate "dossier" layer:
estate-aware classifications of the same captured party rows and ROA text (see
scripts/estate_dossier.py), emitted only for probate matters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estate_dossier  # noqa: E402  (estate role / event / name-change classifiers)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_DIR = ROOT / "archive" / "cases"
DEFAULT_OUT_DIR = ROOT / "data"
DEFAULT_ENTITY_PROFILES = DEFAULT_OUT_DIR / "entity-profiles.json"
DEFAULT_JUDGES_JSON = ROOT / "judges.json"
DEFAULT_ENTITY_PROFILE_SHARD_BYTES = 40 * 1024 * 1024
DEFAULT_CASE_REPRESENTATION_DIR = DEFAULT_OUT_DIR / "case-representation"
DEFAULT_CASE_REPRESENTATION_MANIFEST = DEFAULT_OUT_DIR / "case-representation-manifest.json"
CASE_REPRESENTATION_PAGE_SIZE = 50
CASE_REPRESENTATION_PARTY_THRESHOLD = 100
CASE_REPRESENTATION_ATTORNEY_THRESHOLD = 50
CASE_REPRESENTATION_EDGE_THRESHOLD = 200

PRO_PER_RE = re.compile(r"^\s*(?:PRO\s*PER|IN\s+PRO\s+PER|PRO\s*SE)\s*$", re.I)


def clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("<br>", " ")).strip()


def first_text(obj: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = clean(obj.get(key))
        if value:
            return value
    return ""


def as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean(v) for v in value if clean(v)]
    scalar = clean(value)
    if not scalar:
        return []
    return [clean(v) for v in re.split(r";|\n", scalar) if clean(v)]


def unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean(value)
        key = text.upper()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def json_scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def json_list(values: Iterable[Any]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def parse_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return unique(clean(v) for v in value)
    text = clean(value)
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return [text]
    if isinstance(data, list):
        return unique(clean(v) for v in data)
    return [clean(data)] if clean(data) else []


def norm_case(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", clean(value)).upper()


def norm_bar(value: Any) -> str:
    digits = re.sub(r"\D+", "", clean(value))
    return digits or clean(value).upper()


def norm_name(value: Any) -> str:
    text = clean(value).upper()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [tok for tok in text.split() if len(tok) > 1]
    return " ".join(tokens)


def norm_entity_name(value: Any) -> str:
    return re.sub(
        r"\s+\((PLAINTIFF|DEFENDANT|PETITIONER|RESPONDENT|CLAIMANT|CREDITOR|DEBTOR|INTERVENOR)\)$",
        "",
        clean(value),
        flags=re.I,
    ).strip()


# --- parties_represented role vocabulary (Fix R3) -------------------------
# ``attorneys[].parties_represented`` arrives as comma-joined, role-tagged
# strings such as "MYERS, NELDA (PERSON TO BE PROTECTED), MYERS, NELDA
# (PETITIONER)" (verbatim from PED10293354.json). The old ingest (as_list, which
# only splits on ';'/newline) stored the whole blob as one "party" and
# norm_entity_name stripped only a single trailing role suffix, so 92.7% of
# party entries kept a "(ROLE)" parenthetical and 49.8% fused multiple parties.
#
# We strip parentheticals *only* when their leading keyword is a known party
# ROLE, so legitimate non-role parentheticals in a party name -- addresses
# ("(825 VAN NESS AVE)"), bar numbers ("(SBN 164575)"), case references
# ("(FROM CASE# 508886)"), or AKA/FKA aliases ("(FKA BRIUS, LLC)") -- are
# preserved. The vocabulary is an explicit allow-list of the role words seen in
# the captured data (see docs/attorney-handling-diagnosis.md sec. B); source
# rows frequently leave the parenthesis unbalanced ("(CONSOLIDATED CASE"), so we
# also accept a role-keyword paren that runs to the next ')' or to end-of-string.
_PARTY_ROLE_WORDS = (
    r"PLAINTIFF|DEFENDANT|PETITIONER|RESPONDENT|CLAIMANT|CREDITOR|DEBTOR|"
    r"INTERVENOR|CONSERVATOR|CONSERVATEE|TRUSTEE|TRUSTOR|BENEFICIARY|GUARDIAN|"
    r"MINOR|DECEDENT|HEIR|WARD|"
    r"APPELLANT|APPELLEE|OBJECTOR|REQUESTOR|REQUESTER|ASSIGNEE|ASSIGNOR|"
    r"RECEIVER|DEPONENT|GARNISHEE|LIEN\s*CLAIMANT|MOVANT|PETITONER|"
    r"CROSS[\s-]?(?:DEFENDANT|COMPLAINANT|PLAINTIFF|RESPONDENT|APPELLANT|PETITIONER)|"
    r"PERSON\s+TO\s+BE\s+PROTECTED|PROTECTED\s+PERSON|PERSONAL\s+REPRESENTATIVE|"
    r"REAL\s+PARTY(?:\s+IN\s+INTEREST)?|PARTY\s+IN\s+INTEREST|"
    r"DEFENDANT\s+IN\s+INTERVENTION|PLAINTIFF\s+IN\s+INTERVENTION|"
    r"GUARDIAN\s+AD\s+LITEM|THIRD\s+PARTY|NON[\s-]?PARTY|"
    r"ADMINISTRATOR|EXECUTOR|EXECUTRIX|PROPONENT|CONTESTANT|SUBROGEE|"
    r"CROSS\s+COMPLAINANT|CROSS\s+DEFENDANT"
)
# A role parenthetical: "(" then optional leading qualifier ("AND"/"&"), then a
# role word, then anything up to the next ")" or end-of-string (handles the
# unbalanced "(DEFENDANT (CONSOLIDATED CASE" rows). Case-insensitive.
_ROLE_PAREN_RE = re.compile(
    r"\(\s*(?:(?:AND|&|/)\s+)*(?:" + _PARTY_ROLE_WORDS + r")\b[^)]*\)?",
    re.I,
)
# Used to decide whether an element is "role-delimited" (well-formed enough to
# split into parties on its role parentheticals).
_ROLE_PAREN_FINDER = re.compile(
    r"\(\s*(?:(?:AND|&|/)\s+)*(?:" + _PARTY_ROLE_WORDS + r")\b[^)]*\)?",
    re.I,
)


def strip_party_roles(value: Any) -> str:
    """Remove *role* parentheticals from a party name, keep non-role ones.

    Strips "(DEFENDANT)", "(CROSS-COMPLAINANT)", "(PERSON TO BE PROTECTED)", etc.
    wherever they appear (interior or trailing), but leaves a parenthetical that
    is not a known role -- e.g. an address or alias -- intact.
    """

    text = _ROLE_PAREN_RE.sub(" ", clean(value))
    # A dangling close-paren can remain when the source had unbalanced parens
    # ("NAME (DEFENDANT (CONSOLIDATED CASE)") -- drop a lone trailing ')' ONLY
    # when parens are unbalanced, so we never clip the closer of a legitimate
    # non-role parenthetical like an address "(825 VAN NESS AVE)".
    while text.rstrip().endswith(")") and text.count(")") > text.count("("):
        text = text.rstrip()[:-1]
    return clean(text).strip(" ,;")


def split_represented_parties(raw_list: Any) -> list[str]:
    """Split & role-strip ``attorneys[].parties_represented`` into party names.

    Each element (already separated on ';'/newline by as_list) may itself fuse
    several "NAME (ROLE)" parties with commas. We split on the *role
    parentheticals* (the explicit, reliable party terminator) rather than on bare
    commas, because party names themselves contain commas ("LAST, FIRST"). An
    element with no role parenthetical is treated as a single party and only its
    trailing role suffix is stripped, so a lone name that legitimately contains a
    comma or a non-role parenthetical is never shredded.

    Returns role-stripped, de-duplicated party names (uppercased to match the
    rest of the entity pipeline).
    """

    out: list[str] = []
    for raw in as_list(raw_list):
        matches = list(_ROLE_PAREN_FINDER.finditer(raw))
        if not matches:
            # No role marker: keep as one party, strip only a trailing role
            # suffix that norm_entity_name would have caught, and any role paren.
            name = strip_party_roles(raw)
            if name:
                out.append(name.upper())
            continue
        # Each role parenthetical terminates a party; the party name is the text
        # since the previous terminator, minus the separating comma/semicolon.
        prev_end = 0
        for m in matches:
            segment = raw[prev_end:m.start()]
            prev_end = m.end()
            name = strip_party_roles(segment).strip(" ,;")
            if name:
                out.append(name.upper())
        # Trailing text after the last role paren (e.g. a final party that had no
        # role tag) -- keep it if it carries name-like content.
        tail = strip_party_roles(raw[prev_end:]).strip(" ,;")
        if tail:
            out.append(tail.upper())
    return unique(out)


def entity_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", norm_entity_name(value).upper()).strip()


# --- judge roster matching (Fix R1/R2) ------------------------------------
# Calendar officer names are bare ("RICHARD B. ULMER", or even a lone surname
# "QUIDACHAY") while judges.json roster names are formal, with suffixes and full
# middle names ("Richard B. Ulmer Jr.", "Ronald Evans Quidachay"). entity_key
# does not strip JR/SR, does not reconcile a middle INITIAL against a full middle
# NAME, and does not reduce to a first+last anchor, so the join missed and the
# same judge split into a populated calendar profile (no dept) plus an empty
# roster profile. 43.5% of judge case-appearances landed on dept-less profiles.
#
# Fix: match on a first+last "anchor" key, suffix-stripped, with middle-initial
# vs middle-name reconciled. We require first-name agreement (equal, or one is a
# single-letter initial of the other) so two DIFFERENT judges who merely share a
# surname (e.g. Bruce E. Chan vs Roger C. Chan) are NOT merged.

# Judicial / generational suffixes to drop before matching.
_JUDGE_SUFFIX_RE = re.compile(r"\b(?:JR|SR|II|III|IV|V|ESQ|ESQUIRE)\b", re.I)

# Pseudo-officers that show up in calendar[].judge or as roster placeholders but
# are not real, identifiable judges. These were promoted to judge profiles with
# large counts (~1,727 case-appearances). High-precision: anchored to these
# exact role phrases, not a loose keyword match. (docs sec. A.)
_PSEUDO_OFFICER_RE = re.compile(
    r"^(?:"
    r"SETTLEMENT\s+ATTORNEY(?:\s*\d+)?|"
    r"VISITING\s+JUDGE|UNKNOWN\s+JUDGE|"
    r"(?:JUDGE\s+)?PRO\s*[\s-]?TEM(?:PORE)?(?:\s*:.*)?|"
    r"PRO\s*TEM\s+JUDGE|JUDGE\s+PRO\s+TEMPORE|"
    r"TBA|TBD|TO\s+BE\s+(?:ASSIGNED|DETERMINED)|"
    r"COMMISSIONER|HEARING\s+OFFICER|TEMPORARY\s+JUDGE|"
    r"DEPT\.?\s*\d+|DEPARTMENT\s*\d+"
    r")$",
    re.I,
)


def is_pseudo_officer(name: Any) -> bool:
    """True for calendar/roster values that are role placeholders, not judges.

    Strips a leading "JUDGE " title first (so "Judge Pro Tem" is caught) and
    matches the whole remaining string against the pseudo-officer vocabulary.
    Bare "PRO TEM", "Pro Tem: Noah Lebowitz", "SETTLEMENT ATTORNEY 1/2/3",
    "VISITING JUDGE", "UNKNOWN JUDGE", "TBA"/"TBD", and dept placeholders all
    return True. (Fix R2.)
    """

    text = norm_entity_name(name)
    text = re.sub(r"^(?:HON(?:ORABLE)?|JUDGE|JUSTICE|THE)\b\.?\s*", "", text, flags=re.I).strip()
    if not text:
        return True
    return bool(_PSEUDO_OFFICER_RE.match(text))


def _judge_tokens(value: Any) -> list[str]:
    """Uppercase name tokens with punctuation and judicial suffixes removed."""

    text = norm_entity_name(value)
    # Drop a leading "Judge "/"Hon." style title before tokenizing.
    text = re.sub(r"^(?:HON(?:ORABLE)?|JUDGE|JUSTICE|THE)\b\.?\s*", "", text, flags=re.I)
    text = text.upper()
    text = re.sub(r"[^A-Z\s]", " ", text)  # drop '.', digits, etc.
    text = _JUDGE_SUFFIX_RE.sub(" ", text)
    return [tok for tok in text.split() if tok]


def judge_match_key(value: Any) -> str:
    """First+last anchor key for joining a judge name to the roster.

    "RICHARD B. ULMER", "Richard B. Ulmer Jr.", and "Richard Bernard Ulmer" all
    reduce to "RICHARD ULMER". A lone surname ("QUIDACHAY") reduces to "QUIDACHAY"
    (resolved later only if the roster surname is unambiguous). Returns "" when
    no usable token exists.
    """

    toks = _judge_tokens(value)
    if not toks:
        return ""
    if len(toks) == 1:
        return toks[0]
    return f"{toks[0]} {toks[-1]}"


def _judge_middle(value: Any) -> str:
    """Middle token(s) joined, for initial-vs-name reconciliation. May be ''."""

    toks = _judge_tokens(value)
    return " ".join(toks[1:-1]) if len(toks) > 2 else ""


# Explicit court-roster first-name nicknames (short form -> formal form). Kept
# as a curated allow-list rather than an open-ended prefix rule, because a prefix
# heuristic would wrongly merge different people whose names happen to nest
# ("DANIEL" vs "DANIELLE", "CARL" vs "CARLA", "ERIC" vs "ERICA"). Only add a pair
# here when it is verified to be the SAME judge appearing under both forms.
# Verified against the captured roster + calendar: "Russ"/"Russell Roeca" and
# "Chris"/"Christopher Hite" are the same judge under a court-roster short form.
_JUDGE_NICKNAMES = {
    "RUSS": "RUSSELL",
    "CHRIS": "CHRISTOPHER",
}


def _canon_first(name: str) -> str:
    return _JUDGE_NICKNAMES.get(name, name)


def _first_names_agree(a: str, b: str) -> bool:
    """First names agree if equal, an initial match, or a known nickname pair.

    - equal, or one is a single-letter initial of the other ("R" vs "RICHARD");
    - a curated court-roster nickname maps to its formal form ("RUSS" -> "RUSSELL",
      "CHRIS" -> "CHRISTOPHER"), so the bare calendar name still joins the roster.

    Different full first names ("PAUL" vs "JOHN", "ADRIENNE" vs "MARLA",
    "DANIEL" vs "DANIELLE") never agree, so distinct same-surname judges stay
    separate.
    """

    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return _canon_first(a) == _canon_first(b)


def _middles_compatible(a: str, b: str) -> bool:
    """Middle names are compatible unless they positively conflict.

    A missing middle on either side is compatible (so "Kathleen Kelly" matches
    "Kathleen A. Kelly"). A single-letter middle matches a full middle name with
    the same first letter ("B" vs "BERNARD"). Two full middle names must share a
    first letter to be compatible; differing initials (e.g. McFadden "S" vs "E")
    are a conflict and block the match.
    """

    if not a or not b:
        return True
    ta, tb = a.split(), b.split()
    for x, y in zip(ta, tb):
        if x[0] != y[0]:
            return False
    return True


def profile_key_text(value: Any) -> str:
    text = clean(value).upper()
    text = re.sub(r"\b(?:HON(?:ORABLE)?|JUDGE|JUSTICE|THE)\b\.?", " ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\b(?:ESQ|SBN|BAR)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def profile_search_text(*values: Any) -> str:
    parts: list[str] = []

    def add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for v in value.values():
                add(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                add(v)
        else:
            text = profile_key_text(value)
            if text:
                parts.append(text)

    for value in values:
        add(value)
    return " ".join(dict.fromkeys(parts))


def stable_hash(*parts: Any) -> str:
    body = "\x1f".join(clean(part) for part in parts)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def split_party_attorneys(raw_list: Any) -> list[str]:
    """Split SFSC party attorney strings and drop pro-per markers.

    Party rows often return attorney names as a single comma-delimited string in
    "LAST, FIRST, LAST, FIRST" form, with "Pro Per" mixed in. This mirrors the
    viewer/litigant-index cleanup so profile counts do not double-count prose.
    """

    out: list[str] = []
    for raw in as_list(raw_list):
        text = re.sub(r"<br\s*/?>|\(Deactive[^)]*\)", "", raw, flags=re.I)
        text = re.sub(r"(?<=[A-Za-z])PRO\s*PER\b", ", PRO PER", text, flags=re.I)
        text = re.sub(r"\b(?:IN\s+PRO\s+PER|PRO\s*PER|PRO\s*SE)\b", "PRO PER", text, flags=re.I)
        text = clean(text)
        text = re.sub(r"^(?:PRO\s*PER\s*,?\s*)+", "", text, flags=re.I)
        text = re.sub(r"(?:,?\s*PRO\s*PER)+\s*$", "", text, flags=re.I)
        text = re.sub(r",\s*PRO\s*PER\s*,", ",", text, flags=re.I)
        text = re.sub(r"\s+PRO\s*PER\s*,", ",", text, flags=re.I)
        text = re.sub(r",\s*PRO\s*PER\s+", ", ", text, flags=re.I)
        parts = [clean(part).strip(" `;") for part in text.split(",") if clean(part).strip(" `;")]
        i = 0
        while i < len(parts):
            part = parts[i]
            if PRO_PER_RE.match(part):
                i += 1
                continue
            name = part
            if i + 1 < len(parts) and not PRO_PER_RE.match(parts[i + 1]):
                name = f"{part}, {parts[i + 1]}"
                i += 2
                if i < len(parts) and re.fullmatch(r"JR\.?|SR\.?|II|III|IV|V", parts[i], flags=re.I):
                    name += f", {parts[i]}"
                    i += 1
            else:
                i += 1
            name = clean(name).strip(" ,;`").upper()
            if name and not PRO_PER_RE.match(name):
                out.append(name)
    return unique(out)


def attorney_id(name: str, bar_number: str) -> str:
    bar = norm_bar(bar_number)
    if bar:
        return f"bar:{bar}"
    key = norm_name(name)
    return f"name:{stable_hash(key)[:16]}" if key else ""


def upsert_attorney_profile(
    seen_attorneys: dict[str, dict[str, Any]],
    *,
    aid: str,
    name: str,
    bar_number: str = "",
    address: str = "",
    contact_block: str = "",
    source: str,
    confidence: float,
    captured_at: str,
    case_number: str,
    parties_represented: Iterable[str] = (),
) -> None:
    if not aid:
        return
    if aid not in seen_attorneys:
        bar = norm_bar(bar_number)
        contacts = unique([contact_block, address])
        seen_attorneys[aid] = {
            "attorney_id": aid,
            "name": name,
            "name_key": norm_name(name),
            "bar_number": bar,
            "bar_number_raw": bar_number,
            "jurisdiction": "CA" if bar else "",
            "address": address,
            "contact_block": contact_block,
            "source": source,
            "confidence": confidence,
            "first_captured_at": captured_at,
            "case_numbers_json": "[]",
            "parties_represented_json": "[]",
            "contacts_json": json_list(contacts),
            "appearance_count": 0,
        }
    row = seen_attorneys[aid]
    if source not in clean(row.get("source")).split("+"):
        row["source"] = "+".join(filter(None, [clean(row.get("source")), source]))
    if not row.get("bar_number") and norm_bar(bar_number):
        row["bar_number"] = norm_bar(bar_number)
        row["bar_number_raw"] = bar_number
        row["jurisdiction"] = "CA"
        row["confidence"] = max(float(row.get("confidence") or 0), 1.0)
    if not clean(row.get("address")) and address:
        row["address"] = address
    if not clean(row.get("contact_block")) and contact_block:
        row["contact_block"] = contact_block
    contacts = unique(parse_json_list(row.get("contacts_json")) + [contact_block, address])
    row["contacts_json"] = json_list(contacts)
    case_numbers = set(json.loads(row["case_numbers_json"]))
    case_numbers.add(case_number)
    row["case_numbers_json"] = json_list(sorted(case_numbers))
    represented = set(json.loads(row["parties_represented_json"]))
    represented.update(clean(p) for p in parties_represented if clean(p))
    row["parties_represented_json"] = json_list(sorted(represented))
    row["appearance_count"] = int(row["appearance_count"]) + 1


def load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_case_files(case_dir: Path, limit: int | None = None) -> Iterable[Path]:
    count = 0
    for path in sorted(case_dir.glob("*.json")):
        yield path
        count += 1
        if limit is not None and count >= limit:
            return


def docket_hash(case_number: str, entry: dict[str, Any], entry_seq: int) -> str:
    return stable_hash(
        case_number,
        first_text(entry, "date_filed", "FILEDATE", "filed", "date"),
        first_text(entry, "description", "RTEXT", "text", "title"),
        first_text(entry, "doc_id", "DocID"),
        first_text(entry, "fee", "FEE"),
        entry_seq,
    )


def rows_from_cases(case_dir: Path, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {
        "cases": [],
        "docket_entries": [],
        "parties": [],
        "attorneys": [],
        "representation": [],
        "calendar": [],
        "payments": [],
        "estate_roles": [],
        "estate_events": [],
    }
    seen_attorneys: dict[str, dict[str, Any]] = {}
    seen_representation: set[tuple[str, str, str, str]] = set()

    for path in iter_case_files(case_dir, limit):
        case = load_case(path)
        case_number = norm_case(case.get("case_number") or path.stem)
        if not case_number:
            continue
        case_title = clean(case.get("case_title"))
        cause_of_action = clean(case.get("cause_of_action"))
        captured_at = clean(case.get("captured_at"))
        source_url = clean(case.get("source_url"))
        try:
            case_path = path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            case_path = path.resolve().as_posix()
        documents = case.get("documents") if isinstance(case.get("documents"), list) else []
        docket_entries = case.get("docket_entries") if isinstance(case.get("docket_entries"), list) else []
        parties = case.get("parties") if isinstance(case.get("parties"), list) else []
        attorneys = case.get("attorneys") if isinstance(case.get("attorneys"), list) else []
        calendar = case.get("calendar") if isinstance(case.get("calendar"), list) else []
        payments = case.get("payments") if isinstance(case.get("payments"), list) else []
        # Estate roles/events are emitted only for probate matters, so civil/UD
        # dockets that merely mention a bond or a petitioner do not pollute the
        # estate dossier.
        is_probate = estate_dossier.is_probate_case(case_number, cause_of_action, parties)
        case_attorney_lookup: dict[str, tuple[str, str, str]] = {}
        for attorney in attorneys:
            if not isinstance(attorney, dict):
                continue
            name = first_text(attorney, "name", "NAME", "attorney")
            bar_number = first_text(attorney, "bar_number", "bar", "BARNUM")
            aid = attorney_id(name, bar_number)
            if name and aid:
                case_attorney_lookup[norm_name(name)] = (aid, name, norm_bar(bar_number))

        tables["cases"].append({
            "case_number": case_number,
            "case_title": case_title,
            "court": clean(case.get("court")),
            "cause_of_action": cause_of_action,
            "captured_at": captured_at,
            "source_url": source_url,
            "case_path": case_path,
            "status": clean(case.get("status")),
            "message": clean(case.get("message")),
            "unavailable_reason": clean(case.get("unavailable_reason")),
            "unavailable_text": clean(case.get("unavailable_text")),
            "document_bytes_captured": bool(case.get("document_bytes_captured")),
            "document_byte_capture_scope": clean(case.get("document_byte_capture_scope")),
            "document_bytes_pending": json_scalar(case.get("document_bytes_pending")),
            "documents_total": len(documents),
            "documents_bytes_count": int(case.get("documents_bytes_count") or sum(1 for doc in documents if clean(doc.get("sha256")))),
            "documents_unavailable_count": int(case.get("documents_unavailable_count") or sum(1 for doc in documents if doc.get("is_available") is False)),
            "documents_deferred_count": int(case.get("documents_deferred_count") or sum(1 for doc in documents if doc.get("byte_capture_deferred") is True)),
            "docket_entry_count": len(docket_entries),
            "party_count": len(parties),
            "attorney_appearance_count": len(attorneys),
            "calendar_count": len(calendar),
            "payment_count": len(payments),
            "payments_total": case.get("payments_total"),
            "method_status_json": json.dumps(case.get("method_status") or {}, ensure_ascii=False, sort_keys=True),
        })

        for entry_seq, entry in enumerate(docket_entries):
            if not isinstance(entry, dict):
                continue
            date_filed = first_text(entry, "date_filed", "FILEDATE", "filed", "date")
            description = first_text(entry, "description", "RTEXT", "text", "title")
            doc_id = first_text(entry, "doc_id", "DocID")
            entry_hash = docket_hash(case_number, entry, entry_seq)
            tables["docket_entries"].append({
                "case_number": case_number,
                "entry_seq": entry_seq,
                "date_filed": date_filed,
                "description": description,
                "doc_id": doc_id,
                "has_document": bool(entry.get("has_document") or doc_id or first_text(entry, "url", "URL")),
                "fee": first_text(entry, "fee", "FEE"),
                "url": first_text(entry, "url", "URL", "href", "source_url"),
                "entry_hash": entry_hash,
                "captured_at": captured_at,
                "source_url": source_url,
            })
            for event in (estate_dossier.detect_estate_events(description) if is_probate else ()):
                tables["estate_events"].append({
                    "case_number": case_number,
                    "entry_seq": entry_seq,
                    "date_filed": date_filed,
                    "event_type": event["event_type"],
                    "event_family": event["event_family"],
                    "amount": event["amount"],
                    "description": description,
                    "doc_id": doc_id,
                    "entry_hash": entry_hash,
                    "matched_text": event["matched_text"],
                    "captured_at": captured_at,
                    "source_url": source_url,
                })

        for party_seq, party in enumerate(parties):
            if not isinstance(party, dict):
                continue
            party_name = first_text(party, "name", "party", "NAME", "PARTY")
            party_type = first_text(party, "party_type", "partyType", "type", "PARTYTYPE", "PARTYDESC")
            party_id = f"{case_number}:party:{party_seq}"
            party_attorneys = split_party_attorneys(party.get("attorneys") or party.get("ATTORNEY(S)"))
            tables["parties"].append({
                "case_number": case_number,
                "party_seq": party_seq,
                "party_id": party_id,
                "party_name": party_name,
                "party_name_key": norm_name(party_name),
                "party_type": party_type,
                "attorneys_json": json_list(party_attorneys),
                "filings_json": json_list(as_list(party.get("filings") or party.get("FILING(S)"))),
                "party_address": first_text(party, "party_address", "address", "ADDRESS"),
                "captured_at": captured_at,
                "source_url": source_url,
            })
            for role in (estate_dossier.estate_roles_for_party(party_name, party_type) if is_probate else ()):
                tables["estate_roles"].append({
                    "case_number": case_number,
                    "party_seq": party_seq,
                    "party_id": party_id,
                    "party_name": party_name,
                    "party_name_key": norm_name(party_name),
                    "party_type": party_type,
                    "role": role["role"],
                    "role_basis": role["role_basis"],
                    "matched_text": role["matched_text"],
                    "captured_at": captured_at,
                    "source_url": source_url,
                })
            for attorney_name in party_attorneys:
                matched_aid, matched_name, matched_bar = case_attorney_lookup.get(norm_name(attorney_name), ("", "", ""))
                edge_aid = matched_aid or attorney_id(attorney_name, "")
                edge_name = matched_name or attorney_name
                if edge_aid and not matched_aid:
                    upsert_attorney_profile(
                        seen_attorneys,
                        aid=edge_aid,
                        name=attorney_name,
                        source="party_attorneys",
                        confidence=0.6,
                        captured_at=captured_at,
                        case_number=case_number,
                        parties_represented=[party_name],
                    )
                key = (case_number, party_id, edge_aid or edge_name, "party_attorneys")
                if key in seen_representation:
                    continue
                seen_representation.add(key)
                tables["representation"].append({
                    "case_number": case_number,
                    "party_id": party_id,
                    "party_name": party_name,
                    "party_type": party_type,
                    "attorney_id": edge_aid,
                    "attorney_name": edge_name,
                    "bar_number": matched_bar,
                    "source_field": f"parties[{party_seq}].attorneys",
                    "confidence": 1.0,
                    "captured_at": captured_at,
                    "source_url": source_url,
                })

        for attorney_seq, attorney in enumerate(attorneys):
            if not isinstance(attorney, dict):
                continue
            name = first_text(attorney, "name", "NAME", "attorney")
            bar_number = first_text(attorney, "bar_number", "bar", "BARNUM")
            aid = attorney_id(name, bar_number)
            # Split the comma-fused, role-tagged blob into individual,
            # role-stripped party names (R3). This feeds both the attorney
            # profile's parties_represented and the representation table's
            # party_name below, so the viewer's counsel->litigant cross-link can
            # resolve a real litigant instead of a whole "(ROLE)" blob.
            parties_represented = split_represented_parties(
                attorney.get("parties_represented")
                or attorney.get("party")
                or attorney.get("PARTY")
            )
            if aid:
                upsert_attorney_profile(
                    seen_attorneys,
                    aid=aid,
                    name=name,
                    bar_number=bar_number,
                    address=first_text(attorney, "address", "ADDRESS"),
                    contact_block=first_text(attorney, "contact_block", "contact", "ADDRESS"),
                    source="case_attorneys",
                    confidence=1.0 if norm_bar(bar_number) else 0.75,
                    captured_at=captured_at,
                    case_number=case_number,
                    parties_represented=parties_represented,
                )
            for party_name in parties_represented:
                key = (case_number, party_name, aid or name, "case_attorneys")
                if key in seen_representation:
                    continue
                seen_representation.add(key)
                tables["representation"].append({
                    "case_number": case_number,
                    "party_id": "",
                    "party_name": party_name,
                    "party_type": "",
                    "attorney_id": aid,
                    "attorney_name": name,
                    "bar_number": norm_bar(bar_number),
                    "source_field": f"attorneys[{attorney_seq}].parties_represented",
                    "confidence": 1.0,
                    "captured_at": captured_at,
                    "source_url": source_url,
                })

        for calendar_seq, row in enumerate(calendar):
            if not isinstance(row, dict):
                continue
            tables["calendar"].append({
                "case_number": case_number,
                "calendar_seq": calendar_seq,
                "court_date": first_text(row, "court_date", "COURTDATE", "date"),
                "matters": first_text(row, "matters", "MATTERS", "matter"),
                "location": first_text(row, "location", "LOCATION"),
                "judge": first_text(row, "judge", "JUDGENAME", "judge_name"),
                "captured_at": captured_at,
                "source_url": source_url,
            })

        for payment_seq, payment in enumerate(payments):
            if not isinstance(payment, dict):
                continue
            tables["payments"].append({
                "case_number": case_number,
                "payment_seq": payment_seq,
                "date": first_text(payment, "date", "TRANSDATE", "transdate"),
                "amount": payment.get("amount") if payment.get("amount") is not None else payment.get("AMOUNT"),
                "method": first_text(payment, "method", "type", "PAYTYPETEXT", "pay_type"),
                "receipt_number": first_text(payment, "receipt_number", "receipt", "RECEIPT_NUMBER"),
                "description": first_text(payment, "description", "DESCRIPTION"),
                "captured_at": captured_at,
                "source_url": source_url,
            })

    tables["attorneys"] = list(seen_attorneys.values())
    tables["cases"].sort(key=lambda r: r["case_number"])
    tables["docket_entries"].sort(key=lambda r: (r["case_number"], r["entry_seq"]))
    tables["parties"].sort(key=lambda r: (r["case_number"], r["party_seq"]))
    tables["attorneys"].sort(key=lambda r: (r["attorney_id"], r["name"]))
    tables["representation"].sort(key=lambda r: (r["case_number"], r["party_name"], r["attorney_name"], r["source_field"]))
    tables["calendar"].sort(key=lambda r: (r["case_number"], r["calendar_seq"]))
    tables["payments"].sort(key=lambda r: (r["case_number"], r["payment_seq"]))
    tables["estate_roles"].sort(key=lambda r: (r["case_number"], r["party_seq"], r["role"]))
    tables["estate_events"].sort(key=lambda r: (r["case_number"], r["entry_seq"], r["event_type"]))
    return tables


def case_summary_map(tables: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for row in tables.get("cases", []):
        case_number = clean(row.get("case_number"))
        if not case_number:
            continue
        summaries[case_number] = {
            "case_number": case_number,
            "case_title": clean(row.get("case_title")),
            "cause_of_action": clean(row.get("cause_of_action")),
            "captured_at": clean(row.get("captured_at")),
        }
    return summaries


def profile_cases(case_numbers: Iterable[str], summaries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_case_number in case_numbers:
        case_number = clean(raw_case_number)
        if not case_number or case_number in seen:
            continue
        seen.add(case_number)
        rows.append(summaries.get(case_number, {
            "case_number": case_number,
            "case_title": "",
            "cause_of_action": "",
            "captured_at": "",
        }))
    rows.sort(key=lambda r: (clean(r.get("captured_at")), clean(r.get("case_number"))), reverse=True)
    return rows


def attorney_profile_key(row: dict[str, Any]) -> tuple[str, str]:
    attorney_id_value = clean(row.get("attorney_id"))
    bar = norm_bar(row.get("bar_number"))
    name = clean(row.get("name"))
    legacy_key = f"bar:{bar}" if bar else f"name:{entity_key(name)}"
    key = f"bar:{bar}" if bar else (attorney_id_value or legacy_key)
    return key, legacy_key


def consolidate_name_shadows(
    attorney_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fold ``name:``-keyed attorney rows into a ``bar:`` row for the same person.

    The party-attorney path mints a ``name:<sha1(norm_name)>`` profile whenever an
    attorney name appears in a party's attorney string with no bar number nearby,
    even though a ``bar:<digits>`` profile for that same person already exists from
    the structured attorneys list. The diagnosis found 56 such ``name:`` shadows
    (docs sec. C).

    We build a map from ``norm_name`` (the *same* normalizer the id hash is built
    from -- not a looser token-sorted key) to the bar numbers seen for that name.
    A ``name:`` row whose ``norm_name`` maps to EXACTLY ONE bar is re-attributed to
    that ``bar:`` id and its cases/parties/contacts merged in. We deliberately:

      * use the exact ``norm_name`` so we only merge identical normalized spellings
        (high precision; a paren/ESQ/word-order variant is left alone -- that is the
        review-gated R5/R7 territory), and
      * refuse to merge when the name maps to MORE THAN ONE bar (genuine name
        collisions like "BROWN, MICHAEL" -> bar:164593 / bar:183609 stay split),
        and never merge two distinct bar numbers into each other (transposed-digit
        duplicates such as Navia 182934/182834 remain separate, review-gated).

    Returns a new list of attorney rows with the shadows merged.
    """

    rows = [dict(r) for r in attorney_rows]
    # norm_name -> set of bar ids that carry that normalized name.
    name_to_bars: dict[str, set[str]] = {}
    bar_row_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        bar = norm_bar(row.get("bar_number"))
        if not bar:
            continue
        bar_id = f"bar:{bar}"
        bar_row_by_id.setdefault(bar_id, row)
        nn = norm_name(row.get("name"))
        if nn:
            name_to_bars.setdefault(nn, set()).add(bar_id)

    merged: list[dict[str, Any]] = []
    for row in rows:
        aid = clean(row.get("attorney_id"))
        bar = norm_bar(row.get("bar_number"))
        nn = norm_name(row.get("name"))
        if bar or not aid.startswith("name:") or not nn:
            merged.append(row)
            continue
        bars = name_to_bars.get(nn)
        if not bars or len(bars) != 1:
            # No bar profile, or an ambiguous name collision -> leave the
            # name: profile intact (do not guess which bar it belongs to).
            merged.append(row)
            continue
        target = bar_row_by_id[next(iter(bars))]
        # Merge the shadow's evidence into the existing bar profile.
        target_cases = set(parse_json_list(target.get("case_numbers_json")))
        target_cases.update(parse_json_list(row.get("case_numbers_json")))
        target["case_numbers_json"] = json_list(sorted(target_cases))
        target_parties = set(parse_json_list(target.get("parties_represented_json")))
        target_parties.update(parse_json_list(row.get("parties_represented_json")))
        target["parties_represented_json"] = json_list(sorted(target_parties))
        target_contacts = unique(
            parse_json_list(target.get("contacts_json"))
            + parse_json_list(row.get("contacts_json"))
        )
        target["contacts_json"] = json_list(target_contacts)
        existing_src = clean(target.get("source")).split("+")
        for src in clean(row.get("source")).split("+"):
            if src and src not in existing_src:
                existing_src.append(src)
        target["source"] = "+".join(s for s in existing_src if s)
        target["appearance_count"] = int(target.get("appearance_count") or 0) + int(
            row.get("appearance_count") or 0
        )
        # Drop the shadow row (do not append it to ``merged``).
    return merged


class JudgeRoster:
    """Resolver that joins a bare calendar officer name to the judges.json roster.

    The roster is indexed by a first+last anchor key (``judge_match_key``). For
    each anchor we keep one canonical entry; when judges.json has redundant codes
    for one judge with conflicting departments (e.g. East RCE=206 vs RE=301, the
    stale row), the *first* non-empty dept in file order wins so an outdated dept
    cannot mask the current one (docs sec. A). We also index distinct anchors per
    surname so a lone-surname calendar value can be resolved only when exactly one
    roster judge has that surname (avoids merging Bruce Chan with Roger Chan).

    Pseudo-officer roster rows ("Judge Pro Tem", "Pro Tem: <name>") are excluded
    so they never seed a real judge profile (Fix R2).
    """

    def __init__(self) -> None:
        self.by_anchor: dict[str, dict[str, Any]] = {}
        self._anchors_by_surname: dict[str, set[str]] = {}
        self.entries: list[dict[str, Any]] = []

    def add_entry(self, entry: dict[str, Any]) -> None:
        name = entry.get("name")
        if is_pseudo_officer(name):
            return
        anchor = judge_match_key(name)
        if not anchor:
            return
        entry = dict(entry)
        entry["anchor"] = anchor
        entry["match_middle"] = _judge_middle(name)
        self.entries.append(entry)
        existing = self.by_anchor.get(anchor)
        if existing is None:
            self.by_anchor[anchor] = entry
        else:
            # Same judge, redundant code. Keep the first row with a non-empty
            # dept; otherwise prefer a non-former row. Deterministic on file order.
            if not clean(existing.get("dept")) and clean(entry.get("dept")):
                self.by_anchor[anchor] = entry
            elif clean(existing.get("former")) and not entry.get("former"):
                if not clean(existing.get("dept")):
                    self.by_anchor[anchor] = entry
        toks = anchor.split()
        if toks:
            self._anchors_by_surname.setdefault(toks[-1], set()).add(anchor)

    def resolve(self, officer_name: Any) -> tuple[str | None, dict[str, Any] | None]:
        """Return (canonical_anchor, roster_entry) for a calendar officer name.

        Candidates are roster judges that share the surname. We keep only those
        whose first name agrees (equal / initial / nickname-prefix) and whose
        middle name/initial is compatible. The match succeeds when EXACTLY ONE
        candidate qualifies, so:
          * a lone surname resolves only when that surname is unambiguous;
          * a nickname/initial calendar form ("RUSSELL S. ROECA") still finds the
            roster's "RUSS ROECA";
          * two distinct same-surname judges (Bruce vs Roger Chan) never collapse.
        """

        toks = _judge_tokens(officer_name)
        if not toks:
            return None, None
        surname = toks[-1]
        anchors = self._anchors_by_surname.get(surname)
        if not anchors:
            return None, None
        cal_first = toks[0] if len(toks) > 1 else ""
        cal_middle = _judge_middle(officer_name)
        candidates: list[str] = []
        for anchor in anchors:
            entry = self.by_anchor.get(anchor)
            if entry is None:
                continue
            roster_first = anchor.split()[0]
            # A lone surname (no first name) is compatible with any single
            # candidate; otherwise require first-name agreement + middle compat.
            if cal_first:
                if not _first_names_agree(cal_first, roster_first):
                    continue
                if not _middles_compatible(cal_middle, entry.get("match_middle", "")):
                    continue
            candidates.append(anchor)
        if len(candidates) != 1:
            return None, None
        anchor = candidates[0]
        return anchor, self.by_anchor.get(anchor)


def load_judge_roster(path: Path) -> JudgeRoster:
    roster = JudgeRoster()
    if not path.exists():
        return roster
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return roster
    code_map = data.get("code_map") if isinstance(data, dict) else {}
    if not isinstance(code_map, dict):
        return roster
    for code, raw in code_map.items():
        if not isinstance(raw, dict):
            continue
        name = norm_entity_name(raw.get("name"))
        if not name:
            continue
        roster.add_entry({
            "name": name,
            "dept": clean(raw.get("dept")),
            "code": clean(code),
            "former": bool(raw.get("former")),
        })
    return roster


def get_judge_profile(
    profiles: dict[str, dict[str, Any]],
    name: Any,
    roster_entry: dict[str, Any] | None = None,
    *,
    profile_key: str | None = None,
) -> dict[str, Any] | None:
    """Get/create a judge profile, keyed on the robust roster anchor when matched.

    Pseudo-officers are dropped (Fix R2). When ``roster_entry`` is supplied the
    profile is keyed on the canonical roster anchor and displays the formal
    roster name, so the bare calendar form ("RICHARD B. ULMER") and the formal
    roster form ("Richard B. Ulmer Jr.") collapse into ONE profile carrying the
    roster dept/code (Fix R1). Unmatched officers key on their own anchor.
    """

    if is_pseudo_officer(name):
        return None
    clean_name = norm_entity_name(name)
    clean_name = re.sub(r"^Judge\s+", "", clean_name, flags=re.I).strip()
    if not clean_name:
        return None
    key = profile_key or judge_match_key(clean_name)
    if not key:
        return None
    # Prefer the formal roster name for display when we have a roster match.
    display_name = clean(roster_entry.get("name")) if roster_entry else clean_name
    if key not in profiles:
        profiles[key] = {
            "profile_type": "judge",
            "key": key,
            "display_name": display_name or clean_name,
            "dept": clean(roster_entry.get("dept")) if roster_entry else "",
            "code": clean(roster_entry.get("code")) if roster_entry else "",
            "former": bool(roster_entry.get("former")) if roster_entry else False,
            "cases": [],
            "case_count": 0,
            "departments": [],
            "calendars": [],
            "_case_numbers": set(),
            "_departments": set(),
            "_calendars": set(),
        }
    profile = profiles[key]
    if roster_entry:
        # A roster match always wins the display name (formal over bare calendar).
        if clean(roster_entry.get("name")):
            profile["display_name"] = clean(roster_entry.get("name"))
        if not profile.get("dept") and clean(roster_entry.get("dept")):
            profile["dept"] = clean(roster_entry.get("dept"))
        if not profile.get("code") and clean(roster_entry.get("code")):
            profile["code"] = clean(roster_entry.get("code"))
        profile["former"] = bool(profile.get("former")) or bool(roster_entry.get("former"))
    return profile


def add_judge_case(profile: dict[str, Any], case_number: str, summaries: dict[str, dict[str, Any]]) -> None:
    case_number = clean(case_number)
    if not case_number or case_number in profile["_case_numbers"]:
        return
    profile["_case_numbers"].add(case_number)
    profile["cases"].append(summaries.get(case_number, {
        "case_number": case_number,
        "case_title": "",
        "cause_of_action": "",
        "captured_at": "",
        "source_url": "",
    }))
    profile["case_count"] = len(profile["cases"])


def add_unique_profile_value(
    profile: dict[str, Any],
    field: str,
    value: Any,
    limit: int | None = None,
) -> None:
    text = norm_entity_name(value)
    if not text:
        return
    set_name = f"_{field}"
    if text in profile[set_name]:
        return
    if limit is not None and len(profile[field]) >= limit:
        return
    profile[set_name].add(text)
    profile[field].append(text)


def finalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("profile_type") == "judge":
        profile["cases"].sort(key=lambda r: (clean(r.get("captured_at")), clean(r.get("case_number"))), reverse=True)
    profile["case_count"] = len(profile.get("cases") or [])
    search_values: list[Any] = [
        profile.get("display_name"),
        profile.get("bar_number"),
        profile.get("dept"),
        profile.get("code"),
        profile.get("key"),
        profile.get("legacy_key"),
        profile.get("attorney_id"),
    ]
    if profile.get("profile_type") == "attorney":
        search_values.extend([profile.get("parties"), profile.get("contacts")])
    else:
        search_values.append(profile.get("departments"))
    profile["search_text"] = profile_search_text(*search_values)
    return {k: v for k, v in profile.items() if not k.startswith("_")}


def build_entity_profiles(
    tables: dict[str, list[dict[str, Any]]],
    judges_path: Path = DEFAULT_JUDGES_JSON,
) -> dict[str, Any]:
    summaries = case_summary_map(tables)
    # Fold name: shadows into their bar: profile before building profiles (R5).
    attorney_table = consolidate_name_shadows(tables.get("attorneys", []))
    attorneys: list[dict[str, Any]] = []
    for row in attorney_table:
        name = norm_entity_name(row.get("name"))
        if not name:
            continue
        case_numbers = parse_json_list(row.get("case_numbers_json"))
        cases = profile_cases(case_numbers, summaries)
        key, legacy_key = attorney_profile_key(row)
        contacts = parse_json_list(row.get("contacts_json"))
        if not contacts:
            contacts = unique([row.get("contact_block"), row.get("address")])
        profile = {
            "profile_type": "attorney",
            "key": key,
            "legacy_key": legacy_key,
            "attorney_id": clean(row.get("attorney_id")),
            "display_name": name,
            "bar_number": norm_bar(row.get("bar_number")),
            "cases": cases,
            "case_count": len(cases),
            "parties": parse_json_list(row.get("parties_represented_json")),
            "contacts": contacts,
            "source": clean(row.get("source")),
            "confidence": row.get("confidence"),
            "first_captured_at": clean(row.get("first_captured_at")),
        }
        attorneys.append(finalize_profile(profile))

    roster = load_judge_roster(judges_path)
    judges: dict[str, dict[str, Any]] = {}
    for row in tables.get("calendar", []):
        officer = norm_entity_name(row.get("judge"))
        if not officer or is_pseudo_officer(officer):
            # Pseudo-officers (SETTLEMENT ATTORNEY n, VISITING/UNKNOWN JUDGE,
            # PRO TEM, TBA/TBD) are role placeholders, not judges -- their cases
            # are simply left unattributed rather than minting a judge profile.
            continue
        anchor, roster_entry = roster.resolve(officer)
        # Matched -> key on the canonical roster anchor so the bare calendar name
        # and the formal roster name merge into one profile. Unmatched -> the
        # officer's own first+last anchor, NAMESPACED with a "cal:" prefix so it
        # cannot collide with a roster anchor that resolve() deliberately rejected
        # (e.g. a different judge with the same surname but a conflicting middle):
        # otherwise the roster-seed loop below could stamp that roster dept onto an
        # off-roster judge. Two unmatched spellings of the same off-roster judge
        # still share one "cal:" key and merge with each other.
        profile_key = anchor or f"cal:{judge_match_key(officer)}"
        profile = get_judge_profile(judges, officer, roster_entry, profile_key=profile_key)
        if not profile:
            continue
        add_judge_case(profile, clean(row.get("case_number")), summaries)
        add_unique_profile_value(profile, "departments", row.get("location") or profile.get("dept") or "")
        calendar_label = " - ".join(filter(None, [clean(row.get("court_date")), clean(row.get("matters"))]))
        add_unique_profile_value(profile, "calendars", calendar_label, limit=200)
    # Seed roster judges that never appeared on a calendar, keyed on the same
    # canonical anchor so they attach to (not shadow) any calendar profile.
    for entry in roster.entries:
        get_judge_profile(judges, entry.get("name"), entry, profile_key=entry.get("anchor"))

    attorney_rows = sorted(
        attorneys,
        key=lambda r: (-int(r.get("case_count") or 0), clean(r.get("display_name"))),
    )
    judge_rows = sorted(
        (finalize_profile(profile) for profile in judges.values()),
        key=lambda r: (-int(r.get("case_count") or 0), clean(r.get("display_name"))),
    )
    return {
        "schema_version": 1,
        "source": "archive/cases/*.json via scripts/build_case_tables.py",
        "source_case_count": len(summaries),
        "attorneys": attorney_rows,
        "judges": judge_rows,
    }


def write_parquet_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".parquet", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        df.to_parquet(tmp, index=False, compression="zstd")
        if tmp.stat().st_size <= 0:
            raise RuntimeError(f"refusing to replace {path}: temporary parquet is empty")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".json", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        if tmp.stat().st_size <= 0:
            raise RuntimeError(f"refusing to replace {path}: temporary JSON is empty")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def entity_profile_manifest_path(profiles_out: Path) -> Path:
    return profiles_out.with_name(f"{profiles_out.stem}-manifest.json")


def entity_profile_shard_path(profiles_out: Path, kind: str, shard_index: int) -> Path:
    return profiles_out.with_name(f"{profiles_out.stem}-{kind}-{shard_index:03d}.json")


def cleanup_entity_profile_outputs(profiles_out: Path) -> None:
    profiles_out.unlink(missing_ok=True)
    entity_profile_manifest_path(profiles_out).unlink(missing_ok=True)
    for path in profiles_out.parent.glob(f"{profiles_out.stem}-attorneys-*.json"):
        path.unlink(missing_ok=True)
    for path in profiles_out.parent.glob(f"{profiles_out.stem}-judges-*.json"):
        path.unlink(missing_ok=True)


def shard_entity_records(records: list[dict[str, Any]], max_bytes: int) -> list[list[dict[str, Any]]]:
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 2  # JSON array brackets.
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        projected = current_size + len(encoded) + (1 if current else 0)
        if current and projected > max_bytes:
            shards.append(current)
            current = []
            current_size = 2
            projected = current_size + len(encoded)
        current.append(record)
        current_size = projected
    if current or not shards:
        shards.append(current)
    return shards


def write_entity_profiles_atomic(profiles_out: Path, profiles: dict[str, Any]) -> None:
    profiles_out = profiles_out.resolve()
    profiles_out.parent.mkdir(parents=True, exist_ok=True)
    cleanup_entity_profile_outputs(profiles_out)

    manifest: dict[str, Any] = {
        "schema_version": 2,
        "source_schema_version": profiles.get("schema_version", 1),
        "source": profiles.get("source", ""),
        "source_case_count": profiles.get("source_case_count", 0),
        "kinds": {},
    }
    for kind in ("attorneys", "judges"):
        records = profiles.get(kind) or []
        shards = []
        for shard_index, shard_records in enumerate(shard_entity_records(records, DEFAULT_ENTITY_PROFILE_SHARD_BYTES)):
            shard_path = entity_profile_shard_path(profiles_out, kind, shard_index)
            payload = {
                "schema_version": 2,
                "kind": kind,
                "source_case_count": profiles.get("source_case_count", 0),
                "records": shard_records,
            }
            write_json_atomic(shard_path, payload)
            shards.append({
                "path": shard_path.name,
                "count": len(shard_records),
                "bytes": shard_path.stat().st_size,
            })
        manifest["kinds"][kind] = {
            "count": len(records),
            "shards": shards,
        }

    write_json_atomic(entity_profile_manifest_path(profiles_out), manifest)


def case_representation_prefix(case_number: str) -> str:
    m = re.match(r"^([A-Za-z]+)", clean(case_number))
    if m:
        return m.group(1).upper()
    m = re.match(r"^\d{2}([A-Za-z]+)", clean(case_number))
    return m.group(1).upper() if m else "_none"


def case_representation_path(out_dir: Path, case_number: str) -> Path:
    prefix = case_representation_prefix(case_number)
    return out_dir / prefix / f"{norm_case(case_number)}.json"


def case_representation_empty_reason(
    case_row: dict[str, Any],
    parties: list[dict[str, Any]],
    attorneys: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> str:
    if edges:
        return ""
    if not parties and not attorneys:
        if clean(case_row.get("status")).lower() == "unavailable":
            return clean(case_row.get("unavailable_reason")) or "unavailable"
        return "no_party_or_attorney_rows"
    if parties and not attorneys:
        return "party_rows_without_attorney_rows"
    if attorneys and not parties:
        return "attorney_rows_without_party_rows"
    return "parser_zero_edges"


def should_write_case_representation_sidecar(
    case_number: str,
    parties: list[dict[str, Any]],
    attorneys: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    empty_reason: str,
) -> bool:
    prefix = case_representation_prefix(case_number)
    return (
        prefix == "CJC"
        or len(parties) >= CASE_REPRESENTATION_PARTY_THRESHOLD
        or len(attorneys) >= CASE_REPRESENTATION_ATTORNEY_THRESHOLD
        or len(edges) >= CASE_REPRESENTATION_EDGE_THRESHOLD
    )


def compact_case_party(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "party_id": clean(row.get("party_id")),
        "party_seq": row.get("party_seq"),
        "name": clean(row.get("party_name")),
        "party_type": clean(row.get("party_type")),
        "attorneys": parse_json_list(row.get("attorneys_json")),
        "filings_count": len(parse_json_list(row.get("filings_json"))),
    }


def compact_case_attorney(row: dict[str, Any], edge_parties: Iterable[str]) -> dict[str, Any]:
    return {
        "attorney_id": clean(row.get("attorney_id")),
        "name": clean(row.get("name")),
        "bar_number": norm_bar(row.get("bar_number")),
        "contact_block": clean(row.get("contact_block")) or clean(row.get("address")),
        "represented_parties": sorted({clean(p) for p in edge_parties if clean(p)}),
    }


def compact_case_edge(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "party_id": clean(row.get("party_id")),
        "party_name": clean(row.get("party_name")),
        "party_type": clean(row.get("party_type")),
        "attorney_id": clean(row.get("attorney_id")),
        "attorney_name": clean(row.get("attorney_name")),
        "bar_number": norm_bar(row.get("bar_number")),
        "source_field": clean(row.get("source_field")),
        "confidence": row.get("confidence"),
    }


def case_representation_summary(
    case_row: dict[str, Any],
    parties: list[dict[str, Any]],
    attorneys: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    empty_reason: str,
) -> dict[str, Any]:
    party_source_edges = sum(1 for row in edges if clean(row.get("source_field")).startswith("parties["))
    attorney_source_edges = sum(1 for row in edges if clean(row.get("source_field")).startswith("attorneys["))
    recommended_view = "table" if (
        case_representation_prefix(clean(case_row.get("case_number"))) == "CJC"
        or len(parties) >= CASE_REPRESENTATION_PARTY_THRESHOLD
        or len(attorneys) >= CASE_REPRESENTATION_ATTORNEY_THRESHOLD
        or len(edges) >= CASE_REPRESENTATION_EDGE_THRESHOLD
    ) else "diagram"
    return {
        "party_count": len(parties),
        "attorney_count": len(attorneys),
        "edge_count": len(edges),
        "empty_reason": empty_reason,
        "recommended_view": recommended_view,
        "page_size": CASE_REPRESENTATION_PAGE_SIZE,
        "pages": {
            "parties": (len(parties) + CASE_REPRESENTATION_PAGE_SIZE - 1) // CASE_REPRESENTATION_PAGE_SIZE,
            "attorneys": (len(attorneys) + CASE_REPRESENTATION_PAGE_SIZE - 1) // CASE_REPRESENTATION_PAGE_SIZE,
            "edges": (len(edges) + CASE_REPRESENTATION_PAGE_SIZE - 1) // CASE_REPRESENTATION_PAGE_SIZE,
        },
        "source_counts": {
            "party_attorney_edges": party_source_edges,
            "attorney_party_edges": attorney_source_edges,
        },
        "case_status": clean(case_row.get("status")),
        "unavailable_reason": clean(case_row.get("unavailable_reason")),
    }


def build_case_representation_payload(
    case_row: dict[str, Any],
    parties: list[dict[str, Any]],
    attorneys: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    empty_reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_number": clean(case_row.get("case_number")),
        "case_title": clean(case_row.get("case_title")),
        "captured_at": clean(case_row.get("captured_at")),
        "summary": case_representation_summary(case_row, parties, attorneys, edges, empty_reason),
        "parties": [compact_case_party(row) for row in parties],
        "attorneys": attorneys,
        "edges": [compact_case_edge(row) for row in edges],
    }


def write_case_representation_sidecars_atomic(
    out_dir: Path,
    manifest_path: Path,
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    out_dir = out_dir.resolve()
    manifest_path = manifest_path.resolve()
    tmp_dir = out_dir.with_name(out_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    parties_by_case: dict[str, list[dict[str, Any]]] = {}
    edges_by_case: dict[str, list[dict[str, Any]]] = {}
    attorney_parties_by_case: dict[str, dict[str, set[str]]] = {}
    for row in tables.get("parties", []):
        parties_by_case.setdefault(clean(row.get("case_number")), []).append(row)
    for row in tables.get("representation", []):
        case_number = clean(row.get("case_number"))
        edges_by_case.setdefault(case_number, []).append(row)
        aid = clean(row.get("attorney_id")) or attorney_id(clean(row.get("attorney_name")), clean(row.get("bar_number")))
        if aid:
            attorney_parties_by_case.setdefault(case_number, {}).setdefault(aid, set()).add(clean(row.get("party_name")))

    attorneys_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in tables.get("attorneys", []):
        aid = clean(row.get("attorney_id"))
        for case_number in parse_json_list(row.get("case_numbers_json")):
            edge_parties = attorney_parties_by_case.get(case_number, {}).get(aid, set())
            attorneys_by_case.setdefault(case_number, []).append(compact_case_attorney(row, edge_parties))

    sidecars: list[dict[str, Any]] = []
    zero_edge_cases: list[dict[str, Any]] = []
    empty_reason_counts: dict[str, int] = {}
    for case_row in sorted(tables.get("cases", []), key=lambda r: clean(r.get("case_number"))):
        case_number = clean(case_row.get("case_number"))
        if not case_number:
            continue
        parties = parties_by_case.get(case_number, [])
        attorneys = sorted(attorneys_by_case.get(case_number, []), key=lambda r: clean(r.get("name")))
        edges = edges_by_case.get(case_number, [])
        empty_reason = case_representation_empty_reason(case_row, parties, attorneys, edges)
        if empty_reason:
            empty_reason_counts[empty_reason] = empty_reason_counts.get(empty_reason, 0) + 1
            if parties or attorneys:
                zero_edge_cases.append({
                    "case_number": case_number,
                    "party_count": len(parties),
                    "attorney_count": len(attorneys),
                    "edge_count": len(edges),
                    "empty_reason": empty_reason,
                })
        if not should_write_case_representation_sidecar(case_number, parties, attorneys, edges, empty_reason):
            continue
        rel_path = case_representation_path(out_dir, case_number).relative_to(out_dir.parent)
        sidecar_path = tmp_dir / rel_path.relative_to(out_dir.name)
        payload = build_case_representation_payload(case_row, parties, attorneys, edges, empty_reason)
        write_json_atomic(sidecar_path, payload)
        summary = payload["summary"]
        sidecars.append({
            "case_number": case_number,
            "case_title": clean(case_row.get("case_title")),
            "path": rel_path.as_posix(),
            "prefix": case_representation_prefix(case_number),
            "party_count": summary["party_count"],
            "attorney_count": summary["attorney_count"],
            "edge_count": summary["edge_count"],
            "empty_reason": summary["empty_reason"],
            "recommended_view": summary["recommended_view"],
            "bytes": sidecar_path.stat().st_size,
        })

    manifest = {
        "schema_version": 1,
        "source": "archive/cases/*.json via scripts/build_case_tables.py",
        "source_case_count": len(tables.get("cases", [])),
        "page_size": CASE_REPRESENTATION_PAGE_SIZE,
        "thresholds": {
            "party_count": CASE_REPRESENTATION_PARTY_THRESHOLD,
            "attorney_count": CASE_REPRESENTATION_ATTORNEY_THRESHOLD,
            "edge_count": CASE_REPRESENTATION_EDGE_THRESHOLD,
            "always_prefixes": ["CJC"],
        },
        "sidecar_count": len(sidecars),
        "zero_edge_case_count": len(zero_edge_cases),
        "empty_reason_counts": dict(sorted(empty_reason_counts.items())),
        "sidecars": sidecars,
        "zero_edge_cases": zero_edge_cases,
    }
    write_json_atomic(manifest_path, manifest)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    tmp_dir.replace(out_dir)
    return manifest


def write_tables(tables: dict[str, list[dict[str, Any]]], out_dir: Path) -> None:
    import pandas as pd

    for name, rows in tables.items():
        write_parquet_atomic(out_dir / f"{name}.parquet", pd.DataFrame(rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", type=Path, default=DEFAULT_CASE_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--profiles-out",
        type=Path,
        default=DEFAULT_ENTITY_PROFILES,
        help="Base profile path. The writer emits a manifest and sharded JSON next to it.",
    )
    parser.add_argument("--representation-out-dir", type=Path, default=DEFAULT_CASE_REPRESENTATION_DIR)
    parser.add_argument("--representation-manifest", type=Path, default=DEFAULT_CASE_REPRESENTATION_MANIFEST)
    parser.add_argument("--judges-json", type=Path, default=DEFAULT_JUDGES_JSON)
    parser.add_argument("--profiles-only", action="store_true", help="Write only the sharded entity profile JSON.")
    parser.add_argument("--limit", type=int, default=None, help="Limit case files for smoke tests.")
    parser.add_argument("--no-write", action="store_true", help="Validate and print counts without writing derived files.")
    args = parser.parse_args(argv)

    case_dir = args.case_dir.resolve()
    if not case_dir.exists():
        raise SystemExit(f"case directory not found: {case_dir}")

    tables = rows_from_cases(case_dir, args.limit)
    entity_profiles = build_entity_profiles(tables, args.judges_json.resolve())
    for name, rows in tables.items():
        print(f"{name}: {len(rows)}")
    print(
        "entity_profiles: "
        f"{len(entity_profiles['attorneys'])} attorneys, "
        f"{len(entity_profiles['judges'])} judges"
    )
    if not args.no_write:
        if not args.profiles_only:
            write_tables(tables, args.out_dir.resolve())
            for name in tables:
                print(f"wrote {(args.out_dir.resolve() / f'{name}.parquet').relative_to(ROOT)}")
            representation_manifest = write_case_representation_sidecars_atomic(
                args.representation_out_dir,
                args.representation_manifest,
                tables,
            )
            print(
                f"wrote {args.representation_manifest.resolve().relative_to(ROOT)} "
                f"and {representation_manifest['sidecar_count']} representation sidecar(s)"
            )
        profiles_out = args.profiles_out.resolve()
        write_entity_profiles_atomic(profiles_out, entity_profiles)
        print(f"wrote {entity_profile_manifest_path(profiles_out).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
