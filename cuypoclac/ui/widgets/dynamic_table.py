"""Tabla generada desde el esquema: carga las filas por páginas a medida que se ven."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...db.repo import Schema
from ...query import RecordQuery, count_records, query_records
from .common import align_for, format_value

_PAGE = 200
_ORIGIN_COLUMN = "__origin__"


class RecordsModel(QAbstractTableModel):
    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        self._schema: Schema | None = None
        self._columns: list[tuple[str, str, str | None]] = []
        self._rows: list[dict] = []
        self._total = 0
        self.query = RecordQuery()

    def reload(self, schema: Schema | None, query: RecordQuery | None = None) -> None:
        self.beginResetModel()
        self._schema = schema
        if query is not None:
            self.query = query
        self._columns = self._build_columns(schema)
        self._rows = []
        self._total = 0
        if schema is not None and self._ctx.workspace_id is not None:
            self._total = count_records(self._ctx.conn, self._ctx.workspace_id, self.query)
            self._load(0)
        self.endResetModel()

    @property
    def total(self) -> int:
        return self._total

    def _build_columns(self, schema):
        if schema is None:
            return []
        columns = [(_ORIGIN_COLUMN, "Estado", None)]
        seen_groups: set[str] = set()
        repeatable = {g.id: g for g in schema.active_groups if g.repeatable}
        for field in schema.active_fields:
            if field.group_id in repeatable:
                if field.group_id not in seen_groups:
                    seen_groups.add(field.group_id)
                    columns.append((field.group_id, repeatable[field.group_id].label, "group"))
            else:
                columns.append((field.id, field.label, field.type))
        return columns

    def _load(self, offset: int) -> None:
        self.query.limit, self.query.offset = _PAGE, offset
        self._rows.extend(query_records(self._ctx.conn, self._ctx.workspace_id, self.query))

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and len(self._rows) < self._total

    def fetchMore(self, parent=QModelIndex()) -> None:
        before = len(self._rows)
        amount = min(_PAGE, self._total - before)
        self.beginInsertRows(QModelIndex(), before, before + amount - 1)
        self._load(before)
        self.endInsertRows()

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self._columns[section][1]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        record = self._rows[index.row()]
        column_id, _, type_ = self._columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if column_id == _ORIGIN_COLUMN:
                return _record_state(record)
            return format_value(record["data"].get(column_id), type_)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return align_for(type_)
        if role == Qt.ItemDataRole.ToolTipRole and column_id == _ORIGIN_COLUMN:
            return record["rel_path"] or "Creado en la app"
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder) -> None:
        if not self._columns or column >= len(self._columns):
            return
        column_id, _, type_ = self._columns[column]
        if column_id == _ORIGIN_COLUMN or type_ == "group":
            self.query.sort_field = None
        else:
            self.query.sort_field = column_id
            self.query.descending = order == Qt.SortOrder.DescendingOrder
        self.reload(self._schema)

    def record_at(self, row: int) -> dict | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None


def _record_state(record: dict) -> str:
    if record["conflicts"]:
        return "Conflicto"
    if record["status"] == "orphaned":
        return "Sin fila de origen"
    if record["origin"] == "manual":
        return "Manual"
    if record["edits"]:
        return "Editado"
    return "Importado"
