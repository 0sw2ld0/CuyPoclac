"""API HTTP local para el agente: solo 127.0.0.1, con clave y de solo lectura."""

import hmac
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import db
from . import queries
from .queries import AgentError

_MAX_BODY = 64 * 1024

logger = logging.getLogger(__name__)

_ROUTES = {
    ("GET", "/api/estado"): lambda conn, ws, _: queries.status(conn, ws),
    ("GET", "/api/esquema"): lambda conn, ws, _: queries.schema_info(conn, ws),
    ("POST", "/api/registros"): queries.list_records,
    ("POST", "/api/ranking"): queries.ranking,
    ("POST", "/api/agregado"): queries.aggregate,
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "CuyPoclac"
    sys_version = ""
    timeout = 5  # Un cliente lento no debe impedir cerrar la app.

    def log_message(self, format, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def _serve(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        code, payload = self._respond(method, path)
        logger.log(
            logging.WARNING if code >= 400 else logging.INFO, "%s %s -> %d", method, path, code,
        )
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _respond(self, method: str, path: str) -> tuple[int, dict]:
        api: AgentServer = self.server.api
        # Un navegador siempre envía Origin en estas peticiones, y Host evita el DNS rebinding.
        if self.headers.get("Origin") or self.headers.get("Host") not in api.allowed_hosts:
            return 403, {"error": "Solo se admiten clientes locales que no sean navegadores."}
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, f"Bearer {api.token}".encode("utf-8")):
            return 401, {"error": "Clave incorrecta. Vuelve a generar el proyecto desde la app."}
        route = _ROUTES.get((method, path))
        if route is None:
            return 404, {"error": "Ruta desconocida. Rutas: " + ", ".join(sorted(p for _, p in _ROUTES))}
        try:
            body = self._read_body() if method == "POST" else {}
            conn = db.connect_readonly(api.db_path, api.key)
            try:
                return 200, route(conn, api.workspace_id, body)
            finally:
                conn.close()
        except AgentError as error:
            return 400, {"error": str(error)}
        except Exception:
            logger.exception("Falló una consulta del agente en %s", path)
            return 500, {"error": "Error interno de la app. El detalle quedó en su log."}

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise AgentError("Content-Length inválido.") from None
        if not 0 <= length <= _MAX_BODY:
            raise AgentError("La consulta es demasiado grande.")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise AgentError("El cuerpo debe ser JSON válido.") from None
        if not isinstance(body, dict):
            raise AgentError("El cuerpo debe ser un objeto JSON.")
        return body


class _Server(ThreadingHTTPServer):
    # Al cerrar se esperan las consultas en curso: la base se puede restaurar justo después.
    daemon_threads = False
    # En Windows permitiría que otro programa escuche en el mismo puerto.
    allow_reuse_address = sys.platform != "win32"


class AgentServer:
    def __init__(self, db_path: Path, key: str | None, workspace_id: int, token: str, port: int) -> None:
        self.db_path, self.key, self.workspace_id, self.token = db_path, key, workspace_id, token
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._server.api = self
        self.port = self._server.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self._thread = threading.Thread(target=self._server.serve_forever, name="api-agente", daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("API del agente activada en 127.0.0.1:%d", self.port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
        logger.info("API del agente desactivada")
