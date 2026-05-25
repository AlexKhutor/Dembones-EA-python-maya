from __future__ import annotations

import re
from dataclasses import dataclass


_INVALID_FILE_STEM_PATTERN = re.compile(r'[<>:"/\\|?*]+')
_INVALID_NODE_TOKEN_PATTERN = re.compile(r"[^0-9A-Za-z_]+")
_MULTI_UNDERSCORE_PATTERN = re.compile(r"_+")
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class ResolvedExportNaming:
    source_name: str
    base_name: str
    file_stem: str
    node_prefix: str


def short_name(value: str) -> str:
    return str(value or "").split("|")[-1]


def strip_namespace(value: str) -> str:
    return str(value or "").split(":")[-1]


def clean_export_name_from_dag(value: str) -> str:
    return strip_namespace(short_name(value))


def sanitize_file_stem(value: str) -> str:
    text = str(value or "").strip()
    text = _INVALID_FILE_STEM_PATTERN.sub("_", text)
    text = _WHITESPACE_PATTERN.sub("_", text)
    text = _MULTI_UNDERSCORE_PATTERN.sub("_", text)
    return text.strip("._")


def sanitize_node_token(value: str) -> str:
    text = clean_export_name_from_dag(value).strip()
    text = _INVALID_NODE_TOKEN_PATTERN.sub("_", text)
    text = _MULTI_UNDERSCORE_PATTERN.sub("_", text)
    text = text.strip("_")
    if not text:
        text = "node"
    if text[0].isdigit():
        text = "_" + text
    return text


def resolve_export_base_name(requested_fbx_name: str, fallback_source_name: str) -> str:
    requested = sanitize_file_stem(requested_fbx_name)
    if requested:
        return requested
    fallback = sanitize_file_stem(clean_export_name_from_dag(fallback_source_name))
    if fallback:
        return fallback
    return "export"


def build_prefixed_file_stem(clip_prefix: str, base_name: str) -> str:
    combined = "{0}{1}".format(str(clip_prefix or ""), str(base_name or ""))
    sanitized = sanitize_file_stem(combined)
    if sanitized:
        return sanitized
    return "export"


def build_prefixed_node_name(base_name: str, original_short_name: str) -> str:
    prefix = sanitize_node_token(base_name)
    suffix = sanitize_node_token(original_short_name)
    return "{0}_{1}".format(prefix, suffix)


def resolve_export_naming(
    requested_fbx_name: str,
    clip_prefix: str,
    fallback_source_name: str,
) -> ResolvedExportNaming:
    source_name = clean_export_name_from_dag(fallback_source_name) or "export"
    base_name = resolve_export_base_name(requested_fbx_name, source_name)
    file_stem = build_prefixed_file_stem(clip_prefix, base_name)
    node_prefix = sanitize_node_token(base_name)
    return ResolvedExportNaming(
        source_name=source_name,
        base_name=base_name,
        file_stem=file_stem,
        node_prefix=node_prefix,
    )
