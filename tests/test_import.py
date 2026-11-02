import json
import shutil
from pathlib import Path

import openpyxl
import pytest

from cuypoclac import db, records
from cuypoclac.agent import config as agent_config
from cuypoclac.db import repo
from cuypoclac.export import ExportError, export_workspace
from cuypoclac.importing.importer import scan_workspace
from cuypoclac.migration.apply import apply_migration, propose, resolve_ops
from cuypoclac.migration.dry_run import dry_run
from cuypoclac.query import RecordQuery, count_records, query_records, value_counts

from .conftest import build_workbook


@pytest.fixture
def env(tmp_path):
    folder = tmp_path / "carpeta"
    folder.mkdir()
    db_path = tmp_path / "datos" / db.DB_NAME
    db_path.parent.mkdir()
    conn = db.connect(db_path)
    workspace = repo.get_or_create_workspace(conn, folder)
    yield conn, workspace, folder, db_path
    conn.close()


def field_id(conn, workspace, label):
    schema = repo.current_schema(conn, workspace)
    return next(f.id for f in schema.fields if f.normalized_label == label)


def all_records(conn, workspace):
    return query_records(conn, workspace, RecordQuery(limit=1000))


def set_key(conn, workspace):
    records.set_roles(conn, workspace, {"key": [field_id(conn, workspace, "codigo")]})


def test_first_import_needs_no_configuration(env):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx")
    summary = scan_workspace(conn, workspace)
    assert (summary.imported, summary.records, summary.pending) == (1, 3, [])
    schema = repo.current_schema(conn, workspace)
    assert schema.version == 1 and len(schema.groups) == 2 and len(schema.fields) == 5
    data = all_records(conn, workspace)[0]["data"]
    assert data[field_id(conn, workspace, "fecha")] == "2026-01-15"
    assert data[field_id(conn, workspace, "cantidad")] == 10


def test_unchanged_file_is_skipped_and_never_modified(env):
    conn, workspace, folder, _ = env
    path = build_workbook(folder / "a.xlsx")
    before = path.read_bytes()
    scan_workspace(conn, workspace)
    summary = scan_workspace(conn, workspace)
    assert summary.unchanged == 1 and summary.imported == 0
    assert path.read_bytes() == before
    assert sorted(p.name for p in folder.iterdir()) == ["a.xlsx"]


def test_rename_changes_no_record(env):
    """Criterio: columna renombrada → rename con confianza alta; ningún registro cambia."""
    conn, workspace, folder, db_path = env
    build_workbook(folder / "a.xlsx", second_label="Campo Dos")
    scan_workspace(conn, workspace)
    before = {r["id"]: r["data"] for r in all_records(conn, workspace)}
    build_workbook(folder / "b.xlsx", second_label="Campo Dos Nuevo", rows=[])
    summary = scan_workspace(conn, workspace)
    assert summary.pending == ["b.xlsx"]

    plan = propose(conn, workspace, "b.xlsx")
    assert [p.op["op"] for p in plan.proposals] == ["rename"]
    assert plan.proposals[0].confidence == "alta"
    ops = resolve_ops(plan, set())
    result = dry_run(conn, workspace, repo.current_schema(conn, workspace), ops)
    assert result.total_records == 3 and result.stats[0][1].affected == 0
    backup_path = apply_migration(conn, db_path, workspace, ops, plan.fingerprint)
    assert backup_path.exists() and ".bak-v1-" in backup_path.name

    assert {r["id"]: r["data"] for r in all_records(conn, workspace)} == before
    schema = repo.current_schema(conn, workspace)
    assert schema.version == 2
    assert any(f.label == "Campo Dos Nuevo" for f in schema.active_fields)
    assert scan_workspace(conn, workspace).pending == []


def test_option_change_dry_run_counts(env):
    """Criterio: cambio de combo → map_values, y el dry-run cuenta los registros."""
    conn, workspace, folder, db_path = env
    build_workbook(folder / "a.xlsx")
    scan_workspace(conn, workspace)
    build_workbook(folder / "b.xlsx", options=["Alto", "Medio superior", "Bajo"], rows=[])
    scan_workspace(conn, workspace)
    plan = propose(conn, workspace, "b.xlsx")
    ops = resolve_ops(plan, set())
    result = dry_run(conn, workspace, repo.current_schema(conn, workspace), ops)
    by_op = {op["op"]: stats for op, stats in result.stats}
    assert by_op["map_values"].affected == 1
    apply_migration(conn, db_path, workspace, ops, plan.fingerprint)
    level = field_id(conn, workspace, "nivel")
    assert sorted(r["data"][level] for r in all_records(conn, workspace)) == ["Alto", "Bajo", "Medio superior"]


def test_old_file_added_later_uses_chain(env):
    """Criterio: un Excel v1 agregado cuando ya existe v3 se ve con el esquema v3."""
    conn, workspace, folder, db_path = env
    build_workbook(folder / "v1.xlsx", second_label="Campo Dos")
    scan_workspace(conn, workspace)
    old_copy = folder.parent / "v1_tardio.xlsx"
    build_workbook(old_copy, second_label="Campo Dos", rows=[("B-1", "viejo", "Medio", None, 5)])

    build_workbook(folder / "v2.xlsx", second_label="Campo Dos Nuevo", rows=[])
    scan_workspace(conn, workspace)
    plan = propose(conn, workspace, "v2.xlsx")
    apply_migration(conn, db_path, workspace, resolve_ops(plan, set()), plan.fingerprint)

    build_workbook(folder / "v3.xlsx", second_label="Campo Dos Nuevo", options=["Alto", "Medio superior", "Bajo"], rows=[])
    scan_workspace(conn, workspace)
    plan = propose(conn, workspace, "v3.xlsx")
    apply_migration(conn, db_path, workspace, resolve_ops(plan, set()), plan.fingerprint)
    assert repo.current_schema(conn, workspace).version == 3

    shutil.copy(old_copy, folder / "v1_tardio.xlsx")
    summary = scan_workspace(conn, workspace)
    # También se importa v3.xlsx, que estaba pendiente de su migración.
    assert summary.pending == [] and summary.imported == 2
    level = field_id(conn, workspace, "nivel")
    second = field_id(conn, workspace, "campo dos nuevo")
    late = [r for r in all_records(conn, workspace) if r["rel_path"] == "v1_tardio.xlsx"][0]
    assert late["data"][second] == "viejo"
    assert late["data"][level] == "Medio superior"
    assert late["origin_version"] == 1


def test_locked_file_then_retried(env):
    """Criterio: un archivo abierto en Excel queda locked y se reintenta luego."""
    conn, workspace, folder, _ = env
    build_workbook(folder / "reporte.xlsx")
    lock = folder / "~$porte.xlsx"
    lock.write_bytes(b"x")
    summary = scan_workspace(conn, workspace)
    assert summary.locked == 1 and summary.records == 0
    status = conn.execute("SELECT status FROM source_file").fetchone()["status"]
    assert status == "locked"
    lock.unlink()
    summary = scan_workspace(conn, workspace)
    assert summary.imported == 1 and summary.records == 3


def test_edit_survives_reimport_and_conflicts(env):
    conn, workspace, folder, _ = env
    rows = [("A-1", "uno", "Alto", None, 1), ("A-2", "dos", "Bajo", None, 2)]
    path = build_workbook(folder / "a.xlsx", rows=rows)
    scan_workspace(conn, workspace)
    second = field_id(conn, workspace, "campo dos")
    first_record = all_records(conn, workspace)[0]

    with pytest.raises(records.RecordError, match="clave"):
        records.update_record(conn, workspace, first_record["id"], {second: "editado"})
    set_key(conn, workspace)
    records.update_record(conn, workspace, first_record["id"], {second: "editado"})

    # Otro campo cambia en el Excel: la edición se mantiene sin conflicto.
    build_workbook(path, rows=[("A-1", "uno", "Bajo", None, 9), ("A-2", "dos", "Bajo", None, 2)])
    scan_workspace(conn, workspace)
    record = all_records(conn, workspace)[0]
    assert record["id"] == first_record["id"]
    assert record["data"][second] == "editado"
    assert record["data"][field_id(conn, workspace, "cantidad")] == 9
    assert records.record_edits(conn, record["id"])[0]["status"] == "active"

    # El mismo campo cambia en el Excel: conflicto, y la edición sigue visible.
    build_workbook(path, rows=[("A-1", "cambiado", "Bajo", None, 9), ("A-2", "dos", "Bajo", None, 2)])
    scan_workspace(conn, workspace)
    record = all_records(conn, workspace)[0]
    assert record["data"][second] == "editado" and record["conflicts"] == 1
    edit = records.record_edits(conn, record["id"])[0]
    assert (edit["status"], edit["incoming_value"]) == ("conflict", "cambiado")
    assert conn.execute("SELECT status FROM source_file").fetchone()["status"] == "warnings"

    records.resolve_conflict(conn, record["id"], second, keep_edit=False)
    record = all_records(conn, workspace)[0]
    assert record["data"][second] == "cambiado" and record["edits"] == 0


def test_key_field_not_editable_on_imported_record(env):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx")
    scan_workspace(conn, workspace)
    set_key(conn, workspace)
    record = all_records(conn, workspace)[0]
    with pytest.raises(records.RecordError, match="clave"):
        records.update_record(conn, workspace, record["id"], {field_id(conn, workspace, "codigo"): "Z"})
    with pytest.raises(records.RecordError, match="clave"):
        other = field_id(conn, workspace, "campo dos")
        records.update_record(conn, workspace, record["id"], {other: "x"})
        records.set_roles(conn, workspace, {"key": [other]})


def test_vanished_rows_and_files(env):
    conn, workspace, folder, _ = env
    rows = [("A-1", "uno", "Alto", None, 1), ("A-2", "dos", "Bajo", None, 2)]
    path = build_workbook(folder / "a.xlsx", rows=rows)
    scan_workspace(conn, workspace)
    set_key(conn, workspace)
    second = field_id(conn, workspace, "campo dos")
    edited = all_records(conn, workspace)[0]
    records.update_record(conn, workspace, edited["id"], {second: "editado"})

    build_workbook(path, rows=[("A-2", "dos", "Bajo", None, 2)])
    scan_workspace(conn, workspace)
    found = {r["id"]: r for r in all_records(conn, workspace)}
    assert found[edited["id"]]["status"] == "orphaned"
    assert len(found) == 2

    path.unlink()
    summary = scan_workspace(conn, workspace)
    assert summary.missing == 1
    assert conn.execute("SELECT status FROM source_file").fetchone()["status"] == "missing"
    assert [r["id"] for r in all_records(conn, workspace)] == [edited["id"]]

    # Si el archivo vuelve, el registro huérfano se reactiva.
    build_workbook(path, rows=rows)
    scan_workspace(conn, workspace)
    found = {r["id"]: r for r in all_records(conn, workspace)}
    assert found[edited["id"]]["status"] == "active"
    assert found[edited["id"]]["data"][second] == "editado"
    assert len(found) == 2


def test_manual_record_survives_and_duplicate_warns(env):
    conn, workspace, folder, _ = env
    path = build_workbook(folder / "a.xlsx")
    scan_workspace(conn, workspace)
    set_key(conn, workspace)
    code = field_id(conn, workspace, "codigo")
    manual = records.create_manual(conn, workspace, {code: "A-1", field_id(conn, workspace, "cantidad"): "7"})
    records.set_roles(conn, workspace, {"key": [code]})
    issues = conn.execute("SELECT code FROM import_issue WHERE code = 'duplicate'").fetchall()
    assert len(issues) == 1

    build_workbook(path, rows=[("A-9", "x", "Alto", None, 1)])
    scan_workspace(conn, workspace)
    remaining = {r["id"]: r for r in all_records(conn, workspace)}
    assert remaining[manual]["data"][field_id(conn, workspace, "cantidad")] == 7
    assert remaining[manual]["origin"] == "manual"
    records.delete_manual(conn, manual)
    assert manual not in {r["id"] for r in all_records(conn, workspace)}


def test_duplicates_between_files_warn_without_overwriting(env):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx", rows=[("A-1", "uno", "Alto", None, 1)])
    build_workbook(folder / "b.xlsx", rows=[("A-1", "otro", "Bajo", None, 2)])
    scan_workspace(conn, workspace)
    set_key(conn, workspace)
    assert count_records(conn, workspace) == 2
    assert len(conn.execute("SELECT 1 FROM import_issue WHERE code = 'duplicate'").fetchall()) == 2


def test_filters_search_and_counts(env):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx")
    scan_workspace(conn, workspace)
    level = field_id(conn, workspace, "nivel")
    date_field = field_id(conn, workspace, "fecha")
    records.set_roles(conn, workspace, {"filterable": [level], "date": [date_field]})
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (f"idx_rec_{level}",)).fetchone()
    assert count_records(conn, workspace, RecordQuery(filters={level: "Alto"})) == 1
    assert count_records(conn, workspace, RecordQuery(search="tre")) == 1
    query = RecordQuery(date_field=date_field, date_from="2026-02-01", date_to="2026-02-28")
    assert count_records(conn, workspace, query) == 1
    assert dict(value_counts(conn, workspace, level)) == {"Alto": 1, "Medio": 1, "Bajo": 1}
    with pytest.raises(ValueError):
        count_records(conn, workspace, RecordQuery(filters={"f_1') OR 1=1 --": "x"}))


def test_total_and_blank_rows_are_skipped(env):
    conn, workspace, folder, _ = env
    rows = [("A-1", "uno", "Alto", None, 1), (None, None, None, None, None), ("A-2", "dos", "Bajo", None, 2), ("Total", None, None, None, 3)]
    build_workbook(folder / "a.xlsx", rows=rows)
    assert scan_workspace(conn, workspace).records == 2


def test_invalid_values_become_issues(env):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx", rows=[("A-1", "uno", "Inventado", None, 1)])
    scan_workspace(conn, workspace)
    messages = [r["message"] for r in conn.execute("SELECT message FROM import_issue")]
    assert any("no es una opción" in m for m in messages)
    assert conn.execute("SELECT status FROM source_file").fetchone()["status"] == "warnings"


def test_corrupt_file_is_reported(env):
    conn, workspace, folder, _ = env
    (folder / "malo.xlsx").write_bytes(b"esto no es un excel")
    summary = scan_workspace(conn, workspace)
    assert summary.errors == 1
    assert conn.execute("SELECT status FROM source_file").fetchone()["status"] == "error"


def test_convert_failures_are_kept_in_unmigrated(env):
    conn, workspace, folder, db_path = env
    build_workbook(folder / "a.xlsx", rows=[("A-1", "12", "Alto", None, 1), ("A-2", "abc", "Bajo", None, 2)])
    scan_workspace(conn, workspace)
    second = field_id(conn, workspace, "campo dos")
    ops = [{"op": "convert", "field": second, "to": "number"}]
    schema = repo.current_schema(conn, workspace)
    result = dry_run(conn, workspace, schema, ops)
    assert result.stats[0][1].failed == 1 and result.stats[0][1].failed_values == ["abc"]
    apply_migration(conn, db_path, workspace, ops, None)
    found = all_records(conn, workspace)
    assert found[0]["data"][second] == 12
    assert found[1]["data"]["_unmigrated"][second] == "abc"
    # La huella del archivo sigue resolviendo a la versión anterior y pasa por la cadena.
    assert repo.current_schema(conn, workspace).version == 2
    assert scan_workspace(conn, workspace).pending == []


def test_split_merge_and_edit_migration(env):
    conn, workspace, folder, db_path = env
    build_workbook(folder / "a.xlsx", rows=[("A-1", "uno - dos", "Alto", None, 1)])
    scan_workspace(conn, workspace)
    set_key(conn, workspace)
    second = field_id(conn, workspace, "campo dos")
    amount = field_id(conn, workspace, "cantidad")
    record = all_records(conn, workspace)[0]
    new_field = repo.next_field_id_factory(conn, workspace, "f_")()
    ops = [{"op": "split", "field": second, "into": [second, new_field], "separator": " - ", "labels": {new_field: "Parte Dos"}}]
    apply_migration(conn, db_path, workspace, ops, None)
    data = all_records(conn, workspace)[0]["data"]
    assert (data[second], data[new_field]) == ("uno", "dos")
    assert data["_original"][second] == "uno - dos"
    ops = [{"op": "merge", "fields": [second, new_field], "into": second, "separator": "+"}]
    apply_migration(conn, db_path, workspace, ops, None)
    data = all_records(conn, workspace)[0]["data"]
    assert data[second] == "uno+dos" and new_field not in data
    records.update_record(conn, workspace, record["id"], {amount: "50"})
    ops = [{"op": "convert", "field": amount, "to": "text"}]
    apply_migration(conn, db_path, workspace, ops, None)
    assert records.record_edits(conn, record["id"])[0]["value"] == "50"
    assert all_records(conn, workspace)[0]["data"][amount] == "50"


def test_failed_migration_rolls_back(env):
    conn, workspace, folder, db_path = env
    build_workbook(folder / "a.xlsx")
    scan_workspace(conn, workspace)
    before = [r["data"] for r in all_records(conn, workspace)]
    with pytest.raises(KeyError):
        apply_migration(conn, db_path, workspace, [{"op": "rename", "field": "f_99", "label": "x"}], None)
    assert repo.current_schema(conn, workspace).version == 1
    assert [r["data"] for r in all_records(conn, workspace)] == before
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_export_round_trip(env, tmp_path):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx", rows=[])
    scan_workspace(conn, workspace)
    records.create_manual(conn, workspace, {field_id(conn, workspace, "codigo"): "=1+1"})
    target = tmp_path / "salida.xlsx"
    assert export_workspace(conn, workspace, target) == 1
    wb = openpyxl.load_workbook(target)
    ws = wb["Consolidado"]
    assert ws["A1"].value == "Identificación" and "A1:B1" in [str(r) for r in ws.merged_cells.ranges]
    assert ws["A2"].value == "Código"
    assert ws["A3"].value == "=1+1" and ws["A3"].data_type == "s"
    validations = ws.data_validations.dataValidation
    assert validations and validations[0].formula1 == '"Alto,Medio,Bajo"'
    with pytest.raises(ExportError):
        export_workspace(conn, workspace, folder / "salida.xlsx")
    assert not (folder / "salida.xlsx").exists()


def test_repeatable_numbered_import_and_export(env, tmp_path):
    conn, workspace, folder, _ = env
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Código", "Tarea 1", "Tarea 2"])
    ws.append(["A", "t1", "t2"])
    ws.append(["B", "t3", None])
    wb.save(folder / "a.xlsx")
    scan_workspace(conn, workspace)
    schema = repo.current_schema(conn, workspace)
    group = schema.groups[0]
    task = next(f for f in schema.fields if f.normalized_label == "tarea")
    found = all_records(conn, workspace)
    assert found[0]["data"][group.id] == [{task.id: "t1"}, {task.id: "t2"}]
    assert found[1]["data"][group.id] == [{task.id: "t3"}]
    target = tmp_path / "s.xlsx"
    export_workspace(conn, workspace, target)
    out = openpyxl.load_workbook(target).active
    assert [out.cell(2, c).value for c in (1, 2, 3)] == ["Código", "Tarea 1", "Tarea 2"]
    assert out.cell(3, 3).value == "t2"


def test_repeated_rows_after_confirmation(env):
    conn, workspace, folder, _ = env
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Código", "Ítem", "Valor"])
    ws.append(["A", "i1", 1])
    ws.append([None, "i2", 2])
    ws.append(["B", "i3", 3])
    wb.save(folder / "a.xlsx")
    scan_workspace(conn, workspace)
    hint = json.loads(conn.execute("SELECT repeated_rows_hint FROM source_file").fetchone()[0])
    assert hint == ["|codigo"]
    records.confirm_repeated_rows(conn, workspace, hint, "Detalle")
    summary = scan_workspace(conn, workspace)
    assert summary.imported == 1 and summary.records == 2
    schema = repo.current_schema(conn, workspace)
    group = next(g for g in schema.groups if g.repeat_mode == "repeated_rows")
    found = all_records(conn, workspace)
    assert len(found[0]["data"][group.id]) == 2
    assert len(found[1]["data"][group.id]) == 1


def test_process_pool_scan(env):
    conn, workspace, folder, _ = env
    for name in ("a.xlsx", "b.xlsx", "c.xlsx"):
        build_workbook(folder / name)
    assert scan_workspace(conn, workspace, workers=2).records == 9


def test_encryption_round_trip(tmp_path):
    if not db.encryption_available():
        pytest.skip("sqlcipher no instalado")
    path = tmp_path / db.DB_NAME
    conn = db.connect(path)
    workspace = repo.get_or_create_workspace(conn, tmp_path)
    old_backup = db.backup(conn, path, 1)
    conn.close()
    key = db.enable_encryption(path, "clave-de-prueba")
    assert db.is_encrypted(path)
    assert not old_backup.exists()
    conn = db.connect(path, key)
    assert repo.workspace_folder(conn, workspace) == tmp_path
    new_backup = db.backup(conn, path, 2)
    assert db.is_encrypted(new_backup)
    conn.close()
    assert db.derive_key(path, "clave-de-prueba") == key
    with pytest.raises(Exception):
        db.connect(path, db.derive_key(path, "otra-clave"))


def _workspace_rows(conn) -> dict[str, int]:
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def test_delete_workspace_removes_everything_of_that_folder_only(env, tmp_path):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx")
    scan_workspace(conn, workspace, workers=1)
    before = _workspace_rows(conn)

    other_folder = tmp_path / "otra"
    other_folder.mkdir()
    build_workbook(other_folder / "b.xlsx")
    (other_folder / "roto.xlsx").write_bytes(b"no es un excel")
    other = repo.get_or_create_workspace(conn, other_folder)
    scan_workspace(conn, other, workers=1)
    version = repo.current_schema(conn, other).version_id
    field = repo.current_schema(conn, other).active_fields[0].id
    repo.add_alias(conn, other, "otro nombre", field)
    conn.execute("INSERT INTO field_role VALUES (?, 'key', ?)", (version, field))
    agent_config.get_settings(conn, other)
    record = conn.execute("SELECT id FROM record WHERE workspace_id = ? LIMIT 1", (other,)).fetchone()[0]
    conn.execute(
        "INSERT INTO record_edit(record_id, field_id, value, edited_at) VALUES (?, ?, 'x', '2026-01-01')", (record, field)
    )

    repo.delete_workspace(conn, other)

    assert _workspace_rows(conn) == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert count_records(conn, workspace) == 3


def test_removed_folder_can_be_added_again(env, tmp_path):
    conn, workspace, folder, _ = env
    build_workbook(folder / "a.xlsx")
    scan_workspace(conn, workspace, workers=1)
    repo.delete_workspace(conn, workspace)
    assert repo.last_workspace(conn) is None
    again = repo.get_or_create_workspace(conn, folder)
    scan_workspace(conn, again, workers=1)
    assert count_records(conn, again) == 3

