"""Lanzador para PyInstaller: necesita un script fuera del paquete."""

import sys

from cuypoclac.app import main

if __name__ == "__main__":
    sys.exit(main())
