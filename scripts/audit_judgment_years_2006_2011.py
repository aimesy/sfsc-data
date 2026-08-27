#!/usr/bin/env python3
"""Full-population event-year coverage audit for 2006 and 2011 judgment amounts."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/dataverse/judgment-trends/audits/2006-2011"
OUT.mkdir(parents=True, exist_ok=True)
db = duckdb.connect()

summary_glob = str(ROOT / "data/judgments/summaries/*.parquet")
event_glob = str(ROOT / "data/judgments/parquet/*.parquet")
db.execute(f"""
CREATE OR REPLACE TEMP VIEW summaries AS
SELECT * FROM read_parquet('{summary_glob}', union_by_name=true);
CREATE OR REPLACE TEMP VIEW events AS
SELECT *,
       try_cast(substr(entry_date, 1, 4) AS INTEGER) AS entry_year,
       coalesce(array_length(money_mentions), 0) AS money_mention_count,
       event_kind IN (
         'judgment','amended_judgment','default_judgment','dismissal',
         'judgment_of_dismissal','take_nothing','monetary_judgment',
         'possession','declaratory/injunctive','name_change_decree'
       ) AS is_legacy_judgment_kind,
       status IN ('operative','superseding') AS is_operative
FROM read_parquet('{event_glob}', union_by_name=true);
CREATE OR REPLACE TEMP VIEW selected_original AS
SELECT
  s.case_number,
  s.case_prefix,
  coalesce(nullif(s.case_model, ''), 'unknown') AS case_model,
  try_cast(s.filing_year AS INTEGER) AS filing_year,
  s.original_judgment_total_amount,
  s.recorded_judgment_amount,
  s.original_judgment_event_hash,
  s.recorded_judgment_amount_event_hash,
  s.review_required AS summary_review_required,
  e.entry_date,
  e.entry_year,
  e.entry_date_source_field,
  e.source_field,
  e.event_kind,
  e.status,
  e.rule_id,
  e.rule_version,
  e.extraction_period,
  e.total_amount,
  e.principal_amount,
  e.interest_amount,
  e.costs_amount,
  e.fees_amount,
  e.damages_amount,
  e.money_mention_count,
  e.review_reasons,
  e.source_text
FROM summaries s
LEFT JOIN events e
  ON e.case_number = s.case_number
 AND e.entry_hash = s.original_judgment_event_hash
WHERE s.original_judgment_event_hash IS NOT NULL;
""")

def write_csv(name: str, sql: str) -> list[dict[str, object]]:
    cur = db.execute(sql)
    cols = [x[0] for x in cur.description]
    rows = cur.fetchall()
    with (OUT / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(cols)
        writer.writerows(rows)
    return [dict(zip(cols, row)) for row in rows]

annual_event = write_csv("annual-event-coverage.csv", """
SELECT entry_year,
  count(*) event_rows,
  count(DISTINCT case_number) event_cases,
  count(*) FILTER (WHERE is_operative) operative_rows,
  count(DISTINCT case_number) FILTER (WHERE is_operative) operative_cases,
  count(*) FILTER (WHERE is_operative AND is_legacy_judgment_kind) operative_judgment_rows,
  count(DISTINCT case_number) FILTER (WHERE is_operative AND is_legacy_judgment_kind) operative_judgment_cases,
  count(*) FILTER (WHERE is_operative AND is_legacy_judgment_kind AND money_mention_count > 0) judgment_money_mention_rows,
  count(*) FILTER (WHERE is_operative AND is_legacy_judgment_kind AND total_amount IS NOT NULL) judgment_total_rows,
  count(*) FILTER (WHERE is_operative AND is_legacy_judgment_kind AND money_mention_count > 0 AND total_amount IS NULL) judgment_unassigned_money_rows,
  count(DISTINCT case_number) FILTER (WHERE is_operative AND is_legacy_judgment_kind AND money_mention_count > 0 AND total_amount IS NULL) judgment_unassigned_money_cases,
  count(*) FILTER (WHERE is_operative AND is_legacy_judgment_kind AND list_contains(review_reasons, 'unclassified_money_mentions')) unclassified_money_rows,
  count(*) FILTER (WHERE is_operative AND is_legacy_judgment_kind AND regexp_matches(source_text, '\\$\\s*[0-9]') AND money_mention_count = 0) dollar_token_missed_rows,
  count(*) FILTER (WHERE entry_date IS NULL OR entry_year IS NULL) invalid_or_missing_year_rows,
  count(DISTINCT rule_version) rule_versions,
  count(DISTINCT extraction_period) extraction_periods
FROM events
WHERE entry_year BETWEEN 2004 AND 2013
GROUP BY entry_year ORDER BY entry_year
""")

annual_selected = write_csv("annual-selected-original-coverage.csv", """
SELECT entry_year,
  count(*) selected_original_rows,
  count(DISTINCT case_number) selected_original_cases,
  count(*) FILTER (WHERE original_judgment_total_amount IS NOT NULL) selected_original_amount_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL) selected_original_no_amount_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL AND recorded_judgment_amount IS NOT NULL) recorded_only_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL AND money_mention_count > 0) selected_unassigned_money_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL AND list_contains(review_reasons, 'unclassified_money_mentions')) selected_unclassified_money_rows,
  median(original_judgment_total_amount) FILTER (WHERE original_judgment_total_amount IS NOT NULL) median_original_amount,
  count(DISTINCT case_model) case_models,
  count(DISTINCT source_field) source_fields,
  count(DISTINCT rule_version) rule_versions,
  count(DISTINCT extraction_period) extraction_periods
FROM selected_original
WHERE entry_year BETWEEN 2004 AND 2013
GROUP BY entry_year ORDER BY entry_year
""")

annual_summary = write_csv("annual-filing-year-summary-funnel.csv", """
SELECT try_cast(filing_year AS INTEGER) filing_year,
  count(*) summary_cases,
  count(*) FILTER (WHERE event_count > 0) any_extracted_event_cases,
  count(*) FILTER (WHERE operative_event_count > 0) operative_event_cases,
  count(*) FILTER (WHERE original_judgment_event_hash IS NOT NULL) original_judgment_event_cases,
  count(*) FILTER (WHERE original_judgment_total_amount IS NOT NULL) original_amount_cases,
  count(*) FILTER (WHERE recorded_judgment_amount IS NOT NULL) recorded_amount_cases,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL AND recorded_judgment_amount IS NOT NULL) recorded_only_cases,
  count(*) FILTER (WHERE review_required) review_required_cases,
  count(DISTINCT case_model) case_models,
  count(DISTINCT extraction_period) extraction_periods,
  count(DISTINCT rule_version) rule_versions
FROM summaries
WHERE try_cast(filing_year AS INTEGER) BETWEEN 2004 AND 2013
GROUP BY 1 ORDER BY 1
""")

by_model = write_csv("annual-original-by-case-model.csv", """
SELECT entry_year, case_model,
  count(*) selected_original_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NOT NULL) amount_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL) no_amount_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL AND recorded_judgment_amount IS NOT NULL) recorded_only_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL AND money_mention_count > 0) unassigned_money_rows,
  median(original_judgment_total_amount) FILTER (WHERE original_judgment_total_amount IS NOT NULL) median_original_amount
FROM selected_original
WHERE entry_year BETWEEN 2004 AND 2013
GROUP BY entry_year, case_model
ORDER BY entry_year, selected_original_rows DESC, case_model
""")

focal_structure = write_csv("focal-event-structure.csv", """
SELECT entry_year, extraction_period, rule_version, source_field,
       entry_date_source_field, case_model, event_kind, status, rule_id,
  count(*) event_rows,
  count(DISTINCT case_number) case_count,
  count(*) FILTER (WHERE money_mention_count > 0) money_mention_rows,
  count(*) FILTER (WHERE total_amount IS NOT NULL) total_amount_rows,
  count(*) FILTER (WHERE money_mention_count > 0 AND total_amount IS NULL) unassigned_money_rows,
  count(*) FILTER (WHERE list_contains(review_reasons, 'unclassified_money_mentions')) unclassified_money_rows,
  count(*) FILTER (WHERE regexp_matches(source_text, '\\$\\s*[0-9]') AND money_mention_count = 0) dollar_token_missed_rows
FROM events
WHERE entry_year IN (2006, 2011)
GROUP BY ALL
HAVING count(*) >= 2
ORDER BY entry_year, unassigned_money_rows DESC, event_rows DESC
""")

format_cohorts = write_csv("annual-monetary-format-cohorts.csv", """
WITH formats AS (
  SELECT *,
    CASE
      WHEN regexp_matches(source_text, '\\$\\s*[0-9]') THEN 'dollar_symbol'
      WHEN regexp_matches(source_text, '[0-9][0-9,]*(?:\\.[0-9]{1,2})?\\s+(?:DOLLARS?|USD)\\b', 'i') THEN 'number_plus_dollars_or_usd'
      WHEN regexp_matches(source_text, '\\bAMOUNT\\s+OF\\s+[0-9][0-9,]*\\.[0-9]{2}', 'i') THEN 'amount_of_bare_decimal'
      WHEN regexp_matches(source_text, '\\bAMOUNT\\s+OF\\s+[0-9][0-9,]*\\b', 'i') THEN 'amount_of_bare_integer'
      WHEN regexp_matches(source_text, '[0-9][0-9,]*\\.[0-9]{2}') THEN 'bare_decimal'
      WHEN regexp_matches(source_text, '[0-9]{1,3}(?:,[0-9]{3})+') THEN 'bare_comma_number'
      ELSE 'no_recognized_amount_shape'
    END amount_format
  FROM events
  WHERE entry_year BETWEEN 2004 AND 2013
    AND is_operative AND is_legacy_judgment_kind
)
SELECT entry_year, amount_format,
  count(*) judgment_rows,
  count(DISTINCT case_number) judgment_cases,
  count(*) FILTER (WHERE money_mention_count > 0) parsed_mention_rows,
  count(*) FILTER (WHERE total_amount IS NOT NULL) total_amount_rows,
  count(*) FILTER (WHERE money_mention_count > 0 AND total_amount IS NULL) unassigned_money_rows,
  count(*) FILTER (WHERE list_contains(review_reasons, 'unclassified_money_mentions')) unclassified_money_rows,
  median(total_amount) FILTER (WHERE total_amount IS NOT NULL) median_total_amount
FROM formats
GROUP BY entry_year, amount_format
ORDER BY entry_year, judgment_rows DESC
""")

lag = write_csv("focal-event-filing-lag.csv", """
SELECT entry_year, filing_year, case_model,
  count(*) selected_original_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NOT NULL) amount_rows,
  count(*) FILTER (WHERE original_judgment_total_amount IS NULL AND recorded_judgment_amount IS NOT NULL) recorded_only_rows,
  median(original_judgment_total_amount) FILTER (WHERE original_judgment_total_amount IS NOT NULL) median_original_amount
FROM selected_original
WHERE entry_year IN (2006, 2011)
GROUP BY entry_year, filing_year, case_model
ORDER BY entry_year, selected_original_rows DESC
""")

selected_reasons = write_csv("focal-selected-no-amount-reasons.csv", """
SELECT entry_year, extraction_period, rule_version, source_field, case_model,
       event_kind, status, rule_id,
       coalesce(array_to_string(review_reasons, '|'), '') review_reason_set,
  count(*) no_amount_rows,
  count(*) FILTER (WHERE recorded_judgment_amount IS NOT NULL) recorded_only_rows,
  count(*) FILTER (WHERE money_mention_count > 0) money_mention_rows,
  count(*) FILTER (WHERE principal_amount IS NOT NULL OR interest_amount IS NOT NULL OR
      costs_amount IS NOT NULL OR fees_amount IS NOT NULL OR damages_amount IS NOT NULL) component_amount_rows
FROM selected_original
WHERE entry_year IN (2006, 2011)
  AND original_judgment_total_amount IS NULL
GROUP BY ALL
HAVING count(*) >= 2
ORDER BY entry_year, recorded_only_rows DESC, money_mention_rows DESC, no_amount_rows DESC
""")

diagnostics = {
  "source": {
    "summaries": "data/judgments/summaries/*.parquet",
    "events": "data/judgments/parquet/*.parquet",
    "population": "complete authoritative judgment Parquet shards",
    "grain": "events for event coverage; one selected original event per case for original coverage",
    "comparison_window": "2004-2013; focal event years 2006 and 2011",
  },
  "outputs": {
    "annual_event_rows": len(annual_event),
    "annual_selected_rows": len(annual_selected),
    "annual_summary_rows": len(annual_summary),
    "by_model_rows": len(by_model),
    "focal_structure_rows": len(focal_structure),
    "format_cohort_rows": len(format_cohorts),
    "lag_rows": len(lag),
    "selected_reason_rows": len(selected_reasons),
  },
}
(OUT / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2) + "\n", encoding="utf-8")
(OUT / "README.md").write_text("""# 2006 and 2011 judgment-amount coverage audit

This privacy-preserving audit compares the focal event years against 2004-2013 controls.

The principal funnels distinguish:

- extracted operative judgments from selected original-judgment events;
- money tokens from totals assigned to the judgment;
- missing original amounts from cases where a later recorded amount exists;
- case-model, source-field, extraction-period, rule-version, and filing/event-year mix.

No names, case numbers, source text, paths, or entry hashes are written to these outputs.
""", encoding="utf-8")
print(json.dumps(diagnostics))
