"""Lectura y escritura de metadatos: workspaces, versiones, campos y roles."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..schema.diff import FieldDef, GroupDef
from ..schema.model import SchemaCandidate
from ..schema.normalize import normalize_label
from . import now_iso

ROLES = ("key", "date", "owner", "filterable")
_FIELD_ID = re.compile(r"^[fg]_\d+$")


def check_field_id(field_id: str) -> str:
    """Los ids terminan dentro de rutas JSON y nombres de índice."""
    if not _FIELD_ID.match(field_id):
        raise ValueError(f"Id de campo inválido: {field_id}")
    return field_id


@dataclass
class Schema:
    version_id: int
    version: int
    fields: list[FieldDef]
    groups: list[GroupDef]

    @property
    def active_fields(self) -> list[FieldDef]:
        return [f for f in self.fields if f.status == "active"]

    @property
    def active_groups(self) -> list[GroupDef]:
        return [g for g in self.groups if g.status == "active"]

    def group(self, group_id: str | None) -> GroupDef | None:
        return next((g for g in self.groups if g.id == group_id), None)


def get_or_create_workspace(conn, folder: Path) -> int:
    path = str(folder.resolve())
    row = conn.execute("SELECT id FROM workspace WHERE folder_path = ?", (path,)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO workspace(folder_path, created_at) VALUES (?, ?)", (path, now_iso())
    ).lastrowid


def workspace_folder(conn, workspace_id: int) -> Path:
    return Path(conn.execute("SELECT folder_path FROM workspace WHERE id = ?", (workspace_id,)).fetchone()[0])


def last_workspace(conn) -> tuple[int, Path] | None:
    row = conn.execute("SELECT id, folder_path FROM workspace ORDER BY id DESC LIMIT 1").fetchone()
    return (row["id"], Path(row["folder_path"])) if row else None


def load_schema(conn, version_id: int) -> Schema:
    version = conn.execute("SELECT version FROM schema_version WHERE id = ?", (version_id,)).fetchone()["version"]
    groups = [
        GroupDef(
            id=r["id"], key=normalize_label(r["label"]), label=r["label"],
            repeatable=bool(r["repeatable"]), repeat_mode=r["repeat_mode"], status=r["status"],
        )
        for r in conn.execute("SELECT * FROM field_group WHERE schema_version_id = ? ORDER BY ord", (version_id,))
    ]
    keys = {g.id: g.key for g in groups}
    fields = [
        FieldDef(
            id=r["id"], label=r["label"], normalized_label=r["normalized_label"],
            group_key=keys.get(r["group_id"]), group_id=r["group_id"], type=r["type"],
            declared_type=r["declared_type"], options=json.loads(r["options_json"] or "[]"),
            ord=r["ord"], status=r["status"],
        )
        for r in conn.execute("SELECT * FROM field WHERE schema_version_id = ? ORDER BY ord", (version_id,))
    ]
    return Schema(version_id, version, fields, groups)


def current_schema(conn, workspace_id: int) -> Schema | None:
    row = conn.execute(
        "SELECT id FROM schema_version WHERE workspace_id = ? AND is_current = 1", (workspace_id,)
    ).fetchone()
    return load_schema(conn, row["id"]) if row else None


def create_first_version(conn, workspace_id: int, candidate: SchemaCandidate) -> int:
    version_id = conn.execute(
        "INSERT INTO schema_version(workspace_id, version, fingerprint, is_current, created_at) VALUES (?, 1, ?, 1, ?)",
        (workspace_id, candidate.fingerprint, now_iso()),
    ).lastrowid
    group_ids = {}
    for i, group in enumerate(candidate.groups, start=1):
        group_ids[group.key] = f"g_{i:02d}"
        conn.execute(
            "INSERT INTO field_group(id, schema_version_id, label, ord, repeatable, repeat_mode) VALUES (?, ?, ?, ?, ?, ?)",
            (group_ids[group.key], version_id, group.label, i, int(group.repeatable), group.repeat_mode),
        )
    for i, field in enumerate(candidate.fields, start=1):
        conn.execute(
            """INSERT INTO field(id, schema_version_id, group_id, label, normalized_label, type,
               type_source, declared_type, options_json, ord) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"f_{i:02d}", version_id, group_ids.get(field.group), field.label, field.normalized_label,
                field.type, field.type_source, field.declared_type,
                json.dumps(field.options, ensure_ascii=False), field.ord,
            ),
        )
    return version_id


def save_schema_version(conn, workspace_id: int, version: int, fingerprint: str, schema: Schema) -> int:
    conn.execute("UPDATE schema_version SET is_current = 0 WHERE workspace_id = ?", (workspace_id,))
    version_id = conn.execute(
        "INSERT INTO schema_version(workspace_id, version, fingerprint, is_current, created_at) VALUES (?, ?, ?, 1, ?)",
        (workspace_id, version, fingerprint, now_iso()),
    ).lastrowid
    for i, group in enumerate(schema.groups, start=1):
        conn.execute(
            "INSERT INTO field_group(id, schema_version_id, label, ord, repeatable, repeat_mode, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group.id, version_id, group.label, i, int(group.repeatable), group.repeat_mode, group.status),
        )
    for field in schema.fields:
        conn.execute(
            """INSERT INTO field(id, schema_version_id, group_id, label, normalized_label, type,
               declared_type, options_json, ord, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                field.id, version_id, field.group_id, field.label, field.normalized_label, field.type,
                field.declared_type, json.dumps(field.options, ensure_ascii=False), field.ord, field.status,
            ),
        )
    return version_id


def next_field_id_factory(conn, workspace_id: int, prefix: str):
    table = "field" if prefix == "f_" else "field_group"
    highest = 0
    query = f"""SELECT t.id FROM {table} t JOIN schema_version v ON v.id = t.schema_version_id
                WHERE v.workspace_id = ?"""
    for row in conn.execute(query, (workspace_id,)):
        highest = max(highest, int(row[0].split("_")[1]))
    state = {"n": highest}

    def next_id() -> str:
        state["n"] += 1
        return f"{prefix}{state['n']:02d}"

    return next_id


def aliases(conn, workspace_id: int) -> dict[str, str]:
    return {
        r["normalized_label"]: r["field_id"]
        for r in conn.execute("SELECT normalized_label, field_id FROM field_alias WHERE workspace_id = ?", (workspace_id,))
    }


def add_alias(conn, workspace_id: int, label: str, field_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO field_alias(workspace_id, normalized_label, field_id) VALUES (?, ?, ?)",
        (workspace_id, normalize_label(label), field_id),
    )


def get_roles(conn, version_id: int) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {role: [] for role in ROLES}
    for row in conn.execute(
        "SELECT role, field_id FROM field_role WHERE schema_version_id = ? ORDER BY rowid", (version_id,)
    ):
        roles.setdefault(row["role"], []).append(row["field_id"])
    return roles


def replace_roles(conn, version_id: int, roles: dict[str, list[str]]) -> None:
    conn.execute("DELETE FROM field_role WHERE schema_version_id = ?", (version_id,))
    for role, field_ids in roles.items():
        for field_id in field_ids:
            conn.execute(
                "INSERT OR IGNORE INTO field_role(schema_version_id, role, field_id) VALUES (?, ?, ?)",
                (version_id, role, check_field_id(field_id)),
            )


def rebuild_filter_indexes(conn, version_id: int) -> None:
    """Crea índices sobre los campos filtrables y elimina los que ya no lo son."""
    wanted = set(get_roles(conn, version_id).get("filterable", []))
    existing = [
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_rec_f_%'"
        )
    ]
    for name in existing:
        if name.removeprefix("idx_rec_") not in wanted:
            conn.execute(f"DROP INDEX {name}")
    for field_id in wanted:
        check_field_id(field_id)
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_rec_{field_id} ON record(json_extract(data, '$.{field_id}'))"
        )


def key_for(data: dict, key_fields: list[str]) -> str | None:
    if not key_fields:
        return None
    parts = []
    for field_id in key_fields:
        value = data.get(field_id)
        if value in (None, ""):
            return None
        parts.append(str(value))
    return "\x1f".join(parts)


def recompute_keys(conn, workspace_id: int, key_fields: list[str]) -> None:
    for row in conn.execute("SELECT id, data FROM record WHERE workspace_id = ?", (workspace_id,)).fetchall():
        conn.execute(
            "UPDATE record SET record_key = ? WHERE id = ?",
            (key_for(json.loads(row["data"]), key_fields), row["id"]),
        )
