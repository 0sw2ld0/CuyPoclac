"""Tablero: distribución de registros por cada campo filtrable."""

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...db import repo
from ...query import count_records, value_counts
from ..widgets.common import muted_label, title_label


class DashboardPage(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        layout = QVBoxLayout(self)
        layout.addWidget(title_label("Tablero"))
        layout.addWidget(muted_label("Marca campos como «Filtrable» en Esquema para verlos aquí."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Campo"))
        self.field_box = QComboBox()
        self.field_box.currentIndexChanged.connect(self._show)
        row.addWidget(self.field_box)
        row.addStretch()
        layout.addLayout(row)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Valor", "Registros", "Proporción"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        ctx.data_changed.connect(self.refresh)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        ctx = self._ctx
        previous = self.field_box.currentData()
        self.field_box.blockSignals(True)
        self.field_box.clear()
        schema = repo.current_schema(ctx.conn, ctx.workspace_id) if ctx.workspace_id else None
        if schema is not None:
            labels = {f.id: f.label for f in schema.active_fields}
            for field_id in repo.get_roles(ctx.conn, schema.version_id).get("filterable", []):
                if field_id in labels:
                    self.field_box.addItem(labels[field_id], field_id)
        if previous is not None and self.field_box.findData(previous) >= 0:
            self.field_box.setCurrentIndex(self.field_box.findData(previous))
        self.field_box.blockSignals(False)
        self._show()

    def _show(self) -> None:
        self.table.setRowCount(0)
        field_id = self.field_box.currentData()
        if field_id is None:
            return
        workspace = self._ctx.workspace_id
        total = count_records(self._ctx.conn, workspace) or 1
        for label, count in value_counts(self._ctx.conn, workspace, field_id):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(label))
            self.table.setItem(row, 1, QTableWidgetItem(str(count)))
            bar = QProgressBar()
            bar.setRange(0, total)
            bar.setValue(count)
            bar.setFormat(f"{round(count * 100 / total)} %")
            self.table.setCellWidget(row, 2, bar)
