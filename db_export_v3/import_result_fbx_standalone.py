from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _ensure_fbx_plugin(cmds) -> None:
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")


def _mel_symbol_exists(mel, name: str) -> bool:
    if not name:
        return False
    try:
        return bool(mel.eval('exists "{0}"'.format(name)))
    except Exception:
        return False


def _query_mel_option(mel, symbol_name: str):
    if not _mel_symbol_exists(mel, symbol_name):
        return "<unsupported>"
    try:
        return mel.eval("{0} -q;".format(symbol_name))
    except Exception as exc:
        return "<query_failed: {0}>".format(exc)


def _try_mel_option(mel, symbol_name: str, command_text: str, log) -> None:
    if not _mel_symbol_exists(mel, symbol_name):
        log("mel_option_skipped: {0}".format(symbol_name))
        return
    mel.eval(command_text)


def _fbx_import_option_snapshot(mel) -> dict:
    option_names = [
        "FBXImportMode",
        "FBXImportSkins",
        "FBXImportShapes",
        "FBXImportAnimation",
        "FBXImportFillTimeline",
        "FBXImportBakeComplexAnimation",
        "FBXImportBakeComplexStart",
        "FBXImportBakeComplexEnd",
        "FBXImportBakeComplexStep",
    ]
    return {name: _query_mel_option(mel, name) for name in option_names}


def _configure_fbx_import(mel, start_f: int, end_f: int, step_f: int, log) -> None:
    _try_mel_option(mel, "FBXResetImport", "FBXResetImport;", log)
    commands = [
        ("FBXImportMode", "FBXImportMode -v add;"),
        ("FBXImportSkins", "FBXImportSkins -v true;"),
        ("FBXImportShapes", "FBXImportShapes -v true;"),
        ("FBXImportAnimation", "FBXImportAnimation -v true;"),
        ("FBXImportFillTimeline", "FBXImportFillTimeline -v true;"),
        ("FBXImportBakeComplexAnimation", "FBXImportBakeComplexAnimation -v true;"),
        ("FBXImportBakeComplexStart", "FBXImportBakeComplexStart -v {0};".format(int(start_f))),
        ("FBXImportBakeComplexEnd", "FBXImportBakeComplexEnd -v {0};".format(int(end_f))),
        ("FBXImportBakeComplexStep", "FBXImportBakeComplexStep -v {0};".format(int(step_f))),
    ]
    for symbol_name, command_text in commands:
        _try_mel_option(mel, symbol_name, command_text, log)


def _joint_key_stats(cmds, joints: list[str]) -> tuple[int, int]:
    total_keys = 0
    keyed_joints = 0
    for joint in joints or []:
        try:
            count = int(cmds.keyframe(joint, query=True, keyframeCount=True) or 0)
        except Exception:
            count = 0
        total_keys += count
        if count > 0:
            keyed_joints += 1
    return keyed_joints, total_keys


def _root_joint_candidates(cmds, joints: list[str]) -> list[str]:
    roots: list[str] = []
    joint_set = set(joints or [])
    for joint in joints or []:
        parents = cmds.listRelatives(joint, parent=True, fullPath=True) or []
        if not parents or parents[0] not in joint_set:
            roots.append(joint)
    return roots


def _visible_mesh_shapes(cmds) -> list[str]:
    rows: list[str] = []
    for shape in cmds.ls(type="mesh", long=True) or []:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
        except Exception:
            pass
        rows.append(shape)
    return rows


def _safe_namespace(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = []
    for char in text:
        if char.isalnum() or char == "_":
            cleaned.append(char)
        else:
            cleaned.append("_")
    ns = "".join(cleaned).strip("_")
    if not ns:
        return ""
    if ns[0].isdigit():
        ns = "db_export_v3_" + ns
    return ns


class _Logger:
    def __init__(self, path: str = ""):
        self._path = str(path or "").strip()
        if self._path:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fp:
                fp.write("")

    def write(self, message: str) -> None:
        line = str(message)
        print(line, flush=True)
        if self._path:
            with open(self._path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")


def import_result_fbx(
    fbx_path: str,
    namespace: str = "",
    start_frame: int = 1,
    end_frame: int = 600,
    step_frame: int = 1,
    log_path: str = "",
) -> dict:
    import maya.cmds as cmds
    import maya.mel as mel

    logger = _Logger(log_path)
    _ensure_fbx_plugin(cmds)

    resolved_path = str(Path(fbx_path).expanduser().resolve())
    if not os.path.isfile(resolved_path):
        raise RuntimeError("FBX not found: {0}".format(resolved_path))

    requested_namespace = _safe_namespace(namespace)
    before_nodes = set(cmds.ls(long=True) or [])

    logger.write("fbx_import_helper_path: {0}".format(resolved_path))
    logger.write("fbx_import_helper_namespace: {0}".format(requested_namespace or "<root>"))
    logger.write(
        "fbx_import_helper_option_snapshot_before: {0}".format(
            json.dumps(_fbx_import_option_snapshot(mel), ensure_ascii=False, sort_keys=True)
        )
    )

    _configure_fbx_import(mel, start_frame, end_frame, step_frame, logger.write)

    logger.write(
        "fbx_import_helper_option_snapshot_after_config: {0}".format(
            json.dumps(_fbx_import_option_snapshot(mel), ensure_ascii=False, sort_keys=True)
        )
    )

    previous_namespace = cmds.namespaceInfo(cur=True) or ":"
    try:
        if requested_namespace and not cmds.namespace(exists=requested_namespace):
            cmds.namespace(add=requested_namespace)
        if requested_namespace:
            cmds.namespace(set=requested_namespace)
        mel.eval('FBXImport -f "{0}";'.format(resolved_path.replace("\\", "/")))
    finally:
        try:
            cmds.namespace(set=previous_namespace if previous_namespace else ":")
        except Exception:
            cmds.namespace(set=":")

    after_nodes = set(cmds.ls(long=True) or [])
    new_nodes = sorted(after_nodes - before_nodes)
    joints = cmds.ls(new_nodes, type="joint", long=True) or []
    meshes = cmds.ls(new_nodes, type="mesh", long=True) or []
    anim_curves = cmds.ls(
        new_nodes, type=("animCurveTL", "animCurveTA", "animCurveTU"), long=True
    ) or []
    keyed_joints, total_keys = _joint_key_stats(cmds, joints)
    roots = _root_joint_candidates(cmds, joints)
    visible_meshes = [shape for shape in meshes if shape in _visible_mesh_shapes(cmds)]

    result = {
        "path": resolved_path,
        "namespace": requested_namespace,
        "newNodeCount": len(new_nodes),
        "jointCount": len(joints),
        "meshCount": len(meshes),
        "visibleMeshCount": len(visible_meshes),
        "animCurveCount": len(anim_curves),
        "keyedJointCount": keyed_joints,
        "totalJointKeys": total_keys,
        "rootJoints": roots[:20],
        "visibleMeshes": visible_meshes[:20],
        "playbackMin": cmds.playbackOptions(query=True, min=True),
        "playbackMax": cmds.playbackOptions(query=True, max=True),
        "animationStart": cmds.playbackOptions(query=True, animationStartTime=True),
        "animationEnd": cmds.playbackOptions(query=True, animationEndTime=True),
    }

    logger.write("fbx_import_helper_result: {0}".format(json.dumps(result, ensure_ascii=False, sort_keys=True)))
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import DB_export_v3 result FBX with deterministic FBX options.")
    parser.add_argument("--fbx", required=True, help="Path to the FBX file.")
    parser.add_argument("--namespace", default="", help="Optional import namespace.")
    parser.add_argument("--start", type=int, default=1, help="Bake/import start frame.")
    parser.add_argument("--end", type=int, default=600, help="Bake/import end frame.")
    parser.add_argument("--step", type=int, default=1, help="Bake/import step.")
    parser.add_argument("--log", default="", help="Optional log path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    import_result_fbx(
        fbx_path=args.fbx,
        namespace=args.namespace,
        start_frame=args.start,
        end_frame=args.end,
        step_frame=args.step,
        log_path=args.log,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
