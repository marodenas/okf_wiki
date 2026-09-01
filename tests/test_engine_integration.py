"""Integración con el motor OKF fijado: índices, sello v0.2, verify y visor.

Se salta si `reference_agent` no está instalado (ejecuta `bash scripts/setup.sh`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("reference_agent", reason="motor OKF no instalado: bash scripts/setup.sh")

from okf_wiki import cli  # noqa: E402

PAGE = """\
---
type: {type}
title: "{title}"
description: "{desc}"
tags: [a, b]
status: stable
generated: {{ by: claude-code/opus-5, at: 2026-08-06T10:00:00Z }}
{extra}---

{body}
"""


def write_page(path: Path, *, type="Ventas", title="Plan", desc="Un plan.",
               extra="", body="Resumen de la página.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        PAGE.format(type=type, title=title, desc=desc, extra=extra, body=body),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def instance(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert cli.main(["init", str(inst), "--name", "Mi Wiki", "--no-git"]) == 0
    capsys.readouterr()
    return inst


def frontmatter_of(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    return yaml.safe_load(text.split("---\n", 2)[1])


# --------------------------------------------------------------------------- #
# index: el sello okf_version sobrevive a la regeneración del motor            #
# --------------------------------------------------------------------------- #
def test_index_stamps_only_the_root_index(instance, capsys):
    write_page(instance / "wiki" / "ventas" / "plan.md")
    write_page(instance / "wiki" / "producto" / "roadmap.md", type="Producto",
               title="Roadmap", desc="El roadmap.")
    assert cli.main(["index", str(instance)]) == 0
    out = capsys.readouterr().out
    assert "okf_version" in out

    wiki = instance / "wiki"
    assert frontmatter_of(wiki / "index.md") == {"okf_version": "0.2"}
    # OKF v0.2 §8: el índice raíz es el único `index.md` que puede llevar frontmatter.
    assert frontmatter_of(wiki / "ventas" / "index.md") is None
    assert frontmatter_of(wiki / "producto" / "index.md") is None
    # Y el cuerpo generado por el motor sigue ahí.
    assert "ventas/index.md" in (wiki / "index.md").read_text(encoding="utf-8")


def test_index_is_repeatable(instance, capsys):
    write_page(instance / "wiki" / "ventas" / "plan.md")
    cli.main(["index", str(instance)])
    once = (instance / "wiki" / "index.md").read_text(encoding="utf-8")
    cli.main(["index", str(instance)])
    capsys.readouterr()
    assert (instance / "wiki" / "index.md").read_text(encoding="utf-8") == once


# --------------------------------------------------------------------------- #
# verify: errores del motor + avisos v0.2                                      #
# --------------------------------------------------------------------------- #
def verify(argv: list[str], capsys) -> tuple[int, dict]:
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_verify_accepts_a_v02_bundle(instance, capsys):
    write_page(
        instance / "wiki" / "ventas" / "plan.md",
        extra=('sources:\n'
               '  - id: informe-q2\n'
               '    resource: sources/informe-q2.pdf\n'
               '    title: Informe Q2\n'
               'verified: { by: human:ana, at: 2026-08-06T11:00:00Z }\n'
               'stale_after: 2027-01-01\n'),
        body="El churn bajó al 3%.[^informe-q2]\n\n[^informe-q2]: informe-q2.pdf, p.4",
    )
    code, result = verify(["verify", str(instance)], capsys)
    assert code == 0
    assert result["ok"] is True
    assert result["okf_version"] == "0.2"
    assert result["warnings"] == []


def test_verify_warns_about_v01_frontmatter_without_failing(instance, capsys):
    write_page(instance / "wiki" / "ventas" / "plan.md",
               extra="timestamp: 2026-07-15T00:00:00Z\n")
    code, result = verify(["verify", str(instance)], capsys)
    assert code == 0, "un bundle v0.1 sigue siendo consumible (§11)"
    assert result["ok"] is True
    paths = {w["path"] for w in result["warnings"]}
    assert paths == {"ventas/plan.md"}
    assert any("timestamp" in w["warning"] for w in result["warnings"])


def test_verify_strict_fails_on_warnings(instance, capsys):
    write_page(instance / "wiki" / "ventas" / "plan.md",
               extra="timestamp: 2026-07-15T00:00:00Z\n")
    code, result = verify(["verify", str(instance), "--strict"], capsys)
    assert code == 1
    assert result["ok"] is True and result["warnings"]


def test_verify_no_lint_skips_the_v02_warnings(instance, capsys):
    write_page(instance / "wiki" / "ventas" / "plan.md",
               extra="timestamp: 2026-07-15T00:00:00Z\n")
    code, result = verify(["verify", str(instance), "--no-lint"], capsys)
    assert code == 0
    assert result["warnings"] == []


def test_verify_fails_on_a_document_without_type(instance, capsys):
    (instance / "wiki" / "ventas").mkdir(parents=True)
    (instance / "wiki" / "ventas" / "roto.md").write_text(
        '---\ntitle: "Sin tipo"\n---\n\nCuerpo.\n', encoding="utf-8")
    code, result = verify(["verify", str(instance)], capsys)
    assert code == 1
    assert result["ok"] is False
    assert result["errors"][0]["path"] == "ventas/roto.md"


@pytest.mark.parametrize("name", ['Wiki "Ventas"', "Ventas: 2026", "a, b", "Wiki [beta]"])
def test_verify_accepts_a_bundle_created_with_a_hostile_name(name, tmp_path, capsys):
    """M2 de punta a punta: el `log.md` que escribe `init` tiene que validar.

    Con las comillas interpoladas a mano, `--name 'Wiki "Ventas"'` producía un
    frontmatter que ni el motor podía parsear: la instancia nacía inválida.
    """
    inst = tmp_path / "hostil"
    assert cli.main(["init", str(inst), "--name", name, "--no-git"]) == 0
    capsys.readouterr()
    code, result = verify(["verify", str(inst), "--strict"], capsys)
    assert code == 0, result
    assert result["ok"] is True and result["warnings"] == []


def test_verify_ignores_the_sources_dropzone(instance, capsys):
    (instance / "sources").mkdir(exist_ok=True)
    (instance / "sources" / "nota.md").write_text("sin frontmatter\n", encoding="utf-8")
    code, result = verify(["verify", str(instance)], capsys)
    assert code == 0 and result["ok"] is True


# --------------------------------------------------------------------------- #
# viz: el visor solo dibuja aristas con enlaces relativos                      #
# --------------------------------------------------------------------------- #
def test_viz_draws_edges_from_relative_links_only(instance, capsys):
    write_page(instance / "wiki" / "ventas" / "plan.md",
               body="Ver [Roadmap](../producto/roadmap.md).")
    write_page(instance / "wiki" / "producto" / "roadmap.md", type="Producto",
               title="Roadmap", desc="El roadmap.",
               body="Ver [Plan](/ventas/plan.md) (absoluto: el visor lo ignora).")
    assert cli.main(["viz", str(instance), "--name", "Mi Wiki"]) == 0
    out = capsys.readouterr().out
    edges = int(re.search(r"(\d+) aristas", out).group(1))
    assert edges == 1, out
    assert (instance / "wiki" / "viz.html").is_file()


# --------------------------------------------------------------------------- #
# Acoplamiento a la API interna del motor: si desaparece, que falle aquí        #
# --------------------------------------------------------------------------- #
def test_private_engine_api_used_by_index_and_viz_still_exists():
    """`cmd_index`/`cmd_viz` usan internos de `reference_agent` (sin `_`-guard alguno).

    No hay alternativa pública para colorear por `type` ni para regenerar índices
    sin LLM, así que el acoplamiento se acepta — pero tiene que romperse en la
    suite al mover el pin del motor, no en la wiki de alguien.
    """
    import inspect

    from reference_agent.bundle.index import regenerate_indexes
    from reference_agent.viewer import generator

    assert callable(getattr(generator, "_walk_concepts", None))
    assert isinstance(getattr(generator, "_TYPE_PALETTE", None), dict)
    assert "synthesize" in inspect.signature(regenerate_indexes).parameters


def test_viz_surfaces_v02_trust_metadata(instance, capsys):
    write_page(
        instance / "wiki" / "ventas" / "plan.md",
        extra=('status: deprecated\n'
               'verified: { by: human:ana, at: 2026-08-06T11:00:00Z }\n'
               'stale_after: 2000-01-01\n'),
    )
    assert cli.main(["viz", str(instance)]) == 0
    capsys.readouterr()
    html = (instance / "wiki" / "viz.html").read_text(encoding="utf-8")
    assert '"trust_tier": "human-reviewed"' in html
    assert '"status": "deprecated"' in html
    assert '"stale": true' in html


# --------------------------------------------------------------------------- #
# Estructura wiki/<tema>/<subtema>/: navegación en carpetas, grafo en enlaces  #
# --------------------------------------------------------------------------- #
def build_two_topics(instance: Path) -> None:
    """Dos temas con un subtema cada uno, enlazados entre sí."""
    write_page(
        instance / "wiki" / "ventas" / "retencion" / "plan-retencion-2026.md",
        type="Ventas", title="Plan de retención 2026",
        desc="Cómo se retiene a las cuentas grandes.",
        body="Resumen.\n\nVer [Roadmap](../../producto/roadmap/roadmap-2026.md).",
    )
    write_page(
        instance / "wiki" / "producto" / "roadmap" / "roadmap-2026.md",
        type="Producto", title="Roadmap 2026", desc="Entregables comprometidos.",
        body="Resumen.\n\nVer [Plan](../../ventas/retencion/plan-retencion-2026.md).",
    )


def test_every_level_gets_a_generated_index(instance, capsys):
    """Raíz, tema y subtema: el agente no escribe ninguno de estos `index.md`."""
    build_two_topics(instance)
    assert cli.main(["index", str(instance)]) == 0
    capsys.readouterr()
    wiki = instance / "wiki"
    for level in (
        wiki / "index.md",
        wiki / "ventas" / "index.md",
        wiki / "ventas" / "retencion" / "index.md",
        wiki / "producto" / "index.md",
        wiki / "producto" / "roadmap" / "index.md",
    ):
        assert level.is_file(), level


def test_topic_index_lists_its_subtopics_and_subtopic_index_lists_its_pages(instance, capsys):
    build_two_topics(instance)
    assert cli.main(["index", str(instance)]) == 0
    capsys.readouterr()
    wiki = instance / "wiki"
    topic = (wiki / "ventas" / "index.md").read_text(encoding="utf-8")
    assert "retencion/index.md" in topic
    subtopic = (wiki / "ventas" / "retencion" / "index.md").read_text(encoding="utf-8")
    assert "plan-retencion-2026.md" in subtopic


def test_only_the_root_index_is_stamped_across_a_nested_bundle(instance, capsys):
    """OKF v0.2 §8: el sello va sólo en la raíz, también con subtemas de por medio."""
    build_two_topics(instance)
    assert cli.main(["index", str(instance)]) == 0
    capsys.readouterr()
    wiki = instance / "wiki"
    assert frontmatter_of(wiki / "index.md") == {"okf_version": "0.2"}
    for nested in (wiki / "ventas" / "index.md",
                   wiki / "ventas" / "retencion" / "index.md"):
        assert frontmatter_of(nested) is None, nested


def test_subtopic_pages_keep_the_topic_type_and_validate(instance, capsys):
    """El subtema es organización: `type` sigue siendo el del tema de primer nivel."""
    build_two_topics(instance)
    assert cli.main(["verify", str(instance)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["warnings"] == []
    page = instance / "wiki" / "ventas" / "retencion" / "plan-retencion-2026.md"
    assert frontmatter_of(page)["type"] == "Ventas"


def test_links_across_subtopics_become_graph_edges(instance, capsys):
    """El grafo es transversal a las carpetas: lo construyen los enlaces relativos."""
    build_two_topics(instance)
    assert cli.main(["viz", str(instance), "--name", "Mi Wiki"]) == 0
    out = capsys.readouterr().out
    # log.md + las dos páginas; una arista por sentido entre los dos subtemas.
    assert "3 conceptos" in out
    assert "2 aristas" in out
    # Y el color sigue siendo por tema, no por subtema.
    assert "2 tipos coloreados" in out


def test_generated_indexes_do_not_add_nodes_to_the_graph(instance, capsys):
    """Si los `index.md` contasen, cada subtema inflaría el grafo con un nodo hueco."""
    build_two_topics(instance)
    assert cli.main(["index", str(instance)]) == 0
    capsys.readouterr()
    assert cli.main(["viz", str(instance)]) == 0
    assert "3 conceptos" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# `purpose.md`/`schema.md` viven fuera del bundle y no lo contaminan            #
# --------------------------------------------------------------------------- #
def test_context_files_are_invisible_to_verify(instance, capsys):
    """Sin frontmatter, dentro de `wiki/` serían conceptos rotos. Están fuera."""
    build_two_topics(instance)
    assert (instance / "purpose.md").is_file()  # las escribió `init`
    assert cli.main(["verify", str(instance)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["checked"] == 3  # log.md + las dos páginas, y nada más
    assert result["ok"] is True


def test_context_files_are_invisible_to_index_and_viz(instance, capsys):
    build_two_topics(instance)
    assert cli.main(["index", str(instance)]) == 0
    capsys.readouterr()
    assert cli.main(["viz", str(instance)]) == 0
    assert "3 conceptos" in capsys.readouterr().out
    root_index = (instance / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "purpose" not in root_index
    assert "schema" not in root_index


def test_a_filled_page_template_validates(instance, capsys):
    """La plantilla que el agente copia tiene que producir una página OKF válida."""
    from okf_wiki import templates

    page = templates.render("page")
    for placeholder, value in (
        ("<Tema>", "Ventas"),
        ("<Título legible>", "Plan de retención 2026"),
        ("<Una frase concreta que resume la página.>", "Cómo se retiene a las cuentas."),
        ("<tag1>, <tag2>", "ventas, retencion"),
        ("<modelo>", "opus-5"),
        ("<el at de okf-wiki now --json>", "2026-08-06T10:00:00Z"),
        ("<slug-corto-y-estable>", "informe-q2"),
        ("<fichero-completo.pdf>", "informe-q2.pdf"),
        ("<Título del documento>", "Informe trimestral Q2"),
        ("<otro-tema>/<su-subtema>/<su-pagina>.md",
         "producto/roadmap/roadmap-2026.md"),
    ):
        page = page.replace(placeholder, value)

    target = instance / "wiki" / "ventas" / "retencion" / "plan.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    write_page(instance / "wiki" / "producto" / "roadmap" / "roadmap-2026.md",
               type="Producto", title="Roadmap 2026", desc="Entregables.")

    assert cli.main(["verify", str(instance), "--strict"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["warnings"] == [], result["warnings"]


# --------------------------------------------------------------------------- #
# H2 — la taxonomía temática, de punta a punta: avisa sin romper lo permisivo   #
# --------------------------------------------------------------------------- #
def test_a_type_replicating_the_subtopic_warns_but_still_validates(instance, capsys):
    """OKF no invalida esta página (§11): sale por `warnings`, con exit 0."""
    write_page(instance / "wiki" / "ventas" / "retencion" / "plan.md", type="Retencion")
    code, result = verify(["verify", str(instance)], capsys)
    assert code == 0, "el formato es permisivo con la estructura: no puede fallar aquí"
    assert result["ok"] is True
    avisos = [w for w in result["warnings"] if w["path"] == "ventas/retencion/plan.md"]
    assert avisos, result["warnings"]
    assert any("replica el subtema" in w["warning"] for w in avisos)
    # El aviso trae la evidencia: qué `type` hay, qué carpeta lo replica y cuál manda.
    assert any("`type: Retencion`" in w["warning"] and "`ventas/`" in w["warning"]
               for w in avisos)


def test_the_same_bundle_fails_under_strict(instance, capsys):
    """`--strict` es la comprobación final de la ingesta: ahí sí es un fallo."""
    write_page(instance / "wiki" / "ventas" / "retencion" / "plan.md", type="Retencion")
    code, result = verify(["verify", str(instance), "--strict"], capsys)
    assert code == 1
    assert result["ok"] is True and result["warnings"]


def test_entity_and_concept_folders_warn_with_their_path(instance, capsys):
    write_page(instance / "wiki" / "conceptos" / "margen-bruto.md", type="Concepto")
    code, result = verify(["verify", str(instance)], capsys)
    assert code == 0
    avisos = [w["warning"] for w in result["warnings"]
              if w["path"] == "conceptos/margen-bruto.md"]
    assert any("`conceptos/`" in w for w in avisos), avisos
    assert any("`type: Concepto`" in w for w in avisos), avisos
    assert cli.main(["verify", str(instance), "--strict"]) == 1
    capsys.readouterr()


def test_a_third_folder_level_warns_with_the_full_path(instance, capsys):
    write_page(instance / "wiki" / "ventas" / "retencion" / "grandes" / "acme.md")
    code, result = verify(["verify", str(instance)], capsys)
    assert code == 0
    avisos = [w["warning"] for w in result["warnings"]
              if w["path"] == "ventas/retencion/grandes/acme.md"]
    assert any("`ventas/retencion/grandes/`" in w for w in avisos), avisos


def test_no_lint_silences_the_taxonomy_warnings_too(instance, capsys):
    """`--no-lint` es "solo el motor": no puede colar avisos de taxonomía."""
    write_page(instance / "wiki" / "conceptos" / "margen-bruto.md", type="Concepto")
    code, result = verify(["verify", str(instance), "--no-lint"], capsys)
    assert code == 0
    assert result["warnings"] == []
    # Y en estricto, sin lint, tampoco falla: no hay avisos que convertir en fallo.
    assert cli.main(["verify", str(instance), "--strict", "--no-lint"]) == 0
    capsys.readouterr()


def test_a_conformant_bundle_stays_clean_under_strict(instance, capsys):
    """La compatibilidad que hay que preservar: lo correcto sigue pasando estricto."""
    build_two_topics(instance)
    write_page(instance / "wiki" / "ventas" / "plan-general.md")  # tema sin subtema
    assert cli.main(["verify", str(instance), "--strict"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["warnings"] == [], result["warnings"]


# --------------------------------------------------------------------------- #
# H3 — `purpose.md`/`schema.md` fuera de verify y del lint, también en plano    #
# --------------------------------------------------------------------------- #
def test_a_flat_bundle_ignores_the_context_files(tmp_path, capsys):
    """Sin subcarpeta `wiki/`, el contexto cae DENTRO del recorrido del bundle.

    Sin la exclusión explícita, `verify` reportaba dos errores de documento sin
    frontmatter por dos ficheros que a propósito no son páginas OKF.
    """
    flat = tmp_path / "bundle_plano"
    write_page(flat / "ventas" / "plan.md")
    for name in ("purpose.md", "schema.md"):
        (flat / name).write_text(f"# {name}\n\nSin frontmatter, a propósito.\n",
                                 encoding="utf-8")
    code, result = verify(["verify", str(flat)], capsys)
    assert code == 0, result
    assert result["ok"] is True
    assert result["checked"] == 1  # solo `ventas/plan.md`
    assert result["warnings"] == []
    assert cli.main(["verify", str(flat), "--strict"]) == 0
    capsys.readouterr()


def test_a_flat_bundle_with_only_context_files_is_not_a_bundle(tmp_path, capsys):
    """Excluidos del recorrido, no pueden hacer pasar por bundle una carpeta sin páginas."""
    solo_contexto = tmp_path / "solo_contexto"
    solo_contexto.mkdir()
    for name in ("purpose.md", "schema.md"):
        (solo_contexto / name).write_text("# contexto\n", encoding="utf-8")
    assert cli.main(["verify", str(solo_contexto)]) == cli.EXIT_INGEST
    assert capsys.readouterr().out == ""


def test_a_context_file_inside_a_topic_folder_is_still_a_page(tmp_path, capsys):
    """La exclusión es de la RAÍZ del bundle: un `schema.md` dentro de un tema no es contexto.

    Si se excluyera por nombre a cualquier profundidad, una página legítima llamada
    `schema.md` dejaría de validarse en silencio.
    """
    flat = tmp_path / "plano"
    write_page(flat / "ventas" / "plan.md")
    (flat / "ventas" / "schema.md").write_text("sin frontmatter\n", encoding="utf-8")
    code, result = verify(["verify", str(flat)], capsys)
    assert code == 1
    assert [e["path"] for e in result["errors"]] == ["ventas/schema.md"]
