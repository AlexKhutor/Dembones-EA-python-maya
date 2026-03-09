from __future__ import annotations

from dataclasses import dataclass

from .selection import SelectedMeshInfo


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
