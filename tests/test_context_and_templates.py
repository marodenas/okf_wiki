"""Contexto de instancia (`purpose.md`/`schema.md`) y plantillas del CLI.

Nada de aquí necesita el motor OKF: las plantillas son texto y `context` sólo lee
ficheros. Lo que estas pruebas fijan es el contrato que consume la skill:

- que el estado "sin rellenar" se decida por un marcador y no por parecido;
- que `init` no pise nunca un `purpose.md` que ya tenga contenido del usuario;
- que la falta de contexto avise pero **no** bloquee (una wiki antigua sigue
  ingiriendo).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from okf_wiki import cli, helpers, templates

SRC = Path(__file__).resolve().parents[1] / "src"


def run(argv: list[str]) -> int:
    return cli.main(argv)


def cli_subprocess(*argv: str) -> subprocess.CompletedProcess:
    """Lanza el CLI en un proceso aparte: el exit code y el stderr que ve la skill."""
    return subprocess.run(
        [sys.executable, "-m", "okf_wiki.cli", *argv],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
    )


@pytest.fixture
def instance(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert run(["init", str(inst), "--name", "Mi Wiki", "--no-git"]) == 0
    capsys.readouterr()
    return inst


def fill(path: Path, body: str = "# Propósito\n\nSintetizar los informes de ventas.\n") -> None:
    """Rellena un fichero de contexto: contenido real y sin la línea del marcador."""
    path.write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Plantillas                                                                   #
# --------------------------------------------------------------------------- #
def test_the_two_context_templates_are_stubs():
    """`purpose` y `schema` los rellena una persona: tienen que traer el marcador."""
    for name in ("purpose", "schema"):
        text = templates.render(name)
        assert text.startswith(templates.STUB_MARKER), name
        assert templates.is_stub(text), name


def test_the_agent_templates_are_not_stubs():
    """`page` y `proposal` los consume el agente: marcarlos frenaría la ingesta."""
    for name in ("page", "proposal"):
        assert not templates.is_stub(templates.render(name)), name


def test_render_substitutes_the_wiki_name():
    text = templates.render("purpose", "Wiki de Ventas")
    assert "Wiki de Ventas" in text
    assert templates._NAME_TOKEN not in text


def test_render_leaves_no_token_behind_in_any_template():
    for name in templates.TEMPLATES:
        assert templates._NAME_TOKEN not in templates.render(name, "X"), name


def test_render_rejects_an_unknown_template_listing_the_valid_ones():
    with pytest.raises(KeyError) as err:
        templates.render("inventada")
    message = err.value.args[0]
    for name in templates.TEMPLATES:
        assert name in message


def test_is_stub_only_looks_at_the_first_line():
    """Un fichero relleno que *cite* el marcador no puede contar como plantilla.

    El `README` y este propio repo hablan de `okf-wiki:stub` en prosa; si la
    comprobación fuese sobre el fichero entero, un `purpose.md` que documentase
    su propio marcador se reportaría eternamente como vacío.
    """
    citing = "# Propósito\n\nBorra la línea `<!-- okf-wiki:stub -->` al rellenarlo.\n"
    assert not templates.is_stub(citing)


def test_is_stub_tolerates_leading_blank_lines():
    assert templates.is_stub("\n\n" + templates.PURPOSE_STUB)


# --------------------------------------------------------------------------- #
# Las plantillas enseñan la estructura tema/subtema, no otra                   #
# --------------------------------------------------------------------------- #
def test_purpose_template_asks_for_the_out_of_scope_section():
    """Es la sección que permite descartar material en vez de escribir por inercia."""
    text = templates.render("purpose")
    for heading in ("## Objetivo", "## Preguntas que debe saber responder",
                    "## Alcance", "## Fuera de alcance", "## Audiencia"):
        assert heading in text, heading


def test_schema_template_declares_topics_and_subtopics():
    text = templates.render("schema")
    assert "## Temas y subtemas" in text
    # La tabla es lo que hace que la taxonomía sea de la instancia y no de la skill.
    assert "| Tema (carpeta) | `type` | Subtemas (carpetas) |" in text
    # Y deja dicho que el subtema no toca el `type`, que es el error fácil.
    assert "NO cambia el `type`" in text


def test_schema_template_repeats_the_taxonomy_prohibitions():
    """H4 — la tabla de temas es de la instancia; estas cuatro reglas no.

    `schema.md` es lo que la skill lee como taxonomía de la wiki. Si sólo trae la
    tabla, una instancia puede declararse en `conceptos/` o con un `type` por
    subtema sin que nada en el fichero lo desmienta, y lo que manda es «lo que
    pongas aquí» sobre lo que supondría la skill.
    """
    text = templates.render("schema")
    assert "### Prohibido (esto no lo decide la instancia)" in text
    # 1) nada de tipo de cosa, ni como carpeta ni como `type`.
    for wrong in ("`entidades/`", "`conceptos/`", "`personas/`", "`definiciones/`",
                  "`type: Entidad`", "`type: Concepto`"):
        assert wrong in text, wrong
    # 2) el `type` lo fija el tema de primer nivel, no el subtema.
    assert "`type: Ventas`" in text and "`type: Retencion`" in text
    # 3) dos niveles como máximo.
    assert "DOS niveles" in text and "<tema>/<subtema>/" in text
    # 4) una página, una carpeta; lo demás son enlaces.
    assert "UNA sola carpeta" in text
    assert "enlace markdown relativo" in text


def test_schema_template_says_who_checks_the_prohibitions():
    """Una prohibición sin consecuencia visible se ignora: se cita el comando."""
    text = templates.render("schema")
    assert "okf-wiki verify" in text
    assert "--strict" in text


def test_the_schema_prohibitions_match_what_the_lint_actually_flags():
    """La plantilla y el lint no pueden divergir: prometerían reglas distintas.

    Se comprueba contra `_lint_taxonomy` con las tres violaciones que describe el
    fichero, para que quitar una regla del código rompa aquí.
    """
    from okf_wiki.cli import _lint_taxonomy

    fm = {"type": "Ventas"}
    assert _lint_taxonomy(Path("conceptos/acme.md"), fm)                 # tipo de cosa
    assert _lint_taxonomy(Path("ventas/retencion/plan.md"),
                          {"type": "Retencion"})                        # `type` del subtema
    assert _lint_taxonomy(Path("ventas/retencion/grandes/acme.md"), fm)  # tercer nivel
    # Y la estructura que la plantilla prescribe no dispara ninguna.
    assert _lint_taxonomy(Path("ventas/retencion/plan.md"), fm) == []


def test_page_template_is_okf_v02_and_links_relatively():
    text = templates.render("page")
    assert text.startswith("---\n")
    for field in ("type:", "title:", "description:", "tags:", "status:",
                  "generated: {", "sources:"):
        assert field in text, field
    # v0.1 no debe reaparecer por la puerta de atrás.
    assert "timestamp:" not in text
    assert "# Citations" not in text
    # El enlace de ejemplo cruza temas con ruta relativa desde un subtema.
    assert "](../../<otro-tema>/<su-subtema>/<su-pagina>.md)" in text
    # Footnote atada por id, no numérica.
    assert "[^<slug-corto-y-estable>]" in text
    assert "[^1]" not in text


def test_page_template_frontmatter_parses_as_yaml_even_unfilled():
    """Un agente que rellene sólo el cuerpo no puede toparse con un YAML roto.

    Por eso los huecos son `<…>` sin comillas ni comas dentro: cualquiera de las
    dos convertiría el flow mapping de `generated` en un error de parseo.
    """
    import yaml

    text = templates.render("page")
    parsed = yaml.safe_load(text.split("---\n", 2)[1])
    assert parsed["status"] == "draft"
    assert set(parsed["generated"]) == {"by", "at"}
    assert isinstance(parsed["sources"], list) and len(parsed["sources"]) == 1


def test_page_template_starts_as_a_draft():
    """`stable` por defecto haría pasar por revisada una página a medio escribir."""
    assert "status: draft" in templates.render("page")


def test_proposal_template_covers_the_seven_required_sections():
    """La propuesta es el artefacto que se aprueba: si le falta un apartado,
    el usuario aprueba a ciegas."""
    text = templates.render("proposal")
    for heading in (
        "## 1. Fuentes analizadas",
        "## 2. Encaje con `purpose.md`",
        "## 3. Temas y subtemas",
        "## 4. Páginas",
        "## 5. Enlaces transversales previstos",
        "## 6. Contradicciones y conflictos con lo ya escrito",
        "## 7. Preguntas abiertas",
    ):
        assert heading in text, heading


def test_proposal_template_plans_pages_inside_topic_and_subtopic():
    text = templates.render("proposal")
    assert "wiki/<tema>/<subtema>/<pagina>.md" in text
    # Y distingue crear de actualizar: no duplicar es media calidad de la wiki.
    assert "| crear |" in text
    assert "| actualizar |" in text


def test_proposal_template_says_nothing_is_written_before_approval():
    assert "Nada de esto se escribe hasta que lo apruebes" in templates.render("proposal")


# --------------------------------------------------------------------------- #
# context_status: el estado sin juicios de valor                               #
# --------------------------------------------------------------------------- #
def test_context_status_on_a_missing_instance_reports_absence_not_an_error(tmp_path):
    """Una ruta que no existe no puede reventar: `context` es informativo."""
    status = helpers.context_status(tmp_path / "no-existe")
    assert status["purpose"]["exists"] is False
    assert status["schema"]["exists"] is False
    assert status["purpose"]["stub"] is False
    assert status["purpose"]["content"] is None
    assert status["ready"] is False


def test_context_status_on_a_legacy_instance_without_the_files(tmp_path):
    """Wikis creadas antes de que existiera el contexto siguen funcionando."""
    legacy = tmp_path / "vieja"
    (legacy / "sources").mkdir(parents=True)
    (legacy / "wiki").mkdir()
    status = helpers.context_status(legacy)
    assert (status["purpose"]["exists"], status["ready"]) == (False, False)


def test_fresh_init_leaves_both_files_as_stubs(instance):
    status = helpers.context_status(instance)
    assert status["purpose"]["exists"] and status["purpose"]["stub"]
    assert status["schema"]["exists"] and status["schema"]["stub"]
    assert status["ready"] is False


def test_ready_flips_once_purpose_is_filled(instance):
    fill(instance / helpers.PURPOSE_FILENAME)
    status = helpers.context_status(instance)
    assert status["purpose"]["stub"] is False
    assert status["ready"] is True


def test_ready_ignores_schema(instance):
    """Sin taxonomía declarada la ingesta puede seguir; sin propósito, no.

    Si `schema.md` entrase en `ready`, la skill se pararía en un caso en el que
    el diseño dice explícitamente que debe continuar y ofrecerse a volcar la
    taxonomía al terminar.
    """
    fill(instance / helpers.PURPOSE_FILENAME)
    assert helpers.context_status(instance)["ready"] is True
    fill(instance / helpers.SCHEMA_FILENAME, "# Estructura\n\n| ventas/ | Ventas |\n")
    assert helpers.context_status(instance)["ready"] is True


def test_an_empty_purpose_is_not_ready(instance):
    """Un fichero vacío no lleva marcador, pero tampoco es un propósito."""
    (instance / helpers.PURPOSE_FILENAME).write_text("", encoding="utf-8")
    status = helpers.context_status(instance)
    assert status["purpose"]["exists"] is True
    assert status["ready"] is False


def test_context_status_reports_an_unreadable_file_without_raising(instance):
    """Un `purpose.md` que es un directorio se informa, no rompe la ingesta."""
    (instance / helpers.PURPOSE_FILENAME).unlink()
    (instance / helpers.PURPOSE_FILENAME).mkdir()
    status = helpers.context_status(instance)
    assert status["purpose"]["exists"] is False  # no es un fichero
    assert status["ready"] is False


# --------------------------------------------------------------------------- #
# init: escribe el contexto, y nunca lo pisa                                   #
# --------------------------------------------------------------------------- #
def test_init_writes_both_context_files_at_the_instance_root(instance):
    assert (instance / "purpose.md").is_file()
    assert (instance / "schema.md").is_file()
    # En la RAÍZ, no dentro del bundle: dentro serían conceptos OKF inválidos.
    assert not (instance / "wiki" / "purpose.md").exists()
    assert not (instance / "wiki" / "schema.md").exists()


def test_init_personalises_the_templates_with_the_wiki_name(instance):
    assert "Mi Wiki" in (instance / "purpose.md").read_text(encoding="utf-8")
    assert "Mi Wiki" in (instance / "schema.md").read_text(encoding="utf-8")


def test_init_is_idempotent_and_never_clobbers_a_filled_purpose(instance, capsys):
    """Re-ejecutar `init` es el camino normal al reparar una instancia."""
    fill(instance / "purpose.md", "# Propósito\n\nNo me pises.\n")
    fill(instance / "schema.md", "# Estructura\n\nTampoco a mí.\n")
    assert run(["init", str(instance), "--name", "Mi Wiki", "--no-git"]) == 0
    out = capsys.readouterr().out
    assert "ya existían" in out
    assert (instance / "purpose.md").read_text(encoding="utf-8") == \
        "# Propósito\n\nNo me pises.\n"
    assert (instance / "schema.md").read_text(encoding="utf-8") == \
        "# Estructura\n\nTampoco a mí.\n"


def test_init_tells_the_user_that_filling_purpose_is_the_next_step(tmp_path, capsys):
    assert run(["init", str(tmp_path / "w"), "--no-git"]) == 0
    out = capsys.readouterr().out
    assert "purpose.md" in out
    assert "rellena purpose.md" in out.lower()


def test_init_on_a_legacy_instance_adds_the_missing_context(tmp_path, capsys):
    legacy = tmp_path / "vieja"
    (legacy / "sources").mkdir(parents=True)
    (legacy / "wiki").mkdir()
    (legacy / "wiki" / "log.md").write_text("---\ntype: Log\n---\n\nvieja\n", encoding="utf-8")
    assert run(["init", str(legacy), "--no-git"]) == 0
    capsys.readouterr()
    assert (legacy / "purpose.md").is_file()
    # Y no ha tocado el log que ya existía.
    assert "vieja" in (legacy / "wiki" / "log.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# `okf-wiki context`                                                           #
# --------------------------------------------------------------------------- #
def test_context_command_warns_when_purpose_is_a_stub(instance, capsys):
    assert run(["context", str(instance)]) == 0
    out = capsys.readouterr().out
    assert "SIN RELLENAR (plantilla)" in out
    assert "AVISO" in out
    assert "okf-wiki:stub" in out


def test_context_command_confirms_a_filled_purpose(instance, capsys):
    fill(instance / "purpose.md")
    assert run(["context", str(instance)]) == 0
    out = capsys.readouterr().out
    assert "purpose.md — ok" in out
    assert "Contexto listo" in out
    assert "AVISO" not in out


def test_context_command_prints_the_contents_so_the_skill_reads_them_once(instance, capsys):
    fill(instance / "purpose.md", "# Propósito\n\nRESPONDER PREGUNTAS DE VENTAS.\n")
    assert run(["context", str(instance)]) == 0
    assert "RESPONDER PREGUNTAS DE VENTAS." in capsys.readouterr().out


def test_context_command_reports_missing_files_without_failing(tmp_path, capsys):
    """Exit 0 sobre una instancia antigua: es un aviso, no un error de ruta."""
    legacy = tmp_path / "vieja"
    legacy.mkdir()
    assert run(["context", str(legacy)]) == 0
    assert "FALTA" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# H1 — `context` valida la instancia con el mismo criterio que los demás        #
# --------------------------------------------------------------------------- #
# Las tres rutas malas producían el mismo `FALTA` con exit 0 que una instancia
# antigua legítima, así que la skill seguía la ingesta sin ancla de dominio
# creyendo que esa wiki simplemente no tenía contexto.
def test_context_on_a_path_that_does_not_exist_exits_3(tmp_path, capsys):
    assert run(["context", str(tmp_path / "no-existe")]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    assert captured.out == ""  # un `FALTA` en stdout se lee como "no hay contexto"
    assert "no-existe" in captured.err
    assert "okf-wiki init" in captured.err
    assert "Traceback" not in captured.err


def test_context_on_a_file_exits_3(tmp_path, capsys):
    fichero = tmp_path / "purpose.md"
    fichero.write_text("# Propósito\n", encoding="utf-8")
    assert run(["context", str(fichero)]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No es un directorio" in captured.err
    assert "Traceback" not in captured.err


def test_context_on_the_wiki_bundle_points_back_to_the_instance(instance, capsys):
    """El error fácil: pasarle `<instancia>/wiki`, donde el contexto NO vive."""
    assert run(["context", str(instance / helpers.WIKI_DIRNAME)]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert "wiki/" in err
    # El mensaje es accionable: trae la ruta correcta, la de la instancia.
    assert f"okf-wiki context {instance}" in err
    assert "Traceback" not in err


def test_context_on_the_wiki_bundle_exits_3_in_a_real_process(instance):
    """El código de salida que ve la skill, sin traceback."""
    done = cli_subprocess("context", str(instance / helpers.WIKI_DIRNAME))
    assert done.returncode == cli.EXIT_INGEST, done.stderr
    assert done.stdout == ""
    assert "Traceback" not in done.stderr


def test_context_json_also_refuses_a_bad_path(tmp_path, capsys):
    """Con `--json` el fallo tiene que ser igual: la skill parsea esa salida."""
    assert run(["context", str(tmp_path / "no-existe"), "--json"]) == cli.EXIT_INGEST
    assert capsys.readouterr().out == ""


def test_context_error_travels_as_an_ingest_error():
    assert issubclass(helpers.MissingInstanceError, helpers.IngestError)


def test_resolve_instance_accepts_an_instance_named_wiki(tmp_path):
    """Una instancia que se llame `wiki` no es su propio bundle.

    Se distingue por tener dentro su `wiki/` (o su contexto): sin esta salvaguarda,
    `~/wikis/wiki` quedaría inaccesible para `context`.
    """
    inst = tmp_path / "wiki"
    (inst / helpers.WIKI_DIRNAME).mkdir(parents=True)
    (inst / helpers.SOURCES_DIRNAME).mkdir()
    assert helpers.resolve_instance(inst) == inst

    # Y con el contexto en la raíz, aunque no haya subcarpeta `wiki/` todavía.
    otra = tmp_path / "otras" / "wiki"
    otra.mkdir(parents=True)
    (otra / helpers.PURPOSE_FILENAME).write_text("# Propósito\n", encoding="utf-8")
    (otra.parent / helpers.SOURCES_DIRNAME).mkdir()  # el padre despista, el contexto manda
    assert helpers.resolve_instance(otra) == otra


def test_resolve_instance_leaves_a_legacy_instance_alone(tmp_path):
    """Sin `purpose.md` ni `schema.md`, pero es un directorio: se acepta y se avisa."""
    legacy = tmp_path / "vieja"
    (legacy / helpers.SOURCES_DIRNAME).mkdir(parents=True)
    assert helpers.resolve_instance(legacy) == legacy
    assert helpers.context_status(legacy)["ready"] is False


def test_context_status_stays_lenient_for_callers_that_only_want_the_state(tmp_path):
    """El guard vive en el comando, no en el helper: `context_status` nunca lanza."""
    status = helpers.context_status(tmp_path / "no-existe")
    assert status["purpose"]["exists"] is False


def test_context_json_is_the_raw_status(instance, capsys):
    assert run(["context", str(instance), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["purpose"]["stub"] is True
    assert payload["purpose"]["filename"] == "purpose.md"
    assert set(payload) == {"instance", "purpose", "schema", "ready"}


def test_context_json_ready_is_the_single_signal_the_skill_checks(instance, capsys):
    fill(instance / "purpose.md")
    assert run(["context", str(instance), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True


# --------------------------------------------------------------------------- #
# `okf-wiki template`                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(templates.TEMPLATES))
def test_template_command_prints_each_template(name, capsys):
    assert run(["template", name]) == 0
    assert capsys.readouterr().out == templates.render(name)


def test_template_command_accepts_a_wiki_name(capsys):
    assert run(["template", "purpose", "--name-for", "Wiki de Soporte"]) == 0
    assert "Wiki de Soporte" in capsys.readouterr().out


def test_template_command_rejects_an_unknown_name(capsys):
    with pytest.raises(SystemExit) as exit_info:
        run(["template", "inventada"])
    assert exit_info.value.code == 2  # argparse: `choices` lo rechaza antes de ejecutar


def test_template_output_can_be_written_back_as_a_valid_stub(tmp_path, capsys):
    """`okf-wiki template purpose > purpose.md` tiene que dar un stub detectable."""
    assert run(["template", "purpose"]) == 0
    target = tmp_path / "purpose.md"
    target.write_text(capsys.readouterr().out, encoding="utf-8")
    (tmp_path / "sources").mkdir()
    assert helpers.context_status(tmp_path)["purpose"]["stub"] is True
