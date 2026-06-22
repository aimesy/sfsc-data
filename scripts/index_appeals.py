#!/usr/bin/env python3
"""Prototype: index SFSC trial cases by appealability and link to appeals.

WHAT THIS DOES
==============
This is a PROTOTYPE that takes the San Francisco Superior Court (SFSC)
tentative-rulings archive (``tentatives.parquet`` / ``data/tentatives-<dept>.parquet``)
and:

  (a) reads the parquet(s);
  (b) classifies each motion row by APPEALABILITY -- whether an order on that
      motion, if entered, would be directly appealable under California law,
      separately from whether the motion is dispositive of the case on the
      merits. The four buckets and their statutory bases are described in the
      APPEALABILITY TAXONOMY section below;
  (c) flags rows whose motion is *dispositive* (could end the case on the
      merits: summary judgment/adjudication, demurrer, judgment on the
      pleadings, anti-SLAPP / CCP 425.16, motion to dismiss) AND rows whose
      order would be *interlocutory-appealable* even though non-dispositive
      (injunctions, receivers, new trial/JNOV, anti-SLAPP denial, denial of a
      petition to compel arbitration, class-cert "death knell", and the large
      family of directly-appealable Probate Code orders that dominate Dept 204);
  (d) groups by ``case_number`` into a per-case record (parties, department,
      motion outcomes + appealability, hearing-date range);
  (e) defines a clean schema (``AppealRecord``) for an appellate-outcome index
      and a documented stub ``lookup_appeal(case)`` that explains how each
      researched data source *would* be queried. Live network calls are gated
      behind ``--online`` and marked TODO; nothing here hammers a live site.

Run the offline candidate finder:

    python scripts/index_appeals.py --dry-run
    python scripts/index_appeals.py --dry-run --appealability interlocutory_appealable
    python scripts/index_appeals.py --dry-run --department 204 --appealability interlocutory_appealable
    python scripts/index_appeals.py --dry-run --appeal-track appealable_now
    python scripts/index_appeals.py --dry-run --department 302 --appeal-track appealable_now

Dependencies: pandas + Python stdlib only.


TWO AXES: appealability (TYPE) vs appeal_track (OUTCOME-CONDITIONED)
===================================================================
This file now classifies each motion row on TWO distinct axes:

  ``appealability`` (the original axis) is a function of MOTION TYPE: "is an
  order on this KIND of motion one that California law makes directly
  appealable?" -> dispositive / interlocutory_appealable / writ_only / unknown.
  That is legally INCOMPLETE on its own, because appealability turns on the
  OUTCOME too (a denied MSJ yields no judgment; an overruled demurrer ends
  nothing).

  ``appeal_track`` (the axis added here) is a function of
  (MOTION TYPE x OUTCOME x FULL-vs-PARTIAL): "given how this motion was actually
  ruled, does the disposition open a direct appeal NOW, only by writ, or not
  yet?" -> appealable_now / writ_only / not_yet / unknown. The outcome is read
  from the SUBSTANTIVE ruling text (``ruling_substantive``; the per-department
  parquets store it in ``ruling``). See compute_appeal_track() for the rule
  table and the controlling tentative-vs-entered-order caveat.

Both fields are emitted per row; the original ``appealability`` is unchanged.


APPEALABILITY TAXONOMY (the new dimension this prototype adds)
==============================================================
"Dispositive" answers "could this motion end the case on the merits?".
"Appealable" answers a *different* question: "is an order on this motion one
that California law lets a party appeal directly (now), versus one reviewable
only by extraordinary writ, versus one that must wait for final judgment?".
The two overlap (a granted MSJ is both dispositive and -- once reduced to
judgment -- appealable) but are not the same axis; many non-dispositive orders
are nonetheless *immediately* appealable, and those were previously missed.

Each motion row is tagged ``appealability`` with one of:

  dispositive
      The motion can resolve the case/claim on the merits; the resulting
      judgment is appealable as a final judgment (CCP 904.1(a)(1)). Includes
      MSJ/MSA, demurrer / judgment on the pleadings (when sustained/granted
      without leave), motion to dismiss, and anti-SLAPP *grant*. NB anti-SLAPP
      is special: see interlocutory_appealable for its *denial*.

  interlocutory_appealable
      A NON-final order that California law makes directly appealable anyway.
      Statutory bases carried per matcher (see INTERLOCUTORY_APPEALABLE_RULES):
        * CCP 904.1(a)(2)  order after an appealable judgment
        * CCP 904.1(a)(3)  order quashing service / staying for forum non
                           conveniens / dismissing under CCP 581d
        * CCP 904.1(a)(4)  order granting a new trial OR denying JNOV
        * CCP 904.1(a)(5)  attachment / right-to-attach order
        * CCP 904.1(a)(6)  order granting/dissolving/refusing an injunction
        * CCP 904.1(a)(7)  order appointing a receiver
        * CCP 904.1(a)(8)/(9) interlocutory judgments re redemption / partition
        * CCP 904.1(a)(11)/(12) order directing monetary sanctions > $5,000
                           (amount-dependent -- see the sanctions caveat)
        * CCP 904.1(a)(13) order granting OR denying an anti-SLAPP special
                           motion to strike (Sections 425.16, 425.19);
                           also stated in CCP 425.16(i)
        * CCP 1294(a)      order dismissing or denying a petition to COMPEL
                           arbitration (and orders confirming/correcting/
                           vacating/dismissing-the-petition re an award)
        * "death knell"    order denying class certification / otherwise
                           ending all class claims while individual claims
                           survive (judicially created; Daar v. Yellow Cab
                           (1967) 67 Cal.2d 695; In re Baycol (2011) 51 Cal.4th
                           751). Caption-only; we cannot see from the caption
                           whether cert was DENIED, so this is a candidate flag.
        * Probate Code 1300/1301/1301.5/1302/1303/1304 -- the large family of
          directly appealable probate orders that dominates Dept 204:
            - PC 1300(b)        settling a fiduciary's ACCOUNT
            - PC 1300(a)        sale/lease/encumbrance/exchange of property
            - PC 1300(c)        instructing / approving a fiduciary's acts
            - PC 1300(d)/(e)/(f) payment of a claim / fees / compensation
            - PC 1300(g)        surcharging / REMOVING / discharging a fiduciary
            - PC 1300(i)        allowing/denying a fiduciary's resignation
            - PC 1303(a)        granting/revoking letters (personal rep)
            - PC 1303(b)        admitting a will to probate / revoking probate
            - PC 1303(f)        determining heirship / succession
            - PC 1303(g)        directing DISTRIBUTION of estate property
            - PC 1303(h)        spousal / surviving-spouse property
            - PC 1301           granting/revoking guardianship/conservatorship,
                                and related conservatorship orders
            - PC 1304           final orders on a trust under PC 17200
                                (EXCEPT an order merely compelling an account
                                 or accepting a trustee's resignation)

  writ_only
      Not directly appealable; reviewable (if at all) only by extraordinary
      writ before judgment -- flag for the SEPARATE writs track (this script
      does NOT build that track). Includes: discovery orders (compel / quash /
      protective order / sanctions-as-discovery), venue (CCP 400), attorney
      DISQUALIFICATION, and -- importantly -- an order COMPELLING arbitration
      (CCP 1294 makes only the *denial* appealable; a grant is writ-only).

  unknown
      Caption doesn't map to a known appealability rule, OR appealability is
      contingent on a fact the caption/tentative can't reveal (e.g. a sanctions
      order is appealable only if it directs payment of MORE THAN $5,000 --
      CCP 904.1(a)(11)/(12) -- and the dollar amount is not in the caption).

CAVEATS specific to appealability (read before trusting a bucket)
  - A *tentative ruling* is not an entered order, and an entered order is not a
    judgment. Appealability attaches to the ENTERED order/judgment; a tentative
    only tells us the motion type (and a likely outcome). Treat every tag as
    "would be appealable IF an order of this kind is entered".
  - Outcome conditions some tags. Anti-SLAPP: a GRANT is dispositive (final
    judgment route); a DENIAL is interlocutory_appealable under 904.1(a)(13).
    Petition to compel arbitration: only DENIAL/dismissal is appealable (1294);
    a GRANT is writ_only. New trial: only GRANTING is appealable; JNOV only
    DENYING. We use the parsed outcome to refine the tag where the caption
    alone is ambiguous, and fall back to the caption-default otherwise.
  - Sanctions are the messiest: appealable only if > $5,000 and not a discovery
    sanction folded into a compel order. We tag standalone sanctions ``unknown``
    (amount-contingent) and route discovery-sanctions captions to writ_only.
  - The class-action "death knell" tag is caption-only and over-inclusive: most
    "class action" captions in the corpus are settlement-approval hearings, not
    cert *denials*. We only flag captions that read as a certification motion;
    confirm the DENIAL outcome before relying on it.


LINKAGE STRATEGY (the hard part)
================================
There is NO reliable foreign key from a trial-court case (SFSC numbers look
like ``CGC16556148``) to its appeal. When a case is appealed from San Francisco
Superior Court it goes to the **First Appellate District** of the California
Court of Appeal, where it receives a brand-new docket number of the form
``A`` + six digits (e.g. ``A123456``). The Supreme Court uses ``S######``.

So linkage is a *record-linkage / entity-resolution* problem, not a join:

  1. PARTY NAMES + COUNTY + DATE WINDOW.  Normalize the SFSC ``case_title``
     ("AMITABHO CHATTOPADHYAY VS. MICHAEL ROWELL ET AL") into plaintiff /
     defendant surnames, then search an appellate source for matching party
     names within First Appellate District (San Francisco is one of its
     counties) and within a plausible time window AFTER the dispositive ruling
     (appeals are filed within ~60 days of entry of judgment; an opinion
     issues 1-3 years later). This is the primary, always-available method.

  2. TRIAL-COURT CASE NUMBER PRINTED IN THE APPELLATE RECORD.  The California
     Appellate Courts Case Information System (ACCIS) and many opinions print
     the originating "Superior Court No." This lets you *search by* or
     *confirm against* the trial number when present -- the strongest signal
     when available, but it is not present in every record and is not exposed
     as a queryable field in third-party APIs.

  3. CITATION / CAPTION CONFIRMATION.  Once a candidate appeal is found, the
     opinion text usually recites the trial judge and case caption; matching
     those raises confidence.

FALSE-MATCH RISKS
  - Common-name collisions (e.g. "People v. Smith", banks, insurers, cities
    that appear in thousands of cases).
  - Case captions legitimately drift through litigation (parties added /
    dismissed), so an exact title match is too strict and a single-token
    match is too loose -- the SFSC ingest pipeline already uses a "share one
    substantive token" heuristic for the same reason.
  - Many dispositive *tentative* rulings never become an appealable judgment:
    leave to amend is granted, the case settles, or the order is interlocutory
    (a denied MSJ, a sustained-with-leave demurrer, an MSA granted on some
    claims only). A tentative ruling alone does NOT tell you whether a final,
    appealable judgment was entered, let alone appealed. Treat every link as a
    *candidate* requiring confirmation, never an assertion.


DATA SOURCES (researched; see module-level constant ``SOURCES``)
================================================================
  - ACCIS (appellatecases.courtinfo.ca.gov): authoritative dockets for Supreme
    Court + Courts of Appeal; *can search by trial-court case number*, party,
    caption, attorney, or calendar date. No documented public API -- it is a
    ColdFusion web form (``request_token`` per session). Hourly updates.
    Programmatic use = HTML scraping, which sits in a legal grey area and
    against the spirit of a government access portal; throttle hard, identify
    your client, and prefer it for *confirmation* of a candidate rather than
    bulk discovery.
  - CourtListener / Free Law Project REST API v4 (courtlistener.com/api/rest/v4):
    documented JSON API, token auth. Default limits ~5 req/min, 50/hr, 125/day
    (membership/commercial tiers raise this). Quarterly bulk CSV dumps for
    large jobs. Search + opinion-cluster + docket + parties endpoints; dockets
    carry ``docket_number`` / ``docket_number_core``. RECOMMENDED primary
    programmatic source (clear ToS, real API).
  - courts.ca.gov/opinions: official slip (last 120 days) + unpublished (last
    60 days) opinion PDFs; older opinions via ACCIS. Good for fetching opinion
    text/URL once a case number is known; not a structured search API.
  - Justia (law.justia.com/cases/california/court-of-appeal): free full text,
    no public API; ToS restricts automated scraping.
  - Google Scholar (scholar.google.com): broad coverage, NO API and ToS
    forbids automated querying -- unsuitable for a pipeline.


TERMS-OF-SERVICE CAVEAT (read before flipping --online)
=======================================================
Only CourtListener offers a sanctioned API with explicit, documented rate
limits; use it first and respect its limits and token requirement. ACCIS and
courts.ca.gov are government portals without a documented API -- scrape only
sparingly, with a descriptive User-Agent, generous delays, and a cache, and
prefer them for confirming a single candidate. Justia and Google Scholar
prohibit automated access; do not script them. This prototype performs NO
network I/O unless ``--online`` is passed, and even then the per-source
fetchers are unimplemented stubs marked TODO.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

try:
    import pandas as pd
except ImportError:  # pragma: no cover - dependency hint
    sys.exit("This script requires pandas (pip install pandas pyarrow).")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
CANONICAL_PARQUET = os.path.join(REPO_ROOT, "tentatives.parquet")


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Dispositive-motion detection
#
# Ported from index.html's CIVIL_LAW_MOTION_RULES (search OUTCOME_RULES /
# lookupMotion there). "Dispositive" per index.html's MOTION_CATEGORY are
# motions that resolve a claim or the whole case on the merits (or a
# strike-equivalent like anti-SLAPP). We treat these motion types as
# dispositive: Summary Judgment/Adjudication, Anti-SLAPP, Demurrer, Judgment
# on the Pleadings, and Motion to Dismiss.
#
# NB: Demurrer and JOP are categorized "Pleadings" (not "Dispositive") in
# index.html because they are only case-ending when sustained/granted WITHOUT
# leave to amend. We still flag them here -- a dispositive *candidate* index
# wants them -- but the outcome text is what determines whether the case
# actually ended (see DISPOSITIVE_OUTCOME below).
# --------------------------------------------------------------------------- #
DISPOSITIVE_MOTION_RULES: list[tuple[str, re.Pattern]] = [
    ("Special Motion to Strike (CCP 425.16/Anti-SLAPP)",
     re.compile(r"anti[-\s]?slapp|ccp\s*425\.16|\b425\.16\b|special\s+motion\s+to\s+strike", re.I)),
    ("Summary Judgment / Adjudication",
     re.compile(r"summary\s+judgment|summary\s+adjudication|\bmsj\b", re.I)),
    ("Demurrer",
     re.compile(r"\bdemurrer\b", re.I)),
    ("Judgment on the Pleadings",
     re.compile(r"judg(?:e?ment|mnt)\s+on\s+the\s+pleadings?", re.I)),
    ("Motion to Dismiss",
     re.compile(r"\bmotion\s+to\s+dismiss\b|\bdismiss(?:al)?\b", re.I)),
]

# Motion types whose *granting/sustaining* actually disposes of the case on
# the merits. (MSJ granted, anti-SLAPP granted, demurrer/JOP sustained without
# leave, dismissal with prejudice.) Used only to annotate confidence -- never
# to assert that an appeal exists.
TRULY_CASE_ENDING = {
    "Special Motion to Strike (CCP 425.16/Anti-SLAPP)",
    "Summary Judgment / Adjudication",
}


def classify_dispositive_motion(calendar_matter: Optional[str]) -> Optional[str]:
    """Return the dispositive motion-type name for a caption, else None."""
    if not calendar_matter:
        return None
    text = str(calendar_matter)
    for name, pat in DISPOSITIVE_MOTION_RULES:
        if pat.search(text):
            return name
    return None


# --------------------------------------------------------------------------- #
# Outcome detection -- a faithful port of index.html's OUTCOME_RULES (search
# ``const OUTCOME_RULES`` / ``classifyOutcome`` there). index.html returns an
# {outcome, subtype} pair; here we collapse to the single most specific label
# (the subtype where one exists, else the outcome) because the appeal_track
# rules below key on those specific dispositions ("Sustained without leave",
# "Granted in part", "Overruled", "Denied in part", ...).
#
# IMPORTANT: this MUST run against the SUBSTANTIVE ruling text
# (``ruling_substantive`` -- the merits half of the tentative, with the admin /
# courtcall boilerplate stripped). The per-department parquets already store the
# substantive text in ``ruling``; the canonical parquet keeps it in a separate
# ``ruling_substantive`` column. The loader normalizes both to ``ruling`` so
# this classifier always sees the substantive ruling (see _load_frames).
#
# Order matters and mirrors index.html exactly: procedural / non-merits
# dispositions first, then the granted-family (in-part > with-leave > without-
# leave > plain), then the denied-family, then a moot fallback. "In part" and
# "moot" qualifiers are tested against the FIRST SENTENCE only, as in index.html
# (``firstSentence``), so a later sentence that says "in part" doesn't override
# a clean grant/denial in the opening disposition.
# --------------------------------------------------------------------------- #
def _first_sentence(t: str) -> str:
    """Port of index.html firstSentence(): text up to the first . ! or ?."""
    m = re.match(r"[^.!?]*[.!?]", t)
    return m.group(0) if m else t


# (test, label). ``fs=True`` => the pattern is tested against the first
# sentence only (matches index.html's firstSentence() usage). First hit wins.
_OUTCOME_RULES: list[tuple[re.Pattern, str, bool]] = [
    # --- procedural / non-merits dispositions (no operative order) ---------- #
    (re.compile(r"\boff\s+calendar\b", re.I), "Off calendar", False),
    (re.compile(r"\bwithdrawn\b|\btaken\s+off\s+calendar\b", re.I), "Withdrawn", False),
    (re.compile(r"\btransferr?ed\b", re.I), "Transferred", False),
    (re.compile(r"\bcontinued\b|\bcontinuance\s+granted\b", re.I), "Continued", False),
    (re.compile(r"\bunder\s+submission\b|\bsubmitted\s+for\s+(?:decision|ruling)\b", re.I),
     "Under submission", False),
    (re.compile(r"\bhearing\s+required\b|\bappearance(?:s)?\s+required\b|"
                r"\bparties\s+are\s+to\s+appear\b|\bparties\s+(?:must|shall)\s+appear\b", re.I),
     "Hearing required", False),
    # --- granted family (most specific first) ------------------------------- #
    (re.compile(r"\bgranted\b.{0,40}\bin\s+part\b", re.I), "Granted in part", True),
    (re.compile(r"\bsustained\b.{0,40}\bwith\s+leave\s+to\s+amend\b", re.I),
     "Sustained with leave to amend", False),
    (re.compile(r"\bsustained\b.{0,40}\bwithout\s+leave\b", re.I),
     "Sustained without leave", False),
    (re.compile(r"\bsustained\b", re.I), "Sustained", False),
    (re.compile(r"\bgranted\b.{0,60}\b(?:as|because\s+it\s+is|because\s+it'?s)\s+moot\b", re.I),
     "Granted (moot)", True),
    (re.compile(r"\bgranted\b", re.I), "Granted", False),
    # --- denied family ------------------------------------------------------ #
    (re.compile(r"\bdenied\b.{0,40}\bin\s+part\b", re.I), "Denied in part", True),
    (re.compile(r"\bdenied\s+without\s+prejudice\b", re.I), "Denied without prejudice", False),
    (re.compile(r"\bdenied\b.{0,60}\b(?:as|because\s+it\s+is|because\s+it'?s)\s+moot\b", re.I),
     "Denied (moot)", True),
    (re.compile(r"\bdenied\b", re.I), "Denied", False),
    (re.compile(r"\boverruled\b", re.I), "Overruled", False),
    # --- moot fallback ------------------------------------------------------ #
    (re.compile(r"\bmoot(?:ed)?\b", re.I), "Moot", False),
]

# Coarse buckets used by the appeal_track logic. These let a rule say "any
# grant-like disposition" without re-listing every subtype.
_GRANT_LIKE = {"Granted", "Granted in part", "Granted (moot)",
               "Sustained", "Sustained with leave to amend",
               "Sustained without leave"}
_DENY_LIKE = {"Denied", "Denied in part", "Denied without prejudice",
              "Denied (moot)", "Overruled"}
# Procedural outcomes => no operative order was made; appeal_track is not_yet.
_PROCEDURAL = {"Continued", "Off calendar", "Hearing required",
               "Under submission", "Withdrawn", "Transferred", "Moot"}


def classify_outcome(ruling_text: Optional[str]) -> str:
    """Return the most specific index.html disposition label for a ruling.

    Mirrors index.html classifyOutcome(): returns the subtype where one exists
    (e.g. "Sustained without leave", "Granted in part"), else the umbrella
    outcome (e.g. "Granted", "Denied"). "Unknown" for empty text, "Other" for
    text that matches no rule.
    """
    if ruling_text is None:
        return "Unknown"
    try:
        if pd.isna(ruling_text):
            return "Unknown"
    except (TypeError, ValueError):
        pass
    if not ruling_text:
        return "Unknown"
    text = str(ruling_text)
    fs = _first_sentence(text)
    for pat, outcome, first_sentence_only in _OUTCOME_RULES:
        if pat.search(fs if first_sentence_only else text):
            return outcome
    return "Other"


# --------------------------------------------------------------------------- #
# Appealability classification
#
# The new dimension. We answer "is an order on this motion directly appealable
# under California law?" -- a separate axis from "dispositive" (see the module
# docstring's APPEALABILITY TAXONOMY). Each value carries a short statutory
# ``basis`` string so a downstream index can cite WHY a row is flagged.
#
# Design notes:
#   * These are CAPTION matchers. A tentative ruling is not an entered order;
#     appealability attaches to the entered order/judgment. Read every tag as
#     "would be appealable IF an order of this kind is entered".
#   * Order matters. We run the most specific / strongest rules first so a
#     caption that matches several (e.g. "Petition ... Appoint Receiver To Sell
#     Property") lands on the most precise appealability basis.
#   * Outcome can REFINE the tag (anti-SLAPP grant vs denial; arbitration
#     compel grant vs denial). The caption matcher sets a DEFAULT; the
#     refinement step below adjusts using the parsed outcome where the caption
#     alone is ambiguous.
# --------------------------------------------------------------------------- #

# The four appealability buckets.
APPEALABILITY_VALUES = (
    "dispositive",
    "interlocutory_appealable",
    "writ_only",
    "unknown",
)


@dataclass(frozen=True)
class AppealabilityTag:
    """An appealability classification plus its statutory basis."""
    appealability: str          # one of APPEALABILITY_VALUES
    basis: str                  # short citation, e.g. "CCP 904.1(a)(6)"
    label: str                  # human label, e.g. "Injunction order"

    def __post_init__(self):
        if self.appealability not in APPEALABILITY_VALUES:
            raise ValueError(
                f"appealability must be one of {APPEALABILITY_VALUES}")


# ---- DISPOSITIVE caption rules (map onto the existing dispositive detector) ---
# Kept as appealability tags so a single classifier yields one taxonomy value
# per row. The resulting JUDGMENT is appealable under CCP 904.1(a)(1).
_DISPOSITIVE_TAG = AppealabilityTag(
    "dispositive", "CCP 904.1(a)(1)", "Final-judgment route (dispositive motion)")


# ---- INTERLOCUTORY-APPEALABLE caption rules --------------------------------- #
# (pattern, AppealabilityTag). First match wins. Probate rules come FIRST
# because Dept 204 captions are unambiguous and dominate the corpus; the civil
# CCP 904.1 / 1294 rules follow.
INTERLOCUTORY_APPEALABLE_RULES: list[tuple[re.Pattern, AppealabilityTag]] = [
    # ---- Probate Code directly-appealable orders (Dept 204) ----------------- #
    # PC 1303(a): granting/revoking letters to a personal representative.
    (re.compile(r"\bletters\s+(?:of\s+administration|testamentary)\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1303(a)",
                      "Letters to personal representative")),
    # PC 1303(b): admitting a will to probate / revoking probate of a will.
    (re.compile(r"\bprobate\s+of\s+(?:the\s+)?will\b|\b(?:admit|revok\w+)\b[\s\S]{0,30}\bwill\s+to\s+probate\b|\bwill\s+contest\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1303(b)",
                      "Probate of will / will contest")),
    # PC 1300(b): settling a fiduciary's ACCOUNT (the huge accounting family).
    (re.compile(r"\baccount(?:ing)?\s+petition\b|\bsettl\w+\s+(?:of\s+)?(?:the\s+)?account\b|\bfirst\s+account\b|\bfinal\s+account(?:ing)?\b|\b(?:future|annual)\s+accounting\b|\bfiling\s+of\s+(?:first|final)\s+account\b|\bprobate\s+accounting\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1300(b)",
                      "Settling fiduciary's account")),
    # PC 1303(g): directing DISTRIBUTION of estate property; PC 1303(f):
    # determining heirship / succession.
    (re.compile(r"\bfinal\s+distribution\b|\bpetition\s+for\s+(?:order\s+)?distribution\b|\bdetermine\s+succession\b|\bsuccession\s+to\s+(?:real\s+)?property\b|\bdetermin\w+\s+(?:of\s+)?(?:heirship|entitlement|distribution)\b|\bdetermine\s+heir", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1303(f)/(g)",
                      "Distribution / heirship / succession")),
    # PC 1303(h): spousal / surviving-spouse property petitions.
    (re.compile(r"\bspousal\s+property\b|\bsurviving\s+spouse\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1303(h)",
                      "Spousal / surviving-spouse property")),
    # PC 1300(g): surcharging / REMOVING / discharging a fiduciary;
    # PC 1300(i): allowing/denying a fiduciary's resignation.
    (re.compile(r"\b(?:remov\w+|surcharg\w+|discharg\w+)\b[\s\S]{0,40}\b(?:trustee|executor|administrator|fiduciary|personal\s+representative|guardian|conservator)\b|\b(?:trustee|executor|administrator|fiduciary)\b[\s\S]{0,20}\bresign", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1300(g)/(i)",
                      "Surcharge / remove / resign fiduciary")),
    # PC 1301: granting/revoking guardianship/conservatorship (and related).
    (re.compile(r"\bconservatorship\b|\bguardianship\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1301",
                      "Guardianship / conservatorship order")),
    # PC 1304: final orders on a trust under PC 17200 (establish / terminate /
    # modify / confirm trust assets / instructions re trust). The statutory
    # EXCEPTION -- an order merely compelling an account or accepting a
    # trustee's resignation -- is handled by earlier rules / writ caveat.
    (re.compile(r"\bconfirm\w*\s+trust\s+assets?\b|\b(?:establish|terminate|modify|reform)\b[\s\S]{0,40}\btrust\b|\binter[\s-]?vivos\s+trust\b|\bspecial\s+needs\s+trust\b|\bsubstituted\s+judgment\s+trust\b|\btrust\b[\s\S]{0,40}\b(?:17200|petition\s+for\s+instructions)\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1304",
                      "Final trust order (PC 17200)")),
    # PC 1300(a): sale/lease/encumbrance/exchange of estate property.
    (re.compile(r"\b(?:report\s+of\s+sale|confirm\w*\s+sale|petition\s+to\s+(?:sell|confirm\s+sale))\b|\bsale\s+of\s+real\s+property\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1300(a)",
                      "Sale/lease/encumbrance of estate property")),
    # PC 1300(c): authorizing/instructing/approving a fiduciary's acts.
    (re.compile(r"\bpetition\s+for\s+instructions?\b|\bsubstituted\s+judgment\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Prob. Code 1300(c)",
                      "Instruct / approve fiduciary's acts")),

    # ---- Anti-SLAPP DENIAL (CCP 904.1(a)(13) / 425.16(i)) ------------------- #
    # Caption-default is interlocutory_appealable; an anti-SLAPP GRANT is
    # refined to ``dispositive`` in refine_appealability(). The caption alone
    # can't tell grant from denial, so we tag the appealable-by-statute basis
    # and let outcome refine it.
    (re.compile(r"anti[-\s]?slapp|ccp\s*425\.16|\b425\.16\b|special\s+motion\s+to\s+strike", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 904.1(a)(13) / 425.16(i)",
                      "Anti-SLAPP special motion to strike")),

    # ---- New trial (grant) / JNOV (denial) -- CCP 904.1(a)(4) --------------- #
    (re.compile(r"\bnew\s+trial\b|judgment\s+notwithstanding(?:\s+the\s+verdict)?|\bjnov\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 904.1(a)(4)",
                      "New trial granted / JNOV denied")),

    # ---- Injunction -- CCP 904.1(a)(6) ------------------------------------- #
    # Grant, dissolve, OR refuse all appealable. (A TRO standing alone is not,
    # but TRO captions in this corpus are paired with a preliminary injunction
    # OSC; we tag and let confirmation sort the rare pure-TRO case.)
    (re.compile(r"\bpreliminary\s+inju\w+\b|\binjunction\b|\binjunctive\s+relief\b|\bdissolve\s+(?:the\s+)?injunction\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 904.1(a)(6)",
                      "Injunction granted/dissolved/refused")),

    # ---- Receiver appointment -- CCP 904.1(a)(7) --------------------------- #
    (re.compile(r"\bappoint\w*\s+(?:a\s+)?receiver\b|\bappointment\s+of\s+(?:a\s+)?receiver\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 904.1(a)(7)",
                      "Order appointing a receiver")),

    # ---- Attachment / right-to-attach -- CCP 904.1(a)(5) ------------------- #
    (re.compile(r"\bright\s+to\s+attach\b|\bwrit\s+of\s+attachment\b|\battach(?:ment)?\s+order\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 904.1(a)(5)",
                      "Attachment / right-to-attach order")),

    # ---- Denial of petition to COMPEL arbitration -- CCP 1294(a) ----------- #
    # Caption-default appealable; a GRANT (writ-only) is refined out by
    # refine_appealability() using the parsed outcome.
    (re.compile(r"\b(?:petition|motion)\b[\s\S]{0,40}\bcompel\s+arbitration\b|\bcompel\s+arbitration\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 1294(a)",
                      "Petition to compel arbitration (denial appealable)")),
    # ---- Confirm / vacate / correct arbitration AWARD -- CCP 1294(b)-(e) ---- #
    # The order on a petition to confirm/vacate/correct an award (and the
    # judgment entered thereon) is appealable.
    (re.compile(r"\bconfirm\w*\s+(?:contractual\s+)?arbitration\s+award\b|\bvacate\b[\s\S]{0,40}\barbitration(?:\s+award)?\b|\bcorrect\b[\s\S]{0,30}\barbitration\s+award\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 1294(b)-(e)",
                      "Order on arbitration award (confirm/vacate/correct)")),

    # ---- Class certification "death knell" --------------------------------- #
    # Judicially created (Daar v. Yellow Cab (1967); In re Baycol (2011)).
    # Caption-only & over-inclusive -- only the DENIAL is appealable, and only
    # if individual claims survive. We deliberately do NOT match generic
    # "class action settlement" captions (those are settlement-approval
    # hearings, not cert motions).
    (re.compile(r"\bclass\s+certificat(?:ion|e)\b|\bcertify\s+(?:a\s+)?class\b|\bdecertif\w+\b", re.I),
     AppealabilityTag("interlocutory_appealable", "Death-knell (Daar; In re Baycol)",
                      "Class certification denial (death knell)")),

    # ---- Order after an appealable judgment -- CCP 904.1(a)(2) -------------- #
    # Renewal of judgment, assignment/charging orders, claims of exemption,
    # contempt made final, etc. are post-judgment orders. We tag the clearest
    # post-judgment vocabulary; many of these are also collection mechanics.
    (re.compile(r"\brenewal\s+of\s+judgment\b|\bassignment\s+order\b|\bcharging\s+order\b|\bclaim\s+of\s+exemption\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 904.1(a)(2)",
                      "Order after appealable judgment")),

    # ---- Order quashing service / forum non conveniens stay -- 904.1(a)(3) -- #
    (re.compile(r"\bquash\b[\s\S]{0,30}\bservice\b|\bforum\s+non\s+conveniens\b", re.I),
     AppealabilityTag("interlocutory_appealable", "CCP 904.1(a)(3)",
                      "Quash service / forum non conveniens stay")),
]


# ---- WRIT-ONLY caption rules (flag for the SEPARATE writs track) ----------- #
# These orders are NOT directly appealable; review is by extraordinary writ
# before judgment. We classify them so the writs-track agent can pick them up.
# IMPORTANT: an order COMPELLING arbitration is writ-only -- but its caption
# overlaps the CCP 1294 interlocutory rule above; refine_appealability() moves
# a *granted* compel-arbitration row here. Discovery / venue / disqualification
# captions land here directly.
WRIT_ONLY_RULES: list[tuple[re.Pattern, AppealabilityTag]] = [
    # Discovery (compel/quash-subpoena/protective order/IME/etc.).
    (re.compile(r"\bcompel\b(?![\s\S]{0,40}\barbitration\b)|\bquash\s+subpoena\b|\bquashing\s+subpoena\b|\bprotective\s+order\b|\bdeem\b[\s\S]{0,40}\badmit|\b(?:mental|physical|independent\s+medical)\s+examination\b|\bdiscovery\s+referee\b|\breopen\w*\s+discovery\b|\bfurther\s+responses?\b", re.I),
     AppealabilityTag("writ_only", "CCP 1085/1086 (writ)",
                      "Discovery order (writ review only)")),
    # Venue (CCP 400 -- statutory writ).
    (re.compile(r"\bchange\s+of\s+venue\b|\btransfer\s+(?:of\s+)?venue\b|\bmotion\s+to\s+transfer\b|\bvenue\b", re.I),
     AppealabilityTag("writ_only", "CCP 400 (statutory writ)",
                      "Venue (writ review only)")),
    # Attorney disqualification (writ; not the same as substitution/withdraw).
    (re.compile(r"\bdisqualif\w+\b(?![\s\S]{0,20}\b(?:judge|judicial|juror)\b)", re.I),
     AppealabilityTag("writ_only", "CCP 1085 (writ)",
                      "Attorney disqualification (writ review only)")),
]


def classify_appealability(calendar_matter: Optional[str]) -> AppealabilityTag:
    """Map a caption to its CAPTION-DEFAULT appealability tag.

    Resolution order (most specific / strongest first):
      1. dispositive motion types  -> ``dispositive`` (CCP 904.1(a)(1))
      2. interlocutory-appealable rules (Probate first, then CCP 904.1 / 1294 /
         death knell)              -> ``interlocutory_appealable``
      3. writ-only rules           -> ``writ_only``
      4. standalone sanctions      -> ``unknown`` (amount-contingent; see below)
      5. otherwise                 -> ``unknown``

    NOTE: anti-SLAPP appears in BOTH the dispositive list and the
    interlocutory list. We intentionally route it through the interlocutory
    rule (so its statutory basis is 904.1(a)(13)) and let
    refine_appealability() promote a GRANT to ``dispositive``.
    """
    if not calendar_matter:
        return AppealabilityTag("unknown", "n/a", "No caption")
    text = str(calendar_matter)

    # Anti-SLAPP is handled by the interlocutory rule (its basis is more
    # precise), so we check interlocutory rules BEFORE the generic dispositive
    # ones for that caption family. For all OTHER dispositive motions, the
    # dispositive route wins.
    is_antislapp = re.search(
        r"anti[-\s]?slapp|ccp\s*425\.16|\b425\.16\b|special\s+motion\s+to\s+strike",
        text, re.I)

    disp = classify_dispositive_motion(text)
    if disp and not is_antislapp:
        return _DISPOSITIVE_TAG

    for pat, tag in INTERLOCUTORY_APPEALABLE_RULES:
        if pat.search(text):
            return tag

    for pat, tag in WRIT_ONLY_RULES:
        if pat.search(text):
            return tag

    # Standalone sanctions: appealable only if directing payment > $5,000
    # (CCP 904.1(a)(11)/(12)) -- the dollar amount is not in the caption, so we
    # cannot assert appealability. Tag unknown with the amount-contingent basis.
    if re.search(r"\bsanction", text, re.I):
        return AppealabilityTag(
            "unknown", "CCP 904.1(a)(11)/(12) (>$5,000)",
            "Sanctions (appealable only if > $5,000)")

    return AppealabilityTag("unknown", "n/a", "No appealability rule matched")


def refine_appealability(tag: AppealabilityTag,
                         outcome: str) -> AppealabilityTag:
    """Adjust a caption-default appealability tag using the parsed outcome.

    Outcome conditions a few categories where the statute keys on grant vs
    denial. We only DOWNGRADE/PROMOTE when the outcome is unambiguous; an
    "Unknown"/"Other"/procedural outcome leaves the caption default intact
    (the row stays a candidate to confirm).

      * Anti-SLAPP (904.1(a)(13)): a GRANT is the dispositive final-judgment
        route; a DENIAL is the interlocutory-appealable order. The caption
        default is interlocutory_appealable; promote a grant to dispositive.
      * Compel arbitration (1294): only DENIAL is appealable; a GRANT is
        writ-only. Caption default is interlocutory_appealable; demote a grant.
      * New trial / JNOV (904.1(a)(4)): a granted new trial is appealable; a
        DENIED new trial is reviewable on appeal from the final judgment, not
        separately. We leave the tag (still a candidate) but this is noted.
    """
    granted = outcome in ("Granted", "Granted in part", "Sustained",
                          "Sustained without leave")
    denied = outcome in ("Denied", "Overruled")

    basis = tag.basis
    if "425.16" in basis:  # anti-SLAPP
        if granted:
            return AppealabilityTag(
                "dispositive", "CCP 904.1(a)(1) (anti-SLAPP granted)",
                "Anti-SLAPP granted -> final-judgment route")
        # denial or ambiguous: keep interlocutory_appealable (904.1(a)(13))
        return tag
    if basis == "CCP 1294(a)":  # petition to compel arbitration
        if granted:
            return AppealabilityTag(
                "writ_only", "CCP 1294 (grant is writ-only)",
                "Arbitration COMPELLED -> writ review only")
        # denial or ambiguous: appealable under 1294(a)
        return tag
    return tag


# --------------------------------------------------------------------------- #
# APPEAL TRACK -- the outcome-conditioned axis (the refinement this file adds)
# ===========================================================================
# The legacy ``appealability`` field above is a TYPE axis: it asks "what kind of
# motion is this, and is an order of that kind directly appealable?". That is
# legally incomplete, because appealability turns on the OUTCOME too: an MSJ is
# the final-judgment route ONLY if it is GRANTED in full; a demurrer opens the
# appeal track ONLY if it is SUSTAINED WITHOUT LEAVE; a petition to compel
# arbitration is appealable ONLY when DENIED. ``appeal_track`` answers the real
# question -- "given THIS motion type AND THIS outcome (and full-vs-partial),
# does the disposition open a direct appeal NOW, only by writ, or not yet?".
#
# appeal_track is one of:
#   appealable_now  the disposition, IF ENTERED AS RULED, opens a direct appeal
#                   now (a final judgment under CCP 904.1(a)(1), or one of the
#                   statutorily / judicially enumerated immediately-appealable
#                   orders).
#   writ_only       reviewable, if at all, only by extraordinary writ before
#                   judgment (e.g. an MSJ denied, summary adjudication alone, a
#                   motion to quash service DENIED, arbitration COMPELLED).
#   not_yet         no appeal opens now -- the case continues and the ruling is
#                   reviewable on appeal from the eventual final judgment
#                   (e.g. demurrer overruled / sustained-with-leave, JOP with
#                   leave, motion to dismiss denied), OR no operative order was
#                   made (a procedural outcome: continued, off calendar, etc.).
#   unknown         the outcome is Unknown, or the motion type doesn't map to a
#                   known appeal-track rule.
#
# CONTROLLING CAVEAT (read before trusting any ``appealable_now``)
# ----------------------------------------------------------------
# A TENTATIVE ruling is NOT an entered order and is NOT a judgment. Direct
# appealability attaches to the ENTERED order/judgment, not to a tentative.
# Therefore ``appealable_now`` here means ONLY: "the disposition, IF ENTERED AS
# RULED, opens the appeal track" -- NOT that any appeal was in fact taken, nor
# even that an order/judgment was ever entered (the case may settle, the
# tentative may be contested at the hearing and changed, leave to amend may be
# used, etc.). Every ``appealable_now`` is a CANDIDATE for the appellate-linkage
# track, never an assertion that an appeal exists.
# --------------------------------------------------------------------------- #
APPEAL_TRACK_VALUES = ("appealable_now", "writ_only", "not_yet", "unknown")

# Motion-type matchers for the appeal_track axis. These are MORE granular than
# the dispositive detector because appeal_track must distinguish, e.g., summary
# JUDGMENT (final-judgment route) from summary ADJUDICATION (never final alone),
# and a motion to QUASH SERVICE / personal-jurisdiction (904.1(a)(3)) from a
# discovery quash-subpoena (writ-only). First match wins; order is most-specific
# first.
_AT_ANTI_SLAPP = re.compile(
    r"anti[-\s]?slapp|ccp\s*425\.16|\b425\.16\b|special\s+motion\s+to\s+strike", re.I)
# Summary adjudication / partial MSJ that is NOT styled as a whole MSJ.
_AT_SUMMARY_ADJ = re.compile(r"summary\s+adjudication", re.I)
_AT_SUMMARY_JUDG = re.compile(r"summary\s+judgment|\bmsj\b", re.I)
_AT_JOP = re.compile(r"judg(?:e?ment|mnt)\s+on\s+the\s+pleadings?", re.I)
_AT_DEMURRER = re.compile(r"\bdemurrer\b", re.I)
# Motion to quash SERVICE of summons / for lack of personal jurisdiction.
# Distinct from quash-SUBPOENA (discovery, writ-only) handled below.
_AT_QUASH_SERVICE = re.compile(
    r"\bquash\b[\s\S]{0,40}\b(?:service|summons)\b|"
    r"\bquash\s+service\s+of\s+summons\b|"
    r"\black\s+of\s+personal\s+jurisdiction\b", re.I)
# Motion to dismiss (with prejudice when granted). Exclude "quash ... or
# dismiss" captions, which are personal-jurisdiction motions handled above.
_AT_MOTION_TO_DISMISS = re.compile(r"\bmotion\s+to\s+dismiss\b|\bdismiss(?:al)?\b", re.I)
_AT_INJUNCTION = re.compile(
    r"\bpreliminary\s+inju\w+\b|\binjunction\b|\binjunctive\s+relief\b", re.I)
_AT_COMPEL_ARB = re.compile(
    r"\b(?:petition|motion)\b[\s\S]{0,40}\bcompel\s+arbitration\b|\bcompel\s+arbitration\b",
    re.I)
_AT_ARB_AWARD = re.compile(
    r"\bconfirm\w*\s+(?:contractual\s+)?arbitration\s+award\b|"
    r"\bvacate\b[\s\S]{0,40}\barbitration(?:\s+award)?\b|"
    r"\bcorrect\b[\s\S]{0,30}\barbitration\s+award\b", re.I)
_AT_ATTACHMENT = re.compile(
    r"\bright\s+to\s+attach\b|\bwrit\s+of\s+attachment\b|\battach(?:ment)?\s+order\b", re.I)
_AT_CLASS_CERT = re.compile(
    r"\bclass\s+certificat(?:ion|e)\b|\bcertify\s+(?:a\s+)?class\b|\bdecertif\w+\b", re.I)
_AT_NEW_TRIAL_JNOV = re.compile(
    r"\bnew\s+trial\b|judgment\s+notwithstanding(?:\s+the\s+verdict)?|\bjnov\b", re.I)
# Probate orders are recognized via the existing INTERLOCUTORY_APPEALABLE_RULES
# (their basis starts with "Prob. Code"); see compute_appeal_track.
#
# Probate operative-disposition fallback. index.html's OUTCOME_RULES key on
# the PAST-tense verb ("granted"/"denied"); probate tentatives use the
# IMPERATIVE ("Grant without hearing", "Deny", "Approve the account", "Settle
# the account", "Admit the will", "Confirm sale"). When the outcome classifier
# returns "Other", this matcher (anchored to the OPENING of the substantive
# ruling) recognizes such an operative order so the probate appealability rule
# isn't defeated. It deliberately does NOT match procedural openings (Continue,
# Hearing, Appearance, Off calendar) -- those route to not_yet.
_PROBATE_OPERATIVE_RE = re.compile(
    r"^\s*(?:tentative\s+ruling[:.\s-]*)?"
    r"(?:grant|den(?:y|ied)|approv|settl|admit|confirm|allow|surcharg|remov|"
    r"appoint|authoriz|instruct|distribut)", re.I)


def _proc(outcome: str) -> bool:
    """True if the outcome is procedural (no operative order made)."""
    return outcome in _PROCEDURAL


def compute_appeal_track(calendar_matter: Optional[str],
                         outcome: str,
                         caption_tag: Optional[AppealabilityTag] = None,
                         ruling_text: Optional[str] = None
                         ) -> tuple[str, str]:
    """Return (appeal_track, basis) from (motion_type x outcome x full/partial).

    This is the legal core. It is intentionally a separate function from
    ``classify_appealability`` so the type axis is preserved unchanged. Rules
    (each faithful to the cited authority):

      Summary Judgment (whole): appealable_now ONLY if GRANTED in full
        (-> final judgment, CCP 904.1(a)(1)). "Granted in part" / summary
        adjudication or "Denied" -> writ_only (no final judgment yet).
      Summary Adjudication: never final alone -> writ_only (denied / partial)
        or not_yet (procedural).
      Demurrer: appealable_now ONLY if "Sustained WITHOUT leave to amend"
        (-> dismissal). Sustained-with-leave / Overruled / sustained-in-part /
        Denied -> not_yet (case continues; reviewed on appeal from the eventual
        judgment).
      Judgment on the Pleadings: appealable_now ONLY if granted WITHOUT leave;
        otherwise not_yet.
      Motion to Dismiss: Granted (with prejudice) -> appealable_now; Denied ->
        not_yet.
      Anti-SLAPP (CCP 425.16): appealable_now on BOTH grant AND denial
        (CCP 425.16(i) / 904.1(a)(13)) -- outcome-independent.
      Motion to quash service of summons / PJ (904.1(a)(3)): GRANTED (quashing)
        -> appealable_now; Denied -> writ_only.
      Injunction (904.1(a)(6)): granting OR refusing/denying -> appealable_now
        (both directions appealable).
      Petition to compel arbitration (CCP 1294(a)): DENIED -> appealable_now;
        GRANTED/compelled -> writ_only. Order confirming/vacating/correcting an
        award -> appealable_now.
      Attachment (904.1(a)(5)): granting or denying -> appealable_now.
      Class certification death-knell: DENIAL -> appealable_now; grant ->
        not_yet.
      New trial granted / JNOV (904.1(a)(4)): granted -> appealable_now.
      Probate (PC 1300/1303 family): the order, once made (granted / settled /
        admitted / denied as a final order on that matter), is appealable_now;
        if continued / off-calendar -> not_yet.
      Any motion whose OUTCOME is procedural (Continued, Off calendar, Hearing
        required, Under submission, Withdrawn, Transferred) -> not_yet (no
        operative order). Unknown outcome -> unknown.

    ``caption_tag`` is the result of classify_appealability(); it is used only to
    detect the Probate family by statutory basis (the probate matchers already
    live in INTERLOCUTORY_APPEALABLE_RULES, no need to duplicate them).

    CAVEAT: appealable_now means "the disposition, IF ENTERED AS RULED, opens
    the appeal track" -- a tentative is not an entered order/judgment.
    """
    if outcome == "Unknown":
        return "unknown", "Outcome unknown -- cannot resolve appeal track"

    text = str(calendar_matter or "")
    rul = str(ruling_text or "")
    grant = outcome in _GRANT_LIKE
    deny = outcome in _DENY_LIKE
    in_part = outcome in ("Granted in part", "Denied in part")
    # The outcome classifier collapses "Granted with leave" and "Granted
    # without leave" to plain "Granted" (the with/without-leave SUBTYPES only
    # fire on demurrer "sustained" vocabulary). For motions whose grant is
    # phrased "granted ... leave to amend" (JOP) or "granted ... [with/without]
    # prejudice" (motion to dismiss), inspect the substantive ruling text to
    # decide whether the grant is case-ending. Default to case-ending when no
    # leave / without-prejudice qualifier is present.
    with_leave = bool(re.search(r"\bwith\s+leave\s+to\s+amend\b", rul, re.I))
    without_prejudice = bool(re.search(r"\bwithout\s+prejudice\b", rul, re.I))

    # ---- Anti-SLAPP: appealable on BOTH grant and denial, outcome-independent.
    # (Checked first because anti-SLAPP captions can also contain "strike".)
    if _AT_ANTI_SLAPP.search(text):
        if _proc(outcome):
            return "not_yet", "Anti-SLAPP: no operative order (procedural outcome)"
        return ("appealable_now",
                "CCP 425.16(i) / 904.1(a)(13): anti-SLAPP appealable on grant AND denial")

    # ---- Summary ADJUDICATION (never final on its own).
    if _AT_SUMMARY_ADJ.search(text) and not _AT_SUMMARY_JUDG.search(text):
        if _proc(outcome):
            return "not_yet", "Summary adjudication: no operative order (procedural outcome)"
        return ("writ_only",
                "Summary adjudication is not a final judgment -- writ review only")

    # ---- Summary JUDGMENT (whole): appealable_now ONLY if granted in full.
    if _AT_SUMMARY_JUDG.search(text):
        if _proc(outcome):
            return "not_yet", "MSJ: no operative order (procedural outcome)"
        if outcome == "Granted":  # full grant only (NOT "Granted in part")
            return "appealable_now", "CCP 904.1(a)(1): MSJ granted in full -> final judgment"
        if in_part:
            return ("writ_only",
                    "MSJ granted in part (summary adjudication) -> no final judgment; writ only")
        # Denied (or any other non-grant disposition): no final judgment.
        return "writ_only", "MSJ denied -> no final judgment yet; writ review only"

    # ---- Demurrer: appealable_now ONLY if sustained WITHOUT leave.
    if _AT_DEMURRER.search(text):
        if _proc(outcome):
            return "not_yet", "Demurrer: no operative order (procedural outcome)"
        if outcome == "Sustained without leave":
            return ("appealable_now",
                    "CCP 904.1(a)(1): demurrer sustained without leave -> dismissal/judgment")
        # Sustained-with-leave, Overruled, sustained-in-part, Denied: case
        # continues; reviewed on appeal from the eventual judgment.
        return ("not_yet",
                "Demurrer (with leave / overruled / in part / denied) -> case continues; "
                "reviewed on appeal from eventual judgment")

    # ---- Judgment on the Pleadings: appealable_now ONLY if granted WITHOUT leave.
    if _AT_JOP.search(text):
        if _proc(outcome):
            return "not_yet", "JOP: no operative order (procedural outcome)"
        if (grant and not in_part and not with_leave) \
                or outcome == "Sustained without leave":
            return ("appealable_now",
                    "CCP 904.1(a)(1): JOP granted without leave -> judgment")
        # With leave / in part / denied: case continues.
        return "not_yet", "JOP (with leave / in part / denied) -> case continues"

    # ---- Motion to quash service of summons / personal jurisdiction.
    # GRANTED (quashing) -> appealable_now (904.1(a)(3)); Denied -> writ_only.
    if _AT_QUASH_SERVICE.search(text):
        if _proc(outcome):
            return "not_yet", "Quash service: no operative order (procedural outcome)"
        if grant:
            return ("appealable_now",
                    "CCP 904.1(a)(3): order quashing service of summons is appealable")
        return ("writ_only",
                "Denial of motion to quash service -> writ review only")

    # ---- Petition to compel arbitration: DENIED -> appealable_now; GRANTED ->
    # writ_only (CCP 1294(a)).
    if _AT_COMPEL_ARB.search(text):
        if _proc(outcome):
            return "not_yet", "Compel arbitration: no operative order (procedural outcome)"
        if deny:
            return ("appealable_now",
                    "CCP 1294(a): denial of petition to compel arbitration is appealable")
        if grant:
            return ("writ_only",
                    "CCP 1294: order compelling arbitration -> writ review only")
        return "not_yet", "Compel arbitration: non-dispositive outcome"

    # ---- Order on arbitration AWARD (confirm / vacate / correct) -> appealable.
    if _AT_ARB_AWARD.search(text):
        if _proc(outcome):
            return "not_yet", "Arbitration award order: no operative order (procedural outcome)"
        return ("appealable_now",
                "CCP 1294(b)-(e): order confirming/vacating/correcting an award is appealable")

    # ---- Injunction: granting OR refusing/denying -> appealable_now (both).
    if _AT_INJUNCTION.search(text):
        if _proc(outcome):
            return "not_yet", "Injunction: no operative order (procedural outcome)"
        if grant or deny:
            return ("appealable_now",
                    "CCP 904.1(a)(6): order granting OR refusing an injunction is appealable")
        return "not_yet", "Injunction: non-dispositive outcome"

    # ---- Attachment / right-to-attach: granting OR denying -> appealable_now.
    if _AT_ATTACHMENT.search(text):
        if _proc(outcome):
            return "not_yet", "Attachment: no operative order (procedural outcome)"
        if grant or deny:
            return ("appealable_now",
                    "CCP 904.1(a)(5): attachment / right-to-attach order is appealable")
        return "not_yet", "Attachment: non-dispositive outcome"

    # ---- New trial granted / JNOV: granted -> appealable_now (904.1(a)(4)).
    if _AT_NEW_TRIAL_JNOV.search(text):
        if _proc(outcome):
            return "not_yet", "New trial/JNOV: no operative order (procedural outcome)"
        if grant:
            return ("appealable_now",
                    "CCP 904.1(a)(4): order granting a new trial / JNOV is appealable")
        # A DENIED new trial is reviewed on appeal from the underlying judgment.
        return "not_yet", "New trial/JNOV denied -> reviewed on appeal from the judgment"

    # ---- Class certification death-knell: DENIAL -> appealable_now; grant ->
    # not_yet (Daar v. Yellow Cab; In re Baycol).
    if _AT_CLASS_CERT.search(text):
        if _proc(outcome):
            return "not_yet", "Class certification: no operative order (procedural outcome)"
        if deny:
            return ("appealable_now",
                    "Death knell (Daar; In re Baycol): denial of class certification is appealable")
        return "not_yet", "Class certification GRANTED -> not appealable (no death knell)"

    # ---- Motion to dismiss: Granted (with prejudice) -> appealable_now;
    # Denied -> not_yet. Checked AFTER quash-service so a "quash or dismiss"
    # personal-jurisdiction motion is routed correctly above.
    if _AT_MOTION_TO_DISMISS.search(text):
        if _proc(outcome):
            return "not_yet", "Motion to dismiss: no operative order (procedural outcome)"
        if grant and not without_prejudice and not with_leave:
            return ("appealable_now",
                    "CCP 904.1(a)(1): motion to dismiss granted (with prejudice) -> judgment")
        if grant:  # dismissed without prejudice / with leave -> not case-ending
            return ("not_yet",
                    "Dismissal without prejudice / with leave -> case may continue")
        return "not_yet", "Motion to dismiss denied -> case continues"

    # ---- Probate (PC 1300/1303 family): the order, once made, is appealable_now;
    # if continued / off-calendar -> not_yet. Detected via the caption tag's
    # probate basis (matchers live in INTERLOCUTORY_APPEALABLE_RULES).
    if caption_tag is not None and caption_tag.basis.startswith("Prob. Code"):
        if _proc(outcome):
            return "not_yet", (
                f"Probate ({caption_tag.basis}): continued/off-calendar -> no final order yet")
        # Granted / settled / admitted / denied -- any FINAL order on the matter.
        if grant or deny:
            return ("appealable_now",
                    f"{caption_tag.basis}: probate order, once made, is directly appealable")
        # JUDGMENT CALL: index.html's OUTCOME_RULES key on the PAST-tense
        # "granted"/"denied"/"sustained". Probate tentatives overwhelmingly use
        # the IMPERATIVE disposition verb ("Grant without hearing", "Deny",
        # "Approve the account", "Settle", "Admit the will", "Confirm"), which
        # the classifier returns as "Other". Treating those thousands of
        # genuine operative orders as not_yet would gut the probate rule, so we
        # recognize the imperative operative-disposition vocabulary HERE (probate
        # branch only -- never for civil motions). The matter being acted on,
        # once the order is made, is appealable under PC 1300/1303 etc.
        if outcome == "Other" and _PROBATE_OPERATIVE_RE.search(rul):
            return ("appealable_now",
                    f"{caption_tag.basis}: probate order made "
                    "(operative disposition; imperative phrasing)")
        # Outcome present but not clearly a final order (e.g. ambiguous "Other"
        # with no operative verb): leave as not_yet, a candidate to confirm.
        return "not_yet", (
            f"Probate ({caption_tag.basis}): no clearly final order on this disposition")

    # ---- Fallback ---------------------------------------------------------- #
    if _proc(outcome):
        return "not_yet", "Procedural outcome -> no operative order"
    return "unknown", "No appeal-track rule for this motion type"


# --------------------------------------------------------------------------- #
# Party-name normalization (for the linkage strategy / candidate search keys)
# --------------------------------------------------------------------------- #
_VS_SPLIT = re.compile(r"\s+vs?\.?\s+|\s+v\.\s+", re.I)
_NOISE = re.compile(r"\b(et\s+al\.?|inc\.?|llc\.?|llp\.?|l\.?p\.?|co\.?|corp\.?|"
                    r"the|a|an|of|and|company|trust|estate|dba)\b", re.I)
_NONWORD = re.compile(r"[^a-z0-9\s]", re.I)


def split_parties(case_title: Optional[str]) -> tuple[list[str], list[str]]:
    """Split an SFSC case_title into (plaintiff_tokens, defendant_tokens)."""
    if not case_title:
        return [], []
    parts = _VS_SPLIT.split(str(case_title), maxsplit=1)
    if len(parts) == 2:
        left, right = parts
    else:
        left, right = parts[0], ""
    return _tokens(left), _tokens(right)


def _tokens(s: str) -> list[str]:
    s = _NONWORD.sub(" ", s)
    s = _NOISE.sub(" ", s)
    return [w.lower() for w in s.split() if len(w) > 1]


# --------------------------------------------------------------------------- #
# Per-case record (local, derived purely from the parquet)
# --------------------------------------------------------------------------- #
@dataclass
class DispositiveCaseCandidate:
    """One SFSC case that had >=1 appeal-relevant motion in the archive.

    Despite the legacy name, this now collects BOTH dispositive motions and
    interlocutory-appealable orders for a case. ``appealable_motions`` carries
    every row whose appealability tag is ``dispositive`` or
    ``interlocutory_appealable`` (each annotated with the statutory basis);
    ``dispositive_motions`` is the back-compatible subset matched by the
    original dispositive detector.
    """
    case_number: str
    department: str
    case_title: str
    plaintiff_tokens: list[str]
    defendant_tokens: list[str]
    judges: list[str]
    first_hearing_date: Optional[str]
    last_hearing_date: Optional[str]
    dispositive_motions: list[dict] = field(default_factory=list)
    appealable_motions: list[dict] = field(default_factory=list)

    @property
    def has_case_ending_grant(self) -> bool:
        """True if any dispositive motion appears to have ended the case."""
        for m in self.dispositive_motions:
            mt, oc = m["motion_type"], m["outcome"]
            if mt in TRULY_CASE_ENDING and oc in ("Granted", "Granted in part"):
                return True
            if oc == "Sustained without leave":
                return True
            if mt == "Motion to Dismiss" and oc in ("Granted", "Granted in part"):
                return True
        return False

    @property
    def appealability_values(self) -> set:
        """Set of appealability buckets present across this case's motions."""
        return {m["appealability"] for m in self.appealable_motions}

    @property
    def has_interlocutory_appealable(self) -> bool:
        return "interlocutory_appealable" in self.appealability_values

    @property
    def appeal_track_values(self) -> set:
        """Set of OUTCOME-CONDITIONED appeal tracks across this case's motions."""
        return {m.get("appeal_track") for m in self.appealable_motions
                if m.get("appeal_track")}

    @property
    def has_appealable_now(self) -> bool:
        """True if any motion's disposition (if entered) opens a direct appeal.

        NB still a CANDIDATE signal: a tentative is not an entered judgment.
        """
        return "appealable_now" in self.appeal_track_values


# --------------------------------------------------------------------------- #
# Appellate-outcome index schema (the deliverable's target shape)
# --------------------------------------------------------------------------- #
APPEAL_OUTCOMES = ("affirmed", "reversed", "remanded", "dismissed",
                   "pending", "unknown")
MATCH_METHODS = ("trial_case_number", "party_name_date_window",
                 "caption_citation_confirm", "manual", "none")


@dataclass
class AppealRecord:
    """A link from one SFSC trial case to (at most) one appellate proceeding.

    This is the schema a populated appellate index would persist. All fields
    except ``trial_case_number`` are nullable because for most cases no appeal
    is found (appeal_outcome="unknown", match_method="none").
    """
    trial_case_number: str                      # e.g. "CGC16556148"
    appellate_case_number: Optional[str] = None  # e.g. "A123456" / "S270000"
    court: Optional[str] = None                  # "Cal. Ct. App., 1st Dist." etc.
    appeal_outcome: str = "unknown"              # one of APPEAL_OUTCOMES
    opinion_url: Optional[str] = None
    opinion_date: Optional[str] = None           # ISO date string
    match_confidence: float = 0.0                # 0.0 - 1.0
    match_method: str = "none"                   # one of MATCH_METHODS
    source: Optional[str] = None                 # which SOURCES key produced it
    notes: Optional[str] = None

    def __post_init__(self):
        if self.appeal_outcome not in APPEAL_OUTCOMES:
            raise ValueError(f"appeal_outcome must be one of {APPEAL_OUTCOMES}")
        if self.match_method not in MATCH_METHODS:
            raise ValueError(f"match_method must be one of {MATCH_METHODS}")


# --------------------------------------------------------------------------- #
# Researched data sources (metadata only -- drives lookup_appeal's docs)
# --------------------------------------------------------------------------- #
SOURCES = {
    "accis": {
        "name": "California Appellate Courts Case Information System (ACCIS)",
        "base_url": "https://appellatecases.courtinfo.ca.gov/",
        "authoritative": True,
        "has_api": False,                       # ColdFusion web form, per-session token
        "can_search_by_trial_number": True,     # rare among sources
        "search_keys": ["trial_court_case_number", "party", "caption",
                        "attorney", "calendar_date", "appellate_case_number"],
        "first_district_for_sf": True,          # San Francisco -> 1st Appellate District
        "rate_limit": "undocumented; throttle hard, government portal",
        "tos": "No documented public API. Scrape sparingly, identify client, "
               "cache; prefer single-candidate confirmation over bulk crawl.",
    },
    "courtlistener": {
        "name": "CourtListener / Free Law Project REST API v4",
        "base_url": "https://www.courtlistener.com/api/rest/v4/",
        "authoritative": False,                 # aggregator, but high quality
        "has_api": True,
        "can_search_by_trial_number": False,    # appellate docket numbers only
        "search_keys": ["party_name", "court", "date_filed", "docket_number",
                        "citation", "full_text"],
        "endpoints": ["search/", "opinions/", "clusters/", "dockets/", "parties/"],
        "rate_limit": "~5/min, 50/hr, 125/day (token auth); higher with membership; "
                      "quarterly bulk CSV for large jobs",
        "tos": "Sanctioned API. Requires free token; respect documented limits. "
               "RECOMMENDED primary programmatic source.",
    },
    "courts_ca_gov": {
        "name": "California Courts published/unpublished opinions",
        "base_url": "https://courts.ca.gov/opinions",
        "authoritative": True,
        "has_api": False,
        "can_search_by_trial_number": False,
        "search_keys": ["appellate_case_number", "date", "district"],
        "rate_limit": "undocumented; slip=last 120d, unpublished=last 60d, "
                      "older via ACCIS",
        "tos": "Government opinion PDFs. Fetch by known case number; not a "
               "structured search API. Scrape sparingly.",
    },
    "justia": {
        "name": "Justia California Court of Appeal",
        "base_url": "https://law.justia.com/cases/california/court-of-appeal/",
        "authoritative": False,
        "has_api": False,
        "can_search_by_trial_number": False,
        "search_keys": ["party_name", "citation", "date"],
        "rate_limit": "n/a",
        "tos": "No public API; ToS restricts automated scraping. Manual lookup only.",
    },
    "google_scholar": {
        "name": "Google Scholar case law",
        "base_url": "https://scholar.google.com/",
        "authoritative": False,
        "has_api": False,
        "can_search_by_trial_number": False,
        "search_keys": ["party_name", "citation"],
        "rate_limit": "n/a",
        "tos": "No API; ToS forbids automated querying. NOT usable in a pipeline.",
    },
}


# --------------------------------------------------------------------------- #
# The appeal-lookup stub
# --------------------------------------------------------------------------- #
def lookup_appeal(case: DispositiveCaseCandidate,
                  online: bool = False,
                  source: str = "courtlistener") -> AppealRecord:
    """Resolve a trial case to its appeal (if any). PROTOTYPE STUB.

    Offline (default) this returns an empty/unknown AppealRecord and documents
    -- per source -- exactly what query *would* run. With ``--online`` the
    per-source fetchers are still TODO and intentionally not implemented, so we
    never hit a live site from this prototype.

    Query plans by source (see SOURCES):

      courtlistener:  GET search/?type=o&court=calctapp1d&q="<plaintiff> <defendant>"
                      &filed_after=<ruling_date>  -> opinion clusters; then
                      GET dockets/?id=... and parties/?docket=... to confirm
                      caption; map appellate docket_number (A######) and
                      disposition text -> AppealRecord. Token auth, respect
                      rate limits. Trial number is NOT a queryable field here,
                      so this is party-name + court(1st Dist.) + date-window
                      matching (match_method="party_name_date_window").

      accis:          POST the case-search form with the trial-court case
                      number (the one source that supports it) OR party name +
                      First Appellate District; parse the returned docket for
                      the A-number, disposition, and opinion link
                      (match_method="trial_case_number" when the trial number
                      keyed the hit -> highest confidence). Scrape gently.

      courts_ca_gov / justia / google_scholar:
                      confirmation / manual only -- fetch opinion text by a
                      known appellate number (courts_ca_gov) or do not script
                      at all (justia, google_scholar) per ToS.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {sorted(SOURCES)}")
    rec = AppealRecord(trial_case_number=case.case_number, source=source)

    if not online:
        rec.notes = (
            f"OFFLINE: would query {SOURCES[source]['name']} using keys "
            f"{SOURCES[source]['search_keys']}. "
            f"plaintiff={case.plaintiff_tokens} defendant={case.defendant_tokens} "
            f"window>= {case.last_hearing_date} court='1st Appellate District'."
        )
        return rec

    # --- ONLINE path: deliberately unimplemented ---------------------------- #
    # TODO(online): implement per-source fetchers with caching, a descriptive
    # User-Agent, and conservative delays. Only courtlistener (token, honoring
    # ~5/min) and a gentle accis confirmation are appropriate; never script
    # justia or google_scholar. Returning the unknown record keeps the
    # prototype from ever touching a live site.
    rec.notes = ("ONLINE path is a TODO stub -- no network call performed. "
                 "See lookup_appeal docstring for the per-source query plan.")
    return rec


# --------------------------------------------------------------------------- #
# Loading + grouping
# --------------------------------------------------------------------------- #
def _read_substantive(path: str) -> "pd.DataFrame":
    """Read one parquet, normalizing the SUBSTANTIVE ruling text into ``ruling``.

    Outcome classification must run on the substantive ruling only (the merits
    half, with admin / courtcall boilerplate stripped). Two on-disk layouts:

      * canonical ``tentatives.parquet`` keeps the substantive text in a
        separate ``ruling_substantive`` column (its ``ruling`` column still
        includes admin/courtcall language). Prefer ``ruling_substantive``.
      * per-department ``tentatives-<dept>.parquet`` already store the
        substantive text IN ``ruling`` (the admin/courtcall split lives in the
        ``-extras`` sidecar), so ``ruling`` is used as-is.

    Either way the returned frame exposes the substantive text as ``ruling``.
    """
    base = ["department", "case_number", "case_title", "court_date",
            "calendar_matter", "judge"]
    # Cheap schema probe -- read column names without loading any row data.
    import pyarrow.parquet as pq  # ships with pandas' parquet support
    names = set(pq.ParquetFile(path).schema.names)
    missing = [c for c in base if c not in names]
    if missing:
        sys.exit(f"parquet {path} missing required column(s): {', '.join(missing)}")
    if "ruling_substantive" in names:
        df = pd.read_parquet(path, columns=base + ["ruling_substantive"])
        return df.rename(columns={"ruling_substantive": "ruling"})
    if "ruling" not in names:
        sys.exit(f"parquet {path} has neither 'ruling_substantive' nor 'ruling'")
    return pd.read_parquet(path, columns=base + ["ruling"])


def _load_frames(department: Optional[str]) -> "pd.DataFrame":
    if department:
        path = os.path.join(DATA_DIR, f"tentatives-{department}.parquet")
        if not os.path.exists(path):
            sys.exit(f"No parquet for department {department}: {path}")
        return _read_substantive(path)

    per_dept = sorted(p for p in glob.glob(os.path.join(DATA_DIR, "tentatives-*.parquet"))
                      if "extras" not in os.path.basename(p))
    if per_dept:
        return pd.concat([_read_substantive(p) for p in per_dept],
                         ignore_index=True)
    if os.path.exists(CANONICAL_PARQUET):
        return _read_substantive(CANONICAL_PARQUET)
    sys.exit("No parquet files found.")


def annotate_appealability(df: "pd.DataFrame") -> "pd.DataFrame":
    """Add per-row appealability + appeal_track columns to a copy of ``df``.

    Outcome is parsed from ``ruling`` -- which the loader normalizes to the
    SUBSTANTIVE ruling text (see _read_substantive).

    Columns added:
      disp_motion        -- legacy dispositive motion type (or None)
      disp_outcome        -- parsed outcome of the ruling (index.html subtype)
      appealability       -- TYPE axis; one of APPEALABILITY_VALUES (unchanged)
      appeal_basis        -- statutory basis for the type-axis tag
      appeal_label        -- human label for the type-axis rule
      appeal_track        -- OUTCOME-CONDITIONED axis; one of APPEAL_TRACK_VALUES
      appeal_track_basis  -- short basis explaining the appeal_track value
    """
    cm = df["calendar_matter"].fillna("")
    out = df.assign(
        disp_motion=cm.map(classify_dispositive_motion),
        disp_outcome=df["ruling"].map(classify_outcome),
    )
    tags = [classify_appealability(c) for c in cm]
    refined = [refine_appealability(t, oc)
               for t, oc in zip(tags, out["disp_outcome"])]
    # appeal_track is the NEW outcome-conditioned axis (motion x outcome x
    # full/partial). It uses the caption tag only to recognize the probate
    # family; everything else keys on caption + parsed outcome. The legacy
    # caption-default tag (``tags``, pre-refinement) is passed so probate
    # detection sees the probate basis.
    rul = df["ruling"].fillna("")
    tracks = [compute_appeal_track(c, oc, t, ru)
              for c, oc, t, ru in zip(cm, out["disp_outcome"], tags, rul)]
    out = out.assign(
        appealability=[r.appealability for r in refined],
        appeal_basis=[r.basis for r in refined],
        appeal_label=[r.label for r in refined],
        appeal_track=[t[0] for t in tracks],
        appeal_track_basis=[t[1] for t in tracks],
    )
    return out


def find_dispositive_candidates(
        df: "pd.DataFrame", *, annotated: bool = False) -> list[DispositiveCaseCandidate]:
    """Flag appeal-relevant rows and collapse to one record per case_number.

    A case is a candidate if it has >=1 row tagged ``dispositive`` OR
    ``interlocutory_appealable``. Each candidate carries:
      - ``appealable_motions``: every dispositive / interlocutory-appealable row
        (with its statutory basis), and
      - ``dispositive_motions``: the back-compatible dispositive subset.
    """
    ann = df if annotated else annotate_appealability(df)
    relevant = ann[ann["appealability"].isin(
        ("dispositive", "interlocutory_appealable"))].copy()
    if relevant.empty:
        return []

    candidates: list[DispositiveCaseCandidate] = []
    for case_number, grp in relevant.groupby("case_number", sort=True):
        title = next((t for t in grp["case_title"].dropna()), "") or ""
        p_tok, d_tok = split_parties(title)
        judges = sorted({str(j) for j in grp["judge"].dropna()
                         if str(j) and str(j).lower() != "nan"})
        date_values = pd.to_datetime(grp["court_date"], errors="coerce")
        valid_dates = sorted(d.date().isoformat() for d in date_values.dropna())

        appealable = [
            {
                "court_date": str(r.court_date),
                "appealability": r.appealability,
                "basis": r.appeal_basis,
                "label": r.appeal_label,
                "appeal_track": r.appeal_track,
                "appeal_track_basis": r.appeal_track_basis,
                # disp_motion is None for interlocutory-only rows; pandas
                # surfaces it as NaN via itertuples, so normalize to None.
                "motion_type": (r.disp_motion
                                if isinstance(r.disp_motion, str) else None),
                "outcome": r.disp_outcome,
                "calendar_matter": str(r.calendar_matter),
            }
            for r in grp.itertuples()
        ]
        # Back-compat dispositive subset (only rows the original detector hit).
        dispositive = [
            {
                "court_date": m["court_date"],
                "motion_type": m["motion_type"],
                "outcome": m["outcome"],
                "calendar_matter": m["calendar_matter"],
            }
            for m in appealable if m["motion_type"]
        ]
        candidates.append(DispositiveCaseCandidate(
            case_number=str(case_number),
            department=str(grp["department"].iloc[0]),
            case_title=title,
            plaintiff_tokens=p_tok,
            defendant_tokens=d_tok,
            judges=judges,
            first_hearing_date=valid_dates[0] if valid_dates else None,
            last_hearing_date=valid_dates[-1] if valid_dates else None,
            dispositive_motions=dispositive,
            appealable_motions=appealable,
        ))
    return candidates


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def appealability_row_summary(df: "pd.DataFrame") -> dict:
    """Return {appealability_value: row_count} over an annotated frame."""
    ann = annotate_appealability(df)
    return ann["appealability"].value_counts().to_dict()


def appeal_track_row_summary(df: "pd.DataFrame") -> dict:
    """Return {appeal_track_value: row_count} over an annotated frame.

    This is the OUTCOME-CONDITIONED axis (motion x outcome x full/partial),
    computed over EVERY row in the corpus -- not just dispositive/interlocutory
    candidates -- because appeal_track is defined for all rows (procedural and
    no-rule rows resolve to not_yet / unknown).
    """
    ann = annotate_appealability(df)
    return ann["appeal_track"].value_counts().to_dict()


_CANDIDATE_BUCKETS = ("dispositive", "interlocutory_appealable")


def _print_appeal_track_summary(track_summary: dict) -> None:
    """Print the corpus-wide OUTCOME-CONDITIONED appeal_track breakdown."""
    print("ALL ROWS by appeal_track (OUTCOME-conditioned axis: "
          "motion x outcome x full/partial):")
    total = sum(track_summary.values())
    for t in APPEAL_TRACK_VALUES:
        cnt = int(track_summary.get(t, 0))
        print(f"  {cnt:>9,}  {t}")
    print(f"  {total:>9,}  TOTAL rows")
    print("  (appealable_now = the disposition, IF ENTERED AS RULED, opens the "
          "appeal\n   track -- a tentative is NOT an entered order/judgment.)")
    print("-" * 74)


def _print_dry_run(candidates: list[DispositiveCaseCandidate],
                   limit: int,
                   row_summary: Optional[dict] = None,
                   appealability_filter: Optional[str] = None,
                   track_summary: Optional[dict] = None,
                   appeal_track_filter: Optional[str] = None) -> None:
    # writ_only / unknown rows are intentionally NOT collected into per-case
    # candidates (the writs track is owned by a separate agent, and unknown
    # rows have no actionable appellate hook). For those filters, show only the
    # corpus row-level summary plus a pointer, then return.
    if appealability_filter and appealability_filter not in _CANDIDATE_BUCKETS:
        print("=" * 74)
        print(f"SFSC APPEALABILITY -- bucket '{appealability_filter}' "
              "(row-level only)")
        print("=" * 74)
        if row_summary:
            cnt = int(row_summary.get(appealability_filter, 0))
            total = sum(row_summary.values())
            print(f"  {cnt:>9,}  rows tagged '{appealability_filter}' "
                  f"(of {total:,} total)")
        if appealability_filter == "writ_only":
            print("\nThese orders are NOT directly appealable; they are flagged "
                  "for the\nSEPARATE writs track (discovery, venue, attorney "
                  "disqualification, and\norders COMPELLING arbitration). This "
                  "script does not build that track.")
        else:
            print("\nThese captions map to no appealability rule, or their "
                  "appealability is\ncontingent on a fact the caption/tentative "
                  "can't reveal (e.g. sanctions\n> $5,000 -- CCP "
                  "904.1(a)(11)/(12)).")
        return

    # Optionally narrow candidates to those exhibiting the requested bucket /
    # appeal_track.
    if appealability_filter:
        candidates = [c for c in candidates
                      if appealability_filter in c.appealability_values]
    if appeal_track_filter:
        candidates = [c for c in candidates
                      if appeal_track_filter in c.appeal_track_values]

    n = len(candidates)
    ending = sum(1 for c in candidates if c.has_case_ending_grant)
    interloc = sum(1 for c in candidates if c.has_interlocutory_appealable)
    appnow = sum(1 for c in candidates if c.has_appealable_now)

    # ROW-level tallies across the appealable motions of these candidates.
    by_bucket: dict[str, int] = {}
    by_basis: dict[str, int] = {}
    by_track: dict[str, int] = {}
    for c in candidates:
        for m in c.appealable_motions:
            if appealability_filter and m["appealability"] != appealability_filter:
                continue
            if appeal_track_filter and m.get("appeal_track") != appeal_track_filter:
                continue
            by_bucket[m["appealability"]] = by_bucket.get(m["appealability"], 0) + 1
            key = f"{m['appealability']}  |  {m['basis']}  ({m['label']})"
            by_basis[key] = by_basis.get(key, 0) + 1
            tk = m.get("appeal_track", "unknown")
            by_track[tk] = by_track.get(tk, 0) + 1

    print("=" * 74)
    print("SFSC APPEALABILITY CANDIDATES (local parquet only)")
    print("=" * 74)
    if row_summary:
        print("ALL ROWS by appealability bucket (TYPE axis; whole corpus / dept):")
        total = sum(row_summary.values())
        for b in APPEALABILITY_VALUES:
            cnt = int(row_summary.get(b, 0))
            print(f"  {cnt:>9,}  {b}")
        print(f"  {total:>9,}  TOTAL rows")
        print("-" * 74)
    if track_summary:
        _print_appeal_track_summary(track_summary)
    flt_parts = []
    if appealability_filter:
        flt_parts.append(f"appealability='{appealability_filter}'")
    if appeal_track_filter:
        flt_parts.append(f"appeal_track='{appeal_track_filter}'")
    flt = f"  [filtered to {', '.join(flt_parts)}]" if flt_parts else ""
    print(f"Cases with >=1 appealable motion : {n}{flt}")
    print(f"  ...with an interlocutory-appealable order  : {interloc}")
    print(f"  ...with a case-ending dispositive grant    : {ending}")
    print(f"  ...with an appealable_now disposition       : {appnow}")
    print("\nCandidate appealable-motion ROWS by appeal_track "
          "(OUTCOME-conditioned):")
    for t, c in sorted(by_track.items(), key=lambda kv: -kv[1]):
        print(f"  {c:>8,}  {t}")
    print("\nCandidate appealable-motion ROWS by appealability bucket (TYPE axis):")
    for b, c in sorted(by_bucket.items(), key=lambda kv: -kv[1]):
        print(f"  {c:>8,}  {b}")
    print("\nCandidate appealable-motion ROWS by statutory basis (TYPE axis):")
    for key, c in sorted(by_basis.items(), key=lambda kv: -kv[1]):
        print(f"  {c:>8,}  {key}")
    print("-" * 74)
    print(f"Showing first {min(limit, n)} candidate cases:")
    for c in candidates[:limit]:
        tags = "+".join(sorted(c.appealability_values))
        tracks = "+".join(sorted(c.appeal_track_values))
        flag = "  [appealable_now]" if c.has_appealable_now else ""
        print(f"\n* {c.case_number}  (Dept {c.department})  "
              f"appealability=[{tags}]  appeal_track=[{tracks}]{flag}")
        print(f"    {c.case_title}")
        print(f"    hearings {c.first_hearing_date} .. {c.last_hearing_date}")
        for m in c.appealable_motions:
            if appealability_filter and m["appealability"] != appealability_filter:
                continue
            if appeal_track_filter and m.get("appeal_track") != appeal_track_filter:
                continue
            mt = m.get("motion_type") or m["label"]
            print(f"      - {m['court_date']}  {mt}  -> {m['outcome']}")
            print(f"          appeal_track={m.get('appeal_track')}: "
                  f"{m.get('appeal_track_basis')}")
            print(f"          appealability={m['appealability']}: {m['basis']}")
    print("\n" + "-" * 74)
    print("NOTE: a *tentative ruling* is not an entered order, and an entered "
          "order is\nnot a judgment. Appealability attaches to the ENTERED "
          "order/judgment.\n'appeal_track=appealable_now' therefore means ONLY "
          "'the disposition, IF\nENTERED AS RULED, opens the appeal track' -- "
          "NOT that an appeal was taken,\nnor even that an order/judgment was "
          "ever entered (the case may settle, the\ntentative may change at the "
          "hearing, leave to amend may be used). Every\nrow is a CANDIDATE for "
          "appellate linkage -- run lookup_appeal() to attempt a\nmatch. "
          "Writ-only orders are flagged for the SEPARATE writs track.")


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Index SFSC cases by appealability and (stub) link to appeals.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print appealability candidates from the local "
                         "parquet and exit (no network).")
    ap.add_argument("--department", help="Limit to one department, e.g. 302 or 204.")
    ap.add_argument("--limit", type=int, default=20,
                    help="Max candidate cases to print in --dry-run.")
    ap.add_argument("--appealability", choices=APPEALABILITY_VALUES,
                    help="Filter candidates/output to a single appealability "
                         "bucket (TYPE axis), e.g. interlocutory_appealable.")
    ap.add_argument("--appeal-track", dest="appeal_track",
                    choices=APPEAL_TRACK_VALUES,
                    help="Filter candidates/output to a single appeal_track "
                         "(OUTCOME-conditioned axis), e.g. appealable_now.")
    ap.add_argument("--json-out", help="Write candidates as JSON to this path.")
    ap.add_argument("--force", action="store_true",
                    help="Allow --json-out to overwrite an existing file.")
    ap.add_argument("--online", action="store_true",
                    help="(TODO) enable live appellate lookups. Currently a "
                         "no-op stub; no network calls are made.")
    ap.add_argument("--list-sources", action="store_true",
                    help="Print researched appellate data sources and exit.")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.list_sources:
        print(json.dumps(SOURCES, indent=2))
        return 0

    df = _load_frames(args.department)
    # Annotate once; derive both axis summaries from the same annotated frame.
    ann = annotate_appealability(df)
    candidates = find_dispositive_candidates(ann, annotated=True)
    row_summary = ann["appealability"].value_counts().to_dict()
    track_summary = ann["appeal_track"].value_counts().to_dict()

    if args.json_out:
        out = candidates
        if args.appealability:
            out = [c for c in out
                   if args.appealability in c.appealability_values]
        if args.appeal_track:
            out = [c for c in out
                   if args.appeal_track in c.appeal_track_values]
        json_path = Path(args.json_out)
        if json_path.exists() and not args.force:
            sys.exit(f"Refusing to overwrite existing --json-out path without --force: {json_path}")
        write_json_atomic(json_path, [asdict(c) for c in out])
        print(f"Wrote {len(out)} candidates -> {args.json_out}")

    if args.dry_run or not (args.json_out or args.online):
        _print_dry_run(candidates, args.limit, row_summary=row_summary,
                       appealability_filter=args.appealability,
                       track_summary=track_summary,
                       appeal_track_filter=args.appeal_track)
        return 0

    if args.online:
        print("--online is a documented stub; no live queries performed.")
        for c in candidates[: args.limit]:
            rec = lookup_appeal(c, online=True)
            print(f"{rec.trial_case_number}: {rec.appeal_outcome} "
                  f"({rec.match_method})  {rec.notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
