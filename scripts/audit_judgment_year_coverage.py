#!/usr/bin/env python3
"""Compile deidentified year-level judgment parsing coverage diagnostics."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "dataverse" / "judgment-trends" / "year-audit"
OUT.mkdir(parents=True, exist_ok=True)

FOCAL_YEARS = (1991, 1992, 2006, 2011)
MIN_YEAR = 1988
MAX_YEAR = 2014

db = duckdb.connect()
summary_glob = str(ROOT / "data/judgments/summaries/*.parquet")
event_glob = str(ROOT / "data/judgments/parquet/*.parquet")

db.execute(f"""
CREATE OR REPLACE TEMP VIEW summaries AS
SELECT * FROM read_parquet('{summary_glob}', union_by_name=true);

CREATE OR REPLACE TEMP VIEW events AS
SELECT * FROM read_parquet('{event_glob}', union_by_name=true);

CREATE OR REPLACE TEMP VIEW original_events AS
SELECT
  s.case_number,
  s.case_prefix,
  coalesce(nullif(s.case_model, ''), 'unknown') AS case_model,
  try_cast(s.filing_year AS INTEGER) AS filing_year,
  try_cast(substr(e.entry_date, 1, 4) AS INTEGER) AS event_year,
  e.entry_date,
  e.entry_hash,
  e.event_kind,
  e.status,
  coalesce(nullif(e.extraction_period, ''), 'unknown') AS extraction_period,
  coalesce(nullif(e.source_field, ''), 'unknown') AS source_field,
  e.rule_id,
  s.original_judgment_total_amount AS summary_total_amount,
  e.total_amount AS event_total_amount,
  s.recorded_judgment_amount,
  s.recorded_judgment_amount_event_hash,
  try_cast(substr(re.entry_date, 1, 4) AS INTEGER) AS recorded_event_year,
  re.event_kind AS recorded_event_kind,
  coalesce(len(e.money_mentions), 0) AS money_mention_count,
  coalesce(e.source_text, '') AS source_text,
  regexp_matches(coalesce(e.source_text, ''), '\\$\\s*[0-9]') AS has_dollar_marker,
  regexp_matches(lower(coalesce(e.source_text, '')), '[0-9][0-9,. ]*\\s*(dollars?|usd)\\b') AS has_word_currency,
  regexp_matches(
    lower(coalesce(e.source_text, '')),
    '(judg|adjudg|award|recover|principal|amount|total)[^0-9]{0,40}[0-9][0-9,]*(\\.[0-9]{1,2})?'
  ) AS has_unmarked_numeric_context
FROM summaries s
JOIN events e
  ON e.case_number = s.case_number
 AND e.entry_hash = s.original_judgment_event_hash
LEFT JOIN events re
  ON re.case_number = s.case_number
 AND re.entry_hash = s.recorded_judgment_amount_event_hash
WHERE s.original_judgment_event_hash IS NOT NULL;

CREATE OR REPLACE TEMP VIEW original_coverage AS
SELECT *,
  (event_total_amount IS NOT NULL) AS parsed_total,
  (
    event_total_amount IS NOT NULL
    OR money_mention_count > 0
    OR has_dollar_marker
    OR has_word_currency
    OR has_unmarked_numeric_context
    OR event_kind IN ('monetary_judgment', 'default_judgment', 'amended_judgment')
  ) AS likely_monetary,
  CASE
    WHEN event_total_amount IS NOT NULL THEN 'parsed_total'
    WHEN money_mention_count > 0 THEN 'recognized_money_but_no_total'
    WHEN has_dollar_marker OR has_word_currency THEN 'explicit_currency_not_recognized'
    WHEN has_unmarked_numeric_context THEN 'unmarked_numeric_context'
    ELSE 'no_amount_signal'
  END AS amount_format_cohort
FROM original_events;

CREATE OR REPLACE TEMP VIEW operative_judgment_events AS
SELECT
  case_number,
  coalesce(nullif(case_model, ''), 'unknown') AS case_model,
  try_cast(filing_year AS INTEGER) AS filing_year,
  try_cast(substr(entry_date, 1, 4) AS INTEGER) AS event_year,
  event_kind,
  coalesce(nullif(extraction_period, ''), 'unknown') AS extraction_period,
  coalesce(nullif(source_field, ''), 'unknown') AS source_field,
  entry_hash,
  total_amount,
  coalesce(len(money_mentions), 0) AS money_mention_count,
  regexp_matches(coalesce(source_text, ''), '\\$\\s*[0-9]') AS has_dollar_marker,
  regexp_matches(lower(coalesce(source_text, '')), '[0-9][0-9,. ]*\\s*(dollars?|usd)\\b') AS has_word_currency,
  regexp_matches(
    lower(coalesce(source_text, '')),
    '(judg|adjudg|award|recover|principal|amount|total)[^0-9]{0,40}[0-9][0-9,]*(\\.[0-9]{1,2})?'
  ) AS has_unmarked_numeric_context
FROM events
WHERE status IN ('operative', 'superseding')
  AND event_kind IN (
    'judgment', 'amended_judgment', 'default_judgment', 'monetary_judgment',
    'possession', 'declaratory/injunctive', 'take_nothing'
  );
""")


def write_query_csv(name: str, sql: str) -> tuple[list[str], list[tuple]]:
    cur = db.execute(sql)
    columns = [item[0] for item in cur.description]
    rows = cur.fetchall()
    with (OUT / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return columns, rows


annual_columns, annual_rows = write_query_csv(
    "original-coverage-by-event-year.csv",
    f"""
    SELECT
      event_year,
      count(*) AS selected_original_events,
      count(*) FILTER (WHERE likely_monetary) AS likely_monetary_events,
      count(*) FILTER (WHERE parsed_total) AS parsed_total_count,
      count(*) FILTER (WHERE money_mention_count > 0) AS recognized_money_event_count,
      count(*) FILTER (WHERE money_mention_count > 0 AND NOT parsed_total)
        AS recognized_money_without_total_count,
      count(*) FILTER (
        WHERE NOT parsed_total AND money_mention_count = 0
          AND (has_dollar_marker OR has_word_currency)
      ) AS explicit_currency_not_recognized_count,
      count(*) FILTER (
        WHERE NOT parsed_total AND money_mention_count = 0
          AND NOT has_dollar_marker AND NOT has_word_currency
          AND has_unmarked_numeric_context
      ) AS unmarked_numeric_context_count,
      round(
        100.0 * count(*) FILTER (WHERE parsed_total)
        / nullif(count(*) FILTER (WHERE likely_monetary), 0),
        2
      ) AS parsed_total_pct_of_likely_monetary,
      median(try_cast(event_total_amount AS DECIMAL(38,2)))
        FILTER (WHERE parsed_total) AS median_parsed_total,
      count(DISTINCT case_model) AS represented_case_models,
      count(DISTINCT extraction_period) AS represented_extraction_periods,
      count(DISTINCT source_field) AS represented_source_fields
    FROM original_coverage
    WHERE event_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}
    GROUP BY event_year
    ORDER BY event_year
    """,
)

write_query_csv(
    "original-coverage-by-case-model.csv",
    f"""
    SELECT
      event_year,
      case_model,
      count(*) AS selected_original_events,
      count(*) FILTER (WHERE likely_monetary) AS likely_monetary_events,
      count(*) FILTER (WHERE parsed_total) AS parsed_total_count,
      count(*) FILTER (WHERE money_mention_count > 0 AND NOT parsed_total)
        AS recognized_money_without_total_count,
      count(*) FILTER (
        WHERE NOT parsed_total AND money_mention_count = 0
          AND (has_dollar_marker OR has_word_currency)
      ) AS explicit_currency_not_recognized_count,
      count(*) FILTER (
        WHERE NOT parsed_total AND money_mention_count = 0
          AND NOT has_dollar_marker AND NOT has_word_currency
          AND has_unmarked_numeric_context
      ) AS unmarked_numeric_context_count,
      round(
        100.0 * count(*) FILTER (WHERE parsed_total)
        / nullif(count(*) FILTER (WHERE likely_monetary), 0),
        2
      ) AS parsed_total_pct_of_likely_monetary,
      median(try_cast(event_total_amount AS DECIMAL(38,2)))
        FILTER (WHERE parsed_total) AS median_parsed_total
    FROM original_coverage
    WHERE event_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}
    GROUP BY event_year, case_model
    ORDER BY event_year, case_model
    """,
)

write_query_csv(
    "original-coverage-by-source.csv",
    f"""
    SELECT
      event_year,
      extraction_period,
      source_field,
      count(*) AS selected_original_events,
      count(*) FILTER (WHERE likely_monetary) AS likely_monetary_events,
      count(*) FILTER (WHERE parsed_total) AS parsed_total_count,
      count(*) FILTER (WHERE money_mention_count > 0 AND NOT parsed_total)
        AS recognized_money_without_total_count,
      count(*) FILTER (
        WHERE NOT parsed_total AND money_mention_count = 0
          AND (has_dollar_marker OR has_word_currency)
      ) AS explicit_currency_not_recognized_count,
      count(*) FILTER (
        WHERE NOT parsed_total AND money_mention_count = 0
          AND NOT has_dollar_marker AND NOT has_word_currency
          AND has_unmarked_numeric_context
      ) AS unmarked_numeric_context_count,
      round(
        100.0 * count(*) FILTER (WHERE parsed_total)
        / nullif(count(*) FILTER (WHERE likely_monetary), 0),
        2
      ) AS parsed_total_pct_of_likely_monetary
    FROM original_coverage
    WHERE event_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}
    GROUP BY event_year, extraction_period, source_field
    ORDER BY event_year, extraction_period, source_field
    """,
)

write_query_csv(
    "missing-amount-format-cohorts.csv",
    f"""
    SELECT
      event_year,
      case_model,
      extraction_period,
      source_field,
      amount_format_cohort,
      count(*) AS event_count
    FROM original_coverage
    WHERE event_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}
      AND NOT parsed_total
    GROUP BY event_year, case_model, extraction_period, source_field, amount_format_cohort
    ORDER BY event_year, event_count DESC, case_model, extraction_period, source_field,
      amount_format_cohort
    """,
)

write_query_csv(
    "operative-judgment-event-funnel.csv",
    f"""
    SELECT
      event_year,
      case_model,
      event_kind,
      extraction_period,
      source_field,
      count(*) AS operative_event_count,
      count(*) FILTER (WHERE total_amount IS NOT NULL) AS parsed_total_count,
      count(*) FILTER (WHERE money_mention_count > 0) AS recognized_money_event_count,
      count(*) FILTER (WHERE total_amount IS NULL AND money_mention_count > 0)
        AS recognized_money_without_total_count,
      count(*) FILTER (
        WHERE total_amount IS NULL AND money_mention_count = 0
          AND (has_dollar_marker OR has_word_currency)
      ) AS explicit_currency_not_recognized_count,
      count(*) FILTER (
        WHERE total_amount IS NULL AND money_mention_count = 0
          AND NOT has_dollar_marker AND NOT has_word_currency
          AND has_unmarked_numeric_context
      ) AS unmarked_numeric_context_count
    FROM operative_judgment_events
    WHERE event_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}
    GROUP BY event_year, case_model, event_kind, extraction_period, source_field
    ORDER BY event_year, operative_event_count DESC, case_model, event_kind,
      extraction_period, source_field
    """,
)

write_query_csv(
    "original-event-year-by-filing-year.csv",
    f"""
    SELECT
      event_year,
      filing_year,
      case_model,
      count(*) AS selected_original_events,
      count(*) FILTER (WHERE parsed_total) AS parsed_total_count,
      median(try_cast(event_total_amount AS DECIMAL(38,2)))
        FILTER (WHERE parsed_total) AS median_parsed_total
    FROM original_coverage
    WHERE event_year IN ({", ".join(str(year) for year in FOCAL_YEARS)})
    GROUP BY event_year, filing_year, case_model
    ORDER BY event_year, selected_original_events DESC, filing_year, case_model
    """,
)

write_query_csv(
    "original-vs-recorded-selection-gap.csv",
    f"""
    SELECT
      event_year AS original_event_year,
      case_model,
      coalesce(recorded_event_kind, 'none') AS recorded_event_kind,
      count(*) AS selected_original_events,
      count(*) FILTER (WHERE summary_total_amount IS NOT NULL)
        AS original_total_count,
      count(*) FILTER (
        WHERE summary_total_amount IS NULL AND recorded_judgment_amount IS NOT NULL
      ) AS recorded_amount_but_original_missing_count,
      count(*) FILTER (
        WHERE summary_total_amount IS NULL AND recorded_judgment_amount IS NULL
      ) AS no_original_or_recorded_amount_count,
      median(try_cast(recorded_judgment_amount AS DECIMAL(38,2))) FILTER (
        WHERE summary_total_amount IS NULL AND recorded_judgment_amount IS NOT NULL
      ) AS recorded_only_median,
      median(recorded_event_year) FILTER (
        WHERE summary_total_amount IS NULL AND recorded_judgment_amount IS NOT NULL
      ) AS recorded_only_median_event_year
    FROM original_coverage
    WHERE event_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}
    GROUP BY event_year, case_model, coalesce(recorded_event_kind, 'none')
    ORDER BY event_year, recorded_amount_but_original_missing_count DESC,
      case_model, recorded_event_kind
    """,
)

write_query_csv(
    "original-vs-recorded-sensitivity.csv",
    f"""
    WITH base AS (
      SELECT
        event_year,
        case_model,
        try_cast(summary_total_amount AS DECIMAL(38,2)) AS original_amount,
        try_cast(recorded_judgment_amount AS DECIMAL(38,2)) AS recorded_amount
      FROM original_coverage
      WHERE event_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}
    ), expanded AS (
      SELECT event_year, 'all' AS case_model, original_amount, recorded_amount
      FROM base
      UNION ALL
      SELECT event_year, case_model, original_amount, recorded_amount
      FROM base
    )
    SELECT
      event_year AS original_event_year,
      case_model,
      count(*) FILTER (WHERE original_amount IS NOT NULL) AS original_amount_count,
      median(original_amount) FILTER (WHERE original_amount IS NOT NULL)
        AS original_amount_median,
      count(*) FILTER (
        WHERE original_amount IS NULL AND recorded_amount IS NOT NULL
      ) AS recorded_only_supplement_count,
      median(recorded_amount) FILTER (
        WHERE original_amount IS NULL AND recorded_amount IS NOT NULL
      ) AS recorded_only_supplement_median,
      count(*) FILTER (
        WHERE coalesce(original_amount, recorded_amount) IS NOT NULL
      ) AS supplemented_count,
      median(coalesce(original_amount, recorded_amount)) FILTER (
        WHERE coalesce(original_amount, recorded_amount) IS NOT NULL
      ) AS supplemented_median,
      round(
        100.0 * (
          median(coalesce(original_amount, recorded_amount)) FILTER (
            WHERE coalesce(original_amount, recorded_amount) IS NOT NULL
          )
          / nullif(
            median(original_amount) FILTER (WHERE original_amount IS NOT NULL),
            0
          ) - 1
        ),
        2
      ) AS supplemented_median_change_pct
    FROM expanded
    GROUP BY event_year, case_model
    ORDER BY event_year, CASE WHEN case_model = 'all' THEN 0 ELSE 1 END, case_model
    """,
)

crosscheck_cur = db.execute(
    f"""
    SELECT
      count(*) AS joined_original_events,
      count(*) FILTER (
        WHERE summary_total_amount IS DISTINCT FROM event_total_amount
      ) AS summary_event_total_mismatches,
      count(*) FILTER (WHERE event_year IS NULL) AS missing_event_year,
      count(*) FILTER (
        WHERE event_year IN ({", ".join(str(year) for year in FOCAL_YEARS)})
      ) AS focal_selected_original_events,
      count(*) FILTER (
        WHERE event_year IN ({", ".join(str(year) for year in FOCAL_YEARS)})
          AND parsed_total
      ) AS focal_parsed_total_count
    FROM original_coverage
    """
)
crosscheck_columns = [item[0] for item in crosscheck_cur.description]
crosscheck = dict(zip(crosscheck_columns, crosscheck_cur.fetchone()))

focal_rows = [
    dict(zip(annual_columns, row))
    for row in annual_rows
    if row[0] in FOCAL_YEARS
]
diagnostics = {
    "source": {
        "summary_glob": "data/judgments/summaries/*.parquet",
        "event_glob": "data/judgments/parquet/*.parquet",
        "grain": "one row per selected original judgment event unless a breakdown says otherwise",
        "privacy": "aggregate counts only; source text, party names, and case numbers are excluded",
    },
    "audit_window": {"min_event_year": MIN_YEAR, "max_event_year": MAX_YEAR},
    "focal_years": list(FOCAL_YEARS),
    "crosscheck": crosscheck,
    "focal_annual_rows": focal_rows,
}
(OUT / "diagnostics.json").write_text(
    json.dumps(diagnostics, indent=2, default=str) + "\n",
    encoding="utf-8",
)

readme = [
    "# Judgment year parsing coverage audit",
    "",
    "Deidentified full-population diagnostics for the 1991, 1992, 2006, and 2011 median anomalies, with neighboring years retained for comparison.",
    "",
    "The audit separates selected original events, monetary signals, recognized money tokens without total-role assignment, and currency/numeric formats missed by the current amount parser.",
    "",
    "A likely-monetary event is a diagnostic candidate, not a confirmed monetary judgment. Counts must be reviewed by case model, source field, and extraction period before changing parser rules.",
    "",
    "## Files",
    "",
    "- original-coverage-by-event-year.csv: annual coverage funnel and medians.",
    "- original-coverage-by-case-model.csv: case-mix and within-model coverage.",
    "- original-coverage-by-source.csv: extraction-period and source-field discontinuities.",
    "- missing-amount-format-cohorts.csv: exclusive aggregate failure cohorts.",
    "- operative-judgment-event-funnel.csv: all operative judgment-like events, not only the event selected as original.",
    "- original-event-year-by-filing-year.csv: focal event years split by filing cohort and case model.",
    "- original-vs-recorded-selection-gap.csv: later recorded amounts omitted by the earliest-event original measure.",
    "- original-vs-recorded-sensitivity.csv: median sensitivity when later recorded amounts supplement missing original totals; this is a coverage diagnostic, not an alternative original-judgment definition.",
    "- diagnostics.json: source definitions, cross-checks, and focal annual rows.",
    "",
    "No case numbers, party names, or source text are written to these outputs.",
    "",
    "Generated by scripts/audit_judgment_year_coverage.py.",
]
(OUT / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
print(json.dumps(diagnostics, default=str))
