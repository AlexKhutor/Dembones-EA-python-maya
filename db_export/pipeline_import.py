from __future__ import annotations

import os
import re

import maya.cmds as cmds
import maya.mel as mel


def safe_namespace(value: str) -> str:
    ns = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "")).strip("_")
    if not ns:
        ns = "db_export_cli"
    if ns[0].isdigit():
        ns = "db_export_" + ns
    return ns


def next_namespace(base: str) -> str:
    candidate = base
    index = 1
    while cmds.namespace(exists=candidate):
        candidate = "{0}{1}".format(base, index)
        index += 1
    return candidate


def resolve_import_namespace(raw_namespace: str) -> tuple[str, str]:
    requested = safe_namespace(raw_namespace)
    if cmds.namespace(exists=requested):
        resolved = next_namespace(requested)
        return resolved, "namespace_conflict: '{0}' -> '{1}'".format(requested, resolved)
    return requested, "namespace_selected: '{0}'".format(requested)


def _root_paths_from_nodes(nodes: list[str]) -> list[str]:
    roots = set()
    for node in nodes or []:
        if not cmds.objExists(node):
            continue
        long_path = cmds.ls(node, long=True) or []
        if not long_path:
            continue
        parts = long_path[0].strip("|").split("|")
        if parts:
            roots.add("|" + parts[0])
    return sorted(roots, key=len, reverse=True)


def _delete_nodes_safe(nodes: list[str]) -> None:
    for node in nodes or []:
        if not cmds.objExists(node):
            continue
        try:
            cmds.delete(node)
        except Exception:
            pass


def cleanup_imported_nodes(new_nodes: list[str], namespace: str | None) -> None:
    roots = _root_paths_from_nodes(new_nodes)
    _delete_nodes_safe(roots)

    if namespace and cmds.namespace(exists=namespace):
        try:
            cmds.namespace(removeNamespace=namespace, mergeNamespaceWithRoot=True)
        except Exception:
            pass


def _mel_symbol_exists(name: str) -> bool:
    if not name:
        return False
    try:
        return bool(mel.eval('exists "{0}"'.format(name)))
    except Exception:
        return False


def _try_mel_import_option(symbol_name: str, cmd_text: str, log) -> None:
    if not _mel_symbol_exists(symbol_name):
        if log:
            log("fbx_import_option_skipped_unsupported: {0}".format(symbol_name))
        return
    try:
        mel.eval(cmd_text)
    except Exception as exc:
        if log:
            log("fbx_import_option_failed: {0} :: {1}".format(cmd_text, exc))


def _import_cli_fbx_mel(
    path: str,
    namespace: str,
    start_f: int,
    end_f: int,
    step_f: int,
    log,
) -> list[str]:
    norm = path.replace("\\", "/")
    before = set(cmds.ls(long=True) or [])

    _try_mel_import_option("FBXResetImport", "FBXResetImport;", log)
    mel_cmds = [
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
    for symbol_name, cmd_text in mel_cmds:
        _try_mel_import_option(symbol_name, cmd_text, log)

    prev_ns = cmds.namespaceInfo(cur=True) or ":"
    try:
        if namespace and not cmds.namespace(exists=namespace):
            cmds.namespace(add=namespace)
        if namespace:
            cmds.namespace(set=namespace)
        _try_mel_import_option("FBXImport", 'FBXImport -f "{0}";'.format(norm), log)
    finally:
        try:
            cmds.namespace(set=prev_ns if prev_ns else ":")
        except Exception:
            cmds.namespace(set=":")

    after = set(cmds.ls(long=True) or [])
    return sorted(after - before)


def _import_cli_fbx_cmds(path: str, namespace: str) -> list[str]:
    before = set(cmds.ls(long=True) or [])
    kwargs = {
        "i": True,
        "type": "FBX",
        "ignoreVersion": True,
        "mergeNamespacesOnClash": False,
        "namespace": namespace,
        "options": "v=0;",
        "preserveReferences": True,
    }
    try:
        cmds.file(path, importTimeRange="combine", **kwargs)
    except TypeError:
        cmds.file(path, **kwargs)
    after = set(cmds.ls(long=True) or [])
    return sorted(after - before)


def _joint_key_stats(joints: list[str]) -> tuple[int, int]:
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


def cleanup_unwanted_dynamic_nodes(new_nodes: list[str], log):
    dynamic_types = ["nCloth", "nucleus", "nRigid", "dynamicConstraint"]
    dynamic_nodes = cmds.ls(new_nodes, type=dynamic_types, long=True) or []
    if dynamic_nodes:
        try:
            cmds.delete(dynamic_nodes)
            log("cleanup_dynamic_nodes: removed {0}".format(len(dynamic_nodes)))
        except Exception as exc:
            log("cleanup_dynamic_nodes_failed: {0}".format(exc))

    transform_like = [
        n
        for n in (cmds.ls(new_nodes, type="transform", long=True) or [])
        if n.split("|")[-1].lower().startswith("ncloth")
    ]
    if transform_like:
        try:
            cmds.delete(transform_like)
            log("cleanup_dynamic_transforms: removed {0}".format(len(transform_like)))
        except Exception as exc:
            log("cleanup_dynamic_transforms_failed: {0}".format(exc))


def import_cli_fbx(path: str, namespace: str, start_f: int, end_f: int, step_f: int, log):
    methods = [
        ("mel_fbximport", lambda ns: _import_cli_fbx_mel(path, ns, start_f, end_f, step_f, log)),
        ("cmds_file_import", lambda ns: _import_cli_fbx_cmds(path, ns)),
    ]

    last_nodes: list[str] = []
    last_ns = namespace
    last_method = "none"

    for idx, (method_name, importer) in enumerate(methods):
        ns = namespace if idx == 0 else next_namespace(namespace + "_retry_")
        new_nodes = importer(ns)
        last_nodes = new_nodes
        last_ns = ns
        last_method = method_name

        joints = cmds.ls(new_nodes, type="joint", long=True) or []
        anim_curves = cmds.ls(
            new_nodes, type=("animCurveTL", "animCurveTA", "animCurveTU"), long=True
        ) or []
        keyed_joints, total_joint_keys = _joint_key_stats(joints)

        if log:
            log("import_method: {0}".format(method_name))
            log("import_namespace_attempt: {0}".format(ns))
            log("import_animCurves_attempt: {0}".format(len(anim_curves)))
            log("import_keyed_joints_attempt: {0}".format(keyed_joints))
            log("import_total_joint_keys_attempt: {0}".format(total_joint_keys))

        if anim_curves or keyed_joints > 0:
            return new_nodes, ns, method_name

        cleanup_imported_nodes(new_nodes, ns)

    return last_nodes, last_ns, last_method


def fbx_animation_token_probe(path: str) -> tuple[bool, dict[str, int]]:
    tokens = [
        b"AnimationCurve",
        b"AnimCurveNode",
        b"KeyTime",
        b"Take 001",
        b"demBones",
    ]
    counts: dict[str, int] = {}
    try:
        with open(path, "rb") as fp:
            data = fp.read()
        for token in tokens:
            counts[token.decode("ascii")] = int(data.count(token))
        has_anim = counts.get("AnimationCurve", 0) > 0 and counts.get("KeyTime", 0) > 0
        return has_anim, counts
    except Exception:
        return False, {}
