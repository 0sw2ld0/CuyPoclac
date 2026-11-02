# CuyPoclac

Aplicación de escritorio para Windows y macOS que consolida y consulta información tomada de archivos Excel ubicados en una carpeta.

La aplicación no conoce de antemano ningún campo: descubre la estructura desde los propios archivos. Trabaja de forma local, no usa ningún servidor y nunca modifica los archivos de origen.

## Funciones

- Descubre la estructura de los archivos sin configuración previa.
- Detecta cambios de estructura y propone una migración que se revisa y simula antes de aplicarla.
- Consulta, filtra, edita y exporta la información consolidada.
- Permite cifrar la base local con una contraseña.

Formatos admitidos: `.xlsx`, `.xls` y `.csv`.

## Desarrollo

Requiere Python 3.12 o superior.

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev,encryption]"
```

Abrir la aplicación:

```bash
.venv/bin/python -m cuypoclac.app
```

Ejecutar las pruebas:

```bash
.venv/bin/python -m pytest
```

Generar la aplicación empaquetada, en el mismo sistema donde se usará:

```bash
.venv/bin/pyinstaller packaging/cuypoclac.spec --noconfirm
```

## Datos

No agregues archivos de datos reales ni bases locales a este repositorio. Las pruebas generan sus propios archivos con datos ficticios.
