"""Consultas del agente: solo campos compartidos, con la persona como seudónimo."""

import hashlib
import hmac
import json
import math
from dataclasses import dataclass

from ..db import repo
from ..db.repo import Schema
from ..migration.ops import convert_value
from ..query import workspace_summary
from ..schema.normalize import normalize_label
from .config import ROLE_HIGHER_BETTER, ROLE_LOWER_BETTER, ROLE_PERSON, ROLE_SHARED, get_settings

MAX_LIMIT = 100
_DEFAULT_RECORDS = 50
_DEFAULT_RANKING = 10
_MAX_GROUPS = 100
_NO_PERSON_WARNING = (
    "No hay un campo de persona definido: cada registro cuenta como una persona distinta. "
    "Avísalo al responder si la pregunta es sobre personas."
)


class AgentError(Exception):
    """Solicitud inválida. No distingue un campo oculto de uno inexistente."""


@dataclass
class _View:
    schema: Schema
    shared: dict
    metrics: dict[str, str]
    person: str | None
    secret: str


def _view(conn, workspace_id: int) -> _View:
    schema = repo.current_schema(conn, workspace_id)
    if schema is None:
        raise AgentError("Aún no hay datos importados.")
    roles = repo.get_roles(conn, schema.version_id)
    person = next(iter(roles.get(ROLE_PERSON, [])), None)
    repeatable = {g.id for g in schema.groups if g.repeatable}
    allowed = set(roles.get(ROLE_SHARED, [])) - {person}
    shared = {f.id: f for f in schema.active_fields if f.id in allowed and f.group_id not in repeatable}
    metrics = {f: "mayor_es_mejor" for f in roles.get(ROLE_HIGHER_BETTER, []) if f in shared}
    metrics |= {f: "menor_es_mejor" for f in roles.get(ROLE_LOWER_BETTER, []) if f in shared}
    return _View(schema, shared, metrics, person, get_settings(conn, workspace_id).pseudonym_secret)


def _pseudonym(view: _View, record_id: int, data: dict) -> str:
    value = data.get(view.person) if view.person else None
    if value in (None, ""):
        seed = f"registro:{record_id}"
    else:
        seed = f"persona:{normalize_label(value) or value}"
    digest = hmac.new(bytes.fromhex(view.secret), seed.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"P-{digest[:10].upper()}"


def _rows(conn, workspace_id: int, view: _View):
    for row in conn.execute("SELECT id, data FROM record WHERE workspace_id = ? ORDER BY id", (workspace_id,)):
        data = json.loads(row["data"])
        yield _pseudonym(view, row["id"], data), data


def _number(value) -> float | None:
    """Una columna con «NP» u otro texto queda como texto, pero sus números cuentan."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    converted, ok = convert_value(value, "number")
    # float() acepta «nan» e «inf», que romperían promedios y JSON.
    return converted if ok and math.isfinite(converted) else None


def _limit(body: dict, default: int) -> int:
    value = body.get("limite", default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentError("«limite» debe ser un entero positivo.")
    return min(value, MAX_LIMIT)


def _offset(body: dict) -> int:
    value = body.get("desplazamiento", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentError("«desplazamiento» debe ser un entero mayor o igual a cero.")
    return value


def _shared_field(view: _View, field_id):
    field = view.shared.get(field_id) if isinstance(field_id, str) else None
    if field is None:
        raise AgentError(f"Campo no disponible: {field_id}. Consulta /api/esquema.")
    return field


def _mapping(body: dict, name: str) -> dict:
    value = body.get(name, {})
    if not isinstance(value, dict):
        raise AgentError(f"«{name}» debe ser un objeto.")
    return value


def _predicate(body: dict, view: _View):
    equals = _mapping(body, "filtros")
    ranges = _mapping(body, "rangos")
    person = body.get("persona")
    checks = []
    for field_id, expected in equals.items():
        _shared_field(view, field_id)
        checks.append(lambda _, data, f=field_id, e=str(expected): str(data.get(f)) == e)
    for field_id, bounds in ranges.items():
        checks.append(_range_check(_shared_field(view, field_id), bounds))
    if person is not None:
        checks.append(lambda pseudonym, _, p=str(person): pseudonym == p)
    return lambda pseudonym, data: all(check(pseudonym, data) for check in checks)


def _range_check(field, bounds):
    if not isinstance(bounds, dict) or not set(bounds) <= {"desde", "hasta"}:
        raise AgentError("Cada rango debe tener «desde» o «hasta».")
    low, high = bounds.get("desde"), bounds.get("hasta")
    if field.type == "number":
        if any(b is not None and _number(b) is None for b in (low, high)):
            raise AgentError(f"Los límites de {field.id} deben ser números.")
        read = _number
    elif field.type == "date":
        if any(b is not None and not isinstance(b, str) for b in (low, high)):
            raise AgentError(f"Los límites de {field.id} deben ser fechas AAAA-MM-DD.")
        read = lambda value: value[:10] if isinstance(value, str) else None  # noqa: E731
    else:
        raise AgentError(f"{field.id} no admite rangos: solo números y fechas.")

    def check(_, data) -> bool:
        value = read(data.get(field.id))
        return value is not None and (low is None or value >= low) and (high is None or value <= high)

    return check


def _warnings(view: _View) -> list[str]:
    return [] if view.person else [_NO_PERSON_WARNING]


def status(conn, workspace_id: int) -> dict:
    view = _view(conn, workspace_id)
    summary = workspace_summary(conn, workspace_id)
    return {
        "version_esquema": view.schema.version,
        "archivos": summary["files"],
        "registros": summary["records"],
        "problemas_de_importacion": summary["problems"],
        "archivos_con_migracion_pendiente": summary["pending"],
        "campos_compartidos": len(view.shared),
        "metricas": len(view.metrics),
        "hay_campo_persona": view.person is not None,
    }


def schema_info(conn, workspace_id: int) -> dict:
    view = _view(conn, workspace_id)
    groups = {g.id: g.label for g in view.schema.groups}
    return {
        "campos": [
            {
                "id": field.id,
                "etiqueta": field.label,
                "grupo": groups.get(field.group_id),
                "tipo": field.type,
                "opciones": field.options,
                "metrica": view.metrics.get(field.id),
            }
            for field in view.shared.values()
        ],
        "hay_campo_persona": view.person is not None,
        "advertencias": _warnings(view),
    }


def list_records(conn, workspace_id: int, body: dict) -> dict:
    view = _view(conn, workspace_id)
    matches = _predicate(body, view)
    limit, offset = _limit(body, _DEFAULT_RECORDS), _offset(body)
    order = body.get("orden")
    if order is not None:
        _shared_field(view, order)
    found = [
        {"persona": pseudonym, **{f: data[f] for f in view.shared if f in data}}
        for pseudonym, data in _rows(conn, workspace_id, view)
        if matches(pseudonym, data)
    ]
    if order is not None:
        present = [r for r in found if r.get(order) is not None]
        missing = [r for r in found if r.get(order) is None]
        present.sort(key=lambda r: (type(r[order]).__name__, r[order]), reverse=bool(body.get("descendente")))
        found = present + missing
    return {"total": len(found), "registros": found[offset:offset + limit], "advertencias": _warnings(view)}


def ranking(conn, workspace_id: int, body: dict) -> dict:
    view = _view(conn, workspace_id)
    metric = body.get("metrica")
    field = _shared_field(view, metric)
    direction = view.metrics.get(metric)
    if direction is None:
        raise AgentError(f"{metric} no está definida como métrica. Consulta /api/esquema.")
    side = body.get("orden", "peores")
    if side not in ("peores", "mejores"):
        raise AgentError("«orden» debe ser «peores» o «mejores».")
    matches = _predicate(body, view)
    values: dict[str, list] = {}
    excluded = 0
    for pseudonym, data in _rows(conn, workspace_id, view):
        if not matches(pseudonym, data):
            continue
        number = _number(data.get(metric))
        if number is None:
            excluded += 1
        else:
            values.setdefault(pseudonym, []).append(number)
    people = [
        {
            "persona": person,
            "promedio": round(sum(numbers) / len(numbers), 6),
            "registros": len(numbers),
            "minimo": min(numbers),
            "maximo": max(numbers),
        }
        for person, numbers in values.items()
    ]
    # «Peores» sale primero: el valor más bajo si mayor es mejor, y al revés.
    descending = (direction == "mayor_es_mejor") == (side == "mejores")
    people.sort(key=lambda p: (-p["promedio"] if descending else p["promedio"], p["persona"]))
    position = 0
    previous = None
    for index, person in enumerate(people, start=1):
        if person["promedio"] != previous:
            position, previous = index, person["promedio"]
        person["posicion"] = position
    return {
        "metrica": {"id": field.id, "etiqueta": field.label, "direccion": direction},
        "orden": side,
        "total_personas": len(people),
        "registros_sin_valor_numerico": excluded,
        "personas": people[:_limit(body, _DEFAULT_RANKING)],
        "advertencias": _warnings(view),
    }


def aggregate(conn, workspace_id: int, body: dict) -> dict:
    view = _view(conn, workspace_id)
    group_by = body.get("agrupar_por")
    if group_by is not None:
        _shared_field(view, group_by)
    metric = body.get("metrica")
    field = _shared_field(view, metric) if metric is not None else None
    if field is not None and field.type != "number" and metric not in view.metrics:
        raise AgentError(f"{metric} no es numérico.")
    matches = _predicate(body, view)
    groups: dict[str, dict] = {}
    for pseudonym, data in _rows(conn, workspace_id, view):
        if not matches(pseudonym, data):
            continue
        label = str(data[group_by]) if group_by and data.get(group_by) is not None else ("(vacío)" if group_by else "(todos)")
        group = groups.setdefault(label, {"registros": 0, "people": set(), "numbers": []})
        group["registros"] += 1
        group["people"].add(pseudonym)
        number = _number(data.get(metric)) if metric else None
        if number is not None:
            group["numbers"].append(number)
    result = []
    for label, group in groups.items():
        entry = {"valor": label, "registros": group["registros"], "personas": len(group["people"])}
        if metric:
            numbers = group["numbers"]
            entry |= {
                "con_valor": len(numbers),
                "promedio": round(sum(numbers) / len(numbers), 6) if numbers else None,
                "minimo": min(numbers) if numbers else None,
                "maximo": max(numbers) if numbers else None,
            }
        result.append(entry)
    result.sort(key=lambda e: (-e["registros"], e["valor"]))
    return {
        "agrupado_por": group_by,
        "metrica": metric,
        "total_grupos": len(result),
        "grupos": result[:_MAX_GROUPS],
        "advertencias": _warnings(view),
    }


def resolve_pseudonym(conn, workspace_id: int, pseudonym: str) -> list[dict]:
    """Solo para la pantalla local: nunca se expone por la API."""
    view = _view(conn, workspace_id)
    wanted = pseudonym.strip().upper()
    fields = [f for f in view.schema.active_fields if f.group_id not in {g.id for g in view.schema.groups if g.repeatable}]
    return [
        {f.label: data[f.id] for f in fields if data.get(f.id) not in (None, "")}
        for current, data in _rows(conn, workspace_id, view)
        if current == wanted
    ]
