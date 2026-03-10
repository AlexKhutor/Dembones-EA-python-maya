from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path

try:
    from PySide6 import QtCore
except ImportError:  # pragma: no cover
    from PySide2 import QtCore

import maya.cmds as cmds

from .log_utils import now_stamp
from .paths import default_cache_root
from .pipeline import build_cli_args, export_result_fbx, import_cli_result, prepare_run

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

    def is_running(self) -> bool:
        return self._process is not None

    @staticmethod
    def _lock_root() -> Path:
        return Path(default_cache_root()).parent

    @classmethod
    def _lock_file_path(cls) -> Path:
        return cls._lock_root() / "db_export_cli.run.lock"

    @classmethod
    def _state_file_path(cls) -> Path:
        return cls._lock_root() / "db_export_cli.run_state.json"

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
                    "Another DB_export CLI run is already in progress. Wait for the current run to finish."
                )
        self._global_run_lock = lock_file
        self.log.emit("global_run_lock_acquired: {0}".format(str(lock_path)))

    def _release_global_run_lock(self):
        if self._global_run_lock is None:
            return
        try:
            self._global_run_lock.unlock()
            self.log.emit("global_run_lock_released")
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
            self.log.emit("run_state_written: {0} pid={1}".format(str(path), int(pid)))
        except Exception as exc:
            self.log.emit("run_state_write_failed: {0}".format(exc))

    def _clear_run_state(self):
        path = self._state_file_path()
        if not path.exists():
            return
        try:
            path.unlink()
            self.log.emit("run_state_cleared")
        except Exception:
            pass

    def _kill_orphan_process_from_state(self):
        state_path = self._state_file_path()
        if not state_path.exists():
            return
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log.emit("run_state_read_failed: {0}".format(exc))
            self._clear_run_state()
            return

        pid = int(payload.get("pid") or 0)
        expected_exe = str(payload.get("exe") or "DemBones.exe").lower()
        if pid <= 0:
            self._clear_run_state()
            return

        image_name = self._query_process_image_name(pid).lower()
        if not image_name:
            self.log.emit("orphan_process_not_found_by_pid: {0}".format(pid))
            self._clear_run_state()
            return

        if image_name != expected_exe:
            self.log.emit(
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
            )
            self.log.emit("orphan_process_killed: pid={0} image={1}".format(pid, image_name))
        except Exception as exc:
            self.log.emit("orphan_process_kill_failed: pid={0} err={1}".format(pid, exc))
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
        for node in (selected.transform, selected.shape):
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
        self.log.emit("source_nodes_locked: {0}".format(", ".join(self._locked_source_nodes)))

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
        self.log.emit("source_nodes_unlocked: {0}".format(", ".join(self._locked_source_nodes)))
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

    def start(self, settings: CliRunSettings):
        if self._process is not None:
            raise RuntimeError("CLI process is already running.")

        self._acquire_global_run_lock()
        self._settings = settings
        self._buffer = []
        self._last_progress = 0
        self._stopping_requested = False

        try:
            self._set_progress(1, "Preparing export...")
            self.log.emit("=== DB_export run started ===")
            self.log.emit("time: {0}".format(now_stamp()))
            self.log.emit("cli_exe: {0}".format(settings.cli_exe))

            self._prepared = prepare_run(settings, self.log.emit)
            self._lock_source_nodes()
            self._set_progress(12, "FBX/ABC export completed")

            args = build_cli_args(settings, self._prepared)
            self.log.emit("cli_args: {0}".format(" ".join(args)))

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

            self._set_progress(18, "CLI started")
            self.run_started.emit()
        except Exception:
            self._release_guards()
            raise

    def stop(self, timeout_ms: int = 2000):
        if self._process is None:
            return
        self._stopping_requested = True
        self.log.emit("Stopping CLI process...")
        try:
            self._process.terminate()
            if not self._process.waitForFinished(int(timeout_ms)):
                self._process.kill()
                self._process.waitForFinished(int(timeout_ms))
        except Exception as exc:
            self.log.emit("Stop process error: {0}".format(exc))

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
            self.log.emit("[CLI] " + line)
            self._update_progress_from_line(line)

    def _on_process_error(self, _err):
        if self._process is None:
            return
        self.log.emit("[CLI] process error: {0}".format(self._process.errorString()))

    def _on_finished(self, exit_code, _exit_status):
        proc = self._process
        self._process = None
        try:
            if self._stopping_requested:
                self.log.emit("CLI process stopped by UI close/user action.")
                self._set_progress(100, "Stopped")
                self.run_finished.emit(False, "Run stopped.")
                return

            if exit_code != 0:
                message = "CLI exited with code {0}".format(exit_code)
                self.log.emit(message)
                self._set_progress(100, "CLI error")
                self.run_finished.emit(False, message)
                return

            if self._settings is None:
                raise RuntimeError("Internal error: missing run settings.")

            imported_message = "Scene import skipped by UI setting."
            result = None
            if self._settings.import_result_in_scene:
                self._set_progress(97, "Importing FBX into Maya...")
                namespace = self._settings.namespace if self._settings else "db_export_cli"
                result = import_cli_result(self._prepared, namespace, self._settings, self.log.emit)
                if not result.get("anim_curves") and not result.get("keyed_joints"):
                    self.log.emit(
                        "Warning: import completed, but no animCurve or joint keyframes were found."
                    )
                imported_message = (
                    "CLI result imported into scene "
                    "(ns={0}, method={1}).".format(
                        result.get("namespace", "unknown"),
                        result.get("method", "unknown"),
                    )
                )
            else:
                self.log.emit("import_skipped_by_ui: true")

            export_path = export_result_fbx(self._prepared, self._settings, self.log.emit)

            self.log.emit("cache_run_dir: {0}".format(self._prepared.run_dir))
            self.log.emit("cache_manifest: {0}".format(self._prepared.latest_manifest))
            self.log.emit("=== DB_export run finished ===")
            self._set_progress(100, "Done")
            self.run_finished.emit(
                True,
                "Done: {0}\nFBX saved: {1}".format(imported_message, export_path),
            )
        except Exception as exc:
            self._set_progress(100, "Error")
            self.run_finished.emit(False, str(exc))
        finally:
            self._stopping_requested = False
            if proc is not None:
                proc.deleteLater()
            self._release_guards()


