"""Lectura de filas de datos con bajo consumo de memoria."""

from collections.abc import Iterator
from pathlib import Path
import warnings

import openpyxl

from .workbook import open_workbook


def iter_rows(
    path: Path,
    sheet: str,
    first_row: int,
    merged: list[tuple[int, int, int, int]],
) -> Iterator[tuple[int, tuple]]:
    """Recorre (número de fila, valores), propagando el valor de las celdas combinadas."""
    covered = {
        (r, c): (min_row, min_col)
        for min_col, min_row, max_col, max_row in merged
        if max_row >= first_row
        for r in range(min_row, max_row + 1)
        for c in range(min_col, max_col + 1)
        if (r, c) != (min_row, min_col)
    }
    sources = set(covered.values())
    source_values: dict[tuple[int, int], object] = {}
    workbook = None
    try:
        if path.suffix.lower() == ".xlsx":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
            worksheet = workbook[sheet]
            worksheet.reset_dimensions()
        else:
            workbook = open_workbook(path)
            worksheet = workbook[sheet]
        for r, values in enumerate(worksheet.iter_rows(values_only=True), start=1):
            if not covered:
                if r >= first_row:
                    yield r, tuple(values)
                continue
            row = list(values)
            for c, value in enumerate(row, start=1):
                if (r, c) in sources:
                    source_values[(r, c)] = value
                elif (r, c) in covered:
                    row[c - 1] = source_values.get(covered[(r, c)])
            if r >= first_row:
                yield r, tuple(row)
    finally:
        if workbook is not None and hasattr(workbook, "close"):
            workbook.close()
