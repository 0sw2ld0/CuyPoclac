"""Exporta un Excel consolidado con encabezados agrupados y combos."""

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter, quote_sheetname
from openpyxl.worksheet.datavalidation import DataValidation

from .db import repo
from .query import RecordQuery, query_records

_HEADER = PatternFill("solid", fgColor="DDE7F0")
_BOLD = Font(bold=True)
_INLINE_LIMIT = 255
_PAGE = 2000


class ExportError(Exception):
    pass


def export_workspace(conn, workspace_id: int, target: Path, query: RecordQuery | None = None) -> int:
    folder = repo.workspace_folder(conn, workspace_id).resolve()
    if target.resolve().is_relative_to(folder):
        raise ExportError("Elige una ubicación fuera de la carpeta de trabajo: esa carpeta no se modifica.")
    schema = repo.current_schema(conn, workspace_id)
    if schema is None:
        raise ExportError("No hay datos para exportar.")

    base = query or RecordQuery()
    records = []
    offset = 0
    while True:
        base.limit, base.offset = _PAGE, offset
        page = query_records(conn, workspace_id, base)
        records.extend(page)
        if len(page) < _PAGE:
            break
        offset += _PAGE

    columns, spans = _layout(schema, records)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Consolidado"
    for group_label, start, end in spans:
        ws.cell(1, start, group_label)
        if end > start:
            ws.merge_cells(start_row=1, start_column=start, end_row=1, end_column=end)
    for index, (label, *_rest) in enumerate(columns, start=1):
        ws.cell(2, index, label)
    for row in ws.iter_rows(min_row=1, max_row=2, max_col=max(len(columns), 1)):
        for cell in row:
            cell.fill, cell.font = _HEADER, _BOLD
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A3"

    for r, record in enumerate(records, start=3):
        for c, (_, field_def, group_id, item) in enumerate(columns, start=1):
            value = _value(record["data"], field_def.id, group_id, item)
            if value is not None:
                _write(ws.cell(r, c), value, field_def.type)
    _add_validations(wb, ws, columns, len(records) + 200)
    for c in range(1, len(columns) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 18
    wb.save(target)
    return len(records)


def _layout(schema, records):
    repeatable = {g.id for g in schema.active_groups if g.repeatable}
    max_items: dict[str, int] = defaultdict(lambda: 1)
    for record in records:
        for group_id in repeatable:
            items = record["data"].get(group_id)
            if isinstance(items, list):
                max_items[group_id] = max(max_items[group_id], len(items))
    columns, spans = [], []
    placed_groups: set[str] = set()
    for field_def in schema.active_fields:
        group_id = field_def.group_id
        if group_id is None:
            columns.append((field_def.label, field_def, None, 0))
            continue
        if group_id in placed_groups:
            continue
        placed_groups.add(group_id)
        group = schema.group(group_id)
        members = [f for f in schema.active_fields if f.group_id == group_id]
        start = len(columns) + 1
        if group_id in repeatable:
            for item in range(max_items[group_id]):
                for member in members:
                    columns.append((f"{member.label} {item + 1}", member, group_id, item))
        else:
            for member in members:
                columns.append((member.label, member, None, 0))
        spans.append((group.label if group else "", start, len(columns)))
    return columns, spans


def _value(data: dict, field_id: str, group_id: str | None, item: int):
    if group_id is None:
        return data.get(field_id)
    items = data.get(group_id)
    if isinstance(items, list) and item < len(items) and isinstance(items[item], dict):
        return items[item].get(field_id)
    return None


def _write(cell, value, type_: str) -> None:
    if type_ == "date" and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None:
            cell.value = parsed.date() if parsed.time() == datetime.min.time() else parsed
            cell.number_format = "dd/mm/yyyy"
            return
    if isinstance(value, (int, float, bool, date)):
        cell.value = value
        return
    # Un texto que empieza con "=" no debe convertirse en fórmula.
    cell.value = str(value)
    cell.data_type = "s"


def _add_validations(wb, ws, columns, last_row: int) -> None:
    lists = None
    next_column = 1
    for c, (_, field_def, _, _) in enumerate(columns, start=1):
        if not field_def.options:
            continue
        inline = ",".join(field_def.options)
        if len(inline) <= _INLINE_LIMIT and not any("," in o or '"' in o for o in field_def.options):
            formula = f'"{inline}"'
        else:
            if lists is None:
                lists = wb.create_sheet("Listas")
                lists.sheet_state = "hidden"
            letter = get_column_letter(next_column)
            for r, option in enumerate(field_def.options, start=1):
                lists.cell(r, next_column).value = option
                lists.cell(r, next_column).data_type = "s"
            formula = f"{quote_sheetname(lists.title)}!${letter}$1:${letter}${len(field_def.options)}"
            next_column += 1
        validation = DataValidation(type="list", formula1=formula, allow_blank=True)
        letter = get_column_letter(c)
        validation.add(f"{letter}3:{letter}{last_row}")
        ws.add_data_validation(validation)
