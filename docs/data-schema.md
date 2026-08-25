# SFSC Public Data Schema

This document describes the public archive layout on `master`. JSON objects can
gain additive fields; consumers should ignore unknown fields and should not
assume that an optional array or object is present for every case.

## Case Paths

Canonical case records use a sharded path:

```text
archive/cases/<PREFIX>/<YY>/<CASE_NUMBER>.json
```

`PREFIX` is the leading alphabetic case-number prefix. `YY` is the first two
digits after that prefix. Numbers that do not match that form are stored under
`archive/cases/_MISC/unknown/`. Older snapshots may also contain flat
`archive/cases/<CASE_NUMBER>.json` paths.

Case-number lookup is punctuation-insensitive. For example,
`CGC-23-605428` and `CGC23605428` identify the same normalized key.

## Case Record

Common top-level fields include:

| Field | Type | Meaning |
|---|---|---|
| `case_number` | string | Normalized archive key |
| `case_title` | string | Court-supplied or repaired display title |
| `court` | string/object | Court identification supplied by the source |
| `source_url` | string | Public source page for the capture |
| `captured_at` | timestamp | Time represented by this capture |
| `filing_date` / `filed_date` | date | Filing date when supplied |
| `case_type` | string | Court case type when supplied |
| `cause_of_action` | string/array | Clerk-assigned category or categories |
| `parties` | array | Party names, roles, and source attributes |
| `attorneys` | array | Attorney observations associated with the case |
| `docket_entries` | array | Register-of-actions rows in source order |
| `documents` | array | Public document metadata and byte status |
| `calendar` | array | Hearings and calendar observations |
| `payments` | array | Court payment rows when available |
| `payments_total` | number | Sum supplied or normalized from payment rows |
| `status` | string/object | Source status where the court supplies one |

Civil docket entries commonly expose a filing date, title or description,
filing party, document identifier, and source attributes. Criminal docket rows
can retain the court's original keys and normalized aliases such as
`date_filed`, `description`, `submitter`, and `source`.

## Criminal Records

Criminal case JSON can additionally include:

| Field | Type | Meaning |
|---|---|---|
| `criminal_case_number` | string | Court criminal number without archive ambiguity |
| `display_case_number` | string | Punctuated court display number |
| `portal_case_id` | string | Court portal record identifier |
| `defendant` | string | Defendant name obtained from the criminal index/header |
| `case_header` | object | Court-supplied number, defendant, and filing metadata |
| `criminal` | object | Normalized criminal header, statute, and charge fields |
| `roa` | array | Criminal register-of-actions rows |
| `search` | object | Public index observation retained with the case |

`criminal.statutes` records normalized statute codes, counts, and the source
fields supporting them. `criminal.inferred_charges` is distinct from a
court-supplied charge and must not be presented as a court finding.

## Documents And Bytes

Document rows may include a stable court document identifier, title, filing
date, availability state, MIME type, byte size, SHA-256 digest, release tag,
asset name, and release URL. The top-level byte fields summarize the case:

| Field | Type | Meaning |
|---|---|---|
| `document_byte_capture_scope` | string | Scope intentionally applied to this capture |
| `document_bytes_captured` | boolean | Whether the applicable byte scope is satisfied |
| `documents_bytes_count` | integer | Documents with archived byte metadata |
| `documents_deferred_count` | integer | Available documents intentionally deferred |
| `documents_unavailable_count` | integer | Documents the source would not provide |
| `document_coverage` | object | Case-type-specific coverage summary when present |

Metadata, byte availability, and OCR are separate states. Replacing a docket
capture must not remove previously retained document, hash, release, or OCR
metadata.

## Name-Change Records

Name-change (`CNC`) records can include `decreed_name`, `decreed_names`, and
`decreed_name_changes`. These values are extracted from decree documents and
retain source-document references. For `CNC`, core-document coverage means the
decree changing the name; unrelated filings do not satisfy that core scope.

## Case Directory

`archive/case-directory/manifest.json` summarizes all browse rows and links to
prefix manifests. Prefix manifests link to year NDJSON shards. Each row includes
at least a case number and scan state, with additive title, date, category, and
count fields when available.

Public scan states are:

| State | Meaning |
|---|---|
| `complete` | All listed public documents have archived bytes |
| `core_docs` | The defined essential document set is captured |
| `partial_docs` | Some, but not all applicable document bytes are captured |
| `summary_only` | Docket or summary data is present without applicable bytes |
| `no_docs` | The public docket has no document bytes to capture |
| `indexed` | Identified by an index but not yet represented by a usable docket |
| `discovered` | Identified for capture and still pending |
| `restricted` | The court reports the case as unavailable for public viewing |

An unavailable court response is `restricted`, not a successful case capture
and not an invisible row.

## New Filings

`archive/new-filings-cases/<PREFIX>/<YEAR>.ndjson` contains compact clerk filing
observations. Its manifests publish prefix/year counts. These rows identify case
numbers for docket capture; they do not imply that document bytes were captured.

## Judgments And Derived Fields

Judgment status, amount, satisfaction, and dispositive-event presentation are
derived viewer/research fields backed by cited docket or document entries. They
are not substituted for the source case JSON. A monetary total must retain its
source event; vacated, superseded, renewed, partially satisfied, and
case-type-specific dispositions require separate treatment. Current payoff or
enforceable balances are not inferred from principal alone because statutory
post-judgment interest and later events may apply.

## Compatibility

- Treat unknown fields as additive.
- Preserve source arrays and retained byte/OCR metadata when merging updates.
- Normalize case-number punctuation for lookup, but retain court display values.
- Do not convert restricted rows into missing or discovered rows.
- Do not treat inferred classifications as clerk or court findings.
