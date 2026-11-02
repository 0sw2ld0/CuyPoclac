"""Compara un esquema vigente con uno candidato y propone operaciones de migración."""

from collections.abc import Callable

from msgspec import Struct, field as struct_field
from rapidfuzz.distance import JaroWinkler
from rapidfuzz.fuzz import token_sort_ratio

from .model import SchemaCandidate
from .normalize import normalize_label

RENAME_THRESHOLD = 0.8
OPTION_THRESHOLD = 0.75
VALUES_THRESHOLD = 0.6
MAX_POSITION_GAP = 2


class FieldDef(Struct):
    id: str
    label: str
    normalized_label: str
    group_key: str | None
    type: str
    declared_type: str
    options: list[str]
    ord: int
    status: str = "active"
    group_id: str | None = None

    @property
    def key(self) -> str:
        return f"{self.group_key or ''}|{self.normalized_label}"


class GroupDef(Struct):
    id: str
    key: str
    label: str
    repeatable: bool = False
    repeat_mode: str | None = None
    status: str = "active"


class Proposal(Struct):
    op: dict
    confidence: str
    reason: str
    # Operaciones que se aplican si el usuario rechaza esta propuesta.
    alternative: list[dict] = struct_field(default_factory=list)
    required: bool = False


def diff_schemas(
    old_fields: list[FieldDef],
    old_groups: list[GroupDef],
    candidate: SchemaCandidate,
    old_samples: dict[str, list[str]],
    new_samples: dict[str, list[str]],
    aliases: dict[str, str],
    next_field_id: Callable[[], str],
    next_group_id: Callable[[], str],
) -> list[Proposal]:
    proposals: list[Proposal] = []
    active = [f for f in old_fields if f.status == "active"]
    active_groups = {g.key: g for g in old_groups if g.status == "active"}

    # Grupos: renombrados primero, para no mover cada campo de un grupo renombrado.
    group_ids: dict[str, str] = {k: g.id for k, g in active_groups.items()}
    new_groups = {g.key: g for g in candidate.groups}
    unmatched_old_groups = [k for k in active_groups if k not in new_groups]
    for group in candidate.groups:
        if group.key in active_groups:
            continue
        best, best_score = None, 0.0
        new_labels = {f.normalized_label for f in candidate.fields if f.group == group.key}
        for key in unmatched_old_groups:
            old_labels = {f.normalized_label for f in active if f.group_key == key}
            overlap = len(new_labels & old_labels) / max(len(old_labels), 1)
            score = max(JaroWinkler.normalized_similarity(key, group.key), overlap)
            if score > best_score:
                best, best_score = key, score
        if best is not None and best_score >= 0.6:
            unmatched_old_groups.remove(best)
            old_group = active_groups[best]
            group_ids[group.key] = old_group.id
            proposals.append(
                Proposal(
                    {"op": "rename_group", "group": old_group.id, "label": group.label},
                    "alta" if best_score >= 0.85 else "media",
                    f"El grupo «{old_group.label}» parece llamarse ahora «{group.label}».",
                    required=True,
                )
            )
        else:
            new_id = next_group_id()
            group_ids[group.key] = new_id
            proposals.append(
                Proposal(
                    {
                        "op": "add_group",
                        "id": new_id,
                        "label": group.label,
                        "repeatable": group.repeatable,
                        "repeat_mode": group.repeat_mode,
                    },
                    "alta",
                    f"Grupo nuevo «{group.label}».",
                    required=True,
                )
            )

    def same_group(old: FieldDef, new_group: str | None) -> bool:
        if new_group is None:
            return old.group_key is None
        return old.group_id == group_ids.get(new_group)

    matches: dict[str, FieldDef] = {}
    used_old: set[str] = set()
    pending = list(candidate.fields)

    def take(new, old) -> None:
        matches[new.key] = old
        used_old.add(old.id)
        pending.remove(new)

    # 1. Misma etiqueta en el mismo grupo.
    for new in list(pending):
        for old in active:
            if old.id not in used_old and old.normalized_label == new.normalized_label and same_group(old, new.group):
                take(new, old)
                break
    # 2. Alias de renombrados anteriores.
    by_id = {f.id: f for f in active}
    for new in list(pending):
        old = by_id.get(aliases.get(new.normalized_label, ""))
        if old is not None and old.id not in used_old and same_group(old, new.group):
            take(new, old)
    # 3. Misma etiqueta en otro grupo: se movió.
    for new in list(pending):
        same_label = [o for o in active if o.id not in used_old and o.normalized_label == new.normalized_label]
        if len(same_label) == 1:
            old = same_label[0]
            take(new, old)
            proposals.append(
                Proposal(
                    {"op": "move_field", "field": old.id, "to_group": group_ids.get(new.group) if new.group else None},
                    "alta",
                    f"«{new.label}» cambió de grupo.",
                    required=True,
                )
            )
    # 4. Renombrados por similitud: mismo grupo, posición cercana y valores parecidos.
    scored = []
    for new in pending:
        for old in active:
            if old.id in used_old or not same_group(old, new.group):
                continue
            gap = abs(old.ord - new.ord)
            name = max(
                JaroWinkler.normalized_similarity(old.normalized_label, new.normalized_label),
                token_sort_ratio(old.normalized_label, new.normalized_label) / 100,
            )
            values = _jaccard(old_samples.get(old.key, []), new_samples.get(new.key, []))
            near = gap <= MAX_POSITION_GAP
            if name >= RENAME_THRESHOLD and near:
                score = name + values * 0.5
            elif values >= VALUES_THRESHOLD and near:
                score = values * 0.8
            else:
                continue
            high = (name >= RENAME_THRESHOLD and (values >= VALUES_THRESHOLD or name >= 0.9))
            scored.append((score, new, old, "alta" if high else "media"))
    renames: list[tuple] = []
    for score, new, old, confidence in sorted(scored, key=lambda item: -item[0]):
        if old.id in used_old or new not in pending:
            continue
        take(new, old)
        renames.append((new, old, confidence))
    for new, old, confidence in renames:
        new_id = next_field_id()
        add = _add_op(new, new_id, group_ids)
        archive = {"op": "archive_field", "field": old.id}
        proposals.append(
            Proposal(
                {"op": "rename", "field": old.id, "label": new.label},
                confidence,
                f"«{old.label}» parece llamarse ahora «{new.label}».",
                alternative=[add, archive],
            )
        )
    # 5. Sin correspondencia.
    for new in pending:
        proposals.append(
            Proposal(
                _add_op(new, next_field_id(), group_ids),
                "alta",
                f"Campo nuevo «{new.label}».",
                required=True,
            )
        )
    for old in active:
        if old.id not in used_old:
            proposals.append(
                Proposal(
                    {"op": "archive_field", "field": old.id},
                    "alta",
                    f"«{old.label}» ya no aparece; se oculta sin borrar sus valores.",
                    required=True,
                )
            )
    # 6. Cambios en los campos emparejados.
    for new in candidate.fields:
        old = matches.get(new.key)
        if old is not None:
            proposals.extend(_field_changes(old, new))
    return proposals


def _add_op(new, new_id: str, group_ids: dict[str, str]) -> dict:
    return {
        "op": "add_field",
        "id": new_id,
        "group": group_ids.get(new.group) if new.group else None,
        "label": new.label,
        "type": new.type,
        "declared_type": new.declared_type,
        "options": new.options,
        "default": None,
    }


def _field_changes(old: FieldDef, new) -> list[Proposal]:
    changes: list[Proposal] = []
    if old.declared_type != new.declared_type and old.type != new.type:
        convert = {"op": "convert", "field": old.id, "to": new.type, "declared_type": new.declared_type}
        if new.type == "date":
            convert["formats"] = ["dd/MM/yyyy", "yyyy-MM-dd", "excel_serial"]
        changes.append(Proposal(convert, "media", f"«{new.label}» cambió de tipo: {old.type} → {new.type}."))
    if sorted(old.options) != sorted(new.options) and new.options:
        changes.append(
            Proposal(
                {"op": "set_options", "field": old.id, "options": new.options},
                "media",
                f"Cambiaron las opciones de «{new.label}».",
                required=True,
            )
        )
        removed = [o for o in old.options if o not in new.options]
        added = [o for o in new.options if o not in old.options]
        mapping = _pair_options(removed, added)
        if mapping:
            changes.append(
                Proposal(
                    {"op": "map_values", "field": old.id, "map": mapping},
                    "media",
                    f"Valores equivalentes en «{new.label}»: "
                    + ", ".join(f"«{a}» → «{b}»" for a, b in mapping.items()),
                )
            )
    return changes


def _pair_options(removed: list[str], added: list[str]) -> dict[str, str]:
    scored = sorted(
        (
            (JaroWinkler.normalized_similarity(normalize_label(a), normalize_label(b)), a, b)
            for a in removed
            for b in added
        ),
        key=lambda item: -item[0],
    )
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for score, a, b in scored:
        if score >= OPTION_THRESHOLD and a not in mapping and b not in used:
            mapping[a] = b
            used.add(b)
    return mapping


def _jaccard(first: list[str], second: list[str]) -> float:
    a, b = set(first), set(second)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def defs_from_candidate(candidate: SchemaCandidate) -> tuple[list[FieldDef], list[GroupDef]]:
    groups = [
        GroupDef(id=f"g_{i:02d}", key=g.key, label=g.label, repeatable=g.repeatable, repeat_mode=g.repeat_mode)
        for i, g in enumerate(candidate.groups, start=1)
    ]
    ids = {g.key: g.id for g in groups}
    fields = [
        FieldDef(
            id=f"f_{i:02d}",
            label=f.label,
            normalized_label=f.normalized_label,
            group_key=f.group,
            type=f.type,
            declared_type=f.declared_type,
            options=f.options,
            ord=f.ord,
            group_id=ids.get(f.group) if f.group else None,
        )
        for i, f in enumerate(candidate.fields, start=1)
    ]
    return fields, groups
