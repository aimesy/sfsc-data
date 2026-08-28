# Focal-year judgment parsing audit

## Executive finding

The 1991, 1992, 2006, and 2011 median movements are not explained by a year-specific failure of the current currency-token parser.

- 1991 and 1992 are a historical source-disclosure and case-mix boundary. The published amount samples are overwhelmingly small claims because older civil-general docket captions usually omit the award amount.
- 2006 has strong parser coverage. Its final-operative median is much closer to the neighboring cohort trend than its initially entered median.
- 2011 has only a small role-assignment shortfall. Its apparent original-median spike disappears in the final-operative series, showing that judgment lineage and case mix—not a missed numeric format—create the anomaly.

The primary trend now uses the latest operative or superseding merits-judgment amount in each lineage, attributed to the initial judgment cohort year. Later amended judgments after remittitur replace earlier amounts; renewals remain separate, whole-judgment vacaturs are excluded, and a bare remittitur never supplies an inferred amount.

The recorded-amount coverage sensitivity remains diagnostic only. It may contain renewals and must not be relabeled as a final merits judgment.

## Controlling data

- Current judgment classifier: sfsc.strict_judgment_end_state 2.9.1.
- Raw-recall audit: sfsc.raw_population_disposition_recall 1.5.0.
- Raw source snapshot: 826b7a0f6eaa4fb5890cea6fadecdf94806ceeb2.
- Published analytical tables: 1,170,815 case summaries and 7,082,928 events.
- Final-operative primary population: 174,145 cases; 2,473 whole-judgment vacaturs excluded.
- Final-operative lineage checks: zero missing current-event hashes and zero renewal hashes selected as final.
- Audit grain: one row per selected original judgment event, with separate operative-event and source-coverage funnels.
- Privacy: outputs contain aggregate counts only; no case numbers, party names, or source text.

All 705,856 selected original-event hashes joined to their event rows. Summary and event totals had zero mismatches, and zero selected originals had an unparseable event year.

## Focal-year results

| Initial judgment cohort year | Initially entered rows | Initial median | Final-operative median | Parsed totals among likely monetary originals | Main diagnosis |
|---|---:|---:|---:|---:|---|
| 1991 | 5,957 | $928.00 | $898.92 | 99.30% | Final sample remains almost entirely small claims; other models lack disclosed amounts |
| 1992 | 6,381 | $1,092.61 | $1,109.03 | 98.73% | Civil/UD amount disclosure is only beginning |
| 2006 | 3,245 | $2,969.53 | $3,898.93 | 96.09% | Final lineage reduces the local anomaly; case composition still matters |
| 2011 | 2,767 | $4,424.72 | $5,136.00 | 94.86% | Final-operative series is smooth across 2010–2012 |

The current parser found no focal-year selected-original cohort with an explicit currency token that it failed to recognize, and no focal-year cohort matching a bare labeled-number candidate outside the existing dollar/dollars/USD syntax.

Recognized money mentions without a total-role assignment were 20 in 1991, 31 in 1992, 126 in 2006, and 144 in 2011. The 2006 and 2011 residuals are concentrated in writ matters (87 and 117 rows). That is a real role-assignment review queue, but it is not large or low-valued enough to explain either elevated median.

## 1991–1992 source boundary

The original amount sample changes composition sharply:

| Event year | Small claims | Civil general | Unlawful detainer | Writ |
|---|---:|---:|---:|---:|
| 1991 | 5,957 (100.00%) | 0 | 0 | 0 |
| 1992 | 6,110 (95.75%) | 235 (3.68%) | 18 (0.28%) | 18 (0.28%) |
| 1993 | 5,292 (61.77%) | 2,277 (26.58%) | 997 (11.64%) | 1 |

This is not a falling parser-success series. Parsed totals among likely monetary originals are 99.30% in 1991, 98.73% in 1992, 96.31% in 1993, and 96.64% in 1994.

The missing historical civil set is upstream of the amount regex. In 1991, civil-general operative/superseding money-judgment rows number 341; 335 contain no recognized money mention and none has a parsed total. In 1992, 980 of 1,557 comparable rows contain no money mention and 571 have totals. By 1993, 3,126 of 4,470 have totals. The format transition is in what the docket caption discloses.

Later recorded amounts barely change the 1991 median ($928.00 to $929.44) and move 1992 only to $1,163.76. Recovering the truly missing pre-transition civil awards requires underlying judgment documents and OCR, where available.

## 2006

The likely-monetary total coverage is 96.09%, compared with 90.54% in 2005 and 96.84% in 2007. There are no detected explicit-currency or unmarked-number format misses.

Original amount composition changed:

| Event year | Civil general share | Small-claims share | Civil median | Small-claims median |
|---|---:|---:|---:|---:|
| 2005 | 13.42% | 64.70% | $10,702.74 | $1,510.00 |
| 2006 | 22.71% | 57.81% | $8,538.98 | $1,770.22 |
| 2007 | 18.30% | 60.26% | $9,692.42 | $1,732.79 |

The primary final-operative median is $3,898.93, compared with $3,407.27 in 2005 and $3,862.84 in 2007. It is 7.3% above the neighboring-year midpoint, versus 15.8% for the initially entered series. The final lineage therefore reduces the bump substantially, and there is no evidence of a 2006 parser collapse.

## 2011

The likely-monetary total coverage is 94.86%, compared with 95.73% in 2010 and 95.62% in 2012. The difference is less than one percentage point, and there are no detected focal-year numeric-format misses.

The original amount sample has an unusual composition:

| Event year | Civil general share | Small-claims share | Civil median | Small-claims median |
|---|---:|---:|---:|---:|
| 2010 | 19.97% | 58.08% | $9,020.88 | $1,879.87 |
| 2011 | 31.08% | 46.99% | $7,575.93 | $2,249.59 |
| 2012 | 23.77% | 51.90% | $7,870.04 | $2,180.00 |

The within-model civil median is lower in 2011 than in either neighbor; the aggregate rises because more civil-general rows enter the original amount sample.

The primary final-operative median is $5,136.00, compared with $5,077.19 in 2010 and $5,368.23 in 2012. It is 1.7% below the neighboring-year midpoint. The isolated 2011 spike therefore does not survive legally ordered supersession lineage.

## Implications for annual trend QA

1. Use the final-operative merits median as the primary judgment trend, attributed to the initial judgment cohort year.
2. Keep initially entered, final operative, latest recorded, and renewal medians separate.
3. Never treat a renewal as a merits-judgment revision. Exclude whole-judgment vacaturs, and require an amount-bearing superseding judgment after remittitur rather than inferring an amount from the remittitur itself.
4. Always publish coverage and case-model shares beside the median.
5. Mark 1991 and 1992 as composition/source-limited and do not interpret their aggregate medians as comparable to 1993 onward.
6. Remove 2011 from the parser-failure suspect list. Treat 2006 as a mild composition watch, not a parsing incident.
7. Review recognized-money/no-total writ rows as a separate total-role assignment task.
8. Historical civil award recovery should target judgment documents/OCR, not broaden the currency regex without evidence.

## Reproducible outputs

- ../annual-final-operative-judgment-trends.csv
- ../annual-final-operative-by-case-model.csv
- original-coverage-by-event-year.csv
- original-coverage-by-case-model.csv
- original-coverage-by-source.csv
- missing-amount-format-cohorts.csv
- operative-judgment-event-funnel.csv
- original-event-year-by-filing-year.csv
- original-vs-recorded-selection-gap.csv
- original-vs-recorded-sensitivity.csv
- diagnostics.json

Generated by scripts/audit_judgment_year_coverage.py.
