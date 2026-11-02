"""Un control de formulario por tipo de campo. Todos admiten quedar vacíos."""

from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QHBoxLayout, QLineEdit, QPlainTextEdit, QWidget,
)

_LONG_TEXT = 80


class FieldInput(QWidget):
    def __init__(self, field, value=None) -> None:
        super().__init__()
        self.field = field
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        kind = field.type
        if kind == "combo":
            self._editor = QComboBox()
            self._editor.addItem("", None)
            for option in field.options:
                self._editor.addItem(option, option)
            layout.addWidget(self._editor)
        elif kind == "bool":
            self._editor = QCheckBox()
            self._editor.setTristate(True)
            self._editor.setCheckState(Qt.CheckState.PartiallyChecked)
            layout.addWidget(self._editor)
            layout.addStretch()
        elif kind == "date":
            self._set = QCheckBox()
            self._set.setToolTip("Tiene fecha")
            self._editor = QDateEdit()
            self._editor.setCalendarPopup(True)
            self._editor.setDisplayFormat("dd/MM/yyyy")
            self._editor.setDate(QDate.currentDate())
            self._editor.setEnabled(False)
            self._set.toggled.connect(self._editor.setEnabled)
            self._raw_date: str | None = None
            layout.addWidget(self._set)
            layout.addWidget(self._editor)
            layout.addStretch()
        elif kind == "text" and isinstance(value, str) and (len(value) > _LONG_TEXT or "\n" in value):
            self._editor = QPlainTextEdit()
            self._editor.setFixedHeight(90)
            layout.addWidget(self._editor)
        else:
            self._editor = QLineEdit()
            layout.addWidget(self._editor)
        self.set_value(value)

    def set_value(self, value) -> None:
        editor = self._editor
        if isinstance(editor, QComboBox):
            if value is not None and editor.findData(value) < 0:
                # Un valor fuera de las opciones se conserva hasta que el usuario lo cambie.
                editor.addItem(str(value), value)
            editor.setCurrentIndex(editor.findData(value) if value is not None else 0)
        elif isinstance(editor, QCheckBox):
            if value is True:
                editor.setCheckState(Qt.CheckState.Checked)
            elif value is False:
                editor.setCheckState(Qt.CheckState.Unchecked)
            else:
                editor.setCheckState(Qt.CheckState.PartiallyChecked)
        elif isinstance(editor, QDateEdit):
            self._raw_date = None
            parsed = None
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError:
                    self._raw_date = value
            if parsed is not None:
                editor.setDate(QDate(parsed.year, parsed.month, parsed.day))
            self._set.setChecked(parsed is not None)
            if self._raw_date:
                self._set.setToolTip(f"Valor actual no reconocido como fecha: {self._raw_date}")
        elif isinstance(editor, QPlainTextEdit):
            editor.setPlainText("" if value is None else str(value))
        else:
            editor.setText("" if value is None else str(value))

    def value(self):
        editor = self._editor
        if isinstance(editor, QComboBox):
            return editor.currentData()
        if isinstance(editor, QCheckBox):
            state = editor.checkState()
            if state == Qt.CheckState.PartiallyChecked:
                return None
            return state == Qt.CheckState.Checked
        if isinstance(editor, QDateEdit):
            if self._set.isChecked():
                return editor.date().toString("yyyy-MM-dd")
            return self._raw_date
        if isinstance(editor, QPlainTextEdit):
            return editor.toPlainText().strip() or None
        return editor.text().strip() or None

    def set_read_only(self, reason: str) -> None:
        self.setEnabled(False)
        self.setToolTip(reason)
