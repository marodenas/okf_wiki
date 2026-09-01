"""`okf-wiki build`: el sitio estático de lectura.

Las pruebas están agrupadas por la promesa que protegen: la salida vive fuera
del bundle, es reproducible, no necesita el motor OKF, y contiene lo que la
interfaz promete (portada, árbol, lector, procedencia/estado, enlaces cruzados,
búsqueda y datos de grafo).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from okf_wiki import cli, helpers, site

PLAN_HTML = "ventas/retencion/plan-retencion-2026.html"
ROADMAP_HTML = "producto/roadmap/roadmap-2026.html"

#: Dos entradas de manifest que JSON admite y ninguna ruta puede llevar: un NUL
#: embebido y un surrogate suelto. Con ellas `os.path.realpath` no devuelve ruta,
#: lanza (`ValueError` y `UnicodeEncodeError`), así que el guard tiene que
#: rechazarlas como a cualquier otra entrada que no cae dentro de `--out`. Se
#: escriben con `chr()` para que este fichero siga siendo texto imprimible.
UNNAMEABLE_ENTRIES = ["nulo" + chr(0) + ".html", "sur" + chr(0xD800) + "ate.html"]
UNNAMEABLE_IDS = ["nul-embebido", "surrogate-suelto"]


def read(out: Path, rel: str) -> str:
    return (out / rel).read_text(encoding="utf-8")


def add_manifest_entries(out: Path, *entries: str) -> None:
    """Mete entradas en `.okf-site.json`, como si el manifest viniera así del disco.

    El manifest es un fichero cualquiera de la carpeta de salida: lo puede haber
    editado su dueño, un merge o alguien con mala idea. `build` lo lee para saber
    qué borrar, así que hay que probar qué hace con entradas que no escribió él.
    """
    path = out / site.MANIFEST_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["files"] = sorted(set(data["files"]) | set(entries))
    # `ensure_ascii=True`: hay entradas que al disco solo pueden llegar escapadas
    # —un surrogate suelto no es UTF-8 codificable—, y `json.loads` las devuelve
    # igual. El fichero dice lo mismo que con el `ensure_ascii=False` que escribe
    # `build`, y así el helper sirve para cualquier entrada.
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n",
                    encoding="utf-8")


def symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):  # Windows sin privilegios
        pytest.skip("este sistema no permite crear enlaces simbólicos")


def page_without_type(bundle: Path, rel: str, title: str) -> Path:
    """Una página con frontmatter pero sin `type`, como la que `verify` rechaza."""
    path = bundle / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{title}"\ndescription: "Sin type."\n---\n\nCuerpo.\n',
                    encoding="utf-8")
    return path


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# --------------------------------------------------------------------------- #
# La salida vive fuera del bundle                                              #
# --------------------------------------------------------------------------- #
def test_default_output_is_a_sibling_of_wiki(instance):
    stats = site.build_site(instance)
    assert stats["out"] == instance / site.SITE_DIRNAME
    assert (instance / "site" / "index.html").is_file()


def test_build_writes_nothing_inside_the_bundle(instance):
    before = snapshot(instance / "wiki")
    site.build_site(instance)
    assert snapshot(instance / "wiki") == before, "el bundle no se toca al construir"


def test_build_writes_nothing_inside_sources(instance):
    before = snapshot(instance / "sources")
    site.build_site(instance)
    assert snapshot(instance / "sources") == before


def test_out_inside_the_bundle_is_refused(instance):
    with pytest.raises(site.SiteError) as err:
        site.build_site(instance, out=instance / "wiki" / "site")
    assert "dentro del bundle" in str(err.value)
    assert not (instance / "wiki" / "site").exists(), "no se crea nada al rechazar"


def test_out_containing_the_bundle_is_refused(instance):
    with pytest.raises(site.SiteError) as err:
        site.build_site(instance, out=instance)
    assert "contiene al bundle" in str(err.value)


def test_flat_bundle_demands_an_explicit_out(tmp_path):
    """Sin `wiki/`, cualquier subcarpeta cae dentro del bundle: hay que elegir."""
    flat = tmp_path / "plana"
    flat.mkdir()
    (flat / "nota.md").write_text('---\ntype: Nota\ntitle: "N"\n---\n\ncuerpo\n',
                                  encoding="utf-8")
    with pytest.raises(site.SiteError) as err:
        site.build_site(flat)
    assert "--out" in str(err.value)
    stats = site.build_site(flat, out=tmp_path / "sitio")
    assert stats["pages"] == 1


def test_missing_bundle_fails_like_the_other_commands(tmp_path):
    with pytest.raises(helpers.MissingBundleError):
        site.build_site(tmp_path / "no-existe")


def test_foreign_output_directory_is_not_clobbered(instance, tmp_path):
    foreign = tmp_path / "ajena"
    foreign.mkdir()
    (foreign / "mio.txt").write_text("no me borres", encoding="utf-8")
    with pytest.raises(site.SiteError) as err:
        site.build_site(instance, out=foreign)
    assert "--force" in str(err.value)
    assert (foreign / "mio.txt").is_file()

    site.build_site(instance, out=foreign, force=True)
    assert (foreign / "index.html").is_file()
    assert (foreign / "mio.txt").is_file(), "--force adopta, no vacía"


# --------------------------------------------------------------------------- #
# Reproducibilidad                                                             #
# --------------------------------------------------------------------------- #
def test_two_builds_produce_identical_bytes(instance):
    site.build_site(instance)
    first = snapshot(instance / "site")
    site.build_site(instance)
    assert snapshot(instance / "site") == first


def test_a_second_build_reports_no_file_written(instance):
    site.build_site(instance)
    assert site.build_site(instance)["changed"] == []


def test_build_does_not_bake_todays_date_into_the_html(instance):
    """La caducidad se evalúa al leer: si no, el HTML dependería del día."""
    out = site.build_site(instance)["out"]
    page = read(out, PLAN_HTML)
    assert 'data-stale-after="2027-01-01"' in page
    assert "okf-badge-stale" in page and "hidden>" in page


def test_deleting_a_page_removes_its_html_on_rebuild(instance):
    out = site.build_site(instance)["out"]
    assert (out / ROADMAP_HTML).is_file()
    (instance / "wiki" / "producto" / "roadmap" / "roadmap-2026.md").unlink()
    stats = site.build_site(instance)
    assert ROADMAP_HTML in stats["removed"]
    assert not (out / ROADMAP_HTML).exists()
    assert not (out / "producto").exists(), "las carpetas vacías se recogen"


def test_rebuild_never_deletes_files_it_did_not_generate(instance):
    out = site.build_site(instance)["out"]
    (out / "apunte.txt").write_text("mío", encoding="utf-8")
    site.build_site(instance)
    assert (out / "apunte.txt").is_file()


# --------------------------------------------------------------------------- #
# H1: la limpieza no sale nunca de `--out`                                     #
# --------------------------------------------------------------------------- #
# El manifest dice qué borró la pasada anterior, y `build` lo obedece con
# `unlink()`. Como es un fichero del disco que `build` no controla, cada forma de
# escaparse de `--out` tiene que quedarse en la puerta: un fichero de fuera nunca
# desaparece por reconstruir el sitio.
def test_a_manifest_entry_with_dotdot_in_the_middle_does_not_escape_the_output(
        instance, tmp_path):
    """`assets/../../` no empieza por `..` y se salía igual de la carpeta."""
    out = site.build_site(instance)["out"]
    outsider = tmp_path / "no-me-toques.txt"
    outsider.write_text("un fichero de fuera del sitio", encoding="utf-8")
    escape = f"assets/../../../{outsider.name}"
    assert (out / escape).resolve() == outsider, "la ruta de verdad se escapa"

    add_manifest_entries(out, escape)
    stats = site.build_site(instance)

    assert outsider.read_text(encoding="utf-8") == "un fichero de fuera del sitio"
    assert escape in stats["refused"]
    assert escape not in stats["removed"]


def test_an_absolute_manifest_entry_is_refused(instance, tmp_path):
    out = site.build_site(instance)["out"]
    outsider = tmp_path / "absoluto.txt"
    outsider.write_text("tampoco", encoding="utf-8")

    add_manifest_entries(out, outsider.as_posix())
    stats = site.build_site(instance)

    assert outsider.is_file()
    assert outsider.as_posix() in stats["refused"]


def test_a_manifest_entry_behind_a_symlinked_directory_is_not_followed(instance, tmp_path):
    """`out/atajo` → fuera: seguirlo borraría ficheros que el sitio no escribió."""
    out = site.build_site(instance)["out"]
    outside_dir = tmp_path / "fuera"
    outside_dir.mkdir()
    (outside_dir / "clave.txt").write_text("secreto", encoding="utf-8")
    symlink(out / "atajo", outside_dir)

    add_manifest_entries(out, "atajo/clave.txt")
    stats = site.build_site(instance)

    assert (outside_dir / "clave.txt").is_file()
    assert "atajo/clave.txt" in stats["refused"]
    assert (out / "atajo").is_symlink(), "el enlace tampoco se toca"


def test_a_manifest_entry_that_is_itself_a_symlink_is_not_followed(instance, tmp_path):
    out = site.build_site(instance)["out"]
    outsider = tmp_path / "diario.txt"
    outsider.write_text("mi diario", encoding="utf-8")
    symlink(out / "enlace.txt", outsider)

    add_manifest_entries(out, "enlace.txt")
    stats = site.build_site(instance)

    assert outsider.is_file()
    assert "enlace.txt" in stats["refused"]


def test_a_symlinked_directory_inside_the_output_survives_the_empty_dir_sweep(
        instance, tmp_path):
    """La recogida de carpetas vacías no puede entrar ni en `rmdir` un enlace."""
    out = site.build_site(instance)["out"]
    empty_outside = tmp_path / "vacia-de-fuera"
    empty_outside.mkdir()
    symlink(out / "atajo", empty_outside)

    site.build_site(instance)

    assert (out / "atajo").is_symlink()
    assert empty_outside.is_dir()


def test_a_corrupt_manifest_entry_that_is_not_a_string_is_refused(instance):
    """Un manifest a mano puede traer cualquier cosa: no puede reventar el build."""
    out = site.build_site(instance)["out"]
    path = out / site.MANIFEST_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["files"] = [*data["files"], 42, None]
    path.write_text(json.dumps(data), encoding="utf-8")

    stats = site.build_site(instance)

    assert stats["refused"] == ["42", "None"]
    assert (out / "index.html").is_file()


@pytest.mark.parametrize("entry", UNNAMEABLE_ENTRIES, ids=UNNAMEABLE_IDS)
def test_a_manifest_entry_the_filesystem_cannot_name_is_refused(instance, entry):
    """Rechazada, no reventada: con estas cadenas `realpath` lanza en vez de resolver.

    Un NUL embebido da `ValueError` y un surrogate suelto `UnicodeEncodeError`, y
    los dos subían sin tocar hasta el CLI: el sitio se quedaba a medio construir
    con un traceback, que es la peor respuesta posible a un manifest de otro.
    """
    out = site.build_site(instance)["out"]
    before = snapshot(out)
    add_manifest_entries(out, entry)

    stats = site.build_site(instance)

    assert stats["refused"] == [ascii(entry)], "se avisa, y de forma imprimible"
    assert stats["removed"] == [], "no había nada que borrar: no es una ruta"
    assert snapshot(out) == before, "el sitio queda entero y sin borrados"


def test_the_warning_for_an_unnameable_manifest_entry_can_be_printed(instance, capsys):
    """El aviso acaba en `print`: si la entrada no es codificable, revienta ahí.

    Da igual haber rechazado la entrada si contarlo tira el comando: `build` tiene
    que salir con 0 y el aviso tiene que verse.
    """
    out = site.build_site(instance)["out"]
    capsys.readouterr()
    add_manifest_entries(out, *UNNAMEABLE_ENTRIES)

    code = cli.main(["build", str(instance)])

    captured = capsys.readouterr()
    assert code == 0
    assert f"AVISO: {len(UNNAMEABLE_ENTRIES)} entradas" in captured.out
    for entry in UNNAMEABLE_ENTRIES:
        assert ascii(entry) in captured.out


def test_the_real_cleanup_still_works_after_the_guard(instance):
    """La contrapartida: lo que sí está dentro de `--out` se sigue borrando."""
    out = site.build_site(instance)["out"]
    (instance / "wiki" / "producto" / "roadmap" / "roadmap-2026.md").unlink()
    stats = site.build_site(instance)
    assert ROADMAP_HTML in stats["removed"] and stats["refused"] == []
    assert not (out / ROADMAP_HTML).exists()


def test_force_does_not_start_deleting_the_foreign_files_it_adopts(instance, tmp_path):
    """`--force` adopta la carpeta; el manifest que escribe solo lista lo suyo."""
    foreign = tmp_path / "ajena"
    foreign.mkdir()
    (foreign / "mio.txt").write_text("no me borres", encoding="utf-8")
    site.build_site(instance, out=foreign, force=True)
    site.build_site(instance, out=foreign, force=True)
    assert (foreign / "mio.txt").is_file()


def test_manifest_target_rejects_every_way_out_of_the_output(tmp_path):
    """El guard, en unidad: lo que entra y lo que no."""
    out = tmp_path / "salida"
    out.mkdir()
    for escape in ("", "..", "../vecino.txt", "assets/../../fuera.txt",
                   "/etc/passwd", r"C:\Windows\win.ini", r"..\vecino.txt",
                   "assets/./../../fuera.txt", *UNNAMEABLE_ENTRIES):
        assert site._manifest_target(out, escape) is None, ascii(escape)
    assert site._manifest_target(out, "index.html") == out / "index.html"
    assert site._manifest_target(out, "assets/okf-site.css") == out / "assets/okf-site.css"
    assert site._manifest_target(out, "./data/graph.json") == out / "data/graph.json"


# --------------------------------------------------------------------------- #
# H3: el bundle `wiki/` no es la instancia                                     #
# --------------------------------------------------------------------------- #
def test_the_bundle_passed_as_the_instance_is_refused(instance, tmp_path):
    """Se aceptaba como bundle plano y salía un sitio sin propósito ni fuentes."""
    destino = tmp_path / "sitio"
    with pytest.raises(helpers.BundleAsInstanceError) as err:
        site.build_site(instance / "wiki", out=destino)
    assert "no la instancia" in str(err.value)
    assert str(instance.resolve()) in str(err.value), "dice dónde repetirlo"
    assert not destino.exists(), "no se escribe nada al rechazar"


def test_the_bundle_passed_as_the_instance_is_refused_with_the_default_out(instance):
    """Sin `--out` fallaba por «la salida está dentro del bundle»: otra causa."""
    with pytest.raises(helpers.BundleAsInstanceError):
        site.build_site(instance / "wiki")
    assert not (instance / "wiki" / "site").exists()


def test_a_flat_bundle_called_wiki_still_builds_when_its_parent_is_not_an_instance(
        tmp_path):
    """Compatibilidad: `wiki/` solo se rechaza si su padre parece una instancia."""
    flat = tmp_path / "descarga" / "wiki"
    flat.mkdir(parents=True)
    (flat / "nota.md").write_text('---\ntype: Nota\ntitle: "N"\n---\n\ncuerpo\n',
                                  encoding="utf-8")
    stats = site.build_site(flat, out=tmp_path / "sitio")
    assert stats["pages"] == 1


def test_an_instance_that_is_itself_called_wiki_still_builds(instance, tmp_path):
    """La contrapartida de H3: el guard mira el nombre, y `wiki` es un nombre legal.

    Se monta el caso que dispara la heurística sin ser un bundle: la instancia se
    llama `wiki` **y** su padre parece otra instancia (tiene `sources/`). Lo que la
    salva son las dos salvaguardas de `looks_like_bundle_dir`: trae su propio
    `wiki/` dentro y su propio `purpose.md`.
    """
    parent = tmp_path / "padre"
    (parent / "sources").mkdir(parents=True)
    disguised = parent / "wiki"
    shutil.copytree(instance, disguised)
    assert not helpers.looks_like_bundle_dir(disguised)

    stats = site.build_site(disguised)

    assert stats["out"] == disguised / "site"
    assert stats["bundle"] == disguised / "wiki", "el bundle es el de dentro"
    assert PLAN_HTML in stats["files"]
    cover = read(stats["out"], "index.html")
    assert "Decidir dónde invertir en retención." in cover, "la portada tiene propósito"


def test_resolve_site_instance_returns_both_paths_for_a_real_instance(instance):
    assert helpers.resolve_site_instance(instance) == (instance, instance / "wiki")


def test_bundle_as_instance_is_an_ingest_error():
    """Tiene que salir por exit 3, como el resto de rutas de trabajo inservibles."""
    assert issubclass(helpers.BundleAsInstanceError, helpers.IngestError)


# --------------------------------------------------------------------------- #
# No depende del motor OKF                                                     #
# --------------------------------------------------------------------------- #
def test_the_site_modules_do_not_import_the_engine():
    """`build`/`watch` tienen que funcionar sin `reference_agent` instalado."""
    root = Path(cli.__file__).parent
    for module in ("site.py", "markdown.py", "watch.py", "site_assets.py"):
        source = (root / module).read_text(encoding="utf-8")
        assert "import reference_agent" not in source
        assert "_engine(" not in source


def test_frontmatter_parsing_matches_the_engine(instance):
    """El parser propio tiene que leer lo mismo que `OKFDocument.parse`."""
    engine = pytest.importorskip("reference_agent.bundle.document")
    for md in sorted((instance / "wiki").rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        frontmatter, body = site.parse_document(text)
        doc = engine.OKFDocument.parse(text)
        assert frontmatter == doc.frontmatter
        # El salto final sobra o falta según se parta con `splitlines()` (motor) o
        # con `split("\n")` (aquí); no cambia ni una palabra del cuerpo.
        assert body.rstrip("\n") == doc.body.rstrip("\n")


# --------------------------------------------------------------------------- #
# Portada y árbol tema/subtema                                                 #
# --------------------------------------------------------------------------- #
def test_cover_lists_every_topic_subtopic_and_page(instance):
    out = site.build_site(instance)["out"]
    cover = read(out, "index.html")
    assert "Ventas" in cover and "Producto" in cover
    assert "Retencion" in cover and "Pipeline" in cover
    assert f'href="{PLAN_HTML}"' in cover
    assert f'href="{ROADMAP_HTML}"' in cover
    assert 'href="log.html"' in cover, "la página de la raíz del bundle también sale"


def test_cover_shows_status_and_trust_of_the_bundle(instance):
    cover = read(site.build_site(instance)["out"], "index.html")
    assert "borrador" in cover and "estable" in cover
    assert "revisado por una persona" in cover and "sin verificar" in cover
    assert "Sin fuentes" in cover and "Aisladas en el grafo" in cover


def test_cover_shows_the_purpose_when_it_is_filled_in(instance):
    cover = read(site.build_site(instance)["out"], "index.html")
    assert "Decidir dónde invertir en retención." in cover
    assert "Este comentario no debe aparecer" not in cover


def test_cover_hides_an_unfilled_purpose_stub(instance):
    from okf_wiki import templates

    (instance / "purpose.md").write_text(templates.render("purpose", "X"),
                                         encoding="utf-8")
    cover = read(site.build_site(instance)["out"], "index.html")
    assert templates.STUB_MARKER not in cover
    assert "Rellena este fichero" not in cover


# H2: una página sin `type` no tiene por qué inventarse el nombre del tema
def test_a_topic_of_pages_without_type_is_labelled_by_its_folder(instance):
    """La portada ponía «Sin tipo» como si fuera el nombre del tema."""
    page_without_type(instance / "wiki", "operaciones/turnos.md", "Turnos")
    cover = read(site.build_site(instance)["out"], "index.html")
    assert '<h3 class="okf-topic">Operaciones' in cover
    assert site.TYPE_UNSET_LABEL not in cover


def test_a_page_without_type_stops_its_topic_from_borrowing_the_neighbours_label(instance):
    """Si no todas declaran lo mismo, no hay nombre de tema: manda la carpeta."""
    operaciones = instance / "wiki" / "operaciones"
    page_without_type(instance / "wiki", "operaciones/guardias.md", "Guardias")
    (operaciones / "turnos.md").write_text(
        '---\ntype: Personas\ntitle: "Turnos"\n---\n\nCuerpo.\n', encoding="utf-8")
    cover = read(site.build_site(instance)["out"], "index.html")
    assert '<h3 class="okf-topic">Operaciones' in cover
    assert "Personas" not in cover, "el type de la vecina no nombra el tema"


def test_a_topic_whose_pages_agree_on_type_is_still_labelled_by_it(instance):
    """La contrapartida: con consenso, la etiqueta sigue siendo el `type`."""
    operaciones = instance / "wiki" / "operaciones"
    operaciones.mkdir(parents=True)
    for stem in ("turnos", "guardias"):
        (operaciones / f"{stem}.md").write_text(
            f'---\ntype: Personas\ntitle: "{stem}"\n---\n\nCuerpo.\n', encoding="utf-8")
    cover = read(site.build_site(instance)["out"], "index.html")
    assert '<h3 class="okf-topic">Personas' in cover


def test_graph_json_never_declares_a_type_the_page_does_not_have(instance):
    """`Sin tipo` es una etiqueta de la interfaz, no un valor del frontmatter."""
    page_without_type(instance / "wiki", "operaciones/turnos.md", "Turnos")
    out = site.build_site(instance)["out"]

    graph = json.loads(read(out, site.GRAPH_JSON_PATH))
    assert site.TYPE_UNSET_LABEL not in graph["types"]
    assert graph["types"] == ["Log", "Producto", "Ventas"]
    node = next(n for n in graph["nodes"] if n["id"] == "operaciones/turnos")
    assert node["type"] == "", "el hueco se declara vacío, no relleno"
    assert node["topic"] == "operaciones"

    entry = next(p for p in json.loads(read(out, site.SEARCH_JSON_PATH))["pages"]
                 if p["source"] == "operaciones/turnos.md")
    assert entry["type"] == ""
    assert site.TYPE_UNSET_LABEL not in read(out, site.DATA_JS_PATH)


def test_the_reader_says_out_loud_that_the_page_declares_no_type(instance):
    """Al leerla sí se avisa: el hueco se ve, pero como falta, no como dato."""
    page_without_type(instance / "wiki", "operaciones/turnos.md", "Turnos")
    page = read(site.build_site(instance)["out"], "operaciones/turnos.html")
    assert f"<dt>type</dt><dd>{site.TYPE_UNSET_LABEL}</dd>" in page


def test_cover_links_viz_when_the_bundle_has_it(instance):
    cover = read(site.build_site(instance)["out"], "index.html")
    assert "viz.html" in cover and "genéralo con" in cover
    (instance / "wiki" / "viz.html").write_text("<html></html>", encoding="utf-8")
    cover = read(site.build_site(instance)["out"], "index.html")
    assert 'href="../wiki/viz.html"' in cover


# --------------------------------------------------------------------------- #
# Lector: contenido, procedencia y estado                                      #
# --------------------------------------------------------------------------- #
def test_reader_renders_the_page_body(instance):
    page = read(site.build_site(instance)["out"], PLAN_HTML)
    assert "<h1>Plan de retención 2026</h1>" in page
    assert "Cómo se retiene a las cuentas grandes" in page
    assert "<strong>3%</strong>" in page
    assert "<table>" in page
    assert '<h2 id="palancas">' in page


def test_reader_shows_provenance_and_state(instance):
    page = read(site.build_site(instance)["out"], PLAN_HTML)
    assert "claude-code/opus-5" in page
    assert "2026-08-06T10:12:33Z" in page, "la fecha se muestra en ISO con Z, como el fichero"
    assert "human:ana" in page
    assert "estable" in page and "revisado por una persona" in page
    assert "Informe trimestral Q2 2026" in page
    assert "informe-q2" in page
    assert "ventas/retencion/plan-retencion-2026.md" in page


def test_reader_links_each_source_to_the_real_document(instance):
    page = read(site.build_site(instance)["out"], PLAN_HTML)
    assert 'href="../../../sources/informe-trimestral-q2.pdf"' in page


def test_reader_says_when_a_page_has_no_sources(instance):
    page = read(site.build_site(instance)["out"], "ventas/pipeline/forecast.html")
    assert "no declara" in page and "sources" in page
    assert "borrador" in page


def test_reader_renders_footnotes_as_notes(instance):
    page = read(site.build_site(instance)["out"], PLAN_HTML)
    assert "informe-trimestral-q2.pdf, p.4" in page
    assert '<section class="okf-notes"' in page


def test_reader_breadcrumbs_lead_back_to_the_cover(instance):
    page = read(site.build_site(instance)["out"], PLAN_HTML)
    assert '<a href="../../index.html">Portada</a>' in page
    assert "Ventas" in page and "Retencion" in page


# --------------------------------------------------------------------------- #
# Enlaces cruzados                                                             #
# --------------------------------------------------------------------------- #
def test_relative_md_links_become_html_links(instance):
    page = read(site.build_site(instance)["out"], PLAN_HTML)
    assert f'href="../../{ROADMAP_HTML}"' in page


def test_backlinks_are_listed_on_the_target_page(instance):
    page = read(site.build_site(instance)["out"], ROADMAP_HTML)
    assert "Enlazan aquí" in page
    assert f'href="../../{PLAN_HTML}"' in page


def test_broken_links_are_flagged_not_hidden(instance):
    stats = site.build_site(instance)
    assert stats["broken_links"] == ["../no-existe.md"]
    page = read(stats["out"], PLAN_HTML)
    assert "okf-link-broken" in page
    assert "Enlaces rotos" in page


def test_absolute_links_are_readable_but_draw_no_edge(instance):
    """El visor OKF ignora las rutas absolutas: el grafo del sitio dice lo mismo."""
    stats = site.build_site(instance)
    assert stats["absolute_links"] == ["/ventas/retencion/plan-retencion-2026.md"]
    page = read(stats["out"], ROADMAP_HTML)
    assert "okf-link-absolute" in page
    assert f'href="../../{PLAN_HTML}"' in page
    graph = json.loads(read(stats["out"], site.GRAPH_JSON_PATH))
    assert {"source": "producto/roadmap/roadmap-2026",
            "target": "ventas/retencion/plan-retencion-2026"} not in graph["edges"]


def test_assets_are_linked_where_they_live_not_copied(instance):
    """Una imagen del bundle no se duplica en el sitio: se enlaza donde está."""
    page = instance / "wiki" / "ventas" / "retencion" / "plan-retencion-2026.md"
    page.write_text(page.read_text(encoding="utf-8") + "\n![Curva](curva.png)\n",
                    encoding="utf-8")
    (instance / "wiki" / "ventas" / "retencion" / "curva.png").write_bytes(b"png")
    out = site.build_site(instance)["out"]
    assert 'src="../../../wiki/ventas/retencion/curva.png"' in read(out, PLAN_HTML)
    assert not (out / "ventas" / "retencion" / "curva.png").exists()


def test_an_out_outside_the_instance_keeps_every_link_working(instance, tmp_path):
    out = site.build_site(instance, out=tmp_path / "publicado")["out"]
    page = read(out, PLAN_HTML)
    assert f'href="../../{ROADMAP_HTML}"' in page, "entre páginas, el sitio es autónomo"
    resource = (out / PLAN_HTML).parent
    linked = "../../../mi_wiki/sources/informe-trimestral-q2.pdf"
    assert f'href="{linked}"' in page
    assert (resource / linked).resolve() == instance / "sources" / "informe-trimestral-q2.pdf"


def test_isolated_pages_are_reported(instance):
    stats = site.build_site(instance)
    assert "ventas/pipeline/forecast.md" in stats["isolated"]
    assert "log.md" in stats["isolated"]
    page = read(stats["out"], "ventas/pipeline/forecast.html")
    assert "Página aislada" in page


# --------------------------------------------------------------------------- #
# Datos: grafo y búsqueda                                                      #
# --------------------------------------------------------------------------- #
def test_graph_json_uses_the_same_node_ids_as_the_viewer(instance):
    graph = json.loads(read(site.build_site(instance)["out"], site.GRAPH_JSON_PATH))
    ids = {node["id"] for node in graph["nodes"]}
    assert "ventas/retencion/plan-retencion-2026" in ids
    assert not any(i.endswith(".md") for i in ids)
    assert graph["edges"] == [{"source": "ventas/retencion/plan-retencion-2026",
                               "target": "producto/roadmap/roadmap-2026"}]
    assert graph["types"] == ["Log", "Producto", "Ventas"]
    assert graph["topics"] == ["producto", "ventas"]


def test_graph_nodes_carry_state_provenance_and_a_link_to_the_reader(instance):
    graph = json.loads(read(site.build_site(instance)["out"], site.GRAPH_JSON_PATH))
    plan = next(n for n in graph["nodes"] if n["id"].endswith("plan-retencion-2026"))
    assert plan["href"] == PLAN_HTML
    assert plan["type"] == "Ventas" and plan["topic"] == "ventas"
    assert plan["trust"] == "human-reviewed" and plan["status"] == "stable"
    assert plan["stale_after"] == "2027-01-01"
    assert plan["sources"] == ["informe-q2"]
    assert plan["out_degree"] == 1 and plan["in_degree"] == 0


def test_graph_edge_count_matches_the_okf_viewer(instance):
    """Si los dos grafos discrepasen, el aviso de `verify` dejaría de tener sentido."""
    generator = pytest.importorskip("reference_agent.viewer.generator")
    stats = site.build_site(instance)
    concepts = generator._walk_concepts(instance / "wiki")
    engine_edges = generator._build_graph(concepts)["edges"]
    assert stats["edges"] == len(engine_edges)


def test_search_index_covers_every_page_with_its_text(instance):
    out = site.build_site(instance)["out"]
    payload = json.loads(read(out, site.SEARCH_JSON_PATH))
    entries = {page["path"]: page for page in payload["pages"]}
    assert set(entries) == {PLAN_HTML, ROADMAP_HTML,
                            "ventas/pipeline/forecast.html", "log.html"}
    plan = entries[PLAN_HTML]
    assert plan["title"] == "Plan de retención 2026"
    assert plan["tags"] == ["ventas", "retencion"]
    assert "churn" in plan["text"]
    assert plan["source"] == "ventas/retencion/plan-retencion-2026.md"


def test_browser_data_is_a_classic_script_not_a_json_fetch(instance):
    """Abierto con doble clic (`file://`) un `fetch()` de JSON está bloqueado."""
    out = site.build_site(instance)["out"]
    data = read(out, site.DATA_JS_PATH)
    assert data.startswith("window.OKF_SITE = {")
    payload = json.loads(data[len("window.OKF_SITE = "):].rstrip().rstrip(";"))
    assert {"search", "graph"} == set(payload)
    for rel in (PLAN_HTML, "index.html"):
        assert f'<script src="{"../" * rel.count("/")}assets/data.js" defer>' in read(out, rel)


def test_assets_are_written_next_to_the_pages(instance):
    out = site.build_site(instance)["out"]
    assert read(out, site.CSS_PATH) == site.SITE_CSS
    assert read(out, site.JS_PATH) == site.SITE_JS
    assert f'href="../../{site.CSS_PATH}"' in read(out, PLAN_HTML)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def test_cli_build_reports_what_it_wrote(instance, capsys):
    assert cli.main(["build", str(instance)]) == 0
    out = capsys.readouterr().out
    assert "sitio escrito" in out
    assert "4 páginas" in out
    assert "AVISO" in out and "../no-existe.md" in out
    assert "doble clic" in out


def test_cli_build_json_is_machine_readable(instance, capsys):
    assert cli.main(["build", str(instance), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pages"] == 4
    assert payload["edges"] == 1
    assert payload["broken_links"] == ["../no-existe.md"]
    assert payload["out"].endswith("/site")


def test_cli_build_uses_name_for_the_cover(instance, capsys):
    assert cli.main(["build", str(instance), "--name", "Wiki de Ventas"]) == 0
    capsys.readouterr()
    assert "<h1>Wiki de Ventas</h1>" in read(instance / "site", "index.html")


def test_cli_build_bad_out_exits_with_bad_input(instance, capsys):
    assert cli.main(["build", str(instance), "--out", str(instance / "wiki" / "x")]) == 4
    assert "dentro del bundle" in capsys.readouterr().err


def test_cli_build_bad_path_exits_with_three(tmp_path, capsys):
    assert cli.main(["build", str(tmp_path / "typo")]) == 3
    assert "no existe" in capsys.readouterr().err


def test_cli_build_on_the_bundle_exits_three_with_an_actionable_message(
        instance, tmp_path, capsys):
    destino = tmp_path / "sitio"
    assert cli.main(["build", str(instance / "wiki"), "--out", str(destino)]) == 3
    err = capsys.readouterr().err
    assert "no la instancia" in err
    assert "un nivel más" in err, "dice qué hacer, no solo qué pasa"
    assert "bundle plano" in err, "y que la forma plana sigue valiendo"
    assert "Traceback" not in err
    assert not destino.exists()


def test_cli_build_on_the_bundle_exits_three_not_four_by_default(instance, capsys):
    """Antes salía 4 («la salida está dentro del bundle»), que es otra causa."""
    assert cli.main(["build", str(instance / "wiki")]) == 3
    assert capsys.readouterr().out == ""


def test_cli_build_warns_about_manifest_entries_it_refused_to_follow(
        instance, tmp_path, capsys):
    out = site.build_site(instance)["out"]
    add_manifest_entries(out, "assets/../../../fuera.txt")
    capsys.readouterr()
    assert cli.main(["build", str(instance)]) == 0
    report = capsys.readouterr().out
    assert "no caen dentro" in report and "fuera.txt" in report


def test_cli_build_needs_no_engine(instance, monkeypatch, capsys):
    """Un import del motor aquí sería un fallo: `build` no puede depender de él."""
    def explode(module):
        raise AssertionError(f"build no debe importar el motor ({module})")

    monkeypatch.setattr(cli, "_engine", explode)
    assert cli.main(["build", str(instance)]) == 0
    capsys.readouterr()
