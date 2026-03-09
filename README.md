# DB_export

`DB_export` is a Maya CLI-first plugin wrapper for DemBones.

Compatibility note:
Tested on Autodesk Maya 2026.3.
Support for earlier Maya versions is not guaranteed.

It does exactly this:
1. Takes one selected deforming mesh shape from Maya.
2. Exports cached `rest FBX` + `anim ABC`.
3. Runs `DemBones.exe` in background.
4. Imports CLI output FBX back into Maya (with animation and skeleton).

No preview/solver parity mode is included in this version.

## Legacy snapshot

Previous implementation was archived to:

`OLD/legacy_snapshot_20260305_154509`

## Install in Maya (drag-and-drop)

Drag this file into Maya viewport/Script Editor:

`DB_export_dragdrop.py`

Installer will:
- copy `db_export` Python package into Maya modules folder,
- download `DemBones.exe` from the original DemBones release URL into Maya module `bin`,
- create `DB_export.mod`,
- open `DB_export` UI.

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
