from __future__ import annotations

from dataclasses import dataclass

import maya.cmds as cmds


_EXPLICIT_DEFORMER_TYPES = {
    "nCloth",
    "blendShape",
    "skinCluster",
    "cluster",
    "deltaMush",
    "nonLinear",
    "wire",
    "ffd",
    "lattice",
    "wrap",
    "proximityWrap",
    "sculpt",
    "jiggle",
}


@dataclass
class SelectedMeshInfo:
    transform: str
    shape: str
    deformers: list[str]
    deformer_types: list[str]


def _strip_component(path: str) -> str:
    if "." in path:
        return path.split(".", 1)[0]
    return path


def _renderable_mesh_shapes_from_node(node: str) -> list[str]:
    node_type = cmds.nodeType(node)
    if node_type == "mesh":
        if not cmds.getAttr(node + ".intermediateObject"):
            return [node]
        return []

    if node_type != "transform":
        return []

    shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or []
    out = []
    for shape in shapes:
        if cmds.nodeType(shape) != "mesh":
            continue
        if cmds.getAttr(shape + ".intermediateObject"):
            continue
        out.append(shape)
    return out


def _has_deformer_trait(node: str) -> bool:
    try:
        inherited = cmds.nodeType(node, inherited=True) or []
    except Exception:
        inherited = []
    if "geometryFilter" in inherited:
        return True
    try:
        node_type = cmds.nodeType(node)
    except Exception:
        return False
    return node_type in _EXPLICIT_DEFORMER_TYPES


def _collect_deformers(shape: str) -> tuple[list[str], list[str]]:
    history = cmds.listHistory(shape, pruneDagObjects=True) or []
    deformers = []
    deformer_types = []
    seen = set()

    for node in history:
        if node in seen:
            continue
        if _has_deformer_trait(node):
            seen.add(node)
            deformers.append(node)
            try:
                deformer_types.append(cmds.nodeType(node))
            except Exception:
                deformer_types.append("unknown")

    # Dynamic pipelines can drive inMesh from transform-level sources.
    # We normalize transform sources to their shape drivers to avoid noisy
    # types like "transform" in diagnostics.
    in_mesh_sources = cmds.listConnections(shape + ".inMesh", source=True, destination=False) or []
    for node in in_mesh_sources:
        source_nodes = [node]
        try:
            if cmds.nodeType(node) == "transform":
                child_shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or []
                if child_shapes:
                    source_nodes = child_shapes
        except Exception:
            source_nodes = [node]

        for source in source_nodes:
            if source in seen:
                continue
            seen.add(source)
            deformers.append(source)
            try:
                deformer_types.append(cmds.nodeType(source))
            except Exception:
                deformer_types.append("unknown")

    return deformers, deformer_types


def resolve_selected_mesh_with_deformers() -> SelectedMeshInfo:
    selected = cmds.ls(selection=True, long=True) or []
    if not selected:
        raise RuntimeError("Nothing selected. Select a deforming mesh shape or transform.")

    mesh_shapes = []
    for item in selected:
        node = _strip_component(item)
        if not cmds.objExists(node):
            continue
        mesh_shapes.extend(_renderable_mesh_shapes_from_node(node))

    mesh_shapes = sorted(set(mesh_shapes))
    if not mesh_shapes:
        raise RuntimeError("Selection does not contain a renderable mesh shape.")
    if len(mesh_shapes) > 1:
        raise RuntimeError(
            "Multiple mesh shapes selected. Leave only one shape or transform selected."
        )

    shape = mesh_shapes[0]
    parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
    if not parent:
        raise RuntimeError("Failed to resolve the transform for the selected shape.")
    transform = parent[0]

    deformers, deformer_types = _collect_deformers(shape)
    if not deformers:
        raise RuntimeError(
            "No deformers or dynamic inputs were found on the selected shape in history/inMesh."
        )

    return SelectedMeshInfo(
        transform=transform,
        shape=shape,
        deformers=deformers,
        deformer_types=deformer_types,
    )
