#!/usr/bin/env python3
"""Validate criminal statute version metadata and its viewer mirror."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "assets" / "data" / "criminal-statute-current-versions.json"
DEFAULT_JS = ROOT / "assets" / "js" / "criminal-statute-current-versions.js"
DEFAULT_MANIFEST = ROOT / "assets" / "sources" / "criminal-statutes" / "manifest.json"
EXPECTED_SCHEMA = "sfsc-criminal-statute-current-versions-v1"
EXPECTED_MANIFEST_SCHEMA = "sfsc-criminal-statute-originals-v1"
JS_EXPORT_RE = re.compile(
    r"\A\s*(?://[^\n]*\n)*export const CRIMINAL_STATUTE_CURRENT_VERSION_LOOKUP = (?P<payload>\{.*\});\s*\Z",
    re.DOTALL,
)


class ValidationError(Exception):
    """Raised when statute-version artifacts are malformed or divergent."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return payload


def load_js(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = JS_EXPORT_RE.match(text)
    if not match:
        raise ValidationError(f"{path} must export CRIMINAL_STATUTE_CURRENT_VERSION_LOOKUP as a JSON object literal")
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path} export is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{path} export must be a JSON object")
    return payload


def validate_versions(payload: dict[str, Any]) -> None:
    if payload.get("schema") != EXPECTED_SCHEMA:
        raise ValidationError(f"statute version schema must be {EXPECTED_SCHEMA!r}")
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        raise ValidationError("statute version payload must include coverage")
    if coverage.get("observed_section_count") != len(payload.get("sections") or {}):
        raise ValidationError("coverage observed_section_count does not match sections")
    if not coverage.get("observed_code_year_count"):
        raise ValidationError("coverage must include observed code/year counts")
    sections = payload.get("sections")
    if not isinstance(sections, dict) or not sections:
        raise ValidationError("statute version payload must include sections")
    for key, record in sections.items():
        if not isinstance(record, dict):
            raise ValidationError(f"{key}: record must be an object")
        code_system = record.get("code_system")
        section = record.get("section")
        if not code_system or not section or key != f"{code_system} {section}":
            raise ValidationError(f"{key}: record code_system/section must match key")
        if not record.get("source_url"):
            raise ValidationError(f"{key}: record missing official current source_url")
        if not isinstance(record.get("observed_years"), dict) or not record.get("observed_years"):
            raise ValidationError(f"{key}: record missing observed_years")
        versions = record.get("historical_versions") or []
        if versions and not isinstance(versions, list):
            raise ValidationError(f"{key}: historical_versions must be a list")
        previous_end = ""
        for version in versions:
            if not isinstance(version, dict):
                raise ValidationError(f"{key}: historical version must be an object")
            for required in ("effective_from", "url", "official_source_url", "release_tag", "release_asset", "sha256"):
                if not version.get(required):
                    raise ValidationError(f"{key}: historical version missing {required}")
            if "releases/download/" not in str(version.get("url")):
                raise ValidationError(f"{key}: historical url must point to a GitHub Release download asset")
            start = str(version.get("effective_from"))
            end = str(version.get("effective_to") or "")
            if previous_end and start <= previous_end:
                raise ValidationError(f"{key}: historical versions must be sorted and non-overlapping")
            previous_end = end or "9999-12-31"


def validate_manifest(manifest: dict[str, Any], versions: dict[str, Any]) -> None:
    if manifest.get("schema") != EXPECTED_MANIFEST_SCHEMA:
        raise ValidationError(f"statute originals manifest schema must be {EXPECTED_MANIFEST_SCHEMA!r}")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValidationError("statute originals manifest must include release assets")
    by_asset = {asset.get("asset_name"): asset for asset in assets if isinstance(asset, dict)}
    for key, record in versions.get("sections", {}).items():
        for version in record.get("historical_versions") or []:
            asset = by_asset.get(version.get("release_asset"))
            if not asset:
                raise ValidationError(f"{key}: release asset {version.get('release_asset')!r} missing from manifest")
            if asset.get("sha256") != version.get("sha256"):
                raise ValidationError(f"{key}: release asset sha256 does not match manifest")


def validate(json_path: Path, js_path: Path, manifest_path: Path, *, allow_missing_js: bool = False) -> dict[str, Any]:
    payload = load_json(json_path)
    if js_path.exists():
        js_payload = load_js(js_path)
        if payload != js_payload:
            raise ValidationError("criminal statute JSON/JS drift detected")
    elif not allow_missing_js:
        raise ValidationError(f"{js_path} is missing; use --allow-missing-js only in the data repo")
    validate_versions(payload)
    manifest = load_json(manifest_path)
    validate_manifest(manifest, payload)
    return {
        "json": str(json_path),
        "js": str(js_path) if js_path.exists() else "",
        "manifest": str(manifest_path),
        "sections": len(payload["sections"]),
        "historical_versions": sum(len(record.get("historical_versions") or []) for record in payload["sections"].values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--js", type=Path, default=DEFAULT_JS)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--allow-missing-js", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = validate(args.json, args.js, args.manifest, allow_missing_js=args.allow_missing_js)
    except ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
