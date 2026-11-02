"""Formulario de alta y edición generado desde el esquema."""

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ... import records
from ...db import repo
from .common import confirm, format_value, show_error
from .field_input import FieldInput
from .repeatable_group import RepeatableGroup


class RecordDialog(QDialog):
    def __init__(self, ctx, parent, record: dict | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._record = record
        self._schema = repo.current_schema(ctx.conn, ctx.workspace_id)
        self._inputs: dict[str, FieldInput | RepeatableGroup] = {}
        self.setWindowTitle("Editar registro" if record else "Nuevo registro")
        self.resize(640, 720)

        data = record["data"] if record else {}
        locked = set()
        if record and record["origin"] == "import":
            locked = set(repo.get_roles(ctx.conn, self._schema.version_id).get("key", []))

        root = QVBoxLayout(self)
        if record and record["rel_path"]:
            where = f"Origen: {record['rel_path']}, fila {record['source_row']}. El archivo no se modifica."
            root.addWidget(QLabel(where))
        self._add_conflicts(root)

        body = QWidget()
        form_layout = QVBoxLayout(body)
        ungrouped = [f for f in self._schema.active_fields if f.group_id is None]
        if ungrouped:
            box, form = self._section("General")
            form_layout.addWidget(box)
            for field in ungrouped:
                self._add_input(form, field, data, locked)
        for group in self._schema.active_groups:
            members = [f for f in self._schema.active_fields if f.group_id == group.id]
            if not members:
                continue
            box, form = self._section(group.label)
            form_layout.addWidget(box)
            if group.repeatable:
                editor = RepeatableGroup(group, members, data.get(group.id) or [])
                self._inputs[group.id] = editor
                form.addRow(editor)
            else:
                for field in members:
                    self._add_input(form, field, data, locked)
        form_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Guardar")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        if record and record["origin"] == "manual":
            delete = buttons.addButton("Eliminar", QDialogButtonBox.ButtonRole.DestructiveRole)
            delete.clicked.connect(self._delete)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _section(self, title: str):
        box = QGroupBox(title)
        return box, QFormLayout(box)

    def _add_input(self, form, field, data, locked) -> None:
        editor = FieldInput(field, data.get(field.id))
        if field.id in locked:
            editor.set_read_only("Es parte de la clave del registro importado: se toma del archivo.")
        self._inputs[field.id] = editor
        form.addRow(field.label, editor)

    def _add_conflicts(self, root) -> None:
        if not self._record:
            return
        labels = {f.id: f for f in self._schema.fields}
        labels.update({g.id: g for g in self._schema.groups})
        for edit in records.record_edits(self._ctx.conn, self._record["id"]):
            if edit["status"] != "conflict":
                continue
            target = labels.get(edit["field_id"])
            type_ = getattr(target, "type", None)
            box = QGroupBox(f"Conflicto en «{target.label if target else edit['field_id']}»")
            row = QHBoxLayout(box)
            message = (
                f"Tu edición: {format_value(edit['value'], type_)}\n"
                f"Nuevo valor del archivo: {format_value(edit['incoming_value'], type_) or '(vacío)'}"
            )
            row.addWidget(QLabel(message), 1)
            keep = QPushButton("Mantener edición")
            take = QPushButton("Usar el archivo")
            keep.clicked.connect(lambda _=False, e=edit: self._resolve(e, True))
            take.clicked.connect(lambda _=False, e=edit: self._resolve(e, False))
            row.addWidget(keep)
            row.addWidget(take)
            root.addWidget(box)

    def _resolve(self, edit: dict, keep: bool) -> None:
        records.resolve_conflict(self._ctx.conn, self._record["id"], edit["field_id"], keep)
        self._ctx.data_changed.emit()
        self.accept()

    def _save(self) -> None:
        values = {field_id: editor.value() for field_id, editor in self._inputs.items() if editor.isEnabled()}
        try:
            if self._record:
                records.update_record(self._ctx.conn, self._ctx.workspace_id, self._record["id"], values)
            else:
                if all(v in (None, [], "") for v in values.values()):
                    QMessageBox.information(self, "Registro vacío", "Completa al menos un campo.")
                    return
                records.create_manual(self._ctx.conn, self._ctx.workspace_id, values)
        except records.RecordError as error:
            show_error(self, str(error), "Revisa los datos")
            return
        self._ctx.data_changed.emit()
        self.accept()

    def _delete(self) -> None:
        if confirm(self, "Eliminar registro", "Este registro solo existe en la app. ¿Eliminarlo?", "Eliminar"):
            records.delete_manual(self._ctx.conn, self._record["id"])
            self._ctx.data_changed.emit()
            self.accept()
