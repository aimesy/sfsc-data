#!/usr/bin/env python3
"""Fail commits that add known bulky/generated repository paths."""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
GITHUB_HARD_MAX_BYTES = 100 * 1024 * 1024
ALLOWED_LARGE = {
    # Browser cache kept for Pages until it is sharded or release-backed.
    "data/entity-profiles.json",
    # Canonical append-only case index; integrity is checked separately.
    "archive/cases-index.ndjson",
    "tentatives.parquet",
    "sfsc-extension.zip",
}
ALLOWED_LARGE_GLOBS = (
    "data/*.parquet",
    "coverage/*.json",
    "coverage/**/*.json",
)
PRODUCT_REPO_DATA_GLOBS = (
    "archive/cases-index.ndjson",
    "archive/document-index.ndjson",
    "archive/cases/**",
    "archive/case-directory/**",
    "archive/new-filings-cases/**",
    "coverage/**",
    "data/**",
    "raw/**",
)
AUTHORIZED_DOCUMENT_COMMIT_SUBJECTS = (
    re.compile(r"^Archive \d+ scanner case\(s\)(?: \[skip ci\])?$"),
    re.compile(r"^Archive \d+ SFSC document byte object\(s\)(?: \[skip ci\])?$"),
    re.compile(r"^Migrate \d+ doc byte\(s\) across \d+ case\(s\) from releases to git(?: \[skip ci\])?$"),
    re.compile(r"^Migrate \d+ doc byte\(s\) to git \(batch \d+/\d+\)(?: \[skip ci\])?$"),
)


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def changed_paths(args: argparse.Namespace) -> list[str]:
    if args.path:
        return sorted({normalize_path(p) for p in args.path if normalize_path(p)})
    if args.staged:
        proc = run_git(["diff", "--cached", "--name-only", "--diff-filter=AM"])
    elif args.range:
        proc = run_git(["diff", "--name-only", "--diff-filter=AM", args.range])
    elif args.changed_from:
        proc = run_git(["diff", "--name-only", "--diff-filter=AM", args.changed_from, "HEAD"])
    else:
        proc = run_git(["diff", "--cached", "--name-only", "--diff-filter=AM"])
    return sorted({normalize_path(line) for line in proc.stdout.splitlines() if normalize_path(line)})


def revision_range(args: argparse.Namespace) -> str:
    if args.range:
        return args.range
    if args.changed_from:
        return f"{args.changed_from}..HEAD"
    return ""


def normalize_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")


def current_repo_slug() -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip().lower()
    if env_repo:
        return env_repo
    proc = run_git(["config", "--get", "remote.origin.url"], check=False)
    remote = proc.stdout.strip().lower().removesuffix(".git") if proc.returncode == 0 else ""
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if remote.startswith(prefix):
            return remote[len(prefix):]
    return ""


def staged_size(path: str) -> int:
    proc = run_git(["cat-file", "-s", f":{path}"], check=False)
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def head_size(path: str) -> int:
    proc = run_git(["cat-file", "-s", f"HEAD:{path}"], check=False)
    if proc.returncode == 0:
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return 0
    local = ROOT / path
    return local.stat().st_size if local.is_file() else 0


def path_size(path: str, args: argparse.Namespace) -> int:
    if args.staged or (not args.range and not args.changed_from and not args.path):
        return staged_size(path)
    return head_size(path)


def is_allowed_large(path: str) -> bool:
    if path in ALLOWED_LARGE:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in ALLOWED_LARGE_GLOBS)


def authorized_document_commit_subject(subject: str) -> bool:
    return any(pattern.match(subject) for pattern in AUTHORIZED_DOCUMENT_COMMIT_SUBJECTS)


def archive_document_write_authorized(args: argparse.Namespace) -> bool:
    if args.allow_git_document_writes:
        return True
    rev = revision_range(args)
    if not rev:
        return False
    proc = run_git(["log", "--format=%s", rev, "--", "archive/documents"], check=False)
    if proc.returncode != 0:
        return False
    subjects = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return bool(subjects) and all(authorized_document_commit_subject(subject) for subject in subjects)


def violations(paths: list[str], args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    max_bytes = max(0, int(args.max_bytes))
    allow_document_writes = archive_document_write_authorized(args)
    product_repo = current_repo_slug() == "aimesy/sfsc"
    for path in paths:
        lower = path.lower()
        if product_repo and any(fnmatch.fnmatch(path, pattern) for pattern in PRODUCT_REPO_DATA_GLOBS):
            out.append(f"{path}: data-side artifact belongs in aimesy/sfsc-data, not the product repo")
            continue
        if path.startswith("archive/documents/"):
            if not allow_document_writes:
                out.append(
                    f"{path}: archive/document byte objects require --allow-git-document-writes "
                    "or an authorized archive/backfill commit subject"
                )
            continue
        if path.startswith("data/ocr/"):
            out.append(f"{path}: OCR output belongs in external/artifact storage, not git")
            continue
        if lower.endswith(".zip") and path != "sfsc-extension.zip":
            out.append(f"{path}: stray zip export; only sfsc-extension.zip is allowed")
            continue
        size = path_size(path, args)
        if size > GITHUB_HARD_MAX_BYTES:
            out.append(f"{path}: {size} bytes exceeds GitHub's {GITHUB_HARD_MAX_BYTES} byte hard limit")
            continue
        if max_bytes and size > max_bytes and not is_allowed_large(path):
            out.append(f"{path}: {size} bytes exceeds {max_bytes} byte limit")
    return out


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Check staged added/modified files.")
    parser.add_argument("--range", default="", help="Git revision range to check, e.g. origin/master..HEAD.")
    parser.add_argument("--changed-from", default="", help="Check files changed from this ref to HEAD.")
    parser.add_argument("--path", action="append", default=[], help="Specific path to check. May be repeated.")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--allow-git-document-writes",
        action="store_true",
        help="Allow intentional archive/documents byte-object writes for a trusted repair/capture run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    paths = changed_paths(args)
    bad = violations(paths, args)
    if not bad:
        print(f"repo bloat guard passed ({len(paths)} changed path(s) checked)")
        return 0
    print("repo bloat guard failed:", file=sys.stderr)
    for item in bad:
        print(f"  - {item}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
