"""Operaciones declarativas: se aplican igual al esquema y a los registros."""

import copy
from dataclasses import dataclass, field

from ..db.repo import Schema
from ..excel.dates import parse_date
from ..schema.diff import FieldDef, GroupDef
from ..schema.normalize import normalize_label

ORDER = {
    "add_group": 0, "rename_group": 1, "rename": 2, "move_field": 3, "add_field": 4, "set_options": 5,
    "map_values": 6, "convert": 7, "split": 8, "merge": 9, "archive_field": 10,
}
UNMIGRATED = "_unmigrated"
ORIGINAL = "_original"


def sort_ops(ops: list[dict]) -> list[dict]:
    return sorted(ops, key=lambda op: ORDER.get(op["op"], 99))


def apply_ops_to_schema(schema: Schema, ops: list[dict]) -> Schema:
    fields = {f.id: copy.copy(f) for f in schema.fields}
    groups = {g.id: copy.copy(g) for g in schema.groups}
    next_ord = max((f.ord for f in fields.values()), default=-1) + 1
    for op in ops:
        kind = op["op"]
        if kind == "add_group":
            groups[op["id"]] = GroupDef(
                id=op["id"], key=normalize_label(op["label"]), label=op["label"],
                repeatable=bool(op.get("repeatable")), repeat_mode=op.get("repeat_mode"),
            )
        elif kind == "rename_group":
            groups[op["group"]].label = op["label"]
            groups[op["group"]].key = normalize_label(op["label"])
        elif kind == "rename":
            fields[op["field"]].label = op["label"]
            fields[op["field"]].normalized_label = normalize_label(op["label"])
        elif kind == "add_field":
            fields[op["id"]] = FieldDef(
                id=op["id"], label=op["label"], normalized_label=normalize_label(op["label"]),
                group_key=None, group_id=op.get("group"), type=op.get("type", "text"),
                declared_type=op.get("declared_type", "libre"), options=list(op.get("options") or []), ord=next_ord,
            )
            next_ord += 1
        elif kind == "archive_field":
            fields[op["field"]].status = "archived"
        elif kind == "move_field":
            fields[op["field"]].group_id = op.get("to_group")
        elif kind == "set_options":
            fields[op["field"]].options = list(op["options"])
            if fields[op["field"]].declared_type == "libre":
                fields[op["field"]].declared_type = "combo"
                fields[op["field"]].type = "combo"
        elif kind == "convert":
            target = fields[op["field"]]
            target.type = op["to"]
            target.declared_type = op.get("declared_type", target.declared_type)
            if op["to"] != "combo":
                target.options = []
        elif kind == "split":
            source = fields[op["field"]]
            labels = op.get("labels") or {}
            for new_id in op["into"]:
                if new_id not in fields:
                    label = labels.get(new_id, new_id)
                    fields[new_id] = FieldDef(
                        id=new_id, label=label, normalized_label=normalize_label(label),
                        group_key=None, group_id=source.group_id, type="text",
                        declared_type="libre", options=[], ord=next_ord,
                    )
                    next_ord += 1
            if op["field"] not in op["into"]:
                source.status = "archived"
        elif kind == "merge":
            for old_id in op["fields"]:
                if old_id != op["into"]:
                    fields[old_id].status = "archived"
            fields[op["into"]].type = "text"
    keys = {g.id: g.key for g in groups.values()}
    for item in fields.values():
        item.group_key = keys.get(item.group_id)
    ordered = sorted(fields.values(), key=lambda f: f.ord)
    return Schema(schema.version_id, schema.version, ordered, list(groups.values()))


@dataclass
class OpStats:
    affected: int = 0
    failed: int = 0
    failed_values: list[str] = field(default_factory=list)


@dataclass
class CompiledOp:
    op: dict
    repeat_of: dict[str, str | None]
    repeatable: frozenset[str]


def compile_ops(schema: Schema, ops: list[dict]) -> list[CompiledOp]:
    """Guarda dónde vive cada campo justo antes de cada operación."""
    repeatable = {g.id for g in schema.groups if g.repeatable}
    repeat_of = {f.id: (f.group_id if f.group_id in repeatable else None) for f in schema.fields}
    compiled = []
    for op in ops:
        compiled.append(CompiledOp(op, dict(repeat_of), frozenset(repeatable)))
        kind = op["op"]
        if kind == "add_group" and op.get("repeatable"):
            repeatable.add(op["id"])
        elif kind == "add_field":
            repeat_of[op["id"]] = op.get("group") if op.get("group") in repeatable else None
        elif kind == "move_field":
            repeat_of[op["field"]] = op.get("to_group") if op.get("to_group") in repeatable else None
        elif kind == "split":
            for new_id in op["into"]:
                repeat_of.setdefault(new_id, repeat_of.get(op["field"]))
    return compiled


def _containers(data: dict, group: str | None) -> list[dict]:
    if group is None:
        return [data]
    items = data.get(group)
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def convert_value(value, to: str, formats: list[str] | None = None) -> tuple[object, bool]:
    if value in (None, ""):
        return value, True
    if to == "date":
        parsed = parse_date(value, formats)
        return (parsed, True) if parsed is not None else (value, False)
    if to == "number":
        if isinstance(value, bool):
            return value, False
        if isinstance(value, (int, float)):
            return value, True
        try:
            number = float(str(value).strip().replace(",", "."))
        except ValueError:
            return value, False
        return (int(number) if number.is_integer() else number), True
    if to == "bool":
        if isinstance(value, bool):
            return value, True
        text = normalize_label(value)
        if text in {"si", "true", "verdadero", "1", "x"}:
            return True, True
        if text in {"no", "false", "falso", "0"}:
            return False, True
        return value, False
    if isinstance(value, float) and value.is_integer():
        return str(int(value)), True
    return (value if isinstance(value, str) else str(value)), True


def apply_op(data: dict, compiled: CompiledOp, stats: OpStats | None = None) -> None:
    op, kind = compiled.op, compiled.op["op"]
    stats = stats if stats is not None else OpStats()
    if kind in {"rename", "archive_field", "set_options", "add_group", "rename_group"}:
        return
    if kind == "add_field":
        if op.get("default") is not None:
            for container in _containers(data, compiled.repeat_of.get(op["id"])):
                if op["id"] not in container:
                    container[op["id"]] = op["default"]
                    stats.affected += 1
    elif kind == "map_values":
        for container in _containers(data, compiled.repeat_of.get(op["field"])):
            value = container.get(op["field"])
            if isinstance(value, str) and value in op["map"]:
                container[op["field"]] = op["map"][value]
                stats.affected += 1
    elif kind == "convert":
        field_id = op["field"]
        for container in _containers(data, compiled.repeat_of.get(field_id)):
            if field_id not in container:
                continue
            value = container[field_id]
            converted, ok = convert_value(value, op["to"], op.get("formats"))
            if not ok:
                container.setdefault(UNMIGRATED, {})[field_id] = value
                del container[field_id]
                stats.failed += 1
                if len(stats.failed_values) < 20:
                    stats.failed_values.append(str(value))
            elif converted != value or type(converted) is not type(value):
                container.setdefault(ORIGINAL, {})[field_id] = value
                container[field_id] = converted
                stats.affected += 1
    elif kind == "split":
        field_id = op["field"]
        for container in _containers(data, compiled.repeat_of.get(field_id)):
            value = container.get(field_id)
            if not isinstance(value, str):
                continue
            parts = value.split(op["separator"], len(op["into"]) - 1)
            container.setdefault(ORIGINAL, {})[field_id] = value
            if field_id not in op["into"]:
                del container[field_id]
            for new_id, part in zip(op["into"], parts):
                container[new_id] = part.strip()
            stats.affected += 1
    elif kind == "merge":
        group = compiled.repeat_of.get(op["into"])
        for container in _containers(data, group):
            values = [container.get(f) for f in op["fields"]]
            if all(v is None for v in values):
                continue
            joined = op.get("separator", " ").join(str(v) for v in values if v is not None)
            original = container.setdefault(ORIGINAL, {})
            for field_id, value in zip(op["fields"], values):
                if value is not None:
                    original[field_id] = value
                if field_id != op["into"]:
                    container.pop(field_id, None)
            container[op["into"]] = joined
            stats.affected += 1
    elif kind == "move_field":
        _move(data, compiled, stats)


def _move(data: dict, compiled: CompiledOp, stats: OpStats) -> None:
    field_id, target = compiled.op["field"], compiled.op.get("to_group")
    source = compiled.repeat_of.get(field_id)
    target_repeat = target if target in compiled.repeatable else None
    if source == target_repeat:
        return
    if source is None:
        if field_id not in data:
            return
        value = data.pop(field_id)
        items = data.setdefault(target_repeat, [])
        if not items:
            items.append({})
        items[0][field_id] = value
        stats.affected += 1
        return
    values = [item.pop(field_id) for item in _containers(data, source) if field_id in item]
    if not values:
        return
    stats.affected += 1
    if target_repeat is None:
        data[field_id] = values[0]
        if len(values) > 1:
            data.setdefault(UNMIGRATED, {})[field_id] = values[1:]
            stats.failed += 1
        return
    items = data.setdefault(target_repeat, [])
    for index, value in enumerate(values):
        if index >= len(items):
            items.append({})
        items[index][field_id] = value


def apply_chain(data: dict, compiled_chain: list[CompiledOp], stats: list[OpStats] | None = None) -> dict:
    for index, compiled in enumerate(compiled_chain):
        apply_op(data, compiled, stats[index] if stats is not None else None)
    return data
