# San Francisco Superior Court Data Archive

This repository publishes the court-record data used by the independent
[SFSC viewer](https://sfsc.amyc.us/). It contains data only: private capture,
credential, deployment, and host configuration are not part of this repository.

## Archive Coverage

The compact case directory currently describes 1,581,131 visible rows:
1,446,257 captured case records, 134,874 restricted or unavailable records,
and 0 discovered backlog rows. Restricted rows remain visible in the directory;
they are not hidden or reported as missing cases.

Counts and per-prefix/year coverage are recorded in
[`archive/case-directory/manifest.json`](archive/case-directory/manifest.json).
The searchable presentation is available at
[sfsc.amyc.us](https://sfsc.amyc.us/#/cases).

## Repository Layout

| Path | Contents |
|---|---|
| `archive/cases/<PREFIX>/<YY>/<CASE>.json` | Canonical record for one captured case |
| `archive/case-directory/` | Compact prefix/year browse and lookup shards |
| `archive/new-filings-cases/` | Clerk new-filings discovery rows by prefix and year |
| `archive/cases-index.ndjson` | Compatibility summary index for captured cases |
| `archive/unavailable-cases.ndjson` | Explicit restricted or unavailable case observations |

For example, `CGC23605428` is stored at
[`archive/cases/CGC/23/CGC23605428.json`](archive/cases/CGC/23/CGC23605428.json).
Consumers should derive the two-digit shard from the first two digits following
the alphabetic prefix and may retain a flat-path fallback for older snapshots.

See [`docs/data-schema.md`](docs/data-schema.md) for field definitions and
compatibility rules.

## Scope

Records may include the register of actions, parties, attorneys, document
metadata, calendars, payments, clerk case categories, criminal case headers and
ROA rows, and case-type-specific fields such as decreed names. Availability
varies by case and by what the court makes viewable.

Document metadata is distinct from archived document bytes. A document row can
be present while its bytes are deferred, unavailable, or stored as a release
asset identified by content metadata in the record.

SFSC is not an official court system and is not affiliated with or operated by
the San Francisco Superior Court. Derived classifications shown by the viewer
are research aids, not court findings.
