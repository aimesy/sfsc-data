#!/usr/bin/env python3
"""Cross-case litigant aggregator + identity-confidence scoring (first version).

Builds the per-litigant index described in DESIGN.md §5.3 (fee aggregates +
name provenance), §5.10 (identity resolution — confidence scoring) and §10
(entity profiles). It is a CROSS-CASE AGGREGATOR over data we ALREADY have; it
does NOT touch the live court site and does NOT run a name-search driver
(FindCaseName / CaseNumFromName) — that needs a live session and belongs
on-device / on a VPS (see DESIGN §3, §5; left for later).

Two name sources, in authority order (docket > tentative, per DESIGN §6):
  1. CAPTURED cases (archive/cases/*.json) — the ``parties[]`` array is the
     court's Parties tab: a court-confirmed name + party_type + attorney list.
     This is the strong, docket-authoritative source.
  2. ENRICHED TENTATIVES — party names parsed out of the case_title caption by
     scripts/enrich_cases.py (split_versus + split_side + classify_party). Weak:
     captions are truncated and adversary-only, so these are corroboration /
     coverage, never the sole basis for a high-confidence merge.

Clustering. Names are grouped by the SHARED normalizer from enrich_cases
(``normalize_key``: uppercase, drop punctuation, drop middle-initials and a small
suffix set). Embedded contact tails the court appends to a party name
(e.g. "BERG, JEROME 1255 POST SF, CA 94109") are stripped FIRST, and the stripped
address is kept as a corroborating contact signal. The normalized key is the
blocking key: every occurrence with the same key forms one candidate cluster.
A normalized key is explicitly NOT an identity assertion (DESIGN §5.10) — it is a
hypothesis that we then SCORE.

Confidence score (DESIGN §5.10). For each cluster we compute, as additive
LOG-ODDS POINTS, the probability that the cluster is a single real entity, then
squash to 0..1 with a logistic. Every contributing factor is stored verbatim on
the record (``confidence_factors``) so the UI hover can show the exact math
(DESIGN §5.10 step 5). Singleton clusters (one occurrence) are certain by
construction. The factors, with TUNABLE weights declared at the top:

  * Entity-type prior (dominant). Organizations have near-unique legal names and
    litigate at scale, so many cases is EXPECTED, not suspicious -> high prior,
    volume never penalized. Individuals mostly appear in 1-2 cases ever, so a
    personal name across many cases is more likely many different people sharing
    a common name -> low prior, and each extra case adds a volume penalty.
  * Corroborating signals (raise confidence above the name prior):
      - shared attorney across occurrences  (MODERATE)
      - shared contact (address/phone tail) across occurrences (STRONG)
      - shared case-type / court prefix across occurrences (WEAK)
      - exact (vs merely fuzzy/normalized) surface-form match, but only across
        non-defendant-side roles that plausibly control their own captioned name
        (small boost)
  * Vexatious-lift. A JC-listed vexatious INDIVIDUAL genuinely does have many
    cases (DESIGN §5.10 step 1 exception), so for them we REMOVE the individual
    volume penalty. This consumes the AUTHORITATIVE vexatious flag only; it does
    NOT infer vexatiousness and does NOT conflate it with identity-confidence.

Hard guards (DESIGN §5.10 step 6): DOE/ROE/fictitious placeholders and bare
numeric defendant ranges ("1-20") are never emitted; well-known generic public
bodies (CITY AND COUNTY OF SAN FRANCISCO, THE PEOPLE) are treated as singleton
entities (kept, but never used to claim a same-person merge).

Outputs:
  * data/litigants-parquet.json       — manifest for the canonical Parquet store.
  * data/litigants-parquet/*.parquet  — one row per cluster, sharded under GitHub limits.
  * data/litigants.json               — small browser manifest for curated JSON shards.
  * data/litigants/*.json             — curated browser rows, sharded under GitHub limits.

Per-litigant columns: litigant_id, display_name, norm_key, entity_type,
entity_subtype, case_numbers[], case_count, party_types[], attorneys[], aliases[],
confidence, confidence_factors[] (structured: {factor, detail, points}),
total_fees_waived, total_fees_paid, total_fees_repaid, last_seen,
pull_history[{case_number, captured_at}], name_sources (docket/tentative/both),
is_vexatious_authoritative (carried through, NOT inferred).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import shutil
import sys

import pandas as pd

# Reuse the SHARED normalizer + classifier from the enrichment pipeline so the
# litigant index groups names exactly the way the tag pipeline does.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_cases  # noqa: E402  (normalize_key, classify_party, split_versus, split_side, display_name)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_GLOB = os.path.join(REPO_ROOT, "archive", "cases", "*.json")
TENTATIVES_GLOB = os.path.join(REPO_ROOT, "data", "tentatives-*.parquet")
FINANCIALS_PARQUET = os.path.join(REPO_ROOT, "data", "financials.parquet")
VEXATIOUS_PARQUET = os.path.join(REPO_ROOT, "data", "vexatious.parquet")
LEGACY_OUT_PARQUET = os.path.join(REPO_ROOT, "data", "litigants.parquet")
OUT_PARQUET_MANIFEST = os.path.join(REPO_ROOT, "data", "litigants-parquet.json")
OUT_PARQUET_DIR = os.path.join(REPO_ROOT, "data", "litigants-parquet")
OUT_JSON = os.path.join(REPO_ROOT, "data", "litigants.json")
OUT_JSON_SHARD_DIR = os.path.join(REPO_ROOT, "data", "litigants")
PARQUET_SHARD_ROWS = 100_000
PARQUET_SHARD_HARD_MAX_BYTES = 90 * 1024 * 1024
JSON_SHARD_TARGET_BYTES = 25 * 1024 * 1024
PARQUET_JSON_COLUMNS = (
    "case_numbers",
    "party_types",
    "attorneys",
    "aliases",
    "contacts",
    "confidence_factors",
    "pull_history",
)

# ---------------------------------------------------------------------------
# TUNABLE SCORING WEIGHTS — all confidence math lives here (DESIGN §5.10).
# Units are LOG-ODDS POINTS (natural log); they are summed then squashed by a
# logistic to 0..1. To read them: +2.2 pts ~= 90% on its own, -2.2 ~= 10%.
# Calibrate against hand-labeled pairs over time (re-runs in CI; nothing lost).
# ---------------------------------------------------------------------------

# Entity-type base prior (step 1). Organizations: near-unique legal names, expect
# scale -> strong positive prior. Individuals: most appear once -> near-neutral
# prior at one case, with a per-extra-case penalty applied separately. Unknown:
# neutral.
PRIOR_ENTITY = 2.0           # organization same-entity prior (~88%)
PRIOR_INDIVIDUAL = 0.4       # individual base prior at 1-2 cases (~60%)
PRIOR_UNKNOWN = 0.0          # unclassifiable -> coin flip before other signals

# Individual volume penalty (steps 1+2): each case BEYOND the first lowers the
# same-person prior, because a personal name across many cases is more likely
# many different people. Capped so corroboration can still recover it.
INDIV_VOLUME_PENALTY_PER_CASE = -0.9
INDIV_VOLUME_PENALTY_CAP = -4.0

# Corroborating signals (step 3).
SIG_SHARED_ATTORNEY = 1.1     # same attorney name across occurrences (MODERATE)
SIG_SHARED_CONTACT = 2.0      # same address/contact tail across occ. (STRONG)
SIG_SHARED_CASETYPE = 0.5     # same case-type prefix across occurrences (WEAK)
SIG_EXACT_SELF_DESCRIBED_SURFACE = 0.4       # matching controlled surface form
SIG_FUZZY_SELF_DESCRIBED_SURFACE_PENALTY = -0.6
# Two cases that literally cite each other's case number are the SAME dispute
# ("intertwined with case number PES-10-293352"), so a litigant appearing in
# both is almost certainly the same party. STRONG positive identity signal.
SIG_CROSSREF_SAME_MATTER = 2.2

# Confidence thresholds (step 4) — surfaced for the viewer; not used to gate
# output (we emit everything with its score).
T_CERTAIN = 0.95
T_PROBABLE = 0.70

# ---------------------------------------------------------------------------
# Guards (DESIGN §5.10 step 6) — names we never treat as a real identity.
# ---------------------------------------------------------------------------
# DOE/ROE/fictitious/placeholder defendants and bare numeric defendant ranges.
# Note the DOE/ROE/MOE word must be a WHOLE TOKEN — real surnames that merely
# CONTAIN those letters (Doeden, Roebuck, Roettgers, Villacarlos "Roel ...") are
# NOT placeholders and must survive. Anonymized parties use the bare token, often
# with an infix initial/number ("JANE H. DOE 1", "JOHN SF-18 DOE", "Bernard
# Doe"), so we match the token anywhere in the name.
PLACEHOLDER_RE = re.compile(
    r"\b(?:DOE|DOES|ROE|ROES|MOE|MOES)\b"   # whole-token DOE/ROE/MOE anywhere
    r"|^\s*ALL\s+PERSONS\b"
    r"|^\s*\d+\s*[-/]\s*\d+\s*$"            # pure numeric range "1-20"
    r"|UNKNOWN\s+(?:CLAIMANTS|HEIRS|PERSONS)"
    , re.IGNORECASE)

# Generic public bodies that are real, singular entities but must never be used
# to assert a same-person merge across cases (they are one body by definition).
GENERIC_SINGLETON_RE = re.compile(
    r"CITY AND COUNTY OF SAN FRANCISCO|THE PEOPLE(?:\s+OF\s+THE\s+STATE)?"
    r"|STATE OF CALIFORNIA|UNITED STATES OF AMERICA"
    r"|REGENTS OF THE UNIVERSITY", re.IGNORECASE)

# Address / contact tail the court appends to a party name on the Parties tab,
# e.g. "BERG, JEROME 1255 POST SF, CA 94109" or "... POST OFFICE BOX 15186 ...".
# Captures everything from the first street-number / PO-box token onward.
CONTACT_TAIL_RE = re.compile(
    r"\s+(?P<tail>(?:\d{1,6}\s+\S|P\.?\s?O\.?\s+BOX|POST\s+OFFICE\s+BOX|"
    r"PO\s+BOX|\d{1,6}\s+[A-Z]).*)$")
CONTACT_CARE_OF_RE = re.compile(
    r"\s+(?P<tail>(?:ATTN:?\s+|C/O\s+|CARE\s+OF\s+|"
    r"AGENT\s+FOR\s+SERVICE\s+OF\s+PROCESS\b).*)$",
    re.IGNORECASE)

# Capacity/status text belongs in aliases/provenance, not canonical names.
CAPACITY_TAIL_RE = re.compile(
    r"\s*,?\s+(?P<tail>"
    r"INDIVIDUALLY\b.*|"
    r"ON\s+BEHALF\s+OF\b.*|"
    r"AS\s+(?:AN?\s+)?(?:AGGRIEVED\s+EMPLOYEE|GUARDIAN\s+AD\s+LITEM|"
    r"PERSONAL\s+REPRESENTATIVE|EXECUTOR|ADMINISTRATOR|CONSERVATOR|"
    r"TRUSTEE|SUCCESSOR\s+TRUSTEE|REPRESENTATIVE|PARENT|MINOR|"
    r"HEIR|BENEFICIARY)\b.*)"
    r"$",
    re.IGNORECASE)
DESCRIPTOR_TAIL_RE = re.compile(
    r"\s*,+\s*(?P<tail>(?:AN?\s+)?(?:INDIVIDUAL|PERSON|BUSINESS\s+ENTITY|"
    r"CALIFORNIA\s+CORPORATION|CORPORATION|CALIFORNIA\s+LIMITED\s*,?\s*"
    r"LIABILITY\s+COMPANY|LIMITED\s+LIABILITY\s+COMPANY|"
    r"CALIFORNIA\s+GENERAL\s+PARTNERSHIP|GENERAL\s+PARTNERSHIP)\s*)$",
    re.IGNORECASE)

ALIASED_AS_RE = (
    r"(?:erroneously\s+)?(?:sued|named)(?:\s+and\s+served)?"
    r"(?:\s+herein)?\s+as"
)
AKA_RE = (
    r"(?:aka|a/k/a|also\s+known\s+as|formerly\s+known\s+as|fka|f/k/a|"
    r"dba|d/b/a|doing\s+business\s+as)"
)

# Attorney decoration the captured Parties tab leaves on attorney strings.
ATTY_CLEAN_RE = re.compile(r"<br>|\(Deactive[^)]*\)", re.IGNORECASE)
PRO_PER_RE = re.compile(r"^\s*(?:PRO\s*PER|IN\s+PRO\s+PER|PRO\s*SE)\s*$", re.IGNORECASE)
DEFENDANT_SIDE_ROLE_RE = re.compile(
    r"\b(?:DEFENDANT|RESPONDENT|DEBTOR|CONSERVATEE|CROSS[-\s]*DEFENDANT|"
    r"REAL\s+PARTY)\b",
    re.IGNORECASE,
)
CONTROLLED_NAME_ROLE_RE = re.compile(
    r"\b(?:PLAINTIFF|PETITIONER|CLAIMANT|CREDITOR|CROSS[-\s]*COMPLAINANT|"
    r"OBJECTOR|APPLICANT)\b",
    re.IGNORECASE,
)


def clean_text(s) -> str:
    if s is None:
        return ""
    s = re.sub(r"\s+", " ", str(s).replace("<br>", " ")).strip()
    # Collapse comma runs left behind when an alias/capacity phrase is removed
    # from the MIDDLE of a name ("SMITH, , AS TRUSTEE" -> "SMITH, AS TRUSTEE"),
    # which otherwise produced thousands of records with a ", ," artifact.
    s = re.sub(r"(?:\s*,\s*){2,}", ", ", s)
    return s.strip()


def split_contact(name: str):
    """Return (clean_name, contact_tail|None). Strips an embedded address tail."""
    name = clean_text(name)
    m = CONTACT_CARE_OF_RE.search(name)
    if m:
        return name[: m.start()].strip().rstrip(",;"), clean_text(m.group("tail"))
    m = CONTACT_TAIL_RE.search(name)
    if m:
        return name[: m.start()].strip().rstrip(",;"), clean_text(m.group("tail"))
    return name, None


def strip_party_accoutrements(name: str) -> tuple[str, list[str]]:
    """Canonical party name plus removed literal/capacity descriptors."""
    clean = clean_text(name).strip(" ,;")
    removed: list[str] = []
    changed = True
    while changed:
        changed = False
        for rx in (CAPACITY_TAIL_RE, DESCRIPTOR_TAIL_RE):
            m = rx.search(clean)
            if not m:
                continue
            tail = clean_text(m.group("tail")).strip(" ,;")
            clean = clean[:m.start()].strip(" ,;")
            if tail:
                removed.append(tail)
            changed = True
    return clean_text(clean), removed


def split_party_aliases(name: str) -> tuple[str, list[str]]:
    """Strip alias/status phrases from a party name and return alias names."""
    clean = clean_text(name)
    aliases: list[str] = []

    def keep_alias(m):
        aliases.append(clean_text(m.group(1)))
        return " "

    clean = re.sub(rf"\(\s*(?:{ALIASED_AS_RE})\s+([^)]+)\)", keep_alias, clean, flags=re.I)
    clean = re.sub(rf"\(\s*(?:{AKA_RE})\s+([^)]+)\)", keep_alias, clean, flags=re.I)
    clean = re.sub(rf"\b(?:{ALIASED_AS_RE})\s+([^,;()]+)(?=$|[,;])", keep_alias, clean, flags=re.I)
    clean = re.sub(rf"\b(?:{AKA_RE})\s+([^,;()]+)(?=$|[,;])", keep_alias, clean, flags=re.I)

    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        alias_clean, _contact = split_contact(alias)
        alias_type, _alias_subtype = enrich_cases.classify_party(alias_clean)
        alias_key = litigant_norm_key(alias_clean, alias_type)
        if alias_key and alias_key not in seen:
            seen.add(alias_key)
            out.append(alias_clean)
    return clean_text(clean), out


def canonical_party_name(raw_name: str) -> tuple[str, str | None, list[str], list[str]]:
    """Canonical profile name, contact tail, all aliases, identity aliases only."""
    literal = clean_text(raw_name)
    base_name, identity_aliases = split_party_aliases(literal)
    no_contact, contact = split_contact(base_name)
    clean, stripped = strip_party_accoutrements(no_contact)
    out_aliases = list(identity_aliases)
    out_aliases.extend(stripped)
    if literal and enrich_cases.normalize_key(literal) != enrich_cases.normalize_key(clean):
        out_aliases.append(literal)
    # De-dup by normalized surface, but keep literal spelling.
    seen: set[str] = set()
    deduped: list[str] = []
    for alias in out_aliases:
        alias = clean_text(alias).strip(" ,;")
        key = litigant_norm_key(alias, enrich_cases.classify_party(alias)[0])
        if not alias or not key or key in seen:
            continue
        seen.add(key)
        deduped.append(alias)
    identity_keys = {
        litigant_norm_key(a, enrich_cases.classify_party(a)[0])
        for a in identity_aliases
        if litigant_norm_key(a, enrich_cases.classify_party(a)[0])
    }
    identity_deduped = [
        a for a in deduped
        if litigant_norm_key(a, enrich_cases.classify_party(a)[0]) in identity_keys
    ]
    return clean, contact, deduped, identity_deduped


def derived_individual_name_parts(display_name: str, entity_type: str) -> dict[str, str]:
    """Best-effort derived name fields for UI filtering/display."""
    if entity_type != "individual":
        return {}
    name = clean_text(display_name).strip(" ,;")
    if not name:
        return {}
    if "," in name:
        last, rest = name.split(",", 1)
        tokens = clean_text(rest).split()
        first = tokens[0] if tokens else ""
        middle = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        return {
            "first_name": first,
            "middle_name": middle,
            "last_name": clean_text(last),
            "name_order": "family-given",
        }
    tokens = name.split()
    if len(tokens) == 1:
        return {"first_name": tokens[0], "middle_name": "", "last_name": "", "name_order": "given-only"}
    return {
        "first_name": tokens[0],
        "middle_name": " ".join(tokens[1:-1]),
        "last_name": tokens[-1],
        "name_order": "given-family",
    }


# Trailing corporate/firm-form tokens that the SAME organization is written with
# or without ("PACIFIC GAS AND ELECTRIC" == "... COMPANY"; "WELLS FARGO BANK" ==
# "... N.A."). Stripped from the END of an ENTITY's blocking key so those
# spellings cluster instead of fragmenting. Guarded to never reduce a key below
# two tokens, so "SMITH CO" is NOT collapsed into the surname "SMITH".
_ENTITY_SUFFIX_TOKENS = {
    "COMPANY", "COMPANIES", "CO", "CORP", "CORPORATION", "INCORPORATED", "INC",
    "LLC", "LLP", "LP", "PLLC", "PC", "LTD", "LIMITED", "NA",
}


def _strip_entity_suffix_tokens(key: str) -> str:
    toks = key.split()
    # Trailing "NATIONAL ASSOCIATION" (the bank form of N.A.) drops as a pair.
    while len(toks) > 3 and toks[-2:] == ["NATIONAL", "ASSOCIATION"]:
        toks = toks[:-2]
    while len(toks) > 2 and toks[-1] in _ENTITY_SUFFIX_TOKENS:
        toks.pop()
    return " ".join(toks)


def litigant_norm_key(name: str, entity_type: str | None = None) -> str:
    """Blocking key for litigants, with comma-form individuals reordered."""
    clean = clean_text(name)
    if entity_type in (None, "individual", "unknown") and "," in clean:
      left, rest = clean.split(",", 1)
      right = clean_text(rest)
      left = clean_text(left)
      if left and right:
          return enrich_cases.normalize_key(f"{right} {left}")
    key = enrich_cases.normalize_key(clean)
    # Organizations: fold corporate-suffix spelling variants onto one key.
    if entity_type == "entity":
        key = _strip_entity_suffix_tokens(key)
    return key


def clean_attorneys(raw_list) -> list[str]:
    """Normalize party attorneys: split combined lists and drop Pro Per markers."""
    out: list[str] = []
    for a in raw_list or []:
        a = ATTY_CLEAN_RE.sub("", str(a))
        a = re.sub(r"(?<=[A-Za-z])PRO\s*PER\b", ", PRO PER", a, flags=re.I)
        a = re.sub(r"\b(?:IN\s+PRO\s+PER|PRO\s*PER|PRO\s*SE)\b", "PRO PER", a, flags=re.I)
        a = clean_text(a)
        if not a:
            continue
        parts = [clean_text(p).strip(" `;") for p in re.split(r"\s*,\s*", a) if clean_text(p).strip(" `;")]
        i = 0
        while i < len(parts):
            part = parts[i]
            if PRO_PER_RE.match(part):
                i += 1
                continue
            if i + 1 < len(parts) and not PRO_PER_RE.match(parts[i + 1]):
                name = f"{part}, {parts[i + 1]}"
                i += 2
                if i < len(parts) and re.fullmatch(r"JR\.?|SR\.?|II|III|IV|V", parts[i], flags=re.I):
                    name += f", {parts[i]}"
                    i += 1
            else:
                name = part
                i += 1
            name = clean_text(name).strip(" ,;`").upper()
            if name and not PRO_PER_RE.match(name):
                out.append(name)
    # de-dup, preserve order
    seen, uniq = set(), []
    for a in out:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def is_placeholder(name: str) -> bool:
    n = clean_text(name)
    return not n or bool(PLACEHOLDER_RE.search(n))


def role_controls_captioned_name(role: str | None) -> bool:
    """Whether surface-form sameness is meaningful for this party role."""
    r = clean_text(role or "")
    if not r or DEFENDANT_SIDE_ROLE_RE.search(r):
        return False
    return bool(CONTROLLED_NAME_ROLE_RE.search(r))


def case_type_prefix(case_number: str) -> str:
    """Leading alpha prefix of an SF case number (CGC/CUD/PES/FDI/...)."""
    m = re.match(r"^([A-Za-z]+)", case_number or "")
    return m.group(1).upper() if m else ""


# ---------------------------------------------------------------------------
# Cross-case reference extraction (DESIGN §5.10 step 3 — litigation-pattern /
# same-matter signal). When one case's docket/calendar TEXT literally names
# ANOTHER case's number ("intertwined with case number PES-10-293352", "related
# matter ...", "consolidated with ..."), that is strong evidence the two cases
# are the same dispute, hence that a litigant appearing in BOTH is the same real
# person/entity. We harvest these references once per case during the case scan
# and feed them to the scorer as a positive confidence factor.
# ---------------------------------------------------------------------------
# SF case numbers are <2-4 letter prefix><digits>: dashed "CGC-10-1234567" /
# "PES-10-293352" or dashless "CGC101234567" / "PES10293352". We canonicalize
# every reference to DASHLESS UPPERCASE (the form stored in archive/cases).
#
# False-positive guard (CRITICAL): the dashed shape [A-Z]{2,4}-\d{2}-\d{4,7}
# ALSO matches calendar/docket DATES like "MAY-03-2010", "JUN-28-2010". Real SF
# case prefixes are never English month abbreviations, so we reject any "month
# abbreviation" prefix outright. (Verified on real data: PED10293354 / PES10293352
# docket+calendar text is full of MAY-/JUN-/etc. date tokens that must NOT be
# read as case-number references.)
_MONTH_ABBR = {
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "SEPT",
    "OCT", "NOV", "DEC",
}
# Dashed form: PREFIX-YY-NNNN.. (prefix 2-4 letters, a 2-digit year segment,
# then 4-7 digits). Dashless form: PREFIX + 8-9 digits.
_CASE_REF_DASHED_RE = re.compile(r"\b([A-Z]{2,4})-(\d{2})-(\d{4,7})\b")
_CASE_REF_DASHLESS_RE = re.compile(r"\b([A-Z]{2,4})(\d{8,9})\b")


def normalize_case_number(s: str) -> str:
    """Canonical DASHLESS UPPERCASE case number ("PES-10-293352" -> "PES10293352")."""
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def extract_case_refs(text: str, self_cn: str | None = None) -> set[str]:
    """Return the set of OTHER case numbers referenced in `text` (dashless upper).

    High-precision: rejects month-abbreviation prefixes (date false positives)
    and drops a reference equal to the case's own number (self-references are not
    cross-references). Matches both the dashed and dashless surface forms.
    """
    refs: set[str] = set()
    if not text:
        return refs
    up = text.upper()
    self_norm = normalize_case_number(self_cn) if self_cn else None
    for m in _CASE_REF_DASHED_RE.finditer(up):
        prefix = m.group(1)
        if prefix in _MONTH_ABBR:           # date like MAY-03-2010 — not a case
            continue
        cn = f"{prefix}{m.group(2)}{m.group(3)}"
        if cn != self_norm:
            refs.add(cn)
    for m in _CASE_REF_DASHLESS_RE.finditer(up):
        prefix = m.group(1)
        if prefix in _MONTH_ABBR:           # extremely unlikely dashless, but safe
            continue
        cn = f"{prefix}{m.group(2)}"
        if cn != self_norm:
            refs.add(cn)
    return refs


def case_text_blob(d: dict) -> str:
    """Concatenate the free-text fields of a case JSON for reference scanning.

    Covers the fields that actually carry cross-case prose: docket entry
    descriptions, calendar `matters` minute text (where "intertwined with case
    number PES-10-293352" really appears in the wild), case_title, and
    cause_of_action. Bounded to the fields we know carry text, not the whole
    JSON, so document/digest hex strings can't masquerade as case numbers.
    """
    parts: list[str] = []
    for de in d.get("docket_entries") or []:
        if isinstance(de, dict) and de.get("description"):
            parts.append(str(de["description"]))
    for cal in d.get("calendar") or []:
        if isinstance(cal, dict) and cal.get("matters"):
            parts.append(str(cal["matters"]))
    if d.get("case_title"):
        parts.append(str(d["case_title"]))
    coa = d.get("cause_of_action")
    if isinstance(coa, str):
        parts.append(coa)
    elif isinstance(coa, list):
        parts.extend(str(x) for x in coa)
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Occurrence collection
# ---------------------------------------------------------------------------
class Occurrence:
    """One (case, name, role) mention of a litigant, from one source."""
    __slots__ = ("case_number", "raw_name", "clean_name", "norm_key",
                 "entity_type", "entity_subtype", "party_type", "attorneys",
                 "contact", "aliases", "identity_aliases", "source",
                 "captured_at", "case_type")

    def __init__(self, case_number, raw_name, party_type, attorneys, source,
                 captured_at):
        self.case_number = case_number
        self.raw_name = raw_name
        clean, contact, aliases, identity_aliases = canonical_party_name(raw_name)
        et, st = enrich_cases.classify_party(clean)
        self.entity_type, self.entity_subtype = et, st
        self.clean_name = clean
        self.contact = contact
        self.norm_key = litigant_norm_key(clean, et)
        self.aliases = aliases
        self.identity_aliases = identity_aliases
        self.party_type = (party_type or "").strip().upper() or None
        self.attorneys = clean_attorneys(attorneys)
        self.source = source           # "docket" | "tentative"
        self.captured_at = captured_at
        self.case_type = case_type_prefix(case_number)


def collect_captured() -> tuple[list[Occurrence], dict[str, set[str]]]:
    """Occurrences from archive/cases/*.json parties[] (docket-authoritative).

    Also returns a cross-reference map ``case_xrefs``: dashless-uppercase case
    number -> set of OTHER dashless case numbers its docket/calendar text names
    (DESIGN §5.10 step 3 same-matter signal; see extract_case_refs). Built here so
    the case JSON is read exactly once.
    """
    occ: list[Occurrence] = []
    case_xrefs: dict[str, set[str]] = {}
    for f in sorted(glob.glob(ARCHIVE_GLOB)):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  skip unreadable {os.path.basename(f)}: {e}", file=sys.stderr)
            continue
        if not isinstance(d, dict):
            continue
        cn = d.get("case_number")
        if not cn:
            continue
        captured_at = d.get("captured_at")
        for p in d.get("parties") or []:
            raw = clean_text(p.get("name"))
            if is_placeholder(raw):
                continue
            occ.append(Occurrence(cn, raw, p.get("party_type"),
                                  p.get("attorneys"), "docket", captured_at))
        # Harvest cross-case references from this case's free text (same-matter
        # signal). Keyed by the case's own dashless-uppercase number.
        refs = extract_case_refs(case_text_blob(d), self_cn=cn)
        if refs:
            case_xrefs.setdefault(normalize_case_number(cn), set()).update(refs)
    return occ, case_xrefs


def collect_tentatives() -> list[Occurrence]:
    """Occurrences parsed from enriched tentative captions (weak corroboration).

    One row per (case, side-party). No attorneys/contact (captions don't carry
    them). captured_at is None (tentatives aren't device captures).
    """
    occ: list[Occurrence] = []
    seen_case_name: set[tuple[str, str]] = set()
    for f in glob.glob(TENTATIVES_GLOB):
        if "extras" in f:
            continue
        df = pd.read_parquet(f, columns=["case_number", "case_title"])
        for cn, title in zip(df["case_number"], df["case_title"]):
            if cn is None or title is None:
                continue
            cn = str(cn)
            left, right, marker = enrich_cases.split_versus(str(title))
            sides = []
            if marker:
                sides = [(left, "PLAINTIFF"), (right, "DEFENDANT")]
            for side_text, role in sides:
                if not side_text:
                    continue
                parties, _ = enrich_cases.split_side(side_text)
                for nm in parties:
                    nm = clean_text(nm)
                    if is_placeholder(nm):
                        continue
                    key = (cn, nm.upper())
                    if key in seen_case_name:
                        continue
                    seen_case_name.add(key)
                    occ.append(Occurrence(cn, nm, role, None, "tentative", None))
    return occ


# ---------------------------------------------------------------------------
# Financial + vexatious side inputs
# ---------------------------------------------------------------------------
def load_fee_aggregates() -> dict[str, dict]:
    """Per-case fee roll-up from data/financials.parquet (DESIGN §5.3 / §7).

    The financials table is per fee EVENT (kind/direction/amount). We can't yet
    attribute a fee to a SPECIFIC party (no reliable party link in the rows), so
    we attribute a case's fee totals to ALL of that case's litigants — a coarse
    first pass, flagged in DESIGN as identity-confidence-gated. waived vs paid:
    a fee_waiver kind -> waived; everything else with an amount -> paid; an
    'awarded_to' direction that is later satisfied -> repaid (best-effort; 0 if
    not derivable).
    """
    by_case: dict[str, dict] = {}
    if not os.path.exists(FINANCIALS_PARQUET):
        return by_case
    df = pd.read_parquet(FINANCIALS_PARQUET,
                         columns=["case_number", "amount", "kind", "direction"])
    for cn, amount, kind, direction in zip(df["case_number"], df["amount"],
                                           df["kind"], df["direction"]):
        if cn is None or amount is None or (isinstance(amount, float) and math.isnan(amount)):
            continue
        cn = enrich_cases_norm_case(str(cn))
        rec = by_case.setdefault(cn, {"waived": 0.0, "paid": 0.0, "repaid": 0.0})
        kind = (kind or "").lower()
        if "waiver" in kind or kind == "fee_waiver":
            rec["waived"] += float(amount)
        else:
            rec["paid"] += float(amount)
    return by_case


def enrich_cases_norm_case(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def load_vexatious_individuals() -> set[str]:
    """Normalized keys of JC-listed vexatious litigant NAMES (authoritative).

    Used ONLY to lift the individual volume penalty (DESIGN §5.10 step 1
    exception). We never infer vexatiousness here. The vexatious parquet stores a
    litigant_name per row — normalize it with the same key.
    """
    keys: set[str] = set()
    if not os.path.exists(VEXATIOUS_PARQUET):
        return keys
    df = pd.read_parquet(VEXATIOUS_PARQUET, columns=["litigant_name"])
    for nm in df["litigant_name"]:
        if nm:
            keys.add(enrich_cases.normalize_key(clean_text(nm)))
    return keys


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def cluster_entity_type(occs: list[Occurrence]) -> tuple[str, str | None]:
    """Resolve the cluster entity type: entity wins (orgs have strong tokens),
    else individual, else unknown. Subtype = most specific seen."""
    types = {o.entity_type for o in occs}
    if "entity" in types:
        subtypes = [o.entity_subtype for o in occs
                    if o.entity_type == "entity" and o.entity_subtype
                    and o.entity_subtype != "unknown"]
        return "entity", (subtypes[0] if subtypes else None)
    if "individual" in types:
        return "individual", None
    return "unknown", None


def score_cluster(occs: list[Occurrence], entity_type: str,
                  name_distinct_occ_count: int, is_vexatious: bool,
                  case_xrefs: dict[str, set[str]] | None = None):
    """Return (confidence_0_1, factors[]). factors are {factor, detail, points}.

    name_distinct_occ_count is accepted for compatibility only. Corpus
    occurrence count is the candidate cluster itself, not independent evidence of
    how many distinct real people share the name, so it is not scored.
    """
    factors: list[dict] = []
    case_numbers = sorted({o.case_number for o in occs})
    n_cases = len(case_numbers)

    # Singleton (one case) -> certain by construction, no merge claimed.
    if n_cases <= 1:
        factors.append({"factor": "singleton", "points": 0.0,
                        "detail": "appears in a single case — no cross-case merge claimed"})
        return 1.0, factors

    pts = 0.0

    # --- Step 1: entity-type prior --------------------------------------
    if entity_type == "entity":
        pts += PRIOR_ENTITY
        factors.append({"factor": "entity_prior", "points": PRIOR_ENTITY,
                        "detail": f"organization — near-unique legal name, litigates at scale "
                                  f"({n_cases} cases expected, not suspicious)"})
    elif entity_type == "individual":
        pts += PRIOR_INDIVIDUAL
        factors.append({"factor": "individual_prior", "points": PRIOR_INDIVIDUAL,
                        "detail": "individual — base prior (most people appear in 1-2 cases)"})
        # Volume penalty for individuals, unless JC-vexatious (genuinely many cases).
        if is_vexatious:
            factors.append({"factor": "vexatious_lift", "points": 0.0,
                            "detail": "JC-listed vexatious individual — volume penalty waived "
                                      "(authoritative flag; identity NOT inferred from it)"})
        else:
            pen = max(INDIV_VOLUME_PENALTY_CAP,
                      INDIV_VOLUME_PENALTY_PER_CASE * (n_cases - 1))
            pts += pen
            factors.append({"factor": "individual_volume", "points": round(pen, 3),
                            "detail": f"individual in {n_cases} cases — likely several distinct "
                                      f"people sharing a common name"})
    else:
        pts += PRIOR_UNKNOWN
        factors.append({"factor": "unknown_type_prior", "points": PRIOR_UNKNOWN,
                        "detail": "entity type unclassified — neutral prior"})

    # Corpus-frequency "name rarity" is deliberately not scored. Counting
    # occurrences in this corpus is circular for the same cluster we are trying
    # to judge, and it duplicates the individual-volume prior above.
    _ = name_distinct_occ_count

    # --- Step 2: corroborating signals ----------------------------------
    # Shared attorney across occurrences.
    atty_sets = [set(o.attorneys) for o in occs if o.attorneys]
    shared_atty = set.intersection(*atty_sets) if len(atty_sets) >= 2 else set()
    if shared_atty:
        pts += SIG_SHARED_ATTORNEY
        factors.append({"factor": "shared_attorney", "points": SIG_SHARED_ATTORNEY,
                        "detail": f"same attorney across cases: {', '.join(sorted(shared_atty))}"})

    # Shared contact (address tail) across occurrences.
    contacts = {o.contact for o in occs if o.contact}
    if len(contacts) == 1 and sum(1 for o in occs if o.contact) >= 2:
        pts += SIG_SHARED_CONTACT
        factors.append({"factor": "shared_contact", "points": SIG_SHARED_CONTACT,
                        "detail": f"same contact across cases: {next(iter(contacts))}"})

    # Shared case-type prefix.
    casetypes = {o.case_type for o in occs if o.case_type}
    if len(casetypes) == 1 and n_cases >= 2:
        pts += SIG_SHARED_CASETYPE
        factors.append({"factor": "shared_case_type", "points": SIG_SHARED_CASETYPE,
                        "detail": f"all cases same type/court prefix ({next(iter(casetypes))})"})

    # Exact vs fuzzy surface form. Defendant-side parties usually do not control
    # their own caption description, so superficial sameness/difference there is
    # not identity evidence. Only compare surfaces from roles that plausibly
    # control their own captioned name, and require at least two such rows.
    controlled_occ_count = sum(1 for o in occs if role_controls_captioned_name(o.party_type))
    controlled_surfaces = {
        o.clean_name.upper()
        for o in occs
        if role_controls_captioned_name(o.party_type)
    }
    if len(controlled_surfaces) == 1 and controlled_occ_count >= 2:
        pts += SIG_EXACT_SELF_DESCRIBED_SURFACE
        factors.append({"factor": "self_described_exact_name", "points": SIG_EXACT_SELF_DESCRIBED_SURFACE,
                        "detail": "identical self-described surface name across controlled-role occurrences"})
    elif len(controlled_surfaces) > 1:
        pts += SIG_FUZZY_SELF_DESCRIBED_SURFACE_PENALTY
        factors.append({"factor": "self_described_fuzzy_name", "points": SIG_FUZZY_SELF_DESCRIBED_SURFACE_PENALTY,
                        "detail": f"self-described surface forms differ, matched only after normalization: "
                                  f"{', '.join(sorted(controlled_surfaces))}"})

    # Same-matter cross-reference: any two of this cluster's cases literally cite
    # each other's case number (harvested from docket/calendar text). That makes
    # them the same dispute, so a same-named party in both is almost certainly the
    # same identity — a strong corroborating signal independent of name rarity.
    if case_xrefs and n_cases >= 2:
        cnset = {normalize_case_number(c) for c in case_numbers if c}
        linked_pair = None
        for c in cnset:
            hit = case_xrefs.get(c, set()) & (cnset - {c})
            if hit:
                linked_pair = (c, sorted(hit)[0])
                break
        if linked_pair:
            pts += SIG_CROSSREF_SAME_MATTER
            factors.append({"factor": "cross_referenced_cases", "points": SIG_CROSSREF_SAME_MATTER,
                            "detail": f"cluster's cases cite each other (same matter): "
                                      f"{linked_pair[0]} <-> {linked_pair[1]} — strong same-party evidence"})

    conf = logistic(pts)
    factors.append({"factor": "TOTAL", "points": round(pts, 3),
                    "detail": f"log-odds sum -> {conf:.0%} "
                              f"({'certain' if conf >= T_CERTAIN else 'probable' if conf >= T_PROBABLE else 'possible'})"})
    return conf, factors


def union_alias_norm_keys(occs: list[Occurrence]) -> None:
    """Collapse explicit same-party alias names into the canonical block key."""
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        if parent[key] != key:
            parent[key] = find(parent[key])
        return parent[key]

    def union(canonical: str, alias_key: str) -> None:
        if not canonical or not alias_key or canonical == alias_key:
            return
        parent[find(alias_key)] = find(canonical)

    for o in occs:
        if not o.norm_key:
            continue
        find(o.norm_key)
        for alias in o.identity_aliases:
            alias_clean, _contact = split_contact(alias)
            alias_type, _alias_subtype = enrich_cases.classify_party(alias_clean)
            alias_key = litigant_norm_key(alias_clean, alias_type)
            if alias_key:
                union(o.norm_key, alias_key)

    for o in occs:
        if o.norm_key:
            o.norm_key = find(o.norm_key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build():
    print("Collecting occurrences …")
    captured, case_xrefs = collect_captured()
    tentative = collect_tentatives()
    print(f"  {len(captured)} docket party occurrences (captured cases)")
    print(f"  {len(tentative)} tentative caption party occurrences")
    print(f"  {sum(len(v) for v in case_xrefs.values())} cross-case references "
          f"from {len(case_xrefs)} cases (same-matter signal)")
    all_occ = captured + tentative

    fee_by_case = load_fee_aggregates()
    vex_keys = load_vexatious_individuals()
    print(f"  {len(fee_by_case)} cases with fee aggregates, "
          f"{len(vex_keys)} JC vexatious name keys (volume-lift only)")

    union_alias_norm_keys(all_occ)

    # Corpus-wide name commonness: distinct (case,name) occurrences per norm_key.
    name_occ_count: dict[str, int] = {}
    for o in all_occ:
        if not o.norm_key:
            continue
        name_occ_count[o.norm_key] = name_occ_count.get(o.norm_key, 0) + 1

    # Block by normalized key.
    clusters: dict[str, list[Occurrence]] = {}
    for o in all_occ:
        if not o.norm_key:
            continue
        clusters.setdefault(o.norm_key, []).append(o)

    rows = []
    for i, (key, occs) in enumerate(sorted(clusters.items())):
        entity_type, entity_subtype = cluster_entity_type(occs)
        is_generic = bool(GENERIC_SINGLETON_RE.search(occs[0].clean_name))
        # JC-vexatious name (authoritative) — only used to lift the volume penalty.
        is_vex = key in vex_keys

        conf, factors = score_cluster(occs, entity_type,
                                      name_occ_count.get(key, len(occs)), is_vex, case_xrefs)
        # Generic public bodies: keep as singleton entity, never assert a merge.
        if is_generic:
            conf = 1.0
            factors = [{"factor": "generic_singleton", "points": 0.0,
                        "detail": "well-known public body — treated as one entity, "
                                  "no same-person inference"}]

        case_numbers = sorted({o.case_number for o in occs})
        # Canonical display name: prefer rows that explicitly carried alias
        # language ("erroneously sued as", aka/fka/dba), because their cleaned
        # name is the real caption party and their alias list is the noncanonical
        # surface. Fall back to the longest cleaned surface form.
        canonical_names = [o.clean_name for o in occs if o.identity_aliases] or [o.clean_name for o in occs]
        display_source = max(canonical_names, key=len)
        display = enrich_cases.display_name(
            display_source,
            title_case=(entity_type == "individual"))
        name_parts = derived_individual_name_parts(display, entity_type)
        display_key = litigant_norm_key(display_source, entity_type)
        aliases = sorted({
            enrich_cases.display_name(alias_name, title_case=(entity_type == "individual"))
            for o in occs
            for alias_name in [*o.aliases, o.clean_name]
            if litigant_norm_key(alias_name, enrich_cases.classify_party(alias_name)[0])
            and litigant_norm_key(alias_name, enrich_cases.classify_party(alias_name)[0]) != display_key
        })

        party_types = sorted({o.party_type for o in occs if o.party_type})
        attorneys = sorted({a for o in occs for a in o.attorneys})
        contacts = sorted({o.contact for o in occs if o.contact})

        sources = sorted({o.source for o in occs})
        name_source = "both" if len(sources) > 1 else sources[0]

        # Fee roll-up across this litigant's cases (coarse; case-attributed).
        waived = paid = repaid = 0.0
        for cn in case_numbers:
            agg = fee_by_case.get(enrich_cases_norm_case(cn))
            if agg:
                waived += agg["waived"]
                paid += agg["paid"]
                repaid += agg["repaid"]

        # Pull history + last_seen (captured cases only carry captured_at).
        pulls = sorted(
            ({"case_number": o.case_number, "captured_at": o.captured_at}
             for o in occs if o.captured_at),
            key=lambda d: d["captured_at"])
        # de-dup pull history by case_number, keep latest captured_at
        pull_by_case: dict[str, str] = {}
        for p in pulls:
            pull_by_case[p["case_number"]] = p["captured_at"]
        pull_history = [{"case_number": cn, "captured_at": ts}
                        for cn, ts in sorted(pull_by_case.items(),
                                             key=lambda kv: kv[1])]
        last_seen = pull_history[-1]["captured_at"] if pull_history else None

        rows.append({
            "litigant_id": f"L{i:06d}",
            "display_name": display,
            **name_parts,
            "norm_key": key,
            "entity_type": entity_type,
            "entity_subtype": entity_subtype,
            "case_numbers": case_numbers,
            "case_count": len(case_numbers),
            "party_types": party_types,
            "attorneys": attorneys,
            "aliases": aliases,
            "contacts": contacts,
            "name_source": name_source,
            "confidence": round(conf, 4),
            "confidence_tier": ("certain" if conf >= T_CERTAIN
                                else "probable" if conf >= T_PROBABLE
                                else "possible"),
            "confidence_factors": factors,
            "total_fees_waived": round(waived, 2),
            "total_fees_paid": round(paid, 2),
            "total_fees_repaid": round(repaid, 2),
            "last_seen": last_seen,
            "pull_history": pull_history,
            "is_vexatious_authoritative": is_vex,
        })

    return rows


def json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def write_litigant_json_shards(records: list[dict]) -> tuple[list[dict], int]:
    if os.path.isdir(OUT_JSON_SHARD_DIR):
        shutil.rmtree(OUT_JSON_SHARD_DIR)
    os.makedirs(OUT_JSON_SHARD_DIR, exist_ok=True)

    shards: list[dict] = []
    current: list[dict] = []
    current_size = json_size({"schema_version": 1, "litigants": []})

    def flush() -> None:
        nonlocal current, current_size
        if not current:
            return
        name = f"{len(shards):04d}.json"
        path = os.path.join(OUT_JSON_SHARD_DIR, name)
        payload = {"schema_version": 1, "litigants": current}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        size = os.path.getsize(path)
        shards.append({
            "path": f"data/litigants/{name}",
            "count": len(current),
            "bytes": size,
        })
        current = []
        current_size = json_size({"schema_version": 1, "litigants": []})

    for record in records:
        record_size = json_size(record) + 1
        if current and current_size + record_size > JSON_SHARD_TARGET_BYTES:
            flush()
        current.append(record)
        current_size += record_size
    flush()
    return shards, sum(shard["bytes"] for shard in shards)


def write_litigant_parquet_shards(records: list[dict]) -> tuple[list[dict], int]:
    if os.path.isfile(LEGACY_OUT_PARQUET):
        os.remove(LEGACY_OUT_PARQUET)
    if os.path.isdir(OUT_PARQUET_DIR):
        shutil.rmtree(OUT_PARQUET_DIR)
    os.makedirs(OUT_PARQUET_DIR, exist_ok=True)

    df_pq = pd.DataFrame(records)
    for col in PARQUET_JSON_COLUMNS:
        if col in df_pq.columns:
            df_pq[col] = df_pq[col].apply(json.dumps)

    shards: list[dict] = []
    total_rows = len(df_pq)
    for shard_index, start in enumerate(range(0, total_rows, PARQUET_SHARD_ROWS)):
        stop = min(start + PARQUET_SHARD_ROWS, total_rows)
        name = f"{shard_index:04d}.parquet"
        path = os.path.join(OUT_PARQUET_DIR, name)
        df_pq.iloc[start:stop].to_parquet(path, index=False)
        size = os.path.getsize(path)
        if size > PARQUET_SHARD_HARD_MAX_BYTES:
            raise RuntimeError(
                f"{path} is {size} bytes, above the {PARQUET_SHARD_HARD_MAX_BYTES} byte shard guard"
            )
        shards.append({
            "path": f"data/litigants-parquet/{name}",
            "count": stop - start,
            "bytes": size,
        })

    with open(OUT_PARQUET_MANIFEST, "w", encoding="utf-8") as fh:
        json.dump({
            "schema_version": 1,
            "generated_clusters": len(records),
            "shard_rows": PARQUET_SHARD_ROWS,
            "note": "Canonical full litigant store, sharded so no Git blob approaches GitHub's 100 MiB hard limit.",
            "shards": shards,
        }, fh, ensure_ascii=False, separators=(",", ":"))

    return shards, sum(shard["bytes"] for shard in shards)


def write_outputs(rows: list[dict]):
    parquet_shards, parquet_bytes = write_litigant_parquet_shards(rows)

    # Compact JSON for the browser. The full per-litigant store is the sharded
    # Parquet dataset. The JSON is a
    # CURATED, small subset for quick load: every litigant that touches a
    # captured case (docket / both source — these carry real party data, fees,
    # pull history) PLUS every multi-case cluster (the cross-case candidates that
    # need the confidence UI). Tentative-only single-case litigants (the bulk)
    # live only in the parquet. The curated set is written as a small manifest
    # plus shards so no single Git blob approaches GitHub's 100 MiB hard limit.
    compact = []
    for r in rows:
        if r["name_source"] == "tentative" and r["case_count"] <= 1:
            continue
        c = {k: v for k, v in r.items()
             if v not in (None, [], 0.0, "", False)}
        if c.get("case_count", 0) <= 1:
            # Singleton docket profiles dominate the browser JSON. Keep the
            # detailed evidence trail in parquet; the JSON only needs summary
            # fields for quick viewer load and repo-bloat guard headroom.
            c.pop("confidence_factors", None)
            c.pop("pull_history", None)
        compact.append(c)
    shards, shard_bytes = write_litigant_json_shards(compact)
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 2,
                   "generated_clusters": len(rows),
                   "included": len(compact),
                   "note": "Curated subset: captured-case litigants + all multi-case "
                           "clusters. Full set is data/litigants-parquet.json and "
                           "data/litigants-parquet/*.parquet. Records are split "
                           "across the listed JSON shards.",
                   "shards": shards},
                  fh, ensure_ascii=False, separators=(",", ":"))

    print(f"\nWrote {len(rows)} litigant clusters:")
    print(f"  {OUT_PARQUET_MANIFEST}  ({os.path.getsize(OUT_PARQUET_MANIFEST)} bytes manifest, "
          f"{len(parquet_shards)} parquet shards, {parquet_bytes} shard bytes)")
    print(f"  {OUT_JSON}  ({os.path.getsize(OUT_JSON)} bytes manifest, "
          f"{len(compact)} curated litigants in {len(shards)} shards, "
          f"{shard_bytes} shard bytes)")


def summarize(rows: list[dict]):
    multi = [r for r in rows if r["case_count"] > 1]
    tiers: dict[str, int] = {}
    etypes: dict[str, int] = {}
    for r in rows:
        tiers[r["confidence_tier"]] = tiers.get(r["confidence_tier"], 0) + 1
        etypes[r["entity_type"]] = etypes.get(r["entity_type"], 0) + 1
    print(f"\n--- summary ---")
    print(f"  total clusters: {len(rows)}  (multi-case: {len(multi)})")
    print(f"  entity_type: {etypes}")
    print(f"  confidence_tier: {tiers}")
    print(f"  name_source: " + str({
        s: sum(1 for r in rows if r['name_source'] == s)
        for s in {r['name_source'] for r in rows}}))
    print(f"\n  multi-case clusters (cross-case candidates):")
    for r in sorted(multi, key=lambda x: (-x["case_count"], -x["confidence"]))[:15]:
        print(f"    {r['display_name'][:42]:42s} {r['entity_type']:11s} "
              f"x{r['case_count']:<2d} conf={r['confidence']:.2f} "
              f"[{r['confidence_tier']}] {r['name_source']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    rows = build()
    write_outputs(rows)
    if not args.quiet:
        summarize(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
