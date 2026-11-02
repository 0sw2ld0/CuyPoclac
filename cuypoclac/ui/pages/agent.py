"""Agente: elegir qué datos se comparten, activar la API y crear el proyecto."""

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...agent import config, project
from ...agent.project import ProjectError
from ...agent.queries import AgentError, resolve_pseudonym
from ...db import repo
from ..widgets.common import TYPE_LABELS, confirm, muted_label, show_error, title_label

logger = logging.getLogger(__name__)

_COL_FIELD, _COL_TYPE, _COL_SHARE, _COL_PERSON, _COL_METRIC = range(5)
_METRIC_OPTIONS = [("Ninguna", None), ("Mayor es mejor", "high"), ("Menor es mejor", "low")]


class AgentPage(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        self._updating = False
        self._loaded = None
        layout = QVBoxLayout(self)
        layout.addWidget(title_label("Agente"))
        layout.addWidget(muted_label(
            "Permite consultar los datos desde un agente de VS Code. Lo que compartas aquí le llega al agente y, "
            "si usa un modelo en la nube, sale de este equipo: confirma que puedes enviarlo. "
            "Lo no marcado queda oculto, y también los campos que aparezcan en el futuro."
        ))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Campo", "Tipo", "Compartir", "Persona (seudónimo)", "Métrica de desempeño"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_FIELD, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._item_changed)
        layout.addWidget(self.table, 1)
        layout.addWidget(muted_label(
            "Persona: el campo que identifica a alguien, como su nombre o código. Nunca se comparte: el agente solo "
            "recibe un seudónimo. Revisa que los campos compartidos no contengan nombres ni comentarios personales."
        ))
        save_row = QHBoxLayout()
        save_row.addStretch()
        self.save_button = QPushButton("Guardar campos")
        self.save_button.clicked.connect(self.save)
        save_row.addWidget(self.save_button)
        layout.addLayout(save_row)

        api_row = QHBoxLayout()
        self.api_toggle = QCheckBox("Activar API local")
        self.api_toggle.toggled.connect(self._toggle_api)
        api_row.addWidget(self.api_toggle)
        api_row.addWidget(QLabel("Puerto"))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setGroupSeparatorShown(False)
        api_row.addWidget(self.port)
        self.api_status = QLabel()
        api_row.addWidget(self.api_status, 1)
        self.project_button = QPushButton("Crear proyecto…")
        self.project_button.clicked.connect(self._choose_project)
        api_row.addWidget(self.project_button)
        self.rotate_button = QPushButton("Renovar clave")
        self.rotate_button.clicked.connect(self._rotate)
        api_row.addWidget(self.rotate_button)
        layout.addLayout(api_row)

        lookup = QHBoxLayout()
        lookup.addWidget(QLabel("Ver quién es un seudónimo"))
        self.lookup_input = QLineEdit()
        self.lookup_input.setPlaceholderText("P-XXXXXXXXXX")
        self.lookup_input.returnPressed.connect(self._lookup)
        lookup.addWidget(self.lookup_input, 1)
        lookup_button = QPushButton("Buscar")
        lookup_button.clicked.connect(self._lookup)
        lookup.addWidget(lookup_button)
        layout.addLayout(lookup)
        self.status = muted_label()
        layout.addWidget(self.status)

        ctx.data_changed.connect(self.refresh)
        ctx.workspace_changed.connect(self.reload)
        ctx.agent_api_changed.connect(self._refresh_api)
        self.reload()

    def reload(self) -> None:
        self._loaded = None
        self.refresh()

    def refresh(self) -> None:
        """Los escaneos en segundo plano no deben borrar lo marcado sin guardar."""
        ctx = self._ctx
        schema = repo.current_schema(ctx.conn, ctx.workspace_id) if ctx.workspace_id else None
        signature = schema and (ctx.workspace_id, schema.version_id, [(f.id, f.label, f.type) for f in schema.active_fields])
        if signature == self._loaded and schema is not None:
            self._refresh_api()
            return
        self._loaded = signature
        self._updating = True
        self.table.setRowCount(0)
        if schema is not None:
            fields = config.get_fields(ctx.conn, ctx.workspace_id)
            repeatable = {g.id for g in schema.groups if g.repeatable}
            for field in schema.active_fields:
                self._add_row(field, fields, field.group_id in repeatable)
            self.port.setValue(config.get_settings(ctx.conn, ctx.workspace_id).port)
        self._updating = False
        self.save_button.setEnabled(schema is not None)
        self.api_toggle.setEnabled(schema is not None)
        self.project_button.setEnabled(schema is not None)
        self.rotate_button.setEnabled(schema is not None)
        self._refresh_api()

    def _add_row(self, field, fields: dict, in_repeatable: bool) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        name = QTableWidgetItem(field.label)
        name.setData(Qt.ItemDataRole.UserRole, field.id)
        self.table.setItem(row, _COL_FIELD, name)
        self.table.setItem(row, _COL_TYPE, QTableWidgetItem(TYPE_LABELS.get(field.type, field.type)))
        for column, role in ((_COL_SHARE, config.ROLE_SHARED), (_COL_PERSON, config.ROLE_PERSON)):
            cell = QTableWidgetItem()
            cell.setFlags(Qt.ItemFlag.ItemIsEnabled | (Qt.ItemFlag.NoItemFlags if in_repeatable else Qt.ItemFlag.ItemIsUserCheckable))
            cell.setCheckState(Qt.CheckState.Checked if field.id in fields[role] else Qt.CheckState.Unchecked)
            self.table.setItem(row, column, cell)
        metric = QComboBox()
        for label, value in _METRIC_OPTIONS:
            metric.addItem(label, value)
        metric.setEnabled(field.type in ("number", "text") and not in_repeatable)
        if field.id in fields[config.ROLE_HIGHER_BETTER]:
            metric.setCurrentIndex(metric.findData("high"))
        elif field.id in fields[config.ROLE_LOWER_BETTER]:
            metric.setCurrentIndex(metric.findData("low"))
        metric.currentIndexChanged.connect(lambda _, r=row: self._metric_changed(r))
        self.table.setCellWidget(row, _COL_METRIC, metric)
        if in_repeatable:
            name.setToolTip("Los campos de grupos repetibles todavía no se pueden compartir.")

    def _item_changed(self, item) -> None:
        if self._updating or item.checkState() != Qt.CheckState.Checked:
            return
        self._updating = True
        row = item.row()
        if item.column() == _COL_PERSON:
            # Solo hay una persona, y ese campo nunca se comparte.
            for other in range(self.table.rowCount()):
                if other != row:
                    self.table.item(other, _COL_PERSON).setCheckState(Qt.CheckState.Unchecked)
            self.table.item(row, _COL_SHARE).setCheckState(Qt.CheckState.Unchecked)
            self.table.cellWidget(row, _COL_METRIC).setCurrentIndex(0)
        elif item.column() == _COL_SHARE:
            self.table.item(row, _COL_PERSON).setCheckState(Qt.CheckState.Unchecked)
        self._updating = False

    def _metric_changed(self, row: int) -> None:
        if self._updating or self.table.cellWidget(row, _COL_METRIC).currentData() is None:
            return
        self.table.item(row, _COL_SHARE).setCheckState(Qt.CheckState.Checked)

    def save(self) -> None:
        shared, higher, lower = [], [], []
        person = None
        for row in range(self.table.rowCount()):
            field_id = self.table.item(row, _COL_FIELD).data(Qt.ItemDataRole.UserRole)
            if self.table.item(row, _COL_SHARE).checkState() == Qt.CheckState.Checked:
                shared.append(field_id)
            if self.table.item(row, _COL_PERSON).checkState() == Qt.CheckState.Checked:
                person = field_id
            metric = self.table.cellWidget(row, _COL_METRIC).currentData()
            if metric == "high":
                higher.append(field_id)
            elif metric == "low":
                lower.append(field_id)
        config.save_fields(self._ctx.conn, self._ctx.workspace_id, shared, person, higher, lower)
        logger.info("Campos del agente guardados: %d compartidos, %d métricas", len(shared), len(higher) + len(lower))
        self.status.setText("Campos guardados. La API usa esta configuración en la siguiente consulta.")
        self._ctx.data_changed.emit()

    def _toggle_api(self, enabled: bool) -> None:
        if self._updating:
            return
        ctx = self._ctx
        if not enabled:
            ctx.stop_agent_api()
            return
        config.set_port(ctx.conn, ctx.workspace_id, self.port.value())
        try:
            ctx.start_agent_api()
        except OSError as error:
            logger.warning("No se pudo activar la API del agente", exc_info=True)
            show_error(self, f"No se pudo usar el puerto {self.port.value()}. Elige otro.\n\n{error.strerror or error}")
            self._refresh_api()
            return
        self._refresh_project()

    def _refresh_api(self) -> None:
        server = self._ctx.agent_server
        self._updating = True
        self.api_toggle.setChecked(server is not None)
        self._updating = False
        self.port.setEnabled(server is None)
        self.api_status.setText(f"Escuchando en 127.0.0.1:{server.port}" if server else "Apagada")

    def _refresh_project(self) -> None:
        """Mantiene al día el proyecto creado antes si cambió el puerto."""
        ctx = self._ctx
        settings = config.get_settings(ctx.conn, ctx.workspace_id)
        if settings.project_path and (settings.project_path / "AGENTS.md").exists():
            try:
                project.create_project(settings.project_path, ctx.folder, settings.port, settings.token)
            except (ProjectError, OSError):
                logger.warning("No se pudo actualizar el proyecto del agente", exc_info=True)

    def _choose_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta del proyecto", str(Path.home()))
        if folder:
            self.create_project_at(Path(folder))

    def create_project_at(self, folder: Path) -> None:
        ctx = self._ctx
        config.set_port(ctx.conn, ctx.workspace_id, self.port.value())
        settings = config.get_settings(ctx.conn, ctx.workspace_id)
        try:
            created = project.create_project(folder, ctx.folder, settings.port, settings.token)
        except (ProjectError, OSError) as error:
            logger.warning("No se pudo crear el proyecto del agente", exc_info=True)
            show_error(self, str(error))
            return
        config.set_project_path(ctx.conn, ctx.workspace_id, created)
        logger.info("Proyecto del agente creado en %s", created)
        self.status.setText(f"Proyecto creado en {created}. Ábrelo en VS Code y activa la API para consultarlo.")

    def _rotate(self) -> None:
        if confirm(
            self, "Renovar clave",
            "Los proyectos que usen la clave actual dejarán de funcionar. El último proyecto creado se actualiza solo.",
            "Renovar",
        ):
            self.rotate()

    def rotate(self) -> None:
        ctx = self._ctx
        token = config.rotate_token(ctx.conn, ctx.workspace_id)
        if ctx.agent_server is not None:
            ctx.agent_server.token = token
        settings = config.get_settings(ctx.conn, ctx.workspace_id)
        if settings.project_path and settings.project_path.is_dir():
            try:
                project.write_curl_config(settings.project_path, token)
            except OSError:
                logger.warning("No se pudo actualizar la clave del proyecto", exc_info=True)
        logger.info("Clave del agente renovada")
        self.status.setText("Clave renovada.")

    def _lookup(self) -> None:
        text = self.lookup_input.text().strip()
        if not text or self._ctx.workspace_id is None:
            return
        try:
            found = resolve_pseudonym(self._ctx.conn, self._ctx.workspace_id, text)
        except AgentError as error:
            show_error(self, str(error))
            return
        logger.info("Consulta local de un seudónimo: %d registros", len(found))
        if not found:
            self.status.setText("No hay registros con ese seudónimo.")
            return
        lines = [f"{label}: {value}" for label, value in found[0].items()]
        extra = f"\n\nTiene {len(found)} registros; este es el primero." if len(found) > 1 else ""
        QMessageBox.information(self, text.upper(), "\n".join(lines) + extra)
