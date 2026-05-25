from __future__ import annotations

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtCore, QtGui, QtWidgets

from .widgets.option_slider_widget import OptionSliderWidget
from .text import (
    FIXED_TIP_TEXT,
    FIXED_VARIANT_LABEL_OPTIMIZE,
    FIXED_VARIANT_LABEL_TRANSFORMS,
    FIXED_VARIANT_LABEL_WEIGHTS,
    SECTION_LABEL_LOG,
    SECTION_LABEL_PATHS,
    TAB_LABEL_EXISTING,
    TAB_LABEL_GENERATE,
    TAB_LABEL_SOLVER,
    fixed_variant_note,
    help_text,
)
from ..maya.paths import default_cache_root, default_cli_path, default_result_export_root


_FORM_LABELS = (
    "Target Bone Count",
    "Max Influences Per Vertex",
    "Frame Range",
    "FBX Name",
    "Clip Prefix",
    "Fixed Solve Variant",
    "Source Animated Mesh",
    "Source Alembic",
    "Bound Init Mesh",
    "Fixed Hierarchy Root",
    "Hierarchy Build Mode",
    "Initialization Iterations",
    "Optimization Iterations",
    "Convergence Threshold",
    "Early Stop Patience",
)
def _bind_disclosure(button: QtWidgets.QPushButton, panel: QtWidgets.QWidget, title: str) -> None:
    def _update(checked: bool) -> None:
        panel.setVisible(checked)
        button.setText(("v" if checked else ">") + "  " + title)

    button.toggled.connect(_update)
    _update(bool(button.isChecked()))


def _path_row(window, line_edit: QtWidgets.QLineEdit, callback, button_text: str = "Browse..."):
    row = QtWidgets.QWidget(window)
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(line_edit, 1)
    button = QtWidgets.QPushButton(button_text, row)
    button.clicked.connect(callback)
    layout.addWidget(button, 0)
    return row, button


def _style_section_toggle(button: QtWidgets.QPushButton) -> None:
    button.setFlat(False)
    button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
    button.setStyleSheet(
        """
        QPushButton {
            text-align: left;
            padding: 6px 8px;
            border: 1px solid #3b3b3b;
            border-radius: 4px;
            background-color: #202020;
        }
        QPushButton:hover {
            background-color: #282828;
        }
        QPushButton:pressed,
        QPushButton:checked {
            background-color: #202020;
        }
        """
    )


def _label_column_width(widget: QtWidgets.QWidget) -> int:
    metrics = widget.fontMetrics()
    return max(metrics.horizontalAdvance(text) for text in _FORM_LABELS) + 16


def build_window_ui(window) -> None:
    root = QtWidgets.QVBoxLayout(window)
    root.setContentsMargins(10, 10, 10, 10)
    root.setSpacing(8)
    root.setAlignment(QtCore.Qt.AlignTop)

    window.menu_bar = QtWidgets.QMenuBar(window)
    window.menu_bar.setNativeMenuBar(False)
    window.help_action = window.menu_bar.addAction("Help")
    window.help_action.triggered.connect(window._on_show_help)
    root.addWidget(window.menu_bar)

    window.progress_strip = QtWidgets.QWidget(window)
    progress_layout = QtWidgets.QHBoxLayout(window.progress_strip)
    progress_layout.setContentsMargins(0, 0, 0, 0)
    progress_layout.setSpacing(8)
    window.progress_bar = QtWidgets.QProgressBar(window.progress_strip)
    window.progress_bar.setRange(0, 100)
    window.progress_bar.setValue(0)
    window.progress_label = QtWidgets.QLabel("Idle", window.progress_strip)
    progress_layout.addWidget(window.progress_bar, 1)
    progress_layout.addWidget(window.progress_label, 0)
    window.progress_strip.setVisible(False)
    root.addWidget(window.progress_strip)

    window.cli_path_edit = QtWidgets.QLineEdit(default_cli_path(), window)
    window.cli_path_edit.setToolTip(
        "Path to DemBones.exe.\n"
        "If the path is wrong, the run will not start."
    )
    window.cache_edit = QtWidgets.QLineEdit(default_cache_root(), window)
    window.cache_edit.setToolTip(
        "Cache root. A separate run_id folder is created for each run.\n"
        "Files are not deleted automatically."
    )
    window.cache_edit.editingFinished.connect(window._refresh_cache_size)
    window.result_export_edit = QtWidgets.QLineEdit(default_result_export_root(), window)
    window.result_export_edit.setToolTip(
        "Folder for the final deliverable FBX."
    )
    window.fbx_name_edit = QtWidgets.QLineEdit("", window)
    window.fbx_name_edit.setPlaceholderText("Optional. Uses the selected mesh shape name if empty.")
    window.fbx_name_edit.setToolTip(
        "Optional base name for the deliverable FBX.\n"
        "If empty, the selected mesh shape name is used.\n"
        "This base name is also used as the prefix for joints and exported mesh transforms inside the final FBX."
    )
    window.clip_prefix_edit = QtWidgets.QLineEdit("", window)
    window.clip_prefix_edit.setPlaceholderText("Optional file-only prefix, for example shot02_")
    window.clip_prefix_edit.setToolTip(
        "Optional file-only prefix added before FBX Name.\n"
        "This does not affect joint or mesh names inside the final FBX."
    )
    window.namespace_edit = QtWidgets.QLineEdit("db_export_v3_cli", window)
    window.namespace_edit.setToolTip(
        "Namespace for importing the deliverable FBX back into Maya.\n"
        "If empty, import goes to the root namespace."
    )

    window.bones_spin = OptionSliderWidget(parent=window)
    window.bones_spin.setRange(1, 1024)
    window.bones_spin.setValue(128)

    window.bind_update_combo = QtWidgets.QComboBox(window)
    window.bind_update_combo.addItem("Keep source hierarchy (0)", 0)
    window.bind_update_combo.addItem("Partial hierarchy update (1)", 1)
    window.bind_update_combo.addItem("Regroup joints under one root (2)", 2)
    window.bind_update_combo.setCurrentIndex(2)

    window.nnz_spin = OptionSliderWidget(parent=window)
    window.nnz_spin.setRange(1, 16)
    window.nnz_spin.setValue(8)

    window.init_iters_spin = OptionSliderWidget(parent=window)
    window.init_iters_spin.setRange(1, 500)
    window.init_iters_spin.setValue(10)

    window.iters_spin = OptionSliderWidget(parent=window)
    window.iters_spin.setRange(1, 5000)
    window.iters_spin.setValue(100)

    window.tolerance_spin = QtWidgets.QDoubleSpinBox(window)
    window.tolerance_spin.setDecimals(6)
    window.tolerance_spin.setRange(0.000001, 1.0)
    window.tolerance_spin.setSingleStep(0.0005)
    window.tolerance_spin.setValue(0.001)

    window.patience_spin = OptionSliderWidget(parent=window)
    window.patience_spin.setRange(1, 100)
    window.patience_spin.setValue(3)

    window.frame_start = OptionSliderWidget(parent=window)
    window.frame_start.setRange(-100000, 100000)
    window.frame_start.setValue(1)
    window.frame_end = OptionSliderWidget(parent=window)
    window.frame_end.setRange(-100000, 100000)
    window.frame_end.setValue(60)
    window.frame_step = OptionSliderWidget(parent=window)
    window.frame_step.setRange(1, 1000)
    window.frame_step.setValue(1)

    window.fixed_fbx_name_edit = QtWidgets.QLineEdit("", window)
    window.fixed_fbx_name_edit.setPlaceholderText("Optional. Uses the selected mesh shape name if empty.")
    window.fixed_fbx_name_edit.setToolTip(
        "Optional base name for the deliverable FBX in fixed-bones mode.\n"
        "If empty, the selected mesh shape name is used.\n"
        "This base name is also used as the prefix for joints and exported mesh transforms inside the final FBX."
    )
    window.fixed_clip_prefix_edit = QtWidgets.QLineEdit("", window)
    window.fixed_clip_prefix_edit.setPlaceholderText("Optional file-only prefix, for example shot02_")
    window.fixed_clip_prefix_edit.setToolTip(
        "Optional file-only prefix added before FBX Name in fixed-bones mode.\n"
        "This does not affect joint or mesh names inside the final FBX."
    )
    window.fixed_source_animated_mesh_edit = QtWidgets.QLineEdit("", window)
    window.fixed_source_animated_mesh_edit.setPlaceholderText("Alembic-driven cloth mesh from scene")
    window.fixed_source_animated_mesh_edit.setToolTip(
        "Mesh driven by simulated cloth / Alembic animation.\n"
        "This is exported to Alembic and used as the animated source for DemBones."
    )
    window.fixed_bound_init_mesh_edit = QtWidgets.QLineEdit("", window)
    window.fixed_bound_init_mesh_edit.setPlaceholderText("Skinned mesh bound to the target hierarchy")
    window.fixed_bound_init_mesh_edit.setToolTip(
        "Mesh already skinned to the hierarchy that we want to preserve.\n"
        "This mesh and the selected hierarchy are exported together into the init FBX."
    )
    window.fixed_hierarchy_root_edit = QtWidgets.QLineEdit("", window)
    window.fixed_hierarchy_root_edit.setPlaceholderText("Root joint from scene")
    window.fixed_hierarchy_root_edit.setToolTip(
        "Root joint for the fixed hierarchy.\n"
        "This should match the hierarchy used by Bound Init Mesh."
    )
    window.fixed_alembic_source_edit = QtWidgets.QLineEdit("", window)
    window.fixed_alembic_source_edit.setReadOnly(True)
    window.fixed_alembic_source_edit.setPlaceholderText("Resolved AlembicNode from Source Animated Mesh")
    window.fixed_alembic_source_edit.setToolTip(
        "Read-only Alembic source reference resolved from Source Animated Mesh history."
    )
    window.fixed_variant_combo = QtWidgets.QComboBox(window)
    window.fixed_variant_combo.addItem(FIXED_VARIANT_LABEL_OPTIMIZE, "optimize_existing")
    window.fixed_variant_combo.addItem(FIXED_VARIANT_LABEL_WEIGHTS, "weights_only")
    window.fixed_variant_combo.addItem(FIXED_VARIANT_LABEL_TRANSFORMS, "transforms_only")
    window.fixed_variant_combo.setToolTip(
        "Choose the DemBones solve mode for the existing hierarchy.\n"
        "Optimize Existing is the primary fixed-skeleton path.\n"
        "Transforms Only is diagnostic and can produce unstable child-joint translate solves."
    )
    window.fixed_bind_update_combo = QtWidgets.QComboBox(window)
    window.fixed_bind_update_combo.addItem("Keep source hierarchy (0)", 0)
    window.fixed_bind_update_combo.addItem("Partial hierarchy update (1)", 1)
    window.fixed_bind_update_combo.addItem("Regroup joints under one root (2)", 2)
    window.fixed_bind_update_combo.setCurrentIndex(0)
    window.fixed_frame_start = OptionSliderWidget(parent=window)
    window.fixed_frame_start.setRange(-100000, 100000)
    window.fixed_frame_start.setValue(1)
    window.fixed_frame_end = OptionSliderWidget(parent=window)
    window.fixed_frame_end.setRange(-100000, 100000)
    window.fixed_frame_end.setValue(60)
    window.fixed_frame_step = OptionSliderWidget(parent=window)
    window.fixed_frame_step.setRange(1, 1000)
    window.fixed_frame_step.setValue(1)

    window.debug_cli_checkbox = QtWidgets.QCheckBox("Verbose CLI Log", window)
    window.debug_cli_checkbox.setChecked(True)
    window.debug_cli_checkbox.setToolTip(
        "Show full CLI stdout/stderr in the in-window run log."
    )

    window.write_debug_logs_checkbox = QtWidgets.QCheckBox("Write Debug Logs To Disk", window)
    window.write_debug_logs_checkbox.setChecked(False)
    window.write_debug_logs_checkbox.setToolTip(
        "Write structured debug logs and Maya FBX snapshots into the Logs folder."
    )

    window.import_result_checkbox = QtWidgets.QCheckBox("Import Result Into Scene", window)
    window.import_result_checkbox.setChecked(True)

    window.tabs = QtWidgets.QTabWidget(window)
    window.tabs.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)

    label_width = _label_column_width(window)

    window.main_tab = QtWidgets.QWidget(window.tabs)
    main_layout = QtWidgets.QVBoxLayout(window.main_tab)
    main_layout.setContentsMargins(0, 0, 0, 0)
    main_layout.setSpacing(8)

    main_form = QtWidgets.QFormLayout()
    main_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    main_form.addRow("Target Bone Count", window.bones_spin)
    main_form.addRow("Max Influences Per Vertex", window.nnz_spin)

    frame_range_row = QtWidgets.QWidget(window.main_tab)
    frame_range_layout = QtWidgets.QHBoxLayout(frame_range_row)
    frame_range_layout.setContentsMargins(0, 0, 0, 0)
    frame_range_layout.setSpacing(6)
    frame_range_layout.addWidget(window.frame_start, 1)
    frame_range_layout.addWidget(QtWidgets.QLabel("-", frame_range_row), 0)
    frame_range_layout.addWidget(window.frame_end, 1)
    frame_range_layout.addWidget(QtWidgets.QLabel("step", frame_range_row), 0)
    frame_range_layout.addWidget(window.frame_step, 1)
    main_form.addRow("Frame Range", frame_range_row)
    main_form.addRow("FBX Name", window.fbx_name_edit)
    main_form.addRow("Clip Prefix", window.clip_prefix_edit)
    main_form.addRow("Hierarchy Build Mode", window.bind_update_combo)
    main_layout.addLayout(main_form)

    window.context_tip_panel = QtWidgets.QWidget(window.main_tab)
    tip_layout = QtWidgets.QHBoxLayout(window.context_tip_panel)
    tip_layout.setContentsMargins(6, 0, 0, 0)
    tip_layout.setSpacing(0)
    window.context_tip_label = QtWidgets.QLabel("", window.main_tab)
    window.context_tip_label.setWordWrap(True)
    window.context_tip_label.setMinimumHeight(34)
    window.context_tip_label.setStyleSheet("color: #9a9a9a;")
    tip_layout.addWidget(window.context_tip_label, 1)
    main_layout.addWidget(window.context_tip_panel)
    window.tabs.addTab(window.main_tab, TAB_LABEL_GENERATE)

    window.fixed_tab = QtWidgets.QWidget(window.tabs)
    fixed_layout = QtWidgets.QVBoxLayout(window.fixed_tab)
    fixed_layout.setContentsMargins(0, 0, 0, 0)
    fixed_layout.setSpacing(8)

    fixed_form = QtWidgets.QFormLayout()
    fixed_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

    fixed_frame_range_row = QtWidgets.QWidget(window.fixed_tab)
    fixed_frame_range_layout = QtWidgets.QHBoxLayout(fixed_frame_range_row)
    fixed_frame_range_layout.setContentsMargins(0, 0, 0, 0)
    fixed_frame_range_layout.setSpacing(6)
    fixed_frame_range_layout.addWidget(window.fixed_frame_start, 1)
    fixed_frame_range_layout.addWidget(QtWidgets.QLabel("-", fixed_frame_range_row), 0)
    fixed_frame_range_layout.addWidget(window.fixed_frame_end, 1)
    fixed_frame_range_layout.addWidget(QtWidgets.QLabel("step", fixed_frame_range_row), 0)
    fixed_frame_range_layout.addWidget(window.fixed_frame_step, 1)

    fixed_source_mesh_row, window.btn_fixed_pick_source_mesh = _path_row(
        window.fixed_tab,
        window.fixed_source_animated_mesh_edit,
        window._on_use_selected_fixed_source_mesh,
        "Use Selected Mesh",
    )
    fixed_bound_mesh_row, window.btn_fixed_pick_bound_mesh = _path_row(
        window.fixed_tab,
        window.fixed_bound_init_mesh_edit,
        window._on_use_selected_fixed_bound_mesh,
        "Use Selected Mesh",
    )
    fixed_root_row, window.btn_fixed_pick_hierarchy = _path_row(
        window.fixed_tab,
        window.fixed_hierarchy_root_edit,
        window._on_use_selected_fixed_hierarchy,
        "Use Selected Joint",
    )
    fixed_alembic_row, window.btn_fixed_refresh_alembic = _path_row(
        window.fixed_tab,
        window.fixed_alembic_source_edit,
        window._on_refresh_fixed_alembic_from_source_mesh,
        "Refresh",
    )

    fixed_form.addRow("Frame Range", fixed_frame_range_row)
    fixed_form.addRow("FBX Name", window.fixed_fbx_name_edit)
    fixed_form.addRow("Clip Prefix", window.fixed_clip_prefix_edit)
    fixed_form.addRow("Fixed Solve Variant", window.fixed_variant_combo)
    fixed_form.addRow("Source Animated Mesh", fixed_source_mesh_row)
    fixed_form.addRow("Source Alembic", fixed_alembic_row)
    fixed_form.addRow("Bound Init Mesh", fixed_bound_mesh_row)
    fixed_form.addRow("Fixed Hierarchy Root", fixed_root_row)
    fixed_form.addRow("Hierarchy Build Mode", window.fixed_bind_update_combo)
    fixed_layout.addLayout(fixed_form)

    window.fixed_variant_note_label = QtWidgets.QLabel(
        fixed_variant_note("optimize_existing"),
        window.fixed_tab,
    )
    window.fixed_variant_note_label.setWordWrap(True)
    window.fixed_variant_note_label.setStyleSheet("color: #b0b0b0;")
    fixed_layout.addWidget(window.fixed_variant_note_label)

    window.fixed_tip_label = QtWidgets.QLabel(FIXED_TIP_TEXT, window.fixed_tab)
    window.fixed_tip_label.setWordWrap(True)
    window.fixed_tip_label.setStyleSheet("color: #9a9a9a;")
    fixed_layout.addWidget(window.fixed_tip_label)
    window.tabs.addTab(window.fixed_tab, TAB_LABEL_EXISTING)

    window.advanced_tab = QtWidgets.QWidget(window.tabs)
    advanced_form = QtWidgets.QFormLayout(window.advanced_tab)
    advanced_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    advanced_form.addRow("Initialization Iterations", window.init_iters_spin)
    advanced_form.addRow("Optimization Iterations", window.iters_spin)
    advanced_form.addRow("Convergence Threshold", window.tolerance_spin)
    advanced_form.addRow("Early Stop Patience", window.patience_spin)
    window.tabs.addTab(window.advanced_tab, TAB_LABEL_SOLVER)

    root.addWidget(window.tabs)

    window.btn_stop = QtWidgets.QPushButton("Stop", window)
    window.btn_stop.setVisible(False)
    window.btn_stop.setFixedWidth(label_width)
    window.btn_stop.clicked.connect(window._on_stop)

    window.btn_run = QtWidgets.QPushButton("Run", window)
    window.btn_run.setFixedWidth(label_width)
    window.btn_run.clicked.connect(window._on_run)

    run_row = QtWidgets.QWidget(window)
    run_layout = QtWidgets.QHBoxLayout(run_row)
    run_layout.setContentsMargins(0, 0, 0, 0)
    run_layout.setSpacing(8)
    run_layout.addWidget(window.import_result_checkbox, 0)
    run_layout.addStretch(1)
    run_layout.addWidget(window.btn_stop, 0)
    run_layout.addWidget(window.btn_run, 0)
    root.addWidget(run_row)

    window.btn_paths_toggle = QtWidgets.QPushButton(">  " + SECTION_LABEL_PATHS, window)
    window.btn_paths_toggle.setCheckable(True)
    window.btn_paths_toggle.setChecked(False)
    _style_section_toggle(window.btn_paths_toggle)
    root.addWidget(window.btn_paths_toggle)

    window.paths_panel = QtWidgets.QWidget(window)
    paths_layout = QtWidgets.QVBoxLayout(window.paths_panel)
    paths_layout.setContentsMargins(0, 0, 0, 0)
    paths_layout.setSpacing(6)

    paths_form = QtWidgets.QFormLayout()
    paths_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

    cli_row, window.btn_browse_cli = _path_row(window.paths_panel, window.cli_path_edit, window._on_browse_cli)
    cache_row, window.btn_browse_cache = _path_row(window.paths_panel, window.cache_edit, window._on_browse_cache)
    result_row, window.btn_browse_result_export = _path_row(
        window.paths_panel,
        window.result_export_edit,
        window._on_browse_result_export,
    )
    paths_form.addRow("CLI Executable", cli_row)
    paths_form.addRow("Cache Root", cache_row)
    paths_form.addRow("Result Folder", result_row)
    paths_form.addRow("Namespace", window.namespace_edit)
    paths_layout.addLayout(paths_form)
    paths_layout.addWidget(window.debug_cli_checkbox)
    paths_layout.addWidget(window.write_debug_logs_checkbox)
    window.cache_size_label = QtWidgets.QLabel("Cache usage: - MB", window.paths_panel)
    paths_layout.addWidget(window.cache_size_label)
    window.paths_panel.setVisible(False)
    root.addWidget(window.paths_panel)
    _bind_disclosure(window.btn_paths_toggle, window.paths_panel, SECTION_LABEL_PATHS)

    window.btn_log_toggle = QtWidgets.QPushButton(">  " + SECTION_LABEL_LOG, window)
    window.btn_log_toggle.setCheckable(True)
    window.btn_log_toggle.setChecked(False)
    _style_section_toggle(window.btn_log_toggle)
    root.addWidget(window.btn_log_toggle)

    window.log_panel = QtWidgets.QWidget(window)
    log_layout = QtWidgets.QVBoxLayout(window.log_panel)
    log_layout.setContentsMargins(0, 0, 0, 0)
    window.log_edit = QtWidgets.QPlainTextEdit(window.log_panel)
    window.log_edit.setReadOnly(True)
    log_layout.addWidget(window.log_edit)
    window.log_panel.setVisible(False)
    root.addWidget(window.log_panel)
    _bind_disclosure(window.btn_log_toggle, window.log_panel, SECTION_LABEL_LOG)

    root.addStretch(1)
