"""Recorre la interfaz real sin pantalla: mismos widgets que usa la app."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

from cuypoclac import db
from cuypoclac.db import repo
from cuypoclac.ui.context import AppContext
from cuypoclac.ui.main_window import MainWindow
from cuypoclac.ui.pages.migration_wizard import MigrationWizard
from cuypoclac.ui.widgets.dynamic_form import RecordDialog

from .conftest import build_workbook

SHOTS = os.environ.get("CUYPOCLAC_SCREENSHOTS")


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def settle(window, qapp) -> None:
    for _ in range(50):
        qapp.processEvents()
        window.ctx.pool.waitForDone(100)
        qapp.processEvents()
        if window.ctx._running == 0 and not window.home._scanning:
            break
    qapp.processEvents()


def shot(window, name: str) -> None:
    if SHOTS:
        window.grab().save(os.path.join(SHOTS, f"{name}.png"))


@pytest.fixture
def window(tmp_path, qapp):
    folder = tmp_path / "carpeta"
    folder.mkdir()
    build_workbook(folder / "a.xlsx")
    ctx = AppContext(tmp_path / db.DB_NAME)
    win = MainWindow(ctx)
    win.show()
    win.home.open_folder(folder)
    settle(win, qapp)
    yield win
    win.close()


def test_home_shows_summary(window):
    assert window.home.cards["records"]._value.text() == "3"
    assert "1 importados" in window.home.status.text()
    shot(window, "inicio")


def test_records_table_filter_and_sort(window, qapp):
    window.nav.setCurrentRow(1)
    model = window.records.model
    assert model.rowCount() == 3
    headers = [model.headerData(c, Qt.Orientation.Horizontal) for c in range(model.columnCount())]
    assert headers == ["Estado", "Código", "Campo Dos", "Nivel", "Fecha", "Cantidad"]
    assert model.index(0, 4).data() == "15/01/2026"
    window.records.search.setText("tres")
    window.records.apply_query()
    assert model.rowCount() == 1
    window.records.search.setText("")
    window.records.apply_query()
    model.sort(5, Qt.SortOrder.DescendingOrder)
    assert model.index(0, 5).data() == "30"
    shot(window, "registros")


def test_schema_roles_enable_filters_and_edit(window, qapp):
    window.nav.setCurrentRow(3)
    page = window.schema
    labels = [page.table.item(r, 1).text() for r in range(page.table.rowCount())]
    assert labels == ["Código", "Campo Dos", "Nivel", "Fecha", "Cantidad"]
    page.table.item(0, 3).setCheckState(Qt.CheckState.Checked)
    page.table.item(2, 6).setCheckState(Qt.CheckState.Checked)
    page.table.item(3, 4).setCheckState(Qt.CheckState.Checked)
    page._save()
    settle(window, qapp)
    shot(window, "esquema")
    roles = repo.get_roles(window.ctx.conn, repo.current_schema(window.ctx.conn, window.ctx.workspace_id).version_id)
    assert len(roles["key"]) == 1 and len(roles["filterable"]) == 1

    window.nav.setCurrentRow(1)
    records_page = window.records
    box = next(iter(records_page._filters.values()))
    box.setCurrentIndex(box.findData("Alto"))
    assert records_page.model.rowCount() == 1
    record = records_page.model.record_at(0)
    box.setCurrentIndex(0)

    dialog = RecordDialog(window.ctx, window, record)
    ids = {f.label: f.id for f in repo.current_schema(window.ctx.conn, window.ctx.workspace_id).fields}
    assert not dialog._inputs[ids["Código"]].isEnabled()
    dialog._inputs[ids["Campo Dos"]].set_value("editado")
    dialog._save()
    settle(window, qapp)
    updated = next(r for r in [records_page.model.record_at(i) for i in range(3)] if r["id"] == record["id"])
    assert updated["data"][ids["Campo Dos"]] == "editado" and updated["edits"] == 1

    window.nav.setCurrentRow(4)
    assert window.dashboard.table.rowCount() == 3
    shot(window, "tablero")


def test_new_manual_record(window, qapp):
    dialog = RecordDialog(window.ctx, window)
    ids = {f.label: f.id for f in repo.current_schema(window.ctx.conn, window.ctx.workspace_id).fields}
    dialog._inputs[ids["Código"]].set_value("NUEVO")
    dialog._inputs[ids["Cantidad"]].set_value("5")
    dialog._inputs[ids["Nivel"]].set_value("Bajo")
    dialog._save()
    settle(window, qapp)
    assert window.records.model.rowCount() == 4
    assert window.home.cards["records"]._value.text() == "4"


def test_migration_wizard_requires_simulation(window, qapp, monkeypatch, tmp_path):
    folder = window.ctx.folder
    build_workbook(folder / "b.xlsx", second_label="Campo Dos Nuevo", options=["Alto", "Medio superior", "Bajo"], rows=[])
    window.home.scan()
    settle(window, qapp)
    assert window.pending_files() == ["b.xlsx"]
    assert not window.home.pending_box.isHidden()
    shot(window, "pendiente")

    wizard = MigrationWizard(window.ctx, window, "b.xlsx")
    wizard.show()
    kinds = [p.op["op"] for p in wizard.proposals]
    assert set(kinds) == {"rename", "set_options", "map_values"}
    assert not wizard.apply_button.isEnabled()
    assert any("Campo Dos Nuevo" in wizard.right.item(i).text() for i in range(wizard.right.count()))
    wizard._simulate()
    assert wizard.apply_button.isEnabled()
    map_row = kinds.index("map_values")
    assert wizard.table.item(map_row, 3).text() == "1 cambian"
    wizard.grab().save(os.path.join(SHOTS, "asistente.png")) if SHOTS else None

    # Cambiar la selección invalida la simulación anterior.
    wizard.table.item(kinds.index("rename"), 0).setCheckState(Qt.CheckState.Unchecked)
    assert not wizard.apply_button.isEnabled()
    wizard.table.item(kinds.index("rename"), 0).setCheckState(Qt.CheckState.Checked)
    wizard._simulate()

    monkeypatch.setattr("cuypoclac.ui.pages.migration_wizard.confirm", lambda *a, **k: True)
    wizard._apply()
    assert wizard.backup_path and wizard.backup_path.exists()
    wizard.close()
    window.ctx.data_changed.emit()
    window.home.scan()
    settle(window, qapp)
    assert window.pending_files() == []
    schema = repo.current_schema(window.ctx.conn, window.ctx.workspace_id)
    assert schema.version == 2
    window.nav.setCurrentRow(3)
    assert window.schema.history.count() == 2
    assert window.schema.backups.count() == 1
    shot(window, "historial")


def test_files_page_shows_issues(window, qapp):
    (window.ctx.folder / "malo.xlsx").write_bytes(b"no es excel")
    window.home.scan()
    settle(window, qapp)
    window.nav.setCurrentRow(2)
    page = window.files
    names = [page.files.item(r, 0).text() for r in range(page.files.rowCount())]
    bad = names.index("malo.xlsx")
    assert page.files.item(bad, 1).text() == "Error"
    page.files.selectRow(bad)
    QCoreApplication.processEvents()
    assert page.issues.rowCount() == 1
    shot(window, "archivos")


def test_export_page(window, qapp, tmp_path):
    target = tmp_path / "salida"
    window.export.export_to(target)
    settle(window, qapp)
    assert (tmp_path / "salida.xlsx").exists()
    assert "3 registros" in window.export.status.text()


def test_restore_backup_from_ui(window, qapp, monkeypatch):
    ctx = window.ctx
    backup = db.backup(ctx.conn, ctx.db_path, 1)
    ctx.conn.execute("DELETE FROM record")
    ctx.data_changed.emit()
    window.nav.setCurrentRow(3)
    page = window.schema
    row = next(i for i in range(page.backups.count()) if page.backups.item(i).data(Qt.ItemDataRole.UserRole) == str(backup))
    page.backups.setCurrentRow(row)
    monkeypatch.setattr("cuypoclac.ui.pages.schema.confirm", lambda *a, **k: True)
    page._restore()
    settle(window, qapp)
    assert window.home.cards["records"]._value.text() == "3"


def test_watcher_triggers_rescan(window, qapp):
    import time

    build_workbook(window.ctx.folder / "nuevo.xlsx")
    deadline = time.time() + 15
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.1)
        if window.home.cards["records"]._value.text() == "6":
            break
    settle(window, qapp)
    assert window.home.cards["records"]._value.text() == "6"


def test_last_used_folder_opens_on_start(window, qapp, tmp_path):
    first = window.ctx.folder
    other = tmp_path / "otra"
    other.mkdir()
    window.home.open_folder(other)
    settle(window, qapp)
    window.home.open_folder(first)
    settle(window, qapp)
    reopened = AppContext(window.ctx.db_path)
    assert reopened.folder == first
    reopened.conn.close()


def test_remove_folder_returns_to_previous_and_keeps_files(window, qapp, tmp_path, monkeypatch):
    home, ctx = window.home, window.ctx
    good = ctx.folder
    wrong = tmp_path / "equivocada"
    wrong.mkdir()
    workbook = build_workbook(wrong / "b.xlsx")
    content = workbook.read_bytes()
    home.open_folder(wrong)
    settle(window, qapp)
    assert ctx.conn.execute("SELECT COUNT(*) FROM record").fetchone()[0] == 6

    asked = []
    monkeypatch.setattr("cuypoclac.ui.pages.home.confirm", lambda _p, _t, message, _a: asked.append(message) or False)
    home._remove()
    assert str(wrong) in asked[0] and ctx.folder == wrong

    monkeypatch.setattr("cuypoclac.ui.pages.home.confirm", lambda *a: True)
    shown = []
    monkeypatch.setattr("cuypoclac.ui.pages.home.QMessageBox.information", lambda _p, _t, text: shown.append(text))
    home._remove()
    settle(window, qapp)

    assert ctx.folder == good and home.cards["records"]._value.text() == "3"
    assert ctx.conn.execute("SELECT COUNT(*) FROM record").fetchone()[0] == 3
    assert workbook.read_bytes() == content
    assert ctx.watcher is not None
    assert len(db.list_backups(ctx.db_path)) == 1 and db.list_backups(ctx.db_path)[0].name in shown[0]


def test_remove_only_folder_clears_every_page(window, qapp):
    home, ctx = window.home, window.ctx
    window.nav.setCurrentRow(1)
    assert window.records.model.total == 3
    home.remove_folder()
    settle(window, qapp)
    assert ctx.workspace_id is None and ctx.folder is None and ctx.watcher is None
    assert home.folder_label.text() == "Ninguna carpeta seleccionada"
    assert home.cards["records"]._value.text() == "—"
    assert not home.remove_button.isEnabled()
    assert window.records.model.total == 0 and window.files.files.rowCount() == 0
    assert window.schema.table.rowCount() == 0 and window.agent.table.rowCount() == 0


def test_remove_folder_waits_for_running_tasks(window, qapp):
    ctx = window.ctx
    ctx._running = 1
    try:
        with pytest.raises(RuntimeError):
            window.home.remove_folder()
    finally:
        ctx._running = 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM record").fetchone()[0] == 3


def test_remove_folder_stops_the_agent_api(window, qapp):
    from cuypoclac.agent import config

    ctx = window.ctx
    config.set_port(ctx.conn, ctx.workspace_id, 38766)
    ctx.start_agent_api()
    assert ctx.agent_server is not None
    window.home.remove_folder()
    settle(window, qapp)
    assert ctx.agent_server is None


def test_failed_backup_keeps_the_folder(window, qapp, monkeypatch):
    ctx = window.ctx
    def fail(*_):
        raise OSError("sin espacio")
    monkeypatch.setattr("cuypoclac.ui.context.db.backup", fail)
    with pytest.raises(OSError):
        window.home.remove_folder()
    assert ctx.conn.execute("SELECT COUNT(*) FROM record").fetchone()[0] == 3
    assert ctx.workspace_id is not None and ctx.watcher is not None


def test_restore_brings_back_removed_folder(window, qapp, monkeypatch):
    ctx = window.ctx
    folder = ctx.folder
    saved = window.home.remove_folder()
    settle(window, qapp)
    assert ctx.workspace_id is None
    window.schema.refresh()
    page = window.schema
    row = next(i for i in range(page.backups.count()) if page.backups.item(i).data(Qt.ItemDataRole.UserRole) == str(saved))
    page.backups.setCurrentRow(row)
    monkeypatch.setattr("cuypoclac.ui.pages.schema.confirm", lambda *a, **k: True)
    page._restore()
    settle(window, qapp)
    assert ctx.folder == folder and window.home.cards["records"]._value.text() == "3"
    assert ctx.watcher is not None


def test_log_page_shows_the_log_when_opened(window, qapp, tmp_path, monkeypatch):
    from cuypoclac import applog

    monkeypatch.setenv("CUYPOCLAC_DATA_DIR", str(tmp_path))
    applog.log_dir().mkdir()
    applog.log_path().write_text(
        "2026-01-01 10:00:00,000 INFO     cuypoclac: abierto\n"
        "2026-01-01 10:00:01,000 ERROR    cuypoclac: fallido\n",
        encoding="utf-8",
    )
    labels = [window.nav.item(i).text() for i in range(window.nav.count())]
    window.nav.setCurrentRow(labels.index("Log"))
    qapp.processEvents()
    assert "abierto" in window.log.text.toPlainText()
    window.log.only_problems.setChecked(True)
    assert "abierto" not in window.log.text.toPlainText()
    assert "fallido" in window.log.text.toPlainText()
    shot(window, "log")


def test_agent_page_saves_fields_and_toggles_api(window, qapp, tmp_path):
    from cuypoclac.agent import config

    page = window.agent
    labels = [window.nav.item(i).text() for i in range(window.nav.count())]
    window.nav.setCurrentRow(labels.index("Agente"))
    qapp.processEvents()
    rows = {page.table.item(r, 0).text(): r for r in range(page.table.rowCount())}
    assert page.table.cellWidget(rows["Cantidad"], 4).isEnabled()
    assert not page.table.cellWidget(rows["Fecha"], 4).isEnabled()

    page.table.item(rows["Nivel"], 2).setCheckState(Qt.CheckState.Checked)
    page.table.cellWidget(rows["Cantidad"], 4).setCurrentIndex(1)
    assert page.table.item(rows["Cantidad"], 2).checkState() == Qt.CheckState.Checked
    page.table.item(rows["Código"], 2).setCheckState(Qt.CheckState.Checked)
    page.table.item(rows["Código"], 3).setCheckState(Qt.CheckState.Checked)
    assert page.table.item(rows["Código"], 2).checkState() == Qt.CheckState.Unchecked

    # Un escaneo en segundo plano no borra lo marcado sin guardar.
    page.table.item(rows["Fecha"], 2).setCheckState(Qt.CheckState.Checked)
    window.ctx.data_changed.emit()
    assert page.table.item(rows["Fecha"], 2).checkState() == Qt.CheckState.Checked
    page.table.item(rows["Fecha"], 2).setCheckState(Qt.CheckState.Unchecked)

    page.save()
    fields = config.get_fields(window.ctx.conn, window.ctx.workspace_id)
    assert len(fields[config.ROLE_SHARED]) == 2
    assert len(fields[config.ROLE_PERSON]) == 1
    assert len(fields[config.ROLE_HIGHER_BETTER]) == 1

    page.port.setValue(38765)
    page.api_toggle.setChecked(True)
    assert window.ctx.agent_server is not None
    assert "38765" in page.api_status.text()
    shot(window, "agente")

    page.create_project_at(tmp_path / "proyecto")
    assert (tmp_path / "proyecto" / "AGENTS.md").exists()
    old = (tmp_path / "proyecto" / ".cuypoclac" / "curl.cfg").read_text(encoding="utf-8")
    page.rotate()
    assert (tmp_path / "proyecto" / ".cuypoclac" / "curl.cfg").read_text(encoding="utf-8") != old
    assert window.ctx.agent_server.token in (tmp_path / "proyecto" / ".cuypoclac" / "curl.cfg").read_text(encoding="utf-8")

    window.ctx.stop_agent_api()
    assert not page.api_toggle.isChecked()
    assert page.api_status.text() == "Apagada"
