"""Importa la carpeta: lee archivos, aplica la cadena de migraciones y conserva ediciones."""

import hashlib
import json
import time
import unicodedata
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, time as dt_time
from pathlib import Path

from openpyxl.utils.exceptions import InvalidFileException

from .. import db
from ..db import repo
from ..db.repo import Schema
from ..excel.dates import parse_date
from ..excel.reader import iter_rows
from ..excel.workbook import SUPPORTED_SUFFIXES
from ..migration.apply import migration_chain
from ..migration.ops import apply_chain, convert_value
from ..schema.discovery import DiscoveryError, discover
from ..schema.model import SchemaCandidate
from ..schema.normalize import normalize_label

_TOTAL_WORDS = {"total", "totales", "subtotal", "suma", "gran total", "promedio"}
_READ_RETRIES = 3
_READ_ERRORS = (OSError, DiscoveryError, InvalidFileException, zipfile.BadZipFile, KeyError, ValueError, IndexError)


@dataclass
class ParsedFile:
    candidate: SchemaCandidate
    samples: dict[str, list[str]]
    # (fila, {clave de campo candidato: [(índice de ítem, valor)]})
    rows: list[tuple[int, dict[str, list[tuple[int, object]]]]]


@dataclass
class ScanSummary:
    imported: int = 0
    unchanged: int = 0
    locked: int = 0
    errors: int = 0
    missing: int = 0
    records: int = 0
    pending: list[str] = dc_field(default_factory=list)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_locked(path: Path) -> bool:
    """Excel crea ~$ reemplazando las dos primeras letras del nombre, o anteponiéndolo."""
    return (
        path.with_name("~$" + path.name).exists()
        or path.with_name("~$" + path.name[2:]).exists()
    )


def list_source_files(folder: Path) -> dict[str, Path]:
    """Rutas relativas normalizadas: mismos separadores y misma forma Unicode en Win y Mac."""
    found = {}
    for path in folder.rglob("*"):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES or not path.is_file():
            continue
        relative = path.relative_to(folder)
        if any(part.startswith((".", "~$")) for part in relative.parts):
            continue
        found[unicodedata.normalize("NFC", relative.as_posix())] = path
    return found


def parse_file(path: Path) -> ParsedFile:
    """Función pura, se ejecuta en otro proceso. Reintenta por archivos en la nube."""
    last_error: Exception | None = None
    for attempt in range(_READ_RETRIES):
        try:
            return _parse(path)
        except OSError as error:
            last_error = error
            time.sleep(0.5 * (attempt + 1))
    raise last_error


def _parse(path: Path) -> ParsedFile:
    discovered = discover(path)
    candidate = discovered.candidate
    rows = []
    for row_number, values in iter_rows(path, candidate.sheet, candidate.first_data_row, discovered.merged):
        if all(value in (None, "") for value in values):
            continue
        if _is_total_row(values):
            continue
        mapped: dict[str, list[tuple[int, object]]] = {}
        for candidate_field in candidate.fields:
            cells = [
                (item, _plain(values[col - 1]))
                for item, col in candidate_field.columns
                if col <= len(values) and values[col - 1] not in (None, "")
            ]
            if cells:
                mapped[candidate_field.key] = cells
        if mapped:
            rows.append((row_number, mapped))
    return ParsedFile(candidate, discovered.samples, rows)


def _plain(value):
    return value.strip() if isinstance(value, str) else value


def _is_total_row(values: tuple) -> bool:
    first = next((value for value in values if value not in (None, "")), None)
    return isinstance(first, str) and normalize_label(first) in _TOTAL_WORDS


def normalize_value(field_def, value) -> tuple[object, str | None]:
    """Convierte un valor al tipo del campo. Si no encaja, conserva el original y avisa."""
    if isinstance(value, dt_time):
        value = value.isoformat()
    kind = field_def.type
    if kind == "date":
        parsed = parse_date(value)
        if parsed is None:
            return _to_json(value), f"«{value}» no es una fecha válida para «{field_def.label}»."
        return parsed, None
    if kind == "number":
        converted, ok = convert_value(value, "number")
        return (converted, None) if ok else (_to_json(value), f"«{value}» no es un número para «{field_def.label}».")
    if kind == "bool":
        converted, ok = convert_value(value, "bool")
        return (converted, None) if ok else (_to_json(value), f"«{value}» no es sí/no para «{field_def.label}».")
    text, _ = convert_value(_to_json(value), "text")
    if kind == "combo" and field_def.options:
        if text in field_def.options:
            return text, None
        normalized = normalize_label(text)
        for option in field_def.options:
            if normalize_label(option) == normalized:
                return option, None
        return text, f"«{text}» no es una opción de «{field_def.label}»."
    return text, None


def _to_json(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def find_version(conn, workspace_id: int, candidate: SchemaCandidate) -> int | None:
    row = conn.execute(
        "SELECT id FROM schema_version WHERE workspace_id = ? AND fingerprint = ?",
        (workspace_id, candidate.fingerprint),
    ).fetchone()
    if row:
        return row["id"]
    # Los alias permiten reconocer etiquetas antiguas sin crear otra versión.
    aliases = repo.aliases(conn, workspace_id)
    current = repo.current_schema(conn, workspace_id)
    if current is None:
        return None
    active = {f.id: f for f in current.active_fields}
    matched: set[str] = set()
    for field in candidate.fields:
        target = next(
            (f for f in current.active_fields if f.normalized_label == field.normalized_label), None
        ) or active.get(aliases.get(field.normalized_label, ""))
        if target is None or target.declared_type != field.declared_type or sorted(target.options) != sorted(field.options):
            return None
        matched.add(target.id)
    return current.version_id if matched == set(active) else None


def _field_map(schema: Schema, candidate: SchemaCandidate, aliases: dict[str, str]) -> dict[str, object]:
    active = schema.active_fields
    mapping: dict[str, object] = {}
    used: set[str] = set()
    by_key = {f.key: f for f in active}
    for field in candidate.fields:
        target = by_key.get(field.key)
        if target is not None:
            mapping[field.key] = target
            used.add(target.id)
    by_id = {f.id: f for f in active}
    for field in candidate.fields:
        if field.key in mapping:
            continue
        same_label = [f for f in active if f.normalized_label == field.normalized_label and f.id not in used]
        target = same_label[0] if len(same_label) == 1 else by_id.get(aliases.get(field.normalized_label, ""))
        if target is not None and target.id not in used:
            mapping[field.key] = target
            used.add(target.id)
    return mapping


def _build_records(parsed: ParsedFile, schema: Schema, aliases, issues: list[tuple]) -> list[tuple[int, dict]]:
    mapping = _field_map(schema, parsed.candidate, aliases)
    repeatable = {g.id: g for g in schema.groups if g.repeatable}
    head_only = any(g.repeat_mode == "repeated_rows" for g in repeatable.values())
    built: list[tuple[int, dict]] = []
    for row_number, cells in parsed.rows:
        data: dict = {}
        items: dict[str, dict[int, dict]] = defaultdict(dict)
        for key, values in cells.items():
            target = mapping.get(key)
            if target is None:
                continue
            for item, raw in values:
                value, issue = normalize_value(target, raw)
                if issue:
                    issues.append((row_number, target.id, "warning", "value", issue))
                if target.group_id in repeatable:
                    items[target.group_id].setdefault(item, {})[target.id] = value
                else:
                    data[target.id] = value
        for group_id, by_index in items.items():
            data[group_id] = [by_index[i] for i in sorted(by_index)]
        if head_only and built and not any(not k.startswith("g_") for k in data):
            # Fila de continuación: sus ítems pertenecen al registro anterior.
            previous = built[-1][1]
            for group_id in items:
                previous.setdefault(group_id, []).extend(data[group_id])
            continue
        built.append((row_number, data))
    return built


def _store_records(conn, workspace_id, file_id, version: int, built, key_fields, issues) -> None:
    now = db.now_iso()
    existing: dict[str, list] = defaultdict(list)
    without_key = []
    for row in conn.execute(
        "SELECT id, record_key FROM record WHERE source_file_id = ? ORDER BY source_row", (file_id,)
    ).fetchall():
        (existing[row["record_key"]] if row["record_key"] else without_key).append(row["id"])
    for record_id in without_key:
        conn.execute("DELETE FROM record WHERE id = ?", (record_id,))
    seen_in_file: dict[str, int] = defaultdict(int)
    for row_number, data in built:
        key = repo.key_for(data, key_fields)
        if key:
            seen_in_file[key] += 1
            if seen_in_file[key] == 2:
                issues.append((row_number, None, "warning", "duplicate_in_file", "La clave de este registro se repite dentro del archivo."))
        if key and existing.get(key):
            record_id = existing[key].pop(0)
            effective = _reconcile_edits(conn, record_id, data, row_number, issues)
            conn.execute(
                """UPDATE record SET source_row = ?, origin_version = ?, data = ?, imported_at = ?, status = 'active'
                   WHERE id = ?""",
                (row_number, version, json.dumps(effective, ensure_ascii=False), now, record_id),
            )
        else:
            conn.execute(
                """INSERT INTO record(workspace_id, source_file_id, source_row, origin_version, record_key, data, created_at, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, file_id, row_number, version, key, json.dumps(data, ensure_ascii=False), now, now),
            )
    for remaining in existing.values():
        for record_id in remaining:
            _retire_record(conn, record_id, file_id, issues)


def _retire_record(conn, record_id: int, file_id: int, issues: list[tuple] | None) -> None:
    """Sin ediciones el Excel manda y se elimina; con ediciones se conserva como huérfano."""
    has_edits = conn.execute("SELECT 1 FROM record_edit WHERE record_id = ? LIMIT 1", (record_id,)).fetchone()
    if not has_edits:
        conn.execute("DELETE FROM record WHERE id = ?", (record_id,))
        return
    conn.execute("UPDATE record SET status = 'orphaned' WHERE id = ?", (record_id,))
    if issues is not None:
        issues.append((None, None, "warning", "orphaned", "Una fila con ediciones ya no está en el archivo; se conservó el registro."))


def _reconcile_edits(conn, record_id: int, imported: dict, row_number: int, issues: list[tuple]) -> dict:
    effective = dict(imported)
    for edit in conn.execute("SELECT * FROM record_edit WHERE record_id = ?", (record_id,)).fetchall():
        field_id = edit["field_id"]
        value = json.loads(edit["value"]) if edit["value"] is not None else None
        base = json.loads(edit["base_value"]) if edit["base_value"] is not None else None
        incoming = imported.get(field_id)
        if incoming == value:
            conn.execute("DELETE FROM record_edit WHERE record_id = ? AND field_id = ?", (record_id, field_id))
            continue
        if incoming == base:
            conn.execute(
                "UPDATE record_edit SET status = 'active', incoming_value = NULL WHERE record_id = ? AND field_id = ?",
                (record_id, field_id),
            )
        else:
            conn.execute(
                "UPDATE record_edit SET status = 'conflict', incoming_value = ? WHERE record_id = ? AND field_id = ?",
                (json.dumps(incoming, ensure_ascii=False), record_id, field_id),
            )
            issues.append((row_number, field_id, "warning", "edit_conflict", "El archivo cambió un valor que se había editado en la app."))
        effective[field_id] = value
    return effective


def _set_file(conn, workspace_id, rel_path, file_hash_value, version_id, status, imported=False) -> int:
    conn.execute(
        """INSERT INTO source_file(workspace_id, rel_path, file_hash, schema_version_id, status, last_imported_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
             file_hash = excluded.file_hash,
             schema_version_id = COALESCE(excluded.schema_version_id, schema_version_id),
             status = excluded.status,
             last_imported_at = COALESCE(excluded.last_imported_at, last_imported_at)""",
        (workspace_id, rel_path, file_hash_value, version_id, status, db.now_iso() if imported else None),
    )
    return conn.execute(
        "SELECT id FROM source_file WHERE workspace_id = ? AND rel_path = ?", (workspace_id, rel_path)
    ).fetchone()["id"]


def _set_issues(conn, file_id: int, issues: list[tuple]) -> None:
    conn.execute("DELETE FROM import_issue WHERE source_file_id = ?", (file_id,))
    for row, field_id, severity, code, message in issues:
        conn.execute(
            "INSERT INTO import_issue(source_file_id, row, field_id, severity, code, message) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, row, field_id, severity, code, message),
        )


def _import_parsed(conn, workspace_id: int, rel_path: str, digest: str, parsed: ParsedFile, version_id: int) -> None:
    schema = repo.load_schema(conn, version_id)
    current = repo.current_schema(conn, workspace_id)
    issues: list[tuple] = [(None, None, "warning", "structure", w) for w in parsed.candidate.warnings]
    built = _build_records(parsed, schema, repo.aliases(conn, workspace_id), issues)
    chain = migration_chain(conn, workspace_id, schema.version)
    for _, data in built:
        apply_chain(data, chain)
    key_fields = repo.get_roles(conn, current.version_id).get("key", [])
    with db.transaction(conn):
        file_id = _set_file(conn, workspace_id, rel_path, digest, version_id, "ok", imported=True)
        hint = json.dumps(parsed.candidate.repeated_rows_hint, ensure_ascii=False) if parsed.candidate.repeated_rows_hint else None
        conn.execute("UPDATE source_file SET repeated_rows_hint = ? WHERE id = ?", (hint, file_id))
        _store_records(conn, workspace_id, file_id, schema.version, built, key_fields, issues)
        _set_issues(conn, file_id, issues)
        if issues:
            conn.execute("UPDATE source_file SET status = 'warnings' WHERE id = ?", (file_id,))


def _record_failure(conn, workspace_id, rel_path, digest, status, message) -> None:
    with db.transaction(conn):
        file_id = _set_file(conn, workspace_id, rel_path, digest, None, status)
        _set_issues(conn, file_id, [(None, None, "error", status, message)] if message else [])


def _handle_missing(conn, workspace_id: int, present: set[str], summary: ScanSummary) -> None:
    rows = conn.execute(
        "SELECT id, rel_path, status FROM source_file WHERE workspace_id = ? AND status != 'missing'", (workspace_id,)
    ).fetchall()
    for row in rows:
        if row["rel_path"] in present:
            continue
        with db.transaction(conn):
            for record in conn.execute("SELECT id FROM record WHERE source_file_id = ?", (row["id"],)).fetchall():
                _retire_record(conn, record["id"], row["id"], None)
            conn.execute("UPDATE source_file SET status = 'missing' WHERE id = ?", (row["id"],))
            _set_issues(conn, row["id"], [(None, None, "warning", "missing", "El archivo ya no está en la carpeta.")])
        summary.missing += 1


def refresh_duplicates(conn, workspace_id: int) -> None:
    """Una misma clave en varios orígenes solo genera advertencias: nunca sobrescribe."""
    with db.transaction(conn):
        affected = conn.execute(
            "SELECT DISTINCT source_file_id FROM import_issue i JOIN source_file f ON f.id = i.source_file_id "
            "WHERE i.code = 'duplicate' AND f.workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        conn.execute(
            "DELETE FROM import_issue WHERE code = 'duplicate' AND source_file_id IN "
            "(SELECT id FROM source_file WHERE workspace_id = ?)",
            (workspace_id,),
        )
        duplicates = conn.execute(
            """SELECT r.source_file_id, r.source_row FROM record r
               WHERE r.workspace_id = ? AND r.source_file_id IS NOT NULL AND r.record_key IN (
                 SELECT record_key FROM record WHERE workspace_id = ? AND record_key IS NOT NULL
                 GROUP BY record_key
                 HAVING COUNT(DISTINCT COALESCE(source_file_id, -id)) > 1
               )""",
            (workspace_id, workspace_id),
        ).fetchall()
        for row in duplicates:
            conn.execute(
                "INSERT INTO import_issue(source_file_id, row, severity, code, message) VALUES (?, ?, 'warning', 'duplicate', ?)",
                (row["source_file_id"], row["source_row"], "La clave de este registro también existe en otro origen."),
            )
            conn.execute(
                "UPDATE source_file SET status = 'warnings' WHERE id = ? AND status = 'ok'", (row["source_file_id"],)
            )
        for row in affected:
            remaining = conn.execute(
                "SELECT 1 FROM import_issue WHERE source_file_id = ? LIMIT 1", (row["source_file_id"],)
            ).fetchone()
            if not remaining:
                conn.execute(
                    "UPDATE source_file SET status = 'ok' WHERE id = ? AND status = 'warnings'",
                    (row["source_file_id"],),
                )


def scan_workspace(conn, workspace_id: int, *, workers: int = 0, progress=None) -> ScanSummary:
    folder = repo.workspace_folder(conn, workspace_id)
    summary = ScanSummary()
    if not folder.is_dir():
        raise FileNotFoundError(f"No se encontró la carpeta de trabajo: {folder}")
    files = list_source_files(folder)
    _handle_missing(conn, workspace_id, set(files), summary)

    known = {
        r["rel_path"]: r for r in conn.execute(
            "SELECT rel_path, file_hash, status FROM source_file WHERE workspace_id = ?", (workspace_id,)
        )
    }
    to_parse: list[tuple[str, Path, str]] = []
    for rel_path, path in sorted(files.items()):
        previous = known.get(rel_path)
        if is_locked(path):
            _record_failure(conn, workspace_id, rel_path, previous["file_hash"] if previous else "", "locked", None)
            summary.locked += 1
            continue
        try:
            digest = file_hash(path)
        except OSError as error:
            _record_failure(conn, workspace_id, rel_path, "", "error", f"No se pudo leer el archivo: {error.strerror or error}")
            summary.errors += 1
            continue
        retry = previous and previous["status"] in {"locked", "pending_migration", "error", "missing"}
        if previous and previous["file_hash"] == digest and not retry:
            summary.unchanged += 1
            continue
        to_parse.append((rel_path, path, digest))

    results = _parse_all([item[1] for item in to_parse], workers)
    for index, ((rel_path, _, digest), parsed) in enumerate(zip(to_parse, results), start=1):
        if progress:
            progress(index, len(to_parse), rel_path)
        if isinstance(parsed, Exception):
            _record_failure(conn, workspace_id, rel_path, digest, "error", _describe(parsed))
            summary.errors += 1
            continue
        version_id = find_version(conn, workspace_id, parsed.candidate)
        if version_id is None and repo.current_schema(conn, workspace_id) is None:
            with db.transaction(conn):
                version_id = repo.create_first_version(conn, workspace_id, parsed.candidate)
        if version_id is None:
            _record_failure(conn, workspace_id, rel_path, digest, "pending_migration", None)
            summary.pending.append(rel_path)
            continue
        _import_parsed(conn, workspace_id, rel_path, digest, parsed, version_id)
        summary.imported += 1
    refresh_duplicates(conn, workspace_id)
    summary.records = conn.execute(
        "SELECT COUNT(*) FROM record WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT rel_path FROM source_file WHERE workspace_id = ? AND status = 'pending_migration' ORDER BY rel_path",
        (workspace_id,),
    ).fetchall()
    summary.pending = [r["rel_path"] for r in pending]
    return summary


def _parse_all(paths: list[Path], workers: int) -> list:
    if not paths:
        return []
    if workers <= 0 or len(paths) == 1:
        return [_safe_parse(path) for path in paths]
    with ProcessPoolExecutor(max_workers=min(workers, len(paths))) as pool:
        return list(pool.map(_safe_parse, paths))


def _safe_parse(path: Path):
    try:
        return parse_file(path)
    except _READ_ERRORS as error:
        return RuntimeError(f"{type(error).__name__}: {error}")


def _describe(error: Exception) -> str:
    return f"No se pudo leer el archivo. {error}"


