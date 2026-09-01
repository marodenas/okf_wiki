"""`okf-wiki watch`: reconstruir el sitio mientras se edita la wiki.

El límite es el contrato del proyecto, no una limitación técnica: **`watch` no
sintetiza, no borra páginas y no commitea**. Solo mira, y cuando el markdown del
bundle cambia vuelve a construir el sitio estático (que vive fuera de `wiki/`).
Si lo que cambia es la dropzone `sources/`, lo dice y para ahí: ingerir exige
leer los documentos y proponerle al usuario una estructura, y esa aprobación
humana es parte del formato, no un paso que se pueda automatizar por comodidad.

Vigila por **sondeo** (comparando hashes cada `--interval` segundos) y no con
eventos del sistema de ficheros: `watchdog` es una dependencia transitiva del
motor OKF, no una que este paquete declare, y un bucle de sondeo sobre unas
decenas de ficheros markdown es determinista, portable y suficiente. El hash
—y no la mtime— evita reconstruir cada vez que git, un `touch` o un guardado sin
cambios mueven la fecha.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable

from okf_wiki import helpers
from okf_wiki.site import build_site

#: Segundos entre dos sondeos. Bajo para que editar y recargar sea inmediato,
#: alto para no leer el bundle entero varias veces por segundo.
DEFAULT_INTERVAL = 2.0

Logger = Callable[[str], None]


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                h.update(block)
    except OSError:
        return "?"
    return h.hexdigest()


def bundle_fingerprint(instance: Path, bundle: Path) -> dict[str, str]:
    """Huella de lo que hace cambiar al sitio: las páginas y su contexto.

    Entra todo el markdown del bundle (los `index.md` incluidos: los reescribe
    `okf-wiki index` y su presencia cuenta), `purpose.md` —que sale en la
    portada— y la existencia de `viz.html`, que decide si la portada enlaza al
    grafo. Queda fuera `sources/`: cambia por otras razones y se vigila aparte.
    """
    out: dict[str, str] = {}
    for md in sorted(bundle.rglob("*.md")):
        relative = md.relative_to(bundle)
        if not helpers.is_bundle_page(relative):
            continue
        out[f"wiki:{relative.as_posix()}"] = _digest(md)
    purpose = instance / helpers.PURPOSE_FILENAME
    if purpose.is_file():
        out[f"ctx:{helpers.PURPOSE_FILENAME}"] = _digest(purpose)
    out["viz"] = "1" if (bundle / "viz.html").is_file() else "0"
    return out


def sources_fingerprint(instance: Path) -> dict[str, str]:
    """Huella de la dropzone. Vacía si la instancia no tiene `sources/`."""
    sources = instance / helpers.SOURCES_DIRNAME
    if not sources.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(sources.rglob("*")):
        relative = path.relative_to(sources)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        out[relative.as_posix()] = _digest(path)
    return out


def describe(previous: dict[str, str], current: dict[str, str]) -> str:
    """Resumen de un cambio entre dos huellas, para el log de `watch`."""
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    edited = sorted(k for k in set(previous) & set(current) if previous[k] != current[k])
    parts = []
    for label, items in (("nuevo", added), ("modificado", edited), ("borrado", removed)):
        if items:
            shown = ", ".join(items[:3]) + (f" (+{len(items) - 3})" if len(items) > 3 else "")
            parts.append(f"{label}: {shown}")
    return " · ".join(parts) or "sin cambios"


def _summary(stats: dict) -> str:
    line = (f"{stats['pages']} páginas · {stats['edges']} enlaces · "
            f"{stats['topics']} temas")
    notes = []
    if stats["broken_links"]:
        notes.append(f"{len(stats['broken_links'])} enlaces rotos")
    if stats["absolute_links"]:
        notes.append(f"{len(stats['absolute_links'])} absolutos (sin arista)")
    if stats["isolated"]:
        notes.append(f"{len(stats['isolated'])} páginas aisladas")
    if stats["refused"]:
        # Reconstruyendo en bucle, un manifest que apunta fuera de la salida se
        # rechazaría una y otra vez sin que nadie se enterase.
        notes.append(f"{len(stats['refused'])} entradas del manifest fuera de la "
                     "salida (no seguidas)")
    return line + (f" · avisos: {', '.join(notes)}" if notes else "")


def watch(instance: "Path | str", *, out: "Path | str | None" = None,
          name: str | None = None, force: bool = False,
          interval: float = DEFAULT_INTERVAL, cycles: int | None = None,
          sleep: Callable[[float], None] = time.sleep,
          log: Logger = print) -> dict:
    """Construye el sitio y lo mantiene al día mientras el bundle cambie.

    `cycles` acota el número de sondeos (`None` = hasta Ctrl-C) y `sleep`/`log`
    se inyectan para poder probar el bucle sin esperar ni imprimir. Devuelve
    cuántas construcciones y cuántos sondeos se han hecho.

    Nunca escribe dentro de `wiki/` ni de `sources/`: la única salida es la
    carpeta del sitio.
    """
    instance_path = Path(instance).resolve()
    stats = build_site(instance_path, out=out, name=name, force=force)
    bundle = stats["bundle"]
    log(f"sitio construido → {stats['out']} ({_summary(stats)})")
    log(f"vigilando {bundle} (cada {interval:g}s) — Ctrl-C para salir. "
        "watch no sintetiza, no borra páginas y no commitea.")

    seen = bundle_fingerprint(instance_path, bundle)
    seen_sources = sources_fingerprint(instance_path)
    builds = 1
    polls = 0

    while cycles is None or polls < cycles:
        sleep(interval)
        polls += 1

        current_sources = sources_fingerprint(instance_path)
        if current_sources != seen_sources:
            log(f"sources/ ha cambiado ({describe(seen_sources, current_sources)}). "
                "Ejecuta `/okf-ingest` para sintetizarlo: watch no ingiere, porque "
                "la propuesta la tiene que aprobar una persona.")
            seen_sources = current_sources

        current = bundle_fingerprint(instance_path, bundle)
        if current == seen:
            continue
        change = describe(seen, current)
        seen = current
        try:
            stats = build_site(instance_path, out=out, name=name, force=force)
        except (helpers.IngestError, OSError) as exc:
            # Un bundle a medio guardar no puede tumbar el vigilante: se informa
            # y se sigue sondeando, que es lo que hará que se arregle solo en
            # cuanto el fichero vuelva a estar entero.
            log(f"error al reconstruir ({exc.__class__.__name__}): {exc}")
            continue
        builds += 1
        detail = (f"{len(stats['changed'])} ficheros" if stats["changed"]
                  else "sin cambios en la salida")
        log(f"reconstruido [{change}] → {detail} · {_summary(stats)}")

    return {"builds": builds, "polls": polls, "out": stats["out"]}
