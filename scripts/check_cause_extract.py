#!/usr/bin/env python3
"""Checks for the reliable cause-of-action harvester (scripts/cause_extract.py).

Pure classifiers on synthetic fixtures only. Run: python scripts/check_cause_extract.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cause_extract as ce

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def one(text):
    hits = ce.parse_named_causes(text)
    return hits[0] if hits else None


def test_named_parse() -> None:
    print("\nnamed ordinal cause parsing")
    h = one("Defendant's Demurrer to the SIXTH CAUSE OF ACTION FOR FRAUD BY OMISSION is continued")
    check("ordinal captured", h and h["ordinal"] == 6, str(h))
    check("fraud mapped", h and h["cause_slug"] == "fraud" and h["mapped"], str(h))
    h = one("first cause of action for breach of contract against defendant Ultratick is overruled")
    check("trailing 'against...' trimmed", h and h["cause_name"] == "breach of contract", str(h))
    check("breach-of-contract slug", h and h["cause_slug"] == "breach-of-contract", str(h))
    h = one("fifth cause of action for violation of the unfair competition law")
    check("'of the' not truncated", h and "unfair competition" in h["cause_name"], str(h))
    check("UCL maps to unfair-competition", h and h["cause_slug"] == "unfair-competition", str(h))
    h = one("first cause of action for breach of the implied covenant of good faith and fair dealing")
    check("implied covenant mapped", h and h["cause_slug"] == "breach-of-implied-covenant", str(h))
    h = one("second cause of action for willful failure to warn is granted")
    check("unmapped cause kept raw + flagged",
          h and not h["mapped"] and h["cause_name"] == "willful failure to warn", str(h))
    h = one("third cause of action for writ of mandamus is denied")
    check("writ of mandamus maps to writ-of-mandate",
          h and h["cause_slug"] == "writ-of-mandate" and h["mapped"], str(h))
    h = one("fourth cause of action for administrative mandate under Code Civ. Proc. section 1094.5")
    check("administrative mandate mapped",
          h and h["cause_slug"] == "administrative-mandate" and h["mapped"], str(h))
    h = one("fifth cause of action for breach of written and oral agreements is overruled")
    check("breach of written/oral agreements maps to contract and trims ruling word",
          h and h["cause_slug"] == "breach-of-contract" and h["cause_name"] == "breach of written and oral agreements",
          str(h))
    h = one("sixth cause of action for breach of written express warranty is overruled")
    check("written express warranty mapped",
          h and h["cause_slug"] == "breach-of-warranty" and h["mapped"], str(h))
    h = one("seventh cause of action for breach of the implied warranty is sustained")
    check("'the implied warranty' mapped",
          h and h["cause_slug"] == "breach-of-warranty" and h["mapped"], str(h))


def test_reliability_guards() -> None:
    print("\nreliability guards")
    # A bare ordinal with no "for <name>" must NOT yield a named cause...
    check("bare ordinal yields no name",
          ce.parse_named_causes("Demurrer to the EIGHTH CAUSE OF ACTION is sustained") == [])
    # ...but it DOES set a reliable count lower bound.
    check("bare ordinal still bounds count",
          ce.max_cause_ordinal("Demurrer to the EIGHTH CAUSE OF ACTION is sustained") == 8)
    # Generic prose that never says "cause of action for X" yields nothing.
    check("non-cause prose yields nothing",
          ce.parse_named_causes("Motion to compel further responses is granted") == [])
    # Numeric ordinal form works.
    h = one("Demurrer to the 7th cause of action for conversion")
    check("numeric ordinal parsed", h and h["ordinal"] == 7 and h["cause_slug"] == "conversion", str(h))


def test_multi_cause() -> None:
    print("\nmultiple causes in one blob")
    text = ("Demurrer to the first cause of action for breach of contract and the "
            "second cause of action for fraud is sustained as to the second cause of action.")
    slugs = {h["cause_slug"] for h in ce.parse_named_causes(text)}
    check("both causes captured", {"breach-of-contract", "fraud"} <= slugs, str(slugs))
    check("max ordinal = 2", ce.max_cause_ordinal(text) == 2)


def main() -> int:
    test_named_parse()
    test_reliability_guards()
    test_multi_cause()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("cause_extract checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
