"""okf-wiki — CLI determinista para el pipeline OKF (la síntesis la hace Claude).

Subcomandos:
  init <dir> [--name N]   Crea una instancia de wiki nueva (dropzone + stubs + git).
  scan <bundle>           JSON de {new, changed, unchanged, deleted} en sources/.
  office2text <file>      Extrae texto de docx/pptx/xlsx a stdout.
  index <bundle>          Regenera los index.md (motor OKF, sin Gemini).
  viz <bundle> [--name N]  Regenera viz.html (motor OKF).
  verify <bundle>         Valida que todo .md es OKF válido (exit 1 si falla).
  commit-state <bundle>   Reescribe el manifest; lee el JSON de `scan` por stdin.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from okf_wiki import helpers

_LOG_STUB = """\
---
type: Log
title: "Registro de {name}"
description: "Bitácora append-only de ingestas y cambios de la wiki {name}."
timestamp: {ts}
tags: [log, registro]
---

Registro cronológico. No borrar entradas.
"""


def _ts() -> str:
    # timestamp fijo de arranque; las ingestas reales lo actualiza Claude.
    return "2026-01-01T00:00:00Z"


def cmd_init(args: argparse.Namespace) -> int:
    instance = Path(args.dir).resolve()
    name = args.name or instance.name
    (instance / helpers.SOURCES_DIRNAME).mkdir(parents=True, exist_ok=True)
    wiki = instance / helpers.WIKI_DIRNAME
    wiki.mkdir(parents=True, exist_ok=True)
    log = wiki / "log.md"
    if not log.exists():
        log.write_text(_LOG_STUB.format(name=name, ts=_ts()), encoding="utf-8")
    helpers.commit_state(instance, {"new": [], "changed": [], "unchanged": [], "deleted": []})
    gi = instance / ".gitignore"
    if not gi.exists():
        gi.write_text(".DS_Store\n", encoding="utf-8")
    print(f"Instancia creada en {instance} (name={name!r}).")
    print("Estructura: sources/ (dropzone) + wiki/ (bundle OKF).")
    print("Suelta documentos en sources/ y ejecuta /okf-ingest.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    print(json.dumps(helpers.scan(Path(args.bundle)), indent=2, ensure_ascii=False))
    return 0


def cmd_office2text(args: argparse.Namespace) -> int:
    sys.stdout.write(helpers.office2text(Path(args.file)))
    return 0


def cmd_html2text(args: argparse.Namespace) -> int:
    sys.stdout.write(helpers.html2text(Path(args.file)))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from reference_agent.bundle.index import regenerate_indexes
    bundle = helpers.wiki_root(args.bundle)
    written = regenerate_indexes(bundle, synthesize=lambda *a, **k: "")
    print(f"index.md regenerados: {len(written)}")
    return 0


# Paleta categórica accesible; se asigna una a cada `type` presente en el bundle.
# Overview/Log van en gris para que los temas de contenido destaquen.
_PALETTE = [
    "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899",
    "#14b8a6", "#f97316", "#ef4444", "#6366f1", "#84cc16",
]
_MUTED = "#94a3b8"
_MUTED_TYPES = {"Overview", "Log"}


def cmd_viz(args: argparse.Namespace) -> int:
    from reference_agent.viewer import generate_visualization, generator
    bundle = helpers.wiki_root(args.bundle)
    out = Path(args.out) if args.out else bundle / "viz.html"
    name = args.name or Path(args.bundle).resolve().name
    # Colorear por `type`: asignar un color a cada tipo presente (determinista).
    # No modifica el motor OKF en disco; solo actualiza su paleta en runtime.
    types = sorted({c.type for c in generator._walk_concepts(bundle)} - _MUTED_TYPES)
    palette = {t: _PALETTE[i % len(_PALETTE)] for i, t in enumerate(types)}
    palette.update({t: _MUTED for t in _MUTED_TYPES})
    generator._TYPE_PALETTE.update(palette)
    stats = generate_visualization(bundle, out, bundle_name=name)
    print(f"viz.html escrito → {out} "
          f"({stats['concepts']} conceptos, {stats['edges']} aristas, "
          f"{len(types)} tipos coloreados)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    result = helpers.verify(Path(args.bundle))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


def cmd_commit_state(args: argparse.Namespace) -> int:
    processed = json.load(sys.stdin)
    path = helpers.commit_state(Path(args.bundle), processed)
    print(f"manifest actualizado → {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="okf-wiki")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Crea una instancia de wiki nueva.")
    s.add_argument("dir")
    s.add_argument("--name", default=None)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("scan", help="Detecta nuevos/cambiados/borrados en sources/.")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("office2text", help="Extrae texto de docx/pptx/xlsx.")
    s.add_argument("file")
    s.set_defaults(func=cmd_office2text)

    s = sub.add_parser("html2text", help="Convierte HTML a markdown limpio.")
    s.add_argument("file")
    s.set_defaults(func=cmd_html2text)

    s = sub.add_parser("index", help="Regenera los index.md (sin Gemini).")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("viz", help="Regenera viz.html.")
    s.add_argument("bundle")
    s.add_argument("--name", default=None)
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_viz)

    s = sub.add_parser("verify", help="Valida que todo .md es OKF válido.")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("commit-state", help="Reescribe el manifest (JSON de scan por stdin).")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_commit_state)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
