# Compilar con: pyinstaller packaging/cuypoclac.spec --noconfirm
# Modo carpeta: reduce los falsos positivos de antivirus frente al archivo único.
import sys
from pathlib import Path

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root)],
    datas=[(str(root / "cuypoclac" / "db" / "migrations_sql"), "cuypoclac/db/migrations_sql")],
    hiddenimports=["sqlcipher3", "argon2"],
    excludes=[
        "tkinter", "unittest", "pydoc", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.QtWebEngineCore", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL",
        "PySide6.QtPdf", "PySide6.QtSvg", "PySide6.QtDesigner",
    ],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CuyPoclac", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CuyPoclac")

if sys.platform == "darwin":
    app = BUNDLE(coll, name="CuyPoclac.app", bundle_identifier="com.cuycoders.cuypoclac")
