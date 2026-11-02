"""Sub-lista editable de un grupo repetible."""

from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .common import format_value


class RepeatableGroup(QWidget):
    def __init__(self, group, fields, items=None) -> None:
        super().__init__()
        self.group, self.fields = group, fields
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(fields))
        self.table.setHorizontalHeaderLabels([f.label for f in fields])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(130)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add = QPushButton("Agregar fila")
        remove = QPushButton("Quitar fila")
        add.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout.addLayout(buttons)
        for item in items or []:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for c, field in enumerate(fields):
                self.table.setItem(row, c, QTableWidgetItem(format_value(item.get(field.id))))

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def value(self) -> list[dict]:
        items = []
        for row in range(self.table.rowCount()):
            item = {}
            for c, field in enumerate(self.fields):
                cell = self.table.item(row, c)
                text = cell.text().strip() if cell else ""
                if text:
                    item[field.id] = text
            if item:
                items.append(item)
        return items
