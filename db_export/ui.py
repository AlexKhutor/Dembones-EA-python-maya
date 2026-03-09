from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtCore, QtWidgets

import maya.cmds as cmds
import maya.OpenMayaUI as omui

from .pipeline import (
    CliRunSettings,
    build_cli_args,
    default_cache_root,
    default_cli_path,
    default_result_export_root,
    directory_size_bytes,
    export_result_fbx,
    import_cli_result,
    prepare_run,
)
from .selection import resolve_selected_mesh_with_deformers
from .version import VERSION


WINDOW_TITLE = "DB_export (CLI)"
_WINDOW = None


def _maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    if ptr is None:
        return None
    try:
        from shiboken6 import wrapInstance  # type: ignore
    except Exception:
        from shiboken2 import wrapInstance  # type: ignore
    return wrapInstance(int(ptr), QtWidgets.QWidget)


def _close_stale_windows():
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        try:
            title = widget.windowTitle() or ""
        except Exception:
            continue
        if title.startswith(WINDOW_TITLE):
            try:
                widget.close()
                widget.deleteLater()
            except Exception:
                pass


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
                    "Уже выполняется другой CLI-прогон DB_export. Дождись завершения текущего запуска."
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
                "Не удалось заблокировать source mesh для безопасного прогона: {0}".format(
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
            raise RuntimeError("CLI процесс уже выполняется.")

        self._acquire_global_run_lock()
        self._settings = settings
        self._buffer = []
        self._last_progress = 0
        self._stopping_requested = False

        try:
            self._set_progress(1, "Подготовка экспорта...")
            self.log.emit("=== DB_export run started ===")
            self.log.emit("time: {0}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.log.emit("cli_exe: {0}".format(settings.cli_exe))

            self._prepared = prepare_run(settings, self.log.emit)
            self._lock_source_nodes()
            self._set_progress(12, "Экспорт FBX/ABC выполнен")

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
                raise RuntimeError("Не удалось запустить CLI процесс.")
            try:
                self._write_run_state(int(process.processId()))
            except Exception:
                pass

            self._set_progress(18, "CLI запущен")
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
            self._set_progress(22, "CLI: чтение Alembic")
            return
        if "reading fbx" in line_l:
            self._set_progress(28, "CLI: чтение FBX")
            return
        if "initializing bones" in line_l:
            self._set_progress(35, "CLI: инициализация костей")
            return
        if "computing skinning decomposition" in line_l:
            self._set_progress(40, "CLI: оптимизация skinning")
            return
        if "convergence is reached" in line_l:
            self._set_progress(92, "CLI: сходимость достигнута")
            return
        if "writing outputs" in line_l:
            self._set_progress(95, "CLI: запись output FBX")
            return

        m = self._ITER_RE.search(line)
        if m:
            iter_idx = int(m.group(1))
            total = max(1, int(self._settings.n_iters))
            frac = min(iter_idx + 1, total) / float(total)
            value = 40 + int(50 * frac)
            self._set_progress(value, "CLI: итерация {0}/{1}".format(iter_idx + 1, total))

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
                self._set_progress(100, "Остановлено")
                self.run_finished.emit(False, "Запуск остановлен.")
                return

            if exit_code != 0:
                message = "CLI завершился с кодом {0}".format(exit_code)
                self.log.emit(message)
                self._set_progress(100, "Ошибка CLI")
                self.run_finished.emit(False, message)
                return

            if self._settings is None:
                raise RuntimeError("Internal error: missing run settings.")

            imported_message = "Импорт в сцену пропущен (по настройке UI)."
            result = None
            if self._settings.import_result_in_scene:
                self._set_progress(97, "Импорт FBX в Maya...")
                namespace = self._settings.namespace if self._settings else "db_export_cli"
                result = import_cli_result(self._prepared, namespace, self._settings, self.log.emit)
                if not result.get("anim_curves") and not result.get("keyed_joints"):
                    self.log.emit(
                        "Warning: импорт выполнен, но не найдены ни animCurve, ни keyframes на joint."
                    )
                imported_message = (
                    "CLI результат импортирован в сцену "
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
            self._set_progress(100, "Готово")
            self.run_finished.emit(
                True,
                "Готово: {0}\nFBX сохранен: {1}".format(imported_message, export_path),
            )
        except Exception as exc:
            self._set_progress(100, "Ошибка")
            self.run_finished.emit(False, str(exc))
        finally:
            self._stopping_requested = False
            if proc is not None:
                proc.deleteLater()
            self._release_guards()


class DBExportWindow(QtWidgets.QDialog):
    def __init__(self, parent=None):
        if parent is None:
            parent = _maya_main_window()
        super().__init__(parent)
        self.setWindowFlag(QtCore.Qt.Tool, True)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setWindowTitle("{0} v{1}".format(WINDOW_TITLE, VERSION))
        self.resize(820, 780)

        self.controller = CliRunController(self)
        self._closing = False
        self.controller.log.connect(self._append_log)
        self.controller.progress.connect(self._on_progress)
        self.controller.run_started.connect(self._on_run_started)
        self.controller.run_finished.connect(self._on_run_finished)

        self._build_ui()
        self.controller.recover_orphan_process()

    def _path_row(self, line_edit: QtWidgets.QLineEdit, button_text: str, callback):
        row = QtWidgets.QWidget(self)
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit, 1)
        button = QtWidgets.QPushButton(button_text, row)
        button.clicked.connect(callback)
        tip = line_edit.toolTip() or ""
        if tip:
            row.setToolTip(tip)
            button.setToolTip(tip)
        layout.addWidget(button, 0)
        return row

    @staticmethod
    def _help_text() -> str:
        return (
            "Быстрый старт (обычно достаточно):\n"
            "- Bones: 128\n"
            "- Max Influences Per Vertex: 8\n"
            "- Hierarchy Build Mode: Regroup joints under one root\n"
            "- Frame Step: 1\n\n"
            "Main:\n"
            "- крутить в первую очередь Bones / Max Influences / диапазон кадров.\n\n"
            "Advanced:\n"
            "- Initialization Iterations: качество стартовой раскладки.\n"
            "- Optimization Iterations: финальная точность (дольше = точнее).\n"
            "- Convergence Threshold: порог остановки (меньше = точнее, дольше).\n"
            "- Early Stop Patience: сколько ждать улучшений перед stop.\n\n"
            "Import Result Into Scene:\n"
            "- ON: автоматически импортирует итоговый FBX в сцену.\n"
            "- OFF: только сохраняет FBX в Result FBX Folder."
        )

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)

        top_bar = QtWidgets.QHBoxLayout()
        top_bar.addStretch(1)
        self.btn_help = QtWidgets.QPushButton("Help", self)
        self.btn_help.clicked.connect(self._on_show_help)
        top_bar.addWidget(self.btn_help)
        root.addLayout(top_bar)

        sel_box = QtWidgets.QGroupBox("Source Selection (shape with deformers)", self)
        sel_layout = QtWidgets.QGridLayout(sel_box)
        self.selection_label = QtWidgets.QLineEdit(self)
        self.selection_label.setReadOnly(True)
        self.selection_label.setPlaceholderText(
            "Сначала выбери shape/transform в Outliner, затем нажми 'Use Selection'"
        )
        self.selection_label.setToolTip(
            "Ожидается один renderable mesh shape. "
            "Проверяется, что shape реально деформируется (history/inMesh)."
        )
        self.selection_info = QtWidgets.QLabel("-", self)
        self.btn_use_selection = QtWidgets.QPushButton("Use Selection", self)
        self.btn_use_selection.clicked.connect(self._on_use_selection)
        sel_layout.addWidget(self.selection_label, 0, 0, 1, 2)
        sel_layout.addWidget(self.selection_info, 1, 0, 1, 1)
        sel_layout.addWidget(self.btn_use_selection, 1, 1, 1, 1)

        cfg_box = QtWidgets.QGroupBox("CLI Settings", self)
        cfg_layout = QtWidgets.QVBoxLayout(cfg_box)
        path_form = QtWidgets.QFormLayout()

        self.cli_path_edit = QtWidgets.QLineEdit(default_cli_path(), self)
        self.cli_path_edit.setToolTip(
            "Путь к DemBones.exe.\n"
            "Если указан неверно: запуск не стартует.\n"
            "Рекомендация: использовать установленный путь из модуля DB_export."
        )
        self.cache_edit = QtWidgets.QLineEdit(default_cache_root(), self)
        self.cache_edit.setToolTip(
            "Корень кэша. Для каждого запуска создается отдельная папка run_id.\n"
            "Файлы не удаляются: rest.fbx, anim.abc, output.fbx, manifest.\n"
            "Чем чаще итерации, тем важнее быстрый локальный диск."
        )
        self.cache_edit.editingFinished.connect(self._refresh_cache_size)

        self.result_export_edit = QtWidgets.QLineEdit(default_result_export_root(), self)
        self.result_export_edit.setToolTip(
            "Папка для финального FBX результата CLI.\n"
            "После каждого запуска итоговый FBX копируется в эту папку."
        )
        self.namespace_edit = QtWidgets.QLineEdit("db_export_cli", self)
        self.namespace_edit.setToolTip(
            "Namespace для импортируемого FBX.\n"
            "Если пусто/занято: автоматически выбирается уникальный namespace.\n"
            "Влияет только на удобство структуры сцены, не на качество результата."
        )

        self.bones_spin = QtWidgets.QSpinBox(self)
        self.bones_spin.setRange(1, 1024)
        self.bones_spin.setValue(128)
        self.bones_spin.setToolTip(
            "Целевое количество костей (-b).\n"
            "Больше костей: точнее повтор формы, но тяжелее риг/медленнее solve.\n"
            "Меньше костей: быстрее и стабильнее, но больше сглаживания.\n"
            "Старт: 64-128 для простых мешей, 128-256 для сложных деформируемых мешей.\n"
            "Оптимум для большинства мешей: 128."
        )

        self.bind_update_combo = QtWidgets.QComboBox(self)
        self.bind_update_combo.addItem("Keep source hierarchy (0)", 0)
        self.bind_update_combo.addItem("Partial hierarchy update (1)", 1)
        self.bind_update_combo.addItem("Regroup joints under one root (2)", 2)
        self.bind_update_combo.setCurrentIndex(2)
        self.bind_update_combo.setToolTip(
            "Режим построения иерархии костей (--bindUpdate).\n"
            "0: минимальные изменения иерархии.\n"
            "1: частичная перестройка.\n"
            "2: одна общая root-иерархия (обычно лучший вариант для Maya).\n"
            "Рекомендуется: 2."
        )

        self.nnz_spin = QtWidgets.QSpinBox(self)
        self.nnz_spin.setRange(1, 16)
        self.nnz_spin.setValue(8)
        self.nnz_spin.setToolTip(
            "Максимум влияний на вершину (--nnz).\n"
            "Выше: точнее деформация и мягче переходы, но тяжелее skin/веса.\n"
            "Ниже: чище и легче риг, но может теряться мелкая форма.\n"
            "Рекомендуется: 8; для более жесткого результата: 6."
        )

        self.init_iters_spin = QtWidgets.QSpinBox(self)
        self.init_iters_spin.setRange(1, 500)
        self.init_iters_spin.setValue(10)
        self.init_iters_spin.setToolTip(
            "Итерации инициализации (--nInitIters).\n"
            "Влияет на стартовую раскладку костей/весов перед основным solve.\n"
            "Увеличивать при плохом старте или сложной топологии.\n"
            "Рекомендуется: 10."
        )

        self.iters_spin = QtWidgets.QSpinBox(self)
        self.iters_spin.setRange(1, 5000)
        self.iters_spin.setValue(100)
        self.iters_spin.setToolTip(
            "Основные итерации оптимизации (--nIters).\n"
            "Больше: точнее (ниже RMSE), но дольше расчёт.\n"
            "Рекомендуется: 100-150; для сложных мешей: 150-250."
        )

        self.tolerance_spin = QtWidgets.QDoubleSpinBox(self)
        self.tolerance_spin.setDecimals(6)
        self.tolerance_spin.setRange(0.000001, 1.0)
        self.tolerance_spin.setSingleStep(0.0005)
        self.tolerance_spin.setValue(0.001)
        self.tolerance_spin.setToolTip(
            "Порог остановки по сходимости (--tolerance).\n"
            "Ниже: точнее, но дольше.\n"
            "Выше: быстрее, но больше остаточная ошибка.\n"
            "Рекомендуется: 0.001; для более точного solve: 0.0005."
        )

        self.patience_spin = QtWidgets.QSpinBox(self)
        self.patience_spin.setRange(1, 100)
        self.patience_spin.setValue(3)
        self.patience_spin.setToolTip(
            "Ранняя остановка: сколько итераций ждать улучшения (--patience).\n"
            "Выше: стабильнее на шумных данных, но дольше.\n"
            "Ниже: быстрее, но риск остановиться рано.\n"
            "Рекомендуется: 3-5."
        )

        self.frame_start = QtWidgets.QSpinBox(self)
        self.frame_start.setRange(-100000, 100000)
        self.frame_start.setValue(1)
        self.frame_start.setToolTip(
            "Начальный кадр диапазона.\n"
            "Влияет на то, какие ключи войдут в solve."
        )
        self.frame_end = QtWidgets.QSpinBox(self)
        self.frame_end.setRange(-100000, 100000)
        self.frame_end.setValue(60)
        self.frame_end.setToolTip(
            "Конечный кадр диапазона.\n"
            "Чем длиннее диапазон, тем дольше solve."
        )
        self.frame_step = QtWidgets.QSpinBox(self)
        self.frame_step.setRange(1, 1000)
        self.frame_step.setValue(1)
        self.frame_step.setToolTip(
            "Шаг семплирования кадров.\n"
            "1 = максимальная точность.\n"
            "2+ ускоряет, но может пропускать быстрые движения."
        )

        self.debug_cli_checkbox = QtWidgets.QCheckBox("Verbose CLI Log", self)
        self.debug_cli_checkbox.setChecked(True)
        self.debug_cli_checkbox.setToolTip(
            "Показывать полный stdout/stderr CLI в логе.\n"
            "Полезно для диагностики RMSE, итераций и ошибок импорта."
        )

        self.import_result_checkbox = QtWidgets.QCheckBox("Import Result Into Scene", self)
        self.import_result_checkbox.setChecked(True)
        self.import_result_checkbox.setToolTip(
            "Если включено: итоговый FBX после CLI автоматически импортируется в текущую сцену Maya.\n"
            "Если выключено: только сохраняется файл в Result FBX Folder."
        )

        path_form.addRow("CLI Executable", self._path_row(self.cli_path_edit, "Browse...", self._on_browse_cli))

        cache_row = QtWidgets.QWidget(self)
        cache_row_layout = QtWidgets.QHBoxLayout(cache_row)
        cache_row_layout.setContentsMargins(0, 0, 0, 0)
        cache_row_layout.addWidget(self.cache_edit, 1)
        self.btn_browse_cache = QtWidgets.QPushButton("Browse...", cache_row)
        self.btn_browse_cache.clicked.connect(self._on_browse_cache)
        cache_row_layout.addWidget(self.btn_browse_cache, 0)
        self.cache_size_label = QtWidgets.QLabel("Cache usage: - MB", cache_row)
        cache_row_layout.addWidget(self.cache_size_label, 0)
        path_form.addRow("Cache Root", cache_row)

        path_form.addRow(
            "Result FBX Folder",
            self._path_row(self.result_export_edit, "Browse...", self._on_browse_result_export),
        )
        path_form.addRow("Import Namespace", self.namespace_edit)
        cfg_layout.addLayout(path_form)

        tabs = QtWidgets.QTabWidget(self)
        main_tab = QtWidgets.QWidget(self)
        main_form = QtWidgets.QFormLayout(main_tab)
        main_form.addRow("Target Bone Count", self.bones_spin)
        main_form.addRow("Hierarchy Build Mode", self.bind_update_combo)
        main_form.addRow("Max Influences Per Vertex", self.nnz_spin)
        main_form.addRow("Frame Start", self.frame_start)
        main_form.addRow("Frame End", self.frame_end)
        main_form.addRow("Frame Step", self.frame_step)
        main_form.addRow(self.import_result_checkbox)
        tabs.addTab(main_tab, "Main")

        adv_tab = QtWidgets.QWidget(self)
        adv_form = QtWidgets.QFormLayout(adv_tab)
        adv_form.addRow("Initialization Iterations", self.init_iters_spin)
        adv_form.addRow("Optimization Iterations", self.iters_spin)
        adv_form.addRow("Convergence Threshold", self.tolerance_spin)
        adv_form.addRow("Early Stop Patience", self.patience_spin)
        adv_form.addRow(self.debug_cli_checkbox)
        tabs.addTab(adv_tab, "Advanced")

        cfg_layout.addWidget(tabs)

        actions = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("Run CLI Export", self)
        self.btn_run.clicked.connect(self._on_run)
        actions.addWidget(self.btn_run)
        actions.addStretch(1)

        progress_box = QtWidgets.QGroupBox("Progress", self)
        progress_layout = QtWidgets.QVBoxLayout(progress_box)
        self.progress_bar = QtWidgets.QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label = QtWidgets.QLabel("Idle", self)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_label)

        log_box = QtWidgets.QGroupBox("Run Log", self)
        log_layout = QtWidgets.QVBoxLayout(log_box)
        self.log_edit = QtWidgets.QPlainTextEdit(self)
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)

        root.addWidget(sel_box)
        root.addWidget(cfg_box)
        root.addLayout(actions)
        root.addWidget(progress_box)
        root.addWidget(log_box)
        self._refresh_cache_size()

    def _append_log(self, text: str):
        self.log_edit.appendPlainText(text)
        sb = self.log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_progress(self, value: int, text: str):
        self.progress_bar.setValue(int(value))
        if text:
            self.progress_label.setText(text)

    def _on_use_selection(self):
        try:
            info = resolve_selected_mesh_with_deformers()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "DB_export", str(exc))
            return

        self.selection_label.setText(info.shape)
        self.selection_info.setText(
            "Transform: {0} | Deformers: {1}".format(info.transform, ", ".join(info.deformer_types))
        )
        self._append_log("selection accepted: {0}".format(info.shape))

    def _collect_settings(self) -> CliRunSettings:
        bind_update_value = self.bind_update_combo.currentData()
        if bind_update_value is None:
            bind_update_value = int(self.bind_update_combo.currentText().split("(")[-1].rstrip(")"))
        return CliRunSettings(
            cli_exe=self.cli_path_edit.text().strip(),
            cache_root=self.cache_edit.text().strip() or default_cache_root(),
            result_export_root=self.result_export_edit.text().strip() or default_result_export_root(),
            namespace=self.namespace_edit.text().strip(),
            import_result_in_scene=bool(self.import_result_checkbox.isChecked()),
            bones=int(self.bones_spin.value()),
            bind_update=int(bind_update_value),
            nnz=int(self.nnz_spin.value()),
            n_init_iters=int(self.init_iters_spin.value()),
            n_iters=int(self.iters_spin.value()),
            tolerance=float(self.tolerance_spin.value()),
            patience=int(self.patience_spin.value()),
            frame_start=int(self.frame_start.value()),
            frame_end=int(self.frame_end.value()),
            frame_step=int(self.frame_step.value()),
            debug_cli=bool(self.debug_cli_checkbox.isChecked()),
            keep_imported_cli=True,
        )

    def _on_browse_cli(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select DemBones CLI executable",
            self.cli_path_edit.text().strip() or str(Path.home()),
            "Executable (*.exe);;All files (*.*)",
        )
        if path:
            self.cli_path_edit.setText(path)

    def _on_browse_cache(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select Cache Root Directory",
            self.cache_edit.text().strip() or default_cache_root(),
        )
        if path:
            self.cache_edit.setText(path)
            self._refresh_cache_size()

    def _on_browse_result_export(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select Result FBX Export Directory",
            self.result_export_edit.text().strip() or default_result_export_root(),
        )
        if path:
            self.result_export_edit.setText(path)

    def _refresh_cache_size(self):
        cache_root = self.cache_edit.text().strip() or default_cache_root()
        size_bytes = directory_size_bytes(cache_root)
        size_mb = float(size_bytes) / (1024.0 * 1024.0)
        self.cache_size_label.setText("Cache usage: {0:.2f} MB".format(size_mb))

    def _on_run(self):
        try:
            settings = self._collect_settings()
            self.progress_bar.setValue(0)
            self.progress_label.setText("Запуск...")
            self.controller.start(settings)
        except Exception as exc:
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ошибка запуска")
            QtWidgets.QMessageBox.critical(self, "DB_export failed", str(exc))

    def _on_run_started(self):
        self.btn_run.setEnabled(False)
        self.btn_use_selection.setEnabled(False)
        self.btn_browse_cache.setEnabled(False)

    def _on_run_finished(self, ok: bool, message: str):
        self.btn_run.setEnabled(True)
        self.btn_use_selection.setEnabled(True)
        self.btn_browse_cache.setEnabled(True)
        self._refresh_cache_size()
        if self._closing:
            return
        if ok:
            QtWidgets.QMessageBox.information(self, "DB_export", message)
        else:
            QtWidgets.QMessageBox.critical(self, "DB_export", message)

    def _on_show_help(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("DB_export Help")
        dlg.resize(620, 420)
        layout = QtWidgets.QVBoxLayout(dlg)
        text = QtWidgets.QPlainTextEdit(dlg)
        text.setReadOnly(True)
        text.setPlainText(self._help_text())
        layout.addWidget(text)
        btn = QtWidgets.QPushButton("Close", dlg)
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn)
        if hasattr(dlg, "exec"):
            dlg.exec()
        else:
            dlg.exec_()

    def closeEvent(self, event):
        self._closing = True
        try:
            if self.controller.is_running():
                self.controller.stop()
        except Exception:
            pass
        super().closeEvent(event)


def open_window(parent=None):
    global _WINDOW
    _close_stale_windows()
    if parent is None:
        parent = _maya_main_window()
    if _WINDOW is not None:
        try:
            _WINDOW.close()
            _WINDOW.deleteLater()
        except Exception:
            pass
        _WINDOW = None

    _WINDOW = DBExportWindow(parent=parent)
    _WINDOW.show()
    _WINDOW.raise_()
    _WINDOW.activateWindow()
    return _WINDOW
