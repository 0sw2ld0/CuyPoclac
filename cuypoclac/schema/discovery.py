"""Descubre grupos, campos, tipos y combos de un archivo, sin configuración previa."""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from openpyxl.worksheet.worksheet import Worksheet

from ..excel.dates import looks_like_date
from ..excel.structure import Validation, read_validations
from ..excel.workbook import open_workbook
from .fingerprint import fingerprint
from .model import Discovery, FieldCandidate, GroupCandidate, SchemaCandidate
from .normalize import normalize_label

_OPTION_SHEETS = {
    "lista", "listas", "catalogo", "catalogos", "opcion", "opciones", "parametro",
    "parametros", "tablas", "maestros", "lists", "lookup", "lookups",
}
_NUMBERED = re.compile(r"^(.*?)\s*(\d+)$")
_HEADER_SCAN_ROWS = 30
_SAMPLE_ROWS = 500
_NUMERIC_THRESHOLD = 0.95
_TRUE_FALSE = {"si", "no", "true", "false", "verdadero", "falso"}


class DiscoveryError(Exception):
    pass


@dataclass
class _Column:
    index: int
    label: str
    group: str | None


def discover(path: Path) -> Discovery:
    wb = open_workbook(path)
    try:
        ws = _choose_sheet(wb)
        grid = [list(row) for row in ws.iter_rows(max_row=_HEADER_SCAN_ROWS + _SAMPLE_ROWS, values_only=True)]
        merged = [(r.min_col, r.min_row, r.max_col, r.max_row) for r in ws.merged_cells.ranges]
        group_row, field_row = _detect_header_rows(grid, merged)
        columns, groups, first_data_row = _build_columns(grid, merged, group_row, field_row)
        rows = _data_rows(grid, merged, first_data_row)
        validations = read_validations(path, wb, ws)
    finally:
        if hasattr(wb, "close"):
            wb.close()

    warnings: list[str] = []
    fields, groups = _build_fields(columns, groups)
    samples: dict[str, list[str]] = {}
    for field in fields:
        _apply_types(field, rows, validations, first_data_row, samples, warnings)
    candidate = SchemaCandidate(
        sheet=ws.title,
        first_data_row=first_data_row,
        groups=sorted(groups.values(), key=lambda g: g.ord),
        fields=fields,
        warnings=warnings,
        repeated_rows_hint=_repeated_rows_hint(fields, rows),
    )
    candidate.fingerprint = fingerprint(candidate)
    return Discovery(candidate=candidate, samples=samples, merged=merged)


def _choose_sheet(wb) -> Worksheet:
    fallback = None
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        if ws.max_row == 1 and ws.max_column == 1 and ws.cell(1, 1).value is None:
            continue
        if normalize_label(ws.title) in _OPTION_SHEETS:
            fallback = fallback or ws
            continue
        return ws
    if fallback is not None:
        return fallback
    raise DiscoveryError("El archivo no tiene hojas visibles con datos.")


def _text_count(row: list) -> int:
    return sum(1 for value in row if isinstance(value, str) and value.strip())


def _detect_header_rows(grid: list[list], merged) -> tuple[int | None, int]:
    """Devuelve (fila de grupos, fila de campos), ambas con base 1."""
    head = grid[:_HEADER_SCAN_ROWS]
    if not head or max((_text_count(row) for row in head), default=0) == 0:
        raise DiscoveryError("No se encontró una fila de encabezados.")
    widest = max(_text_count(row) for row in head)
    required = max(2, round(widest * 0.5))
    horizontal = defaultdict(list)
    for min_col, min_row, max_col, max_row in merged:
        if min_row == max_row and max_col > min_col:
            horizontal[min_row].append((min_col, max_col))
    for index, row in enumerate(head):
        r = index + 1
        below = grid[index + 1] if index + 1 < len(grid) else []
        if horizontal.get(r) and _text_count(below) >= 2:
            ranges = horizontal[r]
            below_cols = [c for c, value in enumerate(below, start=1) if isinstance(value, str) and value.strip()]
            is_title = (
                len(ranges) == 1
                and _text_count(row) == 1
                and ranges[0][0] <= below_cols[0]
                and ranges[0][1] >= below_cols[-1]
            )
            if is_title:
                continue
            return r, r + 1
        if _text_count(row) >= required:
            return None, r
    raise DiscoveryError("No se encontró una fila de encabezados.")


def _build_columns(grid, merged, group_row, field_row):
    groups: dict[str, GroupCandidate] = {}
    labels_by_col: dict[int, str] = {}
    group_by_col: dict[int, str] = {}
    fields = grid[field_row - 1]

    if group_row is not None:
        upper = grid[group_row - 1]
        spans = {(a, b): (c, d) for a, b, c, d in merged if b == group_row and d == group_row}
        covered: dict[int, str] = {}
        for c, value in enumerate(upper, start=1):
            if not (isinstance(value, str) and value.strip()):
                continue
            end = next((span[0] for start, span in spans.items() if start == (c, group_row)), c)
            label = value.strip()
            key = normalize_label(label)
            for col in range(c, end + 1):
                covered[col] = key
            if key and key not in groups:
                groups[key] = GroupCandidate(key=key, label=label, ord=len(groups))
        for c in range(1, len(fields) + 1):
            value = fields[c - 1]
            text = str(value).strip() if value not in (None, "") else ""
            if text:
                labels_by_col[c] = text
                if c in covered:
                    group_by_col[c] = covered[c]
            elif isinstance(upper[c - 1] if c <= len(upper) else None, str) and upper[c - 1].strip():
                # Encabezado combinado en vertical: es un campo sin grupo.
                labels_by_col[c] = upper[c - 1].strip()
    else:
        for c, value in enumerate(fields, start=1):
            if value not in (None, ""):
                labels_by_col[c] = str(value).strip()

    columns = [
        _Column(index=c, label=label, group=group_by_col.get(c))
        for c, label in sorted(labels_by_col.items())
        if normalize_label(label)
    ]
    if not columns:
        raise DiscoveryError("Los encabezados no tienen etiquetas válidas.")
    used = {c.group for c in columns if c.group}
    groups = {k: g for k, g in groups.items() if k in used}
    return columns, groups, field_row + 1


def _build_fields(columns: list[_Column], groups: dict[str, GroupCandidate]):
    consumed: set[int] = set()
    fields: list[FieldCandidate] = []
    parents: dict[str | None, list[_Column]] = defaultdict(list)
    for column in columns:
        parents[column.group].append(column)

    for parent, members in parents.items():
        numbered: dict[str, dict[int, _Column]] = defaultdict(dict)
        base_labels: dict[str, str] = {}
        for column in members:
            match = _NUMBERED.match(column.label)
            base = normalize_label(match.group(1)) if match else ""
            if base:
                numbered[base][int(match.group(2))] = column
                base_labels.setdefault(base, match.group(1).strip())
        buckets: dict[tuple[int, ...], list[str]] = defaultdict(list)
        for base, by_number in numbered.items():
            numbers = tuple(sorted(by_number))
            if len(numbers) >= 2 and numbers[0] <= 1 and numbers == tuple(range(numbers[0], numbers[0] + len(numbers))):
                buckets[numbers].append(base)
        for numbers, bases in buckets.items():
            bucket_cols = {by.index for base in bases for by in numbered[base].values()}
            whole_parent = parent is not None and bucket_cols == {c.index for c in members}
            if whole_parent:
                key, label = parent, groups[parent].label
            else:
                key, label = bases[0], base_labels[bases[0]]
                while key in groups:
                    key += " items"
            ord_ = groups[parent].ord if whole_parent else len(groups) + 100
            groups[key] = GroupCandidate(key, label, ord_, True, "numbered_columns")
            for base in bases:
                by_number = numbered[base]
                first = by_number[numbers[0]]
                fields.append(
                    FieldCandidate(
                        label=base_labels[base],
                        normalized_label=base,
                        group=key,
                        type="text",
                        type_source="inferred",
                        declared_type="libre",
                        options=[],
                        ord=first.index,
                        columns=[(i, by_number[n].index) for i, n in enumerate(numbers)],
                    )
                )
                consumed.update(c.index for c in by_number.values())

    seen: dict[str, int] = {}
    for column in columns:
        if column.index in consumed:
            continue
        normalized = normalize_label(column.label)
        identity = f"{column.group or ''}|{normalized}"
        seen[identity] = seen.get(identity, 0) + 1
        if seen[identity] > 1:
            normalized = f"{normalized} #{seen[identity]}"
        fields.append(
            FieldCandidate(
                label=column.label,
                normalized_label=normalized,
                group=column.group,
                type="text",
                type_source="inferred",
                declared_type="libre",
                options=[],
                ord=column.index,
                columns=[(0, column.index)],
            )
        )
    fields.sort(key=lambda f: f.ord)
    for position, field in enumerate(fields):
        field.ord = position
    return fields, groups


def _data_rows(grid, merged, first_data_row) -> list[list]:
    rows = [list(row) for row in grid[first_data_row - 1 :]]
    for min_col, min_row, max_col, max_row in merged:
        if max_row < first_data_row or min_row > len(grid):
            continue
        source = grid[min_row - 1][min_col - 1] if min_col <= len(grid[min_row - 1]) else None
        for r in range(max(min_row, first_data_row), min(max_row, len(grid)) + 1):
            for c in range(min_col, min(max_col, len(rows[r - first_data_row])) + 1):
                rows[r - first_data_row][c - 1] = source
    return [row for row in rows if any(value not in (None, "") for value in row)]


def _apply_types(field, rows, validations: list[Validation], first_data_row, samples, warnings) -> None:
    columns = {col for _, col in field.columns}
    declared = "libre"
    options: list[str] = []
    for validation in validations:
        applies = any(
            min_col <= col <= max_col and max_row >= first_data_row
            for col in columns
            for min_col, min_row, max_col, max_row in validation.ranges
        )
        if not applies:
            continue
        declared = validation.declared_type
        for option in validation.options:
            if option not in options:
                options.append(option)
        if validation.unresolved:
            field.unresolved_options = True
            warnings.append(f"No se pudieron resolver las opciones del campo «{field.label}».")
    field.declared_type = declared
    field.options = options

    values = [
        row[col - 1]
        for row in rows
        for col in sorted(columns)
        if col <= len(row) and row[col - 1] not in (None, "")
    ]
    samples[field.key] = sorted({str(value) for value in values})[:_SAMPLE_ROWS]
    if declared != "libre":
        field.type = declared
        field.type_source = "declared"
    else:
        field.type = infer_type(values)
        field.type_source = "inferred"


def infer_type(values: list) -> str:
    """Una columna sin valores no aporta evidencia y queda como texto provisional."""
    if not values:
        return "text"
    total = len(values)
    if all(isinstance(v, bool) or (isinstance(v, str) and normalize_label(v) in _TRUE_FALSE) for v in values):
        return "bool"
    if sum(looks_like_date(v) for v in values) / total >= _NUMERIC_THRESHOLD:
        return "date"
    if sum(_is_number(v) for v in values) / total >= _NUMERIC_THRESHOLD:
        return "number"
    return "text"


def _is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.strip().replace(",", "."))
        except ValueError:
            return False
        return True
    return False


def _repeated_rows_hint(fields, rows) -> list[str]:
    """Sugiere campos cabecera si varias filas los dejan vacíos y llenan el resto."""
    simple = [f for f in fields if len(f.columns) == 1]
    if len(simple) < 2 or len(rows) < 2:
        return []
    def filled(row, field):
        col = field.columns[0][1]
        return col <= len(row) and row[col - 1] not in (None, "")
    first = simple[0]
    continuation = [row for row in rows if not filled(row, first)]
    if not continuation or not any(filled(row, first) for row in rows):
        return []
    head = [f for f in simple if not any(filled(row, f) for row in continuation)]
    if not head or len(head) == len(simple):
        return []
    return [f.key for f in head]
