"""M1/M2 — `commit-state` valida su entrada y `init` escribe YAML de verdad.

**M1 — `commit-state` se tragaba cualquier JSON.** Sólo comprobaba que fuera un
objeto, así que `okf-wiki now --json | okf-wiki commit-state w` respondía «manifest
actualizado» sin haber marcado nada (un falso ok en el paso 11 de la skill), y una
entrada sin `path`/`sha256` reventaba con `KeyError`/`TypeError` a mitad del
recorrido. Ahora se valida el payload **entero antes de escribir**, así que ningún
error previsto puede dejar el manifest a medias.

**M2 — `init` interpolaba `--name`/`--by` dentro de comillas puestas a mano.**
`--name 'Wiki "Ventas"'` producía `title: "Registro de Wiki "Ventas""`: frontmatter
ilegible, la página no validaba. Peor todavía, `--by 'a, b'` se colaba en el flow
mapping `generated: { by: a, b, at: ... }`, que YAML lee como `by: a` más una clave
`b: null` inventada — la wiki quedaba firmada por un actor equivocado **sin ningún
error visible**. Ahora el escalar lo serializa PyYAML.

Nada de este fichero necesita el motor OKF.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from okf_wiki import cli, helpers

SRC = Path(__file__).resolve().parents[1] / "src"


def digest(seed: str) -> str:
    """sha256 con la forma que emite `scan` (64 hex en minúscula)."""
    return hashlib.sha256(seed.encode()).hexdigest()


def cli_subprocess(*argv: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Lanza el CLI en un proceso aparte: exit code y stderr de verdad."""
    return subprocess.run(
        [sys.executable, "-m", "okf_wiki.cli", *argv],
        input=stdin, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
    )


@pytest.fixture
def instance(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--no-git"]) == 0
    capsys.readouterr()
    return inst


def state_of(instance: Path) -> str:
    return (instance / helpers.SOURCES_DIRNAME / helpers.STATE_FILENAME).read_text(
        encoding="utf-8"
    )


def commit(instance: Path, payload: str, monkeypatch) -> int:
    """Ejecuta `commit-state` con `payload` en stdin."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(payload))
    return cli.main(["commit-state", str(instance)])


VALID_ENTRY = {"path": "informe.pdf", "sha256": digest("informe"), "size": 12, "ext": ".pdf"}
VALID_PAYLOAD = {"new": [VALID_ENTRY], "changed": [], "unchanged": [], "deleted": []}


# --------------------------------------------------------------------------- #
# M1 — el hallazgo literal: la salida de `now --json` no es un scan            #
# --------------------------------------------------------------------------- #
def test_commit_state_rejects_the_output_of_now_json(instance, capsys, monkeypatch):
    """`now --json` no trae ninguna categoría: aceptarlo era un falso ok."""
    cli.main(["now", "--json"])
    now_json = capsys.readouterr().out
    assert json.loads(now_json).keys() == {"at", "date", "generated_by", "okf_version"}

    antes = state_of(instance)
    assert commit(instance, now_json, monkeypatch) == cli.EXIT_BAD_INPUT
    captured = capsys.readouterr()
    assert captured.out == "", "no puede decir «manifest actualizado»"
    assert "Traceback" not in captured.err
    assert "now --json" in captured.err, "el mensaje debe nombrar el error cometido"
    assert "okf-wiki scan" in captured.err
    assert state_of(instance) == antes


def test_commit_state_rejects_now_json_end_to_end_in_real_processes(instance):
    """La tubería equivocada tal cual la teclearía una persona."""
    now = cli_subprocess("now", "--json")
    assert now.returncode == 0
    done = cli_subprocess("commit-state", str(instance), stdin=now.stdout)
    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr


# --------------------------------------------------------------------------- #
# M1 — estructura superior, listas y entradas: ni KeyError ni TypeError         #
# --------------------------------------------------------------------------- #
# Cada caso es un payload que antes reventaba con traceback o pasaba en silencio.
BAD_PAYLOADS = {
    "no es un objeto": "[1, 2, 3]",
    "objeto vacío": "{}",
    "sólo claves ajenas": '{"total": 3}',
    "clave con typo": '{"changes": []}',
    "categoría que no es lista": '{"new": "informe.pdf"}',
    "categoría con número": '{"new": [1, 2]}',
    "entrada que es texto": '{"new": ["informe.pdf"]}',
    "entrada sin sha256": '{"new": [{"path": "informe.pdf"}]}',
    "entrada sin path": '{"new": [{"sha256": "%s"}]}' % digest("x"),
    "path vacío": '{"new": [{"path": "   ", "sha256": "%s"}]}' % digest("x"),
    "path que no es texto": '{"new": [{"path": 7, "sha256": "%s"}]}' % digest("x"),
    "sha256 nulo": '{"new": [{"path": "a.pdf", "sha256": null}]}',
    "sha256 numérico": '{"new": [{"path": "a.pdf", "sha256": 12345}]}',
    "sha256 truncado": '{"new": [{"path": "a.pdf", "sha256": "aaa"}]}',
    "sha256 en mayúsculas": '{"new": [{"path": "a.pdf", "sha256": "%s"}]}' % digest("x").upper(),
    "size que es texto": '{"new": [{"path": "a.pdf", "sha256": "%s", "size": "12"}]}' % digest("x"),
    "ext que es número": '{"new": [{"path": "a.pdf", "sha256": "%s", "ext": 4}]}' % digest("x"),
    "deleted sin path": '{"deleted": [{}]}',
    "deleted que no es lista": '{"deleted": {}}',
    "concepts que no es objeto": '{"new": [], "concepts": []}',
    "concepts con valor suelto": '{"new": [], "concepts": {"a.pdf": "tema.md"}}',
    "concepts con página no textual": '{"new": [], "concepts": {"a.pdf": [7]}}',
}


@pytest.mark.parametrize("caso", sorted(BAD_PAYLOADS), ids=sorted(BAD_PAYLOADS))
def test_bad_payload_exits_4_without_touching_the_manifest(caso, instance, capsys, monkeypatch):
    antes = state_of(instance)
    assert commit(instance, BAD_PAYLOADS[caso], monkeypatch) == cli.EXIT_BAD_INPUT
    captured = capsys.readouterr()
    assert captured.out == "", "un error no puede anunciar «manifest actualizado»"
    # Ni traceback ni las excepciones que salían crudas antes.
    assert "Traceback" not in captured.err
    assert "KeyError" not in captured.err
    assert "TypeError" not in captured.err
    assert "El manifest NO se ha modificado" in captured.err
    # Y de verdad no se ha modificado.
    assert state_of(instance) == antes


@pytest.mark.parametrize("caso", sorted(BAD_PAYLOADS), ids=sorted(BAD_PAYLOADS))
def test_bad_payload_never_reaches_the_terminal_as_a_traceback(caso, instance):
    done = cli_subprocess("commit-state", str(instance), stdin=BAD_PAYLOADS[caso])
    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr


def test_invalid_json_still_exits_4_and_points_at_the_pipe(instance, capsys, monkeypatch):
    antes = state_of(instance)
    assert commit(instance, "no soy json", monkeypatch) == cli.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "okf-wiki scan" in err
    assert state_of(instance) == antes


def test_the_error_lists_every_problem_at_once(instance, capsys, monkeypatch):
    """Un payload con varios fallos los reporta todos: una pasada, no un juego."""
    payload = json.dumps({
        "new": [{"path": "a.pdf", "sha256": "corto", "size": "12"}],
        "deleted": [{}],
        "sobra": 1,
    })
    assert commit(instance, payload, monkeypatch) == cli.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    for esperado in ("sobra", "sha256", "size", "deleted[0]"):
        assert esperado in err, f"falta {esperado!r} en:\n{err}"


# --------------------------------------------------------------------------- #
# M1 — lo que SÍ tiene que aceptar                                             #
# --------------------------------------------------------------------------- #
def test_a_real_scan_round_trips_and_is_idempotent(instance, capsys):
    """El ciclo real de la skill: `scan | commit-state` y una 2ª pasada limpia."""
    (instance / helpers.SOURCES_DIRNAME / "informe.txt").write_text("hola\n", encoding="utf-8")
    assert cli.main(["scan", str(instance)]) == 0
    scan_json = capsys.readouterr().out
    assert json.loads(scan_json)["new"], "el scan debe ver la fuente nueva"

    done = cli_subprocess("commit-state", str(instance), stdin=scan_json)
    assert done.returncode == 0, done.stderr
    assert "manifest actualizado" in done.stdout

    assert cli.main(["scan", str(instance)]) == 0
    segunda = json.loads(capsys.readouterr().out)
    assert segunda["new"] == [] and segunda["changed"] == []
    assert [e["path"] for e in segunda["unchanged"]] == ["informe.txt"]


def test_a_partial_payload_is_accepted(instance, capsys, monkeypatch):
    """La skill puede commitear sólo lo que ha ingerido: basta una categoría."""
    assert commit(instance, json.dumps({"new": [VALID_ENTRY]}), monkeypatch) == 0
    assert "manifest actualizado" in capsys.readouterr().out
    assert helpers.load_state(instance)["files"]["informe.pdf"]["sha256"] == VALID_ENTRY["sha256"]


def test_all_empty_categories_are_accepted(instance, capsys, monkeypatch):
    """«Nada nuevo que ingerir» es un commit legítimo, no un error."""
    assert commit(instance, json.dumps(
        {"new": [], "changed": [], "unchanged": [], "deleted": []}), monkeypatch) == 0
    capsys.readouterr()


def test_concepts_and_deleted_still_work(instance, capsys, monkeypatch):
    payload = json.dumps({"new": [VALID_ENTRY], "concepts": {"informe.pdf": ["ventas/plan.md"]}})
    assert commit(instance, payload, monkeypatch) == 0
    assert helpers.load_state(instance)["files"]["informe.pdf"]["concepts"] == ["ventas/plan.md"]
    assert commit(instance, json.dumps({"deleted": [{"path": "informe.pdf"}]}), monkeypatch) == 0
    capsys.readouterr()
    assert helpers.load_state(instance)["files"] == {}


def test_validate_scan_payload_returns_the_payload():
    assert helpers.validate_scan_payload(VALID_PAYLOAD) is VALID_PAYLOAD


def test_invalid_scan_payload_is_not_an_ingest_error():
    """Es entrada del usuario (exit 4), no un problema de la instancia (exit 3)."""
    assert not issubclass(helpers.InvalidScanPayload, helpers.IngestError)


# --------------------------------------------------------------------------- #
# M3 — `commit-state` no inventa la instancia que le falta                     #
# --------------------------------------------------------------------------- #
# `commit_state` creaba la dropzone con `mkdir(parents=True, exist_ok=True)`, así que
# `commit-state /ruta/con/typo` respondía «manifest actualizado» con exit 0 tras
# fabricar `<typo>/sources/.ingest-state.json`. Y el daño no acababa ahí: la ruta
# falsa quedaba lo bastante formada como para que el `scan` siguiente también pasase
# —devolviendo las fuentes reales como `deleted`— mientras el manifest de la
# instancia de verdad seguía sin marcarse. Ahora las dos órdenes comparten el mismo
# guardián (`resolve_dropzone`) y ninguna crea nada al fallar.


def tree_of(root: Path) -> set[str]:
    """Todo lo que cuelga de `root`: sirve para probar que un fallo no crea nada."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def _missing(root: Path) -> Path:
    return root / "mi_wik"  # typo de `mi_wiki`: nunca se creó


def _a_file(root: Path) -> Path:
    p = root / "notas.txt"
    p.write_text("no soy una instancia\n", encoding="utf-8")
    return p


def _dir_without_dropzone(root: Path) -> Path:
    p = root / "a_medias"
    p.mkdir()
    return p


def _dropzone_is_a_file(root: Path) -> Path:
    p = root / "rara"
    p.mkdir()
    (p / helpers.SOURCES_DIRNAME).write_text("tampoco soy una dropzone\n", encoding="utf-8")
    return p


UNUSABLE = {
    "ruta inexistente": _missing,
    "ruta que es un fichero": _a_file,
    "instancia sin sources/": _dir_without_dropzone,
    "sources/ que es un fichero": _dropzone_is_a_file,
}
UNUSABLE_IDS = sorted(UNUSABLE)


@pytest.mark.parametrize("caso", UNUSABLE_IDS, ids=UNUSABLE_IDS)
def test_commit_state_on_an_unusable_path_exits_3_and_creates_nothing(
    caso, tmp_path, capsys, monkeypatch
):
    target = UNUSABLE[caso](tmp_path)
    antes = tree_of(tmp_path)

    assert commit(target, json.dumps(VALID_PAYLOAD), monkeypatch) == cli.EXIT_INGEST

    captured = capsys.readouterr()
    assert captured.out == "", "no puede decir «manifest actualizado» sobre una ruta mala"
    assert "Traceback" not in captured.err
    assert "NotADirectoryError" not in captured.err  # antes salía crudo con exit 1
    assert str(target) in captured.err, "el mensaje debe nombrar la ruta"
    assert "okf-wiki init" in captured.err, "y decir cómo crear la instancia"
    # Nada nuevo en disco, y en particular ningún manifest inventado.
    assert tree_of(tmp_path) == antes
    assert list(tmp_path.rglob(helpers.STATE_FILENAME)) == []


@pytest.mark.parametrize("caso", UNUSABLE_IDS, ids=UNUSABLE_IDS)
def test_commit_state_on_an_unusable_path_stays_clean_in_a_real_process(caso, tmp_path):
    """El exit code y el stderr de verdad, no los de `cli.main` en proceso."""
    target = UNUSABLE[caso](tmp_path)
    antes = tree_of(tmp_path)

    done = cli_subprocess("commit-state", str(target), stdin=json.dumps(VALID_PAYLOAD))

    assert done.returncode == cli.EXIT_INGEST, done.stderr
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert tree_of(tmp_path) == antes


@pytest.mark.parametrize("caso", UNUSABLE_IDS, ids=UNUSABLE_IDS)
def test_scan_and_commit_state_reject_exactly_the_same_paths(caso, tmp_path):
    """Contrato compartido: ninguna ruta puede valer para uno y no para el otro."""
    target = UNUSABLE[caso](tmp_path)
    escaneo = cli_subprocess("scan", str(target))
    commiteo = cli_subprocess("commit-state", str(target), stdin=json.dumps(VALID_PAYLOAD))
    assert escaneo.returncode == cli.EXIT_INGEST == commiteo.returncode
    assert escaneo.stdout == "" and commiteo.stdout == ""


def test_the_reported_chain_commit_state_then_scan_both_keep_failing(tmp_path):
    """La cadena tal cual se reportó, en procesos de verdad y sobre la misma ruta."""
    ghost = tmp_path / "mi_wik"

    primero = cli_subprocess("commit-state", str(ghost), stdin=json.dumps(VALID_PAYLOAD))
    assert primero.returncode == cli.EXIT_INGEST, primero.stderr
    assert primero.stdout == ""
    assert not ghost.exists(), "el fallo no puede dejar el árbol creado"

    # Y el `scan` siguiente sigue fallando en vez de heredar una instancia inventada.
    segundo = cli_subprocess("scan", str(ghost))
    assert segundo.returncode == cli.EXIT_INGEST, segundo.stderr
    assert segundo.stdout == "", "un JSON vacío se leería como «nada nuevo que ingerir»"
    assert "Traceback" not in segundo.stderr
    assert not ghost.exists()
    assert tree_of(tmp_path) == set()


def test_a_bad_path_wins_over_a_bad_payload(tmp_path, capsys, monkeypatch):
    """Con las dos cosas mal manda la ruta: arreglar el JSON no habría servido."""
    ghost = tmp_path / "mi_wik"
    assert commit(ghost, '{"total": 3}', monkeypatch) == cli.EXIT_INGEST
    err = capsys.readouterr().err
    assert str(ghost) in err
    assert not ghost.exists()


def test_a_failed_commit_elsewhere_leaves_the_real_manifest_intact(
    instance, capsys, monkeypatch
):
    """No mutación: el typo no puede tocar el manifest de la instancia buena."""
    assert commit(instance, json.dumps(VALID_PAYLOAD), monkeypatch) == 0
    capsys.readouterr()
    antes = state_of(instance)

    ghost = instance.parent / "mi_wik"
    assert commit(ghost, json.dumps({"deleted": [{"path": "informe.pdf"}]}), monkeypatch) \
        == cli.EXIT_INGEST
    capsys.readouterr()

    assert state_of(instance) == antes
    assert helpers.load_state(instance)["files"]["informe.pdf"]["sha256"] == \
        VALID_ENTRY["sha256"]
    assert not ghost.exists()


@pytest.mark.parametrize("caso", UNUSABLE_IDS, ids=UNUSABLE_IDS)
def test_helpers_commit_state_raises_missing_sources_without_writing(caso, tmp_path):
    """El helper es el que sostiene el contrato: el CLI sólo lo traduce a exit 3."""
    target = UNUSABLE[caso](tmp_path)
    antes = tree_of(tmp_path)
    with pytest.raises(helpers.MissingSourcesError):
        helpers.commit_state(target, VALID_PAYLOAD)
    assert tree_of(tmp_path) == antes


def test_missing_sources_error_maps_to_exit_3():
    """Es un problema de la instancia (3), no de la entrada de stdin (4)."""
    assert issubclass(helpers.MissingSourcesError, helpers.IngestError)
    assert helpers.MissingSourcesError in cli._INGEST_ACTIONS


def test_resolve_dropzone_returns_the_dropzone_of_a_good_instance(instance):
    assert helpers.resolve_dropzone(instance) == instance / helpers.SOURCES_DIRNAME


# --------------------------------------------------------------------------- #
# M2 — `--name` y `--by` hostiles: el frontmatter tiene que seguir siendo YAML  #
# --------------------------------------------------------------------------- #
HOSTILE = [
    'Wiki "Ventas"',                 # comillas: rompían el frontmatter entero
    "Ventas: 2026",                  # dos puntos
    "a, b",                          # coma: inventaba una clave en el flow mapping
    "back\\slash",                   # barra invertida: escape de YAML
    "corchetes [x] y llaves {y}",    # sintaxis de flow collection
    "acaba en dos puntos:",
    "# parece un comentario",
    "- parece una lista",
    "&ancla *alias !tag",            # indicadores de YAML
    "'comillas simples'",
    "  espacios  al  borde  ",
    "salto\nde línea",
    "ñandú — 2026 · 100%",
    "null",                          # escalares que YAML resolvería a otro tipo
    "true",
    "12345",
]


def frontmatter_of(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} no empieza por frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


@pytest.mark.parametrize("name", HOSTILE, ids=range(len(HOSTILE)))
def test_init_name_round_trips_through_the_frontmatter(name, tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--name", name, "--no-git"]) == 0
    capsys.readouterr()
    fm = frontmatter_of(inst / "wiki" / "log.md")   # parsea → ya no es YAML roto
    assert fm["title"] == f"Registro de {name}"
    assert name in fm["description"]
    assert isinstance(fm["title"], str), "el título no puede resolverse a otro tipo"


@pytest.mark.parametrize("by", HOSTILE, ids=range(len(HOSTILE)))
def test_init_by_round_trips_inside_the_generated_mapping(by, tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--by", by, "--no-git"]) == 0
    capsys.readouterr()
    fm = frontmatter_of(inst / "wiki" / "log.md")
    assert fm["generated"]["by"] == by
    # Lo que hacía la coma: colar claves nuevas en `generated`.
    assert set(fm["generated"]) == {"by", "at"}, fm["generated"]


def test_init_with_a_comma_in_by_does_not_invent_keys(tmp_path, capsys):
    """El fallo silencioso: `--by 'a, b'` firmaba la wiki con `by: a` y añadía `b: null`."""
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--by", "a, b", "--no-git"]) == 0
    capsys.readouterr()
    generated = frontmatter_of(inst / "wiki" / "log.md")["generated"]
    assert generated["by"] == "a, b"
    assert "b" not in generated


def test_init_log_stub_stays_lintable_with_a_hostile_name(tmp_path, capsys):
    """El lint v0.2 tiene que seguir viendo un `generated` bien formado."""
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--name", 'X: "y", z', "--by", "p, q", "--no-git"]) == 0
    capsys.readouterr()
    doc = (inst / "wiki" / "log.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(doc.split("---\n", 2)[1])
    assert cli._lint_document(fm, doc.split("---\n", 2)[2]) == []


def test_generated_at_is_a_quoted_iso_string(tmp_path, capsys):
    """`at` va entre comillas: el spec pide una cadena ISO 8601, no un timestamp YAML."""
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--no-git"]) == 0
    capsys.readouterr()
    at = frontmatter_of(inst / "wiki" / "log.md")["generated"]["at"]
    assert isinstance(at, str), f"`at` se resolvió a {type(at).__name__}"
    assert at.endswith("Z")


def test_index_stub_link_survives_brackets(tmp_path, capsys):
    """Los corchetes del nombre cerrarían el `[texto]` del enlace a `log.md`."""
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--name", "Wiki [beta]", "--no-git"]) == 0
    capsys.readouterr()
    linea = next(l for l in (inst / "wiki" / "index.md").read_text(encoding="utf-8").splitlines()
                 if "log.md" in l)
    assert linea.endswith("(log.md) - Bitácora append-only de ingestas y cambios "
                          r"de la wiki Wiki \[beta\].")
    assert r"\[beta\]" in linea


def test_a_hostile_name_survives_a_real_init_subprocess(tmp_path):
    inst = tmp_path / "mi_wiki"
    done = cli_subprocess("init", str(inst), "--no-git", "--name", 'Wiki "Ventas": a, b')
    assert done.returncode == 0, done.stderr
    fm = frontmatter_of(inst / "wiki" / "log.md")
    assert fm["title"] == 'Registro de Wiki "Ventas": a, b'


# --------------------------------------------------------------------------- #
# M2 — el serializador, por su cuenta                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", HOSTILE, ids=range(len(HOSTILE)))
def test_yaml_scalar_round_trips_in_block_and_flow_context(value):
    quoted = cli.yaml_scalar(value)
    assert "\n" not in quoted, "tiene que caber en una línea para el flow mapping"
    assert yaml.safe_load(f"k: {quoted}\n")["k"] == value
    assert yaml.safe_load(f"g: {{ by: {quoted}, at: z }}\n")["g"]["by"] == value


def test_yaml_scalar_does_not_fold_long_values():
    largo = "x" * 500
    assert yaml.safe_load(f"k: {cli.yaml_scalar(largo)}\n")["k"] == largo


def test_md_link_text_escapes_brackets_and_flattens_newlines():
    assert cli.md_link_text("a [b] c") == r"a \[b\] c"
    assert cli.md_link_text("a\nb") == "a b"
