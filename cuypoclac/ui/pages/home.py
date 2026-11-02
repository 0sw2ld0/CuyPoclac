"""Inicio: elegir carpeta, escanear y ver el resumen."""

import os
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ...importing.importer import scan_workspace
from ...query import workspace_summary
from ..widgets.common import StatCard, muted_label, show_error, title_label

_RETRY_MS = 30000


class HomePage(QWidget):
    review_migration = Signal()

    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        self._scanning = False
        self._scan_again = False
        layout = QVBoxLayout(self)
        layout.addWidget(title_label("Inicio"))
        layout.addWidget(muted_label("Los archivos de la carpeta se leen, pero nunca se modifican. Los datos no salen de este equipo."))

        row = QHBoxLayout()
        self.folder_label = QLabel("Ninguna carpeta seleccionada")
        self.folder_label.setWordWrap(True)
        row.addWidget(self.folder_label, 1)
        choose = QPushButton("Elegir carpeta…")
        choose.clicked.connect(self._choose)
        self.scan_button = QPushButton("Escanear")
        self.scan_button.clicked.connect(self.scan)
        row.addWidget(choose)
        row.addWidget(self.scan_button)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = muted_label()
        layout.addWidget(self.status)

        grid = QGridLayout()
        self.cards = {
            "files": StatCard("Archivos"),
            "records": StatCard("Registros"),
            "problems": StatCard("Problemas"),
            "conflicts": StatCard("Conflictos de edición"),
        }
        for column, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, column)
        layout.addLayout(grid)

        self.pending_box = QWidget()
        pending = QHBoxLayout(self.pending_box)
        pending.setContentsMargins(0, 0, 0, 0)
        self.pending_label = QLabel()
        self.pending_label.setWordWrap(True)
        pending.addWidget(self.pending_label, 1)
        review = QPushButton("Revisar migración")
        review.clicked.connect(self.review_migration)
        pending.addWidget(review)
        self.pending_box.setVisible(False)
        layout.addWidget(self.pending_box)
        layout.addStretch()

        ctx.folder_changed.connect(self.scan)
        ctx.progress.connect(self._on_progress)
        ctx.data_changed.connect(self.refresh)
        ctx.workspace_changed.connect(self.refresh)
        self._retry = QTimer(self)
        self._retry.setInterval(_RETRY_MS)
        self._retry.timeout.connect(self._retry_blocked)
        self._retry.start()
        self.refresh()

    def _choose(self) -> None:
        start = str(self._ctx.folder) if self._ctx.folder else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Elegir carpeta de trabajo", start)
        if folder:
            self.open_folder(Path(folder))

    def open_folder(self, folder: Path) -> None:
        self._ctx.set_folder(folder)
        self.scan()

    def scan(self) -> None:
        if self._ctx.workspace_id is None:
            return
        if self._scanning:
            self._scan_again = True
            return
        self._scanning = True
        self.scan_button.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.status.setText("Escaneando…")
        workspace = self._ctx.workspace_id
        workers = min(4, os.cpu_count() or 1)

        def task(conn, report):
            return scan_workspace(conn, workspace, workers=workers, progress=lambda i, n, _: report((i, n)))

        self._ctx.run(task, self._scan_done, self._scan_failed)

    def _on_progress(self, value) -> None:
        index, total = value
        self.progress.setRange(0, total)
        self.progress.setValue(index)

    def _scan_finished(self) -> None:
        self._scanning = False
        self.scan_button.setEnabled(True)
        self.progress.setVisible(False)
        if self._scan_again:
            self._scan_again = False
            self.scan()

    def _scan_done(self, summary) -> None:
        parts = [f"{summary.imported} importados", f"{summary.unchanged} sin cambios"]
        if summary.locked:
            parts.append(f"{summary.locked} en uso, se reintentará")
        if summary.errors:
            parts.append(f"{summary.errors} con error")
        if summary.missing:
            parts.append(f"{summary.missing} no encontrados")
        self.status.setText("Último escaneo: " + ", ".join(parts) + ".")
        self._scan_finished()
        self._ctx.data_changed.emit()

    def _scan_failed(self, message: str) -> None:
        self.status.setText("El escaneo no se completó.")
        self._scan_finished()
        show_error(self, message)

    def _retry_blocked(self) -> None:
        if self._ctx.workspace_id is None or self._scanning:
            return
        blocked = self._ctx.conn.execute(
            "SELECT 1 FROM source_file WHERE workspace_id = ? AND status IN ('locked', 'error') LIMIT 1",
            (self._ctx.workspace_id,),
        ).fetchone()
        if blocked:
            self.scan()

    def refresh(self) -> None:
        ctx = self._ctx
        if ctx.workspace_id is None:
            self.pending_box.setVisible(False)
            return
        self.folder_label.setText(f"Carpeta: {ctx.folder}")
        summary = workspace_summary(ctx.conn, ctx.workspace_id)
        for key, card in self.cards.items():
            card.set_value(summary[key])
        pending = summary["pending"]
        self.pending_box.setVisible(pending > 0)
        if pending:
            self.pending_label.setText(
                f"{pending} archivo(s) tienen una estructura nueva. Sus datos no se importan hasta revisar la migración."
            )
