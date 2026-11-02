"""Esquema: campos, roles, historial y seguridad."""

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ... import db, records
from ...db import repo
from ..widgets.common import TYPE_LABELS, confirm, format_timestamp, muted_label, show_error, title_label

_ROLE_COLUMNS = [("key", "Clave"), ("date", "Fecha"), ("owner", "Responsable"), ("filterable", "Filtrable")]
_OP_LABELS = {
    "rename": "renombrar", "add_field": "agregar campo", "archive_field": "archivar campo",
    "move_field": "mover", "set_options": "cambiar opciones", "map_values": "mapear valores",
    "convert": "convertir", "split": "dividir", "merge": "unir", "add_group": "agregar grupo",
    "rename_group": "renombrar grupo",
}


class SchemaPage(QWidget):
    manual_migration = Signal()

    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        self._schema = None
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(title_label("Esquema"))
        header.addStretch()
        self.migrate_button = QPushButton("Migración manual…")
        self.migrate_button.clicked.connect(self.manual_migration)
        self.save_button = QPushButton("Guardar roles")
        self.save_button.clicked.connect(self._save)
        header.addWidget(self.migrate_button)
        header.addWidget(self.save_button)
        layout.addLayout(header)
        self.version_label = muted_label()
        layout.addWidget(self.version_label)

        self.hint_box = QWidget()
        hint = QHBoxLayout(self.hint_box)
        hint.setContentsMargins(0, 0, 0, 0)
        hint.addWidget(QLabel("Hay filas que parecen repetir un registro: varias filas dejan vacíos sus campos principales."), 1)
        confirm_hint = QPushButton("Agrupar filas repetidas…")
        confirm_hint.clicked.connect(self._confirm_hint)
        hint.addWidget(confirm_hint)
        self.hint_box.setVisible(False)
        layout.addWidget(self.hint_box)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 3 + len(_ROLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(["Grupo", "Campo", "Tipo", *[label for _, label in _ROLE_COLUMNS]])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        splitter.addWidget(self.table)

        lower = QWidget()
        lower_layout = QHBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        history = QVBoxLayout()
        history.addWidget(QLabel("Historial de versiones"))
        self.history = QListWidget()
        history.addWidget(self.history)
        lower_layout.addLayout(history, 2)
        safety = QVBoxLayout()
        safety.addWidget(QLabel("Backups"))
        self.backups = QListWidget()
        safety.addWidget(self.backups)
        restore = QPushButton("Restaurar backup seleccionado")
        restore.clicked.connect(self._restore)
        safety.addWidget(restore)
        self.encrypt_button = QPushButton("Cifrar la base con contraseña…")
        self.encrypt_button.clicked.connect(self._encrypt)
        safety.addWidget(self.encrypt_button)
        lower_layout.addLayout(safety, 1)
        splitter.addWidget(lower)
        splitter.setSizes([420, 220])
        layout.addWidget(splitter, 1)

        ctx.data_changed.connect(self.refresh)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        ctx = self._ctx
        self._schema = repo.current_schema(ctx.conn, ctx.workspace_id) if ctx.workspace_id else None
        self.table.setRowCount(0)
        self.history.clear()
        self._refresh_backups()
        has_schema = self._schema is not None
        self.save_button.setEnabled(has_schema)
        self.migrate_button.setEnabled(has_schema)
        self.encrypt_button.setVisible(db.encryption_available() and ctx.key is None)
        if not has_schema:
            self.version_label.setText("Aún no hay esquema: elige una carpeta en Inicio.")
            self.hint_box.setVisible(False)
            return
        schema = self._schema
        self.version_label.setText(
            f"Versión vigente: {schema.version} · {len(schema.active_fields)} campos · "
            "Define la clave para poder editar registros importados."
        )
        roles = repo.get_roles(ctx.conn, schema.version_id)
        groups = {g.id: g for g in schema.groups}
        for field in schema.active_fields:
            row = self.table.rowCount()
            self.table.insertRow(row)
            group = groups.get(field.group_id)
            group_text = "" if group is None else group.label + (" (repetible)" if group.repeatable else "")
            self.table.setItem(row, 0, QTableWidgetItem(group_text))
            name = QTableWidgetItem(field.label)
            name.setData(Qt.ItemDataRole.UserRole, field.id)
            if field.options:
                name.setToolTip("Opciones: " + ", ".join(field.options))
            self.table.setItem(row, 1, name)
            kind = QComboBox()
            for value, label in TYPE_LABELS.items():
                kind.addItem(label, value)
            kind.setCurrentIndex(kind.findData(field.type))
            kind.setEnabled(field.declared_type == "libre")
            if field.declared_type != "libre":
                kind.setToolTip("Lo define la validación de datos del archivo.")
            self.table.setCellWidget(row, 2, kind)
            for offset, (role, _) in enumerate(_ROLE_COLUMNS, start=3):
                cell = QTableWidgetItem()
                allowed = group is None or not group.repeatable
                flags = Qt.ItemFlag.ItemIsEnabled
                if allowed and (role != "date" or field.type == "date"):
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                cell.setFlags(flags)
                cell.setCheckState(Qt.CheckState.Checked if field.id in roles.get(role, []) else Qt.CheckState.Unchecked)
                self.table.setItem(row, offset, cell)
        self.table.resizeColumnToContents(0)
        self._refresh_history()
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        row = self._ctx.conn.execute(
            "SELECT repeated_rows_hint FROM source_file WHERE workspace_id = ? AND repeated_rows_hint IS NOT NULL LIMIT 1",
            (self._ctx.workspace_id,),
        ).fetchone()
        self._hint = json.loads(row[0]) if row else None
        self.hint_box.setVisible(bool(self._hint))

    def _refresh_history(self) -> None:
        conn = self._ctx.conn
        migrations = {
            r["to_version"]: json.loads(r["ops_json"])
            for r in conn.execute("SELECT to_version, ops_json FROM migration WHERE workspace_id = ?", (self._ctx.workspace_id,))
        }
        rows = conn.execute(
            "SELECT version, created_at, is_current FROM schema_version WHERE workspace_id = ? ORDER BY version DESC",
            (self._ctx.workspace_id,),
        ).fetchall()
        for row in rows:
            ops = migrations.get(row["version"], [])
            summary = ", ".join(sorted({_OP_LABELS.get(o["op"], o["op"]) for o in ops})) or "descubierta del primer archivo"
            mark = " (vigente)" if row["is_current"] else ""
            self.history.addItem(f"v{row['version']}{mark} · {format_timestamp(row['created_at'])} · {summary}")

    def _refresh_backups(self) -> None:
        self.backups.clear()
        for path in db.list_backups(self._ctx.db_path):
            item = QListWidgetItem(path.name.split(".bak-", 1)[1])
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.backups.addItem(item)

    def _save(self) -> None:
        roles: dict[str, list[str]] = {role: [] for role, _ in _ROLE_COLUMNS}
        conn, workspace = self._ctx.conn, self._ctx.workspace_id
        for row in range(self.table.rowCount()):
            field_id = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            for offset, (role, _) in enumerate(_ROLE_COLUMNS, start=3):
                if self.table.item(row, offset).checkState() == Qt.CheckState.Checked:
                    roles[role].append(field_id)
        try:
            records.set_roles(conn, workspace, roles)
        except records.RecordError as error:
            show_error(self, str(error))
            return
        changed = []
        fields = {f.id: f for f in self._schema.active_fields}
        for row in range(self.table.rowCount()):
            field_id = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            selected = self.table.cellWidget(row, 2).currentData()
            if fields[field_id].type != selected:
                records.set_field_type(conn, workspace, field_id, selected)
                changed.append(field_id)
        self._ctx.data_changed.emit()
        if changed:
            QMessageBox.information(
                self, "Tipos actualizados",
                "Los tipos nuevos se aplican a las próximas importaciones. "
                "Para convertir los valores ya guardados, usa una migración manual.",
            )

    def _confirm_hint(self) -> None:
        label, accepted = QInputDialog.getText(
            self, "Agrupar filas repetidas", "Nombre del grupo de detalle:", QLineEdit.EchoMode.Normal, "Detalle"
        )
        if not accepted or not label.strip():
            return
        records.confirm_repeated_rows(self._ctx.conn, self._ctx.workspace_id, self._hint, label.strip())
        self._ctx.data_changed.emit()
        self._ctx.folder_changed.emit()

    def _restore(self) -> None:
        item = self.backups.currentItem()
        if item is None:
            QMessageBox.information(self, "Restaurar backup", "Selecciona un backup de la lista.")
            return
        if not confirm(
            self, "Restaurar backup",
            "La base volverá a ese momento y se perderán los cambios posteriores. Antes se guardará una copia del estado actual. ¿Continuar?",
            "Restaurar",
        ):
            return
        ctx = self._ctx
        ctx.pool.waitForDone(10000)
        current = repo.current_schema(ctx.conn, ctx.workspace_id) if ctx.workspace_id else None
        db.backup(ctx.conn, ctx.db_path, current.version if current else 0)
        ctx.conn.close()
        db.restore_backup(ctx.db_path, Path(item.data(Qt.ItemDataRole.UserRole)))
        ctx.reconnect()
        ctx.data_changed.emit()

    def _encrypt(self) -> None:
        password, accepted = QInputDialog.getText(
            self, "Cifrar la base",
            "Elige una contraseña. Si la olvidas, no hay forma de recuperar los datos.",
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not password:
            return
        repeated, accepted = QInputDialog.getText(self, "Cifrar la base", "Repite la contraseña:", QLineEdit.EchoMode.Password)
        if not accepted:
            return
        if repeated != password:
            show_error(self, "Las contraseñas no coinciden.")
            return
        if not confirm(
            self, "Cifrar la base",
            "Se cifrará la base y se eliminarán los backups anteriores sin cifrar. A partir de ahora, la app pedirá la contraseña al abrir. ¿Continuar?",
            "Cifrar",
        ):
            return
        ctx = self._ctx
        ctx.stop_watching()
        ctx.pool.waitForDone(10000)
        ctx.conn.close()
        ctx.key = db.enable_encryption(ctx.db_path, password)
        ctx.reconnect()
        ctx.start_watching()
        ctx.data_changed.emit()
