"""Conversión de fechas: seriales de Excel y texto en varios formatos."""

from datetime import date, datetime, timedelta

_TEXT_FORMATS = (
    "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y",
    "%d-%m-%y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
)
_FORMAT_ALIASES = {"dd/MM/yyyy": "%d/%m/%Y", "yyyy-MM-dd": "%Y-%m-%d", "dd-MM-yyyy": "%d-%m-%Y"}


def serial_to_date(serial: float) -> date | None:
    """Excel trata 1900 como bisiesto: el serial 60 (29/02/1900) no existe."""
    if serial < 1 or serial >= 2958466 or int(serial) == 60:
        return None
    base = date(1899, 12, 31) if serial < 60 else date(1899, 12, 30)
    return base + timedelta(days=int(serial))


def parse_date(value: object, formats: list[str] | None = None) -> str | None:
    """Devuelve la fecha en ISO, o None si el valor no es una fecha."""
    if isinstance(value, datetime):
        if value.time() == datetime.min.time():
            return value.date().isoformat()
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    allow_serial = formats is None or "excel_serial" in formats
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        converted = serial_to_date(value) if allow_serial else None
        return converted.isoformat() if converted else None
    if isinstance(value, str):
        text = value.strip()
        if formats is None:
            patterns = _TEXT_FORMATS
        else:
            patterns = tuple(_FORMAT_ALIASES.get(f, f) for f in formats if f != "excel_serial")
        for pattern in patterns:
            try:
                parsed = datetime.strptime(text, pattern)
            except ValueError:
                continue
            if parsed.time() == datetime.min.time():
                return parsed.date().isoformat()
            return parsed.isoformat(timespec="seconds")
    return None


def looks_like_date(value: object) -> bool:
    """Sin formato de celda, un número no basta para decidir que es fecha."""
    if isinstance(value, (datetime, date)):
        return True
    return isinstance(value, str) and parse_date(value) is not None
