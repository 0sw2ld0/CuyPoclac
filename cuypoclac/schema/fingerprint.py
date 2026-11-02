"""Huella del esquema: depende solo de la estructura del archivo, nunca de sus datos."""

import hashlib

from .model import SchemaCandidate


def canonical_form(candidate: SchemaCandidate) -> str:
    repeatable = {g.key for g in candidate.groups if g.repeatable}
    lines = []
    for field in candidate.fields:
        group = field.group or ""
        marker = "*" if group in repeatable else ""
        options = ",".join(sorted(field.options))
        lines.append(f"{group}{marker}|{field.normalized_label}|{field.declared_type}|{options}")
    return ";".join(sorted(lines))


def fingerprint(candidate: SchemaCandidate) -> str:
    return hashlib.sha256(canonical_form(candidate).encode("utf-8")).hexdigest()
