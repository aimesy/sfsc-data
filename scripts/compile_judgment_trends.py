#!/usr/bin/env python3
"""Compile deidentified annual SFSC judgment amount trends and integrity diagnostics."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "dataverse" / "judgment-trends"
OUT.mkdir(parents=True, exist_ok=True)
db = duckdb.connect()

summary_glob = str(ROOT / "data/judgments/summaries/*.parquet")
event_glob = str(ROOT / "data/judgments/parquet/*.parquet")

db.execute(f"""
CREATE OR REPLACE TEMP VIEW summaries AS
SELECT * FROM read_parquet('{summary_glob}', union_by_name=true);
CREATE OR REPLACE TEMP VIEW events AS
SELECT * FROM read_parquet('{event_glob}', union_by_name=true);
CREATE OR REPLACE TEMP VIEW judgment_amounts AS
SELECT
  s.case_number,
  s.case_prefix,
  coalesce(nullif(s.case_model, ''), 'unknown') AS case_model,
  try_cast(s.filing_year AS INTEGER) AS filing_year,
  s.recorded_judgment_amount,
  s.original_judgment_total_amount,
  s.current_judgment_total_amount AS final_operative_judgment_total_amount,
  s.latest_renewal_total_amount,
  e.entry_date AS recorded_event_date,
  try_cast(substr(e.entry_date, 1, 4) AS INTEGER) AS event_year,
  coalesce(
    try_cast(substr(e.entry_date, 1, 4) AS INTEGER),
    try_cast(s.filing_year AS INTEGER)
  ) AS judgment_year,
  (try_cast(substr(e.entry_date, 1, 4) AS INTEGER) IS NULL) AS used_filing_year_fallback,
  oe.entry_date AS original_event_date,
  try_cast(substr(oe.entry_date, 1, 4) AS INTEGER) AS original_judgment_year,
  try_cast(substr(oe.entry_date, 1, 4) AS INTEGER) AS initial_judgment_year,
  ce.entry_date AS final_operative_event_date,
  ce.event_kind AS final_operative_event_kind,
  ce.status AS final_operative_event_status,
  ce.total_amount AS final_operative_event_total_amount,
  le.entry_date AS renewal_event_date,
  try_cast(substr(le.entry_date, 1, 4) AS INTEGER) AS renewal_year,
  s.recorded_judgment_amount_event_hash,
  s.original_judgment_event_hash,
  s.current_judgment_event_hash AS final_operative_judgment_event_hash,
  s.latest_renewal_event_hash,
  coalesce(s.judgment_is_vacated, false) AS judgment_is_vacated,
  s.review_required
FROM summaries s
LEFT JOIN events ce
  ON ce.case_number = s.case_number
 AND ce.entry_hash = s.current_judgment_event_hash
LEFT JOIN events e
  ON e.case_number = s.case_number
 AND e.entry_hash = s.recorded_judgment_amount_event_hash
LEFT JOIN events oe
  ON oe.case_number = s.case_number
 AND oe.entry_hash = s.original_judgment_event_hash
LEFT JOIN events le
  ON le.case_number = s.case_number
 AND le.entry_hash = s.latest_renewal_event_hash
WHERE s.recorded_judgment_amount IS NOT NULL
   OR s.original_judgment_total_amount IS NOT NULL
   OR s.current_judgment_total_amount IS NOT NULL
   OR s.latest_renewal_total_amount IS NOT NULL;
""")

annual_sql = """
WITH valid AS (
  SELECT judgment_year, cast(recorded_judgment_amount AS DECIMAL(38,2)) amount,
         used_filing_year_fallback AS year_fallback, review_required
  FROM judgment_amounts
  WHERE judgment_year BETWEEN 1900 AND 2100 AND recorded_judgment_amount >= 0
), ranked AS (
  SELECT *, row_number() OVER (PARTITION BY judgment_year ORDER BY amount DESC) AS rn,
         count(*) OVER (PARTITION BY judgment_year) AS n
  FROM valid
), stats AS (
  SELECT judgment_year, count(*) judgment_count, sum(amount) total_amount,
    avg(amount) mean_amount, median(amount) median_amount,
    quantile_cont(amount, .75) p75_amount, quantile_cont(amount, .90) p90_amount,
    quantile_cont(amount, .95) p95_amount, quantile_cont(amount, .99) p99_amount,
    max(amount) max_amount,
    sum(CASE WHEN year_fallback THEN 1 ELSE 0 END) fallback_year_count,
    sum(CASE WHEN review_required THEN 1 ELSE 0 END) review_required_count
  FROM valid GROUP BY judgment_year
), concentration AS (
  SELECT judgment_year,
    sum(CASE WHEN rn <= greatest(1, ceil(n * .10)) THEN amount ELSE 0 END) top10_amount,
    sum(CASE WHEN rn <= greatest(1, ceil(n * .05)) THEN amount ELSE 0 END) top5_amount,
    sum(CASE WHEN rn <= greatest(1, ceil(n * .01)) THEN amount ELSE 0 END) top1_amount,
    sum(CASE WHEN rn = 1 THEN amount ELSE 0 END) largest_amount,
    sum(CASE WHEN rn > greatest(1, ceil(n * .01)) THEN amount ELSE 0 END) excluding_top1pct_total,
    sum(CASE WHEN rn > 1 THEN amount ELSE 0 END) excluding_largest_total,
    sum(CASE WHEN rn > 5 THEN amount ELSE 0 END) excluding_largest5_total,
    sum(CASE WHEN rn > 10 THEN amount ELSE 0 END) excluding_largest10_total
  FROM ranked GROUP BY judgment_year
)
SELECT s.*,
  s.mean_amount / nullif(s.median_amount, 0) mean_to_median_ratio,
  s.total_amount / nullif(s.median_amount * s.judgment_count, 0) total_to_median_baseline_ratio,
  c.top10_amount / nullif(s.total_amount, 0) top10_share,
  c.top5_amount / nullif(s.total_amount, 0) top5_share,
  c.top1_amount / nullif(s.total_amount, 0) top1_share,
  c.largest_amount / nullif(s.total_amount, 0) largest_share,
  c.excluding_top1pct_total, c.excluding_largest_total,
  c.excluding_largest5_total, c.excluding_largest10_total
FROM stats s JOIN concentration c USING (judgment_year)
ORDER BY judgment_year
"""

model_sql = """
SELECT judgment_year, case_model, count(*) judgment_count,
  sum(cast(recorded_judgment_amount AS DECIMAL(38,2))) total_amount,
  avg(cast(recorded_judgment_amount AS DECIMAL(38,2))) mean_amount,
  median(cast(recorded_judgment_amount AS DECIMAL(38,2))) median_amount,
  quantile_cont(cast(recorded_judgment_amount AS DECIMAL(38,2)), .90) p90_amount,
  quantile_cont(cast(recorded_judgment_amount AS DECIMAL(38,2)), .99) p99_amount,
  max(cast(recorded_judgment_amount AS DECIMAL(38,2))) max_amount
FROM judgment_amounts
WHERE judgment_year BETWEEN 1900 AND 2100 AND recorded_judgment_amount >= 0
GROUP BY judgment_year, case_model ORDER BY judgment_year, case_model
"""

def write_query_csv(name: str, sql: str):
    cur = db.execute(sql)
    columns = [item[0] for item in cur.description]
    rows = cur.fetchall()
    with (OUT / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return columns, rows

final_annual_sql = annual_sql.replace(
    "recorded_judgment_amount", "final_operative_judgment_total_amount"
).replace("judgment_year", "initial_judgment_year").replace(
    "used_filing_year_fallback", "(original_event_date IS NULL)"
).replace(
    "WHERE initial_judgment_year BETWEEN 1900 AND 2100 AND final_operative_judgment_total_amount >= 0",
    "WHERE initial_judgment_year BETWEEN 1900 AND 2100 AND final_operative_judgment_total_amount >= 0\n"
    "    AND NOT judgment_is_vacated",
)
final_model_sql = model_sql.replace(
    "recorded_judgment_amount", "final_operative_judgment_total_amount"
).replace("judgment_year", "initial_judgment_year").replace(
    "WHERE initial_judgment_year BETWEEN 1900 AND 2100 AND final_operative_judgment_total_amount >= 0",
    "WHERE initial_judgment_year BETWEEN 1900 AND 2100 AND final_operative_judgment_total_amount >= 0\n"
    "  AND NOT judgment_is_vacated",
)
final_annual_columns, final_annual_rows = write_query_csv(
    "annual-final-operative-judgment-trends.csv", final_annual_sql
)
write_query_csv("annual-final-operative-by-case-model.csv", final_model_sql)
annual_columns, annual_rows = write_query_csv("annual-recorded-amount-trends.csv", annual_sql)
original_annual_sql = annual_sql.replace(
    "recorded_judgment_amount", "original_judgment_total_amount"
).replace("judgment_year", "original_judgment_year").replace(
    "used_filing_year_fallback", "(original_event_date IS NULL)"
)
renewal_annual_sql = annual_sql.replace(
    "recorded_judgment_amount", "latest_renewal_total_amount"
).replace("judgment_year", "renewal_year").replace(
    "used_filing_year_fallback", "(renewal_event_date IS NULL)"
)
write_query_csv("annual-original-judgment-trends.csv", original_annual_sql)
write_query_csv("annual-renewal-trends.csv", renewal_annual_sql)
write_query_csv("annual-by-case-model.csv", model_sql)

quality = db.execute("""
SELECT
  (SELECT count(*) FROM summaries) summary_rows,
  (SELECT count(distinct case_number) FROM summaries) distinct_summary_cases,
  (SELECT count(*) FROM events) event_rows,
  (SELECT count(*) FROM judgment_amounts
   WHERE final_operative_judgment_total_amount IS NOT NULL
     AND NOT judgment_is_vacated) final_operative_amount_rows,
  (SELECT count(distinct case_number) FROM judgment_amounts
   WHERE final_operative_judgment_total_amount IS NOT NULL
     AND NOT judgment_is_vacated) distinct_final_operative_amount_cases,
  (SELECT count(*) FROM judgment_amounts
   WHERE final_operative_judgment_total_amount IS NOT NULL
     AND final_operative_event_date IS NULL) missing_final_operative_event_date,
  (SELECT count(*) FROM judgment_amounts
   WHERE final_operative_judgment_total_amount IS NOT NULL
     AND final_operative_event_total_amount IS DISTINCT FROM final_operative_judgment_total_amount)
     final_operative_event_amount_mismatches,
  (SELECT count(*) FROM judgment_amounts
   WHERE final_operative_judgment_total_amount IS NOT NULL
     AND final_operative_event_status NOT IN ('operative', 'superseding'))
     nonoperative_final_event_count,
  (SELECT count(*) FROM judgment_amounts
   WHERE final_operative_judgment_total_amount IS NOT NULL
     AND final_operative_event_kind = 'renewal') renewal_selected_as_final_count,
  (SELECT count(*) FROM judgment_amounts
   WHERE final_operative_judgment_total_amount IS NOT NULL
     AND judgment_is_vacated) vacated_final_amounts_excluded,
  (SELECT count(*) FROM judgment_amounts WHERE recorded_judgment_amount IS NOT NULL) amount_rows,
  (SELECT count(distinct case_number) FROM judgment_amounts WHERE recorded_judgment_amount IS NOT NULL) distinct_amount_cases,
  (SELECT count(*) FROM judgment_amounts WHERE recorded_judgment_amount IS NOT NULL AND recorded_event_date IS NULL) missing_event_date,
  (SELECT count(*) FROM judgment_amounts WHERE original_judgment_total_amount IS NOT NULL) original_amount_rows,
  (SELECT count(*) FROM judgment_amounts WHERE original_judgment_total_amount IS NOT NULL AND original_event_date IS NULL) missing_original_event_date,
  (SELECT count(*) FROM judgment_amounts WHERE latest_renewal_total_amount IS NOT NULL) renewal_amount_rows,
  (SELECT count(*) FROM judgment_amounts WHERE latest_renewal_total_amount IS NOT NULL AND renewal_event_date IS NULL) missing_renewal_event_date,
  (SELECT count(*) FROM judgment_amounts WHERE judgment_year IS NULL) missing_year,
  (SELECT count(*) FROM judgment_amounts WHERE recorded_judgment_amount < 0) negative_amounts,
  (SELECT count(*) FROM judgment_amounts WHERE recorded_judgment_amount = 0) zero_amounts,
  (SELECT count(*) FROM judgment_amounts WHERE recorded_judgment_amount > 1000000000) over_one_billion,
  (SELECT count(*) FROM (
     SELECT case_number, count(*) n FROM judgment_amounts GROUP BY case_number HAVING count(*) > 1
   )) duplicate_case_numbers,
  (SELECT count(*) FROM (
     SELECT case_number, recorded_judgment_amount_event_hash, count(*) n
     FROM judgment_amounts GROUP BY 1,2 HAVING count(*) > 1
   )) duplicate_selected_event_keys
""")
quality_columns = [item[0] for item in quality.description]
diagnostics = dict(zip(quality_columns, quality.fetchone()))

largest = db.execute("""
SELECT judgment_year, case_prefix, case_model,
       cast(recorded_judgment_amount AS DECIMAL(38,2)) amount,
       recorded_event_date, review_required
FROM judgment_amounts
WHERE recorded_judgment_amount IS NOT NULL AND judgment_year BETWEEN 1900 AND 2100
ORDER BY recorded_judgment_amount DESC NULLS LAST LIMIT 100
""")
largest_columns = [item[0] for item in largest.description]
diagnostics["largest_100_anonymized"] = [
    dict(zip(largest_columns, row)) for row in largest.fetchall()
]
final_largest = db.execute("""
SELECT initial_judgment_year, case_prefix, case_model,
       cast(final_operative_judgment_total_amount AS DECIMAL(38,2)) amount,
       final_operative_event_date, final_operative_event_kind, review_required
FROM judgment_amounts
WHERE final_operative_judgment_total_amount IS NOT NULL
  AND NOT judgment_is_vacated
  AND initial_judgment_year BETWEEN 1900 AND 2100
ORDER BY final_operative_judgment_total_amount DESC NULLS LAST LIMIT 100
""")
final_largest_columns = [item[0] for item in final_largest.description]
diagnostics["largest_100_final_operative_anonymized"] = [
    dict(zip(final_largest_columns, row)) for row in final_largest.fetchall()
]
diagnostics["source"] = {
    "summary_glob": "data/judgments/summaries/*.parquet",
    "event_glob": "data/judgments/parquet/*.parquet",
    "primary_amount_definition": "latest operative or superseding merits-judgment total in the judgment lineage; renewals are excluded and whole-judgment vacaturs are omitted",
    "primary_year_definition": "initial judgment event year, so later amended or remitted amounts remain attributed to the original judgment cohort",
    "comparison_series": "original judgment, latest recorded judgment-or-renewal, and latest renewal remain separate",
}
(OUT / "diagnostics.json").write_text(
    json.dumps(diagnostics, indent=2, default=str) + "\n", encoding="utf-8"
)

readme = [
    "# Annual judgment amount trends",
    "",
    "Deidentified aggregate statistics from the authoritative SFSC judgment summary and event shards.",
    "",
    "## Metric definitions",
    "",
    "- Each file has at most one observation per case for its named measure.",
    "- The primary series uses the latest operative or superseding merits-judgment amount in each lineage and attributes it to the initial judgment year.",
    "- A later amended judgment after remittitur supersedes the earlier amount when the amended event states a total; a bare remittitur does not invent an amount.",
    "- Renewals and whole-judgment vacaturs are excluded from the primary final-operative series.",
    "- Original, latest recorded judgment-or-renewal, and renewal series remain separate comparison measures.",
    "- Raw totals are paired with medians, percentiles, and upper-tail-excluded totals because awards are strongly right-skewed.",
    "",
    "## Data-quality summary",
    "",
    f"- Summary rows: {diagnostics['summary_rows']:,}",
    f"- Distinct summary cases: {diagnostics['distinct_summary_cases']:,}",
    f"- Final-operative amount cases: {diagnostics['distinct_final_operative_amount_cases']:,}",
    f"- Final-operative event/amount mismatches: {diagnostics['final_operative_event_amount_mismatches']:,}",
    f"- Renewals selected as final operative: {diagnostics['renewal_selected_as_final_count']:,}",
    f"- Vacated judgment amounts excluded: {diagnostics['vacated_final_amounts_excluded']:,}",
    f"- Recorded amount-bearing cases: {diagnostics['distinct_amount_cases']:,}",
    f"- Missing recorded selected-event date: {diagnostics['missing_event_date']:,}",
    f"- Duplicate case numbers after summary selection: {diagnostics['duplicate_case_numbers']:,}",
    f"- Negative amounts: {diagnostics['negative_amounts']:,}",
    f"- Amounts over $1 billion: {diagnostics['over_one_billion']:,}",
    "",
    "## Files",
    "",
    "- `annual-final-operative-judgment-trends.csv`: primary final operative merits amounts by initial judgment cohort year.",
    "- `annual-final-operative-by-case-model.csv`: primary series segmented by case model.",
    "- `annual-original-judgment-trends.csv`: initially entered judgment amounts by original event year.",
    "- `annual-recorded-amount-trends.csv`: latest recorded judgment-or-renewal amounts by selected event year.",
    "- `annual-renewal-trends.csv`: latest renewal amounts by renewal event year.",
    "- `annual-by-case-model.csv`: recorded-amount comparison statistics segmented by case model.",
    "- `diagnostics.json`: integrity counts and the 100 largest anonymized records for extraction review.",
    "",
    "Generated by `scripts/compile_judgment_trends.py`.",
]
(OUT / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
print(json.dumps({
    "output": str(OUT),
    "primary_final_operative_annual_rows": len(final_annual_rows),
    "recorded_comparison_annual_rows": len(annual_rows),
    **diagnostics,
}, default=str))
