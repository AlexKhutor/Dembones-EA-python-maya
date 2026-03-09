from __future__ import annotations

import maya.api.OpenMaya as om2
import maya.cmds as cmds


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


def probe_motion(shape: str, start_f: int, end_f: int) -> dict[int, float]:
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
