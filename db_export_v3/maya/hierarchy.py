from __future__ import annotations

from dataclasses import dataclass

import maya.cmds as cmds


@dataclass
class JointHierarchyInfo:
    root: str
    joints: list[str]
    max_depth: int
    leaf_count: int
    duplicate_short_names: dict[str, int]
    namespaces: list[str]
    parent_map: dict[str, str]

    @property
    def joint_count(self) -> int:
        return len(self.joints)

    @property
    def sample_joints(self) -> list[str]:
        return list(self.joints[:25])


def _unique_long_nodes(nodes: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for node in nodes or []:
        long_names = cmds.ls(node, long=True) or []
        if not long_names:
            continue
        long_name = long_names[0]
        if long_name in seen:
            continue
        seen.add(long_name)
        out.append(long_name)
    return out


def resolve_selected_joint_root() -> str:
    joints = _unique_long_nodes(cmds.ls(selection=True, long=True, type="joint") or [])
    if not joints:
        raise RuntimeError("No joint selected. Select the root joint for the fixed hierarchy.")
    if len(joints) != 1:
        raise RuntimeError("Multiple joints selected. Select exactly one root joint.")
    return joints[0]


def resolve_joint_root(node_name: str) -> str:
    raw = str(node_name or "").strip()
    if not raw:
        raise RuntimeError("Fixed hierarchy root is empty.")
    matches = _unique_long_nodes(cmds.ls(raw, long=True, type="joint") or [])
    if not matches:
        raise RuntimeError("Fixed hierarchy root joint not found: {0}".format(raw))
    if len(matches) != 1:
        raise RuntimeError("Fixed hierarchy root is ambiguous: {0}".format(raw))
    return matches[0]


def describe_joint_hierarchy(root_joint: str) -> JointHierarchyInfo:
    root = resolve_joint_root(root_joint)
    descendants = cmds.listRelatives(root, allDescendents=True, fullPath=True, type="joint") or []
    joints = [root] + list(reversed(_unique_long_nodes(descendants)))
    joints = _unique_long_nodes(joints)

    duplicate_short_names: dict[str, int] = {}
    namespaces: set[str] = set()
    parent_map: dict[str, str] = {}
    max_depth = 0
    leaf_count = 0

    short_name_counts: dict[str, int] = {}
    joint_set = set(joints)
    for joint in joints:
        short_name = joint.split("|")[-1]
        short_name_counts[short_name] = short_name_counts.get(short_name, 0) + 1
        if ":" in short_name:
            namespaces.add(short_name.split(":", 1)[0])

        parents = cmds.listRelatives(joint, parent=True, fullPath=True, type="joint") or []
        parent_joint = parents[0] if parents and parents[0] in joint_set else ""
        parent_map[joint] = parent_joint

        depth = 0
        current = joint
        while current != root:
            parent_name = parent_map.get(current, "")
            if not parent_name:
                break
            depth += 1
            current = parent_name
        max_depth = max(max_depth, depth)

        child_joints = cmds.listRelatives(joint, children=True, fullPath=True, type="joint") or []
        if not child_joints:
            leaf_count += 1

    duplicate_short_names = {
        name: count for name, count in short_name_counts.items() if count > 1
    }

    return JointHierarchyInfo(
        root=root,
        joints=joints,
        max_depth=max_depth,
        leaf_count=leaf_count,
        duplicate_short_names=duplicate_short_names,
        namespaces=sorted(namespaces),
        parent_map=parent_map,
    )


def find_skin_cluster(shape: str) -> str:
    history = cmds.listHistory(shape, pruneDagObjects=True) or []
    skins = _unique_long_nodes(cmds.ls(history, type="skinCluster", long=True) or [])
    if not skins:
        return ""
    return skins[0]


def find_bind_pose_for_skin_cluster(skin_cluster: str) -> str:
    raw = str(skin_cluster or "").strip()
    if not raw or not cmds.objExists(raw):
        return ""
    connections = cmds.listConnections(raw + ".bindPose", source=True, destination=False) or []
    poses = _unique_long_nodes(cmds.ls(connections, type="dagPose", long=True) or [])
    if not poses:
        return ""
    return poses[0]


def describe_skin_cluster(shape: str, hierarchy_joints: list[str]) -> dict:
    skin = find_skin_cluster(shape)
    if not skin:
        return {
            "exists": False,
            "skinCluster": "",
            "bindPoseNode": "",
            "influences": [],
            "influenceCount": 0,
            "extraInfluencesOutsideHierarchy": [],
            "hierarchyJointsWithoutInfluence": list(hierarchy_joints),
        }

    influences = _unique_long_nodes(cmds.skinCluster(skin, query=True, influence=True) or [])
    hierarchy_set = set(hierarchy_joints)
    influence_set = set(influences)
    extra = [joint for joint in influences if joint not in hierarchy_set]
    missing = [joint for joint in hierarchy_joints if joint not in influence_set]
    return {
        "exists": True,
        "skinCluster": skin,
        "bindPoseNode": find_bind_pose_for_skin_cluster(skin),
        "influences": influences,
        "influenceCount": len(influences),
        "extraInfluencesOutsideHierarchy": extra,
        "hierarchyJointsWithoutInfluence": missing,
    }


def describe_skin_weight_distribution(
    shape: str,
    skin_cluster: str,
    root_joint: str,
    *,
    epsilon: float = 1e-6,
    top_limit: int = 12,
) -> dict:
    raw_shape = str(shape or "").strip()
    raw_skin = str(skin_cluster or "").strip()
    if not raw_shape or not raw_skin or not cmds.objExists(raw_shape) or not cmds.objExists(raw_skin):
        return {
            "exists": False,
            "shape": raw_shape,
            "skinCluster": raw_skin,
        }

    influences = _unique_long_nodes(cmds.skinCluster(raw_skin, query=True, influence=True) or [])
    vertex_count = int(cmds.polyEvaluate(raw_shape, vertex=True) or 0)
    weight_mass = {joint: 0.0 for joint in influences}
    max_weight = {joint: 0.0 for joint in influences}
    affected_vertices = {joint: 0 for joint in influences}
    weight_sum_values: list[float] = []
    invalid_vertex_count = 0

    for index in range(vertex_count):
        component = "{0}.vtx[{1}]".format(raw_shape, index)
        try:
            values = cmds.skinPercent(raw_skin, component, query=True, value=True) or []
        except Exception:
            invalid_vertex_count += 1
            continue
        if len(values) != len(influences):
            invalid_vertex_count += 1
            continue
        total = float(sum(values))
        weight_sum_values.append(total)
        for joint, value in zip(influences, values):
            value_f = float(value)
            weight_mass[joint] += value_f
            if value_f > max_weight[joint]:
                max_weight[joint] = value_f
            if value_f > float(epsilon):
                affected_vertices[joint] += 1

    sorted_by_mass = sorted(
        influences,
        key=lambda joint: (weight_mass[joint], max_weight[joint], affected_vertices[joint]),
        reverse=True,
    )
    top_influences = [
        {
            "joint": joint,
            "weightMass": float(weight_mass[joint]),
            "maxWeight": float(max_weight[joint]),
            "affectedVertexCount": int(affected_vertices[joint]),
        }
        for joint in sorted_by_mass[: max(1, int(top_limit))]
    ]
    near_zero_influences = [
        joint for joint in influences if abs(weight_mass[joint]) <= float(epsilon)
    ]
    root = resolve_joint_root(root_joint) if str(root_joint or "").strip() else ""
    return {
        "exists": True,
        "shape": raw_shape,
        "skinCluster": raw_skin,
        "vertexCount": int(vertex_count),
        "influenceCount": int(len(influences)),
        "invalidVertexCount": int(invalid_vertex_count),
        "weightSumMin": float(min(weight_sum_values)) if weight_sum_values else None,
        "weightSumMax": float(max(weight_sum_values)) if weight_sum_values else None,
        "weightSumMean": (
            float(sum(weight_sum_values) / len(weight_sum_values)) if weight_sum_values else None
        ),
        "rootJoint": root,
        "rootInfluencePresent": bool(root and root in weight_mass),
        "rootWeightMass": float(weight_mass.get(root, 0.0)) if root else 0.0,
        "rootMaxWeight": float(max_weight.get(root, 0.0)) if root else 0.0,
        "rootAffectedVertexCount": int(affected_vertices.get(root, 0)) if root else 0,
        "nearZeroInfluenceCount": int(len(near_zero_influences)),
        "nearZeroInfluenceSamples": near_zero_influences[:12],
        "topInfluencesByWeightMass": top_influences,
    }


def describe_alembic_sources(shape: str) -> list[dict]:
    history = cmds.listHistory(shape, pruneDagObjects=True) or []
    nodes = _unique_long_nodes(cmds.ls(history, type="AlembicNode", long=True) or [])
    out: list[dict] = []
    for node in nodes:
        path = ""
        for attr in ("abc_File", "abc_FileName"):
            if not cmds.attributeQuery(attr, node=node, exists=True):
                continue
            try:
                path = str(cmds.getAttr(node + "." + attr) or "")
            except Exception:
                path = ""
            if path:
                break
        out.append(
            {
                "node": node,
                "filePath": path,
            }
        )
    return out
