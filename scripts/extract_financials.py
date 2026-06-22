#!/usr/bin/env python3
"""Extract monetary figures from SFSC tentative-ruling TEXT.

PROTOTYPE; pandas + stdlib only (CI-friendly — no heavy ML). A rule-based /
light-NLP pass over each ruling's substantive text (``ruling_substantive``,
falling back to ``ruling``) plus ``calendar_matter``. For every dollar amount it
finds, it classifies the figure by its surrounding context into a ``kind``
(sanctions / attorney_fees / costs / judgment_or_settlement / bond / damages /
other), guesses a ``direction`` (who it is awarded to / against), captures a
short context snippet, and assigns a coarse ``confidence``.

Output: ``data/financials.parquet`` (one row per extracted amount).

ERROR / DISAGREEMENT POLICY
---------------------------
This mirrors the project's heuristic-bug-report policy (DESIGN.md §8):

  * Obvious nonsense (garbage / scan-typo strings around an amount) is filtered
    out as *noise* and NOT reported.
  * When a ruling carries several amounts that are *supposed* to reconcile
    (e.g. itemized fees that should sum to a stated "total") but don't, OR an
    amount that plainly doesn't fit its context, we do NOT silently pick one —
    we append a structured bug-report record to
    ``data/financials_bug_reports.ndjson`` ({ts, case_number, court_date,
    amounts, kinds, reason, context}) and keep ALL candidate amounts in the
    output so the conflict is surfaced rather than resolved behind the scenes.

NOTE: this ndjson stream is a separate, financial-specific log today. It should
later be UNIFIED with ``data/heuristic_bug_reports.ndjson`` — same one-object-
per-line shape and same "surface, don't silently resolve" philosophy — once the
financial heuristics graduate out of prototype.

CLI
---
    python scripts/extract_financials.py \
        --out data/financials.parquet \
        --bug-report data/financials_bug_reports.ndjson \
        --limit N --department D

Quick pass:  python scripts/extract_financials.py --limit 2000
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CANONICAL_PARQUET = os.path.join(ROOT, "tentatives.parquet")
DEFAULT_OUT = os.path.join(ROOT, "data", "financials.parquet")
DEFAULT_BUG_REPORT = os.path.join(ROOT, "data", "financials_bug_reports.ndjson")
BUG_REPORT_MAX_BYTES = max(1, int(os.environ.get("SFSC_BUG_REPORT_MAX_BYTES", str(16 * 1024 * 1024)) or str(16 * 1024 * 1024)))

# Columns we read. Per-department slices omit ruling_substantive; we degrade
# gracefully to ``ruling`` when it's missing (handled in load_rulings).
TEXT_COLS = [
    "department",
    "case_number",
    "court_date",
    "calendar_matter",
    "ruling",
    "ruling_substantive",
]


def rotate_if_large(path, max_bytes=BUG_REPORT_MAX_BYTES):
    if not path or max_bytes <= 0 or not os.path.exists(path):
        return
    if os.path.getsize(path) <= max_bytes:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{path}.{stamp}.bak"
    n = 1
    while os.path.exists(backup):
        backup = f"{path}.{stamp}.{n}.bak"
        n += 1
    os.replace(path, backup)


def write_parquet_atomic(path, df):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".parquet", dir=directory)
    os.close(fd)
    try:
        df.to_parquet(tmp_name, index=False)
        if os.path.getsize(tmp_name) <= 0:
            raise RuntimeError(f"refusing to replace {path}: temporary parquet is empty")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

# ---------------------------------------------------------------------------
# Core regexes
# ---------------------------------------------------------------------------
# The canonical dollar pattern from the spec. We additionally tolerate a
# trailing "K"/"M"/"million"/"billion" magnitude word so "$765K bond" parses,
# and common spelled-out dollars such as "fifty thousand dollars".
_AMOUNT_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
    "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
    "hundred", "thousand", "million", "billion",
)
_AMOUNT_WORD_RE = r"(?:%s)" % "|".join(_AMOUNT_WORDS)
SPELLED_NUMBER_PATTERN = (
    _AMOUNT_WORD_RE + r"(?:[\s-]+(?:and\s+)?%s)*" % _AMOUNT_WORD_RE
)
SPELLED_DOLLAR_PATTERN = SPELLED_NUMBER_PATTERN + r"\s+dollars?"
NUMERIC_DOLLAR_PATTERN = (
    r"\$[\d,]+(?:\.\d{1,2})?"
    r"(?:(?:K|M|B)(?![A-Za-z])|\s(?:thousand|million|billion)\b)?"
)
DOLLAR_AMOUNT_PATTERN = (
    r"(?:%s|\b%s\b)" % (NUMERIC_DOLLAR_PATTERN, SPELLED_DOLLAR_PATTERN)
)
MONEY_PREFILTER_PATTERN = (
    r"\$\s?\d|\b%s\b" % SPELLED_DOLLAR_PATTERN
)
AMOUNT_RE = re.compile(NUMERIC_DOLLAR_PATTERN, re.IGNORECASE)
# Capturing variant that also grabs an optional magnitude suffix. The suffix
# must be a *standalone* token (terminated by a word boundary and not be the
# start of a longer word) so "$10,000 bond" does NOT read "bond" as billions.
# Single-letter K/M/B are only honored when glued directly to the number
# ("$765K"); spelled-out words may have one space ("$5 million").
AMOUNT_MAG_RE = re.compile(
    r"\$([\d,]+(?:\.\d{1,2})?)"
    r"(?:(K|M|B)(?![A-Za-z])|\s(thousand|million|billion)\b)?",
    re.IGNORECASE,
)
SPELLED_AMOUNT_RE = re.compile(
    r"\b(" + SPELLED_NUMBER_PATTERN + r")\s+dollars?\b",
    re.IGNORECASE,
)

CONTEXT_PAD = 80  # ±chars of context snippet around each amount

# ---------------------------------------------------------------------------
# Classification vocabulary. Order matters: the first kind whose pattern hits
# the local context (most specific first) wins. "other" is the fallback.
# ---------------------------------------------------------------------------
KIND_PATTERNS = [
    ("sanctions", re.compile(
        r"\bsanction", re.IGNORECASE)),
    ("attorney_fees", re.compile(
        r"\battorney(?:s)?['’]?\s*fee|\battorney\s+fee|\batty\.?\s*fee"
        r"|\bcounsel\s+fee|\bfees?\s+(?:and|&)\s+costs|\bstatutory\s+fee"
        r"|\bconservator(?:'s|’s)?\s+fee", re.IGNORECASE)),
    ("costs", re.compile(
        r"\bcosts?\b|\bfiling\s+fee|\breimburse", re.IGNORECASE)),
    ("bond", re.compile(
        r"\bbond\b|\bundertaking\b", re.IGNORECASE)),
    ("damages", re.compile(
        r"\bdamages?\b|\bcivil\s+penalt|\bpenalt(?:y|ies)\b|\brestitution\b",
        re.IGNORECASE)),
    ("judgment_or_settlement", re.compile(
        r"\bjudgment\b|\bsettle|\bsettlement\b|\bjudgement\b"
        r"|\baward(?:ed|ing|s)?\b.*\bjudgment", re.IGNORECASE)),
    # Generic award/fee language that didn't match a more specific kind.
    ("other", re.compile(
        r"\baward|\bfee\b|\bdeposit\b|\bretainer\b|\bamount\b|\bpay",
        re.IGNORECASE)),
]

# Direction phrasing. "against X" => against; "to / in favor of / payable to X"
# => awarded_to. We look in a window after the amount first, then before.
AGAINST_RE = re.compile(
    r"\b(?:against|imposed\s+(?:up)?on|to\s+be\s+paid\s+by|payable\s+by"
    r"|ordered\s+to\s+pay)\b", re.IGNORECASE)
AWARDED_TO_RE = re.compile(
    r"\b(?:awarded\s+to|in\s+favor\s+of|payable\s+to|to\s+be\s+paid\s+to"
    r"|paid\s+to|awards?\s+(?:to\s+)?)\b", re.IGNORECASE)
DIRECTION_RULES = (
    ("against", AGAINST_RE),
    ("awarded_to", AWARDED_TO_RE),
)
DIRECTION_PATTERNS = dict(DIRECTION_RULES)

# A loose party-noun grabber to fill the "parties" field when discernible.
PARTY_RE = re.compile(
    r"\b("
    r"plaintiff(?:s)?|defendant(?:s)?|petitioner(?:s)?|respondent(?:s)?"
    r"|moving\s+party|movant|cross-?complainant|cross-?defendant"
    r"|counsel|conservator|class\s+counsel|prevailing\s+party"
    r")\b", re.IGNORECASE)

# Confidence is deliberately coarse, but keep the thresholds inspectable and
# validate them so later edits do not silently reshuffle labels.
CONFIDENCE_SIGNALS = (
    ("specific_kind", 2,
     lambda ctx: ctx["kind"] != "other"),
    ("direction", 1,
     lambda ctx: ctx["kind"] != "other" and bool(ctx["direction"])),
    ("party", 1,
     lambda ctx: ctx["kind"] != "other" and bool(ctx["parties"])),
)
CONFIDENCE_THRESHOLDS = (
    ("high", 4),
    ("medium", 2),
    ("low", 0),
)


def validate_confidence_config():
    labels = [label for label, _ in CONFIDENCE_THRESHOLDS]
    scores = [score for _, score in CONFIDENCE_THRESHOLDS]
    if labels != ["high", "medium", "low"]:
        raise RuntimeError("confidence labels must be high, medium, low")
    if scores != sorted(scores, reverse=True) or scores[-1] != 0:
        raise RuntimeError("confidence thresholds must descend to zero")
    if any(weight <= 0 for _, weight, _ in CONFIDENCE_SIGNALS):
        raise RuntimeError("confidence signal weights must be positive")


validate_confidence_config()

# Words that signal a "total" / reconciliation expectation.
TOTAL_RE = re.compile(
    r"\btotal\b|\baggregate\b|\bgrand\s+total\b|\bsum\s+of\b|\bcombined\b",
    re.IGNORECASE)

# A stated total of the form "total ... $X" or "$X total" / "for a total of $X".
# The amount immediately tied to a total cue is the declared total.
STATED_TOTAL_RE = re.compile(
    r"(?:(?:grand\s+)?total|aggregate|combined|sum)\s+(?:of\s+|amount\s+of\s+|"
    r"in\s+the\s+amount\s+of\s+|due\s+|is\s+|:\s*|=\s*)?"
    r"(" + DOLLAR_AMOUNT_PATTERN + r")"
    r"|(" + DOLLAR_AMOUNT_PATTERN + r")\s+(?:in\s+)?(?:total|aggregate)"
    r"|for\s+a\s+total\s+of\s+(" + DOLLAR_AMOUNT_PATTERN + r")",
    re.IGNORECASE)

# An explicit additive itemization: "$A and $B" or "$A plus $B" or "$A, $B and
# $C" — i.e. amounts a human strung together as components of a sum. We require
# this additive linkage before claiming the items "should" reconcile. The
# separators (comma / "and" / "plus" / "+") may repeat, so "$A plus $B plus $C"
# captures all three components.
_SEP = r"(?:\s*,\s*|\s+and\s+|\s+plus\s+|\s*\+\s*)"
ADDITIVE_LIST_RE = re.compile(
    DOLLAR_AMOUNT_PATTERN + r"(?:" + _SEP + DOLLAR_AMOUNT_PATTERN + r")+",
    re.IGNORECASE)

# Avoid reconciliation bug reports where the nearby numbers are judgment math,
# not line-item totals. Interest accrual and amended/renewed judgments often
# name principal, prior judgment, current balance, and total in one sentence,
# but those amounts are not supposed to add up mechanically.
AMENDED_JUDGMENT_RE = re.compile(
    r"\b(?:amend(?:ed|ment)\s+judg(?:e)?ments?"
    r"|judg(?:e)?ments?\s+as\s+amended"
    r"|renew(?:ed|al)\s+(?:of\s+)?judg(?:e)?ments?)\b",
    re.IGNORECASE,
)
INTEREST_MATH_RE = re.compile(
    r"\b(?:compound(?:ed)?|accru(?:ed|ing)|per\s+annum"
    r"|post[-\s]?judg(?:e)?ment|pre[-\s]?judg(?:e)?ment"
    r"|interest\s+rate|rate\s+of\s+\d+(?:\.\d+)?\s*%)\b",
    re.IGNORECASE,
)

# An amount embedded in clear scan garbage: surrounded by long runs of
# non-space, non-alphanumeric punctuation (typical OCR debris). Used to flag
# *noise* that we drop rather than bug-report.
GARBAGE_RE = re.compile(r"[^\w\s.,$%()/&'’–-]{3,}")


# ===========================================================================
# Loading
# ===========================================================================
def dept_parquet(department):
    return os.path.join(ROOT, "data", f"tentatives-{department}.parquet")


def load_rulings(department=None, limit=None):
    """Load one row per (ruling) with the text columns we classify over.

    Unlike enrich_cases.load_cases, we do NOT collapse to one row per case —
    each ruling/hearing can carry its own monetary figures and we want a row
    per amount, so we keep rulings distinct.
    """
    if department:
        path = dept_parquet(department)
        if not os.path.exists(path):
            print(f"ERROR: no parquet for department {department}: {path}",
                  file=sys.stderr)
            sys.exit(2)
    else:
        path = CANONICAL_PARQUET

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

    # Source text: substantive ruling, fall back to raw ruling, then append the
    # calendar_matter so figures appearing only in the caption are caught.
    sub = df["ruling_substantive"].astype(str)
    raw = df["ruling"].astype(str)
    base = sub.where(sub.str.strip() != "", raw)
    df["_text"] = (base.str.strip() + "  " + df["calendar_matter"].astype(str)
                   .str.strip()).str.strip()

    # Pre-filter to rulings that actually contain money. The vast majority
    # have no money and scanning them is wasted work.
    df = df[df["_text"].str.contains(
        MONEY_PREFILTER_PATTERN, flags=re.IGNORECASE, regex=True)]
    if limit:
        df = df.head(limit)
    return df


# ===========================================================================
# Extraction helpers
# ===========================================================================
def parse_amount(raw_num, magnitude):
    """Turn a matched ``$...`` string into a float, honoring K/M/B suffixes."""
    n = float(raw_num.replace(",", ""))
    if magnitude:
        mag = magnitude.lower()
        if mag in ("k", "thousand"):
            n *= 1_000
        elif mag in ("m", "million"):
            n *= 1_000_000
        elif mag in ("b", "billion"):
            n *= 1_000_000_000
    return n


_SMALL_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALE_NUMBER_WORDS = {
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
}


def parse_spelled_amount(words):
    """Turn common spelled-out number words into a float dollar amount."""
    tokens = [t for t in re.split(r"[\s-]+", words.lower()) if t and t != "and"]
    total = 0
    current = 0
    saw_number = False
    last_scale = float("inf")

    for token in tokens:
        if token in _SMALL_NUMBER_WORDS:
            current += _SMALL_NUMBER_WORDS[token]
            saw_number = True
        elif token == "hundred":
            if current == 0:
                return None
            current *= 100
            saw_number = True
        elif token in _SCALE_NUMBER_WORDS:
            scale = _SCALE_NUMBER_WORDS[token]
            if current == 0 or scale >= last_scale:
                return None
            total += current * scale
            current = 0
            last_scale = scale
            saw_number = True
        else:
            return None

    if not saw_number:
        return None
    return float(total + current)


def parse_amount_text(amount_text):
    """Parse either a numeric dollar expression or spelled-out dollars."""
    text = amount_text.strip().rstrip(".,;:")
    numeric = AMOUNT_MAG_RE.fullmatch(text)
    if numeric:
        return parse_amount(numeric.group(1), numeric.group(2) or numeric.group(3))
    spelled = SPELLED_AMOUNT_RE.fullmatch(text)
    if spelled:
        return parse_spelled_amount(spelled.group(1))
    return None


def iter_amount_mentions(text):
    """Yield parsed money mentions with raw text and source spans."""
    hits = []
    for m in AMOUNT_MAG_RE.finditer(text):
        try:
            amount = parse_amount(m.group(1), m.group(2) or m.group(3))
        except ValueError:
            continue
        hits.append({
            "raw": m.group(0),
            "start": m.start(),
            "end": m.end(),
            "amount": amount,
        })
    for m in SPELLED_AMOUNT_RE.finditer(text):
        amount = parse_spelled_amount(m.group(1))
        if amount is None:
            continue
        hits.append({
            "raw": m.group(0),
            "start": m.start(),
            "end": m.end(),
            "amount": amount,
        })

    last_end = -1
    for hit in sorted(hits, key=lambda h: (h["start"], h["end"])):
        if hit["start"] < last_end:
            continue
        last_end = hit["end"]
        yield hit


def classify_kind(context):
    """Classify an amount by its surrounding context. Returns a kind string."""
    for kind, pat in KIND_PATTERNS:
        if pat.search(context):
            return kind
    return "other"


def classify_confidence(kind, direction, parties):
    """Return high/medium/low from the validated signal table above."""
    ctx = {"kind": kind, "direction": direction, "parties": parties}
    score = sum(weight for _, weight, applies in CONFIDENCE_SIGNALS if applies(ctx))
    for label, minimum in CONFIDENCE_THRESHOLDS:
        if score >= minimum:
            return label
    raise RuntimeError("confidence thresholds did not include a fallback")


def detect_direction(context, amount_span):
    """Return (direction, parties) inferred from context, or (None, None).

    ``direction`` is 'against' or 'awarded_to' when discernible. ``parties`` is
    the nearest party noun(s) attached to that direction, joined by '; '.
    """
    start, end = amount_span
    after = context[end:]
    before = context[:start]

    direction = None
    cue_text_region = None
    # Closest cue wins. After-window distances measured from amount end;
    # before-window from amount start (reversed).
    candidates = []
    for candidate_direction, pattern in DIRECTION_RULES:
        after_match = pattern.search(after)
        before_match = pattern.search(before)
        if after_match:
            candidates.append((candidate_direction, after_match.start(), after))
        if before_match:
            candidates.append((
                candidate_direction, start - before_match.end(), before))
    if candidates:
        direction, _, cue_text_region = min(candidates, key=lambda c: c[1])

    parties = None
    if direction:
        # Grab the first party noun appearing after the cue within the same
        # region; fall back to any party noun in the whole context.
        region = cue_text_region
        pm = None
        # Search after the cue position in that region.
        cue_pattern = DIRECTION_PATTERNS[direction]
        m_cue = cue_pattern.search(region)
        if m_cue:
            pm = PARTY_RE.search(region, m_cue.end())
        if not pm:
            pm = PARTY_RE.search(context)
        if pm:
            parties = pm.group(0).strip()
    return direction, parties


def is_noise(amount_str, context):
    """True if the amount sits in obvious scan garbage (drop as noise)."""
    # Garbage debris immediately adjacent to the amount.
    idx = context.find(amount_str)
    if idx == -1:
        return False
    window = context[max(0, idx - 8):idx + len(amount_str) + 8]
    return bool(GARBAGE_RE.search(window))


# ===========================================================================
# Reconciliation / disagreement checks (bug reports)
# ===========================================================================
def _num(s):
    amount = parse_amount_text(s)
    if amount is not None:
        return amount
    try:
        return float(s.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"could not parse amount: {s!r}") from exc


def reconciliation_suppressed(context):
    """True when adjacent numbers are judgment/interest math, not itemization."""
    if AMENDED_JUDGMENT_RE.search(context):
        return True
    if re.search(r"\binterest\b", context, re.IGNORECASE):
        return bool(INTEREST_MATH_RE.search(context))
    return False


def reconciliation_bug(amounts, kinds, full_text):
    """Detect when amounts that a human EXPLICITLY tied together don't reconcile.

    Returns a (reason, detail) tuple if a bug should be reported, else None.

    Deliberately conservative to avoid false positives — we only fire when the
    text both (a) declares an explicit TOTAL amount via a "total ... $X" cue,
    and (b) contains an explicit ADDITIVE list ("$A and $B (and $C)") of the
    components TEXTUALLY ADJACENT to that total (within RECON_WINDOW chars). If
    those listed components don't sum to the declared total (within a $1
    rounding tolerance), THAT is a genuine reconciliation conflict worth
    surfacing — we do not silently pick one. The proximity requirement keeps
    unrelated figures elsewhere in the ruling (hourly rates, statute citations,
    attachment amounts, a separate motion's numbers) from being paired with a
    distant total.
    """
    if len(amounts) < 3:
        return None

    RECON_WINDOW = 140  # chars between the total cue and the itemization

    # Pre-collect every explicit additive list with its span.
    lists = []
    for am in ADDITIVE_LIST_RE.finditer(full_text):
        nums = [hit["amount"] for hit in iter_amount_mentions(am.group(0))]
        if len(nums) >= 2:
            lists.append((am.start(), am.end(), nums))
    if not lists:
        return None

    # For each declared total, find an additive list close enough to be its
    # itemization. Iterate all total cues (a ruling may state several).
    for tm in STATED_TOTAL_RE.finditer(full_text):
        try:
            stated_total = _num(next(g for g in tm.groups() if g))
        except ValueError:
            continue
        for ls, le, nums in lists:
            # Adjacency: list within RECON_WINDOW of the total cue on either side.
            gap = ls - tm.end() if ls >= tm.end() else tm.start() - le
            if gap > RECON_WINDOW:
                continue
            context_start = max(0, min(tm.start(), ls) - RECON_WINDOW)
            context_end = min(len(full_text), max(tm.end(), le) + RECON_WINDOW)
            if reconciliation_suppressed(full_text[context_start:context_end]):
                continue
            parts = [n for n in nums if abs(n - stated_total) > 0.005]
            if len(parts) < 2:
                continue
            parts_sum = round(sum(parts), 2)
            if abs(parts_sum - stated_total) <= 1.0:
                return None  # a nearby list reconciles — treat as clean
            # Genuine adjacent-but-non-reconciling itemization -> bug.
            return (
                "explicitly itemized amounts do not sum to stated total",
                {"parts": parts, "stated_total": stated_total,
                 "parts_sum": parts_sum},
            )
    return None


# A "bond" used as a financial INSTRUMENT (not a surname / firm name like
# "Womble Bond Dickinson"): the word must sit next to fixing/posting/amount
# language or be glued to the dollar figure.
BOND_INSTRUMENT_RE = re.compile(
    r"\bbond\s+(?:is\s+)?(?:fixed|set|in\s+the\s+amount|required|of)\b"
    r"|(?:post(?:ing)?|require(?:s|d)?|fix(?:ed|es)?|set)\s+(?:a\s+)?bond\b"
    r"|" + DOLLAR_AMOUNT_PATTERN + r"\s+bond\b",
    re.IGNORECASE)


def context_fit_bug(amount, kind, context):
    """Flag an amount that plainly doesn't fit its context.

    Conservative single check today: the SAME amount is described as both a
    'sanction' and a (genuine, instrument-sense) 'bond' inside one tight
    snippet — a real kind conflict we surface rather than silently choosing.
    We require an instrument-sense bond match (not a firm/surname containing
    "Bond") to avoid false positives like "Womble Bond Dickinson LLP".
    Returns a reason string or None.
    """
    if kind not in ("sanctions", "bond"):
        return None
    has_sanction = bool(re.search(r"\bsanction", context, re.IGNORECASE))
    has_bond = bool(BOND_INSTRUMENT_RE.search(context))
    if has_sanction and has_bond:
        return "amount sits between conflicting 'sanction' and 'bond' cues"
    return None


# ===========================================================================
# Per-ruling extraction
# ===========================================================================
def extract_from_ruling(case_number, court_date, text, bug_sink):
    """Yield extraction records for one ruling; append bug reports to bug_sink."""
    records = []
    amounts_in_ruling = []
    kinds_in_ruling = []

    for hit in iter_amount_mentions(text):
        raw_full = hit["raw"]
        s = max(0, hit["start"] - CONTEXT_PAD)
        e = min(len(text), hit["end"] + CONTEXT_PAD)
        context = text[s:e]

        if is_noise(raw_full, context):
            continue  # filtered as noise, not bug-reported

        amount = hit["amount"]
        # Drop absurd magnitudes that are almost certainly typos/citations
        # (e.g. a statute "$9,000,000" inside a fee-schedule recitation is real,
        # so we keep large numbers; we only guard against zero).
        if amount <= 0:
            continue

        kind = classify_kind(context)
        # Direction relative to the amount's position INSIDE the snippet.
        local_span = (hit["start"] - s, hit["end"] - s)
        direction, parties = detect_direction(context, local_span)

        confidence = classify_confidence(kind, direction, parties)

        # Per-amount context-fit conflict check.
        fit_reason = context_fit_bug(amount, kind, context)
        if fit_reason:
            bug_sink.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "case_number": case_number,
                "court_date": court_date,
                "amounts": [amount],
                "kinds": [kind],
                "reason": fit_reason,
                "context": context,
            })

        records.append({
            "case_number": case_number,
            "court_date": court_date,
            "amount": amount,
            "kind": kind,
            "direction": direction,
            "parties": parties,
            "context": context,
            "source": "tentative",
            "confidence": confidence,
        })
        amounts_in_ruling.append(amount)
        kinds_in_ruling.append(kind)

    # Whole-ruling reconciliation check.
    if len(amounts_in_ruling) >= 3:
        bug = reconciliation_bug(amounts_in_ruling, kinds_in_ruling, text)
        if bug:
            reason, detail = bug
            bug_sink.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "case_number": case_number,
                "court_date": court_date,
                "amounts": amounts_in_ruling,
                "kinds": kinds_in_ruling,
                "reason": reason,
                "detail": detail,
                "context": text[:400],
            })

    return records


# ===========================================================================
# Driver
# ===========================================================================
def run(args):
    df = load_rulings(department=args.department, limit=args.limit)
    bug_sink = []
    all_records = []
    for _, row in df.iterrows():
        recs = extract_from_ruling(
            str(row["case_number"]),
            str(row["court_date"]),
            str(row["_text"]),
            bug_sink,
        )
        all_records.extend(recs)

    out_df = pd.DataFrame(all_records, columns=[
        "case_number", "court_date", "amount", "kind", "direction",
        "parties", "context", "source", "confidence",
    ])

    write_parquet_atomic(args.out, out_df)

    # Append bug reports (ndjson — one object per line).
    if bug_sink:
        os.makedirs(os.path.dirname(os.path.abspath(args.bug_report)),
                    exist_ok=True)
        rotate_if_large(args.bug_report)
        with open(args.bug_report, "a") as fh:
            for b in bug_sink:
                fh.write(json.dumps(b, default=str) + "\n")

    _report(out_df, bug_sink, args)


def _report(out_df, bug_sink, args):
    print(f"Scanned rulings -> extracted {len(out_df)} amount(s) into "
          f"{args.out}")
    if out_df.empty:
        print("No monetary figures found.")
        return
    dist = out_df["kind"].value_counts()
    print("\nkind distribution:")
    for kind, n in dist.items():
        subtotal = out_df.loc[out_df["kind"] == kind, "amount"].sum()
        print(f"  {kind:<24} {n:>6}   ${subtotal:,.2f}")
    print(f"\ntotal $ found: ${out_df['amount'].sum():,.2f}")
    print("confidence:", dict(out_df["confidence"].value_counts()))

    print("\nexample extractions:")
    for _, r in out_df.head(5).iterrows():
        print(f"  ${r['amount']:,.2f}  [{r['kind']}]  dir={r['direction']}  "
              f"parties={r['parties']}")
        print(f"      …{r['context'].strip()[:140]}…")

    print(f"\nbug reports appended: {len(bug_sink)} -> {args.bug_report}")
    if bug_sink:
        b = bug_sink[0]
        print("  example bug report:")
        print("   ", json.dumps(b, default=str)[:300])


def build_parser():
    p = argparse.ArgumentParser(
        description="Extract & classify monetary figures from SFSC tentative-"
                    "ruling text. PROTOTYPE; pandas + stdlib only; no network.")
    p.add_argument("--out", default=DEFAULT_OUT,
                   help="Output parquet path (default data/financials.parquet).")
    p.add_argument("--bug-report", default=DEFAULT_BUG_REPORT,
                   help="NDJSON bug-report log path "
                        "(default data/financials_bug_reports.ndjson).")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N rulings (after the money prefilter).")
    p.add_argument("--department", default=None,
                   help="Restrict to one department (e.g. 302) and read its "
                        "per-department parquet.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
