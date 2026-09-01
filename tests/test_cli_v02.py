"""CLI `okf-wiki`: fecha UTC real, `init` seguro, sello `okf_version` y errores claros.

Nada de aquí necesita el motor OKF instalado (las pruebas que sí lo necesitan
viven en `test_engine_integration.py`).
"""
from __future__ import annotations

import importlib
import io
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from okf_wiki import __version__, cli

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} no empieza por frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


def run(argv: list[str]) -> int:
    return cli.main(argv)


# --------------------------------------------------------------------------- #
# Versión: la del paquete es la que se firma en `generated.by`                 #
# --------------------------------------------------------------------------- #
def test_package_version_is_the_v02_release():
    assert __version__ == "0.2.0"


def test_producer_is_exactly_the_package_version():
    """`generated.by` no puede anunciar una versión que no es la del paquete."""
    assert cli._producer() == "okf-wiki/0.2.0"
    assert cli._producer() == f"okf-wiki/{__version__}"


def test_producer_ignores_stale_installed_metadata(monkeypatch):
    """Un editable install con dist-info viejo no debe firmar las páginas."""
    import importlib.metadata as md

    monkeypatch.setattr(md, "version", lambda _name: "0.0.1-vieja")
    assert cli._producer() == f"okf-wiki/{__version__}"


def test_producer_is_what_init_writes_in_generated_by(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert run(["init", str(inst), "--no-git"]) == 0
    capsys.readouterr()
    assert frontmatter(inst / "wiki" / "log.md")["generated"]["by"] == "okf-wiki/0.2.0"


def test_version_flag_reports_package_and_format(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"okf-wiki {__version__} (Open Knowledge Format v{cli.OKF_VERSION})"


# --------------------------------------------------------------------------- #
# Fecha UTC real                                                              #
# --------------------------------------------------------------------------- #
def test_iso_utc_is_now_in_utc():
    stamp = cli.iso_utc()
    assert ISO_Z.match(stamp), stamp
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs(parsed - datetime.now(timezone.utc)) < timedelta(minutes=5)


def test_now_command_prints_iso_utc(capsys):
    assert run(["now"]) == 0
    assert ISO_Z.match(capsys.readouterr().out.strip())


def test_now_date_command_prints_utc_date(capsys):
    assert run(["now", "--date"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_now_json_carries_actor_and_okf_version(capsys):
    assert run(["now", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert ISO_Z.match(payload["at"])
    assert payload["date"] == payload["at"][:10]
    assert re.match(r"^okf-wiki/\d+\.\d+", payload["generated_by"]), payload["generated_by"]
    assert payload["okf_version"] == "0.2"


# --------------------------------------------------------------------------- #
# init: stubs OKF v0.2                                                         #
# --------------------------------------------------------------------------- #
def test_init_creates_instance_layout(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert run(["init", str(inst), "--name", "Mi Wiki", "--no-git"]) == 0
    capsys.readouterr()
    assert (inst / "sources").is_dir()
    assert (inst / "wiki" / "log.md").is_file()
    assert (inst / "wiki" / "index.md").is_file()
    assert (inst / "sources" / ".ingest-state.json").is_file()
    assert (inst / ".gitignore").is_file()


def test_init_log_stub_is_okf_v02(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--name", "Mi Wiki", "--no-git"])
    capsys.readouterr()
    log = inst / "wiki" / "log.md"
    fm = frontmatter(log)
    assert fm["type"] == "Log"
    assert "timestamp" not in fm, "el campo v0.1 `timestamp` no debe reaparecer"
    assert re.match(r"^okf-wiki/\d+\.\d+", fm["generated"]["by"])
    at = fm["generated"]["at"]
    at = at if isinstance(at, datetime) else datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
    assert abs(at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)) < timedelta(minutes=5)
    assert fm["status"] == "stable"
    # Cabecera de log conforme a §9: `## YYYY-MM-DD`, no `## [fecha] ingest | …`.
    body = log.read_text(encoding="utf-8")
    assert f"## {datetime.now(timezone.utc):%Y-%m-%d}" in body


def test_init_log_stub_passes_the_v02_lint(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    capsys.readouterr()
    doc = (inst / "wiki" / "log.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(doc.split("---\n", 2)[1])
    body = doc.split("---\n", 2)[2]
    assert cli._lint_document(fm, body) == []


def test_init_by_flag_sets_the_actor(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--by", "human:ana", "--no-git"])
    capsys.readouterr()
    assert frontmatter(inst / "wiki" / "log.md")["generated"]["by"] == "human:ana"


def test_init_root_index_declares_okf_version(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--name", "Mi Wiki", "--no-git"])
    capsys.readouterr()
    index = inst / "wiki" / "index.md"
    assert frontmatter(index) == {"okf_version": "0.2"}
    assert "* [Registro de Mi Wiki](log.md)" in index.read_text(encoding="utf-8")


def test_init_is_idempotent(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    before = (inst / "wiki" / "log.md").read_text(encoding="utf-8")
    (inst / "wiki" / "ventas").mkdir()
    assert run(["init", str(inst), "--no-git"]) == 0
    capsys.readouterr()
    assert (inst / "wiki" / "log.md").read_text(encoding="utf-8") == before
    assert (inst / "wiki" / "ventas").is_dir()


# --------------------------------------------------------------------------- #
# init: git seguro                                                             #
# --------------------------------------------------------------------------- #
def test_init_creates_a_git_repo_when_standalone(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert run(["init", str(inst)]) == 0
    out = capsys.readouterr().out
    assert (inst / ".git").is_dir()
    assert "repo git inicializado" in out
    # Sin commits: la revisión la decide la persona.
    log = subprocess.run(["git", "-C", str(inst), "log", "--oneline"],
                         capture_output=True, text=True)
    assert log.returncode != 0 or log.stdout.strip() == ""


def test_init_does_not_nest_a_repo_inside_another(tmp_path, capsys):
    parent = tmp_path / "monorepo"
    parent.mkdir()
    subprocess.run(["git", "-C", str(parent), "init", "-q"], check=True)
    inst = parent / "wikis" / "mi_wiki"
    assert run(["init", str(inst)]) == 0
    out = capsys.readouterr().out
    assert not (inst / ".git").exists(), "no se debe anidar un repo dentro de otro"
    assert "dentro del repo" in out


def test_init_leaves_an_existing_repo_untouched(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    inst.mkdir()
    subprocess.run(["git", "-C", str(inst), "init", "-q"], check=True)
    head_before = (inst / ".git" / "HEAD").read_text(encoding="utf-8")
    assert run(["init", str(inst)]) == 0
    assert "ya hay un repo git" in capsys.readouterr().out
    assert (inst / ".git" / "HEAD").read_text(encoding="utf-8") == head_before


def test_init_no_git_flag_skips_git(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    assert run(["init", str(inst), "--no-git"]) == 0
    assert "git omitido" in capsys.readouterr().out
    assert not (inst / ".git").exists()


def test_init_survives_git_missing_from_path(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    inst = tmp_path / "mi_wiki"
    assert run(["init", str(inst)]) == 0
    assert "git no está en el PATH" in capsys.readouterr().out
    assert (inst / "wiki" / "log.md").is_file()


# --------------------------------------------------------------------------- #
# Sello `okf_version` en el índice raíz                                        #
# --------------------------------------------------------------------------- #
def test_stamp_okf_version_adds_frontmatter(tmp_path):
    bundle = tmp_path / "wiki"
    bundle.mkdir()
    (bundle / "index.md").write_text("# Ventas\n\n* [Plan](ventas/plan.md) - x\n", encoding="utf-8")
    stamped = cli.stamp_okf_version(bundle)
    text = stamped.read_text(encoding="utf-8")
    assert text.startswith('---\nokf_version: "0.2"\n---\n\n# Ventas\n')


def test_stamp_okf_version_is_idempotent(tmp_path):
    bundle = tmp_path / "wiki"
    bundle.mkdir()
    (bundle / "index.md").write_text("# Ventas\n\n* [Plan](ventas/plan.md) - x\n", encoding="utf-8")
    once = cli.stamp_okf_version(bundle).read_text(encoding="utf-8")
    twice = cli.stamp_okf_version(bundle).read_text(encoding="utf-8")
    assert once == twice


def test_stamp_okf_version_replaces_a_stale_declaration(tmp_path):
    bundle = tmp_path / "wiki"
    bundle.mkdir()
    (bundle / "index.md").write_text(
        '---\nokf_version: "0.1"\n---\n\n# Ventas\n', encoding="utf-8")
    text = cli.stamp_okf_version(bundle).read_text(encoding="utf-8")
    assert text == '---\nokf_version: "0.2"\n---\n\n# Ventas\n'


def test_stamp_okf_version_without_index_is_a_noop(tmp_path):
    bundle = tmp_path / "wiki"
    bundle.mkdir()
    assert cli.stamp_okf_version(bundle) is None


# --------------------------------------------------------------------------- #
# Error accionable cuando falta el motor OKF                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
def engine_missing(monkeypatch):
    real = importlib.import_module

    def fake(name, package=None):
        if name.split(".")[0] == "reference_agent":
            raise ImportError(f"No module named {name!r}")
        return real(name, package)

    monkeypatch.setattr(cli.importlib, "import_module", fake)


@pytest.mark.parametrize("cmd", ["index", "viz", "verify"])
def test_engine_commands_fail_with_setup_instructions(cmd, tmp_path, capsys, engine_missing):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    capsys.readouterr()
    assert run([cmd, str(inst)]) == 2
    err = capsys.readouterr().err
    assert "Falta el motor OKF" in err
    assert "scripts/setup.sh" in err
    assert "OKF_ENGINE=" in err


def test_engine_free_commands_still_work_without_the_engine(tmp_path, capsys, engine_missing):
    inst = tmp_path / "mi_wiki"
    assert run(["init", str(inst), "--no-git"]) == 0
    assert run(["now"]) == 0
    assert run(["scan", str(inst)]) == 0
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# Errores de ingesta: mensaje accionable, código propio y cero tracebacks      #
# --------------------------------------------------------------------------- #
def corrupt_manifest(instance: Path) -> Path:
    """Deja el manifest de la instancia ilegible (JSON truncado)."""
    state = instance / "sources" / ".ingest-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{ truncado", encoding="utf-8")
    return state


def test_exit_codes_are_distinct():
    """La skill distingue los fallos por código: no pueden solaparse."""
    codes = [cli.EXIT_OK, cli.EXIT_INVALID, cli.EXIT_ENGINE_MISSING,
             cli.EXIT_INGEST, cli.EXIT_BAD_INPUT]
    assert codes == [0, 1, 2, 3, 4]
    assert len(set(codes)) == len(codes)


def test_scan_on_a_missing_instance_explains_itself(tmp_path, capsys):
    assert run(["scan", str(tmp_path / "no-existe")]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    assert captured.out == ""  # nada de JSON: un JSON vacío se leería como "todo al día"
    assert "Traceback" not in captured.err
    assert "no-existe" in captured.err
    assert "okf-wiki init" in captured.err


def test_scan_without_a_dropzone_points_at_init(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    inst.mkdir()
    assert run(["scan", str(inst)]) == cli.EXIT_INGEST
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "sources" in err and "okf-wiki init" in err


def test_scan_with_a_corrupt_manifest_refuses_to_reingest(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    capsys.readouterr()
    corrupt_manifest(inst)
    assert run(["scan", str(inst)]) == cli.EXIT_INGEST
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert ".ingest-state.json" in captured.err
    assert "duplicar conceptos" in captured.err  # explica el riesgo de borrarlo


def test_commit_state_with_a_corrupt_manifest_fails_cleanly(tmp_path, capsys, monkeypatch):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    capsys.readouterr()
    state = corrupt_manifest(inst)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(
        {"new": [], "changed": [], "unchanged": [], "deleted": []})))
    assert run(["commit-state", str(inst)]) == cli.EXIT_INGEST
    assert "Traceback" not in capsys.readouterr().err
    # Y no lo ha sobrescrito: el operador decide si repararlo o borrarlo.
    assert state.read_text(encoding="utf-8") == "{ truncado"


def test_init_over_a_corrupt_manifest_fails_cleanly(tmp_path, capsys):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    capsys.readouterr()
    corrupt_manifest(inst)
    assert run(["init", str(inst), "--no-git"]) == cli.EXIT_INGEST
    assert "Traceback" not in capsys.readouterr().err


def test_commit_state_with_invalid_stdin_json_explains_the_pipe(tmp_path, capsys, monkeypatch):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    capsys.readouterr()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("no soy json"))
    assert run(["commit-state", str(inst)]) == cli.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "okf-wiki scan" in err


def test_commit_state_rejects_json_that_is_not_an_object(tmp_path, capsys, monkeypatch):
    inst = tmp_path / "mi_wiki"
    run(["init", str(inst), "--no-git"])
    capsys.readouterr()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("[1, 2, 3]"))
    assert run(["commit-state", str(inst)]) == cli.EXIT_BAD_INPUT
    assert "Traceback" not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Entradas ilegibles en los extractores: tampoco tracebacks                    #
# --------------------------------------------------------------------------- #
def test_office2text_on_a_non_zip_file_reports_the_mismatch(tmp_path, capsys):
    fake = tmp_path / "informe.docx"
    fake.write_text("esto es texto plano, no un docx\n", encoding="utf-8")
    assert run(["office2text", str(fake)]) == cli.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "informe.docx" in err


def test_office2text_on_an_unsupported_extension_is_explicit(tmp_path, capsys):
    import zipfile as zf

    odt = tmp_path / "nota.odt"
    with zf.ZipFile(odt, "w") as z:
        z.writestr("content.xml", "<x/>")
    assert run(["office2text", str(odt)]) == cli.EXIT_BAD_INPUT
    assert "Traceback" not in capsys.readouterr().err


def test_office2text_on_a_missing_file_says_so(tmp_path, capsys):
    assert run(["office2text", str(tmp_path / "no-existe.xlsx")]) == cli.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "no-existe.xlsx" in err


def test_html2text_on_a_missing_file_says_so(tmp_path, capsys):
    assert run(["html2text", str(tmp_path / "no-existe.html")]) == cli.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "no-existe.html" in err
