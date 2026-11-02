"""Log: lo que la app abrió y los errores, para diagnosticar sin consola."""

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ... import applog
from ..widgets.common import muted_label, title_label


class LogPage(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(title_label("Log"))
        layout.addWidget(muted_label(
            "Registra los archivos abiertos, las operaciones y los errores en este equipo. "
            "Incluye nombres de archivos y, aunque no registra los datos de las celdas, un mensaje de error podría citar alguno: "
            "revísalo antes de compartirlo."
        ))
        row = QHBoxLayout()
        self.only_problems = QCheckBox("Solo errores y advertencias")
        self.only_problems.toggled.connect(self.refresh)
        row.addWidget(self.only_problems)
        row.addStretch()
        for label, action in (("Actualizar", self.refresh), ("Copiar", self._copy), ("Abrir carpeta", self._open_folder)):
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.text, 1)

    def showEvent(self, event) -> None:
        self.refresh()
        super().showEvent(event)

    def refresh(self) -> None:
        self.text.setPlainText(applog.read_log(self.only_problems.isChecked()) or "El log está vacío.")
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(applog.log_dir())))
