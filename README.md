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

## Carpetas

Para trabajar con otra carpeta, pulsa **Elegir carpeta…** en **Inicio**. Cada carpeta conserva sus datos por separado y, al abrir la app, se carga la última que usaste.

**Quitar carpeta…** borra de la base los datos de la carpeta actual, incluidas las ediciones y los registros manuales. Antes guarda un backup, que se puede restaurar desde **Esquema** y sigue conteniendo esos datos. La carpeta y sus archivos no se tocan.

## Agente

La pantalla **Agente** permite consultar los datos desde un agente de VS Code mediante una API local y curl:

1. Marca los campos que se pueden compartir, el campo que identifica a la persona y las métricas de desempeño.
2. Pulsa **Crear proyecto** y elige una carpeta fuera de la carpeta de trabajo. Se generan `AGENTS.md` y `.cuypoclac/curl.cfg`.
3. Activa la API y abre esa carpeta en VS Code.

La API está apagada al abrir la app y solo acepta conexiones de este equipo con una clave. Es de solo lectura, oculta por defecto todos los campos y entrega a las personas como seudónimos. Cada consulta queda en el log. Lo que reciba el agente puede salir del equipo si usa un modelo en la nube.

## Log

La pantalla **Log** muestra los archivos abiertos, las operaciones y los errores. Se guarda solo en el equipo:

- macOS: `~/Library/Application Support/CuyPoclac/logs/`
- Windows: `%LOCALAPPDATA%\CuyPoclac\logs\`

`cuypoclac.log` rota al llegar a 1 MB y conserva tres copias. `crash.log` recoge los cierres inesperados del proceso.

## Datos

No agregues archivos de datos reales ni bases locales a este repositorio. Las pruebas generan sus propios archivos con datos ficticios.
