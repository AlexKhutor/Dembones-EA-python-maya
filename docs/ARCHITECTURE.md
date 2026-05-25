# Architecture

## Runtime Overview

`db_export_v3` is organized as a Maya Python package with four main layers:

- `core/`
  shared models, naming, and logging helpers
- `maya/`
  Maya-specific selection, hierarchy, path, and probe helpers
- `pipeline/`
  CLI orchestration, FBX/Alembic export, import, and validation
- `ui/`
  Qt window, controller, layout, and user-facing text

## Execution Flow

1. Collect UI settings.
2. Validate scene inputs.
3. Export rest FBX and animated Alembic.
4. Run `DemBones.exe`.
5. Re-export a clean deliverable FBX.
6. Optionally import the result back into Maya.
7. Show user-facing completion summary and write debug logs.

## Installer Flow

`DB_export_v3_dragdrop.py` loads `db_export_v3_install.py`, which:

- copies the runtime package into Maya modules
- ensures `DemBones.exe` is available
- writes the Maya `.mod` file
- opens the plugin UI

## Public Repo Scope

This repository is intended to expose the production Maya integration layer around DemBones. It is not intended to mirror every internal packaging, release, or research artifact.
