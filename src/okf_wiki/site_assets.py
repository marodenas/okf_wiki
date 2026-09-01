"""CSS y JS del sitio estático que genera `okf-wiki build`.

Van como constantes de módulo, no como ficheros de datos del paquete, por el
mismo motivo que `templates.py`: así el paquete no necesita `package-data` ni una
instalación bien formada para funcionar, y los tests los pueden comparar contra
lo que se escribe en disco.

El contrato con el HTML generado (`site.py`) es corto y está aquí para poder
leerlo de una vez:

- `assets/data.js` declara `window.OKF_SITE = {search: [...], graph: {...}}`.
  Es un script clásico, **no un módulo**, porque el sitio tiene que funcionar
  abierto con doble clic (`file://`), donde `fetch()` de un JSON está bloqueado
  por el origen opaco pero un `<script src>` relativo sí carga.
- `okf-site.js` es progresivo: la portada, el árbol, el lector, la procedencia y
  los enlaces cruzados son HTML plano y se leen sin JavaScript. El JS solo añade
  la búsqueda, el filtro del árbol y el cálculo de caducidad.
- La caducidad (`stale_after`) se resuelve **en el navegador**, no al construir:
  si se calculase en `build`, el HTML dejaría de ser reproducible (dependería del
  día) y una página caducaría sin que nadie reconstruyera el sitio.
"""
from __future__ import annotations

SITE_CSS = """\
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --bg-soft: #f6f7f9;
  --bg-code: #f1f3f5;
  --fg: #1b1f24;
  --fg-soft: #5c6672;
  --line: #dfe3e8;
  --accent: #2563eb;
  --accent-soft: #dbeafe;
  --ok: #0f7b52;
  --warn: #a35a00;
  --bad: #b3261e;
  --radius: 10px;
  --measure: 44rem;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216;
    --bg-soft: #171b21;
    --bg-code: #1c2129;
    --fg: #e6e9ee;
    --fg-soft: #9aa4b2;
    --line: #2a313b;
    --accent: #7aa2f7;
    --accent-soft: #1e293b;
    --ok: #4ade80;
    --warn: #fbbf24;
    --bad: #f87171;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* --- cabecera y búsqueda ------------------------------------------------- */
.okf-top {
  position: sticky; top: 0; z-index: 20;
  display: flex; gap: 1rem; align-items: center; flex-wrap: wrap;
  padding: .7rem 1.4rem;
  background: var(--bg-soft);
  border-bottom: 1px solid var(--line);
}
.okf-brand { font-weight: 700; color: var(--fg); }
.okf-brand small { display: block; font-weight: 400; font-size: .72rem; color: var(--fg-soft); }
.okf-search { position: relative; margin-left: auto; flex: 1 1 16rem; max-width: 26rem; }
.okf-search input {
  width: 100%; padding: .45rem .7rem;
  border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--bg); color: var(--fg); font: inherit; font-size: .92rem;
}
.okf-search input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.okf-results {
  position: absolute; top: calc(100% + .35rem); left: 0; right: 0;
  max-height: 60vh; overflow-y: auto;
  background: var(--bg); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: 0 12px 30px rgba(0,0,0,.16);
  padding: .3rem; margin: 0; list-style: none;
}
.okf-results[hidden] { display: none; }
.okf-results li a { display: block; padding: .45rem .55rem; border-radius: 7px; color: var(--fg); }
.okf-results li a:hover, .okf-results li a:focus { background: var(--accent-soft); text-decoration: none; }
.okf-results .okf-r-title { font-weight: 600; }
.okf-results .okf-r-path { font-size: .74rem; color: var(--fg-soft); }
.okf-results .okf-r-desc { font-size: .82rem; color: var(--fg-soft); }
.okf-results .okf-empty { padding: .5rem .6rem; color: var(--fg-soft); font-size: .88rem; }

/* --- estructura ---------------------------------------------------------- */
.okf-wrap { max-width: 78rem; margin: 0 auto; padding: 1.6rem 1.4rem 4rem; }
.okf-reader { display: grid; grid-template-columns: minmax(0, 1fr) 19rem; gap: 2.2rem; }
@media (max-width: 62rem) { .okf-reader { grid-template-columns: minmax(0, 1fr); } }
.okf-crumbs { font-size: .82rem; color: var(--fg-soft); margin-bottom: .5rem; }
.okf-crumbs a { color: var(--fg-soft); }
article { max-width: var(--measure); }
article h1 { font-size: 1.9rem; line-height: 1.2; margin: .2rem 0 .5rem; }
article h2 { font-size: 1.32rem; margin: 2rem 0 .6rem; padding-bottom: .25rem; border-bottom: 1px solid var(--line); }
article h3 { font-size: 1.1rem; margin: 1.5rem 0 .4rem; }
article h4, article h5, article h6 { font-size: 1rem; margin: 1.2rem 0 .3rem; }
.okf-lead { font-size: 1.05rem; color: var(--fg-soft); margin: 0 0 1.2rem; }
.okf-anchor { margin-left: .4rem; opacity: 0; color: var(--fg-soft); font-weight: 400; }
h2:hover .okf-anchor, h3:hover .okf-anchor { opacity: 1; }
article img { max-width: 100%; height: auto; border-radius: var(--radius); }
blockquote {
  margin: 1rem 0; padding: .1rem 1rem;
  border-left: 3px solid var(--accent); background: var(--bg-soft);
  border-radius: 0 var(--radius) var(--radius) 0;
}
code { background: var(--bg-code); padding: .12em .35em; border-radius: 5px; font-size: .88em; }
.okf-code { position: relative; margin: 1rem 0; }
.okf-code pre, pre.okf-mermaid {
  margin: 0; padding: .9rem 1rem; overflow-x: auto;
  background: var(--bg-code); border: 1px solid var(--line); border-radius: var(--radius);
  font-size: .84rem; line-height: 1.5;
}
.okf-code pre code { background: none; padding: 0; font-size: inherit; }
.okf-code-lang {
  position: absolute; top: .35rem; right: .6rem;
  font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--fg-soft);
}
pre.okf-mermaid::before {
  content: "mermaid"; display: block; margin-bottom: .4rem;
  font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--fg-soft);
}
.okf-table-wrap { overflow-x: auto; margin: 1rem 0; }
table { border-collapse: collapse; width: 100%; font-size: .92rem; }
th, td { border: 1px solid var(--line); padding: .45rem .6rem; text-align: left; vertical-align: top; }
th { background: var(--bg-soft); }
.okf-center { text-align: center; }
.okf-right { text-align: right; }

/* --- enlaces cruzados ----------------------------------------------------- */
a.okf-link-broken { color: var(--bad); text-decoration: underline dotted; }
a.okf-link-broken::after { content: " ⚠"; font-size: .8em; }
a.okf-link-absolute { text-decoration: underline dotted; }
.okf-note-ref a { font-size: .78em; padding: 0 .12em; }
.okf-note-missing { color: var(--bad); }
.okf-notes { margin-top: 2.5rem; border-top: 1px solid var(--line); padding-top: .6rem; font-size: .9rem; }
.okf-notes h2 { font-size: 1rem; border: 0; }
.okf-notes ol { padding-left: 1.2rem; }
.okf-note-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .82em; color: var(--fg-soft); }
.okf-note-back { margin-left: .3rem; }

/* --- panel de procedencia ------------------------------------------------- */
.okf-aside { font-size: .88rem; }
.okf-card {
  background: var(--bg-soft); border: 1px solid var(--line);
  border-radius: var(--radius); padding: .9rem 1rem; margin-bottom: 1rem;
}
.okf-card h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em;
  color: var(--fg-soft); margin: 0 0 .6rem; border: 0; }
.okf-card ul { margin: 0; padding-left: 1.1rem; }
.okf-purpose { font-size: 1rem; }
.okf-purpose h2 { font-size: 1.02rem; text-transform: none; letter-spacing: 0; color: var(--fg); }
.okf-purpose p:last-child, .okf-purpose ul:last-child { margin-bottom: 0; }
.okf-card li + li { margin-top: .35rem; }
.okf-meta { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: .25rem .7rem; }
.okf-meta dt { color: var(--fg-soft); }
.okf-meta dd { margin: 0; word-break: break-word; }
.okf-src-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8em; color: var(--fg-soft); }

/* --- distintivos ---------------------------------------------------------- */
.okf-badges { display: flex; flex-wrap: wrap; gap: .35rem; margin: .5rem 0 1rem; }
.okf-badge {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .12rem .55rem; border-radius: 999px;
  border: 1px solid var(--line); background: var(--bg);
  font-size: .74rem; letter-spacing: .01em; color: var(--fg-soft);
}
.okf-badge-status-stable { border-color: var(--ok); color: var(--ok); }
.okf-badge-status-draft { border-color: var(--warn); color: var(--warn); }
.okf-badge-status-deprecated { border-color: var(--bad); color: var(--bad); }
.okf-badge-trust-human-reviewed { border-color: var(--ok); color: var(--ok); }
.okf-badge-trust-machine-confirmed { border-color: var(--accent); color: var(--accent); }
.okf-badge-stale { border-color: var(--bad); color: var(--bad); }
.okf-badge-stale[hidden] { display: none; }

/* --- portada -------------------------------------------------------------- */
.okf-hero h1 { font-size: 2.1rem; margin: .2rem 0 .3rem; }
.okf-stats { display: flex; flex-wrap: wrap; gap: .6rem; margin: 1.2rem 0 2rem; padding: 0; list-style: none; }
.okf-stats li {
  flex: 1 1 8rem; background: var(--bg-soft); border: 1px solid var(--line);
  border-radius: var(--radius); padding: .7rem .9rem;
}
.okf-stats b { display: block; font-size: 1.5rem; line-height: 1.1; }
.okf-stats span { font-size: .76rem; color: var(--fg-soft); text-transform: uppercase; letter-spacing: .05em; }
.okf-cols { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 2.2rem; }
@media (max-width: 62rem) { .okf-cols { grid-template-columns: minmax(0, 1fr); } }
.okf-tree { list-style: none; padding: 0; margin: 0; }
.okf-tree > li { margin-bottom: 1.4rem; }
.okf-topic { display: flex; align-items: baseline; gap: .5rem; margin: 0 0 .2rem; font-size: 1.15rem; }
.okf-topic .okf-count { font-size: .76rem; color: var(--fg-soft); font-weight: 400; }
.okf-sub { list-style: none; padding-left: 0; margin: .4rem 0 0; }
.okf-sub > li { border-left: 2px solid var(--line); padding-left: .9rem; margin-bottom: .7rem; }
.okf-sub h4 { margin: 0 0 .2rem; font-size: .8rem; text-transform: uppercase;
  letter-spacing: .05em; color: var(--fg-soft); }
.okf-pages { list-style: none; padding: 0; margin: 0; }
.okf-pages li { padding: .12rem 0; }
.okf-pages .okf-p-desc { color: var(--fg-soft); font-size: .84rem; }
.okf-tree li[hidden], .okf-pages li[hidden] { display: none; }
.okf-empty-tree { color: var(--fg-soft); }
footer.okf-foot {
  margin-top: 3rem; padding-top: .8rem; border-top: 1px solid var(--line);
  font-size: .8rem; color: var(--fg-soft);
}
"""


SITE_JS = """\
/* okf-wiki — mejoras progresivas del sitio estático.
   Sin esto la wiki se lee igual: solo se pierden la búsqueda, el filtro del
   árbol y el distintivo de caducidad. */
(function () {
  "use strict";
  var DATA = (window.OKF_SITE || {});
  var INDEX = DATA.search || [];
  var BASE = document.documentElement.getAttribute("data-okf-base") || "";

  function fold(text) {
    return String(text == null ? "" : text)
      .normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
  }

  /* --- caducidad: se decide al leer, no al construir --------------------- */
  function markStale() {
    var today = new Date().toISOString().slice(0, 10);
    var nodes = document.querySelectorAll("[data-stale-after]");
    for (var i = 0; i < nodes.length; i++) {
      var limit = nodes[i].getAttribute("data-stale-after");
      if (limit && today >= limit) { nodes[i].hidden = false; }
    }
  }

  /* --- búsqueda ---------------------------------------------------------- */
  var HAYSTACK = INDEX.map(function (page) {
    return fold([page.title, page.description, (page.tags || []).join(" "),
                 page.topic, page.type, page.path, page.text].join(" \\n "));
  });

  function score(page, i, terms) {
    var hay = HAYSTACK[i];
    var title = fold(page.title);
    var total = 0;
    for (var t = 0; t < terms.length; t++) {
      if (hay.indexOf(terms[t]) === -1) { return 0; }
      total += title.indexOf(terms[t]) !== -1 ? 3 : 1;
    }
    return total;
  }

  function search(query) {
    var terms = fold(query).split(/\\s+/).filter(Boolean);
    if (!terms.length) { return []; }
    var hits = [];
    for (var i = 0; i < INDEX.length; i++) {
      var value = score(INDEX[i], i, terms);
      if (value > 0) { hits.push({ page: INDEX[i], score: value }); }
    }
    hits.sort(function (a, b) {
      if (b.score !== a.score) { return b.score - a.score; }
      return a.page.path < b.page.path ? -1 : 1;   /* empate → orden estable */
    });
    return hits.slice(0, 20);
  }

  function text(tag, cls, value) {
    var node = document.createElement(tag);
    node.className = cls;
    node.textContent = value || "";
    return node;
  }

  function wireSearch() {
    var input = document.getElementById("okf-q");
    var list = document.getElementById("okf-results");
    if (!input || !list) { return; }
    input.disabled = false;
    input.placeholder = "Buscar en " + INDEX.length + " páginas…  ( / )";

    function render() {
      list.textContent = "";
      var hits = search(input.value);
      if (!input.value.trim()) { list.hidden = true; return; }
      if (!hits.length) {
        list.appendChild(text("li", "okf-empty", "Sin resultados para «" + input.value + "»."));
        list.hidden = false;
        return;
      }
      hits.forEach(function (hit) {
        var item = document.createElement("li");
        var link = document.createElement("a");
        link.href = BASE + hit.page.path;
        link.appendChild(text("span", "okf-r-title", hit.page.title));
        link.appendChild(text("span", "okf-r-path", hit.page.source));
        if (hit.page.description) {
          link.appendChild(text("span", "okf-r-desc", hit.page.description));
        }
        item.appendChild(link);
        list.appendChild(item);
      });
      list.hidden = false;
    }

    input.addEventListener("input", render);
    input.addEventListener("focus", render);
    document.addEventListener("click", function (event) {
      if (!list.contains(event.target) && event.target !== input) { list.hidden = true; }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "/" && document.activeElement !== input) {
        event.preventDefault();
        input.focus();
        input.select();
      } else if (event.key === "Escape") {
        list.hidden = true;
        input.blur();
      }
    });
  }

  /* --- filtro del árbol de la portada ------------------------------------ */
  function wireTree() {
    var input = document.getElementById("okf-filter");
    var tree = document.querySelector(".okf-tree");
    if (!input || !tree) { return; }
    input.disabled = false;
    input.addEventListener("input", function () {
      var needle = fold(input.value.trim());
      var topics = tree.children;
      for (var t = 0; t < topics.length; t++) {
        var visible = 0;
        var pages = topics[t].querySelectorAll(".okf-pages > li");
        for (var p = 0; p < pages.length; p++) {
          var hit = !needle || fold(pages[p].textContent).indexOf(needle) !== -1;
          pages[p].hidden = !hit;
          if (hit) { visible++; }
        }
        var groups = topics[t].querySelectorAll(".okf-sub > li");
        for (var g = 0; g < groups.length; g++) {
          groups[g].hidden = !groups[g].querySelector(".okf-pages > li:not([hidden])");
        }
        topics[t].hidden = visible === 0;
      }
    });
  }

  markStale();
  wireSearch();
  wireTree();
})();
"""
