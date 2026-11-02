from cuypoclac.schema.diff import defs_from_candidate, diff_schemas
from cuypoclac.schema.discovery import discover

from .conftest import build_workbook


def run_diff(old_path, new_path):
    old, new = discover(old_path), discover(new_path)
    fields, groups = defs_from_candidate(old.candidate)
    counter = {"f": 100, "g": 100}

    def next_field():
        counter["f"] += 1
        return f"f_{counter['f']}"

    def next_group():
        counter["g"] += 1
        return f"g_{counter['g']}"

    return diff_schemas(fields, groups, new.candidate, old.samples, new.samples, {}, next_field, next_group)


def ops(proposals, name):
    return [p for p in proposals if p.op["op"] == name]


def test_similar_rename_is_high_confidence(tmp_path):
    a = build_workbook(tmp_path / "a.xlsx", second_label="Campo Dos")
    b = build_workbook(tmp_path / "b.xlsx", second_label="Campo Dos Nuevo")
    proposals = run_diff(a, b)
    assert len(proposals) == 1
    rename = ops(proposals, "rename")[0]
    assert rename.confidence == "alta"
    assert rename.op["label"] == "Campo Dos Nuevo"
    assert {o["op"] for o in rename.alternative} == {"add_field", "archive_field"}


def test_reordered_words_rename(tmp_path):
    a = build_workbook(tmp_path / "a.xlsx", second_label="Punto Central")
    b = build_workbook(tmp_path / "b.xlsx", second_label="Central Punto")
    assert ops(run_diff(a, b), "rename")


def test_distinct_name_same_values_is_medium(tmp_path):
    a = build_workbook(tmp_path / "a.xlsx", second_label="Campo Dos")
    b = build_workbook(tmp_path / "b.xlsx", second_label="Totalmente Distinto")
    rename = ops(run_diff(a, b), "rename")[0]
    assert rename.confidence == "media"


def test_option_change_proposes_map(tmp_path):
    a = build_workbook(tmp_path / "a.xlsx", options=["Alto", "Medio", "Bajo"])
    b = build_workbook(tmp_path / "b.xlsx", options=["Alto", "Medio superior", "Bajo"])
    proposals = run_diff(a, b)
    assert ops(proposals, "set_options")[0].op["options"] == ["Alto", "Medio superior", "Bajo"]
    assert ops(proposals, "map_values")[0].op["map"] == {"Medio": "Medio superior"}


def test_added_and_archived(tmp_path):
    a = build_workbook(tmp_path / "a.xlsx", extra_column="Sobrante")
    b = build_workbook(tmp_path / "b.xlsx")
    archived = ops(run_diff(a, b), "archive_field")
    assert len(archived) == 1
    added = ops(run_diff(b, a), "add_field")
    assert added[0].op["label"] == "Sobrante"


def test_same_template_has_no_proposals(tmp_path):
    a = build_workbook(tmp_path / "a.xlsx")
    b = build_workbook(tmp_path / "b.xlsx", rows=[("Z", "z", "Bajo", None, 1)])
    assert run_diff(a, b) == []
