from __future__ import annotations

from dataclasses import dataclass, field

from ..maya.selection import SelectedMeshInfo


@dataclass
class CliRunSettings:
    cli_exe: str
    cache_root: str
    result_export_root: str
    namespace: str
    solve_mode: str = "auto"
    fixed_solve_variant: str = "optimize_existing"
    source_animated_mesh: str = ""
    bound_init_mesh: str = ""
    hierarchy_root: str = ""
    fbx_name: str = ""
    clip_prefix: str = ""
    import_result_in_scene: bool = True
    wrap_world_root: bool = False
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
    write_debug_logs: bool = False
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
    solve_mode: str = "auto"
    expected_joint_count: int = 0
    fixed_hierarchy_root: str = ""
    locked_scene_nodes: list[str] = field(default_factory=list)
    debug_info: dict = field(default_factory=dict)


@dataclass
class ImportValidation:
    success: bool
    issues: list[str] = field(default_factory=list)
    root_nodes: list[str] = field(default_factory=list)
    joints: list[str] = field(default_factory=list)
    meshes: list[str] = field(default_factory=list)
    anim_curves: list[str] = field(default_factory=list)
    keyed_joints: int = 0
    total_joint_keys: int = 0
    mesh_candidates: list[dict] = field(default_factory=list)
    chosen_mesh: str | None = None
    chosen_mesh_vertex_count: int | None = None
    chosen_mesh_motion_probe: dict[int, float] = field(default_factory=dict)
    chosen_mesh_motion_max: float = 0.0
    expected_animation: bool = False
    joint_hierarchy: dict = field(default_factory=dict)
