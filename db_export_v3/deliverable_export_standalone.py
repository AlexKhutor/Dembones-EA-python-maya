from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path


_INVALID_NODE_TOKEN_PATTERN = re.compile(r"[^0-9A-Za-z_]+")
_MULTI_UNDERSCORE_PATTERN = re.compile(r"_+")


class _Logger:
    def __init__(self, log_path: str = ""):
        self._log_path = str(log_path or "").strip()
        if self._log_path:
            Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "w", encoding="utf-8") as fp:
                fp.write("")

    def write(self, message: str) -> None:
        line = str(message)
        print(line, flush=True)
        if self._log_path:
            with open(self._log_path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")


def _sanitize_sys_path_for_maya_imports(log: _Logger) -> None:
    script_dir = Path(__file__).resolve().parent
    cleaned: list[str] = []
    removed: list[str] = []
    for entry in sys.path:
        try:
            resolved = Path(entry or ".").resolve()
        except Exception:
            resolved = None
        if resolved == script_dir:
            removed.append(str(entry))
            continue
        cleaned.append(entry)
    sys.path[:] = cleaned
    if removed:
        log.write("clean_export_sys_path_removed: {0}".format(", ".join(removed)))
    stale = [name for name in list(sys.modules.keys()) if name == "maya" or name.startswith("maya.")]
    for name in stale:
        try:
            del sys.modules[name]
        except Exception:
            pass


def _clean_export_name_from_dag(value: str) -> str:
    return str(value or "").split("|")[-1].split(":")[-1]


def _sanitize_node_token(value: str) -> str:
    text = _clean_export_name_from_dag(value).strip()
    text = _INVALID_NODE_TOKEN_PATTERN.sub("_", text)
    text = _MULTI_UNDERSCORE_PATTERN.sub("_", text)
    text = text.strip("_")
    if not text:
        text = "node"
    if text[0].isdigit():
        text = "_" + text
    return text


def _build_prefixed_node_name(base_name: str, original_short_name: str) -> str:
    prefix = _sanitize_node_token(base_name)
    suffix = _sanitize_node_token(original_short_name)
    return "{0}_{1}".format(prefix, suffix)


def _mel_symbol_exists(mel, name: str) -> bool:
    if not name:
        return False
    try:
        return bool(mel.eval('exists "{0}"'.format(name)))
    except Exception:
        return False


def _try_mel_option(mel, symbol_name: str, command_text: str, log: _Logger) -> None:
    if not _mel_symbol_exists(mel, symbol_name):
        log.write("mel_option_skipped: {0}".format(symbol_name))
        return
    mel.eval(command_text)


def _ensure_fbx_plugin(cmds) -> None:
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")


def _configure_fbx_import(mel, start_f: int, end_f: int, step_f: int, log: _Logger) -> None:
    _try_mel_option(mel, "FBXResetImport", "FBXResetImport;", log)
    options = [
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
    for symbol_name, command_text in options:
        _try_mel_option(mel, symbol_name, command_text, log)


def _import_fbx(mel, cmds, path: str, start_f: int, end_f: int, step_f: int, log: _Logger) -> list[str]:
    before = set(cmds.ls(long=True) or [])
    _configure_fbx_import(mel, start_f, end_f, step_f, log)
    mel.eval('FBXImport -f "{0}";'.format(path.replace("\\", "/")))
    after = set(cmds.ls(long=True) or [])
    return sorted(after - before)


def _list_user_namespaces(cmds) -> list[str]:
    namespaces = cmds.namespaceInfo(listOnlyNamespaces=True, recurse=True) or []
    cleaned: list[str] = []
    seen: set[str] = set()
    for namespace in namespaces:
        value = str(namespace or "").lstrip(":")
        if not value or value in {"UI", "shared"} or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    cleaned.sort(key=lambda item: item.count(":"), reverse=True)
    return cleaned


def _flatten_all_namespaces(cmds, log: _Logger) -> list[str]:
    removed: list[str] = []
    while True:
        namespaces = _list_user_namespaces(cmds)
        if not namespaces:
            break
        progress = False
        for namespace in namespaces:
            log.write("flatten_namespace: {0}".format(namespace))
            cmds.namespace(removeNamespace=namespace, mergeNamespaceWithRoot=True)
            removed.append(namespace)
            progress = True
        if not progress:
            break
    remaining = _list_user_namespaces(cmds)
    if remaining:
        raise RuntimeError("Namespaces remained after flatten: {0}".format(", ".join(remaining)))
    return removed


def _root_joint_nodes(cmds) -> list[str]:
    joints = cmds.ls(type="joint", long=True) or []
    roots: list[str] = []
    joint_set = set(joints)
    for joint in joints:
        parent = cmds.listRelatives(joint, parent=True, fullPath=True) or []
        if not parent or parent[0] not in joint_set:
            roots.append(joint)
    return roots


def _all_joint_nodes(cmds) -> list[str]:
    return cmds.ls(type="joint", long=True) or []


def _mesh_parent_nodes(cmds) -> list[str]:
    parents: list[str] = []
    seen: set[str] = set()
    for shape in cmds.ls(type="mesh", long=True) or []:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
        except Exception:
            continue
        parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
        if not parent:
            continue
        transform = parent[0]
        if transform in seen:
            continue
        seen.add(transform)
        parents.append(transform)
    return parents


def _export_candidate_nodes(cmds) -> list[str]:
    export_nodes: list[str] = []
    seen: set[str] = set()
    for node in _root_joint_nodes(cmds) + _mesh_parent_nodes(cmds):
        if not node or not cmds.objExists(node) or node in seen:
            continue
        seen.add(node)
        export_nodes.append(node)
    return export_nodes


def _wrapper_parent_targets(cmds) -> list[str]:
    candidates = _export_candidate_nodes(cmds)
    candidate_set = set(candidates)
    targets: list[str] = []
    for node in candidates:
        current = node
        has_candidate_ancestor = False
        while True:
            parent = cmds.listRelatives(current, parent=True, fullPath=True) or []
            if not parent:
                break
            current = parent[0]
            if current in candidate_set:
                has_candidate_ancestor = True
                break
        if not has_candidate_ancestor:
            targets.append(node)
    return targets


def _apply_world_root_wrapper(cmds, log: _Logger) -> dict:
    targets = _wrapper_parent_targets(cmds)
    if not targets:
        result = {
            "performed": False,
            "reason": "no_export_targets",
            "targetCount": 0,
            "wrapperJoint": "",
        }
        log.write("clean_export_world_root_wrapper: {0}".format(json.dumps(result, sort_keys=True)))
        return result

    cmds.select(clear=True)
    wrapper_joint = cmds.joint(name="world_root_wrapper", position=(0.0, 0.0, 0.0))
    for attr_name in ("translateX", "translateY", "translateZ", "rotateX", "rotateY", "rotateZ"):
        try:
            cmds.setAttr("{0}.{1}".format(wrapper_joint, attr_name), 0.0)
        except Exception:
            pass
    for attr_name in ("scaleX", "scaleY", "scaleZ"):
        try:
            cmds.setAttr("{0}.{1}".format(wrapper_joint, attr_name), 1.0)
        except Exception:
            pass
    for attr_name in ("jointOrientX", "jointOrientY", "jointOrientZ"):
        plug = "{0}.{1}".format(wrapper_joint, attr_name)
        if not cmds.objExists(plug):
            continue
        try:
            cmds.setAttr(plug, 0.0)
        except Exception:
            pass

    parented_nodes: list[str] = []
    for node in targets:
        if node == wrapper_joint or not cmds.objExists(node):
            continue
        result = cmds.parent(node, wrapper_joint, absolute=True) or []
        parented_nodes.append(str(result[0] if result else node))

    wrapper_values = cmds.ls(wrapper_joint, long=True) or [wrapper_joint]
    wrapper_joint = str(wrapper_values[0] or wrapper_joint)
    result = {
        "performed": True,
        "reason": "",
        "targetCount": int(len(parented_nodes)),
        "wrapperJoint": wrapper_joint,
        "targetPreview": parented_nodes[:25],
    }
    log.write("clean_export_world_root_wrapper: {0}".format(json.dumps(result, sort_keys=True)))
    return result


def _safe_attr_get(cmds, node: str, attr: str, default=None):
    plug = "{0}.{1}".format(node, attr)
    if not node or not cmds.objExists(plug):
        return default
    try:
        return cmds.getAttr(plug)
    except Exception:
        return default


def _safe_attr_set(cmds, node: str, attr: str, value) -> bool:
    plug = "{0}.{1}".format(node, attr)
    if not node or not cmds.objExists(plug):
        return False
    try:
        cmds.setAttr(plug, value)
        return True
    except Exception:
        return False


def _visibility_state(cmds, node: str, *, is_shape: bool = False) -> dict:
    state = {
        "node": node,
        "exists": bool(node and cmds.objExists(node)),
        "visibility": _safe_attr_get(cmds, node, "visibility", None),
        "lodVisibility": _safe_attr_get(cmds, node, "lodVisibility", None),
        "template": _safe_attr_get(cmds, node, "template", None),
        "hiddenInOutliner": _safe_attr_get(cmds, node, "hiddenInOutliner", None),
        "overrideEnabled": _safe_attr_get(cmds, node, "overrideEnabled", None),
        "overrideVisibility": _safe_attr_get(cmds, node, "overrideVisibility", None),
        "overrideDisplayType": _safe_attr_get(cmds, node, "overrideDisplayType", None),
    }
    if not is_shape:
        state["inheritsTransform"] = _safe_attr_get(cmds, node, "inheritsTransform", None)
    else:
        state["intermediateObject"] = _safe_attr_get(cmds, node, "intermediateObject", None)
    return state


def _mesh_visibility_report(cmds) -> list[dict]:
    rows: list[dict] = []
    for shape in cmds.ls(type="mesh", long=True) or []:
        parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
        transform = parent[0] if parent else ""
        rows.append(
            {
                "shape": shape,
                "transform": transform,
                "shapeState": _visibility_state(cmds, shape, is_shape=True),
                "transformState": _visibility_state(cmds, transform, is_shape=False) if transform else {},
            }
        )
    return rows


def _enforce_export_mesh_visibility(cmds, log: _Logger) -> list[dict]:
    changes: list[dict] = []
    for shape in cmds.ls(type="mesh", long=True) or []:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
        except Exception:
            pass
        parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
        transform = parent[0] if parent else ""
        changed = {
            "shape": shape,
            "transform": transform,
            "shapeAttrs": [],
            "transformAttrs": [],
        }
        for attr, value in (
            ("visibility", 1),
            ("template", 0),
            ("overrideEnabled", 0),
            ("overrideVisibility", 1),
            ("hiddenInOutliner", 0),
            ("intermediateObject", 0),
        ):
            if _safe_attr_set(cmds, shape, attr, value):
                changed["shapeAttrs"].append(attr)
        if transform:
            for attr, value in (
                ("visibility", 1),
                ("lodVisibility", 1),
                ("template", 0),
                ("overrideEnabled", 0),
                ("overrideVisibility", 1),
                ("hiddenInOutliner", 0),
            ):
                if _safe_attr_set(cmds, transform, attr, value):
                    changed["transformAttrs"].append(attr)
        changes.append(changed)
    log.write("clean_export_mesh_visibility_enforced_count: {0}".format(len(changes)))
    if changes:
        log.write(
            "clean_export_mesh_visibility_enforced_preview: {0}".format(
                json.dumps(changes[:6], ensure_ascii=False, sort_keys=True)
            )
        )
    return changes


def _export_nodes(cmds) -> list[str]:
    return _export_candidate_nodes(cmds)


def _node_uuid(cmds, node: str) -> str:
    try:
        values = cmds.ls(node, uuid=True) or []
    except Exception:
        values = []
    if not values:
        return ""
    return str(values[0] or "")


def _node_from_uuid(cmds, node_uuid: str) -> str:
    if not node_uuid:
        return ""
    values = cmds.ls(node_uuid, long=True) or []
    if not values:
        return ""
    return str(values[0] or "")


def _rename_export_nodes(cmds, node_prefix: str, log: _Logger) -> list[dict]:
    rename_targets: list[dict] = []
    seen: set[str] = set()
    for node in (_all_joint_nodes(cmds) + _mesh_parent_nodes(cmds)):
        if not node or node in seen or not cmds.objExists(node):
            continue
        seen.add(node)
        node_uuid = _node_uuid(cmds, node)
        if not node_uuid:
            continue
        rename_targets.append(
            {
                "uuid": node_uuid,
                "depth": int(node.count("|")),
                "shortName": node.split("|")[-1],
            }
        )

    if not rename_targets:
        return []

    rename_targets.sort(key=lambda item: item["depth"], reverse=True)
    renamed_rows: list[dict] = []
    for item in rename_targets:
        current = _node_from_uuid(cmds, item["uuid"])
        if not current or not cmds.objExists(current):
            continue
        target_name = _build_prefixed_node_name(node_prefix, item["shortName"])
        try:
            result = cmds.rename(current, target_name)
        except Exception as exc:
            raise RuntimeError(
                "Failed to rename clean-scene export node '{0}' to '{1}': {2}".format(
                    current,
                    target_name,
                    exc,
                )
            )
        renamed_rows.append(
            {
                "before": item["shortName"],
                "after": str(result).split("|")[-1],
            }
        )
    if renamed_rows:
        preview = ", ".join(
            "{0}->{1}".format(row["before"], row["after"]) for row in renamed_rows[:12]
        )
        log.write("clean_export_renamed_nodes: {0}".format(preview))
    else:
        log.write("clean_export_renamed_nodes: none")
    return renamed_rows


def _export_fbx(mel, cmds, path: str, nodes: list[str], start_f: int, end_f: int, step_f: int) -> None:
    if not nodes:
        raise RuntimeError("Standalone export failed: empty export node list.")
    cmds.select(clear=True)
    cmds.select(nodes, replace=True)
    norm = path.replace("\\", "/")
    mel.eval("FBXResetExport;")
    mel.eval("FBXExportSmoothingGroups -v true;")
    mel.eval("FBXExportShapes -v true;")
    mel.eval("FBXExportSkins -v true;")
    try:
        mel.eval("FBXExportInputConnections -v true;")
    except Exception:
        pass
    mel.eval("FBXExportAnimationOnly -v false;")
    try:
        mel.eval("FBXExportBakeComplexAnimation -v true;")
        mel.eval("FBXExportBakeComplexStart -v {0};".format(int(start_f)))
        mel.eval("FBXExportBakeComplexEnd -v {0};".format(int(end_f)))
        mel.eval("FBXExportBakeComplexStep -v {0};".format(int(step_f)))
    except Exception:
        pass
    mel.eval('FBXExport -f "{0}" -s;'.format(norm))
    if not os.path.isfile(path):
        raise RuntimeError("Standalone export failed: file not created: {0}".format(path))


def _keyed_joint_count(cmds, joints: list[str]) -> tuple[int, int]:
    keyed_joints = 0
    total_keys = 0
    for joint in joints:
        try:
            count = int(cmds.keyframe(joint, query=True, keyframeCount=True) or 0)
        except Exception:
            count = 0
        total_keys += count
        if count > 0:
            keyed_joints += 1
    return keyed_joints, total_keys


def _dag_signature(node: str) -> str:
    parts = [part.split(":")[-1] for part in str(node or "").split("|") if part]
    return "|".join(parts)


def _joint_parent(cmds, joint: str) -> str:
    parents = cmds.listRelatives(joint, parent=True, fullPath=True) or []
    if not parents:
        return ""
    parent = parents[0]
    return parent if cmds.nodeType(parent) == "joint" else ""


def _joint_translate_curves(cmds, joint: str) -> list[str]:
    curves: list[str] = []
    for attr in ("translateX", "translateY", "translateZ"):
        plug = "{0}.{1}".format(joint, attr)
        found = cmds.listConnections(plug, source=True, destination=False, type="animCurve") or []
        curves.extend(found)
    unique = []
    seen: set[str] = set()
    for curve in curves:
        if curve in seen:
            continue
        seen.add(curve)
        unique.append(curve)
    return unique


def _import_bind_reference_fbx(cmds, path: str, namespace: str) -> list[str]:
    before = set(cmds.ls(long=True) or [])
    cmds.file(
        path,
        i=True,
        type="FBX",
        ignoreVersion=True,
        mergeNamespacesOnClash=False,
        namespace=namespace,
        options="v=0;",
        preserveReferences=True,
    )
    after = set(cmds.ls(long=True) or [])
    return sorted(after - before)


def _capture_bind_local_translate_map(cmds, joints: list[str]) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for joint in joints:
        signature = _dag_signature(joint)
        try:
            values = cmds.getAttr(joint + ".translate")[0]
            result[signature] = [float(values[0]), float(values[1]), float(values[2])]
        except Exception:
            continue
    return result


def _apply_child_translate_bind_lock(
    cmds,
    source_joints: list[str],
    bind_reference_fbx: str,
    start_f: int,
    log: _Logger,
) -> dict:
    # Archived research helper. No longer used by the production runtime.
    if not bind_reference_fbx or not os.path.isfile(bind_reference_fbx):
        raise RuntimeError(
            "Bind-reference FBX not found for child translate lock: {0}".format(bind_reference_fbx)
        )

    previous_time = cmds.currentTime(query=True)
    bind_namespace = "db_export_v3_bindRef"
    imported_ref_nodes: list[str] = []
    try:
        cmds.currentTime(int(start_f), edit=True)
        cmds.refresh(force=True)
        imported_ref_nodes = _import_bind_reference_fbx(cmds, bind_reference_fbx, bind_namespace)
        bind_joints = cmds.ls(imported_ref_nodes, type="joint", long=True) or []
        bind_translate_map = _capture_bind_local_translate_map(cmds, bind_joints)

        changed_rows: list[dict] = []
        missing_rows: list[str] = []
        deleted_curve_count = 0
        changed_joint_count = 0

        for joint in source_joints:
            if not cmds.objExists(joint):
                continue
            if not _joint_parent(cmds, joint):
                continue
            signature = _dag_signature(joint)
            bind_translate = bind_translate_map.get(signature)
            if bind_translate is None:
                missing_rows.append(signature)
                continue
            translate_curves = _joint_translate_curves(cmds, joint)
            if translate_curves:
                try:
                    cmds.delete(translate_curves)
                except Exception:
                    pass
                deleted_curve_count += len(translate_curves)
            for attr_name, value in zip(("translateX", "translateY", "translateZ"), bind_translate):
                plug = "{0}.{1}".format(joint, attr_name)
                if cmds.objExists(plug):
                    try:
                        cmds.setAttr(plug, lock=False)
                    except Exception:
                        pass
                    cmds.setAttr(plug, float(value))
            changed_joint_count += 1
            if len(changed_rows) < 20:
                changed_rows.append(
                    {
                        "joint": joint,
                        "signature": signature,
                        "bindTranslate": bind_translate,
                        "deletedTranslateCurveCount": len(translate_curves),
                    }
                )

        result = {
            "performed": True,
            "sourceJointCount": int(len(source_joints)),
            "bindReferenceJointCount": int(len(bind_joints)),
            "changedJointCount": int(changed_joint_count),
            "deletedTranslateCurveCount": int(deleted_curve_count),
            "missingBindJointCount": int(len(missing_rows)),
            "missingBindJointSamples": missing_rows[:20],
            "changedJointSamples": changed_rows,
        }
        log.write("clean_export_child_translate_bind_lock: {0}".format(json.dumps(result, ensure_ascii=False, sort_keys=True)))
        return result
    finally:
        if imported_ref_nodes:
            try:
                cmds.delete(imported_ref_nodes)
            except Exception:
                pass
        try:
            if cmds.namespace(exists=bind_namespace):
                cmds.namespace(removeNamespace=bind_namespace, mergeNamespaceWithRoot=False)
        except Exception:
            pass
        try:
            cmds.currentTime(previous_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass


def _visible_mesh_shapes(cmds) -> list[str]:
    rows: list[str] = []
    for shape in cmds.ls(type="mesh", long=True) or []:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
        except Exception:
            continue
        rows.append(shape)
    return rows


def _first_skin_cluster_on_shape(cmds, shape: str) -> str:
    if not shape or not cmds.objExists(shape):
        return ""
    history = cmds.listHistory(shape, pruneDagObjects=True) or []
    skins = cmds.ls(history, type="skinCluster", long=True) or []
    return skins[0] if skins else ""


def _capture_joint_world_map(cmds, joints: list[str]) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for joint in joints:
        signature = _dag_signature(joint)
        values = cmds.xform(joint, query=True, worldSpace=True, translation=True) or []
        if len(values) == 3:
            result[signature] = [float(values[0]), float(values[1]), float(values[2])]
    return result


def _influence_weight_mass_by_signature(cmds, shape: str, skin_cluster: str, joints: list[str]) -> dict[str, float]:
    signatures = {_dag_signature(joint): 0.0 for joint in joints if joint}
    if not shape or not skin_cluster or not cmds.objExists(shape) or not cmds.objExists(skin_cluster):
        return signatures
    try:
        influences = cmds.skinCluster(skin_cluster, query=True, influence=True) or []
    except Exception:
        influences = []
    if not influences:
        return signatures
    influence_signatures = [_dag_signature(joint) for joint in influences]
    vertex_count = int(cmds.polyEvaluate(shape, vertex=True) or 0)
    for index in range(vertex_count):
        component = "{0}.vtx[{1}]".format(shape, index)
        try:
            values = cmds.skinPercent(skin_cluster, component, query=True, value=True) or []
        except Exception:
            continue
        if len(values) != len(influences):
            continue
        for signature, value in zip(influence_signatures, values):
            if signature in signatures:
                signatures[signature] += float(value)
    return signatures


def _weighted_mean_vector(rows: list[list[float]], weights: list[float]) -> list[float]:
    if not rows or not weights or len(rows) != len(weights):
        return []
    total_weight = float(sum(max(0.0, float(weight)) for weight in weights))
    if total_weight <= 1e-8:
        return []
    return [
        float(sum(float(row[index]) * max(0.0, float(weight)) for row, weight in zip(rows, weights)) / total_weight)
        for index in range(3)
    ]


def _weighted_median_scalar(values: list[float], weights: list[float]) -> float | None:
    if not values or not weights or len(values) != len(weights):
        return None
    rows = sorted(
        [(float(value), max(0.0, float(weight))) for value, weight in zip(values, weights)],
        key=lambda item: item[0],
    )
    total_weight = float(sum(weight for _, weight in rows))
    if total_weight <= 1e-8:
        return None
    threshold = total_weight * 0.5
    running = 0.0
    for value, weight in rows:
        running += weight
        if running >= threshold:
            return float(value)
    return float(rows[-1][0])


def _weighted_median_vector(rows: list[list[float]], weights: list[float]) -> list[float]:
    if not rows or not weights or len(rows) != len(weights):
        return []
    out: list[float] = []
    for index in range(3):
        value = _weighted_median_scalar([float(row[index]) for row in rows], weights)
        if value is None:
            return []
        out.append(float(value))
    return out


def _joint_depth(node: str) -> int:
    return int(str(node or "").count("|"))


def _set_joint_translate(cmds, joint: str, values: list[float]) -> None:
    for attr_name, value in zip(("translateX", "translateY", "translateZ"), values):
        plug = "{0}.{1}".format(joint, attr_name)
        if not cmds.objExists(plug):
            continue
        try:
            cmds.setAttr(plug, lock=False)
        except Exception:
            pass
        cmds.setAttr(plug, float(value))


def _world_point_to_parent_local(cmds, parent_joint: str, world_values: list[float]) -> list[float]:
    import maya.api.OpenMaya as om2

    parent_matrix = cmds.xform(parent_joint, query=True, worldSpace=True, matrix=True) or []
    if len(parent_matrix) != 16:
        return [float(world_values[0]), float(world_values[1]), float(world_values[2])]
    parent_inverse = om2.MMatrix(parent_matrix).inverse()
    local_point = om2.MPoint(
        float(world_values[0]),
        float(world_values[1]),
        float(world_values[2]),
        1.0,
    ) * parent_inverse
    return [float(local_point.x), float(local_point.y), float(local_point.z)]


def _apply_space_normalization(
    cmds,
    source_joints: list[str],
    bind_reference_fbx: str,
    start_f: int,
    end_f: int,
    step_f: int,
    unit_scale: float,
    root_motion_method: str,
    log: _Logger,
) -> dict:
    # Archived research helper. No longer used by the production runtime.
    if not bind_reference_fbx or not os.path.isfile(bind_reference_fbx):
        raise RuntimeError(
            "Bind-reference FBX not found for space normalization: {0}".format(bind_reference_fbx)
        )
    valid_source_joints = [joint for joint in source_joints if joint and cmds.objExists(joint)]
    if not valid_source_joints:
        raise RuntimeError("Space normalization requires source joints in the imported raw FBX scene.")
    source_root_joints = _root_joint_nodes(cmds)
    if not source_root_joints:
        raise RuntimeError("Space normalization could not find a root joint in the raw FBX scene.")

    source_mesh_shapes = _visible_mesh_shapes(cmds)
    source_mesh_shape = source_mesh_shapes[0] if source_mesh_shapes else ""
    source_skin_cluster = _first_skin_cluster_on_shape(cmds, source_mesh_shape)

    previous_time = cmds.currentTime(query=True)
    bind_namespace = "db_export_v3_bindRef"
    imported_ref_nodes: list[str] = []
    try:
        cmds.currentTime(int(start_f), edit=True)
        cmds.refresh(force=True)
        imported_ref_nodes = _import_bind_reference_fbx(cmds, bind_reference_fbx, bind_namespace)
        bind_joints = cmds.ls(imported_ref_nodes, type="joint", long=True) or []
        bind_world_map = _capture_joint_world_map(cmds, bind_joints)
        raw_bind_world_map = _capture_joint_world_map(cmds, valid_source_joints)
        if not bind_world_map:
            raise RuntimeError("Space normalization could not capture bind-reference joint world positions.")

        source_joint_by_signature = {_dag_signature(joint): joint for joint in valid_source_joints}
        parent_signature_map = {
            _dag_signature(joint): _dag_signature(_joint_parent(cmds, joint))
            for joint in valid_source_joints
        }
        matched_signatures = [
            signature
            for signature in source_joint_by_signature.keys()
            if signature in bind_world_map and signature in raw_bind_world_map
        ]
        if not matched_signatures:
            raise RuntimeError("Space normalization found no overlapping joints between raw and bind-reference FBX.")

        root_signature = ""
        for root_joint in source_root_joints:
            candidate = _dag_signature(root_joint)
            if candidate in matched_signatures:
                root_signature = candidate
                break
        if not root_signature:
            root_signature = matched_signatures[0]

        frames = list(range(int(start_f), int(end_f) + 1, max(1, int(step_f))))
        raw_world_per_frame: dict[int, dict[str, list[float]]] = {}
        for frame in frames:
            cmds.currentTime(int(frame), edit=True)
            cmds.refresh(force=True)
            frame_rows: dict[str, list[float]] = {}
            for signature in matched_signatures:
                joint = source_joint_by_signature.get(signature) or ""
                values = cmds.xform(joint, query=True, worldSpace=True, translation=True) or []
                if len(values) == 3:
                    frame_rows[signature] = [float(values[0]), float(values[1]), float(values[2])]
            raw_world_per_frame[int(frame)] = frame_rows

        weight_mass_map = _influence_weight_mass_by_signature(
            cmds,
            source_mesh_shape,
            source_skin_cluster,
            valid_source_joints,
        )
        if not any(float(weight_mass_map.get(signature, 0.0) or 0.0) > 0.0 for signature in matched_signatures if signature != root_signature):
            weight_mass_map = {signature: 1.0 for signature in matched_signatures}

        for attr_name in ("translateX", "translateY", "translateZ"):
            try:
                cmds.cutKey(valid_source_joints, attribute=attr_name, time=(int(start_f), int(end_f)))
            except Exception:
                pass

        sorted_source_joints = sorted(valid_source_joints, key=_joint_depth)
        corrected_key_writes = 0
        root_delta_samples: list[dict] = []

        for frame in frames:
            cmds.currentTime(int(frame), edit=True)
            cmds.refresh(force=True)
            frame_map = raw_world_per_frame.get(int(frame)) or {}
            scaled_non_root_deltas: list[list[float]] = []
            scaled_non_root_weights: list[float] = []
            corrected_world_map: dict[str, list[float]] = {}

            for signature in matched_signatures:
                raw_bind_world = raw_bind_world_map.get(signature) or []
                bind_world = bind_world_map.get(signature) or []
                raw_current_world = frame_map.get(signature) or []
                if len(raw_bind_world) != 3 or len(bind_world) != 3 or len(raw_current_world) != 3:
                    continue
                raw_delta = [float(raw_current_world[index] - raw_bind_world[index]) for index in range(3)]
                scaled_delta = [float(component / float(unit_scale)) for component in raw_delta]
                if signature != root_signature:
                    corrected_world_map[signature] = [
                        float(bind_world[index] + scaled_delta[index]) for index in range(3)
                    ]
                    scaled_non_root_deltas.append(scaled_delta)
                    scaled_non_root_weights.append(float(weight_mass_map.get(signature, 0.0) or 0.0))

            method_name = str(root_motion_method or "").strip()
            if method_name == "weighted_non_root_mean_delta":
                root_delta = _weighted_mean_vector(scaled_non_root_deltas, scaled_non_root_weights)
            elif method_name == "static_root":
                root_delta = [0.0, 0.0, 0.0]
            else:
                root_delta = _weighted_median_vector(scaled_non_root_deltas, scaled_non_root_weights)
            if len(root_delta) != 3:
                root_delta = [0.0, 0.0, 0.0]

            root_bind_world = bind_world_map.get(root_signature) or []
            if len(root_bind_world) == 3:
                corrected_world_map[root_signature] = [
                    float(root_bind_world[index] + root_delta[index]) for index in range(3)
                ]
            if len(root_delta_samples) < 24:
                root_delta_samples.append({"frame": int(frame), "rootDelta": [float(v) for v in root_delta]})

            for joint in sorted_source_joints:
                signature = _dag_signature(joint)
                corrected_world = corrected_world_map.get(signature)
                if not corrected_world or len(corrected_world) != 3:
                    continue
                parent_signature = parent_signature_map.get(signature) or ""
                if not parent_signature:
                    local_translate = corrected_world
                else:
                    parent_joint = source_joint_by_signature.get(parent_signature) or ""
                    if not parent_joint or not cmds.objExists(parent_joint):
                        local_translate = corrected_world
                    else:
                        local_translate = _world_point_to_parent_local(cmds, parent_joint, corrected_world)
                _set_joint_translate(cmds, joint, local_translate)
                for attr_name in ("translateX", "translateY", "translateZ"):
                    cmds.setKeyframe(joint, attribute=attr_name, time=(int(frame),))
                corrected_key_writes += 1

        result = {
            "performed": True,
            "unitScale": float(unit_scale),
            "rootMotionMethod": str(root_motion_method or ""),
            "sourceJointCount": int(len(valid_source_joints)),
            "bindReferenceJointCount": int(len(bind_joints)),
            "matchedJointCount": int(len(matched_signatures)),
            "correctedJointKeyWrites": int(corrected_key_writes),
            "rootSignature": root_signature,
            "sourceMeshShape": source_mesh_shape,
            "sourceSkinCluster": source_skin_cluster,
            "sampledRootDeltas": root_delta_samples,
        }
        log.write("clean_export_space_normalization: {0}".format(json.dumps(result, ensure_ascii=False, sort_keys=True)))
        return result
    finally:
        if imported_ref_nodes:
            try:
                cmds.delete(imported_ref_nodes)
            except Exception:
                pass
        try:
            if cmds.namespace(exists=bind_namespace):
                cmds.namespace(removeNamespace=bind_namespace, mergeNamespaceWithRoot=False)
        except Exception:
            pass
        try:
            cmds.currentTime(previous_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass


def _namespaced_dag_nodes(cmds) -> list[str]:
    flagged: list[str] = []
    seen: set[str] = set()
    joints = cmds.ls(type="joint", long=True) or []
    transforms = _mesh_parent_nodes(cmds)
    for node in joints + transforms:
        short_name = node.split("|")[-1]
        if ":" not in short_name or node in seen:
            continue
        seen.add(node)
        flagged.append(node)
    return flagged


def _validate_exported_fbx(mel, cmds, path: str, start_f: int, end_f: int, step_f: int, log: _Logger) -> dict:
    cmds.file(new=True, force=True)
    _ensure_fbx_plugin(cmds)
    new_nodes = _import_fbx(mel, cmds, path, start_f, end_f, step_f, log)
    joints = cmds.ls(type="joint", long=True) or []
    meshes = cmds.ls(type="mesh", long=True) or []
    anim_curves = cmds.ls(
        type=("animCurveTL", "animCurveTA", "animCurveTU", "animCurve"), long=True
    ) or []
    keyed_joints, total_joint_keys = _keyed_joint_count(cmds, joints)
    namespaced_nodes = _namespaced_dag_nodes(cmds)
    mesh_transforms = _mesh_parent_nodes(cmds)
    mesh_visibility = _mesh_visibility_report(cmds)
    errors: list[str] = []
    if not new_nodes:
        errors.append("Validation import produced no new nodes.")
    if not joints:
        errors.append("Validation import produced no joints.")
    if not mesh_transforms:
        errors.append("Validation import produced no mesh transforms.")
    if not anim_curves and keyed_joints <= 0:
        errors.append("Validation import found no animation curves or keyed joints.")
    if namespaced_nodes:
        errors.append("Validation import still has namespaced DAG nodes.")
    result = {
        "newNodeCount": int(len(new_nodes)),
        "jointCount": int(len(joints)),
        "meshCount": int(len(mesh_transforms)),
        "animCurveCount": int(len(anim_curves)),
        "keyedJoints": int(keyed_joints),
        "totalJointKeys": int(total_joint_keys),
        "namespacedDagNodes": namespaced_nodes[:25],
        "meshVisibility": mesh_visibility[:12],
        "errors": errors,
    }
    log.write(
        "clean_export_validation_mesh_visibility: {0}".format(
            json.dumps(mesh_visibility[:12], ensure_ascii=False, sort_keys=True)
        )
    )
    if errors:
        raise RuntimeError("Clean deliverable validation failed: {0}".format(" | ".join(errors)))
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-fbx", required=True)
    parser.add_argument("--dest-fbx", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--step", type=int, required=True)
    parser.add_argument("--log-path", default="")
    parser.add_argument("--node-prefix", default="")
    parser.add_argument("--wrap-world-root", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    log = _Logger(args.log_path)
    try:
        _sanitize_sys_path_for_maya_imports(log)
        import maya.standalone

        maya.standalone.initialize(name="python")
        import maya.cmds as cmds
        import maya.mel as mel

        source_fbx = os.path.abspath(args.source_fbx)
        dest_fbx = os.path.abspath(args.dest_fbx)
        node_prefix = str(args.node_prefix or "").strip()
        Path(dest_fbx).parent.mkdir(parents=True, exist_ok=True)

        log.write("clean_export_source: {0}".format(source_fbx))
        log.write("clean_export_destination: {0}".format(dest_fbx))
        log.write("clean_export_node_prefix: {0}".format(node_prefix or "<none>"))
        log.write("clean_export_wrap_world_root_requested: {0}".format(bool(args.wrap_world_root)))
        _ensure_fbx_plugin(cmds)

        cmds.file(new=True, force=True)
        imported_nodes = _import_fbx(mel, cmds, source_fbx, args.start, args.end, args.step, log)
        log.write("clean_export_imported_node_count: {0}".format(len(imported_nodes)))

        removed_namespaces = _flatten_all_namespaces(cmds, log)
        if removed_namespaces:
            log.write("clean_export_removed_namespaces: {0}".format(", ".join(removed_namespaces)))
        else:
            log.write("clean_export_removed_namespaces: none")

        wrapper_result = _apply_world_root_wrapper(cmds, log) if args.wrap_world_root else {
            "performed": False,
            "reason": "disabled",
            "targetCount": 0,
            "wrapperJoint": "",
        }
        renamed_nodes = _rename_export_nodes(cmds, node_prefix or "export", log)
        log.write(
            "clean_export_mesh_visibility_before_fix: {0}".format(
                json.dumps(_mesh_visibility_report(cmds)[:12], ensure_ascii=False, sort_keys=True)
            )
        )
        visibility_changes = _enforce_export_mesh_visibility(cmds, log)
        log.write(
            "clean_export_mesh_visibility_after_fix: {0}".format(
                json.dumps(_mesh_visibility_report(cmds)[:12], ensure_ascii=False, sort_keys=True)
            )
        )
        export_nodes = _export_nodes(cmds)
        if not export_nodes:
            raise RuntimeError("No exportable joints/mesh transforms found in clean scene.")
        log.write("clean_export_nodes: {0}".format(", ".join(export_nodes[:25])))

        _export_fbx(mel, cmds, dest_fbx, export_nodes, args.start, args.end, args.step)
        validation = _validate_exported_fbx(mel, cmds, dest_fbx, args.start, args.end, args.step, log)

        result = {
            "sourcePath": source_fbx,
            "destinationPath": dest_fbx,
            "removedNamespaces": removed_namespaces,
            "worldRootWrapper": wrapper_result,
            "renamedNodes": renamed_nodes,
            "visibilityChanges": visibility_changes,
            "exportNodes": export_nodes,
            "validation": validation,
        }
        print("RESULT_JSON:{0}".format(json.dumps(result, sort_keys=True)), flush=True)
        return 0
    except Exception as exc:
        log.write("clean_export_failed: {0}".format(exc))
        for line in traceback.format_exc().splitlines():
            log.write("traceback: {0}".format(line))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
