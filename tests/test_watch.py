"""`okf-wiki watch`: reconstruir al vuelo sin cruzar la línea de la aprobación.

El bucle se prueba entero pero sin esperar: `sleep` y `log` se inyectan y
`cycles` acota los sondeos. Lo que más se comprueba aquí no es que reconstruya
—eso es una línea— sino lo que **no** hace: no sintetiza, no toca `wiki/` ni
`sources/`, y no se cae cuando el bundle queda a medias.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from okf_wiki import cli, helpers, site, watch as watch_mod

PLAN = Path("wiki/ventas/retencion/plan-retencion-2026.md")


class Loop:
    """`sleep` de mentira que va aplicando cambios entre sondeo y sondeo."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.slept = 0

    def __call__(self, _seconds: float) -> None:
        self.slept += 1
        if self.steps:
            step = self.steps.pop(0)
            if step is not None:
                step()


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def run(instance, steps=(), cycles=None, **kwargs):
    lines: list[str] = []
    loop = Loop(*steps)
    result = watch_mod.watch(
        instance, interval=0, cycles=len(steps) if cycles is None else cycles,
        sleep=loop, log=lines.append, **kwargs
    )
    return result, lines


# --------------------------------------------------------------------------- #
# Construye una vez y luego solo cuando hace falta                             #
# --------------------------------------------------------------------------- #
def test_watch_builds_once_before_watching(instance):
    result, lines = run(instance, cycles=0)
    assert result["builds"] == 1 and result["polls"] == 0
    assert (instance / "site" / "index.html").is_file()
    assert "sitio construido" in lines[0]
    assert "no sintetiza" in lines[1] and "no commitea" in lines[1]


def test_watch_rebuilds_when_a_page_changes(instance):
    def edit():
        page = instance / PLAN
        page.write_text(page.read_text(encoding="utf-8") + "\nUna frase nueva.\n",
                        encoding="utf-8")

    result, lines = run(instance, steps=[edit, None])
    assert result["builds"] == 2, "una reconstrucción, no una por sondeo"
    assert any("reconstruido" in line for line in lines)
    assert "Una frase nueva." in (
        instance / "site" / "ventas/retencion/plan-retencion-2026.html"
    ).read_text(encoding="utf-8")


def test_watch_does_not_rebuild_when_nothing_changes(instance):
    result, lines = run(instance, steps=[None, None, None])
    assert result["builds"] == 1
    assert not any("reconstruido" in line for line in lines)


def test_watch_notices_a_new_page_and_a_deleted_one(instance):
    def add():
        (instance / "wiki" / "ventas" / "nueva.md").write_text(
            '---\ntype: Ventas\ntitle: "Nueva"\ndescription: "d"\n---\n\ncuerpo\n',
            encoding="utf-8")

    def remove():
        (instance / "wiki" / "ventas" / "nueva.md").unlink()

    result, lines = run(instance, steps=[add, remove, None])
    assert result["builds"] == 3
    assert any("nuevo:" in line for line in lines)
    assert any("borrado:" in line for line in lines)
    assert not (instance / "site" / "ventas" / "nueva.html").exists()


def test_watch_rebuilds_when_the_purpose_changes(instance):
    """El propósito sale en la portada: editarlo tiene que verse."""
    def edit():
        (instance / "purpose.md").write_text(
            "# Propósito\n\n## Objetivo\n\nOtro objetivo distinto.\n", encoding="utf-8")

    result, _lines = run(instance, steps=[edit, None])
    assert result["builds"] == 2
    assert "Otro objetivo distinto." in (
        instance / "site" / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# La línea que no cruza: no ingiere, no toca el bundle, no commitea            #
# --------------------------------------------------------------------------- #
def test_watch_reports_new_sources_but_never_ingests_them(instance):
    def drop_document():
        (instance / "sources" / "nuevo-informe.pdf").write_text("pdf", encoding="utf-8")

    before_pages = snapshot(instance / "wiki")
    result, lines = run(instance, steps=[drop_document, None])
    notice = next(line for line in lines if "sources/" in line)
    assert "okf-ingest" in notice and "aprobar una persona" in notice
    assert "nuevo: nuevo-informe.pdf" in notice
    assert result["builds"] == 1, "una fuente nueva no reconstruye: no cambia el bundle"
    assert snapshot(instance / "wiki") == before_pages, "watch no escribe páginas"


def test_watch_leaves_the_bundle_and_the_dropzone_untouched(instance):
    def edit():
        page = instance / PLAN
        page.write_text(page.read_text(encoding="utf-8") + "\nmás texto\n",
                        encoding="utf-8")

    before_sources = snapshot(instance / "sources")
    run(instance, steps=[edit, None])
    assert snapshot(instance / "sources") == before_sources
    assert not (instance / "wiki" / "site").exists()
    assert not (instance / "wiki" / ".okf-site.json").exists()


def test_watch_says_when_it_refuses_a_manifest_entry_outside_the_output(instance, tmp_path):
    """En bucle, rechazar en silencio sería rechazar sin que nadie se entere."""
    outsider = tmp_path / "no-me-toques.txt"
    outsider.write_text("un fichero de fuera", encoding="utf-8")

    def tamper():
        manifest = instance / "site" / site.MANIFEST_FILENAME
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["files"] = [*data["files"], f"assets/../../../{outsider.name}"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        page = instance / PLAN
        page.write_text(page.read_text(encoding="utf-8") + "\nuna línea más\n",
                        encoding="utf-8")

    _result, lines = run(instance, steps=[tamper, None])
    assert any("entradas del manifest fuera de la salida" in line for line in lines)
    assert outsider.is_file()


#: Las mismas dos entradas imposibles que prueba `build` (ver `test_site_build`):
#: con ellas `realpath` lanza en vez de devolver una ruta. Aquí lo que se protege
#: es el bucle: `watch` no las cazaba ni en la construcción inicial (fuera del
#: `try`) ni en las siguientes (`except` solo mira `IngestError`/`OSError`).
UNNAMEABLE_ENTRIES = ["nulo" + chr(0) + ".html", "sur" + chr(0xD800) + "ate.html"]


def tamper_manifest(out: Path, *entries: str) -> None:
    """Mete entradas en el manifest del sitio, como si viniera así del disco."""
    manifest = out / site.MANIFEST_FILENAME
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"] = [*data["files"], *entries]
    # Escapado: un surrogate suelto no se puede escribir en UTF-8, y así el
    # fichero sigue siendo el JSON que `watch` va a leer.
    manifest.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")


def test_watch_keeps_watching_when_the_manifest_has_an_unnameable_entry(instance):
    """El vigilante no se cae por una entrada que el disco no puede ni nombrar."""
    out = instance / "site"
    site.build_site(instance)
    tamper_manifest(out, *UNNAMEABLE_ENTRIES)
    before = snapshot(out)

    def edit():
        page = instance / PLAN
        page.write_text(page.read_text(encoding="utf-8") + "\nuna línea más\n",
                        encoding="utf-8")

    result, lines = run(instance, steps=[edit, None])

    assert result["builds"] == 2, "la construcción inicial y la del cambio"
    assert any("entradas del manifest fuera de la salida" in line for line in lines)
    assert set(before) <= set(snapshot(out)), "no ha borrado nada del sitio"
    assert (out / "index.html").is_file()


def test_watch_survives_a_manifest_tampered_with_between_two_polls(instance):
    """Y tampoco si la entrada aparece con el bucle ya en marcha."""
    def tamper():
        tamper_manifest(instance / "site", *UNNAMEABLE_ENTRIES)
        page = instance / PLAN
        page.write_text(page.read_text(encoding="utf-8") + "\notra línea\n",
                        encoding="utf-8")

    result, lines = run(instance, steps=[tamper, None])

    assert result["builds"] == 2
    assert any("entradas del manifest fuera de la salida" in line for line in lines)
    assert (instance / "site" / "index.html").is_file()


def test_watch_survives_a_bundle_that_momentarily_has_no_pages(instance):
    """Un `git checkout` a medias no puede tumbar al vigilante."""
    saved: dict[Path, str] = {}

    def empty_it():
        for md in (instance / "wiki").rglob("*.md"):
            saved[md] = md.read_text(encoding="utf-8")
            md.unlink()

    def restore():
        for md, text in saved.items():
            md.write_text(text, encoding="utf-8")

    result, lines = run(instance, steps=[empty_it, restore, None])
    assert any("error al reconstruir" in line for line in lines)
    assert result["builds"] == 2, "se recupera sola en cuanto vuelven las páginas"


def test_watch_refuses_an_output_inside_the_bundle(instance):
    with pytest.raises(site.SiteError):
        run(instance, cycles=0, out=instance / "wiki" / "site")


def test_watch_refuses_the_bundle_passed_as_the_instance(instance, tmp_path):
    """Vigilar `wiki/` mantenía al día un sitio sin propósito y sin fuentes."""
    destino = tmp_path / "sitio"
    with pytest.raises(helpers.BundleAsInstanceError):
        run(instance / "wiki", cycles=0, out=destino)
    assert not destino.exists(), "ni un sondeo ni un fichero antes de rechazar"


# --------------------------------------------------------------------------- #
# Huellas                                                                      #
# --------------------------------------------------------------------------- #
def test_fingerprint_ignores_the_dropzone_and_tracks_viz(instance):
    bundle = instance / "wiki"
    fingerprint = watch_mod.bundle_fingerprint(instance, bundle)
    assert any(key.startswith("wiki:ventas/") for key in fingerprint)
    assert not any("sources" in key for key in fingerprint)
    assert fingerprint["viz"] == "0"
    (bundle / "viz.html").write_text("<html></html>", encoding="utf-8")
    assert watch_mod.bundle_fingerprint(instance, bundle)["viz"] == "1"


def test_fingerprint_follows_content_not_mtime(instance):
    bundle = instance / "wiki"
    before = watch_mod.bundle_fingerprint(instance, bundle)
    page = instance / PLAN
    page.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")  # re-guardado
    assert watch_mod.bundle_fingerprint(instance, bundle) == before


def test_sources_fingerprint_skips_the_manifest(instance):
    (instance / "sources" / ".ingest-state.json").write_text("{}", encoding="utf-8")
    fingerprint = watch_mod.sources_fingerprint(instance)
    assert list(fingerprint) == ["informe-trimestral-q2.pdf"]


def test_describe_summarizes_a_change():
    assert watch_mod.describe({"a": "1"}, {"a": "2"}) == "modificado: a"
    assert watch_mod.describe({}, {"b": "1"}) == "nuevo: b"
    assert watch_mod.describe({"c": "1"}, {}) == "borrado: c"
    assert watch_mod.describe({"a": "1"}, {"a": "1"}) == "sin cambios"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def test_cli_watch_with_zero_cycles_builds_and_exits(instance, capsys):
    assert cli.main(["watch", str(instance), "--cycles", "0"]) == 0
    out = capsys.readouterr().out
    assert "sitio construido" in out
    assert (instance / "site" / "index.html").is_file()


def test_cli_watch_bad_out_exits_with_bad_input(instance, capsys):
    assert cli.main(["watch", str(instance), "--cycles", "0",
                     "--out", str(instance / "wiki" / "x")]) == 4
    assert "dentro del bundle" in capsys.readouterr().err


def test_cli_watch_on_the_bundle_exits_three_without_writing(instance, tmp_path, capsys):
    destino = tmp_path / "sitio"
    assert cli.main(["watch", str(instance / "wiki"), "--cycles", "0",
                     "--out", str(destino)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no la instancia" in captured.err and "un nivel más" in captured.err
    assert not destino.exists()


def test_cli_watch_stops_cleanly_on_ctrl_c(instance, monkeypatch, capsys):
    monkeypatch.setattr(watch_mod, "watch",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    assert cli.main(["watch", str(instance)]) == 0
    assert "watch detenido" in capsys.readouterr().out
