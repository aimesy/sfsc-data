#!/usr/bin/env python3
"""Move tracked archive/document byte objects to GitHub Release assets.

This is the reverse of the old "release assets to git" backfill. It preserves
every tracked `archive/documents/**` blob as a content-addressed release asset,
patches archive case metadata to `release_tag` + `asset_name`, and can remove
the tracked document-byte paths from the current Git tree after upload.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import document_storage as storage
import upload_scanner_doc_assets as assets


ROOT = Path(__file__).resolve().parents[1]
ROOT_RESOLVED = ROOT.resolve()
DEFAULT_ARCHIVE_REPO = os.environ.get("SFSC_ARCHIVE_REPO", "aimesy/sfsc-data")
DEFAULT_BASE_TAG = "docs-git-archive-2026-06-08"
DEFAULT_MAX_ASSETS_PER_RELEASE = 900
PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class PlannedAsset:
    path: str
    sha256: str
    asset_name: str
    release_tag: str
    release_index: int


def log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def run_git(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def tracked_document_paths(rev: str) -> list[str]:
    proc = run_git(["ls-tree", "-r", "--name-only", rev, "archive/documents"])
    return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())


def safe_asset_from_path(path: str) -> tuple[str, str]:
    pure = PurePosixPath(path)
    asset_name = storage.safe_stored_asset_name(pure.name)
    if not asset_name:
        raise ValueError(f"unsafe document asset path: {path}")
    shard = pure.parent.name.lower()
    sha = asset_name.split(".", 1)[0]
    if shard != sha[:2]:
        raise ValueError(f"document shard does not match sha: {path}")
    return sha, asset_name


def release_tag(base_tag: str, index: int) -> str:
    return base_tag if index <= 1 else f"{base_tag}-{index:03d}"


def build_plan(paths: list[str], base_tag: str, max_assets_per_release: int, limit: int = 0) -> list[PlannedAsset]:
    if limit:
        paths = paths[: max(0, limit)]
    out: list[PlannedAsset] = []
    for i, path in enumerate(paths):
        sha, asset_name = safe_asset_from_path(path)
        release_index = (i // max_assets_per_release) + 1
        out.append(
            PlannedAsset(
                path=path,
                sha256=sha,
                asset_name=asset_name,
                release_tag=release_tag(base_tag, release_index),
                release_index=release_index,
            )
        )
    return out


def token_value(args: argparse.Namespace) -> str:
    if args.token_env and os.environ.get(args.token_env):
        return os.environ[args.token_env]
    if args.token_from_git_credential:
        return assets.token_from_git_credential()
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    return ""


def git_blob_bytes(rev: str, path: str) -> bytes:
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout


def release_assets_by_name(owner: str, repo: str, release_id: int, token: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}/assets?per_page=100&page={page}"
        rows = assets.request_json(url, token)
        if not isinstance(rows, list):
            raise RuntimeError(f"unexpected release asset list for release {release_id}")
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name:
                out[name] = row
        if len(rows) < 100:
            return out
        page += 1


def ensure_releases(plan: list[PlannedAsset], args: argparse.Namespace, token: str) -> dict[str, dict[str, Any]]:
    releases: dict[str, dict[str, Any]] = {}
    for tag in sorted({item.release_tag for item in plan}, key=lambda t: (len(t), t)):
        info = assets.get_or_create_release_info(args.owner, args.repo_name, tag, token, args.branch)
        release_id = int(info["id"])
        existing = release_assets_by_name(args.owner, args.repo_name, release_id, token)
        releases[tag] = {"id": release_id, "assets": existing}
        log(f"release {tag}: id={release_id} existing_assets={len(existing)}")
    return releases


def verify_existing_asset(row: dict[str, Any], item: PlannedAsset, expected_size: int | None = None) -> bool:
    digest = str(row.get("digest") or "").strip().lower()
    if digest and digest != f"sha256:{item.sha256}":
        raise RuntimeError(f"{item.release_tag}/{item.asset_name}: existing digest mismatch {digest}")
    if expected_size is not None:
        try:
            size = int(row.get("size"))
        except (TypeError, ValueError):
            size = -1
        if size >= 0 and size != expected_size:
            raise RuntimeError(f"{item.release_tag}/{item.asset_name}: existing size mismatch {size} != {expected_size}")
    return True


def upload_one(item: PlannedAsset, releases: dict[str, dict[str, Any]], args: argparse.Namespace, token: str) -> str:
    release = releases[item.release_tag]
    existing = release["assets"].get(item.asset_name)
    if existing and not args.verify_existing:
        verify_existing_asset(existing, item)
        return "already"
    body = git_blob_bytes(args.rev, item.path)
    got = hashlib.sha256(body).hexdigest()
    if got != item.sha256:
        raise RuntimeError(f"{item.path}: blob sha mismatch {got} != {item.sha256}")
    if existing:
        verify_existing_asset(existing, item, len(body))
        return "already"
    content_type = mimetypes.guess_type(item.asset_name)[0] or "application/octet-stream"
    url = (
        f"https://uploads.github.com/repos/{args.owner}/{args.repo_name}/releases/{release['id']}/assets"
        f"?name={assets.gh_quote(item.asset_name)}"
    )
    status, text = assets.request_bytes(url, token, content_type, body)
    if status == 422:
        refreshed = release_assets_by_name(args.owner, args.repo_name, int(release["id"]), token)
        release["assets"] = refreshed
        existing = refreshed.get(item.asset_name)
        if existing:
            verify_existing_asset(existing, item, len(body))
            return "already"
    if status < 200 or status >= 300:
        raise RuntimeError(f"{item.release_tag}/{item.asset_name}: upload failed HTTP {status} {text[:200]}")
    try:
        uploaded = json.loads(text)
    except json.JSONDecodeError:
        uploaded = {}
    release["assets"][item.asset_name] = uploaded or {"name": item.asset_name, "size": len(body)}
    return "uploaded"


def upload_assets(plan: list[PlannedAsset], releases: dict[str, dict[str, Any]], args: argparse.Namespace, token: str) -> Counter:
    counts: Counter = Counter()
    total = len(plan)
    started = time.time()

    def task(item: PlannedAsset) -> tuple[str, PlannedAsset]:
        return upload_one(item, releases, args, token), item

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(task, item) for item in plan]
        for index, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            status, item = fut.result()
            counts[status] += 1
            if index % args.progress_every == 0 or index == total:
                elapsed = max(0.001, time.time() - started)
                rate = index / elapsed
                log(
                    f"upload progress {index}/{total}: uploaded={counts['uploaded']} "
                    f"already={counts['already']} rate={rate:.2f}/s last={item.release_tag}/{item.asset_name}"
                )
    return counts


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as fh:
        tmp = Path(fh.name)
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def patch_doc(doc: dict[str, Any], plan_by_path: dict[str, PlannedAsset], owner: str, repo: str) -> bool:
    object_path = storage.normalized_document_object_path(doc.get("object_path"))
    if not object_path:
        return False
    item = plan_by_path.get(object_path)
    if not item:
        return False
    if storage.valid_sha(doc.get("sha256")) and storage.valid_sha(doc.get("sha256")) != item.sha256:
        raise RuntimeError(f"{object_path}: doc sha does not match planned asset")
    doc["storage_backend"] = "release"
    doc["release_tag"] = item.release_tag
    doc["asset_name"] = item.asset_name
    doc["release_url"] = f"https://github.com/{owner}/{repo}/releases/download/{item.release_tag}/{item.asset_name}"
    doc.pop("object_path", None)
    doc.pop("object_url", None)
    doc.pop("byte_path", None)
    doc.pop("capture_error", None)
    return True


def patch_case_files(plan: list[PlannedAsset], args: argparse.Namespace) -> Counter:
    plan_by_path = {item.path: item for item in plan}
    counts: Counter = Counter()
    case_dir = ROOT / "archive" / "cases"
    for path in sorted(case_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        docs = data.get("documents") if isinstance(data.get("documents"), list) else []
        changed = 0
        for doc in docs:
            if isinstance(doc, dict) and patch_doc(doc, plan_by_path, args.owner, args.repo_name):
                changed += 1
        if changed:
            data["document_bytes_captured"] = all(
                not isinstance(d, dict)
                or storage.doc_has_archived_object(d)
                or storage.doc_byte_deferred(d)
                or d.get("is_available") is False
                for d in docs
            )
            write_json_atomic(path, data)
            counts["cases_changed"] += 1
            counts["docs_changed"] += changed
    return counts


def load_head_text(path: str) -> str:
    proc = run_git(["show", f"HEAD:{path}"], check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def patch_document_index(plan: list[PlannedAsset], args: argparse.Namespace) -> Counter:
    plan_by_path = {item.path: item for item in plan}
    src = ROOT / "archive" / "document-index.ndjson"
    text = src.read_text(encoding="utf-8") if src.exists() else load_head_text("archive/document-index.ndjson")
    counts: Counter = Counter()
    if not text:
        return counts
    rows: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and patch_doc(row, plan_by_path, args.owner, args.repo_name):
            counts["rows_changed"] += 1
        rows.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8", newline="\n")
    counts["rows"] = len(rows)
    return counts


def remove_tracked_documents() -> None:
    run_git(["rm", "-r", "--ignore-unmatch", "--sparse", "archive/documents"])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_ARCHIVE_REPO)
    parser.add_argument("--branch", default="master")
    parser.add_argument("--rev", default="HEAD")
    parser.add_argument("--base-tag", default=DEFAULT_BASE_TAG)
    parser.add_argument("--max-assets-per-release", type=int, default=DEFAULT_MAX_ASSETS_PER_RELEASE)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--token-env", default="")
    parser.add_argument("--token-from-git-credential", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--patch-metadata", action="store_true")
    parser.add_argument("--remove-git-documents", action="store_true")
    parser.add_argument(
        "--assume-uploaded",
        action="store_true",
        help="Allow metadata patch/removal without uploading in this invocation after a separately verified upload.",
    )
    parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args(argv)
    if "/" not in args.repo:
        raise SystemExit("--repo must be owner/repo")
    args.owner, args.repo_name = args.repo.split("/", 1)
    args.max_assets_per_release = max(1, args.max_assets_per_release)
    args.workers = max(1, args.workers)
    args.progress_every = max(1, args.progress_every)
    if (args.patch_metadata or args.remove_git_documents) and not (args.upload or args.assume_uploaded):
        raise SystemExit("refusing to patch/remove git document metadata without --upload or --assume-uploaded")
    if args.remove_git_documents and not args.patch_metadata:
        raise SystemExit("refusing to remove archive/documents without --patch-metadata")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    paths = tracked_document_paths(args.rev)
    plan = build_plan(paths, args.base_tag, args.max_assets_per_release, args.limit)
    releases_needed = len({item.release_tag for item in plan})
    print(
        f"planned_assets={len(plan)} tracked_document_paths={len(paths)} "
        f"releases_needed={releases_needed} base_tag={args.base_tag}"
    )
    if not plan:
        return 0
    if not args.upload and not args.patch_metadata and not args.remove_git_documents:
        for tag in sorted({item.release_tag for item in plan}, key=lambda t: (len(t), t))[:10]:
            count = sum(1 for item in plan if item.release_tag == tag)
            print(f"dry-run release {tag}: {count} asset(s)")
        if releases_needed > 10:
            print(f"dry-run: {releases_needed - 10} more release shard(s)")
        return 0
    token = token_value(args)
    if args.upload and not token:
        raise SystemExit("No GitHub token. Set GITHUB_TOKEN/GH_TOKEN or pass --token-from-git-credential.")
    if args.upload:
        releases = ensure_releases(plan, args, token)
        counts = upload_assets(plan, releases, args, token)
        print("upload_counts", dict(counts))
    if args.patch_metadata:
        print("case_patch_counts", dict(patch_case_files(plan, args)))
        print("document_index_patch_counts", dict(patch_document_index(plan, args)))
    if args.remove_git_documents:
        remove_tracked_documents()
        print("removed tracked archive/documents paths from index/worktree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
