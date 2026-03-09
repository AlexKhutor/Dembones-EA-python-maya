from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from pathlib import Path

import maya.cmds as cmds
import maya.mel as mel

from .mesh_probe import probe_motion
from .models import CliRunSettings, PreparedRun
from .pipeline_import import (
    cleanup_imported_nodes,
    cleanup_unwanted_dynamic_nodes,
    fbx_animation_token_probe,
    import_cli_fbx,
    next_namespace,
    resolve_import_namespace,
)
from .paths import default_result_export_root
from .selection import resolve_selected_mesh_with_deformers


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_") or "mesh"


def _ensure_maya_io_plugins() -> None:
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")
    if not cmds.pluginInfo("AbcExport", query=True, loaded=True):
        cmds.loadPlugin("AbcExport")
    if not cmds.pluginInfo("AbcImport", query=True, loaded=True):
        cmds.loadPlugin("AbcImport")


def _export_fbx_selection(path: str, nodes: list[str]) -> None:
    if not nodes:
        raise RuntimeError("FBX export failed: empty node list.")

    export_path = path.replace("\\", "/")
    cmds.select(clear=True)
    cmds.select(nodes, replace=True)
    mel.eval("FBXResetExport;")
    mel.eval("FBXExportSmoothingGroups -v true;")
    mel.eval("FBXExportShapes -v true;")
    mel.eval("FBXExportSkins -v false;")
    # Avoid exporting upstream DG networks (nCloth/nucleus/etc.) with the mesh.
    try:
        mel.eval("FBXExportInputConnections -v false;")
    except Exception:
        pass
    mel.eval("FBXExportAnimationOnly -v false;")
    mel.eval('FBXExport -f "{0}" -s;'.format(export_path))

    if not os.path.exists(path):
        raise RuntimeError("FBX export failed: file not created: {0}".format(path))


def _export_alembic(path: str, root_transform: str, start_f: int, end_f: int, step_f: int) -> None:
    abc_path = path.replace("\\", "/")
    root_path = root_transform.replace("\\", "/")
    job = (
        "-frameRange {0} {1} -step {2} "
        "-uvWrite -worldSpace -writeVisibility -dataFormat ogawa "
        '-root "{3}" -file "{4}"'
    ).format(int(start_f), int(end_f), int(step_f), root_path, abc_path)
    cmds.AbcExport(jobArg=job)
    if not os.path.exists(path):
        raise RuntimeError("Alembic export failed: file not created: {0}".format(path))


def _import_alembic_result(path: str, namespace: str) -> list[str]:
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


def _find_imported_mesh_shape(
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


def _copy_latest(src: str, latest_path: str) -> None:
    os.makedirs(os.path.dirname(latest_path), exist_ok=True)
    shutil.copy2(src, latest_path)


def _format_probe(probe: dict[int, float]) -> str:
    return ", ".join("{0}:{1:.6f}".format(k, probe[k]) for k in sorted(probe.keys()))


def prepare_run(settings: CliRunSettings, log) -> PreparedRun:
    if not os.path.isfile(settings.cli_exe):
        raise RuntimeError("CLI не найден: {0}".format(settings.cli_exe))
    if settings.frame_end < settings.frame_start:
        raise RuntimeError("Frame End должен быть >= Frame Start.")

    _ensure_maya_io_plugins()
    selected = resolve_selected_mesh_with_deformers()

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
    log("selection_deformer_types: {0}".format(", ".join(selected.deformer_types)))

    motion_probe = probe_motion(selected.shape, settings.frame_start, settings.frame_end)
    log("source_motion_probe: {0}".format(_format_probe(motion_probe)))

    log("export_rest_fbx: {0}".format(rest_fbx))
    cmds.currentTime(settings.frame_start, edit=True)
    cmds.refresh(force=True)
    _export_fbx_selection(rest_fbx, [selected.transform])

    log("export_anim_abc: {0}".format(anim_abc))
    _export_alembic(anim_abc, selected.transform, settings.frame_start, settings.frame_end, settings.frame_step)
    abc_size = os.path.getsize(anim_abc) if os.path.exists(anim_abc) else -1
    log("abc_export_size_bytes: {0}".format(abc_size))

    # Probe exported Alembic in-scene to ensure it actually contains motion.
    probe_ns = next_namespace("db_export_abcProbe")
    abc_nodes: list[str] = []
    try:
        abc_nodes = _import_alembic_result(anim_abc, probe_ns)
        expected_vtx = int(cmds.polyEvaluate(selected.shape, vertex=True))
        abc_shape, candidates = _find_imported_mesh_shape(
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

    _copy_latest(rest_fbx, str(latest_dir / f"{shape_leaf}_rest_latest.fbx"))
    _copy_latest(anim_abc, str(latest_dir / f"{shape_leaf}_anim_latest.abc"))

    manifest = {
        "run_id": run_id,
        "shape": selected.shape,
        "transform": selected.transform,
        "deformer_types": selected.deformer_types,
        "frame_range": [settings.frame_start, settings.frame_end, settings.frame_step],
        "files": {"rest_fbx": rest_fbx, "anim_abc": anim_abc, "out_fbx": out_fbx},
        "motion_probe": motion_probe,
        "created_at": datetime.datetime.now().isoformat(),
    }
    with open(latest_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return PreparedRun(
        run_id=run_id,
        selected=selected,
        rest_fbx=rest_fbx,
        anim_abc=anim_abc,
        out_fbx=out_fbx,
        run_dir=str(run_dir),
        latest_manifest=latest_manifest,
        source_motion_probe=motion_probe,
    )


def build_cli_args(settings: CliRunSettings, prepared: PreparedRun) -> list[str]:
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


def export_result_fbx(prepared: PreparedRun, settings: CliRunSettings, log) -> str:
    if not os.path.isfile(prepared.out_fbx):
        raise RuntimeError("Result FBX not found for export: {0}".format(prepared.out_fbx))

    export_root = (settings.result_export_root or "").strip() or default_result_export_root()
    export_dir = Path(export_root)
    export_dir.mkdir(parents=True, exist_ok=True)

    shape_leaf = _safe_name(prepared.selected.shape.split("|")[-1])
    dst_name = "{0}_cli_{1}.fbx".format(shape_leaf, prepared.run_id)
    dst = export_dir / dst_name
    shutil.copy2(prepared.out_fbx, dst)

    log("result_export_fbx: {0}".format(str(dst)))
    return str(dst)


def import_cli_result(prepared: PreparedRun, namespace: str, settings: CliRunSettings, log) -> dict:
    if not os.path.isfile(prepared.out_fbx):
        raise RuntimeError("CLI output FBX не найден: {0}".format(prepared.out_fbx))

    has_anim_tokens, token_counts = fbx_animation_token_probe(prepared.out_fbx)
    log("out_fbx_has_anim_tokens: {0}".format(has_anim_tokens))
    if token_counts:
        token_text = ", ".join("{0}={1}".format(k, token_counts[k]) for k in sorted(token_counts.keys()))
        log("out_fbx_token_counts: {0}".format(token_text))

    resolved_ns, ns_note = resolve_import_namespace(namespace)
    log(ns_note)

    new_nodes, used_ns, method = import_cli_fbx(
        prepared.out_fbx,
        resolved_ns,
        settings.frame_start,
        settings.frame_end,
        settings.frame_step,
        log,
    )

    _cleanup_unwanted_dynamic_nodes(new_nodes, log)
    # Re-query nodes after cleanup.
    new_nodes = [n for n in new_nodes if cmds.objExists(n)]

    joints = cmds.ls(new_nodes, type="joint", long=True) or []
    meshes = cmds.ls(new_nodes, type="mesh", long=True) or []
    anim_curves = cmds.ls(
        new_nodes, type=("animCurveTL", "animCurveTA", "animCurveTU", "animCurve"), long=True
    ) or []
    keyed_joints, total_joint_keys = _joint_key_stats(joints)

    log("import_namespace: {0}".format(used_ns))
    log("import_method_final: {0}".format(method))
    log("import_new_nodes: {0}".format(len(new_nodes)))
    log("import_joints: {0}".format(len(joints)))
    log("import_mesh_shapes: {0}".format(len(meshes)))
    log("import_animCurves: {0}".format(len(anim_curves)))
    log("import_keyed_joints: {0}".format(keyed_joints))
    log("import_total_joint_keys: {0}".format(total_joint_keys))
    return {
        "namespace": used_ns,
        "method": method,
        "new_nodes": new_nodes,
        "joints": joints,
        "meshes": meshes,
        "anim_curves": anim_curves,
        "keyed_joints": keyed_joints,
        "total_joint_keys": total_joint_keys,
    }
