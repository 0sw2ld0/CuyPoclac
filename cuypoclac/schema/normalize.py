"""Normalización de etiquetas: la huella y el diff comparan siempre esta forma."""

import re
import unicodedata

_NOT_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_label(label: object) -> str:
    text = unicodedata.normalize("NFKD", str(label or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _NOT_WORD.sub(" ", text).replace("_", " ")
    return _SPACES.sub(" ", text).strip()
