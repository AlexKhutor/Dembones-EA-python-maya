from __future__ import annotations

import os
from pathlib import Path

import maya.cmds as cmds


def default_cache_root() -> str:
    user_app = Path(cmds.internalVar(userAppDir=True))
    maya_ver = str(cmds.about(version=True))
    return str(user_app / maya_ver / "DB_export" / "cache")


def default_result_export_root() -> str:
    user_app = Path(cmds.internalVar(userAppDir=True))
    maya_ver = str(cmds.about(version=True))
    return str(user_app / maya_ver / "DB_export" / "exports")


def default_cli_path() -> str:
    module_root = Path(__file__).resolve().parents[2]
    installed_cli = module_root / "bin" / "DemBones.exe"
    if installed_cli.exists():
        return str(installed_cli)

    repo_cli = (
        Path(__file__).resolve().parents[1]
        / "third_party"
        / "dem_bones_repo"
        / "bin"
        / "Windows"
        / "DemBones.exe"
    )
    return str(repo_cli)


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
