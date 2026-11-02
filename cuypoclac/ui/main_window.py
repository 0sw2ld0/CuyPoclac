"""Ventana principal: navegación entre páginas y asistente de migración."""

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QListWidget, QMainWindow, QMessageBox, QStackedWidget, QStatusBar, QWidget,
)

from .context import AppContext
from .pages.dashboard import DashboardPage
from .pages.export import ExportPage
from .pages.files import FilesPage
from .pages.home import HomePage
from .pages.migration_wizard import MigrationWizard
from .pages.records import RecordsPage
from .pages.schema import SchemaPage
from .widgets.common import show_error


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("CuyPoclac")
        self.resize(QSize(1180, 760))

        self.home = HomePage(ctx)
        self.records = RecordsPage(ctx)
        self.files = FilesPage(ctx)
        self.schema = SchemaPage(ctx)
        self.dashboard = DashboardPage(ctx)
        self.export = ExportPage(ctx, self.records.current_query)

        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        self.nav.setStyleSheet("QListWidget::item { padding: 8px 10px; }")
        self.stack = QStackedWidget()
        for label, page in (
            ("Inicio", self.home), ("Registros", self.records), ("Archivos", self.files),
            ("Esquema", self.schema), ("Tablero", self.dashboard), ("Exportar", self.export),
        ):
            self.nav.addItem(label)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self.home.review_migration.connect(self.open_pending_migration)
        self.schema.manual_migration.connect(self.open_manual_migration)
        ctx.busy_changed.connect(lambda busy: self.statusBar().showMessage("Trabajando…" if busy else ""))
        if ctx.workspace_id is not None:
            ctx.start_watching()
            self.home.scan()

    def pending_files(self) -> list[str]:
        rows = self.ctx.conn.execute(
            "SELECT rel_path FROM source_file WHERE workspace_id = ? AND status = 'pending_migration' ORDER BY rel_path",
            (self.ctx.workspace_id,),
        ).fetchall()
        return [row["rel_path"] for row in rows]

    def open_pending_migration(self) -> None:
        pending = self.pending_files()
        if not pending:
            return
        rel_path = pending[0]
        if len(pending) > 1:
            rel_path, accepted = QInputDialog.getItem(
                self, "Archivos pendientes", "Elige el archivo que define la versión nueva:", pending, 0, False
            )
            if not accepted:
                return
        self._run_wizard(rel_path)

    def open_manual_migration(self) -> None:
        self._run_wizard(None)

    def _run_wizard(self, rel_path: str | None) -> None:
        try:
            wizard = MigrationWizard(self.ctx, self, rel_path)
        except Exception as error:
            show_error(self, f"No se pudo leer la estructura del archivo.\n\n{error}")
            return
        if wizard.exec() and wizard.backup_path:
            QMessageBox.information(self, "Migración aplicada", f"Se guardó un backup: {wizard.backup_path.name}")
            self.ctx.data_changed.emit()
            self.home.scan()

    def closeEvent(self, event) -> None:
        self.ctx.shutdown()
        super().closeEvent(event)
