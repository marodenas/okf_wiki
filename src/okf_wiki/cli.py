"""okf-wiki — CLI determinista para el pipeline OKF v0.2 (la síntesis la hace Claude).

Subcomandos:
  init <dir> [--name N]   Crea una instancia de wiki nueva (dropzone + stubs + git).
  context <instancia>     Estado de `purpose.md`/`schema.md` (el ancla de dominio).
  template <nombre>       Imprime una plantilla (purpose/schema/page/proposal).
  now [--date|--json]     Fecha/hora UTC real para `generated.at` y para `log.md`.
  scan <bundle>           JSON de {new, changed, unchanged, deleted} en sources/.
  office2text <file>      Extrae texto de docx/pptx/xlsx a stdout.
  html2text <file>        Convierte HTML a markdown limpio.
  index <bundle>          Regenera los index.md (motor OKF) y sella `okf_version`.
  viz <bundle> [--name N] Regenera viz.html (motor OKF).
  build <instancia>       Construye el sitio estático de lectura (fuera de wiki/).
  watch <instancia>       Reconstruye el sitio cuando cambia el bundle (sin LLM).
  verify <bundle>         Valida OKF (exit 1) + avisos de conformidad v0.2.
  commit-state <bundle>   Reescribe el manifest; lee el JSON de `scan` por stdin.

Códigos de salida (estables: la skill y los scripts los interpretan):
  0 ok · 1 el bundle no valida · 2 falta el motor OKF ·
  3 ruta de trabajo inservible (instancia, `wiki/` o `sources/` ausentes, bundle sin
    páginas, manifest corrupto, el bundle `wiki/` donde se espera la instancia, o una
    dropzone con fuentes que no se pueden ingerir con garantías: enlaces que salen de
    `sources/`, circulares, rotos o a rutas ocultas, y enlaces duros) ·
  4 entrada ilegible (fichero no extraíble, JSON de stdin que no es de `scan`, o
    un `--out` que no sirve: un directorio inexistente en `viz`, o una carpeta
    dentro del bundle o con contenido ajeno en `build`/`watch`).

Referencia del formato: Open Knowledge Format v0.2 (ver README para la revisión
exacta del motor `reference_agent` contra la que está fijado este CLI).
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml

from okf_wiki import __version__, helpers, site, templates, watch as watch_mod

# Versión del formato que produce este CLI (OKF v0.2 §12).
OKF_VERSION = "0.2"

# Ningún fallo previsto debe salir como traceback: cada familia tiene su código.
EXIT_OK = 0
EXIT_INVALID = 1          # `verify`: el bundle no valida (o `--strict` con avisos)
EXIT_ENGINE_MISSING = 2   # falta `reference_agent` (ver `_ENGINE_HINT`)
EXIT_INGEST = 3           # `helpers.IngestError`: ruta de trabajo inservible
EXIT_BAD_INPUT = 4        # el fichero, el JSON de stdin o una ruta de salida no sirven


def _producer() -> str:
    """Actor de este CLI en la convención `<producer>/<version>` (OKF v0.2 §7).

    La versión sale de `okf_wiki.__version__`, no de los metadatos instalados: un
    editable install con dist-info antiguo firmaba las páginas con una versión que
    ya no existía en el código. Aquí manda el árbol que se está ejecutando, y
    `test_pin_and_docs.py` obliga a que coincida con `pyproject.toml`.
    """
    return f"okf-wiki/{__version__}"


# --------------------------------------------------------------------------- #
# Motor OKF: import perezoso con un error accionable si falta                 #
# --------------------------------------------------------------------------- #
class EngineMissing(RuntimeError):
    """El motor OKF (`reference_agent`) no está instalado en este intérprete."""


class BadInput(RuntimeError):
    """La entrada del usuario (fichero o JSON de stdin) no se puede procesar."""


_ENGINE_HINT = """\
Falta el motor OKF (`reference_agent`), que este comando necesita.

Cómo arreglarlo:
  1) Instala el entorno del repo:      bash scripts/setup.sh
     (fija GoogleCloudPlatform/knowledge-catalog en la revisión OKF v0.2 del README)
  2) Si ya tienes un clon del motor:   OKF_ENGINE=/ruta/knowledge-catalog/okf bash scripts/setup.sh
  3) Usa el Python del venv del repo:  .venv/bin/okf-wiki <comando>

`init`, `now`, `scan`, `office2text`, `html2text` y `commit-state` funcionan sin el motor;
`index`, `viz` y `verify` no."""


def _engine(module: str):
    """Importa un módulo del motor OKF o lanza `EngineMissing` con instrucciones."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise EngineMissing(f"{_ENGINE_HINT}\n\n(import fallido: {module}: {exc})") from exc


# --------------------------------------------------------------------------- #
# Fecha/hora UTC real                                                          #
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime | None = None) -> str:
    """ISO 8601 en UTC con sufijo `Z`, segundos enteros (OKF v0.2 §5.2)."""
    return (moment or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_date(moment: datetime | None = None) -> str:
    """Fecha UTC `YYYY-MM-DD` (cabeceras de `log.md`, OKF v0.2 §9)."""
    return (moment or utc_now()).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# init                                                                         #
# --------------------------------------------------------------------------- #
def yaml_scalar(value: object) -> str:
    """Serializa `value` como un escalar YAML de una sola línea, entre comillas.

    Sustituye a interpolar el texto dentro de unas comillas puestas a mano, que
    rompía el frontmatter en cuanto `--name`/`--by` traían caracteres con
    significado en YAML: `--name 'Wiki "Ventas"'` producía
    `title: "Registro de Wiki "Ventas""` (frontmatter ilegible, la página entera
    dejaba de validar) y `--by 'a, b'` se colaba en el flow mapping de
    `generated` firmando la página con `by: a` más una clave `b: null` inventada.

    El escapado lo hace PyYAML (ya es dependencia declarada) en vez de
    reimplementarlo. `width` desactiva el plegado de líneas largas: el escalar
    tiene que caber en una línea para poder ir dentro de `{ by: …, at: … }`.
    """
    return yaml.safe_dump(
        str(value), default_style='"', width=10**9, allow_unicode=True
    ).strip()


def md_link_text(value: object) -> str:
    """Texto seguro para el `[texto]` de un enlace markdown.

    Los corchetes cerrarían el enlace antes de tiempo y un salto de línea
    partiría el elemento de lista; se escapan y se colapsa el espacio en blanco.
    """
    flat = " ".join(str(value).split())
    return flat.replace("\\", "\\\\").replace("[", r"\[").replace("]", r"\]")


_LOG_STUB = """\
---
type: Log
title: {title}
description: {description}
tags: [log, registro]
status: stable
generated: {{ by: {by}, at: {at} }}
---

# Historial del bundle

## {today}

- **Inicialización**: instancia creada con `okf-wiki init` (Open Knowledge Format v{okf}).
"""

# Misma forma que genera el motor (`# <type>` + `* [title](link) - desc`), más el
# `okf_version` del índice raíz: el único sitio donde OKF v0.2 permite frontmatter
# en un `index.md` (§8, §12).
_INDEX_STUB = """\
---
okf_version: "{okf}"
---

# Log

* [Registro de {name}](log.md) - Bitácora append-only de ingestas y cambios de la wiki {name}.
"""


def _log_title(name: str) -> str:
    return f"Registro de {name}"


def _log_description(name: str) -> str:
    return f"Bitácora append-only de ingestas y cambios de la wiki {name}."


def _git_init(instance: Path) -> str:
    """Inicializa un repo git en `instance` solo si es seguro. Devuelve el motivo.

    Nunca commitea y nunca crea un repo anidado dentro de otro: un `git init`
    dentro de un árbol ya versionado deja dos repos solapados y hace que los
    ficheros de la wiki desaparezcan del repo padre.
    """
    if shutil.which("git") is None:
        return "git no está en el PATH → repo no inicializado."
    if (instance / ".git").exists():
        return "ya hay un repo git en la instancia → no se toca."
    top = subprocess.run(
        ["git", "-C", str(instance), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if top.returncode == 0:
        return (f"la instancia ya está dentro del repo {top.stdout.strip()} "
                "→ no se crea uno anidado.")
    done = subprocess.run(
        ["git", "-C", str(instance), "init", "-q"], capture_output=True, text=True
    )
    if done.returncode != 0:
        detail = done.stderr.strip() or f"código {done.returncode}"
        return f"git init falló ({detail}) → la instancia se creó igualmente, sin repo."
    return "repo git inicializado (sin commits: revisa y commitea tú)."


def cmd_init(args: argparse.Namespace) -> int:
    instance = Path(args.dir).resolve()
    name = args.name or instance.name
    by = args.by or _producer()
    now = utc_now()

    (instance / helpers.SOURCES_DIRNAME).mkdir(parents=True, exist_ok=True)
    wiki = instance / helpers.WIKI_DIRNAME
    wiki.mkdir(parents=True, exist_ok=True)

    log = wiki / "log.md"
    if not log.exists():
        log.write_text(
            _LOG_STUB.format(
                title=yaml_scalar(_log_title(name)),
                description=yaml_scalar(_log_description(name)),
                by=yaml_scalar(by),
                at=yaml_scalar(iso_utc(now)),
                today=iso_date(now),
                okf=OKF_VERSION,
            ),
            encoding="utf-8",
        )
    index = wiki / "index.md"
    if not index.exists():
        index.write_text(
            _INDEX_STUB.format(name=md_link_text(name), okf=OKF_VERSION), encoding="utf-8"
        )

    # Contexto de la instancia: en la RAÍZ, fuera de `wiki/`. Sólo si no existen,
    # igual que `log.md`: `init` sobre una instancia ya poblada no puede pisar el
    # propósito que el usuario haya escrito.
    written_context = []
    for filename, template in (
        (helpers.PURPOSE_FILENAME, "purpose"),
        (helpers.SCHEMA_FILENAME, "schema"),
    ):
        target = instance / filename
        if not target.exists():
            target.write_text(templates.render(template, name), encoding="utf-8")
            written_context.append(filename)

    helpers.commit_state(instance, {"new": [], "changed": [], "unchanged": [], "deleted": []})
    gi = instance / ".gitignore"
    if not gi.exists():
        gi.write_text(".DS_Store\n", encoding="utf-8")

    git_note = "git omitido (--no-git)." if args.no_git else _git_init(instance)

    print(f"Instancia creada en {instance} (name={name!r}, OKF v{OKF_VERSION}).")
    print("Estructura: purpose.md + schema.md (contexto) + sources/ (dropzone) "
          "+ wiki/ (bundle OKF).")
    print(f"Git: {git_note}")
    if written_context:
        print(f"Contexto: plantillas creadas ({', '.join(written_context)}).")
    else:
        print("Contexto: purpose.md y schema.md ya existían → no se tocan.")
    print("Siguiente paso: rellena purpose.md (alcance y fuera de alcance) — "
          "es lo que decide qué se sintetiza.")
    print("Después: suelta documentos en sources/ y ejecuta /okf-ingest.")
    return 0


# --------------------------------------------------------------------------- #
# context / template: el ancla de dominio y el andamiaje que consume la skill  #
# --------------------------------------------------------------------------- #
_CONTEXT_STATES = {
    (False, False): "FALTA",
    (True, True): "SIN RELLENAR (plantilla)",
    (True, False): "ok",
}


def _context_state(entry: dict) -> str:
    return _CONTEXT_STATES[(bool(entry["exists"]), bool(entry["stub"]))]


def cmd_context(args: argparse.Namespace) -> int:
    """Imprime `purpose.md` y `schema.md` con su estado, o el JSON en crudo.

    Una sola llamada le da a la skill el contenido y el aviso a la vez, así que
    el paso 0 de la ingesta no tiene que leer dos ficheros ni juzgar si están
    rellenos.

    Distingue dos cosas que antes se confundían en el mismo `FALTA` con exit 0:

    - **la ruta no sirve** (no existe, es un fichero, o es el bundle `wiki/`) →
      exit 3 con el mismo criterio que el resto de comandos, sin nada en stdout;
    - **la instancia es buena pero el contexto no está** (wiki antigua, plantilla
      sin rellenar) → exit 0 y un aviso: es una mejora de calidad de la síntesis,
      no un requisito de formato, y la ingesta puede seguir.
    """
    instance = helpers.resolve_instance(args.instance)
    status = helpers.context_status(instance)
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return EXIT_OK

    for key in ("purpose", "schema"):
        entry = status[key]
        print(f"=== {entry['filename']} — {_context_state(entry)} ===")
        if entry.get("error"):
            print(f"(no legible: {entry['error']})")
        elif entry["content"]:
            print(entry["content"].rstrip("\n"))
        print()

    if status["ready"]:
        print("Contexto listo: purpose.md está relleno.")
    else:
        print(
            "AVISO: purpose.md no está relleno, así que la propuesta de temas no "
            "tendrá ancla de dominio.\n"
            f"Rellénalo ({status['purpose']['path']}) y borra la línea del marcador "
            "`okf-wiki:stub`,\n"
            "o pide ayuda al agente para redactarlo antes de ingerir."
        )
    return EXIT_OK


def cmd_template(args: argparse.Namespace) -> int:
    """Vuelca una plantilla a stdout, para copiarla o redirigirla a un fichero."""
    try:
        sys.stdout.write(templates.render(args.name, args.name_for or "la wiki"))
    except KeyError as exc:
        raise BadInput(str(exc.args[0])) from exc
    return EXIT_OK


def cmd_now(args: argparse.Namespace) -> int:
    now = utc_now()
    if args.json:
        print(json.dumps({
            "at": iso_utc(now),
            "date": iso_date(now),
            "generated_by": _producer(),
            "okf_version": OKF_VERSION,
        }, indent=2))
    elif args.date:
        print(iso_date(now))
    else:
        print(iso_utc(now))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    print(json.dumps(helpers.scan(Path(args.bundle)), indent=2, ensure_ascii=False))
    return 0


# Cuánto texto de una excepción ajena se reproduce en stderr. Da de sobra para el
# mensaje de cualquier fallo real de `zipfile` o `ElementTree`; lo que corta es el
# caso en que ese mensaje lo escribe el fichero (ver `_extractor_detail`).
MAX_EXTRACTOR_DETAIL_CHARS = 300


def _extractor_detail(exc: Exception) -> str:
    """Texto del fallo, acotado salvo que lo redacte este proyecto.

    `helpers` escribe sus rechazos (`OfficeInputError`) para stderr: ya vienen
    acotados, ya citan con `helpers.quoted` lo que pone el documento y ya dicen qué
    hacer, así que pasan enteros. Los de fuera no dan ninguna de esas garantías:
    `zipfile.BadZipFile` interpola en su mensaje el nombre del miembro y los bytes
    de la cabecera —los elige quien manda el fichero, sin techo—, y el `RuntimeError`
    con el que `zipfile` rechaza un miembro cifrado con ZipCrypto interpola ese mismo
    nombre, así que un zip amañado decidía cuánto stderr imprime este comando, que es
    el mismo defecto que ya se corrigió en las referencias de celda. Se citan como
    dato: `repr` acotado, que de paso neutraliza saltos de línea y escapes de
    terminal.
    """
    if isinstance(exc, helpers.OfficeInputError):
        return str(exc)
    return helpers.quoted(str(exc), MAX_EXTRACTOR_DETAIL_CHARS)


def _extractor_kind(exc: Exception) -> str:
    """Nombre del tipo del fallo, con su módulo si el nombre suelto no dice nada.

    `zlib.error` se llama `error` a secas: sin el módulo, el mensaje quedaba en
    "…: error: 'Error -3 while decompressing data'", que no le dice a nadie qué ha
    fallado. Los builtins (`OSError`, `RuntimeError`) y los tipos de este proyecto
    (`OfficeTooLargeError`) ya se identifican solos y se citan por su nombre.
    """
    kind = type(exc)
    if kind.__module__ in ("builtins", helpers.__name__):
        return kind.__name__
    return f"{kind.__module__}.{kind.__name__}"


# Fallos que puede provocar el contenido de un fichero, y que por tanto son
# "entrada que no sirve" (exit 4) y no un error de este programa:
#
# - `BadZipFile`/`KeyError`/`ParseError`/`ValueError`: el zip o el XML no son lo
#   que dice la extensión, o `helpers` rechaza el documento (`OfficeInputError`).
# - `zlib.error`: el miembro declara DEFLATE pero su flujo comprimido está roto.
# - `NotImplementedError`: el zip pide un modo que `zipfile` no implementa
#   (banderas 5 y 6 de la cabecera: datos parcheados y cifrado fuerte).
# - `RuntimeError`: `zipfile` rechaza así un miembro cifrado con ZipCrypto, que
#   necesitaría contraseña. `NotImplementedError` ya es un `RuntimeError`, pero se
#   nombra aparte porque llega por otra puerta y el mensaje lo tiene que decir.
# - `OSError`: el fichero existe y se puede nombrar, pero no se deja leer (un
#   bucle de enlaces simbólicos, un dispositivo, un fallo de E/S). Las tres
#   subclases con mensaje propio —`FileNotFoundError`, `IsADirectoryError`,
#   `PermissionError`— se atienden antes.
#
# Ninguno de ellos debía salir como traceback: la skill `okf-ingest` lee el exit
# code y stderr, y un `.docx` hostil no puede parecer un fallo del CLI.
#
# Hay una cuarta puerta que no es una excepción y acaba en el mismo sitio: los dos
# zips que `zipfile` no rechaza sino que **avisa** —entradas solapadas y campo extra
# Unicode vacío—. Un aviso no vale aquí porque su texto lo escribe el fichero (60 KB
# de stderr desde un `.pptx` de 120 KB) y porque qué se hace con él lo decide el
# entorno: con `PYTHONWARNINGS=error` escapaba como `UserWarning` y salía con
# traceback y exit 1. `helpers` los traduce a `OfficeStructureError` —un
# `OfficeInputError`, y por tanto un `ValueError` de la lista de arriba—, así que un
# aviso sobre la estructura del zip sale por exit 4 como cualquier otro documento
# que no sirve (ver `helpers._zip_warnings_as_errors`).
_EXTRACTOR_FAILURES = (
    zipfile.BadZipFile, KeyError, ET.ParseError, ValueError,
    zlib.error, NotImplementedError, RuntimeError, OSError,
)


def _extract(fn, path: Path) -> int:
    """Ejecuta un extractor traduciendo sus fallos previsibles a `BadInput`.

    Un `.txt` renombrado a `.docx` reventaba con un `zipfile.BadZipFile` y un
    traceback entero; la skill necesita un mensaje, no una pila de llamadas.

    La escritura a stdout queda **fuera** del `try`: lo que se traduce a `BadInput`
    es lo que falla al extraer, no lo que falla al escribir. Con la llamada dentro,
    un `BrokenPipeError` (`okf-wiki office2text x.docx | head`) es un `OSError` y
    se habría reportado como un documento ilegible.
    """
    try:
        text = fn(path)
    except FileNotFoundError as exc:
        raise BadInput(f"No existe el fichero: {exc.filename}") from exc
    except (IsADirectoryError, PermissionError) as exc:
        raise BadInput(f"{exc.strerror}: {exc.filename}") from exc
    except _EXTRACTOR_FAILURES as exc:
        raise BadInput(
            f"No se pudo extraer texto de {path}: {_extractor_kind(exc)}: "
            f"{_extractor_detail(exc)}\n"
            "Comprueba que el contenido corresponde a la extensión (docx/pptx/xlsx son zips; "
            "html debe ser markup). Los .pdf, imágenes y texto plano los lee Claude directamente, "
            "sin pasar por este comando.\n"
            "Si el documento está dañado, cifrado o protegido con contraseña, ábrelo con su "
            "aplicación y vuelve a guardarlo sin protección antes de dejarlo en `sources/`."
        ) from exc
    sys.stdout.write(text)
    return 0


def cmd_office2text(args: argparse.Namespace) -> int:
    return _extract(helpers.office2text, Path(args.file))


def cmd_html2text(args: argparse.Namespace) -> int:
    return _extract(helpers.html2text, Path(args.file))


# --------------------------------------------------------------------------- #
# index: motor OKF + sello `okf_version` en el índice raíz                     #
# --------------------------------------------------------------------------- #
_FM_DELIM = "---"


def _split_frontmatter(text: str) -> str:
    """Devuelve el cuerpo de un markdown, sin su bloque de frontmatter si lo tiene."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FM_DELIM:
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FM_DELIM:
            body = "\n".join(lines[i + 1:])
            return body[1:] if body.startswith("\n") else body
    return text  # frontmatter sin cerrar: se deja tal cual


def stamp_okf_version(bundle: Path, version: str = OKF_VERSION) -> Path | None:
    """Declara `okf_version` en el `index.md` raíz del bundle (OKF v0.2 §12).

    El motor reescribe los `index.md` sin frontmatter en cada pasada, así que el
    sello se vuelve a poner después de regenerar. Idempotente.
    """
    index = Path(bundle) / "index.md"
    if not index.exists():
        return None
    text = index.read_text(encoding="utf-8")
    body = _split_frontmatter(text).lstrip("\n")
    if body and not body.endswith("\n"):
        body += "\n"
    stamped = f'{_FM_DELIM}\nokf_version: "{version}"\n{_FM_DELIM}\n\n{body}'
    if stamped != text:
        index.write_text(stamped, encoding="utf-8")
    return index


def cmd_index(args: argparse.Namespace) -> int:
    regenerate_indexes = _engine("reference_agent.bundle.index").regenerate_indexes
    bundle = helpers.resolve_bundle(args.bundle)  # ruta mala → exit 3, no "0 regenerados"
    written = regenerate_indexes(bundle, synthesize=lambda *a, **k: "")
    print(f"index.md regenerados: {len(written)}")
    if stamp_okf_version(bundle) is not None:
        print(f'índice raíz sellado con okf_version: "{OKF_VERSION}"')
    else:
        print("sin index.md raíz que sellar (bundle vacío).")
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
    generate_visualization = _engine("reference_agent.viewer").generate_visualization
    generator = _engine("reference_agent.viewer.generator")
    # Ruta mala → exit 3 con mensaje. Antes el motor lanzaba `FileNotFoundError`
    # con el traceback entero, y una carpeta vacía escribía un viz.html de 0 nodos.
    bundle = helpers.resolve_bundle(args.bundle)
    out = Path(args.out) if args.out else bundle / "viz.html"
    if not out.parent.is_dir():
        raise BadInput(
            f"El directorio de salida no existe: {out.parent} (--out {out}).\n"
            "Créalo primero o pasa un `--out` dentro de una carpeta existente."
        )
    name = args.name or Path(args.bundle).resolve().name
    # Colorear por `type`: asignar un color a cada tipo presente (determinista).
    # No modifica el motor OKF en disco; solo actualiza su paleta en runtime.
    types = sorted({c.type for c in generator._walk_concepts(bundle)} - _MUTED_TYPES)
    palette = {t: _PALETTE[i % len(_PALETTE)] for i, t in enumerate(types)}
    palette.update({t: _MUTED for t in _MUTED_TYPES})
    generator._TYPE_PALETTE.update(palette)
    try:
        stats = generate_visualization(bundle, out, bundle_name=name)
    except FileNotFoundError as exc:  # red de seguridad: el motor no debe llegar aquí
        raise helpers.MissingBundleError(
            f"El motor OKF no encontró el bundle {bundle}: {exc}."
        ) from exc
    print(f"viz.html escrito → {out} "
          f"({stats['concepts']} conceptos, {stats['edges']} aristas, "
          f"{len(types)} tipos coloreados)")
    return 0


# --------------------------------------------------------------------------- #
# build / watch: el sitio estático de lectura (sin LLM, sin motor, sin commits) #
# --------------------------------------------------------------------------- #
def _build_report(stats: dict) -> list[str]:
    """Líneas que resumen una construcción, compartidas por `build` y `--json`."""
    lines = [
        f"sitio escrito → {stats['out']} "
        f"({stats['pages']} páginas, {stats['edges']} enlaces, {stats['topics']} temas)",
        f"ficheros: {len(stats['changed'])} escritos, "
        f"{len(stats['files']) - len(stats['changed'])} sin cambios"
        + (f", {len(stats['removed'])} eliminados de la construcción anterior"
           if stats["removed"] else ""),
    ]
    if stats["refused"]:
        lines.append(
            f"AVISO: {len(stats['refused'])} entradas de `{site.MANIFEST_FILENAME}` no "
            f"caen dentro de {stats['out']} (rutas absolutas, `..`, enlaces "
            "simbólicos o nombres de fichero imposibles): NO se han seguido ni "
            "borrado. Revisa ese fichero: "
            + ", ".join(stats["refused"][:5])
        )
    if stats["broken_links"]:
        lines.append(
            f"AVISO: {len(stats['broken_links'])} enlaces no apuntan a ninguna página "
            f"del bundle: {', '.join(stats['broken_links'][:5])}"
            + (" …" if len(stats["broken_links"]) > 5 else "")
        )
    if stats["absolute_links"]:
        lines.append(
            f"AVISO: {len(stats['absolute_links'])} enlaces absolutos: el lector los "
            "sigue, pero el grafo NO dibuja su arista (usa rutas relativas)."
        )
    if stats["missing_notes"]:
        lines.append(
            f"AVISO: {len(stats['missing_notes'])} footnotes sin definir: "
            + ", ".join(f"[^{label}]" for label in stats["missing_notes"][:5])
        )
    if stats["isolated"]:
        lines.append(
            f"AVISO: {len(stats['isolated'])} páginas aisladas (sin enlaces en ningún "
            "sentido): en el grafo son nodos sueltos."
        )
    if not stats["viz"]:
        lines.append("Nota: el bundle no tiene viz.html; genéralo con `okf-wiki viz`.")
    lines.append(f"Ábrelo con doble clic: {Path(stats['out']) / 'index.html'} "
                 "(no hace falta servidor).")
    return lines


def _stats_json(stats: dict) -> dict:
    """`stats` con las rutas como texto, para poder serializarlo."""
    return {**stats, "out": Path(stats["out"]).as_posix(),
            "bundle": Path(stats["bundle"]).as_posix()}


def cmd_build(args: argparse.Namespace) -> int:
    """Construye el sitio estático. No toca `wiki/`, no llama a ningún modelo."""
    try:
        stats = site.build_site(args.instance, out=args.out, name=args.name,
                                force=args.force)
    except site.SiteError as exc:
        raise BadInput(str(exc)) from exc
    if args.json:
        print(json.dumps(_stats_json(stats), indent=2, ensure_ascii=False))
    else:
        print("\n".join(_build_report(stats)))
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    """Reconstruye el sitio mientras el bundle cambie. Nunca sintetiza ni commitea."""
    try:
        watch_mod.watch(args.instance, out=args.out, name=args.name, force=args.force,
                        interval=args.interval, cycles=args.cycles)
    except site.SiteError as exc:
        raise BadInput(str(exc)) from exc
    except KeyboardInterrupt:
        print("\nwatch detenido. El sitio construido sigue en disco.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# verify: validación OKF (errores) + conformidad v0.2 (avisos)                 #
# --------------------------------------------------------------------------- #
_STATUSES = {"draft", "stable", "deprecated"}
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:", re.MULTILINE)
_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_ABS_LINK_RE = re.compile(r"\]\((/(?!/)[^)\s]*\.md)(?:#[^)\s]*)?\)")
_URL_MD_LINK_RE = re.compile(r"\]\(((?:https?:)?//[^)\s]*\.md)(?:#[^)\s]*)?\)")


def _is_iso_datetime(value: object) -> bool:
    if isinstance(value, datetime):
        return True
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return True


def _is_iso_date(value: object) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    try:
        date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _lint_document(fm: dict, body: str) -> list[str]:
    """Avisos de conformidad OKF v0.2 sobre un documento ya parseado."""
    out: list[str] = []

    # --- migración v0.1 → v0.2 (§13.1) ------------------------------------- #
    if "timestamp" in fm:
        out.append("`timestamp` es un campo v0.1: migra a `generated: {by, at}` (§5.2, §13.1).")
    if re.search(r"^#+\s*Citations\s*$", body, re.MULTILINE | re.IGNORECASE):
        out.append("sección `# Citations` de v0.1: pásala a `sources` + footnotes (§5.1, §13.1).")

    # --- trust: generated / verified (§5.2) -------------------------------- #
    generated = fm.get("generated")
    if generated is None:
        out.append("sin `generated`: añade `generated: {by, at}` con actor y fecha UTC (§5.2).")
    elif not isinstance(generated, dict):
        out.append("`generated` debe ser un mapping `{by, at}` (§5.2).")
    else:
        if not generated.get("by"):
            out.append("`generated.by` es obligatorio dentro de `generated` (§5.2, §7).")
        if "at" not in generated:
            out.append("falta `generated.at` (ISO 8601 UTC) (§5.2).")
        elif not _is_iso_datetime(generated.get("at")):
            out.append(f"`generated.at` no es ISO 8601: {generated.get('at')!r} (§5.2).")

    # `verified` admite un mapping suelto: el consumidor lo trata como lista de 1 (§5.2).
    verified = fm.get("verified")
    if verified is None:
        events: list = []
    elif isinstance(verified, dict):
        events = [verified]
    elif isinstance(verified, list):
        events = verified
    else:
        events = []
        out.append("`verified` debe ser un mapping `{by, at}` o una lista de ellos (§5.2).")
    for event in events:
        if not isinstance(event, dict):
            out.append("cada entrada de `verified` es un mapping `{by, at}` (§5.2).")
            continue
        if not event.get("by"):
            out.append("entrada de `verified` sin `by` (§5.2, §7).")
        if not _is_iso_datetime(event.get("at")):
            out.append(f"`verified[].at` no es ISO 8601: {event.get('at')!r} (§5.2).")

    # --- lifecycle: status / stale_after (§5.4, §5.5) ---------------------- #
    status = fm.get("status")
    if status is not None and str(status) not in _STATUSES:
        out.append(f"`status` desconocido: {status!r} (usa {'|'.join(sorted(_STATUSES))}) (§5.4).")
    if "stale_after" in fm and not _is_iso_date(fm.get("stale_after")):
        out.append(f"`stale_after` debe ser una fecha `YYYY-MM-DD`: {fm['stale_after']!r} (§5.5).")

    # --- provenance: sources + footnotes por ID (§5.1) --------------------- #
    sources = fm.get("sources")
    if isinstance(sources, dict):
        sources = [sources]
    entries = [s for s in sources if isinstance(s, dict)] if isinstance(sources, list) else []
    if sources is not None and not entries:
        out.append("`sources` debe ser una lista de entradas `{id, resource, title}` (§5.1).")
    ids: set[str] = set()
    for entry in entries:
        if not entry.get("resource"):
            out.append(f"entrada de `sources` sin `resource`: {entry!r} (§5.1).")
        if entry.get("id"):
            ids.add(str(entry["id"]))

    labels = set(_FOOTNOTE_DEF_RE.findall(body)) | set(_FOOTNOTE_REF_RE.findall(body))
    for label in sorted(labels - ids):
        out.append(f"footnote `[^{label}]` no corresponde a ningún `sources[].id` (§5.1).")
    if labels and not entries:
        out.append("el cuerpo cita footnotes pero no hay `sources` en el frontmatter (§5.1).")

    # --- índices y visor: title/description y enlaces relativos ------------ #
    for key in ("title", "description"):
        if not fm.get(key):
            out.append(f"falta `{key}`: los `index.md` generados lo necesitan (§4.1).")
    for match in _ABS_LINK_RE.findall(body) + _URL_MD_LINK_RE.findall(body):
        out.append(f"enlace `{match}`: el visor solo sigue rutas relativas; no habrá arista.")
    return out


# --------------------------------------------------------------------------- #
# Taxonomía temática: avisos, nunca errores                                    #
# --------------------------------------------------------------------------- #
# OKF es deliberadamente permisivo con la organización del bundle: cualquier árbol
# de carpetas y cualquier `type` son válidos para el motor (§11). Lo que estas
# reglas comprueban es la taxonomía que promete la skill —`wiki/<tema>/<subtema>/`,
# `type` = tema de primer nivel, nunca "tipo de cosa"— y por eso salen por el canal
# `warnings`: `verify` sigue devolviendo 0, y sólo `--strict` las convierte en
# fallo. Un bundle ajeno que use otra taxonomía se sigue pudiendo validar.
_MAX_TOPIC_DEPTH = 2  # tema + subtema

# Categorías de *tipo de cosa*: prohibidas como carpeta y como `type`. Es el error
# más fácil de cometer (es como piensa un extractor de grafos) y rompe dos cosas:
# el `type` deja de decir de qué trata la página y una misma materia queda partida
# en dos carpetas. Esa relación se expresa con un enlace markdown.
_THING_WORDS = frozenset({
    "concepto", "conceptos", "concept", "concepts",
    "entidad", "entidades", "entity", "entities",
    "persona", "personas", "person", "people",
    "definicion", "definiciones", "definition", "definitions",
    "glosario", "glosarios", "glossary",
    "termino", "terminos", "term", "terms",
})


def _fold(value: object) -> str:
    """Minúsculas y sin acentos: `Retención`, `retencion` y `RETENCION` son uno."""
    decomposed = unicodedata.normalize("NFKD", str(value))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip().lower()


def _lint_taxonomy(rel: Path, fm: dict) -> list[str]:
    """Avisos de taxonomía temática de una página, por su ruta dentro del bundle.

    Necesita la ruta (y no sólo el frontmatter) porque las tres reglas comparan el
    `type` contra las carpetas: por eso va aparte de `_lint_document`. Cada aviso
    lleva la evidencia —la carpeta o el `type` concretos— para que se pueda
    arreglar sin volver a abrir el fichero.

    Las páginas de la raíz del bundle (`log.md`) no tienen tema: se les aplica sólo
    la regla de "tipo de cosa".
    """
    out: list[str] = []
    folders = rel.parts[:-1]
    type_value = fm.get("type")
    type_folded = _fold(type_value) if isinstance(type_value, str) and type_value else ""

    if len(folders) > _MAX_TOPIC_DEPTH:
        out.append(
            f"profundidad temática de {len(folders)} niveles (`{'/'.join(folders)}/`): "
            f"la estructura es tema/subtema, {_MAX_TOPIC_DEPTH} niveles como máximo. "
            f"Un tercer nivel suele significar que `{folders[0]}/` eran dos temas."
        )

    for folder in folders:
        if _fold(folder) in _THING_WORDS:
            out.append(
                f"carpeta `{folder}/`: la wiki se organiza por materia (tema/subtema), "
                "no por tipo de cosa (entidades, conceptos, personas, definiciones). "
                "Esa relación se expresa con un enlace markdown entre páginas, no "
                "duplicando la materia en dos carpetas."
            )

    if type_folded in _THING_WORDS:
        out.append(
            f"`type: {type_value}` es un tipo de cosa, no un tema: el `type` dice de qué "
            "trata la página (su carpeta de primer nivel) y es lo que colorea y filtra el "
            "grafo. La relación concepto↔entidad va en los enlaces."
        )

    if folders and type_folded and type_folded != _fold(folders[0]):
        replicated = next((f for f in folders[1:] if _fold(f) == type_folded), None)
        if replicated is not None:
            out.append(
                f"`type: {type_value}` replica el subtema `{replicated}/`: el `type` lo fija "
                f"el tema de primer nivel (`{folders[0]}/` → `type: {folders[0].capitalize()}`) "
                "y el subtema no lo cambia; un `type` por subtema fragmenta la leyenda del "
                "grafo en decenas de colores inútiles."
            )
    return out


def lint_v02(instance: Path) -> list[dict]:
    """Recorre el bundle y devuelve los avisos de conformidad v0.2 por fichero.

    Incluye los de taxonomía temática (`_lint_taxonomy`), que necesitan la ruta
    relativa al bundle. `purpose.md`/`schema.md` de la raíz quedan fuera con el
    mismo criterio que `verify` (`helpers.is_bundle_page`): son contexto de la
    instancia, no páginas, también cuando el bundle es plano.
    """
    OKFDocument = _engine("reference_agent.bundle.document").OKFDocument
    bundle = helpers.resolve_bundle(instance)
    warnings: list[dict] = []
    for md in sorted(bundle.rglob("*.md")):
        relative = md.relative_to(bundle)
        if md.name == "index.md" or not helpers.is_bundle_page(relative):
            continue
        try:
            doc = OKFDocument.parse(md.read_text(encoding="utf-8"))
        except Exception:
            continue  # el error de parseo ya lo reporta `helpers.verify`
        rel = relative.as_posix()
        frontmatter = doc.frontmatter or {}
        for warning in (_lint_document(frontmatter, doc.body or "")
                        + _lint_taxonomy(relative, frontmatter)):
            warnings.append({"path": rel, "warning": warning})
    return warnings


def cmd_verify(args: argparse.Namespace) -> int:
    _engine("reference_agent.bundle.document")  # error accionable antes de tocar disco
    instance = Path(args.bundle)
    result = helpers.verify(instance)  # ruta mala → exit 3, no un `ok: true` vacío
    result["okf_version"] = OKF_VERSION
    result["warnings"] = [] if args.no_lint else lint_v02(instance)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["ok"]:
        return EXIT_INVALID
    if args.strict and result["warnings"]:
        return EXIT_INVALID
    return EXIT_OK


_SCAN_PIPE_HINT = """\
`commit-state` espera por stdin la salida literal de `okf-wiki scan <bundle>`:
  okf-wiki scan <bundle> | okf-wiki commit-state <bundle>

No sirve la salida de `okf-wiki now --json` ni un resumen escrito a mano: hacen falta
las listas `new`/`changed`/`unchanged`/`deleted` con el `path` y el `sha256` de cada
fuente, tal cual los emite `scan`.

El manifest NO se ha modificado: vuelve a lanzarlo con el JSON correcto."""


def cmd_commit_state(args: argparse.Namespace) -> int:
    try:
        processed = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise BadInput(f"El JSON de stdin no se pudo leer: {exc}.\n\n{_SCAN_PIPE_HINT}") from exc
    try:
        path = helpers.commit_state(Path(args.bundle), processed)
    except helpers.InvalidScanPayload as exc:
        raise BadInput(f"{exc}\n\n{_SCAN_PIPE_HINT}") from exc
    print(f"manifest actualizado → {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="okf-wiki")
    p.add_argument("-V", "--version", action="version",
                   version=f"okf-wiki {__version__} (Open Knowledge Format v{OKF_VERSION})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Crea una instancia de wiki nueva.")
    s.add_argument("dir")
    s.add_argument("--name", default=None)
    s.add_argument("--by", default=None,
                   help="Actor para `generated.by` (p.ej. human:ana). Por defecto, este CLI.")
    s.add_argument("--no-git", action="store_true",
                   help="No inicializar un repo git en la instancia.")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("context", help="Estado de purpose.md y schema.md de una instancia.")
    # `instancia`, no `bundle`: este comando es el único que trabaja con la RAÍZ,
    # donde vive el contexto, y no con `wiki/`. El nombre del argumento es parte
    # del mensaje: pasarle el bundle es el error que este comando ahora rechaza.
    s.add_argument("instance", metavar="instancia",
                   help="Carpeta de la instancia (la que contiene purpose.md, "
                        "sources/ y wiki/), no el bundle wiki/.")
    s.add_argument("--json", action="store_true",
                   help="Devolver `context_status()` en crudo (para la skill).")
    s.set_defaults(func=cmd_context)

    s = sub.add_parser("template", help="Imprime una plantilla a stdout.")
    s.add_argument("name", choices=sorted(templates.TEMPLATES),
                   help="purpose | schema | page | proposal")
    s.add_argument("--name-for", default=None, dest="name_for",
                   help="Nombre de la wiki con el que rellenar la plantilla.")
    s.set_defaults(func=cmd_template)

    s = sub.add_parser("now", help="Fecha/hora UTC real para `generated.at` y `log.md`.")
    s.add_argument("--date", action="store_true", help="Solo la fecha (YYYY-MM-DD).")
    s.add_argument("--json", action="store_true", help="JSON con at, date, actor y okf_version.")
    s.set_defaults(func=cmd_now)

    s = sub.add_parser("scan", help="Detecta nuevos/cambiados/borrados en sources/.")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("office2text", help="Extrae texto de docx/pptx/xlsx.")
    s.add_argument("file")
    s.set_defaults(func=cmd_office2text)

    s = sub.add_parser("html2text", help="Convierte HTML a markdown limpio.")
    s.add_argument("file")
    s.set_defaults(func=cmd_html2text)

    s = sub.add_parser("index", help="Regenera los index.md y sella okf_version.")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("viz", help="Regenera viz.html.")
    s.add_argument("bundle")
    s.add_argument("--name", default=None)
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_viz)

    # `build`/`watch` reciben la INSTANCIA (como `context`): la salida por defecto
    # es `<instancia>/site/`, hermana de `wiki/`, porque el sitio no puede vivir
    # dentro del bundle.
    def _site_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("instance", metavar="instancia",
                            help="Carpeta de la instancia (la que contiene wiki/).")
        parser.add_argument("--out", default=None,
                            help=f"Carpeta de salida (por defecto <instancia>/"
                                 f"{site.SITE_DIRNAME}/). Nunca dentro de wiki/.")
        parser.add_argument("--name", default=None,
                            help="Nombre de la wiki para la portada.")
        parser.add_argument("--force", action="store_true",
                            help="Adoptar una carpeta de salida con contenido ajeno.")

    s = sub.add_parser("build", help="Construye el sitio estático de lectura.")
    _site_flags(s)
    s.add_argument("--json", action="store_true",
                   help="Devolver las estadísticas en JSON (para scripts).")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("watch", help="Reconstruye el sitio al cambiar el bundle.")
    _site_flags(s)
    s.add_argument("--interval", type=float, default=watch_mod.DEFAULT_INTERVAL,
                   help=f"Segundos entre sondeos (por defecto "
                        f"{watch_mod.DEFAULT_INTERVAL:g}).")
    s.add_argument("--cycles", type=int, default=None,
                   help="Salir tras N sondeos (para scripts y pruebas).")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("verify", help="Valida OKF y avisa de desvíos de v0.2.")
    s.add_argument("bundle")
    s.add_argument("--strict", action="store_true",
                   help="Salir con 1 también si hay avisos de conformidad v0.2.")
    s.add_argument("--no-lint", action="store_true",
                   help="Solo validación del motor, sin avisos v0.2.")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("commit-state", help="Reescribe el manifest (JSON de scan por stdin).")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_commit_state)

    return p


def _die(message: str, code: int) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return code


# Qué hacer con cada error de ingesta, más allá del mensaje que ya trae la
# excepción: `helpers` describe *qué* pasó y esto describe *qué hacer*.
_INGEST_ACTIONS = {
    helpers.MissingBundleError: (
        "`index`, `viz`, `verify`, `build` y `watch` esperan la instancia (la carpeta\n"
        "que contiene `wiki/`)\n"
        "o un bundle plano con páginas. Revisa la ruta, o créala con `okf-wiki init <dir>`.\n"
        "No se ha tocado nada: un bundle vacío daría un falso `ok: true` con `checked: 0`."
    ),
    helpers.BundleAsInstanceError: (
        "`build` y `watch` esperan la INSTANCIA, la carpeta que CONTIENE `wiki/`: de ahí\n"
        "salen el propósito que va en la portada y los enlaces a los documentos de\n"
        "`sources/`, que no viven dentro del bundle. Repite el comando un nivel más\n"
        "arriba y el sitio saldrá completo.\n"
        "Un bundle plano (una carpeta de páginas sin `wiki/` dentro) sigue valiendo: en\n"
        "ese caso pásale un `--out` fuera de esa carpeta.\n"
        "No se ha escrito nada."
    ),
    helpers.MissingInstanceError: (
        "`context` espera la carpeta de la INSTANCIA, la que contiene `purpose.md`,\n"
        "`schema.md`, `sources/` y `wiki/` — no el bundle `wiki/`, donde el contexto no\n"
        "vive. Revisa la ruta, o crea la instancia con `okf-wiki init <dir>`.\n"
        "No se ha tocado nada: reportar el contexto como `FALTA` sobre una ruta\n"
        "equivocada se confunde con una wiki antigua sin contexto, y la ingesta seguiría\n"
        "sin ancla de dominio."
    ),
    helpers.MissingSourcesError: (
        "`scan` y `commit-state` esperan la instancia que contiene `sources/`.\n"
        "Revisa la ruta (¿un typo?) o crea la instancia: `okf-wiki init <dir>`.\n"
        "No se ha creado ni tocado nada: un `scan` sin dropzone daría un falso 'nada\n"
        "nuevo que ingerir', y un `commit-state` que inventase `sources/` haría pasar\n"
        "el typo por instancia buena dejando el manifest real sin marcar."
    ),
    helpers.UnsafeSourceError: (
        "Arregla la entrada que nombra el mensaje **dentro de `sources/`** y repite el\n"
        "`scan`: copia el documento a la dropzone en vez de enlazarlo (`cp <origen>\n"
        "sources/…`), o borra el enlace si ya no representa nada.\n"
        "No se ha escaneado nada y el manifest sigue como estaba: `scan` no puede\n"
        "ingerir lo que no puede afirmar contenido en `sources/`, y saltárselo daría\n"
        "un JSON de dropzone sana con una fuente de menos."
    ),
    helpers.CorruptStateError: (
        "Abre `<instancia>/sources/.ingest-state.json`: debe ser un objeto JSON con la clave\n"
        "`files`. Arréglalo para conservar el estado, o bórralo aceptando que la siguiente\n"
        "ingesta reprocesará toda la dropzone (y puede duplicar conceptos en la wiki)."
    ),
}


def _ingest_action(exc: helpers.IngestError) -> str:
    for kind, action in _INGEST_ACTIONS.items():
        if isinstance(exc, kind):
            return action
    return "Revisa la instancia antes de reintentar."


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EngineMissing as exc:
        return _die(str(exc), EXIT_ENGINE_MISSING)
    except helpers.IngestError as exc:
        return _die(f"{exc}\n\nCómo seguir:\n{_ingest_action(exc)}", EXIT_INGEST)
    except BadInput as exc:
        return _die(str(exc), EXIT_BAD_INPUT)


if __name__ == "__main__":
    raise SystemExit(main())
