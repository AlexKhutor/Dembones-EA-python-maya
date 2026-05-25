from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path

try:
    from PySide6 import QtCore
except ImportError:  # pragma: no cover
    from PySide2 import QtCore

import maya.cmds as cmds

from ..core.log_utils import RunDebugLogger, now_stamp
from ..maya.paths import default_cache_root
from ..pipeline.run import build_cli_args, export_result_fbx, import_exported_result, prepare_run
from ..pipeline.importer import isolate_root_import_names
from ..version import VERSION

class CliRunController(QtCore.QObject):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(int, str)
    run_started = QtCore.Signal()
    run_finished = QtCore.Signal(bool, str)

    _ITER_RE = re.compile(r"Iter\s*#\s*(\d+)")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process = None
        self._prepared = None
        self._settings = None
        self._buffer = []
        self._last_progress = 0
        self._stopping_requested = False
        self._global_run_lock = None
        self._locked_source_nodes = []
        self._debug_logger = None
        self._last_completion_payload = None

    def is_running(self) -> bool:
        return self._process is not None

    def _log(self, message: str, error: bool = False, source: str = "app", persist: bool = False):
        text = str(message)
        self.log.emit(text)
        if self._debug_logger is not None:
            self._debug_logger.event(text, error=error, source=source, persist=persist)

    def _start_debug_logger(self, settings):
        if not bool(getattr(settings, "write_debug_logs", False)):
            self._debug_logger = None
            self._log("debug_logs_disabled: file logging is off for this run")
            return
        self._debug_logger = RunDebugLogger("DB_export_v3", VERSION)
        self._debug_logger.set_settings(asdict(settings))
        self._debug_logger.set_section(
            "runInfo",
            {
                "textLogPath": self._debug_logger.text_log_path,
                "jsonLogPath": self._debug_logger.json_log_path,
            },
        )
        self._log("debug_text_log: {0}".format(self._debug_logger.text_log_path), persist=True)
        self._log("debug_json_log: {0}".format(self._debug_logger.json_log_path), persist=True)

    def _finalize_debug_logger(self, status: str, error_message: str = ""):
        if self._debug_logger is None:
            return
        try:
            self._debug_logger.finalize(status, error_message)
        except Exception:
            pass
        self._debug_logger = None

    def _snapshot_maya_fbx_log(self, kind: str):
        if self._debug_logger is None:
            return
        info = self._debug_logger.snapshot_maya_fbx_log(kind)
        if info.get("found"):
            self._log(
                "maya_fbx_{0}_log_snapshot: {1}".format(info.get("kind"), info.get("snapshotPath")),
                persist=True,
            )
        else:
            self._log("maya_fbx_{0}_log_snapshot: not found".format(info.get("kind") or kind), persist=True)

    def _record_known_issues(
        self,
        import_result: dict | None,
        export_result: dict | None,
        import_error: str = "",
    ):
        if self._debug_logger is None or self._settings is None:
            return

        expected_joint_count = 0
        if self._prepared is not None:
            expected_joint_count = int(getattr(self._prepared, "expected_joint_count", 0) or 0)
        if expected_joint_count <= 0:
            expected_joint_count = int(getattr(self._settings, "bones", 0) or 0)
        expected_joint_label = (
            "fixedHierarchyJointCount"
            if str(getattr(self._settings, "solve_mode", "auto")) == "fixed_bones"
            else "requestedTargetBoneCount"
        )

        if export_result:
            src = export_result.get("source") or {}
            dst = export_result.get("destination") or {}
            after = export_result.get("directoryAfter") or {}
            export_method = str(export_result.get("exportMethod") or "")
            if src.get("exists") and not dst.get("exists"):
                self._debug_logger.append_issue(
                    "error",
                    "result-export-missing-destination",
                    "CLI output FBX exists in cache, but the copy to Result FBX Folder did not produce a destination file.",
                    {
                        "source": src,
                        "destination": dst,
                        "directoryAfter": after,
                    },
                )
            elif src.get("exists") and int(after.get("fbxFileCount", 0) or 0) == 0:
                self._debug_logger.append_issue(
                    "warning",
                    "result-export-folder-empty",
                    "Result FBX Folder still has zero FBX files after export.",
                    {
                        "source": src,
                        "destination": dst,
                        "directoryAfter": after,
                    },
                )
            if export_method.startswith("raw_cli_copy") and not bool(export_result.get("sizeMatch", True)):
                self._debug_logger.append_issue(
                    "warning",
                    "result-export-size-mismatch",
                    "Copied FBX size does not match the CLI output file size.",
                    {
                        "source": src,
                        "destination": dst,
                    },
                )

    def completion_payload(self) -> dict | None:
        return dict(self._last_completion_payload or {}) if self._last_completion_payload else None

    @staticmethod
    def _safe_float(value):
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _hidden_subprocess_kwargs() -> dict:
        if os.name != "nt":
            return {}
        kwargs: dict = {}
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
        startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
        startf_use_show_window = int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
        sw_hide = int(getattr(subprocess, "SW_HIDE", 0) or 0)
        if startupinfo_cls is not None:
            startupinfo = startupinfo_cls()
            startupinfo.dwFlags |= startf_use_show_window
            startupinfo.wShowWindow = sw_hide
            kwargs["startupinfo"] = startupinfo
        return kwargs

    @staticmethod
    def _format_ratio_percent(value):
        ratio = CliRunController._safe_float(value)
        if ratio is None:
            return "n/a"
        return "{0:.2f}%".format(ratio * 100.0)

    @staticmethod
    def _format_difference_percent_from_ratio(value):
        ratio = CliRunController._safe_float(value)
        if ratio is None:
            return "n/a"
        diff_percent = abs(ratio - 1.0) * 100.0
        if diff_percent < 0.01:
            return "{0:.4f}%".format(diff_percent)
        if diff_percent < 0.1:
            return "{0:.3f}%".format(diff_percent)
        return "{0:.2f}%".format(diff_percent)

    @staticmethod
    def _format_distance(value):
        distance = CliRunController._safe_float(value)
        if distance is None:
            return "n/a"
        return "{0:.3f} cm".format(distance)

    @staticmethod
    def _format_scalar(value):
        scalar = CliRunController._safe_float(value)
        if scalar is None:
            return "n/a"
        return "{0:.3f}".format(scalar)

    @staticmethod
    def _motion_match_text(value):
        ratio = CliRunController._safe_float(value)
        if ratio is None:
            return "Movement difference vs original: unavailable"
        diff = abs(ratio - 1.0) * 100.0
        if diff <= 1.0:
            quality = "very close to the original"
        elif diff <= 3.0:
            quality = "close to the original"
        elif diff <= 8.0:
            quality = "noticeably different from the original"
        else:
            quality = "far from the original"
        return "Movement difference vs original: {0} ({1})".format(
            CliRunController._format_difference_percent_from_ratio(ratio),
            quality,
        )

    def _build_completion_payload(
        self,
        *,
        ok: bool,
        imported_message: str,
        export_result: dict | None,
        import_result: dict | None,
        import_error: str,
    ) -> dict:
        settings = self._settings
        prepared = self._prepared
        summary_lines: list[str] = []
        warnings: list[str] = []
        detail_lines: list[str] = []

        mode_label = "Generate Skeleton"
        if str(getattr(settings, "solve_mode", "auto")).strip() == "fixed_bones":
            mode_label = "Use Existing Skeleton"
            variant_label = str(getattr(settings, "fixed_solve_variant", "") or "unknown")
        else:
            variant_label = ""

        export_path = ""
        if export_result:
            export_path = str(export_result.get("path") or "")
        detail_lines.append("Workflow: {0}".format(mode_label))
        if variant_label:
            detail_lines.append("Solve variant: {0}".format(variant_label))
        if export_path:
            detail_lines.append("Result FBX: {0}".format(export_path))
        if prepared is not None:
            detail_lines.append("Cache Run Dir: {0}".format(prepared.run_dir))
        detail_lines.append("Scene result: {0}".format(imported_message))

        if import_error:
            warnings.append("Scene import failed: {0}".format(import_error))

        if import_result:
            motion_ratio = self._safe_float(import_result.get("motion_ratio_vs_source"))
            if motion_ratio is not None:
                summary_lines.append(self._motion_match_text(motion_ratio))
                detail_lines.append(
                    "Overall movement difference from original: {0}".format(
                        self._format_difference_percent_from_ratio(motion_ratio)
                    )
                )
                if abs(motion_ratio - 1.0) > 0.05:
                    warnings.append("Overall movement is noticeably different from the original.")

            vertex_compare = import_result.get("vertex_compare") or {}
            if vertex_compare.get("exists"):
                world_compare = vertex_compare.get("world") or {}
                avg_rms = self._safe_float(world_compare.get("averageRmsDistance"))
                avg_mean = self._safe_float(world_compare.get("averageMeanDistance"))
                worst_max = self._safe_float(world_compare.get("globalMaxDistance"))
                worst_frame = world_compare.get("globalMaxDistanceFrame")
                sampled_frames = int(vertex_compare.get("sampledFrameCount") or 0)
                summary_lines.append(
                    "Average vertex position difference: {0}".format(self._format_distance(avg_mean))
                )
                summary_lines.append(
                    "Largest single-vertex difference: {0} at frame {1}".format(
                        self._format_distance(worst_max),
                        worst_frame if worst_frame is not None else "n/a",
                    )
                )
                detail_lines.append(
                    "Average vertex position difference from original mesh (world space): {0}".format(
                        self._format_distance(avg_mean)
                    )
                )
                detail_lines.append(
                    "Largest single vertex position difference from original mesh (world space): {0} at frame {1}".format(
                        self._format_distance(worst_max),
                        worst_frame if worst_frame is not None else "n/a",
                    )
                )
                detail_lines.append("Compared frames: {0}".format(sampled_frames))
                detail_lines.append(
                    "These values compare the distance between matching vertices on the original mesh and the final FBX mesh."
                )
                if avg_rms is not None and avg_rms > 5.0:
                    warnings.append("Average mesh difference is still high in motion.")
                if worst_max is not None and worst_max > 20.0:
                    warnings.append("There is at least one frame with a large visible mesh difference.")
            elif settings is not None and not bool(getattr(settings, "import_result_in_scene", True)):
                detail_lines.append("Mesh comparison was skipped because scene import is disabled.")

            contract_compare = import_result.get("fixed_contract_compare") or {}
            if contract_compare:
                mesh_center_ratio = self._safe_float(contract_compare.get("meshCenterMagnitudeRatio"))
                if mesh_center_ratio is not None and abs(mesh_center_ratio - 1.0) > 0.05:
                    warnings.append("The final mesh is offset from the original more than expected.")

            joint_probe = import_result.get("joint_visual_probe") or {}
            top_bone = (joint_probe.get("topBoneLengthRatioJoints") or [])
            if top_bone:
                max_bone_ratio = self._safe_float(top_bone[0].get("maxBoneLengthRatio"))
                if max_bone_ratio is not None:
                    detail_lines.append("Worst bone stretch: {0:.3f}x".format(max_bone_ratio))
                    if max_bone_ratio > 1.5:
                        warnings.append("Some bone lines still stretch noticeably during animation.")
        elif export_result:
            deliverable_probe = export_result.get("deliverableProbe") or {}
            motion_ratio = self._safe_float(deliverable_probe.get("motionRatioVsSource"))
            if motion_ratio is not None:
                summary_lines.append(self._motion_match_text(motion_ratio))
                detail_lines.append(
                    "Overall movement difference from original: {0}".format(
                        self._format_difference_percent_from_ratio(motion_ratio)
                    )
                )
                detail_lines.append("Mesh comparison was skipped because scene import is disabled.")

        status_line = "Done"
        if not ok and import_error:
            status_line = "FBX saved, scene import failed"
        elif not ok:
            status_line = "Run failed"

        return {
            "ok": bool(ok),
            "statusLine": status_line,
            "summaryLines": summary_lines,
            "warningLines": warnings,
            "detailText": "\n".join(detail_lines),
        }

        if import_error:
            issue_code = "scene-import-failed"
            issue_message = "Result FBX was exported, but importing it back into Maya failed."
            self._debug_logger.append_issue(
                "error",
                issue_code,
                issue_message,
                {
                    "errorMessage": str(import_error),
                    "requestedNamespace": self._settings.namespace,
                    "importRequestedByUi": bool(self._settings.import_result_in_scene),
                },
            )

        if import_result:
            joint_hierarchy = import_result.get("joint_hierarchy") or {}
            total_joint_count = int(joint_hierarchy.get("totalJointCount", 0) or 0)
            largest_root_subtree = int(joint_hierarchy.get("largestRootSubtreeJointCount", 0) or 0)
            root_joint_count = int(joint_hierarchy.get("rootJointCount", 0) or 0)

            if expected_joint_count != total_joint_count:
                self._debug_logger.append_issue(
                    "warning",
                    "bone-count-mismatch",
                    "Expected joint contract does not match total imported joint count.",
                    {
                        expected_joint_label: expected_joint_count,
                        "importedTotalJointCount": total_joint_count,
                        "jointHierarchy": joint_hierarchy,
                    },
                )

            if total_joint_count > 0 and largest_root_subtree > 0 and largest_root_subtree < total_joint_count:
                self._debug_logger.append_issue(
                    "info",
                    "multi-root-skeleton",
                    "Imported skeleton is split across multiple root joint hierarchies.",
                    {
                        expected_joint_label: expected_joint_count,
                        "importedTotalJointCount": total_joint_count,
                        "rootJointCount": root_joint_count,
                        "largestRootSubtreeJointCount": largest_root_subtree,
                        "largestRoot": joint_hierarchy.get("largestRoot"),
                        "rootJoints": joint_hierarchy.get("rootJoints"),
                    },
                )

            if expected_joint_count == total_joint_count and largest_root_subtree < expected_joint_count:
                self._debug_logger.append_issue(
                    "info",
                    "hierarchy-subtree-smaller-than-request",
                    "Imported total joint count matches the expected joint contract, but the largest single hierarchy is smaller.",
                    {
                        expected_joint_label: expected_joint_count,
                        "importedTotalJointCount": total_joint_count,
                        "largestRootSubtreeJointCount": largest_root_subtree,
                        "jointHierarchy": joint_hierarchy,
                    },
                )

        if export_result and export_result.get("exportMethod") != "clean_scene_mayapy_reexport":
            self._debug_logger.append_issue(
                "warning",
                "result-export-non-clean-scene-path",
                "Result FBX was saved without the clean standalone Maya export path. Name and import compatibility may differ from the validated deliverable contract.",
                {
                    "exportMethod": export_result.get("exportMethod"),
                    "path": export_result.get("path"),
                },
            )

    @staticmethod
    def _lock_root() -> Path:
        return Path(default_cache_root()).parent

    @classmethod
    def _lock_file_path(cls) -> Path:
        return cls._lock_root() / "db_export_v3_cli.run.lock"

    @classmethod
    def _state_file_path(cls) -> Path:
        return cls._lock_root() / "db_export_v3_cli.run_state.json"

    @classmethod
    def _make_lock_file(cls) -> QtCore.QLockFile:
        lock_root = cls._lock_root()
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_file = QtCore.QLockFile(str(cls._lock_file_path()))
        lock_file.setStaleLockTime(5 * 60 * 1000)
        return lock_file

    def _acquire_global_run_lock(self):
        lock_path = self._lock_file_path()
        lock_file = self._make_lock_file()
        if not lock_file.tryLock(0):
            # Recover from stale lock left after crash/forced close.
            if lock_file.removeStaleLockFile() and lock_file.tryLock(0):
                pass
            else:
                raise RuntimeError(
                    "Another DB_export_v3 CLI run is already in progress. Wait for the current run to finish."
                )
        self._global_run_lock = lock_file
        self._log("global_run_lock_acquired: {0}".format(str(lock_path)))

    def _release_global_run_lock(self):
        if self._global_run_lock is None:
            return
        try:
            self._global_run_lock.unlock()
            self._log("global_run_lock_released")
        except Exception:
            pass
        self._global_run_lock = None

    @staticmethod
    def _query_process_image_name(pid: int) -> str:
        if pid <= 0:
            return ""
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "PID eq {0}".format(int(pid)), "/FO", "CSV", "/NH"],
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **cls._hidden_subprocess_kwargs(),
            ).strip()
        except Exception:
            return ""
        if not out or "No tasks are running" in out:
            return ""
        line = out.splitlines()[0].strip()
        if not line.startswith('"'):
            return ""
        parts = [p.strip().strip('"') for p in line.split('","')]
        if not parts:
            return ""
        return parts[0]

    def _write_run_state(self, pid: int):
        if pid <= 0:
            return
        path = self._state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": int(pid),
            "exe": "DemBones.exe",
            "created_at": datetime.datetime.now().isoformat(),
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log("run_state_written: {0} pid={1}".format(str(path), int(pid)))
        except Exception as exc:
            self._log("run_state_write_failed: {0}".format(exc), error=True)

    def _clear_run_state(self):
        path = self._state_file_path()
        if not path.exists():
            return
        try:
            path.unlink()
            self._log("run_state_cleared")
        except Exception:
            pass

    def _kill_orphan_process_from_state(self):
        state_path = self._state_file_path()
        if not state_path.exists():
            return
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log("run_state_read_failed: {0}".format(exc), error=True)
            self._clear_run_state()
            return

        pid = int(payload.get("pid") or 0)
        expected_exe = str(payload.get("exe") or "DemBones.exe").lower()
        if pid <= 0:
            self._clear_run_state()
            return

        image_name = self._query_process_image_name(pid).lower()
        if not image_name:
            self._log("orphan_process_not_found_by_pid: {0}".format(pid))
            self._clear_run_state()
            return

        if image_name != expected_exe:
            self._log(
                "orphan_process_skip_pid_reused: pid={0} image={1} expected={2}".format(
                    pid, image_name, expected_exe
                )
            )
            self._clear_run_state()
            return

        try:
            subprocess.check_output(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self._hidden_subprocess_kwargs(),
            )
            self._log("orphan_process_killed: pid={0} image={1}".format(pid, image_name))
        except Exception as exc:
            self._log("orphan_process_kill_failed: pid={0} err={1}".format(pid, exc), error=True)
        finally:
            self._clear_run_state()

    def recover_orphan_process(self):
        if self.is_running():
            return
        temp_lock = self._make_lock_file()
        if not temp_lock.tryLock(0):
            # Another active run is holding lock now; do not touch state.
            return
        try:
            self._kill_orphan_process_from_state()
        finally:
            try:
                temp_lock.unlock()
            except Exception:
                pass

    def _lock_source_nodes(self):
        if self._prepared is None:
            return
        selected = self._prepared.selected
        target_nodes = []
        explicit_nodes = list(getattr(self._prepared, "locked_scene_nodes", []) or [])
        for node in explicit_nodes or (selected.transform, selected.shape):
            if node and cmds.objExists(node):
                target_nodes.append(node)
        target_nodes = list(dict.fromkeys(target_nodes))
        if not target_nodes:
            return

        locked = []
        failed = []
        for node in target_nodes:
            try:
                cmds.lockNode(node, lock=True)
                locked.append(node)
            except Exception:
                failed.append(node)

        if failed:
            for node in locked:
                try:
                    cmds.lockNode(node, lock=False)
                except Exception:
                    pass
            raise RuntimeError(
                "Failed to lock source mesh for a safe run: {0}".format(
                    ", ".join(failed)
                )
            )

        self._locked_source_nodes = locked
        self._log("source_nodes_locked: {0}".format(", ".join(self._locked_source_nodes)))

    def _unlock_source_nodes(self):
        if not self._locked_source_nodes:
            return
        for node in self._locked_source_nodes:
            if not cmds.objExists(node):
                continue
            try:
                cmds.lockNode(node, lock=False)
            except Exception:
                pass
        self._log("source_nodes_unlocked: {0}".format(", ".join(self._locked_source_nodes)))
        self._locked_source_nodes = []

    def _release_guards(self):
        self._clear_run_state()
        self._unlock_source_nodes()
        self._release_global_run_lock()

    def _set_progress(self, value: int, text: str):
        value = max(0, min(100, int(value)))
        if value != self._last_progress or text:
            self._last_progress = value
            self.progress.emit(value, text)
            if self._debug_logger is not None:
                self._debug_logger.record_progress(value, text)

    def start(self, settings: CliRunSettings):
        if self._process is not None:
            raise RuntimeError("CLI process is already running.")

        self._acquire_global_run_lock()
        self._settings = settings
        self._buffer = []
        self._last_progress = 0
        self._stopping_requested = False
        self._last_completion_payload = None

        try:
            self._start_debug_logger(settings)
            self._set_progress(1, "Preparing export...")
            self._log("=== DB_export_v3 run started ===", persist=True)
            self._log("time: {0}".format(now_stamp()))
            self._log("cli_exe: {0}".format(settings.cli_exe))
            self._log("cache_root: {0}".format(settings.cache_root))
            self._log("result_export_root: {0}".format(settings.result_export_root))
            self._log("solve_mode: {0}".format(settings.solve_mode))
            self._log("fbx_name: {0}".format(settings.fbx_name or "<auto>"))
            self._log("clip_prefix: {0}".format(settings.clip_prefix or "<none>"))
            if str(settings.solve_mode) == "fixed_bones":
                self._log("fixed_solve_variant: {0}".format(settings.fixed_solve_variant))
                self._log("fixed_runtime_policy: stock_cli_output_only")
                self._log("source_animated_mesh: {0}".format(settings.source_animated_mesh or "<unset>"))
                self._log("bound_init_mesh: {0}".format(settings.bound_init_mesh or "<unset>"))
                self._log("fixed_hierarchy_root: {0}".format(settings.hierarchy_root or "<unset>"))
            else:
                self._log("target_bone_count: {0}".format(int(settings.bones)))
            self._log("max_influences_per_vertex: {0}".format(int(settings.nnz)))
            self._log(
                "frame_range: start={0} end={1} step={2}".format(
                    int(settings.frame_start),
                    int(settings.frame_end),
                    int(settings.frame_step),
                )
            )

            self._prepared = prepare_run(settings, self._log)
            if self._debug_logger is not None:
                self._debug_logger.set_section("prepareRun", self._prepared.debug_info)
            self._snapshot_maya_fbx_log("exp")
            self._lock_source_nodes()
            self._set_progress(12, "FBX/ABC export completed")

            args = build_cli_args(settings, self._prepared)
            self._log("cli_args: {0}".format(" ".join(args)))
            if self._debug_logger is not None:
                self._debug_logger.set_section(
                    "cliInvocation",
                    {
                        "program": settings.cli_exe,
                        "arguments": args,
                    },
                )

            process = QtCore.QProcess(self)
            process.setProgram(settings.cli_exe)
            process.setArguments(args)
            process.setProcessChannelMode(QtCore.QProcess.MergedChannels)
            process.readyRead.connect(self._on_ready_read)
            process.finished.connect(self._on_finished)
            process.errorOccurred.connect(self._on_process_error)
            self._process = process
            process.start()

            if not process.waitForStarted(3000):
                self._process = None
                raise RuntimeError("Failed to start CLI process.")
            try:
                self._write_run_state(int(process.processId()))
            except Exception:
                pass
            if self._debug_logger is not None:
                self._debug_logger.merge_section(
                    "cliInvocation",
                    {
                        "pid": int(process.processId()),
                        "startedAt": now_stamp(),
                    },
                )

            self._set_progress(18, "CLI started")
            self.run_started.emit()
        except Exception as exc:
            self._log("start_failed: {0}".format(exc), error=True, persist=True)
            self._finalize_debug_logger("failed", str(exc))
            self._release_guards()
            raise

    def stop(self, timeout_ms: int = 2000):
        if self._process is None:
            return
        self._stopping_requested = True
        self._log("Stopping CLI process...")
        try:
            self._process.terminate()
            if not self._process.waitForFinished(int(timeout_ms)):
                self._process.kill()
                self._process.waitForFinished(int(timeout_ms))
        except Exception as exc:
            self._log("Stop process error: {0}".format(exc), error=True)

    def _update_progress_from_line(self, line: str):
        if self._settings is None:
            return

        line_l = line.lower()
        if "reading abcs" in line_l:
            self._set_progress(22, "CLI: reading Alembic")
            return
        if "reading fbx" in line_l:
            self._set_progress(28, "CLI: reading FBX")
            return
        if "initializing bones" in line_l:
            self._set_progress(35, "CLI: initializing bones")
            return
        if "computing skinning decomposition" in line_l:
            self._set_progress(40, "CLI: optimizing skinning")
            return
        if "convergence is reached" in line_l:
            self._set_progress(92, "CLI: convergence reached")
            return
        if "writing outputs" in line_l:
            self._set_progress(95, "CLI: writing output FBX")
            return

        m = self._ITER_RE.search(line)
        if m:
            iter_idx = int(m.group(1))
            total = max(1, int(self._settings.n_iters))
            frac = min(iter_idx + 1, total) / float(total)
            value = 40 + int(50 * frac)
            self._set_progress(value, "CLI: iteration {0}/{1}".format(iter_idx + 1, total))

    def _on_ready_read(self):
        if self._process is None:
            return
        data = bytes(self._process.readAll()).decode("utf-8", errors="replace")
        if not data:
            return
        self._buffer.append(data)
        for line in data.splitlines():
            if self._debug_logger is not None:
                self._debug_logger.record_cli_line(line)
            self._log("[CLI] " + line, source="cli")
            self._update_progress_from_line(line)

    def _on_process_error(self, _err):
        if self._process is None:
            return
        self._log("[CLI] process error: {0}".format(self._process.errorString()), error=True, source="cli")

    def _on_finished(self, exit_code, _exit_status):
        proc = self._process
        self._process = None
        final_status = "failed"
        final_message = ""
        try:
            if self._debug_logger is not None:
                self._debug_logger.merge_section(
                    "cliInvocation",
                    {
                        "exitCode": int(exit_code),
                    },
                )
            if self._stopping_requested:
                self._log("CLI process stopped by UI close/user action.", persist=True)
                self._set_progress(100, "Stopped")
                self._last_completion_payload = {
                    "ok": False,
                    "statusLine": "Run stopped",
                    "summaryLines": [],
                    "warningLines": [],
                    "detailText": "Run stopped by user action.",
                }
                self.run_finished.emit(False, "Run stopped.")
                final_status = "stopped"
                final_message = "Run stopped."
                return

            if exit_code != 0:
                message = "CLI exited with code {0}".format(exit_code)
                self._log(message, error=True, persist=True)
                self._set_progress(100, "CLI error")
                self._last_completion_payload = {
                    "ok": False,
                    "statusLine": "CLI error",
                    "summaryLines": [],
                    "warningLines": [],
                    "detailText": message,
                }
                self.run_finished.emit(False, message)
                final_status = "failed"
                final_message = message
                return

            if self._settings is None:
                raise RuntimeError("Internal error: missing run settings.")

            imported_message = "Saved to disk only."
            import_result = None
            import_error = ""

            self._set_progress(96, "Building clean deliverable FBX...")
            export_result = export_result_fbx(self._prepared, self._settings, self._log)

            if self._debug_logger is not None:
                self._debug_logger.set_section(
                    "deliveryImport",
                    {
                        "skipped": True,
                        "reason": "Deliverable is exported in a clean standalone Maya scene.",
                    },
                )
                self._debug_logger.set_section("resultExport", export_result)
                self._debug_logger.set_section(
                    "runArtifacts",
                    {
                        "cacheRunDir": self._prepared.run_dir,
                        "cacheManifest": self._prepared.latest_manifest,
                        "expectedOutFbx": self._prepared.out_fbx,
                    },
                )

            if self._settings.import_result_in_scene:
                self._set_progress(98, "Importing deliverable FBX into Maya...")
                namespace = self._settings.namespace if self._settings else "db_export_v3_cli"
                try:
                    import_result = import_exported_result(
                        self._prepared,
                        export_result.get("path", ""),
                        namespace,
                        self._settings,
                        self._log,
                    )
                except Exception as exc:
                    import_error = str(exc)
                    self._log("scene_import_failed: {0}".format(import_error), error=True, persist=True)
                    if self._debug_logger is not None:
                        self._debug_logger.set_section(
                            "importResult",
                            {
                                "success": False,
                                "errorMessage": import_error,
                                "requestedNamespace": namespace,
                                "importRequestedByUi": True,
                                "sourcePath": export_result.get("path", ""),
                            },
                        )
                else:
                    if not (self._settings.namespace or "").strip():
                        preview_prefix = "db_export_v3_preview_{0}__".format(self._prepared.run_id)
                        import_result = isolate_root_import_names(import_result, preview_prefix, self._log)
                    if self._debug_logger is not None:
                        self._debug_logger.set_section("importResult", import_result)
                    imported_message = "Imported into the current scene."
                    self._snapshot_maya_fbx_log("imp")
            else:
                self._log("import_skipped_by_ui: true")
                if self._debug_logger is not None and import_result is None:
                    self._debug_logger.set_section(
                        "importResult",
                        {
                            "skippedByUi": True,
                        },
                    )

            if import_error:
                imported_message = "Could not import the result back into the scene."

            self._record_known_issues(import_result, export_result, import_error)

            self._log("cache_run_dir: {0}".format(self._prepared.run_dir))
            self._log("cache_manifest: {0}".format(self._prepared.latest_manifest))
            self._log("=== DB_export_v3 run finished ===", persist=True)
            if import_error:
                progress_text = "FBX saved, scene import failed"
                fail_label = "Scene import failed"
                self._set_progress(100, progress_text)
                self._last_completion_payload = self._build_completion_payload(
                    ok=False,
                    imported_message=imported_message,
                    export_result=export_result,
                    import_result=import_result,
                    import_error=import_error,
                )
                self._log(
                    "completion_summary: {0}".format(
                        json.dumps(self._last_completion_payload, ensure_ascii=False, sort_keys=True)
                    ),
                    persist=True,
                )
                self.run_finished.emit(
                    False,
                    "FBX saved: {0}\n{1}: {2}".format(
                        export_result.get("path", ""),
                        fail_label,
                        import_error,
                    ),
                )
                final_status = "partial_success"
                final_message = import_error
            else:
                self._set_progress(100, "Done")
                self._last_completion_payload = self._build_completion_payload(
                    ok=True,
                    imported_message=imported_message,
                    export_result=export_result,
                    import_result=import_result,
                    import_error="",
                )
                self._log(
                    "completion_summary: {0}".format(
                        json.dumps(self._last_completion_payload, ensure_ascii=False, sort_keys=True)
                    ),
                    persist=True,
                )
                self.run_finished.emit(
                    True,
                    "Done: {0}\nFBX saved: {1}".format(imported_message, export_result.get("path", "")),
                )
                final_status = "success"
                final_message = ""
        except Exception as exc:
            self._log("run_failed: {0}".format(exc), error=True, persist=True)
            self._set_progress(100, "Error")
            self._last_completion_payload = {
                "ok": False,
                "statusLine": "Run failed",
                "summaryLines": [],
                "warningLines": [],
                "detailText": str(exc),
            }
            self.run_finished.emit(False, str(exc))
            final_status = "failed"
            final_message = str(exc)
        finally:
            self._stopping_requested = False
            if proc is not None:
                proc.deleteLater()
            self._finalize_debug_logger(final_status, final_message)
            self._release_guards()


