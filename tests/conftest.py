"""Archivos de prueba generados al vuelo, con datos ficticios y nombres genéricos."""

from datetime import date
from pathlib import Path

import openpyxl
import pytest
from openpyxl.worksheet.datavalidation import DataValidation

OPTIONS = ["Alto", "Medio", "Bajo"]


def build_workbook(
    path: Path,
    *,
    second_label: str = "Campo Dos",
    options: list[str] | None = None,
    rows: list[tuple] | None = None,
    extra_column: str | None = None,
) -> Path:
    options = options if options is not None else OPTIONS
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Datos"
    ws["A1"] = "Identificación"
    ws.merge_cells("A1:B1")
    ws["C1"] = "Detalle"
    ws.merge_cells("C1:E1")
    headers = ["Código", second_label, "Nivel", "Fecha", "Cantidad"]
    if extra_column:
        headers.append(extra_column)
    for c, header in enumerate(headers, start=1):
        ws.cell(2, c, header)
    default_rows = [
        ("A-1", "uno", "Alto", date(2026, 1, 15), 10),
        ("A-2", "dos", "Medio", date(2026, 2, 15), 20),
        ("A-3", "tres", "Bajo", date(2026, 3, 15), 30),
    ]
    for row in rows if rows is not None else default_rows:
        ws.append(row)
    validation = DataValidation(type="list", formula1='"' + ",".join(options) + '"')
    validation.add("C3:C200")
    ws.add_data_validation(validation)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


@pytest.fixture
def base_file(tmp_path) -> Path:
    return build_workbook(tmp_path / "base.xlsx")
