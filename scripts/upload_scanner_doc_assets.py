#!/usr/bin/env python3
"""Attach scanner-captured document bytes to archive storage and patch case JSON.

Input case JSONs should come from local_case_scanner.mjs after document-byte
capture. Existing git-object and release metadata remains readable. New
document-byte uploads default to GitHub Releases so capture cannot silently
bloat the repository. Git-object writes under archive/documents/ require an
explicit operator flag. By default, the active first pass archives complaints/
petitions and court orders and marks other available documents deferred.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

import document_storage as storage


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE_REPO = os.environ.get("SFSC_ARCHIVE_REPO", "aimesy/sfsc-data")
DEFAULT_ARCHIVE_BRANCH = os.environ.get("SFSC_ARCHIVE_BRANCH", "master")
DEFAULT_MAX_ASSETS_PER_RELEASE = 1000
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
RETRYABLE_EXC = (
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    TimeoutError,
    socket.timeout,
    urllib.error.URLError,
)


@dataclass(frozen=True)
class GitDocumentBlob:
    path: str
    bytes_path: Path


@dataclass(frozen=True)
class ProcessResult:
    uploaded: int
    already: int
    unavailable: int
    failed: int
    git_blobs: tuple[GitDocumentBlob, ...] = ()
    data: dict | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def token_from_git_credential() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    fields = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k] = v
    return fields.get("password", "")


def token(args: argparse.Namespace) -> str:
    if args.token_env and os.environ.get(args.token_env):
        return os.environ[args.token_env]
    if args.token_from_git_credential:
        return token_from_git_credential()
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    return ""


def retry_delay(attempt: int) -> float:
    return min(30.0, 1.5 * (2 ** attempt))


def retryable_http_error(code: int, body: str = "") -> bool:
    if code in RETRYABLE_HTTP:
        return True
    return code == 403 and "rate limit" in str(body or "").lower()


def http_retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else ""
    if str(retry_after).isdigit():
        return max(1.0, float(retry_after))
    reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else ""
    if str(reset).isdigit():
        return max(1.0, min(3600.0, float(reset) - time.time() + 2.0))
    return retry_delay(attempt)


def request_json(url: str, token_value: str, method: str = "GET", body: dict | None = None, attempts: int = 5):
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token_value}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sfsc-doc-asset-uploader",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            if not retryable_http_error(exc.code, body_text) or attempt == attempts - 1:
                raise
            delay = http_retry_delay(exc, attempt)
            print(f"retry github json {method} {url}: HTTP {exc.code}; sleeping {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
        except RETRYABLE_EXC as exc:
            if attempt == attempts - 1:
                raise
            delay = retry_delay(attempt)
            print(f"retry github json {method} {url}: {exc}; sleeping {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"unreachable retry state for {url}")


def request_bytes(
    url: str,
    token_value: str,
    content_type: str,
    body: bytes,
    method: str = "POST",
) -> tuple[int, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token_value}",
        "Content-Type": content_type or "application/octet-stream",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sfsc-doc-asset-uploader",
    }
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=180) as res:
                return res.status, res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            if exc.code == 422 or not retryable_http_error(exc.code, text) or attempt == 4:
                return exc.code, text
            delay = http_retry_delay(exc, attempt)
            print(f"retry github asset {url}: HTTP {exc.code}; sleeping {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
        except RETRYABLE_EXC as exc:
            if attempt == 4:
                raise
            delay = retry_delay(attempt)
            print(f"retry github asset {url}: {exc}; sleeping {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"unreachable retry state for {url}")


def gh_quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def repo_api_url(owner: str, repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/{path.lstrip('/')}"


def _head_sha_from_ref(ref: dict) -> str:
    sha = str((ref.get("object") or {}).get("sha") or "").strip()
    if len(sha) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in sha):
        return ""
    return sha.lower()


def _create_text_blob(owner: str, repo: str, token_value: str, content: str) -> str:
    payload = request_json(
        repo_api_url(owner, repo, "git/blobs"),
        token_value,
        "POST",
        {
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        },
    )
    sha = str(payload.get("sha") or "").strip()
    if not sha:
        raise RuntimeError(f"GitHub blob create returned no sha for {owner}/{repo}")
    return sha


def _create_initial_archive_commit(owner: str, repo: str, branch: str, token_value: str) -> str:
    files = {
        "README.md": "# SFSC data\n\nArchive/data payload for the aimesy/sfsc viewer.\n",
        "archive/cases-index.ndjson": "",
        "archive/document-index.ndjson": "",
        "data/README.md": "Derived tables are generated from archive case JSON.\n",
    }
    tree = request_json(
        repo_api_url(owner, repo, "git/trees"),
        token_value,
        "POST",
        {
            "tree": [
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": _create_text_blob(owner, repo, token_value, content),
                }
                for path, content in sorted(files.items())
            ]
        },
    )
    tree_sha = str(tree.get("sha") or "").strip()
    if not tree_sha:
        raise RuntimeError(f"GitHub tree create returned no sha for {owner}/{repo}")
    commit = request_json(
        repo_api_url(owner, repo, "git/commits"),
        token_value,
        "POST",
        {
            "message": f"Initialize archive data branch {branch}",
            "tree": tree_sha,
            "parents": [],
        },
    )
    commit_sha = str(commit.get("sha") or "").strip()
    if not commit_sha:
        raise RuntimeError(f"GitHub commit create returned no sha for {owner}/{repo}")
    return commit_sha


def ensure_archive_branch(owner: str, repo: str, branch: str, token_value: str) -> str:
    """Return the destination branch head, creating master for an empty data repo."""
    branch = str(branch or "").strip()
    if not branch:
        raise RuntimeError("archive branch is empty")
    ref_url = repo_api_url(owner, repo, f"git/ref/heads/{gh_quote(branch)}")
    try:
        sha = _head_sha_from_ref(request_json(ref_url, token_value))
        if sha:
            return sha
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 409):
            raise

    # If the repository already has a default branch, mirror that head instead
    # of creating an empty archive branch that would hide existing data.
    try:
        repo_info = request_json(repo_api_url(owner, repo, ""), token_value)
        default_branch = str(repo_info.get("default_branch") or "").strip()
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 409):
            raise
        default_branch = ""
    if default_branch and default_branch != branch:
        try:
            default_ref = request_json(repo_api_url(owner, repo, f"git/ref/heads/{gh_quote(default_branch)}"), token_value)
            default_sha = _head_sha_from_ref(default_ref)
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 409):
                raise
            default_sha = ""
        if default_sha:
            try:
                request_json(
                    repo_api_url(owner, repo, "git/refs"),
                    token_value,
                    "POST",
                    {"ref": f"refs/heads/{branch}", "sha": default_sha},
                )
                print(f"created {owner}/{repo}@{branch} from existing default branch", flush=True)
            except urllib.error.HTTPError as exc:
                if exc.code != 422:
                    raise
            sha = _head_sha_from_ref(request_json(ref_url, token_value))
            if sha:
                return sha

    if branch != "master":
        raise RuntimeError(f"archive branch {branch!r} does not exist; refusing to bootstrap a non-master branch")
    commit_sha = _create_initial_archive_commit(owner, repo, branch, token_value)
    try:
        request_json(
            repo_api_url(owner, repo, "git/refs"),
            token_value,
            "POST",
            {"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )
        print(f"initialized empty archive data branch {owner}/{repo}@{branch}", flush=True)
        return commit_sha
    except urllib.error.HTTPError as exc:
        if exc.code != 422:
            raise
    sha = _head_sha_from_ref(request_json(ref_url, token_value))
    if sha:
        return sha
    raise RuntimeError(f"failed to initialize archive branch {owner}/{repo}@{branch}")


def get_or_create_release_info(owner: str, repo: str, tag: str, token_value: str, branch: str) -> dict:
    tags_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{gh_quote(tag)}"
    try:
        return request_json(tags_url, token_value)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    create_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
    body = {
        "tag_name": tag,
        "name": tag,
        "target_commitish": branch,
        "draft": False,
        "prerelease": False,
        "body": "Content-addressed document bytes (assets named <sha256>.<ext>).",
    }
    try:
        return request_json(create_url, token_value, "POST", body)
    except urllib.error.HTTPError as exc:
        if exc.code != 422:
            raise
    return request_json(tags_url, token_value)


def get_or_create_release(owner: str, repo: str, tag: str, token_value: str, branch: str) -> int:
    return int(get_or_create_release_info(owner, repo, tag, token_value, branch)["id"])


def release_shard_tag(base_tag: str, index: int) -> str:
    return base_tag if index <= 1 else f"{base_tag}-{index:03d}"


def release_asset_count(owner: str, repo: str, release_id: int, token_value: str) -> int:
    total = 0
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}/assets"
            f"?per_page=100&page={page}"
        )
        assets = request_json(url, token_value)
        if not isinstance(assets, list):
            raise RuntimeError(f"unexpected release assets response for release {release_id}")
        total += len(assets)
        if len(assets) < 100:
            return total
        page += 1


class ReleaseShardAllocator:
    """Allocate document bytes to daily release shards.

    The intended shard is `docs-YYYY-MM-DD`, spilling only to
    `docs-YYYY-MM-DD-002`, `-003`, etc. when the current release is at the
    configured asset cap. This prevents accidental one-release-per-case output.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        base_tag: str,
        token_value: str,
        branch: str,
        max_assets_per_release: int = DEFAULT_MAX_ASSETS_PER_RELEASE,
    ):
        self.owner = owner
        self.repo = repo
        self.base_tag = base_tag
        self.token_value = token_value
        self.branch = branch
        self.max_assets_per_release = max(0, int(max_assets_per_release or 0))
        self.index = 1
        self.release_tag = ""
        self.release_id = 0
        self.asset_count = 0

    def _load_current(self) -> None:
        tag = release_shard_tag(self.base_tag, self.index)
        info = get_or_create_release_info(self.owner, self.repo, tag, self.token_value, self.branch)
        release_id = int(info["id"])
        self.release_tag = tag
        self.release_id = release_id
        self.asset_count = release_asset_count(self.owner, self.repo, release_id, self.token_value)

    def reserve(self, needed_slots: int) -> tuple[str, int]:
        needed = max(0, int(needed_slots or 0))
        while True:
            if not self.release_id:
                self._load_current()
            if (
                self.max_assets_per_release <= 0
                or needed <= 0
                or self.asset_count + needed <= self.max_assets_per_release
                or (self.asset_count == 0 and needed > self.max_assets_per_release)
            ):
                self.asset_count += needed
                return self.release_tag, self.release_id
            self.index += 1
            self.release_tag = ""
            self.release_id = 0
            self.asset_count = 0


def upload_asset(
    owner: str,
    repo: str,
    release_id: int,
    token_value: str,
    asset_name: str,
    content_type: str,
    bytes_path: Path,
) -> bool:
    url = (
        f"https://uploads.github.com/repos/{owner}/{repo}/releases/{release_id}/assets"
        f"?name={gh_quote(asset_name)}"
    )
    status, text = request_bytes(url, token_value, content_type, bytes_path.read_bytes())
    if 200 <= status < 300:
        return True
    if status == 422 and "already_exists" in text:
        return True
    raise RuntimeError(f"upload {asset_name} failed: HTTP {status} {text[:200]}")


def write_json_atomic(path: Path, data: dict) -> None:
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


def materialize_git_blobs(blobs: tuple[GitDocumentBlob, ...]) -> int:
    written = 0
    for blob in blobs:
        dest = (ROOT / blob.path).resolve()
        try:
            dest.relative_to(ROOT.resolve())
        except ValueError:
            raise ValueError(f"git object path escapes repo root: {blob.path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and sha256_file(dest) == sha256_file(blob.bytes_path):
            continue
        with NamedTemporaryFile("wb", dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            with blob.bytes_path.open("rb") as src:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        if sha256_file(tmp) != sha256_file(blob.bytes_path):
            tmp.unlink(missing_ok=True)
            raise ValueError(f"temporary git object copy failed verification: {blob.path}")
        tmp.replace(dest)
        written += 1
    return written


def doc_has_archived_object(doc: dict) -> bool:
    return storage.doc_has_archived_object(doc)


def doc_needs_upload(doc: dict) -> bool:
    return (
        isinstance(doc, dict)
        and doc.get("is_available") is not False
        and not doc_has_archived_object(doc)
        and bool(doc.get("sha256"))
        and bool(doc.get("asset_name"))
        and bool(doc.get("byte_path"))
    )


def count_case_uploads(path: Path, doc_scope: str | None = None) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    docs = data.get("documents") if isinstance(data.get("documents"), list) else []
    scope = storage.normalize_doc_scope(doc_scope) if doc_scope is not None else None
    return sum(
        1
        for doc in docs
        if doc_needs_upload(doc)
        and (scope is None or storage.should_capture_doc_for_scope(doc, scope))
    )


def _verified_local_byte_path(path: Path, doc: dict) -> tuple[str, str, Path]:
    sha = storage.valid_sha(doc.get("sha256"))
    asset_name = storage.safe_asset_name(doc)
    byte_rel = str(doc.get("byte_path") or "")
    if not sha or not asset_name or not byte_rel:
        raise RuntimeError(f"{path.name}: document missing local byte metadata")
    byte_path = (ROOT / byte_rel).resolve() if not Path(byte_rel).is_absolute() else Path(byte_rel).resolve()
    # Containment guard: byte_path comes from case JSON; never read/upload a
    # file outside the repo root via a crafted "../.." byte_path.
    try:
        byte_path.relative_to(ROOT.resolve())
    except ValueError:
        raise ValueError(f"{path.name}: byte_path escapes repo root: {byte_rel}")
    if not byte_path.exists():
        raise FileNotFoundError(f"{path.name}: missing byte file {byte_path}")
    got = sha256_file(byte_path)
    if got != sha:
        raise ValueError(f"{path.name}: sha mismatch for {asset_name}: got {got}, expected {sha}")
    return sha, asset_name, byte_path


def _patch_git_object(doc: dict, args: argparse.Namespace, asset_name: str, object_path: str) -> None:
    doc["storage_backend"] = "git"
    doc["asset_name"] = asset_name
    doc["object_path"] = object_path
    doc["object_url"] = storage.raw_github_url(args.owner, args.repo_name, args.branch, object_path)
    doc.pop("release_tag", None)
    doc.pop("byte_path", None)
    doc.pop("capture_error", None)


def process_case_result(path: Path, args: argparse.Namespace, release_id: int = 0, token_value: str = "") -> ProcessResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    docs = data.get("documents") if isinstance(data.get("documents"), list) else []
    release_needed = 0
    release_seen = 0
    uploaded = already = unavailable = failed = 0
    git_blobs: list[GitDocumentBlob] = []
    changed = False
    backend = str(getattr(args, "storage_backend", "git") or "git").lower()
    write_case_json = bool(getattr(args, "write_case_json", True))
    doc_scope = storage.normalize_doc_scope(getattr(args, "doc_scope", storage.DOC_SCOPE_FIRST_PASS))
    if backend == "release":
        for doc in docs:
            if (
                isinstance(doc, dict)
                and doc_needs_upload(doc)
                and storage.should_capture_doc_for_scope(doc, doc_scope)
            ):
                release_needed += 1
    data["document_byte_capture_scope"] = doc_scope
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("is_available") is False:
            unavailable += 1
            doc.pop("byte_path", None)
            doc.pop("capture_error", None)
            changed = True
            continue
        if storage.doc_byte_deferred(doc):
            already += 1
            continue
        if doc_has_archived_object(doc):
            already += 1
            continue
        if not storage.should_capture_doc_for_scope(doc, doc_scope):
            storage.mark_doc_deferred(doc, doc_scope)
            already += 1
            changed = True
            continue
        sha, asset_name, byte_path = _verified_local_byte_path(path, doc)
        if args.dry_run:
            # Local verification done above; do NOT touch the network or mutate
            # the doc in dry-run.
            uploaded += 1
            continue
        if backend == "release":
            release_seen += 1
            try:
                size = byte_path.stat().st_size
            except OSError:
                size = 0
            if release_seen == 1 or release_seen % 10 == 0 or size >= 32 * 1024 * 1024:
                print(
                    f"release upload {path.name}: {release_seen}/{release_needed} "
                    f"{asset_name} ({size} bytes)",
                    file=sys.stderr,
                    flush=True,
                )
            upload_asset(
                args.owner,
                args.repo_name,
                release_id,
                token_value,
                asset_name,
                str(doc.get("content_type") or "application/octet-stream"),
                byte_path,
            )
            doc["release_tag"] = args.release_tag
            doc["asset_name"] = asset_name
            doc.pop("storage_backend", None)
            doc.pop("object_path", None)
            doc.pop("object_url", None)
            doc.pop("byte_path", None)
            doc.pop("capture_error", None)
        elif backend == "git":
            if not getattr(args, "allow_git_document_writes", False):
                raise RuntimeError(
                    f"{path.name}: refusing to write archive/documents git objects by default; "
                    "configure a non-git document backend or pass --allow-git-document-writes "
                    "for an intentional repair"
                )
            object_path = storage.document_object_path({"sha256": sha, "asset_name": asset_name})
            _patch_git_object(doc, args, asset_name, object_path)
            git_blobs.append(GitDocumentBlob(object_path, byte_path))
        else:
            raise ValueError(f"unknown storage backend: {backend}")
        uploaded += 1
        changed = True
    data["documents_bytes_count"] = sum(1 for d in docs if isinstance(d, dict) and d.get("sha256"))
    data["documents_unavailable_count"] = sum(1 for d in docs if isinstance(d, dict) and d.get("is_available") is False)
    data["documents_deferred_count"] = sum(1 for d in docs if isinstance(d, dict) and storage.doc_byte_deferred(d))
    data["document_bytes_captured"] = all(
        not isinstance(d, dict)
        or doc_has_archived_object(d)
        or storage.doc_byte_deferred(d)
        or d.get("is_available") is False
        for d in docs
    )
    if changed and not args.dry_run and write_case_json:
        write_json_atomic(path, data)
    deduped = {blob.path: blob for blob in git_blobs}
    return ProcessResult(uploaded, already, unavailable, failed, tuple(deduped.values()), data)


def process_case(path: Path, args: argparse.Namespace, release_id: int = 0, token_value: str = "") -> tuple[int, int, int, int]:
    result = process_case_result(path, args, release_id, token_value)
    return result.uploaded, result.already, result.unavailable, result.failed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", type=Path, default=ROOT / ".scanner" / "cases")
    parser.add_argument("--repo", default=DEFAULT_ARCHIVE_REPO)
    parser.add_argument("--branch", default=DEFAULT_ARCHIVE_BRANCH)
    parser.add_argument("--storage-backend", choices=("git", "release"), default="release")
    parser.add_argument("--doc-scope", choices=(storage.DOC_SCOPE_FIRST_PASS, storage.DOC_SCOPE_LEGACY_FIRST_PASS, storage.DOC_SCOPE_ALL, storage.DOC_SCOPE_DOCKET_ONLY),
                        default=storage.DOC_SCOPE_FIRST_PASS,
                        help="Document bytes to archive now. Default: complaints/petitions/orders first pass.")
    parser.add_argument("--allow-legacy-release-writes", action="store_true",
                        help="Deprecated compatibility flag; release storage is now the default.")
    parser.add_argument("--allow-git-document-writes", action="store_true",
                        help="Allow writing new archive/documents git objects. Default blocks new document-byte bloat.")
    parser.add_argument("--release-tag", default=f"docs-{date.today().isoformat()}",
                        help="Release tag base for current GitHub Release asset storage.")
    parser.add_argument("--max-assets-per-release", type=int, default=DEFAULT_MAX_ASSETS_PER_RELEASE,
                        help="Maximum assets per Release shard.")
    parser.add_argument("--token-env", default="")
    parser.add_argument("--token-from-git-credential", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if "/" not in args.repo:
        raise SystemExit("--repo must be owner/repo")
    args.owner, args.repo_name = args.repo.split("/", 1)
    args.cases_dir = args.cases_dir.resolve()
    args.doc_scope = storage.normalize_doc_scope(args.doc_scope)
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    token_value = token(args)
    if not token_value and not args.dry_run and args.storage_backend == "release":
        raise SystemExit("No GitHub token. Set GITHUB_TOKEN/GH_TOKEN or pass --token-from-git-credential.")
    allocator = None
    if not args.dry_run and args.storage_backend == "release":
        ensure_archive_branch(args.owner, args.repo_name, args.branch, token_value)
        allocator = ReleaseShardAllocator(
            args.owner,
            args.repo_name,
            args.release_tag,
            token_value,
            args.branch,
            args.max_assets_per_release,
        )
    files = [p for p in sorted(args.cases_dir.glob("*.json")) if not p.name.endswith(".error.json")]
    if args.limit:
        files = files[: args.limit]
    totals = {"cases": 0, "uploaded": 0, "already": 0, "unavailable": 0, "failed": 0, "errors": 0}
    for p in files:
        try:
            release_id = 0
            case_tag = args.release_tag
            needed_uploads = count_case_uploads(p, args.doc_scope)
            if needed_uploads and allocator:
                case_tag, release_id = allocator.reserve(needed_uploads)
            upload_args = argparse.Namespace(**vars(args))
            upload_args.release_tag = case_tag
            result = process_case_result(p, upload_args, release_id, token_value)
            materialized = 0
            if args.storage_backend == "git" and not args.dry_run:
                materialized = materialize_git_blobs(result.git_blobs)
            uploaded, already, unavailable, failed = (
                result.uploaded,
                result.already,
                result.unavailable,
                result.failed,
            )
            totals["cases"] += 1
            totals["uploaded"] += uploaded
            totals["already"] += already
            totals["unavailable"] += unavailable
            totals["failed"] += failed
            if uploaded or already or unavailable or failed:
                staged = len(result.git_blobs)
                print(
                    f"{p.name}: uploaded={uploaded} already={already} unavailable={unavailable} "
                    f"failed={failed} git_blobs={staged} materialized={materialized}"
                )
        except Exception as exc:
            totals["errors"] += 1
            print(f"ERROR {p.name}: {exc}", file=sys.stderr)
            break
        time.sleep(0.1)
    for k, v in totals.items():
        print(f"{k}: {v}")
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
