"""Log local de la app: eventos y errores. Los mensajes propios no incluyen valores de celdas."""

import faulthandler
import logging
import platform
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .db import data_dir

LOG_NAME = "cuypoclac.log"
CRASH_NAME = "crash.log"
_MAX_BYTES = 1024 * 1024
_BACKUPS = 3
_TAIL_BYTES = 512 * 1024
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_ENTRY_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (\w+)")
_PROBLEM_LEVELS = {"WARNING", "ERROR", "CRITICAL"}

logger = logging.getLogger("cuypoclac")


def log_dir() -> Path:
    return data_dir() / "logs"


def log_path() -> Path:
    return log_dir() / LOG_NAME


def setup() -> None:
    """Sin log la app sigue funcionando: un fallo aquí no debe impedir abrirla."""
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    formatter = logging.Formatter(_FORMAT)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    except OSError:
        pass
    else:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    # En la app empaquetada no hay consola y stderr es None.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
    logger.info(
        "Inicio: CuyPoclac %s, Python %s, %s, empaquetada=%s",
        __version__, platform.python_version(), platform.platform(), getattr(sys, "frozen", False),
    )


def install_hooks(on_uncaught=None) -> None:
    """Registra lo que nadie capturó. on_uncaught se llama solo desde el hilo principal."""

    def handle(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Error no controlado", exc_info=(exc_type, exc, tb))
        if on_uncaught and threading.current_thread() is threading.main_thread():
            on_uncaught()

    def handle_thread(args) -> None:
        if args.exc_type is not SystemExit:
            logger.critical(
                "Error no controlado en el hilo %s", args.thread.name if args.thread else "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

    sys.excepthook = handle
    threading.excepthook = handle_thread
    # Los cierres nativos (Qt, SQLite) no pasan por Python: quedan en su propio archivo.
    try:
        crash_file = (log_dir() / CRASH_NAME).open("a", encoding="utf-8")
    except OSError:
        return
    faulthandler.enable(crash_file)


def read_log(only_problems: bool = False) -> str:
    """El final del log actual. Las líneas de un traceback siguen a su entrada."""
    try:
        with log_path().open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - _TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    if size > _TAIL_BYTES:
        text = text.partition("\n")[2]
    if not only_problems:
        return text
    kept = []
    keep = False
    for line in text.splitlines():
        match = _ENTRY_START.match(line)
        if match:
            keep = match.group(1) in _PROBLEM_LEVELS
        if keep:
            kept.append(line)
    return "\n".join(kept)
