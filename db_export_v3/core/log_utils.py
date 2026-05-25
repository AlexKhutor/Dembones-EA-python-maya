from __future__ import annotations

import datetime
import glob
import json
import os
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import maya.cmds as cmds


_LOG_FILE_NAME = "db_export_v3_export_debug.log"
_JSON_FILE_NAME = "db_export_v3_export_debug.json"
_MAYA_FBX_EXPORT_LOG_NAME = "db_export_v3_maya_fbx_export.log"
_MAYA_FBX_IMPORT_LOG_NAME = "db_export_v3_maya_fbx_import.log"


def now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso() -> str:
    return datetime.datetime.now().isoformat()


def logs_root() -> Path:
    return Path(__file__).resolve().parents[1] / "Logs"


def old_logs_root() -> Path:
    return logs_root() / "old"


def ensure_dir(path: str | Path) -> Path:
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj


def _archive_stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _archive_existing_file(current_path: str | Path) -> None:
    current = Path(current_path)
    if not current.exists() or not current.is_file():
        return

    ensure_dir(old_logs_root())
    dst = old_logs_root() / current.name
    if dst.exists():
        dst = old_logs_root() / "{0}_{1}{2}".format(
            current.stem,
            _archive_stamp(),
            current.suffix,
        )
    shutil.move(str(current), str(dst))


def maya_user_app_dir() -> str:
    try:
        return os.path.abspath(cmds.internalVar(userAppDir=True))
    except Exception:
        return os.path.abspath(os.path.join(os.path.expanduser("~"), "Documents", "maya"))


def find_latest_maya_fbx_log_path(kind: str) -> str:
    suffix = str(kind or "").strip().lower()
    if suffix not in {"exp", "imp"}:
        return ""

    try:
        maya_version = str(cmds.about(version=True))
    except Exception:
        return ""

    pattern = os.path.join(maya_user_app_dir(), "FBX", "Logs", "*", "maya{0}{1}.log".format(maya_version, suffix))
    candidates = [path for path in glob.glob(pattern) if os.path.isfile(path)]
    if not candidates:
        return ""

    candidates.sort(key=os.path.getmtime, reverse=True)
    return os.path.abspath(candidates[0])


def path_state(path: str | Path) -> dict[str, Any]:
    raw = str(path or "")
    if not raw:
        return {
            "path": "",
            "absolutePath": "",
            "exists": False,
            "isFile": False,
            "isDir": False,
            "sizeBytes": 0,
            "parentPath": "",
            "parentExists": False,
        }

    abs_path = os.path.abspath(raw)
    is_file = os.path.isfile(abs_path)
    is_dir = os.path.isdir(abs_path)
    parent_path = os.path.dirname(abs_path)
    size_bytes = 0
    if is_file:
        try:
            size_bytes = int(os.path.getsize(abs_path))
        except Exception:
            size_bytes = -1

    return {
        "path": raw,
        "absolutePath": abs_path,
        "exists": bool(os.path.exists(abs_path)),
        "isFile": bool(is_file),
        "isDir": bool(is_dir),
        "sizeBytes": int(size_bytes),
        "parentPath": parent_path,
        "parentExists": bool(parent_path and os.path.isdir(parent_path)),
    }


def directory_snapshot(path: str | Path, max_entries: int = 20) -> dict[str, Any]:
    state = path_state(path)
    out = {
        "pathState": state,
        "entryCount": 0,
        "fbxFileCount": 0,
        "entries": [],
    }
    if not state["isDir"]:
        return out

    entries = []
    try:
        names = sorted(os.listdir(state["absolutePath"]))
    except Exception as exc:
        out["listError"] = str(exc)
        return out

    out["entryCount"] = int(len(names))
    fbx_count = 0
    for name in names:
        full = os.path.join(state["absolutePath"], name)
        is_file = os.path.isfile(full)
        if is_file and name.lower().endswith(".fbx"):
            fbx_count += 1
        if len(entries) >= max_entries:
            continue
        item = {
            "name": name,
            "isFile": bool(is_file),
            "isDir": bool(os.path.isdir(full)),
        }
        if is_file:
            try:
                item["sizeBytes"] = int(os.path.getsize(full))
            except Exception:
                item["sizeBytes"] = -1
        entries.append(item)

    out["fbxFileCount"] = int(fbx_count)
    out["entries"] = entries
    return out


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class RunDebugLogger:
    def __init__(self, package_name: str, package_version: str):
        self._package_name = str(package_name)
        self._package_version = str(package_version)
        self._logs_root = ensure_dir(logs_root())
        ensure_dir(old_logs_root())
        self._text_path = self._logs_root / _LOG_FILE_NAME
        self._json_path = self._logs_root / _JSON_FILE_NAME
        self._maya_export_snapshot_path = self._logs_root / _MAYA_FBX_EXPORT_LOG_NAME
        self._maya_import_snapshot_path = self._logs_root / _MAYA_FBX_IMPORT_LOG_NAME
        self._text_handle = None
        self._payload: dict[str, Any] = {}
        self._cli_tail_limit = 300
        self._start_new_current_logs()

    @property
    def text_log_path(self) -> str:
        return str(self._text_path)

    @property
    def json_log_path(self) -> str:
        return str(self._json_path)

    def _start_new_current_logs(self) -> None:
        for path in (
            self._text_path,
            self._json_path,
            self._maya_export_snapshot_path,
            self._maya_import_snapshot_path,
        ):
            _archive_existing_file(path)

        self._text_handle = open(self._text_path, "w", encoding="utf-8")
        self._payload = {
            "createdAt": now_stamp(),
            "createdAtIso": now_iso(),
            "packageName": self._package_name,
            "packageVersion": self._package_version,
            "status": "running",
            "errorMessage": "",
            "logsRoot": str(self._logs_root),
            "oldLogsRoot": str(old_logs_root()),
            "environment": {
                "mayaVersion": str(cmds.about(version=True) or ""),
                "mayaScene": cmds.file(query=True, sceneName=True) or "",
                "currentSceneSelection": cmds.ls(selection=True, long=True) or [],
            },
            "settings": {},
            "sections": {},
            "issues": [],
            "events": [],
            "progress": [],
            "cli": {
                "tailLines": [],
                "lineCount": 0,
            },
            "mayaFbxLogs": {},
        }
        self._write_json()

    def close(self) -> None:
        if self._text_handle is None:
            return
        try:
            self._text_handle.close()
        finally:
            self._text_handle = None

    def _write_text(self, line: str) -> None:
        if self._text_handle is None:
            return
        self._text_handle.write(line + "\n")
        self._text_handle.flush()

    def _write_json(self) -> None:
        with open(self._json_path, "w", encoding="utf-8") as handle:
            json.dump(_json_safe(self._payload), handle, indent=2, sort_keys=True, ensure_ascii=False)

    def event(self, message: str, error: bool = False, source: str = "app", persist: bool = False) -> None:
        text = str(message)
        entry = {
            "timestamp": now_stamp(),
            "timestampIso": now_iso(),
            "error": bool(error),
            "source": str(source),
            "message": text,
        }
        self._payload["events"].append(entry)
        prefix = "[ERROR]" if error else "[INFO]"
        source_prefix = "[{0}]".format(source.upper())
        self._write_text("{0} {1} {2}".format(entry["timestamp"], prefix, source_prefix + " " + text))
        if persist or error:
            self._write_json()

    def record_progress(self, value: int, text: str) -> None:
        self._payload["progress"].append(
            {
                "timestamp": now_stamp(),
                "value": int(value),
                "text": str(text or ""),
            }
        )
        if len(self._payload["progress"]) > 200:
            self._payload["progress"] = self._payload["progress"][-200:]

    def record_cli_line(self, line: str) -> None:
        cli = self._payload.setdefault("cli", {})
        cli["lineCount"] = int(cli.get("lineCount", 0)) + 1
        tail = list(cli.get("tailLines") or [])
        tail.append(str(line))
        if len(tail) > self._cli_tail_limit:
            tail = tail[-self._cli_tail_limit :]
        cli["tailLines"] = tail

    def set_settings(self, settings: Any) -> None:
        self._payload["settings"] = _json_safe(settings)
        self._write_json()

    def set_section(self, name: str, value: Any, persist: bool = True) -> None:
        self._payload.setdefault("sections", {})[str(name)] = _json_safe(value)
        if persist:
            self._write_json()

    def merge_section(self, name: str, value: dict[str, Any], persist: bool = True) -> None:
        section_name = str(name)
        section = self._payload.setdefault("sections", {}).setdefault(section_name, {})
        if not isinstance(section, dict):
            section = {}
            self._payload["sections"][section_name] = section
        for key, item in (value or {}).items():
            section[str(key)] = _json_safe(item)
        if persist:
            self._write_json()

    def append_issue(self, severity: str, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        issue = {
            "timestamp": now_stamp(),
            "severity": str(severity),
            "code": str(code),
            "message": str(message),
            "details": _json_safe(details or {}),
        }
        self._payload.setdefault("issues", []).append(issue)
        self._write_json()

    def snapshot_maya_fbx_log(self, kind: str) -> dict[str, Any]:
        suffix = str(kind or "").strip().lower()
        if suffix == "exp":
            dst = self._maya_export_snapshot_path
            label = "export"
        elif suffix == "imp":
            dst = self._maya_import_snapshot_path
            label = "import"
        else:
            return {"kind": suffix, "found": False, "sourcePath": "", "snapshotPath": ""}

        src = find_latest_maya_fbx_log_path(suffix)
        info = {
            "kind": label,
            "found": bool(src and os.path.isfile(src)),
            "sourcePath": src,
            "snapshotPath": str(dst),
            "snapshotExists": False,
            "sizeBytes": 0,
        }
        if info["found"]:
            try:
                shutil.copy2(src, dst)
                info["snapshotExists"] = True
                info["sizeBytes"] = int(os.path.getsize(dst))
            except Exception as exc:
                info["copyError"] = str(exc)
        self._payload.setdefault("mayaFbxLogs", {})[label] = info
        self._write_json()
        return info

    def finalize(self, status: str, error_message: str = "") -> None:
        self._payload["status"] = str(status)
        self._payload["errorMessage"] = str(error_message or "")
        self._payload["finishedAt"] = now_stamp()
        self._payload["finishedAtIso"] = now_iso()
        self._write_json()
        self.close()
