from __future__ import annotations

import maya.api.OpenMaya as om2
import maya.cmds as cmds

from ..core.models import CliRunSettings, ImportValidation, PreparedRun
from ..maya.hierarchy import describe_skin_cluster, describe_skin_weight_distribution
from .fbx_io import find_imported_mesh_shape, import_alembic_result
from .importer import (
    cleanup_imported_nodes,
    cleanup_unwanted_dynamic_nodes,
    fbx_animation_token_probe,
    import_cli_fbx,
    import_cli_fbx_with_method,
    next_namespace,
    validate_imported_result,
)


def safe_attr_get(node: str, attr: str, default=None):
    plug = "{0}.{1}".format(node, attr)
    if not node or not cmds.objExists(plug):
        return default
    try:
        return cmds.getAttr(plug)
    except Exception:
        return default


def node_visibility_state(node: str, *, is_shape: bool) -> dict:
    state = {
        "node": node,
        "exists": bool(node and cmds.objExists(node)),
        "visibility": safe_attr_get(node, "visibility", None),
        "lodVisibility": safe_attr_get(node, "lodVisibility", None),
        "template": safe_attr_get(node, "template", None),
        "hiddenInOutliner": safe_attr_get(node, "hiddenInOutliner", None),
        "overrideEnabled": safe_attr_get(node, "overrideEnabled", None),
        "overrideVisibility": safe_attr_get(node, "overrideVisibility", None),
        "overrideDisplayType": safe_attr_get(node, "overrideDisplayType", None),
    }
    if is_shape:
        state["intermediateObject"] = safe_attr_get(node, "intermediateObject", None)
    else:
        state["inheritsTransform"] = safe_attr_get(node, "inheritsTransform", None)
    return state


def node_transform_snapshot(node: str) -> dict:
    snap = {
        "node": node,
        "translate": [],
        "rotate": [],
        "scale": [],
        "worldMatrix": [],
        "parent": "",
    }
    if not node or not cmds.objExists(node):
        return snap
    try:
        snap["translate"] = [float(v) for v in (cmds.xform(node, query=True, worldSpace=True, translation=True) or [])]
    except Exception:
        snap["translate"] = []
    try:
        snap["rotate"] = [float(v) for v in (cmds.xform(node, query=True, worldSpace=True, rotation=True) or [])]
    except Exception:
        snap["rotate"] = []
    try:
        snap["scale"] = [float(cmds.getAttr(node + ".scale" + axis)) for axis in ("X", "Y", "Z")]
    except Exception:
        snap["scale"] = []
    try:
        snap["worldMatrix"] = [float(v) for v in (cmds.xform(node, query=True, worldSpace=True, matrix=True) or [])]
    except Exception:
        snap["worldMatrix"] = []
    try:
        parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
        snap["parent"] = parents[0] if parents else ""
    except Exception:
        snap["parent"] = ""
    return snap


def shape_bbox_snapshot(shape: str) -> dict:
    snap = {
        "shape": shape,
        "worldBoundingBox": [],
        "worldSize": [],
        "worldCenter": [],
    }
    if not shape or not cmds.objExists(shape):
        return snap
    try:
        bbox = [float(v) for v in (cmds.exactWorldBoundingBox(shape) or [])]
    except Exception:
        bbox = []
    snap["worldBoundingBox"] = bbox
    if len(bbox) == 6:
        snap["worldSize"] = [bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]]
        snap["worldCenter"] = [
            (bbox[0] + bbox[3]) * 0.5,
            (bbox[1] + bbox[4]) * 0.5,
            (bbox[2] + bbox[5]) * 0.5,
        ]
    return snap


def root_relationship_snapshot(transform: str, shape: str, root_snapshot: dict) -> dict:
    relation = {
        "transform": transform,
        "shape": shape,
        "meshCenterMinusRootTranslate": [],
        "meshTransformMinusRootTranslate": [],
        "meshCenterDistanceFromRoot": None,
        "meshTransformDistanceFromRoot": None,
        "meshTransformSnapshot": node_transform_snapshot(transform),
        "meshBBox": shape_bbox_snapshot(shape),
    }
    root_translate = root_snapshot.get("translate") or []
    mesh_center = relation["meshBBox"].get("worldCenter") or []
    mesh_translate = relation["meshTransformSnapshot"].get("translate") or []
    if len(root_translate) == 3 and len(mesh_center) == 3:
        center_delta = [float(mesh_center[i] - root_translate[i]) for i in range(3)]
        relation["meshCenterMinusRootTranslate"] = center_delta
        relation["meshCenterDistanceFromRoot"] = float(
            (center_delta[0] ** 2 + center_delta[1] ** 2 + center_delta[2] ** 2) ** 0.5
        )
    if len(root_translate) == 3 and len(mesh_translate) == 3:
        transform_delta = [float(mesh_translate[i] - root_translate[i]) for i in range(3)]
        relation["meshTransformMinusRootTranslate"] = transform_delta
        relation["meshTransformDistanceFromRoot"] = float(
            (transform_delta[0] ** 2 + transform_delta[1] ** 2 + transform_delta[2] ** 2) ** 0.5
        )
    return relation


def mesh_points(shape: str, *, world_space: bool) -> list[om2.MPoint]:
    if not shape or not cmds.objExists(shape):
        return []
    selection = om2.MSelectionList()
    selection.add(shape)
    dag_path = selection.getDagPath(0)
    mesh_fn = om2.MFnMesh(dag_path)
    space = om2.MSpace.kWorld if world_space else om2.MSpace.kObject
    return list(mesh_fn.getPoints(space))


def compare_mesh_points(shape_a: str, shape_b: str) -> dict:
    def _stats(points_a: list[om2.MPoint], points_b: list[om2.MPoint]) -> dict:
        if not points_a or not points_b or len(points_a) != len(points_b):
            return {
                "countA": int(len(points_a)),
                "countB": int(len(points_b)),
                "comparable": False,
                "maxDistance": None,
                "meanDistance": None,
                "rmsDistance": None,
                "worstVertexIndices": [],
            }
        distances: list[tuple[float, int]] = []
        total = 0.0
        total_sq = 0.0
        for index, (point_a, point_b) in enumerate(zip(points_a, points_b)):
            delta = point_a - point_b
            distance = float((delta.x * delta.x + delta.y * delta.y + delta.z * delta.z) ** 0.5)
            distances.append((distance, index))
            total += distance
            total_sq += distance * distance
        distances.sort(reverse=True)
        count = len(distances)
        return {
            "countA": int(count),
            "countB": int(count),
            "comparable": True,
            "maxDistance": float(distances[0][0]) if distances else 0.0,
            "meanDistance": float(total / count) if count else 0.0,
            "rmsDistance": float((total_sq / count) ** 0.5) if count else 0.0,
            "worstVertexIndices": [
                {"index": int(index), "distance": float(distance)}
                for distance, index in distances[:10]
            ],
        }

    return {
        "world": _stats(mesh_points(shape_a, world_space=True), mesh_points(shape_b, world_space=True)),
        "object": _stats(mesh_points(shape_a, world_space=False), mesh_points(shape_b, world_space=False)),
    }


def compare_mesh_animation(
    source_shape: str,
    result_shape: str,
    start_f: int,
    end_f: int,
    step_f: int,
    *,
    max_samples: int = 601,
    top_limit: int = 10,
) -> dict:
    if not source_shape or not cmds.objExists(source_shape) or not result_shape or not cmds.objExists(result_shape):
        return {
            "exists": False,
            "sourceShape": source_shape,
            "resultShape": result_shape,
            "reason": "missing_mesh_shape",
        }

    frames = sample_frames(start_f, end_f, step_f, max_samples=max_samples)
    source_vertex_count = int(cmds.polyEvaluate(source_shape, vertex=True) or 0)
    result_vertex_count = int(cmds.polyEvaluate(result_shape, vertex=True) or 0)
    original_time = cmds.currentTime(query=True)

    world_rows: list[dict] = []
    object_rows: list[dict] = []
    global_max = {
        "frame": None,
        "distance": 0.0,
        "vertexIndex": None,
    }

    try:
        for frame in frames:
            cmds.currentTime(int(frame), edit=True)
            cmds.refresh(force=True)
            compare = compare_mesh_points(source_shape, result_shape)
            world_stats = (compare.get("world") or {})
            object_stats = (compare.get("object") or {})
            world_rows.append(
                {
                    "frame": int(frame),
                    "maxDistance": world_stats.get("maxDistance"),
                    "meanDistance": world_stats.get("meanDistance"),
                    "rmsDistance": world_stats.get("rmsDistance"),
                    "worstVertexIndices": world_stats.get("worstVertexIndices") or [],
                }
            )
            object_rows.append(
                {
                    "frame": int(frame),
                    "maxDistance": object_stats.get("maxDistance"),
                    "meanDistance": object_stats.get("meanDistance"),
                    "rmsDistance": object_stats.get("rmsDistance"),
                    "worstVertexIndices": object_stats.get("worstVertexIndices") or [],
                }
            )

            if bool(world_stats.get("comparable")):
                frame_max = float(world_stats.get("maxDistance") or 0.0)
                if frame_max > float(global_max.get("distance") or 0.0):
                    worst_vertices = world_stats.get("worstVertexIndices") or []
                    global_max = {
                        "frame": int(frame),
                        "distance": float(frame_max),
                        "vertexIndex": (
                            int(worst_vertices[0].get("index"))
                            if worst_vertices and worst_vertices[0].get("index") is not None
                            else None
                        ),
                    }
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass

    comparable_world_rows = [row for row in world_rows if row.get("maxDistance") is not None]
    comparable_object_rows = [row for row in object_rows if row.get("maxDistance") is not None]

    def _average(rows: list[dict], key: str):
        values = [float(row.get(key) or 0.0) for row in rows if row.get(key) is not None]
        if not values:
            return None
        return float(sum(values) / len(values))

    def _top(rows: list[dict], key: str) -> list[dict]:
        ordered = sorted(rows, key=lambda row: float(row.get(key) or 0.0), reverse=True)
        return ordered[: max(1, int(top_limit))]

    return {
        "exists": True,
        "sourceShape": source_shape,
        "resultShape": result_shape,
        "sourceVertexCount": int(source_vertex_count),
        "resultVertexCount": int(result_vertex_count),
        "sampledFrameCount": int(len(frames)),
        "frameStart": int(frames[0]) if frames else int(start_f),
        "frameEnd": int(frames[-1]) if frames else int(end_f),
        "frameStep": max(1, int(step_f)),
        "usedFrameSampling": bool(len(frames) < len(range(int(start_f), int(end_f) + 1, max(1, int(step_f))))),
        "world": {
            "averageMeanDistance": _average(comparable_world_rows, "meanDistance"),
            "averageRmsDistance": _average(comparable_world_rows, "rmsDistance"),
            "globalMaxDistance": float(global_max.get("distance") or 0.0),
            "globalMaxDistanceFrame": global_max.get("frame"),
            "globalMaxDistanceVertexIndex": global_max.get("vertexIndex"),
            "topFramesByRmsDistance": _top(comparable_world_rows, "rmsDistance"),
        },
        "object": {
            "averageMeanDistance": _average(comparable_object_rows, "meanDistance"),
            "averageRmsDistance": _average(comparable_object_rows, "rmsDistance"),
            "topFramesByRmsDistance": _top(comparable_object_rows, "rmsDistance"),
        },
    }


def evaluate_fixed_contract_pair_probe(pair_probe: dict) -> dict:
    object_stats = ((pair_probe.get("meshPointComparison") or {}).get("object") or {})
    world_stats = ((pair_probe.get("meshPointComparison") or {}).get("world") or {})
    abc_transform = (pair_probe.get("abcProbe") or {}).get("chosenMeshTransformSnapshot") or {}
    abc_translate = abc_transform.get("translate") or []
    abc_rotate = abc_transform.get("rotate") or []
    abc_scale = abc_transform.get("scale") or []
    issues: list[str] = []
    obj_max = object_stats.get("maxDistance")
    obj_rms = object_stats.get("rmsDistance")
    world_max = world_stats.get("maxDistance")
    if object_stats.get("comparable"):
        if obj_max is not None and float(obj_max) > 0.01:
            issues.append("object_max_distance_above_threshold")
        if obj_rms is not None and float(obj_rms) > 0.001:
            issues.append("object_rms_distance_above_threshold")
    else:
        issues.append("object_pair_not_comparable")
    if (
        object_stats.get("comparable")
        and world_stats.get("comparable")
        and obj_rms is not None
        and world_max is not None
        and float(obj_rms) > 0.001
        and float(world_max) <= 0.01
    ):
        issues.append("world_object_transform_compensation_mismatch")
    if len(abc_translate) == 3 and any(abs(float(v)) > 1e-4 for v in abc_translate):
        issues.append("abc_transform_translate_not_identity")
    if len(abc_rotate) == 3 and any(abs(float(v)) > 1e-4 for v in abc_rotate):
        issues.append("abc_transform_rotate_not_identity")
    if len(abc_scale) == 3 and any(abs(float(v) - 1.0) > 1e-4 for v in abc_scale):
        issues.append("abc_transform_scale_not_identity")
    return {
        "issues": issues,
        "objectStats": object_stats,
        "worldStats": world_stats,
        "abcTransform": abc_transform,
        "success": not issues,
    }


def joint_root_candidates(joints: list[str]) -> list[str]:
    joint_set = set(joints or [])
    roots: list[str] = []
    for joint in joints or []:
        parent = cmds.listRelatives(joint, parent=True, fullPath=True, type="joint") or []
        if not parent or parent[0] not in joint_set:
            roots.append(joint)
    return sorted(roots)


def summarize_imported_fbx_contract_nodes(
    new_nodes: list[str],
    expected_vertex_count: int,
    *,
    path: str = "",
    probe_namespace: str = "",
    frame: int = 0,
) -> dict:
    joints = cmds.ls(new_nodes, type="joint", long=True) or []
    meshes = []
    for shape in cmds.ls(new_nodes, type="mesh", long=True) or []:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
        except Exception:
            pass
        meshes.append(shape)

    chosen_mesh = None
    chosen_transform = ""
    chosen_bbox = {}
    chosen_relation = {}
    chosen_visibility = {}
    chosen_joint_contract = {}
    chosen_skin_bind_probe = {}
    mesh_visibility_sample: list[dict] = []
    best_score = (-1, -1)
    for shape in meshes:
        try:
            vtx = int(cmds.polyEvaluate(shape, vertex=True) or 0)
        except Exception:
            vtx = -1
        transform = ""
        try:
            parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
            transform = parents[0] if parents else ""
        except Exception:
            transform = ""
        mesh_visibility_sample.append(
            {
                "shape": shape,
                "transform": transform,
                "shapeState": node_visibility_state(shape, is_shape=True),
                "transformState": node_visibility_state(transform, is_shape=False) if transform else {},
            }
        )
        score = (1 if vtx == int(expected_vertex_count) else 0, vtx)
        if score > best_score:
            best_score = score
            chosen_mesh = shape
            chosen_transform = transform
            chosen_bbox = shape_bbox_snapshot(shape)
            chosen_visibility = mesh_visibility_sample[-1]

    root_candidates = joint_root_candidates(joints)
    chosen_root = root_candidates[0] if root_candidates else ""
    root_snapshot = node_transform_snapshot(chosen_root)
    if chosen_mesh and chosen_transform and chosen_root:
        chosen_relation = root_relationship_snapshot(chosen_transform, chosen_mesh, root_snapshot)
        chosen_joint_contract = joint_contract_preflight(joints, chosen_mesh, chosen_root)
        skin_info = describe_skin_cluster(chosen_mesh, joints)
        chosen_skin_bind_probe = skin_bind_matrix_probe(
            str(skin_info.get("skinCluster") or ""),
            list(skin_info.get("influences") or []),
            root_joint=chosen_root,
        )

    return {
        "path": path,
        "probeNamespace": probe_namespace,
        "frame": int(frame),
        "jointCount": int(len(joints)),
        "meshCount": int(len(meshes)),
        "rootJointCandidates": root_candidates[:12],
        "chosenRootJoint": chosen_root,
        "chosenRootSnapshot": root_snapshot,
        "chosenMeshShape": chosen_mesh or "",
        "chosenMeshTransform": chosen_transform,
        "chosenMeshBBox": chosen_bbox,
        "chosenMeshVsRoot": chosen_relation,
        "chosenMeshVisibility": chosen_visibility,
        "jointContractProbe": chosen_joint_contract,
        "skinBindMatrixProbe": chosen_skin_bind_probe,
        "meshVisibilitySample": mesh_visibility_sample[:12],
    }


def probe_imported_fbx_contract(path: str, frame: int, expected_vertex_count: int) -> dict:
    probe_namespace = next_namespace("db_export_v3_contractProbe")
    new_nodes: list[str] = []
    try:
        new_nodes, _, _ = import_cli_fbx_with_method(
            path,
            probe_namespace,
            frame,
            frame,
            1,
            None,
            "mel_fbximport",
        )
        return summarize_imported_fbx_contract_nodes(
            new_nodes,
            expected_vertex_count,
            path=path,
            probe_namespace=probe_namespace,
            frame=frame,
        )
    finally:
        cleanup_imported_nodes(new_nodes, probe_namespace)


def probe_fixed_contract_pair(
    init_fbx: str,
    anim_abc: str,
    frame: int,
    end_frame: int,
    expected_vertex_count: int,
) -> dict:
    init_namespace = next_namespace("db_export_v3_pairInit")
    abc_namespace = next_namespace("db_export_v3_pairAbc")
    init_nodes: list[str] = []
    abc_nodes: list[str] = []
    original_time = cmds.currentTime(query=True)
    try:
        cmds.currentTime(frame, edit=True)
        cmds.refresh(force=True)
        init_nodes, _, _ = import_cli_fbx_with_method(
            init_fbx,
            init_namespace,
            frame,
            frame,
            1,
            None,
            "mel_fbximport",
        )
        init_probe = summarize_imported_fbx_contract_nodes(
            init_nodes,
            expected_vertex_count,
            path=init_fbx,
            probe_namespace=init_namespace,
            frame=frame,
        )

        abc_nodes = import_alembic_result(anim_abc, abc_namespace)
        abc_shape, candidates = find_imported_mesh_shape(
            abc_nodes,
            expected_vertex_count,
            frame,
            end_frame,
        )
        abc_transform = ""
        if abc_shape:
            parents = cmds.listRelatives(abc_shape, parent=True, fullPath=True) or []
            abc_transform = parents[0] if parents else ""
        abc_probe = {
            "path": anim_abc,
            "probeNamespace": abc_namespace,
            "frame": int(frame),
            "candidateCount": int(len(candidates)),
            "chosenMeshShape": abc_shape or "",
            "chosenMeshTransform": abc_transform,
            "chosenMeshBBox": shape_bbox_snapshot(abc_shape) if abc_shape else {},
            "chosenMeshTransformSnapshot": (
                node_transform_snapshot(abc_transform) if abc_transform else {}
            ),
        }
        comparison = {}
        if init_probe.get("chosenMeshShape") and abc_shape:
            comparison = compare_mesh_points(
                str(init_probe.get("chosenMeshShape") or ""),
                abc_shape,
            )
        return {
            "frame": int(frame),
            "initFbxProbe": init_probe,
            "abcProbe": abc_probe,
            "meshPointComparison": comparison,
        }
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass
        cleanup_imported_nodes(abc_nodes, abc_namespace)
        cleanup_imported_nodes(init_nodes, init_namespace)


def compare_contract_probes(baseline: dict, candidate: dict) -> dict:
    baseline_root = (baseline.get("chosenRootSnapshot") or {}).get("translate") or []
    candidate_root = (candidate.get("chosenRootSnapshot") or {}).get("translate") or []
    baseline_center = ((baseline.get("chosenMeshBBox") or {}).get("worldCenter") or [])
    candidate_center = ((candidate.get("chosenMeshBBox") or {}).get("worldCenter") or [])
    baseline_size = ((baseline.get("chosenMeshBBox") or {}).get("worldSize") or [])
    candidate_size = ((candidate.get("chosenMeshBBox") or {}).get("worldSize") or [])
    baseline_relation = baseline.get("chosenMeshVsRoot") or {}
    candidate_relation = candidate.get("chosenMeshVsRoot") or {}
    out = {
        "baselineRootTranslate": baseline_root,
        "candidateRootTranslate": candidate_root,
        "baselineMeshCenter": baseline_center,
        "candidateMeshCenter": candidate_center,
        "baselineMeshSize": baseline_size,
        "candidateMeshSize": candidate_size,
        "rootTranslateDelta": [],
        "meshCenterDelta": [],
        "meshTransformMinusRootDelta": [],
        "meshCenterMagnitudeRatio": None,
        "rootTranslateMagnitudeRatio": None,
        "meshCenterDistanceFromRootRatio": None,
        "meshSizeRatio": [],
    }
    if len(baseline_root) == 3 and len(candidate_root) == 3:
        out["rootTranslateDelta"] = [
            float(candidate_root[index] - baseline_root[index]) for index in range(3)
        ]
    if len(baseline_center) == 3 and len(candidate_center) == 3:
        out["meshCenterDelta"] = [
            float(candidate_center[index] - baseline_center[index]) for index in range(3)
        ]
    baseline_transform_root = baseline_relation.get("meshTransformMinusRootTranslate") or []
    candidate_transform_root = candidate_relation.get("meshTransformMinusRootTranslate") or []
    if len(baseline_transform_root) == 3 and len(candidate_transform_root) == 3:
        out["meshTransformMinusRootDelta"] = [
            float(candidate_transform_root[index] - baseline_transform_root[index])
            for index in range(3)
        ]
    baseline_center_mag = vector_length3(baseline_center)
    candidate_center_mag = vector_length3(candidate_center)
    baseline_root_mag = vector_length3(baseline_root)
    candidate_root_mag = vector_length3(candidate_root)
    out["meshCenterMagnitudeRatio"] = safe_ratio(candidate_center_mag, baseline_center_mag)
    out["rootTranslateMagnitudeRatio"] = safe_ratio(candidate_root_mag, baseline_root_mag)
    baseline_distance = baseline_relation.get("meshCenterDistanceFromRoot")
    candidate_distance = candidate_relation.get("meshCenterDistanceFromRoot")
    if baseline_distance is not None and candidate_distance is not None:
        out["meshCenterDistanceFromRootRatio"] = safe_ratio(
            float(candidate_distance),
            float(baseline_distance),
        )
    if len(baseline_size) == 3 and len(candidate_size) == 3:
        out["meshSizeRatio"] = [
            safe_ratio(float(candidate_size[index]), float(baseline_size[index]))
            for index in range(3)
        ]
    return out


def motion_ratio(source_motion_probe: dict[int, float], result_motion_max: float) -> float | None:
    source_motion_max = max(source_motion_probe.values()) if source_motion_probe else 0.0
    if abs(source_motion_max) <= 1e-8:
        return None
    return float(result_motion_max / source_motion_max)


def safe_ratio(numerator: float, denominator: float, *, eps: float = 1e-8) -> float | None:
    if abs(float(denominator)) <= eps:
        return None
    return float(numerator / denominator)


def vector_length3(values) -> float:
    if not values or len(values) != 3:
        return 0.0
    return float((float(values[0]) ** 2 + float(values[1]) ** 2 + float(values[2]) ** 2) ** 0.5)


def vector_component_ratios(current_values, bind_values, *, eps: float = 1e-8) -> list[float | None]:
    if not current_values or not bind_values or len(current_values) != 3 or len(bind_values) != 3:
        return []
    return [
        safe_ratio(float(current_values[index]), float(bind_values[index]), eps=eps)
        for index in range(3)
    ]


def vector_abs_component_ratio_median(current_values, bind_values, *, eps: float = 1e-8) -> float | None:
    ratios = [
        abs(float(value))
        for value in vector_component_ratios(current_values, bind_values, eps=eps)
        if value is not None
    ]
    if not ratios:
        return None
    ratios.sort()
    middle = len(ratios) // 2
    if len(ratios) % 2:
        return float(ratios[middle])
    return float((ratios[middle - 1] + ratios[middle]) * 0.5)


def _median_float(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) * 0.5)


def vector_delta3(a_values, b_values) -> list[float]:
    if not a_values or not b_values or len(a_values) != 3 or len(b_values) != 3:
        return []
    return [
        float(a_values[0] - b_values[0]),
        float(a_values[1] - b_values[1]),
        float(a_values[2] - b_values[2]),
    ]


def safe_vector_attr(node: str, attr: str) -> list[float]:
    plug = "{0}.{1}".format(node, attr)
    if not node or not cmds.objExists(plug):
        return []
    try:
        value = cmds.getAttr(plug)
    except Exception:
        return []
    if not value:
        return []
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], (list, tuple)):
        value = value[0]
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return []
    return [float(value[0]), float(value[1]), float(value[2])]


def safe_matrix_attr(node: str, attr: str) -> list[float]:
    plug = "{0}.{1}".format(node, attr)
    if not node or not cmds.objExists(plug):
        return []
    try:
        value = cmds.getAttr(plug)
    except Exception:
        return []
    if not value:
        return []
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], (list, tuple)):
        value = value[0]
    if not isinstance(value, (list, tuple)) or len(value) != 16:
        return []
    return [float(item) for item in value]


def rotate_order_name(index: int | None) -> str:
    mapping = {
        0: "xyz",
        1: "yzx",
        2: "zxy",
        3: "xzy",
        4: "yxz",
        5: "zyx",
    }
    return mapping.get(int(index or 0), "unknown")


def matrix_is_identity(values, *, tolerance: float = 1e-6) -> bool:
    if not values or len(values) != 16:
        return False
    identity = [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    return all(abs(float(values[index]) - identity[index]) <= float(tolerance) for index in range(16))


def matrix_translation(values) -> list[float]:
    if not values or len(values) != 16:
        return []
    try:
        transform = om2.MTransformationMatrix(om2.MMatrix(values))
        translation = transform.translation(om2.MSpace.kWorld)
        return [float(translation.x), float(translation.y), float(translation.z)]
    except Exception:
        return [float(values[12]), float(values[13]), float(values[14])]


def matrix_scale(values) -> list[float]:
    if not values or len(values) != 16:
        return []
    try:
        transform = om2.MTransformationMatrix(om2.MMatrix(values))
        scale_values = transform.scale(om2.MSpace.kTransform)
        return [float(scale_values[0]), float(scale_values[1]), float(scale_values[2])]
    except Exception:
        return []


def points_bbox(points: list[list[float]]) -> dict:
    if not points:
        return {
            "worldBoundingBox": [],
            "worldCenter": [],
            "worldSize": [],
        }
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    zs = [float(point[2]) for point in points]
    bbox = [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]
    return {
        "worldBoundingBox": bbox,
        "worldCenter": [
            float((bbox[0] + bbox[3]) * 0.5),
            float((bbox[1] + bbox[4]) * 0.5),
            float((bbox[2] + bbox[5]) * 0.5),
        ],
        "worldSize": [
            float(bbox[3] - bbox[0]),
            float(bbox[4] - bbox[1]),
            float(bbox[5] - bbox[2]),
        ],
    }


def point_outside_bbox_distance(point, bbox_values) -> float:
    if not point or len(point) != 3 or not bbox_values or len(bbox_values) != 6:
        return 0.0
    x, y, z = [float(v) for v in point]
    min_x, min_y, min_z, max_x, max_y, max_z = [float(v) for v in bbox_values]
    dx = 0.0 if min_x <= x <= max_x else (min_x - x if x < min_x else x - max_x)
    dy = 0.0 if min_y <= y <= max_y else (min_y - y if y < min_y else y - max_y)
    dz = 0.0 if min_z <= z <= max_z else (min_z - z if z < min_z else z - max_z)
    return float((dx * dx + dy * dy + dz * dz) ** 0.5)


def joint_contract_preflight(
    joints: list[str],
    mesh_shape: str,
    root_joint: str,
    *,
    top_limit: int = 15,
) -> dict:
    valid_joints = [joint for joint in joints if joint and cmds.objExists(joint)]
    if not valid_joints or not mesh_shape or not cmds.objExists(mesh_shape):
        return {
            "exists": False,
            "jointCount": int(len(valid_joints)),
            "meshShape": mesh_shape,
            "rootJoint": root_joint,
        }

    mesh_bbox = shape_bbox_snapshot(mesh_shape)
    mesh_center = mesh_bbox.get("worldCenter") or []
    mesh_world_bbox = mesh_bbox.get("worldBoundingBox") or []
    mesh_diagonal = vector_length3(mesh_bbox.get("worldSize") or [])

    joint_set = set(valid_joints)
    rows: list[dict] = []
    world_points: list[list[float]] = []
    non_zero_joint_orient = 0
    non_zero_rotate_axis = 0
    segment_scale_compensate_off = 0
    non_unit_scale = 0
    non_identity_offset_parent_matrix = 0
    inherits_transform_off = 0

    parent_map: dict[str, str] = {}
    world_position_map: dict[str, list[float]] = {}
    for joint in valid_joints:
        parent = cmds.listRelatives(joint, parent=True, fullPath=True, type="joint") or []
        parent_map[joint] = str(parent[0]) if parent and parent[0] in joint_set else ""
        world_position = list(cmds.xform(joint, query=True, worldSpace=True, translation=True))
        world_position_map[joint] = world_position
        world_points.append(world_position)

    for joint in valid_joints:
        parent = parent_map.get(joint) or ""
        local_translate = safe_vector_attr(joint, "translate")
        local_rotate = safe_vector_attr(joint, "rotate")
        local_scale = safe_vector_attr(joint, "scale")
        joint_orient = safe_vector_attr(joint, "jointOrient")
        rotate_axis = safe_vector_attr(joint, "rotateAxis")
        rotate_order_index = safe_attr_get(joint, "rotateOrder", 0)
        segment_scale_compensate = bool(safe_attr_get(joint, "segmentScaleCompensate", True))
        inherits_transform = bool(safe_attr_get(joint, "inheritsTransform", True))
        offset_parent_matrix = safe_matrix_attr(joint, "offsetParentMatrix")
        offset_parent_identity = matrix_is_identity(offset_parent_matrix)
        world_position = world_position_map.get(joint) or []
        parent_scale = safe_vector_attr(parent, "scale") if parent else []

        bind_bone_length = 0.0
        if parent and parent in world_position_map:
            bind_bone_length = vector_length3(
                vector_delta3(world_position, world_position_map.get(parent) or [])
            )

        distance_from_mesh_bbox = point_outside_bbox_distance(world_position, mesh_world_bbox)
        distance_from_mesh_center = (
            vector_length3(vector_delta3(world_position, mesh_center)) if len(mesh_center) == 3 else 0.0
        )
        local_translate_magnitude = vector_length3(local_translate)
        parent_scale_magnitude = vector_length3(parent_scale)

        if any(abs(float(v)) > 1e-6 for v in joint_orient):
            non_zero_joint_orient += 1
        if any(abs(float(v)) > 1e-6 for v in rotate_axis):
            non_zero_rotate_axis += 1
        if not segment_scale_compensate:
            segment_scale_compensate_off += 1
        if local_scale and any(abs(float(v) - 1.0) > 1e-6 for v in local_scale):
            non_unit_scale += 1
        if offset_parent_matrix and not offset_parent_identity:
            non_identity_offset_parent_matrix += 1
        if not inherits_transform:
            inherits_transform_off += 1

        suspicious_flags: list[str] = []
        if any(abs(float(v)) > 1e-6 for v in joint_orient):
            suspicious_flags.append("jointOrient_non_zero")
        if any(abs(float(v)) > 1e-6 for v in rotate_axis):
            suspicious_flags.append("rotateAxis_non_zero")
        if local_scale and any(abs(float(v) - 1.0) > 1e-6 for v in local_scale):
            suspicious_flags.append("scale_non_unit")
        if not segment_scale_compensate:
            suspicious_flags.append("segmentScaleCompensate_off")
        if not inherits_transform:
            suspicious_flags.append("inheritsTransform_off")
        if offset_parent_matrix and not offset_parent_identity:
            suspicious_flags.append("offsetParentMatrix_non_identity")

        rows.append(
            {
                "joint": joint,
                "parent": parent,
                "worldPosition": world_position,
                "bindLocalTranslate": local_translate,
                "bindLocalRotate": local_rotate,
                "bindLocalScale": local_scale,
                "jointOrient": joint_orient,
                "rotateAxis": rotate_axis,
                "rotateOrder": rotate_order_name(rotate_order_index),
                "segmentScaleCompensate": segment_scale_compensate,
                "inheritsTransform": inherits_transform,
                "offsetParentMatrixIsIdentity": offset_parent_identity,
                "parentLocalScale": parent_scale,
                "bindBoneLength": float(bind_bone_length),
                "bindLocalTranslateMagnitude": float(local_translate_magnitude),
                "distanceFromMeshBBox": float(distance_from_mesh_bbox),
                "distanceFromMeshCenter": float(distance_from_mesh_center),
                "parentScaleMagnitude": float(parent_scale_magnitude),
                "suspiciousFlags": suspicious_flags,
            }
        )

    skeleton_bbox = points_bbox(world_points)
    skeleton_center = skeleton_bbox.get("worldCenter") or []
    mesh_to_skeleton_center_delta = vector_delta3(mesh_center, skeleton_center)
    direct_children = [row for row in rows if row.get("parent") == root_joint]
    child_rows = [row for row in rows if row.get("joint") != root_joint]

    def _top(items: list[dict], key: str) -> list[dict]:
        sorted_items = sorted(items, key=lambda item: float(item.get(key) or 0.0), reverse=True)
        return sorted_items[: max(1, int(top_limit))]

    suspicious_rows = [
        {
            "joint": row.get("joint"),
            "parent": row.get("parent"),
            "suspiciousFlags": row.get("suspiciousFlags") or [],
            "bindLocalTranslate": row.get("bindLocalTranslate") or [],
            "jointOrient": row.get("jointOrient") or [],
            "rotateAxis": row.get("rotateAxis") or [],
            "bindLocalScale": row.get("bindLocalScale") or [],
            "segmentScaleCompensate": row.get("segmentScaleCompensate"),
            "inheritsTransform": row.get("inheritsTransform"),
            "offsetParentMatrixIsIdentity": row.get("offsetParentMatrixIsIdentity"),
            "distanceFromMeshBBox": float(row.get("distanceFromMeshBBox") or 0.0),
            "bindBoneLength": float(row.get("bindBoneLength") or 0.0),
        }
        for row in rows
        if row.get("suspiciousFlags")
    ]
    suspicious_rows.sort(
        key=lambda item: (
            len(item.get("suspiciousFlags") or []),
            float(item.get("distanceFromMeshBBox") or 0.0),
            float(item.get("bindBoneLength") or 0.0),
        ),
        reverse=True,
    )

    return {
        "exists": True,
        "meshShape": mesh_shape,
        "rootJoint": root_joint,
        "jointCount": int(len(valid_joints)),
        "rootDirectChildCount": int(len(direct_children)),
        "rootDirectChildRatio": (
            float(len(direct_children) / max(1, len(valid_joints) - 1)) if len(valid_joints) > 1 else 0.0
        ),
        "meshBBox": mesh_bbox,
        "skeletonBBox": skeleton_bbox,
        "meshToSkeletonCenterDelta": mesh_to_skeleton_center_delta,
        "meshDiagonal": float(mesh_diagonal),
        "rootSnapshot": node_transform_snapshot(root_joint),
        "contractFlags": {
            "nonZeroJointOrientCount": int(non_zero_joint_orient),
            "nonZeroRotateAxisCount": int(non_zero_rotate_axis),
            "segmentScaleCompensateOffCount": int(segment_scale_compensate_off),
            "nonUnitScaleCount": int(non_unit_scale),
            "nonIdentityOffsetParentMatrixCount": int(non_identity_offset_parent_matrix),
            "inheritsTransformOffCount": int(inherits_transform_off),
        },
        "maxChildBindLocalTranslateMagnitude": float(
            max((float(row.get("bindLocalTranslateMagnitude") or 0.0) for row in child_rows), default=0.0)
        ),
        "maxChildDistanceFromMeshBBox": float(
            max((float(row.get("distanceFromMeshBBox") or 0.0) for row in child_rows), default=0.0)
        ),
        "maxChildBindBoneLength": float(
            max((float(row.get("bindBoneLength") or 0.0) for row in child_rows), default=0.0)
        ),
        "maxChildBindBoneLengthToMeshDiagonalRatio": (
            float(
                max((float(row.get("bindBoneLength") or 0.0) for row in child_rows), default=0.0)
                / mesh_diagonal
            )
            if mesh_diagonal > 1e-8
            else None
        ),
        "topBindLocalTranslateMagnitudeJoints": [
            {
                "joint": row.get("joint"),
                "parent": row.get("parent"),
                "bindLocalTranslateMagnitude": float(row.get("bindLocalTranslateMagnitude") or 0.0),
                "bindLocalTranslate": row.get("bindLocalTranslate") or [],
                "distanceFromMeshBBox": float(row.get("distanceFromMeshBBox") or 0.0),
            }
            for row in _top(child_rows, "bindLocalTranslateMagnitude")
        ],
        "topDistanceFromMeshBBoxJoints": [
            {
                "joint": row.get("joint"),
                "parent": row.get("parent"),
                "distanceFromMeshBBox": float(row.get("distanceFromMeshBBox") or 0.0),
                "worldPosition": row.get("worldPosition") or [],
                "bindLocalTranslate": row.get("bindLocalTranslate") or [],
            }
            for row in _top(child_rows, "distanceFromMeshBBox")
        ],
        "topBindBoneLengthJoints": [
            {
                "joint": row.get("joint"),
                "parent": row.get("parent"),
                "bindBoneLength": float(row.get("bindBoneLength") or 0.0),
                "distanceFromMeshBBox": float(row.get("distanceFromMeshBBox") or 0.0),
            }
            for row in _top(child_rows, "bindBoneLength")
        ],
        "topParentScaleMagnitudeJoints": [
            {
                "joint": row.get("joint"),
                "parent": row.get("parent"),
                "parentScaleMagnitude": float(row.get("parentScaleMagnitude") or 0.0),
                "parentLocalScale": row.get("parentLocalScale") or [],
            }
            for row in _top(child_rows, "parentScaleMagnitude")
            if float(row.get("parentScaleMagnitude") or 0.0) > 0.0
        ],
        "suspiciousJointSettings": suspicious_rows[: max(1, int(top_limit))],
    }


def skin_bind_matrix_probe(
    skin_cluster: str,
    influences: list[str],
    root_joint: str = "",
    top_limit: int = 15,
) -> dict:
    raw_skin = str(skin_cluster or "").strip()
    valid_influences = [joint for joint in influences if joint and cmds.objExists(joint)]
    if not raw_skin or not cmds.objExists(raw_skin):
        return {
            "exists": False,
            "skinCluster": raw_skin,
            "influenceCount": int(len(valid_influences)),
        }

    rows: list[dict] = []
    missing_bind_pre_matrix = 0
    for index, joint in enumerate(valid_influences):
        bind_pre_matrix = safe_matrix_attr(raw_skin, "bindPreMatrix[{0}]".format(index))
        if not bind_pre_matrix:
            missing_bind_pre_matrix += 1
            continue
        bind_matrix = om2.MMatrix(bind_pre_matrix).inverse()
        bind_matrix_values = [float(bind_matrix[item]) for item in range(16)]
        bind_translate = matrix_translation(bind_matrix_values)
        bind_scale = matrix_scale(bind_matrix_values)
        current_snapshot = node_transform_snapshot(joint)
        current_translate = current_snapshot.get("translate") or []
        current_scale = current_snapshot.get("scale") or []
        translate_delta = vector_length3(vector_delta3(current_translate, bind_translate))
        scale_delta = vector_length3(vector_delta3(current_scale, bind_scale))
        component_ratios = vector_component_ratios(current_translate, bind_translate)
        abs_component_ratio_median = vector_abs_component_ratio_median(current_translate, bind_translate)
        bind_magnitude = vector_length3(bind_translate)
        current_magnitude = vector_length3(current_translate)
        magnitude_ratio = safe_ratio(current_magnitude, bind_magnitude)
        rows.append(
            {
                "joint": joint,
                "matrixIndex": int(index),
                "bindWorldTranslate": bind_translate,
                "currentWorldTranslate": current_translate,
                "bindToCurrentDistance": float(translate_delta),
                "bindScale": bind_scale,
                "currentScale": current_scale,
                "bindToCurrentScaleDelta": float(scale_delta),
                "bindWorldTranslateComponentRatios": component_ratios,
                "bindWorldTranslateAbsComponentRatioMedian": abs_component_ratio_median,
                "bindWorldTranslateMagnitude": float(bind_magnitude),
                "currentWorldTranslateMagnitude": float(current_magnitude),
                "bindToCurrentMagnitudeRatio": magnitude_ratio,
                "isRootJoint": bool(root_joint and joint == root_joint),
            }
        )

    top_translation = sorted(rows, key=lambda item: float(item.get("bindToCurrentDistance") or 0.0), reverse=True)
    top_scale = sorted(rows, key=lambda item: float(item.get("bindToCurrentScaleDelta") or 0.0), reverse=True)
    top_ratio = sorted(
        rows,
        key=lambda item: abs(float(item.get("bindToCurrentMagnitudeRatio") or 0.0)),
        reverse=True,
    )
    root_rows = [row for row in rows if bool(row.get("isRootJoint"))]
    non_root_rows = [row for row in rows if not bool(row.get("isRootJoint"))]
    root_ratio = (
        float(root_rows[0].get("bindToCurrentMagnitudeRatio"))
        if root_rows and root_rows[0].get("bindToCurrentMagnitudeRatio") is not None
        else None
    )
    non_root_ratio_values = [
        float(row.get("bindToCurrentMagnitudeRatio"))
        for row in non_root_rows
        if row.get("bindToCurrentMagnitudeRatio") is not None
    ]
    non_root_abs_component_values = [
        float(row.get("bindWorldTranslateAbsComponentRatioMedian"))
        for row in non_root_rows
        if row.get("bindWorldTranslateAbsComponentRatioMedian") is not None
    ]
    root_abs_component_ratio = (
        float(root_rows[0].get("bindWorldTranslateAbsComponentRatioMedian"))
        if root_rows and root_rows[0].get("bindWorldTranslateAbsComponentRatioMedian") is not None
        else None
    )
    non_root_median_ratio = _median_float(non_root_ratio_values)
    non_root_median_abs_component_ratio = _median_float(non_root_abs_component_values)
    return {
        "exists": True,
        "skinCluster": raw_skin,
        "influenceCount": int(len(valid_influences)),
        "missingBindPreMatrixCount": int(missing_bind_pre_matrix),
        "topBindToCurrentTranslationMismatch": top_translation[: max(1, int(top_limit))],
        "topBindToCurrentScaleMismatch": top_scale[: max(1, int(top_limit))],
        "rootBindToCurrentMagnitudeRatio": root_ratio,
        "rootBindToCurrentAbsComponentRatioMedian": root_abs_component_ratio,
        "nonRootMedianBindToCurrentMagnitudeRatio": non_root_median_ratio,
        "nonRootMedianBindToCurrentAbsComponentRatioMedian": non_root_median_abs_component_ratio,
        "nonRootToRootMagnitudeRatioSplit": (
            safe_ratio(float(non_root_median_ratio), float(root_ratio))
            if root_ratio is not None and non_root_median_ratio is not None
            else None
        ),
        "nonRootToRootAbsComponentRatioSplit": (
            safe_ratio(float(non_root_median_abs_component_ratio), float(root_abs_component_ratio))
            if root_abs_component_ratio is not None and non_root_median_abs_component_ratio is not None
            else None
        ),
        "topBindToCurrentMagnitudeRatios": top_ratio[: max(1, int(top_limit))],
    }


def sample_frames(start_f: int, end_f: int, step_f: int, *, max_samples: int = 121) -> list[int]:
    step_value = max(1, int(step_f))
    frames = list(range(int(start_f), int(end_f) + 1, step_value))
    if not frames:
        return [int(start_f)]
    if len(frames) <= max_samples:
        return frames
    stride = max(1, int((len(frames) + max_samples - 1) / max_samples))
    sampled = frames[::stride]
    if sampled[-1] != frames[-1]:
        sampled.append(frames[-1])
    return sampled


def anim_curve_connections(node: str, attrs: list[str]) -> list[str]:
    curves: list[str] = []
    for attr in attrs:
        plug = "{0}.{1}".format(node, attr)
        if not cmds.objExists(plug):
            continue
        found = cmds.listConnections(plug, source=True, destination=False, type="animCurve") or []
        curves.extend(found)
    return list(dict.fromkeys(curves))


def joint_visual_probe(
    joints: list[str],
    mesh_shape: str,
    root_joint: str,
    start_f: int,
    end_f: int,
    step_f: int,
    *,
    top_limit: int = 20,
) -> dict:
    valid_joints = [joint for joint in joints if joint and cmds.objExists(joint)]
    if not valid_joints or not mesh_shape or not cmds.objExists(mesh_shape):
        return {
            "exists": False,
            "jointCount": int(len(valid_joints)),
            "meshShape": mesh_shape,
        }

    frames = sample_frames(start_f, end_f, step_f)
    original_time = cmds.currentTime(query=True)

    try:
        cmds.currentTime(int(frames[0]), edit=True)
        cmds.refresh(force=True)

        parent_map: dict[str, str] = {}
        bind_local_translate: dict[str, list[float]] = {}
        bind_local_rotate: dict[str, list[float]] = {}
        bind_world_position: dict[str, list[float]] = {}
        bind_bone_length: dict[str, float] = {}

        for joint in valid_joints:
            parent = cmds.listRelatives(joint, parent=True, fullPath=True, type="joint") or []
            parent_map[joint] = str(parent[0]) if parent else ""
            bind_local_translate[joint] = list(cmds.getAttr(joint + ".translate")[0])
            bind_local_rotate[joint] = list(cmds.getAttr(joint + ".rotate")[0])
            bind_world_position[joint] = list(cmds.xform(joint, query=True, worldSpace=True, translation=True))

        for joint, parent in parent_map.items():
            if not parent or parent not in bind_world_position:
                continue
            child_pos = bind_world_position.get(joint) or []
            parent_pos = bind_world_position.get(parent) or []
            bind_bone_length[joint] = vector_length3(
                [
                    float(child_pos[0] - parent_pos[0]),
                    float(child_pos[1] - parent_pos[1]),
                    float(child_pos[2] - parent_pos[2]),
                ]
            )

        joint_stats: dict[str, dict] = {}
        channel_summary = {
            "jointsWithTranslateKeys": 0,
            "jointsWithRotateKeys": 0,
            "jointsWithScaleKeys": 0,
            "totalTranslateCurves": 0,
            "totalRotateCurves": 0,
            "totalScaleCurves": 0,
        }

        for joint in valid_joints:
            translate_curves = anim_curve_connections(joint, ["translateX", "translateY", "translateZ"])
            rotate_curves = anim_curve_connections(joint, ["rotateX", "rotateY", "rotateZ"])
            scale_curves = anim_curve_connections(joint, ["scaleX", "scaleY", "scaleZ"])
            if translate_curves:
                channel_summary["jointsWithTranslateKeys"] += 1
            if rotate_curves:
                channel_summary["jointsWithRotateKeys"] += 1
            if scale_curves:
                channel_summary["jointsWithScaleKeys"] += 1
            channel_summary["totalTranslateCurves"] += len(translate_curves)
            channel_summary["totalRotateCurves"] += len(rotate_curves)
            channel_summary["totalScaleCurves"] += len(scale_curves)
            joint_stats[joint] = {
                "parent": parent_map.get(joint) or "",
                "bindLocalTranslate": bind_local_translate.get(joint) or [],
                "bindLocalRotate": bind_local_rotate.get(joint) or [],
                "bindWorldPosition": bind_world_position.get(joint) or [],
                "bindBoneLength": float(bind_bone_length.get(joint, 0.0)),
                "translateCurveCount": len(translate_curves),
                "rotateCurveCount": len(rotate_curves),
                "scaleCurveCount": len(scale_curves),
                "maxLocalTranslateDelta": 0.0,
                "frameAtMaxLocalTranslateDelta": int(frames[0]),
                "maxLocalRotateDelta": 0.0,
                "frameAtMaxLocalRotateDelta": int(frames[0]),
                "maxWorldDisplacement": 0.0,
                "frameAtMaxWorldDisplacement": int(frames[0]),
                "maxOutsideMeshBBoxDistance": 0.0,
                "frameAtMaxOutsideMeshBBoxDistance": int(frames[0]),
                "maxBoneLength": float(bind_bone_length.get(joint, 0.0)),
                "minBoneLength": float(bind_bone_length.get(joint, 0.0)),
                "frameAtMaxBoneLength": int(frames[0]),
                "frameAtMinBoneLength": int(frames[0]),
                "maxBoneLengthRatio": 1.0 if bind_bone_length.get(joint, 0.0) > 1e-8 else None,
            }

        for frame in frames:
            cmds.currentTime(int(frame), edit=True)
            cmds.refresh(force=True)
            mesh_bbox = (shape_bbox_snapshot(mesh_shape).get("worldBoundingBox") or [])
            world_positions: dict[str, list[float]] = {}
            for joint in valid_joints:
                world_positions[joint] = list(
                    cmds.xform(joint, query=True, worldSpace=True, translation=True)
                )

            for joint in valid_joints:
                local_translate = list(cmds.getAttr(joint + ".translate")[0])
                local_rotate = list(cmds.getAttr(joint + ".rotate")[0])
                world_position = world_positions.get(joint) or []
                bind_translate = bind_local_translate.get(joint) or [0.0, 0.0, 0.0]
                bind_rotate = bind_local_rotate.get(joint) or [0.0, 0.0, 0.0]
                bind_world = bind_world_position.get(joint) or [0.0, 0.0, 0.0]
                stats = joint_stats[joint]

                translate_delta = vector_length3(
                    [
                        float(local_translate[0] - bind_translate[0]),
                        float(local_translate[1] - bind_translate[1]),
                        float(local_translate[2] - bind_translate[2]),
                    ]
                )
                if translate_delta > float(stats["maxLocalTranslateDelta"]):
                    stats["maxLocalTranslateDelta"] = float(translate_delta)
                    stats["frameAtMaxLocalTranslateDelta"] = int(frame)

                rotate_delta = vector_length3(
                    [
                        float(local_rotate[0] - bind_rotate[0]),
                        float(local_rotate[1] - bind_rotate[1]),
                        float(local_rotate[2] - bind_rotate[2]),
                    ]
                )
                if rotate_delta > float(stats["maxLocalRotateDelta"]):
                    stats["maxLocalRotateDelta"] = float(rotate_delta)
                    stats["frameAtMaxLocalRotateDelta"] = int(frame)

                world_delta = vector_length3(
                    [
                        float(world_position[0] - bind_world[0]),
                        float(world_position[1] - bind_world[1]),
                        float(world_position[2] - bind_world[2]),
                    ]
                )
                if world_delta > float(stats["maxWorldDisplacement"]):
                    stats["maxWorldDisplacement"] = float(world_delta)
                    stats["frameAtMaxWorldDisplacement"] = int(frame)

                outside_distance = point_outside_bbox_distance(world_position, mesh_bbox)
                if outside_distance > float(stats["maxOutsideMeshBBoxDistance"]):
                    stats["maxOutsideMeshBBoxDistance"] = float(outside_distance)
                    stats["frameAtMaxOutsideMeshBBoxDistance"] = int(frame)

                parent = parent_map.get(joint) or ""
                if parent and parent in world_positions:
                    parent_world = world_positions.get(parent) or []
                    bone_length = vector_length3(
                        [
                            float(world_position[0] - parent_world[0]),
                            float(world_position[1] - parent_world[1]),
                            float(world_position[2] - parent_world[2]),
                        ]
                    )
                    if bone_length > float(stats["maxBoneLength"]):
                        stats["maxBoneLength"] = float(bone_length)
                        stats["frameAtMaxBoneLength"] = int(frame)
                    if bone_length < float(stats["minBoneLength"]):
                        stats["minBoneLength"] = float(bone_length)
                        stats["frameAtMinBoneLength"] = int(frame)
                    bind_length = float(bind_bone_length.get(joint, 0.0))
                    if bind_length > 1e-8:
                        ratio = float(bone_length / bind_length)
                        current_ratio = stats.get("maxBoneLengthRatio")
                        if current_ratio is None or ratio > float(current_ratio):
                            stats["maxBoneLengthRatio"] = float(ratio)

        root = root_joint if root_joint in joint_stats else (valid_joints[0] if valid_joints else "")
        root_frames = sorted(set([int(frames[0]), int(frames[len(frames) // 2]), int(frames[-1])]))
        root_samples: list[dict] = []
        for frame in root_frames:
            cmds.currentTime(int(frame), edit=True)
            cmds.refresh(force=True)
            root_samples.append(
                {
                    "frame": int(frame),
                    "localTranslate": list(cmds.getAttr(root + ".translate")[0]) if root else [],
                    "localRotate": list(cmds.getAttr(root + ".rotate")[0]) if root else [],
                    "worldTranslate": (
                        list(cmds.xform(root, query=True, worldSpace=True, translation=True))
                        if root
                        else []
                    ),
                }
            )

        top_translate = sorted(
            [
                {
                    "joint": joint,
                    "parent": stats.get("parent") or "",
                    "maxLocalTranslateDelta": float(stats.get("maxLocalTranslateDelta") or 0.0),
                    "frame": int(stats.get("frameAtMaxLocalTranslateDelta") or frames[0]),
                    "bindLocalTranslate": stats.get("bindLocalTranslate") or [],
                    "translateCurveCount": int(stats.get("translateCurveCount") or 0),
                }
                for joint, stats in joint_stats.items()
            ],
            key=lambda item: item["maxLocalTranslateDelta"],
            reverse=True,
        )[: max(1, int(top_limit))]

        top_world = sorted(
            [
                {
                    "joint": joint,
                    "parent": stats.get("parent") or "",
                    "maxWorldDisplacement": float(stats.get("maxWorldDisplacement") or 0.0),
                    "frame": int(stats.get("frameAtMaxWorldDisplacement") or frames[0]),
                    "translateCurveCount": int(stats.get("translateCurveCount") or 0),
                    "rotateCurveCount": int(stats.get("rotateCurveCount") or 0),
                }
                for joint, stats in joint_stats.items()
            ],
            key=lambda item: item["maxWorldDisplacement"],
            reverse=True,
        )[: max(1, int(top_limit))]

        top_bone_ratio = sorted(
            [
                {
                    "joint": joint,
                    "parent": stats.get("parent") or "",
                    "bindBoneLength": float(stats.get("bindBoneLength") or 0.0),
                    "maxBoneLength": float(stats.get("maxBoneLength") or 0.0),
                    "minBoneLength": float(stats.get("minBoneLength") or 0.0),
                    "maxBoneLengthRatio": float(stats.get("maxBoneLengthRatio") or 0.0),
                    "frameAtMaxBoneLength": int(stats.get("frameAtMaxBoneLength") or frames[0]),
                }
                for joint, stats in joint_stats.items()
                if (stats.get("parent") or "") and stats.get("maxBoneLengthRatio") is not None
            ],
            key=lambda item: item["maxBoneLengthRatio"],
            reverse=True,
        )[: max(1, int(top_limit))]

        top_outside_bbox = sorted(
            [
                {
                    "joint": joint,
                    "parent": stats.get("parent") or "",
                    "maxOutsideMeshBBoxDistance": float(stats.get("maxOutsideMeshBBoxDistance") or 0.0),
                    "frame": int(stats.get("frameAtMaxOutsideMeshBBoxDistance") or frames[0]),
                    "translateCurveCount": int(stats.get("translateCurveCount") or 0),
                    "rotateCurveCount": int(stats.get("rotateCurveCount") or 0),
                }
                for joint, stats in joint_stats.items()
            ],
            key=lambda item: item["maxOutsideMeshBBoxDistance"],
            reverse=True,
        )[: max(1, int(top_limit))]

        return {
            "exists": True,
            "meshShape": mesh_shape,
            "jointCount": int(len(valid_joints)),
            "sampledFrameCount": int(len(frames)),
            "sampledFrames": [int(frames[0]), int(frames[len(frames) // 2]), int(frames[-1])],
            "rootJoint": root,
            "channelSummary": channel_summary,
            "rootSamples": root_samples,
            "topLocalTranslateDeltaJoints": top_translate,
            "topWorldDisplacementJoints": top_world,
            "topBoneLengthRatioJoints": top_bone_ratio,
            "topOutsideMeshBBoxJoints": top_outside_bbox,
        }
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass


def quality_issues(expected_animation: bool, result_motion_max: float, ratio: float | None) -> list[str]:
    issues: list[str] = []
    if expected_animation:
        if result_motion_max <= 1e-6:
            issues.append("mesh_motion_zero")
        if ratio is not None:
            if ratio < 0.2:
                issues.append("motion_ratio_too_small")
            elif ratio > 5.0:
                issues.append("motion_ratio_too_large")
    return issues


def probe_fbx_quality(
    fbx_path: str,
    expected_vertex_count: int,
    prepared: PreparedRun,
    settings: CliRunSettings,
    *,
    path_label: str,
) -> dict:
    namespace = next_namespace("db_export_v3_probe")
    silent_log = lambda _message: None
    new_nodes, used_ns, method = import_cli_fbx(
        fbx_path,
        namespace,
        settings.frame_start,
        settings.frame_end,
        settings.frame_step,
        silent_log,
    )
    try:
        cleanup_unwanted_dynamic_nodes(new_nodes, silent_log)
        new_nodes = [node for node in new_nodes if cmds.objExists(node)]
        has_anim_tokens, token_counts = fbx_animation_token_probe(fbx_path)
        validation: ImportValidation = validate_imported_result(
            new_nodes,
            expected_vertex_count=expected_vertex_count,
            expect_animation=has_anim_tokens,
            start_f=settings.frame_start,
            end_f=settings.frame_end,
            log=silent_log,
        )
        contract_probe = summarize_imported_fbx_contract_nodes(
            new_nodes,
            expected_vertex_count,
            path=fbx_path,
            probe_namespace=used_ns,
            frame=settings.frame_start,
        )
        visual_probe = joint_visual_probe(
            validation.joints,
            str(contract_probe.get("chosenMeshShape") or ""),
            str(contract_probe.get("chosenRootJoint") or ""),
            settings.frame_start,
            settings.frame_end,
            settings.frame_step,
        )
        ratio = motion_ratio(
            prepared.source_motion_probe,
            float(validation.chosen_mesh_motion_max or 0.0),
        )
        issues = quality_issues(
            bool(validation.expected_animation),
            float(validation.chosen_mesh_motion_max or 0.0),
            ratio,
        )
        return {
            "pathLabel": path_label,
            "path": fbx_path,
            "namespace": used_ns,
            "method": method,
            "hasAnimTokens": bool(has_anim_tokens),
            "tokenCounts": token_counts,
            "validationSuccess": bool(validation.success),
            "validationIssues": list(validation.issues),
            "qualityIssues": issues,
            "chosenMeshMotionMax": float(validation.chosen_mesh_motion_max or 0.0),
            "motionRatioVsSource": ratio,
            "contractProbe": contract_probe,
            "bindScaleConsistency": bind_matrix_scale_consistency_summary(
                contract_probe.get("skinBindMatrixProbe") or {}
            ),
            "jointVisualProbe": visual_probe,
            "jointCount": int(len(validation.joints)),
            "meshCount": int(len(validation.meshes)),
        }
    finally:
        cleanup_imported_nodes(new_nodes, used_ns)


def probe_score(probe: dict) -> float:
    ratio = probe.get("motionRatioVsSource")
    if ratio is None or ratio <= 0.0:
        return float("inf")
    return abs(float(ratio) - 1.0)


def should_fallback_to_cli_out(cli_out_probe: dict, exported_probe: dict) -> tuple[bool, str]:
    cli_validation_ok = bool(cli_out_probe.get("validationSuccess"))
    exported_validation_ok = bool(exported_probe.get("validationSuccess"))
    cli_quality = list(cli_out_probe.get("qualityIssues") or [])
    exported_quality = list(exported_probe.get("qualityIssues") or [])
    if not cli_validation_ok:
        return False, "cli_out_probe_validation_failed"
    if cli_quality:
        return False, "cli_out_probe_quality_failed"
    if exported_validation_ok and not exported_quality:
        return False, "exported_probe_ok"
    cli_score = probe_score(cli_out_probe)
    exported_score = probe_score(exported_probe)
    if exported_score > cli_score:
        return True, "clean_export_degraded_quality"
    if exported_quality and not cli_quality:
        return True, "clean_export_quality_issues"
    return False, "no_fallback_condition_met"


def contract_root_basis_summary(probe: dict) -> dict:
    root_snapshot = probe.get("chosenRootSnapshot") or {}
    mesh_bbox = probe.get("chosenMeshBBox") or {}
    relation = probe.get("chosenMeshVsRoot") or {}
    return {
        "chosenRootJoint": str(probe.get("chosenRootJoint") or ""),
        "chosenMeshShape": str(probe.get("chosenMeshShape") or ""),
        "chosenMeshTransform": str(probe.get("chosenMeshTransform") or ""),
        "rootTranslate": root_snapshot.get("translate") or [],
        "rootRotate": root_snapshot.get("rotate") or [],
        "meshWorldCenter": mesh_bbox.get("worldCenter") or [],
        "meshWorldSize": mesh_bbox.get("worldSize") or [],
        "meshTransformMinusRootTranslate": relation.get("meshTransformMinusRootTranslate") or [],
        "meshCenterMinusRootTranslate": relation.get("meshCenterMinusRootTranslate") or [],
        "meshTransformDistanceFromRoot": relation.get("meshTransformDistanceFromRoot"),
        "meshCenterDistanceFromRoot": relation.get("meshCenterDistanceFromRoot"),
    }


def bind_matrix_scale_consistency_summary(bind_probe: dict) -> dict:
    return {
        "rootBindToCurrentMagnitudeRatio": bind_probe.get("rootBindToCurrentMagnitudeRatio"),
        "rootBindToCurrentAbsComponentRatioMedian": bind_probe.get("rootBindToCurrentAbsComponentRatioMedian"),
        "nonRootMedianBindToCurrentMagnitudeRatio": bind_probe.get("nonRootMedianBindToCurrentMagnitudeRatio"),
        "nonRootMedianBindToCurrentAbsComponentRatioMedian": bind_probe.get("nonRootMedianBindToCurrentAbsComponentRatioMedian"),
        "nonRootToRootMagnitudeRatioSplit": bind_probe.get("nonRootToRootMagnitudeRatioSplit"),
        "nonRootToRootAbsComponentRatioSplit": bind_probe.get("nonRootToRootAbsComponentRatioSplit"),
    }


def _skeleton_bbox_from_positions(position_map: dict[str, list[float]]) -> dict:
    rows = [pos for pos in position_map.values() if len(pos) == 3]
    if not rows:
        return {}
    xs = [float(pos[0]) for pos in rows]
    ys = [float(pos[1]) for pos in rows]
    zs = [float(pos[2]) for pos in rows]
    return {
        "min": [min(xs), min(ys), min(zs)],
        "max": [max(xs), max(ys), max(zs)],
        "center": [
            (min(xs) + max(xs)) * 0.5,
            (min(ys) + max(ys)) * 0.5,
            (min(zs) + max(zs)) * 0.5,
        ],
        "size": [
            max(xs) - min(xs),
            max(ys) - min(ys),
            max(zs) - min(zs),
        ],
    }


def _normalization_scale_candidates(bind_probe: dict) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = [("fixed_100", 100.0)]
    estimated = bind_probe.get("nonRootMedianBindToCurrentMagnitudeRatio")
    if estimated is not None:
        try:
            estimated_value = float(estimated)
        except Exception:
            estimated_value = 0.0
        if estimated_value > 1e-6 and abs(estimated_value - 100.0) > 1e-3:
            rows.append(("estimated_non_root_bind_median", estimated_value))
    return rows


def _influence_weight_mass_map(shape: str, skin_cluster: str, influences: list[str]) -> dict[str, float]:
    raw_shape = str(shape or "").strip()
    raw_skin = str(skin_cluster or "").strip()
    valid_influences = [joint for joint in influences if joint]
    if not raw_shape or not raw_skin or not cmds.objExists(raw_shape) or not cmds.objExists(raw_skin):
        return {joint: 0.0 for joint in valid_influences}
    summary = describe_skin_weight_distribution(raw_shape, raw_skin, valid_influences[0] if valid_influences else "")
    top_rows = summary.get("topInfluencesByWeightMass") or []
    mass_map = {joint: 0.0 for joint in valid_influences}
    for row in top_rows:
        joint = str(row.get("joint") or "")
        if joint in mass_map:
            mass_map[joint] = float(row.get("weightMass") or 0.0)
    if len(top_rows) < len(valid_influences):
        vertex_count = int(cmds.polyEvaluate(raw_shape, vertex=True) or 0)
        mass_map = {joint: 0.0 for joint in valid_influences}
        for index in range(vertex_count):
            component = "{0}.vtx[{1}]".format(raw_shape, index)
            try:
                values = cmds.skinPercent(raw_skin, component, query=True, value=True) or []
            except Exception:
                continue
            if len(values) != len(valid_influences):
                continue
            for joint, value in zip(valid_influences, values):
                mass_map[joint] += float(value)
    return mass_map


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


def raw_output_space_normalization_candidate_probe(
    skin_cluster: str,
    influences: list[str],
    root_joint: str,
    start_f: int,
    end_f: int,
    step_f: int,
    *,
    source_mesh_shape: str = "",
    bind_probe: dict | None = None,
    top_limit: int = 15,
) -> dict:
    raw_skin = str(skin_cluster or "").strip()
    valid_influences = [joint for joint in influences if joint and cmds.objExists(joint)]
    if not raw_skin or not cmds.objExists(raw_skin):
        return {
            "exists": False,
            "skinCluster": raw_skin,
            "jointCount": int(len(valid_influences)),
            "reason": "missing_skin_cluster",
        }
    if not valid_influences:
        return {
            "exists": False,
            "skinCluster": raw_skin,
            "jointCount": 0,
            "reason": "no_valid_influences",
        }

    working_bind_probe = bind_probe or skin_bind_matrix_probe(raw_skin, valid_influences, root_joint=root_joint)
    bind_rows = (working_bind_probe.get("topBindToCurrentTranslationMismatch") or []) + (
        working_bind_probe.get("topBindToCurrentScaleMismatch") or []
    ) + (working_bind_probe.get("topBindToCurrentMagnitudeRatios") or [])
    bind_world_map: dict[str, list[float]] = {}
    for row in bind_rows:
        joint = str(row.get("joint") or "")
        bind_translate = row.get("bindWorldTranslate") or []
        if joint and len(bind_translate) == 3:
            bind_world_map[joint] = [float(bind_translate[0]), float(bind_translate[1]), float(bind_translate[2])]

    if len(bind_world_map) < len(valid_influences):
        for index, joint in enumerate(valid_influences):
            if joint in bind_world_map:
                continue
            bind_pre_matrix = safe_matrix_attr(raw_skin, "bindPreMatrix[{0}]".format(index))
            if not bind_pre_matrix:
                continue
            bind_matrix = om2.MMatrix(bind_pre_matrix).inverse()
            bind_matrix_values = [float(bind_matrix[item]) for item in range(16)]
            bind_world_map[joint] = matrix_translation(bind_matrix_values)

    if not bind_world_map:
        return {
            "exists": False,
            "skinCluster": raw_skin,
            "jointCount": int(len(valid_influences)),
            "reason": "no_bind_world_map",
        }

    root_value = str(root_joint or "").strip()
    if root_value not in bind_world_map and valid_influences:
        root_value = valid_influences[0]

    parent_map: dict[str, str] = {}
    bind_bone_length: dict[str, float] = {}
    for joint in valid_influences:
        parent = cmds.listRelatives(joint, parent=True, fullPath=True, type="joint") or []
        parent_map[joint] = str(parent[0]) if parent else ""
    for joint, parent in parent_map.items():
        child_bind = bind_world_map.get(joint) or []
        parent_bind = bind_world_map.get(parent) or []
        if len(child_bind) == 3 and len(parent_bind) == 3:
            bind_bone_length[joint] = vector_length3(vector_delta3(child_bind, parent_bind))

    weight_mass_map = _influence_weight_mass_map(
        str(source_mesh_shape or ""),
        raw_skin,
        valid_influences,
    )
    if not any(float(value) > 0.0 for value in weight_mass_map.values()):
        weight_mass_map = {joint: 1.0 for joint in valid_influences}

    frames = sample_frames(start_f, end_f, step_f)
    original_time = cmds.currentTime(query=True)
    try:
        cmds.currentTime(int(start_f), edit=True)
        cmds.refresh(force=True)
        raw_bind_world_map = {
            joint: [float(v) for v in (cmds.xform(joint, query=True, worldSpace=True, translation=True) or [])]
            for joint in valid_influences
        }
        source_bind_bbox = (
            shape_bbox_snapshot(source_mesh_shape)
            if source_mesh_shape and cmds.objExists(source_mesh_shape)
            else {}
        )
        source_bind_center = list(source_bind_bbox.get("worldCenter") or [])

        scale_tests: list[dict] = []
        for label, unit_scale in _normalization_scale_candidates(working_bind_probe):
            if abs(float(unit_scale)) <= 1e-8:
                continue
            world_disp_rows: list[dict] = []
            bone_ratio_rows: list[dict] = []
            frame_bboxes: list[dict] = []
            root_reconstruction_rows: list[dict] = []

            for frame in frames:
                cmds.currentTime(int(frame), edit=True)
                cmds.refresh(force=True)

                corrected_world_map: dict[str, list[float]] = {}
                scaled_non_root_deltas: list[list[float]] = []
                scaled_non_root_weights: list[float] = []
                for joint in valid_influences:
                    bind_world = bind_world_map.get(joint) or []
                    raw_bind_world = raw_bind_world_map.get(joint) or []
                    raw_current_world = [
                        float(v)
                        for v in (cmds.xform(joint, query=True, worldSpace=True, translation=True) or [])
                    ]
                    if len(bind_world) != 3 or len(raw_bind_world) != 3 or len(raw_current_world) != 3:
                        continue
                    if joint == root_value:
                        corrected_world = [float(bind_world[0]), float(bind_world[1]), float(bind_world[2])]
                    else:
                        raw_delta = vector_delta3(raw_current_world, raw_bind_world)
                        scaled_delta = [
                            float(raw_delta[index] / float(unit_scale))
                            for index in range(3)
                        ]
                        corrected_world = [
                            float(bind_world[index] + scaled_delta[index])
                            for index in range(3)
                        ]
                        scaled_non_root_deltas.append(scaled_delta)
                        scaled_non_root_weights.append(float(weight_mass_map.get(joint, 0.0) or 0.0))
                    corrected_world_map[joint] = corrected_world

                source_frame_bbox = (
                    shape_bbox_snapshot(source_mesh_shape)
                    if source_mesh_shape and cmds.objExists(source_mesh_shape)
                    else {}
                )
                source_frame_center = list(source_frame_bbox.get("worldCenter") or [])
                source_center_delta = vector_delta3(source_frame_center, source_bind_center)
                weighted_mean_delta = _weighted_mean_vector(scaled_non_root_deltas, scaled_non_root_weights)
                weighted_median_delta = _weighted_median_vector(scaled_non_root_deltas, scaled_non_root_weights)

                if corrected_world_map:
                    frame_bboxes.append(
                        {
                            "frame": int(frame),
                            "skeletonBBox": _skeleton_bbox_from_positions(corrected_world_map),
                        }
                    )

                for joint in valid_influences:
                    corrected_world = corrected_world_map.get(joint) or []
                    bind_world = bind_world_map.get(joint) or []
                    if len(corrected_world) != 3 or len(bind_world) != 3:
                        continue
                    corrected_disp = vector_length3(vector_delta3(corrected_world, bind_world))
                    world_disp_rows.append(
                        {
                            "joint": joint,
                            "parent": parent_map.get(joint) or "",
                            "frame": int(frame),
                            "correctedWorldDisplacement": float(corrected_disp),
                        }
                    )
                    parent = parent_map.get(joint) or ""
                    parent_corrected = corrected_world_map.get(parent) or []
                    if not parent or len(parent_corrected) != 3:
                        continue
                    bind_length = float(bind_bone_length.get(joint, 0.0) or 0.0)
                    corrected_bone_length = vector_length3(vector_delta3(corrected_world, parent_corrected))
                    bone_ratio_rows.append(
                        {
                            "joint": joint,
                            "parent": parent,
                            "frame": int(frame),
                            "bindBoneLength": bind_length,
                            "correctedBoneLength": float(corrected_bone_length),
                            "correctedBoneLengthRatio": (
                                safe_ratio(float(corrected_bone_length), bind_length) if bind_length > 1e-8 else None
                            ),
                        }
                    )

                root_bind_world = bind_world_map.get(root_value) or []
                root_methods: list[tuple[str, list[float]]] = [("static_root", [0.0, 0.0, 0.0])]
                if len(source_center_delta) == 3:
                    root_methods.append(("source_mesh_center_delta", source_center_delta))
                if len(weighted_mean_delta) == 3:
                    root_methods.append(("weighted_non_root_mean_delta", weighted_mean_delta))
                if len(weighted_median_delta) == 3:
                    root_methods.append(("weighted_non_root_median_delta", weighted_median_delta))

                for method_label, root_delta in root_methods:
                    reconstructed_world_map: dict[str, list[float]] = {}
                    for joint in valid_influences:
                        corrected_world = corrected_world_map.get(joint) or []
                        bind_world = bind_world_map.get(joint) or []
                        if len(corrected_world) != 3 or len(bind_world) != 3:
                            continue
                        if joint == root_value and len(root_bind_world) == 3:
                            reconstructed_world_map[joint] = [
                                float(root_bind_world[index] + root_delta[index]) for index in range(3)
                            ]
                        else:
                            reconstructed_world_map[joint] = corrected_world

                    max_bone_ratio = 1.0
                    max_outside_source_bbox = 0.0
                    for joint in valid_influences:
                        current_world = reconstructed_world_map.get(joint) or []
                        parent = parent_map.get(joint) or ""
                        parent_world = reconstructed_world_map.get(parent) or []
                        if source_frame_bbox:
                            max_outside_source_bbox = max(
                                max_outside_source_bbox,
                                point_outside_bbox_distance(
                                    current_world,
                                    source_frame_bbox.get("worldBoundingBox") or [],
                                ),
                            )
                        if not parent or len(current_world) != 3 or len(parent_world) != 3:
                            continue
                        bind_length = float(bind_bone_length.get(joint, 0.0) or 0.0)
                        if bind_length <= 1e-8:
                            continue
                        current_length = vector_length3(vector_delta3(current_world, parent_world))
                        ratio = safe_ratio(float(current_length), bind_length)
                        if ratio is not None:
                            max_bone_ratio = max(max_bone_ratio, float(ratio))

                    root_reconstruction_rows.append(
                        {
                            "frame": int(frame),
                            "method": method_label,
                            "rootDelta": root_delta,
                            "rootDeltaToSourceMeshCenterDeltaDistance": (
                                vector_length3(vector_delta3(root_delta, source_center_delta))
                                if len(source_center_delta) == 3
                                else None
                            ),
                            "maxCorrectedBoneLengthRatio": float(max_bone_ratio),
                            "maxCorrectedOutsideSourceMeshBBoxDistance": float(max_outside_source_bbox),
                        }
                    )

            top_world = sorted(
                world_disp_rows,
                key=lambda item: float(item.get("correctedWorldDisplacement") or 0.0),
                reverse=True,
            )[: max(1, int(top_limit))]
            top_bone_ratio = sorted(
                [row for row in bone_ratio_rows if row.get("correctedBoneLengthRatio") is not None],
                key=lambda item: float(item.get("correctedBoneLengthRatio") or 0.0),
                reverse=True,
            )[: max(1, int(top_limit))]
            root_reconstruction_summary: list[dict] = []
            method_names = sorted(set(str(row.get("method") or "") for row in root_reconstruction_rows))
            for method_label in method_names:
                method_rows = [row for row in root_reconstruction_rows if str(row.get("method") or "") == method_label]
                root_reconstruction_summary.append(
                    {
                        "method": method_label,
                        "maxCorrectedBoneLengthRatio": (
                            max(float(row.get("maxCorrectedBoneLengthRatio") or 0.0) for row in method_rows)
                            if method_rows
                            else None
                        ),
                        "maxCorrectedOutsideSourceMeshBBoxDistance": (
                            max(float(row.get("maxCorrectedOutsideSourceMeshBBoxDistance") or 0.0) for row in method_rows)
                            if method_rows
                            else None
                        ),
                        "maxRootDeltaToSourceMeshCenterDeltaDistance": (
                            max(
                                float(row.get("rootDeltaToSourceMeshCenterDeltaDistance") or 0.0)
                                for row in method_rows
                                if row.get("rootDeltaToSourceMeshCenterDeltaDistance") is not None
                            )
                            if any(row.get("rootDeltaToSourceMeshCenterDeltaDistance") is not None for row in method_rows)
                            else None
                        ),
                    }
                )

            scale_tests.append(
                {
                    "label": label,
                    "unitScale": float(unit_scale),
                    "sampledFrameCount": int(len(frames)),
                    "sampledFrames": [int(frames[0]), int(frames[len(frames) // 2]), int(frames[-1])],
                    "maxCorrectedWorldDisplacement": (
                        float(top_world[0].get("correctedWorldDisplacement") or 0.0) if top_world else 0.0
                    ),
                    "maxCorrectedBoneLengthRatio": (
                        float(top_bone_ratio[0].get("correctedBoneLengthRatio") or 0.0)
                        if top_bone_ratio
                        else 1.0
                    ),
                    "topCorrectedWorldDisplacementJoints": top_world,
                    "topCorrectedBoneLengthRatioJoints": top_bone_ratio,
                    "sampledCorrectedSkeletonBBoxes": frame_bboxes[:12],
                    "rootMotionReconstructionSummary": root_reconstruction_summary,
                    "rootMotionReconstructionSamples": root_reconstruction_rows[:48],
                }
            )
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass

    return {
        "exists": True,
        "skinCluster": raw_skin,
        "jointCount": int(len(valid_influences)),
        "rootJoint": root_value,
        "estimatedUnitScaleFromBindMedian": working_bind_probe.get("nonRootMedianBindToCurrentMagnitudeRatio"),
        "estimatedUnitScaleFromAbsComponentMedian": working_bind_probe.get(
            "nonRootMedianBindToCurrentAbsComponentRatioMedian"
        ),
        "rootBindToCurrentMagnitudeRatio": working_bind_probe.get("rootBindToCurrentMagnitudeRatio"),
        "nonRootToRootMagnitudeRatioSplit": working_bind_probe.get("nonRootToRootMagnitudeRatioSplit"),
        "scaleTests": scale_tests,
    }


# Archived research helpers:
# The custom-hierarchy space-normalization experiments were removed from the
# production runtime on 2026-05-25. These helpers remain only as reference for
# future investigation and should not be reintroduced into the shipping Maya
# path without validating the behavior inside DemBones C++ first.
def normalization_candidate_summary(candidate_probe: dict) -> dict:
    if not candidate_probe or not bool(candidate_probe.get("exists")):
        return {
            "exists": False,
            "reason": candidate_probe.get("reason") if isinstance(candidate_probe, dict) else "missing_probe",
        }
    scale_summaries: list[dict] = []
    for row in candidate_probe.get("scaleTests") or []:
        scale_summaries.append(
            {
                "label": str(row.get("label") or ""),
                "unitScale": row.get("unitScale"),
                "maxCorrectedWorldDisplacement": row.get("maxCorrectedWorldDisplacement"),
                "maxCorrectedBoneLengthRatio": row.get("maxCorrectedBoneLengthRatio"),
                "rootMotionReconstructionSummary": row.get("rootMotionReconstructionSummary") or [],
            }
        )
    return {
        "exists": True,
        "estimatedUnitScaleFromBindMedian": candidate_probe.get("estimatedUnitScaleFromBindMedian"),
        "estimatedUnitScaleFromAbsComponentMedian": candidate_probe.get("estimatedUnitScaleFromAbsComponentMedian"),
        "rootBindToCurrentMagnitudeRatio": candidate_probe.get("rootBindToCurrentMagnitudeRatio"),
        "nonRootToRootMagnitudeRatioSplit": candidate_probe.get("nonRootToRootMagnitudeRatioSplit"),
        "scaleTests": scale_summaries,
    }


def normalization_apply_recommendation(candidate_probe: dict) -> dict:
    if not candidate_probe or not bool(candidate_probe.get("exists")):
        return {
            "shouldApply": False,
            "reason": str(candidate_probe.get("reason") or "missing_probe") if isinstance(candidate_probe, dict) else "missing_probe",
        }

    try:
        root_ratio = float(candidate_probe.get("rootBindToCurrentMagnitudeRatio"))
    except Exception:
        root_ratio = None
    try:
        split_ratio = float(candidate_probe.get("nonRootToRootMagnitudeRatioSplit"))
    except Exception:
        split_ratio = None

    if root_ratio is None or split_ratio is None:
        return {
            "shouldApply": False,
            "reason": "missing_scale_consistency_metrics",
        }
    if root_ratio < 0.5 or root_ratio > 2.0:
        return {
            "shouldApply": False,
            "reason": "root_ratio_not_near_identity",
            "rootBindToCurrentMagnitudeRatio": root_ratio,
            "nonRootToRootMagnitudeRatioSplit": split_ratio,
        }
    if split_ratio < 20.0:
        return {
            "shouldApply": False,
            "reason": "non_root_root_scale_split_below_threshold",
            "rootBindToCurrentMagnitudeRatio": root_ratio,
            "nonRootToRootMagnitudeRatioSplit": split_ratio,
        }

    scale_tests = list(candidate_probe.get("scaleTests") or [])
    chosen_scale_test = None
    for row in scale_tests:
        if str(row.get("label") or "") == "fixed_100":
            chosen_scale_test = row
            break
    if chosen_scale_test is None and scale_tests:
        chosen_scale_test = scale_tests[0]
    if not chosen_scale_test:
        return {
            "shouldApply": False,
            "reason": "no_scale_tests_available",
            "rootBindToCurrentMagnitudeRatio": root_ratio,
            "nonRootToRootMagnitudeRatioSplit": split_ratio,
        }

    method_rows = []
    for row in chosen_scale_test.get("rootMotionReconstructionSummary") or []:
        method_name = str(row.get("method") or "")
        if not method_name or method_name == "static_root":
            continue
        bone_ratio = row.get("maxCorrectedBoneLengthRatio")
        outside_distance = row.get("maxCorrectedOutsideSourceMeshBBoxDistance")
        root_delta_distance = row.get("maxRootDeltaToSourceMeshCenterDeltaDistance")
        if bone_ratio is None:
            continue
        method_rows.append(
            (
                float(bone_ratio),
                float(outside_distance if outside_distance is not None else float("inf")),
                float(root_delta_distance if root_delta_distance is not None else float("inf")),
                method_name,
                row,
            )
        )
    if not method_rows:
        return {
            "shouldApply": False,
            "reason": "no_root_motion_reconstruction_candidates",
            "rootBindToCurrentMagnitudeRatio": root_ratio,
            "nonRootToRootMagnitudeRatioSplit": split_ratio,
            "unitScale": chosen_scale_test.get("unitScale"),
        }

    method_rows.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    _, _, _, chosen_method, chosen_row = method_rows[0]
    return {
        "shouldApply": True,
        "reason": "detected_non_root_scale_split",
        "rootBindToCurrentMagnitudeRatio": root_ratio,
        "nonRootToRootMagnitudeRatioSplit": split_ratio,
        "scaleLabel": str(chosen_scale_test.get("label") or ""),
        "unitScale": float(chosen_scale_test.get("unitScale") or 100.0),
        "rootMotionMethod": chosen_method,
        "selectedMethodMetrics": {
            "maxCorrectedBoneLengthRatio": chosen_row.get("maxCorrectedBoneLengthRatio"),
            "maxCorrectedOutsideSourceMeshBBoxDistance": chosen_row.get("maxCorrectedOutsideSourceMeshBBoxDistance"),
            "maxRootDeltaToSourceMeshCenterDeltaDistance": chosen_row.get("maxRootDeltaToSourceMeshCenterDeltaDistance"),
        },
    }
