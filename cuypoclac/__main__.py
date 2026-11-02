"""CLI de prueba del núcleo: nunca imprime valores de los archivos, solo su estructura."""

import argparse
import json
import sys
from pathlib import Path

import msgspec

from .schema.diff import defs_from_candidate, diff_schemas
from .schema.discovery import DiscoveryError, discover


def _counter(prefix: str, start: int):
    state = {"n": start}

    def next_id() -> str:
        state["n"] += 1
        return f"{prefix}{state['n']:02d}"

    return next_id


def cmd_discover(args) -> int:
    result = discover(Path(args.file))
    print(json.dumps(msgspec.to_builtins(result.candidate), ensure_ascii=False, indent=2))
    return 0


def cmd_diff(args) -> int:
    old = discover(Path(args.old))
    new = discover(Path(args.new))
    fields, groups = defs_from_candidate(old.candidate)
    proposals = diff_schemas(
        fields, groups, new.candidate, old.samples, new.samples, {},
        _counter("f_", len(fields)), _counter("g_", len(groups)),
    )
    output = {
        "same_fingerprint": old.candidate.fingerprint == new.candidate.fingerprint,
        "proposals": msgspec.to_builtins(proposals),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cuypoclac", description="Núcleo de CuyPoclac")
    commands = parser.add_subparsers(dest="command", required=True)
    discover_cmd = commands.add_parser("discover", help="Imprime el esquema descubierto")
    discover_cmd.add_argument("file")
    discover_cmd.set_defaults(handler=cmd_discover)
    diff_cmd = commands.add_parser("diff", help="Compara el esquema de dos archivos")
    diff_cmd.add_argument("old")
    diff_cmd.add_argument("new")
    diff_cmd.set_defaults(handler=cmd_diff)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        return args.handler(args)
    except (DiscoveryError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
