from __future__ import annotations

try:
    from PySide6 import QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtWidgets

from .paths import default_cache_root, default_cli_path, default_result_export_root


def help_text() -> str:
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


def _path_row(window, line_edit: QtWidgets.QLineEdit, button_text: str, callback):
    row = QtWidgets.QWidget(window)
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


def build_window_ui(window) -> None:
    root = QtWidgets.QVBoxLayout(window)

    top_bar = QtWidgets.QHBoxLayout()
    top_bar.addStretch(1)
    window.btn_help = QtWidgets.QPushButton("Help", window)
    window.btn_help.clicked.connect(window._on_show_help)
    top_bar.addWidget(window.btn_help)
    root.addLayout(top_bar)

    sel_box = QtWidgets.QGroupBox("Source Selection (shape with deformers)", window)
    sel_layout = QtWidgets.QGridLayout(sel_box)
    window.selection_label = QtWidgets.QLineEdit(window)
    window.selection_label.setReadOnly(True)
    window.selection_label.setPlaceholderText(
        "Сначала выбери shape/transform в Outliner, затем нажми 'Use Selection'"
    )
    window.selection_label.setToolTip(
        "Ожидается один renderable mesh shape. "
        "Проверяется, что shape реально деформируется (history/inMesh)."
    )
    window.selection_info = QtWidgets.QLabel("-", window)
    window.btn_use_selection = QtWidgets.QPushButton("Use Selection", window)
    window.btn_use_selection.clicked.connect(window._on_use_selection)
    sel_layout.addWidget(window.selection_label, 0, 0, 1, 2)
    sel_layout.addWidget(window.selection_info, 1, 0, 1, 1)
    sel_layout.addWidget(window.btn_use_selection, 1, 1, 1, 1)

    cfg_box = QtWidgets.QGroupBox("CLI Settings", window)
    cfg_layout = QtWidgets.QVBoxLayout(cfg_box)
    path_form = QtWidgets.QFormLayout()

    window.cli_path_edit = QtWidgets.QLineEdit(default_cli_path(), window)
    window.cli_path_edit.setToolTip(
        "Путь к DemBones.exe.\n"
        "Если указан неверно: запуск не стартует.\n"
        "Рекомендация: использовать установленный путь из модуля DB_export."
    )
    window.cache_edit = QtWidgets.QLineEdit(default_cache_root(), window)
    window.cache_edit.setToolTip(
        "Корень кэша. Для каждого запуска создается отдельная папка run_id.\n"
        "Файлы не удаляются: rest.fbx, anim.abc, output.fbx, manifest.\n"
        "Чем чаще итерации, тем важнее быстрый локальный диск."
    )
    window.cache_edit.editingFinished.connect(window._refresh_cache_size)

    window.result_export_edit = QtWidgets.QLineEdit(default_result_export_root(), window)
    window.result_export_edit.setToolTip(
        "Папка для финального FBX результата CLI.\n"
        "После каждого запуска итоговый FBX копируется в эту папку."
    )
    window.namespace_edit = QtWidgets.QLineEdit("db_export_cli", window)
    window.namespace_edit.setToolTip(
        "Namespace для импортируемого FBX.\n"
        "Если пусто/занято: автоматически выбирается уникальный namespace.\n"
        "Влияет только на удобство структуры сцены, не на качество результата."
    )

    window.bones_spin = QtWidgets.QSpinBox(window)
    window.bones_spin.setRange(1, 1024)
    window.bones_spin.setValue(128)
    window.bones_spin.setToolTip(
        "Целевое количество костей (-b).\n"
        "Больше костей: точнее повтор формы, но тяжелее риг/медленнее solve.\n"
        "Меньше костей: быстрее и стабильнее, но больше сглаживания.\n"
        "Старт: 64-128 для простых мешей, 128-256 для сложных деформируемых мешей.\n"
        "Оптимум для большинства мешей: 128."
    )

    window.bind_update_combo = QtWidgets.QComboBox(window)
    window.bind_update_combo.addItem("Keep source hierarchy (0)", 0)
    window.bind_update_combo.addItem("Partial hierarchy update (1)", 1)
    window.bind_update_combo.addItem("Regroup joints under one root (2)", 2)
    window.bind_update_combo.setCurrentIndex(2)
    window.bind_update_combo.setToolTip(
        "Режим построения иерархии костей (--bindUpdate).\n"
        "0: минимальные изменения иерархии.\n"
        "1: частичная перестройка.\n"
        "2: одна общая root-иерархия (обычно лучший вариант для Maya).\n"
        "Рекомендуется: 2."
    )

    window.nnz_spin = QtWidgets.QSpinBox(window)
    window.nnz_spin.setRange(1, 16)
    window.nnz_spin.setValue(8)
    window.nnz_spin.setToolTip(
        "Максимум влияний на вершину (--nnz).\n"
        "Выше: точнее деформация и мягче переходы, но тяжелее skin/веса.\n"
        "Ниже: чище и легче риг, но может теряться мелкая форма.\n"
        "Рекомендуется: 8; для более жесткого результата: 6."
    )

    window.init_iters_spin = QtWidgets.QSpinBox(window)
    window.init_iters_spin.setRange(1, 500)
    window.init_iters_spin.setValue(10)
    window.init_iters_spin.setToolTip(
        "Итерации инициализации (--nInitIters).\n"
        "Влияет на стартовую раскладку костей/весов перед основным solve.\n"
        "Увеличивать при плохом старте или сложной топологии.\n"
        "Рекомендуется: 10."
    )

    window.iters_spin = QtWidgets.QSpinBox(window)
    window.iters_spin.setRange(1, 5000)
    window.iters_spin.setValue(100)
    window.iters_spin.setToolTip(
        "Основные итерации оптимизации (--nIters).\n"
        "Больше: точнее (ниже RMSE), но дольше расчёт.\n"
        "Рекомендуется: 100-150; для сложных мешей: 150-250."
    )

    window.tolerance_spin = QtWidgets.QDoubleSpinBox(window)
    window.tolerance_spin.setDecimals(6)
    window.tolerance_spin.setRange(0.000001, 1.0)
    window.tolerance_spin.setSingleStep(0.0005)
    window.tolerance_spin.setValue(0.001)
    window.tolerance_spin.setToolTip(
        "Порог остановки по сходимости (--tolerance).\n"
        "Ниже: точнее, но дольше.\n"
        "Выше: быстрее, но больше остаточная ошибка.\n"
        "Рекомендуется: 0.001; для более точного solve: 0.0005."
    )

    window.patience_spin = QtWidgets.QSpinBox(window)
    window.patience_spin.setRange(1, 100)
    window.patience_spin.setValue(3)
    window.patience_spin.setToolTip(
        "Ранняя остановка: сколько итераций ждать улучшения (--patience).\n"
        "Выше: стабильнее на шумных данных, но дольше.\n"
        "Ниже: быстрее, но риск остановиться рано.\n"
        "Рекомендуется: 3-5."
    )

    window.frame_start = QtWidgets.QSpinBox(window)
    window.frame_start.setRange(-100000, 100000)
    window.frame_start.setValue(1)
    window.frame_start.setToolTip(
        "Начальный кадр диапазона.\n"
        "Влияет на то, какие ключи войдут в solve."
    )
    window.frame_end = QtWidgets.QSpinBox(window)
    window.frame_end.setRange(-100000, 100000)
    window.frame_end.setValue(60)
    window.frame_end.setToolTip(
        "Конечный кадр диапазона.\n"
        "Чем длиннее диапазон, тем дольше solve."
    )
    window.frame_step = QtWidgets.QSpinBox(window)
    window.frame_step.setRange(1, 1000)
    window.frame_step.setValue(1)
    window.frame_step.setToolTip(
        "Шаг семплирования кадров.\n"
        "1 = максимальная точность.\n"
        "2+ ускоряет, но может пропускать быстрые движения."
    )

    window.debug_cli_checkbox = QtWidgets.QCheckBox("Verbose CLI Log", window)
    window.debug_cli_checkbox.setChecked(True)
    window.debug_cli_checkbox.setToolTip(
        "Показывать полный stdout/stderr CLI в логе.\n"
        "Полезно для диагностики RMSE, итераций и ошибок импорта."
    )

    window.import_result_checkbox = QtWidgets.QCheckBox("Import Result Into Scene", window)
    window.import_result_checkbox.setChecked(True)
    window.import_result_checkbox.setToolTip(
        "Если включено: итоговый FBX после CLI автоматически импортируется в текущую сцену Maya.\n"
        "Если выключено: только сохраняется файл в Result FBX Folder."
    )

    path_form.addRow("CLI Executable", _path_row(window, window.cli_path_edit, "Browse...", window._on_browse_cli))

    cache_row = QtWidgets.QWidget(window)
    cache_row_layout = QtWidgets.QHBoxLayout(cache_row)
    cache_row_layout.setContentsMargins(0, 0, 0, 0)
    cache_row_layout.addWidget(window.cache_edit, 1)
    window.btn_browse_cache = QtWidgets.QPushButton("Browse...", cache_row)
    window.btn_browse_cache.clicked.connect(window._on_browse_cache)
    cache_row_layout.addWidget(window.btn_browse_cache, 0)
    window.cache_size_label = QtWidgets.QLabel("Cache usage: - MB", cache_row)
    cache_row_layout.addWidget(window.cache_size_label, 0)
    path_form.addRow("Cache Root", cache_row)

    path_form.addRow(
        "Result FBX Folder",
        _path_row(window, window.result_export_edit, "Browse...", window._on_browse_result_export),
    )
    path_form.addRow("Import Namespace", window.namespace_edit)
    cfg_layout.addLayout(path_form)

    tabs = QtWidgets.QTabWidget(window)
    main_tab = QtWidgets.QWidget(window)
    main_form = QtWidgets.QFormLayout(main_tab)
    main_form.addRow("Target Bone Count", window.bones_spin)
    main_form.addRow("Hierarchy Build Mode", window.bind_update_combo)
    main_form.addRow("Max Influences Per Vertex", window.nnz_spin)
    main_form.addRow("Frame Start", window.frame_start)
    main_form.addRow("Frame End", window.frame_end)
    main_form.addRow("Frame Step", window.frame_step)
    main_form.addRow(window.import_result_checkbox)
    tabs.addTab(main_tab, "Main")

    adv_tab = QtWidgets.QWidget(window)
    adv_form = QtWidgets.QFormLayout(adv_tab)
    adv_form.addRow("Initialization Iterations", window.init_iters_spin)
    adv_form.addRow("Optimization Iterations", window.iters_spin)
    adv_form.addRow("Convergence Threshold", window.tolerance_spin)
    adv_form.addRow("Early Stop Patience", window.patience_spin)
    adv_form.addRow(window.debug_cli_checkbox)
    tabs.addTab(adv_tab, "Advanced")

    cfg_layout.addWidget(tabs)

    actions = QtWidgets.QHBoxLayout()
    window.btn_run = QtWidgets.QPushButton("Run CLI Export", window)
    window.btn_run.clicked.connect(window._on_run)
    actions.addWidget(window.btn_run)
    actions.addStretch(1)

    progress_box = QtWidgets.QGroupBox("Progress", window)
    progress_layout = QtWidgets.QVBoxLayout(progress_box)
    window.progress_bar = QtWidgets.QProgressBar(window)
    window.progress_bar.setRange(0, 100)
    window.progress_bar.setValue(0)
    window.progress_label = QtWidgets.QLabel("Idle", window)
    progress_layout.addWidget(window.progress_bar)
    progress_layout.addWidget(window.progress_label)

    log_box = QtWidgets.QGroupBox("Run Log", window)
    log_layout = QtWidgets.QVBoxLayout(log_box)
    window.log_edit = QtWidgets.QPlainTextEdit(window)
    window.log_edit.setReadOnly(True)
    log_layout.addWidget(window.log_edit)

    root.addWidget(sel_box)
    root.addWidget(cfg_box)
    root.addLayout(actions)
    root.addWidget(progress_box)
    root.addWidget(log_box)
