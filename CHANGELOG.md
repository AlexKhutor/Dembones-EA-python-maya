# Changelog

## 3.2.14 - 2026-06-10

- Split the hierarchy UX into two independent controls:
  - `DemBones Hierarchy Mode` now clearly maps to DemBones CLI `--bindUpdate`.
  - `World Root Wrapper` is a separate export option for adding one static empty joint at world `0,0,0`.
- Added optional clean-export wrapper-root creation in the final deliverable FBX.
- Preserved the wrapper-root behavior through the normal `Import Result Into Scene` flow because the wrapper is written into the exported FBX itself.
- Clarified in the in-app Help that the wrapper root is not part of the DemBones solve and is only added during final clean-scene FBX export.
