# Status

Current production runtime version: `3.2.17`

## Shipping Baseline

The public repository tracks the current production Maya wrapper around stock EA `DemBones.exe`.

Supported runtime workflows:

- `Generate Skeleton`
- `Use Existing Skeleton`

## What Is Included

- production `db_export_v3` runtime package
- drag-and-drop Maya installer entry point
- Maya module installer script
- public docs for usage and architecture

## What Is Not Included

- internal delivery package assembly
- release build folders
- Maya-side post-solve experiments for custom hierarchy repair
- research snapshots and archived test branches

## What Was Already Validated

- auto skeleton generation from animated meshes
- fixed-skeleton contract validation before CLI solve
- final FBX import back into Maya
- user-facing completion summary for animators
- vertex compare diagnostics against source animation

## Research Boundary

Custom hierarchy correction, post-solve normalization experiments, and deeper fixed-skeleton parity problems remain research topics. If those become product requirements, the next step is likely inside DemBones C++ rather than more Maya-side patching.
