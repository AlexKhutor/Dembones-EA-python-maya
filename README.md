# DB_export_v3 for Autodesk Maya and DemBones

`DB_export_v3` is a Python Maya plugin and CLI wrapper around EA Dem Bones (`DemBones.exe`). It converts a deforming mesh, cloth simulation cache, or Alembic animation into a skeletal FBX result by exporting a rest-pose FBX and animated Alembic, running DemBones in the background, and importing the generated skeleton and animation back into Autodesk Maya.

If you are looking for a **Maya DemBones plugin**, a **Python wrapper for EA Dem Bones**, an **FBX and Alembic to skeleton workflow**, or a **cloth simulation to bones tool for Autodesk Maya**, this repository is the maintained production baseline.

Compatibility note:
Tested on Autodesk Maya 2026.3.
Support for earlier Maya versions is not guaranteed.

## Demo

![DB_export_v3 demo](dembones_demonstration_1080p.gif)

## What This Plugin Does

1. Takes one selected deforming mesh from Maya.
2. Exports cached `rest FBX` and animated `Alembic`.
3. Runs `DemBones.exe` in the background.
4. Imports the generated FBX skeleton and animation back into Maya.
5. Exports a final FBX deliverable for downstream animation, rigging, or runtime pipeline use.

This makes the repository useful for:

- mesh-to-skeleton conversion in Maya
- cloth or simulation to bones workflows
- skinning decomposition and rig reconstruction
- animation-to-skeleton conversion through DemBones
- technical art, rigging, and Maya pipeline automation
- solving onto an existing Maya skeleton hierarchy

## Main Workflows

### Generate Skeleton

Use this mode when DemBones should build a new skeleton from one animated or deforming mesh.

Typical use cases:

- cloth simulation to skeleton
- cached mesh animation to joints
- automatic bone extraction from deformation

### Use Existing Skeleton

Use this mode when the scene already contains the target hierarchy and a bound mesh.

Typical use cases:

- solving deformation onto an existing game skeleton
- validating a DemBones solve against an existing hierarchy
- reusing a prepared rig structure inside Maya

## Repository Layout

- `DB_export_v3_dragdrop.py`
  Maya drag-and-drop installer entry point
- `db_export_v3_install.py`
  installer logic for Maya modules deployment
- `db_export_v3/`
  production runtime package
- `docs/`
  public documentation for install, workflow, and architecture
- `dembones_demonstration_1080p.gif`
  demo animation

## Install in Maya

Recommended for artists and TDs:

1. Download the repository as a ZIP or clone it locally.
2. Make sure these items are in the repository root:
   - `DB_export_v3_dragdrop.py`
   - `db_export_v3_install.py`
   - `db_export_v3/`
3. Drag `DB_export_v3_dragdrop.py` into the Maya viewport or Script Editor.

Installer behavior:

- copies `db_export_v3` into the Maya modules folder
- downloads `DemBones.exe` from the official DemBones source if it is not already installed
- creates the Maya module entry
- opens the plugin UI

Important:

- `DemBones.exe` is not committed to this public repository
- first install may require internet access if the binary is not already installed locally
- if `DemBones.exe` already exists in the Maya module install, the installer reuses it

## Open UI Manually

In Maya Script Editor (Python):

```python
import db_export_v3
db_export_v3.open_window()
```

## Documentation

- [User Guide](docs/USER_GUIDE.md)
- [Status](docs/STATUS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Changelog](CHANGELOG.md)

## License

This repository is distributed under the license in [LICENSE](LICENSE).

DemBones itself is an EA project and should be treated according to its own license and distribution terms.
