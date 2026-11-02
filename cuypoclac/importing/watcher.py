"""Vigila la carpeta y agrupa los eventos: Excel guarda en varios pasos."""

import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..excel.workbook import SUPPORTED_SUFFIXES

DEBOUNCE_SECONDS = 2.0


class _Handler(FileSystemEventHandler):
    def __init__(self, trigger: Callable[[], None]) -> None:
        self._trigger = trigger

    def on_any_event(self, event) -> None:
        if event.is_directory or event.event_type in {"opened", "closed_no_write"}:
            return
        paths = [event.src_path, getattr(event, "dest_path", "") or ""]
        for raw in paths:
            path = Path(str(raw))
            # Un ~$ que aparece o desaparece también indica que hay que revisar el archivo.
            if path.suffix.lower() in SUPPORTED_SUFFIXES:
                self._trigger()
                return


class FolderWatcher:
    def __init__(self, folder: Path, on_change: Callable[[], None], debounce: float = DEBOUNCE_SECONDS) -> None:
        self._on_change = on_change
        self._debounce = debounce
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._observer = Observer()
        self._observer.schedule(_Handler(self._schedule), str(folder), recursive=True)

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._on_change)
            self._timer.daemon = True
            self._timer.start()

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
        self._observer.stop()
        self._observer.join(timeout=5)
