#!/usr/bin/env python3
"""Checks for the derived metrics layer (scripts/derive_metrics.py).

Pure classifiers + roll-ups on synthetic fixtures only (no court access, no
parquet I/O required). Run: python scripts/check_derive_metrics.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import derive_metrics as dm

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


def test_dismissal() -> None:
    section("dismissal classifier")
    d = dm.classify_dismissal("Request for Dismissal with Prejudice - Entire Action")
    check("with-prejudice voluntary", d and d["signal"] == "dismissal_voluntary_with_prejudice", str(d))
    d = dm.classify_dismissal("Voluntary dismissal without prejudice filed")
    check("without-prejudice voluntary", d and d["prejudice"] == "without" and d["voluntariness"] == "voluntary", str(d))
    d = dm.classify_dismissal("Order of Dismissal for failure to prosecute")
    check("involuntary detected", d and d["voluntariness"] == "involuntary", str(d))
    check("motion to dismiss DENIED is not a dismissal",
          dm.classify_dismissal("Motion to Dismiss is DENIED") is None)
    check("plain text is not a dismissal", dm.classify_dismissal("Case management conference held") is None)


def test_appellate() -> None:
    section("appellate classifier")
    check("reversed", dm.classify_appellate("Judgment REVERSED")["signal"] == "reversed")
    check("affirmed", dm.classify_appellate("Judgment AFFIRMED on appeal")["signal"] == "affirmed")
    check("both -> mixed", dm.classify_appellate("Affirmed in part and reversed in part")["signal"]
          == "affirmed_in_part_reversed_in_part")
    check("reversed in part", dm.classify_appellate("Order reversed in part")["signal"] == "reversed_in_part")
    check("remittitur", dm.classify_appellate("Remittitur filed")["signal"] == "remittitur")
    check("notice of appeal", dm.classify_appellate("Notice of Appeal filed")["signal"] == "notice_of_appeal")
    check("appeal dismissed", dm.classify_appellate("Appeal is dismissed")["signal"] == "appeal_dismissed")
    check("writ petition filed", dm.classify_appellate("Petition for Writ of Mandate filed by petitioner")["signal"]
          == "writ_petition_filed")
    check("writ denied", dm.classify_appellate("Order denying petition for writ of mandate")["signal"]
          == "writ_denied")
    check("writ granted", dm.classify_appellate("Order granting petition for writ of prohibition")["signal"]
          == "writ_granted")
    check("alternative writ", dm.classify_appellate("Alternative writ of mandate issued")["signal"]
          == "alternative_writ_issued")
    check("peremptory writ", dm.classify_appellate("Peremptory writ of mandate issued")["signal"]
          == "peremptory_writ_issued")
    check("enforcement writ excluded", dm.classify_appellate("Writ of possession issued") is None)
    check("non-appellate -> None", dm.classify_appellate("Answer filed") is None)


def test_judgment() -> None:
    section("judgment classifier")
    check("default judgment", dm.classify_judgment("Default Judgment entered")["signal"] == "default_judgment")
    check("entry of judgment", dm.classify_judgment("Notice of Entry of Judgment")["signal"] == "judgment_entered")
    check("settled", dm.classify_judgment("Notice of Settlement of Entire Case")["signal"] == "settled")
    check("non-judgment -> None", dm.classify_judgment("Proof of service filed") is None)


def test_valence() -> None:
    section("abstract valence")
    check("with-prejudice voluntary -> resolved",
          dm.abstract_valence("dismissal_voluntary_with_prejudice") == "resolved")
    check("without-prejudice voluntary -> refile",
          dm.abstract_valence("dismissal_voluntary_without_prejudice") == "tentative_refile")
    check("reversed -> adverse to prevailing below",
          dm.abstract_valence("reversed") == "adverse_to_prevailing_below")
    check("affirmed -> favorable to prevailing below",
          dm.abstract_valence("affirmed") == "favorable_to_prevailing_below")
    check("writ denied -> writ_denied",
          dm.abstract_valence("writ_denied") == "writ_denied")
    check("alternative writ -> review proceeding",
          dm.abstract_valence("alternative_writ_issued") == "writ_review_proceeding")


def test_motion_type_and_family() -> None:
    section("motion type + disposition family")
    check("demurrer", dm.classify_motion_type("Hearing on Demurrer") == "demurrer")
    check("summary judgment", dm.classify_motion_type("Motion for Summary Judgment") == "summary_judgment")
    check("anti-slapp", dm.classify_motion_type("Special Motion to Strike (CCP 425.16)") == "anti_slapp")
    check("compel", dm.classify_motion_type("Motion to Compel Further Responses") == "motion_to_compel")
    check("writ petition", dm.classify_motion_type("Petition for Writ of Administrative Mandate") == "writ_petition")
    check("blank -> other", dm.classify_motion_type("") == "other")
    check("granted -> grant family", dm.disposition_family("Granted") == "grant")
    check("overruled -> deny family", dm.disposition_family("Overruled") == "deny")
    check("granted in part -> partial", dm.disposition_family("Granted in part") == "partial")
    check("continued -> procedural", dm.disposition_family("Continued") == "procedural")


def test_clean_judge() -> None:
    section("judge NaN handling")
    check("nan -> empty", dm._clean_judge("nan") == "")
    check("float nan -> empty", dm._clean_judge(float("nan")) == "")
    check("real name kept", dm._clean_judge("Ronald Evans Quidachay") == "Ronald Evans Quidachay")


def test_case_outcome_signals() -> None:
    section("per-case outcome aggregation")
    entries = [
        {"description": "Notice of Settlement of Entire Case", "date_filed": "2020-01-01"},
        {"description": "Request for Dismissal with Prejudice", "date_filed": "2020-02-01"},
        {"description": "Notice of Appeal filed", "date_filed": "2020-03-01"},
        {"description": "Remittitur: judgment AFFIRMED", "date_filed": "2021-01-01"},
        {"description": "Order denying petition for writ of mandate", "date_filed": "2021-02-01"},
        {"description": "Case management conference", "date_filed": "2020-04-01"},
    ]
    sigs = {s["signal"] for s in dm.case_outcome_signals(entries)}
    check("settled found", "settled" in sigs)
    check("dismissal w/prej found", "dismissal_unspecified_with_prejudice" in sigs
          or "dismissal_voluntary_with_prejudice" in sigs, str(sigs))
    check("appeal found", "notice_of_appeal" in sigs)
    check("affirmed found", "affirmed" in sigs, str(sigs))
    check("writ denial found", "writ_denied" in sigs, str(sigs))


def _disp(officer, motion, disposition, family, dept="302"):
    return {"officer": officer, "department": dept,
            "calendar_context": dm.calendar_context(dept),
            "assignment_regime": dm.assignment_regime(dept), "motion_type": motion,
            "disposition": disposition, "family": family}


def test_officer_types_and_context() -> None:
    section("officer type + calendar context + assignment regime (master vs direct)")
    check("dept 204 -> probate context", dm.calendar_context("204") == "probate")
    check("dept 302 -> law_and_motion", dm.calendar_context("302") == "law_and_motion")
    check("dept 304 -> complex_civil", dm.calendar_context("304") == "complex_civil")
    check("dept 501 -> real_property", dm.calendar_context("501") == "real_property")
    check("dept 206 -> master_calendar", dm.calendar_context("206") == "master_calendar")
    check("unknown dept -> other", dm.calendar_context("999") == "other")
    # assignment regime: master-calendar L&M vs direct/all-purpose.
    check("302 regime = master-calendar L&M",
          dm.assignment_regime("302") == "master_calendar_law_and_motion")
    check("304 (complex) regime = direct_calendar", dm.assignment_regime("304") == "direct_calendar")
    check("501 (real property) regime = direct_calendar", dm.assignment_regime("501") == "direct_calendar")
    check("204 (probate) regime = direct_calendar", dm.assignment_regime("204") == "direct_calendar")
    check("civil officer -> judge_or_commissioner",
          dm._officer_type("Jane Judge", ["302"]) == "judge_or_commissioner")
    check("dept 204 officer -> probate_examiner",
          dm._officer_type("Helen Examiner", ["204"]) == "probate_examiner")
    check("unknown probate+civil officer -> mixed_officer",
          dm._officer_type("Unknown Officer", ["204", "302"]) == "mixed_officer")
    roster_judges = {dm.officer_match_key("Harold E. Kahn")}
    check("roster judge probate+civil override -> judge_or_commissioner",
          dm._officer_type("Harold E. Kahn", ["204", "302"], roster_judges) == "judge_or_commissioner")
    check("pro tem detected from name",
          dm._officer_type("Judge Pro Tem: Pat Volunteer", ["302"]) == "judge_pro_tempore")


def test_officer_metrics() -> None:
    section("judicial-officer metrics roll-up (judges + examiners + pro tems)")
    disp = []
    # Officer A (civil dept 302): demurrers mostly granted (8 grant, 2 deny) -> 0.8.
    disp += [_disp("Judge A", "demurrer", "Sustained", "grant")] * 8
    disp += [_disp("Judge A", "demurrer", "Overruled", "deny")] * 2
    # Officer B (civil): demurrers mostly denied (1 grant, 4 deny) -> 0.2
    disp += [_disp("Judge B", "demurrer", "Sustained", "grant")]
    disp += [_disp("Judge B", "demurrer", "Overruled", "deny")] * 4
    # Probate EXAMINER (dept 204): petitions granted -> tagged probate_examiner
    disp += [_disp("Helen Examiner", "petition", "Granted", "grant", dept="204")] * 6
    # officer-less rows must be ignored
    disp += [_disp("", "demurrer", "Granted", "grant")] * 5
    J = dm.officer_metrics(disp)
    check("blank officer excluded", "" not in J and len(J) == 3, str(list(J)))
    check("Judge A demurrer grant_rate 0.8", J["Judge A"]["by_motion"]["demurrer"]["grant_rate"] == 0.8)
    check("Judge B demurrer grant_rate 0.2", J["Judge B"]["by_motion"]["demurrer"]["grant_rate"] == 0.2)
    check("civil officer typed judge_or_commissioner", J["Judge A"]["officer_type"] == "judge_or_commissioner")
    check("dept-204 officer typed probate_examiner", J["Helen Examiner"]["officer_type"] == "probate_examiner")
    check("departments recorded", J["Helen Examiner"]["departments"] == ["204"])
    check("calendar context recorded", J["Helen Examiner"]["calendar_contexts"] == ["probate"])
    check("L&M officer scope = per-order not trial",
          J["Judge A"]["ruling_scope"] == "law_and_motion_per_order_not_trial_judge")
    check("L&M officer regime recorded",
          J["Judge A"]["assignment_regimes"] == ["master_calendar_law_and_motion"])
    check("probate examiner scope = assigned direct calendar",
          J["Helen Examiner"]["ruling_scope"] == "assigned_judge_or_examiner_direct_calendar")
    # A motion with <3 decided is not published as a rate.
    Jc = dm.officer_metrics([_disp("Judge C", "motion_to_quash", "Granted", "grant")] * 2)
    check("rate suppressed below 3 rulings", "motion_to_quash" not in Jc["Judge C"]["by_motion"], str(Jc["Judge C"]))
    Jd = dm.officer_metrics(
        [_disp("Known Judge", "petition", "Granted", "grant", dept="204")] * 3
        + [_disp("Known Judge", "demurrer", "Overruled", "deny", dept="302")] * 3,
        {dm.officer_match_key("Known Judge")},
    )
    check("roster judge metrics override mixed departments",
          Jd["Known Judge"]["officer_type"] == "judge_or_commissioner",
          Jd["Known Judge"]["officer_type"])


def test_attorney_metrics() -> None:
    section("attorney metrics roll-up")
    tables = {"representation": [
        {"case_number": "C1", "attorney_id": "bar:1", "attorney_name": "DOE, JANE", "party_type": "Defendant"},
        {"case_number": "C2", "attorney_id": "bar:1", "attorney_name": "DOE, JANE", "party_type": "Defendant"},
        {"case_number": "C3", "attorney_id": "bar:2", "attorney_name": "ROE, JOHN", "party_type": "Plaintiff"},
    ]}
    outcomes = [
        {"case_number": "C1", "signal": "dismissal_unspecified_with_prejudice",
         "abstract_valence": "resolved"},
        {"case_number": "C2", "signal": "settled", "abstract_valence": "resolved"},
        {"case_number": "C3", "signal": "reversed", "abstract_valence": "adverse_to_prevailing_below"},
    ]
    A = dm.attorney_metrics(tables, outcomes)
    check("attorney bar:1 has 2 cases", A["bar:1"]["case_count"] == 2)
    check("defense favorable on resolved", A["bar:1"]["favorable"] == 2 and A["bar:1"]["favorable_rate"] == 1.0,
          str(A["bar:1"]))
    check("appellate reversal captured for bar:2", A["bar:2"]["appellate"].get("reversed") == 1, str(A["bar:2"]))


def main() -> int:
    test_dismissal()
    test_appellate()
    test_judgment()
    test_valence()
    test_motion_type_and_family()
    test_clean_judge()
    test_case_outcome_signals()
    test_officer_types_and_context()
    test_officer_metrics()
    test_attorney_metrics()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("derive_metrics checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
