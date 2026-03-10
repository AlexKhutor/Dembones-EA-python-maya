from __future__ import annotations

from pathlib import Path

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtCore, QtWidgets

import maya.OpenMayaUI as omui

from .models import CliRunSettings
from .paths import (
    default_cache_root,
    default_result_export_root,
    directory_size_bytes,
)
from .selection import resolve_selected_mesh_with_deformers
from .ui_controller import CliRunController
from .ui_layout import build_window_ui, help_text
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

    @staticmethod
    def _help_text() -> str:
        return help_text()

    def _build_ui(self):
        build_window_ui(self)
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
            self.progress_label.setText("Starting...")
            self.controller.start(settings)
        except Exception as exc:
            self.progress_bar.setValue(0)
            self.progress_label.setText("Start failed")
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
