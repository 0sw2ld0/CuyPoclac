"""Registros: tabla dinámica, filtros, búsqueda y formulario."""

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ...db import repo
from ...query import RecordQuery, distinct_values
from ..widgets.common import muted_label, title_label
from ..widgets.dynamic_form import RecordDialog
from ..widgets.dynamic_table import RecordsModel

_SEARCH_DELAY_MS = 300


class RecordsPage(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        self._filters: dict[str, QComboBox] = {}
        self._date_field = None
        self._owner_field = None
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(title_label("Registros"))
        header.addStretch()
        self.new_button = QPushButton("Nuevo registro")
        self.new_button.clicked.connect(self._new)
        self.edit_button = QPushButton("Abrir")
        self.edit_button.clicked.connect(self._open_selected)
        header.addWidget(self.edit_button)
        header.addWidget(self.new_button)
        layout.addLayout(header)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar en todos los campos…")
        layout.addWidget(self.search)
        self.filter_bar = QHBoxLayout()
        layout.addLayout(self.filter_bar)

        self.model = RecordsModel(ctx)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.doubleClicked.connect(lambda index: self._open(index.row()))
        layout.addWidget(self.table, 1)
        self.count = muted_label()
        layout.addWidget(self.count)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_SEARCH_DELAY_MS)
        self._timer.timeout.connect(self.apply_query)
        self.search.textChanged.connect(lambda: self._timer.start())
        ctx.data_changed.connect(self.refresh)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        ctx = self._ctx
        schema = repo.current_schema(ctx.conn, ctx.workspace_id) if ctx.workspace_id else None
        self.new_button.setEnabled(schema is not None)
        self.edit_button.setEnabled(schema is not None)
        self._rebuild_filters(schema)
        self.model.reload(schema, self._current_query())
        self.table.resizeColumnsToContents()
        for column in range(self.model.columnCount()):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column), 280))
        self._update_count()

    def _rebuild_filters(self, schema) -> None:
        selected = {field_id: box.currentData() for field_id, box in self._filters.items()}
        date_from = getattr(self, "date_from", None)
        previous_dates = (
            self.date_enabled.isChecked(), date_from.date(), self.date_to.date()
        ) if date_from is not None else None
        previous_owner = self.owner_box.currentData() if self._owner_field else None
        while self.filter_bar.count():
            item = self.filter_bar.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._filters = {}
        self._date_field = self._owner_field = None
        self.date_from = None
        if schema is None:
            return
        roles = repo.get_roles(self._ctx.conn, schema.version_id)
        by_id = {f.id: f for f in schema.active_fields}
        for field_id in roles.get("filterable", []):
            if field_id in by_id:
                self.filter_bar.addWidget(QLabel(by_id[field_id].label))
                box = self._value_box(field_id, selected.get(field_id))
                self._filters[field_id] = box
                self.filter_bar.addWidget(box)
        owner = next((f for f in roles.get("owner", []) if f in by_id), None)
        if owner:
            self._owner_field = owner
            self.filter_bar.addWidget(QLabel(by_id[owner].label))
            self.owner_box = self._value_box(owner, previous_owner)
            self.filter_bar.addWidget(self.owner_box)
        date_field = next((f for f in roles.get("date", []) if f in by_id), None)
        if date_field:
            self._date_field = date_field
            self.date_enabled = QCheckBox(by_id[date_field].label)
            self.date_from = QDateEdit(QDate.currentDate().addYears(-1))
            self.date_to = QDateEdit(QDate.currentDate())
            for editor in (self.date_from, self.date_to):
                editor.setCalendarPopup(True)
                editor.setDisplayFormat("dd/MM/yyyy")
                editor.dateChanged.connect(self.apply_query)
            if previous_dates:
                self.date_enabled.setChecked(previous_dates[0])
                self.date_from.setDate(previous_dates[1])
                self.date_to.setDate(previous_dates[2])
            self.date_enabled.toggled.connect(self.apply_query)
            self.filter_bar.addWidget(self.date_enabled)
            self.filter_bar.addWidget(self.date_from)
            self.filter_bar.addWidget(QLabel("a"))
            self.filter_bar.addWidget(self.date_to)
        self.filter_bar.addStretch()

    def _value_box(self, field_id: str, selected) -> QComboBox:
        box = QComboBox()
        box.addItem("Todos", None)
        for value in distinct_values(self._ctx.conn, self._ctx.workspace_id, field_id):
            box.addItem(value, value)
        if selected is not None and box.findData(selected) >= 0:
            box.setCurrentIndex(box.findData(selected))
        box.currentIndexChanged.connect(self.apply_query)
        return box

    def _current_query(self) -> RecordQuery:
        query = RecordQuery(search=self.search.text().strip())
        query.sort_field = self.model.query.sort_field
        query.descending = self.model.query.descending
        for field_id, box in self._filters.items():
            if box.currentData() is not None:
                query.filters[field_id] = box.currentData()
        if self._owner_field and self.owner_box.currentData() is not None:
            query.owner_field, query.owner = self._owner_field, self.owner_box.currentData()
        if self._date_field and self.date_from is not None and self.date_enabled.isChecked():
            query.date_field = self._date_field
            query.date_from = self.date_from.date().toString("yyyy-MM-dd")
            query.date_to = self.date_to.date().toString("yyyy-MM-dd")
        return query

    def apply_query(self) -> None:
        ctx = self._ctx
        schema = repo.current_schema(ctx.conn, ctx.workspace_id) if ctx.workspace_id else None
        self.model.reload(schema, self._current_query())
        self._update_count()

    def _update_count(self) -> None:
        total = self.model.total
        self.count.setText(f"{total} registro" + ("" if total == 1 else "s"))

    def _new(self) -> None:
        RecordDialog(self._ctx, self).exec()

    def _open_selected(self) -> None:
        indexes = self.table.selectionModel().selectedRows()
        if indexes:
            self._open(indexes[0].row())
        else:
            QMessageBox.information(self, "Abrir registro", "Selecciona un registro en la tabla.")

    def _open(self, row: int) -> None:
        record = self.model.record_at(row)
        if record:
            RecordDialog(self._ctx, self, record).exec()

    def current_query(self) -> RecordQuery:
        return self._current_query()
