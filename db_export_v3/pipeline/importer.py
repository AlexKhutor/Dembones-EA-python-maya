from __future__ import annotations

import os
import re

import maya.cmds as cmds
import maya.mel as mel

from ..maya.mesh_probe import probe_motion
from ..core.models import ImportValidation


def safe_namespace(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    ns = re.sub(r"[^0-9A-Za-z_]+", "_", raw).strip("_")
    if not ns:
        return ""
    if ns[0].isdigit():
        ns = "db_export_v3_" + ns
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
    if not requested:
        return "", "namespace_disabled: importing into root namespace"
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


def _namespace_nodes(namespace: str | None) -> list[str]:
    if not namespace:
        return []
    patterns = [
        namespace + ":*",
        "*|" + namespace + ":*",
    ]
    found: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for node in cmds.ls(pattern, long=True) or []:
            if node in seen:
                continue
            seen.add(node)
            found.append(node)
    return found


def cleanup_imported_nodes(new_nodes: list[str], namespace: str | None) -> None:
    namespaced_nodes = _namespace_nodes(namespace)
    delete_targets = list(new_nodes or []) + namespaced_nodes
    _delete_nodes_safe(delete_targets)

    # Some DAG nodes can survive individual deletes if only descendants were
    # listed in new_nodes. Remove any remaining roots as a final cleanup pass.
    remaining_roots = _root_paths_from_nodes(delete_targets)
    _delete_nodes_safe(remaining_roots)

    if namespace and cmds.namespace(exists=namespace):
        try:
            cmds.namespace(removeNamespace=namespace)
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


def _configure_fbx_import(start_f: int, end_f: int, step_f: int, log) -> None:
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


def _import_cli_fbx_mel(path: str, namespace: str, start_f: int, end_f: int, step_f: int, log) -> list[str]:
    norm = path.replace("\\", "/")
    before = set(cmds.ls(long=True) or [])
    _configure_fbx_import(start_f, end_f, step_f, log)

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


def _import_cli_fbx_cmds(path: str, namespace: str, start_f: int, end_f: int, step_f: int, log) -> list[str]:
    before = set(cmds.ls(long=True) or [])
    if log:
        log("import_cmds_file_mode: using raw cmds.file import without FBX MEL preconfiguration")
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


def import_cli_fbx_with_method(
    path: str,
    namespace: str,
    start_f: int,
    end_f: int,
    step_f: int,
    log,
    method_name: str,
):
    if method_name == "cmds_file_import":
        importer = _import_cli_fbx_cmds
    elif method_name == "mel_fbximport":
        importer = _import_cli_fbx_mel
    else:
        raise RuntimeError("Unsupported FBX import method: {0}".format(method_name))

    try:
        new_nodes = importer(path, namespace, start_f, end_f, step_f, log)
    except Exception as exc:
        if log:
            log("import_method_failed: {0} :: {1}".format(method_name, exc))
        raise RuntimeError("FBX import failed via {0}: {1}".format(method_name, exc))

    joints = cmds.ls(new_nodes, type="joint", long=True) or []
    anim_curves = cmds.ls(
        new_nodes, type=("animCurveTL", "animCurveTA", "animCurveTU"), long=True
    ) or []
    keyed_joints, total_joint_keys = _joint_key_stats(joints)

    if log:
        log("import_method: {0}".format(method_name))
        log("import_namespace_attempt: {0}".format(namespace))
        log("import_new_nodes_attempt: {0}".format(len(new_nodes)))
        log("import_animCurves_attempt: {0}".format(len(anim_curves)))
        log("import_keyed_joints_attempt: {0}".format(keyed_joints))
        log("import_total_joint_keys_attempt: {0}".format(total_joint_keys))

    return new_nodes, namespace, method_name


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


def _joint_hierarchy_stats(joints: list[str]) -> dict:
    joint_list = [joint for joint in (joints or []) if cmds.objExists(joint)]
    if not joint_list:
        return {
            "totalJointCount": 0,
            "rootJointCount": 0,
            "rootJoints": [],
            "largestRoot": "",
            "largestRootSubtreeJointCount": 0,
            "largestRootDepth": 0,
            "maxDepthOverall": 0,
            "leafJointCount": 0,
            "duplicateShortNames": {},
            "sampleJointNames": [],
        }

    joint_set = set(joint_list)
    root_joints: list[str] = []
    leaf_joint_count = 0
    max_depth_overall = 0
    largest_root = ""
    largest_root_subtree_count = 0
    largest_root_depth = 0
    short_name_counts: dict[str, int] = {}

    for joint in joint_list:
        short_name = joint.split("|")[-1].split(":")[-1]
        short_name_counts[short_name] = int(short_name_counts.get(short_name, 0)) + 1

        parent = cmds.listRelatives(joint, parent=True, fullPath=True) or []
        if not parent or parent[0] not in joint_set:
            root_joints.append(joint)

        child_joints = cmds.listRelatives(joint, children=True, type="joint", fullPath=True) or []
        local_children = [child for child in child_joints if child in joint_set]
        if not local_children:
            leaf_joint_count += 1

        depth = 0
        current = joint
        while True:
            parent = cmds.listRelatives(current, parent=True, fullPath=True) or []
            if not parent or parent[0] not in joint_set:
                break
            current = parent[0]
            depth += 1
        if depth > max_depth_overall:
            max_depth_overall = depth

    for root_joint in root_joints:
        subtree = cmds.listRelatives(root_joint, allDescendents=True, type="joint", fullPath=True) or []
        subtree = [joint for joint in subtree if joint in joint_set]
        subtree_count = 1 + len(subtree)
        subtree_depth = 0
        for joint in subtree:
            depth = 0
            current = joint
            while current != root_joint:
                parent = cmds.listRelatives(current, parent=True, fullPath=True) or []
                if not parent or parent[0] not in joint_set:
                    break
                current = parent[0]
                depth += 1
            if depth > subtree_depth:
                subtree_depth = depth
        if subtree_count > largest_root_subtree_count:
            largest_root = root_joint
            largest_root_subtree_count = subtree_count
            largest_root_depth = subtree_depth

    duplicate_short_names = {
        name: count for name, count in sorted(short_name_counts.items()) if int(count) > 1
    }
    sample_joint_names = joint_list[:25]

    return {
        "totalJointCount": int(len(joint_list)),
        "rootJointCount": int(len(root_joints)),
        "rootJoints": root_joints[:20],
        "largestRoot": largest_root,
        "largestRootSubtreeJointCount": int(largest_root_subtree_count),
        "largestRootDepth": int(largest_root_depth),
        "maxDepthOverall": int(max_depth_overall),
        "leafJointCount": int(leaf_joint_count),
        "duplicateShortNames": duplicate_short_names,
        "sampleJointNames": sample_joint_names,
    }


def _node_uuid(node: str) -> str:
    ids = cmds.ls(node, uuid=True) or []
    return ids[0] if ids else ""


def _node_from_uuid(node_uuid: str) -> str | None:
    if not node_uuid:
        return None
    nodes = cmds.ls(node_uuid, long=True) or []
    return nodes[0] if nodes else None


def _safe_preview_name(prefix: str, short_name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", str(short_name or "").replace(":", "_")).strip("_")
    if not cleaned:
        cleaned = "node"
    return "{0}{1}".format(prefix, cleaned)


def isolate_root_import_names(import_result: dict, preview_prefix: str, log) -> dict:
    namespace = str(import_result.get("namespace") or "")
    if namespace:
        return import_result

    dag_nodes = []
    seen = set()
    for node in import_result.get("new_nodes") or []:
        if not node or not cmds.objExists(node) or node in seen:
            continue
        try:
            node_type = cmds.nodeType(node)
        except Exception:
            continue
        if node_type not in {"joint", "transform", "mesh"}:
            continue
        seen.add(node)
        dag_nodes.append(node)

    if not dag_nodes:
        return import_result

    uuid_entries = []
    new_nodes_uuid_map: list[str] = []
    joints_uuid_map: list[str] = []
    meshes_uuid_map: list[str] = []
    chosen_mesh_uuid = ""
    for node in dag_nodes:
        node_uuid = _node_uuid(node)
        if not node_uuid:
            continue
        depth = node.count("|")
        short_name = node.split("|")[-1]
        uuid_entries.append(
            {
                "uuid": node_uuid,
                "depth": depth,
                "shortName": short_name,
            }
        )
    for node in import_result.get("new_nodes") or []:
        node_uuid = _node_uuid(node)
        if node_uuid:
            new_nodes_uuid_map.append(node_uuid)
    for node in import_result.get("joints") or []:
        node_uuid = _node_uuid(node)
        if node_uuid:
            joints_uuid_map.append(node_uuid)
    for node in import_result.get("meshes") or []:
        node_uuid = _node_uuid(node)
        if node_uuid:
            meshes_uuid_map.append(node_uuid)
    chosen_mesh = str(import_result.get("chosen_mesh") or "")
    if chosen_mesh:
        chosen_mesh_uuid = _node_uuid(chosen_mesh)

    uuid_entries.sort(key=lambda item: item["depth"], reverse=True)
    log("rootless_preview_name_isolation: {0}".format(preview_prefix))

    for item in uuid_entries:
        current = _node_from_uuid(item["uuid"])
        if not current or not cmds.objExists(current):
            continue
        target_name = _safe_preview_name(preview_prefix, current.split("|")[-1])
        try:
            cmds.rename(current, target_name)
        except Exception as exc:
            raise RuntimeError(
                "Scene import succeeded, but preview name isolation failed for '{0}': {1}".format(
                    current, exc
                )
            )

    remapped = dict(import_result)
    remapped["new_nodes"] = [node for node in (_node_from_uuid(v) for v in new_nodes_uuid_map) if node]
    remapped["joints"] = [node for node in (_node_from_uuid(v) for v in joints_uuid_map) if node]
    remapped["meshes"] = [node for node in (_node_from_uuid(v) for v in meshes_uuid_map) if node]
    remapped["chosen_mesh"] = _node_from_uuid(chosen_mesh_uuid) if chosen_mesh_uuid else chosen_mesh
    remapped["joint_hierarchy"] = _joint_hierarchy_stats(remapped.get("joints") or [])
    remapped["previewNameIsolationPrefix"] = preview_prefix
    preview_roots = _root_paths_from_nodes(remapped.get("new_nodes") or [])
    log("rootless_preview_roots: {0}".format(", ".join(preview_roots[:20])))
    return remapped


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


def _find_best_imported_mesh_shape(
    new_nodes: list[str], expected_vertex_count: int, start_f: int, end_f: int
) -> tuple[str | None, list[dict]]:
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
                    "motion_probe": motion_probe,
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


def import_cli_fbx(path: str, namespace: str, start_f: int, end_f: int, step_f: int, log):
    if log:
        log("import_authoritative_method: mel_fbximport")
        log("import_authoritative_reason: cmds.file FBX import ignores namespace on Maya 2026")
    return import_cli_fbx_with_method(
        path,
        namespace,
        start_f,
        end_f,
        step_f,
        log,
        "mel_fbximport",
    )


def validate_imported_result(
    new_nodes: list[str],
    expected_vertex_count: int,
    expect_animation: bool,
    start_f: int,
    end_f: int,
    log,
) -> ImportValidation:
    root_nodes = _root_paths_from_nodes(new_nodes)
    joints = cmds.ls(new_nodes, type="joint", long=True) or []
    meshes = cmds.ls(new_nodes, type="mesh", long=True) or []
    anim_curves = cmds.ls(
        new_nodes, type=("animCurveTL", "animCurveTA", "animCurveTU", "animCurve"), long=True
    ) or []
    keyed_joints, total_joint_keys = _joint_key_stats(joints)
    joint_hierarchy = _joint_hierarchy_stats(joints)
    chosen_mesh, candidates = _find_best_imported_mesh_shape(
        new_nodes,
        expected_vertex_count,
        start_f,
        end_f,
    )

    chosen_mesh_vertex_count = None
    chosen_mesh_motion_probe: dict[int, float] = {}
    chosen_mesh_motion_max = 0.0
    if chosen_mesh:
        for candidate in candidates:
            if candidate.get("shape") == chosen_mesh:
                chosen_mesh_vertex_count = int(candidate.get("vcount", 0))
                chosen_mesh_motion_probe = dict(candidate.get("motion_probe") or {})
                chosen_mesh_motion_max = float(candidate.get("motion_max") or 0.0)
                break

    issues: list[str] = []
    if not new_nodes:
        issues.append("FBX import produced no new nodes.")
    if not root_nodes:
        issues.append("Imported FBX produced no root DAG nodes.")
    if not joints:
        issues.append("Imported FBX produced no joints.")
    if not meshes:
        issues.append("Imported FBX produced no mesh shapes.")
    if expected_vertex_count > 0:
        if chosen_mesh is None:
            issues.append(
                "Imported FBX produced no mesh candidate that could be evaluated against the expected topology."
            )
        elif chosen_mesh_vertex_count != int(expected_vertex_count):
            issues.append(
                "Imported mesh topology mismatch. expected_vtx={0} actual_vtx={1}".format(
                    int(expected_vertex_count),
                    int(chosen_mesh_vertex_count or 0),
                )
            )
    if expect_animation:
        has_animation_signal = bool(anim_curves or keyed_joints > 0 or chosen_mesh_motion_max > 1e-6)
        if not has_animation_signal:
            issues.append(
                "Imported FBX appears static: no animCurve, no keyed joints, and no detected mesh motion."
            )

    if log:
        log("import_validation_root_nodes: {0}".format(len(root_nodes)))
        log("import_validation_joint_total: {0}".format(joint_hierarchy.get("totalJointCount", 0)))
        log("import_validation_joint_root_count: {0}".format(joint_hierarchy.get("rootJointCount", 0)))
        log(
            "import_validation_largest_root_subtree_joint_count: {0}".format(
                joint_hierarchy.get("largestRootSubtreeJointCount", 0)
            )
        )
        log("import_validation_largest_root: {0}".format(joint_hierarchy.get("largestRoot") or "none"))
        log("import_validation_max_depth_overall: {0}".format(joint_hierarchy.get("maxDepthOverall", 0)))
        log("import_validation_mesh_candidates: {0}".format(len(candidates)))
        log("import_validation_chosen_mesh: {0}".format(chosen_mesh or "none"))
        if chosen_mesh is not None:
            log("import_validation_chosen_mesh_vtx: {0}".format(int(chosen_mesh_vertex_count or 0)))
            log("import_validation_chosen_mesh_motion_max: {0:.6f}".format(chosen_mesh_motion_max))
        log("import_validation_expected_animation: {0}".format(bool(expect_animation)))
        if issues:
            log("import_validation_issues: {0}".format(" | ".join(issues)))
        else:
            log("import_validation_issues: none")

    return ImportValidation(
        success=not issues,
        issues=issues,
        root_nodes=root_nodes,
        joints=joints,
        meshes=meshes,
        anim_curves=anim_curves,
        keyed_joints=keyed_joints,
        total_joint_keys=total_joint_keys,
        mesh_candidates=candidates,
        chosen_mesh=chosen_mesh,
        chosen_mesh_vertex_count=chosen_mesh_vertex_count,
        chosen_mesh_motion_probe=chosen_mesh_motion_probe,
        chosen_mesh_motion_max=chosen_mesh_motion_max,
        expected_animation=bool(expect_animation),
        joint_hierarchy=joint_hierarchy,
    )


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
