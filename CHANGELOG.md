# Changelog

## 3.2.16 - 2026-06-17

- Fixed the optional `World Root Wrapper` so it only reparents solved joint roots during final clean-scene export.
- Mesh geometry now stays separate instead of being parented under the extra wrapper joint.
- Clarified the wrapper-root UI/help text to state that it is an export-only joint container for skeleton roots, not a geometry grouping step.
- Kept local release output assembly under `source/release/` so the isolate-workspace root stays clean.

## 3.2.15 - 2026-06-11

- Added automated public drag-and-drop release publishing through GitHub Actions.
- Public GitHub Releases now publish both `.zip` and `.7z` archives for the artist-facing drag-and-drop installer package.
- Added a local PowerShell build script so the same public package can be assembled manually for team handoff without using GitHub Releases.
- Includes the optional World Root Wrapper final-export behavior from the current production runtime.

## 3.2.14 - 2026-06-10

- Split the hierarchy UX into two independent controls:
  - `DemBones Hierarchy Mode` now clearly maps to DemBones CLI `--bindUpdate`.
  - `World Root Wrapper` is a separate export option for adding one static empty joint at world `0,0,0`.
- Added optional clean-export wrapper-root creation in the final deliverable FBX.
- Preserved the wrapper-root behavior through the normal `Import Result Into Scene` flow because the wrapper is written into the exported FBX itself.
- Clarified in the in-app Help that the wrapper root is not part of the DemBones solve and is only added during final clean-scene FBX export.
