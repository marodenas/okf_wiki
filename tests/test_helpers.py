"""Tests de fiabilidad de ingesta para `okf_wiki.helpers`.

Todo el material de prueba es sintético (se construye en un tmpdir): no hace
falta ni el motor OKF ni ficheros Office reales. Se ejecutan con la stdlib:

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
import tracemalloc
import unittest
import warnings
import zipfile
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okf_wiki import helpers  # noqa: E402


def digest(seed: str) -> str:
    """sha256 de juguete con la forma real (64 hex): `commit_state` la exige.

    Los placeholders tipo `"aaa"` los rechaza `validate_scan_payload`, y con
    razón: un hash truncado en el manifest hace que el siguiente `scan` reporte
    la fuente como `changed` y la reingiera.
    """
    return hashlib.sha256(seed.encode()).hexdigest()


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_instance(self) -> Path:
        """Instancia mínima: `<tmp>/inst/sources/`."""
        inst = self.tmp / "inst"
        (inst / helpers.SOURCES_DIRNAME).mkdir(parents=True)
        return inst

    def state_path(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME / helpers.STATE_FILENAME


# --------------------------------------------------------------------------- #
# scan / load_state                                                            #
# --------------------------------------------------------------------------- #
class ScanFailsLoudlyTest(_TmpCase):
    def test_instancia_inexistente_falla(self):
        with self.assertRaises(helpers.MissingSourcesError):
            helpers.scan(self.tmp / "no-existe")

    def test_sources_ausente_falla(self):
        inst = self.tmp / "inst"
        inst.mkdir()
        with self.assertRaises(helpers.MissingSourcesError) as ctx:
            helpers.scan(inst)
        self.assertIn(helpers.SOURCES_DIRNAME, str(ctx.exception))

    def test_sources_es_fichero_falla(self):
        inst = self.tmp / "inst"
        inst.mkdir()
        (inst / helpers.SOURCES_DIRNAME).write_text("no soy un directorio")
        with self.assertRaises(helpers.MissingSourcesError):
            helpers.scan(inst)

    def test_sources_vacio_no_falla(self):
        inst = self.make_instance()
        result = helpers.scan(inst)
        self.assertEqual(
            result, {"new": [], "changed": [], "unchanged": [], "deleted": []}
        )

    def test_scan_clasifica_new_changed_unchanged_deleted(self):
        inst = self.make_instance()
        src = inst / helpers.SOURCES_DIRNAME
        (src / "a.md").write_text("uno", encoding="utf-8")
        (src / "b.md").write_text("dos", encoding="utf-8")

        first = helpers.scan(inst)
        self.assertEqual({e["path"] for e in first["new"]}, {"a.md", "b.md"})
        helpers.commit_state(inst, first)

        (src / "a.md").write_text("uno modificado", encoding="utf-8")
        (src / "b.md").unlink()
        (src / "c.html").write_text("<p>tres</p>", encoding="utf-8")

        second = helpers.scan(inst)
        self.assertEqual([e["path"] for e in second["changed"]], ["a.md"])
        self.assertEqual([e["path"] for e in second["deleted"]], ["b.md"])
        self.assertEqual([e["path"] for e in second["new"]], ["c.html"])
        self.assertEqual(second["new"][0]["extract"], "html2text")


class ScanSymlinkTest(_TmpCase):
    """La dropzone no es una ventana al resto del disco.

    `scan` recorría `sources/` con `rglob` + `is_file()`, y las dos cosas
    resuelven enlaces: un `sources/atajo.md → ~/.ssh/id_rsa` (o una carpeta
    enlazada entera) se hasheaba, se reportaba como fuente `new` y la skill lo
    leía e ingería en la wiki como si estuviese en la instancia, dejando además
    en el manifest una ruta relativa que no corresponde a ningún fichero de
    `sources/`.
    """

    def _sources(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME

    def test_enlace_a_fichero_de_fuera_falla(self):
        inst = self.make_instance()
        secreto = self.tmp / "fuera" / "secreto.md"
        secreto.parent.mkdir()
        secreto.write_text("no debería entrar en la wiki", encoding="utf-8")
        (self._sources(inst) / "atajo.md").symlink_to(secreto)

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("atajo.md", str(ctx.exception))

    def test_enlace_a_directorio_de_fuera_no_se_recorre(self):
        inst = self.make_instance()
        fuera = self.tmp / "fuera"
        fuera.mkdir()
        (fuera / "doc.md").write_text("tampoco", encoding="utf-8")
        (self._sources(inst) / "carpeta").symlink_to(fuera, target_is_directory=True)

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("carpeta", str(ctx.exception))

    def test_enlace_roto_que_apunta_fuera_falla(self):
        """Aún no filtra nada, pero es el mismo enlace en cuanto el destino exista."""
        inst = self.make_instance()
        (self._sources(inst) / "atajo.md").symlink_to(self.tmp / "fuera" / "todavia-no.md")
        with self.assertRaises(helpers.UnsafeSourceError):
            helpers.scan(inst)

    def test_enlace_a_fichero_de_dentro_sigue_aceptandose(self):
        """Compatibilidad: lo que no sale de la dropzone se ingiere como antes."""
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "real.md").write_text("contenido", encoding="utf-8")
        (src / "alias.md").symlink_to(src / "real.md")

        result = helpers.scan(inst)
        self.assertEqual({e["path"] for e in result["new"]}, {"real.md", "alias.md"})

    def test_enlace_a_directorio_de_dentro_no_duplica_ni_hace_bucle(self):
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "sub").mkdir()
        (src / "sub" / "doc.md").write_text("uno", encoding="utf-8")
        (src / "sub" / "hacia-arriba").symlink_to(src, target_is_directory=True)
        (src / "espejo").symlink_to(src / "sub", target_is_directory=True)

        result = helpers.scan(inst)
        self.assertEqual([e["path"] for e in result["new"]], ["sub/doc.md"])

    def test_instancia_alcanzada_por_una_ruta_enlazada_funciona(self):
        """El enlace está *encima* de la dropzone (p.ej. `/tmp` en macOS): no es fuga."""
        inst = self.make_instance()
        (self._sources(inst) / "doc.md").write_text("uno", encoding="utf-8")
        alias = self.tmp / "alias-instancia"
        alias.symlink_to(inst, target_is_directory=True)

        result = helpers.scan(alias)
        self.assertEqual([e["path"] for e in result["new"]], ["doc.md"])

    @unittest.skipIf(hasattr(os, "getuid") and os.getuid() == 0,
                     "root lee cualquier carpeta: el permiso no se puede negar")
    def test_carpeta_ilegible_no_pasa_por_dropzone_vacia(self):
        """`rglob` se comía el `PermissionError`: la carpeta desaparecía del scan."""
        inst = self.make_instance()
        cerrada = self._sources(inst) / "cerrada"
        cerrada.mkdir()
        (cerrada / "doc.md").write_text("uno", encoding="utf-8")
        cerrada.chmod(0o000)
        self.addCleanup(cerrada.chmod, 0o755)

        with self.assertRaises(helpers.IngestError) as ctx:
            helpers.scan(inst)
        self.assertIn("cerrada", str(ctx.exception))

    def test_el_error_es_de_ingesta_para_que_el_cli_lo_traduzca(self):
        """`UnsafeSourceError` es un `IngestError`: exit 3 con instrucciones, no traceback."""
        self.assertTrue(issubclass(helpers.UnsafeSourceError, helpers.IngestError))


class ScanSymlinkCicloTest(_TmpCase):
    """Un enlace que da vueltas sobre sí mismo no puede acabar en traceback.

    `_collect_sources` llamaba a `Path.resolve()` a pelo, y un ciclo
    (`a → b → a`, o un enlace a sí mismo) revienta ahí con un `RuntimeError`
    —"Symlink loop from ..."— que el CLI no captura: la skill recibía una pila de
    llamadas en vez de un mensaje. En los Python donde `resolve()` no lanza, el
    ciclo pasaba el control de contención y luego no era ni fichero ni carpeta,
    así que el enlace **desaparecía del scan sin decir nada**. Las dos salidas se
    traducen ahora a `UnsafeSourceError`.
    """

    def _sources(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME

    def test_enlace_a_si_mismo(self):
        inst = self.make_instance()
        (self._sources(inst) / "yo.md").symlink_to("yo.md")
        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("yo.md", str(ctx.exception))

    def test_ciclo_entre_dos_enlaces(self):
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "a.md").symlink_to("b.md")
        (src / "b.md").symlink_to("a.md")
        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("circular", str(ctx.exception).lower())

    def test_ciclo_en_un_subdirectorio_tampoco_revienta(self):
        inst = self.make_instance()
        sub = self._sources(inst) / "sub"
        sub.mkdir()
        (sub / "doc.md").write_text("uno", encoding="utf-8")
        (sub / "vueltas.md").symlink_to("vueltas.md")
        with self.assertRaises(helpers.UnsafeSourceError):
            helpers.scan(inst)

    def test_no_se_escapa_ningun_runtimeerror(self):
        """El contrato es el tipo, no el mensaje: `RuntimeError` no es `IngestError`."""
        inst = self.make_instance()
        (self._sources(inst) / "yo.md").symlink_to("yo.md")
        try:
            helpers.scan(inst)
        except helpers.IngestError:
            pass
        except RuntimeError as exc:  # pragma: no cover - es justo la regresión
            self.fail(f"`scan` dejó escapar un RuntimeError sin traducir: {exc}")


class ScanCarreraTest(_TmpCase):
    """Validar el enlace y leer el fichero eran dos momentos distintos.

    `_collect_sources` comprobaba a dónde apunta cada entrada y después `scan`
    volvía a abrir esa misma **ruta** dos veces más —`_hash_file(path)` y
    `path.stat()`—, siguiendo otra vez los enlaces. Quien pueda escribir en
    `sources/` (la dropzone es, por diseño, una carpeta donde se sueltan cosas)
    tenía dos huecos: sustituir el fichero por un enlace a `~/.ssh/id_rsa`
    después del control, y cambiarlo entre el hash y el `stat` para que el
    manifest anotase el sha256 de un contenido con el tamaño de otro.
    """

    def _sources(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME

    def test_sustituir_la_fuente_por_un_enlace_tras_validarla_falla(self):
        inst = self.make_instance()
        src = self._sources(inst)
        doc = src / "doc.md"
        doc.write_text("contenido legítimo", encoding="utf-8")
        secreto = self.tmp / "fuera" / "secreto.md"
        secreto.parent.mkdir()
        secreto.write_text("SECRETO", encoding="utf-8")

        recorrido = helpers._iter_sources

        def con_carrera(sources_dir):
            for rel, path in recorrido(sources_dir):
                # El control de contención ya ha pasado; ahora se cambia la ruta
                # justo antes de que `scan` la lea.
                doc.unlink()
                doc.symlink_to(secreto)
                yield rel, path

        with mock.patch.object(helpers, "_iter_sources", con_carrera):
            with self.assertRaises(helpers.UnsafeSourceError) as ctx:
                helpers.scan(inst)
        self.assertIn("doc.md", str(ctx.exception))

    def test_el_enlace_de_dentro_se_lee_por_su_destino_ya_resuelto(self):
        """Lo que se abre no es el nombre del enlace, así que no se puede desviar.

        Y se abre por una ruta **relativa a la dropzone**: es lo que permite bajar
        componente a componente desde ella en vez de recorrer otra vez desde `/`.
        """
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "real.md").write_text("contenido", encoding="utf-8")
        (src / "alias.md").symlink_to(src / "real.md")

        rutas = {rel.as_posix(): real for rel, real in helpers._iter_sources(src)}
        self.assertEqual(rutas["alias.md"], Path("real.md"))
        self.assertFalse(rutas["alias.md"].is_absolute())
        self.assertFalse((src / rutas["alias.md"]).is_symlink())

    def test_hash_y_tamano_salen_de_la_misma_lectura(self):
        inst = self.make_instance()
        doc = self._sources(inst) / "doc.md"
        doc.write_bytes(b"hola mundo")

        entry = helpers.scan(inst)["new"][0]
        self.assertEqual(entry["sha256"], hashlib.sha256(b"hola mundo").hexdigest())
        self.assertEqual(entry["size"], 10)

    def test_la_extension_es_la_de_la_dropzone_no_la_del_destino(self):
        """`alias.docx → real.bin` se sigue extrayendo como docx: manda lo que se ve."""
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "real.bin").write_bytes(b"PK\x03\x04")
        (src / "alias.docx").symlink_to(src / "real.bin")

        modos = {e["path"]: e["extract"] for e in helpers.scan(inst)["new"]}
        self.assertEqual(modos["alias.docx"], "office2text")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "sin FIFOs (Windows)")
    def test_una_fuente_que_deja_de_ser_fichero_regular_no_cuelga_el_scan(self):
        """Un FIFO en su sitio dejaría `scan` esperando un escritor para siempre."""
        inst = self.make_instance()
        src = self._sources(inst)
        os.mkfifo(src / "tuberia.md")
        with self.assertRaises(helpers.UnsafeSourceError):
            helpers._hash_and_size(src, Path("tuberia.md"))

    def test_abrir_una_fuente_que_ahora_es_un_enlace_no_lo_sigue(self):
        inst = self.make_instance()
        src = self._sources(inst)
        secreto = self.tmp / "secreto.md"
        secreto.write_text("SECRETO", encoding="utf-8")
        (src / "atajo.md").symlink_to(secreto)

        with self.assertRaises(helpers.UnsafeSourceError):
            helpers._hash_and_size(src, Path("atajo.md"))

    def test_una_fuente_ilegible_es_error_de_ingesta_no_un_oserror_pelado(self):
        inst = self.make_instance()
        src = self._sources(inst)
        with self.assertRaises(helpers.IngestError):
            helpers._hash_and_size(src, Path("no-existe.md"))


class LecturaBinariaTest(_TmpCase):
    """Las fuentes se hashean en binario, también donde no hay `openat`.

    `_hash_and_size` sustituyó `open(path, "rb")` por `os.open` + `os.read` para
    leer hash y tamaño de un único descriptor. En POSIX no cambia nada, pero
    `os.open` de Windows abre en modo **texto** salvo que se le pase
    `O_BINARY`: el CRT convertiría cada `\\r\\n` en `\\n` y cortaría el fichero en
    el primer `0x1A`. El manifest anotaría entonces el sha256 y el tamaño de una
    versión mutilada de la fuente, así que todo documento con CRLF —cualquier
    `.md` escrito en Windows— saldría `changed` en cada `scan` y la skill lo
    reingeriría en bucle. Windows es justamente donde no hay contención por
    componentes, y esta es la rama que se usa allí.
    """

    def _sources(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME

    CRUDO = b"linea\r\nsegunda\x1atras el ctrl-z\r\n"

    def test_el_camino_sin_openat_lee_los_bytes_tal_cual(self):
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "doc.md").write_bytes(self.CRUDO)

        with mock.patch.object(helpers, "_HAS_OPENAT", False):
            sha, size = helpers._hash_and_size(src, Path("doc.md"))

        self.assertEqual(sha, hashlib.sha256(self.CRUDO).hexdigest())
        self.assertEqual(size, len(self.CRUDO))

    def test_el_camino_con_openat_da_lo_mismo(self):
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "doc.md").write_bytes(self.CRUDO)

        sha, size = helpers._hash_and_size(src, Path("doc.md"))

        self.assertEqual(sha, hashlib.sha256(self.CRUDO).hexdigest())
        self.assertEqual(size, len(self.CRUDO))

    def test_la_apertura_pide_modo_binario(self):
        """Lo que en POSIX no se puede observar (`O_BINARY` vale 0) sí se exige.

        Se simula la constante de Windows con un bit propio y se comprueba que
        `_open_leaf` lo incluye en los flags: es la única forma de cubrir en
        POSIX la regresión que sólo se manifestaría en Windows.
        """
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "doc.md").write_bytes(b"contenido")
        marca = 1 << 24  # bit inventado, en el sitio donde Windows pone O_BINARY
        vistos: list[int] = []
        real = os.open

        def espia(name, flags, *args, **kwargs):
            vistos.append(flags)
            return real(name, flags & ~marca, *args, **kwargs)

        with mock.patch.object(helpers, "_HAS_OPENAT", False), \
             mock.patch.object(helpers, "_O_BINARY", marca), \
             mock.patch.object(os, "open", espia):
            helpers._hash_and_size(src, Path("doc.md"))

        self.assertTrue(vistos, "no se ha abierto nada")
        self.assertTrue(
            all(flags & marca for flags in vistos),
            f"alguna apertura de fuente va sin modo binario: {vistos}",
        )

    def test_o_binary_es_la_constante_real_donde_exista(self):
        self.assertEqual(helpers._O_BINARY, getattr(os, "O_BINARY", 0))


@unittest.skipUnless(helpers._HAS_OPENAT, "sin `openat` no hay contención por componentes")
class ScanDirectorioIntermedioTest(_TmpCase):
    """El cambiazo no es sólo del fichero: también del directorio que lo contiene.

    `O_NOFOLLOW` protege el **último** componente de la ruta y nada más, así que
    abrir `sources/tema/doc.md` de una sola vez dejaba libre todo lo de en medio:
    quien puede escribir en la dropzone —que es, por diseño, una carpeta donde se
    sueltan cosas— sustituye `sources/tema` por un enlace a otro sitio entre el
    recorrido y la lectura, y `scan` hasheaba `<otro sitio>/doc.md` reportándolo
    como una fuente de la instancia. La ruta se abre ahora componente a
    componente desde la dropzone, con `O_NOFOLLOW` en cada tramo.
    """

    def _sources(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME

    def _instancia_con_subcarpeta(self) -> tuple[Path, Path, Path]:
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "tema").mkdir()
        (src / "tema" / "doc.md").write_text("contenido legítimo", encoding="utf-8")
        fuera = self.tmp / "fuera"
        fuera.mkdir()
        (fuera / "doc.md").write_text("SECRETO DE FUERA", encoding="utf-8")
        return inst, src, fuera

    def test_sustituir_el_directorio_intermedio_por_un_enlace_falla(self):
        inst, src, fuera = self._instancia_con_subcarpeta()
        recorrido = helpers._iter_sources

        def con_carrera(sources_dir):
            for rel, real in recorrido(sources_dir):
                # El recorrido ya ha validado `tema/`; ahora se cambia por un
                # enlace justo antes de que `scan` lea `tema/doc.md`.
                (src / "tema" / "doc.md").unlink()
                (src / "tema").rmdir()
                (src / "tema").symlink_to(fuera, target_is_directory=True)
                yield rel, real

        with mock.patch.object(helpers, "_iter_sources", con_carrera):
            with self.assertRaises(helpers.UnsafeSourceError) as ctx:
                helpers.scan(inst)
        mensaje = str(ctx.exception)
        self.assertIn("tema", mensaje)
        # El errno no es el mismo en Linux (ELOOP) que en macOS (ENOTDIR): el
        # mensaje sale de mirar qué hay ahí, no del código de error.
        self.assertIn("enlace simbólico", mensaje)

    def test_el_contenido_de_fuera_no_llega_al_manifest(self):
        """El contrato es que no se lea, no sólo que se avise."""
        inst, src, fuera = self._instancia_con_subcarpeta()
        (src / "tema" / "doc.md").unlink()
        (src / "tema").rmdir()
        (src / "tema").symlink_to(fuera, target_is_directory=True)

        with self.assertRaises(helpers.UnsafeSourceError):
            helpers.scan(inst)
        # Y por la vía directa: aunque alguien pida esa ruta a mano, no se abre.
        with self.assertRaises(helpers.UnsafeSourceError):
            helpers._hash_and_size(src, Path("tema/doc.md"))

    def test_el_directorio_intermedio_que_pasa_a_ser_fichero_tampoco_pasa(self):
        inst, src, _ = self._instancia_con_subcarpeta()
        (src / "tema" / "doc.md").unlink()
        (src / "tema").rmdir()
        (src / "tema").write_text("ya no soy una carpeta", encoding="utf-8")

        with self.assertRaises(helpers.UnsafeSourceError):
            helpers._hash_and_size(src, Path("tema/doc.md"))

    def test_una_subcarpeta_normal_se_sigue_leyendo(self):
        """Control: la contención por componentes no estorba al caso corriente."""
        inst, src, _ = self._instancia_con_subcarpeta()
        (src / "tema" / "sub").mkdir()
        (src / "tema" / "sub" / "hondo.md").write_text("tres niveles", encoding="utf-8")

        rutas = {e["path"] for e in helpers.scan(inst)["new"]}
        self.assertEqual(rutas, {"tema/doc.md", "tema/sub/hondo.md"})


class ScanEnlaceDuroTest(_TmpCase):
    """Un enlace duro es la misma fuga que el simbólico, sin destino que enseñar.

    `sources/` es la única superficie de entrada, y el modelo de contención dice
    que todo lo que se lee tiene que estar dentro. Un `ln /etc/passwd
    sources/doc.md` cumple el recorrido —no es un enlace simbólico, es un fichero
    regular más— y aun así el contenido vive fuera de la instancia: aceptarlo
    dejaba entrar por la puerta de al lado justo lo que se rechaza cuando llega
    por un enlace simbólico. `st_nlink` los cuenta pero no dice dónde están, así
    que no hay forma de afirmar la contención: se rechaza.
    """

    def _sources(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME

    def test_enlace_duro_a_un_fichero_de_fuera_falla(self):
        inst = self.make_instance()
        secreto = self.tmp / "secreto.md"
        secreto.write_text("no debería entrar en la wiki", encoding="utf-8")
        os.link(secreto, self._sources(inst) / "doc.md")

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        mensaje = str(ctx.exception)
        self.assertIn("doc.md", mensaje)
        self.assertIn("enlace duro", mensaje)

    def test_el_mensaje_dice_que_copie_en_vez_de_enlazar(self):
        inst = self.make_instance()
        secreto = self.tmp / "secreto.md"
        secreto.write_text("x", encoding="utf-8")
        os.link(secreto, self._sources(inst) / "doc.md")

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("cp ", str(ctx.exception))

    def test_enlace_duro_entre_dos_ficheros_de_dentro_tambien_falla(self):
        """Desde el fd no se distingue de la fuga: los otros nombres no se ven."""
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "real.md").write_text("contenido", encoding="utf-8")
        os.link(src / "real.md", src / "copia.md")

        with self.assertRaises(helpers.UnsafeSourceError):
            helpers.scan(inst)

    def test_un_fichero_con_un_solo_nombre_se_ingiere_igual(self):
        """Control: la comprobación no toca a la dropzone corriente."""
        inst = self.make_instance()
        (self._sources(inst) / "doc.md").write_text("uno", encoding="utf-8")
        self.assertEqual([e["path"] for e in helpers.scan(inst)["new"]], ["doc.md"])


class ScanEnlaceInternoInservibleTest(_TmpCase):
    """Los enlaces de dentro que no se pueden ingerir tampoco se callan.

    El enlace que apunta fuera ya fallaba, pero el que apunta **dentro** a algo
    que no se puede leer se caía del recorrido sin decir nada: `is_file()` daba
    `False` y el bucle seguía. La dropzone quedaba con una fuente menos y el JSON
    de `scan` se leía como una ingesta completa. Son tres formas del mismo fallo:
    roto, no-fichero, y apuntando a una ruta oculta —que el recorrido excluye a
    propósito, así que el enlace serviría para colar por el nombre lo que la
    dropzone da por descartado.
    """

    def _sources(self, inst: Path) -> Path:
        return inst / helpers.SOURCES_DIRNAME

    def test_enlace_roto_hacia_dentro_falla(self):
        inst = self.make_instance()
        src = self._sources(inst)
        (src / "atajo.md").symlink_to(src / "no-existe.md")

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        mensaje = str(ctx.exception)
        self.assertIn("atajo.md", mensaje)
        self.assertIn("roto", mensaje)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "sin FIFOs (Windows)")
    def test_enlace_a_un_fifo_de_dentro_falla(self):
        inst = self.make_instance()
        src = self._sources(inst)
        os.mkfifo(src / "tuberia")
        (src / "atajo.md").symlink_to(src / "tuberia")

        with self.assertRaises(helpers.UnsafeSourceError):
            helpers.scan(inst)

    def test_enlace_a_un_fichero_oculto_de_dentro_falla(self):
        inst = self.make_instance()
        src = self._sources(inst)
        (src / ".oculto").mkdir()
        (src / ".oculto" / "doc.md").write_text("descartado a propósito", encoding="utf-8")
        (src / "visible.md").symlink_to(src / ".oculto" / "doc.md")

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("oculta", str(ctx.exception).lower())

    def test_enlace_a_una_carpeta_oculta_de_dentro_falla(self):
        """Si se saltase, su contenido no se recorrería por ninguna otra ruta."""
        inst = self.make_instance()
        src = self._sources(inst)
        (src / ".archivo").mkdir()
        (src / ".archivo" / "doc.md").write_text("uno", encoding="utf-8")
        (src / "docs").symlink_to(src / ".archivo", target_is_directory=True)

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("docs", str(ctx.exception))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "sin FIFOs (Windows)")
    def test_un_fifo_suelto_en_la_dropzone_falla(self):
        """Mismo criterio que el enlace que apunta a uno: nada de saltos mudos."""
        inst = self.make_instance()
        os.mkfifo(self._sources(inst) / "tuberia.md")

        with self.assertRaises(helpers.UnsafeSourceError) as ctx:
            helpers.scan(inst)
        self.assertIn("tuberia.md", str(ctx.exception))


class LoadStateCorruptTest(_TmpCase):
    def test_manifest_ausente_devuelve_estado_vacio(self):
        inst = self.make_instance()
        self.assertEqual(helpers.load_state(inst), {"files": {}})

    def test_json_corrupto_lanza_error(self):
        inst = self.make_instance()
        self.state_path(inst).write_text("{ esto no es json", encoding="utf-8")
        with self.assertRaises(helpers.CorruptStateError) as ctx:
            helpers.load_state(inst)
        self.assertIn(helpers.STATE_FILENAME, str(ctx.exception))

    def test_json_valido_pero_no_es_objeto_lanza_error(self):
        inst = self.make_instance()
        self.state_path(inst).write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(helpers.CorruptStateError):
            helpers.load_state(inst)

    def test_files_con_forma_invalida_lanza_error(self):
        inst = self.make_instance()
        self.state_path(inst).write_text('{"files": "nope"}', encoding="utf-8")
        with self.assertRaises(helpers.CorruptStateError):
            helpers.load_state(inst)

    def test_scan_no_reingiere_en_silencio_con_manifest_corrupto(self):
        """Antes: manifest corrupto → estado vacío → todo `new` otra vez."""
        inst = self.make_instance()
        (inst / helpers.SOURCES_DIRNAME / "a.md").write_text("uno", encoding="utf-8")
        helpers.commit_state(inst, helpers.scan(inst))
        self.state_path(inst).write_text("{trunc", encoding="utf-8")
        with self.assertRaises(helpers.CorruptStateError):
            helpers.scan(inst)


# --------------------------------------------------------------------------- #
# commit_state                                                                 #
# --------------------------------------------------------------------------- #
class CommitStateTest(_TmpCase):
    def test_escribe_hashes_y_borra_eliminados(self):
        inst = self.make_instance()
        processed = {
            "new": [{"path": "a.md", "sha256": digest("a"), "size": 3, "ext": ".md"}],
            "changed": [],
            "unchanged": [],
            "deleted": [],
        }
        helpers.commit_state(inst, processed)
        self.assertEqual(helpers.load_state(inst)["files"]["a.md"]["sha256"], digest("a"))

        helpers.commit_state(
            inst,
            {"new": [], "changed": [], "unchanged": [], "deleted": [{"path": "a.md"}]},
        )
        self.assertEqual(helpers.load_state(inst)["files"], {})

    def test_conserva_conceptos_previos_si_no_se_reportan(self):
        inst = self.make_instance()
        entry = {"path": "a.md", "sha256": digest("a"), "size": 3, "ext": ".md"}
        helpers.commit_state(
            inst,
            {"new": [entry], "changed": [], "unchanged": [], "deleted": [],
             "concepts": {"a.md": ["tema.md"]}},
        )
        helpers.commit_state(
            inst, {"new": [], "changed": [entry], "unchanged": [], "deleted": []}
        )
        self.assertEqual(
            helpers.load_state(inst)["files"]["a.md"]["concepts"], ["tema.md"]
        )

    def test_escritura_atomica_no_deja_manifest_a_medias(self):
        """Si el volcado falla, el manifest anterior sigue siendo válido."""
        inst = self.make_instance()
        entry = {"path": "a.md", "sha256": digest("a"), "size": 3, "ext": ".md"}
        helpers.commit_state(
            inst, {"new": [entry], "changed": [], "unchanged": [], "deleted": []}
        )
        antes = self.state_path(inst).read_text(encoding="utf-8")

        def boom(src, dst):  # falla justo antes de publicar el fichero nuevo
            raise OSError("disco lleno")

        with mock.patch.object(helpers.os, "replace", boom):
            with self.assertRaises(OSError):
                helpers.commit_state(
                    inst,
                    {"new": [{"path": "b.md", "sha256": digest("b"), "size": 1, "ext": ".md"}],
                     "changed": [], "unchanged": [], "deleted": []},
                )

        self.assertEqual(self.state_path(inst).read_text(encoding="utf-8"), antes)
        self.assertEqual(set(helpers.load_state(inst)["files"]), {"a.md"})
        # y sin temporales huérfanos en la dropzone
        sobras = list((inst / helpers.SOURCES_DIRNAME).glob("*.tmp-*"))
        self.assertEqual(sobras, [])

    def test_no_deja_temporales_tras_escritura_correcta(self):
        inst = self.make_instance()
        helpers.commit_state(
            inst, {"new": [], "changed": [], "unchanged": [], "deleted": []}
        )
        sobras = list((inst / helpers.SOURCES_DIRNAME).glob("*.tmp-*"))
        self.assertEqual(sobras, [])
        json.loads(self.state_path(inst).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# html2text                                                                    #
# --------------------------------------------------------------------------- #
_HTML_CON_SCRIPT = """<!DOCTYPE html>
<html><head>
<style>body { color: red; } .oculto::after { content: "CSS_FILTRADO"; }</style>
<script>var x = 1; alert("JS_FILTRADO");</script>
</head><body>
<h1>Titulo visible</h1>
<p>Parrafo &amp; visible</p>
<script type="application/json">{"secreto": "JSON_FILTRADO"}</script>
</body></html>
"""


class Html2TextTest(_TmpCase):
    def _write(self, html: str) -> Path:
        p = self.tmp / "doc.html"
        p.write_text(html, encoding="utf-8")
        return p

    def test_elimina_script_y_style_con_contenido(self):
        out = helpers.html2text(self._write(_HTML_CON_SCRIPT))
        for filtrado in ("JS_FILTRADO", "CSS_FILTRADO", "JSON_FILTRADO",
                         "var x", "color: red"):
            self.assertNotIn(filtrado, out)
        self.assertIn("Titulo visible", out)
        self.assertIn("visible", out)

    def test_stripper_conserva_markup_y_entidades(self):
        limpio = helpers.strip_raw_text_elements(_HTML_CON_SCRIPT)
        self.assertNotIn("alert(", limpio)
        self.assertNotIn("<script", limpio)
        self.assertNotIn("<style", limpio)
        self.assertIn("<h1>", limpio)
        self.assertIn("&amp;", limpio)          # entidades intactas
        self.assertIn("<!DOCTYPE html>", limpio)

    def test_script_sin_cerrar_no_filtra_contenido(self):
        out = helpers.html2text(
            self._write("<p>visible</p><script>alert('COLADO');")
        )
        self.assertIn("visible", out)
        self.assertNotIn("COLADO", out)

    def test_script_autocerrado_no_traga_el_resto(self):
        out = helpers.html2text(
            self._write("<script src='x.js'/><p>despues</p>")
        )
        self.assertIn("despues", out)

    def test_ruta_markdownify_tambien_recibe_html_limpio(self):
        """Con markdownify instalado el script ya no llega al conversor."""
        import types

        visto: dict[str, str] = {}
        fake = types.ModuleType("markdownify")

        def _md(html, **kwargs):
            visto["html"] = html
            return html

        fake.markdownify = _md
        sys.modules["markdownify"] = fake
        self.addCleanup(sys.modules.pop, "markdownify", None)

        out = helpers.html2text(self._write(_HTML_CON_SCRIPT))
        self.assertNotIn("JS_FILTRADO", visto["html"])
        self.assertNotIn("CSS_FILTRADO", visto["html"])
        self.assertNotIn("JS_FILTRADO", out)
        self.assertIn("Titulo visible", out)


# --------------------------------------------------------------------------- #
# office2text / xlsx                                                           #
# --------------------------------------------------------------------------- #
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _sheet_xml(rows_xml: str) -> str:
    return (
        f'<worksheet xmlns="{_NS}"><sheetData>{rows_xml}</sheetData></worksheet>'
    )


def _shared_xml(strings: list[str]) -> str:
    items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    return f'<sst xmlns="{_NS}" count="{len(strings)}">{items}</sst>'


class XlsxTest(_TmpCase):
    def _xlsx(self, sheet: str, shared: list[str] | None = None) -> Path:
        p = self.tmp / "hoja.xlsx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
            if shared is not None:
                zf.writestr("xl/sharedStrings.xml", _shared_xml(shared))
        return p

    def test_conserva_columnas_vacias_por_referencia(self):
        # Fila 1: A,B,C llenas. Fila 2: solo A y C (falta B en el XML).
        sheet = _sheet_xml(
            '<row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="s"><v>2</v></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2"><v>10</v></c>'
            '<c r="C2"><v>30</v></c>'
            "</row>"
        )
        text = helpers.office2text(self._xlsx(sheet, ["nombre", "medio", "valor"]))
        lineas = text.splitlines()
        self.assertEqual(lineas[1], "nombre | medio | valor")
        self.assertEqual(lineas[2], "10 |  | 30")  # la columna B queda vacía

    def test_hueco_inicial_desplaza_a_su_columna(self):
        sheet = _sheet_xml(
            '<row r="1"><c r="A1"><v>1</v></c><c r="B1"><v>2</v></c></row>'
            '<row r="2"><c r="B2"><v>9</v></c></row>'  # A2 ausente
        )
        text = helpers.office2text(self._xlsx(sheet))
        self.assertEqual(text.splitlines()[2], " | 9")

    def test_filas_se_alinean_a_la_anchura_de_la_hoja(self):
        sheet = _sheet_xml(
            '<row r="1"><c r="A1"><v>1</v></c><c r="C1"><v>3</v></c></row>'
            '<row r="2"><c r="A2"><v>4</v></c></row>'   # fila corta → se rellena
            '<row r="3"><c r="A3"><v>5</v></c><c r="C3"><v>6</v></c></row>'
        )
        lineas = helpers.office2text(self._xlsx(sheet)).splitlines()
        self.assertEqual(lineas[1], "1 |  | 3")
        self.assertEqual(lineas[2], "4 |  | ")
        self.assertEqual(lineas[3], "5 |  | 6")
        self.assertTrue(all(l.count("|") == 2 for l in lineas[1:]))

    def test_soporta_inline_str(self):
        sheet = _sheet_xml(
            '<row r="1">'
            '<c r="A1" t="inlineStr"><is><t>hola</t></is></c>'
            '<c r="B1" t="inlineStr"><is><r><t>mun</t></r><r><t>do</t></r></is></c>'
            "</row>"
        )
        text = helpers.office2text(self._xlsx(sheet))
        self.assertEqual(text.splitlines()[1], "hola | mundo")

    def test_indice_shared_invalido_no_rompe(self):
        sheet = _sheet_xml(
            '<row r="1">'
            '<c r="A1" t="s"><v>99</v></c>'   # fuera de rango
            '<c r="B1" t="s"><v>x</v></c>'    # no numérico
            '<c r="C1" t="s"><v>0</v></c>'
            "</row>"
        )
        text = helpers.office2text(self._xlsx(sheet, ["ok"]))
        self.assertEqual(text.splitlines()[1], " |  | ok")

    def test_filas_vacias_se_omiten(self):
        sheet = _sheet_xml(
            '<row r="1"><c r="A1"><v>1</v></c></row>'
            '<row r="2"><c r="A2"/><c r="B2"/></row>'
            '<row r="3"/>'
        )
        lineas = helpers.office2text(self._xlsx(sheet)).splitlines()
        self.assertEqual(lineas, ["# sheet1.xml", "1"])

    def test_celdas_sin_referencia_caen_a_posicion_secuencial(self):
        sheet = _sheet_xml("<row><c><v>1</v></c><c><v>2</v></c></row>")
        self.assertEqual(
            helpers.office2text(self._xlsx(sheet)).splitlines()[1], "1 | 2"
        )

    def test_col_index(self):
        self.assertEqual(helpers._col_index("A1"), 0)
        self.assertEqual(helpers._col_index("C7"), 2)
        self.assertEqual(helpers._col_index("Z1"), 25)
        self.assertEqual(helpers._col_index("AA1"), 26)
        self.assertEqual(helpers._col_index("AB10"), 27)
        self.assertIsNone(helpers._col_index(None))
        self.assertIsNone(helpers._col_index("12"))


_DOC_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _document_xml(texto: str) -> str:
    return (
        f'<document xmlns="{_DOC_NS}"><body><p><r><t>{texto}</t></r></p></body></document>'
    )


class OfficeExpansionGuardTest(_TmpCase):
    """Un OOXML es un zip, y un zip declara cuánto expande: puede exagerar.

    `office2text` hacía `zf.read(...)` a pelo y le pasaba el resultado a
    `ET.iterparse`, así que un fichero de 70 KB que descomprime en gigabytes
    (zip bomb) se llevaba toda la memoria de la máquina antes de que nadie
    pudiese decir nada. Ahora se rechaza con un `ValueError` —el CLI ya lo
    traduce a su código de entrada inservible— **antes** de descomprimir.
    """

    def _zip(self, name: str, members: dict[str, str | bytes]) -> Path:
        p = self.tmp / name
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            for member, payload in members.items():
                zf.writestr(member, payload)
        return p

    def test_docx_desproporcionado_se_rechaza_con_los_limites_por_defecto(self):
        """Los límites que se envían valen: no hace falta recortarlos para que salten."""
        relleno = b"a" * (helpers.MAX_OFFICE_MEMBER_BYTES + 1)
        gordo = self._zip("bomba.docx", {"word/document.xml": b"<d>" + relleno + b"</d>"})
        self.assertLess(gordo.stat().st_size, 1024 * 1024)  # unos KB en disco...
        with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
            helpers.office2text(gordo)
        mensaje = str(ctx.exception)
        self.assertIn("word/document.xml", mensaje)
        self.assertIn("declara", mensaje)  # rechazado por la cabecera, sin descomprimir

    def test_el_error_es_un_valueerror_que_el_cli_ya_traduce(self):
        self.assertTrue(issubclass(helpers.OfficeTooLargeError, ValueError))

    def test_documento_valido_grande_sigue_extrayendose(self):
        """Compatibilidad: el límite deja pasar documentos de tamaño realista."""
        prosa = "palabra " * 60_000  # ~480 KB de texto real
        ok = self._zip("grande.docx", {"word/document.xml": _document_xml(prosa)})
        self.assertEqual(helpers.office2text(ok), prosa.strip())

    def test_limite_por_miembro(self):
        doc = self._zip("d.docx", {"word/document.xml": _document_xml("x" * 5000)})
        with mock.patch.object(helpers, "MAX_OFFICE_MEMBER_BYTES", 1024):
            with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
                helpers.office2text(doc)
        self.assertIn("1024", str(ctx.exception))
        # sin el límite recortado, el mismo fichero se extrae igual
        self.assertEqual(helpers.office2text(doc), "x" * 5000)

    def test_ratio_de_expansion_desproporcionado(self):
        doc = self._zip("d.docx", {"word/document.xml": _document_xml("x" * 200_000)})
        with mock.patch.object(helpers, "MAX_OFFICE_RATIO", 2), \
             mock.patch.object(helpers, "MIN_RATIO_COMPRESSED_BYTES", 64):
            with self.assertRaises(helpers.OfficeTooLargeError):
                helpers.office2text(doc)

    def test_presupuesto_total_corta_un_pptx_con_muchas_slides(self):
        slides = {
            f"ppt/slides/slide{i}.xml": f"<sld><p><t>{'s' * 2000}</t></p></sld>"
            for i in range(1, 6)
        }
        pptx = self._zip("p.pptx", slides)
        with mock.patch.object(helpers, "MAX_OFFICE_TOTAL_BYTES", 4000):
            with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
                helpers.office2text(pptx)
        self.assertIn("ppt/slides/slide", str(ctx.exception))
        # con el presupuesto por defecto las cinco slides se extraen
        self.assertEqual(helpers.office2text(pptx).count("---"), 4)

    def test_xlsx_tambien_pasa_por_el_limite(self):
        sheet = _sheet_xml('<row r="1"><c r="A1" t="inlineStr"><is><t>'
                           + "y" * 5000 + "</t></is></c></row>")
        hoja = self._zip("h.xlsx", {"xl/worksheets/sheet1.xml": sheet})
        with mock.patch.object(helpers, "MAX_OFFICE_MEMBER_BYTES", 1024):
            with self.assertRaises(helpers.OfficeTooLargeError):
                helpers.office2text(hoja)

    def test_la_lectura_real_tambien_esta_acotada(self):
        """Defensa en profundidad: el límite no depende solo de lo que declara el zip.

        La cabecera la escribe quien genera el fichero. Si la comprobación
        barata (lo declarado) no salta, la lectura sigue estando acotada y no se
        materializa más de lo permitido.
        """
        doc = self._zip("d.docx", {"word/document.xml": _document_xml("z" * 5000)})
        with mock.patch.object(helpers, "MAX_OFFICE_MEMBER_BYTES", 1024), \
             mock.patch.object(helpers._BoundedZipReader, "_check_declared",
                               lambda self, info, cap, limite: None):
            with self.assertRaises(helpers.OfficeTooLargeError):
                helpers.office2text(doc)


class HojaLegitimaPorEncimaDeLosViejos64MBTest(_TmpCase):
    """El límite por miembro no puede rechazar la hoja de cálculo que sí es real.

    `MAX_OFFICE_MEMBER_BYTES` valía 64 MB, y eso no cortaba bombas: cortaba
    documentos. Un xlsx legítimo con un `sheet1.xml` de unos 105 MB y un ratio de
    compresión corriente —muy por debajo de `MAX_OFFICE_RATIO`— salía con exit 4
    pidiendo trocearlo a mano; una hoja de cálculo de unos cientos de miles de filas
    pasa de los 100 MB de XML sin nada raro dentro. Un límite de compatibilidad que
    rechaza la entrada legítima no protege de nada. Está en 128 MB.

    La fixture apunta al **borde del límite viejo** (una hoja de 64 MB + 1) y no a
    los 105 MB reportados: es lo mínimo que distingue el antes del después, y la
    mitad de bytes. Aun así son 64 MB de XML, así que se fabrica una vez para toda
    la clase —sólo se lee— en lugar de por test: repetirla es lo que convertiría la
    suite en lenta.

    Y se fabrica con un ratio realista a propósito (x15 medido, no x1000): si la
    hoja comprimiese como un relleno de una sola letra, pasar el límite de tamaño no
    demostraría nada porque el que cortaría sería `MAX_OFFICE_RATIO`.
    """

    LIMITE_VIEJO = 64 * 1024 * 1024
    XLSX_REPORTADO = 105 * 1024 * 1024
    SHEET = "xl/worksheets/sheet1.xml"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp_clase = TemporaryDirectory()
        cls.addClassCleanup(cls._tmp_clase.cleanup)
        cls.hoja, cls.filas = cls._fabricar_hoja(Path(cls._tmp_clase.name))

    @classmethod
    def _fabricar_hoja(cls, tmp: Path) -> tuple[Path, int]:
        """xlsx de una hoja algo mayor que el límite viejo, con ratio de hoja real.

        Pocas filas y muy anchas: lo que se prueba es el techo de **bytes de XML**,
        así que la rejilla se deja pequeña —unos cientos de celdas— y no se mezcla
        con lo que vigila `MAX_XLSX_CELLS`.

        Cada fila mezcla texto repetitivo con un trozo de entropía real. Sin la
        entropía, DEFLATE comprime el relleno x1000 y el rechazo vendría del ratio;
        con ella el ratio queda donde lo deja una hoja de verdad y, de paso,
        comprimir los 64 MB cuesta medio segundo en vez de dos.
        """
        relleno = "palabra " * 12_000                     # ~96 KB comprimibles
        filas: list[str] = []
        total = n = 0
        while total <= cls.LIMITE_VIEJO:
            n += 1
            sal = base64.b32encode(os.urandom(6 * 1024)).decode()  # ~9,6 KB de entropía
            fila = (f'<row r="{n}"><c r="A{n}" t="inlineStr"><is>'
                    f"<t>{relleno}{sal}</t></is></c></row>")
            filas.append(fila)
            total += len(fila)
        p = tmp / "hoja_grande.xlsx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(cls.SHEET, _sheet_xml("".join(filas)))
        return p, n

    def _info(self) -> zipfile.ZipInfo:
        with zipfile.ZipFile(self.hoja) as zf:
            return zf.getinfo(self.SHEET)

    def test_la_hoja_es_de_verdad_el_caso_que_se_rechazaba(self):
        """La fixture sólo vale como regresión si sus números son los del caso.

        Tiene que pasarse del límite viejo (o no prueba nada), caber en el nuevo, y
        ser legítima **también** en ratio y en método: si se colase por la exención
        de `MIN_RATIO_COMPRESSED_BYTES` o por STORED, el test estaría midiendo otra
        cosa.
        """
        info = self._info()
        self.assertGreater(info.file_size, self.LIMITE_VIEJO,
                           "por debajo del límite viejo la hoja no reproduce nada")
        self.assertLessEqual(info.file_size, helpers.MAX_OFFICE_MEMBER_BYTES)
        self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED,
                         "el método que escribe Excel, no STORED")
        self.assertGreaterEqual(info.compress_size, helpers.MIN_RATIO_COMPRESSED_BYTES,
                                "el ratio se mira: no se cuela por la exención")
        ratio = info.file_size // info.compress_size
        self.assertLess(ratio, helpers.MAX_OFFICE_RATIO,
                        f"ratio x{ratio}: la hoja tiene que ser legítima en ratio")
        self.assertGreater(ratio, 2, f"ratio x{ratio}: y comprimir como un XML real")

    def test_se_extrae_en_vez_de_salir_por_entrada_inservible(self):
        texto = helpers.office2text(self.hoja)
        self.assertIn("palabra", texto)
        # Una cabecera de hoja y una línea por fila: se ha leído entera.
        self.assertEqual(texto.count("\n") + 1, self.filas + 1)
        self.assertTrue(texto.startswith("# sheet1.xml"))

    def test_con_el_limite_viejo_la_misma_hoja_se_rechazaba(self):
        """La otra mitad de la regresión: sin el cambio, esta hoja no pasa.

        Deja constancia de que lo que la dejaba fuera era exactamente el valor de la
        constante —y por la cabecera del zip, sin descomprimir—, no otra guarda.
        """
        with mock.patch.object(helpers, "MAX_OFFICE_MEMBER_BYTES", self.LIMITE_VIEJO):
            with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
                helpers.office2text(self.hoja)
        self.assertIn("declara", str(ctx.exception), "rechazada por la cabecera")
        self.assertIn(str(self.LIMITE_VIEJO), str(ctx.exception))

    def test_el_limite_admite_el_xlsx_de_105_MB_que_motivo_el_cambio(self):
        """El caso reportado, que es mayor que la fixture, tiene que caber también."""
        self.assertGreaterEqual(helpers.MAX_OFFICE_MEMBER_BYTES, self.XLSX_REPORTADO,
                                "el xlsx reportado volvería a salir por exit 4")
        self.assertGreaterEqual(helpers.MAX_OFFICE_TOTAL_BYTES,
                                helpers.MAX_OFFICE_MEMBER_BYTES,
                                "un miembro solo no puede pasarse del presupuesto")

    def test_el_tramo_nuevo_de_64_a_128_MB_no_queda_sin_vigilar(self):
        """Subir el tope no abre 64 MB nuevos: en ese tramo el que ata es el ratio.

        Es lo que sostiene que el cambio sea de compatibilidad y no de seguridad. Una
        bomba que declara 68 MB ya no se distingue por el tamaño —ahora cabe—, pero
        sigue multiplicando por miles su tamaño comprimido, y eso no ha cambiado.
        """
        declarado = self.LIMITE_VIEJO + 4 * 1024 * 1024   # entre los dos límites
        bomba = self.tmp / "bomba_en_el_tramo_nuevo.docx"
        with zipfile.ZipFile(bomba, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", b"<d>" + b"a" * declarado + b"</d>")
        info = zipfile.ZipFile(bomba).getinfo("word/document.xml")
        self.assertGreater(info.file_size, self.LIMITE_VIEJO,
                           "el tamaño ya no la corta...")
        self.assertLess(info.file_size, helpers.MAX_OFFICE_MEMBER_BYTES,
                        "...porque cabe en el límite nuevo")

        with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
            helpers.office2text(bomba)

        mensaje = str(ctx.exception)
        self.assertIn("multiplica por", mensaje, "el que corta es el ratio")
        self.assertIn(f"máximo x{helpers.MAX_OFFICE_RATIO}", mensaje)


class MensajeDelLimiteQueSaltaTest(_TmpCase):
    """El rechazo tiene que nombrar el límite que corta, no uno cualquiera.

    Los dos topes de bytes se combinan en un único techo efectivo
    (`min(por miembro, lo que queda del presupuesto)`) y el mensaje imprimía ese
    número a secas como «máximo». Cuando el que ataba era el presupuesto —el caso
    del pptx de muchas slides— el texto decía «máximo 4000» sin que 4000 fuese
    ningún límite configurado: manda a subir `MAX_OFFICE_MEMBER_BYTES`, que no es
    la constante que corta, y esconde lo único que explica el fallo, que es la
    **suma** de las slides y no la slide que aparece en el mensaje.
    """

    def _zip(self, name: str, members: dict[str, str | bytes]) -> Path:
        p = self.tmp / name
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            for member, payload in members.items():
                zf.writestr(member, payload)
        return p

    def test_cuando_ata_el_presupuesto_el_mensaje_lo_dice(self):
        slides = {
            f"ppt/slides/slide{i}.xml": f"<sld><p><t>{'s' * 2000}</t></p></sld>"
            for i in range(1, 6)
        }
        pptx = self._zip("p.pptx", slides)
        with mock.patch.object(helpers, "MAX_OFFICE_TOTAL_BYTES", 4000):
            with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
                helpers.office2text(pptx)
        mensaje = str(ctx.exception)
        self.assertIn("documento entero", mensaje, "el que corta es el del documento")
        self.assertIn("4000", mensaje, "y cita el presupuesto configurado")
        self.assertNotIn("por miembro", mensaje, "el de miembro no es el que ata")

    def test_cuando_ata_el_limite_por_miembro_el_mensaje_lo_dice(self):
        doc = self._zip("d.docx", {"word/document.xml": _document_xml("x" * 5000)})
        with mock.patch.object(helpers, "MAX_OFFICE_MEMBER_BYTES", 1024):
            with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
                helpers.office2text(doc)
        mensaje = str(ctx.exception)
        self.assertIn("máximo por miembro es 1024", mensaje)
        self.assertNotIn("documento entero", mensaje)

    def test_lo_gastado_por_una_slide_se_descuenta_en_la_siguiente(self):
        """El presupuesto que se anuncia es el que queda, no el de partida."""
        slides = {
            f"ppt/slides/slide{i}.xml": f"<sld><p><t>{'s' * 2000}</t></p></sld>"
            for i in range(1, 4)
        }
        pptx = self._zip("p.pptx", slides)
        with mock.patch.object(helpers, "MAX_OFFICE_TOTAL_BYTES", 3000):
            with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
                helpers.office2text(pptx)
        # La primera slide (2.025 bytes) cabe; la segunda se encuentra con 975.
        self.assertIn("slide2.xml", str(ctx.exception))
        self.assertIn("quedan 975 bytes de los 3000", str(ctx.exception))


class OfficeCompresionNoAdmitidaTest(_TmpCase):
    """Un OOXML sólo se comprime con STORED o DEFLATE; lo demás es una bomba.

    Los topes de tamaño y de ratio se apoyaban sin decirlo en una propiedad de
    DEFLATE: no pasa de x1032, así que un miembro por debajo de
    `MIN_RATIO_COMPRESSED_BYTES` —donde el ratio ni se mira— no puede llegar a lo
    que admite el límite por miembro. `zipfile` descomprime también BZIP2 y LZMA,
    que no tienen ese techo, y por ahí se colaba el caso que ninguna de las dos
    medidas veía: un `.docx` de **235 bytes** con un solo miembro BZIP2 que
    declaraba los 64 MB de entonces pasaba el límite por miembro (por un byte), se
    saltaba el ratio (103 bytes comprimidos) y se descomprimía entero. Medido:
    208 MB de pico de RSS y `exit 0`, una amplificación de x280.000 sobre el
    fichero.

    La fixture sigue apuntando al límite vigente, no a los 64 MB de la medición: con
    el tope por miembro en 128 MB el mismo truco pide el doble desde 266 bytes de
    zip. Que la cifra se mueva es el argumento de la guarda, no un detalle: es una
    lista blanca de métodos y se comprueba antes que nada, porque no es un problema
    de cuánto ocupa el miembro sino de con qué está comprimido.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp_clase = TemporaryDirectory()
        cls.addClassCleanup(cls._tmp_clase.cleanup)
        cls._bombas: dict[tuple[str, int, int], Path] = {}

    def _bomba(self, name: str, metodo: int, declarado: int | None = None) -> Path:
        """OOXML de unos cientos de bytes que declara `declarado` bytes de XML.

        Se cachea por clase: comprimir con BZIP2 los 128 MB que declara la bomba por
        defecto cuesta casi un segundo, y la piden tres tests. Es determinista y
        nadie la modifica —sólo se intenta abrir—, así que rehacerla por test sólo
        alarga la suite.
        """
        declarado = helpers.MAX_OFFICE_MEMBER_BYTES - 1 if declarado is None else declarado
        clave = (name, metodo, declarado)
        if clave not in self._bombas:
            p = Path(self._tmp_clase.name) / name
            with zipfile.ZipFile(p, "w", metodo) as zf:
                zf.writestr("word/document.xml", b"<d>" + b"a" * (declarado - 7) + b"</d>")
            self._bombas[clave] = p
        return self._bombas[clave]

    def test_bzip2_diminuto_ya_no_se_descomprime(self):
        bomba = self._bomba("bomba.docx", zipfile.ZIP_BZIP2)
        info = zipfile.ZipFile(bomba).getinfo("word/document.xml")
        # Se documenta el bypass: ninguna de las dos medidas anteriores lo veía.
        self.assertLess(info.compress_size, helpers.MIN_RATIO_COMPRESSED_BYTES,
                        "por debajo del mínimo para mirar el ratio")
        self.assertLessEqual(info.file_size, helpers.MAX_OFFICE_MEMBER_BYTES,
                             "y dentro del límite por miembro")
        tamaño = bomba.stat().st_size
        self.assertLess(tamaño, 1024, f"unos cientos de bytes en disco, no {tamaño}")

        with self.assertRaises(helpers.OfficeCompressionError) as ctx:
            helpers.office2text(bomba)

        mensaje = str(ctx.exception)
        self.assertIn("word/document.xml", mensaje)
        self.assertIn("BZIP2", mensaje)
        self.assertIn("STORED o DEFLATE", mensaje, "dice qué sí se admite")
        self.assertIn("sources/", mensaje, "y qué hacer con el documento")

    def test_lzma_diminuto_tampoco(self):
        bomba = self._bomba("bomba_lzma.docx", zipfile.ZIP_LZMA, declarado=8 * 1024 * 1024)
        with self.assertRaises(helpers.OfficeCompressionError) as ctx:
            helpers.office2text(bomba)
        self.assertIn("LZMA", str(ctx.exception))

    def test_no_se_abre_el_miembro_siquiera(self):
        """La prueba de que no cuesta nada: nadie llega a `ZipFile.open`.

        Es más fuerte que medir memoria: el rechazo es por la cabecera, así que el
        descompresor de BZIP2 no se instancia y da igual cuánto declare el miembro.
        """
        bomba = self._bomba("bomba.docx", zipfile.ZIP_BZIP2)

        def no_abrir(self, *a, **k):
            raise AssertionError("no debería descomprimirse ningún miembro")

        with mock.patch.object(zipfile.ZipFile, "open", no_abrir):
            with self.assertRaises(helpers.OfficeCompressionError):
                helpers.office2text(bomba)

    def test_el_pico_de_memoria_se_queda_en_nada(self):
        """El mismo fichero, con y sin la guarda: 148 MB de diferencia."""
        bomba = self._bomba("bomba.docx", zipfile.ZIP_BZIP2)
        tamaño = bomba.stat().st_size

        tracemalloc.start()
        try:
            with self.assertRaises(helpers.OfficeCompressionError):
                helpers.office2text(bomba)
            pico = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(pico, 1024 * 1024,
                        f"pico de {pico} bytes para rechazar {tamaño}")

    def test_el_rechazo_es_por_el_metodo_no_por_el_tamano(self):
        """Un miembro BZIP2 de diez bytes también sale: la guarda es previa."""
        p = self.tmp / "minusculo.docx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_BZIP2) as zf:
            zf.writestr("word/document.xml", _document_xml("hola"))
        with self.assertRaises(helpers.OfficeCompressionError):
            helpers.office2text(p)

    def test_un_metodo_desconocido_no_sale_como_notimplementederror(self):
        """`zipfile` lanza `NotImplementedError`, que el CLI no captura.

        Con el método 98 (PPMd) o el 99 (cifrado propietario) el comando salía con
        un traceback y exit 1. La lista blanca lo convierte en el mismo rechazo
        acotado que los demás, y de paso nombra el número del método.
        """
        p = self.tmp / "raro.docx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", _document_xml("hola"))
        p.write_bytes(_con_metodo_central(p.read_bytes(), 98))
        self.assertEqual(zipfile.ZipFile(p).getinfo("word/document.xml").compress_type, 98)

        with self.assertRaises(helpers.OfficeCompressionError) as ctx:
            helpers.office2text(p)
        self.assertIn("98", str(ctx.exception))
        # Sin la lista blanca, el mismo fichero llega a `open` y revienta.
        with mock.patch.object(helpers, "OFFICE_ZIP_METHODS", frozenset({0, 8, 98})):
            with self.assertRaises(NotImplementedError):
                helpers.office2text(p)

    def test_stored_y_deflate_siguen_pasando(self):
        """Compatibilidad: es lo único que escriben Word, Excel y LibreOffice."""
        for metodo in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(metodo=metodo):
                p = self.tmp / f"ok{metodo}.docx"
                with zipfile.ZipFile(p, "w", metodo) as zf:
                    zf.writestr("word/document.xml", _document_xml("acta de la reunión"))
                self.assertEqual(helpers.office2text(p), "acta de la reunión")

    def test_un_docx_mixto_stored_mas_deflate_se_extrae(self):
        """El caso real: las imágenes van STORED y el XML DEFLATE en el mismo zip."""
        p = self.tmp / "con_imagen.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr(zipfile.ZipInfo("word/media/logo.png"), b"\x89PNG" + b"\x00" * 64,
                        compress_type=zipfile.ZIP_STORED)
            zf.writestr(zipfile.ZipInfo("word/document.xml"), _document_xml("informe"),
                        compress_type=zipfile.ZIP_DEFLATED)
        self.assertEqual(helpers.office2text(p), "informe")

    def test_solo_estorba_a_los_miembros_que_se_leen(self):
        """La guarda se aplica al leer, no al abrir el zip.

        Un miembro que `office2text` no toca no cuesta nada, así que rechazar el
        documento entero por él sería rechazar de más sin ganar seguridad.
        """
        p = self.tmp / "mixto.pptx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr(zipfile.ZipInfo("ppt/slides/slide1.xml"),
                        "<sld><p><t>única slide</t></p></sld>",
                        compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr(zipfile.ZipInfo("docProps/thumbnail.jpeg"), b"j" * 4096,
                        compress_type=zipfile.ZIP_BZIP2)
        self.assertEqual(helpers.office2text(p), "única slide")

    def test_el_error_es_de_la_familia_que_el_cli_traduce(self):
        self.assertTrue(issubclass(helpers.OfficeCompressionError, helpers.OfficeInputError))
        self.assertTrue(issubclass(helpers.OfficeInputError, ValueError))
        self.assertTrue(issubclass(helpers.OfficeTooLargeError, helpers.OfficeInputError))


def _con_metodo_central(crudo: bytes, metodo: int) -> bytes:
    """Reescribe el método de compresión en la cabecera central del único miembro.

    `zipfile` no deja escribir un método que no implementa, así que el zip con un
    método desconocido —el caso que salía como `NotImplementedError`— hay que
    fabricarlo tocando los bytes. El campo son los dos que siguen a los ocho
    primeros de la cabecera `PK\\x01\\x02`.
    """
    i = crudo.index(b"PK\x01\x02")
    return crudo[: i + 10] + metodo.to_bytes(2, "little") + crudo[i + 12:]


def _zip_con_dos_entradas(nombre: bytes, extra: bytes = b"", eco: bytes | None = None) -> bytes:
    """Zip DEFLATE de un miembro, con campo extra y entradas del central a elegir.

    `zipfile` no deja escribir ninguna de las dos cosas que interesan aquí: un
    campo extra arbitrario y dos entradas del directorio central apuntando a la
    misma cabecera local. Sin argumentos sale un zip legítimo.
    """
    cuerpo = _document_xml("acta").encode("utf-8")
    datos = zlib.compress(cuerpo)[2:-4]  # deflate crudo, sin cabecera zlib
    crc = zlib.crc32(cuerpo)
    local = struct.pack(
        "<4s5H3L2H", b"PK\x03\x04", 20, 0, zipfile.ZIP_DEFLATED, 0, 0,
        crc, len(datos), len(cuerpo), len(nombre), 0,
    ) + nombre
    fichero = local + datos

    def _central(name: bytes, campo_extra: bytes = b"") -> bytes:
        return struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, zipfile.ZIP_DEFLATED, 0, 0,
            crc, len(datos), len(cuerpo), len(name), len(campo_extra), 0, 0, 0, 0, 0,
        ) + name + campo_extra

    central = _central(nombre, extra)
    entradas = 1
    if eco is not None:
        central += _central(eco)
        entradas = 2
    fin = struct.pack(
        "<4s4H2LH", b"PK\x05\x06", 0, 0, entradas, entradas,
        len(central), len(fichero), 0,
    )
    return fichero + central + fin


class AvisosDelZipTest(_TmpCase):
    """`zipfile` no siempre rechaza: hay dos casos que avisa y sigue leyendo.

    Las entradas solapadas ("possible zip bomb") y el campo extra Unicode vacío
    salían por `warnings.warn`, y un aviso no sirve aquí por dos razones. Su texto
    lo redacta el fichero —el de solape interpola el nombre del miembro, con los
    65.535 bytes que admite la cabecera—, así que un `.pptx` de 120 KB escribía
    60 KB de stderr y `office2text` devolvía texto como si nada. Y qué se hace con
    el aviso lo decide el entorno, no este código: con `warnings` en modo error
    escapaba como `UserWarning`, que el CLI no traduce a "documento que no sirve".
    """

    _NOMBRE = b"word/document.xml"

    def _extra_unicode_vacio(self, nombre: bytes) -> bytes:
        """Campo extra 0x7075 con nombre vacío. Sólo se mira si el CRC cuadra."""
        return struct.pack("<HHBL", 0x7075, 5, 1, zlib.crc32(nombre))

    def _docx(self, nombre_fichero: str, **amaño) -> Path:
        p = self.tmp / nombre_fichero
        p.write_bytes(_zip_con_dos_entradas(self._NOMBRE, **amaño))
        return p

    def test_entradas_solapadas_son_entrada_invalida(self):
        p = self._docx("solapado.docx", eco=b"[eco]")
        with self.assertRaises(helpers.OfficeStructureError) as ctx:
            helpers.office2text(p)
        self.assertIn("Overlapped entries", str(ctx.exception))
        self.assertIn("al leer el miembro", str(ctx.exception))
        self.assertIn("vuelve a guardarlo", str(ctx.exception))

    def test_campo_extra_unicode_vacio_es_entrada_invalida(self):
        """Este aviso lo emite el constructor de `ZipFile`, no la lectura.

        `ZipInfo._decodeExtra` corre al leer el directorio central, así que la
        guarda tiene que envolver también la apertura del zip.
        """
        p = self._docx("extra-vacio.docx", extra=self._extra_unicode_vacio(self._NOMBRE))
        with self.assertRaises(helpers.OfficeStructureError) as ctx:
            helpers.office2text(p)
        self.assertIn("0x7075", str(ctx.exception))
        self.assertIn("al abrir el fichero", str(ctx.exception))

    def test_el_nombre_del_miembro_solapado_se_acota(self):
        """El aviso lo escribe el fichero: 60.000 letras no son 60 KB de mensaje."""
        nombre = b"ppt/slides/slide" + b"N" * 60_000 + b".xml"
        p = self.tmp / "amplificador.pptx"
        p.write_bytes(_zip_con_dos_entradas(nombre, eco=b"[eco]"))
        with self.assertRaises(helpers.OfficeStructureError) as ctx:
            helpers.office2text(p)
        mensaje = str(ctx.exception)
        self.assertLess(len(mensaje), 1024, f"mensaje desproporcionado: {len(mensaje)}")
        self.assertIn("60020 caracteres", mensaje, "dice cuánto medía el nombre")

    def test_el_rechazo_no_depende_de_los_filtros_de_avisos(self):
        """Ni con `error` ni con `ignore` cambia el resultado.

        Con `error` el aviso escapaba como `UserWarning` (exit 1 y traceback en el
        CLI); con `ignore` se leía el documento en silencio. Las dos son formas de
        que el rechazo lo decida el entorno y no el contenido del fichero.
        """
        p = self._docx("solapado.docx", eco=b"[eco]")
        for accion in ("error", "ignore", "always"):
            with self.subTest(accion=accion):
                with warnings.catch_warnings():
                    warnings.simplefilter(accion)
                    with self.assertRaises(helpers.OfficeStructureError):
                        helpers.office2text(p)

    def test_no_deja_escapar_el_aviso_al_proceso(self):
        """El aviso no llega a `warnings`: se traduce, no se propaga ni se imprime."""
        p = self._docx("solapado.docx", eco=b"[eco]")
        with warnings.catch_warnings(record=True) as vistos:
            warnings.simplefilter("always")
            with self.assertRaises(helpers.OfficeStructureError):
                helpers.office2text(p)
        self.assertEqual([str(v.message) for v in vistos], [])

    def test_un_zip_legitimo_no_avisa_ni_se_rechaza(self):
        """Control: el mismo constructor byte a byte, sin amaños, se extrae."""
        p = self._docx("acta.docx")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertEqual(helpers.office2text(p), "acta")

    def test_el_error_es_de_la_familia_que_el_cli_traduce(self):
        self.assertTrue(issubclass(helpers.OfficeStructureError, helpers.OfficeInputError))
        self.assertTrue(issubclass(helpers.OfficeStructureError, ValueError))


class XlsxColumnaDesproporcionadaTest(_TmpCase):
    """El límite del zip no cubre lo que la hoja **pide reservar**.

    `_xlsx_text` colocaba cada celda en la columna que dice su atributo `r` y
    rellenaba los huecos con `[by_col.get(i, "") for i in range(max(by_col) + 1)]`.
    El índice sale de contar letras sin techo, así que la referencia la manda: un
    xlsx de 263 bytes con una sola celda en `ZZZZZ1` reservaba 12,3 millones de
    posiciones (321 MB de RSS, 37 MB de texto), y con 40 letras el índice es un
    entero de 54 cifras: el `range` no termina nunca y el proceso muere sin decir
    por qué. El presupuesto de bytes descomprimidos no lo ve —el XML hostil son
    unos cientos de bytes—, así que hace falta acotar la rejilla aparte.

    Se acota por los dos lados: la referencia no puede salirse de la hoja (el
    formato acaba en `XFD`, 16.384 columnas) y el total de celdas materializadas
    por documento tiene techo, porque miles de filas dentro del límite de columnas
    multiplican igual.
    """

    def _xlsx(self, sheet: str) -> Path:
        p = self.tmp / "hostil.xlsx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        return p

    def test_referencia_astronomica_se_rechaza_en_vez_de_colgar_el_proceso(self):
        hoja = self._xlsx(_sheet_xml(f'<row r="1"><c r="{"A" * 40}1"><v>1</v></c></row>'))
        self.assertLess(hoja.stat().st_size, 2048, "unos cientos de bytes en disco")

        with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
            helpers.office2text(hoja)

        mensaje = str(ctx.exception)
        # La referencia se nombra recortada: ver `ReferenciaEnElMensajeAcotadaTest`.
        self.assertIn("A" * helpers.MAX_QUOTED_CHARS, mensaje, "nombra la referencia")
        self.assertIn(str(helpers.MAX_XLSX_COLUMNS), mensaje)

    def test_referencia_grande_pero_finita_tambien(self):
        """`ZZZZZ1` cabe en un entero pequeño y aun así reserva 12 millones."""
        hoja = self._xlsx(_sheet_xml('<row r="1"><c r="ZZZZZ1"><v>1</v></c></row>'))
        with self.assertRaises(helpers.OfficeTooLargeError):
            helpers.office2text(hoja)

    def test_el_borde_de_la_hoja_pasa_y_lo_siguiente_no(self):
        ultima = self._xlsx(_sheet_xml('<row r="1"><c r="XFD1"><v>9</v></c></row>'))
        linea = helpers.office2text(ultima).splitlines()[1]
        self.assertEqual(linea.count("|"), helpers.MAX_XLSX_COLUMNS - 1)
        self.assertTrue(linea.endswith("| 9"))

        pasada = self._xlsx(_sheet_xml('<row r="1"><c r="XFE1"><v>9</v></c></row>'))
        with self.assertRaises(helpers.OfficeTooLargeError):
            helpers.office2text(pasada)

    def test_muchas_filas_anchas_agotan_el_presupuesto_de_celdas(self):
        """Cada fila cabe en la hoja; el producto filas x columnas, no.

        Con los límites por defecto: 400 filas de 16.384 columnas son 6,5 millones
        de celdas salidas de 15 KB de XML.
        """
        filas = "".join(
            f'<row r="{i}"><c r="XFD{i}"><v>{i}</v></c></row>' for i in range(1, 401)
        )
        hoja = self._xlsx(_sheet_xml(filas))
        self.assertLess(hoja.stat().st_size, 8 * 1024)

        with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
            helpers.office2text(hoja)
        self.assertIn(str(helpers.MAX_XLSX_CELLS), str(ctx.exception))

    def test_el_presupuesto_de_celdas_es_del_documento_no_de_la_hoja(self):
        p = self.tmp / "muchas.xlsx"
        fila = '<row r="1">' + "".join(
            f'<c r="{chr(65 + i)}1"><v>{i}</v></c>' for i in range(20)
        ) + "</row>"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            for n in range(1, 6):
                zf.writestr(f"xl/worksheets/sheet{n}.xml", _sheet_xml(fila * 30))
        with mock.patch.object(helpers, "MAX_XLSX_CELLS", 1000):
            with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
                helpers.office2text(p)
        self.assertIn("sheet", str(ctx.exception))
        # con el presupuesto por defecto las cinco hojas se extraen
        self.assertEqual(helpers.office2text(p).count("# sheet"), 5)

    def test_hoja_grande_legitima_sigue_extrayendose(self):
        """Compatibilidad: 5.000 filas x 10 columnas reales no roza el límite."""
        filas = "".join(
            f'<row r="{i}">'
            + "".join(f'<c r="{chr(65 + c)}{i}"><v>{c}</v></c>' for c in range(10))
            + "</row>"
            for i in range(1, 5001)
        )
        hoja = self._xlsx(_sheet_xml(filas))
        lineas = helpers.office2text(hoja).splitlines()
        self.assertEqual(len(lineas), 5001)  # cabecera de hoja + 5.000 filas
        self.assertEqual(lineas[1], " | ".join(str(c) for c in range(10)))

    def test_col_index_no_calcula_el_entero_de_una_referencia_absurda(self):
        """El techo se aplica al contar, no después: el entero nunca se completa."""
        # Se para en el primer índice que ya está fuera de la hoja y devuelve ese
        # centinela: el llamador sólo necesita saber que se sale.
        self.assertEqual(helpers._col_index("A" * 40 + "1"), helpers.MAX_XLSX_COLUMNS)
        self.assertEqual(helpers._col_index("XFD1"), helpers.MAX_XLSX_COLUMNS - 1)

    def test_el_error_lo_traduce_el_cli_como_cualquier_entrada_inservible(self):
        self.assertTrue(issubclass(helpers.OfficeTooLargeError, ValueError))


class ReferenciaEnElMensajeAcotadaTest(_TmpCase):
    """Rechazar la hoja hostil no puede costar lo que el fichero quiera de stderr.

    El mensaje citaba `{ref!r}` tal cual, y `ref` es el atributo `r` de una celda:
    lo escribe quien fabrica el xlsx. El techo de bytes descomprimidos no lo tapa
    —una referencia de 2 MB de letras comprime a 2 KB, y por debajo de
    `MIN_RATIO_COMPRESSED_BYTES` ni se mira el ratio—, así que un fichero de 2,3 KB
    producía 2 MB de mensaje: x900 de amplificación hacia el stderr de quien lo
    rechaza, que es la skill, un log o la CI. Ahora se cita recortado y la longitud
    del mensaje deja de depender del documento.
    """

    def _xlsx(self, ref: str) -> Path:
        p = self.tmp / "amplificador.xlsx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "xl/worksheets/sheet1.xml",
                _sheet_xml(f'<row r="1"><c r="{ref}"><v>1</v></c></row>'),
            )
        return p

    def test_una_referencia_enorme_no_amplifica_el_mensaje(self):
        ref = "A" * (2 * 1024 * 1024) + "1"
        hoja = self._xlsx(ref)
        tamaño = hoja.stat().st_size
        self.assertLess(tamaño, 8 * 1024, "unos KB en disco pedían megas de mensaje")

        with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
            helpers.office2text(hoja)

        mensaje = str(ctx.exception)
        self.assertLess(len(mensaje), 2048, "el mensaje cabe en una pantalla")
        self.assertLess(len(mensaje), tamaño, "y no amplifica: menos texto que fichero")
        self.assertIn("A" * helpers.MAX_QUOTED_CHARS, mensaje, "sigue nombrándola")
        self.assertIn(str(len(ref)), mensaje, "y dice cuánta se ha recortado")

    def test_una_referencia_normal_se_cita_entera(self):
        """Recortar no puede empeorar el mensaje del caso que se lee de verdad."""
        hoja = self._xlsx("ZZZZZ1")
        with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
            helpers.office2text(hoja)
        self.assertIn("'ZZZZZ1'", str(ctx.exception))
        self.assertNotIn("caracteres)", str(ctx.exception), "sin nota de recorte")

    def test_quoted_acota_por_caracteres_y_dice_cuantos_habia(self):
        self.assertEqual(helpers.quoted("XFD1"), "'XFD1'")
        largo = "B" * 500
        acotado = helpers.quoted(largo)
        self.assertIn("500 caracteres", acotado)
        self.assertLess(len(acotado), 100)
        # El borde: justo en el límite se cita entera, un carácter más se recorta.
        justo = "C" * helpers.MAX_QUOTED_CHARS
        self.assertEqual(helpers.quoted(justo), repr(justo))
        self.assertIn("caracteres", helpers.quoted(justo + "C"))

    def test_el_nombre_del_miembro_del_zip_tambien_se_acota(self):
        """La otra cadena que pone el fichero: el nombre del miembro que expande."""
        p = self.tmp / "bomba.docx"
        nombre = "word/" + "x" * 4000 + "document.xml"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(nombre, b"<d>" + b"a" * (helpers.MAX_OFFICE_MEMBER_BYTES + 1))
        reader = helpers._BoundedZipReader(zipfile.ZipFile(p), p)
        with self.assertRaises(helpers.OfficeTooLargeError) as ctx:
            reader.read(nombre)
        self.assertLess(len(str(ctx.exception)), 1024)


class XlsxNoRetieneLaHojaEnteraTest(_TmpCase):
    """Leer una hoja fila a fila no puede costar la hoja entera en memoria.

    `ET.iterparse` emite los eventos mientras lee, pero construye el árbol completo
    debajo: el bucle recorría las filas sin soltar ninguna, así que al llegar a la
    última seguían vivas todas las anteriores con cada `<c>` y cada `<v>` dentro.
    Una hoja de 10 MB de XML —500 KB de zip, dentro de todos los límites— costaba
    173 MB de pico para producir 700 KB de texto, y `MAX_OFFICE_MEMBER_BYTES` deja
    entrar 128 MB de XML por miembro: el orden de magnitud es el giga.

    El coste ahora es proporcional a lo que se lee, no al número de nodos: se
    comprueba que las filas ya procesadas se liberan de verdad (no es un detalle de
    rendimiento: es lo que acota el pico) y que el texto y los límites no cambian.
    """

    N_FILAS = 20_000
    COLUMNAS = "ABCDE"

    def _hoja_grande(self) -> Path:
        filas = "".join(
            f'<row r="{i}">' + "".join(
                f'<c r="{c}{i}" t="inlineStr"><is><t>v{i}</t></is></c>'
                for c in self.COLUMNAS
            ) + "</row>"
            for i in range(1, self.N_FILAS + 1)
        )
        p = self.tmp / "grande.xlsx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(filas))
        return p

    def test_el_pico_de_memoria_es_proporcional_al_xml_no_a_los_nodos(self):
        import tracemalloc

        hoja = self._hoja_grande()
        with zipfile.ZipFile(hoja) as zf:
            xml_bytes = zf.getinfo("xl/worksheets/sheet1.xml").file_size

        tracemalloc.start()
        try:
            base = tracemalloc.get_traced_memory()[0]
            texto = helpers.office2text(hoja)
            _actual, pico = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(len(texto.splitlines()), self.N_FILAS + 1, "cabecera + filas")
        # Medido sobre estas 20.000 filas: sin liberar el pico es 15-16x los bytes
        # del XML (el árbol de 100.000 nodos vivos a la vez); liberando es 2,5-3,5x,
        # y lo que queda ya no es el árbol sino lo inevitable —los bytes del miembro,
        # su copia en el `BytesIO` y el texto que se está produciendo—. El umbral va
        # en medio con holgura x2 por cada lado, para no depender de cuánto ocupa un
        # `Element` en cada versión de CPython.
        self.assertLess(
            pico - base, 7 * xml_bytes,
            f"pico {(pico - base) / 1e6:.0f} MB para {xml_bytes / 1e6:.0f} MB de XML: "
            "se está reteniendo el árbol XML entero",
        )

    def test_cada_fila_se_libera_antes_de_leer_la_siguiente(self):
        """La comprobación directa: la fila anterior ya no está viva.

        Un weakref muerto sólo puede significar que nadie la retiene —ni el padre
        que `iterparse` va llenando—, que es exactamente la propiedad que acota el
        pico. Medir memoria dice que el pico bajó; esto dice por qué.
        """
        import weakref

        xml = _sheet_xml("".join(
            f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 6)
        )).encode()

        anterior = None
        leidos = []
        for fila in helpers._iter_freed(xml, "row"):
            if anterior is not None:
                self.assertIsNone(anterior(), "la fila anterior sigue retenida")
            leidos.append(fila.get("r"))
            self.assertEqual(len(fila), 1, "y la actual sí está entera al recibirla")
            anterior = weakref.ref(fila)
            del fila

        self.assertEqual(leidos, ["1", "2", "3", "4", "5"], "y se leen todas")

    def test_el_texto_y_los_limites_no_cambian(self):
        """Control: liberar filas no altera lo que se extrae ni cuándo se corta."""
        hoja = self.tmp / "mixta.xlsx"
        with zipfile.ZipFile(hoja, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/sharedStrings.xml", _shared_xml(["norte", "sur"]))
            zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
                '<row r="2"><c r="A2"><v>7</v></c></row>'
            ))
        self.assertEqual(
            helpers.office2text(hoja).splitlines(),
            # La última línea llega sin el relleno final: `office2text` hace `strip()`.
            ["# sheet1.xml", "norte |  | sur", "7 |  |"],
        )

    def test_sigue_cortando_la_hoja_que_pide_demasiada_rejilla(self):
        filas = '<row r="1"><c r="XFD1"><v>1</v></c></row>' * 300
        hoja = self.tmp / "rejilla.xlsx"
        with zipfile.ZipFile(hoja, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(filas))
        with self.assertRaises(helpers.OfficeTooLargeError):
            helpers.office2text(hoja)


class OfficeOtrosFormatosTest(_TmpCase):
    """Regresión: docx/pptx siguen funcionando igual."""

    def test_docx(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        p = self.tmp / "d.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr(
                "word/document.xml",
                f'<document xmlns="{ns}"><body>'
                "<p><r><t>Hola</t></r></p><p><r><t>mundo</t></r></p>"
                "</body></document>",
            )
        self.assertEqual(helpers.office2text(p), "Hola\nmundo")

    def test_formato_no_soportado(self):
        p = self.tmp / "x.odt"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("dummy", "x")
        with self.assertRaises(ValueError):
            helpers.office2text(p)


if __name__ == "__main__":
    unittest.main()
