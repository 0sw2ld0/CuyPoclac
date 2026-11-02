"""Abre .xlsx, .xls y .csv como un Workbook de openpyxl, para tratarlos igual."""

import csv
import warnings
from pathlib import Path

import openpyxl
from openpyxl.workbook import Workbook

SUPPORTED_SUFFIXES = {".xlsx", ".xls", ".csv"}


def open_workbook(path: Path) -> Workbook:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        with warnings.catch_warnings():
            # Las validaciones x14 que openpyxl descarta se leen aparte en structure.py.
            warnings.simplefilter("ignore", UserWarning)
            return openpyxl.load_workbook(path, data_only=True)
    if suffix == ".xls":
        return _load_xls(path)
    if suffix == ".csv":
        return _load_csv(path)
    raise ValueError(f"Formato no soportado: {suffix}")


def _load_csv(path: Path) -> Workbook:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Datos"
    for r, row in enumerate(csv.reader(text.splitlines(), dialect), start=1):
        for c, value in enumerate(row, start=1):
            if value != "":
                # Asignación directa para que un texto como "=A1" no sea una fórmula.
                cell = ws.cell(row=r, column=c)
                cell.value = value
                cell.data_type = "s"
    return wb


def _load_xls(path: Path) -> Workbook:
    import xlrd

    book = xlrd.open_workbook(path, formatting_info=True)
    wb = Workbook()
    wb.remove(wb.active)
    for sheet in book.sheets():
        ws = wb.create_sheet(sheet.name[:31])
        if sheet.visibility != 0:
            ws.sheet_state = "hidden"
        for r in range(sheet.nrows):
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
                    continue
                value = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    value = xlrd.xldate.xldate_as_datetime(value, book.datemode)
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    value = bool(value)
                target = ws.cell(row=r + 1, column=c + 1)
                target.value = value
                if isinstance(value, str):
                    target.data_type = "s"
        for rlo, rhi, clo, chi in sheet.merged_cells:
            ws.merge_cells(start_row=rlo + 1, end_row=rhi, start_column=clo + 1, end_column=chi)
    return wb
