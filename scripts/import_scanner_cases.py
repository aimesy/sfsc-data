#!/usr/bin/env python3
"""Promote local headless-scanner case JSON into the committed archive.

The scanner intentionally writes to .scanner/cases so raw harvesting can run
without GitHub credentials. This script is the explicit bridge from that local
cache into archive/cases plus the append-only cases index.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from tempfile import NamedTemporaryFile

import document_storage as storage


ROOT = Path(__file__).resolve().parents[1]
CRIMINAL_PORTAL_SCHEMA = "sfsc-criminal-portal-case-v1"
CRIMINAL_PORTAL_SOURCE = "sftc-criminal-portal"
CRIMINAL_PORTAL_URL = "https://webapps.sftc.org/crimportal/crimportal.dll"
CRIMINAL_SESSION_RE = re.compile(r"([?&]SessionID=)[^&#]+", re.I)
STATUTE_RE = re.compile(
    r"\b(?:PC|PEN(?:AL)?\s+CODE|HS|VC|BP|CC|GC)\s*(?:§|SECTION|SEC\.)?\s*"
    r"\d+[A-Za-z]?(?:\.\d+)?(?:\([^)]+\))*",
    re.I,
)
PROCEDURAL_STATUTE_RE = re.compile(
    r"\bPC\s*(?:1001\.3[56]|1001\.95|1538\.5|1050|1203\.2|1369|1370|1382|1385|1417|3000\.08|3455|4011(?:\.6)?)\b",
    re.I,
)
CHARGE_STATUTE_RE = re.compile(
    r"\b(?:(?P<code1>PC|PEN(?:AL)?\s+CODE|HS|H\s*&\s*S|HEALTH\s+AND\s+SAFETY\s+CODE|VC|VEH(?:ICLE)?\s+CODE|BP|B\s*&\s*P|BUS(?:INESS)?\s+AND\s+PROF(?:ESSIONS)?\s+CODE|CC|CIV(?:IL)?\s+CODE|GC|GOV(?:ERNMENT)?\s+CODE)\s*(?:§|SECTION|SEC\.)?\s*(?P<section1>\d+[A-Za-z]?(?:\.\d+)?(?:\([A-Za-z0-9]+\))*)|(?P<section2>\d+[A-Za-z]?(?:\.\d+)?(?:\([A-Za-z0-9]+\))*)\s*(?P<code2>PC|HS|H\s*&\s*S|VC|BP|B\s*&\s*P|CC|GC))(?=$|[^A-Za-z0-9])",
    re.I,
)
CHARGE_SENTINEL_RE = re.compile(r"\b(?:8{5,}|9{5,}|0{5,})\b")

CODE_NAMES = {
    "PC": ("Penal Code", "PEN"),
    "HS": ("Health and Safety Code", "HSC"),
    "VC": ("Vehicle Code", "VEH"),
    "BP": ("Business and Professions Code", "BPC"),
    "CC": ("Civil Code", "CIV"),
    "GC": ("Government Code", "GOV"),
}
CHARGE_TITLE_OVERRIDES = {
    ("PC", "211"): "Robbery",
    ("PC", "236"): "False imprisonment",
    ("PC", "242"): "Battery",
    ("PC", "137(b)"): "Influencing testimony or information",
    ("PC", "245(a)(1)"): "Assault with a deadly weapon",
    ("PC", "459"): "Burglary",
    ("PC", "466"): "Possession of burglary tools",
    ("HS", "11368"): "Forging or altering prescriptions",
}


def norm_case(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("<br>", " ")).strip()


def normalize_charge_code(value: object) -> str:
    text = clean(value).upper().replace(".", "")
    text = re.sub(r"\s+", " ", text)
    if text in {"PC", "PEN CODE", "PENAL CODE"}:
        return "PC"
    if text in {"HS", "H&S", "H & S", "HEALTH AND SAFETY CODE"}:
        return "HS"
    if text in {"VC", "VEH CODE", "VEHICLE CODE"}:
        return "VC"
    if text in {"BP", "B&P", "B & P", "BUS AND PROF CODE", "BUSINESS AND PROF CODE", "BUSINESS AND PROFESSIONS CODE"}:
        return "BP"
    if text in {"CC", "CIV CODE", "CIVIL CODE"}:
        return "CC"
    if text in {"GC", "GOV CODE", "GOVERNMENT CODE"}:
        return "GC"
    return text


def leginfo_url(code: str, section: str) -> str:
    code_name = CODE_NAMES.get(code)
    if not code_name:
        return ""
    base_section = re.sub(r"\(.*$", "", clean(section)).strip()
    if not base_section:
        return ""
    return (
        "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?"
        + urllib.parse.urlencode({"sectionNum": f"{base_section}.", "lawCode": code_name[1]})
    )


def charge_title_for(code: str, section: str) -> str:
    exact = clean(section)
    base = re.sub(r"\(.*$", "", exact)
    return CHARGE_TITLE_OVERRIDES.get((code, exact)) or CHARGE_TITLE_OVERRIDES.get((code, base)) or ""


def charge_parts(value: object) -> list[str]:
    text = clean(value)
    if not text:
        return []
    # The criminal index generally uses semicolons/newlines for separate charges.
    # Avoid splitting on slash because the portal also uses it for felony/misdemeanor
    # suffixes and subdivisions.
    parts = re.split(r"\s*(?:;|\n|\r|\|)\s*", text)
    return [clean(part).strip(" ,") for part in parts if clean(part).strip(" ,")]


def add_charge_row(rows: list[dict], seen: set[str], row: dict) -> None:
    key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).casefold()
    if key in seen:
        return
    seen.add(key)
    rows.append(row)


def charge_row_from_match(raw: str, match: re.Match, title: str = "") -> dict:
    code = normalize_charge_code(match.group("code1") or match.group("code2"))
    section = clean(match.group("section1") or match.group("section2"))
    suffix = re.search(r"(?:^|[/\s-])([FMI])(?:$|[/\s-])", raw, re.I)
    classification = ""
    if suffix:
        classification = {"F": "felony", "M": "misdemeanor", "I": "infraction"}.get(suffix.group(1).upper(), "")
    row = {
        "raw": clean(raw).strip(" ,"),
        "title": clean(title) or charge_title_for(code, section) or clean(raw).strip(" ,"),
    }
    if code and section and code in CODE_NAMES:
        row["code"] = f"{code} {section}"
        row["code_system"] = code
        row["section"] = section
        row["citation"] = f"{CODE_NAMES[code][0]} § {section}"
        row["url"] = leginfo_url(code, section)
    if classification:
        row["classification"] = classification
    return row


def parse_charge_rows(value: object) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in charge_parts(value):
        matches = list(CHARGE_STATUTE_RE.finditer(raw))
        if len(matches) > 1:
            consumed: list[tuple[int, int]] = []
            for match in matches:
                end = match.end()
                class_match = re.match(r"\s*/\s*([FMI])\b", raw[end:], re.I)
                if class_match:
                    end += class_match.end()
                consumed.append((match.start(), end))
                add_charge_row(rows, seen, charge_row_from_match(raw[match.start():end], match))
            residual = raw
            for start, end in reversed(consumed):
                residual = residual[:start] + " " + residual[end:]
            for sentinel in CHARGE_SENTINEL_RE.findall(residual):
                add_charge_row(rows, seen, {
                    "raw": sentinel,
                    "title": f"Unrecognized criminal index charge code {sentinel}",
                    "unparsed": True,
                })
            continue
        match = matches[0] if matches else None
        if match:
            title = clean((raw[: match.start()] + " " + raw[match.end() :]).strip(" -:;,"))
            title = re.sub(r"^[/\s-]*[FMI]\b[/\s-]*", "", title, flags=re.I)
            title = re.sub(r"\b(?:felony|misdemeanor|infraction|F|M|I)\b\s*$", "", title, flags=re.I).strip(" -:;,/")
            add_charge_row(rows, seen, charge_row_from_match(raw, match, title))
            continue
        for sentinel in CHARGE_SENTINEL_RE.findall(raw):
            add_charge_row(rows, seen, {
                "raw": sentinel,
                "title": f"Unrecognized criminal index charge code {sentinel}",
                "unparsed": True,
            })
        if not CHARGE_SENTINEL_RE.search(raw):
            add_charge_row(rows, seen, {"raw": raw, "title": raw, "unparsed": True})
    return rows


def is_criminal_portal_case(data: dict) -> bool:
    return (
        clean(data.get("schema")).lower() == CRIMINAL_PORTAL_SCHEMA
        or clean(data.get("source")).lower() == CRIMINAL_PORTAL_SOURCE
        or clean(data.get("case_type")).lower() == "criminal"
        or bool(clean(data.get("criminal_case_number")))
    )


def criminal_raw_number(data: dict, fallback: str = "") -> str:
    direct = clean(data.get("criminal_case_number") or data.get("criminalCaseNumber"))
    if direct:
        return re.sub(r"[^0-9]", "", direct)
    case_number = clean(data.get("case_number") or fallback)
    m = re.match(r"^CRI[-_\s]*(\d{6,})$", case_number, re.I)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{6,}", clean(fallback)):
        return clean(fallback)
    return ""


def criminal_archive_case_number(data: dict, fallback: str = "") -> str:
    existing = norm_case(data.get("case_number"))
    if re.fullmatch(r"CRI\d{6,}", existing):
        return existing
    raw = criminal_raw_number(data, fallback)
    return f"CRI{raw}" if raw else existing


def redact_criminal_portal_url(value: object) -> str:
    raw = clean(value)
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(k, v) for k, v in query if k.lower() != "sessionid"]
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    except Exception:
        return CRIMINAL_SESSION_RE.sub(r"\1[redacted]", raw)


def criminal_source_url(data: dict) -> str:
    for key in ("source_url", "court_url", "url"):
        url = redact_criminal_portal_url(data.get(key))
        if url:
            return url
    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    redirect = redact_criminal_portal_url(search.get("redirect"))
    if redirect:
        return redirect
    portal_id = clean(data.get("portal_case_id") or data.get("portalCaseId"))
    if portal_id:
        return f"{CRIMINAL_PORTAL_URL}?CaseId={urllib.parse.quote(portal_id)}"
    return CRIMINAL_PORTAL_URL


def first_text(row: dict, *keys: str) -> str:
    for key in keys:
        value = clean(row.get(key))
        if value:
            return value
    return ""


def split_criminal_start_time(value: object) -> tuple[str, str]:
    raw = clean(value)
    if not raw:
        return "", ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T\s]+(.+))?$", raw)
    if m:
        return m.group(1), clean(m.group(2))
    m = re.match(r"^(\d{1,2}/\d{1,2}/\d{2,4})(?:\s+(.+))?$", raw)
    if m:
        return m.group(1), clean(m.group(2))
    return raw, ""


def normalize_criminal_docket_rows(data: dict) -> list[dict]:
    rows = data.get("roa") if isinstance(data.get("roa"), list) else []
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["__index"] = index
        item["date_filed"] = first_text(row, "date_filed", "filedDate", "FILEDATE", "filed", "date")
        item["description"] = first_text(row, "description", "docketEntryComment", "RTEXT", "text", "title")
        item["submitter"] = first_text(row, "submitter", "otherSubmitter")
        item["source"] = "criminal_portal_roa"
        out.append(item)
    return out


def normalize_criminal_calendar_rows(data: dict) -> list[dict]:
    rows = data.get("calendar") if isinstance(data.get("calendar"), list) else []
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        court_date, hearing_time = split_criminal_start_time(
            first_text(row, "court_date", "startTime", "date", "start_time")
        )
        item = dict(row)
        item["__index"] = index
        item["court_date"] = first_text(row, "court_date", "date") or court_date
        item["hearing_time"] = first_text(row, "hearing_time", "time") or hearing_time
        item["matters"] = first_text(row, "matters", "hearingType", "calendar_matter", "description")
        item["hearing_type"] = first_text(row, "hearing_type", "hearingType")
        item["location"] = first_text(row, "location", "room")
        item["department"] = first_text(row, "department", "dept")
        item["source"] = "criminal_portal_calendar"
        out.append(item)
    return out


def statute_code(value: str) -> str:
    value = re.sub(r"\bPEN(?:AL)?\s+CODE\b", "PC", value, flags=re.I)
    value = re.sub(r"\bSECTION\b|\bSEC\.\b|§", "", value, flags=re.I)
    return clean(value).upper()


def normalize_criminal_statutes(data: dict, docket_entries: list[dict]) -> list[dict]:
    hits: dict[str, dict] = {}
    seen_lines: set[str] = set()

    def add_text(source: str, value: object) -> None:
        line = clean(value)
        if not line:
            return
        line_key = line.upper()
        if line_key in seen_lines:
            return
        seen_lines.add(line_key)
        for match in STATUTE_RE.finditer(line):
            code = statute_code(match.group(0))
            if not code:
                continue
            prev = hits.setdefault(
                code,
                {
                    "code": code,
                    "count": 0,
                    "sources": [],
                    "classification": "procedural" if PROCEDURAL_STATUTE_RE.search(code) else "unknown",
                },
            )
            prev["count"] += 1
            if source not in prev["sources"]:
                prev["sources"].append(source)
            if prev["classification"] != "procedural" and re.search(
                r"\b(?:complaint|information|indictment|charge|plea)\b", line, re.I
            ):
                prev["classification"] = "charge_candidate"

    raw_roa = data.get("roa") if isinstance(data.get("roa"), list) else []
    for row in raw_roa:
        if isinstance(row, dict):
            add_text("roa", row.get("docketEntryComment") or row.get("description") or row.get("text"))
    for row in docket_entries:
        if isinstance(row, dict):
            add_text("docket_entries", row.get("description") or row.get("text") or row.get("title"))
    add_text("criminal_index", data.get("charges"))
    criminal_index = data.get("criminal_index") if isinstance(data.get("criminal_index"), dict) else {}
    add_text("criminal_index", criminal_index.get("charges"))
    rows = criminal_index.get("rows") if isinstance(criminal_index.get("rows"), list) else []
    for row in rows:
        if isinstance(row, dict):
            add_text("criminal_index", row.get("charges") or row.get("CHARGES"))
    return sorted(hits.values(), key=lambda row: row["code"])


def has_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(clean(value))
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return value is not None


def merge_unique_rows(*rows: object) -> list:
    out: list = []
    seen: set[str] = set()
    for group in rows:
        if not isinstance(group, list):
            continue
        for row in group:
            try:
                key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except TypeError:
                key = repr(row)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def merge_criminal_index(existing: object, incoming: object) -> dict:
    existing_dict = existing if isinstance(existing, dict) else {}
    incoming_dict = incoming if isinstance(incoming, dict) else {}
    merged = {**incoming_dict, **existing_dict}
    for key in ("rows", "matches"):
        merged_rows = merge_unique_rows(existing_dict.get(key), incoming_dict.get(key))
        if merged_rows:
            merged[key] = merged_rows
    return merged


def merge_case_header(existing: object, incoming: object) -> dict:
    existing_dict = existing if isinstance(existing, dict) else {}
    incoming_dict = incoming if isinstance(incoming, dict) else {}
    merged = dict(existing_dict)
    for key, value in incoming_dict.items():
        if has_value(value):
            merged[key] = value
    return merged


def merge_preserved_fields(existing: object, fields: dict) -> dict:
    existing_dict = existing if isinstance(existing, dict) else {}
    merged = dict(existing_dict)
    for key, value in fields.items():
        if has_value(value):
            merged[key] = value
    return merged


def merge_dict_preserving_existing(existing: object, incoming: object) -> dict:
    existing_dict = existing if isinstance(existing, dict) else {}
    incoming_dict = incoming if isinstance(incoming, dict) else {}
    merged = dict(existing_dict)
    for key, value in incoming_dict.items():
        if not has_value(merged.get(key)) and has_value(value):
            merged[key] = value
    return merged


def values_differ(left: object, right: object) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        return clean(left).casefold() != clean(right).casefold()
    return left != right


def note_portal_observations(out: dict, existing_norm: dict, incoming_norm: dict, portal_redaction: str) -> None:
    observed = out.get("criminal_portal") if isinstance(out.get("criminal_portal"), dict) else {}
    for key in ("defendant", "filed_date", "charges", "case_title", "criminal_case_type"):
        incoming_value = incoming_norm.get(key)
        existing_value = existing_norm.get(key)
        if has_value(incoming_value) and (not has_value(existing_value) or values_differ(existing_value, incoming_value)):
            observed[key] = incoming_value
    if isinstance(incoming_norm.get("case_header"), dict) and incoming_norm.get("case_header"):
        observed["case_header"] = incoming_norm["case_header"]
    if portal_redaction:
        observed["redaction"] = {
            "text": portal_redaction,
            "policy": "index fields remain canonical; portal redaction/conflict is preserved as portal metadata",
        }
        out["criminal_portal_redaction"] = observed["redaction"]
    if observed:
        out["criminal_portal"] = observed


def intentional_criminal_portal_redaction_text(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    explicit = data.get("criminal_portal_redaction") if isinstance(data.get("criminal_portal_redaction"), dict) else {}
    if explicit:
        return clean(explicit.get("text") or explicit.get("reason") or "Criminal portal redaction indicated.")
    status = clean(data.get("status")).lower()
    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    messages = [
        data.get("message"),
        data.get("unavailable_text"),
        data.get("unavailable_reason"),
        search.get("message"),
    ]
    rows = search.get("rows")
    if isinstance(rows, list):
        messages.extend(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows if isinstance(row, dict))
    joined = clean(" ".join(clean(value) for value in messages if clean(value)))
    if (
        status in {"restricted", "not_public", "not_publicly_available"}
        or re.search(r"\b(?:not\s+public(?:ly)?\s+available|confidential|sealed|restricted|not\s+available\s+to\s+the\s+public)\b", joined, re.I)
    ):
        return joined or "Criminal portal indicates this case is not publicly available."
    return ""


def has_criminal_index_facts(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    criminal_index = data.get("criminal_index")
    return isinstance(criminal_index, dict) and bool(criminal_index)


def merge_criminal_case_data(existing: dict | None, incoming: dict) -> dict:
    """Merge a later criminal portal pull with earlier index-discovered facts.

    The criminal index exposes charges that usually do not appear in ROA rows.
    A portal refresh must enrich the index stub with ROA/header data, not replace
    those index-only fields.
    """
    if not existing or not isinstance(existing, dict):
        return incoming
    if not (is_criminal_portal_case(existing) and is_criminal_portal_case(incoming)):
        return incoming

    existing_norm = normalize_case_data(existing, clean(existing.get("case_number")))
    incoming_norm = normalize_case_data(incoming, clean(incoming.get("case_number")))
    # Start from the raw existing archive object, not only the normalized view, so
    # future index-only fields survive even before the importer knows their names.
    portal_redaction = (
        intentional_criminal_portal_redaction_text(incoming)
        or intentional_criminal_portal_redaction_text(incoming_norm)
        or intentional_criminal_portal_redaction_text(existing)
        or intentional_criminal_portal_redaction_text(existing_norm)
    )
    existing_has_index = has_criminal_index_facts(existing) or has_criminal_index_facts(existing_norm)
    incoming_has_index = has_criminal_index_facts(incoming) or has_criminal_index_facts(incoming_norm)
    index_norm = incoming_norm if incoming_has_index else existing_norm if existing_has_index else {}
    portal_norm = existing_norm if incoming_has_index and not existing_has_index else incoming_norm if not incoming_has_index else {}
    out = dict(existing)
    out.update({key: value for key, value in existing_norm.items() if has_value(value)})

    prefer_incoming_scalar = [
        "schema",
        "source",
        "status",
        "case_type",
        "case_number",
        "criminal_case_number",
        "display_case_number",
        "court",
        "cause_of_action",
        "source_url",
        "criminal_case_type",
        "captured_at",
        "case_header_checked_at",
        "portal_case_id",
    ]
    for key in prefer_incoming_scalar:
        if has_value(incoming_norm.get(key)):
            out[key] = incoming_norm[key]
        elif has_value(existing_norm.get(key)):
            out[key] = existing_norm[key]

    # Index-derived fields are canonical for identity and charges because the
    # portal ROA usually omits charges and may redact fields inconsistently.
    for key in ("defendant", "filed_date", "charges", "case_title"):
        if has_value(index_norm.get(key)):
            out[key] = index_norm[key]
        elif has_value(existing_norm.get(key)):
            out[key] = existing_norm[key]
        elif has_value(incoming_norm.get(key)):
            out[key] = incoming_norm[key]
    if portal_norm:
        note_portal_observations(out, index_norm or existing_norm, portal_norm, portal_redaction)
    else:
        note_portal_observations(out, existing_norm, incoming_norm, portal_redaction)

    for key, value in incoming.items():
        if key in out and has_value(out.get(key)):
            continue
        if has_value(value):
            out[key] = value

    for key in ("source_detail", "document_byte_capture_scope", "unavailable_reason", "unavailable_text"):
        if has_value(incoming_norm.get(key)):
            out[key] = incoming_norm[key]
        elif has_value(existing_norm.get(key)):
            out[key] = existing_norm[key]

    for key in ("roa", "docket_entries", "calendar", "attorneys", "documents", "payments"):
        if isinstance(incoming_norm.get(key), list) and incoming_norm.get(key):
            out[key] = incoming_norm[key]
        elif isinstance(existing_norm.get(key), list):
            out[key] = existing_norm[key]

    out["parties"] = merge_unique_rows(existing_norm.get("parties"), incoming_norm.get("parties"))
    out["case_header"] = merge_dict_preserving_existing(existing_norm.get("case_header"), incoming_norm.get("case_header"))
    out["criminal_index"] = merge_criminal_index(existing_norm.get("criminal_index"), incoming_norm.get("criminal_index"))

    existing_criminal = existing_norm.get("criminal") if isinstance(existing_norm.get("criminal"), dict) else {}
    incoming_criminal = incoming_norm.get("criminal") if isinstance(incoming_norm.get("criminal"), dict) else {}
    out["criminal"] = {**existing_criminal, **incoming_criminal}
    if isinstance(existing_criminal.get("statutes"), list) or isinstance(incoming_criminal.get("statutes"), list):
        out["criminal"]["statutes"] = merge_unique_rows(existing_criminal.get("statutes"), incoming_criminal.get("statutes"))
    if isinstance(existing_criminal.get("inferred_charges"), list) or isinstance(incoming_criminal.get("inferred_charges"), list):
        out["criminal"]["inferred_charges"] = merge_unique_rows(
            existing_criminal.get("inferred_charges"),
            incoming_criminal.get("inferred_charges"),
        )

    if existing_norm.get("document_bytes_captured") is True or incoming_norm.get("document_bytes_captured") is True:
        out["document_bytes_captured"] = True

    return normalize_case_data(out, clean(out.get("case_number")))


def criminal_unavailable_text(data: dict) -> str:
    status = clean(data.get("status")).lower()
    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    reason = clean(data.get("unavailable_reason"))
    human_reason = "" if re.fullmatch(r"[a-z0-9_:-]+", reason) else reason
    messages = [
        data.get("message"),
        data.get("unavailable_text"),
        human_reason,
        search.get("message"),
    ]
    rows = search.get("rows")
    if isinstance(rows, list):
        messages.extend(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows if isinstance(row, dict))
    joined = clean(" ".join(clean(value) for value in messages if clean(value)))
    if (
        status in {"unavailable", "restricted", "not_public", "not_publicly_available"}
        or re.search(r"\b(?:not\s+public(?:ly)?\s+available|confidential|sealed|restricted|not\s+available\s+to\s+the\s+public)\b", joined, re.I)
    ):
        return joined or "Criminal portal indicates this case is not publicly available."
    return ""


def criminal_charge_text(data: dict) -> str:
    values = [data.get("charges")]
    criminal_index = data.get("criminal_index") if isinstance(data.get("criminal_index"), dict) else {}
    values.append(criminal_index.get("charges"))
    rows = criminal_index.get("rows") if isinstance(criminal_index.get("rows"), list) else []
    for row in rows:
        if isinstance(row, dict):
            values.append(row.get("charges") or row.get("CHARGES"))
    for value in values:
        text = clean(value)
        if text:
            return text
    return ""


def criminal_no_information_text(defendant: str = "", filed_date: str = "", charges: str = "") -> str:
    defendant = clean(defendant)
    filed_date = clean(filed_date)
    charges = clean(charges)
    facts: list[str] = []
    if defendant:
        facts.append(f"the name of the defendant, {defendant}")
    if filed_date:
        facts.append(f"date of filing, {filed_date}")
    if charges:
        facts.append(f"charges in the case: {charges}")
    if facts:
        if len(facts) == 1:
            return f"No information available besides {facts[0]}."
        return "No information available besides " + ", ".join(facts[:-1]) + f", and {facts[-1]}."
    return "No information available."


def criminal_case_exists(data: dict) -> bool:
    if clean(data.get("portal_case_id") or data.get("portalCaseId")):
        return True
    if criminal_unavailable_text(data):
        return True
    status = clean(data.get("status")).lower()
    return status in {"found", "unavailable", "restricted", "not_public", "not_publicly_available"}


def normalize_case_data(data: dict, fallback_stem: str = "") -> dict:
    if not is_criminal_portal_case(data):
        return data
    case_number = criminal_archive_case_number(data, fallback_stem)
    raw_number = criminal_raw_number(data, fallback_stem)
    case_header = data.get("case_header") if isinstance(data.get("case_header"), dict) else {}
    portal_redaction = intentional_criminal_portal_redaction_text(data)
    defendant = clean(data.get("defendant") or case_header.get("defendant"))
    filed_date = clean(data.get("filed_date") or case_header.get("filed_date"))
    display_case_number = clean(data.get("display_case_number") or case_header.get("case_number"))
    criminal_case_type = clean(data.get("criminal_case_type") or case_header.get("case_type"))
    criminal_title = clean(data.get("case_title") or data.get("title"))
    charges = criminal_charge_text(data)
    charge_rows = parse_charge_rows(charges)
    if defendant and (
        not criminal_title
        or re.fullmatch(r"San Francisco criminal case\s+\d+", criminal_title, re.I)
        or criminal_title.upper() == defendant.upper()
    ):
        criminal_title = f"People v. {defendant}"
    docket_entries = data.get("docket_entries") if isinstance(data.get("docket_entries"), list) else normalize_criminal_docket_rows(data)
    calendar = normalize_criminal_calendar_rows(data)
    statutes = normalize_criminal_statutes(data, docket_entries)
    unavailable_text = criminal_unavailable_text(data)
    unavailable_reason = clean(data.get("unavailable_reason")) if unavailable_text else ""
    parties = data.get("parties") if isinstance(data.get("parties"), list) else (
        [{"name": defendant, "party_type": "Defendant", "source": "criminal_portal_case_header"}]
        if defendant
        else []
    )
    attorneys = data.get("attorneys") if isinstance(data.get("attorneys"), list) else []
    documents = data.get("documents") if isinstance(data.get("documents"), list) else []
    payments = data.get("payments") if isinstance(data.get("payments"), list) else []
    no_public_rows = not docket_entries and not calendar and not attorneys and not documents and not payments
    stale_no_public_text = (
        unavailable_reason == "criminal_portal_no_public_entries"
        or "criminal_portal_no_public_entries" in unavailable_text
    )
    if (not unavailable_text or stale_no_public_text) and no_public_rows and (defendant or filed_date or charges):
        unavailable_text = criminal_no_information_text(defendant, filed_date, charges)
        unavailable_reason = "criminal_portal_no_public_entries"
    criminal = data.get("criminal") if isinstance(data.get("criminal"), dict) else {}
    criminal = {
        **criminal,
        "raw_case_number": raw_number,
        "portal_case_id": clean(data.get("portal_case_id") or data.get("portalCaseId")),
        "display_case_number": display_case_number,
        "defendant": defendant,
        "filed_date": filed_date,
        "charges": charges,
        "charge_rows": charge_rows,
        "case_type": criminal_case_type,
        "case_header": case_header,
        "statutes": statutes,
        "inferred_charges": [
            {**row, "inference": "tentative_from_criminal_docket_text"}
            for row in statutes
            if row.get("classification") == "charge_candidate"
        ],
    }
    out = dict(data)
    search = out.get("search") if isinstance(out.get("search"), dict) else {}
    if search:
        out["search"] = {
            **search,
            "redirect": redact_criminal_portal_url(search.get("redirect")) or search.get("redirect"),
        }
    for url_key in ("source_url", "court_url", "url"):
        if out.get(url_key):
            out[url_key] = redact_criminal_portal_url(out.get(url_key))
    out.update(
        {
            "schema": data.get("schema") or CRIMINAL_PORTAL_SCHEMA,
            "source": CRIMINAL_PORTAL_SOURCE,
            "case_type": "criminal",
            "case_number": case_number,
            "criminal_case_number": raw_number,
            "display_case_number": display_case_number,
            "defendant": defendant,
            "filed_date": filed_date,
            "charges": charges,
            "charges_parsed": charge_rows,
            "criminal_case_type": criminal_case_type,
            "case_header": case_header,
            "case_title": criminal_title
            or (f"San Francisco criminal case {raw_number}" if raw_number else "San Francisco criminal case"),
            "court": clean(data.get("court")) or "San Francisco Superior Court - Criminal",
            "cause_of_action": clean(data.get("cause_of_action") or data.get("cause")) or "Criminal",
            "source_url": criminal_source_url(data),
            "docket_entries": docket_entries,
            "calendar": calendar,
            "parties": parties,
            "attorneys": attorneys,
            "documents": documents,
            "payments": payments,
            "document_bytes_captured": data.get("document_bytes_captured") is True
            or not documents,
            "document_byte_capture_scope": "criminal-portal-no-documents"
            if not documents
            else data.get("document_byte_capture_scope"),
            "criminal": criminal,
        }
    )
    if unavailable_text:
        out["status"] = "unavailable"
        out["unavailable_reason"] = unavailable_reason or "criminal_portal_not_publicly_available"
        out["unavailable_text"] = unavailable_text
    if portal_redaction:
        out["criminal_portal_redaction"] = {
            "text": portal_redaction,
            "policy": "index fields remain canonical; portal redaction/conflict is preserved as portal metadata",
        }
        criminal_portal = out.get("criminal_portal") if isinstance(out.get("criminal_portal"), dict) else {}
        criminal_portal.setdefault("redaction", out["criminal_portal_redaction"])
        out["criminal_portal"] = criminal_portal
    return out


def read_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"skip invalid {path}: {exc}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as f:
        tmp = Path(f.name)
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def append_index(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    by_case: dict[str, dict] = {}
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as existing_fh:
            for lineno, line in enumerate(existing_fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{lineno}: invalid JSON in case index: {exc}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{lineno}: row is not an object")
                case_number = norm_case(row.get("case_number"))
                if not case_number:
                    raise ValueError(f"{path}:{lineno}: row missing case_number")
                row["case_number"] = case_number
                by_case.pop(case_number, None)
                by_case[case_number] = row
    for record in records:
        case_number = norm_case(record.get("case_number"))
        if not case_number:
            continue
        row = dict(record)
        row["case_number"] = case_number
        by_case.pop(case_number, None)
        by_case[case_number] = row
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as f:
        tmp = Path(f.name)
        for record in by_case.values():
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def schema_error(data: dict) -> str:
    if is_criminal_portal_case(data) and not criminal_case_exists(data):
        return "criminal_case_not_found"
    if not norm_case(data.get("case_number")):
        return "missing_case_number"
    if data.get("status") == "unavailable":
        return ""
    if not isinstance(data.get("docket_entries"), list):
        return "docket_entries_not_list"
    if not isinstance(data.get("documents"), list):
        return "documents_not_list"
    return ""


def summary_record(data: dict, case_number: str) -> dict:
    docket_entries = data.get("docket_entries")
    documents = data.get("documents")
    docs = documents if isinstance(documents, list) else []
    record = {
        "case_number": case_number,
        "captured_at": data.get("captured_at"),
        "n_entries": len(docket_entries) if isinstance(docket_entries, list) else 0,
        "n_documents": len(docs),
        "documents_bytes_count": sum(1 for doc in docs if isinstance(doc, dict) and doc.get("sha256")),
        "documents_unavailable_count": sum(
            1 for doc in docs if isinstance(doc, dict) and doc.get("is_available") is False
        ),
        "documents_deferred_count": sum(
            1 for doc in docs if isinstance(doc, dict) and storage.doc_byte_deferred(doc)
        ),
        "document_bytes_captured": has_complete_document_assets(data),
        "source_url": data.get("source_url"),
    }
    if data.get("case_type"):
        record["case_type"] = data.get("case_type")
    if data.get("criminal_case_number"):
        record["criminal_case_number"] = data.get("criminal_case_number")
    if data.get("portal_case_id"):
        record["portal_case_id"] = data.get("portal_case_id")
    if data.get("source"):
        record["source"] = data.get("source")
    return record


def docket_indicates_documents(data: dict) -> bool:
    entries = data.get("docket_entries")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("has_document") is True or entry.get("doc_id"):
            return True
        for key in ("document_url", "view_url", "url"):
            value = str(entry.get(key) or "")
            if "CaseInfo.dll" in value or "imgquery" in value:
                return True
    return False


def has_complete_document_assets(data: dict) -> bool:
    if data.get("status") == "unavailable":
        return True
    documents = data.get("documents")
    # Byte-first invariant: a full capture ALWAYS writes a `documents` array (the
    # scanner sets `documents: docs`, possibly empty). A MISSING `documents` key
    # therefore means document enumeration never ran (metadata-only capture) and
    # the case must NOT be treated as complete. An empty list is complete only
    # when byte capture was marked complete and the docket advertises no docs.
    if not isinstance(documents, list):
        return False
    if not documents:
        return data.get("document_bytes_captured") is True and not docket_indicates_documents(data)
    for doc in documents:
        if not isinstance(doc, dict):
            return False
        if doc.get("is_available") is False:
            continue
        if storage.doc_byte_deferred(doc):
            continue
        if not storage.doc_has_archived_object(doc):
            return False
    return True


def existing_archive_is_complete(dest: Path) -> bool:
    data = read_json(dest)
    return bool(data and has_complete_document_assets(data))


def import_cases(args: argparse.Namespace) -> int:
    scanner_dir = args.scanner_dir
    archive_dir = args.archive_dir
    index_path = args.index
    imported: list[dict] = []
    stats = {
        "imported": 0,
        "skipped_existing": 0,
        "skipped_error_files": 0,
        "skipped_invalid": 0,
        "skipped_schema_invalid": 0,
        "skipped_case_mismatch": 0,
        "skipped_incomplete_documents": 0,
    }

    if not scanner_dir.exists():
        raise SystemExit(f"scanner dir does not exist: {scanner_dir}")

    for src in sorted(scanner_dir.glob("*.json")):
        if src.name.endswith(".error.json"):
            stats["skipped_error_files"] += 1
            continue
        data = read_json(src)
        if data is None:
            stats["skipped_invalid"] += 1
            continue
        data = normalize_case_data(data, src.stem)
        case_number = norm_case(data.get("case_number") or src.stem)
        src_case_number = norm_case(src.stem)
        if is_criminal_portal_case(data):
            raw_number = criminal_raw_number(data, src.stem)
            source_matches = src_case_number in {case_number, norm_case(raw_number)}
        else:
            source_matches = case_number == src_case_number
        if not case_number or not source_matches:
            stats["skipped_case_mismatch"] += 1
            print(f"skip case mismatch {src}: {data.get('case_number')!r}", file=sys.stderr)
            continue
        dest = archive_dir / f"{case_number}.json"
        if dest.exists() and is_criminal_portal_case(data):
            existing = read_json(dest)
            if existing:
                data = merge_criminal_case_data(existing, data)
        schema_reason = schema_error(data)
        if schema_reason:
            stats["skipped_schema_invalid"] += 1
            print(f"skip schema invalid {src}: {schema_reason}", file=sys.stderr)
            continue
        if not has_complete_document_assets(data):
            stats["skipped_incomplete_documents"] += 1
            print(f"skip incomplete document assets {src}", file=sys.stderr)
            continue
        if dest.exists() and not args.overwrite_existing:
            if existing_archive_is_complete(dest):
                stats["skipped_existing"] += 1
                continue
            stats.setdefault("replaced_incomplete_existing", 0)
            stats["replaced_incomplete_existing"] += 1
        stats["imported"] += 1
        imported.append(summary_record(data, case_number))
        if not args.dry_run:
            write_json_atomic(dest, data)

    if not args.dry_run:
        append_index(index_path, imported)

    for key, value in stats.items():
        print(f"{key}: {value}")
    if args.dry_run:
        print("dry_run: true")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scanner-dir",
        type=Path,
        default=ROOT / ".scanner" / "cases",
        help="Directory containing local_case_scanner JSON output.",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=ROOT / "archive" / "cases",
        help="Committed archive/cases directory.",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=ROOT / "archive" / "cases-index.ndjson",
        help="Append-only archive cases index.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Replace archive/cases files that already exist.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.scanner_dir = args.scanner_dir.resolve()
    args.archive_dir = args.archive_dir.resolve()
    args.index = args.index.resolve()
    return args


if __name__ == "__main__":
    raise SystemExit(import_cases(parse_args(sys.argv[1:])))
