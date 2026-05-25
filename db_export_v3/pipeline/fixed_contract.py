from __future__ import annotations

import json

import maya.api.OpenMaya as om2
import maya.cmds as cmds

from .contract_probe import node_transform_snapshot, root_relationship_snapshot, shape_bbox_snapshot
from .fbx_io import export_fbx_selection


def matrix_from_values(values) -> om2.MMatrix:
    data = values or []
    if len(data) != 16:
        return om2.MMatrix()
    return om2.MMatrix([float(v) for v in data])


def matrix_to_values(matrix: om2.MMatrix) -> list[float]:
    return [float(matrix[index]) for index in range(16)]


def capture_fixed_space_diagnostics(source_selected, bound_selected, hierarchy_root: str, frame: int) -> dict:
    original_time = cmds.currentTime(query=True)
    try:
        cmds.currentTime(frame, edit=True)
        cmds.refresh(force=True)
        source_transform = node_transform_snapshot(source_selected.transform)
        bound_transform = node_transform_snapshot(bound_selected.transform)
        hierarchy_transform = node_transform_snapshot(hierarchy_root)
        source_bbox = shape_bbox_snapshot(source_selected.shape)
        bound_bbox = shape_bbox_snapshot(bound_selected.shape)
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass

    source_size = source_bbox.get("worldSize") or []
    bound_size = bound_bbox.get("worldSize") or []
    size_ratio = []
    if len(source_size) == 3 and len(bound_size) == 3:
        for src, dst in zip(source_size, bound_size):
            if abs(dst) > 1e-8:
                size_ratio.append(float(src / dst))
            else:
                size_ratio.append(None)

    return {
        "frame": int(frame),
        "sourceAnimatedTransform": source_transform,
        "boundInitTransform": bound_transform,
        "hierarchyRoot": hierarchy_transform,
        "sourceAnimatedMeshBBox": source_bbox,
        "boundInitMeshBBox": bound_bbox,
        "sourceToBoundSizeRatio": size_ratio,
        "sourceVsHierarchyRoot": root_relationship_snapshot(
            source_selected.transform,
            source_selected.shape,
            hierarchy_transform,
        ),
        "boundVsHierarchyRoot": root_relationship_snapshot(
            bound_selected.transform,
            bound_selected.shape,
            hierarchy_transform,
        ),
    }


def build_staged_source_mesh(source_selected, hierarchy_root: str, frame: int, log) -> tuple[str, str, dict]:
    original_time = cmds.currentTime(query=True)
    staged_transform = ""
    staged_shape = ""
    try:
        cmds.currentTime(frame, edit=True)
        cmds.refresh(force=True)

        staged_transform = cmds.createNode("transform", name="db_export_v3_fixedSourceStage")
        staged_shape = cmds.createNode("mesh", name="db_export_v3_fixedSourceStageShape", parent=staged_transform)
        cmds.connectAttr(source_selected.shape + ".outMesh", staged_shape + ".inMesh", force=True)
        cmds.xform(staged_transform, worldSpace=True, matrix=matrix_to_values(om2.MMatrix()))
        cmds.refresh(force=True)

        stage_diag = {
            "mode": "source_outMesh_passthrough_object_space",
            "frame": int(frame),
            "sourceShape": source_selected.shape,
            "hierarchyRoot": hierarchy_root,
            "stagedTransform": staged_transform,
            "stagedShape": staged_shape,
            "stagedTransformSnapshot": node_transform_snapshot(staged_transform),
            "stagedShapeBBox": shape_bbox_snapshot(staged_shape),
        }
        log("fixed_source_staging_mode: {0}".format(stage_diag["mode"]))
        log("fixed_source_staging_transform: {0}".format(staged_transform))
        log("fixed_source_staging_bbox: {0}".format(json.dumps(stage_diag["stagedShapeBBox"], sort_keys=True)))
        return staged_transform, staged_shape, stage_diag
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass


def find_skin_cluster_for_shape(shape: str) -> str:
    history = cmds.listHistory(shape, pruneDagObjects=True) or []
    skins = cmds.ls(history, type="skinCluster", long=True) or []
    return str(skins[0]) if skins else ""


def capture_skin_cluster_weights(mesh_shape: str, skin_cluster: str, influences: list[str]) -> dict:
    vertex_count = int(cmds.polyEvaluate(mesh_shape, vertex=True) or 0)
    weight_rows: list[list[tuple[str, float]]] = []
    non_zero_assignment_count = 0
    max_assignments_per_vertex = 0
    invalid_vertex_count = 0

    for index in range(vertex_count):
        component = "{0}.vtx[{1}]".format(mesh_shape, index)
        try:
            values = cmds.skinPercent(skin_cluster, component, query=True, value=True) or []
        except Exception:
            values = []
        if len(values) != len(influences):
            invalid_vertex_count += 1
            values = [0.0] * len(influences)
        row = [
            (joint, float(value))
            for joint, value in zip(influences, values)
            if abs(float(value)) > 1e-12
        ]
        non_zero_assignment_count += len(row)
        if len(row) > max_assignments_per_vertex:
            max_assignments_per_vertex = len(row)
        weight_rows.append(row)

    return {
        "vertexCount": vertex_count,
        "weightRows": weight_rows,
        "nonZeroAssignmentCount": int(non_zero_assignment_count),
        "maxAssignmentsPerVertex": int(max_assignments_per_vertex),
        "invalidVertexCount": int(invalid_vertex_count),
    }


def safe_get_numeric_attr(node: str, attr_name: str, default):
    plug = "{0}.{1}".format(node, attr_name)
    if not cmds.objExists(plug):
        return default
    try:
        return cmds.getAttr(plug)
    except Exception:
        return default


def rebuild_skin_cluster_at_current_pose(mesh_transform: str, mesh_shape: str, log) -> dict:
    skin_cluster = find_skin_cluster_for_shape(mesh_shape)
    if not skin_cluster:
        return {
            "performed": False,
            "reason": "no_skin_cluster",
        }

    influences = cmds.skinCluster(skin_cluster, query=True, influence=True) or []
    if not influences:
        return {
            "performed": False,
            "reason": "no_influences",
            "skinCluster": skin_cluster,
        }

    captured = capture_skin_cluster_weights(mesh_shape, skin_cluster, influences)
    log(
        "fixed_bind_matrix_rebuild_capture: {0}".format(
            json.dumps(
                {
                    "skinCluster": skin_cluster,
                    "influenceCount": int(len(influences)),
                    "vertexCount": int(captured.get("vertexCount") or 0),
                    "nonZeroAssignmentCount": int(captured.get("nonZeroAssignmentCount") or 0),
                    "maxAssignmentsPerVertex": int(captured.get("maxAssignmentsPerVertex") or 0),
                    "invalidVertexCount": int(captured.get("invalidVertexCount") or 0),
                },
                sort_keys=True,
            )
        )
    )

    skin_method = int(safe_get_numeric_attr(skin_cluster, "skinningMethod", 0) or 0)
    normalize_weights = int(safe_get_numeric_attr(skin_cluster, "normalizeWeights", 1) or 1)
    max_influences = int(safe_get_numeric_attr(skin_cluster, "maxInfluences", 0) or 0)
    maintain_max_influences = bool(safe_get_numeric_attr(skin_cluster, "maintainMaxInfluences", 0))
    bind_method = int(safe_get_numeric_attr(skin_cluster, "bindMethod", 0) or 0)

    try:
        cmds.skinCluster(mesh_transform, edit=True, unbind=True)
    except Exception:
        try:
            cmds.delete(skin_cluster)
        except Exception:
            pass

    dag_poses = cmds.ls(type="dagPose", long=True) or []
    if dag_poses:
        try:
            cmds.delete(dag_poses)
        except Exception:
            pass

    create_kwargs = {
        "toSelectedBones": True,
        "bindMethod": bind_method,
        "skinMethod": skin_method,
        "normalizeWeights": normalize_weights,
    }
    if max_influences > 0:
        create_kwargs["maximumInfluences"] = max_influences
        create_kwargs["obeyMaxInfluences"] = maintain_max_influences

    new_skin = cmds.skinCluster(influences, mesh_transform, **create_kwargs)[0]

    weight_rows = captured.get("weightRows") or []
    restored_vertex_count = 0
    for index, row in enumerate(weight_rows):
        component = "{0}.vtx[{1}]".format(mesh_shape, index)
        try:
            cmds.skinPercent(new_skin, component, transformValue=row, normalize=False)
            restored_vertex_count += 1
        except Exception:
            continue

    result = {
        "performed": True,
        "oldSkinCluster": skin_cluster,
        "newSkinCluster": new_skin,
        "vertexCount": int(captured.get("vertexCount") or 0),
        "restoredVertexCount": int(restored_vertex_count),
        "influenceCount": int(len(influences)),
        "invalidVertexCount": int(captured.get("invalidVertexCount") or 0),
        "maxAssignmentsPerVertex": int(captured.get("maxAssignmentsPerVertex") or 0),
        "skinMethod": int(skin_method),
        "normalizeWeights": int(normalize_weights),
        "bindMethod": int(bind_method),
        "maxInfluences": int(max_influences),
        "maintainMaxInfluences": bool(maintain_max_influences),
    }
    log("fixed_bind_matrix_rebuild: {0}".format(json.dumps(result, sort_keys=True)))
    return result


def export_fixed_init_bind_fbx(
    path: str,
    bound_transform: str,
    hierarchy_root: str,
    skin_cluster: str,
    log,
) -> dict:
    bind_pose_node = ""
    bind_pose_restored = False
    restore_error = ""
    original_time = cmds.currentTime(query=True)
    try:
        connections = cmds.listConnections(skin_cluster + ".bindPose", source=True, destination=False) or []
        poses = cmds.ls(connections, type="dagPose", long=True) or []
        if poses:
            bind_pose_node = poses[0]
        if bind_pose_node:
            try:
                cmds.dagPose(bind_pose_node, restore=True)
                cmds.refresh(force=True)
                bind_pose_restored = True
            except Exception as exc:
                restore_error = str(exc)
        export_fbx_selection(
            path,
            [bound_transform, hierarchy_root],
            export_skins=True,
            bake_animation=False,
        )
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass
    if bind_pose_node and bind_pose_restored:
        log("fixed_bind_pose_restore: success")
    elif bind_pose_node:
        log("fixed_bind_pose_restore_warning: failed on {0}: {1}".format(bind_pose_node, restore_error or "<unknown error>"))
    else:
        log("fixed_bind_pose_restore_warning: no bindPose node connected to skinCluster")
    return {
        "bindPoseNode": bind_pose_node,
        "bindPoseRestored": bool(bind_pose_restored),
        "bindPoseRestoreError": restore_error,
        "originalTime": float(original_time),
    }
