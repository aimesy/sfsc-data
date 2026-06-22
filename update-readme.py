#!/usr/bin/env python3
"""Regenerate README.md from tentatives.parquet with one section per department."""

import argparse
import json
import os
import subprocess
import urllib.request
import pandas as pd
from datetime import date, timedelta
from pathlib import Path

from scripts.court_holidays import ca_court_holidays as shared_ca_court_holidays

HERE     = Path(__file__).parent
README   = HERE / 'README.md'
LIVE     = HERE / 'LIVE.md'
COVERAGE = HERE / 'coverage'
DATA_DIR = HERE / 'data'
CASE_TABLE_STATS = DATA_DIR / 'case-table-stats.json'
CASES_INDEX = HERE / 'archive' / 'cases-index.ndjson'
CASE_DIRECTORY_MANIFEST = HERE / 'archive' / 'case-directory' / 'manifest.json'
DOCUMENT_INDEX = HERE / 'archive' / 'document-index.ndjson'
PRODUCT_REPO = 'aimesy/sfsc'

LIVE_COUNTS_START = '<!-- sfsc-live-counts:start -->'
LIVE_COUNTS_END = '<!-- sfsc-live-counts:end -->'
LIVE_MANIFEST_URL = 'https://sfsc.amyc.us/data/manifest.json'
LIVE_CASE_TABLE_STATS_URL = 'https://sfsc.amyc.us/data/case-table-stats.json'
LIVE_CASES_INDEX_URL = 'https://sfsc.amyc.us/archive/cases-index.ndjson'
LIVE_CASE_DIRECTORY_MANIFEST_URL = 'https://sfsc.amyc.us/archive/case-directory/manifest.json'
LIVE_DOCUMENT_INDEX_URL = 'https://sfsc.amyc.us/archive/document-index.ndjson'

DEPT_NAMES = {
    # Map each SFSC department number to its full name. Departments not in
    # this map fall back to the generic "Department <N>" label, so adding a
    # new dept never breaks anything; it just shows up unnamed until you
    # extend this dict.
    '204': 'Department 204 - Probate',
    '301': 'Department 301 - Discovery',
    '302': 'Department 302 - Civil Law and Motion',
    '304': 'Department 304 - Asbestos Law and Motion',
    '501': 'Department 501 - Real Property Court',
}

# Dept 304 hosts two sub-calendars on different days. The data browser
# merges them into a single Department 304 view, but the README's
# section-per-dept layout splits them so contributors can see each
# sub-calendar's gaps independently. Each tuple is
#   (calendar_kind, "<sub-folder name>", "Display name for the section").
DEPT_SUB_CALENDARS = {
    '304': [
        ('law-and-motion', 'law-and-motion', 'Department 304 - Asbestos Law and Motion'),
        ('discovery', 'discovery', 'Department 304 - Asbestos Discovery'),
    ],
}

# Per-calendar floor: ignore parquet rows and raw-scrape dates older than this
# when computing coverage / gaps. SFSC's tentative endpoint has calendar-specific
# historical floors; pre-floor empty markers are proof of absence, not gaps.
# Dept 304 is split by sub-calendar, so keys may be plain depts ("302") or
# "<dept>/<subfolder>" ("304/law-and-motion").
DEPT_DATA_FLOORS = {
    '204': '2000-04-05',
    '301': '2024-03-19',
    '302': '2001-10-22',
    '304/law-and-motion': '2010-01-11',
    '304/discovery': '2010-01-12',
    '501': '2011-10-18',
}

DEPT_AVAILABILITY_NOTES = {
    '204': 'Department 204 Probate tentatives are not available online before 2000-04-05; earlier dates are excluded from gap-finding and bulk scraping.',
    '301': 'Department 301 Discovery tentatives are not available online before 2024-03-19; earlier dates are excluded from gap-finding and bulk scraping.',
    '302': 'Department 302 Civil Law and Motion tentatives are not available online before 2001-10-22; earlier dates are excluded from gap-finding and bulk scraping.',
    '304/law-and-motion': 'Department 304 Asbestos Law and Motion tentatives are not available online before 2010-01-11; earlier dates are excluded from gap-finding and bulk scraping.',
    '304/discovery': 'Department 304 Asbestos Discovery tentatives are not available online before 2010-01-12; earlier dates are excluded from gap-finding and bulk scraping.',
    '501': 'Department 501 Real Property tentatives are not available online before 2011-10-18; earlier dates are excluded from gap-finding and bulk scraping.',
}


def read_git_blob(git_path: str) -> str | None:
    """Read a tracked file without forcing it into a sparse checkout."""
    try:
        completed = subprocess.run(
            ['git', 'show', f'HEAD:{git_path}'],
            cwd=HERE,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return completed.stdout


def current_repo_slug() -> str:
    env_repo = os.environ.get('GITHUB_REPOSITORY', '').strip().lower()
    if env_repo:
        return env_repo
    try:
        completed = subprocess.run(
            ['git', 'config', '--get', 'remote.origin.url'],
            cwd=HERE,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ''
    remote = completed.stdout.strip().lower()
    remote = remote.removesuffix('.git')
    for prefix in ('https://github.com/', 'http://github.com/', 'git@github.com:'):
        if remote.startswith(prefix):
            return remote[len(prefix):]
    return ''


def read_url_text(url: str) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'sfsc-readme-counts'})
    with urllib.request.urlopen(req, timeout=120) as res:
        return res.read().decode('utf-8')


def manifest_counts(dept_stats: list[dict] | None = None) -> tuple[int, str]:
    if dept_stats is not None:
        rulings = sum(int(d.get('rulings') or 0) for d in dept_stats)
        latest = max((d.get('latest') or '') for d in dept_stats)
        return rulings, latest

    manifest_path = DATA_DIR / 'manifest.json'
    if manifest_path.exists():
        raw = manifest_path.read_text(encoding='utf-8')
    else:
        raw = read_git_blob('data/manifest.json') or read_url_text(LIVE_MANIFEST_URL)
    manifest = json.loads(raw)
    departments = manifest.get('departments') or []
    rulings = sum(int(d.get('rulings') or 0) for d in departments)
    latest = max((d.get('latest') or '') for d in departments)
    return rulings, latest


def case_index_counts() -> tuple[int, int, int]:
    if CASES_INDEX.exists():
        raw = CASES_INDEX.read_text(encoding='utf-8')
    else:
        raw = read_git_blob('archive/cases-index.ndjson') or read_url_text(LIVE_CASES_INDEX_URL)

    dockets = filings = docket_entries = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        dockets += 1
        row = json.loads(line)
        filings += int(row.get('n_documents') or 0)
        docket_entries += int(row.get('n_entries') or 0)
    return dockets, filings, docket_entries


def case_table_stats_counts() -> tuple[int, int, int]:
    if CASE_TABLE_STATS.exists():
        raw = CASE_TABLE_STATS.read_text(encoding='utf-8')
    else:
        try:
            raw = read_git_blob('data/case-table-stats.json') or read_url_text(LIVE_CASE_TABLE_STATS_URL)
        except Exception:
            return 0, 0, 0
    stats = json.loads(raw)
    return (
        int(stats.get('cases') or 0),
        int(stats.get('case_documents') or 0),
        int(stats.get('docket_entries') or 0),
    )


def case_directory_docket_count() -> int:
    if CASE_DIRECTORY_MANIFEST.exists():
        raw = CASE_DIRECTORY_MANIFEST.read_text(encoding='utf-8')
    else:
        raw = (
            read_git_blob('archive/case-directory/manifest.json')
            or read_url_text(LIVE_CASE_DIRECTORY_MANIFEST_URL)
        )

    manifest = json.loads(raw)
    source_counts = manifest.get('source_counts') or {}
    source_rows = max(
        int(source_counts.get('case_json_rows') or 0),
        int(source_counts.get('case_table_rows') or 0),
        int(source_counts.get('case_index_rows') or 0),
    )
    directory_rows = (
        int(manifest.get('case_count') or 0)
        + int(manifest.get('restricted_count') or 0)
        + int(manifest.get('indexed_count') or 0)
    )
    return max(source_rows, directory_rows)


def document_index_bytes() -> int:
    if DOCUMENT_INDEX.exists():
        raw = DOCUMENT_INDEX.read_text(encoding='utf-8')
    else:
        raw = read_git_blob('archive/document-index.ndjson') or read_url_text(LIVE_DOCUMENT_INDEX_URL)

    total = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        total += int(row.get('bytes_len') or row.get('bytes') or 0)
    return total


def archive_counts(dept_stats: list[dict] | None = None) -> dict:
    rulings, latest_ruling_date = manifest_counts(dept_stats)
    dockets, filings, docket_entries = case_index_counts()
    table_dockets, table_filings, table_docket_entries = case_table_stats_counts()
    dockets = max(dockets, table_dockets)
    filings = max(filings, table_filings)
    docket_entries = max(docket_entries, table_docket_entries)
    dockets = max(dockets, case_directory_docket_count())
    document_bytes = document_index_bytes()
    return {
        'rulings': rulings,
        'latest_ruling_date': latest_ruling_date,
        'dockets': dockets,
        'documents': filings,
        'filings': filings,
        'docket_entries': docket_entries,
        'document_bytes': document_bytes,
        'archive_mb': round(document_bytes / (1024 * 1024)),
    }


def render_live_table(stats: dict) -> str:
    return f"""\
## LIVE

| Metric | Count |
|---|---:|
| Tentative rulings | {stats['rulings']:,} |
| Dockets | {stats['dockets']:,} |
| Case documents | {stats['documents']:,} |
| Docket entries | {stats['docket_entries']:,} |
| Archive size | {stats['archive_mb']:,} MB |
| Latest tentative ruling | {stats['latest_ruling_date']} |

Generated by `update-readme.py` from `data/manifest.json` and
`data/case-table-stats.json` plus `archive/case-directory/manifest.json`, with
compatibility aggregates from `archive/cases-index.ndjson` and
`archive/document-index.ndjson`. Refresh with
`python update-readme.py --live-counts-only`.
"""


def write_live_file(stats: dict) -> None:
    LIVE.write_text(render_live_table(stats), encoding='utf-8')


def render_live_counts(stats: dict) -> str:
    return f"""\
{LIVE_COUNTS_START}
{render_live_table(stats).rstrip()}
{LIVE_COUNTS_END}
"""


def with_live_counts(content: str, stats: dict) -> str:
    block = render_live_counts(stats)
    start = content.find(LIVE_COUNTS_START)
    end = content.find(LIVE_COUNTS_END)
    if start != -1 and end != -1 and end > start:
        tail_start = end + len(LIVE_COUNTS_END)
        return content[:start] + block + '\n' + content[tail_start:].lstrip('\n')

    title = '# SFSC Public-Record Archive\n\n'
    if content.startswith(title):
        return title + block + '\n' + content[len(title):]
    return block + '\n' + content


def refresh_live_counts_only() -> None:
    if not README.exists():
        raise SystemExit('README.md not found')
    stats = archive_counts()
    write_live_file(stats)
    README.write_text(
        with_live_counts(README.read_text(encoding='utf-8'), stats),
        encoding='utf-8',
    )


STATIC_TOP = """\
# SFSC Public-Record Archive

This repository is a public-record backup and research index for the San
Francisco Superior Court. It began as a searchable database of tentative rulings,
but it now has two connected layers:

1. **Tentative rulings.** The static viewer at
   <https://sfsc.amyc.us/> searches and filters SFSC
   tentative rulings from Departments 204, 301, 302, 304, and 501.
2. **Case records seeded by those rulings.** The capture tools preserve the
   court's case-information record for selected cases: register-of-actions
   entries, document metadata, parties, attorneys, calendars, payments,
   document bytes, OCR, case status, litigant indexes, fee/financial data, and
   related appellate/writ signals where the scripts can identify them.

The point is preservation first, search second. SFSC posts important public
records through interfaces that are hard to search, easy to lose track of, and
not designed as research datasets. This repo keeps the raw captures, derived
indexes, and static browser together so researchers can inspect the record
without depending on a single live UI path.

This is not an official court system. Capture runs only in the operator's own
authenticated browser or WebView session, uses the operator's own GitHub token,
and stores public-record material for archival and research use.

**[Open the searchable viewer](https://sfsc.amyc.us/)**

## Schemas and Provenance

- [Data schema and provenance](docs/data-schema.md) lists the current files,
  variables, source fields, derivation rules, and normalized Parquet tables.
- [Design and operations](DESIGN.md) is the canonical architecture for capture,
  promotion, enrichment, viewer loading, and recovery.
- [Harvest invariants](docs/sfsc-harvest-invariants.md) records the operational
  rules that keep document-bearing case records complete.

## What Is Here

### Tentative-rulings corpus

The original corpus covers these SFSC tentative ruling calendars:

- Department 204 - Probate
- Department 301 - Discovery
- Department 302 - Civil Law and Motion
- Department 304 - Asbestos Law and Motion / Asbestos Discovery
- Department 501 - Real Property

Known online floors for the supported calendars:

| Calendar | First non-empty online tentative date |
|---|---:|
| Department 204 - Probate | 2000-04-05 |
| Department 301 - Discovery | 2024-03-19 |
| Department 302 - Civil Law and Motion | 2001-10-22 |
| Department 304 - Asbestos Law and Motion | 2010-01-11 |
| Department 304 - Asbestos Discovery | 2010-01-12 |
| Department 501 - Real Property | 2011-10-18 |

Raw daily scrapes live in `raw/dept<N>/` in the companion
[`aimesy/sfsc-data`](https://github.com/aimesy/sfsc-data) repository.
`ingest.py` merges them into the canonical `tentatives.parquet`;
`update-readme.py` emits the per-department browser slices in
`data/tentatives-<N>.parquet`, lazy-loaded extras files, coverage files, the
manifest, and the department summary below.

### Case-record archive

`archive/cases/<case_number>.json` in
[`aimesy/sfsc-data`](https://github.com/aimesy/sfsc-data) is the per-case record captured from SFSC's
Case Information portal. JSON is the canonical capture/salvage format: it keeps
the court tabs, byte metadata, availability notices, and provenance together for
one case. Normalized analytics exports are generated separately as Parquet by
`scripts/build_case_tables.py`. Counts and aggregate claims should use those
Parquet tables, not ad hoc walks over nested JSON. The capture model includes:

- docket / register-of-actions rows
- document rows, stable DocIDs, availability notices, SHA-256 hashes, and
  archived byte-object metadata for captured document bytes
- parties, attorneys, calendars, payments, and cause of action
- provenance fields showing when and how the case record was captured

Document-bearing captures are byte-first under the active first-pass scope:
complaints/petitions and court orders must have archived byte-object metadata
before the case is promoted; other available documents are marked explicitly
deferred for a later full pass. New scanner/promoter document bytes are stored
as content-addressed GitHub Release assets (`docs-YYYY-MM-DD` shards) and the
case JSON records `release_tag`, `asset_name`, `sha256`, size, and MIME type.
Existing `archive/documents/**` git-object records remain readable as historical
or explicit repair material, but new git-object writes require an explicit
repair flag.

### Derived research data

The companion data repository's committed `data/` directory and workflow
artifacts contain derived indexes used by the viewer and the harvest tools. Some
products below are committed when current data is available; others are
generated by the scripts/workflows and loaded by the viewer when present.

- `cases.parquet`, `docket_entries.parquet`, `parties.parquet`,
  `attorneys.parquet`, `representation.parquet`, `calendar.parquet`, and
  `payments.parquet` - normalized case-record exports flattened from
  `archive/cases/*.json`
- `documents.parquet` - normalized document rows joined to byte/OCR metadata
- `tags.parquet` - deterministic/tentative ruling tags
- `vexatious.parquet` - authoritative vexatious litigant signals
- `case_status.parquet` and `satisfied_cases.txt` - status classification and
  rescan skip inputs
- `litigants.parquet` / `litigants.json` - cross-case litigant aggregation
- `financials.parquet` - fees, judgments, renewals, and related monetary
  entries extracted from tentative ruling text; payment rows remain preserved in
  the per-case JSON captures
- `causes.parquet` - high-precision named cause-of-action mentions harvested
  from docket, document-title, and tentative-ruling text
- `estate_roles.parquet` / `estate_events.parquet` - generated probate dossier
  rows for estate, conservatorship, guardianship, and trust proceedings
- `case_outcomes.parquet`, `tentative_dispositions.parquet`, and
  `profile-metrics.json` - generated inferred outcome, motion-disposition, and
  profile roll-ups; these are fuzzy research signals, not court findings
- `data/ocr/` - ignored local/workflow-artifact OCR cache for captured document bytes

The scripts in `scripts/` rebuild these products. The GitHub workflows run the
same scripts after raw captures land.

## Viewer

Open <https://sfsc.amyc.us/>. Use the **Database
Downloads** menu to select the departments you want to load. Once loaded, the
viewer supports:

- full-text search across rulings and case metadata
- date, department, judge, motion-type, outcome, and custom filters
- column filters and sorting
- row detail views with full ruling text, administrative/appearance blocks, and
  provenance links
- tag display with provenance and deterministic/tentative markers
- charts and CSV export
- case-record views for archived cases, including ROA, parties, attorneys,
  documents, OCR text, financial entries, and related indexes where available

Filters and the current page are encoded in the URL, so searches can be
bookmarked or shared.

## Capture Tools

### Tentative rulings browser extension

**[Download sfsc-extension.zip](https://github.com/aimesy/sfsc/raw/master/sfsc-extension.zip)**
(Firefox / Chrome — load unpacked or install via your browser's extension manager)
· [Source](extension/)

Captures tentative ruling pages from <https://webapps.sftc.org/tr/tr.dll> and
commits raw JSON to `raw/` in `aimesy/sfsc-data`. Supports single-page upload, bulk date-range
scraping, coverage-aware gap-finding, multiple independent SFSC tabs,
CAPTCHA/session-expiry pause-and-resume, row-count validation, and guards
against case-number/title mismatches.

### Android case archiver

**[Download APK](https://github.com/aimesy/sfsc/releases/download/apk-latest/app-debug.apk)**
([release page](https://github.com/aimesy/sfsc/releases/tag/apk-latest))
· [Source](android-case-archiver/)

Phone-native CaseInfo capture app. Runs the SFSC portal in a WebView, reuses
the operator's verified session, captures case JSON, and can stage first-pass
complaint/petition/order bytes locally. Release-backed byte upload is the
current archive storage path; until the Android release uploader is enabled,
scanner/promoter runs remain the primary completion path for document-bearing
captures.

The APK is a debug build auto-signed with the Android debug keystore for
sideloading onto your own device. Your GitHub PAT is entered on-device and
stored encrypted; it is never built into the APK. Reinstall over any existing
copy when the release page shows a newer build timestamp.

### Desktop verification grid (Windows)

[`tools/sfsc-webview-verifier/`](tools/sfsc-webview-verifier/) is a Windows
Forms app (C# + WebView2) that presents a grid of embedded browser slots for
verification handoffs during VPS scanner runs. It connects to the session
agent, receives challenge jobs from the scanner, and forwards verified sessions
back to the right worker, keeping each result tied to its originating job.

Build with `build.ps1` (requires .NET Framework and the WebView2 runtime).

### VPS / local scanner

`scripts/local_case_scanner.mjs` and `scripts/session_agent.mjs`, together with
the watcher, promoter, and telemetry dashboard scripts, support desktop- and
VPS-assisted harvests. The scanner fetches case JSON and first-pass document
bytes; the promoter commits completed records; the session agent brokers CAPTCHA
challenges to the desktop verifier. The same byte-first rule applies: no
metadata-only promotion for document-bearing cases.

### Case archive browser extension

[`case-archive-extension/`](case-archive-extension/) is an earlier browser
extension CaseInfo prototype. It captures case metadata while browsing the SFSC
portal. The Android app and scanner/promoter pipeline are the current
byte-first paths for document-bearing records.

### Appellate and official-list sidecars

[`appellate-extension/`](appellate-extension/),
`scripts/index_appeals.py`, `scripts/index_writs.py`,
`scripts/index_vexatious.py`, and `scripts/fetch_fee_schedules.py` collect or
derive related public-record signals. These sidecars connect trial-court events
to appellate review, official vexatious litigant records, and historical fee
schedules.

## For Developers / Archivists

<details>
<summary>Repository layout, data ingestion pipeline, and contribution mechanics</summary>

### Repo layout

| Path | What |
|------|------|
| `index.html` | Static browser for rulings, case records, OCR, tags, litigants, financials, and related indexes |
| `tentatives.parquet` | Canonical dataset of tentative rulings (all departments; data repo) |
| `aimesy/sfsc-data:raw/dept<N>/` | Per-day raw tentative ruling scrapes, organized by department and sub-calendar where needed |
| `aimesy/sfsc-data:coverage/dept<N>.json` | Dates covered by parquet rows plus raw filenames; used by scraper gap-finding |
| `aimesy/sfsc-data:data/tentatives-<N>.parquet` | Per-department browser slice |
| `aimesy/sfsc-data:data/*` | Derived browser/index products: normalized case tables, documents, tags, status, litigants, financials, OCR, vexatious signals, fee schedules |
| `archive/README.md` | Pointer to the companion `aimesy/sfsc-data` archive repository |
| `aimesy/sfsc-data:archive/cases/` | Canonical captured SFSC case records, one JSON file per case |
| `aimesy/sfsc-data:archive/cases-index.ndjson` | Append-oriented provenance index for captured cases |
| `docs/data-schema.md` | Full schema, variable, and provenance guide for committed and target data products |
| `extension/` | Tentative ruling capture extension (download: `sfsc-extension.zip`) |
| `android-case-archiver/` | Phone-native CaseInfo capture app (download: release `apk-latest`) |
| `case-archive-extension/` | Browser-based CaseInfo capture extension prototype |
| `appellate-extension/` | California appellate docket/opinion capture prototype |
| `tools/sfsc-webview-verifier/` | Windows verification grid (C# + WebView2) for VPS scanner runs |
| `scripts/` | Ingest, enrichment, OCR, scanner, session agent, watcher, promoter, and index-building scripts |
| `.github/workflows/` | Automation for ingest, extension/APK builds, enrichment, OCR, status, litigants, judges, fee schedules |
| `DESIGN.md` | Canonical design and operations document |

### Ingest

Raw JSON belongs in `aimesy/sfsc-data:raw/dept<N>/`; ingest/build jobs must run
from that data repo, not from the product repo. The ingest workflow throttles
back-to-back runs: every push queues a run,
runs execute sequentially via the concurrency group, and any run within 60
seconds of the last bot commit exits fast. A 50-file bulk-scrape burst therefore
collapses to roughly one ingest plus quick no-ops. Each pass diffs against the
last bot commit, so any file that a previous run missed gets picked up
automatically; `workflow_dispatch` with `mode: all-raw` re-ingests every raw JSON
if a deeper repair is needed.

Local:

```bash
pip install pandas pyarrow openpyxl
python ingest.py raw/dept302/2026-04-28-120000.json
```

To regenerate the per-department parquets, coverage files, and department sections below:

```bash
pip install pandas pyarrow holidays
python update-readme.py
```

To regenerate the normalized case-record Parquet exports from the canonical case
JSON:

```bash
pip install pandas pyarrow
python scripts/build_case_tables.py
python scripts/build_document_index.py
```

</details>

---

"""

def ca_court_holidays(min_year: int, max_year: int) -> set[str]:
    """Return ISO date strings for California court holidays in the given year range.

    Combines CA state public holidays with federal government holidays (to
    capture Columbus Day), then manually adds Lincoln's Birthday (Feb 12),
    which is a California legal holiday (Gov. Code § 6700) not included in
    the holidays library's CA subdivision.
    """
    return shared_ca_court_holidays(min_year, max_year)


def find_gap_runs(min_date: str, max_date: str, checked: set,
                  court_holidays: set | None = None) -> list[tuple[str, str]]:
    """Returns (start, end) tuples for each gap of missing weekdays.

    Weekends and court holidays are skipped; they do not open or close a gap.
    A gap closes only when a weekday with data is encountered.
    """
    court_holidays = court_holidays or set()
    d   = date.fromisoformat(min_date)
    end = date.fromisoformat(max_date)
    runs = []
    run_start = run_end = None
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in court_holidays:
            if d.isoformat() not in checked:
                if run_start is None:
                    run_start = d.isoformat()
                run_end = d.isoformat()
            else:
                if run_start is not None:
                    runs.append((run_start, run_end))
                    run_start = run_end = None
        d += timedelta(days=1)
    if run_start is not None:
        runs.append((run_start, run_end))
    return runs

def format_gaps(runs: list[tuple[str, str]]) -> str:
    if not runs:
        return '_None - all weekdays in range are accounted for._'
    lines = []
    for start, end in runs:
        lines.append(f'- {start}' if start == end else f'- {start} to {end}')
    return '\n'.join(lines)

def scraped_dates_for_dept(dept: str, subfolder: str = '') -> set[str]:
    """Dates we have raw scrape evidence for, derived from filenames in
    raw/dept<N>/[<subfolder>/]. A date with a raw file is *not* a gap
    even if no rulings landed in the parquet for it (e.g. the page
    returned zero tentatives, or returned tentatives whose hearings are
    on a different date).

    `subfolder` scopes the walk to a single sub-calendar (used for
    Dept 304's per-kind coverage). Empty `subfolder` walks the
    top-level dept dir only, non-recursive, to keep
    sub-calendar files from contaminating the merged dept-level
    coverage."""
    raw_dir = HERE / 'raw' / f'dept{dept}'
    if subfolder:
        raw_dir = raw_dir / subfolder
    if not raw_dir.is_dir():
        return set()
    out = set()
    for p in raw_dir.glob('*.json'):
        stem = p.stem
        if len(stem) >= 10 and stem[4] == '-' and stem[7] == '-':
            out.add(stem[:10])
    return out


def dept_section(dept: str, df_dept: pd.DataFrame,
                 *, kind: str | None = None,
                 subfolder: str = '',
                 display_name: str | None = None) -> str:
    """Render one collapsible <details> block for a department (or one
    sub-calendar of a department). `kind` filters df_dept to rows whose
    `calendar_kind` equals it; `subfolder` scopes the raw-file scan;
    `display_name` overrides the DEPT_NAMES default for the section
    header (used for Dept 304 sub-calendar splits)."""
    if kind is not None and 'calendar_kind' in df_dept.columns:
        df_dept = df_dept[df_dept['calendar_kind'] == kind]
    name    = display_name or DEPT_NAMES.get(dept, f'Department {dept}')
    count   = len(df_dept)
    # `dates`: hearing dates that produced rulings, the meaningful coverage
    #          for someone searching the archive ("days with data").
    # `scraped`: dates we have a raw scrape file for, including days the
    #            court posted nothing, which still close gaps but aren't
    #            useful as search anchors.
    # The collapsed summary leads with first/last day-with-data because
    # that's what users actually care about; the harvest extents and gap
    # mechanics live inside as an aside.
    dates   = set(df_dept['court_date'].unique())
    scraped = scraped_dates_for_dept(dept, subfolder=subfolder)
    # Apply the per-dept floor: if a department only began publishing
    # tentatives online on a given date, anything before it is excluded
    # from gap calculation entirely (otherwise three pre-floor empty-marker
    # files manufacture a multi-year fake "gap").
    floor_key = f'{dept}/{subfolder}' if subfolder else dept
    floor = DEPT_DATA_FLOORS.get(floor_key)
    if floor:
        dates   = {d for d in dates   if d >= floor}
        scraped = {d for d in scraped if d >= floor}
    checked = dates | scraped

    if not checked:
        # For a sub-calendar (kind set) we still render an empty section
        # so the reader sees "this calendar exists, no data yet"; that
        # was the whole point of separate counters for Dept 304's two
        # sub-calendars. For a top-level dept with no data anywhere
        # we'd genuinely have nothing useful to show.
        if kind is None:
            return ''
        summary = (f'<strong>{name}</strong>'
                   f' | 0 rulings'
                   f' | no scans yet')
        avail_note = DEPT_AVAILABILITY_NOTES.get(floor_key, '')
        body = ('\n_No rulings or in-floor scans have landed for this sub-calendar yet. '
                'Once the extension records its first in-floor scrape it will start '
                'showing here, and gaps will be enumerated against the '
                'court\'s posted hearing days._\n')
        if avail_note:
            body += f'\n> _{avail_note}_\n'
        return f'<details>\n<summary>{summary}</summary>\n{body}</details>\n'

    earliest_harvest = min(checked)
    latest_harvest   = max(checked)
    earliest_data    = min(dates) if dates else earliest_harvest
    latest_data      = max(dates) if dates else latest_harvest
    n_days_data      = len(dates)
    n_days_scanned   = len(checked)

    holidays = ca_court_holidays(int(earliest_harvest[:4]), int(latest_harvest[:4]))
    gaps     = find_gap_runs(earliest_harvest, latest_harvest, checked, holidays)
    n_gaps   = len(gaps)

    # Total weekdays (excluding court holidays) inside the harvest window:
    # the denominator for "X of Y weekdays scanned".
    holidays_within_data = ca_court_holidays(int(earliest_data[:4]), int(latest_data[:4]))
    weekdays_in_data_range = sum(
        1 for d in pd.date_range(earliest_data, latest_data)
        if d.weekday() < 5 and d.strftime('%Y-%m-%d') not in holidays_within_data
    ) if dates else 0

    # Markdown bold (`**...**`) inside a <summary> tag is rendered literally
    # by GitHub; the asterisks show up as text. Use <strong> so the
    # department name renders bold in the collapsed header.
    summary = (f'<strong>{name}</strong>'
               f' | {count:,} rulings'
               f' | {earliest_data} to {latest_data}'
               f' | {n_days_data:,} hearing day{"s" if n_days_data != 1 else ""}'
               f' | {n_gaps} gap{"s" if n_gaps != 1 else ""}')

    coverage_pct = (n_days_data / weekdays_in_data_range * 100) if weekdays_in_data_range else 0

    # Surface the floor note inside the collapsible body so a reader doesn't
    # wonder why the gap list excludes a long pre-online stretch.
    avail_note = DEPT_AVAILABILITY_NOTES.get(floor_key, '')

    body = f"""\

{count:,} tentative rulings across {n_days_data:,} hearing day{"s" if n_days_data != 1 else ""} ({earliest_data} to {latest_data}).
{f'{chr(10)}> _{avail_note}_{chr(10)}' if avail_note else ''}
### Coverage

- **Hearing days with data:** {n_days_data:,} of {weekdays_in_data_range:,} weekdays in range ({coverage_pct:.1f}%)
- **Days scanned:** {n_days_scanned:,} (including days the court posted no rulings)
- **Earliest harvested:** {earliest_harvest}{' (same as first hearing day)' if earliest_harvest == earliest_data else ''}
- **Latest harvested:** {latest_harvest}{' (same as last hearing day)' if latest_harvest == latest_data else ''}

### Gaps ({n_gaps})

{format_gaps(gaps)}

"""

    return f'<details>\n<summary>{summary}</summary>\n{body}</details>\n'

def write_coverage(dept: str, df_dept: pd.DataFrame,
                   *, kind: str | None = None, subfolder: str = ''):
    """Write coverage/dept<N>[-<subfolder>].json as the union of dates
    that appear in the parquet (court_date) and dates with a raw
    scrape file in this dept (or sub-calendar). The browser extension
    uses this to decide which dates still need scraping; without it,
    the extension only sees raw filenames and treats every
    parquet-only date as unscanned (historical Excel imports
    populated 2017-2024 rulings without any raw files).

    For Dept 304 this is called twice, once per sub-calendar, so
    the extension's "scan unscanned" check on an Asbestos Discovery
    page only counts dates already scraped on the discovery
    sub-calendar, and likewise for Law and Motion."""
    if kind is not None and 'calendar_kind' in df_dept.columns:
        df_dept = df_dept[df_dept['calendar_kind'] == kind]
    parquet_dates = set(df_dept['court_date'].dropna().unique())
    file_dates    = scraped_dates_for_dept(dept, subfolder=subfolder)
    covered       = sorted(parquet_dates | file_dates)
    COVERAGE.mkdir(exist_ok=True)
    fname = f'dept{dept}-{subfolder}.json' if subfolder else f'dept{dept}.json'
    out = COVERAGE / fname
    out.write_text(json.dumps({
        'department': dept,
        'calendar_kind': kind,
        'covered':    covered,
        'min':        covered[0] if covered else None,
        'max':        covered[-1] if covered else None,
        'count':      len(covered),
    }, indent=0, separators=(',', ':')), encoding='utf-8')


def write_dept_parquet(dept: str, df_dept: pd.DataFrame):
    """Write data/tentatives-<N>.parquet, a single-department slice the
    browser can fetch on demand. Two parquets are emitted per dept:

    - tentatives-<N>.parquet (main): everything the table view needs,
      with ruling_substantive promoted to `ruling`. No admin /
      courtcall. Those bytes are deferred to the extras file.
    - tentatives-<N>-extras.parquet (sidecar): row_hash + ruling_admin
      + ruling_courtcall. The data browser fetches this only when a
      user opens a modal and expands the admin / CourtCall
      collapsible.

    The combined tentatives.parquet stays unchanged (canonical, with
    all three split columns) for anyone scripting against it directly.
    """
    DATA_DIR.mkdir(exist_ok=True)
    df_dept = df_dept.reset_index(drop=True).copy()

    # Fall back gracefully if the canonical parquet pre-dates the
    # ruling-split columns. In that case we ship the original ruling
    # in the main file and emit no extras file.
    has_splits = all(c in df_dept.columns for c in (
        'ruling_substantive', 'ruling_admin', 'ruling_courtcall'))

    main_out = DATA_DIR / f'tentatives-{dept}.parquet'
    if has_splits:
        main = df_dept.drop(columns=['ruling_admin', 'ruling_courtcall']).copy()
        # Promote the substantive split into the user-facing `ruling`
        # column so the browser doesn't have to know about the
        # split-column convention.
        main['ruling'] = main['ruling_substantive']
        main = main.drop(columns=['ruling_substantive'])
        main.to_parquet(main_out, index=False, compression='zstd')

        extras_out = DATA_DIR / f'tentatives-{dept}-extras.parquet'
        extras = df_dept[['row_hash', 'ruling_admin', 'ruling_courtcall']].copy()
        # Drop rows with neither admin nor courtcall; keeps the
        # sidecar small and the lookup-by-row_hash cheap on the
        # browser side.
        keep = (extras['ruling_admin'].fillna('').ne('')
                | extras['ruling_courtcall'].fillna('').ne(''))
        extras[keep].to_parquet(extras_out, index=False, compression='zstd')
    else:
        df_dept.to_parquet(main_out, index=False, compression='zstd')


def write_manifest(dept_stats: list[dict]):
    """Write data/manifest.json; describes each per-dept parquet so the
    browser can populate the Database Downloads dropdown without hard-coding
    department numbers."""
    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / 'manifest.json'
    out.write_text(json.dumps({
        'departments': dept_stats,
    }, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--live-counts-only',
        action='store_true',
        help='Refresh only the top README live-counts block.',
    )
    args = parser.parse_args()

    if args.live_counts_only:
        refresh_live_counts_only()
        print('Updated README.md live archive counts')
        return

    if (
        current_repo_slug() == PRODUCT_REPO
        and os.environ.get('SFSC_ALLOW_PRODUCT_README_REBUILD') != '1'
    ):
        raise SystemExit(
            'Refusing full data README regeneration from aimesy/sfsc. '
            'Run this from aimesy/sfsc-data, or use --live-counts-only for the product README.'
        )

    if not (HERE / 'tentatives.parquet').exists():
        refresh_live_counts_only()
        print('tentatives.parquet not found; updated README.md live archive counts only')
        return

    df = pd.read_parquet(HERE / 'tentatives.parquet')
    # Coerce so a malformed date string can't abort the whole regeneration with an
    # opaque parse error, and drop (loudly) any rows whose date won't parse so a
    # stray NaT can't pollute the date sets / min-max comparisons downstream.
    parsed = pd.to_datetime(df['court_date'], errors='coerce')
    n_bad = int(parsed.isna().sum())
    if n_bad:
        print(f'warning: dropping {n_bad} row(s) with unparseable court_date')
    df = df[parsed.notna()].copy()
    df['court_date'] = parsed[parsed.notna()].dt.date.astype(str)

    sections = ''
    dept_stats = []
    for dept in sorted(df['department'].unique()):
        sub = df[df['department'] == dept]
        # Always emit a single per-dept parquet + manifest entry. The
        # data browser shows departments as one row each, with sub-
        # calendar (if any) preserved as a column for downstream tooling.
        write_dept_parquet(dept, sub)
        size_bytes = (DATA_DIR / f'tentatives-{dept}.parquet').stat().st_size
        # Extras parquet (admin + courtcall, lazy-loaded by the data
        # browser when the user opens a modal and expands the
        # collapsible). Optional, older parquets without the split
        # columns won't have a sidecar file.
        extras_path = DATA_DIR / f'tentatives-{dept}-extras.parquet'
        extras_size = int(extras_path.stat().st_size) if extras_path.exists() else None
        latest = sub['court_date'].max() if not sub.empty else None
        entry = {
            'department': dept,
            'name':       DEPT_NAMES.get(dept, f'Department {dept}'),
            'rulings':    int(len(sub)),
            'size_bytes': int(size_bytes),
            'latest':     latest,
        }
        if extras_size is not None:
            entry['extras_size_bytes'] = extras_size
        dept_stats.append(entry)
        # Sub-calendar split (currently only Dept 304): emit a separate
        # README section + coverage file per sub-calendar so contributors
        # can see each sub-calendar's gaps independently. The data
        # browser still merges them into a single Department 304 view.
        subcals = DEPT_SUB_CALENDARS.get(dept)
        if subcals:
            for kind, subfolder, display_name in subcals:
                sections += dept_section(dept, sub,
                                         kind=kind, subfolder=subfolder,
                                         display_name=display_name)
                write_coverage(dept, sub, kind=kind, subfolder=subfolder)
            # Also write the merged dept-level coverage so any
            # downstream tooling that asks for coverage/dept304.json
            # (without a sub-calendar suffix) still works.
            write_coverage(dept, sub)
        else:
            sections += dept_section(dept, sub)
            write_coverage(dept, sub)
    write_manifest(dept_stats)

    live_stats = archive_counts(dept_stats)
    write_live_file(live_stats)
    content = with_live_counts(
        STATIC_TOP + '## Departments\n\n' + sections,
        live_stats,
    )
    README.write_text(content, encoding='utf-8')
    print(f'Updated README.md, coverage/, data/ for {len(dept_stats)} department(s)')

if __name__ == '__main__':
    main()
