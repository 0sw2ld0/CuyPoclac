"""Propone y aplica migraciones: siempre con backup y dentro de una transacción."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..db import repo
from ..db.repo import Schema
from ..schema.diff import Proposal, diff_schemas
from ..schema.discovery import discover
from .ops import apply_chain, apply_ops_to_schema, compile_ops, sort_ops

logger = logging.getLogger(__name__)


@dataclass
class MigrationPlan:
    rel_path: str
    fingerprint: str
    proposals: list[Proposal]


def _samples(conn, workspace_id: int, schema: Schema, limit: int = 500) -> dict[str, list[str]]:
    collected: dict[str, set[str]] = {f.key: set() for f in schema.active_fields}
    by_id = {f.id: f for f in schema.active_fields}
    for row in conn.execute("SELECT data FROM record WHERE workspace_id = ? LIMIT ?", (workspace_id, limit)):
        data = json.loads(row["data"])
        for key, value in data.items():
            if key in by_id and value is not None:
                collected[by_id[key].key].add(str(value))
            elif isinstance(value, list):
                for item in value:
                    for sub_key, sub_value in (item.items() if isinstance(item, dict) else ()):
                        if sub_key in by_id and sub_value is not None:
                            collected[by_id[sub_key].key].add(str(sub_value))
    return {key: sorted(values) for key, values in collected.items()}


def propose(conn, workspace_id: int, rel_path: str) -> MigrationPlan:
    schema = repo.current_schema(conn, workspace_id)
    folder = repo.workspace_folder(conn, workspace_id)
    discovered = discover(folder / rel_path)
    proposals = diff_schemas(
        schema.fields, schema.groups, discovered.candidate,
        _samples(conn, workspace_id, schema), discovered.samples,
        repo.aliases(conn, workspace_id),
        repo.next_field_id_factory(conn, workspace_id, "f_"),
        repo.next_field_id_factory(conn, workspace_id, "g_"),
    )
    return MigrationPlan(rel_path, discovered.candidate.fingerprint, proposals)


def resolve_ops(plan: MigrationPlan, rejected: set[int], manual_ops: list[dict] | None = None) -> list[dict]:
    """Une lo aceptado con las alternativas de lo rechazado. Lo obligatorio no se rechaza."""
    ops: list[dict] = []
    for index, proposal in enumerate(plan.proposals):
        if index in rejected and not proposal.required:
            ops.extend(proposal.alternative)
        else:
            ops.append(proposal.op)
    return sort_ops(ops + (manual_ops or []))


def apply_migration(conn, db_path: Path, workspace_id: int, ops: list[dict], fingerprint: str | None) -> Path:
    """Crea la versión N+1 y migra registros y ediciones. Devuelve la ruta del backup."""
    schema = repo.current_schema(conn, workspace_id)
    backup_path = db.backup(conn, db_path, schema.version)
    next_version = schema.version + 1
    # Una migración manual no viene de un archivo: su huella no debe chocar con la vigente.
    final_fingerprint = fingerprint or _manual_fingerprint(conn, schema, next_version)
    compiled = compile_ops(schema, ops)
    with db.transaction(conn):
        updated = apply_ops_to_schema(schema, ops)
        version_id = repo.save_schema_version(conn, workspace_id, next_version, final_fingerprint, updated)
        previous_labels = {f.id: f.label for f in schema.fields}
        for op in ops:
            if op["op"] == "rename":
                repo.add_alias(conn, workspace_id, previous_labels[op["field"]], op["field"])
                repo.add_alias(conn, workspace_id, op["label"], op["field"])
        active_ids = {f.id for f in updated.active_fields}
        roles = {
            role: [f for f in fields if f in active_ids]
            for role, fields in repo.get_roles(conn, schema.version_id).items()
        }
        repo.replace_roles(conn, version_id, roles)
        conn.execute(
            "INSERT INTO migration(workspace_id, from_version, to_version, ops_json, applied_at) VALUES (?, ?, ?, ?, ?)",
            (workspace_id, schema.version, next_version, json.dumps(ops, ensure_ascii=False), db.now_iso()),
        )
        for row in conn.execute("SELECT id, data FROM record WHERE workspace_id = ?", (workspace_id,)).fetchall():
            before = row["data"]
            data = apply_chain(json.loads(before), compiled)
            after = json.dumps(data, ensure_ascii=False)
            if after != before:
                conn.execute("UPDATE record SET data = ? WHERE id = ?", (after, row["id"]))
        _migrate_edits(conn, workspace_id, compiled)
        repo.recompute_keys(conn, workspace_id, roles.get("key", []))
        repo.rebuild_filter_indexes(conn, version_id)
    logger.info("Migración aplicada: versión %d a %d, %d operaciones", schema.version, next_version, len(ops))
    return backup_path


def _manual_fingerprint(conn, schema: Schema, version: int) -> str:
    current = conn.execute("SELECT fingerprint FROM schema_version WHERE id = ?", (schema.version_id,)).fetchone()[0]
    return f"{current}+manual{version}"


def _migrate_edits(conn, workspace_id: int, compiled) -> None:
    """Cada edición se transforma igual que su registro. Un split genera varias ediciones."""
    rows = conn.execute(
        """SELECT e.* FROM record_edit e JOIN record r ON r.id = e.record_id
           WHERE r.workspace_id = ?""",
        (workspace_id,),
    ).fetchall()
    for row in rows:
        results = {}
        for column in ("value", "base_value", "incoming_value"):
            if row[column] is None:
                results[column] = {}
                continue
            results[column] = apply_chain({row["field_id"]: json.loads(row[column])}, compiled)
        keys = {k for result in results.values() for k in result if not k.startswith("_")} or {row["field_id"]}
        conn.execute(
            "DELETE FROM record_edit WHERE record_id = ? AND field_id = ?", (row["record_id"], row["field_id"])
        )
        for key in keys:
            encoded = {
                column: json.dumps(result[key], ensure_ascii=False) if key in result else None
                for column, result in results.items()
            }
            conn.execute(
                """INSERT OR REPLACE INTO record_edit(record_id, field_id, value, base_value, incoming_value, status, edited_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["record_id"], key, encoded["value"], encoded["base_value"],
                    encoded["incoming_value"], row["status"], row["edited_at"],
                ),
            )


def migration_chain(conn, workspace_id: int, from_version: int) -> list[dict]:
    """Operaciones compiladas para llevar datos de una versión antigua a la vigente."""
    compiled = []
    rows = conn.execute(
        "SELECT from_version, ops_json FROM migration WHERE workspace_id = ? AND from_version >= ? ORDER BY from_version",
        (workspace_id, from_version),
    ).fetchall()
    for row in rows:
        version = conn.execute(
            "SELECT id FROM schema_version WHERE workspace_id = ? AND version = ?",
            (workspace_id, row["from_version"]),
        ).fetchone()
        compiled.extend(compile_ops(repo.load_schema(conn, version["id"]), json.loads(row["ops_json"])))
    return compiled
