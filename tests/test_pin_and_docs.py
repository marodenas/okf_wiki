"""El pin del motor OKF y la documentación no se pueden desincronizar en silencio.

Si alguien mueve la revisión fijada en `scripts/setup.sh` sin tocar README/pyproject, o
devuelve la skill al frontmatter v0.1, estas pruebas fallan.
"""
from __future__ import annotations

import re
from pathlib import Path

import okf_wiki
from okf_wiki import cli

REPO = Path(__file__).resolve().parents[1]
SETUP = (REPO / "scripts" / "setup.sh").read_text(encoding="utf-8")
README = (REPO / "README.md").read_text(encoding="utf-8")
PYPROJECT = (REPO / "pyproject.toml").read_text(encoding="utf-8")
SKILL = (REPO / "skill" / "okf-ingest" / "SKILL.md").read_text(encoding="utf-8")


def flat(text: str) -> str:
    """El mismo texto con el espacio en blanco colapsado.

    Las afirmaciones sobre prosa se comprueban contra esta versión: si no, un
    reajuste del ancho de línea rompe la prueba sin que la documentación haya
    cambiado de sentido, y la forma de arreglarlo (volver a partir la frase por
    donde estaba) no tiene nada que ver con lo que la prueba protege.
    """
    return " ".join(text.split())


SKILL_FLAT = flat(SKILL)
README_FLAT = flat(README)


def pinned_rev() -> str:
    match = re.search(r'OKF_REV="\$\{OKF_REV:-([0-9a-f]{40})\}"', SETUP)
    assert match, "setup.sh debe fijar OKF_REV a un SHA completo de 40 caracteres"
    return match.group(1)


def test_setup_pins_a_full_sha():
    assert len(pinned_rev()) == 40


def test_pinned_rev_is_documented_in_readme_and_pyproject():
    rev = pinned_rev()
    assert rev in README, "documenta en el README la revisión fijada"
    assert rev in PYPROJECT, "la nota del pyproject sobre el motor debe citar la revisión"


def test_setup_checks_out_the_pin_and_verifies_it():
    assert 'checkout --quiet --detach "$OKF_REV"' in SETUP
    # Un clon superficial no puede hacer checkout de un SHA arbitrario.
    assert "--depth" not in SETUP
    # Y se comprueba que el HEAD quedó donde debía.
    assert SETUP.count('rev-parse HEAD') >= 2


def test_setup_refuses_to_clobber_local_changes():
    assert "status --porcelain" in SETUP


def test_setup_installs_the_test_extras():
    assert '-e "$REPO_DIR[dev]"' in SETUP
    assert 'dev = ["pytest' in PYPROJECT


def test_setup_smoke_test_exercises_the_v02_api():
    for symbol in ("trust_tier", "normalize_verified", "is_stale"):
        assert symbol in SETUP, f"el smoke test debe comprobar {symbol} (API de v0.2)"


def test_setup_explains_a_v01_engine_instead_of_dumping_a_traceback():
    assert "no expone la API de OKF v0.2" in SETUP


def test_setup_is_rerunnable():
    # Re-ejecutarlo es el camino normal al mover OKF_REV: no debe morir por el venv.
    assert "--allow-existing" in SETUP


def test_okf_version_is_consistent():
    assert cli.OKF_VERSION == "0.2"
    assert 'okf_version: "0.2"' in README
    assert 'okf_version: "0.2"' in SKILL


def test_package_version_matches_pyproject():
    """`__version__` es el fallback de `generated.by`: no puede quedarse atrás."""
    declared = re.search(r'^version = "([^"]+)"', PYPROJECT, re.MULTILINE)
    assert declared, "pyproject.toml debe declarar project.version"
    assert declared.group(1) == okf_wiki.__version__ == "0.2.0"


# --------------------------------------------------------------------------- #
# Los contratos de fiabilidad viven en la doc, no sólo en el código             #
# --------------------------------------------------------------------------- #
def test_readme_documents_the_bundle_guard():
    """A1/A2: la tabla de códigos tiene que contar que una ruta mala sale con 3."""
    assert "falta `wiki/`" in README
    assert "`verify`, `index` y `viz` **fallan**" in README
    # Y deja constancia del falso ok que se corrigió, que es el motivo de la regla.
    assert '"ok": true' in README


def test_readme_documents_the_commit_state_contract():
    """M1: exigir la salida literal de `scan` es parte del contrato público."""
    assert "commit-state` valida el JSON de stdin" in README
    assert "now --json" in README
    assert "sin tocar el manifest" in README


def test_readme_documents_that_commit_state_never_invents_the_instance():
    """M3: que no cree `sources/` al fallar también es contrato público."""
    assert "sale con `3` **sin crear nada**" in README
    # Y queda constancia del falso ok que motivó la regla.
    assert "mkdir -p" in README


def test_skill_tells_the_agent_that_exit_3_leaves_the_sources_unmarked():
    """Un 3 en el paso 11 no es una ingesta cerrada: el manifest sigue sin marcar."""
    assert "siguen sin marcar" in SKILL
    assert "no des la ingesta por cerrada" in SKILL


def test_skill_warns_that_exit_3_is_a_path_error_not_an_empty_wiki():
    """La skill es quien interpreta los códigos: no puede leer un 3 como «todo bien»."""
    assert "la ruta no contiene un bundle" in SKILL
    assert "no lo interpretes como «todo bien»" in SKILL
    assert "falta `wiki/` o no tiene páginas" in SKILL


def test_skill_tells_the_agent_what_commit_state_accepts():
    assert "no vale la salida de\n    `now --json`" in SKILL or "no vale la salida de `now --json`" in SKILL
    assert "el JSON literal del paso 2" in SKILL
    assert "no toca el manifest" in SKILL


def test_every_exit_code_is_documented_in_both_places():
    for code in (cli.EXIT_OK, cli.EXIT_INVALID, cli.EXIT_ENGINE_MISSING,
                 cli.EXIT_INGEST, cli.EXIT_BAD_INPUT):
        assert f"| `{code}` |" in README, f"el README no documenta el código {code}"
    for code in (cli.EXIT_INVALID, cli.EXIT_ENGINE_MISSING,
                 cli.EXIT_INGEST, cli.EXIT_BAD_INPUT):
        assert f"`{code}`" in SKILL, f"la skill no menciona el código {code}"


# --------------------------------------------------------------------------- #
# La skill enseña v0.2, no v0.1                                                #
# --------------------------------------------------------------------------- #
def test_skill_prescribes_generated_not_timestamp():
    assert "generated: { by: claude-code/<modelo>, at:" in SKILL
    prescribed = [ln for ln in SKILL.splitlines() if ln.strip().startswith("timestamp:")]
    assert prescribed == [], f"la skill no debe prescribir `timestamp`: {prescribed}"


def test_skill_prescribes_sources_with_ids_and_keyed_footnotes():
    assert re.search(r"^sources:$", SKILL, re.MULTILINE)
    assert "id: informe-q2" in SKILL
    assert "[^informe-q2]" in SKILL
    assert "sources[].id" in SKILL


def test_skill_covers_lifecycle_and_trust_fields():
    for field in ("status:", "stale_after:", "verified"):
        assert field in SKILL, field
    # El agente no se auto-firma como revisor humano.
    assert "NUNCA te auto-asignes" in SKILL


def test_skill_keeps_the_relative_link_rule_for_the_current_viewer():
    assert "solo sigue rutas\n  relativas" in SKILL or "solo sigue rutas relativas" in SKILL
    assert "PROHIBIDO enlaces absolutos" in SKILL


def test_skill_tells_the_agent_to_ask_the_cli_for_the_date():
    assert "okf-wiki now --json" in SKILL
    assert "Nunca inventes" in SKILL


# --------------------------------------------------------------------------- #
# El flujo humano-en-el-bucle: dos fases con una aprobación en medio           #
# --------------------------------------------------------------------------- #
def test_skill_states_the_two_phase_contract():
    """Proponer y escribir son fases separadas, no un matiz de redacción."""
    assert "## Las dos fases: primero propones, después escribes" in SKILL
    assert "FASE 1 — ANÁLISIS" in SKILL
    assert "FASE 2 — ESCRITURA" in SKILL
    assert "APROBACIÓN" in SKILL


def test_skill_forbids_writing_before_approval():
    assert "En la fase 1 no se escribe nada en `wiki/`" in SKILL_FLAT
    assert "La aprobación es explícita" in SKILL_FLAT
    # Y descubrir a mitad que falta un tema devuelve a la fase 1, no lo improvisa.
    assert "vuelve a la fase 1" in SKILL_FLAT


def test_skill_reads_the_instance_context_before_scanning():
    """El paso 0 va antes del `scan`: sin ancla, la propuesta no se puede contrastar."""
    assert "okf-wiki context <instancia>" in SKILL
    assert "0. **Leer el contexto.**" in SKILL
    assert SKILL.index("0. **Leer el contexto.**") < SKILL.index("2. **Escanear la dropzone.**")


def test_docs_call_the_context_argument_the_instance_not_the_bundle():
    """`context` es el único comando que trabaja con la raíz: el nombre lo dice.

    Documentarlo como `<bundle>` invitaba justo al error que ahora sale con exit 3:
    pasarle `<instancia>/wiki`, donde el contexto no vive.
    """
    assert "okf-wiki context <bundle>" not in SKILL
    assert "okf-wiki context <bundle>" not in README
    assert "okf-wiki context <instancia>" in SKILL
    assert "okf-wiki context <instancia>" in README
    # Y el propio CLI usa el mismo nombre en su ayuda.
    assert "instancia" in _subparser_help("context")


def _subparser_help(name: str) -> str:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.dest == "cmd"]
    return actions[0].choices[name].format_help()


def test_docs_describe_the_taxonomy_warnings_of_verify():
    """Si `--strict` empieza a fallar por taxonomía, tiene que estar escrito dónde."""
    assert "taxonomía temática" in SKILL_FLAT
    assert "taxonomía temática" in README_FLAT
    for evidence in ("replica el subtema", "entidad/concepto"):
        assert evidence in README_FLAT, evidence


def test_skill_keeps_the_step_numbering_other_docs_reference():
    """Añadir el contexto como paso 0 no puede renumerar los pasos citados fuera."""
    for step in ("1. **Fecha real.**", "2. **Escanear la dropzone.**",
                 "5. **PROPONER el plan", "11. **Actualizar el manifest:**"):
        assert step in SKILL, step


def test_skill_requires_a_structured_proposal_with_every_section():
    """Los siete apartados son el contrato de lo que el usuario aprueba."""
    for section in ("Fuentes analizadas", "Encaje con `purpose.md`",
                    "Temas y subtemas", "Enlaces transversales previstos",
                    "Contradicciones y conflictos", "Preguntas abiertas"):
        assert section in SKILL, section
    assert "okf-wiki template proposal" in SKILL


def test_skill_demands_evidence_and_forbids_silent_contradiction_fixes():
    assert "evidencia" in SKILL
    assert "Nunca resuelvas una contradicción en silencio" in SKILL_FLAT


def test_skill_explains_purpose_and_schema_without_making_them_mandatory():
    assert "purpose.md" in SKILL and "schema.md" in SKILL
    # Falta de contexto: avisa y ofrece ayuda, pero una wiki antigua sigue ingiriendo.
    assert "`context` los reporta como `FALTA` y no falla" in SKILL_FLAT


# --------------------------------------------------------------------------- #
# Estructura temática: temas, subtemas, y nada de entidades/conceptos          #
# --------------------------------------------------------------------------- #
def test_skill_prescribes_topic_and_subtopic_folders():
    assert "**Estructura por TEMA y SUBTEMA, nunca por tipo de cosa.**" in SKILL_FLAT
    assert "wiki/<tema>/<subtema>/" in SKILL or "<tema>/<subtema>/" in SKILL


def test_skill_says_the_subtopic_does_not_change_the_type():
    """El grafo colorea por `type`: un `type` por subtema haría la leyenda inútil."""
    assert "El subtema es organización, no semántica." in SKILL_FLAT
    assert "`type: Ventas`, no `type: Retencion`" in SKILL_FLAT


def test_skill_rejects_entities_and_concepts_as_the_primary_model():
    assert "**NO organices por entidades y conceptos.**" in SKILL_FLAT
    for wrong in ("`entidades/`", "`conceptos/`", "`type: Concepto`", "`type: Entidad`"):
        assert wrong in SKILL, wrong


def test_skill_keeps_links_as_the_cross_cutting_graph():
    assert "Las carpetas son el árbol de navegación; los enlaces son el grafo." in SKILL_FLAT
    # Y prohíbe la salida fácil: duplicar la página en dos carpetas.
    assert "Nunca dupliques una página en dos carpetas" in SKILL_FLAT


def test_skill_never_asks_the_agent_to_write_an_index():
    """Los `index.md` de tema y subtema los genera el motor, en cada nivel."""
    assert "Todos los `index.md` los genera `okf-wiki index`" in SKILL_FLAT


# --------------------------------------------------------------------------- #
# README: arquitectura, roles y qué NO existe todavía                          #
# --------------------------------------------------------------------------- #
def test_readme_documents_the_four_stage_architecture():
    assert "## Arquitectura" in README
    for stage in ("**Fuentes**", "**Propuesta (HITL)**", "**Markdown temático**",
                  "**Construcción estática**"):
        assert stage in README, stage


def test_readme_documents_who_does_what():
    for role in ("**Humano**", "**LLM**", "**CLI**"):
        assert role in README, role
    # La frontera que justifica los roles: el CLI no lleva LLM dentro.
    assert "el CLI **no tiene LLM dentro**" in README_FLAT


def test_readme_says_nothing_is_written_before_the_human_approves():
    assert "**No escribe nada hasta que la apruebas.**" in README_FLAT
    assert "no escribe nada en `wiki/` hasta que apruebas la propuesta" in README_FLAT


def test_readme_describes_the_build_that_exists_today():
    """`build`/`watch` ya existen; lo que el README no puede perder es el límite.

    La versión anterior de esta prueba exigía la frase «No hay `okf-wiki build`
    ni `okf-wiki watch`» y, junto a ella, el límite que tendría un `watch` el día
    que existiera: que nunca sintetizaría ni borraría páginas por su cuenta. El
    comando ya existe con ese límite intacto, así que lo que se comprueba ahora
    es que el README lo siga diciendo (ver también las pruebas de `build`/`watch`
    al final de este fichero).
    """
    assert "No hay `okf-wiki build`" not in README_FLAT
    assert "no sintetiza, no borra páginas y no commitea" in README_FLAT
    # La construcción sigue siendo explícita y bajo demanda: nada se dispara solo.
    assert "explícita y bajo demanda" in README_FLAT


def test_readme_documents_the_topic_subtopic_layout():
    assert "wiki/<tema>/<subtema>/" in README
    assert "SUBTEMA → NO cambia el `type`" in README_FLAT
    assert "Las carpetas son el árbol de navegación; los enlaces son el grafo." in README_FLAT


def test_readme_rejects_entity_and_concept_folders():
    assert "por materia, nunca por tipo de cosa" in README_FLAT
    assert "no hay `entidades/` ni `conceptos/`" in README_FLAT


def test_readme_documents_the_instance_context_files():
    assert "purpose.md" in README and "schema.md" in README
    assert "okf-wiki:stub" in README
    # Y que no son obligatorios: una wiki antigua sigue funcionando.
    assert "los reporta como `FALTA` sin fallar" in README_FLAT


# --------------------------------------------------------------------------- #
# Los comandos nuevos existen en el CLI y están documentados en los dos sitios #
# --------------------------------------------------------------------------- #
def test_context_and_template_are_documented_where_they_are_used():
    for command in ("okf-wiki context", "okf-wiki template"):
        assert command in README, f"el README no documenta `{command}`"
        assert command in SKILL, f"la skill no menciona `{command}`"


def test_every_template_name_is_documented():
    from okf_wiki import templates

    for name in templates.TEMPLATES:
        assert f"`{name}`" in README or f"{name}\\|" in README or f"\\|{name}" in README, name


def test_the_documented_subcommands_exist_in_the_parser():
    """La doc y el parser no pueden desincronizarse: la skill copia estos comandos."""
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.dest == "cmd"]
    assert actions, "el parser debe exponer subcomandos"
    available = set(actions[0].choices)
    for command in ("context", "template", "init", "scan", "verify"):
        assert command in available, command


# --------------------------------------------------------------------------- #
# `build`/`watch`: la interfaz estática y sus límites, en los dos documentos   #
# --------------------------------------------------------------------------- #
def test_build_and_watch_are_documented_where_they_are_used():
    for command in ("okf-wiki build", "okf-wiki watch"):
        assert command in README, f"el README no documenta `{command}`"
    assert "okf-wiki build" in SKILL, "la skill tiene que reconstruir el sitio (paso 9)"


def test_readme_no_longer_claims_that_build_and_watch_do_not_exist():
    """El README decía «No hay `okf-wiki build` ni `okf-wiki watch`». Ahora los hay."""
    assert "No hay `okf-wiki build`" not in README_FLAT


def test_readme_documents_that_the_site_lives_outside_the_bundle():
    assert "FUERA del bundle" in README or "fuera de `wiki/`" in README_FLAT
    assert "site/" in README
    assert "se niega" in README_FLAT and "--out" in README


def test_readme_documents_that_the_site_is_reproducible():
    assert "Mismo bundle → mismos bytes" in README_FLAT
    assert "stale_after" in README


def test_readme_documents_every_piece_of_the_static_interface():
    for piece in ("Portada", "Árbol tema/subtema", "Lector",
                  "Procedencia y estado", "Enlaces cruzados", "Búsqueda",
                  "Datos de grafo"):
        assert piece in README, f"el README no describe: {piece}"
    assert "graph.json" in README and "backlinks" in README


def test_readme_and_skill_keep_the_human_approval_limit_on_watch():
    assert "no sintetiza, no borra" in README_FLAT
    assert "no commitea" in README_FLAT
    assert "no ingiere ni commitea" in SKILL_FLAT


def test_skill_forbids_writing_inside_the_generated_site():
    assert "NUNCA lo edites" in SKILL or "Nunca escribas ni edites nada dentro de `site/`" in SKILL_FLAT
    assert "artefacto derivado" in SKILL_FLAT


def test_build_and_watch_exist_in_the_parser():
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.dest == "cmd"]
    available = set(actions[0].choices)
    assert {"build", "watch"} <= available


def test_the_site_output_directory_is_named_the_same_everywhere():
    from okf_wiki import site

    assert f"{site.SITE_DIRNAME}/" in README
    assert f"{site.SITE_DIRNAME}/" in SKILL
    assert site.MANIFEST_FILENAME in README
