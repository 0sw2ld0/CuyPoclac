"""Conexión a la base local: ubicación, migraciones internas, cifrado y backups."""

import os
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_MIGRATIONS = Path(__file__).parent / "migrations_sql"
_PLAIN_HEADER = b"SQLite format 3\x00"
DB_NAME = "cuypoclac.db"


def default_db_path() -> Path:
    """La base vive fuera de la carpeta de trabajo: esa carpeta nunca se modifica."""
    override = os.environ.get("CUYPOCLAC_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CuyPoclac"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "CuyPoclac"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "cuypoclac"
    base.mkdir(parents=True, exist_ok=True)
    return base / DB_NAME


def is_encrypted(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        return handle.read(16) != _PLAIN_HEADER


def encryption_available() -> bool:
    try:
        import argon2  # noqa: F401
        import sqlcipher3  # noqa: F401
    except ImportError:
        return False
    return True


def _salt_path(path: Path) -> Path:
    return path.with_name(path.name + ".salt")


def derive_key(path: Path, password: str) -> str:
    """Deriva la clave con Argon2. La sal se guarda junto a la base y no es secreta."""
    from argon2.low_level import Type, hash_secret_raw

    salt_file = _salt_path(path)
    if not salt_file.exists():
        salt_file.write_bytes(os.urandom(16))
    raw = hash_secret_raw(
        password.encode("utf-8"), salt_file.read_bytes(),
        time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, type=Type.ID,
    )
    return raw.hex()


def connect(path: Path, key: str | None = None):
    if key is not None:
        import sqlcipher3 as module
    else:
        module = sqlite3
    conn = module.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = module.Row
    if key is not None:
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    _apply_internal_migrations(conn)
    return conn


def _apply_internal_migrations(conn) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for script in sorted(_MIGRATIONS.glob("*.sql")):
        number = int(script.name.split("_", 1)[0])
        if number > current:
            conn.executescript(
                "BEGIN;\n" + script.read_text(encoding="utf-8") + f"\nPRAGMA user_version = {number};\nCOMMIT;"
            )


@contextmanager
def transaction(conn) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def backup(conn, path: Path, version: int) -> Path:
    """Copia el archivo tal cual, así el backup conserva el cifrado de la base."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(f"{path.name}.bak-v{version}-{stamp}")
    shutil.copy2(path, target)
    return target


def list_backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"), reverse=True)


def restore_backup(path: Path, backup_path: Path) -> None:
    """Reemplaza la base por un backup. Todas las conexiones deben estar cerradas."""
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.unlink()
    shutil.copy2(backup_path, path)


def enable_encryption(path: Path, password: str) -> str:
    """Cifra la base y elimina los backups sin cifrar. Devuelve la clave derivada."""
    import sqlcipher3

    plain = sqlite3.connect(path)
    plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    plain.close()
    key = derive_key(path, password)
    temporary = path.with_name(path.name + ".encrypting")
    if temporary.exists():
        temporary.unlink()
    conn = sqlcipher3.connect(path, isolation_level=None)
    conn.execute(f"ATTACH DATABASE ? AS encrypted KEY \"x'{key}'\"", (str(temporary),))
    conn.execute("SELECT sqlcipher_export('encrypted')")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute(f"PRAGMA encrypted.user_version = {version}")
    conn.execute("DETACH DATABASE encrypted")
    conn.close()
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.unlink()
    temporary.replace(path)
    for old in list_backups(path):
        if not is_encrypted(old):
            old.unlink()
    return key
