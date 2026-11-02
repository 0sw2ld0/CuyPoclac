"""Estado compartido de la interfaz y tareas en segundo plano."""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .. import db
from ..agent import config as agent_config
from ..agent.server import AgentServer
from ..db import repo
from ..importing.watcher import FolderWatcher

logger = logging.getLogger(__name__)


class _TaskSignals(QObject):
    finished = Signal(object, object)
    failed = Signal(object, str)
    progress = Signal(object)


class _Task(QRunnable):
    def __init__(self, ctx: "AppContext", fn, done, error) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self._ctx, self._fn, self._done, self._error = ctx, fn, done, error

    def run(self) -> None:
        conn = None
        try:
            conn = db.connect(self._ctx.db_path, self._ctx.key)
            result = self._fn(conn, self.signals.progress.emit)
        except Exception as error:  # Se muestra al usuario: la tarea no debe matar la app.
            logger.exception("Falló una tarea en segundo plano")
            self.signals.failed.emit(self._error, str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(self._done, result)
        finally:
            if conn is not None:
                conn.close()


class AppContext(QObject):
    data_changed = Signal()
    workspace_changed = Signal()
    folder_changed = Signal()
    busy_changed = Signal(bool)
    progress = Signal(object)
    agent_api_changed = Signal()

    def __init__(self, db_path: Path, key: str | None = None) -> None:
        super().__init__()
        self.db_path = db_path
        self.key = key
        self.conn = db.connect(db_path, key)
        self.workspace_id: int | None = None
        self.folder: Path | None = None
        self.watcher: FolderWatcher | None = None
        self.agent_server: AgentServer | None = None
        self.pool = QThreadPool(self)
        self._running = 0
        last = repo.last_workspace(self.conn)
        if last:
            self.workspace_id, self.folder = last

    def set_folder(self, folder: Path) -> None:
        self.stop_agent_api()
        self.workspace_id = repo.get_or_create_workspace(self.conn, folder)
        self.folder = folder.resolve()
        logger.info("Carpeta de trabajo: %s", self.folder)
        self.start_watching()
        self.workspace_changed.emit()

    def start_watching(self) -> None:
        self.stop_watching()
        if self.folder and self.folder.is_dir():
            try:
                self.watcher = FolderWatcher(self.folder, self.folder_changed.emit)
                self.watcher.start()
            except OSError:
                # Sin vigilancia aún se puede escanear a mano.
                logger.warning("No se pudo vigilar la carpeta", exc_info=True)
                self.watcher = None

    def stop_watching(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None

    def start_agent_api(self) -> None:
        self.stop_agent_api()
        settings = agent_config.get_settings(self.conn, self.workspace_id)
        server = AgentServer(self.db_path, self.key, self.workspace_id, settings.token, settings.port)
        server.start()
        self.agent_server = server
        self.agent_api_changed.emit()

    def stop_agent_api(self) -> None:
        if self.agent_server is not None:
            self.agent_server.stop()
            self.agent_server = None
            self.agent_api_changed.emit()

    def run(self, fn, done=None, error=None) -> None:
        """Ejecuta fn(conn, progress) en otro hilo, con su propia conexión."""
        task = _Task(self, fn, done, error)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        task.signals.progress.connect(self.progress)
        self._running += 1
        if self._running == 1:
            self.busy_changed.emit(True)
        self.pool.start(task)

    @Slot(object, object)
    def _on_finished(self, callback, result) -> None:
        self._task_ended()
        if callback:
            callback(result)

    @Slot(object, str)
    def _on_failed(self, callback, message) -> None:
        self._task_ended()
        if callback:
            callback(message)

    def _task_ended(self) -> None:
        self._running -= 1
        if self._running == 0:
            self.busy_changed.emit(False)

    def reconnect(self) -> None:
        self.conn = db.connect(self.db_path, self.key)
        self.workspace_changed.emit()

    def shutdown(self) -> None:
        self.stop_agent_api()
        self.stop_watching()
        self.pool.waitForDone(10000)
        self.conn.close()
