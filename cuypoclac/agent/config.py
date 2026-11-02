"""Qué ve el agente y con qué credenciales. Lo no marcado no existe para la API."""

import secrets
from dataclasses import dataclass
from pathlib import Path

from .. import records
from ..db import repo

ROLE_SHARED = "agent_shared"
ROLE_PERSON = "agent_person"
ROLE_HIGHER_BETTER = "agent_metric_high"
ROLE_LOWER_BETTER = "agent_metric_low"
DEFAULT_PORT = 8765


@dataclass
class AgentSettings:
    token: str
    pseudonym_secret: str
    port: int
    project_path: Path | None


def get_settings(conn, workspace_id: int) -> AgentSettings:
    row = conn.execute("SELECT * FROM agent_settings WHERE workspace_id = ?", (workspace_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO agent_settings(workspace_id, token, pseudonym_secret, port) VALUES (?, ?, ?, ?)",
            (workspace_id, secrets.token_urlsafe(32), secrets.token_hex(32), DEFAULT_PORT),
        )
        return get_settings(conn, workspace_id)
    project = row["project_path"]
    return AgentSettings(row["token"], row["pseudonym_secret"], row["port"], Path(project) if project else None)


def set_port(conn, workspace_id: int, port: int) -> None:
    get_settings(conn, workspace_id)
    conn.execute("UPDATE agent_settings SET port = ? WHERE workspace_id = ?", (port, workspace_id))


def set_project_path(conn, workspace_id: int, path: Path) -> None:
    conn.execute("UPDATE agent_settings SET project_path = ? WHERE workspace_id = ?", (str(path), workspace_id))


def rotate_token(conn, workspace_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE agent_settings SET token = ? WHERE workspace_id = ?", (token, workspace_id))
    return token


def save_fields(
    conn, workspace_id: int, shared: list[str], person: str | None, higher_better: list[str], lower_better: list[str]
) -> None:
    """La persona nunca se comparte: solo sirve para calcular su seudónimo."""
    if set(higher_better) & set(lower_better):
        raise ValueError("Una métrica no puede tener las dos direcciones.")
    metrics = {*higher_better, *lower_better}
    if person in metrics:
        raise ValueError("El campo de la persona no puede ser una métrica.")
    records.set_roles(conn, workspace_id, {
        ROLE_SHARED: sorted((set(shared) | metrics) - {person}),
        ROLE_PERSON: [person] if person else [],
        ROLE_HIGHER_BETTER: list(higher_better),
        ROLE_LOWER_BETTER: list(lower_better),
    })


def get_fields(conn, workspace_id: int) -> dict[str, list[str]]:
    schema = repo.current_schema(conn, workspace_id)
    roles = repo.get_roles(conn, schema.version_id) if schema else {}
    return {role: roles.get(role, []) for role in (ROLE_SHARED, ROLE_PERSON, ROLE_HIGHER_BETTER, ROLE_LOWER_BETTER)}
