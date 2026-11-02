"""La API del agente: lo no compartido nunca sale, y se prueba con el curl real."""

import json
import logging
import shutil
import stat
import subprocess
import sys
from datetime import date

import pytest

from cuypoclac import db
from cuypoclac.agent import config, project, queries
from cuypoclac.agent.project import ProjectError
from cuypoclac.agent.queries import AgentError
from cuypoclac.agent.server import AgentServer
from cuypoclac.db import repo
from cuypoclac.importing.importer import scan_workspace
from cuypoclac.migration.apply import apply_migration

from .conftest import build_workbook

ROWS = [
    ("Ana Ficticia", "secreto-uno", "Alto", date(2026, 1, 10), 90),
    ("Ana Ficticia", "secreto-dos", "Alto", date(2026, 2, 10), 70),
    ("Beto Ficticio", "secreto-tres", "Bajo", date(2026, 1, 10), 40),
    ("Caro Ficticia", "secreto-cuatro", "Medio", date(2026, 3, 10), 60),
]

curl_required = pytest.mark.skipif(shutil.which("curl") is None, reason="curl no está instalado")


def fid(conn, workspace, label):
    schema = repo.current_schema(conn, workspace)
    return next(f.id for f in schema.active_fields if f.label == label)


@pytest.fixture
def env(tmp_path):
    folder = tmp_path / "carpeta"
    build_workbook(folder / "a.xlsx", rows=ROWS)
    db_path = tmp_path / db.DB_NAME
    conn = db.connect(db_path)
    workspace = repo.get_or_create_workspace(conn, folder)
    scan_workspace(conn, workspace)
    config.get_settings(conn, workspace)
    yield conn, workspace, folder, db_path
    conn.close()


@pytest.fixture
def shared(env):
    conn, workspace, *_ = env
    ids = {label: fid(conn, workspace, label) for label in ("Código", "Campo Dos", "Nivel", "Fecha", "Cantidad")}
    config.save_fields(conn, workspace, [ids["Nivel"], ids["Fecha"]], ids["Código"], [ids["Cantidad"]], [])
    return ids


@pytest.fixture
def server(env, shared):
    conn, workspace, _, db_path = env
    api = AgentServer(db_path, None, workspace, config.get_settings(conn, workspace).token, 0)
    api.start()
    yield api
    api.stop()


def curl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["curl", "-s", "--noproxy", "*", *args], capture_output=True, text=True, timeout=20)


def call(server, path, body=None, *extra):
    args = ["-H", f"Authorization: Bearer {server.token}", *extra, f"http://127.0.0.1:{server.port}{path}"]
    if body is not None:
        args += ["-d", json.dumps(body)]
    return json.loads(curl(*args).stdout)


def status_code(*args) -> str:
    return curl("-o", "/dev/null", "-w", "%{http_code}", *args).stdout


def test_nothing_is_shared_until_marked(env):
    conn, workspace, *_ = env
    assert queries.schema_info(conn, workspace)["campos"] == []
    result = queries.list_records(conn, workspace, {})
    assert result["total"] == 4
    assert set(result["registros"][0]) == {"persona"}
    assert result["advertencias"]


def test_person_and_hidden_fields_never_appear(env, shared):
    conn, workspace, *_ = env
    outputs = [
        queries.status(conn, workspace),
        queries.schema_info(conn, workspace),
        queries.list_records(conn, workspace, {}),
        queries.ranking(conn, workspace, {"metrica": shared["Cantidad"]}),
        queries.aggregate(conn, workspace, {"agrupar_por": shared["Nivel"], "metrica": shared["Cantidad"]}),
    ]
    text = json.dumps(outputs, ensure_ascii=False)
    assert "Ficticia" not in text and "Ficticio" not in text and "secreto" not in text
    assert "Campo Dos" not in text and "Código" not in text


@pytest.mark.parametrize("label", ["Código", "Campo Dos"])
def test_hidden_fields_cannot_be_probed(env, shared, label):
    conn, workspace, *_ = env
    hidden = shared[label]
    for call_query, body in [
        (queries.list_records, {"filtros": {hidden: "Ana Ficticia"}}),
        (queries.list_records, {"rangos": {hidden: {"desde": "A"}}}),
        (queries.list_records, {"orden": hidden}),
        (queries.aggregate, {"agrupar_por": hidden}),
        (queries.aggregate, {"metrica": hidden}),
        (queries.ranking, {"metrica": hidden}),
    ]:
        with pytest.raises(AgentError, match="Campo no disponible"):
            call_query(conn, workspace, body)


def test_ranking_averages_per_person_and_respects_direction(env, shared):
    conn, workspace, *_ = env
    metric = shared["Cantidad"]
    result = queries.ranking(conn, workspace, {"metrica": metric, "orden": "peores"})
    assert [p["promedio"] for p in result["personas"]] == [40, 60, 80]
    assert result["personas"][2]["registros"] == 2
    assert result["total_personas"] == 3
    assert result["advertencias"] == []
    best = queries.ranking(conn, workspace, {"metrica": metric, "orden": "mejores", "limite": 1})
    assert [p["promedio"] for p in best["personas"]] == [80]

    config.save_fields(conn, workspace, [], shared["Código"], [], [metric])
    lower = queries.ranking(conn, workspace, {"metrica": metric, "orden": "peores"})
    assert [p["promedio"] for p in lower["personas"]] == [80, 60, 40]


def test_ties_share_position_and_non_numeric_are_counted(tmp_path):
    folder = tmp_path / "carpeta"
    build_workbook(folder / "a.xlsx", rows=[
        ("A", "x", "Alto", date(2026, 1, 1), 5), ("B", "x", "Alto", date(2026, 1, 1), 5),
        ("C", "x", "Alto", date(2026, 1, 1), 7), ("D", "x", "Alto", date(2026, 1, 1), "n/a"),
        ("E", "x", "Alto", date(2026, 1, 1), "nan"),
    ])
    conn = db.connect(tmp_path / db.DB_NAME)
    workspace = repo.get_or_create_workspace(conn, folder)
    scan_workspace(conn, workspace)
    metric = fid(conn, workspace, "Cantidad")
    config.save_fields(conn, workspace, [], fid(conn, workspace, "Código"), [metric], [])
    result = queries.ranking(conn, workspace, {"metrica": metric})
    assert [p["posicion"] for p in result["personas"]] == [1, 1, 3]
    assert result["registros_sin_valor_numerico"] == 2
    conn.close()


def test_filters_ranges_and_limits(env, shared):
    conn, workspace, *_ = env
    assert queries.list_records(conn, workspace, {"filtros": {shared["Nivel"]: "Alto"}})["total"] == 2
    period = {"rangos": {shared["Fecha"]: {"desde": "2026-01-01", "hasta": "2026-01-31"}}}
    assert queries.list_records(conn, workspace, period)["total"] == 2
    ordered = queries.list_records(conn, workspace, {"orden": shared["Cantidad"], "descendente": True})
    assert ordered["registros"][0][shared["Cantidad"]] == 90
    assert len(queries.list_records(conn, workspace, {"limite": 1000})["registros"]) == 4
    with pytest.raises(AgentError):
        queries.list_records(conn, workspace, {"limite": "mucho"})
    with pytest.raises(AgentError, match="no admite rangos"):
        queries.list_records(conn, workspace, {"rangos": {shared["Nivel"]: {"desde": "A"}}})


def test_aggregate_counts_people(env, shared):
    conn, workspace, *_ = env
    result = queries.aggregate(conn, workspace, {"agrupar_por": shared["Nivel"], "metrica": shared["Cantidad"]})
    high = next(g for g in result["grupos"] if g["valor"] == "Alto")
    assert (high["registros"], high["personas"], high["promedio"]) == (2, 1, 80)


def test_pseudonym_is_stable_secret_and_resolves_locally(env, shared):
    conn, workspace, *_ = env
    person = queries.ranking(conn, workspace, {"metrica": shared["Cantidad"]})["personas"][0]["persona"]
    assert queries.list_records(conn, workspace, {"persona": person})["total"] == 1
    assert queries.resolve_pseudonym(conn, workspace, person.lower())[0]["Código"] == "Beto Ficticio"
    assert queries.resolve_pseudonym(conn, workspace, "P-0000000000") == []
    # Con otro secreto, el mismo nombre da otro seudónimo: no se puede calcular sin la app.
    conn.execute("UPDATE agent_settings SET pseudonym_secret = ?", ("ab" * 32,))
    assert queries.ranking(conn, workspace, {"metrica": shared["Cantidad"]})["personas"][0]["persona"] != person


def test_new_fields_after_migration_stay_hidden(env, shared):
    conn, workspace, folder, db_path = env
    op = {"op": "add_field", "id": "f_50", "label": "Nuevo privado", "group": None, "type": "text", "options": []}
    apply_migration(conn, db_path, workspace, [op], None)
    labels = [f["etiqueta"] for f in queries.schema_info(conn, workspace)["campos"]]
    assert "Nuevo privado" not in labels
    assert {"Nivel", "Fecha", "Cantidad"} == set(labels)


def test_schema_page_roles_do_not_erase_agent_roles(env, shared):
    from cuypoclac import records

    conn, workspace, *_ = env
    records.set_roles(conn, workspace, {"key": [], "date": [], "owner": [], "filterable": [shared["Nivel"]]})
    assert config.get_fields(conn, workspace)[config.ROLE_PERSON] == [shared["Código"]]


def test_readonly_connection_rejects_writes(env):
    *_, db_path = env
    conn = db.connect_readonly(db_path)
    with pytest.raises(Exception, match="readonly|read-only"):
        conn.execute("DELETE FROM record")
    conn.close()


@curl_required
def test_http_rejects_missing_or_wrong_credentials(server):
    url = f"http://127.0.0.1:{server.port}/api/estado"
    assert status_code(url) == "401"
    assert status_code("-H", "Authorization: Bearer incorrecta", url) == "401"
    auth = ("-H", f"Authorization: Bearer {server.token}")
    assert status_code(*auth, url) == "200"
    assert status_code(*auth, "-H", "Origin: https://sitio.example", url) == "403"
    assert status_code(*auth, "-H", "Host: atacante.example", url) == "403"
    assert status_code(*auth, f"http://127.0.0.1:{server.port}/api/otra") == "404"
    assert status_code(*auth, "-X", "DELETE", url) == "501"


@curl_required
def test_http_bad_input_is_a_clear_error(server, shared):
    assert "JSON" in call(server, "/api/registros", None, "-d", "no json")["error"]
    assert "no disponible" in call(server, "/api/ranking", {"metrica": shared["Código"]})["error"]
    big = curl(
        "-H", f"Authorization: Bearer {server.token}", "--data-binary", "x" * 70000,
        f"http://127.0.0.1:{server.port}/api/registros",
    )
    assert "demasiado grande" in big.stdout


def test_api_listens_only_on_loopback(server):
    assert server._server.server_address[0] == "127.0.0.1"


@curl_required
def test_project_works_with_real_curl_config(env, server, shared, tmp_path):
    conn, workspace, folder, _ = env
    target = project.create_project(tmp_path / "proyecto", folder, server.port, server.token)
    (target / ".cuypoclac" / "consulta.json").write_text(json.dumps({"metrica": shared["Cantidad"], "limite": 1}), encoding="utf-8")
    result = subprocess.run(
        ["curl", "-K", ".cuypoclac/curl.cfg", f"http://127.0.0.1:{server.port}/api/ranking", "-d", "@.cuypoclac/consulta.json"],
        cwd=target, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout)["personas"][0]["promedio"] == 40


def test_project_files_keep_secrets_out_of_instructions(env, tmp_path):
    conn, workspace, folder, _ = env
    target = project.create_project(tmp_path / "proyecto", folder, 8765, "clave-de-prueba")
    agents = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert "clave-de-prueba" not in agents
    assert "Cantidad" not in agents
    assert "127.0.0.1:8765" in agents
    config_file = target / ".cuypoclac" / "curl.cfg"
    assert "clave-de-prueba" in config_file.read_text(encoding="utf-8")
    assert 'noproxy = "*"' in config_file.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    ignored = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".cuypoclac/" in ignored and "AGENTS.md" in ignored


def test_project_respects_work_folder_and_foreign_files(env, tmp_path):
    _, _, folder, _ = env
    before = sorted(p.name for p in folder.iterdir())
    with pytest.raises(ProjectError):
        project.create_project(folder / "sub", folder, 8765, "x")
    assert sorted(p.name for p in folder.iterdir()) == before
    other = tmp_path / "ajeno"
    other.mkdir()
    (other / "AGENTS.md").write_text("instrucciones de alguien más", encoding="utf-8")
    (other / ".gitignore").write_text("mio.txt", encoding="utf-8")
    with pytest.raises(ProjectError):
        project.create_project(other, folder, 8765, "x")
    assert (other / "AGENTS.md").read_text(encoding="utf-8") == "instrucciones de alguien más"
    (other / "AGENTS.md").unlink()
    project.create_project(other, folder, 8765, "x")
    project.create_project(other, folder, 9000, "y")
    assert (other / ".gitignore").read_text(encoding="utf-8").splitlines() == ["mio.txt", ".cuypoclac/", "AGENTS.md"]
    assert "127.0.0.1:9000" in (other / "AGENTS.md").read_text(encoding="utf-8")


def test_windows_instructions_use_curl_exe():
    text = project._agents_text(8765, windows=True)
    assert "curl.exe -K .cuypoclac\\curl.cfg" in text


@curl_required
def test_audit_log_has_no_values_or_token(server, shared, caplog):
    caplog.set_level(logging.INFO, logger="cuypoclac")
    call(server, "/api/registros", {"filtros": {shared["Nivel"]: "Alto"}})
    curl(f"http://127.0.0.1:{server.port}/api/estado")
    assert "POST /api/registros -> 200" in caplog.text
    assert "GET /api/estado -> 401" in caplog.text
    assert "Alto" not in caplog.text
    assert server.token not in caplog.text


@curl_required
def test_api_works_on_encrypted_database(env, tmp_path):
    if not db.encryption_available():
        pytest.skip("sin soporte de cifrado")
    conn, workspace, _, db_path = env
    conn.close()
    key = db.enable_encryption(db_path, "contraseña-de-prueba")
    secure = db.connect(db_path, key)
    token = config.get_settings(secure, workspace).token
    api = AgentServer(db_path, key, workspace, token, 0)
    api.start()
    try:
        assert call(api, "/api/estado")["registros"] == 4
    finally:
        api.stop()
        secure.close()


def test_port_in_use_raises_oserror(env, server):
    conn, workspace, _, db_path = env
    with pytest.raises(OSError):
        AgentServer(db_path, None, workspace, "x", server.port)
