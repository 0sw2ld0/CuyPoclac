"""Tipos del esquema candidato descubierto desde un archivo."""

from msgspec import Struct, field


class GroupCandidate(Struct):
    key: str
    label: str
    ord: int
    repeatable: bool = False
    repeat_mode: str | None = None


class FieldCandidate(Struct):
    label: str
    normalized_label: str
    group: str | None
    type: str
    type_source: str
    declared_type: str
    options: list[str]
    ord: int
    # (índice del ítem, columna); los campos simples tienen un solo par con índice 0.
    columns: list[tuple[int, int]]
    unresolved_options: bool = False

    @property
    def key(self) -> str:
        return f"{self.group or ''}|{self.normalized_label}"


class SchemaCandidate(Struct):
    sheet: str
    first_data_row: int
    groups: list[GroupCandidate]
    fields: list[FieldCandidate]
    fingerprint: str = ""
    warnings: list[str] = field(default_factory=list)
    # Campos cabecera sugeridos si las filas parecen repetirse (se confirma con el usuario).
    repeated_rows_hint: list[str] = field(default_factory=list)


class Discovery(Struct):
    candidate: SchemaCandidate
    # Muestras de valores por campo: solo para comparar, nunca se imprimen.
    samples: dict[str, list[str]]
    merged: list[tuple[int, int, int, int]]
