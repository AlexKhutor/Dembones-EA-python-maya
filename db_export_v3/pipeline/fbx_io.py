from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import maya.cmds as cmds
import maya.mel as mel

from ..core.models import CliRunSettings, PreparedRun


def _hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    kwargs: dict = {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    startf_use_show_window = int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
    sw_hide = int(getattr(subprocess, "SW_HIDE", 0) or 0)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= startf_use_show_window
        startupinfo.wShowWindow = sw_hide
        kwargs["startupinfo"] = startupinfo
    return kwargs


def ensure_maya_io_plugins() -> None:
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")
    if not cmds.pluginInfo("AbcExport", query=True, loaded=True):
        cmds.loadPlugin("AbcExport")
    if not cmds.pluginInfo("AbcImport", query=True, loaded=True):
        cmds.loadPlugin("AbcImport")


def export_fbx_selection(
    path: str,
    nodes: list[str],
    *,
    export_skins: bool = False,
    include_input_connections: bool = False,
    bake_animation: bool = False,
    start_f: int | None = None,
    end_f: int | None = None,
    step_f: int | None = None,
) -> None:
    if not nodes:
        raise RuntimeError("FBX export failed: empty node list.")

    export_path = path.replace("\\", "/")
    cmds.select(clear=True)
    cmds.select(nodes, replace=True)
    mel.eval("FBXResetExport;")
    mel.eval("FBXExportSmoothingGroups -v true;")
    mel.eval("FBXExportShapes -v true;")
    mel.eval("FBXExportSkins -v {0};".format("true" if export_skins else "false"))
    try:
        mel.eval(
            "FBXExportInputConnections -v {0};".format(
                "true" if include_input_connections else "false"
            )
        )
    except Exception:
        pass
    mel.eval("FBXExportAnimationOnly -v false;")
    if bake_animation:
        try:
            mel.eval("FBXExportBakeComplexAnimation -v true;")
            if start_f is not None:
                mel.eval("FBXExportBakeComplexStart -v {0};".format(int(start_f)))
            if end_f is not None:
                mel.eval("FBXExportBakeComplexEnd -v {0};".format(int(end_f)))
            if step_f is not None:
                mel.eval("FBXExportBakeComplexStep -v {0};".format(int(step_f)))
        except Exception:
            pass
    mel.eval('FBXExport -f "{0}" -s;'.format(export_path))

    if not os.path.exists(path):
        raise RuntimeError("FBX export failed: file not created: {0}".format(path))


def export_alembic(
    path: str,
    root_transform: str,
    start_f: int,
    end_f: int,
    step_f: int,
    *,
    world_space: bool = True,
) -> None:
    abc_path = path.replace("\\", "/")
    root_path = root_transform.replace("\\", "/")
    world_space_flag = "-worldSpace " if world_space else ""
    job = (
        "-frameRange {0} {1} -step {2} "
        "-uvWrite {3}-writeVisibility -dataFormat ogawa "
        '-root "{4}" -file "{5}"'
    ).format(int(start_f), int(end_f), int(step_f), world_space_flag, root_path, abc_path)
    cmds.AbcExport(jobArg=job)
    if not os.path.exists(path):
        raise RuntimeError("Alembic export failed: file not created: {0}".format(path))


def import_alembic_result(path: str, namespace: str) -> list[str]:
    before = set(cmds.ls(long=True) or [])
    cmds.file(
        path,
        i=True,
        type="Alembic",
        ignoreVersion=True,
        mergeNamespacesOnClash=False,
        namespace=namespace,
        options="v=0;",
        preserveReferences=True,
    )
    after = set(cmds.ls(long=True) or [])
    return sorted(after - before)


def find_imported_mesh_shape(
    new_nodes: list[str], expected_vertex_count: int, start_f: int, end_f: int
) -> tuple[str | None, list[dict]]:
    from ..maya.mesh_probe import probe_motion

    meshes = cmds.ls(new_nodes, long=True, type="mesh") or []
    candidates = []
    for shape in meshes:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
            vcount = int(cmds.polyEvaluate(shape, vertex=True))
            motion_probe = probe_motion(shape, start_f, end_f)
            motion_max = max(motion_probe.values()) if motion_probe else 0.0
            candidates.append(
                {
                    "shape": shape,
                    "vcount": vcount,
                    "motion_max": float(motion_max),
                }
            )
        except Exception:
            continue

    if not candidates:
        return None, []

    candidates.sort(
        key=lambda c: (
            1 if c["vcount"] == int(expected_vertex_count) else 0,
            c["motion_max"],
        ),
        reverse=True,
    )
    return candidates[0]["shape"], candidates


def copy_latest(src: str, latest_path: str) -> None:
    os.makedirs(os.path.dirname(latest_path), exist_ok=True)
    shutil.copy2(src, latest_path)


def resolve_mayapy_path() -> str:
    candidates: list[Path] = []
    maya_location = str(os.environ.get("MAYA_LOCATION") or "").strip()
    if maya_location:
        candidates.append(Path(maya_location) / "bin" / "mayapy.exe")

    current_executable = Path(sys.executable)
    candidates.append(current_executable.with_name("mayapy.exe"))
    candidates.append(current_executable)

    seen: set[str] = set()
    for candidate in candidates:
        norm = str(candidate)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if candidate.is_file() and candidate.name.lower() == "mayapy.exe":
            return str(candidate)
    raise RuntimeError("Could not resolve mayapy.exe for clean deliverable export.")


def clean_export_script_path() -> str:
    script_path = Path(__file__).resolve().parents[1] / "deliverable_export_standalone.py"
    if not script_path.is_file():
        raise RuntimeError("Clean deliverable export helper not found: {0}".format(script_path))
    return str(script_path)


def run_clean_scene_deliverable_export(
    prepared: PreparedRun,
    settings: CliRunSettings,
    destination_path: str,
    node_prefix: str,
    log,
) -> dict:
    mayapy_exe = resolve_mayapy_path()
    helper_script = clean_export_script_path()
    helper_log_path = str(Path(prepared.run_dir) / "deliverable_clean_export.log")
    command = [
        mayapy_exe,
        helper_script,
        "--source-fbx",
        prepared.out_fbx,
        "--dest-fbx",
        destination_path,
        "--start",
        str(int(settings.frame_start)),
        "--end",
        str(int(settings.frame_end)),
        "--step",
        str(int(settings.frame_step)),
        "--log-path",
        helper_log_path,
        "--node-prefix",
        node_prefix,
    ]
    if bool(getattr(settings, "wrap_world_root", False)):
        command.append("--wrap-world-root")
    log("result_export_mayapy: {0}".format(mayapy_exe))
    log("result_export_helper_script: {0}".format(helper_script))
    log("result_export_helper_log: {0}".format(helper_log_path))
    log("result_export_node_prefix: {0}".format(node_prefix))
    log("result_export_wrap_world_root: {0}".format(bool(getattr(settings, "wrap_world_root", False))))
    helper_env = os.environ.copy()
    helper_env["MAYA_SKIP_USERSETUP_PY"] = "1"
    helper_env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            env=helper_env,
            **_hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Clean deliverable export timed out after {0} seconds.".format(int(exc.timeout or 0)))

    helper_result: dict = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT_JSON:"):
            payload = line[len("RESULT_JSON:") :].strip()
            try:
                helper_result = json.loads(payload)
            except Exception as exc:
                log("result_export_helper_json_parse_failed: {0}".format(exc))
            continue
        if line.strip():
            log("result_export_helper_stdout: {0}".format(line))
    for line in (proc.stderr or "").splitlines():
        if line.strip():
            log("result_export_helper_stderr: {0}".format(line))

    if proc.returncode != 0:
        raise RuntimeError(
            "Clean deliverable export failed via mayapy (exit={0}). See helper log: {1}".format(
                int(proc.returncode), helper_log_path
            )
        )
    if not os.path.isfile(destination_path):
        raise RuntimeError("Clean deliverable export did not create destination FBX: {0}".format(destination_path))
    return {
        "mayapy": mayapy_exe,
        "helperScript": helper_script,
        "helperLog": helper_log_path,
        "helperResult": helper_result,
    }
