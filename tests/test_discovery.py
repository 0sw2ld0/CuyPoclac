import zipfile
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.worksheet.datavalidation import DataValidation

from cuypoclac.excel.dates import parse_date, serial_to_date
from cuypoclac.schema.discovery import discover
from cuypoclac.schema.normalize import normalize_label

from .conftest import OPTIONS, build_workbook


def by_label(candidate):
    return {f.normalized_label: f for f in candidate.fields}


def test_normalize_label():
    assert normalize_label("  Código   de  Área! ") == "codigo de area"


def test_groups_fields_types_and_options(base_file):
    candidate = discover(base_file).candidate
    assert [g.label for g in candidate.groups] == ["Identificación", "Detalle"]
    fields = by_label(candidate)
    assert fields["codigo"].group == "identificacion"
    assert fields["nivel"].type == "combo"
    assert fields["nivel"].options == OPTIONS
    assert fields["fecha"].type == "date"
    assert fields["cantidad"].type == "number"
    assert fields["campo dos"].type == "text"
    assert candidate.first_data_row == 3


def test_single_level_header_and_title_row(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Reporte"
    ws.merge_cells("A1:C1")
    ws.append(["Uno", "Dos", "Tres"])
    ws.append([1, 2, 3])
    path = tmp_path / "t.xlsx"
    wb.save(path)
    candidate = discover(path).candidate
    assert candidate.groups == []
    assert [f.normalized_label for f in candidate.fields] == ["uno", "dos", "tres"]
    assert candidate.first_data_row == 3


def test_options_from_other_sheet_and_defined_name(tmp_path):
    from openpyxl.workbook.defined_name import DefinedName

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Datos"
    ws.append(["Uno", "Dos", "Tres"])
    ws.append(["a", "b", "c"])
    lists = wb.create_sheet("Listas")
    for i, value in enumerate(["X", "Y", "Z"], start=2):
        lists.cell(i, 1, value)
    lists.sheet_state = "hidden"
    wb.defined_names["MisOpciones"] = DefinedName("MisOpciones", attr_text="Listas!$A$2:$A$4")
    first = DataValidation(type="list", formula1="Listas!$A$2:$A$4")
    first.add("A2:A50")
    second = DataValidation(type="list", formula1="MisOpciones")
    second.add("B2:B50")
    ws.add_data_validation(first)
    ws.add_data_validation(second)
    path = tmp_path / "t.xlsx"
    wb.save(path)
    fields = by_label(discover(path).candidate)
    assert fields["uno"].options == ["X", "Y", "Z"]
    assert fields["dos"].options == ["X", "Y", "Z"]
    assert fields["tres"].type == "text"


def _inject_x14(path: Path) -> None:
    """Añade una validación x14, como las que Excel genera para listas en otra hoja."""
    ext = (
        '<extLst><ext uri="{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}" '
        'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
        '<x14:dataValidations xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main" count="1">'
        '<x14:dataValidation type="list" allowBlank="1"><x14:formula1><xm:f>Listas!$A$2:$A$4</xm:f>'
        "</x14:formula1><xm:sqref>A2:A50</xm:sqref></x14:dataValidation></x14:dataValidations></ext></extLst>"
    )
    with zipfile.ZipFile(path) as source:
        items = {name: source.read(name) for name in source.namelist()}
    sheet = items["xl/worksheets/sheet1.xml"].decode("utf-8")
    items["xl/worksheets/sheet1.xml"] = sheet.replace("</worksheet>", ext + "</worksheet>").encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in items.items():
            target.writestr(name, content)


def test_x14_validation_is_read(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Datos"
    ws.append(["Uno", "Dos"])
    ws.append(["X", "b"])
    lists = wb.create_sheet("Listas")
    for i, value in enumerate(["X", "Y", "Z"], start=2):
        lists.cell(i, 1, value)
    path = tmp_path / "t.xlsx"
    wb.save(path)
    _inject_x14(path)
    fields = by_label(discover(path).candidate)
    assert fields["uno"].type == "combo"
    assert fields["uno"].options == ["X", "Y", "Z"]


def test_unresolved_formula_warns(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Uno", "Dos"])
    ws.append(["a", "b"])
    validation = DataValidation(type="list", formula1="INDIRECT(B1)")
    validation.add("A2:A9")
    ws.add_data_validation(validation)
    path = tmp_path / "t.xlsx"
    wb.save(path)
    candidate = discover(path).candidate
    assert by_label(candidate)["uno"].unresolved_options
    assert candidate.warnings


def test_numbered_columns_become_repeatable(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Código", "Tarea 1", "Dueño 1", "Tarea 2", "Dueño 2", "Año 2024"])
    ws.append(["A", "t1", "d1", "t2", "d2", 5])
    path = tmp_path / "t.xlsx"
    wb.save(path)
    candidate = discover(path).candidate
    assert len(candidate.groups) == 1
    group = candidate.groups[0]
    assert group.repeatable and group.repeat_mode == "numbered_columns"
    fields = by_label(candidate)
    assert fields["tarea"].group == group.key
    assert fields["tarea"].columns == [(0, 2), (1, 4)]
    assert fields["dueno"].group == group.key
    assert fields["ano 2024"].group is None


def test_repeated_rows_hint(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Código", "Nombre", "Ítem", "Valor"])
    ws.append(["A", "uno", "i1", 1])
    ws.append([None, None, "i2", 2])
    ws.append(["B", "dos", "i3", 3])
    path = tmp_path / "t.xlsx"
    wb.save(path)
    assert discover(path).candidate.repeated_rows_hint == ["|codigo", "|nombre"]


def test_fingerprint_ignores_data_and_case(tmp_path):
    first = discover(build_workbook(tmp_path / "a.xlsx")).candidate
    other_rows = discover(
        build_workbook(tmp_path / "b.xlsx", rows=[("A-9", 123, "Alto", "sin fecha", "texto")])
    ).candidate
    empty = discover(build_workbook(tmp_path / "c.xlsx", rows=[])).candidate
    upper = discover(build_workbook(tmp_path / "d.xlsx", second_label="CAMPO  DOS")).candidate
    assert first.fingerprint == other_rows.fingerprint == empty.fingerprint == upper.fingerprint


def test_fingerprint_detects_structure_change(tmp_path):
    first = discover(build_workbook(tmp_path / "a.xlsx")).candidate
    renamed = discover(build_workbook(tmp_path / "b.xlsx", second_label="Otra cosa")).candidate
    new_options = discover(build_workbook(tmp_path / "c.xlsx", options=["Alto", "Bajo"])).candidate
    assert first.fingerprint != renamed.fingerprint
    assert first.fingerprint != new_options.fingerprint


def test_excel_serial_dates():
    assert serial_to_date(59) == date(1900, 2, 28)
    assert serial_to_date(60) is None
    assert serial_to_date(61) == date(1900, 3, 1)
    assert serial_to_date(46000) == date(2025, 12, 9)
    assert parse_date("15/10/2026") == "2026-10-15"
    assert parse_date("no es fecha") is None


def test_csv_and_xls_paths(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("Código;Número\nA;1\nB;2\n", encoding="utf-8")
    fields = by_label(discover(path).candidate)
    assert fields["numero"].type == "number"
    assert fields["codigo"].type == "text"
