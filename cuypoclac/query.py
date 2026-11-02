"""Consultas sobre los registros: filtros y paginación sobre JSON en SQLite."""

import json
from dataclasses import dataclass, field

from .db.repo import check_field_id


@dataclass
class RecordQuery:
    filters: dict[str, str] = field(default_factory=dict)
    search: str = ""
    date_field: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    owner_field: str | None = None
    owner: str | None = None
    sort_field: str | None = None
    descending: bool = False
    limit: int = 200
    offset: int = 0


def _where(workspace_id: int, query: RecordQuery) -> tuple[str, list]:
    clauses = ["r.workspace_id = ?"]
    params: list = [workspace_id]
    for field_id, value in query.filters.items():
        clauses.append(f"json_extract(r.data, '$.{check_field_id(field_id)}') = ?")
        params.append(value)
    if query.search:
        clauses.append("EXISTS (SELECT 1 FROM json_each(r.data) j WHERE j.key NOT LIKE '\\_%' ESCAPE '\\' AND j.value LIKE ?)")
        params.append(f"%{query.search}%")
    if query.date_field:
        path = f"json_extract(r.data, '$.{check_field_id(query.date_field)}')"
        if query.date_from:
            clauses.append(f"{path} >= ?")
            params.append(query.date_from)
        if query.date_to:
            clauses.append(f"substr({path}, 1, 10) <= ?")
            params.append(query.date_to)
    if query.owner_field and query.owner:
        clauses.append(f"json_extract(r.data, '$.{check_field_id(query.owner_field)}') = ?")
        params.append(query.owner)
    return " AND ".join(clauses), params


def count_records(conn, workspace_id: int, query: RecordQuery | None = None) -> int:
    where, params = _where(workspace_id, query or RecordQuery())
    return conn.execute(f"SELECT COUNT(*) FROM record r WHERE {where}", params).fetchone()[0]


def query_records(conn, workspace_id: int, query: RecordQuery | None = None) -> list[dict]:
    query = query or RecordQuery()
    where, params = _where(workspace_id, query)
    order = "r.id"
    if query.sort_field:
        path = f"json_extract(r.data, '$.{check_field_id(query.sort_field)}')"
        order = f"{path} {'DESC' if query.descending else 'ASC'}, r.id"
    rows = conn.execute(
        f"""SELECT r.id, r.origin, r.status, r.source_row, r.origin_version, r.record_key, r.data, f.rel_path,
                   (SELECT COUNT(*) FROM record_edit e WHERE e.record_id = r.id) AS edits,
                   (SELECT COUNT(*) FROM record_edit e WHERE e.record_id = r.id AND e.status = 'conflict') AS conflicts
            FROM record r LEFT JOIN source_file f ON f.id = r.source_file_id
            WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, query.limit, query.offset],
    ).fetchall()
    return [{**dict(row), "data": json.loads(row["data"])} for row in rows]


def distinct_values(conn, workspace_id: int, field_id: str, limit: int = 500) -> list[str]:
    path = f"json_extract(data, '$.{check_field_id(field_id)}')"
    rows = conn.execute(
        f"SELECT DISTINCT {path} FROM record WHERE workspace_id = ? AND {path} IS NOT NULL ORDER BY 1 LIMIT ?",
        (workspace_id, limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


def value_counts(conn, workspace_id: int, field_id: str, limit: int = 20) -> list[tuple[str, int]]:
    path = f"json_extract(data, '$.{check_field_id(field_id)}')"
    rows = conn.execute(
        f"""SELECT COALESCE({path}, '(vacío)') AS label, COUNT(*) AS total FROM record
            WHERE workspace_id = ? GROUP BY label ORDER BY total DESC, label LIMIT ?""",
        (workspace_id, limit),
    ).fetchall()
    return [(str(row["label"]), row["total"]) for row in rows]


def workspace_summary(conn, workspace_id: int) -> dict:
    one = lambda sql: conn.execute(sql, (workspace_id,)).fetchone()[0]  # noqa: E731
    return {
        "files": one("SELECT COUNT(*) FROM source_file WHERE workspace_id = ? AND status != 'missing'"),
        "records": one("SELECT COUNT(*) FROM record WHERE workspace_id = ?"),
        "problems": one(
            """SELECT COUNT(*) FROM import_issue i JOIN source_file f ON f.id = i.source_file_id
               WHERE f.workspace_id = ?"""
        ),
        "pending": one("SELECT COUNT(*) FROM source_file WHERE workspace_id = ? AND status = 'pending_migration'"),
        "conflicts": one(
            """SELECT COUNT(*) FROM record_edit e JOIN record r ON r.id = e.record_id
               WHERE r.workspace_id = ? AND e.status = 'conflict'"""
        ),
    }
