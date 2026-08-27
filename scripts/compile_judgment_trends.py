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
  s.latest_renewal_total_amount,
  e.entry_date AS recorded_event_date,
  try_cast(substr(e.entry_date, 1, 4) AS INTEGER) AS event_year,
  coalesce(
    try_cast(substr(e.entry_date, 1, 4) AS INTEGER),
    try_cast(s.filing_year AS INTEGER)
  ) AS judgment_year,
  (try_cast(substr(e.entry_date, 1, 4) AS INTEGER) IS NULL) AS used_filing_year_fallback,
  s.recorded_judgment_amount_event_hash,
  s.original_judgment_event_hash,
  s.latest_renewal_event_hash,
  s.review_required
FROM summaries s
LEFT JOIN events e
  ON e.case_number = s.case_number
 AND e.entry_hash = s.recorded_judgment_amount_event_hash
WHERE s.recorded_judgment_amount IS NOT NULL;
""")

annual_sql = """
WITH valid AS (
  SELECT judgment_year, cast(recorded_judgment_amount AS DOUBLE) amount,
         used_filing_year_fallback, review_required
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
    sum(CASE WHEN used_filing_year_fallback THEN 1 ELSE 0 END) fallback_year_count,
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
  sum(cast(recorded_judgment_amount AS DOUBLE)) total_amount,
  avg(cast(recorded_judgment_amount AS DOUBLE)) mean_amount,
  median(cast(recorded_judgment_amount AS DOUBLE)) median_amount,
  quantile_cont(cast(recorded_judgment_amount AS DOUBLE), .90) p90_amount,
  quantile_cont(cast(recorded_judgment_amount AS DOUBLE), .99) p99_amount,
  max(cast(recorded_judgment_amount AS DOUBLE)) max_amount
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

annual_columns, annual_rows = write_query_csv("annual-judgment-trends.csv", annual_sql)
write_query_csv("annual-by-case-model.csv", model_sql)

quality = db.execute("""
SELECT
  (SELECT count(*) FROM summaries) summary_rows,
  (SELECT count(distinct case_number) FROM summaries) distinct_summary_cases,
  (SELECT count(*) FROM events) event_rows,
  (SELECT count(*) FROM judgment_amounts) amount_rows,
  (SELECT count(distinct case_number) FROM judgment_amounts) distinct_amount_cases,
  (SELECT count(*) FROM judgment_amounts WHERE recorded_event_date IS NULL) missing_event_date,
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
       cast(recorded_judgment_amount AS DOUBLE) amount,
       recorded_event_date, review_required
FROM judgment_amounts
WHERE judgment_year BETWEEN 1900 AND 2100
ORDER BY recorded_judgment_amount DESC NULLS LAST LIMIT 100
""")
largest_columns = [item[0] for item in largest.description]
diagnostics["largest_100_anonymized"] = [
    dict(zip(largest_columns, row)) for row in largest.fetchall()
]
diagnostics["source"] = {
    "summary_glob": "data/judgments/summaries/*.parquet",
    "event_glob": "data/judgments/parquet/*.parquet",
    "amount_definition": "one recorded_judgment_amount per case summary",
    "year_definition": "selected recorded amount event year; filing year only when event date is absent",
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
    "- One observation per case with a non-null `recorded_judgment_amount`.",
    "- Year is the selected amount-bearing event year; filing year is used only when the event date is unavailable.",
    "- Renewals are not added to original judgments; the recorded amount is the latest expressly recorded judgment or renewal amount and is not a payoff balance.",
    "- Raw totals are paired with medians, percentiles, and upper-tail-excluded totals because awards are strongly right-skewed.",
    "",
    "## Data-quality summary",
    "",
    f"- Summary rows: {diagnostics['summary_rows']:,}",
    f"- Distinct summary cases: {diagnostics['distinct_summary_cases']:,}",
    f"- Amount-bearing cases: {diagnostics['distinct_amount_cases']:,}",
    f"- Missing selected-event date: {diagnostics['missing_event_date']:,}",
    f"- Duplicate case numbers after summary selection: {diagnostics['duplicate_case_numbers']:,}",
    f"- Negative amounts: {diagnostics['negative_amounts']:,}",
    f"- Amounts over $1 billion: {diagnostics['over_one_billion']:,}",
    "",
    "## Files",
    "",
    "- `annual-judgment-trends.csv`: annual totals, quantiles, concentration, and sensitivity totals.",
    "- `annual-by-case-model.csv`: annual statistics segmented by case model.",
    "- `diagnostics.json`: integrity counts and the 100 largest anonymized records for extraction review.",
    "",
    "Generated by `scripts/compile_judgment_trends.py`.",
]
(OUT / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
print(json.dumps({"output": str(OUT), "annual_rows": len(annual_rows), **diagnostics}, default=str))
