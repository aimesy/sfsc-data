#!/usr/bin/env python3
"""Enrich SFSC tentative-ruling cases with structured parties and high-precision tags.

This is a PROTOTYPE. It uses only pandas + the Python standard library and makes
NO network calls. It reads the canonical ``tentatives.parquet`` (or per-department
``data/tentatives-<dept>.parquet`` when ``--department`` is given), collapses the
~206k ruling rows to one record per distinct ``case_number``, and derives two
families of enrichment from ``case_title`` (and, for tags, ``calendar_matter`` +
``ruling`` / ``ruling_substantive``):

  (1) LITIGANT EXTRACTION  -- parse ``case_title`` into plaintiff/defendant parties.
  (2) SIMPLE TAGGING       -- restrained, high-precision litigant_type / matter_type /
                              cause_of_action / outcome tags.

------------------------------------------------------------------------------
METHOD
------------------------------------------------------------------------------
Party extraction:
  * The case caption is split into a "plaintiff side" and a "defendant side" on the
    versus marker. Markers, tried in order: " VS. ", " VS ", and -- only when it is
    NOT a personal middle initial -- " V. " / " V ".  (See the V. caveat below.)
  * Each side is split into individual party names on commas, " AND ", and " & ".
    A small allow-list of phrases that legitimately contain those connectors
    (e.g. "CITY AND COUNTY OF SAN FRANCISCO") is protected from splitting.
  * Trailing "ET AL" / "ET AL." / "et al" (case-insensitive, several spellings) is
    stripped from the last party and recorded as the boolean ``has_et_al`` ("and
    others"), rather than being emitted as a party.
  * The ORIGINAL ``case_title`` is always preserved. Parties are emitted both as
    captured text and as a display-normalized form (whitespace collapsed, optional
    title-case).

Tagging (deliberately conservative -- we prefer to leave a case UNTAGGED over
guessing; every tag comes from an explicit, documented matcher near the top of
this file):
  * litigant_type: each party is classified 'individual' / 'entity' / 'unknown'.
    Entities additionally get a single best subtype (corporation, llc, lp-llp,
    bank, insurer, government, trust, estate, hospital-medical, university, hoa,
    partnership) from strong name-pattern signals.
  * matter_type (multi-label) and cause_of_action (multi-label) are emitted only on
    strong textual signals drawn from the caption + ruling text.

------------------------------------------------------------------------------
PRECISION CAVEATS (please read before trusting any field)
------------------------------------------------------------------------------
  * NAME-VARIANT GROUPING IS A HEURISTIC, NOT IDENTITY RESOLUTION.  We compute a
    light ``norm_key`` per party (uppercase, punctuation/middle-initials stripped,
    suffixes dropped, whitespace collapsed) purely to GROUP captions whose parties
    *look* alike. Two parties sharing a ``norm_key`` are NOT asserted to be the same
    legal person -- common names collide, the same person appears under many
    spellings, and captions get truncated. ``name_match_confidence`` ('high' for an
    exact original match, 'low' for a norm-key-only match) reflects this. DO NOT use
    these keys to merge real-world identities without human review.
  * " V. " / " V " AS A SEPARATOR IS DANGEROUS.  In this dataset every occurrence of
    " V. " that is NOT part of " VS. " was a personal middle initial
    (e.g. "THE ESTATE OF HALLA V. HAMPTON"), never a true versus marker. We therefore
    only treat " V. "/" V " as a separator when the token on its left is plainly not
    a single-letter middle initial. Residual mis-splits are possible.
  * TRUNCATED CAPTIONS.  The court truncates long left-hand party names
    (e.g. "U.S. BANK, NATIONAL ASSOCIATION AS TRUSTEE FOR VS. ..."). Such plaintiffs
    parse as a dangling fragment; counts on the plaintiff side can be understated.
  * NON-ADVERSARIAL CAPTIONS.  Probate-style titles ("THE ESTATE OF ...",
    "CONSERVATORSHIP OF ...", "IN RE: ...") have no versus marker; they are reported
    as parse "failures" for the party splitter (``parse_ok=False``) but still receive
    a subject party and tags where possible.
  * ENTITY-vs-INDIVIDUAL AMBIGUITY.  A surname like "BANK" or a sole proprietor
    "JOHN SMITH DBA ACME" can be misclassified. Subtypes are emitted only on strong
    tokens; everything else stays 'unknown'/'individual', biasing toward under-tagging.
  * Tag matchers favour precision over recall: many genuinely on-topic cases will be
    left untagged because the textual signal was not unambiguous.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from collections import Counter

try:
    import pandas as pd
except ImportError:  # pragma: no cover - dependency hint
    sys.exit("This script requires pandas (pip install pandas pyarrow).")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CANONICAL_PARQUET = os.path.join(REPO_ROOT, "tentatives.parquet")
TAG_REGISTRY_PATH = os.path.join(REPO_ROOT, "tag_registry.json")
DEFAULT_BUG_REPORT_PATH = os.path.join(REPO_ROOT, "data",
                                       "heuristic_bug_reports.ndjson")
BUG_REPORT_MAX_BYTES = max(1, int(os.environ.get("SFSC_BUG_REPORT_MAX_BYTES", str(16 * 1024 * 1024)) or str(16 * 1024 * 1024)))


def write_text_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def rotate_if_large(path: str, max_bytes: int = BUG_REPORT_MAX_BYTES) -> None:
    if not path or max_bytes <= 0 or not os.path.exists(path):
        return
    if os.path.getsize(path) <= max_bytes:
        return
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{path}.{stamp}.bak"
    n = 1
    while os.path.exists(backup):
        backup = f"{path}.{stamp}.{n}.bak"
        n += 1
    os.replace(path, backup)


def write_parquet_atomic(path: str, df: "pd.DataFrame") -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".parquet", dir=directory)
    os.close(fd)
    try:
        df.to_parquet(tmp, index=False)
        if os.path.getsize(tmp) <= 0:
            raise RuntimeError(f"refusing to replace {path}: temporary parquet is empty")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ===========================================================================
# HEURISTIC BUG-REPORT LOG  (early-stage error-handling POLICY)
# ===========================================================================
# Policy (intentional, see DESIGN_NOTES "heuristic" notes): at this prototype
# stage we want to MAXIMIZE information for heuristic development. We therefore
# NEVER silently drop or normalize a value that a heuristic can't fit, and we
# NEVER silently resolve a genuine disagreement between two heuristics/sources.
# Instead we emit a structured bug report (one JSON line per event) so the cases
# the heuristics miss are visible and improvable.
#
# A bug report is triggered when:
#   (1) a heuristic CAN'T FIT a value -- e.g. a case_title that has a versus
#       marker but fails to parse into parties, an entity name that matches no
#       subtype yet clearly looks like an org, or a field that won't parse --
#       UNLESS the value is OBVIOUS NONSENSE (mostly non-alphanumeric / garbage /
#       scan-typo characters), which is filtered out as noise rather than logged.
#   (2) two heuristics/sources DISAGREE on a value and it's not an obvious typo;
#       both candidates are logged and NEITHER is auto-prioritized. (The registry
#       authority is the default DISPLAY order; genuine disagreements must be
#       surfaced for review, not silently resolved.)
#
# Record schema (one NDJSON line each):
#   { ts, case_number, namespace_or_field, value, candidates, reason, heuristic }
#
# Cross-source disagreements are RARE today (only the tentative tier exists), so
# the framework is wired but the emitted reports are mostly the "doesn't fit"
# class (e.g. the versus-marked-but-failed-to-parse titles, org-looking-but-
# unclassifiable entity names).
def is_obvious_nonsense(value) -> bool:
    """True when a value is garbage/scan-typo noise (filtered, NOT bug-reported).

    Heuristic: a string is "obvious nonsense" when, after stripping whitespace,
    it is empty, has fewer than 3 letters, or is MOSTLY non-alphanumeric (more
    than 40% of its non-space characters are punctuation/symbols). Real captions
    and names always carry substantial alphabetic content; OCR/scan garbage and
    stray-punctuation fragments do not. We deliberately keep this conservative so
    we only suppress true noise and still surface borderline-but-real values.
    """
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True
    letters = sum(c.isalpha() for c in s)
    if letters < 3:
        return True
    non_space = [c for c in s if not c.isspace()]
    if not non_space:
        return True
    junk = sum(1 for c in non_space if not c.isalnum())
    return (junk / len(non_space)) > 0.40


class BugReporter:
    """Collects heuristic bug reports and writes them as NDJSON.

    Each report is a dict with the fixed schema below. ``report(...)`` applies
    the obvious-nonsense noise filter for "doesn't fit" reports (reason starting
    with ``cant_fit``); disagreement reports are always kept. ``write(path)``
    appends one JSON line per report (creating the parent dir if needed).
    """

    SCHEMA = ["ts", "case_number", "namespace_or_field", "value",
              "candidates", "reason", "heuristic"]

    def __init__(self):
        self.reports = []
        self.suppressed_nonsense = 0

    def report(self, case_number, namespace_or_field, value, reason,
               heuristic, candidates=None):
        """Record one bug report. Returns the dict, or None if filtered.

        "doesn't fit" reports (reason prefixed ``cant_fit``) whose value is
        OBVIOUS NONSENSE are suppressed as noise (counted, not logged). Genuine
        cross-source DISAGREEMENT reports are never suppressed -- a real conflict
        is information, even if one candidate looks odd.
        """
        is_cant_fit = str(reason).startswith("cant_fit")
        if is_cant_fit and is_obvious_nonsense(value):
            self.suppressed_nonsense += 1
            return None
        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "case_number": case_number,
            "namespace_or_field": namespace_or_field,
            "value": value,
            "candidates": candidates,   # list for disagreements, else None
            "reason": reason,
            "heuristic": heuristic,
        }
        self.reports.append(rec)
        return rec

    def write(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        rotate_if_large(path)
        with open(path, "a") as fh:
            for rec in self.reports:
                fh.write(json.dumps(rec) + "\n")
        return len(self.reports)


# Module-level default reporter so a bare ``report_bug(...)`` call works from
# anywhere in the pipeline (run() swaps in a fresh one per invocation).
BUG_REPORTER = BugReporter()


def report_bug(record=None, *, case_number=None, namespace_or_field=None,
               value=None, reason=None, heuristic=None, candidates=None):
    """Append a heuristic bug report to the active reporter (noise-filtered).

    Accepts either a ready-made ``record`` dict (keys: case_number,
    namespace_or_field, value, reason, heuristic, optional candidates) or the
    equivalent keyword arguments. Returns the stored dict, or None if the value
    was filtered out as obvious nonsense.
    """
    if record is not None:
        case_number = record.get("case_number", case_number)
        namespace_or_field = record.get("namespace_or_field", namespace_or_field)
        value = record.get("value", value)
        reason = record.get("reason", reason)
        heuristic = record.get("heuristic", heuristic)
        candidates = record.get("candidates", candidates)
    return BUG_REPORTER.report(
        case_number=case_number, namespace_or_field=namespace_or_field,
        value=value, reason=reason, heuristic=heuristic, candidates=candidates)


# ---------------------------------------------------------------------------
# Tag registry — the central, programmatic SOURCE OF TRUTH for tag provenance.
# Loaded ONCE here; every emitted tag row is stamped (source/tier/authority/
# tentative) from it instead of hardcoding those values. See tag_registry.json.
# ---------------------------------------------------------------------------
def load_tag_registry(path: str = TAG_REGISTRY_PATH) -> dict:
    """Load the central tag registry (sources + namespaces). Stdlib only."""
    with open(path, "r") as fh:
        return json.load(fh)


# Lazily loaded so a bad/missing registry does not break --help or imports.
TAG_REGISTRY = None


def get_tag_registry() -> dict:
    global TAG_REGISTRY
    if TAG_REGISTRY is None:
        TAG_REGISTRY = load_tag_registry()
    return TAG_REGISTRY


TAG_VALUE_ALIASES = {
    ("status", "pro-per"): "propria-persona",
    ("status", "pro-se"): "propria-persona",
    ("status", "self-represented"): "propria-persona",
    ("status", "propria-persona"): "propria-persona",
    ("status", "vexatious-litigant"): "vexatious-litigant",
    ("outcome", "hearing-is-required"): "hearing-required",
}


def normalize_tag_value(ns: str, name: object) -> str:
    """Canonical machine form for tag values.

    Namespaces are registry identifiers; values are tag slugs. Keep this narrow
    and deterministic so every producer emits the same value for the same tag.
    """
    s = str(name or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9.-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return TAG_VALUE_ALIASES.get((str(ns or ""), s), s)


def dept_parquet(dept: str) -> str:
    return os.path.join(REPO_ROOT, "data", f"tentatives-{dept}.parquet")


# ===========================================================================
# VOCABULARIES & MATCHERS  (explicit, documented, high-precision)
# ===========================================================================

# --- Versus markers --------------------------------------------------------
# The "VS"/"VS." token (case-insensitive, spaces optional) is the trusted
# separator. " V. "/" V " is handled separately with a middle-initial guard in
# split_versus() because, in this dataset, almost every non-"VS" " V. " is a
# personal middle initial rather than a versus marker.
VS_TOKEN_RE = re.compile(r"(?i)(?:^|(?<=[\s,.;]))\s*VS(?:\.|(?=[\s,;])|$)\s*")
V_TOKEN_RE = re.compile(r"(?i)\s+V\.?\s+")

# --- "ET AL" (and-others) trailing flag -----------------------------------
# Matches ET AL, ET AL., ETAL, ET. AL, case-insensitive, at the end of a side.
ET_AL_RE = re.compile(r"\s*,?\s*\bet\.?\s*al\.?\s*$", re.IGNORECASE)

# --- Connectors used to split one side into individual parties -------------
PARTY_SPLIT_RE = re.compile(r"\s+AND\s+|\s*&\s*|\s*;\s*", re.IGNORECASE)

# Phrases that legitimately contain " AND "/"&" and must NOT be split apart.
# We mask these before splitting, then restore them.
AND_PROTECTED = [
    "CITY AND COUNTY OF SAN FRANCISCO",
    "CITY AND COUNTY",
    "AND COUNTY OF",
]

# --- Organization names that legitimately contain "AND"/"&" ----------------
# WHY: PARTY_SPLIT_RE splits a caption side on " AND ", "&", and ";" to separate
# co-parties ("JOHN SMITH AND MARY JONES" -> two people). But many ORGANIZATIONS
# carry "AND"/"&" inside their single legal name -- the canonical example being
# "PACIFIC GAS AND ELECTRIC COMPANY" / "PG&E", which the bare splitter shredded
# into a bogus "PACIFIC GAS" fragment plus "ELECTRIC COMPANY". That fragment then
# (a) misclassified as an *individual* (no entity token survived on "PACIFIC
# GAS") and (b) fractured the cluster. The fix is high-precision: only when the
# span around the conjunction looks like an ORGANIZATION do we treat it as one
# party and protect it from the split. We deliberately do NOT protect a generic
# "X AND Y" (that is still two co-parties); we require an explicit organizational
# signal, so two joined PEOPLE are never wrongly merged.
#
# Two precise signals, ORed:
#   (1) An industry/utility word pair joined by the conjunction -- the historic
#       "<WORD> AND/& <INDUSTRY>" company-naming pattern. Vocabulary is explicit
#       and conservative (GAS/ELECTRIC/POWER/LIGHT/WATER/ENERGY/TELEGRAPH/
#       TELEPHONE/RAILROAD/RAILWAY/STEAMSHIP/...), e.g. "GAS AND ELECTRIC",
#       "LIGHT & POWER", "GAS & ELECTRIC".
#   (2) A side that, taken whole, ends in a corporate/firm suffix
#       (COMPANY/CO/INC/CORP/LLC/LLP/LP/LTD/ASSOCIATES/PARTNERS/...) AND contains
#       an "AND"/"&" -- the legal name of one firm ("SMITH AND WESSON, INC.",
#       "BROWN AND TOLAND MEDICAL GROUP"). This is the classic professional-firm
#       "<NAME> AND <NAME>, <SUFFIX>" form, which is ONE entity.
# Each matched span is masked (like AND_PROTECTED) so the splitter leaves it
# whole; we then restore the original text.

# Industry/utility nouns that pair across the conjunction in old company names.
_ORG_INDUSTRY_WORD = (
    r"GAS|ELECTRIC|ELECTRICITY|POWER|LIGHT|WATER|ENERGY|STEAM|FUEL|OIL|"
    r"TELEGRAPH|TELEPHONE|RAILROAD|RAILWAY|TRACTION|STEAMSHIP|NAVIGATION|"
    r"IRON|STEEL|COAL|LUMBER|MINING|MILLING|MANUFACTURING|REFINING|TRUST|"
    r"SAVINGS|LOAN|INSURANCE|CASUALTY|INDEMNITY|SURETY"
)
# Signal (1): "<INDUSTRY> AND/& <INDUSTRY>" (e.g. GAS AND ELECTRIC, LIGHT & POWER).
ORG_INDUSTRY_PAIR_RE = re.compile(
    rf"\b(?:{_ORG_INDUSTRY_WORD})\s+(?:AND|&)\s+(?:{_ORG_INDUSTRY_WORD})\b",
    re.IGNORECASE)

# Corporate/firm suffix tokens (a trailing one marks a single firm's legal name).
_ORG_SUFFIX_WORD = (
    r"COMPANY|COMPANIES|CO|CORP|CORPORATION|INCORPORATED|INC|LLC|L\.L\.C|"
    r"LLP|L\.L\.P|LP|L\.P|LTD|LIMITED|ASSOCIATES|ASSOCIATION|PARTNERS|"
    r"PARTNERSHIP|GROUP|HOLDINGS|ENTERPRISES|MEDICAL GROUP"
)
# Signal (2): a whole side that contains a conjunction AND ends in a firm suffix
# ("SMITH AND WESSON, INC.", "BROWN & TOLAND MEDICAL GROUP", "DAVID LEE AND
# ASSOCIATES"). Commas are allowed inside the name span so the common
# "<NAME>, <SUFFIX>" form (", INC.") is covered. The trailing suffix must be the
# LAST token, so a genuine two-person side with no firm suffix
# ("JOHN SMITH AND MARY JONES") does NOT match and still splits into two people.
ORG_SUFFIX_SIDE_RE = re.compile(
    rf"^\s*[\w&'.\-]+(?:\s+[\w&'.\-]+)*?\s+(?:AND|&)\s+"
    rf"[\w&'.,\- ]*?\b(?:{_ORG_SUFFIX_WORD})\b\.?\s*$",
    re.IGNORECASE)


def org_and_spans(side: str) -> list[str]:
    """Return substrings of `side` that are org names spanning an "AND"/"&".

    High-precision: each returned span is a phrase the party-splitter must NOT
    break. Used by split_side() to mask org names before splitting on
    AND/&/;. Returns [] when no organizational AND/& span is present (so generic
    "PERSON AND PERSON" co-party sides are untouched and still split normally).
    """
    if not side:
        return []
    spans: list[str] = []
    # (2) whole side is one firm's legal name (suffix-anchored) -> protect it all.
    if ORG_SUFFIX_SIDE_RE.match(side):
        spans.append(side.strip())
        return spans
    # (1) industry-pair spans inside the side (there can be more than one).
    for m in ORG_INDUSTRY_PAIR_RE.finditer(side):
        spans.append(m.group(0))
    return spans

# --- Party-name suffixes/role words to drop when normalizing a key --------
NAME_SUFFIXES = {
    "JR", "SR", "II", "III", "IV", "V", "ESQ", "MD", "PHD", "DDS", "DO",
    "TRUSTEE", "INDIVIDUALLY", "DECEASED", "DECEDENT", "AKA", "FKA", "DBA",
    "AN", "A", "THE",
}

# --- Entity detection -------------------------------------------------------
# A party is an ENTITY if any of these word/token patterns appear. Each maps to
# a subtype; ENTITY_SUBTYPE_ORDER decides which wins when several match.
ENTITY_SUBTYPE_PATTERNS = {
    # subtype          : regex (word-boundaried, applied to UPPERCASED name)
    # Estate REQUIRES the standalone "ESTATE OF" decedent's-estate form. Bare
    # "ESTATE" is NOT enough (e.g. "HAWTHORNE/STONE REAL ESTATE INVESTMENTS,
    # INC." is a corporation, not a decedent's estate).
    "estate":          re.compile(r"\bESTATE OF\b"),
    "trust":           re.compile(r"\bTRUST\b|\bTRUSTEE\b|\bLIVING TRUST\b"),
    # Government REQUIRES a standalone government FORM, never a bare token. Bare
    # "CITY"/"U.S."/"PEOPLE"/"DISTRICT" inside a company name (e.g. "GARDEN
    # CITY, INC.", "U.S. BANK, N.A.") must NOT be labelled government. We match
    # the canonical "<X> OF" phrases ("CITY OF", "COUNTY OF", "STATE OF",
    # "PEOPLE OF", "DEPARTMENT OF"), the full "UNITED STATES" / "THE PEOPLE",
    # and gov-ish institutional suffixes ("... COMMISSION/BOARD/AUTHORITY/
    # DISTRICT/AGENCY", "HOUSING AUTHORITY", "DEPT OF", a trailing
    # "COMMISSIONER").
    "government":      re.compile(
        r"\bCITY OF\b|\bCOUNTY OF\b|\bCITY AND COUNTY\b|\bSTATE OF\b|"
        r"\bPEOPLE OF\b|\bTHE PEOPLE\b|\bUNITED STATES\b|"
        r"\bDEPARTMENT OF\b|\bDEPT\.? OF\b|\bHOUSING AUTHORITY\b|"
        # SF/CA public offices that carry no CITY-OF/agency token but ARE
        # government bodies. Without these they fell to the individual/unknown
        # heuristic and scored ~0% across hundreds of cases (Public Guardian
        # 236c, Public Administrator 137c, State Bar 48c, CCSF 66c, ...).
        r"\bPUBLIC (?:GUARDIAN|ADMINISTRATOR|DEFENDER|CONSERVATOR)\b|"
        r"\b(?:DISTRICT|CITY) ATTORNEY\b|\bATTORNEY GENERAL\b|\bSTATE BAR\b|"
        r"\bC\.?C\.?S\.?F\b|"
        r"\b\w[\w&'.\- ]*?\b(?:COMMISSION|BOARD|AUTHORITY|DISTRICT|AGENCY|"
        r"BUREAU|COMMISSIONER)\b"),
    # Bank: BANK / BANCORP / N.A. / NATIONAL ASSOCIATION / savings / credit
    # union / mortgage. (So "U.S. BANK, N.A." classifies as a bank.)
    "bank":            re.compile(r"\bBANK\b|\bBANCORP\b|\bN\.?A\.?\b|"
                                  r"\bNATIONAL ASSOCIATION\b|"
                                  r"\bSAVINGS\b|\bCREDIT UNION\b|\bMORTGAGE\b"),
    "insurer":         re.compile(r"\bINSURANCE\b|\bINSURER\b|\bCASUALTY\b|"
                                  r"\bMUTUAL\b|\bASSURANCE\b|\bINDEMNITY\b|\bSURETY\b"),
    "hospital-medical": re.compile(r"\bHOSPITAL\b|\bMEDICAL CENTER\b|"
                                   r"\bMEDICAL GROUP\b|\bHEALTHCARE\b|"
                                   r"\bHEALTH CARE\b|\bCLINIC\b|\bKAISER\b"),
    "university":      re.compile(r"\bUNIVERSITY\b|\bCOLLEGE\b|"
                                  r"\bREGENTS\b|\bSCHOOL DISTRICT\b"),
    "hoa":             re.compile(r"\bHOMEOWNERS\b|\bHOMEOWNER'?S\b|"
                                  r"\bOWNERS ASSOCIATION\b|\bH\.?O\.?A\.?\b|"
                                  r"\bCONDOMINIUM\b"),
    # Utility / industrial company. WHY: utilities and old industrial companies
    # ("PACIFIC GAS AND ELECTRIC COMPANY", "SAN FRANCISCO GAS & ELECTRIC",
    # "SOUTHERN PACIFIC RAILROAD", "PACIFIC TELEPHONE") have a generic-noun legal
    # name that carries NO INC/CORP/LLC token, so before this they fell through
    # to the individual heuristic and were mislabelled as people. We classify
    # them ENTITY on two HIGH-PRECISION signals only, to avoid catching real
    # surnames that happen to be a common noun (e.g. someone surnamed "Power" or
    # "Waters"):
    #   (a) the "<INDUSTRY> AND/& <INDUSTRY>" company-naming pair (shares the
    #       exact ORG_INDUSTRY_PAIR_RE used to protect the split) -- this two-word
    #       industrial pair is an org name, never a personal name; and
    #   (b) a standalone industry word IMMEDIATELY followed by an org-form noun
    #       ("... RAILROAD COMPANY", "POWER AUTHORITY", "WATER DISTRICT",
    #       "GAS CORPORATION"), i.e. the industry word is in an organizational
    #       context, not a bare surname.
    "utility":         re.compile(
        ORG_INDUSTRY_PAIR_RE.pattern + r"|"
        r"\b(?:GAS|ELECTRIC|ELECTRICITY|POWER|LIGHT|WATER|ENERGY|STEAM|FUEL|"
        r"OIL|TELEGRAPH|TELEPHONE|RAILROAD|RAILWAY|TRACTION|STEAMSHIP|"
        r"NAVIGATION|IRON|STEEL|COAL|LUMBER|MINING|MILLING|MANUFACTURING|"
        r"REFINING)\s+(?:COMPANY|COMPANIES|CO|CORP|CORPORATION|INC|LLC|"
        r"DISTRICT|AUTHORITY|COMMISSION|BOARD|AGENCY|WORKS|UTILITY|UTILITIES)\b",
        re.IGNORECASE),
    "llc":             re.compile(r"\bLLC\b|\bL\.L\.C\.?\b"),
    "lp-llp":          re.compile(r"\bLLP\b|\bL\.L\.P\.?\b|\bL\.?P\.?\b(?!\w)"),
    "corporation":     re.compile(r"\bINC\b|\bINC\.\b|\bCORP\b|\bCORPORATION\b|"
                                  r"\bINCORPORATED\b"),
    "partnership":     re.compile(r"\bPARTNERSHIP\b|\bPARTNERS\b|\bLIMITED PARTNERSHIP\b"),
}
# Generic entity hints that prove "entity" but carry no specific subtype.
GENERIC_ENTITY_RE = re.compile(
    r"\bCOMPANY\b|\bCOMPANIES\b|\bCO\.\b|\bLTD\b|\bLIMITED\b|\bASSOCIATES\b|"
    r"\bASSOCIATION\b|\bGROUP\b|\bHOLDINGS\b|\bENTERPRISES\b|\bSERVICES\b|"
    r"\bSYSTEMS\b|\bPROPERTIES\b|\bPARTNERS\b|\bL\.P\.\b|\bAPARTMENTS\b|"
    r"\bMANAGEMENT\b|\bFOUNDATION\b|\bINSTITUTE\b|\bSOCIETY\b|\bCHURCH\b|"
    r"\bFUND\b|\bCAPITAL\b|\bREALTY\b|\bDEVELOPMENT\b|\bCONSTRUCTION\b|"
    r"\bINVESTMENTS?\b|\bTECHNOLOGIES\b|\bINDUSTRIES\b")

# Priority when multiple subtype patterns hit one name (most specific first).
# "utility" sits after the named institutional types but before the generic
# corporate forms (llc/corporation/partnership), so e.g. "PACIFIC GAS AND
# ELECTRIC COMPANY" (matches BOTH the utility industry-pair and the generic
# COMPANY hint) is labelled the more descriptive "utility" rather than a bare
# corporation, while still being firmly ENTITY either way.
ENTITY_SUBTYPE_ORDER = [
    "estate", "trust", "government", "bank", "insurer", "hospital-medical",
    "university", "hoa", "utility", "llc", "lp-llp", "corporation", "partnership",
]

# --- matter_type matchers (multi-label). Searched over caption + ruling. ---
# Each value is a compiled regex; a tag fires only when its pattern matches.
MATTER_TYPE_PATTERNS = {
    "unlawful-detainer": re.compile(
        r"\bunlawful detainer\b|\bU\.?D\.?\b(?=.{0,40}(possession|tenan|evict))|"
        r"\bwrit of possession\b", re.IGNORECASE),
    "writ": re.compile(
        r"\bwrit of (?:mandate|mandamus|prohibition|review|certiorari|"
        r"possession|attachment)\b|\bpetition for writ\b", re.IGNORECASE),
    "injunction": re.compile(
        r"\b(?:preliminary |permanent |temporary )?injunction\b|"
        r"\binjunctive relief\b|\btemporary restraining order\b|\bT\.?R\.?O\.?\b",
        re.IGNORECASE),
    "probate": re.compile(
        r"\bprobate\b|\bconservatorship\b|\bestate of\b|\bdecedent\b|"
        r"\bletters testamentary\b|\bguardianship\b", re.IGNORECASE),
    "civil-forfeiture": re.compile(
        r"\b(?:civil )?forfeiture\b|\basset forfeiture\b", re.IGNORECASE),
    "real-property": re.compile(
        r"\bquiet title\b|\bpartition\b|\bforeclosure\b|\beasement\b|"
        r"\btrespass to land\b|\breal property\b|\bdeed of trust\b|"
        r"\blis pendens\b|\bnuisance\b", re.IGNORECASE),
    "employment": re.compile(
        r"\bwrongful termination\b|\bwrongful discharge\b|\bemployment\b|"
        r"\bwage(?:s)?\b|\bovertime\b|\bFEHA\b|\bharassment\b|\bdiscrimination\b|"
        r"\bretaliation\b|\bLabor Code\b", re.IGNORECASE),
    "pi": re.compile(
        r"\bpersonal injury\b|\bbodily injury\b|\bwrongful death\b|"
        r"\bmedical malpractice\b|\bproduct(?:s)? liability\b|\bslip and fall\b",
        re.IGNORECASE),
    "family": re.compile(
        r"\bdissolution of marriage\b|\bchild custody\b|\bchild support\b|"
        r"\bspousal support\b|\bmarital\b|\bpaternity\b|\bdomestic violence\b",
        re.IGNORECASE),
    "contract": re.compile(
        r"\bbreach of contract\b|\bbreach of the (?:written |oral )?contract\b|"
        r"\bbreach of (?:written |oral )?agreement\b|\bpromissory note\b|"
        r"\bgoods sold and delivered\b|\bopen book account\b|"
        r"\baccount stated\b|\bcommon counts\b", re.IGNORECASE),
    "damages": re.compile(
        r"\bcompensatory damages\b|\bpunitive damages\b|\bmoney damages\b|"
        r"\bdamages in the (?:amount|sum)\b", re.IGNORECASE),
}

# --- cause_of_action matchers (multi-label). Kept deliberately small. ------
CAUSE_OF_ACTION_PATTERNS = {
    "breach-of-contract": re.compile(
        r"\bbreach of (?:written |oral |the )?(?:contract|agreement)\b",
        re.IGNORECASE),
    "negligence": re.compile(
        r"\bnegligen(?:ce|t)\b|\bnegligent infliction\b", re.IGNORECASE),
    "fraud": re.compile(
        r"\bfraud\b|\bfraudulent\b|\bmisrepresentation\b|\bdeceit\b|"
        r"\bconcealment\b", re.IGNORECASE),
    "quiet-title": re.compile(r"\bquiet title\b", re.IGNORECASE),
    "defamation": re.compile(r"\bdefamation\b|\blibel\b|\bslander\b", re.IGNORECASE),
    "wrongful-termination": re.compile(
        r"\bwrongful termination\b|\bwrongful discharge\b", re.IGNORECASE),
    "breach-of-fiduciary-duty": re.compile(
        r"\bbreach of fiduciary duty\b", re.IGNORECASE),
    "conversion": re.compile(r"\bconversion\b", re.IGNORECASE),
    "unjust-enrichment": re.compile(r"\bunjust enrichment\b", re.IGNORECASE),
    "elder-abuse": re.compile(r"\belder (?:financial )?abuse\b", re.IGNORECASE),
    "unfair-competition": re.compile(
        r"\bunfair competition\b|\bunfair business practices\b|"
        r"\b(?:Bus(?:iness)?\.? ?&? ?Prof(?:essions)?\.? Code )?(?:section )?17200\b",
        re.IGNORECASE),
    "intentional-infliction": re.compile(
        r"\bintentional infliction of emotional distress\b|\bIIED\b", re.IGNORECASE),
    "trespass": re.compile(r"\btrespass\b", re.IGNORECASE),
    "nuisance": re.compile(r"\bnuisance\b", re.IGNORECASE),
}

# --- fee_waiver matcher (single-label boolean) ----------------------------
# A case is tagged ns="fee_waiver" / name="fee-waiver" when its concatenated
# case_title + calendar_matter + ruling text shows an indigent / in-forma-
# pauperis court-fee-waiver signal. This is the LOW-RECALL tentative-text
# fallback; the registry source_of_truth for this namespace is the docket
# (request -> grant/deny -> withdraw-on-improved-situation -> §68637 repayment).
FEE_WAIVER_RE = re.compile(
    r"fee waiver|in forma pauperis|forma pauperis|waiver of court fees|"
    r"waive[ds]?\s+.{0,15}court fees|68637",
    re.IGNORECASE)


# --- propria-persona matcher (self-represented; single-label boolean) ------
# A case is tagged ns="status" / name="propria-persona" when its concatenated
# case_title + calendar_matter + ruling text shows a self-represented
# appearance. This is the tentative-text fallback; the registry source_of_truth
# for this namespace is the docket (a party listed with NO attorney / "PRO
# PER"). The separator between pro/per is REQUIRED (\s or .) so "proper" never
# matches; "pro se" needs a space so "prose" never matches.
PROPRIA_PERSONA_RE = re.compile(
    r"in\s+propria\s+persona|\bpropria\s+persona\b|"
    r"\bpro(?:\.\s*|\s+)per\b|\bpro\s+se\b|self[\s-]represent",
    re.IGNORECASE)


# --- outcome matcher (single-label boolean) --------------------------------
# Hearing required is already a first-class viewer outcome. Emit it into the
# normalized tag parquet too, so bucket-building/search can use the same signal.
HEARING_REQUIRED_RE = re.compile(
    r"\bhearing\s+(?:is\s+)?required\b|"
    r"\bappearance(?:s)?\s+(?:is\s+|are\s+)?required\b|"
    r"\bpart(?:y|ies)\s+(?:are\s+)?(?:to|required\s+to|must)\s+appear\b",
    re.IGNORECASE)
HEARING_REQUIRED_NEG_RE = re.compile(
    r"\b(?:no|not)\s+(?:hearing|appearance(?:s)?)\s+(?:is\s+|are\s+)?required\b",
    re.IGNORECASE)


def has_hearing_required_signal(blob: str) -> bool:
    return bool(HEARING_REQUIRED_RE.search(blob)
                and not HEARING_REQUIRED_NEG_RE.search(blob))


# --- instrument matchers (single-label-per-name, TITLE/CAPTION-derived) -----
# Tier-1 / tentative disposition tags inferred from the motion/petition CAPTION
# (case_title + calendar_matter; the ruling text is a backup signal). These are
# HIGH-PRECISION: each fires only on an explicit instrument/caption form, and
# the "judgment" matcher excludes the motion types that merely contain the word.
# Matched per caption SEGMENT (calendar_matter aggregates many motions joined by
# " || "), so a single on-topic caption is enough to tag the case.
#
#   * fee-award   -- a motion/petition/order/notice/award ABOUT attorney/expert
#                    fees & costs (an award or a motion seeking one).
#   * judgment    -- a JUDGMENT instrument (notice of entry of judgment, default
#                    judgment, judgment of dismissal, entry/consent/amended/
#                    vacate judgment). EXCLUDES "judgment on the pleadings",
#                    "summary judgment", and probate "substituted judgment".
#   * stipulation -- a stipulation / stipulation-and-order.
INSTRUMENT_FEE_AWARD_RE = re.compile(
    r"(?i)"
    r"(?:MOTION|APPLICATION|REQUEST|PETITION|NOTICE|ORDER|AWARD|MEMORANDUM)"
    r"[^|]{0,60}?"
    r"(?:ATTORNEY\S*\s+FEES?|EXPERT\s+(?:WITNESS\s+)?FEES?|FEES?\s+AND\s+COSTS)")
# Exclude the probate accounting-line "RE: ACCOUNTING ..., REPORT, FEES" form
# (that is an accounting petition, not a fee-AWARD motion).
INSTRUMENT_FEE_AWARD_EXCL_RE = re.compile(
    r"(?i)ACCOUNTING\b.*\bREPORT,?\s+FEES?|RE:\s*ACCOUNTING")
INSTRUMENT_JUDGMENT_RE = re.compile(
    r"(?i)"
    r"(?:NOTICE\s+OF\s+ENTRY\s+OF\s+JUDGMENT|DEFAULT\s+JUDGMENT"
    r"|JUDGMENT\s+OF\s+DISMISSAL|ENTRY\s+OF\s+JUDGMENT"
    r"|(?:PROPOSED\s+|AMENDED\s+|RENEWED\s+|VACATE\s+)?\bJUDGMENT\b)")
INSTRUMENT_JUDGMENT_EXCL_RE = re.compile(
    r"(?i)JUDGMENT\s+ON\s+THE\s+PLEADINGS|SU[BS]+TITUT\w*\s+JUDGMENT"
    r"|SUMMARY\s+JUDGMENT|MOTION\s+FOR\s+SUMMARY")
INSTRUMENT_STIPULATION_RE = re.compile(
    r"(?i)\bSTIPULAT(?:ION|ED)\b|\bSTIP\.?\s+AND\s+ORDER\b")

# STRICT instrument-form matchers used ONLY for the RULING backup. Ruling BODIES
# narrate stipulations / judgments / fees in prose ("the parties stipulated...",
# "the court enters judgment...") that are NOT a filed instrument of that type;
# matching the loose caption regexes there destroys precision. The backup
# therefore fires only on an explicit FILED-INSTRUMENT form, never a bare word.
INSTRUMENT_FEE_AWARD_STRICT_RE = re.compile(
    r"(?i)"
    r"(?:MOTION|APPLICATION|NOTICE\s+OF\s+MOTION)\s+(?:AND\s+MOTION\s+)?FOR\b"
    r"[^.]{0,50}?(?:ATTORNEY\S*\s+FEES?|EXPERT\s+(?:WITNESS\s+)?FEES?)"
    r"|\bAWARD\s+OF\s+(?:ATTORNEY|EXPERT)\S*\s+FEES?")
INSTRUMENT_JUDGMENT_STRICT_RE = re.compile(
    r"(?i)NOTICE\s+OF\s+ENTRY\s+OF\s+JUDGMENT|DEFAULT\s+JUDGMENT"
    r"|JUDGMENT\s+OF\s+DISMISSAL|CONSENT\s+JUDGMENT")
# Backup stipulation: ONLY the unambiguous filed-instrument forms. We drop bare
# "STIPULATION TO/FOR ..." here because ruling prose narrates "extended by
# written stipulation to ..." (an act, not a filed Stipulation document).
INSTRUMENT_STIPULATION_STRICT_RE = re.compile(
    r"(?i)\bSTIPULATION\s+AND\s+ORDER\b|\bSTIP\.?\s+AND\s+ORDER\b"
    r"|\bSTIPULATION\s+FOR\s+ENTRY\b|\bSTIPULATED\s+JUDGMENT\b")

# Order of the instrument names (stable emit order).
INSTRUMENT_NAMES = ["fee-award", "judgment", "stipulation"]


def tag_instrument(title: str, calendar_matter: str, ruling: str = ""):
    """Return the sorted set of instrument names matched in the caption.

    Each instrument is checked per caption SEGMENT (title, then every motion
    caption in the " || "-joined calendar_matter, then the ruling text as a
    backup) so that a bare mention bleeding across an aggregated multi-motion
    string can't fire it without the right caption context. High precision.
    """
    segs = [title or ""]
    if calendar_matter:
        segs.extend(calendar_matter.split(" || "))
    found = set()
    for seg in segs:
        if not seg:
            continue
        if "fee-award" not in found and (
                INSTRUMENT_FEE_AWARD_RE.search(seg)
                and not INSTRUMENT_FEE_AWARD_EXCL_RE.search(seg)):
            found.add("fee-award")
        if "judgment" not in found and (
                INSTRUMENT_JUDGMENT_RE.search(seg)
                and not INSTRUMENT_JUDGMENT_EXCL_RE.search(seg)):
            found.add("judgment")
        if "stipulation" not in found and INSTRUMENT_STIPULATION_RE.search(seg):
            found.add("stipulation")
        if len(found) == len(INSTRUMENT_NAMES):
            break
    # Ruling text as a backup signal ONLY for instruments still unmatched, and
    # ONLY via the STRICT filed-instrument forms (never the bare-word fallbacks)
    # so narrative prose mentions don't over-tag.
    if ruling and len(found) < len(INSTRUMENT_NAMES):
        if "fee-award" not in found and (
                INSTRUMENT_FEE_AWARD_STRICT_RE.search(ruling)
                and not INSTRUMENT_FEE_AWARD_EXCL_RE.search(ruling)):
            found.add("fee-award")
        if "judgment" not in found and (
                INSTRUMENT_JUDGMENT_STRICT_RE.search(ruling)
                and not INSTRUMENT_JUDGMENT_EXCL_RE.search(ruling)):
            found.add("judgment")
        if "stipulation" not in found and (
                INSTRUMENT_STIPULATION_STRICT_RE.search(ruling)):
            found.add("stipulation")
    return sorted(found)


# Non-adversarial caption prefixes (no real versus marker expected).
# A leading "***TRANSFERRED TO X COUNTY***" administrative banner is stripped
# before matching so it doesn't hide the real caption type.
NON_ADVERSARIAL_BANNER_RE = re.compile(r"^\s*\*+[^*]*\*+\s*")
NON_ADVERSARIAL_RE = re.compile(
    r"^\s*(THE ESTATE OF|ESTATE OF|EST\.? OF|CONSERVATORSHIP OF|CONS\.? OF|"
    r"GUARDIANSHIP OF|GDN\.? OF|IN RE:?|IN THE MATTER OF|MATTER OF|"
    r"PETITION OF|RE:|TRUST OF|REV(?:OCABLE|\.)? (?:LIVING )?TRUST|"
    r".*\bTRUST(?: OF| CREATED| AGREEMENT| DATED| U/?A| U/?D/?T)?\b)",
    re.IGNORECASE)


def is_non_adversarial(title: str) -> bool:
    if not title:
        return False
    t = NON_ADVERSARIAL_BANNER_RE.sub("", title)
    # Only treat trailing/embedded TRUST as non-adversarial when there's no
    # versus marker (a "X TRUST VS. Y" caption is still adversarial).
    if VS_TOKEN_RE.search(t):
        return False
    return bool(NON_ADVERSARIAL_RE.match(t)) or bool(NON_ADVERSARIAL_RE.match(title))


# ===========================================================================
# Helpers
# ===========================================================================
def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _strip_trailing_junk(name: str) -> str:
    """Trim trailing commas/periods/connectors left over from splitting."""
    name = name.strip()
    name = re.sub(r"[,\s]+$", "", name)
    name = re.sub(r"^[,\s]+", "", name)
    return name


def normalize_key(name: str) -> str:
    """Light normalized GROUPING key. NOT an identity assertion (see docstring).

    Uppercase, canonicalize the "&"/"AND" conjunction, drop punctuation, drop
    single-letter (middle-initial) tokens, drop a small set of suffix/role words,
    collapse whitespace.

    Conjunction canonicalization (WHY): "PACIFIC GAS & ELECTRIC" and "PACIFIC GAS
    AND ELECTRIC" are the SAME organization, but the old key stripped "&" as bare
    punctuation (-> "PACIFIC GAS ELECTRIC") while keeping the word "AND" (->
    "PACIFIC GAS AND ELECTRIC"), so the two spellings never shared a blocking key
    and the entity shattered into separate clusters. We fold "&" to the literal
    word " AND " up front so both spellings collapse to one key. This is a pure
    conjunction-spelling fold (ampersand == the word "and"); it does NOT merge
    otherwise-distinct names, and the suffix logic below still treats
    "...ELECTRIC" vs "...ELECTRIC COMPANY" as different keys (reconciled, if at
    all, by the explicit alias/union pass, not by this fold).
    """
    up = name.upper()
    up = up.replace("&", " AND ")             # "&" == the word "AND" (one entity)
    up = re.sub(r"[^\w\s]", " ", up)          # strip punctuation
    tokens = up.split()
    kept = []
    for t in tokens:
        if len(t) == 1:                        # middle initial / stray letter
            continue
        if t in NAME_SUFFIXES:
            continue
        kept.append(t)
    return " ".join(kept).strip()


def display_name(name: str, title_case: bool) -> str:
    out = _collapse_ws(name)
    if title_case:
        out = out.title()
        # fix common all-caps abbreviations that title() mangles
        out = re.sub(r"\bLlc\b", "LLC", out)
        out = re.sub(r"\bLlp\b", "LLP", out)
        out = re.sub(r"\bN\.a\.\b", "N.A.", out, flags=re.IGNORECASE)
        out = re.sub(r"\bU\.s\.\b", "U.S.", out, flags=re.IGNORECASE)
    return out


# ---------------------------------------------------------------------------
# Versus split
# ---------------------------------------------------------------------------
def split_versus(title: str):
    """Return (left, right, marker) or (None, None, None) if no versus marker.

    "VS." / "VS" (case-insensitive, with or without surrounding spaces, e.g. the
    occasional run-together "INC.VS.RESOURCEFUL") is tried first and is always
    trusted. " V. " / " V " is then tried ONLY as a last resort and ONLY when the
    caption is not a non-adversarial (estate / conservatorship / in re) caption,
    because in this dataset nearly every non-"VS" occurrence of " V. " was a
    personal middle initial (e.g. "HALLA V. HAMPTON"), not a versus marker. We
    accept " V. " only when the text on the RIGHT of the marker is more than a
    single bare name word -- a real defendant ("CITY AND COUNTY OF ...", "Regents
    of the University ...", "ESSILOR LABORATORIES ...") -- which reliably
    distinguishes true separators from "<first> X. <last>" middle initials.
    See the module-level V. caveat.
    """
    if not title:
        return None, None, None

    # "VS." / "VS" as a COMPLETE token -- tolerate missing spaces (e.g. the
    # run-together "INC.VS.RESOURCEFUL") but require that "VS" is not part of a
    # larger word on either side (so "VST 2020 ... LLC" is not split at "VS").
    # A token boundary = start/space/punctuation on the left, and "." or
    # space/punctuation on the right.
    m = VS_TOKEN_RE.search(title)
    if m and title[m.end():].strip() and title[:m.start()].strip():
        return title[:m.start()], title[m.end():], "VS"

    # Guarded " V. " / " V " — suppressed for non-adversarial captions.
    if is_non_adversarial(title):
        return None, None, None
    m = V_TOKEN_RE.search(title)
    if m:
        left = title[:m.start()]
        right = title[m.end():]
        # Reject middle-initial shape: a bare single name word immediately to
        # the right of "V." (e.g. "HALLA V. HAMPTON" -> right == "HAMPTON").
        right_first = right.strip()
        is_single_word = bool(re.match(r"^[A-Za-z][A-Za-z'\-]*$", right_first))
        if is_single_word:
            return None, None, None
        if len(left.strip()) >= 3 and len(right.strip()) >= 3:
            return left, right, "V"
    return None, None, None


def split_side(side: str):
    """Split one caption side into (parties[], has_et_al)."""
    has_et_al = bool(ET_AL_RE.search(side))
    side = ET_AL_RE.sub("", side)

    # Mask protected AND-phrases so PARTY_SPLIT_RE leaves them intact.
    masks = {}
    masked = side

    # FIRST mask organization names that legitimately span an "AND"/"&" (e.g.
    # "PACIFIC GAS AND ELECTRIC COMPANY", "BROWN & TOLAND MEDICAL GROUP"). Done
    # before the static AND_PROTECTED list so the longer, side-specific org span
    # wins. Each span is verbatim from `side`, so a literal (case-sensitive)
    # replace restores casing without a callback. See org_and_spans() for why
    # this is high-precision (generic "PERSON AND PERSON" sides are NOT matched).
    for j, phrase in enumerate(org_and_spans(masked)):
        if phrase and phrase in masked:
            token = f"\x00O{j}\x00"
            masked = masked.replace(phrase, token)
            masks[token] = phrase

    for i, phrase in enumerate(AND_PROTECTED):
        token = f"\x00P{i}\x00"
        # case-insensitive replace, preserve original casing via callback
        pat = re.compile(re.escape(phrase), re.IGNORECASE)
        masked, n = pat.subn(token, masked)
        if n:
            masks[token] = phrase

    raw_parts = PARTY_SPLIT_RE.split(masked)
    parties = []
    for p in raw_parts:
        for token, phrase in masks.items():
            p = p.replace(token, phrase)
        p = _strip_trailing_junk(p)
        # Drop residual et-al / empty fragments / pure suffix tokens.
        if not p:
            continue
        if ET_AL_RE.search(" " + p):
            has_et_al = True
            p = ET_AL_RE.sub("", p).strip()
            if not p:
                continue
        parties.append(p)
    return parties, has_et_al


# ---------------------------------------------------------------------------
# Litigant-type classification
# ---------------------------------------------------------------------------
def classify_party(name: str):
    """Return (litigant_type, subtype). litigant_type in {individual,entity,unknown}."""
    up = " " + name.upper() + " "
    # Find best entity subtype by priority.
    matched = [st for st in ENTITY_SUBTYPE_ORDER
               if ENTITY_SUBTYPE_PATTERNS[st].search(up)]
    if matched:
        return "entity", matched[0]
    if GENERIC_ENTITY_RE.search(up):
        return "entity", "unknown"

    # Heuristic individual: looks like "FIRST [MIDDLE] LAST" (2-4 cap words,
    # no entity tokens). Otherwise leave unknown to avoid over-claiming.
    cleaned = re.sub(r"[^\w\s]", " ", name).split()
    if 1 <= len(cleaned) <= 4 and all(re.match(r"^[A-Za-z][A-Za-z'\-]*$", t)
                                      for t in cleaned):
        return "individual", None
    return "unknown", None


# A party that classify_party() left 'unknown' but that CLEARLY looks like an
# organization -- a truncated entity-formation descriptor ("..., A CALIFORNIA"
# / "A DELAWARE" / "A NON-PROFIT" / ... cut off before the CORPORATION / COMPANY
# / LLC word). This is a high-precision "looks like an org but matched no
# subtype" signal that we BUG-REPORT (rather than silently leaving as unknown),
# so the entity heuristic can be improved to recover the truncated subtype.
ORG_LOOKING_RE = re.compile(
    r"(?i),?\s+(?:A|AN)\s+"
    r"(?:CALIFORNIA|DELAWARE|NEVADA|NEW YORK|TEXAS|FLORIDA|ARIZONA|FEDERAL|"
    r"NATIONAL|PUBLIC|MUNICIPAL|NON[- ]?PROFIT|NONPROFIT|GENERAL|FOREIGN|"
    r"DOMESTIC|PROFESSIONAL|LIMITED)\s*$")


def looks_like_unclassified_org(name: str) -> bool:
    """True when an 'unknown'-classified party clearly looks like an org.

    Used only for bug reporting -- see ORG_LOOKING_RE. Requires the truncated
    entity-formation descriptor at the end of the name so we stay high-precision
    (sole-proprietor DBA/AKA aliases and ordinary individuals do NOT match).
    """
    return bool(name) and bool(ORG_LOOKING_RE.search(name))


def best_party_subtype(parties_meta):
    """Pick a representative subtype across a side's parties (first non-unknown)."""
    for _, _, st in parties_meta:
        if st and st != "unknown":
            return st
    for lt, _, _ in parties_meta:
        if lt == "entity":
            return "unknown"
    return None


def side_litigant_types(parties_meta):
    """Ordered, de-duplicated list of EVERY party's litigant_type on a side."""
    return list(dict.fromkeys(lt for lt, _, _ in parties_meta if lt))


def side_subtypes(parties_meta):
    """Ordered, de-duplicated list of every concrete entity_subtype on a side.

    Only real subtypes are included (the bare 'unknown' entity placeholder and
    None are dropped) so a trust/bank/etc. co-party is always surfaced.
    """
    return list(dict.fromkeys(st for _, _, st in parties_meta
                              if st and st != "unknown"))


# ---------------------------------------------------------------------------
# Tagging over text
# ---------------------------------------------------------------------------
def tag_text(blob: str):
    matter = sorted({tag for tag, pat in MATTER_TYPE_PATTERNS.items()
                     if pat.search(blob)})
    coa = sorted({tag for tag, pat in CAUSE_OF_ACTION_PATTERNS.items()
                  if pat.search(blob)})
    fee_waiver = bool(FEE_WAIVER_RE.search(blob))
    propria_persona = bool(PROPRIA_PERSONA_RE.search(blob))
    hearing_required = has_hearing_required_signal(blob)
    return matter, coa, fee_waiver, propria_persona, hearing_required


def _bool_signal(value, fallback):
    if value is None:
        return fallback
    try:
        if pd.isna(value):
            return fallback
    except (TypeError, ValueError):
        pass
    return bool(value)


# ===========================================================================
# Per-case enrichment
# ===========================================================================
def enrich_case(row, title_case=False):
    title = (row.get("case_title") or "").strip()
    rec = {
        "case_number": row.get("case_number"),
        "department": row.get("department"),
        "case_title": title,                     # original, preserved
        "parse_ok": False,
        "non_adversarial": is_non_adversarial(title),
        "plaintiffs": [],
        "defendants": [],
        "plaintiffs_display": [],
        "defendants_display": [],
        "plaintiff_count": 0,
        "defendant_count": 0,
        "has_et_al": False,
        "plaintiff_keys": [],
        "defendant_keys": [],
        "plaintiff_litigant_type": None,
        "defendant_litigant_type": None,
        "plaintiff_subtype": None,
        "defendant_subtype": None,
        # Full per-side type/subtype sets so secondary parties (e.g. a trust
        # co-defendant) aren't hidden behind the first party's classification.
        "plaintiff_litigant_types": [],
        "defendant_litigant_types": [],
        "plaintiff_subtypes": [],
        "defendant_subtypes": [],
        "litigant_types": [],     # flat list of every party's type
        "matter_type": [],
        "cause_of_action": [],
        "fee_waiver": False,
        "propria_persona": False,
        "outcome": [],
        "instrument": [],
        "name_match_confidence": None,
    }
    cn = row.get("case_number")

    left, right, marker = split_versus(title)
    if left is not None:
        p_parties, p_etal = split_side(left)
        d_parties, d_etal = split_side(right)
        rec["parse_ok"] = bool(p_parties and d_parties)
        rec["has_et_al"] = p_etal or d_etal

        # BUG REPORT (doesn't fit): a real versus marker WAS found but one/both
        # sides parsed to zero parties (e.g. "CHERRYL ABDULLAH VS." -- truncated
        # caption). Surfaced, not silently dropped, unless it's obvious nonsense.
        if not rec["parse_ok"]:
            report_bug(
                case_number=cn, namespace_or_field="parties", value=title,
                reason="cant_fit:versus_marker_but_unparsed",
                heuristic="split_versus+split_side")

        rec["plaintiffs"] = p_parties
        rec["defendants"] = d_parties
        rec["plaintiffs_display"] = [display_name(x, title_case) for x in p_parties]
        rec["defendants_display"] = [display_name(x, title_case) for x in d_parties]
        rec["plaintiff_count"] = len(p_parties)
        rec["defendant_count"] = len(d_parties)
        rec["plaintiff_keys"] = [normalize_key(x) for x in p_parties]
        rec["defendant_keys"] = [normalize_key(x) for x in d_parties]

        p_meta = [(lt, x, st) for x in p_parties
                  for (lt, st) in [classify_party(x)]]
        d_meta = [(lt, x, st) for x in d_parties
                  for (lt, st) in [classify_party(x)]]
        rec["litigant_types"] = [lt for lt, _, _ in p_meta + d_meta]
        rec["plaintiff_litigant_type"] = (
            p_meta[0][0] if p_meta else None)
        rec["defendant_litigant_type"] = (
            d_meta[0][0] if d_meta else None)
        rec["plaintiff_subtype"] = best_party_subtype(p_meta)
        rec["defendant_subtype"] = best_party_subtype(d_meta)
        # Full per-side sets (all parties, not just the first).
        rec["plaintiff_litigant_types"] = side_litigant_types(p_meta)
        rec["defendant_litigant_types"] = side_litigant_types(d_meta)
        rec["plaintiff_subtypes"] = side_subtypes(p_meta)
        rec["defendant_subtypes"] = side_subtypes(d_meta)

        # BUG REPORT (doesn't fit): a party that classified as 'unknown' but
        # clearly looks like an organization (a truncated "..., A CALIFORNIA"
        # entity-formation descriptor). The entity-subtype heuristic missed it;
        # log so it can be improved rather than silently leaving it 'unknown'.
        for lt, pname, st in p_meta + d_meta:
            if lt == "unknown" and looks_like_unclassified_org(pname):
                report_bug(
                    case_number=cn, namespace_or_field="entity_subtype",
                    value=pname,
                    reason="cant_fit:org_looking_but_unclassified",
                    heuristic="classify_party")
    else:
        # Non-adversarial / unparseable: still classify the subject if present.
        subj = NON_ADVERSARIAL_BANNER_RE.sub("", title) if title else ""
        subj = NON_ADVERSARIAL_RE.sub("", subj).strip()
        if subj:
            lt, st = classify_party(subj)
            rec["litigant_types"] = [lt]
            rec["plaintiff_litigant_type"] = lt
            rec["plaintiff_subtype"] = st
            rec["plaintiff_litigant_types"] = [lt] if lt else []
            rec["plaintiff_subtypes"] = (
                [st] if st and st != "unknown" else [])
        # BUG REPORT (doesn't fit): a title the party heuristic can't fit into a
        # P-vs-D caption that is ALSO not a recognized non-adversarial caption
        # (estate/conservatorship/in re/trust). Reported regardless of whether a
        # fallback subject was classified above. Two flavours:
        #   * a DANGLING versus marker -- a "VS"/"VS." token is present but one
        #     side is empty (e.g. "CHERRYL ABDULLAH VS." -- truncated caption);
        #   * no versus marker at all (a stray "MICHAEL BENBEN", a malformed
        #     "EST.. OF ..." with bad period spacing, "ADN OF ...", a
        #     transferred-out banner, or a bare "PETITION FOR ..." caption).
        # Obvious nonsense is filtered inside report_bug; real-but-unfit titles
        # are logged for heuristic development.
        if title and not rec["non_adversarial"]:
            if VS_TOKEN_RE.search(title):
                report_bug(
                    case_number=cn, namespace_or_field="parties", value=title,
                    reason="cant_fit:versus_marker_but_unparsed",
                    heuristic="split_versus+split_side")
            else:
                report_bug(
                    case_number=cn, namespace_or_field="parties", value=title,
                    reason="cant_fit:no_versus_and_not_non_adversarial",
                    heuristic="split_versus+is_non_adversarial")

    # ---- tagging over caption + (representative) ruling text --------------
    blob = " ".join(filter(None, [
        title,
        row.get("calendar_matter") or "",
        row.get("ruling_substantive") or "",
        row.get("ruling") or "",
    ]))
    (rec["matter_type"], rec["cause_of_action"],
     fee_waiver_blob, propria_blob, hearing_required_blob) = tag_text(blob)
    # Fee-waiver: prefer the per-case full-text signal pre-computed in
    # load_cases (untruncated, across ALL rows); fall back to the (truncated)
    # tag blob when that column is absent (e.g. a record built ad hoc).
    row_fw = row.get("fee_waiver")
    rec["fee_waiver"] = _bool_signal(row_fw, fee_waiver_blob)
    # Propria-persona: same full-text-first resolution as fee_waiver.
    row_pp = row.get("propria_persona")
    rec["propria_persona"] = _bool_signal(row_pp, propria_blob)
    if hearing_required_blob:
        rec["outcome"].append("hearing-required")

    # ---- instrument tags (Tier-1 / tentative, from the CAPTION) -----------
    rec["instrument"] = tag_instrument(
        title,
        row.get("calendar_matter") or "",
        " ".join(filter(None, [row.get("ruling_substantive") or "",
                               row.get("ruling") or ""])),
    )

    return rec


# ===========================================================================
# Normalized tags (long format: one row per (case, tag))
# ===========================================================================
# Every current tag is Tier-1 / tentative-derived (see DESIGN_NOTES.md
# "Tag provenance & source tiers"): inferred SOLELY from tentative-ruling text,
# so each row is stamped tentative=True / tier=1 / source="tentative" /
# authority=1 -- all READ FROM tag_registry.json (registry.sources.tentative),
# never hardcoded -- and the viewer renders them as DOTTED tentative pills.
#
# Namespaces (column `ns`) for Tier-1 tags (validated against
# registry.namespaces at emit time):
#   * matter         -- matter_type tag (rendered as an "ns-" pill).
#   * cause          -- cause_of_action tag (STANDALONE for Tier-1: no docket
#                       category parent yet, so `parent` is null; later a Tier-2
#                       `category:` tag becomes the parent of cause children).
#   * litigant       -- a party's litigant_type (individual / entity / unknown).
#   * entity_subtype -- a concrete entity subtype (bank / trust / corporation
#                       / government / ...); optional pill in the viewer.
#   * fee_waiver     -- indigent / in-forma-pauperis court-fee waiver. The
#                       registry source_of_truth is the DOCKET ROA/Payments;
#                       this tentative-text hit (~50 cases) is the low-recall
#                       dotted fallback until docket capture runs.
#   * instrument     -- caption-derived disposition / filed-instrument type
#                       (fee-award / judgment / stipulation). Derived from the
#                       motion/petition caption (case_title + calendar_matter,
#                       ruling text as backup); Tier-1 / tentative today, with
#                       the docket disposition as the eventual source of truth.
# `category` is RESERVED for the future docket Tier-2 tag and is NOT emitted
# here.
TAGS_COLUMNS = [
    "case_number", "department", "ns", "name",
    "parent", "tier", "source", "authority", "tentative",
]


def build_tags_rows(records, registry=None):
    """Flatten enriched case records into long-format normalized tag rows.

    Returns a list of dicts (one per (case, tag)) with TAGS_COLUMNS keys. Every
    row's provenance (source/tier/authority/tentative) is STAMPED FROM THE
    CENTRAL REGISTRY (tag_registry.json), not hardcoded: the current data
    source for every Tier-1 tag is "tentative", so each row reads its
    tier/authority/tentative from ``registry.sources.tentative``. Each emitted
    ``ns`` is validated against ``registry.namespaces`` (a warning is printed
    for any unknown namespace).
    """
    if registry is None:
        registry = get_tag_registry()
    rows = []

    # Provenance for the CURRENT source of every Tier-1 tag: "tentative".
    src_name = "tentative"
    src = registry["sources"][src_name]
    src_tier = src["tier"]
    src_authority = src["authority"]
    src_tentative = src["tentative"]

    known_ns = set(registry.get("namespaces", {}))
    known_values = {
        ns: {normalize_tag_value(ns, v) for v in spec.get("values", [])}
        for ns, spec in registry.get("namespaces", {}).items()
        if spec.get("values")
    }
    warned_ns = set()
    warned_values = set()

    def emit(case_number, department, ns, name):
        name = normalize_tag_value(ns, name)
        if not name:
            return
        if ns not in known_ns and ns not in warned_ns:
            warned_ns.add(ns)
            print(f"WARNING: emitted ns {ns!r} is not in "
                  f"tag_registry.json namespaces", file=sys.stderr)
        if ns in known_values and name not in known_values[ns] and (ns, name) not in warned_values:
            warned_values.add((ns, name))
            print(f"WARNING: emitted tag {ns}:{name} is not in "
                  f"tag_registry.json values", file=sys.stderr)
        rows.append({
            "case_number": case_number,
            "department": department,
            "ns": ns,
            "name": name,
            "parent": None,      # Tier-1 tags are standalone (no docket parent)
            "tier": src_tier,
            "source": src_name,
            "authority": src_authority,
            "tentative": src_tentative,
        })

    for r in records:
        cn = r.get("case_number")
        dept = r.get("department")

        # matter_type -> ns="matter"
        for name in r.get("matter_type") or []:
            emit(cn, dept, "matter", name)

        # cause_of_action -> ns="cause" (standalone for Tier-1)
        for name in r.get("cause_of_action") or []:
            emit(cn, dept, "cause", name)

        # litigant_type (all parties, both sides) -> ns="litigant"
        litigant_types = (
            (r.get("plaintiff_litigant_types") or [])
            + (r.get("defendant_litigant_types") or [])
        )
        if not litigant_types:
            # Non-adversarial subject / fallback flat list.
            litigant_types = r.get("litigant_types") or []
        for name in dict.fromkeys(litigant_types):   # de-dup, keep order
            emit(cn, dept, "litigant", name)

        # concrete entity subtypes (all parties, both sides) -> ns="entity_subtype"
        subtypes = (
            (r.get("plaintiff_subtypes") or [])
            + (r.get("defendant_subtypes") or [])
        )
        if not subtypes:
            for st in (r.get("plaintiff_subtype"), r.get("defendant_subtype")):
                if st and st != "unknown":
                    subtypes.append(st)
        for name in dict.fromkeys(subtypes):         # de-dup, keep order
            emit(cn, dept, "entity_subtype", name)

        # fee_waiver -> ns="fee_waiver" / name="fee-waiver" (registry
        # source_of_truth is the docket; this tentative-text hit is the
        # low-recall dotted fallback).
        if r.get("fee_waiver"):
            emit(cn, dept, "fee_waiver", "fee-waiver")

        # propria-persona -> ns="status" (self-represented appearance). Registry
        # source_of_truth is the docket (a party listed with NO attorney / "PRO
        # PER"); this tentative-text hit is the low-recall dotted fallback.
        if r.get("propria_persona"):
            emit(cn, dept, "status", "propria-persona")

        # outcome -> ns="outcome" (ruling-derived disposition tags). The full
        # viewer has a richer JS outcome classifier; this parquet tag exposes
        # high-priority hearing-required tentatives to search/buckets.
        for name in r.get("outcome") or []:
            emit(cn, dept, "outcome", name)

        # instrument -> ns="instrument" (caption-derived disposition tags:
        # fee-award / judgment / stipulation). Tier-1 / tentative today.
        for name in r.get("instrument") or []:
            emit(cn, dept, "instrument", name)

    return rows


def tags_dataframe(records, registry=None):
    """Build the normalized tags rows and return them as a typed DataFrame."""
    rows = build_tags_rows(records, registry=registry)
    df = pd.DataFrame(rows, columns=TAGS_COLUMNS)
    # Stable dtypes even when empty.
    df["tier"] = df["tier"].astype("int64") if len(df) else pd.Series([], dtype="int64")
    df["authority"] = (
        df["authority"].astype("int64") if len(df)
        else pd.Series([], dtype="int64"))
    df["tentative"] = (
        df["tentative"].astype("bool") if len(df)
        else pd.Series([], dtype="bool"))
    return df


# ===========================================================================
# Data loading / aggregation
# ===========================================================================
TEXT_COLS = ["department", "case_number", "case_title",
             "calendar_matter", "ruling", "ruling_substantive"]


def load_cases(department=None, limit=None):
    """Load rulings and collapse to one representative row per case_number.

    For tag recall we concatenate calendar_matter + ruling text across ALL of a
    case's rows (a case spans many motions), then keep one title per case.
    """
    if department:
        path = dept_parquet(department)
        if not os.path.exists(path):
            print(f"ERROR: no parquet for department {department}: {path}",
                  file=sys.stderr)
            sys.exit(2)
    else:
        path = CANONICAL_PARQUET
        if not os.path.exists(path):
            print(f"ERROR: no canonical parquet: {path}", file=sys.stderr)
            sys.exit(2)

    # Only request columns the target parquet actually has (per-department
    # slices omit ruling_substantive; the canonical parquet has it).
    import pyarrow.parquet as pq
    available = set(pq.read_schema(path).names)
    cols = [c for c in TEXT_COLS if c in available]
    df = pd.read_parquet(path, columns=cols)
    for c in TEXT_COLS:
        if c not in df.columns:
            df[c] = ""
    if department:
        df = df[df["department"].astype(str) == str(department)]

    df = df.fillna("")
    # Per-ROW fee-waiver signal over the FULL (untruncated) case_title +
    # calendar_matter + ruling, then OR'd per case below. The general tag blob
    # is length-capped (8000 chars) for performance, which can hide a late
    # fee-waiver mention; this low-recall fallback is evaluated on the full
    # text across ALL of a case's rows so no signal is dropped to truncation.
    _fw_blob = (df["case_title"].astype(str) + " "
                + df["calendar_matter"].astype(str) + " "
                + df["ruling"].astype(str))
    # na=False so a NaN blob (already covered by fillna above, but defensive)
    # yields False rather than NaN, which downstream bool() would treat as True.
    df["_fee_waiver_row"] = _fw_blob.str.contains(FEE_WAIVER_RE, na=False)
    # Same full-text-per-case treatment for the self-represented signal.
    df["_propria_persona_row"] = _fw_blob.str.contains(PROPRIA_PERSONA_RE, na=False)

    # Aggregate text per case so tags see the whole case, not one motion.
    grouped = (
        df.groupby("case_number", sort=False)
          .agg(
              department=("department", "first"),
              case_title=("case_title", "first"),
              calendar_matter=("calendar_matter",
                               lambda s: " || ".join(dict.fromkeys(
                                   x for x in s if x))),
              ruling_substantive=("ruling_substantive",
                                  lambda s: " ".join(dict.fromkeys(
                                      x for x in s if x))[:8000]),
              ruling=("ruling",
                      lambda s: " ".join(dict.fromkeys(
                          x for x in s if x))[:8000]),
              fee_waiver=("_fee_waiver_row", "any"),
              propria_persona=("_propria_persona_row", "any"),
          )
          .reset_index()
    )
    if limit:
        grouped = grouped.head(limit)
    return grouped


# ===========================================================================
# CLI / reporting
# ===========================================================================
def _json_safe(rec):
    return rec  # records are already JSON-native (lists/strs/bools/ints/None)


def run(args):
    # Fresh bug-report collector for this invocation. enrich_case() appends to
    # the module-level BUG_REPORTER as it encounters values its heuristics
    # can't fit (the noise filter is applied inside report_bug()).
    global BUG_REPORTER
    BUG_REPORTER = BugReporter()

    cases = load_cases(department=args.department, limit=args.limit)
    records = [enrich_case(row, title_case=args.title_case)
               for _, row in cases.iterrows()]

    # ---- name-variant grouping (heuristic) -------------------------------
    # Build norm_key -> set of original spellings to illustrate grouping and
    # set per-record name_match_confidence.
    key_to_originals = {}
    for r in records:
        for orig, key in zip(r["plaintiffs"] + r["defendants"],
                             r["plaintiff_keys"] + r["defendant_keys"]):
            if not key:
                continue
            key_to_originals.setdefault(key, set()).add(orig.upper().strip())
    for r in records:
        confs = []
        for orig, key in zip(r["plaintiffs"] + r["defendants"],
                             r["plaintiff_keys"] + r["defendant_keys"]):
            if not key:
                continue
            variants = key_to_originals.get(key, set())
            # 'high' if this exact spelling is the only one under the key,
            # else 'low' (multiple spellings collapsed -> heuristic only).
            confs.append("high" if len(variants) <= 1 else "low")
        r["name_match_confidence"] = (
            "low" if "low" in confs else ("high" if confs else None))

    if args.json_out:
        write_text_atomic(args.json_out, "".join(json.dumps(_json_safe(r)) + "\n" for r in records))
        print(f"Wrote {len(records)} case records to {args.json_out}")

    if args.tags_parquet:
        tags_df = tags_dataframe(records)
        if not args.dry_run:
            write_parquet_atomic(args.tags_parquet, tags_df)
            print(f"Wrote {len(tags_df)} tag rows "
                  f"({tags_df['case_number'].nunique()} cases) "
                  f"to {args.tags_parquet}")
        else:
            print(f"[dry-run] would write {len(tags_df)} tag rows "
                  f"({tags_df['case_number'].nunique()} cases) "
                  f"to {args.tags_parquet}")

    # ---- heuristic bug-report log ----------------------------------------
    # Always SUMMARIZE what the heuristics couldn't fit; write the NDJSON unless
    # this is a dry run (so a dry run never mutates files on disk).
    n_bugs = len(BUG_REPORTER.reports)
    n_noise = BUG_REPORTER.suppressed_nonsense
    if args.bug_report and not args.dry_run:
        written = BUG_REPORTER.write(args.bug_report)
        print(f"Wrote {written} heuristic bug report(s) to {args.bug_report} "
              f"({n_noise} obvious-nonsense value(s) filtered as noise)")
    else:
        print(f"[dry-run] {n_bugs} heuristic bug report(s) collected "
              f"({n_noise} obvious-nonsense value(s) filtered as noise); "
              f"would write to {args.bug_report}")

    if args.dry_run or not (args.json_out or args.tags_parquet):
        print_report(records)


def print_report(records):
    n = len(records)
    if not n:
        print("No cases to report (empty input).")
        return
    adversarial = [r for r in records if r["plaintiffs"] or r["defendants"]]
    parsed = [r for r in records if r["parse_ok"]]
    non_adv = [r for r in records if r["non_adversarial"]]
    failed = [r for r in records
              if not r["parse_ok"] and not r["non_adversarial"]]

    print("=" * 72)
    print("SFSC CASE ENRICHMENT — PROTOTYPE DRY RUN")
    print("=" * 72)
    print(f"Distinct cases analyzed     : {n}")
    print(f"Parsed cleanly into parties : {len(parsed)} "
          f"({100*len(parsed)/n:.1f}%)")
    print(f"Non-adversarial (no VS)     : {len(non_adv)} "
          f"({100*len(non_adv)/n:.1f}%)  [estate/conservatorship/in re]")
    print(f"Had versus marker but failed: {len(failed)} "
          f"({100*len(failed)/n:.1f}%)")

    # --- party stats ------------------------------------------------------
    et_al = sum(1 for r in parsed if r["has_et_al"])
    pc = [r["plaintiff_count"] for r in parsed]
    dc = [r["defendant_count"] for r in parsed]
    print()
    print("--- Party extraction (parsed cases) ---")
    if parsed:
        print(f"has_et_al                   : {et_al} "
              f"({100*et_al/len(parsed):.1f}%)")
        print(f"avg plaintiffs / defendants : "
              f"{sum(pc)/len(pc):.2f} / {sum(dc)/len(dc):.2f}")
        print(f"max plaintiffs / defendants : {max(pc)} / {max(dc)}")

    # --- litigant_type distribution --------------------------------------
    lt_counter = Counter()
    for r in records:
        lt_counter.update(r["litigant_types"])
    print()
    print("--- litigant_type distribution (per party) ---")
    total_lt = sum(lt_counter.values()) or 1
    for lt, c in lt_counter.most_common():
        print(f"  {lt:12s}: {c:7d}  ({100*c/total_lt:.1f}%)")

    # --- entity subtype distribution -------------------------------------
    st_counter = Counter()
    for r in records:
        for st in (r["plaintiff_subtype"], r["defendant_subtype"]):
            if st:
                st_counter[st] += 1
    print()
    print("--- entity subtype distribution (party-sides) ---")
    for st, c in st_counter.most_common():
        print(f"  {st:16s}: {c:7d}")

    # --- matter_type / cause_of_action -----------------------------------
    mt_counter, coa_counter = Counter(), Counter()
    for r in records:
        mt_counter.update(r["matter_type"])
        coa_counter.update(r["cause_of_action"])
    tagged_mt = sum(1 for r in records if r["matter_type"])
    tagged_coa = sum(1 for r in records if r["cause_of_action"])
    print()
    print(f"--- matter_type tags ({tagged_mt} cases tagged, "
          f"{100*tagged_mt/n:.1f}%) ---")
    for t, c in mt_counter.most_common():
        print(f"  {t:18s}: {c:7d}")
    print()
    print(f"--- cause_of_action tags ({tagged_coa} cases tagged, "
          f"{100*tagged_coa/n:.1f}%) ---")
    for t, c in coa_counter.most_common():
        print(f"  {t:24s}: {c:7d}")

    # --- examples --------------------------------------------------------
    print()
    print("--- ~10 example cases (parties + tags) ---")
    examples = [r for r in parsed if r["matter_type"] or r["cause_of_action"]]
    examples = examples[:8] + [r for r in non_adv[:2]]
    for r in examples[:10]:
        print()
        print(f"  [{r['department']}] {r['case_number']}  {r['case_title'][:80]}")
        print(f"    plaintiffs : {r['plaintiffs']} "
              f"({r['plaintiff_litigant_type']}/{r['plaintiff_subtype']})")
        print(f"    defendants : {r['defendants']} "
              f"({r['defendant_litigant_type']}/{r['defendant_subtype']})")
        print(f"    et_al={r['has_et_al']}  "
              f"name_match_confidence={r['name_match_confidence']}")
        print(f"    matter_type={r['matter_type']}  "
              f"cause_of_action={r['cause_of_action']}")


def build_parser():
    p = argparse.ArgumentParser(
        description="Enrich SFSC tentative-ruling cases (parties + tags). "
                    "PROTOTYPE; pandas + stdlib only; no network.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print party-extraction + tag distributions and "
                        "~10 example cases.")
    p.add_argument("--department", default=None,
                   help="Restrict to one department (e.g. 302) and read its "
                        "per-department parquet.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N cases (after aggregation).")
    p.add_argument("--json-out", default=None,
                   help="Write one JSON object per case (JSONL) to this path.")
    p.add_argument("--tags-parquet", default=None,
                   help="Write the normalized long-format tags parquet (one "
                        "row per (case, tag); columns: case_number, department, "
                        "ns, name, parent, tier, source, authority, tentative) "
                        "to this path. Provenance is stamped from "
                        "tag_registry.json; all rows are Tier-1 "
                        "tentative-derived today.")
    p.add_argument("--title-case", action="store_true",
                   help="Also produce title-cased display names "
                        "(originals always preserved).")
    p.add_argument("--bug-report", default=DEFAULT_BUG_REPORT_PATH,
                   help="Path for the heuristic bug-report NDJSON log (one JSON "
                        "object per line: ts, case_number, namespace_or_field, "
                        "value, candidates, reason, heuristic). Records values "
                        "the heuristics can't fit (obvious nonsense is filtered "
                        "as noise) and cross-source disagreements (both "
                        "candidates kept, none auto-prioritized). "
                        f"Default: {DEFAULT_BUG_REPORT_PATH}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not (args.dry_run or args.json_out or args.tags_parquet):
        args.dry_run = True  # default to a dry run so it's runnable bare
    run(args)


if __name__ == "__main__":
    main()
