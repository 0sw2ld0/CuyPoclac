"""Punto de entrada de la aplicación de escritorio."""

import logging
import multiprocessing
import sys

from . import applog

logger = logging.getLogger(__name__)


def _open_database():
    from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

    from . import db
    from .ui.context import AppContext

    path = db.default_db_path()
    if not db.is_encrypted(path):
        return AppContext(path)
    if not db.encryption_available():
        QMessageBox.critical(None, "CuyPoclac", "La base está cifrada y esta instalación no incluye soporte de cifrado.")
        return None
    while True:
        password, accepted = QInputDialog.getText(
            None, "CuyPoclac", "Contraseña de la base de datos:", QLineEdit.EchoMode.Password
        )
        if not accepted:
            return None
        try:
            return AppContext(path, db.derive_key(path, password))
        except Exception:
            # Puede ser la contraseña u otro fallo: la traza permite distinguirlos.
            logger.warning("No se pudo abrir la base cifrada", exc_info=True)
            QMessageBox.warning(None, "CuyPoclac", "Contraseña incorrecta.")


def _show_unexpected_error() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    if QApplication.instance() is not None:
        QMessageBox.critical(
            None, "CuyPoclac", f"Ocurrió un error inesperado. El detalle quedó en el log:\n{applog.log_path()}"
        )


def _log_qt_message(mode, _context, message: str) -> None:
    from PySide6.QtCore import QtMsgType

    if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        logging.getLogger("cuypoclac.qt").warning(message)


def main() -> int:
    multiprocessing.freeze_support()
    applog.setup()
    applog.install_hooks(_show_unexpected_error)
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    qInstallMessageHandler(_log_qt_message)
    app = QApplication(sys.argv)
    app.setApplicationName("CuyPoclac")
    context = _open_database()
    if context is None:
        return 0
    window = MainWindow(context)
    window.show()
    code = app.exec()
    logger.info("Cierre normal")
    return code


if __name__ == "__main__":
    sys.exit(main())
