#!/usr/bin/env python3
"""Validate generated per-case party/attorney representation sidecars."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "case-representation-manifest.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def inside(base: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def check_case_representation(manifest_path: Path) -> list[str]:
    failures: list[str] = []
    if not manifest_path.exists():
        return [f"missing representation manifest: {manifest_path}"]
    try:
        manifest = load_json(manifest_path)
    except Exception as exc:
        return [f"could not read {manifest_path}: {exc}"]

    sidecars = manifest.get("sidecars")
    if not isinstance(sidecars, list):
        failures.append("manifest.sidecars is not an array")
        sidecars = []
    zero_edge_cases = manifest.get("zero_edge_cases")
    if not isinstance(zero_edge_cases, list):
        failures.append("manifest.zero_edge_cases is not an array")
        zero_edge_cases = []
    for row in zero_edge_cases:
        if not isinstance(row, dict):
            failures.append("zero_edge_cases contains a non-object row")
            continue
        if not row.get("case_number"):
            failures.append("zero_edge_cases row missing case_number")
        if not row.get("empty_reason"):
            failures.append(f"{row.get('case_number')}: zero-edge case missing empty_reason")
        if int(row.get("edge_count") or 0) != 0:
            failures.append(f"{row.get('case_number')}: zero-edge case has edge_count={row.get('edge_count')}")

    data_root = manifest_path.parent
    for meta in sidecars:
        if not isinstance(meta, dict):
            failures.append("manifest.sidecars contains a non-object row")
            continue
        rel = str(meta.get("path") or "").strip()
        case_number = str(meta.get("case_number") or "").strip()
        if not rel:
            failures.append(f"{case_number or '(unknown)'}: sidecar missing path")
            continue
        path = data_root / rel
        if not inside(data_root, path):
            failures.append(f"{case_number}: sidecar path escapes data root: {rel}")
            continue
        if not path.exists():
            failures.append(f"{case_number}: missing sidecar {rel}")
            continue
        try:
            payload = load_json(path)
        except Exception as exc:
            failures.append(f"{case_number}: could not read {rel}: {exc}")
            continue
        summary = payload.get("summary")
        if not isinstance(summary, dict):
            failures.append(f"{case_number}: summary is not an object")
            summary = {}
        parties = payload.get("parties") if isinstance(payload.get("parties"), list) else []
        attorneys = payload.get("attorneys") if isinstance(payload.get("attorneys"), list) else []
        edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
        counts = {
            "party_count": len(parties),
            "attorney_count": len(attorneys),
            "edge_count": len(edges),
        }
        for key, actual in counts.items():
            if int(summary.get(key) or 0) != actual:
                failures.append(f"{case_number}: summary.{key}={summary.get(key)} but array has {actual}")
            if int(meta.get(key) or 0) != actual:
                failures.append(f"{case_number}: manifest {key}={meta.get(key)} but sidecar has {actual}")
        empty_reason = str(summary.get("empty_reason") or "").strip()
        if edges and empty_reason:
            failures.append(f"{case_number}: sidecar has edges but non-empty empty_reason={empty_reason!r}")
        if not edges and (parties or attorneys) and not empty_reason:
            failures.append(f"{case_number}: sidecar has party/attorney rows, zero edges, and no empty_reason")
        if payload.get("case_number") != case_number:
            failures.append(f"{case_number}: payload case_number={payload.get('case_number')!r}")

    expected = int(manifest.get("sidecar_count") or 0)
    if expected != len(sidecars):
        failures.append(f"manifest.sidecar_count={expected} but sidecars has {len(sidecars)} row(s)")
    expected_zero = int(manifest.get("zero_edge_case_count") or 0)
    if expected_zero != len(zero_edge_cases):
        failures.append(
            f"manifest.zero_edge_case_count={expected_zero} but zero_edge_cases has {len(zero_edge_cases)} row(s)"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    failures = check_case_representation(args.manifest)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    manifest = load_json(args.manifest)
    print(
        "case-representation ok: "
        f"{manifest.get('sidecar_count', 0)} sidecars, "
        f"{manifest.get('zero_edge_case_count', 0)} explicit zero-edge cases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
