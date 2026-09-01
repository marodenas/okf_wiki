"""Markdown → HTML: el renderizador del lector estático (`okf-wiki build`).

Es un subconjunto deliberado, no un CommonMark completo. Cubre exactamente lo que
`SKILL.md` le pide a las páginas OKF —encabezados, párrafos, listas, tablas,
código vallado (incluido Mermaid), citas, enlaces relativos e imágenes, y
footnotes por ID— y nada más.

Tres decisiones que conviene no revertir sin pensarlo:

- **Sin dependencias.** El repo declara `pyyaml` y `markdownify` y nada más; meter
  un parser de markdown para poder *leer* la wiki añadiría una dependencia al
  camino crítico de una herramienta cuya tesis es «solo ficheros». `build` y
  `watch` tampoco necesitan el motor OKF: un fallo de instalación del motor no
  puede dejarte sin poder leer tu propia wiki.
- **El HTML de entrada no pasa.** Todo el texto se escapa; no hay passthrough de
  markup crudo. Las páginas las redacta un LLM a partir de documentos de terceros,
  así que el contenido no es de fiar aunque el bundle sea tuyo, y un `<script>`
  copiado de una fuente no puede acabar ejecutándose en el lector.
- **La salida es determinista.** Mismo markdown → mismos bytes: ni fechas, ni
  aleatoriedad, ni orden dependiente de un `set`. Es lo que permite que
  `okf-wiki build` sea reproducible y que `watch` no reescriba ficheros idénticos.

Lo que NO soporta, a propósito: HTML embebido, listas de definición, tablas con
celdas multilínea y enlaces de referencia (`[texto][ref]`). Un enlace de
referencia se renderiza literal, que es visible en el lector, en vez de
desaparecer en silencio.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------------------- #
# Bloques                                                                      #
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})\s*([^`\s]*)\s*$")
_ATX_RE = re.compile(r"^(\s{0,3})(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}(?:-\s*-\s*-[-\s]*|\*\s*\*\s*\*[\*\s]*|_\s*_\s*_[_\s]*)$")
_UL_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
# MULTILINE porque además de casar línea a línea (`_collect_notes`) se aplica
# sobre el cuerpo entero en `to_plain_text`, donde sin él solo casaría la
# primera línea y las definiciones se colaban con corchetes en la búsqueda.
_NOTE_DEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:[ \t]*(.*)$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{1,}:?\s*\|)+\s*:?-{1,}:?\s*\|?\s*$")
# Los comentarios HTML se descartan, no se escapan: las plantillas del repo
# (`purpose.md`, `schema.md`) llevan sus instrucciones dentro de comentarios, y
# escaparlos los enseñaría como si fueran contenido de la wiki.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Sentinelas de un solo carácter de control: sobreviven a `html.escape` y no
# pueden aparecer en un markdown escrito a mano.
_CODE_MARK = "\x00c{}\x00"
_ESCAPE_MARK = "\x00e{}\x00"
_ESCAPABLE = "\\`*_{}[]()#+-.!|>~"


@dataclass
class Heading:
    """Un encabezado del cuerpo, con el `id` que se le ha asignado."""

    level: int
    text: str
    anchor: str


@dataclass
class Rendered:
    """Resultado de renderizar un cuerpo markdown."""

    html: str
    headings: list[Heading] = field(default_factory=list)
    #: Etiquetas de footnote definidas en el cuerpo, en orden de definición.
    notes: list[str] = field(default_factory=list)
    #: Etiquetas referenciadas que no tienen definición (`[^x]` sin `[^x]:`).
    missing_notes: list[str] = field(default_factory=list)


#: Firma de la política de enlaces: recibe el `href` tal cual está en el markdown
#: y devuelve `(href_final, clase_css)`. El sitio la usa para reescribir
#: `../tema/pagina.md` → `../tema/pagina.html` y para marcar los rotos.
LinkRewriter = Callable[[str], "tuple[str, str]"]


def _identity_link(href: str) -> tuple[str, str]:
    return href, ""


def slugify(text: str) -> str:
    """Slug ASCII estable para anclas y nombres de fichero.

    Sin acentos, en minúsculas y con guiones: los mismos criterios que la skill
    exige a los nombres de página, para que un ancla se pueda escribir a mano.
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or "seccion"


class _Context:
    """Estado compartido por el render de un cuerpo: anclas, notas y enlaces."""

    def __init__(
        self,
        link: LinkRewriter,
        note_numbers: dict[str, int],
        note_anchors: dict[str, str],
    ) -> None:
        self.link = link
        self.note_numbers = note_numbers
        #: Etiqueta de nota → ancla única, calculada una vez en `render_markdown`.
        self.note_anchors = note_anchors
        self.headings: list[Heading] = []
        self.missing_notes: list[str] = []
        self.codes: list[str] = []
        self._anchors: dict[str, int] = {}
        #: Etiquetas ya referenciadas: solo la primera cita lleva `id`, para que
        #: el `↩` de la nota vuelva a un ancla que existe una sola vez en la
        #: página (dos `id` iguales son HTML inválido y el salto queda indefinido).
        self.referenced: set[str] = set()

    def anchor(self, text: str) -> str:
        base = slugify(text)
        seen = self._anchors.get(base, 0)
        self._anchors[base] = seen + 1
        return base if not seen else f"{base}-{seen + 1}"

    def stash_code(self, rendered: str) -> str:
        self.codes.append(rendered)
        return _CODE_MARK.format(len(self.codes) - 1)


# --------------------------------------------------------------------------- #
# Inline                                                                       #
# --------------------------------------------------------------------------- #
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_NOTE_REF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]*)(?:\s+&quot;([^&]*)&quot;)?\s*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*([^)\s]*)(?:\s+&quot;([^&]*)&quot;)?\s*\)")
_STRONG_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_EM_STAR_RE = re.compile(r"(?<!\*)\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)")
_EM_UNDER_RE = re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_AUTOLINK_RE = re.compile(r"(?<![\"(\w])(https?://[^\s<>\"')]+[^\s<>\"'),.;:])")


def _attr(value: str) -> str:
    """Escapa un valor para meterlo entre comillas dobles en un atributo."""
    return html.escape(str(value), quote=True)


def _href(value: str) -> str:
    """Escapa un `href`/`src`, neutralizando esquemas ejecutables.

    `javascript:` y `data:` en un enlace son ejecución de código en el lector: el
    cuerpo lo escribe un LLM leyendo documentos ajenos, así que se cortan aquí y
    no en una revisión humana.
    """
    raw = value.strip()
    scheme = raw.split(":", 1)[0].lower() if ":" in raw.split("/", 1)[0] else ""
    if scheme in {"javascript", "data", "vbscript"}:
        return "#bloqueado"
    # El `&` ya viene escapado como `&amp;` (se escapa el texto antes de casar los
    # enlaces): volver a escaparlo produciría `&amp;amp;`.
    return html.escape(raw, quote=True).replace("&amp;amp;", "&amp;")


#: Barra invertida delante de un carácter escapable, dentro de una etiqueta de nota.
_LABEL_ESCAPE_RE = re.compile(rf"\\([{re.escape(_ESCAPABLE)}])")


def _note_key(label: str) -> str:
    """Etiqueta de nota normalizada: `[^a\\_b]` y `[^a_b]` son la misma fuente.

    Se aplica a las dos caras —definición y referencia— para que casen: sin esto
    una referencia con un carácter escapado se declara huérfana aunque su
    definición esté escrita justo debajo.
    """
    return _LABEL_ESCAPE_RE.sub(r"\1", label)


def _raw_escapes(text: str, escapes: list[str]) -> str:
    """Deshace los centinelas del paso 1, dejando el carácter tal cual se escribió.

    Sin escapar: es la forma que necesitan quienes leen el valor como dato —la
    política de enlaces, que tiene que resolver `a\\(b\\).md` contra el fichero
    `a(b).md`— y no como HTML ya montado.
    """
    for n, literal in enumerate(escapes):
        text = text.replace(_ESCAPE_MARK.format(n), literal)
    return text


def _emphasis(text: str) -> str:
    """Negrita, tachado y cursiva. Se aplica también dentro de una etiqueta de
    enlace, que se aparta antes de que el resto de reglas la vean."""
    text = _STRONG_RE.sub(r"<strong>\1</strong>", text)
    text = _STRIKE_RE.sub(r"<del>\1</del>", text)
    text = _EM_STAR_RE.sub(r"<em>\1</em>", text)
    return _EM_UNDER_RE.sub(r"<em>\1</em>", text)


def _inline(text: str, ctx: _Context) -> str:
    """Convierte el markdown en línea de `text` a HTML, escapando todo lo demás."""
    # 1) Escapes con barra invertida: fuera del juego antes que nada.
    escapes: list[str] = []

    def _take_escape(match: re.Match) -> str:
        escapes.append(match.group(1))
        return _ESCAPE_MARK.format(len(escapes) - 1)

    text = re.sub(rf"\\([{re.escape(_ESCAPABLE)}])", _take_escape, text)

    # 2) Código en línea: su contenido es literal, así que se aparta antes de
    #    aplicar cualquier otra regla.
    def _take_code(match: re.Match) -> str:
        code = match.group(2).strip()
        return ctx.stash_code(f"<code>{html.escape(code, quote=False)}</code>")

    text = _CODE_SPAN_RE.sub(_take_code, text)

    # 3) El resto del texto es literal hasta que una regla diga lo contrario.
    text = html.escape(text, quote=False)

    # 4) Footnotes antes que enlaces: `[^id]` no lleva paréntesis y no colisiona,
    #    pero el orden explícito evita sorpresas si alguien toca los regex.
    def _label(raw: str) -> str:
        """La etiqueta tal como se escribió, desde el texto ya transformado.

        Aquí el texto ya pasó por los pasos 1 y 2: una etiqueta como `[^p&g]`
        llega como `p&amp;g` y una con `\\_` lleva un centinela. Las definiciones
        se recogieron del markdown crudo, así que hay que deshacer ambas cosas
        antes de buscarlas —y antes de mostrarlas, o se escapan dos veces.
        """
        return _note_key(html.unescape(_raw_escapes(raw, escapes)))

    def _note(match: re.Match) -> str:
        label = _label(match.group(1))
        number = ctx.note_numbers.get(label)
        if number is None:
            ctx.missing_notes.append(label)
            return f'<span class="okf-note-missing">[^{html.escape(label)}]</span>'
        anchor = ctx.note_anchors[label]
        first = label not in ctx.referenced
        ctx.referenced.add(label)
        ident = f' id="nota-ref-{anchor}"' if first else ""
        return (
            f'<sup class="okf-note-ref"{ident}>'
            f'<a href="#nota-{anchor}" title="{_attr(label)}">{number}</a></sup>'
        )

    text = _NOTE_REF_RE.sub(_note, text)

    # 5) Imágenes y enlaces se apartan enteros, igual que el código: su HTML ya
    #    está resuelto y ninguna regla posterior debe entrar en él. Sin esto, el
    #    autolink vuelve a casar una URL que está en la etiqueta de un enlace
    #    —dejando un `<a>` dentro de otro `<a>`, que el navegador no anida: cierra
    #    el primero y el resto de la etiqueta deja de ser enlace— o dentro de un
    #    atributo como `alt`, donde mete una etiqueta en medio del valor.
    def _image(match: re.Match) -> str:
        alt, src, title = match.group(1), match.group(2), match.group(3)
        href, _cls = ctx.link(_raw_escapes(src, escapes))
        title_attr = f' title="{_attr(title)}"' if title else ""
        return ctx.stash_code(
            f'<img src="{_href(href)}" alt="{_attr(alt)}"{title_attr} loading="lazy">'
        )

    text = _IMAGE_RE.sub(_image, text)

    def _link(match: re.Match) -> str:
        label, target, title = match.group(1), match.group(2), match.group(3)
        href, cls = ctx.link(_raw_escapes(target, escapes))
        attrs = f' class="{cls}"' if cls else ""
        if title:
            attrs += f' title="{_attr(title)}"'
        if "://" in href:
            attrs += ' target="_blank" rel="noopener noreferrer"'
        # La etiqueta sí lleva énfasis, así que se resuelve aquí: es lo único que
        # el paso siguiente ya no podrá aplicarle.
        return ctx.stash_code(f'<a href="{_href(href)}"{attrs}>{_emphasis(label)}</a>')

    text = _LINK_RE.sub(_link, text)
    text = _AUTOLINK_RE.sub(
        lambda m: f'<a href="{_href(m.group(1))}" target="_blank" '
                  f'rel="noopener noreferrer">{m.group(1)}</a>',
        text,
    )

    text = _emphasis(text)

    # 6) Restaurar lo apartado, del último al primero: un enlace o una imagen
    #    puede contener el centinela de un código apartado antes que él, así que
    #    hay que reinsertar el contenedor para que su interior también se restaure.
    for i in range(len(ctx.codes) - 1, -1, -1):
        text = text.replace(_CODE_MARK.format(i), ctx.codes[i])
    # Los escapes van al final, cuando ya no queda nada apartado: una etiqueta o
    # un `alt` con `\*` sigue guardado en `ctx.codes` mientras se reinsertan los
    # contenedores, así que restaurarlos antes dejaba el centinela NUL crudo en
    # el HTML. Ningún carácter escapable es `"`, `&` ni `<`, así que reinsertarlo
    # dentro de un atributo ya montado no puede romperlo (`>` sale como `&gt;`).
    for i, raw in enumerate(escapes):
        text = text.replace(_ESCAPE_MARK.format(i), html.escape(raw, quote=False))
    return text


# --------------------------------------------------------------------------- #
# Parser de bloques                                                            #
# --------------------------------------------------------------------------- #
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_list_start(line: str) -> bool:
    return bool(_UL_RE.match(line) or _OL_RE.match(line))


def _is_block_start(line: str) -> bool:
    """¿`line` interrumpe el párrafo que se está acumulando?"""
    return bool(
        not line.strip()
        or _ATX_RE.match(line)
        or _FENCE_RE.match(line)
        or _HR_RE.match(line)
        or _QUOTE_RE.match(line)
        or _is_list_start(line)
        or _NOTE_DEF_RE.match(line)
    )


def _render_blocks(lines: list[str], ctx: _Context) -> str:
    out: list[str] = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker, lang = fence.group(2), fence.group(3)
            i += 1
            code: list[str] = []
            while i < total:
                closing = _FENCE_RE.match(lines[i])
                if closing and closing.group(2)[0] == marker[0] and len(
                    closing.group(2)
                ) >= len(marker) and not closing.group(3):
                    i += 1
                    break
                code.append(lines[i])
                i += 1
            out.append(_code_block("\n".join(code), lang))
            continue

        heading = _ATX_RE.match(line)
        if heading:
            # El título de la página ya es el `<h1>` del lector: un `#` en el
            # cuerpo baja a `<h2>` en vez de crear un segundo `<h1>`.
            level = max(2, len(heading.group(2)))
            text = _inline(heading.group(3), ctx)
            anchor = ctx.anchor(heading.group(3))
            ctx.headings.append(Heading(level, heading.group(3).strip(), anchor))
            out.append(
                f'<h{level} id="{anchor}">{text}'
                f'<a class="okf-anchor" href="#{anchor}" aria-hidden="true">#</a>'
                f"</h{level}>"
            )
            i += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < total and (_QUOTE_RE.match(lines[i]) or
                                 (lines[i].strip() and quoted and
                                  not _is_block_start(lines[i]))):
                match = _QUOTE_RE.match(lines[i])
                quoted.append(match.group(1) if match else lines[i])
                i += 1
            out.append(f"<blockquote>{_render_blocks(quoted, ctx)}</blockquote>")
            continue

        if _is_list_start(line):
            rendered, i = _render_list(lines, i, ctx)
            out.append(rendered)
            continue

        if "|" in line and i + 1 < total and _TABLE_SEP_RE.match(lines[i + 1]):
            rendered, i = _render_table(lines, i, ctx)
            out.append(rendered)
            continue

        # Párrafo.
        para: list[str] = []
        while i < total and not _is_block_start(lines[i]):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append(f"<p>{_inline(' '.join(para), ctx)}</p>")
        else:  # línea que abre bloque pero ningún caso la ha consumido
            i += 1
    return "\n".join(out)


def _code_block(code: str, lang: str) -> str:
    """Bloque vallado. Mermaid conserva su clase para que un visor lo detecte."""
    escaped = html.escape(code, quote=False)
    if lang.lower() == "mermaid":
        return f'<pre class="mermaid okf-mermaid">{escaped}</pre>'
    cls = f' class="language-{_attr(lang)}"' if lang else ""
    label = f'<span class="okf-code-lang">{html.escape(lang)}</span>' if lang else ""
    return f'<div class="okf-code">{label}<pre><code{cls}>{escaped}</code></pre></div>'


def _render_list(lines: list[str], start: int, ctx: _Context) -> tuple[str, int]:
    """Renderiza la lista que empieza en `start`; devuelve (html, siguiente índice).

    Los items se recortan a su indentación y se renderizan recursivamente, así
    que una lista anidada o un párrafo dentro de un item funcionan sin reglas
    especiales. Una lista es *suelta* (sus items llevan `<p>`) si hay una línea
    en blanco entre dos items, igual que en markdown clásico.
    """
    first = _OL_RE.match(lines[start]) or _UL_RE.match(lines[start])
    ordered = bool(_OL_RE.match(lines[start]))
    base = len(first.group(1))
    items: list[list[str]] = []
    loose = False
    pending_blank = False
    i = start
    total = len(lines)

    while i < total:
        line = lines[i]
        if not line.strip():
            # Una línea en blanco solo corta la lista si lo que sigue no le
            # pertenece (otro item al mismo nivel o una continuación indentada).
            nxt = next((j for j in range(i + 1, total) if lines[j].strip()), None)
            if nxt is None:
                break
            follows = lines[nxt]
            same_level = _is_list_start(follows) and _indent(follows) >= base
            if not (same_level or _indent(follows) > base):
                break
            pending_blank = True
            i = nxt
            continue

        match = _OL_RE.match(line) if ordered else _UL_RE.match(line)
        other = _UL_RE.match(line) if ordered else _OL_RE.match(line)
        if match and len(match.group(1)) == base:
            if items and pending_blank:
                loose = True
            pending_blank = False
            items.append([match.group(3)])
            i += 1
            continue
        if other and len(other.group(1)) == base:
            break  # cambia el tipo de lista: es otra lista
        if _indent(line) > base and items:
            if pending_blank:
                items[-1].append("")
                loose = True
                pending_blank = False
            items[-1].append(line[base:])
            i += 1
            continue
        if items and not _is_list_start(line) and _indent(line) >= base \
                and not pending_blank:
            items[-1].append(line.strip())  # continuación "perezosa" del item
            i += 1
            continue
        break

    tag = "ol" if ordered else "ul"
    rendered_items: list[str] = []
    for item in items:
        inner = _render_blocks(_dedent(item), ctx)
        if not loose:
            inner = _unwrap_leading_paragraph(inner)
        rendered_items.append(f"<li>{inner}</li>")
    return f"<{tag}>\n" + "\n".join(rendered_items) + f"\n</{tag}>", i


def _dedent(item: list[str]) -> list[str]:
    """Quita la indentación común de las líneas de continuación de un item."""
    continuation = [ln for ln in item[1:] if ln.strip()]
    if not continuation:
        return item
    common = min(_indent(ln) for ln in continuation)
    return [item[0]] + [ln[common:] if ln.strip() else ln for ln in item[1:]]


_LEADING_P_RE = re.compile(r"^<p>(.*?)</p>", re.DOTALL)


def _unwrap_leading_paragraph(inner: str) -> str:
    """Quita el `<p>` del primer bloque de un item de lista compacta.

    Un item con una lista anidada (`- Playbook` + sub-bullets) renderiza
    `<p>Playbook</p><ul>…</ul>`; en una lista compacta ese `<p>` mete un salto de
    línea que no está en el markdown. Solo se quita el primero: si el item tiene
    dos párrafos de verdad, la lista ya es suelta y esto ni se llama.
    """
    stripped = inner.strip()
    match = _LEADING_P_RE.match(stripped)
    if not match or "<p>" in match.group(1):
        return inner
    return match.group(1) + stripped[match.end():]


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)]


def _alignments(sep: str) -> list[str]:
    out = []
    for cell in _split_row(sep):
        left, right = cell.startswith(":"), cell.endswith(":")
        out.append({(True, True): "center", (False, True): "right"}.get(
            (left, right), "left"
        ))
    return out


def _render_table(lines: list[str], start: int, ctx: _Context) -> tuple[str, int]:
    header = _split_row(lines[start])
    align = _alignments(lines[start + 1])
    i = start + 2
    rows: list[list[str]] = []
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        rows.append(_split_row(lines[i]))
        i += 1

    def cell(tag: str, text: str, position: int) -> str:
        style = align[position] if position < len(align) else "left"
        return f'<{tag} class="okf-{style}">{_inline(text, ctx)}</{tag}>'

    head = "".join(cell("th", text, n) for n, text in enumerate(header))
    body = "\n".join(
        "<tr>" + "".join(
            cell("td", text, n) for n, text in enumerate(row[:len(header)])
        ) + "</tr>"
        for row in rows
    )
    return (
        '<div class="okf-table-wrap"><table>\n'
        f"<thead><tr>{head}</tr></thead>\n"
        f"<tbody>\n{body}\n</tbody>\n</table></div>",
        i,
    )


# --------------------------------------------------------------------------- #
# Footnotes                                                                    #
# --------------------------------------------------------------------------- #
def _collect_notes(lines: list[str]) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Separa las definiciones de footnote del resto del cuerpo.

    Devuelve `(líneas_sin_definiciones, definiciones, orden)`. Las definiciones se
    numeran por orden de aparición, que es el orden en que la skill las escribe
    justo debajo de la afirmación que citan.
    """
    body: list[str] = []
    notes: dict[str, list[str]] = {}
    order: list[str] = []
    i = 0
    while i < len(lines):
        match = _NOTE_DEF_RE.match(lines[i])
        if not match:
            body.append(lines[i])
            i += 1
            continue
        label, first = _note_key(match.group(1)), match.group(2)
        content = [first]
        i += 1
        while i < len(lines) and lines[i].startswith(("    ", "\t")) and lines[i].strip():
            content.append(lines[i].strip())
            i += 1
        if label in notes:  # una etiqueta repetida acumula, no se pierde
            notes[label].append(" ".join(content))
        else:
            notes[label] = [" ".join(content)]
            order.append(label)
    return body, notes, order


def _note_anchors(order: list[str]) -> dict[str, str]:
    """Ancla única por etiqueta de nota, en orden de definición.

    `slugify` normaliza —`a&b` y `a-b` dan el mismo slug—, así que dos etiquetas
    distintas producirían dos `id="nota-a-b"`: HTML inválido en el que las dos
    citas saltan a la misma nota y el `↩` queda indefinido.
    """
    anchors: dict[str, str] = {}
    taken: set[str] = set()
    for label in order:
        base = slugify(label)
        candidate, n = base, 1
        while candidate in taken:
            n += 1
            candidate = f"{base}-{n}"
        taken.add(candidate)
        anchors[label] = candidate
    return anchors


def _render_notes(order: list[str], notes: dict[str, list[str]], ctx: _Context) -> str:
    if not order:
        return ""
    items = []
    for label in order:
        anchor = ctx.note_anchors[label]
        text = _inline(" ".join(notes[label]), ctx)
        items.append(
            f'<li id="nota-{anchor}"><span class="okf-note-id">{html.escape(label)}</span> '
            f'{text} <a class="okf-note-back" href="#nota-ref-{anchor}" '
            'title="volver al texto">↩</a></li>'
        )
    return (
        '<section class="okf-notes" aria-label="Notas y fuentes citadas">\n'
        "<h2>Notas</h2>\n<ol>\n" + "\n".join(items) + "\n</ol>\n</section>"
    )


def render_markdown(text: str, *, link: LinkRewriter | None = None) -> Rendered:
    """Renderiza un cuerpo markdown OKF a HTML.

    `link` reescribe cada destino de enlace o imagen y devuelve `(href, clase)`:
    el sitio la usa para convertir `../tema/pagina.md` en `.html` y para marcar en
    rojo los enlaces que no apuntan a ninguna página del bundle.
    """
    clean = _COMMENT_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = clean.split("\n")
    body_lines, notes, order = _collect_notes(lines)
    numbers = {label: n + 1 for n, label in enumerate(order)}
    ctx = _Context(link or _identity_link, numbers, _note_anchors(order))
    body_html = _render_blocks(body_lines, ctx)
    notes_html = _render_notes(order, notes, ctx)
    full = "\n".join(part for part in (body_html, notes_html) if part)
    return Rendered(
        html=full,
        headings=ctx.headings,
        notes=order,
        missing_notes=sorted(set(ctx.missing_notes)),
    )


def to_plain_text(text: str) -> str:
    """Texto plano de un cuerpo markdown, para el índice de búsqueda.

    Quita la sintaxis pero conserva las palabras, incluidas las de los enlaces:
    buscar «retención» tiene que encontrar la página que la nombra dentro de un
    enlace. Los bloques de código se descartan enteros (son ruido para una
    búsqueda de prosa y disparan el tamaño del índice).
    """
    lines = _COMMENT_RE.sub("", text.replace("\r\n", "\n")).split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence and fence.group(3) is not None and (in_fence or fence.group(2)):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append(line)
    plain = "\n".join(out)
    plain = _NOTE_DEF_RE.sub(r"\2", plain)
    plain = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    plain = _NOTE_REF_RE.sub("", plain)
    plain = re.sub(r"`+", "", plain)
    plain = re.sub(r"[*_~>#|]+", " ", plain)
    plain = re.sub(r"[ \t]+", " ", plain)
    return "\n".join(ln.strip() for ln in plain.split("\n") if ln.strip())
