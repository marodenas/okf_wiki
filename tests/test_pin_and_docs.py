"""El pin del motor OKF y la documentación no se pueden desincronizar en silencio.

Lo que se comprueba aquí son CONTRATOS, no redacción: que la revisión fijada en
`scripts/setup.sh` esté documentada, que cada código de salida del CLI aparezca en el
README y en la skill, que los comandos documentados existan en el parser, que la skill
enseñe el formato v0.2 y no el v0.1, y que el nombre de la carpeta del sitio sea el mismo
en todos los sitios.

Lo que NO se comprueba es la prosa. Una versión anterior exigía unas cuarenta frases
literales del README y de la skill ("el CLI **no tiene LLM dentro**", "Mismo bundle →
mismos bytes"...). Eso no protegía el código: protegía una frase, y convertía cualquier
reescritura de la documentación en una caza de literales. Los comportamientos que esas
frases describían están cubiertos por los tests del código (`test_cli_v02`,
`test_bundle_guard`, `test_site_build`, `test_watch`).
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


def pinned_rev() -> str:
    match = re.search(r'OKF_REV="\$\{OKF_REV:-([0-9a-f]{40})\}"', SETUP)
    assert match, "setup.sh debe fijar OKF_REV a un SHA completo de 40 caracteres"
    return match.group(1)


def _subcommands() -> set[str]:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.dest == "cmd"]
    assert actions, "el parser debe exponer subcomandos"
    return set(actions[0].choices)


def _subparser_help(name: str) -> str:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.dest == "cmd"]
    return actions[0].choices[name].format_help()


# --------------------------------------------------------------------------- #
# El pin del motor OKF                                                          #
# --------------------------------------------------------------------------- #
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
    assert SETUP.count("rev-parse HEAD") >= 2


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


# --------------------------------------------------------------------------- #
# Versiones                                                                     #
# --------------------------------------------------------------------------- #
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
# Los códigos de salida son un contrato con la skill                            #
# --------------------------------------------------------------------------- #
def test_every_exit_code_is_documented_in_both_places():
    for code in (cli.EXIT_OK, cli.EXIT_INVALID, cli.EXIT_ENGINE_MISSING,
                 cli.EXIT_INGEST, cli.EXIT_BAD_INPUT):
        assert f"| `{code}` |" in README, f"el README no documenta el código {code}"
    for code in (cli.EXIT_INVALID, cli.EXIT_ENGINE_MISSING,
                 cli.EXIT_INGEST, cli.EXIT_BAD_INPUT):
        assert f"`{code}`" in SKILL, f"la skill no menciona el código {code}"


# --------------------------------------------------------------------------- #
# La skill enseña el formato v0.2, no el v0.1                                   #
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


def test_skill_asks_the_cli_for_the_date():
    """La fecha sale del CLI, no de la memoria del modelo."""
    assert "okf-wiki now --json" in SKILL


# --------------------------------------------------------------------------- #
# Lo documentado existe en el parser, y se llama igual en todos los sitios      #
# --------------------------------------------------------------------------- #
def test_the_documented_subcommands_exist_in_the_parser():
    available = _subcommands()
    for command in ("context", "template", "init", "scan", "verify", "build", "watch"):
        assert command in available, command


def test_context_and_template_are_documented_where_they_are_used():
    for command in ("okf-wiki context", "okf-wiki template"):
        assert command in README, f"el README no documenta `{command}`"
        assert command in SKILL, f"la skill no menciona `{command}`"


def test_build_and_watch_are_documented_where_they_are_used():
    for command in ("okf-wiki build", "okf-wiki watch"):
        assert command in README, f"el README no documenta `{command}`"
    assert "okf-wiki build" in SKILL, "la skill tiene que reconstruir el sitio"


def test_docs_call_the_context_argument_the_instance_not_the_bundle():
    """`context` trabaja con la raíz de la instancia: documentarlo como `<bundle>`
    invitaba justo al error que sale con exit 3."""
    assert "okf-wiki context <bundle>" not in SKILL
    assert "okf-wiki context <bundle>" not in README
    assert "okf-wiki context <instancia>" in SKILL
    assert "okf-wiki context <instancia>" in README
    assert "instancia" in _subparser_help("context")


def test_every_template_name_is_documented():
    from okf_wiki import templates

    for name in templates.TEMPLATES:
        assert f"`{name}`" in README or f"{name}\\|" in README or f"\\|{name}" in README, name


def test_the_site_output_directory_is_named_the_same_everywhere():
    from okf_wiki import site

    assert f"{site.SITE_DIRNAME}/" in README
    assert f"{site.SITE_DIRNAME}/" in SKILL
    assert site.MANIFEST_FILENAME in README
