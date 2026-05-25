from __future__ import annotations

from decimal import Decimal

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtCore, QtGui, QtWidgets


_BASE_STYLESHEET = """
QWidget#dbOptionSlider {
    background-color: #2f2f2f;
    border: 1px solid #4a4a4a;
    border-radius: 3px;
}
QWidget#dbOptionSlider:hover {
    border-color: #6b6b6b;
}
QToolButton {
    background-color: #3a3a3a;
    border: 0;
    min-width: 14px;
    max-width: 14px;
    color: #d8d8d8;
}
QToolButton:hover {
    background-color: #4a4a4a;
}
QLineEdit {
    background: transparent;
    border: 0;
    padding: 0 4px;
    color: #e6e6e6;
}
QLabel {
    background: transparent;
    color: #e6e6e6;
}
"""


class _OutsideClickFilter(QtCore.QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def eventFilter(self, _obj, event):
        if event.type() == QtCore.QEvent.MouseButtonPress:
            owner = self._owner
            if owner is None or not owner.isVisible():
                return False
            if owner.is_editing() and not owner._contains_global(event.globalPos()):
                owner.finish_editing()
        return False


class _SliderToolButton(QtWidgets.QToolButton):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCursor(QtGui.QCursor(QtCore.Qt.ArrowCursor))


class _SliderLabel(QtWidgets.QLabel):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setAlignment(QtCore.Qt.AlignCenter)

    def enterEvent(self, event):
        self.setCursor(QtGui.QCursor(QtCore.Qt.SizeHorCursor))
        parent = self.parentWidget()
        if parent is not None:
            parent._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        parent = self.parentWidget()
        if parent is not None:
            parent._set_hovered(False)
        super().leaveEvent(event)


class OptionSliderWidget(QtWidgets.QWidget):
    valueChanged = QtCore.Signal(float)

    _DRAG_PIXELS_PER_STEP = 8.0
    _DRAG_PIXELS_PER_STEP_FAST = 3.0
    _DRAG_PIXELS_PER_STEP_SLOW = 24.0

    def __init__(self, minimum=0, maximum=100, step=1, value=0, decimals=0, parent=None):
        super().__init__(parent)
        self.setObjectName("dbOptionSlider")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setStyleSheet(_BASE_STYLESHEET)
        self.setMouseTracking(True)
        self.setFixedHeight(22)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._step = float(step)
        self._decimals = max(0, int(decimals))
        self._hovered = False
        self._is_dragging = False
        self._is_editing = False
        self._drag_start_pos = QtCore.QPoint()
        self._drag_start_value = 0.0
        self._value = self._normalize_value(value)

        self.left_button = _SliderToolButton("<", self)
        self.right_button = _SliderToolButton(">", self)
        self.value_label = _SliderLabel("", self)
        self.line_edit = QtWidgets.QLineEdit(self)
        self.line_edit.setAlignment(QtCore.Qt.AlignCenter)
        self.line_edit.setVisible(False)

        locale = QtCore.QLocale(QtCore.QLocale.English, QtCore.QLocale.UnitedStates)
        validator = QtGui.QDoubleValidator(self._minimum, self._maximum, self._decimals, locale=locale)
        validator.setNotation(QtGui.QDoubleValidator.StandardNotation)
        self.line_edit.setValidator(validator)
        self.line_edit.installEventFilter(self)
        self.line_edit.editingFinished.connect(self.finish_editing)

        self.left_button.clicked.connect(lambda: self._apply_delta(-self._step))
        self.right_button.clicked.connect(lambda: self._apply_delta(self._step))

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.left_button, 0)
        layout.addWidget(self.value_label, 1)
        layout.addWidget(self.line_edit, 1)
        layout.addWidget(self.right_button, 0)

        self._outside_click_filter = _OutsideClickFilter(self)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self._outside_click_filter)

        self._set_buttons_visible(False)
        self._sync_value_widgets()

    def _precision_quant(self) -> Decimal:
        if self._decimals <= 0:
            return Decimal("1")
        return Decimal("1." + ("0" * self._decimals))

    def _normalize_value(self, value: float) -> float:
        clamped = max(self._minimum, min(self._maximum, float(value)))
        quantized = Decimal(str(clamped)).quantize(self._precision_quant())
        return float(quantized)

    def _format_value(self, value: float) -> str:
        if self._decimals <= 0:
            return str(int(round(value)))
        return f"{float(value):.{self._decimals}f}"

    def _sync_value_widgets(self) -> None:
        text = self._format_value(self._value)
        self.value_label.setText(text)
        self.line_edit.setText(text)
        self.update()

    def _set_buttons_visible(self, visible: bool) -> None:
        self.left_button.setVisible(visible)
        self.right_button.setVisible(visible)

    def _set_hovered(self, hovered: bool) -> None:
        if self._hovered != bool(hovered):
            self._hovered = bool(hovered)
            self.update()

    def _contains_global(self, global_pos) -> bool:
        return self.rect().contains(self.mapFromGlobal(global_pos))

    def _apply_delta(self, delta: float) -> None:
        self.setValue(self._value + delta)

    def _drag_pixels_per_step(self, modifiers) -> float:
        if modifiers & QtCore.Qt.ShiftModifier and modifiers & QtCore.Qt.ControlModifier:
            return self._DRAG_PIXELS_PER_STEP
        if modifiers & QtCore.Qt.ShiftModifier:
            return self._DRAG_PIXELS_PER_STEP_SLOW
        if modifiers & QtCore.Qt.ControlModifier:
            return self._DRAG_PIXELS_PER_STEP_FAST
        return self._DRAG_PIXELS_PER_STEP

    def _wheel_step_multiplier(self, modifiers) -> float:
        if modifiers & QtCore.Qt.ShiftModifier and modifiers & QtCore.Qt.ControlModifier:
            return 20.0
        if modifiers & QtCore.Qt.ShiftModifier:
            return 10.0
        if modifiers & QtCore.Qt.ControlModifier:
            return 5.0
        return 1.0

    def _drag_step_count(self, pixel_delta: float, modifiers) -> int:
        pixels_per_step = max(1.0, self._drag_pixels_per_step(modifiers))
        return int(pixel_delta / pixels_per_step)

    def is_editing(self) -> bool:
        return self._is_editing

    def setRange(self, minimum, maximum) -> None:
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self.setValue(self._value)
        locale = QtCore.QLocale(QtCore.QLocale.English, QtCore.QLocale.UnitedStates)
        validator = QtGui.QDoubleValidator(self._minimum, self._maximum, self._decimals, locale=locale)
        validator.setNotation(QtGui.QDoubleValidator.StandardNotation)
        self.line_edit.setValidator(validator)

    def setSingleStep(self, step) -> None:
        self._step = float(step)

    def setDecimals(self, decimals) -> None:
        self._decimals = max(0, int(decimals))
        self.setValue(self._value)

    def setValue(self, value) -> None:
        normalized = self._normalize_value(value)
        if normalized == self._value:
            self._sync_value_widgets()
            return
        self._value = normalized
        self._sync_value_widgets()
        self.valueChanged.emit(float(self._value))

    def value(self):
        if self._decimals <= 0:
            return int(round(self._value))
        return float(self._value)

    def toggle_line_edit(self, visible: bool) -> None:
        self._is_editing = bool(visible)
        self.line_edit.setVisible(visible)
        self.value_label.setVisible(not visible)
        self._set_buttons_visible(not visible)
        if visible:
            self.line_edit.selectAll()
            self.line_edit.setFocus()

    def finish_editing(self) -> None:
        if not self._is_editing:
            return
        text = self.line_edit.text().strip()
        value = self._value
        if text:
            try:
                value = float(text)
            except ValueError:
                value = self._value
        self.setValue(value)
        self.toggle_line_edit(False)
        self._set_buttons_visible(False)

    def cleanup(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is not None and self._outside_click_filter is not None:
            app.removeEventFilter(self._outside_click_filter)
        self._outside_click_filter = None

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)

    def eventFilter(self, source, event):
        if source is self.line_edit and event.type() == QtCore.QEvent.KeyPress:
            if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                self.finish_editing()
                return True
        return super().eventFilter(source, event)

    def enterEvent(self, event):
        if not self._is_editing:
            self._set_buttons_visible(True)
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._is_editing:
            self._set_buttons_visible(False)
        self._set_hovered(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._is_dragging = False
        self._drag_start_pos = event.pos()
        self._drag_start_value = self._value
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.LeftButton and not self._is_editing:
            pixel_delta = event.pos().x() - self._drag_start_pos.x()
            if not self._is_dragging and abs(pixel_delta) < QtWidgets.QApplication.startDragDistance():
                super().mouseMoveEvent(event)
                return
            self._is_dragging = True
            step_count = self._drag_step_count(pixel_delta, event.modifiers())
            self.setValue(self._drag_start_value + (step_count * self._step))
            self.setCursor(QtGui.QCursor(QtCore.Qt.SizeHorCursor))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if not self._is_dragging and self.value_label.underMouse() and not self._is_editing:
            self.toggle_line_edit(True)
        self._is_dragging = False
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._is_editing:
            super().wheelEvent(event)
            return
        angle_delta = event.angleDelta().y()
        if angle_delta == 0:
            super().wheelEvent(event)
            return
        step_count = angle_delta / 120.0
        delta = step_count * self._step * self._wheel_step_multiplier(event.modifiers())
        self._apply_delta(delta)
        event.accept()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        option = QtWidgets.QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(QtWidgets.QStyle.PE_Widget, option, painter, self)

        span = self._maximum - self._minimum
        if span <= 0:
            return
        ratio = (self._value - self._minimum) / span
        ratio = max(0.0, min(1.0, ratio))
        if ratio <= 0.0:
            return

        fill_rect = QtCore.QRect(self.rect())
        fill_rect.setWidth(int(fill_rect.width() * ratio))
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor("#5a5a5a" if self._hovered else "#4a4a4a"))
        painter.drawRect(fill_rect)
