# DB_export

`DB_export` is a Maya CLI-first plugin wrapper for DemBones.

Compatibility note:
Tested on Autodesk Maya 2026.3.
Support for earlier Maya versions is not guaranteed.

## Demo

![DB_export demo](dembones_demonstration_1080p.gif)

It does exactly this:
1. Takes one selected deforming mesh shape from Maya.
2. Exports cached `rest FBX` + `anim ABC`.
3. Runs `DemBones.exe` in background.
4. Imports CLI output FBX back into Maya (with animation and skeleton).

No preview/solver parity mode is included in this version.

This repository is a Maya DemBones CLI tool for mesh-to-skeleton workflows, skinning decomposition, rig reconstruction, and animation-to-skeleton conversion. It is intended for Maya pipeline, rigging, and technical art use cases where a deforming mesh needs to be converted into a skeletal result through FBX and Alembic export.

## Use cases

- Convert deforming mesh animation into a joint-based result through DemBones CLI.
- Export rest pose FBX and animated Alembic from Maya for skinning decomposition.
- Re-import generated skeleton and animation back into Maya.
- Produce a result FBX that can be handed to downstream rigging or runtime pipelines.

## Install in Maya (drag-and-drop)

1. Download the archive.
2. Unpack it to any local folder.
3. Drag `DB_export_dragdrop.py` into Maya viewport or Script Editor.

Why there are two drag-and-drop scripts:
- `DB_export_dragdrop.py` is the main installer for artists and normal updates.
- `tools/DB_export_dragdrop_refresh.py` is a fallback for developers/support when Maya keeps old modules in memory and the main installer appears to load an old version.
- In normal usage, use only `DB_export_dragdrop.py`.

Installer will:
- copy `db_export` Python package into Maya modules folder,
- download `DemBones.exe` from the original DemBones release URL into Maya module `bin` if it is not installed yet,
- create `DB_export.mod`,
- open `DB_export` UI.

Important:

- `DemBones.exe` is not shipped inside the archive.
- First install requires internet access to download `DemBones.exe`.
- If `DemBones.exe` is already present in the installed Maya module, installer will reuse it and skip download.

Default CLI source URL (primary):

`https://raw.githubusercontent.com/electronicarts/dem-bones/master/bin/Windows/DemBones.exe`

## Open UI manually

In Maya Script Editor (Python):

```python
import db_export
db_export.open_window()
```

## Key behavior

- Works only with **one selected mesh shape** (or transform with one renderable shape).
- Validates that shape has deformers/dynamic input in history/inMesh.
- Keeps cached exports and CLI outputs in:
  - default: `~/Documents/maya/<version>/DB_export/cache`
- Exposes CLI params in UI:
  - bones, bindUpdate, nnz, nInitIters, nIters, tolerance, patience,
  - frame range and namespace.
