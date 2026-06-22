#!/usr/bin/env python3
"""Refresh the California civil-court fee-schedule index.

Authoritative source (Judicial Council of California):
    https://courts.ca.gov/news-reference/reports-publications/civil-fees

Civil filing/motion/jury/etc. fees are set statewide and CHANGE OVER TIME.
A fee waived in 2017 must be repaid (Gov. Code §68637) at the 2017 rate, not
at today's rate, so the §7 fee-waiver roll-ups (DESIGN.md §7) need the
historical schedule that was IN EFFECT on each waiver's date — never the
current one.

This script does NOT extract fee amounts. Reading the PDFs and translating
them into a `data/fee_schedules/*.json` rate table is a human-reviewed task
(per CLAUDE.md "cite, verbatim, or don't assert it"). What this DOES:

  1. Fetch the index page.
  2. Enumerate every linked PDF (anchor href ending `.pdf`, absolute-ified).
  3. For each PDF, download to `sources/fee-schedules/<sha256>.pdf` IF we
     don't already have those bytes; record sha256, byte length, link text,
     and the date the link was last seen on the index.
  4. Reconcile against `data/fee_schedules/index.json`:
       * NEW links (sha256 we haven't seen) → archived + listed in
         `pending_review.new`.
       * VANISHED links (in our index, no longer on the page) → moved into
         `pending_review.gone` (kept in `archived`; do not delete bytes).
       * CHANGED bytes at a stable URL (same href, new sha256) → flagged in
         `pending_review.changed`.
  5. Write `data/fee_schedules/index.json` (committed) summarizing what we
     have on disk. `pending_review` is exactly the actionable diff for the
     human / next agent to extract amounts + effective dates from.

Pure stdlib (urllib + re + hashlib). Run by .github/workflows/fee-schedules.yml
annually + on workflow_dispatch.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_URL = "https://courts.ca.gov/news-reference/reports-publications/civil-fees"
INDEX_JSON = os.path.join(REPO_ROOT, "data", "fee_schedules", "index.json")
ARCHIVE_DIR = os.path.join(REPO_ROOT, "sources", "fee-schedules")
UA = "sfsc/fee-schedules-refresh (+https://github.com/aimesy/sfsc)"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def write_bytes_atomic(path: str, body: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.25 * (attempt + 1))
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_json_atomic(path: str, data: dict) -> None:
    body = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    write_bytes_atomic(path, body)


def validate_pdf_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith("courts.ca.gov"):
        raise ValueError(f"refusing non-courts.ca.gov PDF URL: {url}")
    if not parsed.path.lower().endswith(".pdf"):
        raise ValueError(f"refusing non-PDF URL: {url}")
    return url


def looks_like_pdf(body: bytes) -> bool:
    return body[:1024].lstrip().startswith(b"%PDF")


def fetch(url: str, *, binary: bool = False, expected_pdf: bool = False) -> bytes | str:
    if expected_pdf:
        validate_pdf_url(url)
    accept = "application/pdf,*/*;q=0.5" if expected_pdf else "*/*"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=120) as resp:
        content_type = resp.headers.get_content_type().lower()
        data = resp.read()
    if expected_pdf:
        if "pdf" not in content_type and not looks_like_pdf(data):
            raise ValueError(f"{url} returned non-PDF Content-Type {content_type!r}")
        if not looks_like_pdf(data):
            raise ValueError(f"{url} did not return PDF bytes")
    return data if binary else data.decode("utf-8", "replace")


def parse_pdf_links(index_html: str, base_url: str) -> list[dict]:
    """Return [{href, text}] for every anchor whose href ends with .pdf."""
    out, seen = [], set()
    for m in re.finditer(
        r'<a\b[^>]*\bhref\s*=\s*"([^"]+\.pdf(?:\?[^"]*)?)"[^>]*>(.*?)</a>',
        index_html, re.I | re.S,
    ):
        href = urllib.parse.urljoin(base_url, html.unescape(m.group(1)).strip())
        try:
            href = validate_pdf_url(href)
        except ValueError as exc:
            print(f"  ! skipped PDF link: {exc}", file=sys.stderr)
            continue
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        if href in seen:
            continue
        seen.add(href)
        out.append({"href": href, "text": text})
    return out


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_index() -> dict:
    if not os.path.exists(INDEX_JSON):
        return {"source": INDEX_URL, "updated": None, "archived": [], "pending_review": {}}
    with open(INDEX_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", default=None, help="Local index HTML (else fetch live)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    raw = (open(args.html, encoding="utf-8", errors="replace").read()
           if args.html else fetch(INDEX_URL))
    links = parse_pdf_links(raw, INDEX_URL)
    if not links:
        # Layout change OR an outage — never silently overwrite the index empty.
        print("No PDF links found on the index page — layout may have changed; aborting.",
              file=sys.stderr)
        return 1
    print(f"Index page: {len(links)} PDF links found.")

    idx = load_index()
    archived = {a["sha256"]: a for a in idx.get("archived", [])}
    by_href = {}
    for a in archived.values():
        by_href.setdefault(a.get("href"), []).append(a)

    today = dt.date.today().isoformat()
    new_records: list[dict] = []
    changed: list[dict] = []
    seen_sha: set[str] = set()
    live_hrefs: set[str] = {l["href"] for l in links}

    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    for link in links:
        href = link["href"]
        try:
            body = fetch(href, binary=True, expected_pdf=True)
        except Exception as exc:
            print(f"  ! fetch failed: {href} ({exc})", file=sys.stderr)
            continue
        sha = sha256_bytes(body)
        seen_sha.add(sha)

        if sha in archived:
            archived[sha]["last_seen"] = today
            # Link text on the page can change wording between years; refresh it.
            archived[sha]["text"] = link["text"]
            archived[sha]["href"] = href
            continue

        path = os.path.join(ARCHIVE_DIR, f"{sha}.pdf")
        if not args.dry_run:
            write_bytes_atomic(path, body)

        rec = {
            "sha256": sha,
            "bytes": len(body),
            "href": href,
            "text": link["text"],
            "archive_path": os.path.relpath(path, REPO_ROOT),
            "first_seen": today,
            "last_seen": today,
            # `effective_date` is filled by a human reviewer (or a later
            # extractor) — never by this fetcher. The PDF must be read.
            "effective_date": None,
            "supersedes": None,
            "notes": None,
        }
        archived[sha] = rec
        new_records.append(rec)

        # Same href as something we already had, but new bytes → the JCC
        # replaced the PDF behind a stable URL. Flag both.
        prior = [a for a in by_href.get(href, []) if a["sha256"] != sha]
        if prior:
            changed.append({"href": href, "old_sha256": [p["sha256"] for p in prior],
                            "new_sha256": sha})

    # Mark archived entries that aren't on the page anymore (do NOT delete).
    gone = []
    for sha, a in archived.items():
        if sha in seen_sha:
            continue
        if a.get("href") in live_hrefs:
            # Same URL still present but resolved to different bytes — covered
            # by `changed`; don't double-count as "gone".
            continue
        gone.append({"sha256": sha, "href": a.get("href"), "text": a.get("text")})

    idx["source"] = INDEX_URL
    idx["updated"] = today
    idx["archived"] = sorted(archived.values(),
                             key=lambda r: (r.get("first_seen") or "", r["sha256"]))
    idx["pending_review"] = {
        "checked_at": dt.datetime.utcnow().isoformat() + "Z",
        "new": [{"sha256": r["sha256"], "href": r["href"], "text": r["text"]}
                for r in new_records],
        "changed": changed,
        "gone": gone,
    }

    print(f"  archived total: {len(archived)} | new: {len(new_records)} | "
          f"changed: {len(changed)} | gone: {len(gone)}")
    for r in new_records:
        print(f"   NEW     {r['sha256'][:12]}  {r['text']!r}  {r['href']}")
    for c in changed:
        print(f"   CHANGED {c['href']}  {c['old_sha256']} -> {c['new_sha256']}")
    for g in gone:
        print(f"   GONE    {g['sha256'][:12]}  {g['text']!r}  {g['href']}")

    if args.dry_run:
        print("[dry-run] not writing index.")
        return 0

    write_json_atomic(INDEX_JSON, idx)
    print(f"Wrote {os.path.relpath(INDEX_JSON, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
