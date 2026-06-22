#!/usr/bin/env python3
"""Headless scraper for SFSC tentative rulings — first-cut companion to the
browser extension. Runs in GitHub Actions on a schedule and commits raw JSON
in the same shape the extension produces, so the existing ingest workflow
picks it up without changes.

Design notes:
  - Reuses the extension's parser (extension/content.js) by injecting it
    into the page and calling its `scrape` global, so parsing logic
    stays in one place. The chrome.runtime listener block at the bottom
    of content.js is sliced off before injection because it references
    a chrome extension API we don't have.
  - Date stepping is driven from Python (not via fillAndScrape) because
    the SFSC datepicker's onSelect typically full-page-navigates, which
    would destroy any JS promise mid-flight. Python kicks the datepicker
    and waits for navigation, then re-injects the parser on the new page.
  - Conservative per-run limit (default: 5 dates, 3 court days back).
    Well under the SFSC site's ~50-search CAPTCHA threshold.
  - On CAPTCHA / session-expiry detection, exits early for that dept.
    The extension still fills any gaps a human can clear by solving the
    challenge interactively.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from court_holidays import ca_court_holidays as shared_ca_court_holidays


REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_JS = REPO_ROOT / 'extension' / 'content.js'

DEPT_URLS = {
    '204': 'https://webapps.sftc.org/tr/tr.dll?RulingID=7',   # Probate
    '301': 'https://webapps.sftc.org/tr/tr.dll?RulingID=10',  # Discovery
    '302': 'https://webapps.sftc.org/tr/tr.dll?RulingID=2',   # Civil Law and Motion
    '304': 'https://webapps.sftc.org/tr/tr.dll?RulingID=5',   # Asbestos Law and Motion
    '501': 'https://webapps.sftc.org/tr/tr.dll?RulingID=3',   # Real Property
}

PACIFIC = ZoneInfo('America/Los_Angeles')


def ca_court_holidays(min_year: int, max_year: int) -> set[str]:
    """Return California court holidays shared with README coverage checks."""
    return shared_ca_court_holidays(min_year, max_year)


def recent_court_days(n_back: int) -> list[date]:
    today = datetime.now(PACIFIC).date()
    earliest = today - timedelta(days=n_back * 3)
    holidays = ca_court_holidays(earliest.year, today.year)
    out: list[date] = []
    d = today
    while d >= earliest and len(out) < n_back:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def existing_dates_for_dept(dept: str) -> set[str]:
    raw_dir = REPO_ROOT / 'raw' / f'dept{dept}'
    if not raw_dir.exists():
        return set()
    out: set[str] = set()
    for p in raw_dir.rglob('*.json'):
        m = re.match(r'(\d{4}-\d{2}-\d{2})-', p.name)
        if m:
            out.add(m.group(1))
    return out


def parser_script() -> str:
    """Return the prefix of content.js that defines the parser, with the
    chrome.runtime listener block (which we don't have in Playwright) stripped.
    """
    src = CONTENT_JS.read_text(encoding='utf-8')
    cut = src.find('chrome.runtime.onMessage.addListener')
    if cut == -1:
        raise RuntimeError('content.js layout changed — could not find chrome.runtime listener')
    return src[:cut]


# SFSC datepicker selectors are hardcoded by necessity: the court page exposes
# no stable API. Keep this selector table aligned with extension/content.js.
# JS that mirrors the date-setting half of content.js's fillAndScrape, but
# without the post-fill polling loop — Python handles waiting for navigation.
SET_DATE_JS = r"""
(dateStr) => {
  const sels = [
    'input[name="DatePick"]', 'input[id="DatePick"]', 'input.hasDatepicker',
    'input[name="HearingDt"]', 'input[name="hearingDt"]',
  ];
  let input = null;
  for (const s of sels) { input = document.querySelector(s); if (input) break; }
  if (!input) return { error: 'No date input found.' };

  const jq = window.jQuery || window.$;
  if (jq && jq(input).data('datepicker')) {
    try {
      const dpInst = jq(input).data('datepicker');
      const fmt = dpInst.settings.dateFormat
        || (jq.datepicker._defaults && jq.datepicker._defaults.dateFormat)
        || 'mm/dd/yy';
      const dateObj = new Date(dateStr + 'T12:00:00');
      const formatted = jq.datepicker.formatDate(fmt, dateObj);
      if (typeof jq(input).datepicker === 'function') {
        jq(input).datepicker('setDate', dateObj);
      }
      jq(input).val(formatted);
      const liveInst = jq(input).data('datepicker') || dpInst;
      const onSelect = liveInst.settings.onSelect;
      if (typeof onSelect === 'function') {
        onSelect.call(jq(input)[0], formatted, liveInst);
        return { ok: true, path: 'datepicker.onSelect' };
      }
      jq(input).trigger('change');
    } catch (e) {
      // fall through to plain submit
    }
  }
  input.value = dateStr;
  input.dispatchEvent(new Event('input',  { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));

  const form = input.closest('form');
  const stdBtn = (form || document).querySelector(
    'input[type="submit"], input[type="image"], button[type="submit"]'
  );
  if (stdBtn) { stdBtn.click(); return { ok: true, path: 'submit-button' }; }
  if (form) {
    try {
      const actionUrl = new URL(form.action);
      for (const el of form.elements) if (el.name) actionUrl.searchParams.set(el.name, el.value);
      window.location.href = actionUrl.toString();
      return { ok: true, path: 'form-action-url' };
    } catch (_) { form.submit(); return { ok: true, path: 'form.submit' }; }
  }
  return { error: 'No submit path available.' };
}
"""


def page_ready_state(page) -> str:
    """Best-effort document.readyState for timeout diagnostics."""
    try:
        if page.is_closed():
            return "closed"
        state = page.evaluate('() => document.readyState')
        return str(state)
    except Exception as e:  # noqa: BLE001 - diagnostic path only
        return f"unavailable: {e!r}"


def reset_dept_page(page, dept: str, src: str) -> bool:
    """Return the browser to a known dept landing page after an unsafe timeout."""
    try:
        if page.is_closed():
            print(f'[{dept}] browser page closed while resetting after timeout',
                  file=sys.stderr)
            return False
        page.goto(DEPT_URLS[dept], wait_until='domcontentloaded', timeout=45_000)
        page.add_script_tag(content=src)
        if not page.evaluate('() => typeof scrape === "function"'):
            print(f'[{dept}] parser injection failed during timeout reset',
                  file=sys.stderr)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - reset path should not hide failure
        print(f'[{dept}] timeout reset failed: {e!r}', file=sys.stderr)
        return False


def scrape_dept(dept: str, dates: list[date], wait_ms: int) -> int:
    """Drive the headless browser for one dept. Returns count of files written."""
    from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

    src = parser_script()
    written = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            viewport={'width': 1280, 'height': 900},
        )
        page = context.new_page()
        page.goto(DEPT_URLS[dept], wait_until='domcontentloaded', timeout=45_000)

        page.add_script_tag(content=src)
        if not page.evaluate('() => typeof scrape === "function"'):
            print(f'[{dept}] parser injection failed — scrape() not on window', file=sys.stderr)
            browser.close()
            return 0

        initial = page.evaluate('() => scrape()')
        if initial.get('captchaChallenge'):
            print(f'[{dept}] CAPTCHA on initial load — bailing')
            browser.close()
            return 0

        for d in dates:
            iso = d.isoformat()
            kick = page.evaluate(SET_DATE_JS, iso)
            if kick.get('error'):
                print(f'[{dept}] {iso}: {kick["error"]}, skipping')
                continue

            # Either the page navigates (typical SFSC behaviour) or it does an
            # AJAX swap. A networkidle timeout can be benign on pages with
            # long-polling assets, but a DOMContentLoaded timeout leaves the
            # page in an unknown state. In that case, reset and skip the date
            # rather than scraping stale or half-loaded content.
            try:
                page.wait_for_load_state('domcontentloaded', timeout=wait_ms)
            except PWTimeout:
                state = page_ready_state(page)
                print(f'[{dept}] {iso}: timed out waiting for DOMContentLoaded '
                      f'(readyState={state}); resetting page and skipping',
                      file=sys.stderr)
                if not reset_dept_page(page, dept, src):
                    break
                continue

            try:
                page.wait_for_load_state('networkidle', timeout=wait_ms)
            except PWTimeout:
                state = page_ready_state(page)
                if state not in {'interactive', 'complete'}:
                    print(f'[{dept}] {iso}: timed out before page became usable '
                          f'(readyState={state}); resetting page and skipping',
                          file=sys.stderr)
                    if not reset_dept_page(page, dept, src):
                        break
                    continue
                print(f'[{dept}] {iso}: networkidle timeout after '
                      f'readyState={state}; continuing with parser pending check',
                      file=sys.stderr)

            page.add_script_tag(content=src)
            try:
                result = page.evaluate('() => scrape()')
            except Exception as e:
                print(f'[{dept}] {iso}: scrape() raised {e!r}', file=sys.stderr)
                continue

            if result.get('captchaChallenge') or result.get('sessionExpired'):
                print(f'[{dept}] {iso}: CAPTCHA / session expiry — stopping run')
                break
            if result.get('pending'):
                print(f'[{dept}] {iso}: page never settled, skipping')
                continue
            if result.get('error'):
                print(f'[{dept}] {iso}: error: {result["error"]}, skipping')
                continue

            scraped_at = result.get('scraped_at') or datetime.utcnow().isoformat() + 'Z'
            time_part = re.sub(r':', '', scraped_at[11:19])

            sub = ''
            if dept == '304':
                kind = result.get('calendar_kind')
                if kind == 'discovery':
                    sub = 'discovery'
                elif kind == 'law-and-motion':
                    sub = 'law-and-motion'

            outdir = REPO_ROOT / 'raw' / f'dept{dept}'
            if sub:
                outdir = outdir / sub
            outdir.mkdir(parents=True, exist_ok=True)
            outpath = outdir / f'{iso}-{time_part}.json'
            payload = dict(result)
            payload['_date'] = iso
            # Exclusive create — fails atomically if the file already
            # exists, so a parallel run (or a same-second timestamp
            # collision) can't silently clobber an existing scrape.
            # Filename already carries HHMMSS so a collision is unlikely
            # in practice; this just closes the TOCTOU window the old
            # exists() + write_text() pattern left open.
            try:
                with outpath.open('x', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2)
            except FileExistsError:
                print(f'[{dept}] {iso}: {outpath.name} already exists; '
                      'exclusive-create race/collision, skipping',
                      file=sys.stderr)
                continue
            n = result.get('reported_total')
            print(f'[{dept}] {iso}: wrote {outpath.relative_to(REPO_ROOT)} '
                  f'({n if n is not None else "?"} rulings)')
            written += 1

        browser.close()
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dept', required=True, choices=sorted(DEPT_URLS))
    ap.add_argument('--days-back', type=int, default=3,
                    help='Look back this many court days for unscanned dates')
    ap.add_argument('--max-per-run', type=int, default=5,
                    help='Hard cap on dates fetched per run (CAPTCHA budget)')
    ap.add_argument('--wait-ms', type=int, default=8000,
                    help='Per-date page-settle timeout passed to fillAndScrape')
    args = ap.parse_args()

    candidates = recent_court_days(args.days_back)
    already = existing_dates_for_dept(args.dept)
    targets = [d for d in candidates if d.isoformat() not in already]
    if not targets:
        print(f'[{args.dept}] up to date (last {args.days_back} court days all present)')
        return 0
    targets = targets[:args.max_per_run]
    print(f'[{args.dept}] scraping {len(targets)} date(s): '
          f'{[d.isoformat() for d in targets]}')

    written = scrape_dept(args.dept, targets, args.wait_ms)
    print(f'[{args.dept}] done — wrote {written} file(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
