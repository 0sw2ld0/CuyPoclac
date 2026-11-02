"""El log debe dejar rastro de los fallos sin guardar el contenido de las celdas."""

import logging
import os
import subprocess
import sys
import textwrap

import pytest

from cuypoclac import applog, db
from cuypoclac.db import repo
from cuypoclac.importing import importer
from cuypoclac.importing.importer import scan_workspace

from .conftest import build_workbook


@pytest.fixture
def env(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="cuypoclac")
    folder = tmp_path / "carpeta"
    folder.mkdir()
    conn = db.connect(tmp_path / db.DB_NAME)
    yield conn, repo.get_or_create_workspace(conn, folder), folder
    conn.close()


def run_script(tmp_path, code: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "CUYPOCLAC_DATA_DIR": str(tmp_path), "QT_QPA_PLATFORM": "offscreen"}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], env=environment, capture_output=True, text=True, timeout=60
    )


def test_opened_file_is_logged_without_cell_values(env, caplog):
    conn, workspace, folder = env
    build_workbook(folder / "a.xlsx", rows=[("VALOR-PRIVADO", "OTRO-PRIVADO", "NIVEL-PRIVADO", "no-fecha", 1)])
    scan_workspace(conn, workspace)
    assert "Abierto a.xlsx" in caplog.text
    assert "Escaneo terminado: 1 importados" in caplog.text
    assert "PRIVADO" not in caplog.text
    assert "no-fecha" not in caplog.text


def test_corrupt_file_logs_name_and_traceback(env, caplog):
    conn, workspace, folder = env
    (folder / "malo.xlsx").write_bytes(b"esto no es un excel")
    scan_workspace(conn, workspace)
    assert "No se pudo abrir malo.xlsx" in caplog.text
    assert "Traceback" in caplog.text


def test_periodic_retries_do_not_flood_the_log(env, caplog):
    conn, workspace, folder = env
    (folder / "malo.xlsx").write_bytes(b"esto no es un excel")
    build_workbook(folder / "nuevo.xlsx", second_label="Otro nombre", extra_column="Extra")
    build_workbook(folder / "base.xlsx")
    for _ in range(3):
        scan_workspace(conn, workspace)
    assert caplog.text.count("No se pudo abrir malo.xlsx") == 1
    assert caplog.text.count("migración pendiente") == 1
    assert caplog.text.count("Escaneo terminado") <= 2

    caplog.clear()
    (folder / "malo.xlsx").write_bytes(b"otro contenido roto")
    scan_workspace(conn, workspace)
    assert "No se pudo abrir malo.xlsx" in caplog.text


def test_unexpected_failure_does_not_stop_the_other_files(env, caplog, monkeypatch):
    conn, workspace, folder = env
    build_workbook(folder / "a.xlsx")
    build_workbook(folder / "b.xlsx")
    real = importer._parse

    def fail_on_a(path):
        if path.name == "a.xlsx":
            raise TypeError("fallo inesperado")
        return real(path)

    monkeypatch.setattr(importer, "_parse", fail_on_a)
    summary = scan_workspace(conn, workspace)
    assert (summary.errors, summary.imported) == (1, 1)
    assert "TypeError: fallo inesperado" in caplog.text


def test_read_log_filters_problems_and_keeps_tracebacks(tmp_path, monkeypatch):
    monkeypatch.setenv("CUYPOCLAC_DATA_DIR", str(tmp_path))
    assert applog.read_log() == ""
    applog.log_dir().mkdir()
    applog.log_path().write_text(
        "2026-01-01 10:00:00,000 INFO     cuypoclac: bien\n"
        "2026-01-01 10:00:01,000 ERROR    cuypoclac: mal\n"
        "Traceback (most recent call last):\n"
        "ValueError: detalle\n"
        "2026-01-01 10:00:02,000 INFO     cuypoclac: bien otra vez\n",
        encoding="utf-8",
    )
    assert "bien otra vez" in applog.read_log()
    problems = applog.read_log(only_problems=True)
    assert "ValueError: detalle" in problems
    assert "bien" not in problems


def test_read_log_starts_at_a_full_line_when_truncated(tmp_path, monkeypatch):
    monkeypatch.setenv("CUYPOCLAC_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(applog, "_TAIL_BYTES", 40)
    applog.log_dir().mkdir()
    applog.log_path().write_text("x" * 100 + "\nlinea entera\n", encoding="utf-8")
    assert applog.read_log() == "linea entera\n"


def test_uncaught_error_reaches_the_log_file(tmp_path):
    result = run_script(tmp_path, """
        from cuypoclac import applog
        applog.setup()
        applog.install_hooks()
        raise RuntimeError("falla principal")
    """)
    assert result.returncode != 0
    text = (tmp_path / "logs" / applog.LOG_NAME).read_text(encoding="utf-8")
    assert "Inicio: CuyPoclac" in text
    assert "RuntimeError: falla principal" in text


def test_uncaught_error_in_thread_reaches_the_log_file(tmp_path):
    run_script(tmp_path, """
        import threading
        from cuypoclac import applog
        applog.setup()
        applog.install_hooks()
        thread = threading.Thread(target=lambda: 1 / 0, name="hilo-prueba")
        thread.start()
        thread.join()
    """)
    text = (tmp_path / "logs" / applog.LOG_NAME).read_text(encoding="utf-8")
    assert "hilo-prueba" in text
    assert "ZeroDivisionError" in text


def test_error_in_qt_slot_reaches_the_log_and_app_survives(tmp_path):
    result = run_script(tmp_path, """
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from cuypoclac import applog
        seen = []
        applog.setup()
        applog.install_hooks(lambda: seen.append(True))
        app = QApplication([])
        def slot():
            raise RuntimeError("falla en slot")
        QTimer.singleShot(0, slot)
        QTimer.singleShot(100, app.quit)
        app.exec()
        print("avisos", len(seen))
    """)
    assert result.returncode == 0, result.stderr
    assert "avisos 1" in result.stdout
    text = (tmp_path / "logs" / applog.LOG_NAME).read_text(encoding="utf-8")
    assert "RuntimeError: falla en slot" in text


def test_native_crash_is_written_to_crash_log(tmp_path):
    result = run_script(tmp_path, """
        import faulthandler
        from cuypoclac import applog
        applog.setup()
        applog.install_hooks()
        faulthandler._sigsegv()
    """)
    assert result.returncode != 0
    assert "Segmentation fault" in (tmp_path / "logs" / applog.CRASH_NAME).read_text(encoding="utf-8")


def test_app_without_writable_log_still_starts(tmp_path):
    (tmp_path / "logs").write_text("un archivo donde debería ir la carpeta", encoding="utf-8")
    result = run_script(tmp_path, """
        from cuypoclac import applog
        applog.setup()
        applog.install_hooks()
        print("arrancó")
    """)
    assert result.returncode == 0, result.stderr
    assert "arrancó" in result.stdout
