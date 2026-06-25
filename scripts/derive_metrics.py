#!/usr/bin/env python3
"""Derive fuzzy second/third-order metrics for attorney / judge / litigant profiles.

This is an INFERENCE layer on top of data we already have. None of it is a court
finding; every metric is a heuristic roll-up of signals parsed from docket text
and tentative rulings, and each underlying signal keeps the verbatim snippet it
came from (CLAUDE.md: "cite, verbatim, or don't assert it"). Treat the numbers as
reasonably-fuzzy indicators, not scores — they are explicitly labelled "inferred"
in the viewer.

Three signal families, then three roll-ups:

  SIGNALS
  * case outcomes (from archive/cases docket text) — dismissals split by
    prejudice (with / without) and voluntariness (voluntary / involuntary),
    judgments (entry / default), settlements, and appellate dispositions
    (affirmed / reversed / reversed-in-part / remanded / remittitur / appeal
    filed). Each carries an ``abstract_valence`` — the GENERIC reading the owner
    described (e.g. voluntary dismissal *with* prejudice usually means the matter
    is resolved; *without* prejudice usually means a refile/venue fix; a reversal
    is adverse to whoever prevailed below) — NOT a party-specific win/loss.
  * tentative motion dispositions (from tentatives) — motion TYPE x DISPOSITION
    (granted / denied / sustained / overruled / in-part / procedural), reusing
    scripts/index_appeals.classify_outcome + classify_dispositive_motion.

  ROLL-UPS (data/profile-metrics.json, keyed to the profile keys the viewer uses)
  * judicial_officers — grant vs deny rates overall and per motion type, for
                EVERY named tentative author: judges, commissioners, and (for
                Dept 204 Probate) examiners. Each carries an inferred
                ``officer_type`` (probate_examiner / judge_or_commissioner /
                mixed_officer) and the department(s) it appears in. The cleanest,
                most defensible metric (the owner: "very easy to infer").
  * attorneys — outcome distribution across their cases, split by the side they
                appeared for, plus an appellate record and a single coarse,
                side-aware ``favorable_rate`` (low confidence, documented).
  * litigants — outcome distribution across a clustered litigant's cases
                (only when data/litigants.json is present).

Outputs:
  data/case_outcomes.parquet          — one row per detected case-outcome signal
  data/tentative_dispositions.parquet — one row per tentative motion disposition
  data/profile-metrics.json           — compact metrics keyed by profile

Dependencies: pandas + stdlib. Pure classifiers (classify_*) are stdlib-only and
unit-tested in scripts/check_derive_metrics.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_case_tables as bct  # noqa: E402  (rows_from_cases, helpers, atomic writers)
import index_appeals as ia       # noqa: E402  (classify_outcome, classify_dispositive_motion, families)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CASE_DIR = os.path.join(REPO, "archive", "cases")
DEFAULT_TENTATIVES_GLOB = os.path.join(REPO, "data", "tentatives-*.parquet")
DEFAULT_LITIGANTS_JSON = os.path.join(REPO, "data", "litigants.json")
DEFAULT_OUT_DIR = os.path.join(REPO, "data")
DEFAULT_JUDGES_JSON = os.path.join(REPO, "judges.json")
PROFILE_METRICS_SHARD_BYTES = 40 * 1024 * 1024
PROFILE_METRICS_KINDS = ("judicial_officers", "attorneys", "litigants")


# ===========================================================================
# 1. Case-outcome classifiers over docket text (pure, stdlib only)
# ===========================================================================

# A dismissal RESULT (not a mere "motion to dismiss" caption, which may be denied).
_DISMISSAL_EVENT = re.compile(
    r"REQUEST\s+FOR\s+DISMISSAL|ORDER\s+(?:OF|FOR)\s+DISMISSAL|DISMISSAL\s+OF\s+(?:THE\s+)?"
    r"(?:ENTIRE\s+)?ACTION|VOLUNTARY\s+DISMISSAL|INVOLUNTARY\s+DISMISSAL|"
    r"\bDISMISSED\b|\bDISMISSAL\s+ENTERED\b", re.I)
# Guard: a dismissal MENTION that is denied/vacated/opposed is not a dismissal.
_DISMISSAL_NEG = re.compile(r"\b(?:DENY|DENIED|DENYING|VACAT\w*|OPPOS\w*|SET\s+ASIDE|"
                            r"MOTION\s+TO\s+DISMISS\s+(?:IS\s+)?(?:DENIED|OVERRULED))\b", re.I)
_WITH_PREJ = re.compile(r"\bWITH\s+PREJUDICE\b", re.I)
_WITHOUT_PREJ = re.compile(r"\bWITHOUT\s+PREJUDICE\b", re.I)
_VOLUNTARY = re.compile(r"\bVOLUNTARY\b|REQUEST\s+FOR\s+DISMISSAL", re.I)
_INVOLUNTARY = re.compile(r"\bINVOLUNTARY\b|FAILURE\s+TO\s+PROSECUTE|FOR\s+(?:WANT|LACK)\s+OF\s+"
                          r"PROSECUTION|DISCOVERY\s+SANCTION|TERMINATING\s+SANCTION", re.I)

_JUDGMENT_DEFAULT = re.compile(r"\bDEFAULT\s+JUDGMENT\b", re.I)
_JUDGMENT_ENTRY = re.compile(r"NOTICE\s+OF\s+ENTRY\s+OF\s+JUDGMENT|\bJUDGMENT\s+ENTERED\b|"
                             r"\bENTRY\s+OF\s+JUDGMENT\b|\bCONSENT\s+JUDGMENT\b|"
                             r"\bJUDGMENT\s+OF\s+DISMISSAL\b", re.I)
_SETTLED = re.compile(r"NOTICE\s+OF\s+SETTLEMENT|CONDITIONAL\s+SETTLEMENT|\bSETTLED\b", re.I)

_REMITTITUR = re.compile(r"\bREMITTITUR\b", re.I)
_NOTICE_OF_APPEAL = re.compile(r"\bNOTICE\s+OF\s+APPEAL\b", re.I)
_AFFIRMED = re.compile(r"\bAFFIRMED\b", re.I)
_REVERSED = re.compile(r"\bREVERSED\b", re.I)
_REMANDED = re.compile(r"\bREMANDED\b", re.I)
_IN_PART = re.compile(r"\bIN\s+PART\b", re.I)
_APPEAL_DISMISSED = re.compile(r"APPEAL\s+(?:IS\s+)?DISMISSED|DISMISS\w*\s+(?:THE\s+)?APPEAL", re.I)
_WRIT_KIND = r"(?:administrative\s+)?(?:mandate|mandamus)|prohibition|certiorari|review|supersedeas"
_EXTRAORDINARY_WRIT = re.compile(
    rf"\bwrit\s+of\s+(?:{_WRIT_KIND})\b|"
    rf"\bpetition\b.{{0,100}}\bwrit\b.{{0,100}}\b(?:{_WRIT_KIND})\b|"
    rf"\b(?:alternative|peremptory)\s+writ\b",
    re.I,
)
_WRIT_EXCLUDED = re.compile(r"\bwrit\s+of\s+(?:attachment|execution|possession)\b", re.I)
_WRIT_DENIED = re.compile(r"\b(?:denied|deny(?:ing)?)\b.{0,80}\bwrit\b|\bwrit\b.{0,80}\b(?:denied|deny(?:ing)?)\b", re.I)
_WRIT_GRANTED = re.compile(r"\b(?:granted|grant(?:ing)?)\b.{0,80}\bwrit\b|\bwrit\b.{0,80}\b(?:granted|grant(?:ing)?)\b", re.I)
_PEREMPTORY_WRIT = re.compile(r"\bperemptory\s+writ\b.{0,80}\b(?:issued|granted|ordered)?\b", re.I)
_ALTERNATIVE_WRIT = re.compile(r"\balternative\s+writ\b.{0,80}\b(?:issued|granted|ordered)?\b", re.I)
_WRIT_OSC = re.compile(r"\border\s+to\s+show\s+cause\b.{0,120}\bwrit\b|\bOSC\b.{0,120}\bwrit\b", re.I)
_WRIT_PETITION_FILED = re.compile(
    r"\bpetition\b.{0,80}\bwrit\b.{0,120}\bfiled\b|\bfiled\b.{0,80}\bpetition\b.{0,80}\bwrit\b",
    re.I,
)


def classify_dismissal(description: str) -> dict[str, str] | None:
    """A dismissal result with prejudice/voluntariness, or None.

    Returns {prejudice: with|without|unspecified, voluntariness:
    voluntary|involuntary|unspecified, signal, matched_text}.
    """
    text = bct.clean(description)
    up = text.upper()
    if not _DISMISSAL_EVENT.search(up) or _DISMISSAL_NEG.search(up):
        return None
    prejudice = ("with" if _WITH_PREJ.search(up)
                 else "without" if _WITHOUT_PREJ.search(up) else "unspecified")
    voluntariness = ("involuntary" if _INVOLUNTARY.search(up)
                     else "voluntary" if _VOLUNTARY.search(up) else "unspecified")
    signal = f"dismissal_{voluntariness}_{prejudice}_prejudice"
    return {"signal": signal, "prejudice": prejudice, "voluntariness": voluntariness,
            "matched_text": text[:160]}


def classify_appellate(description: str) -> dict[str, str] | None:
    """An appellate disposition, writ signal, remittitur, appeal signal, or None."""
    text = bct.clean(description)
    up = text.upper()
    is_writ = bool(_EXTRAORDINARY_WRIT.search(text)) and not _WRIT_EXCLUDED.search(text)
    if is_writ and _PEREMPTORY_WRIT.search(text):
        signal = "peremptory_writ_issued"
    elif is_writ and _ALTERNATIVE_WRIT.search(text):
        signal = "alternative_writ_issued"
    elif is_writ and _WRIT_OSC.search(text):
        signal = "writ_osc_issued"
    elif is_writ and _WRIT_DENIED.search(text):
        signal = "writ_denied"
    elif is_writ and _WRIT_GRANTED.search(text):
        signal = "writ_granted"
    elif is_writ and _WRIT_PETITION_FILED.search(text):
        signal = "writ_petition_filed"
    elif _AFFIRMED.search(up) and _REVERSED.search(up):
        signal = "affirmed_in_part_reversed_in_part"
    elif _REVERSED.search(up):
        signal = "reversed_in_part" if _IN_PART.search(up) else "reversed"
    elif _AFFIRMED.search(up):
        signal = "affirmed"
    elif _REMANDED.search(up):
        signal = "remanded"
    elif _APPEAL_DISMISSED.search(up):
        signal = "appeal_dismissed"
    elif _REMITTITUR.search(up):
        signal = "remittitur"
    elif _NOTICE_OF_APPEAL.search(up):
        signal = "notice_of_appeal"
    else:
        return None
    return {"signal": signal, "matched_text": text[:160]}


def classify_judgment(description: str) -> dict[str, str] | None:
    """A judgment-entry / default-judgment / settlement signal, or None."""
    text = bct.clean(description)
    up = text.upper()
    if _JUDGMENT_DEFAULT.search(up):
        signal = "default_judgment"
    elif _JUDGMENT_ENTRY.search(up):
        signal = "judgment_entered"
    elif _SETTLED.search(up):
        signal = "settled"
    else:
        return None
    return {"signal": signal, "matched_text": text[:160]}


# The GENERIC ("abstract") reading the owner described — NOT party-specific.
# resolved  = the matter is finished cleanly (claims barred / judgment / settle)
# tentative_refile = likely refiled elsewhere (without-prejudice dismissal)
# adverse_to_prevailing_below / favorable_to_prevailing_below = appellate result
# from the perspective of whoever won in the trial court.
_ABSTRACT_VALENCE = {
    "dismissal_voluntary_with_prejudice": "resolved",
    "dismissal_involuntary_with_prejudice": "resolved",
    "dismissal_unspecified_with_prejudice": "resolved",
    "dismissal_voluntary_without_prejudice": "tentative_refile",
    "dismissal_involuntary_without_prejudice": "adverse",
    "dismissal_unspecified_without_prejudice": "tentative_refile",
    "dismissal_voluntary_unspecified_prejudice": "resolved",
    "dismissal_involuntary_unspecified_prejudice": "adverse",
    "dismissal_unspecified_unspecified_prejudice": "neutral",
    "default_judgment": "resolved",
    "judgment_entered": "resolved",
    "settled": "resolved",
    "affirmed": "favorable_to_prevailing_below",
    "reversed": "adverse_to_prevailing_below",
    "reversed_in_part": "mixed_on_appeal",
    "affirmed_in_part_reversed_in_part": "mixed_on_appeal",
    "remanded": "mixed_on_appeal",
    "appeal_dismissed": "favorable_to_prevailing_below",
    "remittitur": "appellate_concluded",
    "notice_of_appeal": "appeal_pending",
    "writ_petition_filed": "writ_pending",
    "writ_denied": "writ_denied",
    "writ_granted": "writ_granted",
    "peremptory_writ_issued": "writ_granted",
    "alternative_writ_issued": "writ_review_proceeding",
    "writ_osc_issued": "writ_review_proceeding",
}


def abstract_valence(signal: str) -> str:
    return _ABSTRACT_VALENCE.get(signal, "neutral")


def case_outcome_signals(docket_entries: Iterable[dict]) -> list[dict[str, Any]]:
    """All outcome signals in one case's docket entries (deduped by signal+date)."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for seq, entry in enumerate(docket_entries):
        if not isinstance(entry, dict):
            continue
        desc = bct.first_text(entry, "description", "RTEXT", "text", "title")
        if not desc:
            continue
        date = bct.first_text(entry, "date_filed", "FILEDATE", "filed", "date")
        for hit in (classify_dismissal(desc), classify_appellate(desc), classify_judgment(desc)):
            if not hit:
                continue
            key = (hit["signal"], date)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "entry_seq": seq, "date_filed": date, "signal": hit["signal"],
                "prejudice": hit.get("prejudice", ""), "voluntariness": hit.get("voluntariness", ""),
                "abstract_valence": abstract_valence(hit["signal"]),
                "matched_text": hit["matched_text"],
            })
    return out


# ===========================================================================
# 2. Motion-type classification for tentative dispositions (broader than the
#    dispositive-only set in index_appeals, so judge stats cover more motions).
# ===========================================================================
MOTION_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("anti_slapp", re.compile(r"anti[-\s]?slapp|ccp\s*425\.16|\b425\.16\b|special\s+motion\s+to\s+strike", re.I)),
    ("summary_judgment", re.compile(r"summary\s+judgment|summary\s+adjudication|\bmsj\b", re.I)),
    ("demurrer", re.compile(r"\bdemurrer\b", re.I)),
    ("judgment_on_pleadings", re.compile(r"judg(?:e?ment|mnt)\s+on\s+the\s+pleadings?", re.I)),
    ("motion_to_strike", re.compile(r"\bmotion\s+to\s+strike\b", re.I)),
    ("motion_to_compel", re.compile(r"\b(?:motion|mtn)\s+to\s+compel\b|compel\s+(?:further\s+)?(?:responses|discovery|deposition)", re.I)),
    ("discovery_sanctions", re.compile(r"\bsanctions?\b", re.I)),
    ("motion_to_quash", re.compile(r"\bquash\b", re.I)),
    ("motion_to_dismiss", re.compile(r"\bmotion\s+to\s+dismiss\b", re.I)),
    ("writ_petition", re.compile(rf"\bpetition\b.{{0,100}}\bwrit\b.{{0,100}}\b(?:{_WRIT_KIND})\b|\bwrit\s+of\s+(?:{_WRIT_KIND})\b", re.I)),
    ("preliminary_injunction", re.compile(r"\bpreliminary\s+injunction\b|\bTRO\b|temporary\s+restraining", re.I)),
    ("attorney_fees", re.compile(r"\battorney\S*\s+fees?\b|\bfees?\s+and\s+costs\b", re.I)),
    ("class_certification", re.compile(r"\bclass\s+cert", re.I)),
    ("motion_to_compel_arbitration", re.compile(r"\bcompel\s+arbitration\b|\barbitrat", re.I)),
    ("new_trial_jnov", re.compile(r"\bnew\s+trial\b|\bjnov\b|judgment\s+notwithstanding", re.I)),
    ("set_aside_default", re.compile(r"\bset\s+aside\b.*\bdefault\b|\bvacate\b.*\bdefault\b|CCP\s*473", re.I)),
    ("petition", re.compile(r"\bpetition\b", re.I)),
    ("demurrer_motion", re.compile(r"\bmotion\b", re.I)),  # generic "motion" fallback
]


def classify_motion_type(calendar_matter: str) -> str:
    """Broad motion-type slug for a tentative's calendar matter, or 'other'."""
    text = bct.clean(calendar_matter)
    if not text:
        return "other"
    for name, pat in MOTION_TYPE_RULES:
        if pat.search(text):
            return "motion" if name == "demurrer_motion" else name
    return "other"


def disposition_family(outcome_label: str) -> str:
    """grant / deny / partial / procedural / other from an index_appeals label."""
    if outcome_label in ("Granted in part", "Denied in part",
                         "Sustained with leave to amend", "Granted (moot)", "Denied (moot)"):
        return "partial"
    if outcome_label in ia._GRANT_LIKE:
        return "grant"
    if outcome_label in ia._DENY_LIKE:
        return "deny"
    if outcome_label in ia._PROCEDURAL:
        return "procedural"
    return "other"


# ===========================================================================
# 3. IO + roll-ups
# ===========================================================================
def _norm(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def _clean_judge(value: Any) -> str:
    """Judge display name, or "" for missing/NaN (pandas str(NaN) == 'nan')."""
    text = bct.clean(value)
    return "" if text.lower() in ("", "nan", "none", "n/a") else text


def log_phase(message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} {message}", flush=True)


def stream_case_outcomes(case_dir: Path, limit: int | None = None, progress_every: int = 0) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    total = bct.count_case_files(case_dir, limit)
    for processed, path in enumerate(bct.iter_case_files(case_dir, limit), 1):
        case = bct.load_case(path)
        case_number = bct.norm_case(case.get("case_number") or path.stem)
        if not case_number:
            continue
        docket_entries = case.get("docket_entries") if isinstance(case.get("docket_entries"), list) else []
        for sig in case_outcome_signals(docket_entries):
            outcomes.append({"case_number": case_number, **sig})
        if progress_every > 0 and (processed % progress_every == 0 or processed == total):
            print(
                "case_outcomes: "
                f"{processed}/{total} cases, "
                f"{len(outcomes)} outcome signals",
                flush=True,
            )
    outcomes.sort(key=lambda r: (r["case_number"], r["entry_seq"], r["signal"]))
    return outcomes


def build_case_outcomes(case_dir: str, limit: int | None = None, progress_every: int = 0):
    """(tables, case_outcomes rows). Reuses build_case_tables for representation."""
    case_path = Path(case_dir)
    tables = bct.rows_from_cases(
        case_path,
        limit,
        progress_every=progress_every,
        include_docket_entries=False,
    )
    log_phase("streaming docket entries for outcome signals")
    outcomes = stream_case_outcomes(case_path, limit, progress_every=progress_every)
    log_phase(f"classified {len(outcomes)} outcome signals")
    return tables, outcomes


def build_tentative_dispositions(tentatives_glob: str):
    """One row per tentative motion disposition (motion_type x disposition x judge)."""
    import pandas as pd
    rows: list[dict[str, Any]] = []
    loaded = 0
    log_phase("loading tentative parquet files")
    for path in glob.iglob(tentatives_glob):
        if "extras" in os.path.basename(path):
            continue
        loaded += 1
        cols = pd.read_parquet(path).columns
        want = [c for c in ("case_number", "case_title", "calendar_matter", "ruling",
                            "ruling_substantive", "judge", "court_date", "department") if c in cols]
        df = pd.read_parquet(path, columns=want)
        for rec in df.to_dict("records"):
            ruling = rec.get("ruling_substantive") or rec.get("ruling")
            disposition = ia.classify_outcome(ruling)
            matter = rec.get("calendar_matter") or rec.get("case_title") or ""
            dept = bct.clean(rec.get("department"))
            rows.append({
                "case_number": _norm(rec.get("case_number")),
                "officer": _clean_judge(rec.get("judge")),
                "department": dept,
                "calendar_context": calendar_context(dept),
                "assignment_regime": assignment_regime(dept),
                "motion_type": classify_motion_type(matter),
                "disposition": disposition,
                "family": disposition_family(disposition),
                "court_date": bct.clean(rec.get("court_date")),
            })
    log_phase(f"loaded {loaded} tentative parquet files")
    rows.sort(key=lambda r: (r["officer"], r["motion_type"], r["disposition"]))
    return rows


# Dept 204 is Probate (DESIGN.md); SF probate tentatives are authored by probate
# EXAMINERS (staff attorneys), so the tentative "judge" field for Dept 204 is the
# examiner, not a bench judge (ingest.py folds the Examiner field into judge).
PROBATE_DEPTS = {"204", "206"}

# SF Superior runs TWO civil assignment regimes, and which one applies decides
# whether a tentative-ruling officer is "the case's judge" or just the decider of
# one order:
#
#   * MASTER CALENDAR (general civil): the case is NOT assigned to one judge. Law
#     & Motion is heard in Depts 301/302 by whoever sits that day; pretrial
#     management of unassigned cases is in Dept 610; the Presiding Judge assigns
#     trials OUT from the Civil Master Calendar (Dept 206) to a trial department.
#     So a tentative here is the LAW-AND-MOTION judge FOR THAT ORDER — not a
#     case-long trial judge.
#   * DIRECT / ALL-PURPOSE calendar: the case stays with one department/officer
#     for all purposes — Complex Civil + Asbestos (Dept 304, which also decides
#     the complex designation), Real Property / unlawful detainer (Dept 501), and
#     Probate (Dept 204, authored by examiners). Here the deciding officer IS,
#     effectively, the case's assigned judge/examiner, so attribution can be
#     case-level rather than per-order.
#
# Sources: SF Superior Court (sf.courts.ca.gov) "Law & Motion and Discovery",
# "Presiding Judge–Master Calendar", "Complex Civil Litigation" (Dept 304),
# "Real Property Court" (Dept 501); Uniform Local Rules of Court; repo DESIGN.md
# (Dept 204 Probate, 301 Discovery, 302 Civil).
CALENDAR_CONTEXT = {
    "204": "probate",
    "206": "master_calendar",
    "301": "law_and_motion",     # discovery / odd case numbers (master calendar)
    "302": "law_and_motion",     # even case numbers (master calendar)
    "304": "complex_civil",      # complex + asbestos (direct / all-purpose)
    "501": "real_property",      # real property / unlawful detainer (dedicated)
    "610": "civil_management",   # pretrial management of unassigned general civil
}

# Departments where the case is assigned to one officer for all purposes, so the
# ruling officer is effectively the case's assigned judge/examiner.
DIRECT_CALENDAR_DEPTS = {"204", "304", "501"}
# Departments that hear motions on a master-calendar basis (per-order officer).
MASTER_LM_DEPTS = {"301", "302"}


def calendar_context(department: Any) -> str:
    return CALENDAR_CONTEXT.get(str(department or "").strip(), "other")


def assignment_regime(department: Any) -> str:
    """How the officer relates to the case for a given department."""
    dept = str(department or "").strip()
    if dept in DIRECT_CALENDAR_DEPTS:
        return "direct_calendar"            # assigned judge/examiner (case-level)
    if dept in MASTER_LM_DEPTS:
        return "master_calendar_law_and_motion"  # per-order officer, not trial judge
    if dept == "206":
        return "master_calendar"
    return "other"


def officer_match_key(name: Any) -> str:
    """Roster/profile comparison key for judicial-officer names."""

    return bct.judge_match_key(name)


def load_roster_judge_keys(path: str = DEFAULT_JUDGES_JSON) -> set[str]:
    """Names that judges.json identifies as judges, excluding commissioners.

    Dept 204 mostly means probate examiner, but historical/current judges can
    also appear there. The roster/code map is the deterministic override for
    those named judges so they do not fall into the broad mixed-officer bucket.
    """

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()

    keys: set[str] = set()

    def add_name(name: Any) -> None:
        if not name or bct.is_pseudo_officer(name):
            return
        key = officer_match_key(name)
        if key:
            keys.add(key)

    code_map = data.get("code_map")
    if isinstance(code_map, dict):
        for raw in code_map.values():
            if isinstance(raw, dict):
                add_name(raw.get("name"))

    roster_rows = data.get("roster")
    if isinstance(roster_rows, list):
        for row in roster_rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "")
            if "comm" in title.lower():
                continue
            parts = [row.get("first"), row.get("middle"), row.get("last")]
            add_name(" ".join(str(p).strip() for p in parts if str(p or "").strip()))

    return keys


def _officer_type(
    name: str,
    departments: Iterable[str],
    roster_judge_keys: set[str] | None = None,
) -> str:
    # Pro tem (temporary) judges are flagged in the name string by ingest.py.
    if str(name or "").strip().lower().startswith("judge pro tem"):
        return "judge_pro_tempore"
    if roster_judge_keys and officer_match_key(name) in roster_judge_keys:
        return "judge_or_commissioner"
    depts = {d for d in departments if d}
    if depts and depts <= PROBATE_DEPTS:
        return "probate_examiner"
    if depts & PROBATE_DEPTS:
        return "mixed_officer"
    return "judge_or_commissioner"


def _rate(grant: int, deny: int) -> float | None:
    total = grant + deny
    return round(grant / total, 4) if total else None


def officer_metrics(
    dispositions: list[dict],
    roster_judge_keys: set[str] | None = None,
) -> dict[str, dict]:
    """Per judicial-officer grant/deny rates overall and by motion type.

    Keyed by the officer's display name. Covers EVERY named tentative author —
    judges, commissioners, and (for Dept 204 Probate) examiners — and tags each
    with an inferred ``officer_type`` and the department(s) they appear in. Blank
    / NaN officer rows are excluded. Per-motion rates need >=3 decided rulings.
    """
    by_officer: dict[str, dict] = {}
    for d in dispositions:
        officer = d.get("officer") or d.get("judge")
        if not officer:
            continue
        om = by_officer.setdefault(officer, {
            "n_tentatives": 0, "overall": Counter(), "by_motion": defaultdict(Counter),
            "departments": Counter(), "contexts": Counter(), "regimes": Counter()})
        om["n_tentatives"] += 1
        om["overall"][d["family"]] += 1
        om["by_motion"][d["motion_type"]][d["family"]] += 1
        if d.get("department"):
            om["departments"][d["department"]] += 1
        om["contexts"][d.get("calendar_context") or calendar_context(d.get("department"))] += 1
        om["regimes"][d.get("assignment_regime") or assignment_regime(d.get("department"))] += 1
    out: dict[str, dict] = {}
    for officer, om in by_officer.items():
        ov = om["overall"]
        motions = {}
        for mt, fam in om["by_motion"].items():
            decided = fam["grant"] + fam["deny"] + fam["partial"]
            if decided < 3:        # don't publish a rate off 1-2 rulings
                continue
            motions[mt] = {
                "n": int(sum(fam.values())),
                "granted": int(fam["grant"]), "denied": int(fam["deny"]),
                "partial": int(fam["partial"]),
                "grant_rate": _rate(fam["grant"], fam["deny"]),
            }
        depts = [d for d, _ in om["departments"].most_common()]
        regimes = [r for r, _ in om["regimes"].most_common()]
        primary_regime = regimes[0] if regimes else "other"
        # In a direct/all-purpose dept the officer IS the case's assigned
        # judge/examiner; on the master-calendar L&M line they only decided that
        # order (not the trial judge).
        ruling_scope = ("assigned_judge_or_examiner_direct_calendar"
                        if primary_regime == "direct_calendar"
                        else "law_and_motion_per_order_not_trial_judge")
        out[officer] = {
            "n_tentatives": om["n_tentatives"],
            "officer_type": _officer_type(officer, depts, roster_judge_keys),
            "departments": depts[:6],
            "calendar_contexts": [c for c, _ in om["contexts"].most_common()],
            "assignment_regimes": regimes,
            "ruling_scope": ruling_scope,
            "granted": int(ov["grant"]), "denied": int(ov["deny"]),
            "partial": int(ov["partial"]), "procedural": int(ov["procedural"]),
            "grant_rate": _rate(ov["grant"], ov["deny"]),
            "by_motion": dict(sorted(motions.items(), key=lambda kv: -kv[1]["n"])),
            "confidence": "inferred-from-tentatives",
        }
    return out


# Side a party appears on (mirrors the viewer's partyBucket): plaintiff-ish vs
# defense-ish. Used to read an outcome from that side's perspective.
_LEFT = re.compile(r"plaintiff|petitioner|claimant|applicant|appellant|cross-?complainant", re.I)
_RIGHT = re.compile(r"defendant|respondent|debtor|cross-?defendant|appellee", re.I)


def _party_side(party_type: str) -> str:
    t = party_type or ""
    if _LEFT.search(t):
        return "plaintiff"
    if _RIGHT.search(t):
        return "defense"
    return "other"


# Coarse, side-aware favorability of an abstract case valence. Documented as a
# low-confidence heuristic: a clean resolution counts mildly favorable for the
# defense (claims end) and neutral for the plaintiff (could be win or give-up);
# an appellate reversal is adverse to whoever prevailed below — which we cannot
# attribute per-attorney, so it is left out of favorable_rate and reported raw.
def _favorability(valence: str, side: str) -> str | None:
    if valence == "resolved":
        return "favorable" if side == "defense" else "neutral"
    if valence == "tentative_refile":
        return "neutral"
    if valence == "adverse":
        return "unfavorable" if side == "plaintiff" else "neutral"
    return None


APPELLATE_SIGNALS = {
    "affirmed",
    "reversed",
    "reversed_in_part",
    "remanded",
    "remittitur",
    "notice_of_appeal",
    "appeal_dismissed",
    "affirmed_in_part_reversed_in_part",
    "writ_petition_filed",
    "writ_denied",
    "writ_granted",
    "peremptory_writ_issued",
    "alternative_writ_issued",
    "writ_osc_issued",
}


def rollup_outcomes_by_case(
    outcomes: Iterable[dict],
) -> tuple[dict[str, Counter], dict[str, Counter], dict[str, Counter]]:
    signal_by_case: dict[str, Counter] = defaultdict(Counter)
    valence_by_case: dict[str, Counter] = defaultdict(Counter)
    appellate_by_case: dict[str, Counter] = defaultdict(Counter)
    for o in outcomes:
        case_number = o["case_number"]
        signal = o["signal"]
        valence = o["abstract_valence"]
        signal_by_case[case_number][signal] += 1
        valence_by_case[case_number][valence] += 1
        if valence.endswith("below") or signal in APPELLATE_SIGNALS:
            appellate_by_case[case_number][signal] += 1
    return signal_by_case, valence_by_case, appellate_by_case


def attorney_metrics(tables: dict, outcomes: list[dict]) -> dict[str, dict]:
    """Per-attorney outcome distribution + side split + coarse favorable_rate."""
    signal_by_case, valence_by_case, appellate_by_case = rollup_outcomes_by_case(outcomes)
    # attorney_id -> {name, sides per case, outcome tallies}
    acc: dict[str, dict] = {}
    # representation rows carry case_number, attorney_id, attorney_name, party_type
    seen_case_atty: set[tuple[str, str]] = set()
    for rep in tables["representation"]:
        aid = rep.get("attorney_id") or rep.get("attorney_name")
        if not aid:
            continue
        a = acc.setdefault(aid, {
            "attorney_id": rep.get("attorney_id", ""), "name": rep.get("attorney_name", ""),
            "cases": set(), "side": Counter(), "outcomes": Counter(),
            "appellate": Counter(), "fav": Counter()})
        a["cases"].add(rep["case_number"])
        side = _party_side(rep.get("party_type", ""))
        ck = (aid, rep["case_number"])
        if ck not in seen_case_atty:
            seen_case_atty.add(ck)
            a["side"][side] += 1
            for signal, count in signal_by_case.get(rep["case_number"], Counter()).items():
                a["outcomes"][signal] += count
            for signal, count in appellate_by_case.get(rep["case_number"], Counter()).items():
                a["appellate"][signal] += count
            for valence, count in valence_by_case.get(rep["case_number"], Counter()).items():
                fav = _favorability(valence, side)
                if fav:
                    a["fav"][fav] += count
    out: dict[str, dict] = {}
    for aid, a in acc.items():
        fav, unfav = a["fav"]["favorable"], a["fav"]["unfavorable"]
        out[aid] = {
            "attorney_id": a["attorney_id"], "name": a["name"],
            "case_count": len(a["cases"]),
            "side": dict(a["side"]),
            "outcomes": dict(a["outcomes"]),
            "appellate": dict(a["appellate"]),
            "favorable": fav, "unfavorable": unfav,
            "favorable_rate": _rate(fav, unfav),
            "confidence": "inferred-fuzzy",
        }
    return out


def resolve_litigant_shard_path(path: str, manifest_path: str) -> str:
    if os.path.isabs(path):
        return path
    repo_path = os.path.join(REPO, path)
    if os.path.exists(repo_path):
        return repo_path
    return os.path.join(os.path.dirname(manifest_path), path)


def load_litigant_json_records(litigants_json: str) -> list[dict]:
    if not os.path.exists(litigants_json):
        return []
    try:
        with open(litigants_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    rows = [row for row in data.get("litigants", []) if isinstance(row, dict)]
    for shard in data.get("shards", []):
        shard_path = shard if isinstance(shard, str) else shard.get("path") if isinstance(shard, dict) else ""
        if not shard_path:
            continue
        try:
            with open(resolve_litigant_shard_path(shard_path, litigants_json), encoding="utf-8") as fh:
                shard_data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(shard_data, list):
            rows.extend(row for row in shard_data if isinstance(row, dict))
        elif isinstance(shard_data, dict):
            rows.extend(row for row in shard_data.get("litigants", []) if isinstance(row, dict))
    return rows


def litigant_metrics(outcomes: list[dict], litigants_json: str) -> dict[str, dict]:
    """Per-litigant outcome distribution, only if data/litigants.json exists."""
    litigants = load_litigant_json_records(litigants_json)
    if not litigants:
        return {}
    signal_by_case, _, _ = rollup_outcomes_by_case(outcomes)
    out: dict[str, dict] = {}
    for lit in litigants:
        lid = lit.get("litigant_id")
        cases = [_norm(c) for c in (lit.get("case_numbers") or [])]
        if not lid or not cases:
            continue
        tally: Counter = Counter()
        for cn in cases:
            tally.update(signal_by_case.get(cn, Counter()))
        if tally:
            out[lid] = {"case_count": len(cases), "outcomes": dict(tally),
                        "confidence": "inferred-fuzzy"}
    return out


def profile_metrics_shard_path(out_dir: str, kind: str, shard_index: int):
    from pathlib import Path
    return Path(out_dir) / f"profile-metrics-{kind}-{shard_index:03d}.json"


def cleanup_profile_metrics_outputs(out_dir: str) -> None:
    from pathlib import Path
    out = Path(out_dir)
    (out / "profile-metrics.json").unlink(missing_ok=True)
    (out / "profile-metrics-manifest.json").unlink(missing_ok=True)
    for path in out.glob("profile-metrics-*.json"):
        path.unlink(missing_ok=True)


def shard_metric_records(records: dict[str, Any], max_bytes: int) -> list[dict[str, Any]]:
    shards: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    current_size = 2
    for key in sorted(records):
        item_size = len(json.dumps({key: records[key]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) - 2
        separator_size = 1 if current else 0
        if current and current_size + separator_size + item_size > max_bytes:
            shards.append(current)
            current = {}
            current_size = 2
            separator_size = 0
        current[key] = records[key]
        current_size += separator_size + item_size
    if current:
        shards.append(current)
    return shards


def write_profile_metrics_atomic(out_dir: str, payload: dict[str, Any]) -> None:
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cleanup_profile_metrics_outputs(out_dir)
    manifest = {
        "schema_version": 2,
        "source_schema_version": payload.get("schema_version", 1),
        "generated_at": payload.get("generated_at", ""),
        "note": payload.get("note", ""),
        "kinds": {},
    }
    for kind in PROFILE_METRICS_KINDS:
        records = payload.get(kind) if isinstance(payload.get(kind), dict) else {}
        shards = []
        for shard_index, shard_records in enumerate(shard_metric_records(records, PROFILE_METRICS_SHARD_BYTES)):
            shard_path = profile_metrics_shard_path(out_dir, kind, shard_index)
            bct.write_json_atomic(shard_path, {
                "schema_version": 2,
                "kind": kind,
                "generated_at": payload.get("generated_at", ""),
                "records": shard_records,
            })
            shards.append({
                "path": shard_path.name,
                "count": len(shard_records),
                "bytes": shard_path.stat().st_size,
            })
        manifest["kinds"][kind] = {"count": len(records), "shards": shards}
    bct.write_json_atomic(out / "profile-metrics-manifest.json", manifest)
    bct.write_json_atomic(out / "profile-metrics.json", {
        "schema_version": 2,
        "manifest": "profile-metrics-manifest.json",
        "generated_at": payload.get("generated_at", ""),
        "note": payload.get("note", ""),
    })


def write_outputs(out_dir: str, case_outcomes: list[dict], dispositions: list[dict],
                  profile_metrics: dict) -> None:
    import pandas as pd
    from pathlib import Path
    log_phase("writing case_outcomes.parquet")
    bct.write_parquet_atomic(Path(out_dir) / "case_outcomes.parquet", pd.DataFrame(case_outcomes))
    log_phase("writing tentative_dispositions.parquet")
    bct.write_parquet_atomic(Path(out_dir) / "tentative_dispositions.parquet", pd.DataFrame(dispositions))
    payload = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": ("Inferred, fuzzy second/third-order metrics. NOT court findings. "
                 "judicial_officers grant rates come from tentative rulings. SF "
                 "civil runs two regimes (see assignment_regime): on the "
                 "MASTER-CALENDAR law-and-motion line (Depts 301/302) the officer "
                 "decided only THAT order and is not the case's trial judge (trials "
                 "assign out from Dept 206); in DIRECT/all-purpose departments "
                 "(Complex 304, Real Property 501, Probate 204) the officer is the "
                 "case's assigned judge/examiner. Covers judges, commissioners, "
                 "probate examiners, and pro tem judges. attorney/litigant metrics "
                 "are heuristic roll-ups of docket-derived case outcomes."),
        **profile_metrics,
    }
    log_phase("writing profile metrics shards")
    write_profile_metrics_atomic(out_dir, payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case-dir", default=DEFAULT_CASE_DIR)
    ap.add_argument("--tentatives-glob", default=DEFAULT_TENTATIVES_GLOB)
    ap.add_argument("--litigants-json", default=DEFAULT_LITIGANTS_JSON)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--judges-json", default=DEFAULT_JUDGES_JSON)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--progress-every", type=int, default=0,
                    help="Print case parsing progress after this many case JSON files.")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    log_phase("building case outcomes from docket text")
    tables, case_outcomes = build_case_outcomes(args.case_dir, args.limit, max(0, args.progress_every))
    log_phase(f"case outcome scan complete: {len(case_outcomes)} signals across {len(tables['cases'])} cases")

    log_phase("classifying tentative motion dispositions")
    dispositions = build_tentative_dispositions(args.tentatives_glob)
    log_phase(f"tentative disposition scan complete: {len(dispositions)} dispositions")

    log_phase("loading judicial roster")
    roster_judge_keys = load_roster_judge_keys(args.judges_json)
    log_phase("building judicial officer metrics")
    om = officer_metrics(dispositions, roster_judge_keys)
    log_phase("building attorney metrics")
    am = attorney_metrics(tables, case_outcomes)
    log_phase("building litigant metrics")
    lm = litigant_metrics(case_outcomes, args.litigants_json)
    n_exam = sum(1 for v in om.values() if v["officer_type"] == "probate_examiner")
    log_phase(
        f"profile metric rollups complete: judicial_officers={len(om)} "
        f"(probate examiners={n_exam}) attorneys={len(am)} litigants={len(lm)}"
    )

    profile_metrics = {"judicial_officers": om, "attorneys": am, "litigants": lm}
    if not args.no_write:
        write_outputs(args.out_dir, case_outcomes, dispositions, profile_metrics)
        log_phase("wrote case_outcomes.parquet, tentative_dispositions.parquet, profile-metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
