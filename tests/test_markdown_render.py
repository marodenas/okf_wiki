"""El renderizador markdown del lector: subconjunto OKF, escapado y determinismo.

Cubre lo que `SKILL.md` obliga a escribir en una página (tablas, mermaid,
footnotes por ID, enlaces relativos) y las dos propiedades de las que depende el
resto del sitio: que el HTML de entrada no pase y que la salida no cambie entre
ejecuciones.
"""
from __future__ import annotations

import re

from okf_wiki import markdown


def render(text: str, **kwargs) -> str:
    return markdown.render_markdown(text, **kwargs).html


# --------------------------------------------------------------------------- #
# Bloques                                                                      #
# --------------------------------------------------------------------------- #
def test_paragraph_joins_wrapped_lines():
    assert render("una frase\npartida en dos") == "<p>una frase partida en dos</p>"


def test_heading_never_emits_a_second_h1():
    """El `<h1>` del lector es el `title` del frontmatter (SKILL: sin `#` en el cuerpo)."""
    assert "<h2 id=\"titulo\">" in render("# Titulo")
    assert "<h1" not in render("# Titulo")
    assert "<h3 id=\"sub\">" in render("### Sub")


def test_heading_anchors_are_unique_and_slugified():
    html = render("## Retención\n\ntexto\n\n## Retención")
    assert 'id="retencion"' in html
    assert 'id="retencion-2"' in html


def test_inline_emphasis_code_and_strike():
    html = render("**fuerte** y *suave* y `codigo` y ~~fuera~~")
    assert "<strong>fuerte</strong>" in html
    assert "<em>suave</em>" in html
    assert "<code>codigo</code>" in html
    assert "<del>fuera</del>" in html


def test_fenced_code_keeps_language_and_escapes_content():
    html = render("```python\nx = '<b>'\n```")
    assert 'class="language-python"' in html
    assert "&lt;b&gt;" in html and "<b>" not in html


def test_mermaid_fence_keeps_its_class():
    """Un visor mermaid busca `pre.mermaid`; sin librería se ve el código."""
    html = render("```mermaid\nflowchart LR\n  A --> B\n```")
    assert '<pre class="mermaid okf-mermaid">' in html
    assert "A --&gt; B" in html


def test_table_with_alignment():
    html = render("| a | b |\n|:--|--:|\n| 1 | 2 |")
    assert "<table>" in html
    assert '<th class="okf-left">a</th>' in html
    assert '<th class="okf-right">b</th>' in html
    assert '<td class="okf-right">2</td>' in html


def test_tight_list_has_no_paragraphs():
    html = render("- uno\n- dos")
    assert html == "<ul>\n<li>uno</li>\n<li>dos</li>\n</ul>"


def test_nested_list_stays_tight_and_nests():
    html = render("- uno\n  - anidado\n- dos")
    assert "<li>uno\n<ul>\n<li>anidado</li>\n</ul></li>" in html
    assert "<p>uno</p>" not in html


def test_loose_list_keeps_paragraphs():
    html = render("- uno\n\n- dos")
    assert html.count("<p>") == 2


def test_ordered_list_and_blockquote():
    assert "<ol>" in render("1. uno\n2. dos")
    assert "<blockquote><p>cita</p></blockquote>" in render("> cita")


def test_horizontal_rule():
    assert "<hr>" in render("antes\n\n---\n\ndespués")


# --------------------------------------------------------------------------- #
# Seguridad: el contenido lo escribe un LLM leyendo documentos ajenos          #
# --------------------------------------------------------------------------- #
def test_raw_html_is_escaped_not_executed():
    html = render("Ojo: <script>alert(1)</script> y <img onerror=x>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_javascript_and_data_hrefs_are_neutralized():
    assert 'href="#bloqueado"' in render("[pulsa](javascript:alert(1))")
    assert 'src="#bloqueado"' in render("![x](data:text/html;base64,AAAA)")


def test_link_title_and_text_are_escaped():
    html = render('[a "b"](x.md "t<i>")')
    assert "<i>" not in html


# --------------------------------------------------------------------------- #
# Enlaces y footnotes: el contrato con `site.py`                              #
# --------------------------------------------------------------------------- #
def test_link_rewriter_receives_the_raw_target_and_sets_the_class():
    seen = []

    def rewrite(href):
        seen.append(href)
        return href.replace(".md", ".html"), "marcado"

    html = render("[x](../otro/pagina.md)", link=rewrite)
    assert seen == ["../otro/pagina.md"]
    assert '<a href="../otro/pagina.html" class="marcado">x</a>' in html


def test_external_links_open_in_a_new_tab():
    html = render("[web](https://example.com/a)")
    assert 'target="_blank"' in html and 'rel="noopener noreferrer"' in html


def test_bare_url_in_prose_is_autolinked():
    html = render("Fuente: https://example.com/a, ver ahí.")
    assert '<a href="https://example.com/a" target="_blank"' in html
    assert html.count("<a ") == 1


def test_url_used_as_link_label_does_not_nest_anchors():
    """Un `<a>` dentro de otro `<a>` es HTML inválido: el navegador cierra el
    primero y el resto de la etiqueta deja de ser enlace."""
    html = render("[https://example.com/a](../otro/pagina.md)")
    assert html == '<p><a href="../otro/pagina.md">https://example.com/a</a></p>'


def test_url_label_does_not_stop_the_autolink_of_the_prose_around_it():
    html = render("Ver [https://example.com/a](../a.md) o https://example.com/b.")
    assert '<a href="../a.md">https://example.com/a</a>' in html
    assert '<a href="https://example.com/b" target="_blank"' in html
    assert html.count("<a ") == 2


def test_url_inside_an_image_alt_is_not_autolinked():
    html = render("![ver https://example.com](grafico.png)")
    assert html == (
        '<p><img src="grafico.png" alt="ver https://example.com" loading="lazy"></p>'
    )


def test_emphasis_inside_a_link_label_still_renders():
    html = render("[el **dato** clave](../a.md)")
    assert html == '<p><a href="../a.md">el <strong>dato</strong> clave</a></p>'


def test_code_span_inside_a_link_label_still_renders():
    html = render("[el `campo`](../a.md)")
    assert html == '<p><a href="../a.md">el <code>campo</code></a></p>'


def test_escaped_char_in_a_link_label_renders_literal_without_sentinels():
    """Enlaces e imágenes se apartan enteros: si los escapes se restauran antes
    de reinsertarlos, el centinela NUL del paso 1 sale crudo al HTML."""
    html = render(r"[a\*b](x.md)")
    assert html == '<p><a href="x.md">a*b</a></p>'
    assert "\x00" not in html


def test_escaped_char_in_an_image_alt_renders_literal_without_sentinels():
    html = render(r"![a\*b](x.png)")
    assert html == '<p><img src="x.png" alt="a*b" loading="lazy"></p>'
    assert "\x00" not in html


def test_escaped_parens_in_a_target_reach_the_rewriter_as_the_real_path():
    """La política de enlaces resuelve el destino contra los ficheros del bundle:
    si le llega con centinelas dentro, `a(b).md` se marca como roto existiendo."""
    seen = []

    def rewrite(href):
        seen.append(href)
        return href.replace(".md", ".html"), ""

    html = render(r"[x](a\(b\).md)", link=rewrite)
    assert seen == ["a(b).md"]
    assert html == '<p><a href="a(b).html">x</a></p>'
    assert "\x00" not in html


def test_escaped_parens_in_an_image_source_reach_the_rewriter_as_the_real_path():
    """Lo mismo para el `src` de una imagen: la política resuelve el fichero del
    bundle, así que tiene que recibir `grafico(1).png`, no el centinela."""
    seen = []

    def rewrite(href):
        seen.append(href)
        return f"../assets/{href}", ""

    html = render(r"![un gráfico](grafico\(1\).png)", link=rewrite)
    assert seen == ["grafico(1).png"]
    assert html == (
        '<p><img src="../assets/grafico(1).png" alt="un gráfico" loading="lazy"></p>'
    )
    assert "\x00" not in html


def test_escaped_char_inside_a_code_span_leaves_no_sentinel():
    html = render(r"`a\*b`")
    assert html == "<p><code>a*b</code></p>"
    assert "\x00" not in html


def test_footnotes_move_to_a_notes_section_and_are_numbered():
    result = markdown.render_markdown(
        "Afirmación.[^informe-q2]\n\n[^informe-q2]: informe.pdf, p.4"
    )
    assert result.notes == ["informe-q2"]
    assert 'href="#nota-informe-q2"' in result.html
    assert '<li id="nota-informe-q2">' in result.html
    assert "informe.pdf, p.4" in result.html
    assert "[^informe-q2]:" not in result.html


def test_repeated_footnote_reference_defines_the_anchor_only_once():
    """Dos `id` iguales son HTML inválido: el `↩` de la nota quedaría indefinido."""
    html = render("Uno.[^a] Dos.[^a]\n\n[^a]: fuente")
    assert html.count('id="nota-ref-a"') == 1
    assert html.count('href="#nota-a"') == 2


def test_footnote_without_definition_is_reported_and_visible():
    result = markdown.render_markdown("Afirmación.[^huerfana]")
    assert result.missing_notes == ["huerfana"]
    assert "okf-note-missing" in result.html


def test_footnote_label_with_ampersand_resolves_to_its_definition():
    """La etiqueta llega al inline ya escapada (`p&amp;g`): si no se deshace, no
    casa con su definición y la cita se pierde aunque esté escrita."""
    result = markdown.render_markdown("Dato.[^p&g]\n\n[^p&g]: informe de P&G, p.2")
    assert result.notes == ["p&g"]
    assert result.missing_notes == []
    assert "okf-note-missing" not in result.html
    assert 'href="#nota-p-g"' in result.html
    assert '<li id="nota-p-g">' in result.html
    assert '<span class="okf-note-id">p&amp;g</span>' in result.html
    assert 'title="p&amp;g"' in result.html
    assert "informe de P&amp;G, p.2" in result.html
    assert "amp;amp;" not in result.html


def test_missing_footnote_label_with_ampersand_is_reported_and_escaped_once():
    result = markdown.render_markdown("Dato.[^p&g]")
    assert result.missing_notes == ["p&g"]
    assert '<span class="okf-note-missing">[^p&amp;g]</span>' in result.html
    assert "amp;amp;" not in result.html


def test_footnote_label_with_backslash_escape_resolves_and_leaves_no_sentinel():
    result = markdown.render_markdown("Dato.[^a\\_b]\n\n[^a\\_b]: fuente")
    assert result.notes == ["a_b"]
    assert result.missing_notes == []
    assert "\x00" not in result.html
    assert 'href="#nota-a-b"' in result.html


def test_note_ids_stay_unique_when_slugify_collapses_two_labels():
    """`slugify` normaliza: `[^a&b]` y `[^a-b]` colapsan al mismo slug y darían
    dos `id="nota-a-b"`, con lo que ambas citas saltarían a la misma nota."""
    result = markdown.render_markdown("Uno.[^a-b] Dos.[^a&b]\n\n[^a-b]: x\n[^a&b]: y")
    ids = re.findall(r'id="(nota[^"]*)"', result.html)
    assert sorted(ids) == ["nota-a-b", "nota-a-b-2", "nota-ref-a-b", "nota-ref-a-b-2"]
    assert 'href="#nota-a-b"' in result.html
    assert 'href="#nota-a-b-2"' in result.html
    assert 'href="#nota-ref-a-b-2"' in result.html


# --------------------------------------------------------------------------- #
# Detalles que rompían la lectura                                              #
# --------------------------------------------------------------------------- #
def test_html_comments_are_dropped_not_shown():
    """`purpose.md` lleva sus instrucciones en comentarios: no son contenido."""
    assert render("antes\n\n<!-- guía interna -->\n\ndespués") == \
        "<p>antes</p>\n<p>después</p>"


def test_backslash_escapes_survive_as_literal_text():
    html = render(r"un \[corchete\] y un \*asterisco\*")
    assert "[corchete]" in html and "*asterisco*" in html
    assert "<em>" not in html


def test_underscores_inside_words_are_not_emphasis():
    assert "<em>" not in render("nombre_de_variable")


def test_rendering_is_deterministic():
    text = "## A\n\n- uno[^f]\n\n| a |\n|---|\n| 1 |\n\n[^f]: nota"
    assert render(text) == render(text)


# --------------------------------------------------------------------------- #
# Texto plano para la búsqueda                                                 #
# --------------------------------------------------------------------------- #
def test_plain_text_keeps_words_and_drops_syntax():
    plain = markdown.to_plain_text(
        "## Retención\n\nEl **churn** cayó.[^f]\n\nVer [Roadmap](../r.md).\n\n[^f]: informe"
    )
    assert "Retención" in plain
    assert "churn" in plain
    assert "Roadmap" in plain          # el texto del enlace también se busca
    assert "**" not in plain and "[" not in plain


def test_plain_text_drops_code_blocks():
    plain = markdown.to_plain_text("prosa\n\n```python\nsecreto_tecnico = 1\n```")
    assert "prosa" in plain
    assert "secreto_tecnico" not in plain


def test_slugify_is_ascii_and_stable():
    assert markdown.slugify("Retención de Clientes") == "retencion-de-clientes"
    assert markdown.slugify("···") == "seccion"
