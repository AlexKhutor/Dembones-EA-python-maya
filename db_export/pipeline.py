from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import maya.api.OpenMaya as om2
import maya.cmds as cmds
import maya.mel as mel

from .selection import SelectedMeshInfo, resolve_selected_mesh_with_deformers


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_") or "mesh"


def _safe_namespace(value: str) -> str:
    ns = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "")).strip("_")
    if not ns:
        ns = "db_export_cli"
    if ns[0].isdigit():
        ns = "db_export_" + ns
    return ns


def _ensure_maya_io_plugins() -> None:
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")
    if not cmds.pluginInfo("AbcExport", query=True, loaded=True):
        cmds.loadPlugin("AbcExport")
    if not cmds.pluginInfo("AbcImport", query=True, loaded=True):
        cmds.loadPlugin("AbcImport")


def default_cache_root() -> str:
    user_app = Path(cmds.internalVar(userAppDir=True))
    maya_ver = str(cmds.about(version=True))
    return str(user_app / maya_ver / "DB_export" / "cache")


def default_result_export_root() -> str:
    user_app = Path(cmds.internalVar(userAppDir=True))
    maya_ver = str(cmds.about(version=True))
    return str(user_app / maya_ver / "DB_export" / "exports")


def default_cli_path() -> str:
    module_root = Path(__file__).resolve().parents[2]
    installed_cli = module_root / "bin" / "DemBones.exe"
    if installed_cli.exists():
        return str(installed_cli)

    repo_cli = (
        Path(__file__).resolve().parents[1]
        / "third_party"
        / "dem_bones_repo"
        / "bin"
        / "Windows"
        / "DemBones.exe"
    )
    return str(repo_cli)


@dataclass
class CliRunSettings:
    cli_exe: str
    cache_root: str
    result_export_root: str
    namespace: str
    import_result_in_scene: bool = True
    bones: int = 128
    bind_update: int = 2
    nnz: int = 8
    n_init_iters: int = 10
    n_iters: int = 100
    tolerance: float = 0.001
    patience: int = 3
    frame_start: int = 1
    frame_end: int = 60
    frame_step: int = 1
    debug_cli: bool = False
    keep_imported_cli: bool = True


@dataclass
class PreparedRun:
    run_id: str
    selected: SelectedMeshInfo
    rest_fbx: str
    anim_abc: str
    out_fbx: str
    run_dir: str
    latest_manifest: str
    source_motion_probe: dict[int, float]


def _mesh_points_world(shape: str) -> list[tuple[float, float, float]]:
    sel = om2.MSelectionList()
    sel.add(shape)
    dag = sel.getDagPath(0)
    fn_mesh = om2.MFnMesh(dag)
    points = fn_mesh.getPoints(om2.MSpace.kWorld)
    return [(p.x, p.y, p.z) for p in points]


def _distance_max(points_a: list[tuple[float, float, float]], points_b: list[tuple[float, float, float]]) -> float:
    if len(points_a) != len(points_b):
        raise RuntimeError("Vertex count mismatch in motion probe.")
    max_len = 0.0
    for i in range(len(points_a)):
        ax, ay, az = points_a[i]
        bx, by, bz = points_b[i]
        dx = ax - bx
        dy = ay - by
        dz = az - bz
        d = (dx * dx + dy * dy + dz * dz) ** 0.5
        if d > max_len:
            max_len = d
    return max_len


def _set_time(frame: int) -> None:
    cmds.currentTime(frame, edit=True)
    cmds.refresh(force=True)


def _probe_motion(shape: str, start_f: int, end_f: int) -> dict[int, float]:
    current = cmds.currentTime(query=True)
    try:
        sample_frames = [int(start_f), int(start_f + (end_f - start_f) // 2), int(end_f)]
        sample_frames = list(dict.fromkeys(sample_frames))
        _set_time(sample_frames[0])
        base = _mesh_points_world(shape)
        out = {sample_frames[0]: 0.0}
        for frame in sample_frames[1:]:
            _set_time(frame)
            points = _mesh_points_world(shape)
            out[frame] = _distance_max(base, points)
        return out
    finally:
        cmds.currentTime(current, edit=True)
        cmds.refresh(force=True)


def _next_namespace(base: str) -> str:
    candidate = base
    index = 1
    while cmds.namespace(exists=candidate):
        candidate = "{0}{1}".format(base, index)
        index += 1
    return candidate


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


def _cleanup_imported_nodes(new_nodes: list[str], namespace: str | None) -> None:
    roots = _root_paths_from_nodes(new_nodes)
    _delete_nodes_safe(roots)

    if namespace and cmds.namespace(exists=namespace):
        try:
            cmds.namespace(removeNamespace=namespace, mergeNamespaceWithRoot=True)
        except Exception:
            pass


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
            motion_probe = _probe_motion(shape, start_f, end_f)
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


def directory_size_bytes(path: str) -> int:
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return 0
    total = 0
    for item in p.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += int(item.stat().st_size)
        except Exception:
            continue
    return int(total)


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

    motion_probe = _probe_motion(selected.shape, settings.frame_start, settings.frame_end)
    log("source_motion_probe: {0}".format(_format_probe(motion_probe)))

    log("export_rest_fbx: {0}".format(rest_fbx))
    _set_time(settings.frame_start)
    _export_fbx_selection(rest_fbx, [selected.transform])

    log("export_anim_abc: {0}".format(anim_abc))
    _export_alembic(anim_abc, selected.transform, settings.frame_start, settings.frame_end, settings.frame_step)
    abc_size = os.path.getsize(anim_abc) if os.path.exists(anim_abc) else -1
    log("abc_export_size_bytes: {0}".format(abc_size))

    # Probe exported Alembic in-scene to ensure it actually contains motion.
    probe_ns = _next_namespace("db_export_abcProbe")
    abc_nodes: list[str] = []
    try:
        abc_nodes = _import_alembic_result(anim_abc, probe_ns)
        expected_vtx = int(cmds.polyEvaluate(selected.shape, vertex=True))
        abc_shape, candidates = _find_imported_mesh_shape(
            abc_nodes, expected_vtx, settings.frame_start, settings.frame_end
        )
        log("abc_probe_candidates: {0}".format(len(candidates)))
        if abc_shape:
            abc_probe = _probe_motion(abc_shape, settings.frame_start, settings.frame_end)
            abc_motion_max = max(abc_probe.values()) if abc_probe else 0.0
            log("abc_probe_shape: {0}".format(abc_shape))
            log("abc_probe_motion: {0}".format(_format_probe(abc_probe)))
            log("abc_export_has_animation: {0}".format(bool(abc_motion_max > 1e-6)))
        else:
            log("abc_probe_warning: Alembic imported but no probe mesh candidate found.")
    except Exception as exc:
        log("abc_probe_error: {0}".format(exc))
    finally:
        _cleanup_imported_nodes(abc_nodes, probe_ns)

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


def resolve_import_namespace(raw_namespace: str) -> tuple[str, str]:
    requested = _safe_namespace(raw_namespace)
    if cmds.namespace(exists=requested):
        resolved = _next_namespace(requested)
        return resolved, "namespace_conflict: '{0}' -> '{1}'".format(requested, resolved)
    return requested, "namespace_selected: '{0}'".format(requested)


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


def _cleanup_unwanted_dynamic_nodes(new_nodes: list[str], log):
    # Defensive cleanup: imported FBX can occasionally contain stray dynamic nodes
    # (nCloth/nucleus/nRigid/dynamicConstraint) that are irrelevant for output rig.
    dynamic_types = ["nCloth", "nucleus", "nRigid", "dynamicConstraint"]
    dynamic_nodes = cmds.ls(new_nodes, type=dynamic_types, long=True) or []
    if dynamic_nodes:
        try:
            cmds.delete(dynamic_nodes)
            log("cleanup_dynamic_nodes: removed {0}".format(len(dynamic_nodes)))
        except Exception as exc:
            log("cleanup_dynamic_nodes_failed: {0}".format(exc))

    # Extra guard for names like "<ns>:nCloth1" that may appear as transforms.
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


def _import_cli_fbx(path: str, namespace: str, start_f: int, end_f: int, step_f: int, log):
    methods = [
        ("mel_fbximport", lambda ns: _import_cli_fbx_mel(path, ns, start_f, end_f, step_f, log)),
        ("cmds_file_import", lambda ns: _import_cli_fbx_cmds(path, ns)),
    ]

    last_nodes: list[str] = []
    last_ns = namespace
    last_method = "none"

    for idx, (method_name, importer) in enumerate(methods):
        ns = namespace if idx == 0 else _next_namespace(namespace + "_retry_")
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

        _cleanup_imported_nodes(new_nodes, ns)

    return last_nodes, last_ns, last_method


def _fbx_animation_token_probe(path: str) -> tuple[bool, dict[str, int]]:
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

    has_anim_tokens, token_counts = _fbx_animation_token_probe(prepared.out_fbx)
    log("out_fbx_has_anim_tokens: {0}".format(has_anim_tokens))
    if token_counts:
        token_text = ", ".join("{0}={1}".format(k, token_counts[k]) for k in sorted(token_counts.keys()))
        log("out_fbx_token_counts: {0}".format(token_text))

    resolved_ns, ns_note = resolve_import_namespace(namespace)
    log(ns_note)

    new_nodes, used_ns, method = _import_cli_fbx(
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
