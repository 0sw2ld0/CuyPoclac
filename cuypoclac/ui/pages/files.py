"""Archivos: estado de cada archivo y su reporte de problemas."""

from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from ..widgets.common import STATUS_LABELS, format_timestamp, muted_label, title_label

_SEVERITY = {"error": "Error", "warning": "Advertencia"}


def _read_only_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().setVisible(False)
    return table


class FilesPage(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        self._file_ids: list[int] = []
        layout = QVBoxLayout(self)
        layout.addWidget(title_label("Archivos"))
        layout.addWidget(muted_label("Selecciona un archivo para ver sus problemas."))
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.files = _read_only_table(["Archivo", "Estado", "Versión", "Registros", "Problemas", "Última importación"])
        self.files.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files.itemSelectionChanged.connect(self._show_issues)
        splitter.addWidget(self.files)
        self.issues = _read_only_table(["Fila", "Tipo", "Detalle"])
        self.issues.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.issues)
        layout.addWidget(splitter, 1)
        ctx.data_changed.connect(self.refresh)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.files.setRowCount(0)
        self.issues.setRowCount(0)
        self._file_ids = []
        if self._ctx.workspace_id is None:
            return
        rows = self._ctx.conn.execute(
            """SELECT f.id, f.rel_path, f.status, f.last_imported_at, v.version,
                      (SELECT COUNT(*) FROM record r WHERE r.source_file_id = f.id) AS records,
                      (SELECT COUNT(*) FROM import_issue i WHERE i.source_file_id = f.id) AS problems
               FROM source_file f LEFT JOIN schema_version v ON v.id = f.schema_version_id
               WHERE f.workspace_id = ? ORDER BY f.rel_path""",
            (self._ctx.workspace_id,),
        ).fetchall()
        for row in rows:
            index = self.files.rowCount()
            self.files.insertRow(index)
            self._file_ids.append(row["id"])
            cells = [
                row["rel_path"], STATUS_LABELS.get(row["status"], row["status"]),
                f"v{row['version']}" if row["version"] else "", str(row["records"]),
                str(row["problems"]), format_timestamp(row["last_imported_at"]),
            ]
            for column, text in enumerate(cells):
                self.files.setItem(index, column, QTableWidgetItem(text))

    def _show_issues(self) -> None:
        self.issues.setRowCount(0)
        selected = self.files.currentRow()
        if not 0 <= selected < len(self._file_ids):
            return
        rows = self._ctx.conn.execute(
            "SELECT row, severity, message FROM import_issue WHERE source_file_id = ? ORDER BY row, id LIMIT 2000",
            (self._file_ids[selected],),
        ).fetchall()
        for row in rows:
            index = self.issues.rowCount()
            self.issues.insertRow(index)
            cells = ["" if row["row"] is None else str(row["row"]), _SEVERITY.get(row["severity"], row["severity"]), row["message"]]
            for column, text in enumerate(cells):
                self.issues.setItem(index, column, QTableWidgetItem(text))
