#!/usr/bin/env python3
"""Shared document-byte storage helpers for SFSC case archives.

New document bytes are stored as content-addressed GitHub Release assets.
Existing records that point at git objects under archive/documents/ are still
accepted as complete so existing archive data remains readable.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ROOT_RESOLVED = ROOT.resolve()
DOCUMENT_OBJECT_DIR = "archive/documents"
DOCUMENT_OBJECT_ROOT = (ROOT_RESOLVED / DOCUMENT_OBJECT_DIR).resolve()
DOC_SCOPE_FIRST_PASS = "complaints-petitions-orders"
DOC_SCOPE_LEGACY_FIRST_PASS = "complaints-orders"
DOC_SCOPE_ALL = "all"
DOC_SCOPE_DOCKET_ONLY = "docket-only"
DEFER_REASON_FIRST_PASS = "first_pass_complaints_petitions_and_orders"
DEFER_REASON_DOCKET_ONLY = "docket_only_mass_scan_requested"
COMPLETE_DEFERRED_REASONS = {
    "",
    DEFER_REASON_FIRST_PASS,
    DEFER_REASON_DOCKET_ONLY,
    "legacy_pre_byte_capture",
}
SHA_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
GITHUB_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".html", ".htm", ".bin"}


def valid_sha(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if SHA_RE.fullmatch(text) else ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def valid_github_component(value: Any) -> str:
    text = str(value or "").strip()
    return text if GITHUB_COMPONENT_RE.fullmatch(text) else ""


def safe_stored_asset_name(value: Any, sha256: Any = None) -> str:
    text = str(value or "").strip()
    if not text or "/" in text or "\\" in text or "\x00" in text:
        return ""
    suffix = PurePosixPath(text).suffix.lower()
    if suffix not in SAFE_SUFFIXES:
        return ""
    stem = text[: -len(PurePosixPath(text).suffix)].lower()
    sha = valid_sha(sha256) if sha256 is not None else valid_sha(stem)
    if not sha or stem != sha:
        return ""
    return f"{sha}{suffix}"


def safe_asset_name(doc: dict[str, Any], default_ext: str = ".pdf") -> str:
    sha = valid_sha(doc.get("sha256"))
    raw = str(doc.get("asset_name") or "").strip()
    suffix = PurePosixPath(raw).suffix.lower() if raw and "/" not in raw and "\\" not in raw else ""
    if suffix not in SAFE_SUFFIXES:
        suffix = default_ext.lower() if default_ext.startswith(".") else f".{default_ext.lower()}"
    if suffix not in SAFE_SUFFIXES:
        suffix = ".bin"
    return f"{sha}{suffix}" if sha else ""


def asset_name_matches_sha(doc: dict[str, Any]) -> bool:
    sha = valid_sha(doc.get("sha256"))
    return bool(sha and safe_stored_asset_name(doc.get("asset_name"), sha))


def normalized_document_object_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or "\x00" in text:
        return ""
    pure = PurePosixPath(text)
    parts = pure.parts
    prefix = PurePosixPath(DOCUMENT_OBJECT_DIR).parts
    if (
        pure.is_absolute()
        or len(parts) != len(prefix) + 2
        or parts[: len(prefix)] != prefix
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return ""
    shard = parts[-2].lower()
    asset_name = safe_stored_asset_name(parts[-1])
    if not re.fullmatch(r"[0-9a-f]{2}", shard) or not asset_name:
        return ""
    sha = asset_name.split(".", 1)[0]
    if shard != sha[:2]:
        return ""
    candidate = (ROOT_RESOLVED / Path(*parts)).resolve()
    try:
        candidate.relative_to(ROOT_RESOLVED)
        candidate.relative_to(DOCUMENT_OBJECT_ROOT)
    except ValueError:
        return ""
    return f"{DOCUMENT_OBJECT_DIR}/{shard}/{asset_name}"


def document_object_path(doc: dict[str, Any]) -> str:
    sha = valid_sha(doc.get("sha256"))
    asset_name = safe_asset_name(doc)
    if not sha or not asset_name:
        return ""
    return f"{DOCUMENT_OBJECT_DIR}/{sha[:2]}/{asset_name}"


def raw_github_url(owner: str, repo: str, branch: str, object_path: str) -> str:
    owner = valid_github_component(owner)
    repo = valid_github_component(repo)
    object_path = normalized_document_object_path(object_path)
    if not owner or not repo or not branch or not object_path:
        return ""
    encoded_branch = urllib.parse.quote(branch, safe="")
    encoded_path = urllib.parse.quote(object_path, safe="/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_branch}/{encoded_path}"


def doc_has_git_object(doc: dict[str, Any]) -> bool:
    sha = valid_sha(doc.get("sha256"))
    if not sha:
        return False
    if doc.get("byte_path") or doc.get("capture_error"):
        return False
    if doc.get("bytes_len") in (None, "") or not doc.get("content_type"):
        return False
    object_path = normalized_document_object_path(doc.get("object_path"))
    if not object_path:
        return False
    path_asset_name = PurePosixPath(object_path).name
    if path_asset_name.split(".", 1)[0] != sha:
        return False
    asset_name = str(doc.get("asset_name") or "").strip()
    return not asset_name or safe_stored_asset_name(asset_name, sha) == path_asset_name


def doc_has_release_asset(doc: dict[str, Any]) -> bool:
    if doc.get("byte_path") or doc.get("capture_error"):
        return False
    return bool(
        valid_sha(doc.get("sha256"))
        and doc.get("release_tag")
        and doc.get("asset_name")
        and doc.get("bytes_len") not in (None, "")
        and doc.get("content_type")
        and asset_name_matches_sha(doc)
    )


def doc_byte_deferred(doc: dict[str, Any]) -> bool:
    reason = str(doc.get("byte_capture_deferred_reason") or "").strip()
    return bool(
        doc.get("is_available") is not False
        and doc.get("byte_capture_deferred") is True
        and reason in COMPLETE_DEFERRED_REASONS
        and not doc.get("byte_path")
        and not doc.get("capture_error")
    )


def document_description(doc: dict[str, Any]) -> str:
    for key in ("description", "title", "name"):
        value = str(doc.get(key) or "").strip()
        if value:
            return re.sub(r"\s+", " ", value.lower()).strip()
    return ""


def is_first_pass_document(doc: dict[str, Any]) -> bool:
    desc = document_description(doc)
    if not desc:
        return False
    pleading_prefix = (
        r"(?:(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
        r"amended|verified|unverified|civil|limited|unlimited)\s+)*"
    )
    if re.search(rf"^(?:{pleading_prefix})(?:cross[-\s]?complaint|complaint|petition)\b", desc) or re.search(
        rf"^[^,]{{0,100}},\s*(?:{pleading_prefix})(?:cross[-\s]?complaint|complaint|petition)\b", desc
    ):
        return True
    if re.search(r"\b(proposed|lodged|submitted)\s+order\b", desc):
        return False
    if re.search(r"\b(?:order|judgment|ruling|decision|decree)\b", desc):
        if re.search(
            r"^(?:\w+,\s*)?(?:"
            r"(?:joint\s+)?(?:stipulation\s+and\s+)?order\b|"
            r"order\s+(?:granting|denying|after|on|to|re:|regarding|appointing|approving|confirming|vacating|continuing|staying|dismissing|setting|shortening|extending|amending|signed|filed)|"
            r"order\s+to\s+show\s+cause|"
            r"case\s+management\s+order|"
            r"judgment|amended\s+judgment|default\s+judgment|entry\s+of\s+judgment|notice\s+of\s+entry\s+of\s+judgment|"
            r"ruling|decision|decree|statement\s+of\s+decision"
            r")\b",
            desc,
        ):
            return True
        return False
    if re.search(r"\b(motion|notice of motion|memorandum|declaration|opposition|reply|application|request)\b.{0,120}\b(?:summary judgment|judgment on the pleadings|order|writ|mandate)\b", desc):
        return False
    return bool(
        re.search(r"^(?:remittitur|opinion|mandate|remand|peremptory\s+writ|alternative\s+writ)\b", desc)
        or re.search(
            r"\b(?:appellate|appeal|court\s+of\s+appeal)\b.*\b(?:opinion|order|ruling|decision|writ|mandate|remand)\b",
            desc,
        )
    )


def normalize_doc_scope(scope: Any) -> str:
    text = str(scope or DOC_SCOPE_FIRST_PASS).strip()
    if text == DOC_SCOPE_LEGACY_FIRST_PASS:
        return DOC_SCOPE_FIRST_PASS
    return text


def should_capture_doc_for_scope(doc: dict[str, Any], scope: str) -> bool:
    scope = normalize_doc_scope(scope)
    if scope == DOC_SCOPE_ALL:
        return True
    if scope == DOC_SCOPE_DOCKET_ONLY:
        return False
    if scope == DOC_SCOPE_FIRST_PASS:
        return is_first_pass_document(doc)
    return False


def mark_doc_deferred(doc: dict[str, Any], scope: str = DOC_SCOPE_FIRST_PASS) -> None:
    doc["byte_capture_deferred"] = True
    doc["byte_capture_deferred_reason"] = (
        DEFER_REASON_DOCKET_ONLY if normalize_doc_scope(scope) == DOC_SCOPE_DOCKET_ONLY else DEFER_REASON_FIRST_PASS
    )
    doc["byte_capture_scope"] = normalize_doc_scope(scope)
    doc.pop("capture_error", None)
    doc.pop("byte_path", None)


def doc_has_archived_object(doc: dict[str, Any]) -> bool:
    return doc_has_git_object(doc) or doc_has_release_asset(doc)


def resolve_repo_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT_RESOLVED / path).resolve()


def local_doc_bytes_present(doc: dict[str, Any]) -> bool:
    sha = valid_sha(doc.get("sha256"))
    byte_path = resolve_repo_path(str(doc.get("byte_path") or ""))
    if not sha or not byte_path:
        return False
    try:
        byte_path.relative_to(ROOT_RESOLVED)
    except ValueError:
        return False
    try:
        if not byte_path.is_file():
            return False
        if doc.get("bytes_len") not in (None, "") and byte_path.stat().st_size != int(doc["bytes_len"]):
            return False
        return sha256_file(byte_path) == sha
    except Exception:
        return False
