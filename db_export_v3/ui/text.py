from __future__ import annotations


TAB_LABEL_GENERATE = "Generate Skeleton"
TAB_LABEL_EXISTING = "Use Existing Skeleton"
TAB_LABEL_SOLVER = "Solver Settings"
SECTION_LABEL_PATHS = "Paths & Diagnostics"
SECTION_LABEL_LOG = "Run Log"

FIXED_TIP_TEXT = (
    "Uses the existing EA DemBones CLI on a prepared scene skeleton. "
    "This mode preserves the incoming skeleton placement and is the "
    "artist-facing fixed-hierarchy path. Custom hierarchy correction experiments "
    "are not part of the production runtime. If a future fix is required for "
    "non-stock hierarchy behavior, investigate DemBones C++ internals rather "
    "than adding more Maya-side post-solve patches."
)

FIXED_VARIANT_LABEL_OPTIMIZE = "Optimize Existing (Recommended)"
FIXED_VARIANT_LABEL_WEIGHTS = "Weights Only (Diagnostic)"
FIXED_VARIANT_LABEL_TRANSFORMS = "Transforms Only (Diagnostic)"

_FIXED_VARIANT_NOTES = {
    "optimize_existing": (
        "Primary fixed-skeleton mode. DemBones updates both weights and transforms. "
        "Use this for normal fixed-hierarchy validation."
    ),
    "weights_only": (
        "Diagnostic mode. Transforms stay unchanged and only weights are optimized. "
        "Useful for contract and skinning checks, not for generating new bone animation."
    ),
    "transforms_only": (
        "Diagnostic mode. Current verified runs show that child joint translates can explode "
        "even when root weights are zero and the input contract passes. Use this only to test "
        "the stock transform solver behavior, not as default production output."
    ),
}


def fixed_variant_note(variant: str) -> str:
    return _FIXED_VARIANT_NOTES.get(str(variant or "").strip(), _FIXED_VARIANT_NOTES["optimize_existing"])


def help_text() -> str:
    return (
        "Overview:\n"
        "- Generate Skeleton creates a new DemBones skeleton from the selected deforming mesh.\n"
        "- Use Existing Skeleton runs DemBones against a skeleton that already exists in the Maya scene.\n"
        "- Solver Settings contains shared solver controls used by both workflows.\n\n"
        "Generate Skeleton:\n"
        "- Select one deforming mesh shape or its transform before pressing Run.\n"
        "- Target Bone Count is the requested upper target for DemBones auto-generation.\n"
        "- Max Influences Per Vertex sets the sparse weight limit.\n"
        "- Hierarchy Build Mode controls how the generated hierarchy is regrouped in the CLI output.\n"
        "- FBX Name controls both the final FBX file name and the exported node prefix inside the deliverable file.\n"
        "- Clip Prefix affects the file name only.\n\n"
        "Use Existing Skeleton:\n"
        "- Source Animated Mesh: the cloth mesh driven by Alembic or other scene deformation. This becomes the animated source.\n"
        "- Source Alembic is resolved automatically from Source Animated Mesh and is shown only for diagnostics.\n"
        "- Bound Init Mesh: the mesh already skinned to the hierarchy you want to preserve.\n"
        "- Fixed Hierarchy Root: the root joint of that hierarchy.\n"
        "- Fixed Solve Variant selects the DemBones solve mode for the prepared hierarchy.\n"
        "- This workflow preserves the input skeleton placement. The tool no longer normalizes the hierarchy into solver space.\n"
        "- If the source and bound meshes do not match by topology, the run stops before CLI execution.\n"
        "- Optimize Existing is the default fixed-skeleton path.\n"
        "- Weights Only is diagnostic and does not generate new joint motion.\n"
        "- Transforms Only is diagnostic. Verified tests show that it can produce extreme child-joint translate solves even when root weights are zero, hierarchy is valid, bind pose is valid, and the source/init contract passes validation.\n"
        "- Custom hierarchy repair experiments are archived as research and are not part of the shipping runtime.\n"
        "- If fixed-bones problems remain after the input contract passes, the next serious step is DemBones C++ investigation rather than more Maya-side output patching.\n\n"
        "Transforms Only Interpretation:\n"
        "- If root stays stable but child joints get huge local/world translations, the result is transform-solver instability, not a simple root-space mismatch.\n"
        "- Once root weights are zero and the fixed contract passes, root placement, bind pose, and scene space are no longer the first suspects for this mode.\n\n"
        "Solver Settings:\n"
        "- Initialization Iterations affects only Generate Skeleton mode.\n"
        "- Optimization Iterations affects the main CLI solve.\n"
        "- Convergence Threshold controls the optimizer stop tolerance.\n"
        "- Early Stop Patience controls how many non-improving iterations are tolerated.\n\n"
        "Paths & Diagnostics:\n"
        "- CLI Executable points to DemBones.exe.\n"
        "- Cache Root stores per-run intermediates such as init FBX, Alembic, logs, and raw CLI output.\n"
        "- Result Folder stores the final deliverable FBX.\n"
        "- Namespace is used only when importing the final result back into Maya.\n"
        "- Verbose CLI Log mirrors CLI stdout/stderr into the window log.\n"
        "- Write Debug Logs To Disk writes structured diagnostics for export, contract validation, and import checks.\n\n"
        "Import Result Into Scene:\n"
        "- ON imports the final deliverable FBX back into the current Maya scene after export.\n"
        "- OFF only writes the deliverable FBX to disk.\n"
        "- The completion dialog shows the most useful runtime stats for the imported deliverable.\n"
        "- When scene import is enabled, the dialog also shows sampled per-vertex compare stats against the source animated mesh.\n"
    )
