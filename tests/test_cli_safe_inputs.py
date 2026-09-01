"""E2E de CLI para las entradas hostiles que `helpers` ya rechaza.

`helpers` puede levantar la excepción correcta y aun así el comando ser
inservible: quien usa esto es la skill `okf-ingest`, que lee **exit code y
stderr**, no excepciones de Python. Estas pruebas ejercen el binario de verdad
en un proceso aparte y comprueban el contrato completo de cada fallo:

- `scan` sobre una dropzone con una fuente que no se puede afirmar contenida en
  `sources/` —un enlace que se sale, uno circular, uno roto, un directorio
  intermedio enlazado a fuera o un enlace duro— → `UnsafeSourceError` → exit 3,
  nada en stdout y un mensaje accionable. Un exit 0 con un JSON de scan sería
  peor que el traceback: la skill lo tomaría por una dropzone en orden.
- `office2text` sobre un OOXML que expande de forma desproporcionada —el zip que
  declara gigabytes, o el xlsx cuya referencia de columna pide una rejilla que no
  cabe— → `OfficeTooLargeError` (un `ValueError`) → exit 4 sin traceback y **sin
  haber materializado ni el XML ni la rejilla**.
- `office2text` sobre un OOXML comprimido con BZIP2 o LZMA —los métodos que dejan
  pedir el límite entero por miembro desde unos cientos de bytes de zip, por debajo
  de todos los límites de tamaño— → `OfficeCompressionError` → exit 4 sin
  descomprimir nada.
- `office2text` sobre un zip amañado para que `zipfile` levante un `BadZipFile`
  con un mensaje de 65 KB: el detalle de una excepción ajena se cita acotado, así
  que el fichero no decide cuánto stderr se imprime.
- `office2text` sobre los dos zips que `zipfile` no rechaza sino que **avisa**
  —entradas solapadas ("possible zip bomb") y campo extra Unicode vacío— → exit 4
  en vez de un aviso de 60 KB por stderr con exit 0, y en vez de un `UserWarning`
  con traceback y exit 1 cuando corre con `PYTHONWARNINGS=error`.
- `office2text` sobre las cuatro formas de romperlo que **no** son `BadZipFile` y
  salían como traceback —flujo DEFLATE corrupto (`zlib.error`), banderas de zip
  que `zipfile` no implementa (`NotImplementedError`), miembro cifrado con
  ZipCrypto (`RuntimeError`) y fichero ilegible (`OSError`)— → exit 4, stdout
  vacío, sin traceback y con el detalle acotado.

No hacen falta ni el motor OKF ni ficheros Office reales.
"""
from __future__ import annotations

import errno
import os
import struct
import subprocess
import sys
import time
import zipfile
import zlib
from pathlib import Path

import pytest

from okf_wiki import cli, helpers

SRC = Path(__file__).resolve().parents[1] / "src"

_DOC_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def cli_subprocess(
    *argv: str, entorno: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Lanza el CLI en un proceso aparte: exit code y stderr de verdad.

    `entorno` añade variables al del proceso. Se usa para `PYTHONWARNINGS`, que
    decide qué hace Python con un aviso y por tanto puede convertir un aviso
    ignorado en una excepción que sale con traceback.
    """
    return subprocess.run(
        [sys.executable, "-m", "okf_wiki.cli", *argv],
        capture_output=True, text=True,
        env={
            **os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8",
            **(entorno or {}),
        },
    )


@pytest.fixture
def instancia(tmp_path) -> Path:
    """Instancia mínima: sólo hace falta que exista la dropzone."""
    inst = tmp_path / "mi_wiki"
    (inst / helpers.SOURCES_DIRNAME).mkdir(parents=True)
    return inst


@pytest.fixture(scope="module")
def bomba_deflate(tmp_path_factory) -> Path:
    """`.docx` de unos KB que declara un byte más de lo que admite un miembro.

    De ámbito módulo porque comprimir los `MAX_OFFICE_MEMBER_BYTES` de relleno no
    es gratis y la piden dos tests (el exit code y el texto del mensaje). Es
    determinista y sólo se lee: ninguno la modifica.
    """
    bomba = tmp_path_factory.mktemp("bombas") / "bomba.docx"
    relleno = b"a" * (helpers.MAX_OFFICE_MEMBER_BYTES + 1)
    with zipfile.ZipFile(bomba, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"<d>" + relleno + b"</d>")
    return bomba


def _dropzone(instancia: Path) -> Path:
    return instancia / helpers.SOURCES_DIRNAME


# --------------------------------------------------------------------------- #
# scan: enlaces que no se pueden ingerir → exit 3                             #
# --------------------------------------------------------------------------- #
def test_scan_con_enlace_fuera_de_la_dropzone(instancia, tmp_path):
    secreto = tmp_path / "fuera" / "secreto.md"
    secreto.parent.mkdir()
    secreto.write_text("no debería entrar en la wiki", encoding="utf-8")
    (_dropzone(instancia) / "atajo.md").symlink_to(secreto)

    done = cli_subprocess("scan", str(instancia))

    assert done.returncode == cli.EXIT_INGEST
    assert done.stdout == "", "un JSON de scan haría pasar la dropzone por sana"
    assert "Traceback" not in done.stderr
    assert "atajo.md" in done.stderr, "el mensaje debe nombrar el enlace"
    assert "secreto.md" in done.stderr, "y a dónde apunta"
    assert "Cómo seguir" in done.stderr


def test_scan_con_enlace_circular(instancia):
    """El ciclo salía como `RuntimeError`: traceback y exit 1 sin explicación."""
    src = _dropzone(instancia)
    (src / "a.md").symlink_to("b.md")
    (src / "b.md").symlink_to("a.md")

    done = cli_subprocess("scan", str(instancia))

    assert done.returncode == cli.EXIT_INGEST
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "RuntimeError" not in done.stderr
    assert "circular" in done.stderr.lower()


def test_scan_con_enlace_duro(instancia, tmp_path):
    """La otra puerta a lo de fuera: un fichero regular con un nombre más.

    No hay enlace simbólico que ver —el recorrido lo acepta como cualquier
    documento—, así que sin la comprobación de `st_nlink` el contenido de fuera
    salía en el JSON de `scan` como una fuente contenida en `sources/`.
    """
    secreto = tmp_path / "fuera" / "secreto.md"
    secreto.parent.mkdir()
    secreto.write_text("no debería entrar en la wiki", encoding="utf-8")
    os.link(secreto, _dropzone(instancia) / "doc.md")

    done = cli_subprocess("scan", str(instancia))

    assert done.returncode == cli.EXIT_INGEST
    assert done.stdout == "", "un JSON de scan haría pasar la dropzone por sana"
    assert "Traceback" not in done.stderr
    assert "doc.md" in done.stderr
    assert "enlace duro" in done.stderr
    assert "Cómo seguir" in done.stderr


def test_scan_con_enlace_interno_roto(instancia):
    """Apuntaba dentro, así que pasaba la contención… y se caía del scan mudo."""
    src = _dropzone(instancia)
    (src / "atajo.md").symlink_to(src / "no-existe.md")

    done = cli_subprocess("scan", str(instancia))

    assert done.returncode == cli.EXIT_INGEST
    assert done.stdout == ""
    assert "atajo.md" in done.stderr
    assert "roto" in done.stderr


def test_el_mensaje_de_una_fuente_insegura_dice_que_hacer(instancia, tmp_path):
    """`UnsafeSourceError` tiene su propia entrada en `_INGEST_ACTIONS`.

    Sin ella caía en el genérico "Revisa la instancia antes de reintentar", que
    no dice ni que el manifest sigue intacto ni qué hacer con el enlace.
    """
    secreto = tmp_path / "secreto.md"
    secreto.write_text("x", encoding="utf-8")
    (_dropzone(instancia) / "atajo.md").symlink_to(secreto)

    done = cli_subprocess("scan", str(instancia))

    assert done.returncode == cli.EXIT_INGEST
    assert "Revisa la instancia antes de reintentar" not in done.stderr
    assert "manifest sigue como estaba" in done.stderr
    assert "cp <origen>" in done.stderr


def test_scan_no_lee_por_un_directorio_intermedio_enlazado(instancia, tmp_path):
    """Un directorio intermedio enlazado a fuera no publica su contenido.

    Es la mitad estática del hallazgo bloqueante —la que se puede montar en
    disco—: nada de lo que hay bajo `sources/tema → /fuera` sale por stdout. La
    otra mitad es la carrera (sustituir `tema` **después** del recorrido), que
    necesita interponerse en el proceso y vive en
    `test_helpers.ScanDirectorioIntermedioTest`.
    """
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    (fuera / "doc.md").write_text("SECRETO DE FUERA", encoding="utf-8")
    (_dropzone(instancia) / "tema").symlink_to(fuera, target_is_directory=True)

    done = cli_subprocess("scan", str(instancia))

    assert done.returncode == cli.EXIT_INGEST
    assert done.stdout == ""
    assert "tema" in done.stderr
    assert "Traceback" not in done.stderr


def test_scan_normal_sigue_dando_json_por_stdout(instancia):
    """Control: sin enlaces raros el comando responde como siempre."""
    (_dropzone(instancia) / "doc.md").write_text("uno", encoding="utf-8")

    done = cli_subprocess("scan", str(instancia))

    assert done.returncode == 0
    assert '"doc.md"' in done.stdout
    assert done.stderr == ""


# --------------------------------------------------------------------------- #
# office2text: expansión desproporcionada → exit 4                            #
# --------------------------------------------------------------------------- #
def test_office2text_rechaza_una_expansion_desproporcionada(bomba_deflate):
    assert bomba_deflate.stat().st_size < 1024 * 1024, "unos KB en disco, gigas al abrirlo"

    done = cli_subprocess("office2text", str(bomba_deflate))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == "", "ni un byte del XML llega a stdout"
    assert "Traceback" not in done.stderr
    assert "word/document.xml" in done.stderr
    assert "OfficeTooLargeError" in done.stderr, "el CLI nombra el tipo del fallo"


def test_office2text_rechaza_una_columna_xlsx_desproporcionada(tmp_path):
    """El xlsx hostil mínimo: una celda, una referencia de columna imposible.

    Sin techo en la rejilla el comando no falla —se queda reservando memoria
    hasta que el kernel mata el proceso—, y la skill ve un `office2text` sin exit
    code ni mensaje. Ahora sale con `4` y dice qué referencia sobra.
    """
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    hostil = tmp_path / "hostil.xlsx"
    with zipfile.ZipFile(hostil, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{ns}"><sheetData>'
            f'<row r="1"><c r="{"A" * 40}1"><v>1</v></c></row>'
            f"</sheetData></worksheet>",
        )
    assert hostil.stat().st_size < 2048, "unos cientos de bytes en disco"

    done = cli_subprocess("office2text", str(hostil))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "A" * 40 in done.stderr, "el mensaje nombra la referencia que sobra"
    assert "OfficeTooLargeError" in done.stderr


def test_un_xlsx_minimo_hostil_no_amplifica_el_stderr(tmp_path):
    """La referencia de celda la escribe el documento: no puede decidir el stderr.

    `r` es un atributo del XML, y el mensaje de rechazo lo interpolaba entero. Con
    2 MB de letras en la referencia —2,3 KB de zip, porque comprime a nada y por
    debajo de `MIN_RATIO_COMPRESSED_BYTES` no se mira el ratio— el comando escupía
    2 MB por stderr: x900 respecto al fichero. Lo paga quien lee el fallo, que es
    la skill `okf-ingest` volcando stderr en su informe, un log o la CI.

    Rechazar sigue costando lo mismo; contarlo, no. Se comprueba el contrato
    entero, porque un mensaje recortado que además pierda el exit code no sirve.
    """
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ref = "A" * (2 * 1024 * 1024) + "1"
    hostil = tmp_path / "amplificador.xlsx"
    with zipfile.ZipFile(hostil, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{ns}"><sheetData>'
            f'<row r="1"><c r="{ref}"><v>1</v></c></row>'
            f"</sheetData></worksheet>",
        )
    tamaño = hostil.stat().st_size
    assert tamaño < 8 * 1024, "unos KB en disco pedían megas de stderr"

    done = cli_subprocess("office2text", str(hostil))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == "", "ni un byte de la rejilla llega a stdout"
    assert "Traceback" not in done.stderr
    assert len(done.stderr) < 4096, f"stderr desproporcionado: {len(done.stderr)} bytes"
    assert len(done.stderr) < tamaño, "no amplifica: menos stderr que fichero de entrada"
    assert "A" * helpers.MAX_QUOTED_CHARS in done.stderr, "sigue nombrando la referencia"
    assert str(len(ref)) in done.stderr, "y dice cuántos caracteres tenía"
    assert "OfficeTooLargeError" in done.stderr


def test_office2text_rechaza_una_bomba_bzip2_diminuta(tmp_path):
    """El bypass de los dos límites de bytes: cambiar de método de compresión.

    Los topes por miembro y por ratio se apoyaban sin decirlo en que DEFLATE no
    pasa de x1032. `zipfile` descomprime también BZIP2, que no tiene ese techo, y
    con él cabe el límite entero por miembro en unos **cientos de bytes** de zip:
    pasa el tope por un byte y se salta el ratio por quedar por debajo de
    `MIN_RATIO_COMPRESSED_BYTES`. Medido cuando el tope eran 64 MB: 235 bytes de
    fichero, el comando lo descomprimía entero y salía con 0 tras un pico de 208 MB
    de RSS — x280.000 sobre el fichero de entrada. Con el tope en 128 MB pide el
    doble desde 266 bytes, que es por qué la guarda no puede depender del tamaño.
    """
    bomba = tmp_path / "bomba.docx"
    relleno = helpers.MAX_OFFICE_MEMBER_BYTES - 8
    with zipfile.ZipFile(bomba, "w", zipfile.ZIP_BZIP2) as zf:
        zf.writestr("word/document.xml", b"<d>" + b"a" * relleno + b"</d>")
    tamaño = bomba.stat().st_size
    assert tamaño < 1024, f"unos cientos de bytes en disco, no {tamaño}"

    empezó = time.monotonic()
    done = cli_subprocess("office2text", str(bomba))
    tardó = time.monotonic() - empezó

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == "", "ni un byte del XML llega a stdout"
    assert "Traceback" not in done.stderr
    assert "word/document.xml" in done.stderr, "el mensaje nombra el miembro"
    assert "BZIP2" in done.stderr, "y el método que lo hace posible"
    assert "OfficeCompressionError" in done.stderr, "el CLI nombra el tipo del fallo"
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"
    assert tardó < 10, f"rechazar 235 bytes no puede costar {tardó:.1f}s"


def test_office2text_rechaza_una_bomba_lzma_diminuta(tmp_path):
    """El otro método sin techo de ratio que `zipfile` sabe descomprimir."""
    bomba = tmp_path / "bomba.docx"
    with zipfile.ZipFile(bomba, "w", zipfile.ZIP_LZMA) as zf:
        zf.writestr("word/document.xml", b"<d>" + b"a" * (8 * 1024 * 1024) + b"</d>")

    done = cli_subprocess("office2text", str(bomba))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "LZMA" in done.stderr
    assert "OfficeCompressionError" in done.stderr


def test_office2text_no_reproduce_un_badzipfile_sin_acotar(tmp_path):
    """El mensaje de una excepción ajena también lo escribe el fichero.

    `zipfile.BadZipFile` interpola el nombre del miembro tal y como viene en la
    cabecera local, y ese campo admite 65.535 bytes: un zip amañado producía un
    mensaje de 65.600 caracteres que el CLI volcaba entero por stderr. Es el
    mismo defecto que ya se corrigió en las referencias de celda, pero por la
    puerta de una excepción que no redacta este proyecto.
    """
    hostil = tmp_path / "mentiroso.docx"
    hostil.write_bytes(_zip_con_nombre_local_falso(b"word/document.xml", b"A" * 65535))
    tamaño = hostil.stat().st_size

    done = cli_subprocess("office2text", str(hostil))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"
    assert len(done.stderr) < tamaño, "no amplifica: menos stderr que fichero de entrada"
    assert "BadZipFile" in done.stderr, "sigue diciendo qué ha fallado"
    assert "caracteres" in done.stderr, "y que el detalle venía recortado"


def test_un_badzipfile_corriente_se_cuenta_entero(tmp_path):
    """Control: acotar no puede empeorar el mensaje del caso que se da de verdad.

    Un `.txt` renombrado a `.docx` es el fallo habitual, y su mensaje cabe de
    sobra en el límite: se cita íntegro y sin nota de recorte.
    """
    falso = tmp_path / "notas.docx"
    falso.write_text("esto es texto plano, no un zip", encoding="utf-8")

    done = cli_subprocess("office2text", str(falso))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert "File is not a zip file" in done.stderr
    assert "caracteres)" not in done.stderr, "sin nota de recorte"


def test_el_rechazo_por_tamano_conserva_su_mensaje_entero(bomba_deflate):
    """Los rechazos que redacta el proyecto no pasan por el recorte del CLI.

    Vienen ya acotados y con las tres partes que la skill necesita —qué miembro,
    por qué se rechaza y qué hacer—, así que reproducirlos como un dato citado
    los dejaría en una sola línea escapada e ilegible.
    """
    done = cli_subprocess("office2text", str(bomba_deflate))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert "máximo por miembro es" in done.stderr, "dice qué límite ha saltado"
    assert "exporta el texto" in done.stderr, "y qué hacer con el documento"
    assert "\\n" not in done.stderr, "sin escapar: el mensaje es de varias líneas"


# --------------------------------------------------------------------------- #
# office2text: los fallos de zipfile que NO son BadZipFile → exit 4           #
# --------------------------------------------------------------------------- #
# `zipfile` sólo usa `BadZipFile` para lo que puede diagnosticar leyendo el
# formato. Todo lo demás que puede provocar un fichero —el flujo comprimido roto,
# la bandera que no implementa, el miembro cifrado— sale con el tipo de excepción
# de la biblioteca que lo detecta, y esos escapaban del `except` del CLI: exit 1 y
# traceback. Para la skill `okf-ingest` eso no es "documento que no sirve" sino
# "el CLI se ha caído", que es un fallo distinto y la deja sin qué contar.


def test_office2text_traduce_un_deflate_corrupto(tmp_path):
    """Un miembro que dice DEFLATE y no lo es: `zlib.error`, no `BadZipFile`.

    El zip está bien formado —cabeceras, tamaños y directorio central cuadran—,
    así que `zipfile` no tiene nada que objetar y delega en zlib, que revienta al
    encontrar un bloque con un tipo que no existe. Salía como traceback de
    `zlib.error: Error -3 while decompressing data`.
    """
    cuerpo = f'<document xmlns="{_DOC_NS}"><body><p><r><t>hola</t></r></p></body></document>'
    corrupto = tmp_path / "corrupto.docx"
    # 0xFF: BFINAL=1 y BTYPE=11, un tipo de bloque que DEFLATE reserva como error
    # (RFC 1951 §3.2.3), así que la corrupción no depende de la versión de zlib.
    corrupto.write_bytes(
        _zip_a_mano(b"word/document.xml", cuerpo.encode(), datos=b"\xff" * 8)
    )

    done = cli_subprocess("office2text", str(corrupto))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == "", "ni un byte del XML a medio descomprimir llega a stdout"
    assert "Traceback" not in done.stderr
    assert "zlib.error" in done.stderr, "el nombre suelto (`error`) no diría nada"
    assert corrupto.name in done.stderr, "el mensaje nombra el fichero"
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"


@pytest.mark.parametrize(
    "bandera, texto",
    [(0x20, "flag bit 5"), (0x40, "flag bit 6")],
    ids=["datos parcheados", "cifrado fuerte"],
)
def test_office2text_traduce_una_bandera_zip_no_admitida(tmp_path, bandera, texto):
    """Las banderas 5 y 6 del zip: `zipfile` levanta `NotImplementedError`.

    Son dos bits de la cabecera —los pone quien fabrica el fichero, sin más— y
    `zipfile` se niega a abrir el miembro antes de descomprimir nada. Que un bit
    en un `.docx` produjera un traceback de "NotImplementedError" hacía parecer
    inacabado el CLI cuando lo que falta es soporte para un modo que ningún
    Word escribe.
    """
    hostil = tmp_path / "banderas.docx"
    hostil.write_bytes(_zip_a_mano(b"word/document.xml", flags=bandera))

    done = cli_subprocess("office2text", str(hostil))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "NotImplementedError" in done.stderr
    assert texto in done.stderr, "el mensaje dice qué bandera es"
    assert "vuelve a guardarlo" in done.stderr, "y qué hacer con el documento"
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"


def test_office2text_traduce_un_miembro_cifrado_con_zipcrypto(tmp_path):
    """Un `.docx` con contraseña: `zipfile` pide la clave con un `RuntimeError`.

    Es el caso más plausible de los cuatro —un documento corporativo protegido
    acaba en `sources/` sin querer— y era el que peor salía: `RuntimeError` a
    secas, traceback y exit 1, cuando lo que hay que hacer es quitarle la
    protección y volver a soltarlo.
    """
    cifrado = tmp_path / "protegido.docx"
    cifrado.write_bytes(_zip_a_mano(b"word/document.xml", flags=0x01))

    done = cli_subprocess("office2text", str(cifrado))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "RuntimeError" in done.stderr
    assert "encrypted" in done.stderr, "el detalle dice que pide contraseña"
    assert "protegido con contraseña" in done.stderr, "y el mensaje, qué hacer"


def test_un_miembro_cifrado_con_nombre_enorme_no_amplifica_el_stderr(tmp_path):
    """El `RuntimeError` de ZipCrypto interpola el nombre del miembro.

    Y ese nombre lo escribe el fichero, con los 65.535 bytes que admite la
    cabecera: es la misma amplificación que ya se acotó en `BadZipFile`, por la
    puerta de otra excepción ajena. Se monta como `.xlsx` porque el nombre de la
    hoja sale del directorio del zip (el `.docx` lee siempre
    `word/document.xml`), así que ahí el largo sí lo elige quien manda el
    documento.
    """
    nombre = b"xl/worksheets/sheet" + b"N" * 60_000 + b".xml"
    hostil = tmp_path / "amplificador-cifrado.xlsx"
    hostil.write_bytes(_zip_a_mano(nombre, flags=0x01))
    tamaño = hostil.stat().st_size

    done = cli_subprocess("office2text", str(hostil))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"
    assert len(done.stderr) < tamaño, "no amplifica: menos stderr que fichero de entrada"
    assert "RuntimeError" in done.stderr, "sigue diciendo qué ha fallado"
    assert "caracteres" in done.stderr, "y que el detalle venía recortado"


# --------------------------------------------------------------------------- #
# office2text: los avisos de `zipfile` → exit 4                                #
# --------------------------------------------------------------------------- #
# `zipfile` no siempre rechaza lo que le parece mal: hay dos casos que resuelve
# con `warnings.warn` y sigue leyendo —las entradas solapadas ("possible zip
# bomb") y el campo extra Unicode vacío—. Eso deja dos agujeros del mismo defecto
# que ya se cerró en las excepciones ajenas:
#
# - el texto del aviso lo escribe el fichero (interpola el nombre del miembro, con
#   los 65.535 bytes que admite la cabecera) y salía por stderr sin acotar, con el
#   comando terminando en 0 como si el documento estuviese bien;
# - y con `PYTHONWARNINGS=error` —o `-W error`, habitual en CI— ese mismo aviso
#   escapaba como `UserWarning` con traceback y exit 1.
#
# Un aviso sobre la estructura del zip es entrada inválida, así que sale por donde
# los demás rechazos: exit 4, mensaje acotado y accionable, y nada por stdout.


def test_office2text_rechaza_entradas_de_zip_solapadas(tmp_path):
    """Dos entradas del directorio central sobre la misma cabecera local.

    `zipfile` lo llama "possible zip bomb" y avisa en vez de rechazarlo (en la
    rama de al lado, cuando el solape es parcial, sí levanta `BadZipFile`), así
    que el documento se leía y el comando salía con 0 tras escribir el aviso.
    """
    solapado = tmp_path / "solapado.docx"
    solapado.write_bytes(_zip_a_mano(b"word/document.xml", eco=b"[eco]"))

    done = cli_subprocess("office2text", str(solapado))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == "", "un exit 0 haría pasar el zip amañado por documento"
    assert "Traceback" not in done.stderr
    assert "UserWarning" not in done.stderr, "no sale como aviso, sino como rechazo"
    assert "OfficeStructureError" in done.stderr
    assert "word/document.xml" in done.stderr, "el mensaje nombra el miembro"
    assert "solapa" in done.stderr, "y por qué se rechaza"
    assert "vuelve a guardarlo" in done.stderr, "y qué hacer con el documento"


def test_un_solapamiento_con_nombre_enorme_no_amplifica_el_stderr(tmp_path):
    """El aviso interpola el nombre del miembro, y ese nombre lo elige el fichero.

    Se monta como `.pptx` porque los nombres de las slides salen del directorio
    del zip (el `.docx` lee siempre `word/document.xml`), así que ahí el largo lo
    decide quien manda el documento: con 60.000 letras, el aviso ocupaba 60 KB de
    stderr y el comando salía con 0.
    """
    nombre = b"ppt/slides/slide" + b"N" * 60_000 + b".xml"
    hostil = tmp_path / "amplificador-solapado.pptx"
    hostil.write_bytes(_zip_a_mano(nombre, eco=b"[eco]"))
    tamaño = hostil.stat().st_size

    done = cli_subprocess("office2text", str(hostil))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"
    assert len(done.stderr) < tamaño, "no amplifica: menos stderr que fichero de entrada"
    assert "caracteres" in done.stderr, "y dice que el nombre venía recortado"


@pytest.mark.parametrize("defecto", ["solapamiento", "campo-extra-vacio"])
def test_un_aviso_del_zip_no_escapa_como_userwarning(tmp_path, defecto):
    """Con `PYTHONWARNINGS=error` el aviso era una excepción, no un aviso.

    Y ninguna de las dos formas de tratarlo la elige el documento: el rechazo no
    depende de cómo esté configurado Python. La skill `okf-ingest` lee exit code y
    stderr, así que un exit 1 con traceback no le dice "documento inválido" sino
    "el CLI se ha caído".
    """
    nombre = b"word/document.xml"
    amaño = (
        {"eco": b"[eco]"} if defecto == "solapamiento"
        else {"extra": _campo_extra_unicode_vacio(nombre)}
    )
    hostil = tmp_path / f"{defecto}.docx"
    hostil.write_bytes(_zip_a_mano(nombre, **amaño))

    done = cli_subprocess(
        "office2text", str(hostil), entorno={"PYTHONWARNINGS": "error"}
    )

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "UserWarning" not in done.stderr
    assert "OfficeStructureError" in done.stderr
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"


def test_office2text_rechaza_un_campo_extra_unicode_vacio(tmp_path):
    """El otro aviso, sin `PYTHONWARNINGS`: se emite al abrir el zip, no al leerlo.

    `ZipInfo._decodeExtra` corre dentro del constructor de `ZipFile`, así que la
    guarda no puede vivir sólo en la lectura de los miembros. Antes salía con 0
    tras imprimir el aviso —el documento se extraía igual—, que es justo el modo
    en que un aviso ignorado se vuelve invisible.
    """
    hostil = tmp_path / "extra-vacio.docx"
    hostil.write_bytes(
        _zip_a_mano(
            b"word/document.xml",
            extra=_campo_extra_unicode_vacio(b"word/document.xml"),
        )
    )

    done = cli_subprocess("office2text", str(hostil))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "UserWarning" not in done.stderr
    assert "0x7075" in done.stderr, "el mensaje cita de qué avisa `zipfile`"
    assert "al abrir el fichero" in done.stderr, "y en qué momento"


def test_un_documento_normal_no_cambia_con_pythonwarnings_error(tmp_path):
    """Control: la guarda no puede convertir en rechazo un documento sano.

    Es la contrapartida del rechazo: tratar los avisos como errores sólo vale si
    el camino bueno no emite ninguno, y con `-W error` un aviso de más aquí (o en
    cualquier importación del CLI) rompería la extracción de un docx corriente.
    """
    doc = tmp_path / "acta.docx"
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            f'<document xmlns="{_DOC_NS}"><body><p><r><t>hola</t></r></p>'
            f"</body></document>",
        )

    done = cli_subprocess(
        "office2text", str(doc), entorno={"PYTHONWARNINGS": "error"}
    )

    assert done.returncode == 0
    assert done.stdout == "hola"
    assert done.stderr == ""


def _bucle_de_enlaces(tmp_path, nombre: str) -> Path:
    """Dos enlaces que se apuntan: abrirlos da `OSError` (ELOOP), no `FileNotFound`."""
    uno = tmp_path / nombre
    otro = tmp_path / f"otro-{nombre}"
    uno.symlink_to(otro)
    otro.symlink_to(uno)
    return uno


def test_office2text_traduce_un_oserror_al_abrir(tmp_path):
    """Un fichero que existe y no se deja leer: exit 4, no traceback.

    `FileNotFoundError`, `IsADirectoryError` y `PermissionError` ya tenían su
    mensaje, pero son tres subclases de `OSError` entre muchas: un bucle de
    enlaces simbólicos (ELOOP), un dispositivo o un fallo de E/S salían con el
    traceback entero. El bucle es el único que se puede montar en disco de forma
    determinista, y ejercita exactamente esa rama.
    """
    bucle = _bucle_de_enlaces(tmp_path, "bucle.docx")

    done = cli_subprocess("office2text", str(bucle))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "OSError" in done.stderr
    assert os.strerror(errno.ELOOP) in done.stderr, "el detalle dice por qué no se abre"
    assert len(done.stderr) < 2048, f"stderr desproporcionado: {len(done.stderr)} bytes"


def test_html2text_traduce_un_oserror_al_abrir(tmp_path):
    """El otro comando que pasa por `_extract`: mismo contrato, mismo exit code."""
    bucle = _bucle_de_enlaces(tmp_path, "bucle.html")

    done = cli_subprocess("html2text", str(bucle))

    assert done.returncode == cli.EXIT_BAD_INPUT
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert "OSError" in done.stderr


def _zip_a_mano(
    nombre: bytes,
    cuerpo: bytes = b"<d>hola</d>",
    *,
    flags: int = 0,
    datos: bytes | None = None,
    nombre_local: bytes | None = None,
    extra: bytes = b"",
    eco: bytes | None = None,
) -> bytes:
    """Zip DEFLATE de un miembro, armado byte a byte.

    Lo que hace hostiles a estos ficheros son campos que `zipfile.writestr` no
    deja escribir: las banderas de propósito general (cifrado, datos parcheados),
    un nombre distinto en la cabecera local y en el directorio central, un campo
    extra amañado, dos entradas del directorio central sobre la misma cabecera
    local, o un flujo comprimido que no es DEFLATE válido. Por defecto los dos
    nombres coinciden y `datos` es el DEFLATE real de `cuerpo`, así que sin
    argumentos sale un zip legítimo.

    `extra` va en el campo extra de la entrada del directorio central (ahí lo lee
    `ZipInfo._decodeExtra`, al abrir el fichero). `eco` añade una **segunda**
    entrada al directorio central, con otro nombre y apuntando a la misma
    cabecera local: para `zipfile` los bytes comprimidos del miembro se solapan
    con los de esa otra entrada.
    """
    if datos is None:
        datos = zlib.compress(cuerpo)[2:-4]  # deflate crudo, sin cabecera zlib
    if nombre_local is None:
        nombre_local = nombre
    crc = zlib.crc32(cuerpo)
    local = struct.pack(
        "<4s5H3L2H", b"PK\x03\x04", 20, flags, zipfile.ZIP_DEFLATED, 0, 0,
        crc, len(datos), len(cuerpo), len(nombre_local), 0,
    ) + nombre_local
    fichero = local + datos

    def _central(name: bytes, campo_extra: bytes = b"") -> bytes:
        return struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", 20, 20, flags, zipfile.ZIP_DEFLATED, 0, 0,
            crc, len(datos), len(cuerpo), len(name), len(campo_extra),
            0, 0, 0, 0, 0,
        ) + name + campo_extra

    central = _central(nombre, extra)
    entradas = 1
    if eco is not None:
        # Misma cabecera local (offset 0) con otro nombre: la primera entrada se
        # queda sin bytes propios y `zipfile` la declara solapada.
        central += _central(eco)
        entradas = 2
    fin = struct.pack(
        "<4s4H2LH", b"PK\x05\x06", 0, 0, entradas, entradas,
        len(central), len(fichero), 0,
    )
    return fichero + central + fin


def _campo_extra_unicode_vacio(nombre: bytes) -> bytes:
    """Campo extra 0x7075 (Unicode Path) con el nombre vacío.

    `zipfile` lo acepta y avisa —`UserWarning`— en vez de rechazarlo, y sólo lo
    mira si el CRC del campo cuadra con el del nombre del directorio central; de
    ahí que haya que calcularlo aquí. Ninguna aplicación de Office escribe esto:
    es la otra puerta por la que un fichero decide qué se imprime por stderr.
    """
    return struct.pack("<HHBL", 0x7075, 5, 1, zlib.crc32(nombre))


def _zip_con_nombre_local_falso(nombre_central: bytes, nombre_local: bytes) -> bytes:
    """Zip cuyo directorio y cabecera local discrepan en el nombre del miembro.

    `zipfile` no puede escribirlo —siempre pone el mismo nombre en los dos
    sitios—, así que se arma a mano. Al abrir el miembro, `ZipFile.open` compara
    ambos y levanta `BadZipFile` citando los dos: el largo lo elige el fichero.
    """
    return _zip_a_mano(nombre_central, nombre_local=nombre_local)


def test_office2text_sigue_extrayendo_un_xlsx_normal(tmp_path):
    """Control: una hoja con huecos se extrae alineada, como antes."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    hoja = tmp_path / "ventas.xlsx"
    with zipfile.ZipFile(hoja, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{ns}"><sheetData>'
            '<row r="1"><c r="A1"><v>10</v></c><c r="C1"><v>30</v></c></row>'
            "</sheetData></worksheet>",
        )

    done = cli_subprocess("office2text", str(hoja))

    assert done.returncode == 0
    assert done.stdout.splitlines()[1] == "10 |  | 30"
    assert done.stderr == ""


def test_office2text_sigue_extrayendo_un_documento_normal(tmp_path):
    """Control: el límite no estorba a un docx de tamaño realista."""
    doc = tmp_path / "acta.docx"
    prosa = "palabra " * 20_000
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            f'<document xmlns="{_DOC_NS}"><body><p><r><t>{prosa}</t></r></p>'
            f"</body></document>",
        )

    done = cli_subprocess("office2text", str(doc))

    assert done.returncode == 0
    assert done.stdout == prosa.strip()
    assert done.stderr == ""
