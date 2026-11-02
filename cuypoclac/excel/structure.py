"""Estructura de una hoja: validaciones de datos, incluidas las x14 que openpyxl ignora."""

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from posixpath import dirname, join, normpath

from defusedxml import ElementTree
from openpyxl.utils import range_boundaries
from openpyxl.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_NS_DOC_RELS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_X14 = "{http://schemas.microsoft.com/office/spreadsheetml/2009/9/main}"
_NS_XM = "{http://schemas.microsoft.com/office/excel/2006/main}"

_RANGE = re.compile(r"^(?:(?:'((?:[^']|'')+)'|([^'!]+))!)?(\$?[A-Za-z]+\$?\d+(?::\$?[A-Za-z]+\$?\d+)?)$")
_DECLARED = {"list": "combo", "date": "date", "decimal": "number", "whole": "number"}


@dataclass
class Validation:
    declared_type: str
    options: list[str]
    unresolved: bool
    # (columna mínima, fila mínima, columna máxima, fila máxima)
    ranges: list[tuple[int, int, int, int]]


def read_validations(path: Path, wb: Workbook, ws: Worksheet) -> list[Validation]:
    result: list[Validation] = []
    for dv in ws.data_validations.dataValidation if ws.data_validations else []:
        ranges = [(r.min_col, r.min_row, r.max_col, r.max_row) for r in dv.sqref.ranges]
        validation = _build(dv.type, dv.formula1, ranges, wb, ws)
        if validation:
            result.append(validation)
    if path.suffix.lower() == ".xlsx":
        for type_, formula, sqref in _read_x14(path, ws.title):
            validation = _build(type_, formula, _parse_sqref(sqref), wb, ws)
            if validation:
                result.append(validation)
    return result


def _build(type_, formula, ranges, wb, ws) -> Validation | None:
    declared = _DECLARED.get(type_ or "")
    if declared is None or not ranges:
        return None
    if declared != "combo":
        return Validation(declared, [], False, ranges)
    options = _resolve_list(formula or "", wb, ws)
    return Validation("combo", options or [], options is None, ranges)


def _parse_sqref(sqref: str) -> list[tuple[int, int, int, int]]:
    ranges = []
    for part in sqref.split():
        try:
            min_col, min_row, max_col, max_row = range_boundaries(part)
        except ValueError:
            continue
        if None not in (min_col, min_row, max_col, max_row):
            ranges.append((min_col, min_row, max_col, max_row))
    return ranges


def _resolve_list(formula: str, wb: Workbook, ws: Worksheet) -> list[str] | None:
    """Devuelve las opciones, o None si la fórmula no se puede resolver (ej. INDIRECT)."""
    formula = formula.strip().lstrip("=")
    if formula.startswith('"') and formula.endswith('"'):
        return [item.strip() for item in formula[1:-1].replace('""', '"').split(",") if item.strip()]
    resolved = _from_range(formula, wb, ws)
    if resolved is not None:
        return resolved
    defined = ws.defined_names.get(formula) or wb.defined_names.get(formula)
    if defined is not None:
        return _from_range((defined.attr_text or "").lstrip("="), wb, ws)
    return None


def _from_range(reference: str, wb: Workbook, ws: Worksheet) -> list[str] | None:
    match = _RANGE.match(reference.strip())
    if not match:
        return None
    sheet = (match.group(1) or "").replace("''", "'") or match.group(2)
    target = ws if sheet is None else (wb[sheet] if sheet in wb.sheetnames else None)
    if target is None:
        return None
    options: list[str] = []
    for row in target.iter_rows(*_iter_args(match.group(3)), values_only=True):
        for value in row:
            if value not in (None, ""):
                text = str(value).strip()
                if text not in options:
                    options.append(text)
    return options


def _iter_args(reference: str) -> tuple[int, int, int, int]:
    min_col, min_row, max_col, max_row = range_boundaries(reference.replace("$", ""))
    return min_row, max_row, min_col, max_col


def _read_x14(path: Path, sheet_name: str) -> list[tuple[str | None, str, str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            part = _sheet_part(archive, sheet_name)
            if part is None:
                return []
            root = ElementTree.fromstring(archive.read(part))
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return []
    found = []
    for dv in root.iter(f"{_NS_X14}dataValidation"):
        formula = dv.find(f"{_NS_X14}formula1/{_NS_XM}f")
        sqref = dv.find(f"{_NS_XM}sqref")
        if sqref is not None and sqref.text:
            found.append((dv.get("type"), (formula.text or "") if formula is not None else "", sqref.text))
    return found


def _sheet_part(archive: zipfile.ZipFile, sheet_name: str) -> str | None:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationship = None
    for sheet in workbook.iter(f"{_NS_MAIN}sheet"):
        if sheet.get("name") == sheet_name:
            relationship = sheet.get(f"{_NS_DOC_RELS}id")
    if relationship is None:
        return None
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.iter(f"{_NS_RELS}Relationship"):
        if rel.get("Id") == relationship:
            target = rel.get("Target") or ""
            if target.startswith("/"):
                return target.lstrip("/")
            return normpath(join(dirname("xl/workbook.xml"), target))
    return None
