from __future__ import annotations

import os
from pathlib import Path

import maya.cmds as cmds


def _candidate_cli_paths() -> list[Path]:
    current = Path(__file__).resolve()
    candidates: list[Path] = []

    parents = list(current.parents)
    # Installed Maya module layout:
    # <module_root>/scripts/db_export_v3/maya/paths.py
    if len(parents) >= 4:
        candidates.append(parents[3] / "bin" / "DemBones.exe")

    # Package-local bin layout for toolset/source integrations.
    if len(parents) >= 2:
        candidates.append(parents[1] / "bin" / "DemBones.exe")

    # Repo-root fallback used in this workspace layout.
    if len(parents) >= 3:
        candidates.append(parents[2] / "third_party" / "dem_bones_repo" / "bin" / "Windows" / "DemBones.exe")

    # Package-local third_party fallback, if someone embeds the binary there.
    if len(parents) >= 2:
        candidates.append(parents[1] / "third_party" / "dem_bones_repo" / "bin" / "Windows" / "DemBones.exe")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def default_cache_root() -> str:
    user_app = Path(cmds.internalVar(userAppDir=True))
    maya_ver = str(cmds.about(version=True))
    return str(user_app / maya_ver / "DB_export_v3" / "cache")


def default_result_export_root() -> str:
    user_app = Path(cmds.internalVar(userAppDir=True))
    maya_ver = str(cmds.about(version=True))
    return str(user_app / maya_ver / "DB_export_v3" / "exports")


def default_cli_path() -> str:
    candidates = _candidate_cli_paths()
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    if candidates:
        return str(candidates[0])
    return "DemBones.exe"


def directory_size_bytes(path: str) -> int:
    if not path:
        return 0
    if not os.path.isdir(path):
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                total += os.path.getsize(full)
            except Exception:
                pass
    return total
