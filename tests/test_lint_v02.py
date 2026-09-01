"""Avisos de conformidad OKF v0.2 (`okf-wiki verify`), a nivel de documento.

Estas pruebas no necesitan el motor OKF: ejercen `_lint_document` con el
frontmatter ya parseado —donde vive la regla de migración v0.1 → v0.2— y
`_lint_taxonomy` con la ruta relativa al bundle, donde viven las reglas de
taxonomía temática (tema/subtema, `type` del tema, nada de entidades/conceptos).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from okf_wiki.cli import _lint_document, _lint_taxonomy

CONFORMANT_FM = {
    "type": "Ventas",
    "title": "Plan de retención",
    "description": "Cómo se retiene a las cuentas grandes.",
    "tags": ["ventas", "retencion"],
    "status": "stable",
    "generated": {"by": "claude-code/opus-5", "at": "2026-08-06T10:00:00Z"},
    "sources": [{"id": "informe-q2", "resource": "sources/informe-q2.pdf",
                 "title": "Informe Q2"}],
}
CONFORMANT_BODY = (
    "Resumen.\n\nEl churn bajó al 3%.[^informe-q2]\n\n"
    "Ver [otra página](../producto/roadmap.md).\n\n[^informe-q2]: informe-q2.pdf, p.4\n"
)


def warnings_for(fm: dict, body: str = "") -> list[str]:
    return _lint_document(fm, body)


def test_conformant_document_has_no_warnings():
    assert warnings_for(CONFORMANT_FM, CONFORMANT_BODY) == []


def test_pyyaml_native_dates_are_accepted():
    """PyYAML resuelve `at:`/`stale_after:` sin comillas a datetime/date."""
    fm = dict(CONFORMANT_FM)
    fm["generated"] = {"by": "claude-code/opus-5",
                       "at": datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)}
    fm["stale_after"] = date(2027, 1, 1)
    assert warnings_for(fm, CONFORMANT_BODY) == []


def test_legacy_timestamp_is_flagged():
    fm = dict(CONFORMANT_FM, timestamp="2026-08-06T10:00:00Z")
    assert any("`timestamp`" in w and "generated" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_missing_generated_is_flagged():
    fm = {k: v for k, v in CONFORMANT_FM.items() if k != "generated"}
    assert any("sin `generated`" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_generated_without_actor_is_flagged():
    fm = dict(CONFORMANT_FM, generated={"at": "2026-08-06T10:00:00Z"})
    assert any("`generated.by`" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_generated_at_must_be_iso8601():
    fm = dict(CONFORMANT_FM, generated={"by": "claude-code/opus-5", "at": "ayer"})
    assert any("no es ISO 8601" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_legacy_citations_section_is_flagged():
    body = CONFORMANT_BODY + "\n# Citations\n- informe-q2.pdf\n"
    assert any("Citations" in w for w in warnings_for(CONFORMANT_FM, body))


def test_bare_verified_mapping_is_accepted_and_list_too():
    bare = dict(CONFORMANT_FM, verified={"by": "human:ana", "at": "2026-08-06T11:00:00Z"})
    listed = dict(CONFORMANT_FM,
                  verified=[{"by": "human:ana", "at": "2026-08-06T11:00:00Z"},
                            {"by": "process:nightly", "at": "2026-08-06T12:00:00Z"}])
    assert warnings_for(bare, CONFORMANT_BODY) == []
    assert warnings_for(listed, CONFORMANT_BODY) == []


def test_verified_entry_without_actor_is_flagged():
    fm = dict(CONFORMANT_FM, verified=[{"at": "2026-08-06T11:00:00Z"}])
    assert any("`verified` sin `by`" in w for w in warnings_for(fm, CONFORMANT_BODY))


@pytest.mark.parametrize("status", ["stable", "draft", "deprecated"])
def test_known_status_values_pass(status):
    assert warnings_for(dict(CONFORMANT_FM, status=status), CONFORMANT_BODY) == []


def test_unknown_status_is_flagged():
    fm = dict(CONFORMANT_FM, status="wip")
    assert any("`status` desconocido" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_stale_after_must_be_a_date():
    fm = dict(CONFORMANT_FM, stale_after="dentro de 3 meses")
    assert any("`stale_after`" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_source_without_resource_is_flagged():
    fm = dict(CONFORMANT_FM, sources=[{"id": "informe-q2", "title": "Informe Q2"}])
    assert any("sin `resource`" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_footnote_label_must_match_a_source_id():
    body = "El churn bajó.[^otro-informe]\n\n[^otro-informe]: otro.pdf\n"
    assert any("[^otro-informe]" in w for w in warnings_for(CONFORMANT_FM, body))


def test_footnotes_without_sources_are_flagged():
    fm = {k: v for k, v in CONFORMANT_FM.items() if k != "sources"}
    assert any("no hay `sources`" in w for w in warnings_for(fm, CONFORMANT_BODY))


def test_missing_title_or_description_is_flagged():
    fm = {k: v for k, v in CONFORMANT_FM.items() if k not in ("title", "description")}
    got = warnings_for(fm, CONFORMANT_BODY)
    assert any("falta `title`" in w for w in got)
    assert any("falta `description`" in w for w in got)


def test_absolute_and_url_md_links_are_flagged():
    """El visor solo dibuja aristas con rutas relativas; avisamos del resto."""
    body = ("Ver [abs](/ventas/plan.md) y [url](https://ejemplo.com/ventas/plan.md).\n"
            "También [ok](../ventas/plan.md).\n")
    got = warnings_for(CONFORMANT_FM, body)
    assert any("/ventas/plan.md" in w and "relativas" in w for w in got)
    assert any("https://ejemplo.com/ventas/plan.md" in w for w in got)
    assert not any("../ventas/plan.md" in w for w in got)


# --------------------------------------------------------------------------- #
# H2 — taxonomía temática: avisos con la ruta y la evidencia, nunca errores     #
# --------------------------------------------------------------------------- #
# `_lint_taxonomy` compara el `type` contra las CARPETAS, así que recibe la ruta
# relativa al bundle además del frontmatter. Va aparte de `_lint_document` por eso.
def taxonomy_for(rel: str, fm: dict | None = None) -> list[str]:
    return _lint_taxonomy(Path(rel), CONFORMANT_FM if fm is None else fm)


def test_the_prescribed_layout_produces_no_taxonomy_warnings():
    """`wiki/<tema>/<subtema>/pagina.md` con el `type` del tema: nada que avisar."""
    assert taxonomy_for("ventas/plan.md") == []
    assert taxonomy_for("ventas/retencion/plan.md") == []


def test_a_root_page_like_the_log_has_no_topic_and_is_left_alone():
    """`log.md` vive en la raíz: no tiene tema, y `type: Log` no es un tipo de cosa."""
    assert taxonomy_for("log.md", {"type": "Log"}) == []


def test_a_type_that_replicates_the_subtopic_is_flagged_with_the_evidence():
    got = taxonomy_for("ventas/retencion/plan.md", dict(CONFORMANT_FM, type="Retencion"))
    assert len(got) == 1
    warning = got[0]
    assert "`type: Retencion`" in warning        # el valor que hay que cambiar
    assert "`retencion/`" in warning             # la carpeta que replica
    assert "`ventas/`" in warning                # el tema que manda
    assert "`type: Ventas`" in warning           # y el valor correcto


def test_the_type_of_the_topic_folder_is_accepted_whatever_the_accents_or_case():
    """`Retención`/`retencion` es el mismo nombre: comparar en crudo daría falsos avisos."""
    assert taxonomy_for("retencion/plan.md", dict(CONFORMANT_FM, type="Retención")) == []
    assert taxonomy_for("ventas/plan.md", dict(CONFORMANT_FM, type="VENTAS")) == []


def test_a_type_that_replicates_a_deep_subtopic_is_flagged_with_accents():
    got = taxonomy_for("ventas/retencion/plan.md", dict(CONFORMANT_FM, type="Retención"))
    assert len(got) == 1 and "replica el subtema" in got[0]


def test_a_type_that_matches_no_folder_is_not_flagged():
    """OKF es permisivo: un `type` libre no es la violación que describe el hallazgo.

    Lo que se avisa es el `type` que REPLICA una carpeta de subtema, porque ése es
    el error concreto que fragmenta la leyenda del grafo.
    """
    assert taxonomy_for("ventas/retencion/plan.md", dict(CONFORMANT_FM, type="Negocio")) == []


@pytest.mark.parametrize("folder", ["entidades", "conceptos", "personas",
                                    "definiciones", "glosario", "Conceptos"])
def test_thing_type_folders_are_flagged(folder):
    got = taxonomy_for(f"{folder}/cuenta-acme.md", dict(CONFORMANT_FM, type="Ventas"))
    assert any(f"`{folder}/`" in w and "tipo de cosa" in w for w in got), got


@pytest.mark.parametrize("folder", ["entidades", "conceptos"])
def test_thing_type_folders_are_flagged_also_as_subtopics(folder):
    got = taxonomy_for(f"ventas/{folder}/cuenta-acme.md")
    assert any(f"`{folder}/`" in w for w in got), got


@pytest.mark.parametrize("value", ["Concepto", "Entidad", "Persona", "Definición",
                                   "Glosario", "concepto"])
def test_thing_type_values_are_flagged(value):
    got = taxonomy_for("ventas/plan.md", dict(CONFORMANT_FM, type=value))
    assert any(f"`type: {value}`" in w and "tipo de cosa" in w for w in got), got


def test_a_third_folder_level_is_flagged_with_the_path():
    got = taxonomy_for("ventas/retencion/grandes-cuentas/plan.md")
    assert len(got) == 1
    assert "`ventas/retencion/grandes-cuentas/`" in got[0]
    assert "tema/subtema" in got[0]


def test_two_levels_are_the_limit_not_a_suggestion():
    assert taxonomy_for("ventas/retencion/plan.md") == []
    assert taxonomy_for("a/b/c/d/plan.md") != []


def test_a_page_without_type_gets_no_taxonomy_warning():
    """La falta de `type` la reporta el motor como ERROR: no se duplica como aviso."""
    fm = {k: v for k, v in CONFORMANT_FM.items() if k != "type"}
    assert taxonomy_for("ventas/retencion/plan.md", fm) == []


def test_the_three_rules_stack_up_on_the_same_page():
    """Cada desvío es su propio aviso: se corrigen uno a uno."""
    got = taxonomy_for("conceptos/margen/detalle/margen-bruto.md",
                       dict(CONFORMANT_FM, type="Concepto"))
    assert len(got) == 3
    assert any("profundidad temática" in w for w in got)
    assert any("`conceptos/`" in w for w in got)
    assert any("`type: Concepto`" in w for w in got)
