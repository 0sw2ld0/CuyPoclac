"""Exportar: Excel consolidado con encabezados agrupados y combos."""

from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...export import export_workspace
from ...query import RecordQuery
from ..widgets.common import muted_label, show_error, title_label


class ExportPage(QWidget):
    def __init__(self, ctx, current_query) -> None:
        super().__init__()
        self._ctx = ctx
        self._current_query = current_query
        layout = QVBoxLayout(self)
        layout.addWidget(title_label("Exportar"))
        layout.addWidget(muted_label(
            "Genera un Excel consolidado con los valores vigentes, incluidas las ediciones. "
            "Guárdalo fuera de la carpeta de trabajo. Contiene datos personales: protégelo como a los originales."
        ))
        self.use_filters = QCheckBox("Aplicar los filtros activos en Registros")
        layout.addWidget(self.use_filters)
        row = QHBoxLayout()
        self.button = QPushButton("Exportar a Excel…")
        self.button.clicked.connect(self._export)
        row.addWidget(self.button)
        row.addStretch()
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

    def _export(self) -> None:
        if self._ctx.workspace_id is None:
            show_error(self, "Primero elige una carpeta en Inicio.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Guardar consolidado", str(Path.home() / "consolidado.xlsx"), "Excel (*.xlsx)"
        )
        if target:
            self.export_to(Path(target))

    def export_to(self, target: Path) -> None:
        if target.suffix.lower() != ".xlsx":
            target = target.with_suffix(".xlsx")
        query = self._current_query() if self.use_filters.isChecked() else RecordQuery()
        workspace = self._ctx.workspace_id
        self.button.setEnabled(False)
        self.status.setText("Exportando…")
        self._ctx.run(
            lambda conn, _: export_workspace(conn, workspace, target, query),
            lambda total: self._done(total, target),
            self._failed,
        )

    def _done(self, total: int, target: Path) -> None:
        self.button.setEnabled(True)
        self.status.setText(f"Se exportaron {total} registros a {target}.")

    def _failed(self, message: str) -> None:
        self.button.setEnabled(True)
        self.status.setText("")
        show_error(self, message)

