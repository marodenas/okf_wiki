"""A1/A2 — `verify`, `index` y `viz` no pueden dar por bueno un bundle inexistente.

El fallo que cubren estas pruebas era silencioso y peligroso: `wiki_root` caía al
propio directorio cuando no había `wiki/`, así que una ruta equivocada se trataba
como un bundle vacío y

- `verify /no/existe` imprimía `{"checked": 0, "errors": [], "ok": true}` con exit 0,
- `index /no/existe` decía «index.md regenerados: 0» con exit 0,
- `viz /no/existe` reventaba con el `FileNotFoundError` del motor y traceback entero,
  y `viz <carpeta-vacía>` escribía un `viz.html` de cero nodos.

Un typo en la ruta se leía, por tanto, como «la wiki está impecable». Ahora las tres
salen con `EXIT_INGEST` (3) y un mensaje que dice qué mirar.

Se ejecuta el CLI de dos formas a propósito: en proceso (`cli.main`, para inspeccionar
stdout/stderr) y en un subproceso real (para comprobar el código de salida que ve la
skill y que no se escapa ningún traceback).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("reference_agent", reason="motor OKF no instalado: bash scripts/setup.sh")

from okf_wiki import cli, helpers  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"

# Los tres comandos que consumen el bundle: el guard tiene que valer para todos.
ENGINE_COMMANDS = ["verify", "index", "viz"]


def cli_subprocess(*argv: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Lanza el CLI en un proceso aparte: exit code y stderr de verdad."""
    return subprocess.run(
        [sys.executable, "-m", "okf_wiki.cli", *argv],
        input=stdin, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
    )


def page(path: Path, *, title: str = "Plan") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\ntype: Ventas\ntitle: "{title}"\ndescription: "Una página."\n'
        f'tags: [a, b]\nstatus: stable\n'
        f'generated: {{ by: "okf-wiki/test", at: "2026-08-06T10:00:00Z" }}\n'
        f'---\n\nCuerpo.\n',
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# La instancia no existe                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd", ENGINE_COMMANDS)
def test_missing_instance_exits_3_without_output(cmd, tmp_path, capsys):
    assert cli.main([cmd, str(tmp_path / "no-existe")]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    # Nada en stdout: un JSON `ok: true` o un «regenerados: 0» se leen como éxito.
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "no-existe" in captured.err
    assert "okf-wiki init" in captured.err


@pytest.mark.parametrize("cmd", ENGINE_COMMANDS)
def test_missing_instance_exits_3_in_a_real_process(cmd, tmp_path):
    done = cli_subprocess(cmd, str(tmp_path / "no-existe"))
    assert done.returncode == cli.EXIT_INGEST, done.stderr
    assert done.stdout == ""
    assert "Traceback" not in done.stderr


def test_verify_never_claims_ok_for_a_path_that_does_not_exist(tmp_path, capsys):
    """El hallazgo, en su forma más literal: no puede salir `ok: true`."""
    code = cli.main(["verify", str(tmp_path / "no-existe")])
    out = capsys.readouterr().out
    assert code != cli.EXIT_OK
    assert "ok" not in out and "checked" not in out


def test_index_never_reports_zero_regenerated_for_a_bad_path(tmp_path, capsys):
    code = cli.main(["index", str(tmp_path / "no-existe")])
    out = capsys.readouterr().out
    assert code != cli.EXIT_OK
    assert "regenerados" not in out


# --------------------------------------------------------------------------- #
# La instancia existe pero no hay bundle: falta `wiki/`, o está vacío           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd", ENGINE_COMMANDS)
def test_instance_without_a_wiki_dir_exits_3(cmd, tmp_path, capsys):
    inst = tmp_path / "sin_wiki"
    (inst / helpers.SOURCES_DIRNAME).mkdir(parents=True)
    assert cli.main([cmd, str(inst)]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "wiki/" in captured.err, captured.err


@pytest.mark.parametrize("cmd", ENGINE_COMMANDS)
def test_empty_wiki_dir_exits_3(cmd, tmp_path, capsys):
    """`wiki/` existe pero sin páginas: sigue siendo un falso ok si no se ataja."""
    inst = tmp_path / "wiki_vacia"
    (inst / helpers.WIKI_DIRNAME).mkdir(parents=True)
    assert cli.main([cmd, str(inst)]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_a_dropzone_full_of_markdown_is_not_a_bundle(tmp_path, capsys):
    """`sources/` puede tener .md sueltos; eso no convierte la carpeta en bundle."""
    inst = tmp_path / "solo_dropzone"
    (inst / helpers.SOURCES_DIRNAME).mkdir(parents=True)
    (inst / helpers.SOURCES_DIRNAME / "nota.md").write_text("apunte\n", encoding="utf-8")
    assert cli.main(["verify", str(inst)]) == cli.EXIT_INGEST
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# viz: ni traceback ni grafo fantasma                                          #
# --------------------------------------------------------------------------- #
def test_viz_on_a_missing_bundle_has_no_traceback(tmp_path):
    done = cli_subprocess("viz", str(tmp_path / "no-existe"))
    assert done.returncode == cli.EXIT_INGEST
    assert "Traceback" not in done.stderr
    assert "FileNotFoundError" not in done.stderr
    assert "ERROR:" in done.stderr


def test_viz_does_not_write_a_graph_for_a_directory_without_a_bundle(tmp_path, capsys):
    """Antes escribía un `viz.html` de 0 nodos, indistinguible de una wiki vacía."""
    inst = tmp_path / "vacia"
    inst.mkdir()
    assert cli.main(["viz", str(inst)]) == cli.EXIT_INGEST
    capsys.readouterr()
    assert list(inst.rglob("viz.html")) == []


def test_viz_with_out_inside_a_missing_directory_exits_4(tmp_path, capsys):
    """El destino de `--out` es un argumento del usuario → «entrada ilegible» (4)."""
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--no-git"]) == 0
    capsys.readouterr()
    destino = tmp_path / "no" / "existe" / "viz.html"
    assert cli.main(["viz", str(inst), "--out", str(destino)]) == cli.EXIT_BAD_INPUT
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert str(destino.parent) in captured.err
    assert not destino.exists()


# --------------------------------------------------------------------------- #
# Lo que NO debe cambiar: bundles reales y bundles planos siguen funcionando    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd", ENGINE_COMMANDS)
def test_a_real_instance_still_succeeds(cmd, tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--no-git"]) == 0
    page(inst / helpers.WIKI_DIRNAME / "ventas" / "plan.md")
    assert cli.main([cmd, str(inst)]) == cli.EXIT_OK
    assert capsys.readouterr().out != ""


@pytest.mark.parametrize("cmd", ENGINE_COMMANDS)
def test_a_flat_bundle_without_a_wiki_subdir_still_works(cmd, tmp_path, capsys):
    """Compatibilidad: si la ruta ya es un bundle, se acepta tal cual."""
    flat = tmp_path / "bundle_plano"
    page(flat / "ventas" / "plan.md")
    assert cli.main([cmd, str(flat)]) == cli.EXIT_OK
    assert capsys.readouterr().out != ""


def test_resolve_bundle_prefers_the_wiki_subdir(tmp_path):
    inst = tmp_path / "inst"
    page(inst / helpers.WIKI_DIRNAME / "ventas" / "plan.md")
    assert helpers.resolve_bundle(inst) == inst / helpers.WIKI_DIRNAME


def test_resolve_bundle_rejects_a_file(tmp_path):
    fichero = tmp_path / "no_soy_carpeta.md"
    fichero.write_text("x\n", encoding="utf-8")
    with pytest.raises(helpers.MissingBundleError):
        helpers.resolve_bundle(fichero)


def test_missing_bundle_is_an_ingest_error():
    """Tiene que viajar por el mismo `except` que el resto de errores de ingesta."""
    assert issubclass(helpers.MissingBundleError, helpers.IngestError)
