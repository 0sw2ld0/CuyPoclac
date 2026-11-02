"""Simula una migración sin tocar la base."""

import json
from dataclasses import dataclass

from ..db.repo import Schema
from .ops import OpStats, apply_chain, compile_ops


@dataclass
class DryRunResult:
    total_records: int
    stats: list[tuple[dict, OpStats]]

    @property
    def has_failures(self) -> bool:
        return any(s.failed for _, s in self.stats)


def dry_run(conn, workspace_id: int, schema: Schema, ops: list[dict]) -> DryRunResult:
    compiled = compile_ops(schema, ops)
    stats = [OpStats() for _ in ops]
    total = 0
    for row in conn.execute("SELECT data FROM record WHERE workspace_id = ?", (workspace_id,)):
        total += 1
        apply_chain(json.loads(row["data"]), compiled, stats)
    return DryRunResult(total, list(zip(ops, stats)))
