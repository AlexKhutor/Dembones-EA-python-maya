from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from pathlib import Path

import maya.cmds as cmds

from ..core.log_utils import directory_snapshot, path_state
from ..core.models import CliRunSettings, ImportValidation, PreparedRun
from ..core.naming import resolve_export_naming
from ..maya.hierarchy import (
    describe_alembic_sources,
    describe_joint_hierarchy,
    describe_skin_cluster,
    describe_skin_weight_distribution,
)
from ..maya.mesh_probe import probe_motion
from ..maya.paths import default_result_export_root
from ..maya.selection import resolve_mesh_with_deformers_from_node, resolve_selected_mesh_with_deformers
from .contract_probe import (
    bind_matrix_scale_consistency_summary,
    compare_mesh_animation,
    compare_contract_probes,
    contract_root_basis_summary,
    evaluate_fixed_contract_pair_probe,
    joint_contract_preflight,
    joint_visual_probe,
    motion_ratio,
    node_transform_snapshot,
    probe_fbx_quality,
    probe_fixed_contract_pair,
    probe_imported_fbx_contract,
    shape_bbox_snapshot,
    skin_bind_matrix_probe,
    should_fallback_to_cli_out,
    summarize_imported_fbx_contract_nodes,
)
from .fbx_io import (
    copy_latest,
    ensure_maya_io_plugins,
    export_alembic,
    export_fbx_selection,
    find_imported_mesh_shape,
    import_alembic_result,
    run_clean_scene_deliverable_export,
)
from .fixed_contract import (
    build_staged_source_mesh,
    capture_fixed_space_diagnostics,
    export_fixed_init_bind_fbx,
)
from .importer import (
    cleanup_imported_nodes,
    cleanup_unwanted_dynamic_nodes,
    fbx_animation_token_probe,
    import_cli_fbx,
    next_namespace,
    resolve_import_namespace,
    validate_imported_result,
)


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_") or "mesh"


def _format_probe(probe: dict[int, float]) -> str:
    return ", ".join("{0}:{1:.6f}".format(k, probe[k]) for k in sorted(probe.keys()))


def _fixed_variant_cli_label(value: str) -> str:
    mapping = {
        "optimize_existing": "optimize_existing",
        "weights_only": "weights_only",
        "transforms_only": "transforms_only",
    }
    key = str(value or "").strip()
    return mapping.get(key, "optimize_existing")


def _prepare_auto_run(settings: CliRunSettings, log) -> PreparedRun:
    if not os.path.isfile(settings.cli_exe):
        raise RuntimeError("CLI not found: {0}".format(settings.cli_exe))
    if settings.frame_end < settings.frame_start:
        raise RuntimeError("Frame End must be >= Frame Start.")

    ensure_maya_io_plugins()
    selected = resolve_selected_mesh_with_deformers()
    selected_vertex_count = int(cmds.polyEvaluate(selected.shape, vertex=True) or 0)

    cache_root = Path(settings.cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = cache_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    latest_dir = cache_root / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    shape_leaf = _safe_name(selected.shape.split("|")[-1])
    rest_fbx = str(run_dir / f"{shape_leaf}_rest.fbx")
    anim_abc = str(run_dir / f"{shape_leaf}_anim.abc")
    out_fbx = str(run_dir / f"{shape_leaf}_cli_out.fbx")
    latest_manifest = str(latest_dir / f"{shape_leaf}_latest_manifest.json")

    log("selection_transform: {0}".format(selected.transform))
    log("selection_shape: {0}".format(selected.shape))
    log("selection_vertex_count: {0}".format(selected_vertex_count))
    log("selection_deformer_types: {0}".format(", ".join(selected.deformer_types)))
    log("cache_root_resolved: {0}".format(str(cache_root)))
    log("run_dir_created: {0}".format(str(run_dir)))
    log("latest_dir_created: {0}".format(str(latest_dir)))
    log("prepared_out_fbx: {0}".format(out_fbx))

    source_motion = probe_motion(selected.shape, settings.frame_start, settings.frame_end)
    log("source_motion_probe: {0}".format(_format_probe(source_motion)))

    log("export_rest_fbx: {0}".format(rest_fbx))
    cmds.currentTime(settings.frame_start, edit=True)
    cmds.refresh(force=True)
    export_fbx_selection(rest_fbx, [selected.transform])
    log("rest_fbx_state: {0}".format(json.dumps(path_state(rest_fbx), sort_keys=True)))

    log("export_anim_abc: {0}".format(anim_abc))
    export_alembic(anim_abc, selected.transform, settings.frame_start, settings.frame_end, settings.frame_step)
    abc_size = os.path.getsize(anim_abc) if os.path.exists(anim_abc) else -1
    log("abc_export_size_bytes: {0}".format(abc_size))
    log("anim_abc_state: {0}".format(json.dumps(path_state(anim_abc), sort_keys=True)))

    probe_ns = next_namespace("db_export_v3_abcProbe")
    abc_nodes: list[str] = []
    try:
        abc_nodes = import_alembic_result(anim_abc, probe_ns)
        expected_vtx = int(cmds.polyEvaluate(selected.shape, vertex=True))
        abc_shape, candidates = find_imported_mesh_shape(
            abc_nodes, expected_vtx, settings.frame_start, settings.frame_end
        )
        log("abc_probe_candidates: {0}".format(len(candidates)))
        if abc_shape:
            abc_probe = probe_motion(abc_shape, settings.frame_start, settings.frame_end)
            abc_motion_max = max(abc_probe.values()) if abc_probe else 0.0
            log("abc_probe_shape: {0}".format(abc_shape))
            log("abc_probe_motion: {0}".format(_format_probe(abc_probe)))
            log("abc_export_has_animation: {0}".format(bool(abc_motion_max > 1e-6)))
        else:
            log("abc_probe_warning: Alembic imported but no probe mesh candidate found.")
    except Exception as exc:
        log("abc_probe_error: {0}".format(exc))
    finally:
        cleanup_imported_nodes(abc_nodes, probe_ns)

    latest_rest = str(latest_dir / f"{shape_leaf}_rest_latest.fbx")
    latest_abc = str(latest_dir / f"{shape_leaf}_anim_latest.abc")
    copy_latest(rest_fbx, latest_rest)
    copy_latest(anim_abc, latest_abc)
    log("latest_rest_fbx_state: {0}".format(json.dumps(path_state(latest_rest), sort_keys=True)))
    log("latest_anim_abc_state: {0}".format(json.dumps(path_state(latest_abc), sort_keys=True)))

    manifest = {
        "run_id": run_id,
        "shape": selected.shape,
        "transform": selected.transform,
        "deformer_types": selected.deformer_types,
        "frame_range": [settings.frame_start, settings.frame_end, settings.frame_step],
        "files": {"rest_fbx": rest_fbx, "anim_abc": anim_abc, "out_fbx": out_fbx},
        "motion_probe": source_motion,
        "created_at": datetime.datetime.now().isoformat(),
    }
    with open(latest_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    debug_info = {
        "selected": {
            "transform": selected.transform,
            "shape": selected.shape,
            "vertexCount": selected_vertex_count,
            "deformerTypes": list(selected.deformer_types),
            "deformerNodes": list(selected.deformers),
        },
        "cache": {
            "cacheRoot": path_state(cache_root),
            "runDir": path_state(run_dir),
            "runDirSnapshot": directory_snapshot(run_dir),
            "latestDir": path_state(latest_dir),
            "latestDirSnapshot": directory_snapshot(latest_dir),
        },
        "files": {
            "restFbx": path_state(rest_fbx),
            "animAbc": path_state(anim_abc),
            "outFbxExpected": path_state(out_fbx),
            "latestRestFbx": path_state(latest_rest),
            "latestAnimAbc": path_state(latest_abc),
            "latestManifest": path_state(latest_manifest),
        },
        "sourceMotionProbe": dict(source_motion),
        "abcProbeNamespace": probe_ns,
    }

    return PreparedRun(
        run_id=run_id,
        selected=selected,
        rest_fbx=rest_fbx,
        anim_abc=anim_abc,
        out_fbx=out_fbx,
        run_dir=str(run_dir),
        latest_manifest=latest_manifest,
        source_motion_probe=source_motion,
        solve_mode="auto",
        expected_joint_count=int(settings.bones),
        debug_info=debug_info,
    )


def _prepare_fixed_bones_run(settings: CliRunSettings, log) -> PreparedRun:
    if not os.path.isfile(settings.cli_exe):
        raise RuntimeError("CLI not found: {0}".format(settings.cli_exe))
    if settings.frame_end < settings.frame_start:
        raise RuntimeError("Frame End must be >= Frame Start.")

    ensure_maya_io_plugins()
    if not str(settings.source_animated_mesh or "").strip():
        raise RuntimeError("Source Animated Mesh is empty. Set it in the Use Existing Skeleton tab.")
    if not str(settings.bound_init_mesh or "").strip():
        raise RuntimeError("Bound Init Mesh is empty. Set it in the Use Existing Skeleton tab.")

    source_selected = resolve_mesh_with_deformers_from_node(settings.source_animated_mesh)
    bound_selected = resolve_mesh_with_deformers_from_node(settings.bound_init_mesh)
    hierarchy = describe_joint_hierarchy(settings.hierarchy_root)
    source_vertex_count = int(cmds.polyEvaluate(source_selected.shape, vertex=True) or 0)
    bound_vertex_count = int(cmds.polyEvaluate(bound_selected.shape, vertex=True) or 0)
    source_face_count = int(cmds.polyEvaluate(source_selected.shape, face=True) or 0)
    bound_face_count = int(cmds.polyEvaluate(bound_selected.shape, face=True) or 0)
    if source_vertex_count != bound_vertex_count:
        raise RuntimeError(
            "Source Animated Mesh and Bound Init Mesh have different vertex counts: {0} vs {1}".format(
                source_vertex_count, bound_vertex_count
            )
        )
    if source_face_count != bound_face_count:
        raise RuntimeError(
            "Source Animated Mesh and Bound Init Mesh have different face counts: {0} vs {1}".format(
                source_face_count, bound_face_count
            )
        )

    skin_info = describe_skin_cluster(bound_selected.shape, hierarchy.joints)
    if not skin_info.get("exists"):
        raise RuntimeError(
            "Use Existing Skeleton requires a skinCluster on Bound Init Mesh. None found on: {0}".format(
                bound_selected.shape
            )
        )
    if skin_info.get("extraInfluencesOutsideHierarchy"):
        raise RuntimeError(
            "Selected hierarchy does not cover all skin influences on Bound Init Mesh. Extra influences outside root: {0}".format(
                ", ".join(skin_info.get("extraInfluencesOutsideHierarchy") or [])
            )
        )

    alembic_sources = describe_alembic_sources(source_selected.shape)
    if not alembic_sources:
        raise RuntimeError(
            "No AlembicNode found in Source Animated Mesh history: {0}".format(source_selected.shape)
        )

    cache_root = Path(settings.cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = cache_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    latest_dir = cache_root / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    shape_leaf = _safe_name(source_selected.shape.split("|")[-1])
    init_fbx = str(run_dir / f"{shape_leaf}_fixed_init.fbx")
    anim_abc = str(run_dir / f"{shape_leaf}_anim.abc")
    out_fbx = str(run_dir / f"{shape_leaf}_cli_out.fbx")
    latest_manifest = str(latest_dir / f"{shape_leaf}_fixed_latest_manifest.json")
    skin_weight_diag = describe_skin_weight_distribution(
        bound_selected.shape,
        str(skin_info.get("skinCluster") or ""),
        hierarchy.root,
    )

    log("source_animated_transform: {0}".format(source_selected.transform))
    log("source_animated_shape: {0}".format(source_selected.shape))
    log("source_animated_vertex_count: {0}".format(source_vertex_count))
    log("source_animated_face_count: {0}".format(source_face_count))
    log("source_animated_deformer_types: {0}".format(", ".join(source_selected.deformer_types)))
    log("bound_init_transform: {0}".format(bound_selected.transform))
    log("bound_init_shape: {0}".format(bound_selected.shape))
    log("bound_init_vertex_count: {0}".format(bound_vertex_count))
    log("bound_init_face_count: {0}".format(bound_face_count))
    log("bound_init_deformer_types: {0}".format(", ".join(bound_selected.deformer_types)))
    log("fixed_cli_strategy: stock_ea_dembones_cli")
    log("fixed_cli_auto_init_expected: false")
    log("fixed_cli_requires_scene_skincluster: true")
    log("solve_mode_fixed_hierarchy_root_requested: {0}".format(settings.hierarchy_root))
    log("solve_mode_fixed_hierarchy_root_resolved: {0}".format(hierarchy.root))
    log("fixed_hierarchy_joint_count: {0}".format(hierarchy.joint_count))
    log("fixed_hierarchy_max_depth: {0}".format(hierarchy.max_depth))
    log("fixed_hierarchy_leaf_count: {0}".format(hierarchy.leaf_count))
    log(
        "fixed_hierarchy_namespaces: {0}".format(
            ", ".join(hierarchy.namespaces) if hierarchy.namespaces else "<none>"
        )
    )
    log(
        "fixed_hierarchy_duplicate_short_names: {0}".format(
            json.dumps(hierarchy.duplicate_short_names, sort_keys=True)
        )
    )
    log("fixed_hierarchy_sample_joints: {0}".format(", ".join(hierarchy.sample_joints)))
    log("fixed_skin_cluster: {0}".format(skin_info.get("skinCluster") or "<none>"))
    log("fixed_bind_pose_node: {0}".format(skin_info.get("bindPoseNode") or "<none>"))
    log("fixed_skin_influence_count: {0}".format(int(skin_info.get("influenceCount", 0) or 0)))
    log(
        "fixed_skin_hierarchy_joints_without_influence: {0}".format(
            len(skin_info.get("hierarchyJointsWithoutInfluence") or [])
        )
    )
    log("fixed_skin_weight_distribution: {0}".format(json.dumps(skin_weight_diag, sort_keys=True)))
    log(
        "fixed_scene_units: {0}".format(
            json.dumps(
                {
                    "linear": str(cmds.currentUnit(query=True, linear=True) or ""),
                    "angle": str(cmds.currentUnit(query=True, angle=True) or ""),
                    "time": str(cmds.currentUnit(query=True, time=True) or ""),
                },
                sort_keys=True,
            )
        )
    )
    log(
        "fixed_scene_alembic_nodes: {0}".format(
            json.dumps(alembic_sources, ensure_ascii=False, sort_keys=True)
        )
    )
    log("cache_root_resolved: {0}".format(str(cache_root)))
    log("run_dir_created: {0}".format(str(run_dir)))
    log("latest_dir_created: {0}".format(str(latest_dir)))
    log("prepared_out_fbx: {0}".format(out_fbx))
    log("fixed_solve_variant_resolved: {0}".format(_fixed_variant_cli_label(settings.fixed_solve_variant)))
    log("fixed_skeleton_space_mode_resolved: preserve_input_space")
    log("fixed_init_contract: preserve_input_skeleton_space")
    log("fixed_root_normalization: {0}".format(json.dumps({"mode": "removed", "performed": False}, sort_keys=True)))
    if _fixed_variant_cli_label(settings.fixed_solve_variant) == "weights_only":
        log(
            "fixed_variant_warning: weights_only expects input bone transformations. "
            "With bind-pose init it is diagnostic only and may converge to a static result."
        )

    original_time = cmds.currentTime(query=True)
    try:
        cmds.currentTime(settings.frame_start, edit=True)
        cmds.refresh(force=True)
        live_joint_contract = joint_contract_preflight(
            hierarchy.joints,
            bound_selected.shape,
            hierarchy.root,
        )
        live_bind_matrix_probe = skin_bind_matrix_probe(
            str(skin_info.get("skinCluster") or ""),
            list(skin_info.get("influences") or []),
            root_joint=hierarchy.root,
        )
    finally:
        try:
            cmds.currentTime(original_time, edit=True)
            cmds.refresh(force=True)
        except Exception:
            pass
    log("fixed_joint_contract_preflight: {0}".format(json.dumps(live_joint_contract, sort_keys=True)))
    log("fixed_skin_bind_matrix_probe: {0}".format(json.dumps(live_bind_matrix_probe, sort_keys=True)))
    log(
        "fixed_skin_bind_scale_consistency: {0}".format(
            json.dumps(bind_matrix_scale_consistency_summary(live_bind_matrix_probe), sort_keys=True)
        )
    )

    space_diag = capture_fixed_space_diagnostics(
        source_selected,
        bound_selected,
        hierarchy.root,
        settings.frame_start,
    )
    log("fixed_space_diagnostics: {0}".format(json.dumps(space_diag, sort_keys=True)))

    source_motion = probe_motion(source_selected.shape, settings.frame_start, settings.frame_end)
    log("source_motion_probe: {0}".format(_format_probe(source_motion)))

    log("export_fixed_init_fbx: {0}".format(init_fbx))
    bind_pose_export = export_fixed_init_bind_fbx(
        init_fbx,
        bound_selected.transform,
        hierarchy.root,
        str(skin_info.get("skinCluster") or ""),
        log,
    )
    log("fixed_init_fbx_state: {0}".format(json.dumps(path_state(init_fbx), sort_keys=True)))
    init_contract_probe = probe_imported_fbx_contract(init_fbx, settings.frame_start, bound_vertex_count)
    log("fixed_init_fbx_probe: {0}".format(json.dumps(init_contract_probe, sort_keys=True)))
    log(
        "fixed_init_fbx_joint_contract_probe: {0}".format(
            json.dumps(init_contract_probe.get("jointContractProbe") or {}, sort_keys=True)
        )
    )
    log(
        "fixed_init_fbx_skin_bind_matrix_probe: {0}".format(
            json.dumps(init_contract_probe.get("skinBindMatrixProbe") or {}, sort_keys=True)
        )
    )
    log(
        "fixed_init_fbx_skin_bind_scale_consistency: {0}".format(
            json.dumps(
                bind_matrix_scale_consistency_summary(
                    init_contract_probe.get("skinBindMatrixProbe") or {}
                ),
                sort_keys=True,
            )
        )
    )
    log(
        "fixed_cli_input_root_basis: {0}".format(
            json.dumps(contract_root_basis_summary(init_contract_probe), sort_keys=True)
        )
    )

    log("export_anim_abc: {0}".format(anim_abc))
    staged_transform = ""
    staging_diag = {}
    try:
        staged_transform, _, staging_diag = build_staged_source_mesh(
            source_selected,
            hierarchy.root,
            settings.frame_start,
            log,
        )
        log("fixed_abc_export_space: object")
        export_alembic(
            anim_abc,
            staged_transform,
            settings.frame_start,
            settings.frame_end,
            settings.frame_step,
            world_space=False,
        )
    finally:
        if staged_transform and cmds.objExists(staged_transform):
            try:
                cmds.delete(staged_transform)
            except Exception:
                pass
    abc_size = os.path.getsize(anim_abc) if os.path.exists(anim_abc) else -1
    log("abc_export_size_bytes: {0}".format(abc_size))
    log("anim_abc_state: {0}".format(json.dumps(path_state(anim_abc), sort_keys=True)))

    probe_ns = next_namespace("db_export_v3_abcProbe")
    abc_nodes: list[str] = []
    try:
        abc_nodes = import_alembic_result(anim_abc, probe_ns)
        expected_vtx = int(cmds.polyEvaluate(source_selected.shape, vertex=True))
        abc_shape, candidates = find_imported_mesh_shape(
            abc_nodes, expected_vtx, settings.frame_start, settings.frame_end
        )
        log("abc_probe_candidates: {0}".format(len(candidates)))
        if abc_shape:
            abc_probe = probe_motion(abc_shape, settings.frame_start, settings.frame_end)
            abc_motion_max = max(abc_probe.values()) if abc_probe else 0.0
            abc_probe_transform = ""
            try:
                abc_parents = cmds.listRelatives(abc_shape, parent=True, fullPath=True) or []
                abc_probe_transform = abc_parents[0] if abc_parents else ""
            except Exception:
                abc_probe_transform = ""
            abc_probe_contract = {
                "shape": abc_shape,
                "transform": abc_probe_transform,
                "transformSnapshot": (
                    node_transform_snapshot(abc_probe_transform) if abc_probe_transform else {}
                ),
                "shapeBBox": shape_bbox_snapshot(abc_shape),
            }
            log("abc_probe_shape: {0}".format(abc_shape))
            log("abc_probe_motion: {0}".format(_format_probe(abc_probe)))
            log("abc_probe_contract: {0}".format(json.dumps(abc_probe_contract, sort_keys=True)))
            log("abc_export_has_animation: {0}".format(bool(abc_motion_max > 1e-6)))
        else:
            log("abc_probe_warning: Alembic imported but no probe mesh candidate found.")
    except Exception as exc:
        log("abc_probe_error: {0}".format(exc))
    finally:
        cleanup_imported_nodes(abc_nodes, probe_ns)

    pair_probe = probe_fixed_contract_pair(
        init_fbx,
        anim_abc,
        settings.frame_start,
        settings.frame_end,
        bound_vertex_count,
    )
    log("fixed_contract_pair_probe: {0}".format(json.dumps(pair_probe, sort_keys=True)))
    pair_quality = evaluate_fixed_contract_pair_probe(pair_probe)
    log("fixed_contract_pair_quality: {0}".format(json.dumps(pair_quality, sort_keys=True)))
    if not pair_quality.get("success"):
        raise RuntimeError(
            "Fixed contract pair validation failed before CLI: {0}".format(
                ", ".join(pair_quality.get("issues") or ["unknown"])
            )
        )

    latest_init = str(latest_dir / f"{shape_leaf}_fixed_init_latest.fbx")
    latest_abc = str(latest_dir / f"{shape_leaf}_anim_latest.abc")
    copy_latest(init_fbx, latest_init)
    copy_latest(anim_abc, latest_abc)
    log("latest_fixed_init_fbx_state: {0}".format(json.dumps(path_state(latest_init), sort_keys=True)))
    log("latest_anim_abc_state: {0}".format(json.dumps(path_state(latest_abc), sort_keys=True)))

    manifest = {
        "run_id": run_id,
        "solveMode": "fixed_bones",
        "fixedSolveVariant": _fixed_variant_cli_label(settings.fixed_solve_variant),
        "sourceAnimatedMesh": {
            "shape": source_selected.shape,
            "transform": source_selected.transform,
            "vertexCount": source_vertex_count,
            "faceCount": source_face_count,
            "deformerTypes": source_selected.deformer_types,
        },
        "boundInitMesh": {
            "shape": bound_selected.shape,
            "transform": bound_selected.transform,
            "vertexCount": bound_vertex_count,
            "faceCount": bound_face_count,
            "deformerTypes": bound_selected.deformer_types,
        },
        "fixedSkeletonSpaceMode": "preserve_input_space",
        "frame_range": [settings.frame_start, settings.frame_end, settings.frame_step],
        "files": {"init_fbx": init_fbx, "anim_abc": anim_abc, "out_fbx": out_fbx},
        "motion_probe": source_motion,
        "hierarchy": {
            "root": hierarchy.root,
            "jointCount": hierarchy.joint_count,
            "maxDepth": hierarchy.max_depth,
            "leafCount": hierarchy.leaf_count,
            "duplicateShortNames": hierarchy.duplicate_short_names,
            "namespaces": hierarchy.namespaces,
            "sampleJoints": hierarchy.sample_joints,
        },
        "skinCluster": skin_info,
        "skinWeightDistribution": skin_weight_diag,
        "bindPoseExport": bind_pose_export,
        "initFbxProbe": init_contract_probe,
        "spaceDiagnostics": space_diag,
        "sourceStaging": staging_diag,
        "fixedContractPairProbe": pair_probe,
        "fixedContractPairQuality": pair_quality,
        "alembicSources": alembic_sources,
        "created_at": datetime.datetime.now().isoformat(),
    }
    with open(latest_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    debug_info = {
        "sourceAnimatedMesh": {
            "transform": source_selected.transform,
            "shape": source_selected.shape,
            "vertexCount": source_vertex_count,
            "faceCount": source_face_count,
            "deformerTypes": list(source_selected.deformer_types),
            "deformerNodes": list(source_selected.deformers),
        },
        "boundInitMesh": {
            "transform": bound_selected.transform,
            "shape": bound_selected.shape,
            "vertexCount": bound_vertex_count,
            "faceCount": bound_face_count,
            "deformerTypes": list(bound_selected.deformer_types),
            "deformerNodes": list(bound_selected.deformers),
        },
        "fixedHierarchy": {
            "root": hierarchy.root,
            "jointCount": hierarchy.joint_count,
            "maxDepth": hierarchy.max_depth,
            "leafCount": hierarchy.leaf_count,
            "duplicateShortNames": hierarchy.duplicate_short_names,
            "namespaces": hierarchy.namespaces,
            "sampleJoints": hierarchy.sample_joints,
            "parentMap": hierarchy.parent_map,
        },
        "skinCluster": skin_info,
        "skinWeightDistribution": skin_weight_diag,
        "bindPoseExport": bind_pose_export,
        "fixedSkeletonSpaceMode": "preserve_input_space",
        "initFbxProbe": init_contract_probe,
        "spaceDiagnostics": space_diag,
        "sourceStaging": staging_diag,
        "fixedContractPairProbe": pair_probe,
        "fixedContractPairQuality": pair_quality,
        "alembicSources": alembic_sources,
        "cache": {
            "cacheRoot": path_state(cache_root),
            "runDir": path_state(run_dir),
            "runDirSnapshot": directory_snapshot(run_dir),
            "latestDir": path_state(latest_dir),
            "latestDirSnapshot": directory_snapshot(latest_dir),
        },
        "files": {
            "initFbx": path_state(init_fbx),
            "animAbc": path_state(anim_abc),
            "outFbxExpected": path_state(out_fbx),
            "latestInitFbx": path_state(latest_init),
            "latestAnimAbc": path_state(latest_abc),
            "latestManifest": path_state(latest_manifest),
        },
        "sourceMotionProbe": dict(source_motion),
        "abcProbeNamespace": probe_ns,
    }

    return PreparedRun(
        run_id=run_id,
        selected=bound_selected,
        rest_fbx=init_fbx,
        anim_abc=anim_abc,
        out_fbx=out_fbx,
        run_dir=str(run_dir),
        latest_manifest=latest_manifest,
        source_motion_probe=source_motion,
        solve_mode="fixed_bones",
        expected_joint_count=hierarchy.joint_count,
        fixed_hierarchy_root=hierarchy.root,
        locked_scene_nodes=[
            source_selected.transform,
            source_selected.shape,
            bound_selected.transform,
            bound_selected.shape,
        ],
        debug_info=debug_info,
    )


def prepare_run(settings: CliRunSettings, log) -> PreparedRun:
    if str(settings.solve_mode or "").strip() == "fixed_bones":
        return _prepare_fixed_bones_run(settings, log)
    return _prepare_auto_run(settings, log)


def build_cli_args(settings: CliRunSettings, prepared: PreparedRun) -> list[str]:
    if str(settings.solve_mode or "").strip() == "fixed_bones":
        args = [
            f"-i={prepared.rest_fbx}",
            f"-a={prepared.anim_abc}",
            f"--bindUpdate={int(settings.bind_update)}",
            f"--nnz={int(settings.nnz)}",
            f"--nIters={int(settings.n_iters)}",
            f"--tolerance={float(settings.tolerance)}",
            f"--patience={int(settings.patience)}",
            f"-o={prepared.out_fbx}",
        ]
        variant = _fixed_variant_cli_label(settings.fixed_solve_variant)
        if variant == "weights_only":
            args.append("--nTransIters=0")
        elif variant == "transforms_only":
            args.append("--nWeightsIters=0")
        return args

    return [
        f"-i={prepared.rest_fbx}",
        f"-a={prepared.anim_abc}",
        f"-b={int(settings.bones)}",
        f"--bindUpdate={int(settings.bind_update)}",
        f"--nnz={int(settings.nnz)}",
        f"--nInitIters={int(settings.n_init_iters)}",
        f"--nIters={int(settings.n_iters)}",
        f"--tolerance={float(settings.tolerance)}",
        f"--patience={int(settings.patience)}",
        f"-o={prepared.out_fbx}",
    ]


def export_result_fbx(
    prepared: PreparedRun,
    settings: CliRunSettings,
    log,
    import_result: dict | None = None,
) -> dict:
    if not os.path.isfile(prepared.out_fbx):
        raise RuntimeError("Result FBX not found for export: {0}".format(prepared.out_fbx))

    export_root = (settings.result_export_root or "").strip() or default_result_export_root()
    export_dir = Path(export_root)
    export_dir_before = directory_snapshot(export_dir)
    source_state = path_state(prepared.out_fbx)
    log("result_export_root_requested: {0}".format(settings.result_export_root))
    log("result_export_root_resolved: {0}".format(str(export_dir)))
    log("result_export_source_state: {0}".format(json.dumps(source_state, sort_keys=True)))
    log("result_export_dir_before: {0}".format(json.dumps(export_dir_before, sort_keys=True)))
    export_dir.mkdir(parents=True, exist_ok=True)

    naming = resolve_export_naming(
        settings.fbx_name,
        settings.clip_prefix,
        prepared.selected.shape,
    )
    dst_name = "{0}.fbx".format(naming.file_stem)
    dst = export_dir / dst_name
    export_method = "clean_scene_mayapy_reexport"
    export_nodes: list[str] = []
    cleanup_nodes: list[str] = []
    cleanup_namespace = ""
    cli_out_probe: dict = {}
    deliverable_probe: dict = {}
    if import_result is not None:
        log("result_export_scene_source_ignored: deliverable export runs in a clean standalone Maya scene")
    if dst.exists():
        try:
            dst.unlink()
        except Exception:
            pass
    log("result_export_source_name: {0}".format(naming.source_name))
    log("result_export_base_name: {0}".format(naming.base_name))
    log("result_export_file_stem: {0}".format(naming.file_stem))
    expected_vtx = int(cmds.polyEvaluate(prepared.selected.shape, vertex=True))
    cli_out_probe = probe_fbx_quality(
        prepared.out_fbx,
        expected_vtx,
        prepared,
        settings,
        path_label="cli_out_probe",
    )
    log("cli_out_probe: {0}".format(json.dumps(cli_out_probe, sort_keys=True)))
    log(
        "cli_out_root_basis: {0}".format(
            json.dumps(
                contract_root_basis_summary(cli_out_probe.get("contractProbe") or {}),
                sort_keys=True,
            )
        )
    )
    log(
        "cli_out_bind_scale_consistency: {0}".format(
            json.dumps(cli_out_probe.get("bindScaleConsistency") or {}, sort_keys=True)
        )
    )
    # Production runtime exports stock CLI output through the clean-scene FBX path only.
    # Custom hierarchy post-solve correction experiments were archived on 2026-05-25.
    helper_info = run_clean_scene_deliverable_export(
        prepared,
        settings,
        str(dst),
        naming.node_prefix,
        log,
    )
    helper_export_nodes = ((helper_info.get("helperResult") or {}).get("exportNodes") or [])
    export_nodes = [str(node) for node in helper_export_nodes if str(node or "").strip()]
    dst_state = path_state(dst)
    export_dir_after = directory_snapshot(export_dir)
    deliverable_probe = probe_fbx_quality(
        str(dst),
        expected_vtx,
        prepared,
        settings,
        path_label="deliverable_probe",
    )
    log("deliverable_probe: {0}".format(json.dumps(deliverable_probe, sort_keys=True)))
    log(
        "deliverable_root_basis: {0}".format(
            json.dumps(
                contract_root_basis_summary(deliverable_probe.get("contractProbe") or {}),
                sort_keys=True,
            )
        )
    )
    log(
        "deliverable_bind_scale_consistency: {0}".format(
            json.dumps(deliverable_probe.get("bindScaleConsistency") or {}, sort_keys=True)
        )
    )
    if str(prepared.solve_mode or "").strip() == "fixed_bones":
        should_fallback, fallback_reason = should_fallback_to_cli_out(cli_out_probe, deliverable_probe)
        log("result_export_fallback_check: {0}".format(fallback_reason))
        if should_fallback:
            shutil.copy2(prepared.out_fbx, str(dst))
            export_method = "direct_cli_out_copy_fallback"
            dst_state = path_state(dst)
            export_dir_after = directory_snapshot(export_dir)
            deliverable_probe = probe_fbx_quality(
                str(dst),
                expected_vtx,
                prepared,
                settings,
                path_label="deliverable_probe_after_fallback",
            )
            log("result_export_fallback_to_cli_out: true")
            log("deliverable_probe_after_fallback: {0}".format(json.dumps(deliverable_probe, sort_keys=True)))
            log(
                "deliverable_probe_after_fallback_root_basis: {0}".format(
                    json.dumps(
                        contract_root_basis_summary(deliverable_probe.get("contractProbe") or {}),
                        sort_keys=True,
                    )
                )
            )
            log(
                "deliverable_probe_after_fallback_bind_scale_consistency: {0}".format(
                    json.dumps(deliverable_probe.get("bindScaleConsistency") or {}, sort_keys=True)
                )
            )

    log("result_export_method: {0}".format(export_method))
    if export_nodes:
        log("result_export_nodes: {0}".format(", ".join(export_nodes)))
    log("result_export_destination: {0}".format(str(dst)))
    log("result_export_destination_state: {0}".format(json.dumps(dst_state, sort_keys=True)))
    log("result_export_dir_after: {0}".format(json.dumps(export_dir_after, sort_keys=True)))
    log("result_export_fbx: {0}".format(str(dst)))
    if not dst_state["exists"]:
        raise RuntimeError("Result FBX export copy did not create the destination file: {0}".format(str(dst)))

    size_match = int(source_state.get("sizeBytes", 0) or 0) == int(dst_state.get("sizeBytes", 0) or 0)
    if not size_match:
        log(
            "result_export_size_mismatch: src={0} dst={1}".format(
                int(source_state.get("sizeBytes", 0) or 0),
                int(dst_state.get("sizeBytes", 0) or 0),
            )
        )

    return {
        "path": str(dst),
        "exportMethod": export_method,
        "exportNodes": export_nodes,
        "cleanupNodes": cleanup_nodes,
        "cleanupNamespace": cleanup_namespace,
        "cleanSceneHelper": helper_info,
        "cliOutProbe": cli_out_probe,
        "deliverableProbe": deliverable_probe,
        "requestedRoot": settings.result_export_root,
        "resolvedRoot": str(export_dir),
        "resolvedBaseName": naming.base_name,
        "resolvedFileStem": naming.file_stem,
        "resolvedNodePrefix": naming.node_prefix,
        "source": source_state,
        "destination": dst_state,
        "directoryBefore": export_dir_before,
        "directoryAfter": export_dir_after,
        "sizeMatch": bool(size_match),
    }


def _import_fbx_result(
    fbx_path: str,
    expected_vertex_count: int,
    namespace: str,
    settings: CliRunSettings,
    log,
    *,
    path_label: str,
) -> dict:
    if not os.path.isfile(fbx_path):
        raise RuntimeError("FBX not found: {0}".format(fbx_path))

    has_anim_tokens, token_counts = fbx_animation_token_probe(fbx_path)
    log("{0}_has_anim_tokens: {1}".format(path_label, has_anim_tokens))
    log("{0}_state: {1}".format(path_label, json.dumps(path_state(fbx_path), sort_keys=True)))
    if token_counts:
        token_text = ", ".join("{0}={1}".format(k, token_counts[k]) for k in sorted(token_counts.keys()))
        log("{0}_token_counts: {1}".format(path_label, token_text))

    resolved_ns, ns_note = resolve_import_namespace(namespace)
    log(ns_note)

    new_nodes, used_ns, method = import_cli_fbx(
        fbx_path,
        resolved_ns,
        settings.frame_start,
        settings.frame_end,
        settings.frame_step,
        log,
    )

    cleanup_unwanted_dynamic_nodes(new_nodes, log)
    new_nodes = [n for n in new_nodes if cmds.objExists(n)]

    validation: ImportValidation = validate_imported_result(
        new_nodes,
        expected_vertex_count=expected_vertex_count,
        expect_animation=has_anim_tokens,
        start_f=settings.frame_start,
        end_f=settings.frame_end,
        log=log,
    )

    if not validation.success:
        issue_text = "; ".join(validation.issues) if validation.issues else "unknown import validation failure"
        log("import_validation_failed: {0}".format(issue_text))
        if not settings.keep_imported_cli:
            cleanup_imported_nodes(new_nodes, used_ns)
            log("import_validation_cleanup: imported nodes removed")
        raise RuntimeError("Imported FBX failed validation: {0}".format(issue_text))

    log("import_namespace: {0}".format(used_ns))
    log("import_method_final: {0}".format(method))
    log("import_new_nodes: {0}".format(len(new_nodes)))
    log("import_joints: {0}".format(len(validation.joints)))
    log("import_mesh_shapes: {0}".format(len(validation.meshes)))
    log("import_animCurves: {0}".format(len(validation.anim_curves)))
    log("import_keyed_joints: {0}".format(validation.keyed_joints))
    log("import_total_joint_keys: {0}".format(validation.total_joint_keys))
    log("import_joint_hierarchy: {0}".format(json.dumps(validation.joint_hierarchy, sort_keys=True)))
    import_contract_probe = summarize_imported_fbx_contract_nodes(
        new_nodes,
        expected_vertex_count,
        path=fbx_path,
        probe_namespace=used_ns,
        frame=settings.frame_start,
    )
    import_joint_probe = joint_visual_probe(
        validation.joints,
        str(import_contract_probe.get("chosenMeshShape") or ""),
        str(import_contract_probe.get("chosenRootJoint") or ""),
        settings.frame_start,
        settings.frame_end,
        settings.frame_step,
    )
    log("import_contract_probe: {0}".format(json.dumps(import_contract_probe, sort_keys=True)))
    log(
        "import_contract_root_basis: {0}".format(
            json.dumps(contract_root_basis_summary(import_contract_probe), sort_keys=True)
        )
    )
    log(
        "import_contract_bind_scale_consistency: {0}".format(
            json.dumps(
                bind_matrix_scale_consistency_summary(
                    import_contract_probe.get("skinBindMatrixProbe") or {}
                ),
                sort_keys=True,
            )
        )
    )
    log("import_joint_visual_probe: {0}".format(json.dumps(import_joint_probe, sort_keys=True)))
    log("import_validation_success: true")
    return {
        "namespace": used_ns,
        "method": method,
        "new_nodes": new_nodes,
        "joints": validation.joints,
        "meshes": validation.meshes,
        "anim_curves": validation.anim_curves,
        "keyed_joints": validation.keyed_joints,
        "total_joint_keys": validation.total_joint_keys,
        "validation_issues": validation.issues,
        "chosen_mesh": validation.chosen_mesh,
        "chosen_mesh_motion_max": validation.chosen_mesh_motion_max,
        "expected_animation": validation.expected_animation,
        "joint_hierarchy": validation.joint_hierarchy,
        "contract_probe": import_contract_probe,
        "joint_visual_probe": import_joint_probe,
        "token_counts": token_counts,
        "out_fbx_state": path_state(fbx_path),
    }


def import_cli_result(prepared: PreparedRun, namespace: str, settings: CliRunSettings, log) -> dict:
    expected_vtx = int(cmds.polyEvaluate(prepared.selected.shape, vertex=True))
    result = _import_fbx_result(
        prepared.out_fbx,
        expected_vtx,
        namespace,
        settings,
        log,
        path_label="out_fbx",
    )
    ratio = motion_ratio(prepared.source_motion_probe, float(result.get("chosen_mesh_motion_max") or 0.0))
    log("import_motion_ratio_vs_source: {0}".format(ratio))
    result["motion_ratio_vs_source"] = float(ratio) if ratio is not None else None
    if str(prepared.solve_mode or "").strip() == "fixed_bones":
        baseline_probe = (prepared.debug_info or {}).get("initFbxProbe") or {}
        candidate_probe = result.get("contract_probe") or {}
        if baseline_probe and candidate_probe:
            contract_compare = compare_contract_probes(baseline_probe, candidate_probe)
            log(
                "fixed_result_vs_init_contract: {0}".format(
                    json.dumps(contract_compare, sort_keys=True)
                )
            )
            result["fixed_contract_compare"] = contract_compare
    return result


def import_exported_result(
    prepared: PreparedRun,
    exported_fbx_path: str,
    namespace: str,
    settings: CliRunSettings,
    log,
) -> dict:
    expected_vtx = int(cmds.polyEvaluate(prepared.selected.shape, vertex=True))
    result = _import_fbx_result(
        exported_fbx_path,
        expected_vtx,
        namespace,
        settings,
        log,
        path_label="result_fbx",
    )
    ratio = motion_ratio(prepared.source_motion_probe, float(result.get("chosen_mesh_motion_max") or 0.0))
    log("result_import_motion_ratio_vs_source: {0}".format(ratio))
    result["motion_ratio_vs_source"] = float(ratio) if ratio is not None else None
    if str(prepared.solve_mode or "").strip() == "fixed_bones":
        baseline_probe = (prepared.debug_info or {}).get("initFbxProbe") or {}
        candidate_probe = result.get("contract_probe") or {}
        if baseline_probe and candidate_probe:
            contract_compare = compare_contract_probes(baseline_probe, candidate_probe)
            log(
                "fixed_exported_result_vs_init_contract: {0}".format(
                    json.dumps(contract_compare, sort_keys=True)
                )
            )
            result["fixed_contract_compare"] = contract_compare
    chosen_mesh = str(result.get("chosen_mesh") or "")
    if chosen_mesh and cmds.objExists(prepared.selected.shape) and cmds.objExists(chosen_mesh):
        vertex_compare = compare_mesh_animation(
            prepared.selected.shape,
            chosen_mesh,
            settings.frame_start,
            settings.frame_end,
            settings.frame_step,
        )
        result["vertex_compare"] = vertex_compare
        log("result_vertex_compare: {0}".format(json.dumps(vertex_compare, sort_keys=True)))
    return result
