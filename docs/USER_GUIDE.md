# User Guide

Current runtime version: `3.2.13`

## Install

Use the repository root as the install package.

Required files:

- `DB_export_v3_dragdrop.py`
- `db_export_v3_install.py`
- `db_export_v3/`

Drag `DB_export_v3_dragdrop.py` into Maya to install or update the plugin.

## Generate Skeleton

Use this mode when the tool should build a new DemBones skeleton from one selected deforming mesh.

Inputs:

- one deforming mesh in the scene
- target bone count
- max influences per vertex
- frame range
- optional export naming

Outputs:

- rest FBX
- animated Alembic
- raw CLI FBX
- final deliverable FBX

## Use Existing Skeleton

Use this mode when the scene already contains the target skeleton and bound mesh.

Required inputs:

- `Source Animated Mesh`
- `Bound Init Mesh`
- `Fixed Hierarchy Root`
- `Fixed Solve Variant`

Supported variants:

- `Optimize Existing`
  production fixed-skeleton mode
- `Weights Only`
  diagnostic mode
- `Transforms Only`
  diagnostic mode

## Completion Summary

After the run finishes, the final dialog shows user-facing stats:

- overall movement difference vs original
- average vertex position difference
- largest single-vertex difference and the frame where it happens
- warnings when mesh drift or bone stretch become noticeable
- expandable `Details` section for technical run information

Interpretation:

- `overall movement difference`
  difference in overall movement amplitude between the source animation and the final FBX
- `average vertex position difference`
  average world-space distance between matching vertices on the source mesh and the final FBX mesh
- `largest single-vertex difference`
  largest world-space distance found on one matching vertex in the sampled frame range
