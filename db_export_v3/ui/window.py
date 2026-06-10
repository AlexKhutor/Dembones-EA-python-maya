from __future__ import annotations

from pathlib import Path

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtCore, QtGui, QtWidgets

import maya.OpenMayaUI as omui

from ..core.models import CliRunSettings
from ..maya.hierarchy import describe_alembic_sources, resolve_selected_joint_root
from ..maya.paths import (
    default_cache_root,
    default_cli_path,
    default_result_export_root,
    directory_size_bytes,
)
from ..maya.selection import resolve_mesh_with_deformers_from_node, resolve_selected_mesh_with_deformers
from .controller import CliRunController
from .layout import build_window_ui
from .text import fixed_variant_note, help_text
from ..version import VERSION


WINDOW_TITLE = "DB_export_v3 (CLI)"
_WINDOW = None
_SETTINGS_ORG = "HeatTools"
_SETTINGS_APP = "db_export_v3"
_SETTINGS_GEOMETRY_KEY = "ui/window_geometry"
_SETTINGS_VALUES_GROUP = "ui/values"


def _maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    if ptr is None:
        return None
    try:
        from shiboken6 import wrapInstance  # type: ignore
    except Exception:
        from shiboken2 import wrapInstance  # type: ignore
    return wrapInstance(int(ptr), QtWidgets.QWidget)


def _window_settings() -> QtCore.QSettings:
    return QtCore.QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def _restore_window_geometry(window) -> bool:
    geometry = _window_settings().value(_SETTINGS_GEOMETRY_KEY)
    if geometry is None:
        return False
    try:
        return bool(window.restoreGeometry(geometry))
    except Exception:
        return False


def _save_window_geometry(window) -> None:
    _window_settings().setValue(_SETTINGS_GEOMETRY_KEY, window.saveGeometry())


def _text_block_width(text: str, font: QtGui.QFont) -> int:
    metrics = QtGui.QFontMetrics(font)
    lines = [line for line in str(text or "").splitlines() if line]
    if not lines:
        return 0
    return max(metrics.horizontalAdvance(line) for line in lines)


def _screen_available_geometry(widget: QtWidgets.QWidget | None) -> QtCore.QRect:
    if widget is not None:
        screen = widget.screen()
        if screen is not None:
            return screen.availableGeometry()
    app = QtWidgets.QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    return screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1920, 1080)


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


class DBExportWindow(QtWidgets.QDialog):
    _SLIDER_CONTEXT_TIP = (
        "Drag to change. Shift+drag: slower. Ctrl+drag: faster. "
        "Wheel: step. Shift/Ctrl+wheel: larger step."
    )

    def __init__(self, parent=None):
        if parent is None:
            parent = _maya_main_window()
        super().__init__(parent)
        self.setWindowFlag(QtCore.Qt.Tool, True)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setWindowTitle("{0} v{1}".format(WINDOW_TITLE, VERSION))

        self.controller = CliRunController(self)
        self._closing = False
        self.controller.log.connect(self._append_log)
        self.controller.progress.connect(self._on_progress)
        self.controller.run_started.connect(self._on_run_started)
        self.controller.run_finished.connect(self._on_run_finished)
        self._active_solve_mode = "auto"
        self._restoring_ui_settings = False
        self._settings_save_timer = QtCore.QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(150)
        self._settings_save_timer.timeout.connect(self._save_ui_settings)

        self._build_ui()
        self._restore_ui_settings()
        self._connect_persistent_controls()
        if not _restore_window_geometry(self):
            self.resize(820, 720)
            self._schedule_adjust_height()
        self.controller.recover_orphan_process()

    @staticmethod
    def _help_text() -> str:
        return help_text()

    def _build_ui(self):
        build_window_ui(self)
        self.tabs.currentChanged.connect(self._on_mode_tab_changed)
        self.fixed_variant_combo.currentIndexChanged.connect(self._on_fixed_variant_changed)
        self._refresh_cache_size()
        self._bind_context_tips()
        self._on_mode_tab_changed(self.tabs.currentIndex())
        self._on_fixed_variant_changed(self.fixed_variant_combo.currentIndex())
        self.btn_paths_toggle.toggled.connect(self._schedule_adjust_height)
        self.btn_log_toggle.toggled.connect(self._schedule_adjust_height)

    @staticmethod
    def _settings_value(settings: QtCore.QSettings, key: str, default=None):
        value = settings.value(key, default)
        return default if value is None else value

    @staticmethod
    def _settings_bool(settings: QtCore.QSettings, key: str, default: bool) -> bool:
        value = settings.value(key, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _settings_int(settings: QtCore.QSettings, key: str, default: int) -> int:
        value = settings.value(key, default)
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _settings_float(settings: QtCore.QSettings, key: str, default: float) -> float:
        value = settings.value(key, default)
        try:
            return float(value)
        except Exception:
            return float(default)

    def _schedule_save_ui_settings(self, *_args) -> None:
        if self._restoring_ui_settings:
            return
        self._settings_save_timer.start()

    def _connect_persistent_controls(self) -> None:
        editable_line_edits = (
            self.cli_path_edit,
            self.cache_edit,
            self.result_export_edit,
            self.namespace_edit,
            self.fbx_name_edit,
            self.clip_prefix_edit,
            self.fixed_fbx_name_edit,
            self.fixed_clip_prefix_edit,
            self.fixed_source_animated_mesh_edit,
            self.fixed_bound_init_mesh_edit,
            self.fixed_hierarchy_root_edit,
        )
        for widget in editable_line_edits:
            widget.textChanged.connect(self._schedule_save_ui_settings)

        checkboxes = (
            self.import_result_checkbox,
            self.debug_cli_checkbox,
            self.write_debug_logs_checkbox,
            self.wrap_world_root_checkbox,
            self.fixed_wrap_world_root_checkbox,
        )
        for widget in checkboxes:
            widget.toggled.connect(self._schedule_save_ui_settings)

        combos = (
            self.bind_update_combo,
            self.fixed_variant_combo,
            self.fixed_bind_update_combo,
        )
        for widget in combos:
            widget.currentIndexChanged.connect(self._schedule_save_ui_settings)
        self.tabs.currentChanged.connect(self._schedule_save_ui_settings)

        toggles = (
            self.btn_paths_toggle,
            self.btn_log_toggle,
        )
        for widget in toggles:
            widget.toggled.connect(self._schedule_save_ui_settings)

        sliders = (
            self.bones_spin,
            self.nnz_spin,
            self.init_iters_spin,
            self.iters_spin,
            self.patience_spin,
            self.frame_start,
            self.frame_end,
            self.frame_step,
            self.fixed_frame_start,
            self.fixed_frame_end,
            self.fixed_frame_step,
        )
        for widget in sliders:
            widget.valueChanged.connect(self._schedule_save_ui_settings)

        self.tolerance_spin.valueChanged.connect(self._schedule_save_ui_settings)

    def _restore_ui_settings(self) -> None:
        settings = _window_settings()
        self._restoring_ui_settings = True
        settings.beginGroup(_SETTINGS_VALUES_GROUP)
        try:
            self.cli_path_edit.setText(str(self._settings_value(settings, "cli_path", self.cli_path_edit.text())))
            self.cache_edit.setText(str(self._settings_value(settings, "cache_root", self.cache_edit.text())))
            self.result_export_edit.setText(
                str(self._settings_value(settings, "result_export_root", self.result_export_edit.text()))
            )
            self.namespace_edit.setText(str(self._settings_value(settings, "namespace", self.namespace_edit.text())))

            self.fbx_name_edit.setText(str(self._settings_value(settings, "auto_fbx_name", self.fbx_name_edit.text())))
            self.clip_prefix_edit.setText(
                str(self._settings_value(settings, "auto_clip_prefix", self.clip_prefix_edit.text()))
            )
            self.bones_spin.setValue(self._settings_int(settings, "bones", int(self.bones_spin.value())))
            self._set_combo_value(settings=settings, combo=self.bind_update_combo, key="bind_update")
            self.wrap_world_root_checkbox.setChecked(
                self._settings_bool(settings, "wrap_world_root", self.wrap_world_root_checkbox.isChecked())
            )
            self.nnz_spin.setValue(self._settings_int(settings, "nnz", int(self.nnz_spin.value())))
            self.init_iters_spin.setValue(
                self._settings_int(settings, "n_init_iters", int(self.init_iters_spin.value()))
            )
            self.iters_spin.setValue(self._settings_int(settings, "n_iters", int(self.iters_spin.value())))
            self.tolerance_spin.setValue(
                self._settings_float(settings, "tolerance", float(self.tolerance_spin.value()))
            )
            self.patience_spin.setValue(self._settings_int(settings, "patience", int(self.patience_spin.value())))
            self.frame_start.setValue(self._settings_int(settings, "frame_start", int(self.frame_start.value())))
            self.frame_end.setValue(self._settings_int(settings, "frame_end", int(self.frame_end.value())))
            self.frame_step.setValue(self._settings_int(settings, "frame_step", int(self.frame_step.value())))

            self.fixed_fbx_name_edit.setText(
                str(self._settings_value(settings, "fixed_fbx_name", self.fixed_fbx_name_edit.text()))
            )
            self.fixed_clip_prefix_edit.setText(
                str(self._settings_value(settings, "fixed_clip_prefix", self.fixed_clip_prefix_edit.text()))
            )
            self.fixed_source_animated_mesh_edit.setText(
                str(
                    self._settings_value(
                        settings,
                        "fixed_source_animated_mesh",
                        self.fixed_source_animated_mesh_edit.text(),
                    )
                )
            )
            self.fixed_bound_init_mesh_edit.setText(
                str(self._settings_value(settings, "fixed_bound_init_mesh", self.fixed_bound_init_mesh_edit.text()))
            )
            self.fixed_hierarchy_root_edit.setText(
                str(self._settings_value(settings, "fixed_hierarchy_root", self.fixed_hierarchy_root_edit.text()))
            )
            self._set_combo_value(settings=settings, combo=self.fixed_variant_combo, key="fixed_solve_variant")
            self._set_combo_value(settings=settings, combo=self.fixed_bind_update_combo, key="fixed_bind_update")
            self.fixed_wrap_world_root_checkbox.setChecked(
                self._settings_bool(
                    settings,
                    "fixed_wrap_world_root",
                    self.fixed_wrap_world_root_checkbox.isChecked(),
                )
            )
            self.fixed_frame_start.setValue(
                self._settings_int(settings, "fixed_frame_start", int(self.fixed_frame_start.value()))
            )
            self.fixed_frame_end.setValue(
                self._settings_int(settings, "fixed_frame_end", int(self.fixed_frame_end.value()))
            )
            self.fixed_frame_step.setValue(
                self._settings_int(settings, "fixed_frame_step", int(self.fixed_frame_step.value()))
            )

            self.import_result_checkbox.setChecked(
                self._settings_bool(settings, "import_result_in_scene", self.import_result_checkbox.isChecked())
            )
            self.debug_cli_checkbox.setChecked(
                self._settings_bool(settings, "debug_cli", self.debug_cli_checkbox.isChecked())
            )
            self.write_debug_logs_checkbox.setChecked(
                self._settings_bool(settings, "write_debug_logs", self.write_debug_logs_checkbox.isChecked())
            )
            self.btn_paths_toggle.setChecked(
                self._settings_bool(settings, "paths_panel_expanded", self.btn_paths_toggle.isChecked())
            )
            self.btn_log_toggle.setChecked(
                self._settings_bool(settings, "log_panel_expanded", self.btn_log_toggle.isChecked())
            )
            tab_index = self._settings_int(settings, "active_tab", self.tabs.currentIndex())
            if 0 <= tab_index < self.tabs.count():
                self.tabs.setCurrentIndex(tab_index)
        finally:
            settings.endGroup()
            self._restoring_ui_settings = False

        self._refresh_fixed_alembic_source(show_errors=False)
        self._refresh_cache_size()
        self._schedule_adjust_height()

    def _set_combo_value(self, settings: QtCore.QSettings, combo: QtWidgets.QComboBox, key: str) -> None:
        self._set_combo_from_value(combo, settings.value(key))

    def _set_combo_from_value(self, combo: QtWidgets.QComboBox, value) -> None:
        if value is None:
            return
        index = combo.findData(value)
        if index < 0:
            try:
                numeric = int(value)
            except Exception:
                numeric = None
            if numeric is not None:
                index = combo.findData(numeric)
        if index < 0:
            try:
                index = int(value)
            except Exception:
                index = -1
        if 0 <= index < combo.count():
            combo.setCurrentIndex(index)

    def _save_ui_settings(self) -> None:
        if self._restoring_ui_settings:
            return
        settings = _window_settings()
        settings.beginGroup(_SETTINGS_VALUES_GROUP)
        try:
            settings.setValue("active_tab", self.tabs.currentIndex())
            settings.setValue("paths_panel_expanded", self.btn_paths_toggle.isChecked())
            settings.setValue("log_panel_expanded", self.btn_log_toggle.isChecked())

            settings.setValue("cli_path", self.cli_path_edit.text().strip())
            settings.setValue("cache_root", self.cache_edit.text().strip())
            settings.setValue("result_export_root", self.result_export_edit.text().strip())
            settings.setValue("namespace", self.namespace_edit.text().strip())

            settings.setValue("auto_fbx_name", self.fbx_name_edit.text().strip())
            settings.setValue("auto_clip_prefix", self.clip_prefix_edit.text().strip())
            settings.setValue("bones", int(self.bones_spin.value()))
            settings.setValue("bind_update", self.bind_update_combo.currentData())
            settings.setValue("wrap_world_root", bool(self.wrap_world_root_checkbox.isChecked()))
            settings.setValue("nnz", int(self.nnz_spin.value()))
            settings.setValue("n_init_iters", int(self.init_iters_spin.value()))
            settings.setValue("n_iters", int(self.iters_spin.value()))
            settings.setValue("tolerance", float(self.tolerance_spin.value()))
            settings.setValue("patience", int(self.patience_spin.value()))
            settings.setValue("frame_start", int(self.frame_start.value()))
            settings.setValue("frame_end", int(self.frame_end.value()))
            settings.setValue("frame_step", int(self.frame_step.value()))

            settings.setValue("fixed_fbx_name", self.fixed_fbx_name_edit.text().strip())
            settings.setValue("fixed_clip_prefix", self.fixed_clip_prefix_edit.text().strip())
            settings.setValue("fixed_solve_variant", self.fixed_variant_combo.currentData())
            settings.setValue("fixed_source_animated_mesh", self.fixed_source_animated_mesh_edit.text().strip())
            settings.setValue("fixed_bound_init_mesh", self.fixed_bound_init_mesh_edit.text().strip())
            settings.setValue("fixed_hierarchy_root", self.fixed_hierarchy_root_edit.text().strip())
            settings.setValue("fixed_bind_update", self.fixed_bind_update_combo.currentData())
            settings.setValue("fixed_wrap_world_root", bool(self.fixed_wrap_world_root_checkbox.isChecked()))
            settings.setValue("fixed_frame_start", int(self.fixed_frame_start.value()))
            settings.setValue("fixed_frame_end", int(self.fixed_frame_end.value()))
            settings.setValue("fixed_frame_step", int(self.fixed_frame_step.value()))

            settings.setValue("import_result_in_scene", bool(self.import_result_checkbox.isChecked()))
            settings.setValue("debug_cli", bool(self.debug_cli_checkbox.isChecked()))
            settings.setValue("write_debug_logs", bool(self.write_debug_logs_checkbox.isChecked()))
        finally:
            settings.endGroup()
        settings.sync()

    def _bind_context_tips(self) -> None:
        self._context_tip_targets = (
            self.bones_spin,
            self.nnz_spin,
            self.init_iters_spin,
            self.iters_spin,
            self.patience_spin,
            self.frame_start,
            self.frame_end,
            self.frame_step,
            self.fixed_frame_start,
            self.fixed_frame_end,
            self.fixed_frame_step,
        )
        for widget in self._context_tip_targets:
            widget.installEventFilter(self)
        self._clear_context_tip()

    def _set_context_tip(self, text: str) -> None:
        self.context_tip_label.setText(text)

    def _clear_context_tip(self) -> None:
        self.context_tip_label.clear()

    def _adjust_window_height(self) -> None:
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self.resize(self.width(), self.sizeHint().height())

    def _schedule_adjust_height(self, *_args) -> None:
        QtCore.QTimer.singleShot(0, self._adjust_window_height)

    def _append_log(self, text: str):
        self.log_edit.appendPlainText(text)
        sb = self.log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_progress(self, value: int, text: str):
        if value or text:
            if not self.progress_strip.isVisible():
                self.progress_strip.setVisible(True)
                self._schedule_adjust_height()
        self.progress_bar.setValue(int(value))
        if text:
            self.progress_label.setText(text)

    def _on_mode_tab_changed(self, index: int) -> None:
        current = self.tabs.widget(index)
        if current is self.main_tab:
            self._active_solve_mode = "auto"
        elif current is self.fixed_tab:
            self._active_solve_mode = "fixed_bones"

    def _on_fixed_variant_changed(self, _index: int) -> None:
        variant = str(self.fixed_variant_combo.currentData() or "optimize_existing")
        self.fixed_variant_note_label.setText(fixed_variant_note(variant))
        if variant == "transforms_only":
            self.fixed_variant_note_label.setStyleSheet("color: #d9a441;")
        elif variant == "weights_only":
            self.fixed_variant_note_label.setStyleSheet("color: #b0b0b0;")
        else:
            self.fixed_variant_note_label.setStyleSheet("color: #8fbf8f;")

    def _collect_settings(self) -> CliRunSettings:
        solve_mode = getattr(self, "_active_solve_mode", "auto")
        if solve_mode == "fixed_bones":
            bind_update_value = self.fixed_bind_update_combo.currentData()
            if bind_update_value is None:
                bind_update_value = int(self.fixed_bind_update_combo.currentText().split("(")[-1].rstrip(")"))
            fixed_variant = str(self.fixed_variant_combo.currentData() or "optimize_existing")
            return CliRunSettings(
                cli_exe=self.cli_path_edit.text().strip() or default_cli_path(),
                cache_root=self.cache_edit.text().strip() or default_cache_root(),
                result_export_root=self.result_export_edit.text().strip() or default_result_export_root(),
                namespace=self.namespace_edit.text().strip(),
                solve_mode="fixed_bones",
                fixed_solve_variant=fixed_variant,
                source_animated_mesh=self.fixed_source_animated_mesh_edit.text().strip(),
                bound_init_mesh=self.fixed_bound_init_mesh_edit.text().strip(),
                hierarchy_root=self.fixed_hierarchy_root_edit.text().strip(),
                fbx_name=self.fixed_fbx_name_edit.text().strip(),
                clip_prefix=self.fixed_clip_prefix_edit.text().strip(),
                import_result_in_scene=bool(self.import_result_checkbox.isChecked()),
                wrap_world_root=bool(self.fixed_wrap_world_root_checkbox.isChecked()),
                bones=int(self.bones_spin.value()),
                bind_update=int(bind_update_value),
                nnz=int(self.nnz_spin.value()),
                n_init_iters=int(self.init_iters_spin.value()),
                n_iters=int(self.iters_spin.value()),
                tolerance=float(self.tolerance_spin.value()),
                patience=int(self.patience_spin.value()),
                frame_start=int(self.fixed_frame_start.value()),
                frame_end=int(self.fixed_frame_end.value()),
                frame_step=int(self.fixed_frame_step.value()),
                debug_cli=bool(self.debug_cli_checkbox.isChecked()),
                write_debug_logs=bool(self.write_debug_logs_checkbox.isChecked()),
                keep_imported_cli=True,
            )

        bind_update_value = self.bind_update_combo.currentData()
        if bind_update_value is None:
            bind_update_value = int(self.bind_update_combo.currentText().split("(")[-1].rstrip(")"))
        return CliRunSettings(
            cli_exe=self.cli_path_edit.text().strip() or default_cli_path(),
            cache_root=self.cache_edit.text().strip() or default_cache_root(),
            result_export_root=self.result_export_edit.text().strip() or default_result_export_root(),
            namespace=self.namespace_edit.text().strip(),
            solve_mode="auto",
            fbx_name=self.fbx_name_edit.text().strip(),
            clip_prefix=self.clip_prefix_edit.text().strip(),
            import_result_in_scene=bool(self.import_result_checkbox.isChecked()),
            wrap_world_root=bool(self.wrap_world_root_checkbox.isChecked()),
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
            write_debug_logs=bool(self.write_debug_logs_checkbox.isChecked()),
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
            self._schedule_save_ui_settings()

    def _on_browse_cache(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select Cache Root Directory",
            self.cache_edit.text().strip() or default_cache_root(),
        )
        if path:
            self.cache_edit.setText(path)
            self._refresh_cache_size()
            self._schedule_save_ui_settings()

    def _on_browse_result_export(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select Result FBX Export Directory",
            self.result_export_edit.text().strip() or default_result_export_root(),
        )
        if path:
            self.result_export_edit.setText(path)
            self._schedule_save_ui_settings()

    def _on_use_selected_fixed_hierarchy(self):
        try:
            self.fixed_hierarchy_root_edit.setText(resolve_selected_joint_root())
            self._schedule_save_ui_settings()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DB_export_v3 failed", str(exc))

    def _on_use_selected_fixed_source_mesh(self):
        try:
            selected = resolve_selected_mesh_with_deformers()
            self.fixed_source_animated_mesh_edit.setText(selected.shape)
            self._on_refresh_fixed_alembic_from_source_mesh()
            self._schedule_save_ui_settings()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DB_export_v3 failed", str(exc))

    def _on_use_selected_fixed_bound_mesh(self):
        try:
            selected = resolve_selected_mesh_with_deformers()
            self.fixed_bound_init_mesh_edit.setText(selected.shape)
            self._schedule_save_ui_settings()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DB_export_v3 failed", str(exc))

    def _refresh_fixed_alembic_source(self, show_errors: bool = True) -> None:
        try:
            raw = self.fixed_source_animated_mesh_edit.text().strip()
            if not raw:
                self.fixed_alembic_source_edit.clear()
                return
            selected = resolve_mesh_with_deformers_from_node(raw)
            alembic_sources = describe_alembic_sources(selected.shape)
            if alembic_sources:
                labels = []
                for item in alembic_sources:
                    label = item.get("node") or "<AlembicNode>"
                    file_path = item.get("filePath") or ""
                    if file_path:
                        label = "{0} :: {1}".format(label, file_path)
                    labels.append(label)
                self.fixed_alembic_source_edit.setText(" | ".join(labels))
            else:
                self.fixed_alembic_source_edit.setText("<no AlembicNode found in history>")
        except Exception as exc:
            self.fixed_alembic_source_edit.setText("<unable to resolve Alembic source>")
            if show_errors:
                QtWidgets.QMessageBox.critical(self, "DB_export_v3 failed", str(exc))

    def _on_refresh_fixed_alembic_from_source_mesh(self):
        self._refresh_fixed_alembic_source(show_errors=True)

    def _refresh_cache_size(self):
        cache_root = self.cache_edit.text().strip() or default_cache_root()
        size_bytes = directory_size_bytes(cache_root)
        size_mb = float(size_bytes) / (1024.0 * 1024.0)
        self.cache_size_label.setText("Cache usage: {0:.2f} MB".format(size_mb))

    def _set_run_controls_enabled(self, enabled: bool) -> None:
        self.tabs.setEnabled(enabled)
        self.import_result_checkbox.setEnabled(enabled)
        self.btn_paths_toggle.setEnabled(enabled)
        self.paths_panel.setEnabled(enabled)
        self.btn_log_toggle.setEnabled(enabled)

    def _on_stop(self) -> None:
        try:
            self.controller.stop()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DB_export_v3 failed", str(exc))

    def _on_run(self):
        try:
            settings = self._collect_settings()
            self.progress_strip.setVisible(True)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Starting...")
            self._schedule_adjust_height()
            self.controller.start(settings)
        except Exception as exc:
            self.progress_bar.setValue(0)
            self.progress_label.setText("Start failed")
            self.progress_strip.setVisible(False)
            self._schedule_adjust_height()
            QtWidgets.QMessageBox.critical(self, "DB_export_v3 failed", str(exc))

    def _on_run_started(self):
        self._set_run_controls_enabled(False)
        self.btn_run.setVisible(False)
        self.btn_stop.setVisible(True)
        self.progress_strip.setVisible(True)
        self.progress_bar.setValue(0)
        self.btn_log_toggle.setChecked(True)
        self._schedule_adjust_height()

    def _on_run_finished(self, ok: bool, message: str):
        self._refresh_cache_size()
        self.progress_strip.setVisible(False)
        self.btn_run.setVisible(True)
        self.btn_stop.setVisible(False)
        self._set_run_controls_enabled(True)
        self._schedule_adjust_height()
        if self._closing:
            return
        payload = self.controller.completion_payload() or {}
        summary_lines = payload.get("summaryLines") or []
        warning_lines = payload.get("warningLines") or []
        detail_text = str(payload.get("detailText") or message)

        informative_lines: list[str] = []
        if summary_lines:
            informative_lines.extend(str(line) for line in summary_lines)
        if warning_lines:
            informative_lines.append("")
            informative_lines.append("Warnings:")
            informative_lines.extend(str(line) for line in warning_lines)
        if not informative_lines:
            informative_lines.append(message)
        self._show_completion_dialog(
            ok=ok,
            status_text=str(payload.get("statusLine") or ("Done" if ok else "Run failed")),
            summary_text="\n".join(informative_lines),
            detail_text=detail_text,
        )

    def _show_completion_dialog(self, *, ok: bool, status_text: str, summary_text: str, detail_text: str) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("DB_export_v3")
        dlg.setSizeGripEnabled(True)

        screen_rect = _screen_available_geometry(self)

        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        status_label = QtWidgets.QLabel(status_text, dlg)
        status_font = status_label.font()
        status_font.setPointSize(status_font.pointSize() + 1)
        status_font.setBold(True)
        status_label.setFont(status_font)
        status_label.setStyleSheet(
            "color: {0};".format("#8fbf8f" if ok else "#d66c6c")
        )
        status_label.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        layout.addWidget(status_label)

        summary_label = QtWidgets.QLabel(summary_text, dlg)
        summary_label.setWordWrap(True)
        summary_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        summary_label.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        layout.addWidget(summary_label)

        details_container = QtWidgets.QWidget(dlg)
        details_layout = QtWidgets.QVBoxLayout(details_container)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(6)

        details_title = QtWidgets.QLabel("Details", details_container)
        details_title_font = details_title.font()
        details_title_font.setBold(True)
        details_title.setFont(details_title_font)
        details_title.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        details_layout.addWidget(details_title)

        details_edit = QtWidgets.QPlainTextEdit(details_container)
        details_edit.setReadOnly(True)
        details_edit.setPlainText(detail_text)
        details_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        details_edit.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        details_edit.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        fixed_font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        details_edit.setFont(fixed_font)
        details_edit.setMinimumHeight(260)
        details_layout.addWidget(details_edit, 1)
        details_container.hide()

        buttons = QtWidgets.QDialogButtonBox(parent=dlg)
        buttons.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        details_button = buttons.addButton("Details", QtWidgets.QDialogButtonBox.ActionRole)
        close_button = buttons.addButton(QtWidgets.QDialogButtonBox.Close)
        details_button.setCheckable(True)
        close_button.clicked.connect(dlg.accept)
        close_button.setDefault(True)
        layout.addWidget(buttons)

        summary_width = max(
            _text_block_width(status_text, status_label.font()),
            _text_block_width(summary_text, summary_label.font()),
        )
        target_width = max(420, min(int(screen_rect.width() * 0.72), summary_width + 80))
        detail_width = _text_block_width(detail_text, fixed_font)
        expanded_width = max(target_width, min(int(screen_rect.width() * 0.9), detail_width + 100))

        margins = layout.contentsMargins()
        spacing = layout.spacing()
        compact_height = (
            margins.top()
            + status_label.sizeHint().height()
            + spacing
            + summary_label.sizeHint().height()
            + spacing
            + buttons.sizeHint().height()
            + margins.bottom()
        )
        compact_height = max(110, compact_height)

        details_height = (
            details_title.sizeHint().height()
            + details_layout.spacing()
            + details_edit.minimumHeight()
        )
        expanded_height = compact_height + spacing + details_height + margins.bottom()
        expanded_height = min(int(screen_rect.height() * 0.82), max(320, expanded_height))

        def _apply_dialog_size(target_size: QtCore.QSize, *, shrink_width: bool) -> None:
            geometry = dlg.geometry()
            if shrink_width:
                new_width = target_size.width()
            else:
                new_width = max(dlg.width(), target_size.width())
            dlg.setMinimumHeight(target_size.height())
            dlg.setMaximumHeight(target_size.height())
            if geometry.width() <= 1 and geometry.height() <= 1:
                dlg.resize(new_width, target_size.height())
                return
            dlg.setGeometry(geometry.x(), geometry.y(), new_width, target_size.height())

        def _toggle_details(checked: bool) -> None:
            if checked:
                if layout.indexOf(details_container) < 0:
                    layout.insertWidget(layout.indexOf(buttons) + 1, details_container)
                details_container.show()
            else:
                if layout.indexOf(details_container) >= 0:
                    layout.removeWidget(details_container)
                details_container.hide()
            details_button.setText("Hide Details" if checked else "Details")
            target_size = QtCore.QSize(
                expanded_width if checked else target_width,
                expanded_height if checked else compact_height,
            )
            _apply_dialog_size(target_size, shrink_width=not checked)

        details_button.toggled.connect(_toggle_details)
        dlg.setMinimumWidth(target_width)
        dlg.setMinimumHeight(compact_height)
        dlg.setMaximumHeight(compact_height)
        dlg.resize(QtCore.QSize(target_width, compact_height))

        if hasattr(dlg, "exec"):
            dlg.exec()
        else:
            dlg.exec_()

    def _on_show_help(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("DB_export_v3 Help")
        dlg.resize(680, 460)
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

    def eventFilter(self, source, event):
        if source in getattr(self, "_context_tip_targets", ()):
            if event.type() == QtCore.QEvent.Enter:
                self._set_context_tip(self._SLIDER_CONTEXT_TIP)
            elif event.type() == QtCore.QEvent.Leave:
                self._clear_context_tip()
        return super().eventFilter(source, event)

    def closeEvent(self, event):
        self._closing = True
        self._save_ui_settings()
        _save_window_geometry(self)
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
