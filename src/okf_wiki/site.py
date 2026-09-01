"""Sitio estático de lectura: `okf-wiki build`.

Convierte un bundle OKF en HTML navegable —portada, árbol tema/subtema, lector,
procedencia y estado, enlaces cruzados con backlinks, búsqueda y datos de grafo—
**sin LLM, sin servidor y sin commits**. Es la quinta etapa derivada del
pipeline: se puede borrar entera y reconstruir desde el markdown.

Cuatro invariantes que sostienen el diseño:

1. **La salida vive FUERA del bundle.** El sitio es un artefacto derivado; dentro
   de `wiki/` contaminaría lo que el motor OKF recorre y convertiría los índices
   y el grafo en un espejo de sí mismos. `build` se niega a escribir dentro del
   bundle, aunque se lo pidan con `--out` (ver `_resolve_out`).
2. **La salida es reproducible.** Mismo bundle → mismos bytes: no hay fecha de
   construcción, ni rutas absolutas, ni orden dependiente de un `set`. Dos
   `build` seguidos no producen ningún cambio en git, que es lo que permite
   tener el sitio versionado sin ruido y que `watch` no reescriba lo idéntico.
   Por eso la caducidad (`stale_after`) se evalúa en el navegador y no aquí.
3. **Nada se sintetiza.** `build` no llama a ningún modelo, no toca `wiki/`, no
   escribe en `sources/` y no commitea. Lee el bundle y escribe HTML.
4. **No necesita el motor OKF.** El frontmatter se parsea aquí con `pyyaml`. Un
   fallo de instalación del motor puede dejarte sin `viz.html`, pero no sin
   poder leer tu propia wiki.

Lo que se **borra** al reconstruir son solo los ficheros que el propio `build`
escribió en la pasada anterior, anotados en `<out>/.okf-site.json`. Una carpeta
de salida con contenido ajeno se rechaza en vez de vaciarse, y ninguna entrada de
ese manifest se sigue si no cae dentro de `--out`: el fichero lo puede haber
escrito cualquiera, así que `..`, las rutas absolutas, los enlaces simbólicos y
las cadenas que ni siquiera son un nombre de fichero posible se rechazan antes de
borrar nada (ver `_manifest_target`).
"""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from okf_wiki import __version__, helpers, markdown
from okf_wiki.site_assets import SITE_CSS, SITE_JS

#: Carpeta de salida por defecto, hermana de `wiki/` dentro de la instancia.
SITE_DIRNAME = "site"
#: Inventario de lo que escribió la última construcción (para poder limpiarlo).
MANIFEST_FILENAME = ".okf-site.json"
#: Versión del layout del sitio: cambia si el manifest deja de ser compatible.
SITE_FORMAT = 1

ASSETS_DIRNAME = "assets"
DATA_DIRNAME = "data"
CSS_PATH = f"{ASSETS_DIRNAME}/okf-site.css"
JS_PATH = f"{ASSETS_DIRNAME}/okf-site.js"
DATA_JS_PATH = f"{ASSETS_DIRNAME}/data.js"
GRAPH_JSON_PATH = f"{DATA_DIRNAME}/graph.json"
SEARCH_JSON_PATH = f"{DATA_DIRNAME}/search.json"

#: Caracteres de texto plano que van al índice de búsqueda por página. Suficiente
#: para encontrar por contenido sin que `data.js` crezca sin control.
SEARCH_TEXT_LIMIT = 2000

#: Cómo se **muestra** una página sin `type`. Es una etiqueta de la interfaz, no
#: un valor del formato: `page.type` se queda vacío para que ni `graph.json` ni
#: `search.json` la declaren como si el frontmatter dijera eso (`verify` ya marca
#: la página como inválida; el sitio no debe tapar el hueco inventando un tipo).
TYPE_UNSET_LABEL = "Sin tipo"

_STATUS_LABELS = {
    "draft": "borrador",
    "stable": "estable",
    "deprecated": "obsoleta",
}
_TRUST_LABELS = {
    "unverified": "sin verificar",
    "machine-confirmed": "verificado por máquina",
    "human-reviewed": "revisado por una persona",
}


class SiteError(Exception):
    """La carpeta de salida no sirve (está dentro del bundle, o es ajena).

    No hereda de `helpers.IngestError` a propósito: el bundle está bien, lo que
    no sirve es el `--out` que ha dado el usuario. El CLI lo traduce a su código
    de «entrada ilegible», igual que hace con el `--out` de `viz`.
    """


# --------------------------------------------------------------------------- #
# Lectura del bundle                                                           #
# --------------------------------------------------------------------------- #
def parse_document(text: str) -> tuple[dict, str]:
    """Frontmatter YAML + cuerpo de una página, sin depender del motor OKF.

    Réplica deliberada de `OKFDocument.parse`: `build` tiene que funcionar en un
    entorno donde el motor no esté instalado (es una dependencia que se clona de
    GitHub), y quedarse sin lector porque falta el paquete que dibuja el grafo
    sería desproporcionado. Un frontmatter ilegible no rompe la construcción: la
    página se muestra con lo que se pueda leer y `verify` es quien la señala.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    body = "\n".join(lines[end + 1:])
    return frontmatter, body[1:] if body.startswith("\n") else body


def normalize_verified(frontmatter: dict) -> list[dict]:
    """`verified` como lista, admitiendo el mapping suelto (OKF v0.2 §5.2)."""
    verified = frontmatter.get("verified")
    if isinstance(verified, dict):
        return [verified]
    if isinstance(verified, list):
        return [v for v in verified if isinstance(v, dict)]
    return []


def trust_tier(frontmatter: dict) -> str:
    """Nivel de confianza derivado de `verified` (OKF v0.2 §5.3)."""
    events = normalize_verified(frontmatter)
    if not events:
        return "unverified"
    if any(str(event.get("by") or "").startswith("human:") for event in events):
        return "human-reviewed"
    return "machine-confirmed"


def _as_list(value: object) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _text(value: object, default: str = "") -> str:
    """Valor de frontmatter como texto, respetando la forma ISO de OKF v0.2.

    PyYAML convierte `at: 2026-08-06T10:12:33Z` en un `datetime`, y su `str()` es
    `2026-08-06 10:12:33+00:00`: la página acabaría enseñando una fecha con otra
    forma que la que tiene escrita en el fichero. Se reconstruye el ISO con `Z`,
    que es lo que exige el formato (§5.2).
    """
    if value is None or isinstance(value, (dict, list)):
        return default
    if isinstance(value, datetime):
        stamp = value.astimezone(timezone.utc) if value.tzinfo else value
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass
class LinkRef:
    """Un enlace del cuerpo, ya resuelto contra el bundle."""

    raw: str
    target: str = ""     # ruta de la página destino, relativa al bundle (`.md`)
    kind: str = "external"  # relative | absolute | asset | external | anchor | broken


@dataclass
class Page:
    """Una página del bundle con todo lo que el sitio necesita de ella."""

    rel: str                     # "ventas/retencion/plan.md"
    frontmatter: dict
    body: str
    title: str
    description: str
    type: str                    # "" si la página no declara `type` (ver TYPE_UNSET_LABEL)
    status: str
    tags: list[str]
    generated: dict
    verified: list[dict]
    stale_after: str
    sources: list[dict]
    trust: str
    topic: str                   # carpeta de primer nivel ("" si está en la raíz)
    subtopic: str                # resto de carpetas ("" si no hay)
    html: str = ""
    plain: str = ""
    links: list[LinkRef] = field(default_factory=list)
    backlinks: list[str] = field(default_factory=list)
    missing_notes: list[str] = field(default_factory=list)

    @property
    def href(self) -> str:
        """Ruta del HTML dentro del sitio, relativa a su raíz."""
        return self.rel[:-3] + ".html"

    @property
    def node_id(self) -> str:
        """Identificador del nodo en el grafo: la ruta sin extensión.

        Es la misma convención que usa el visor OKF (`viz.html`), para que los
        dos grafos hablen de los mismos nodos.
        """
        return self.rel[:-3]

    @property
    def depth(self) -> int:
        return self.rel.count("/")


def collect_pages(bundle: Path) -> list[Page]:
    """Todas las páginas del bundle, ordenadas por ruta.

    Se saltan los `index.md` (son artefactos del motor: el sitio construye su
    propia navegación) y todo lo que `helpers.is_bundle_page` deja fuera
    (`sources/`, y el contexto `purpose.md`/`schema.md` de un bundle plano).
    """
    pages: list[Page] = []
    for md_path in sorted(bundle.rglob("*.md")):
        relative = md_path.relative_to(bundle)
        if md_path.name == "index.md" or not helpers.is_bundle_page(relative):
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        frontmatter, body = parse_document(text)
        rel = relative.as_posix()
        folders = relative.parts[:-1]
        pages.append(Page(
            rel=rel,
            frontmatter=frontmatter,
            body=body,
            title=_text(frontmatter.get("title")) or relative.stem,
            description=_text(frontmatter.get("description")),
            type=_text(frontmatter.get("type")),
            status=_text(frontmatter.get("status"), "stable"),
            tags=[_text(t) for t in _as_list(frontmatter.get("tags")) if _text(t)],
            generated=frontmatter.get("generated") if isinstance(
                frontmatter.get("generated"), dict) else {},
            verified=normalize_verified(frontmatter),
            stale_after=_text(frontmatter.get("stale_after")),
            sources=[s for s in _as_list(frontmatter.get("sources")) if isinstance(s, dict)],
            trust=trust_tier(frontmatter),
            topic=folders[0] if folders else "",
            subtopic="/".join(folders[1:]) if len(folders) > 1 else "",
        ))
    return pages


# --------------------------------------------------------------------------- #
# Enlaces cruzados                                                             #
# --------------------------------------------------------------------------- #
def _normalize(parts: Iterable[str]) -> str | None:
    """Aplana `.`/`..` sobre una ruta relativa. `None` si se sale de la raíz."""
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                return None
            out.pop()
        else:
            out.append(part)
    return "/".join(out)


class _LinkResolver:
    """Reescribe los destinos de una página y anota a dónde apuntan.

    Es el único sitio donde se decide qué es un enlace del grafo: el HTML del
    lector y `graph.json` no pueden discrepar porque salen de aquí los dos.
    """

    def __init__(self, page: Page, known: set[str], base: str, bundle_hop: str) -> None:
        self.page = page
        self.known = known
        self.base = base            # prefijo hasta la raíz del sitio ("../" * n)
        self.bundle_hop = bundle_hop  # de la raíz del sitio al bundle, con "/" final
        self.found: list[LinkRef] = []

    def __call__(self, raw: str) -> tuple[str, str]:
        href, ref = self._resolve(raw)
        self.found.append(ref)
        return href, {
            "broken": "okf-link-broken",
            "absolute": "okf-link-absolute",
        }.get(ref.kind, "")

    def _resolve(self, raw: str) -> tuple[str, LinkRef]:
        target = raw.strip()
        if not target or target.startswith("#"):
            return target, LinkRef(raw, kind="anchor")
        if "://" in target or target.startswith(("mailto:", "tel:")):
            return target, LinkRef(raw, kind="external")

        path, sep, anchor = target.partition("#")
        suffix = f"{sep}{anchor}" if sep else ""

        if path.startswith("/"):
            # OKF v0.2 admite la forma absoluta desde la raíz del bundle, pero el
            # visor no la sigue: el lector sí la resuelve para que se pueda leer,
            # y la marca para que se vea que esa arista no está en el grafo.
            resolved = _normalize(path.lstrip("/").split("/"))
            if resolved and resolved in self.known:
                return f"{self.base}{resolved[:-3]}.html{suffix}", LinkRef(
                    raw, target=resolved, kind="absolute")
            return target, LinkRef(raw, kind="broken")

        here = self.page.rel.rsplit("/", 1)[0].split("/") if "/" in self.page.rel else []
        resolved = _normalize([*here, *path.split("/")])
        if resolved is None:
            return target, LinkRef(raw, kind="broken")
        if path.endswith(".md"):
            if resolved in self.known:
                return f"{self.base}{resolved[:-3]}.html{suffix}", LinkRef(
                    raw, target=resolved, kind="relative")
            return target, LinkRef(raw, kind="broken")
        # No es una página: una imagen o un adjunto que vive en el bundle. No se
        # copia al sitio (duplicaría binarios y el sitio dejaría de ser derivado);
        # se enlaza donde está de verdad.
        return f"{self.base}{self.bundle_hop}{resolved}{suffix}", LinkRef(
            raw, target=resolved, kind="asset")


def build_edges(pages: list[Page]) -> list[dict]:
    """Aristas del grafo: solo enlaces relativos entre páginas, sin duplicados.

    Mismo criterio que el visor OKF (`viz.html`): los enlaces absolutos no
    dibujan arista. Si aquí se contasen, el número de aristas del sitio y el del
    visor discreparían y el aviso de `verify` perdería sentido.
    """
    by_rel = {page.rel: page for page in pages}
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for ref in page.links:
            if ref.kind != "relative" or ref.target == page.rel:
                continue
            target = by_rel.get(ref.target)
            if target is None:
                continue
            key = (page.node_id, target.node_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append({"source": key[0], "target": key[1]})
    return edges


# --------------------------------------------------------------------------- #
# Plantillas HTML                                                              #
# --------------------------------------------------------------------------- #
def _e(value: object) -> str:
    return html.escape(_text(value), quote=True)


def _plural(count: int, singular: str, plural: str) -> str:
    """`3 páginas` / `1 página`: el contador y su sustantivo concordados."""
    return f"{count} {singular if count == 1 else plural}"


def _human_topic(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().capitalize() if name else "Raíz"


def _shell(*, title: str, base: str, description: str, body: str, name: str) -> str:
    """Esqueleto común de toda página del sitio.

    `data-okf-base` le dice al JS cuántos `../` hay hasta la raíz, que es lo que
    necesita para construir los enlaces de los resultados de búsqueda desde
    cualquier profundidad.
    """
    meta = f'<meta name="description" content="{_e(description)}">\n' if description else ""
    return f"""<!DOCTYPE html>
<html lang="es" data-okf-base="{_e(base)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="okf-wiki/{_e(__version__)}">
{meta}<title>{_e(title)}</title>
<link rel="stylesheet" href="{_e(base)}{CSS_PATH}">
</head>
<body>
<header class="okf-top">
  <a class="okf-brand" href="{_e(base)}index.html">{_e(name)}<small>wiki OKF v0.2</small></a>
  <div class="okf-search">
    <input id="okf-q" type="search" autocomplete="off" disabled
           aria-label="Buscar en la wiki"
           placeholder="La búsqueda necesita JavaScript">
    <ul id="okf-results" class="okf-results" hidden></ul>
  </div>
</header>
<div class="okf-wrap">
{body}
</div>
<script src="{_e(base)}{DATA_JS_PATH}" defer></script>
<script src="{_e(base)}{JS_PATH}" defer></script>
</body>
</html>
"""


def _badges(page: Page) -> str:
    status = page.status if page.status in _STATUS_LABELS else "otro"
    parts = [
        f'<span class="okf-badge okf-badge-status-{_e(status)}">'
        f'{_e(_STATUS_LABELS.get(page.status, page.status))}</span>',
        f'<span class="okf-badge okf-badge-trust-{_e(page.trust)}">'
        f'{_e(_TRUST_LABELS[page.trust])}</span>',
    ]
    if page.stale_after:
        # Oculto de fábrica: lo enciende el JS comparando con la fecha de hoy, así
        # el HTML no depende del día en que se construyó.
        parts.append(
            f'<span class="okf-badge okf-badge-stale" data-stale-after="{_e(page.stale_after)}"'
            f' hidden>caducada desde {_e(page.stale_after)}</span>'
        )
    return f'<p class="okf-badges">{"".join(parts)}</p>'


def _provenance_card(page: Page, base: str, resource_hop: str) -> str:
    """Panel de procedencia: de dónde sale la página, quién y cuándo."""
    rows = [("Tema", _human_topic(page.topic) if page.topic else "Raíz del bundle")]
    if page.subtopic:
        rows.append(("Subtema", _human_topic(page.subtopic)))
    rows.append(("type", page.type or TYPE_UNSET_LABEL))
    if page.tags:
        rows.append(("Tags", ", ".join(page.tags)))
    if page.generated:
        rows.append(("Generado por", _text(page.generated.get("by"), "—")))
        rows.append(("Generado el", _text(page.generated.get("at"), "—")))
    for event in page.verified:
        rows.append(("Verificado por",
                     f'{_text(event.get("by"), "—")} · {_text(event.get("at"), "—")}'))
    rows.append(("Fichero", page.rel))
    meta = "".join(f"<dt>{_e(key)}</dt><dd>{_e(value)}</dd>" for key, value in rows)

    if page.sources:
        items = []
        for source in page.sources:
            resource = _text(source.get("resource"))
            title = _text(source.get("title")) or resource or _text(source.get("id"))
            label = _e(title)
            if resource:
                # `resource` es relativo a la RAÍZ de la instancia (`sources/…`),
                # no al bundle: se salta el bundle para llegar a la dropzone.
                label = (f'<a href="{_e(base)}{_e(resource_hop)}{_e(resource)}">'
                         f'{_e(title)}</a>')
            ident = _text(source.get("id"))
            ident_html = f'<span class="okf-src-id">{_e(ident)}</span> ' if ident else ""
            extra = _text(source.get("last_modified"))
            extra_html = f' <span class="okf-src-id">({_e(extra)})</span>' if extra else ""
            items.append(f"<li>{ident_html}{label}{extra_html}</li>")
        sources_html = (
            '<div class="okf-card"><h2>Fuentes</h2><ul>' + "".join(items) + "</ul></div>"
        )
    else:
        sources_html = (
            '<div class="okf-card"><h2>Fuentes</h2>'
            "<p>Esta página no declara <code>sources</code>: lo que afirma no se "
            "puede rastrear hasta un documento.</p></div>"
        )

    return (
        '<div class="okf-card"><h2>Procedencia</h2>'
        f'<dl class="okf-meta">{meta}</dl></div>'
        f"{sources_html}"
    )


def _links_card(page: Page, pages_by_rel: dict[str, Page], base: str) -> str:
    """Enlaces cruzados de la página: salientes y quién la enlaza (backlinks)."""
    outgoing = []
    seen: set[str] = set()
    for ref in page.links:
        if ref.kind not in ("relative", "absolute") or ref.target in seen:
            continue
        target = pages_by_rel.get(ref.target)
        if target is None:
            continue
        seen.add(ref.target)
        outgoing.append(
            f'<li><a href="{_e(base)}{_e(target.href)}">{_e(target.title)}</a></li>'
        )
    incoming = [
        f'<li><a href="{_e(base)}{_e(pages_by_rel[rel].href)}">'
        f'{_e(pages_by_rel[rel].title)}</a></li>'
        for rel in page.backlinks if rel in pages_by_rel
    ]
    broken = [ref.raw for ref in page.links if ref.kind == "broken"]

    blocks = []
    if outgoing:
        blocks.append('<div class="okf-card"><h2>Enlaza a</h2><ul>'
                      + "".join(outgoing) + "</ul></div>")
    if incoming:
        blocks.append('<div class="okf-card"><h2>Enlazan aquí</h2><ul>'
                      + "".join(incoming) + "</ul></div>")
    if not outgoing and not incoming:
        blocks.append('<div class="okf-card"><h2>Enlaces</h2>'
                      "<p>Página aislada: no enlaza a ninguna otra ni la enlazan. "
                      "En el grafo aparece como un nodo suelto.</p></div>")
    if broken:
        items = "".join(f"<li><code>{_e(target)}</code></li>" for target in sorted(set(broken)))
        blocks.append('<div class="okf-card"><h2>Enlaces rotos</h2><ul>'
                      + items + "</ul></div>")
    return "".join(blocks)


def _page_html(page: Page, pages_by_rel: dict[str, Page], *, name: str,
               resource_hop: str) -> str:
    base = "../" * page.depth
    crumbs = [f'<a href="{_e(base)}index.html">Portada</a>']
    if page.topic:
        crumbs.append(_e(_human_topic(page.topic)))
    if page.subtopic:
        crumbs += [_e(_human_topic(part)) for part in page.subtopic.split("/")]
    lead = f'<p class="okf-lead">{_e(page.description)}</p>' if page.description else ""
    body = f"""<div class="okf-reader">
<main>
<nav class="okf-crumbs">{" · ".join(crumbs)}</nav>
<article>
<h1>{_e(page.title)}</h1>
{lead}{_badges(page)}
{page.html}
</article>
</main>
<aside class="okf-aside">
{_provenance_card(page, base, resource_hop)}
{_links_card(page, pages_by_rel, base)}
</aside>
</div>
"""
    return _shell(title=f"{page.title} · {name}", base=base,
                  description=page.description, body=body, name=name)


def _tree(pages: list[Page]) -> list[tuple[str, str, list[tuple[str, list[Page]]]]]:
    """Árbol tema → subtema → páginas, todo ordenado alfabéticamente.

    Devuelve `[(carpeta, etiqueta, [(subtema, páginas)])]`. La etiqueta del tema
    es el `type` que comparten **todas** sus páginas —que es lo que OKF y la skill
    declaran como nombre del tema— y cae al nombre de la carpeta si no hay
    consenso. Una página sin `type` rompe el consenso igual que una que declare
    otro distinto: es exactamente el caso en el que no se sabe cómo se llama el
    tema, así que se etiqueta por la carpeta en vez de tomar prestado el `type` de
    las vecinas o inventarse uno.
    """
    topics: dict[str, dict[str, list[Page]]] = {}
    for page in pages:
        topics.setdefault(page.topic, {}).setdefault(page.subtopic, []).append(page)

    out = []
    for topic in sorted(topics, key=lambda t: (t == "", t)):
        groups = topics[topic]
        flat = [page for group in groups.values() for page in group]
        types = sorted({page.type for page in flat})
        label = (types[0] if topic and len(types) == 1 and types[0]
                 else _human_topic(topic))
        subtopics = [
            (subtopic, sorted(groups[subtopic], key=lambda p: (p.title.lower(), p.rel)))
            for subtopic in sorted(groups)
        ]
        out.append((topic, label, subtopics))
    return out


def _stat(value: object, label: str) -> str:
    return f"<li><b>{_e(value)}</b><span>{_e(label)}</span></li>"


def _index_html(pages: list[Page], edges: list[dict], *, name: str, purpose_html: str,
                viz_href: str) -> str:
    by_status: dict[str, int] = {}
    by_trust: dict[str, int] = {}
    for page in pages:
        by_status[page.status] = by_status.get(page.status, 0) + 1
        by_trust[page.trust] = by_trust.get(page.trust, 0) + 1
    connected = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    isolated = sorted((page for page in pages if page.node_id not in connected),
                      key=lambda p: p.rel)
    unsourced = sorted((page for page in pages if not page.sources), key=lambda p: p.rel)
    tree = _tree(pages)

    stats = "".join([
        _stat(len(pages), "páginas"),
        _stat(len([t for t, _l, _s in tree if t]) or len(tree), "temas"),
        _stat(len(edges), "enlaces"),
        _stat(by_trust.get("human-reviewed", 0), "revisadas"),
    ])

    topic_items = []
    for topic, label, subtopics in tree:
        count = sum(len(group) for _sub, group in subtopics)
        blocks = []
        for subtopic, group in subtopics:
            entries = "".join(
                f'<li><a href="{_e(page.href)}">{_e(page.title)}</a>'
                + (f' <span class="okf-p-desc">— {_e(page.description)}</span>'
                   if page.description else "")
                + "</li>"
                for page in group
            )
            heading = (f"<h4>{_e(_human_topic(subtopic))}</h4>"
                       if subtopic else "")
            blocks.append(f'<li>{heading}<ul class="okf-pages">{entries}</ul></li>')
        topic_items.append(
            f'<li><h3 class="okf-topic">{_e(label)}'
            f'<span class="okf-count">{_e(_plural(count, "página", "páginas"))}'
            "</span></h3>"
            f'<ul class="okf-sub">{"".join(blocks)}</ul></li>'
        )
    tree_html = ("<ul class=\"okf-tree\">" + "".join(topic_items) + "</ul>"
                 if topic_items else
                 '<p class="okf-empty-tree">El bundle no tiene páginas todavía.</p>')

    def listing(title: str, items: list[Page], empty: str) -> str:
        if not items:
            return f'<div class="okf-card"><h2>{_e(title)}</h2><p>{_e(empty)}</p></div>'
        entries = "".join(
            f'<li><a href="{_e(page.href)}">{_e(page.title)}</a></li>' for page in items[:12]
        )
        more = (f"<li>… y {len(items) - 12} más</li>" if len(items) > 12 else "")
        return (f'<div class="okf-card"><h2>{_e(title)} ({len(items)})</h2>'
                f"<ul>{entries}{more}</ul></div>")

    status_rows = "".join(
        f"<dt>{_e(_STATUS_LABELS.get(key, key))}</dt><dd>{count}</dd>"
        for key, count in sorted(by_status.items())
    )
    trust_rows = "".join(
        f"<dt>{_e(_TRUST_LABELS.get(key, key))}</dt><dd>{count}</dd>"
        for key, count in sorted(by_trust.items())
    )
    drafts = sorted((p for p in pages if p.status == "draft"), key=lambda p: p.rel)
    deprecated = sorted((p for p in pages if p.status == "deprecated"), key=lambda p: p.rel)
    dated = sorted((p for p in pages if p.stale_after), key=lambda p: p.rel)

    viz_link = (f'<div class="okf-card"><h2>Grafo</h2><ul>'
                f'<li><a href="{_e(viz_href)}">Abrir viz.html</a></li>'
                f'<li><a href="{_e(GRAPH_JSON_PATH)}">graph.json</a> '
                f"({len(pages)} nodos, {len(edges)} aristas)</li>"
                f'<li><a href="{_e(SEARCH_JSON_PATH)}">search.json</a></li></ul></div>'
                if viz_href else
                f'<div class="okf-card"><h2>Grafo</h2><ul>'
                f'<li><a href="{_e(GRAPH_JSON_PATH)}">graph.json</a> '
                f"({len(pages)} nodos, {len(edges)} aristas)</li>"
                f'<li><a href="{_e(SEARCH_JSON_PATH)}">search.json</a></li></ul>'
                "<p>No hay <code>viz.html</code> en el bundle: genéralo con "
                "<code>okf-wiki viz</code>.</p></div>")

    body = f"""<section class="okf-hero">
<h1>{_e(name)}</h1>
<p class="okf-lead">Wiki Open Knowledge Format v0.2 ·
{_e(_plural(len(pages), "página", "páginas"))} ·
{_e(_plural(len(edges), "enlace entre páginas", "enlaces entre páginas"))}</p>
<ul class="okf-stats">{stats}</ul>
{purpose_html}
</section>
<div class="okf-cols">
<main>
<h2>Temas</h2>
<p><input id="okf-filter" type="search" autocomplete="off" disabled
       aria-label="Filtrar las páginas del árbol"
       placeholder="Filtrar páginas del árbol (necesita JavaScript)"></p>
{tree_html}
</main>
<aside class="okf-aside">
<div class="okf-card"><h2>Estado</h2><dl class="okf-meta">{status_rows}</dl></div>
<div class="okf-card"><h2>Confianza</h2><dl class="okf-meta">{trust_rows}</dl></div>
{viz_link}
{listing("Borradores", drafts, "Ninguna página en borrador.")}
{listing("Obsoletas", deprecated, "Ninguna página marcada como obsoleta.")}
{listing("Con caducidad", dated, "Ninguna página declara stale_after.")}
{listing("Sin fuentes", unsourced, "Todas las páginas declaran sources.")}
{listing("Aisladas en el grafo", isolated, "Todas las páginas están conectadas.")}
</aside>
</div>
<footer class="okf-foot">
Sitio generado por <code>okf-wiki build</code> desde el markdown del bundle.
Es un artefacto derivado: se puede borrar y reconstruir. No edites nada aquí.
</footer>
"""
    return _shell(title=name, base="", description=f"Wiki OKF: {name}",
                  body=body, name=name)


# --------------------------------------------------------------------------- #
# Datos: grafo y búsqueda                                                      #
# --------------------------------------------------------------------------- #
def build_graph(pages: list[Page], edges: list[dict], *, name: str) -> dict:
    """Datos del grafo del bundle, en JSON estable y consumible por terceros.

    Los `id` son los del visor OKF (ruta sin extensión) y las aristas siguen el
    mismo criterio, así que este fichero y `viz.html` describen el mismo grafo.
    Se añade lo que el visor no expone y sí hace falta para navegar o auditar:
    `href` al lector, tema/subtema, confianza y los grados de cada nodo.
    """
    out_degree: dict[str, int] = {}
    in_degree: dict[str, int] = {}
    for edge in edges:
        out_degree[edge["source"]] = out_degree.get(edge["source"], 0) + 1
        in_degree[edge["target"]] = in_degree.get(edge["target"], 0) + 1
    nodes = [{
        "id": page.node_id,
        "path": page.rel,
        "href": page.href,
        "label": page.title,
        "description": page.description,
        "type": page.type,
        "topic": page.topic,
        "subtopic": page.subtopic,
        "tags": page.tags,
        "status": page.status,
        "trust": page.trust,
        "stale_after": page.stale_after,
        "sources": [_text(s.get("id")) for s in page.sources if _text(s.get("id"))],
        "in_degree": in_degree.get(page.node_id, 0),
        "out_degree": out_degree.get(page.node_id, 0),
    } for page in pages]
    return {
        "generator": f"okf-wiki/{__version__}",
        "okf_version": "0.2",
        "bundle": name,
        "nodes": nodes,
        "edges": edges,
        "types": sorted({page.type for page in pages if page.type}),
        "topics": sorted({page.topic for page in pages if page.topic}),
    }


def build_search_index(pages: list[Page]) -> list[dict]:
    """Índice de búsqueda: una entrada por página, con texto plano recortado."""
    return [{
        "path": page.href,
        "source": page.rel,
        "title": page.title,
        "description": page.description,
        "type": page.type,
        "topic": page.topic,
        "subtopic": page.subtopic,
        "tags": page.tags,
        "status": page.status,
        "trust": page.trust,
        "text": page.plain[:SEARCH_TEXT_LIMIT],
    } for page in pages]


def _json_dump(payload: object) -> str:
    """JSON estable: claves ordenadas, UTF-8 literal y salto final."""
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# Carpeta de salida                                                            #
# --------------------------------------------------------------------------- #
def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _resolve_out(instance: Path, bundle: Path, out: "Path | str | None") -> Path:
    """Decide y valida la carpeta de salida. Nunca dentro del bundle.

    El sitio es un artefacto derivado: dentro de `wiki/` lo recorrería el motor
    OKF en la siguiente pasada y los índices y el grafo acabarían describiendo
    sus propios ficheros. Tampoco puede envolver al bundle: una salida que lo
    contenga mezcla las páginas fuente con el HTML generado y hace imposible
    saber qué se puede borrar.
    """
    target = Path(out).resolve() if out is not None else (instance / SITE_DIRNAME).resolve()
    bundle = bundle.resolve()
    if _is_within(target, bundle):
        flat = bundle == instance.resolve()
        detail = (
            "El bundle es plano (la instancia y el bundle son la misma carpeta), "
            "así que cualquier subcarpeta cae dentro de él: pasa un `--out` fuera, "
            "por ejemplo `--out ../sitio-de-mi-wiki`."
            if flat else
            "Elige una carpeta fuera de `wiki/`, por ejemplo la de por defecto "
            f"(`{(instance / SITE_DIRNAME).as_posix()}`)."
        )
        raise SiteError(
            f"La salida {target} está dentro del bundle {bundle}.\n"
            "El sitio es un artefacto derivado: dentro del bundle contaminaría los "
            "`index.md` y el grafo de la siguiente pasada.\n" + detail
        )
    if _is_within(bundle, target):
        raise SiteError(
            f"La salida {target} contiene al bundle {bundle}: el sitio necesita una "
            "carpeta propia para poder limpiar lo que generó la pasada anterior sin "
            "tocar nada más."
        )
    return target


def _read_manifest(out: Path) -> dict | None:
    path = out / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _check_output_dir(out: Path, force: bool) -> dict | None:
    """Comprueba que se puede escribir en `out` y devuelve el manifest anterior.

    Una carpeta con contenido que no generó `build` se rechaza: la construcción
    borra lo que sobra de la pasada anterior, y hacerlo sobre una carpeta ajena
    sería borrar ficheros de alguien. `--force` la adopta.
    """
    if not out.exists():
        return None
    if not out.is_dir():
        raise SiteError(f"La salida existe y no es un directorio: {out}.")
    manifest = _read_manifest(out)
    if manifest is not None or force:
        return manifest
    if any(out.iterdir()):
        raise SiteError(
            f"{out} ya tiene contenido y no lo generó `okf-wiki build` "
            f"(no hay `{MANIFEST_FILENAME}`).\n"
            "No se toca nada: elige otra carpeta con `--out`, vacíala tú, o pasa "
            "`--force` para adoptarla (las siguientes construcciones borrarán de "
            "ahí lo que sobre)."
        )
    return None


def _write(path: Path, content: str) -> bool:
    """Escribe `content` solo si cambia. Devuelve si tocó el disco.

    La construcción es reproducible, así que reescribir lo idéntico solo sirve
    para mover las mtime y hacer que `watch` y los editores vean cambios que no
    existen.
    """
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except (OSError, UnicodeDecodeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _manifest_target(out: Path, rel: str) -> Path | None:
    """Ruta de la entrada `rel` del manifest dentro de `out`, o `None` si no se toca.

    Lo que llega aquí sale de `<out>/.okf-site.json`, un fichero del disco que
    `build` lee pero no controla: puede venir de otra versión, de un merge a mano
    o de alguien que lo haya escrito a propósito. El paso siguiente es `unlink()`,
    así que la entrada tiene que ser una ruta relativa simple que caiga dentro de
    `out`; si no lo es, no se sigue ni se borra:

    - **absolutas** (`/etc/passwd`, `C:\\Windows\\...`): la salida no es su sitio;
    - **con `..` en cualquier tramo**, no solo al principio: `assets/../../clave`
      no empieza por `..` y se escapaba dos niveles por encima de `--out`;
    - **con un enlace simbólico** en cualquier tramo (incluido el último): seguirlo
      borraría un fichero de fuera que el sitio no escribió nunca. `out` ya viene
      resuelto, así que basta comparar con su `realpath`: cualquier diferencia es
      un salto a otro sitio.
    - **que el sistema de ficheros no puede ni nombrar**: JSON admite cadenas que
      no son rutas posibles —un NUL embebido (`"a\\u0000b"`) o un surrogate suelto
      (`"a\\ud800b"`, que no es UTF-8 codificable)—, y con ellas `realpath` lanza
      `ValueError`/`UnicodeEncodeError` en vez de devolver una ruta. Se tratan como
      una entrada rechazada más y no como un fallo del build: si el nombre no se
      puede resolver, tampoco puede ser un fichero que esta pasada escribió, así
      que no hay nada que borrar y `build`/`watch` siguen adelante.
    """
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = [part for part in re.split(r"[/\\]", rel) if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    target = out.joinpath(*parts)
    try:
        resolved = Path(os.path.realpath(target))
    except (ValueError, OSError):  # UnicodeEncodeError hereda de ValueError
        return None
    if resolved != target:
        return None
    return target


def _refused_label(rel: str) -> str:
    """Cómo se nombra en el aviso una entrada del manifest que no se ha seguido.

    El aviso acaba en `print`, así que tiene que poder escribirse. Una entrada
    del manifest no siempre puede: JSON admite cadenas que ninguna ruta puede
    llevar, y un surrogate suelto (`"a\\ud800b"`) no es codificable en UTF-8 —
    imprimirlo tal cual cambiaría el rechazo silencioso por un `UnicodeEncodeError`
    justo al contarlo. Un carácter de control (un NUL embebido) sí se codifica,
    pero en pantalla no se ve, que para un aviso es igual de inútil.

    En los dos casos se informa del `ascii()` de la cadena, visible y sin
    ambigüedad; una ruta normal se devuelve tal cual, que es lo que su dueño
    tiene que reconocer en `.okf-site.json`.
    """
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return ascii(rel)
    return ascii(rel) if any(ch < " " or ch == "\x7f" for ch in rel) else rel


def _clean(out: Path, previous: list, written: set[str]) -> tuple[list[str], list[str]]:
    """Borra lo que generó la pasada anterior y ya no forma parte del sitio.

    Devuelve `(borrados, rechazados)`. Los rechazados son las entradas del
    manifest que no se pueden tocar porque no caen dentro de `out` (ver
    `_manifest_target`): se informan en vez de ignorarse en silencio, porque un
    manifest que apunta fuera de la salida es algo que su dueño querrá ver.
    """
    out = out.resolve()
    removed: list[str] = []
    refused: list[str] = [repr(rel) for rel in previous if not isinstance(rel, str)]
    for rel in sorted(rel for rel in previous if isinstance(rel, str)):
        if rel in written:
            continue
        stale = _manifest_target(out, rel)
        if stale is None:
            refused.append(_refused_label(rel))
            continue
        if stale.is_file():
            stale.unlink()
            removed.append(rel)
    for directory in sorted(
        # Un enlace simbólico a un directorio no es un directorio del sitio: ni se
        # entra en él (`rglob` no lo sigue) ni se intenta borrarlo con `rmdir`.
        (p for p in out.rglob("*") if not p.is_symlink() and p.is_dir()),
        key=lambda p: len(p.parts), reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    return removed, sorted(refused)


# --------------------------------------------------------------------------- #
# build                                                                        #
# --------------------------------------------------------------------------- #
def _relative_hop(frm: Path, to: Path) -> str:
    """Ruta relativa de `frm` a `to`, terminada en `/` (o `""` si son la misma).

    Se usa para enlazar desde el sitio a ficheros que **no** se copian: las
    imágenes del bundle y los documentos de `sources/`.
    """
    try:
        rel = os.path.relpath(to, frm)
    except ValueError:  # unidades distintas en Windows
        return ""
    return "" if rel == "." else rel.replace(os.sep, "/") + "/"


def _purpose_section(instance: Path) -> str:
    """El propósito de la wiki en la portada, si está escrito.

    Se lee de `purpose.md` (raíz de la instancia, fuera del bundle) y solo si no
    es la plantilla sin rellenar: enseñar el stub en la portada sería enseñar las
    instrucciones de relleno como si fueran el propósito.
    """
    status = helpers.context_status(instance)["purpose"]
    if not status["exists"] or status["stub"] or not status["content"]:
        return ""
    text = re.sub(r"^#\s+.*$", "", status["content"], count=1, flags=re.MULTILINE)
    rendered = markdown.render_markdown(text)
    if not rendered.html.strip():
        return ""
    return f'<div class="okf-card okf-purpose">{rendered.html}</div>'


def build_site(instance: "Path | str", *, out: "Path | str | None" = None,
               name: str | None = None, force: bool = False) -> dict:
    """Construye el sitio estático de `instance` y devuelve las estadísticas.

    Lanza `helpers.MissingBundleError` si la ruta no contiene un bundle con
    páginas (mismo criterio que `index`, `viz` y `verify`),
    `helpers.BundleAsInstanceError` si le han pasado el bundle `wiki/` en lugar de
    la instancia, y `SiteError` si la carpeta de salida no sirve. En ninguno de los
    tres casos escribe nada.
    """
    instance_path, bundle = helpers.resolve_site_instance(Path(instance).resolve())
    out_dir = _resolve_out(instance_path, bundle, out)
    previous = _check_output_dir(out_dir, force) or {}
    wiki_name = name or instance_path.name

    pages = collect_pages(bundle)
    known = {page.rel for page in pages}
    bundle_hop = _relative_hop(out_dir, bundle)
    resource_hop = _relative_hop(out_dir, instance_path)

    for page in pages:
        base = "../" * page.depth
        resolver = _LinkResolver(page, known, base, bundle_hop)
        rendered = markdown.render_markdown(page.body, link=resolver)
        page.html = rendered.html
        page.missing_notes = rendered.missing_notes
        page.links = resolver.found
        page.plain = markdown.to_plain_text(page.body)

    edges = build_edges(pages)
    by_rel = {page.rel: page for page in pages}
    node_to_rel = {page.node_id: page.rel for page in pages}
    for edge in edges:
        by_rel[node_to_rel[edge["target"]]].backlinks.append(node_to_rel[edge["source"]])
    for page in pages:
        page.backlinks = sorted(set(page.backlinks))

    graph = build_graph(pages, edges, name=wiki_name)
    search = build_search_index(pages)
    viz = bundle / "viz.html"
    viz_href = f"{bundle_hop}viz.html" if viz.is_file() else ""

    files: dict[str, str] = {
        "index.html": _index_html(pages, edges, name=wiki_name,
                                  purpose_html=_purpose_section(instance_path),
                                  viz_href=viz_href),
        CSS_PATH: SITE_CSS,
        JS_PATH: SITE_JS,
        DATA_JS_PATH: "window.OKF_SITE = " + json.dumps(
            {"search": search, "graph": graph},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + ";\n",
        GRAPH_JSON_PATH: _json_dump(graph),
        SEARCH_JSON_PATH: _json_dump(
            {"generator": f"okf-wiki/{__version__}", "pages": search}
        ),
    }
    for page in pages:
        files[page.href] = _page_html(page, by_rel, name=wiki_name,
                                      resource_hop=resource_hop)

    out_dir.mkdir(parents=True, exist_ok=True)
    changed = sorted(rel for rel, content in sorted(files.items())
                     if _write(out_dir / rel, content))
    stale_files = previous.get("files")
    removed, refused = _clean(
        out_dir, stale_files if isinstance(stale_files, list) else [], set(files))
    manifest = {
        "format": SITE_FORMAT,
        "generator": f"okf-wiki/{__version__}",
        "okf_version": "0.2",
        "bundle": wiki_name,
        "files": sorted(files),
    }
    _write(out_dir / MANIFEST_FILENAME, _json_dump(manifest))

    broken = sorted({ref.raw for page in pages for ref in page.links
                     if ref.kind == "broken"})
    absolute = sorted({ref.raw for page in pages for ref in page.links
                       if ref.kind == "absolute"})
    connected = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    return {
        "out": out_dir,
        "bundle": bundle,
        "name": wiki_name,
        "pages": len(pages),
        "edges": len(edges),
        "topics": len({page.topic for page in pages if page.topic}),
        "files": sorted(files),
        "changed": changed,
        "removed": removed,
        "refused": refused,
        "broken_links": broken,
        "absolute_links": absolute,
        "isolated": sorted(page.rel for page in pages if page.node_id not in connected),
        "missing_notes": sorted({label for page in pages for label in page.missing_notes}),
        "viz": viz.is_file(),
    }
