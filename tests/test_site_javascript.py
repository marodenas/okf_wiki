"""El JavaScript del sitio, ejecutado de verdad (con Node, si está disponible).

La búsqueda y el distintivo de caducidad son las dos únicas piezas del sitio que
viven en el navegador: el resto es HTML plano que ya comprueba
`test_site_build.py`. Sin esta prueba serían las dos únicas cosas que se pueden
romper sin que falle nada.

El DOM se sustituye por un doble mínimo —lo justo que usa `okf-site.js`— en vez
de instalar un navegador: la prueba se salta sola si no hay `node` en el PATH.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from okf_wiki import site

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node no está instalado")

# Doble de DOM: solo las llamadas que hace `okf-site.js`. Al final imprime en
# JSON lo que ha quedado en la página, que es lo que la prueba inspecciona.
HARNESS = """
'use strict';
const fs = require('fs');
const results = { entries: [], stale: [], placeholder: null, empty: null };

function node(tag) {
  return {
    tag, className: '', textContent: '', href: '', hidden: false, children: [],
    appendChild(child) { this.children.push(child); return child; },
    contains() { return false; },
    addEventListener(kind, fn) { (this.handlers ||= {})[kind] = fn; },
    setAttribute() {}, getAttribute() { return null; }, focus() {}, select() {},
    blur() {},
  };
}

const input = node('input');
const list = node('ul');
list.textContent = '';
Object.defineProperty(list, 'textContent', {
  set(value) { if (value === '') { this.children.length = 0; } }, get() { return ''; },
});

const staleNodes = STALE_DATES.map(function (date) {
  const element = node('span');
  element.hidden = true;
  element.getAttribute = function () { return date; };
  return element;
});

global.window = {};
global.document = {
  documentElement: { getAttribute: () => '' },
  getElementById: (id) => (id === 'okf-q' ? input : id === 'okf-results' ? list : null),
  querySelector: () => null,
  querySelectorAll: (selector) =>
    (selector === '[data-stale-after]' ? staleNodes : []),
  createElement: node,
  addEventListener() {},
};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // data.js  → window.OKF_SITE
eval(fs.readFileSync(process.argv[3], 'utf8'));   // okf-site.js

results.placeholder = input.placeholder;
input.value = process.argv[4];
input.handlers.input();
// Un resultado es <li><a>…</a></li>; el aviso de «sin resultados» es un <li> suelto.
const rendered = list.children;
results.empty = rendered.length === 1 && rendered[0].children.length === 0
  ? rendered[0].textContent : null;
results.entries = rendered
  .filter((item) => item.children.length)
  .map(function (item) {
    const link = item.children[0];
    return { href: link.href, title: (link.children[0] || {}).textContent };
  });
results.stale = staleNodes.map((element) => element.hidden);
console.log(JSON.stringify(results));
"""


def run_js(out: Path, query: str, stale_dates: list[str], tmp_path: Path) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(
        HARNESS.replace("STALE_DATES", json.dumps(stale_dates)), encoding="utf-8"
    )
    completed = subprocess.run(
        ["node", str(harness), str(out / site.DATA_JS_PATH), str(out / site.JS_PATH),
         query],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_search_finds_a_page_by_a_word_of_its_body(instance, tmp_path):
    out = site.build_site(instance)["out"]
    found = run_js(out, "churn", [], tmp_path)
    assert [entry["href"] for entry in found["entries"]] == [
        "ventas/retencion/plan-retencion-2026.html"
    ]
    assert found["entries"][0]["title"] == "Plan de retención 2026"


def test_search_ignores_accents_and_case(instance, tmp_path):
    out = site.build_site(instance)["out"]
    found = run_js(out, "RETENCION", [], tmp_path)
    assert any("plan-retencion-2026" in entry["href"] for entry in found["entries"])


def test_search_requires_every_term_and_ranks_the_title_first(instance, tmp_path):
    out = site.build_site(instance)["out"]
    hits = [e["href"] for e in run_js(out, "roadmap", [], tmp_path)["entries"]]
    assert hits[0] == "producto/roadmap/roadmap-2026.html", "el título pesa más que el cuerpo"
    assert run_js(out, "roadmap churnimposible", [], tmp_path)["entries"] == []


def test_search_says_when_there_is_nothing(instance, tmp_path):
    out = site.build_site(instance)["out"]
    assert "Sin resultados" in run_js(out, "zzzzz", [], tmp_path)["empty"]


def test_search_box_announces_how_many_pages_it_covers(instance, tmp_path):
    out = site.build_site(instance)["out"]
    assert "4 páginas" in run_js(out, "", [], tmp_path)["placeholder"]


def test_stale_badge_appears_only_once_the_date_has_passed(instance, tmp_path):
    """Se decide al leer: por eso el HTML construido puede ser reproducible."""
    out = site.build_site(instance)["out"]
    found = run_js(out, "", ["1999-01-01", "2999-01-01"], tmp_path)
    assert found["stale"] == [False, True], "caducada visible, futura oculta"
