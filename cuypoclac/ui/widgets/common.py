"""Piezas pequeñas compartidas por las páginas."""

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFrame, QLabel, QMessageBox, QVBoxLayout, QWidget

STATUS_LABELS = {
    "ok": "Correcto",
    "warnings": "Con advertencias",
    "error": "Error",
    "locked": "En uso",
    "pending_migration": "Migración pendiente",
    "missing": "No encontrado",
}
TYPE_LABELS = {"text": "Texto", "number": "Número", "date": "Fecha", "combo": "Lista", "bool": "Sí/No"}
CONFIDENCE_LABELS = {"alta": "Alta", "media": "Media", "manual": "Manual"}


def title_label(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPointSize(font.pointSize() + 7)
    font.setBold(True)
    label.setFont(font)
    return label


def muted_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    # Texto secundario derivado del tema, legible en modo claro y oscuro.
    palette = label.palette()
    color = palette.color(QPalette.ColorRole.WindowText)
    color.setAlphaF(0.7)
    palette.setColor(QPalette.ColorRole.WindowText, color)
    label.setPalette(palette)
    return label


class StatCard(QFrame):
    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        self._value = QLabel("—")
        font = self._value.font()
        font.setPointSize(font.pointSize() + 12)
        font.setBold(True)
        self._value.setFont(font)
        layout.addWidget(self._value)
        layout.addWidget(muted_label(caption))

    def set_value(self, value: object) -> None:
        self._value.setText(f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value))


def show_error(parent: QWidget, message: str, title: str = "No se pudo completar") -> None:
    QMessageBox.warning(parent, title, message)


def confirm(parent: QWidget, title: str, message: str, accept: str = "Continuar") -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(message)
    yes = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is yes


def format_value(value: object, type_: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or type_ == "bool":
        return "✓" if value is True else ("—" if value is False else str(value))
    if type_ == "date" and isinstance(value, str):
        return format_date(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return f"{len(value)} ítem" + ("" if len(value) == 1 else "s")
    return str(value)


def format_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d/%m/%Y" if parsed.time() == datetime.min.time() else "%d/%m/%Y %H:%M")


def format_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def align_for(type_: str | None) -> Qt.AlignmentFlag:
    if type_ == "number":
        return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

