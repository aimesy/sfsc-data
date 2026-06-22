#!/usr/bin/env python3
"""Materialize archived document bytes for OCR.

Case JSON records store content-addressed document metadata
(`sha256`, current `release_tag`/`asset_name` Release metadata, and
historical/repair `object_path`/`object_url` git-object metadata).
`scripts/ocr_documents.py` is ready to OCR bytes, but it only scans local
paths. This bridge copies or downloads missing archive objects into an ignored
cache, verifies each file against the recorded sha256, writes a manifest, and
can then invoke the OCR producer over that cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_DIRS = [ROOT / "archive" / "cases"]
DEFAULT_CACHE_DIR = ROOT / ".scanner" / "ocr-input"
DEFAULT_MANIFEST = ROOT / "data" / "ocr" / "document-byte-manifest.jsonl"
OCR_DIR = ROOT / "data" / "ocr"
CURATED_OCR_DIR = ROOT / "data" / "ocr-curated"
DEFAULT_ARCHIVE_REPO = os.environ.get("SFSC_ARCHIVE_REPO", "aimesy/sfsc-data")
DEFAULT_ARCHIVE_BRANCH = os.environ.get("SFSC_ARCHIVE_BRANCH", "master")
SHA_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
SAFE_OCR_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".html", ".htm"}


def repo_rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def parse_remote(remote: str) -> tuple[str, str]:
    remote = remote.strip()
    if remote.startswith("git@github.com:"):
        slug = remote.removeprefix("git@github.com:")
    else:
        m = re.search(r"github\.com[:/](?:[^/@]+@)?([^/]+/[^/.]+)(?:\.git)?/?$", remote)
        if not m:
            raise ValueError(f"cannot infer GitHub owner/repo from remote: {remote}")
        slug = m.group(1)
    slug = slug.removesuffix(".git").strip("/")
    owner, repo = slug.split("/", 1)
    return owner, repo


def infer_github_repo() -> tuple[str, str]:
    env_archive_repo = os.environ.get("SFSC_ARCHIVE_REPO", "")
    if "/" in env_archive_repo:
        owner, repo = env_archive_repo.split("/", 1)
        return owner, repo
    env_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in env_repo and env_repo != "aimesy/sfsc":
        owner, repo = env_repo.split("/", 1)
        return owner, repo
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_remote(proc.stdout)


def token_from_env() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def validate_object_path(object_path: str) -> str:
    text = str(object_path or "").strip().replace("\\", "/").strip("/")
    parts = text.split("/")
    if not text or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"invalid object_path: {object_path!r}")
    return text


def validate_download_url(url: str, owner: str, repo: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    owner_l = owner.lower()
    repo_l = repo.lower()
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.scheme != "https":
        raise ValueError(f"refusing non-HTTPS download URL: {url}")
    if host == "raw.githubusercontent.com":
        if len(parts) >= 3 and parts[0].lower() == owner_l and parts[1].lower() == repo_l:
            return url
    elif host == "github.com":
        if (
            len(parts) >= 5
            and parts[0].lower() == owner_l
            and parts[1].lower() == repo_l
            and parts[2] == "releases"
            and parts[3] == "download"
        ):
            return url
    elif host == "api.github.com":
        if (
            len(parts) >= 6
            and parts[0] == "repos"
            and parts[1].lower() == owner_l
            and parts[2].lower() == repo_l
            and parts[3] == "releases"
            and parts[4] == "assets"
        ):
            return url
    raise ValueError(f"refusing download URL outside {owner}/{repo}: {url}")


def request_bytes(url: str, token: str = "", accept: str = "application/octet-stream") -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "sfsc-ocr-materializer",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=120) as res:
        expected_len = res.headers.get("Content-Length")
        body = res.read()
    if expected_len:
        try:
            expected = int(expected_len)
        except ValueError:
            expected = -1
        if expected >= 0 and expected != len(body):
            raise IOError(f"short read from {url}: got {len(body)} bytes, expected {expected}")
    return body


def request_json(url: str, token: str = "") -> Any:
    body = request_bytes(url, token, "application/vnd.github+json")
    return json.loads(body.decode("utf-8"))


def release_assets(owner: str, repo: str, tag: str, token: str) -> dict[str, dict]:
    release_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}"
    release = request_json(release_url, token)
    assets_url = str(release["assets_url"]).split("{", 1)[0]
    assets: dict[str, dict] = {}
    page = 1
    while True:
        page_url = f"{assets_url}?per_page=100&page={page}"
        rows = request_json(page_url, token)
        if not rows:
            break
        for asset in rows:
            assets[asset["name"]] = asset
        page += 1
    return assets


def direct_download_url(owner: str, repo: str, tag: str, asset_name: str) -> str:
    return (
        f"https://github.com/{owner}/{repo}/releases/download/"
        f"{quote(tag, safe='')}/{quote(asset_name, safe='')}"
    )


def raw_object_url(owner: str, repo: str, branch: str, object_path: str) -> str:
    object_path = validate_object_path(object_path)
    return (
        f"https://raw.githubusercontent.com/{owner}/{repo}/"
        f"{quote(branch, safe='')}/{quote(object_path.strip('/'), safe='/')}"
    )


def valid_sha(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if SHA_RE.match(s) else ""


def load_sha_file(path: Path) -> list[str]:
    """Read sha256 filters from a text file."""
    text = path.read_text(encoding="utf-8")
    return re.findall(r"\b[0-9a-fA-F]{64}\b", text)


def safe_asset_suffix(asset_name: Any) -> str:
    name = str(asset_name or "").strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        return ""
    suffix = Path(name).suffix.lower()
    return suffix if suffix in SAFE_OCR_SUFFIXES else ""


def suffix_from_content_type(content_type: Any) -> str:
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return ""
    if "pdf" in ct:
        return ".pdf"
    if "tiff" in ct or ct.endswith("/tif"):
        return ".tif"
    if "png" in ct:
        return ".png"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "html" in ct:
        return ".html"
    return ""


def suffix_from_magic(body: bytes | None) -> str:
    if not body:
        return ""
    if body.startswith(b"%PDF"):
        return ".pdf"
    if body.startswith(b"\x89PNG"):
        return ".png"
    if body.startswith(b"\xff\xd8"):
        return ".jpg"
    if body.startswith(b"II*\x00") or body.startswith(b"MM\x00*"):
        return ".tif"
    prefix = body[:1024].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return ".html"
    return ""


def cache_suffix(rec: dict, body: bytes | None = None) -> str:
    return (
        safe_asset_suffix(rec.get("asset_name"))
        or suffix_from_magic(body)
        or suffix_from_content_type(rec.get("content_type"))
        or ".bin"
    )


def cache_path_for_record(cache_dir: Path, rec: dict, body: bytes | None = None) -> Path:
    path = (cache_dir / f"{rec['sha256']}{cache_suffix(rec, body)}").resolve()
    try:
        path.relative_to(cache_dir.resolve())
    except ValueError:
        raise ValueError(f"cache path escapes cache dir: {path}") from None
    return path


def derived_ocr_exists(sha: str) -> bool:
    return (OCR_DIR / f"{sha}.json").exists()


def curated_ocr_exists(sha: str) -> bool:
    return (CURATED_OCR_DIR / f"{sha}.json").exists()


def ocr_exists(sha: str) -> bool:
    return derived_ocr_exists(sha) or curated_ocr_exists(sha)


def load_case_documents(case_dirs: list[Path], include_ocrd: bool) -> dict[str, dict]:
    by_sha: dict[str, dict] = {}
    for case_dir in case_dirs:
        if not case_dir.exists():
            continue
        for path in sorted(case_dir.glob("*.json")):
            try:
                case = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"! skip {repo_rel(path)}: {exc}", file=sys.stderr)
                continue
            case_number = str(case.get("case_number") or path.stem)
            for doc in case.get("documents") or []:
                sha = valid_sha(doc.get("sha256"))
                if not sha:
                    continue
                if not include_ocrd and ocr_exists(sha):
                    continue
                object_path = str(doc.get("object_path") or "").strip()
                object_url = str(doc.get("object_url") or "").strip()
                storage_backend = str(doc.get("storage_backend") or "").strip()
                release_tag = str(doc.get("release_tag") or "").strip()
                asset_name = str(doc.get("asset_name") or f"{sha}.pdf").strip()
                if not (object_path or object_url or release_tag) or not asset_name:
                    continue
                rec = by_sha.setdefault(
                    sha,
                    {
                        "sha256": sha,
                        "storage_backend": storage_backend or ("git" if object_path or object_url else "release"),
                        "object_path": object_path,
                        "object_url": object_url,
                        "release_tag": release_tag,
                        "asset_name": asset_name,
                        "expected_bytes_len": doc.get("bytes_len"),
                        "content_type": doc.get("content_type"),
                        "source_cases": [],
                    },
                )
                if (
                    rec.get("object_path") != object_path
                    or rec.get("object_url") != object_url
                    or rec.get("release_tag") != release_tag
                    or rec.get("asset_name") != asset_name
                ):
                    rec.setdefault("alternate_assets", []).append({
                        "storage_backend": storage_backend or ("git" if object_path or object_url else "release"),
                        "object_path": object_path,
                        "object_url": object_url,
                        "release_tag": release_tag,
                        "asset_name": asset_name,
                        "case_number": case_number,
                    })
                rec["source_cases"].append({
                    "case_number": case_number,
                    "case_path": repo_rel(path),
                    "doc_id": doc.get("doc_id"),
                    "description": doc.get("description"),
                    "bytes_len": doc.get("bytes_len"),
                    "content_type": doc.get("content_type"),
                })
    return by_sha


def write_downloaded_asset(
    rec: dict,
    cache_dir: Path,
    owner: str,
    repo: str,
    branch: str,
    token: str,
    asset_cache: dict[str, dict[str, dict]],
) -> tuple[str, Path]:
    object_path = str(rec.get("object_path") or "").strip()
    object_url = str(rec.get("object_url") or "").strip()
    if object_path:
        object_path = validate_object_path(object_path)
        local_object = (ROOT / object_path).resolve()
        try:
            local_object.relative_to(ROOT.resolve())
        except ValueError:
            raise ValueError(f"object_path escapes repo root: {object_path}") from None
        if local_object.exists():
            body = local_object.read_bytes()
            source_url = repo_rel(local_object)
        else:
            source_url = object_url or raw_object_url(owner, repo, branch, object_path)
            source_url = validate_download_url(source_url, owner, repo)
            body = request_bytes(source_url, token)
    elif object_url:
        source_url = validate_download_url(object_url, owner, repo)
        body = request_bytes(source_url, token)
    else:
        tag = rec["release_tag"]
        asset_name = rec["asset_name"]
        direct_url = direct_download_url(owner, repo, tag, asset_name)
        direct_url = validate_download_url(direct_url, owner, repo)
        try:
            body = request_bytes(direct_url, token)
            source_url = direct_url
        except (HTTPError, URLError):
            if tag not in asset_cache:
                asset_cache[tag] = release_assets(owner, repo, tag, token)
            asset = asset_cache[tag].get(asset_name)
            if not asset:
                raise FileNotFoundError(f"{asset_name} not found in release {tag}")
            source_url = validate_download_url(str(asset["url"]), owner, repo)
            body = request_bytes(source_url, token, "application/octet-stream")

    out_path = cache_path_for_record(cache_dir, rec, body)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out_path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(body)
    got = sha256_file(tmp_path)
    if got != rec["sha256"]:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"sha mismatch for {asset_name}: got {got}, expected {rec['sha256']}")
    shutil.move(str(tmp_path), out_path)
    return source_url, out_path


def write_manifest(manifest_path: Path, records: list[dict]) -> None:
    write_text_atomic(
        manifest_path,
        "".join(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n" for rec in sorted(records, key=lambda r: r["sha256"])),
    )


def run_ocr(
    input_paths: list[Path],
    force: bool,
    allow_failures: bool,
    curated: bool,
    quality_tier: str,
    allow_overwrite_protected: bool,
    allow_low_information: bool,
) -> int:
    if not input_paths:
        print("No materialized OCR input files selected.")
        return 0
    cmd = [sys.executable, str(ROOT / "scripts" / "ocr_documents.py"), "--paths"]
    cmd.extend(str(path) for path in input_paths)
    if force:
        cmd.append("--force")
    if curated:
        cmd.append("--curated")
    if quality_tier:
        cmd.extend(["--quality-tier", quality_tier])
    if allow_overwrite_protected:
        cmd.append("--allow-overwrite-protected")
    if allow_low_information:
        cmd.append("--allow-low-information")
    if allow_failures:
        cmd.append("--allow-failures")
    return subprocess.run(cmd, cwd=ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case-dir", action="append", default=None,
                    help="Case JSON directory. May be repeated. Default: archive/cases.")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="Ignored local byte cache. Default: .scanner/ocr-input.")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                    help="JSONL manifest path. Default: data/ocr/document-byte-manifest.jsonl.")
    ap.add_argument("--repo", default=DEFAULT_ARCHIVE_REPO,
                    help="GitHub owner/repo for archive bytes. Default: SFSC_ARCHIVE_REPO or aimesy/sfsc-data.")
    ap.add_argument("--branch", default=DEFAULT_ARCHIVE_BRANCH,
                    help="Branch for raw object URLs. Default: SFSC_ARCHIVE_BRANCH or master.")
    ap.add_argument("--token-env", default="",
                    help="Environment variable containing a GitHub token. Default: GITHUB_TOKEN or GH_TOKEN.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Download at most N missing assets.")
    ap.add_argument("--sha", action="append", default=None,
                    help="Only materialize a specific sha256. May be repeated.")
    ap.add_argument("--sha-file", action="append", default=None,
                    help="Read sha256 filters from a text file. May be repeated.")
    ap.add_argument("--include-ocrd", action="store_true",
                    help="Include assets that already have derived or curated OCR.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report targets without downloading or writing the manifest.")
    ap.add_argument("--run-ocr", action="store_true",
                    help="Run scripts/ocr_documents.py over the cache after materializing.")
    ap.add_argument("--force-ocr", action="store_true",
                    help="Pass --force to scripts/ocr_documents.py when --run-ocr is used.")
    ap.add_argument("--curated-ocr", action="store_true",
                    help="When --run-ocr is used, write protected records to data/ocr-curated.")
    ap.add_argument("--ocr-quality-tier", default="",
                    choices=("", "auto", "good-auto", "reviewed", "manual"),
                    help="Quality tier passed to scripts/ocr_documents.py.")
    ap.add_argument("--allow-overwrite-protected-ocr", action="store_true",
                    help="Allow a curated OCR batch to replace existing protected data/ocr-curated records.")
    ap.add_argument("--allow-low-information-ocr", action="store_true",
                    help="Allow OCR batches to write records with almost no visible text.")
    ap.add_argument("--allow-failures", action="store_true",
                    help="Treat per-asset download/OCR failures as logged warnings and exit 0.")
    args = ap.parse_args(argv)

    case_dirs = [Path(p).resolve() for p in (args.case_dir or DEFAULT_CASE_DIRS)]
    cache_dir = Path(args.cache_dir).resolve()
    manifest_path = Path(args.manifest).resolve()
    wanted = {valid_sha(s) for s in (args.sha or [])}
    for sha_file in args.sha_file or []:
        sha_path = Path(sha_file).resolve()
        try:
            wanted.update(valid_sha(s) for s in load_sha_file(sha_path))
        except FileNotFoundError:
            print(f"! sha filter file not found: {sha_file}", file=sys.stderr)
            return 2
    wanted.discard("")

    if (
        args.run_ocr
        and args.limit <= 0
        and not wanted
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("SFSC_ALLOW_FULL_CORPUS_OCR") != "1"
    ):
        print(
            "ERROR: refusing unbounded full-corpus OCR in GitHub Actions. "
            "Pass --limit, --sha/--sha-file, or set SFSC_ALLOW_FULL_CORPUS_OCR=1.",
            file=sys.stderr,
        )
        return 2

    owner, repo = args.repo.split("/", 1) if args.repo else infer_github_repo()
    token = os.environ.get(args.token_env, "") if args.token_env else token_from_env()

    docs = load_case_documents(case_dirs, args.include_ocrd)
    if wanted:
        docs = {sha: rec for sha, rec in docs.items() if sha in wanted}

    cache_dir.mkdir(parents=True, exist_ok=True)
    targets: list[dict] = []
    for sha, rec in docs.items():
        out_path = cache_path_for_record(cache_dir, rec)
        rec = dict(rec)
        rec["local_path"] = repo_rel(out_path)
        rec["ocr_exists"] = ocr_exists(sha)
        rec["derived_ocr_exists"] = derived_ocr_exists(sha)
        rec["curated_ocr_exists"] = curated_ocr_exists(sha)
        rec["protected_ocr_exists"] = rec["curated_ocr_exists"]
        rec["cache_exists"] = out_path.exists()
        targets.append(rec)

    missing = [r for r in targets if not r["cache_exists"]]
    cached = [r for r in targets if r["cache_exists"]]
    if args.limit > 0:
        selected_missing = missing[:args.limit]
        cached_limit = max(0, args.limit - len(selected_missing))
        selected_cached = cached[:cached_limit]
    else:
        selected_missing = missing
        selected_cached = cached
    selected_for_ocr = {r["sha256"] for r in selected_missing}
    selected_for_ocr.update(r["sha256"] for r in selected_cached)

    print(f"GitHub repo: {owner}/{repo}")
    print(f"Case dirs: {', '.join(repo_rel(p) for p in case_dirs if p.exists())}")
    if wanted:
        print(f"SHA filter: {len(wanted)} requested")
    print(f"Known archive-backed docs needing OCR: {len(targets)}")
    print(f"Already in cache: {len(targets) - len(missing)}")
    print(f"Missing cache bytes selected for download: {len(selected_missing)}")
    print(f"Materialized files selected for OCR: {len(selected_for_ocr)}")
    protected_selected = sum(
        1 for r in targets
        if r["sha256"] in selected_for_ocr and r.get("protected_ocr_exists")
    )
    if protected_selected:
        print(f"Protected curated records in selected set: {protected_selected}")
    if args.dry_run:
        for rec in selected_missing[:20]:
            loc = rec.get("object_path") or rec.get("object_url") or rec.get("release_tag")
            print(f"  DRY {rec['sha256'][:12]} {loc} {rec['asset_name']}")
        if len(selected_missing) > 20:
            print(f"  ... {len(selected_missing) - 20} more")
        return 0

    asset_cache: dict[str, dict[str, dict]] = {}
    status_by_sha: dict[str, dict] = {}
    downloaded = 0
    failed = 0
    for rec in selected_missing:
        sha = rec["sha256"]
        try:
            source_url, out_path = write_downloaded_asset(rec, cache_dir, owner, repo, args.branch, token, asset_cache)
            byte_size = out_path.stat().st_size
            rec.update({
                "materialized_at": now_iso(),
                "source_url": source_url,
                "local_path": repo_rel(out_path),
                "byte_size": byte_size,
                "cache_exists": True,
                "status": "downloaded",
            })
            downloaded += 1
            print(f"  GET {sha[:12]} {byte_size:>9} {rec['asset_name']}")
        except Exception as exc:
            rec.update({
                "materialized_at": now_iso(),
                "status": "error",
                "error": str(exc),
            })
            failed += 1
            print(f"  !   {sha[:12]} {exc}", file=sys.stderr)
        status_by_sha[sha] = rec

    manifest_records: list[dict] = []
    ocr_input_paths: list[Path] = []
    skipped_protected_for_ocr = 0
    for rec in targets:
        protected_skip = (
            args.run_ocr
            and args.curated_ocr
            and rec["sha256"] in selected_for_ocr
            and rec.get("protected_ocr_exists")
            and not args.allow_overwrite_protected_ocr
        )
        updated = status_by_sha.get(rec["sha256"])
        if updated:
            manifest_records.append(updated)
            if protected_skip:
                skipped_protected_for_ocr += 1
            elif updated.get("status") in {"downloaded", "cached"}:
                out_path = ROOT / updated["local_path"]
                if out_path.exists():
                    ocr_input_paths.append(out_path)
            continue
        out_path = ROOT / rec["local_path"]
        if out_path.exists():
            rec["byte_size"] = out_path.stat().st_size
            rec["status"] = "cached"
            if protected_skip:
                skipped_protected_for_ocr += 1
            elif rec["sha256"] in selected_for_ocr:
                ocr_input_paths.append(out_path)
        else:
            rec["status"] = "not_materialized"
        manifest_records.append(rec)
    write_manifest(manifest_path, manifest_records)
    print(f"Wrote {repo_rel(manifest_path)}")
    print(f"Downloaded {downloaded}, failed {failed}.")
    if skipped_protected_for_ocr:
        print(f"Skipped protected curated OCR inputs: {skipped_protected_for_ocr}")

    if args.run_ocr:
        ocr_status = run_ocr(
            ocr_input_paths,
            args.force_ocr,
            args.allow_failures,
            args.curated_ocr,
            args.ocr_quality_tier or ("good-auto" if args.curated_ocr else "auto"),
            args.allow_overwrite_protected_ocr,
            args.allow_low_information_ocr,
        )
        if ocr_status:
            return ocr_status
        if args.allow_failures and failed:
            print(
                "WARNING: OCR materialization had per-document download failures; "
                "see data/ocr/document-byte-manifest.jsonl.",
                file=sys.stderr,
            )
            return 0
        return ocr_status if ocr_status else (1 if failed else 0)
    if args.allow_failures and failed:
        print(
            "WARNING: OCR materialization had per-document download failures; "
            "see data/ocr/document-byte-manifest.jsonl.",
            file=sys.stderr,
        )
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
