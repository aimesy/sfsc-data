#!/usr/bin/env python3
"""Reliable cause-of-action harvest from the docket + tentatives (no complaint needed).

The full pleaded list of causes of action normally comes from the OCR'd complaint
caption (the `cause` namespace source of truth). For the many cases where we have
no complaint document, this module recovers causes from signals that are RELIABLE
because they are the court's / parties' own precise phrasing — an ORDINAL tied to
a NAMED cause:

    "... the SIXTH CAUSE OF ACTION FOR FRAUD BY OMISSION ..."
    "... Demurrer to the SEVENTH CAUSE OF ACTION FOR ACTUAL FRAUD ..."
    "Complaint for: (1) Breach of Contract; (2) Fraud; ..."

Such phrasing appears in ROA/docket entries, filed-document titles, and tentative
rulings, and is selectable with a tight regex at high precision. It recovers the
LITIGATED causes (those demurred to, summarily adjudicated, stipulated, decreed),
which is a subset of the pleaded list but reliable for what it names; and the
MAX ordinal seen is a reliable lower bound on how many causes were pleaded.

What this is NOT: it does not invent a full pleaded list from a generic category
or a bare "Complaint" entry. Every row keeps the verbatim matched snippet and its
source tier (docket / document = high authority; tentative = lower, dotted), in
line with tag_registry.json's cause-namespace provenance (ocr > docket > tentative).

Output: data/causes.parquet — one row per (case, cause) reliable mention.
Pure classifiers (parse_named_causes, cause_slug) are stdlib-only and tested in
scripts/check_cause_extract.py.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_case_tables as bct  # noqa: E402  (clean, first_text, atomic parquet writer)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CASE_DIR = os.path.join(REPO, "archive", "cases")
DEFAULT_TENTATIVES_GLOB = os.path.join(REPO, "data", "tentatives-*.parquet")
DEFAULT_OUT = os.path.join(REPO, "data", "causes.parquet")

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20,
}
_ORD_RE = "|".join(_ORDINALS) + r"|\d{1,2}(?:st|nd|rd|th)"

# Clause boundaries where a captured cause name ends (so one match does not
# swallow the NEXT "... cause of action for ..." in the same sentence).
_STOP_ALT = (r"against|is|are|was|were|granted|denied|sustained|overruled|continued|"
             r"dismissed|withdrawn|moot|in\s+(?:the|plaintiff|defendant|count)|by|"
             r"as\s+to|under|pursuant|filed|on\s+behalf|because|fails?|do(?:es)?\s+not|"
             r"alleg\w*|asserted|brought|set\s+forth")
_NAME_BOUNDARY = (rf"(?=$|[.;,)\"]|\s+and\s+(?:the\s+)?(?:{_ORD_RE})\s+cause|"
                  rf"\bcause\s+of\s+action|\s+(?:{_STOP_ALT})\b)")

# "[ordinal] cause of action for <NAME>" — the reliable, court-authored form.
# Name is LAZY and bounded so it stops at the next clause / next cause.
NAMED_CAUSE_RE = re.compile(
    rf"\b(?P<ord>{_ORD_RE})\s+(?:and\s+(?:{_ORD_RE})\s+)?cause(?:s)?\s+of\s+action\s+for\s+"
    rf"(?P<name>[A-Za-z][A-Za-z0-9 '’/&.\-]{{2,70}}?)" + _NAME_BOUNDARY,
    re.IGNORECASE,
)
# Bare ordinal (no name) — used only to bound the COUNT of causes, never to name one.
BARE_ORDINAL_RE = re.compile(rf"\b(?P<ord>{_ORD_RE})\s+cause(?:s)?\s+of\s+action\b", re.IGNORECASE)

# Trailing clause that is not part of the cause name; cut the captured name here.
# NOTE: do NOT stop on a bare "of" — causes legitimately contain it ("breach OF
# contract", "violation OF the unfair competition law", "breach OF the implied
# covenant"). Only stop at genuine clause boundaries.
_NAME_STOP_RE = re.compile(
    r"\s+(?:against\b|is\b|are\b|was\b|were\b|in\s+(?:the|plaintiff|defendant|count)\b|"
    r"granted\b|denied\b|sustained\b|overruled\b|continued\b|dismissed\b|withdrawn\b|moot\b|"
    r"by\b|as\s+to\b|under\b|pursuant\b|filed\b|on\s+behalf\b|because\b|fails?\b|"
    r"do(?:es)?\s+not\b|and\s+(?:the\s+)?(?:" + _ORD_RE + r")\s+cause\b|"
    r"alleg|asserted\b|brought\b|set\s+forth\b)",
    re.IGNORECASE,
)

# Map a raw cause name to the tag_registry `cause` slug where it matches. Ordered;
# first hit wins. Values mirror tag_registry.json plus common, well-defined causes.
CAUSE_VOCAB: list[tuple[str, re.Pattern[str]]] = [
    ("administrative-mandate", re.compile(r"administrative\s+mandate|1094\.5", re.I)),
    ("writ-of-mandate", re.compile(r"writ\s+of\s+(?:administrative\s+)?mandate|writ\s+of\s+mandamus|mandamus", re.I)),
    ("writ-of-prohibition", re.compile(r"writ\s+of\s+prohibition|prohibition", re.I)),
    ("writ-of-certiorari", re.compile(r"writ\s+of\s+certiorari|certiorari", re.I)),
    ("breach-of-fiduciary-duty", re.compile(r"fiduciary", re.I)),
    ("breach-of-implied-covenant", re.compile(r"implied\s+covenant|good\s+faith\s+and\s+fair\s+dealing", re.I)),
    ("breach-of-contract", re.compile(r"breach\s+of\s+(?:written\s+|oral\s+|implied\s+|the\s+)?contract|breach\s+of\s+(?:written\s+|oral\s+|written\s+and\s+oral\s+)?agreements?", re.I)),
    ("breach-of-warranty", re.compile(r"breach\s+of\s+(?:the\s+)?(?:written\s+|express\s+|implied\s+|written\s+express\s+)?warranty", re.I)),
    ("negligent-misrepresentation", re.compile(r"negligent\s+misrepresentation", re.I)),
    ("intentional-misrepresentation", re.compile(r"intentional\s+misrepresentation", re.I)),
    ("fraud", re.compile(r"\bfraud|deceit|concealment|misrepresentation", re.I)),
    ("negligent-infliction", re.compile(r"negligent\s+infliction", re.I)),
    ("intentional-infliction", re.compile(r"intentional\s+infliction|\bIIED\b", re.I)),
    ("premises-liability", re.compile(r"premises\s+liability", re.I)),
    ("strict-liability", re.compile(r"strict\s+(?:products?\s+)?liability", re.I)),
    ("professional-negligence", re.compile(r"professional\s+negligence|legal\s+malpractice|medical\s+malpractice", re.I)),
    ("negligent-hiring", re.compile(r"negligent\s+(?:hiring|supervision|retention)", re.I)),
    ("negligence", re.compile(r"\bnegligence\b", re.I)),
    ("promissory-estoppel", re.compile(r"promissory\s+estoppel", re.I)),
    ("invasion-of-privacy", re.compile(r"invasion\s+of\s+privacy|right\s+(?:to|of)\s+privacy", re.I)),
    ("battery", re.compile(r"\bbattery\b", re.I)),
    ("assault", re.compile(r"\bassault\b", re.I)),
    ("rescission", re.compile(r"\brescission\b", re.I)),
    ("quantum-meruit", re.compile(r"quantum\s+meruit", re.I)),
    ("loss-of-consortium", re.compile(r"loss\s+of\s+consortium", re.I)),
    ("wrongful-eviction", re.compile(r"wrongful\s+eviction", re.I)),
    ("partition", re.compile(r"\bpartition\b", re.I)),
    ("wrongful-termination", re.compile(r"wrongful\s+(?:termination|discharge)", re.I)),
    ("retaliation", re.compile(r"\bretaliation\b", re.I)),
    ("discrimination", re.compile(r"\bdiscrimination\b|\bFEHA\b|harassment", re.I)),
    ("conversion", re.compile(r"\bconversion\b", re.I)),
    ("defamation", re.compile(r"defamation|libel|slander", re.I)),
    ("elder-abuse", re.compile(r"elder\s+abuse|elder\s+(?:financial\s+)?abuse", re.I)),
    ("quiet-title", re.compile(r"quiet\s+title", re.I)),
    ("constructive-trust", re.compile(r"constructive\s+trust", re.I)),
    ("resulting-trust", re.compile(r"resulting\s+trust", re.I)),
    ("breach-of-trust", re.compile(r"breach\s+of\s+trust|participation\s+in\s+breach\s+of\s+trust", re.I)),
    ("trespass", re.compile(r"\btrespass\b", re.I)),
    ("nuisance", re.compile(r"\bnuisance\b", re.I)),
    ("unfair-competition", re.compile(r"unfair\s+(?:business\s+)?(?:competition|practices)|\bUCL\b|17200", re.I)),
    ("unjust-enrichment", re.compile(r"unjust\s+enrichment", re.I)),
    ("declaratory-relief", re.compile(r"declaratory\s+relief", re.I)),
    ("injunctive-relief", re.compile(r"injunctive\s+relief|\binjunction\b|permanent\s+injunction", re.I)),
    ("specific-performance", re.compile(r"specific\s+performance", re.I)),
    ("accounting", re.compile(r"\baccounting\b", re.I)),
    ("breach-of-lease", re.compile(r"breach\s+of\s+lease", re.I)),
    ("indemnity", re.compile(r"\bindemnit|contribution\b", re.I)),
    ("interference", re.compile(r"\binterference\b", re.I)),
    ("conspiracy", re.compile(r"\bconspiracy\b", re.I)),
    ("common-counts", re.compile(r"common\s+counts|open\s+book\s+account|account\s+stated|money\s+(?:had|lent)", re.I)),
    ("wage-and-hour", re.compile(r"labor\s+code|wage|overtime|meal\s+(?:and\s+rest\s+)?break|PAGA", re.I)),
]


def _ord_num(token: str) -> int | None:
    t = token.lower()
    if t in _ORDINALS:
        return _ORDINALS[t]
    m = re.match(r"(\d{1,2})", t)
    return int(m.group(1)) if m else None


def clean_cause_name(raw: str) -> str:
    """Trim a captured cause name to the cause itself (drop trailing clauses)."""
    text = bct.clean(raw).strip(" '\"")
    m = _NAME_STOP_RE.search(text)
    if m:
        text = text[: m.start()]
    text = re.sub(r"\s+(?:and|or)$", "", text, flags=re.IGNORECASE)
    text = text.strip(" '\".,;:-")
    # Drop a dangling leading article.
    text = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.IGNORECASE).strip()
    return text


def cause_slug(name: str) -> tuple[str, bool]:
    """(registry slug, mapped?) for a cause name; slugified raw when unmapped."""
    for slug, pat in CAUSE_VOCAB:
        if pat.search(name):
            return slug, True
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug[:48] or "unknown"), False


def parse_named_causes(text: str) -> list[dict[str, Any]]:
    """Reliable (ordinal, name, slug) causes named in a single text blob."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    blob = bct.clean(text)
    if not blob:
        return out
    for m in NAMED_CAUSE_RE.finditer(blob):
        name = clean_cause_name(m.group("name"))
        if len(name) < 3:
            continue
        slug, mapped = cause_slug(name)
        if slug in seen:
            continue
        seen.add(slug)
        out.append({
            "ordinal": _ord_num(m.group("ord")), "cause_name": name,
            "cause_slug": slug, "mapped": mapped,
            "matched_text": bct.clean(m.group(0))[:160],
        })
    return out


def max_cause_ordinal(text: str) -> int:
    """Highest cause-of-action ordinal mentioned (a lower bound on # of causes)."""
    best = 0
    for m in BARE_ORDINAL_RE.finditer(bct.clean(text)):
        n = _ord_num(m.group("ord"))
        if n and n > best:
            best = n
    return best


# ---------------------------------------------------------------------------
def _norm(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def harvest(case_dir: str, tentatives_glob: str, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(case_number: str, source: str, hit: dict[str, Any]) -> None:
        key = (case_number, hit["cause_slug"], source)
        if key in seen:
            return
        seen.add(key)
        rows.append({"case_number": case_number, "source": source, **hit})

    files = sorted(glob.glob(os.path.join(case_dir, "*.json")))
    if limit:
        files = files[:limit]
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        cn = _norm(d.get("case_number") or os.path.splitext(os.path.basename(f))[0])
        if not cn:
            continue
        for e in d.get("docket_entries") or []:
            for hit in parse_named_causes(bct.first_text(e, "description", "RTEXT", "text", "title")):
                add(cn, "docket", hit)
        for doc in d.get("documents") or []:
            for hit in parse_named_causes(doc.get("description") or ""):
                add(cn, "document", hit)

    if tentatives_glob:
        import pandas as pd
        for path in glob.glob(tentatives_glob):
            if "extras" in os.path.basename(path):
                continue
            cols = pd.read_parquet(path).columns
            want = [c for c in ("case_number", "calendar_matter", "ruling", "ruling_substantive") if c in cols]
            df = pd.read_parquet(path, columns=want)
            for rec in df.to_dict("records"):
                cn = _norm(rec.get("case_number"))
                if not cn:
                    continue
                text = f"{rec.get('calendar_matter') or ''} || {rec.get('ruling_substantive') or rec.get('ruling') or ''}"
                for hit in parse_named_causes(text):
                    add(cn, "tentative", hit)

    rows.sort(key=lambda r: (r["case_number"], r.get("ordinal") or 99, r["cause_slug"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case-dir", default=DEFAULT_CASE_DIR)
    ap.add_argument("--tentatives-glob", default=DEFAULT_TENTATIVES_GLOB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    rows = harvest(args.case_dir, args.tentatives_glob, args.limit)
    cases = {r["case_number"] for r in rows}
    by_source = Counter(r["source"] for r in rows)
    mapped = sum(1 for r in rows if r["mapped"])
    print(f"reliable cause mentions: {len(rows)} across {len(cases)} cases")
    print(f"  by source: {dict(by_source)}")
    print(f"  mapped to registry vocab: {mapped}/{len(rows)}")
    top = Counter(r["cause_slug"] for r in rows).most_common(15)
    print(f"  top causes: {top}")
    if not args.no_write:
        import pandas as pd
        from pathlib import Path
        bct.write_parquet_atomic(Path(args.out), pd.DataFrame(rows))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
