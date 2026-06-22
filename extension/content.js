// Scrapes the SFSC tentative rulings page and returns structured data.
// Runs on: https://webapps.sftc.org/tr/tr.dll*
//
// Injected two ways: declaratively via manifest's content_scripts (so a
// freshly-loaded SFTC page auto-runs without the user opening the popup —
// powers the RECAP-style auto-upload trigger at the bottom of this file)
// AND programmatically via chrome.scripting.executeScript from popup.js /
// background.js (so a tab that loaded before the extension was installed,
// or one a sibling tab claimed mid-bulk, still gets the script). The guard
// below makes the second injection a no-op so we don't stack duplicate
// message listeners and MutationObservers.
if (!window.__sfscContentLoaded) {
  window.__sfscContentLoaded = true;

// Judge code → full name (derived from sources/sftc-judges-roster-alpha-public-022024.json)
const JUDGE_MAP = {
  RBU: 'Richard B. Ulmer Jr.',    RCE: 'Rochelle C. East',
  CK:  'Curtis E.A. Karnow',      RCD: 'Richard C. Darwin',
  JMQ: 'Joseph M. Quinn',         EHG: 'Ernest H. Goldsmith',
  EG:  'Ernest H. Goldsmith',     JPT: 'Judge Pro Tem',
  MB:  'Michael Begert',          SRB: 'Suzanne Ramos Bolanos',
  SMB: 'Susan M. Breall',         TMC: 'Teresa M. Caffese',
  BEC: 'Bruce E. Chan',           RCC: 'Roger C. Chan',
  AYC: 'Andrew Y.S. Cheng',       AMC: 'A. Marisa Chun',
  LC:  'Linda Colfax',            BC:  'Brendan Conroy',
  AC:  'Anne Costin',             CC:  'Charles Crompton',
  HMD: 'Harry M. Dorfman',        MEE: 'Maria E. Evangelista',
  SKF: 'Samuel K. Feng',          BLF: 'Brian L. Ferrall',
  ERF: 'Eric R. Fleming',         DAF: 'Daniel A. Flores',
  SJF: 'Simon J. Frankel',        CG:  'Carolyn Gold',
  ARG: 'Alexandra Robert Gordon', CFH: 'Charles F. Haines',
  CH:  'Chris Hite',              VMH: 'Victor M. Hwang',
  KK:  'Kathleen Kelly',          ACM: 'Anne-Christine Massullo',
  MM:  'Michael McNaughton',      RCM: 'Ross C. Moody',
  SMM: 'Stephen M. Murphy',       VP:  'Vedica Puri',
  MJR: 'Murlene J. Randle',       SMR: 'Sharon M. Reardon',
  MR:  'Michael Rhoads',          RR:  'Russ Roeca',
  JSR: 'Jeffrey S. Ross',         GCS: 'Gerardo C. Sandoval',
  EPS: 'Ethan P. Schulman',       PST: 'Patrick S. Thompson',
  MT:  'Michelle Tong',           CV:  'Christine Van Aken',
  RLW: 'Rebecca L. Wightman',     MFW: 'Monica F. Wiley',
  KW:  'Kenneth Wine',            MEW: 'Mary E. Wiss',
  GLW: 'Garrett L. Wong',         BCW: 'Braden C. Woods',
  ESP: 'Ethan P. Schulman',
  RU:  'Richard B. Ulmer Jr.',    RE:  'Rochelle C. East',
  CEK: 'Curtis E.A. Karnow',      JQ:  'Joseph M. Quinn',
  SB:  'Suzanne Ramos Bolanos',   AYSC:'Andrew Y.S. Cheng',
  CVA: 'Christine Van Aken',
  CM:  'Cindee Mayfield',
  REQ: 'Ronald Evans Quidachay',
  BH:  'Judge Pro Tem: Bruce Highman',     DM:  'Judge Pro Tem: David McDonald',
  PC:  'Judge Pro Tem: Peter Catalanotti', TC:  'Judge Pro Tem: Tom Cohen',
  PR:  'Judge Pro Tem: Paul Renne',        SBS: 'Judge Pro Tem: Steven B. Stein',
  AM:  'Judge Pro Tem: Aaron Minnis',      NL:  'Judge Pro Tem: Noah Lebowitz',
  NJL: 'Judge Pro Tem: Noah J. Lebowitz',  PVZ: 'Judge Pro Tem: Peter Van Zandt',
  SM:  'Judge Pro Tem: Steven Murphy',     PJT: 'Judge Pro Tem',
  NJG: 'Judge Pro Tem: Naomi Jane Gray',   DR:  'Judge Pro Tem: Douglas Robbins',
  JF:  'Judge Pro Tem: James Fleming',     GD:  'Gail Dekreon',
  HK:  'Harold E. Kahn',                   HEK: 'Harold E. Kahn',
  MJM: 'Marla J. Miller',                  AJR: 'A James Robertson II',
  PJB: 'Peter J. Busch',                   JKS: 'John K. Stewart',
  AB:  'Angela Bradstreet',
};

function extractJudge(rulingText) {
  // Trailing tag forms observed in the wild:
  //   =(302/CK)  =(D302/CK)  (302/CK)  =(JPT)  =(525/JPT)  =(JPT/525)
  //   +(302/HEK) =(302.JMQ)  =(HEK)    (D302)  =(D525)     =(525)
  //
  // We require *either* an `=`/`+` prefix on the parenthetical *or* a
  // dept-vs-code separator (/, .) inside the parens. Otherwise a
  // perfectly normal trailing reference like "(CCP 1094.5)" or "(CR
  // 8.10)" — Code-of-Civil-Procedure / California Rules citations,
  // common in petition rulings — gets read as a fictitious "CCP" /
  // "CR" judge and the row's `judge` ends up null instead of being
  // populated by other heuristics.
  const m = rulingText.match(/([=+])?\s*\(\s*([A-Za-z0-9][A-Za-z0-9\s/.,]{0,15})\s*\)\s*\.?\s*$/);
  if (!m) return null;
  const hasPrefix = !!m[1];
  const inside = m[2];
  const hasSeparator = /[\/\.]/.test(inside);
  if (!hasPrefix && !hasSeparator) return null;
  // Pick the first letter-only run that isn't a bare D dept-marker.
  // Dept-only tags like (D302) yield no code → null (data genuinely lacks a judge code).
  const codes = inside.match(/[A-Za-z]+/g) || [];
  const code = codes.find(c => c.toUpperCase() !== 'D')?.toUpperCase();
  if (!code) return null;
  if (code === 'JPT') {
    const pt = rulingText.match(/Pro Tem Judge\s+([A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+)*?)(?:,|;|\s+a\s+member|\s+member|\s+has been|\s+recuses)/);
    if (pt) return `Judge Pro Tem: ${pt[1].trim()}`;
    return 'Judge Pro Tem';
  }
  return JUDGE_MAP[code] || null;
}

// Detect a Cloudflare interstitial / CAPTCHA challenge page. The SFTC site
// sits behind Cloudflare and will occasionally challenge a request mid-scan
// (especially after a session reset or when the bot heuristics trip). The
// challenge HTML is generic — no resultsRulings, no resultsCount — so the
// bulk scraper used to mis-classify it as a hard "No results block" error
// and silently burn through the rest of the date list while every request
// hit the same wall. Treat it like session expiry: pause, prompt the user,
// resume after verification.
function detectCaptchaChallenge() {
  // Title-based: "Just a moment...", "Attention Required! | Cloudflare",
  // "Please Wait... | Cloudflare", "Verifying you are human", etc.
  const title = (document.title || '').toLowerCase();
  if (/just a moment|attention required|please wait|verifying you are human|checking your browser|one more step/i.test(title)) {
    return true;
  }
  // Element / class-based markers Cloudflare emits on its challenge pages.
  if (document.querySelector(
        '#cf-wrapper, .cf-browser-verification, #challenge-form, ' +
        '#challenge-running, #challenge-stage, #cf-challenge-running, ' +
        '.cf-error-details, iframe[src*="challenges.cloudflare.com"], ' +
        'iframe[src*="cloudflare.com/cdn-cgi/challenge-platform"], ' +
        'script[src*="cdn-cgi/challenge-platform"], ' +
        'script[src*="challenges.cloudflare.com/turnstile"]'
      )) {
    return true;
  }
  // hCaptcha / reCAPTCHA shells that occasionally front Cloudflare's challenge.
  if (document.querySelector(
        '.h-captcha, iframe[src*="hcaptcha.com"], ' +
        '.g-recaptcha, iframe[src*="recaptcha"]'
      )) {
    return true;
  }
  // URL-level: cdn-cgi challenge endpoints serve their own pages too.
  const href = location.href || '';
  if (/\/cdn-cgi\/(?:l\/)?challenge-platform|__cf_chl_/i.test(href)) {
    return true;
  }
  return false;
}

function detectSftcGenericError() {
  const text = document.body?.innerText || '';
  return /an error was encountered/i.test(text)
    && /refresh the page and try again/i.test(text);
}

// Dept 304 hosts two sub-calendars — Asbestos Law and Motion and
// Asbestos Discovery — heard in the same courtroom by the same judge
// but on different days. Both are "department 304" but the extension
// tracks scanned dates per-sub-calendar (otherwise scraping one would
// mark a date as done for the other), and ingest stamps each row
// with a calendar_kind column so downstream tools can break the
// archive down by sub-calendar.
//
// Returns 'discovery' | 'law-and-motion' | null. The user-confirmed
// page-header phrasings are
//   "Asbestos Discovery, Department 304"
//   "Asbestos Law & Motion, Department 304"
// We walk every heading; the first one containing "Asbestos" wins,
// and whether it also contains "Discovery" or "Law & Motion" decides
// the kind.
function detectAsbestosKind() {
  const headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6');
  for (const el of headings) {
    const t = el.textContent || '';
    if (!/\bAsbestos\b/i.test(t)) continue;
    if (/\bDiscovery\b/i.test(t))                              return 'discovery';
    if (/\bLaw\s*(?:&|and|&amp;)\s*Motion\b/i.test(t))         return 'law-and-motion';
    return null; // ambiguous Asbestos heading; fall back to dept-level only.
  }
  return null;
}

// Determine the SFTC department this page belongs to. Returns the dept
// number as a string (e.g. '302') or null when no signal is available.
//
// Heuristic ladder (most → least specific):
//   1. Any heading with "Department <N>" (the canonical case).
//   2. Heading text matching a known calendar name (Probate → 204,
//      Discovery → 301, Asbestos Law and Motion → 304, Real Property → 501,
//      Civil Law and Motion → 302). SFTC's probate page reads "Probate"
//      with no number; several other dept pages do the same — without
//      these branches every numberless dept scraped into raw/dept302/
//      alongside the civil-law-and-motion calendars.
//   3. The page URL's query string. SFTC's GET form preserves the calendar
//      ID across navigations, so even when the heading is missing
//      (CAPTCHA-cleared page, partial reload) we can still recover the
//      dept from the URL.
//   4. Any hidden form input whose name suggests a calendar/dept identifier.
// Returns null when none of these fire — the caller (popup.js's
// detectDepartment) then falls back to a tab-specific cached value
// rather than misfiling scrapes under '302'.
function detectPageDepartment() {
  const headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6');
  for (const el of headings) {
    const m = (el.textContent || '').match(/Department\s+(\d+)/i);
    if (m) return m[1];
  }
  for (const el of headings) {
    const t = el.textContent || '';
    if (/\bProbate\b/i.test(t)) return '204';
    // Asbestos before Discovery: only the Asbestos Law and Motion
    // calendar is in active use in Dept 304 today, but the SFTC site
    // has historically also exposed an "Asbestos Discovery" calendar
    // — if it's ever brought back, the heading would still contain
    // "Asbestos" first, so this routing keeps the scrape aimed at 304
    // rather than mis-classifying it as the generic Discovery dept (301).
    if (/\bAsbestos\b/i.test(t)) return '304';
    if (/\bDiscovery\b/i.test(t)) return '301';
    if (/\bReal\s+Property\b/i.test(t)) return '501';
    if (/\bCivil\s+Law\s*(?:&|and)?\s*Motion\b/i.test(t)) return '302';
  }
  try {
    const url = new URL(location.href);
    const candidates = ['CalendarType', 'CalType', 'Calendar', 'Cal',
                        'CalNum', 'CalNo', 'Dept', 'DeptCode', 'Department'];
    for (const key of candidates) {
      const v = url.searchParams.get(key);
      if (v && /^\d+$/.test(v)) return v;
    }
  } catch (_) { /* ignore */ }
  for (const input of document.querySelectorAll('input[type="hidden"], input[type="text"]')) {
    const name = (input.name || '').toLowerCase();
    if (/cal(?:type|num|no|endar)?$|^dept|^department|deptcode/i.test(name)
        && /^\d+$/.test((input.value || '').trim())) {
      return input.value.trim();
    }
  }
  return null;
}

function scrape(expectedDate = null) {
  // CAPTCHA / Cloudflare challenge detection runs first — see
  // detectCaptchaChallenge for why. We surface it as a distinct field so
  // the popup can show a CAPTCHA-specific prompt while the background
  // handler treats it the same as a session expiry (pause + reload).
  if (detectCaptchaChallenge()) {
    return { captchaChallenge: true };
  }
  if (detectSftcGenericError()) {
    return {
      sftcServerError: true,
      error: 'SFTC generic error page: An error was encountered. Please refresh the page and try again.',
    };
  }

  // Session-expiry detection inspects the resultsCount element directly.
  // Earlier versions gated this on an empty resultsRulings table, but
  // SFTC's session-expired response leaves the previous search's <tr>s in
  // place (only the count label is replaced with "Your session has
  // expired."). With the rulingsEmpty gate the check missed the real
  // case, scrape() returned the stale rulings, the stale-court-date guard
  // in commitToGitHub then converted them to an empty marker, and the
  // bulk run silently advanced to the next date.
  const sessionExpiredRe = /session\s+has\s+expired|your\s+session\s+(has\s+)?expired|session\s+timed?\s+out/i;
  const countEl   = document.getElementById('resultsCount');
  const countText = countEl ? countEl.textContent : '';
  if (sessionExpiredRe.test(countText)) {
    return { sessionExpired: true };
  }
  // Belt-and-braces: SFTC's session-expired markup includes a Restart
  // anchor whose href is the literal string javascript:location.reload(true).
  // If we see that, treat the page as expired regardless of where the text
  // landed.
  if (document.querySelector('a[href*="location.reload(true)"]')) {
    return { sessionExpired: true };
  }

  const container = document.getElementById('resultsRulings');
  if (!container) {
    // Last resort: the rulings container is missing AND the body text
    // mentions session expiry — the rulingsEmpty guard remains here
    // because document.body innerText is broad enough that an actual
    // ruling could quote "session expired" without it actually being a
    // session-expired page.
    if (sessionExpiredRe.test(document.body?.innerText || '')) {
      return { sessionExpired: true };
    }
    return { error: 'No results block found. Run a search on this page first.' };
  }

  const totalText = countText;
  const totalMatch = totalText.match(/Total Records Found\s+(\d+)/i);
  const reportedTotal = totalMatch ? parseInt(totalMatch[1]) : null;

  const department = detectPageDepartment();
  // Sub-calendar tag for Dept 304 only — the wrapper carries this
  // through to the JSON the extension commits, ingest.py records it
  // as a parquet column, and the "scan unscanned" coverage check
  // only counts a date as scanned if the sub-folder it lives in
  // matches the active sub-calendar.
  const calendarKind = department === '304' ? detectAsbestosKind() : null;

  const rulings = [];
  let current = {};

  for (const tr of container.querySelectorAll('tr')) {
    const headerTd = tr.querySelector('td.dataHeader');
    if (!headerTd) {
      if (current['Case Number']) {
        rulings.push({ ...current });
        current = {};
      }
      continue;
    }

    const field = headerTd.textContent.replace(':', '').trim();
    const tds   = tr.querySelectorAll('td');
    const valueTd = tds[2] || tds[tds.length - 1];
    const value   = valueTd ? valueTd.innerText.trim() : '';

    if (['Case Number', 'Case Title', 'Court Date', 'Calendar Matter', 'Rulings', 'Examiner'].includes(field)) {
      current[field] = value;
    }
  }
  if (current['Case Number']) rulings.push({ ...current });

  // Auto-populate Judge from the Examiner field (Probate) or from the
  // code tag at the end of each ruling (civil/other departments).
  for (const r of rulings) {
    if (r.Examiner) {
      // Title-case the all-caps examiner name the SFTC page emits.
      r.Judge = r.Examiner.trim().toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
    } else if (r.Rulings) {
      const judge = extractJudge(r.Rulings);
      if (judge) r.Judge = judge;
    }
  }

  const pageDate = dateInputISO();
  const expected = /^\d{4}-\d{2}-\d{2}$/.test(String(expectedDate || ''))
    ? String(expectedDate)
    : null;
  const rowDates = [...new Set(rulings.map(r => courtDateISO(r['Court Date'] || '')).filter(Boolean))];
  if (expected) {
    const rowsProveExpected = rowDates.length > 0 && rowDates.every(d => d === expected);
    if (pageDate && pageDate !== expected) {
      return {
        error: `SFTC page date mismatch: requested ${expected}, page shows ${pageDate}.`,
        dateMismatch: true,
        requested_date: expected,
        page_date: pageDate,
        source_url: window.location.href,
      };
    }
    if (!pageDate && !rowsProveExpected) {
      return {
        error: `Could not verify SFTC page reached ${expected}.`,
        dateMismatch: true,
        requested_date: expected,
        page_date: null,
        source_url: window.location.href,
      };
    }
    if (reportedTotal !== 0 && rowDates.some(d => d !== expected)) {
      return {
        error: `SFTC page still shows rulings for ${rowDates.join(', ')} while scanning ${expected}.`,
        dateMismatch: true,
        requested_date: expected,
        page_date: pageDate,
        source_url: window.location.href,
      };
    }
  }

  // Stale-page guard: when SFTC's count label explicitly says 0 records but the
  // rulings table still holds entries from a previous search, trust the label
  // and drop the stale rows. Otherwise the bulk scraper would commit those rows
  // under the requested date (see e.g. raw/dept302/2020-06-10-054353.json,
  // which had reported_total=0 but 25 rulings whose Court Date was 2016-09-16).
  if (reportedTotal === 0 && rulings.length > 0) {
    return {
      department,
      calendar_kind:  calendarKind,
      scraped_at:     new Date().toISOString(),
      source_url:     window.location.href,
      page_date:      pageDate,
      reported_total: 0,
      rulings:        [],
    };
  }

  return {
    department,
    calendar_kind:  calendarKind,
    scraped_at:     new Date().toISOString(),
    source_url:     window.location.href,
    page_date:      pageDate,
    reported_total: reportedTotal,
    rulings,
  };
}

// ── Auto-navigation helpers ───────────────────────────────────────────────────

function findDateInput() {
  // Only specific known SFTC selectors. Heuristic fallbacks (label-text,
  // input[name*=Date]) were dropped because they risk silently picking the
  // wrong field; if SFTC ever changes their HTML, fail loudly via Diagnose
  // rather than scraping the wrong input.
  for (const sel of [
    'input[name="DatePick"]', 'input[id="DatePick"]',
    'input.hasDatepicker',
    'input[name="HearingDt"]', 'input[name="hearingDt"]',
  ]) {
    const el = document.querySelector(sel);
    if (el) return el;
  }
  return null;
}

function fallbackDateInputValue(dateStr) {
  const m = String(dateStr || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[2]}/${m[3]}/${m[1]}` : String(dateStr || '');
}

function dateInputISO(input = findDateInput()) {
  const raw = (input?.value || '').trim();
  if (!raw) return null;
  let m = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})$/);
  if (m) {
    let year = m[3];
    if (year.length === 2) year = `${parseInt(year, 10) >= 70 ? '19' : '20'}${year}`;
    return `${year}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}`;
  }
  m = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? raw : null;
}

function courtDateISO(raw) {
  if (!raw) return null;
  let m = String(raw).match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (m) return `${m[3]}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}`;
  m = String(raw).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[0] : null;
}

async function fillAndScrape(dateStr, waitMs = 2000) {
  const input = findDateInput();
  if (!input) return { error: 'No date input found on this page.' };

  const jq = window.jQuery || window.$;
  const prevHTML = document.getElementById('resultsRulings')?.innerHTML ?? null;
  // Also capture the count text — when two consecutive dates both have 0
  // rulings, the rulings table HTML is identical but the count line still
  // re-renders. Polling either signal lets the empty→empty transition
  // resolve as a valid 0-record scrape rather than timing out as "pending"
  // (which the bulk handler then mis-attributes to errors).
  const prevCount = document.getElementById('resultsCount')?.textContent ?? null;

  if (jq && jq(input).data('datepicker')) {
    try {
      const dpInst = jq(input).data('datepicker');
      // Format the date using the datepicker's own configured format (e.g. mm/dd/yy).
      // SFTC's onSelect can read the datepicker instance, not just the visible
      // input value, so update both before invoking the handler.
      const fmt = dpInst.settings.dateFormat
        || (jq.datepicker._defaults && jq.datepicker._defaults.dateFormat)
        || 'mm/dd/yy';
      const dateObj = new Date(dateStr + 'T12:00:00');
      const formatted = jq.datepicker.formatDate(fmt, dateObj);
      if (typeof jq(input).datepicker === 'function') {
        jq(input).datepicker('setDate', dateObj);
      }
      jq(input).val(formatted);
      // Invoke onSelect directly — this is what the calendar fires on user pick, and it
      // knows how to build the navigation URL (including SessionID and other params).
      const liveInst = jq(input).data('datepicker') || dpInst;
      const onSelect = liveInst.settings.onSelect;
      if (typeof onSelect === 'function') {
        onSelect.call(jq(input)[0], formatted, liveInst);
      } else {
        jq(input).trigger('change');
      }
    } catch {
      // Datepicker API unavailable — fall back to raw val + change
      jq(input).val(fallbackDateInputValue(dateStr));
      jq(input).trigger('change');
    }
  } else {
    // Fallback: SFTC's raw input expects slash dates when the datepicker wrapper
    // is absent after a CAPTCHA/session handoff.
    input.value = fallbackDateInputValue(dateStr);
    input.dispatchEvent(new Event('input',  { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    if (jq) jq(input).trigger('change');
  }

  // Give an AJAX auto-search a moment to fire before looking for a submit button
  await new Promise(r => setTimeout(r, 400));
  if (pageHasResponded(prevHTML, prevCount)) return scrape(dateStr);

  // Fall back to explicit form submission (full-page-reload sites)
  const form = input.closest('form');

  function findSearchButton(container) {
    // Standard submit-type buttons first
    const std = container?.querySelector('input[type="submit"], input[type="image"], button[type="submit"]');
    if (std) return std;
    // Any button/input whose visible text matches "search"
    for (const el of (container ?? document).querySelectorAll('button, input[type="button"]')) {
      if (/^\s*search\s*$/i.test(el.value || el.textContent)) return el;
    }
    return null;
  }

  const btn = findSearchButton(form) ?? findSearchButton(document);
  if (btn) {
    btn.click();
  } else if (form) {
    // form.submit() on a GET form strips query params from the action URL, losing
    // session tokens like SessionID. Navigate to the action URL instead, copying
    // all existing params and appending the current form field values.
    try {
      const actionUrl = new URL(form.action);
      for (const el of form.elements) {
        if (el.name) actionUrl.searchParams.set(el.name, el.value);
      }
      window.location.href = actionUrl.toString();
    } catch {
      form.submit();
    }
  } else {
    return { error: 'No submit button or auto-search found.' };
  }

  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 500));
    if (pageHasResponded(prevHTML, prevCount)) return scrape(dateStr);
  }
  // Final check: session, CAPTCHA, and SFTC temporary-error pages are
  // definitive even if the DOM-change poll never tripped. A normal numeric
  // count is not definitive here, because it can belong to the prior search.
  // If nothing visibly changed and the tab did not navigate, leave this as
  // pending so bulk records a retryable error instead of a false save.
  const finalScrape = scrape(dateStr);
  if (finalScrape?.sessionExpired || finalScrape?.captchaChallenge) return finalScrape;
  if (finalScrape?.sftcServerError || finalScrape?.error) return finalScrape;
  return { pending: true };
}

// True once the SFTC page has clearly responded to our submit. Either signal
// is sufficient: the rulings table can change without the count text (rulings
// found) or the count text can change without the rulings table (zero rulings
// after a non-zero search, or vice versa).
function pageHasResponded(prevHTML, prevCount) {
  const container = document.getElementById('resultsRulings');
  if (container && container.innerHTML !== prevHTML) return true;
  const countEl = document.getElementById('resultsCount');
  if (countEl && countEl.textContent !== prevCount) return true;
  return false;
}

// ── Message listener ──────────────────────────────────────────────────────────

function diagnose() {
  const input = findDateInput();
  const form  = input?.closest('form');
  const btn   = form?.querySelector('input[type="submit"], input[type="image"], button[type="submit"]')
             ?? document.querySelector('input[type="submit"], input[type="image"], button[type="submit"]');

  const allForms = [...document.querySelectorAll('form')].map(f => ({
    action: f.action,
    method: f.method,
    inputs: [...f.querySelectorAll('input')].map(i => ({
      name: i.name, id: i.id, type: i.type, value: i.value,
    })),
  }));

  return {
    foundInput: input ? { name: input.name, id: input.id, type: input.type } : null,
    formAction: form?.action ?? null,
    btnText:    btn ? (btn.value || btn.textContent).trim() : null,
    allForms,
  };
}

// ── Toast (in-page feedback for hotkey actions) ───────────────────────────────

function showToast(message, type = 'info') {
  const colors = {
    info:    { bg: '#1a3a5c', fg: 'white' },
    success: { bg: '#2a7a4a', fg: 'white' },
    warn:    { bg: '#b8860b', fg: 'white' },
    error:   { bg: '#a02020', fg: 'white' },
  };
  const c = colors[type] || colors.info;
  let toast = document.getElementById('sfsc-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'sfsc-toast';
    toast.style.cssText = `
      position: fixed; top: 16px; right: 16px; z-index: 2147483647;
      padding: 10px 14px; border-radius: 6px;
      font: 13px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      box-shadow: 0 4px 12px rgba(0,0,0,0.2);
      max-width: 360px; pointer-events: none;
      transition: opacity 0.2s;
    `;
    document.body.appendChild(toast);
  }
  toast.style.background = c.bg;
  toast.style.color = c.fg;
  toast.textContent = message;
  toast.style.opacity = '1';
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { toast.style.opacity = '0'; }, 4000);
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (msg.action === 'scrape') {
    respond(scrape(msg.expectedDate));
    return true;
  }
  if (msg.action === 'fill-and-scrape') {
    fillAndScrape(msg.date, msg.waitMs).then(respond);
    return true;
  }
  if (msg.action === 'restart-session') {
    // The session-expired page has a Restart link whose href is just
    // `javascript:location.reload(true);`. We invoke the same call —
    // the SFTC tab reloads, hits the Cloudflare challenge, and the
    // user completes verification before clicking Resume in the popup.
    respond({ ok: true });
    setTimeout(() => location.reload(true), 50);
    return true;
  }
  if (msg.action === 'get-date') {
    const date = dateInputISO();
    respond(date ? { date } : {});
    return true;
  }
  if (msg.action === 'show-toast') {
    showToast(msg.message, msg.type);
    respond({ ok: true });
    return true;
  }
  if (msg.action === 'diagnose') {
    respond(diagnose());
    return true;
  }
});

// ── Auto-upload trigger (RECAP-style) ─────────────────────────────────────────
// On every SFTC tentative-rulings page the user opens — and every AJAX
// re-search within it — we run a debounced scrape and, if the result
// passes the integrity checks in background.js's auto-commit handler,
// upload it to GitHub. The user does not have to open the popup or press
// any button. Manual Send-to-GitHub still works as a fallback.
//
// All defences (future-date refusal, source-URL whitelist, reported_total
// vs scraped count, case-number format, loose case-number/title cross-
// reference against historical archive) live in background.js; this content
// script is just a trigger.
//
// We debounce on body-subtree mutations: SFTC's framework first replaces
// the rulings table, then re-renders the count label, then occasionally
// settles a re-paint a beat later. Firing on the first mutation would
// catch a half-loaded page; firing per-mutation would spam. 1.5 s of
// quiet covers the worst observed settle time.

const AUTO_DEBOUNCE_MS = 1500;
let _autoTimer       = null;
// Lookup key for the "have we already attempted this exact result in this
// page lifetime?" check. We key on (date, count) so a re-scan of the same
// date that picked up newly-posted rulings (the "tentatives drop one-by-
// one starting at 2 pm" pattern) DOES re-attempt, and the duplicate-
// detection logic in background.js decides whether to commit or skip.
let _autoLastAttempt = null;
let _autoInflight    = false;
// Suppress mutation-driven auto-uploads while a bulk run is using this
// tab. bulk-status is the source of truth (per-tab job state in the
// service worker's storage). We refresh the flag whenever storage changes
// land for _bulkJobs — cheap, event-driven, no polling.
let _autoBulkActive  = false;

function _autoParseDateInput() {
  const input = findDateInput();
  const raw   = input?.value?.trim();
  if (!raw) return null;
  const m = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (m) return `${m[3]}-${m[1].padStart(2,'0')}-${m[2].padStart(2,'0')}`;
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw.slice(0, 10);
  return null;
}

function _autoCourtDateFromRulings(rulings) {
  if (!rulings?.length) return null;
  const raw = rulings[0]['Court Date'] || '';
  const m = raw.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (m) return `${m[3]}-${m[1].padStart(2,'0')}-${m[2].padStart(2,'0')}`;
  if (/^\d{4}-\d{2}-\d{2}/.test(raw)) return raw.slice(0, 10);
  return null;
}

function maybeAutoUpload() {
  if (_autoInflight || _autoBulkActive) return;
  let data;
  try { data = scrape(); } catch { return; }
  if (!data) return;
  // Skip the trigger for all non-data outcomes. The popup / bulk paths
  // surface these to the user; auto-upload stays silent on them so we
  // don't toast on every CAPTCHA or session-expired re-render.
  if (data.error || data.pending || data.captchaChallenge || data.sessionExpired) return;
  if (!Array.isArray(data.rulings)) return;

  // Searched date: prefer the rulings' own Court Date (the SFTC server
  // injected it, so it's the most trustworthy signal), fall back to the
  // date input on the page (covers the legitimate "0 rulings today"
  // case where there's no Court Date to read from).
  const date = _autoCourtDateFromRulings(data.rulings) || _autoParseDateInput();
  if (!date) return;

  // Skip a re-attempt only if the page state hasn't changed since the
  // last attempt. (date, rulingsCount) is a stable enough fingerprint:
  // SFTC's count of rulings for the next court day grows monotonically as tentatives
  // are posted from 2 pm onward, so the count changing → new content →
  // try again.
  const fingerprint = `${date}:${data.rulings.length}:${data.reported_total ?? 'null'}`;
  if (_autoLastAttempt === fingerprint) return;
  _autoLastAttempt = fingerprint;

  _autoInflight = true;
  chrome.runtime.sendMessage(
    { action: 'auto-commit', payload: { data: { ...data, _date: date } } },
    response => {
      _autoInflight = false;
      if (chrome.runtime.lastError) return; // SW unavailable; silent
      if (!response) return;
      if (response.uploaded) {
        const n = response.rulingsCount ?? data.rulings.length;
        showToast(
          `SFSC: auto-uploaded ${n} ruling${n === 1 ? '' : 's'} for ${date}`,
          'success'
        );
      } else if (response.refused) {
        // Tell the user WHY — refused uploads are exactly the integrity
        // checks they asked us to enforce, so silence here would hide
        // the feature. Use 'warn' rather than 'error' so legitimate
        // refusals (out-of-range date, stale cached page) don't read as
        // bugs.
        showToast(`SFSC auto-upload skipped — ${response.refused}`, 'warn');
      } else if (response.disabled || response.duplicate || response.noChange || response.bulkActive || response.noToken) {
        // Quietly skip — these are normal not-applicable states, not failures.
      } else if (response.error) {
        showToast(`SFSC auto-upload error: ${response.error}`, 'error');
      }
    }
  );
}

function _autoScheduleScrape() {
  if (_autoTimer) clearTimeout(_autoTimer);
  _autoTimer = setTimeout(() => {
    _autoTimer = null;
    maybeAutoUpload();
  }, AUTO_DEBOUNCE_MS);
}

function _autoInstallObserver() {
  // Body-level subtree observation catches both AJAX-driven rulings-table
  // swaps and full re-renders. The debounce above absorbs the per-
  // mutation noise so we only fire once the DOM settles.
  if (!document.body) return;
  const obs = new MutationObserver(() => _autoScheduleScrape());
  obs.observe(document.body, { childList: true, subtree: true });
}

function _autoRefreshBulkFlag() {
  // The auto-trigger and the bulk-scrape engine both end up calling
  // commitToGitHub; allowing both to fire on the same scrape would
  // double-commit (one would lose the duplicate-guard race). The bulk
  // engine writes a per-tab job into _bulkJobs whenever it's active —
  // we listen for that and suppress auto-uploads while it's running.
  chrome.runtime.sendMessage({ action: 'auto-bulk-active' }, r => {
    if (chrome.runtime.lastError) { _autoBulkActive = false; return; }
    _autoBulkActive = !!r?.active;
  });
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes._bulkJobs) _autoRefreshBulkFlag();
});

_autoRefreshBulkFlag();
_autoInstallObserver();
// Initial scrape attempt — covers the case where the page is already
// fully rendered by the time content.js attaches (no mutation will fire).
if (document.readyState === 'complete') {
  _autoScheduleScrape();
} else {
  window.addEventListener('load', _autoScheduleScrape, { once: true });
}

} // end of __sfscContentLoaded guard
