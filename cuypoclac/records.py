"""Ediciones y altas manuales. Los archivos de origen nunca se tocan."""

import json

from . import db
from .db import repo
from .importing.importer import normalize_value, refresh_duplicates


class RecordError(Exception):
    pass


def _dump(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _load(value):
    return None if value is None else json.loads(value)


def editable_fields(conn, workspace_id: int, record_id: int) -> set[str]:
    """Los campos clave de un registro importado no se editan: emparejan las filas al reimportar."""
    schema = repo.current_schema(conn, workspace_id)
    ids = {f.id for f in schema.active_fields} | {g.id for g in schema.active_groups if g.repeatable}
    row = conn.execute("SELECT origin FROM record WHERE id = ?", (record_id,)).fetchone()
    if row and row["origin"] == "import":
        ids -= set(repo.get_roles(conn, schema.version_id).get("key", []))
    return ids


def _clean(conn, workspace_id: int, changes: dict) -> tuple[dict, list[str]]:
    schema = repo.current_schema(conn, workspace_id)
    by_id = {f.id: f for f in schema.active_fields}
    cleaned, issues = {}, []
    for field_id, value in changes.items():
        target = by_id.get(field_id)
        if isinstance(value, list):
            items = []
            for item in value:
                clean_item, item_issues = _clean(conn, workspace_id, item)
                issues.extend(item_issues)
                clean_item = {k: v for k, v in clean_item.items() if v is not None}
                if clean_item:
                    items.append(clean_item)
            cleaned[field_id] = items or None
            continue
        if target is None or value in (None, ""):
            cleaned[field_id] = None if value == "" else value
            continue
        normalized, issue = normalize_value(target, value)
        if issue:
            issues.append(issue)
        cleaned[field_id] = normalized
    return cleaned, issues


def create_manual(conn, workspace_id: int, values: dict) -> int:
    schema = repo.current_schema(conn, workspace_id)
    if schema is None:
        raise RecordError("Aún no existe un esquema: importa primero un archivo.")
    cleaned, issues = _clean(conn, workspace_id, values)
    if issues:
        raise RecordError("\n".join(issues))
    data = {k: v for k, v in cleaned.items() if v is not None}
    key_fields = repo.get_roles(conn, schema.version_id).get("key", [])
    return conn.execute(
        """INSERT INTO record(workspace_id, origin, origin_version, record_key, data, created_at)
           VALUES (?, 'manual', ?, ?, ?, ?)""",
        (workspace_id, schema.version, repo.key_for(data, key_fields), json.dumps(data, ensure_ascii=False), db.now_iso()),
    ).lastrowid


def update_record(conn, workspace_id: int, record_id: int, changes: dict) -> None:
    row = conn.execute(
        "SELECT origin, data FROM record WHERE id = ? AND workspace_id = ?", (record_id, workspace_id)
    ).fetchone()
    if row is None:
        raise RecordError("El registro ya no existe.")
    cleaned, issues = _clean(conn, workspace_id, changes)
    if issues:
        raise RecordError("\n".join(issues))
    data = json.loads(row["data"])
    schema = repo.current_schema(conn, workspace_id)
    roles = repo.get_roles(conn, schema.version_id)
    if row["origin"] == "manual":
        data.update(cleaned)
        data = {k: v for k, v in data.items() if v is not None}
        with db.transaction(conn):
            conn.execute(
                "UPDATE record SET data = ?, record_key = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), repo.key_for(data, roles.get("key", [])), record_id),
            )
        return
    if not roles.get("key"):
        raise RecordError("Para editar registros importados, primero define el rol «clave» en Esquema.")
    allowed = editable_fields(conn, workspace_id, record_id)
    now = db.now_iso()
    with db.transaction(conn):
        for field_id, value in cleaned.items():
            if value == data.get(field_id):
                continue
            if field_id not in allowed:
                raise RecordError("Los campos clave de un registro importado no se pueden editar.")
            edit = conn.execute(
                "SELECT base_value, incoming_value, status FROM record_edit WHERE record_id = ? AND field_id = ?",
                (record_id, field_id),
            ).fetchone()
            base = _load(edit["base_value"]) if edit else data.get(field_id)
            # Volver al valor del archivo equivale a deshacer la edición.
            reference = _load(edit["incoming_value"]) if edit and edit["status"] == "conflict" else base
            if value == reference:
                conn.execute("DELETE FROM record_edit WHERE record_id = ? AND field_id = ?", (record_id, field_id))
            else:
                conn.execute(
                    """INSERT INTO record_edit(record_id, field_id, value, base_value, status, edited_at)
                       VALUES (?, ?, ?, ?, 'active', ?)
                       ON CONFLICT(record_id, field_id) DO UPDATE SET value = excluded.value,
                       status = record_edit.status, edited_at = excluded.edited_at""",
                    (record_id, field_id, _dump(value), _dump(base), now),
                )
            data[field_id] = value
        conn.execute(
            "UPDATE record SET data = ? WHERE id = ?",
            (json.dumps({k: v for k, v in data.items() if v is not None}, ensure_ascii=False), record_id),
        )


def record_edits(conn, record_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM record_edit WHERE record_id = ? ORDER BY field_id", (record_id,)).fetchall()
    return [
        {
            "field_id": r["field_id"], "value": _load(r["value"]), "base_value": _load(r["base_value"]),
            "incoming_value": _load(r["incoming_value"]), "status": r["status"], "edited_at": r["edited_at"],
        }
        for r in rows
    ]


def resolve_conflict(conn, record_id: int, field_id: str, keep_edit: bool) -> None:
    edit = conn.execute(
        "SELECT * FROM record_edit WHERE record_id = ? AND field_id = ? AND status = 'conflict'", (record_id, field_id)
    ).fetchone()
    if edit is None:
        raise RecordError("Ese conflicto ya no existe.")
    with db.transaction(conn):
        if keep_edit:
            conn.execute(
                """UPDATE record_edit SET status = 'active', base_value = incoming_value, incoming_value = NULL
                   WHERE record_id = ? AND field_id = ?""",
                (record_id, field_id),
            )
            return
        row = conn.execute("SELECT data FROM record WHERE id = ?", (record_id,)).fetchone()
        data = json.loads(row["data"])
        incoming = _load(edit["incoming_value"])
        if incoming is None:
            data.pop(field_id, None)
        else:
            data[field_id] = incoming
        conn.execute("UPDATE record SET data = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), record_id))
        conn.execute("DELETE FROM record_edit WHERE record_id = ? AND field_id = ?", (record_id, field_id))


def delete_manual(conn, record_id: int) -> None:
    row = conn.execute("SELECT origin FROM record WHERE id = ?", (record_id,)).fetchone()
    if row is None or row["origin"] != "manual":
        raise RecordError("Solo se pueden eliminar registros creados en la app.")
    conn.execute("DELETE FROM record WHERE id = ?", (record_id,))


def set_roles(conn, workspace_id: int, roles: dict[str, list[str]]) -> None:
    schema = repo.current_schema(conn, workspace_id)
    current = repo.get_roles(conn, schema.version_id)
    if current.get("key") and current.get("key") != roles.get("key"):
        has_edits = conn.execute(
            """SELECT 1 FROM record_edit e JOIN record r ON r.id = e.record_id
               WHERE r.workspace_id = ? LIMIT 1""",
            (workspace_id,),
        ).fetchone()
        if has_edits:
            raise RecordError("No se puede cambiar la clave mientras existan ediciones: dependen de ella.")
    with db.transaction(conn):
        repo.replace_roles(conn, schema.version_id, roles)
        repo.recompute_keys(conn, workspace_id, roles.get("key", []))
        repo.rebuild_filter_indexes(conn, schema.version_id)
    refresh_duplicates(conn, workspace_id)


def set_field_type(conn, workspace_id: int, field_id: str, type_: str) -> None:
    schema = repo.current_schema(conn, workspace_id)
    conn.execute(
        "UPDATE field SET type = ?, type_source = 'user' WHERE id = ? AND schema_version_id = ?",
        (type_, field_id, schema.version_id),
    )


def confirm_repeated_rows(conn, workspace_id: int, head_keys: list[str], label: str) -> None:
    """Agrupa como repetibles los campos que no son cabecera y vuelve a importar los archivos."""
    schema = repo.current_schema(conn, workspace_id)
    new_group = repo.next_field_id_factory(conn, workspace_id, "g_")()
    head = set(head_keys)
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO field_group(id, schema_version_id, label, ord, repeatable, repeat_mode)
               VALUES (?, ?, ?, ?, 1, 'repeated_rows')""",
            (new_group, schema.version_id, label, len(schema.groups) + 1),
        )
        for target in schema.active_fields:
            if target.key not in head and target.group_id is None:
                conn.execute(
                    "UPDATE field SET group_id = ? WHERE id = ? AND schema_version_id = ?",
                    (new_group, target.id, schema.version_id),
                )
        conn.execute(
            "UPDATE source_file SET status = 'error', repeated_rows_hint = NULL WHERE workspace_id = ?",
            (workspace_id,),
        )
