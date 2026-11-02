"""Asistente de migración: revisar, simular y solo entonces aplicar."""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...db import repo
from ...migration.apply import MigrationPlan, apply_migration, propose, resolve_ops
from ...migration.dry_run import dry_run
from ...migration.ops import apply_ops_to_schema
from ...schema.diff import Proposal
from ..widgets.common import CONFIDENCE_LABELS, TYPE_LABELS, confirm, muted_label, show_error, title_label

logger = logging.getLogger(__name__)


class ManualOpDialog(QDialog):
    """División, unión y conversión no se detectan solas: se agregan a mano."""

    def __init__(self, parent, schema, next_field_id) -> None:
        super().__init__(parent)
        self.setWindowTitle("Agregar operación")
        self._schema, self._next_field_id = schema, next_field_id
        self.operation: dict | None = None
        form = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItem("Dividir un campo", "split")
        self.kind.addItem("Unir dos campos", "merge")
        self.kind.addItem("Cambiar el tipo de un campo", "convert")
        form.addRow("Operación", self.kind)
        self.first = self._field_box()
        form.addRow("Campo", self.first)
        self.second = self._field_box()
        self.second_label = QLabel("Segundo campo")
        form.addRow(self.second_label, self.second)
        self.separator = QLineEdit(" - ")
        self.separator_label = QLabel("Separador")
        form.addRow(self.separator_label, self.separator)
        self.new_label = QLineEdit()
        self.new_label_title = QLabel("Etiqueta del campo nuevo")
        form.addRow(self.new_label_title, self.new_label)
        self.to_type = QComboBox()
        for value in ("text", "number", "date", "bool"):
            self.to_type.addItem(TYPE_LABELS[value], value)
        self.to_label = QLabel("Tipo nuevo")
        form.addRow(self.to_label, self.to_type)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Agregar")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.kind.currentIndexChanged.connect(self._sync)
        self._sync()

    def _field_box(self) -> QComboBox:
        box = QComboBox()
        for field in self._schema.active_fields:
            box.addItem(field.label, field.id)
        return box

    def _sync(self) -> None:
        kind = self.kind.currentData()
        for widget, visible in (
            (self.second, kind == "merge"), (self.second_label, kind == "merge"),
            (self.separator, kind != "convert"), (self.separator_label, kind != "convert"),
            (self.new_label, kind == "split"), (self.new_label_title, kind == "split"),
            (self.to_type, kind == "convert"), (self.to_label, kind == "convert"),
        ):
            widget.setVisible(visible)

    def _accept(self) -> None:
        kind, field_id = self.kind.currentData(), self.first.currentData()
        if kind == "split":
            label = self.new_label.text().strip()
            if not label or not self.separator.text():
                show_error(self, "Indica el separador y la etiqueta del campo nuevo.", "Faltan datos")
                return
            new_id = self._next_field_id()
            self.operation = {
                "op": "split", "field": field_id, "into": [field_id, new_id],
                "separator": self.separator.text(), "labels": {new_id: label},
            }
        elif kind == "merge":
            other = self.second.currentData()
            if other == field_id:
                show_error(self, "Elige dos campos distintos.", "Faltan datos")
                return
            self.operation = {"op": "merge", "fields": [field_id, other], "into": field_id, "separator": self.separator.text()}
        else:
            self.operation = {"op": "convert", "field": field_id, "to": self.to_type.currentData()}
            if self.to_type.currentData() == "date":
                self.operation["formats"] = ["dd/MM/yyyy", "yyyy-MM-dd", "excel_serial"]
        self.accept()


class MigrationWizard(QDialog):
    def __init__(self, ctx, parent, rel_path: str | None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self.setWindowTitle("Asistente de migración")
        self.resize(1000, 720)
        self._schema = repo.current_schema(ctx.conn, ctx.workspace_id)
        if rel_path:
            self._plan = propose(ctx.conn, ctx.workspace_id, rel_path)
        else:
            self._plan = MigrationPlan("", "", [])
        self._manual: list[Proposal] = []
        self._next_field_id = repo.next_field_id_factory(ctx.conn, ctx.workspace_id, "f_")
        for proposal in self._plan.proposals:
            for op in [proposal.op, *proposal.alternative]:
                if op.get("op") == "add_field":
                    self._reserve(op["id"])
        self._simulated = False
        self.backup_path = None

        root = QVBoxLayout(self)
        root.addWidget(title_label(f"De la versión {self._schema.version} a la {self._schema.version + 1}"))
        source = f"Estructura detectada en: {rel_path}." if rel_path else "Migración manual sobre el esquema vigente."
        root.addWidget(muted_label(source + " Se hará un backup antes de aplicar."))

        splitter = QSplitter(Qt.Orientation.Vertical)
        lists = QWidget()
        lists_row = QHBoxLayout(lists)
        lists_row.setContentsMargins(0, 0, 0, 0)
        self.left = QListWidget()
        self.right = QListWidget()
        for title, widget in ((f"Versión {self._schema.version}", self.left), (f"Versión {self._schema.version + 1}", self.right)):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            column.addWidget(widget)
            lists_row.addLayout(column)
        splitter.addWidget(lists)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Aplicar", "Confianza", "Cambio", "Simulación"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._selection_changed)
        splitter.addWidget(self.table)
        splitter.setSizes([260, 360])
        root.addWidget(splitter, 1)

        self.result = muted_label("Simula la migración para poder aplicarla.")
        root.addWidget(self.result)
        buttons = QHBoxLayout()
        add = QPushButton("Agregar operación…")
        add.clicked.connect(self._add_manual)
        buttons.addWidget(add)
        buttons.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        self.simulate_button = QPushButton("Simular")
        self.simulate_button.clicked.connect(self._simulate)
        self.apply_button = QPushButton("Aplicar migración")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        for button in (cancel, self.simulate_button, self.apply_button):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self._fill()

    def _reserve(self, field_id: str) -> None:
        """Evita repetir ids ya usados por las propuestas en las operaciones manuales."""
        taken = int(field_id.split("_")[1])
        while True:
            candidate = self._next_field_id()
            if int(candidate.split("_")[1]) >= taken:
                return

    @property
    def proposals(self) -> list[Proposal]:
        return [*self._plan.proposals, *self._manual]

    def _fill(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for index, proposal in enumerate(self.proposals):
            self.table.insertRow(index)
            check = QTableWidgetItem()
            flags = Qt.ItemFlag.ItemIsEnabled
            if not proposal.required:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            check.setFlags(flags)
            check.setCheckState(Qt.CheckState.Checked)
            if proposal.required:
                check.setToolTip("Este cambio es necesario para que el archivo encaje en la versión nueva.")
            self.table.setItem(index, 0, check)
            self.table.setItem(index, 1, QTableWidgetItem(CONFIDENCE_LABELS.get(proposal.confidence, proposal.confidence)))
            self.table.setItem(index, 2, QTableWidgetItem(proposal.reason))
            self.table.setItem(index, 3, QTableWidgetItem("—"))
        self.table.blockSignals(False)
        self._refresh_preview()

    def _rejected(self) -> set[int]:
        return {
            i for i in range(self.table.rowCount())
            if self.table.item(i, 0).checkState() != Qt.CheckState.Checked
        }

    def selected_ops(self) -> list[dict]:
        combined = MigrationPlan(self._plan.rel_path, self._plan.fingerprint, self.proposals)
        return resolve_ops(combined, self._rejected())

    def _selection_changed(self, item) -> None:
        if item.column() != 0:
            return
        self._simulated = False
        self.apply_button.setEnabled(False)
        self.result.setText("Cambió la selección: vuelve a simular.")
        self.table.blockSignals(True)
        for i in range(self.table.rowCount()):
            self.table.item(i, 3).setText("—")
        self.table.blockSignals(False)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        self._fill_list(self.left, self._schema)
        self.right.clear()
        try:
            self._fill_list(self.right, apply_ops_to_schema(self._schema, self.selected_ops()))
        except KeyError:
            self.right.addItem("(las operaciones elegidas no son compatibles)")

    def _fill_list(self, widget: QListWidget, schema) -> None:
        widget.clear()
        groups = {g.id: g.label for g in schema.groups}
        for field in schema.active_fields:
            prefix = f"{groups[field.group_id]} › " if field.group_id in groups else ""
            widget.addItem(f"{prefix}{field.label} ({TYPE_LABELS.get(field.type, field.type)})")

    def _add_manual(self) -> None:
        dialog = ManualOpDialog(self, self._schema, self._next_field_id)
        if dialog.exec() and dialog.operation:
            op = dialog.operation
            labels = {f.id: f.label for f in self._schema.fields}
            if op["op"] == "split":
                reason = f"Dividir «{labels[op['field']]}» por «{op['separator']}»."
            elif op["op"] == "merge":
                reason = "Unir " + " y ".join(f"«{labels[f]}»" for f in op["fields"]) + "."
            else:
                reason = f"Convertir «{labels[op['field']]}» a {TYPE_LABELS[op['to']]}."
            self._manual.append(Proposal(op, "manual", reason))
            self._simulated = False
            self.apply_button.setEnabled(False)
            self._fill()

    def _ops_by_row(self) -> dict[int, dict]:
        rejected = self._rejected()
        return {i: p.op for i, p in enumerate(self.proposals) if i not in rejected or p.required}

    def _simulate(self) -> None:
        ops = self.selected_ops()
        if not ops:
            self.result.setText("No hay operaciones para simular.")
            return
        try:
            simulation = dry_run(self._ctx.conn, self._ctx.workspace_id, self._schema, ops)
        except (KeyError, ValueError) as error:
            logger.warning("Simulación no compatible", exc_info=True)
            show_error(self, f"Las operaciones elegidas no son compatibles: {error}")
            return
        effects = {id(op): stats for op, stats in simulation.stats}
        self.table.blockSignals(True)
        for row, op in self._ops_by_row().items():
            stats = effects.get(id(op))
            if stats is None:
                continue
            text = f"{stats.affected} cambian"
            if stats.failed:
                text += f", {stats.failed} no se convierten"
            self.table.item(row, 3).setText(text)
        self.table.blockSignals(False)
        failed = sum(stats.failed for _, stats in simulation.stats)
        message = f"Simulación sobre {simulation.total_records} registros."
        if failed:
            samples = [v for _, stats in simulation.stats for v in stats.failed_values][:5]
            message += f" {failed} valores no se pueden convertir ({', '.join(samples)}…); no se borran: se guardan aparte en cada registro."
        self.result.setText(message)
        self._simulated = True
        self.apply_button.setEnabled(True)

    def _apply(self) -> None:
        if not self._simulated:
            return
        if not confirm(self, "Aplicar migración", "Se creará un backup y una versión nueva del esquema. ¿Continuar?", "Aplicar"):
            return
        ctx = self._ctx
        try:
            self.backup_path = apply_migration(
                ctx.conn, ctx.db_path, ctx.workspace_id, self.selected_ops(), self._plan.fingerprint or None
            )
        except Exception as error:
            logger.exception("La migración se revirtió")
            show_error(self, f"La migración se revirtió y los datos no cambiaron.\n\n{error}")
            return
        self.accept()
