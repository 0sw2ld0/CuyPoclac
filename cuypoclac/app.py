"""Punto de entrada de la aplicación de escritorio."""

import multiprocessing
import sys


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
            QMessageBox.warning(None, "CuyPoclac", "Contraseña incorrecta.")


def main() -> int:
    multiprocessing.freeze_support()
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("CuyPoclac")
    context = _open_database()
    if context is None:
        return 0
    window = MainWindow(context)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
