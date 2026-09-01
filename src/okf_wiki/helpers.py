"""okf_wiki helpers — utilidades sin LLM para la skill `okf-ingest`.

Todo lo "determinista" del pipeline vive aquí (la síntesis la hace Claude):
- context:      estado de `purpose.md`/`schema.md` (el ancla de dominio).
- scan:         detecta qué es nuevo/cambiado/borrado en la dropzone `sources/`.
- office2text:  extrae texto de docx/pptx/xlsx con la stdlib (sin dependencias).
- verify:       valida que todos los .md del bundle son OKF válidos.
- commit_state: reescribe el manifest de ingesta.

El motor OKF (reference_agent) solo se importa de forma perezosa en `verify`.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat as stat_mod
import warnings
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from okf_wiki import templates

STATE_FILENAME = ".ingest-state.json"
SOURCES_DIRNAME = "sources"
WIKI_DIRNAME = "wiki"

# Contexto de la instancia: viven en la RAÍZ, nunca dentro de `wiki/`.
# Dentro del bundle serían conceptos OKF inválidos (no tienen frontmatter) y
# romperían `verify`; fuera, ni `index`, ni `viz`, ni `verify` los miran.
PURPOSE_FILENAME = "purpose.md"
SCHEMA_FILENAME = "schema.md"
# Los dos son contexto, no páginas: se excluyen del recorrido del bundle aunque
# caigan dentro de él (ver `is_bundle_page`).
CONTEXT_FILENAMES = (PURPOSE_FILENAME, SCHEMA_FILENAME)


class IngestError(Exception):
    """Error de ingesta que exige intervención: nunca se degrada en silencio."""


class MissingSourcesError(IngestError):
    """La instancia o su dropzone `sources/` no existen."""


class MissingInstanceError(IngestError):
    """La ruta no es la carpeta de una instancia (no existe, es un fichero, o es `wiki/`).

    La usa `context`, que es el único comando que trabaja con la RAÍZ de la
    instancia y no con el bundle: sin este guard, una ruta equivocada —o el propio
    `wiki/` en su lugar— reportaba `purpose.md`/`schema.md` como `FALTA`, el mismo
    mensaje que una instancia antigua legítima, y la ingesta seguía sin ancla de
    dominio creyendo que esa wiki simplemente no tenía contexto.
    """


class MissingBundleError(IngestError):
    """La ruta no contiene un bundle OKF con páginas que procesar.

    Se prefiere fallar a tratarla como un bundle vacío: `verify` respondía
    `{"checked": 0, "ok": true}` con exit 0 y `index` decía "0 regenerados",
    así que un typo en la ruta se leía como "la wiki está impecable".
    """


class BundleAsInstanceError(IngestError):
    """A `build`/`watch` se les ha pasado el bundle `wiki/` en vez de la instancia.

    No es un caso más de `MissingBundleError`: la ruta **sí** tiene páginas, así
    que `resolve_bundle` la acepta como bundle plano y el sitio se construye igual
    —pero mudo. `build` necesita las dos rutas: el bundle (las páginas) y la raíz
    de la instancia, de donde salen el `purpose.md` de la portada y los enlaces a
    `sources/` de la procedencia. Con `wiki/` por instancia, el propósito nunca
    aparece y los `sources[].resource` apuntan dentro del bundle, a ficheros que
    no existen: una wiki a medias que se lee como una wiki completa.
    """


class CorruptStateError(IngestError):
    """El manifest `.ingest-state.json` existe pero no es legible.

    Se prefiere fallar a asumir `{"files": {}}`: un manifest corrupto tratado
    como vacío reingiere toda la dropzone y duplica conceptos en la wiki.
    """


class UnsafeSourceError(IngestError):
    """La dropzone contiene una entrada que `scan` no puede ingerir con garantías.

    El modelo de contención es de una línea: **todo lo que `scan` lee tiene que
    estar dentro de `sources/`, y por un camino sin enlaces**. `sources/` es la
    única superficie de entrada de la wiki y el manifest identifica cada fuente
    por su ruta **relativa** a la dropzone; leer algo de fuera rompe las dos cosas
    a la vez: la skill sintetiza en la wiki lo que hubiera al otro lado (sea
    `~/.ssh/id_rsa` o una carpeta entera enlazada) y `commit_state` lo anota con
    una ruta `sources/…` que no corresponde a ningún fichero de la instancia.

    Se rechaza, por ese motivo, todo lo que no se puede afirmar contenido:

    - el enlace simbólico que apunta fuera de la dropzone (a fichero o a carpeta,
      y también roto: en cuanto el destino exista ya está fuera);
    - el circular, que no tiene destino que mirar;
    - el que apunta dentro pero a algo que no se puede ingerir —roto, o un FIFO,
      socket o dispositivo— y el que apunta a una ruta oculta, excluida a
      propósito del recorrido;
    - el **enlace duro**: un fichero con más de un nombre, de los cuales los otros
      pueden estar fuera de la dropzone sin que `scan` pueda verlo (`st_nlink` los
      cuenta, pero no dice dónde están). Aceptarlo dejaría entrar por la puerta de
      al lado exactamente lo que se rechaza cuando llega por un enlace simbólico;
    - cualquier componente de la ruta —también un directorio intermedio— que
      cambie a enlace entre el recorrido y la lectura.

    Se prefiere fallar a ignorarlo en silencio: quien dejó eso en la dropzone
    espera que ese material se ingiera, y saltárselo sin decir nada deja una wiki
    incompleta que se lee como completa.
    """


def wiki_root(instance: "Path | str") -> Path:
    """Devuelve el bundle OKF de una instancia: `<instancia>/wiki` si existe.

    Separar `sources/` (crudo) de `wiki/` (bundle) evita que el motor OKF meta
    los documentos originales en los índices y en el grafo. Si no hay subcarpeta
    `wiki/`, se asume que la ruta ya es un bundle plano (compatibilidad).
    """
    w = Path(instance) / WIKI_DIRNAME
    return w if w.is_dir() else Path(instance)


def is_bundle_page(rel: "Path | str") -> bool:
    """¿La ruta `rel` (relativa al bundle) es una página que el bundle contiene?

    Es el único criterio de "qué mira el bundle", compartido por `has_pages`,
    `verify` y el lint v0.2 para que no puedan discrepar. Quedan fuera:

    - la dropzone `sources/`: son los documentos crudos, no páginas OKF;
    - `purpose.md` y `schema.md` **en la raíz del bundle**: son contexto de la
      instancia. En una instancia normal viven fuera de `wiki/` y no hacía falta
      excluirlos, pero con un **bundle plano** (`<bundle>` es a la vez la raíz de
      la instancia) caían dentro del recorrido y `verify` reportaba dos errores de
      documento sin frontmatter por dos ficheros que a propósito no son páginas.
    """
    parts = Path(rel).parts
    if SOURCES_DIRNAME in parts:
        return False
    if len(parts) == 1 and parts[0] in CONTEXT_FILENAMES:
        return False
    return True


def has_pages(bundle: Path) -> bool:
    """¿Hay alguna página `.md` en el bundle, fuera de `sources/` y del contexto?"""
    bundle = Path(bundle)
    for md in bundle.rglob("*.md"):
        if is_bundle_page(md.relative_to(bundle)):
            return True
    return False


def resolve_dropzone(instance: "Path | str") -> Path:
    """Devuelve la dropzone `<instancia>/sources/`, exigiendo que **ya** exista.

    La comparten `scan` (que la lee) y `commit_state` (que escribe el manifest
    dentro de ella) para que ambos acepten exactamente las mismas rutas.
    `commit_state` la creaba con `mkdir(parents=True)`, así que
    `commit-state /ruta/con/typo` inventaba el árbol entero, imprimía «manifest
    actualizado» con exit 0 y dejaba la ruta falsa lo bastante formada como para
    que el `scan` siguiente también pasase: el typo se volvía invisible y el
    manifest real nunca se marcaba.
    """
    instance = Path(instance)
    if not instance.exists():
        raise MissingSourcesError(
            f"La instancia no existe: {instance}. "
            "Comprueba la ruta o créala con `okf-wiki init <dir>`."
        )
    if not instance.is_dir():
        raise MissingSourcesError(
            f"No es un directorio: {instance}. Se esperaba la instancia que "
            f"contiene la dropzone `{SOURCES_DIRNAME}/`."
        )
    sources_dir = instance / SOURCES_DIRNAME
    if not sources_dir.is_dir():
        detalle = "existe pero no es un directorio" if sources_dir.exists() else "no existe"
        raise MissingSourcesError(
            f"Falta la dropzone: {sources_dir} {detalle}. "
            "Crea la instancia con `okf-wiki init` antes de escanear o de "
            "commitear estado."
        )
    return sources_dir


def resolve_bundle(instance: "Path | str") -> Path:
    """Como `wiki_root`, pero exige que el bundle exista y tenga páginas.

    `wiki_root` cae al propio directorio cuando no hay `wiki/` — comportamiento
    correcto para bundles planos, pero que convertía cualquier ruta equivocada en
    un bundle vacío que validaba «bien». Los comandos que consumen el bundle
    (`index`, `viz`, `verify`) usan esta versión para fallar con exit 3 en vez de
    devolver un falso ok.
    """
    instance = Path(instance)
    if not instance.exists():
        raise MissingBundleError(
            f"La instancia no existe: {instance}. "
            "Comprueba la ruta o créala con `okf-wiki init <dir>`."
        )
    if not instance.is_dir():
        raise MissingBundleError(f"No es un directorio: {instance}.")
    wiki = instance / WIKI_DIRNAME
    bundle = wiki if wiki.is_dir() else instance
    if has_pages(bundle):
        return bundle
    if bundle == instance:
        raise MissingBundleError(
            f"No hay bundle OKF en {instance}: no existe la subcarpeta "
            f"`{WIKI_DIRNAME}/` ni hay ningún `.md` que tratar como bundle plano."
        )
    raise MissingBundleError(
        f"El bundle {bundle} no tiene ninguna página `.md` que procesar."
    )


def looks_like_bundle_dir(path: Path) -> bool:
    """¿`path` es el bundle `wiki/` de una instancia, y no la instancia?

    Se decide por el nombre de la carpeta más el aspecto de su padre, no por el
    contenido: un `wiki/` recién creado y vacío tiene que reconocerse igual.
    Dos salvaguardas evitan el falso positivo de una instancia que se llame
    literalmente `wiki`: si trae su propio `wiki/` dentro, o si trae su propio
    contexto en la raíz, es una instancia.
    """
    path = Path(path)
    if path.name != WIKI_DIRNAME:
        return False
    if (path / WIKI_DIRNAME).is_dir():
        return False
    if any((path / name).is_file() for name in CONTEXT_FILENAMES):
        return False
    parent = path.parent
    return (parent / SOURCES_DIRNAME).is_dir() or any(
        (parent / name).is_file() for name in CONTEXT_FILENAMES
    )


def resolve_instance(path: "Path | str") -> Path:
    """Valida la carpeta de la **instancia** (la que contiene el contexto y `wiki/`).

    Es el equivalente de `resolve_bundle` para el único comando que trabaja un
    nivel más arriba, `context`. Rechaza las tres formas de ruta equivocada —no
    existe, es un fichero, o es el bundle `wiki/`— porque las tres producían la
    misma salida engañosa: `purpose.md`/`schema.md` como `FALTA` con exit 0, que
    es indistinguible de una instancia antigua sin contexto (caso legítimo que
    **sí** sigue devolviendo 0).
    """
    instance = Path(path)
    if not instance.exists():
        raise MissingInstanceError(
            f"La instancia no existe: {instance}. "
            "Comprueba la ruta o créala con `okf-wiki init <dir>`."
        )
    if not instance.is_dir():
        raise MissingInstanceError(
            f"No es un directorio: {instance}. Se esperaba la carpeta de la instancia, "
            f"la que contiene `{PURPOSE_FILENAME}`, `{SCHEMA_FILENAME}`, "
            f"`{SOURCES_DIRNAME}/` y `{WIKI_DIRNAME}/`."
        )
    if looks_like_bundle_dir(instance):
        raise MissingInstanceError(
            f"{instance} es el bundle `{WIKI_DIRNAME}/`, no la instancia: el contexto "
            f"(`{PURPOSE_FILENAME}`, `{SCHEMA_FILENAME}`) vive en la raíz, un nivel más "
            f"arriba. Prueba con `okf-wiki context {instance.parent}`."
        )
    return instance


def resolve_site_instance(instance: "Path | str") -> tuple[Path, Path]:
    """Instancia **y** bundle, para los comandos que necesitan las dos rutas.

    `build` y `watch` no consumen solo el bundle: leen `purpose.md` para la portada
    y enlazan `sources/` desde la procedencia, y las dos cosas viven en la raíz de
    la instancia, fuera de `wiki/`. Pasarles `wiki/` no daba error —`resolve_bundle`
    lo trataba como un bundle plano legítimo—, así que con `--out` fuera se
    construía un sitio sin propósito y con las fuentes apuntando a rutas que no
    existen, y con el `--out` por defecto se rechazaba con un «la salida está dentro
    del bundle» que no nombra la causa real. Aquí se ataja antes de escribir nada.

    Un bundle plano de verdad (una carpeta de páginas que no se llama `wiki/`, o
    cuyo padre no tiene el aspecto de una instancia) sigue aceptándose: lo decide
    `looks_like_bundle_dir`, el mismo criterio que usa `context`.
    """
    instance = Path(instance)
    if looks_like_bundle_dir(instance):
        raise BundleAsInstanceError(
            f"{instance} es el bundle `{WIKI_DIRNAME}/`, no la instancia: el sitio se "
            f"construye desde la carpeta que contiene `{WIKI_DIRNAME}/`, que es donde "
            f"viven `{PURPOSE_FILENAME}` y `{SOURCES_DIRNAME}/`.\n"
            f"Repite el mismo comando un nivel más arriba: {instance.parent}."
        )
    return instance, resolve_bundle(instance)

# --------------------------------------------------------------------------- #
# context: `purpose.md` + `schema.md`, el ancla de dominio de la instancia     #
# --------------------------------------------------------------------------- #
def _context_file(instance: Path, filename: str) -> dict:
    """Estado de un fichero de contexto. **Nunca lanza**: informa.

    Una instancia antigua que no los tenga, o una ruta que ni siquiera existe,
    se reportan como `exists: false`. La alternativa —fallar— convertiría una
    mejora de calidad de la síntesis en un requisito de formato, y dejaría sin
    ingerir wikis creadas antes de que estos ficheros existieran.
    """
    path = instance / filename
    entry: dict = {
        "filename": filename,
        "path": path.as_posix(),
        "exists": False,
        "stub": False,
        "content": None,
    }
    if not path.is_file():
        return entry
    entry["exists"] = True
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        entry["error"] = f"no se pudo leer {path}: {exc}"
        return entry
    entry["content"] = content
    entry["stub"] = templates.is_stub(content)
    return entry


def context_status(instance: "Path | str") -> dict:
    """Contexto de la instancia: `purpose.md` y `schema.md` con su estado.

    `ready` es la única señal que la skill necesita mirar: el propósito existe y
    está relleno. Se decide por el marcador `okf-wiki:stub` (una comprobación de
    subcadena) y no por si el texto "parece" una plantilla, que es justo el tipo
    de juicio que un LLM hace de forma inconsistente entre ingestas.

    `schema.md` no entra en `ready` a propósito: sin taxonomía declarada la
    ingesta puede seguir (el agente propone una y se ofrece a volcarla ahí),
    mientras que sin propósito no hay nada contra lo que decidir el alcance.
    """
    instance = Path(instance)
    purpose = _context_file(instance, PURPOSE_FILENAME)
    schema = _context_file(instance, SCHEMA_FILENAME)
    return {
        "instance": instance.as_posix(),
        "purpose": purpose,
        "schema": schema,
        "ready": bool(purpose["exists"] and not purpose["stub"] and purpose["content"]),
    }


# Formatos que Claude lee de forma nativa (no necesitan extracción previa).
NATIVE_READ_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".yaml", ".yml",
}
# Formatos Office que extrae `office2text`.
OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
# HTML que extrae `html2text` (a markdown limpio).
HTML_EXTS = {".html", ".htm"}


# --------------------------------------------------------------------------- #
# scan: ingesta incremental por hash                                          #
# --------------------------------------------------------------------------- #
# `openat` —`os.open(nombre, …, dir_fd=fd)`— es lo que permite bajar por la ruta
# componente a componente sin volver a recorrerla desde la raíz. Existe en POSIX;
# en Windows no hay ni eso ni `O_NOFOLLOW`, así que allí se abre por ruta y esta
# defensa (como el resto de la contención basada en enlaces) no aplica.
_HAS_OPENAT = os.open in getattr(os, "supports_dir_fd", frozenset())
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# `os.open` no es `open(path, "rb")`: en Windows delega en el CRT, que sin
# `O_BINARY` abre en modo **texto** —convierte `\r\n` en `\n` y trata el primer
# `0x1A` como fin de fichero—. Leer así una fuente daría el sha256 y el tamaño de
# una versión mutilada de sus bytes, así que cualquier `.md` con CRLF saldría
# `changed` en cada `scan` y la skill lo reingeriría en bucle. En POSIX la
# constante no existe y vale 0.
_O_BINARY = getattr(os, "O_BINARY", 0)
# `O_NOFOLLOW` sobre un enlace: `ELOOP` en Linux y macOS, `EMLINK` en algunos BSD.
_SYMLINK_ERRNOS = (errno.ELOOP, errno.EMLINK)


def _open_dropzone(sources_dir: Path) -> int:
    """Abre la dropzone misma, siguiendo enlaces: es la raíz de la contención.

    Aquí sí se siguen, y a propósito: la instancia entera puede estar detrás de un
    enlace (`/tmp` en macOS, un worktree enlazado) y eso no es una fuga. La
    contención se define **relativa a esta raíz ya resuelta** —el mismo criterio
    que usa `_is_within`—, así que lo que hay por encima da igual.
    """
    try:
        return os.open(sources_dir, os.O_RDONLY | _O_DIRECTORY)
    except OSError as exc:
        raise IngestError(
            f"No se pudo abrir la dropzone {sources_dir}: {exc.strerror or exc}. "
            f"Arregla los permisos o la ruta: sin poder leerla, `scan` no puede "
            f"decir qué hay que ingerir."
        ) from exc


def _descend(name: str, dir_fd: int, shown: Path) -> int:
    """Baja un nivel a `name` dentro de `dir_fd`, sin seguir enlaces.

    Es la mitad que faltaba de la defensa contra el cambiazo. `O_NOFOLLOW` sólo
    protege el **último** componente de una ruta, así que abrir
    `sources/tema/doc.md` de una vez dejaba libre todo lo de en medio: quien puede
    escribir en la dropzone sustituye `sources/tema` por un enlace a `/etc` entre
    el recorrido y la lectura, y `scan` hashea `/etc/doc.md` como si fuese una
    fuente de la instancia — exactamente lo que se rechaza cuando el enlace está
    en el nombre del fichero. Bajando componente a componente con `O_NOFOLLOW`,
    cada directorio intermedio recibe la misma comprobación que la hoja.
    """
    try:
        return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        # `O_DIRECTORY | O_NOFOLLOW` sobre un enlace a carpeta no da el mismo
        # errno en todas partes (`ELOOP` en Linux, `ENOTDIR` en macOS), así que
        # el mensaje no se deduce del código: se mira qué hay ahí ahora.
        if exc.errno in _SYMLINK_ERRNOS or exc.errno == errno.ENOTDIR:
            raise UnsafeSourceError(_changed_component_message(name, dir_fd, shown)) from exc
        raise IngestError(
            f"No se pudo entrar en {shown} dentro de la dropzone: "
            f"{exc.strerror or exc}. Arregla los permisos o saca esa carpeta de "
            f"`{SOURCES_DIRNAME}/`: saltársela en silencio deja la wiki incompleta."
        ) from exc


def _changed_component_message(name: str, dir_fd: int, shown: Path) -> str:
    """Qué decir de un tramo intermedio que ya no es el directorio que se recorrió."""
    try:
        st_mode = os.lstat(name, dir_fd=dir_fd).st_mode
    except OSError:  # ha desaparecido entre el fallo y esta mirada
        st_mode = 0
    if stat_mod.S_ISLNK(st_mode):
        return (
            f"{shown} ha pasado a ser un enlace simbólico entre el recorrido de la "
            f"dropzone y la lectura de sus fuentes.\n"
            f"`scan` no lo sigue: lo que hay debajo de un directorio enlazado no es lo "
            f"que se comprobó, y puede estar fuera de la instancia — que es justo lo "
            f"que se rechaza cuando el enlace está en el nombre del fichero.\n"
            f"Vuelve a lanzar `scan` con la dropzone quieta."
        )
    return (
        f"{shown} ha dejado de ser un directorio bajo el propio `scan`"
        + (f" ({stat_mod.filemode(st_mode)})" if st_mode else "")
        + ".\nVuelve a lanzarlo con la dropzone quieta."
    )


def _open_leaf(name: "str | Path", dir_fd: "int | None", shown: Path) -> int:
    """Abre la fuente ya validada sin volver a seguir ningún enlace.

    `O_NOFOLLOW` cierra el hueco entre validar y leer en el último componente: el
    recorrido comprueba a dónde apunta cada enlace, pero entre esa comprobación y
    la lectura pasa tiempo, y quien puede escribir en la dropzone puede sustituir
    el fichero por un enlace a `~/.ssh/id_rsa` justo en medio. Como aquí solo
    llegan rutas cuyo último componente **no** era un enlace al validarlo (los
    enlaces de dentro entran ya resueltos), que ahora lo sea significa que alguien
    la ha cambiado: `ELOOP` y se aborta, en vez de hashear lo que hubiera detrás.

    `O_NONBLOCK` evita el otro final infeliz del mismo cambio: si la ruta pasa a
    ser un FIFO, abrirla en bloqueo deja el `scan` colgado para siempre esperando
    a un escritor. En un fichero regular POSIX lo ignora.

    `O_BINARY` no defiende de nada: es lo que hace que en Windows —el único sitio
    donde `dir_fd` es `None`, porque no hay `openat`— se lean los bytes del fichero
    y no la traducción de texto del CRT (ver `_O_BINARY`).
    """
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_BINARY | getattr(os, "O_NONBLOCK", 0)
    try:
        return os.open(name, flags) if dir_fd is None else os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise UnsafeSourceError(
                f"{shown} ha pasado a ser un enlace simbólico entre la validación de "
                f"la dropzone y su lectura.\n"
                f"`scan` no lo sigue: hashear el destino significaría meter en la wiki "
                f"algo distinto de lo que se comprobó.\n"
                f"Vuelve a lanzar `scan` con la dropzone quieta."
            ) from exc
        raise IngestError(
            f"No se pudo leer la fuente {shown}: {exc.strerror or exc}. "
            f"Arregla el fichero o sácalo de `{SOURCES_DIRNAME}/`: saltárselo en "
            f"silencio deja la wiki incompleta."
        ) from exc


def _open_source(sources_dir: Path, rel: Path) -> int:
    """Abre `sources_dir / rel` sin atravesar ni un solo enlace por el camino.

    `rel` viene del recorrido y es una ruta **relativa a la dropzone y sin
    enlaces** (los enlaces internos entran ya resueltos: ver `_collect_sources`).
    Abrirla componente a componente desde un descriptor de la dropzone es lo que
    convierte esa promesa en una comprobación: si alguien cambia cualquier tramo
    mientras tanto, la apertura falla en vez de acabar leyendo fuera.
    """
    shown = sources_dir / rel
    if not _HAS_OPENAT:  # Windows: sin `openat` ni `O_NOFOLLOW` no hay contención
        return _open_leaf(shown, None, shown)
    parts = rel.parts
    dir_fd = _open_dropzone(sources_dir)
    try:
        for depth, part in enumerate(parts[:-1], start=1):
            deeper = _descend(part, dir_fd, sources_dir.joinpath(*parts[:depth]))
            os.close(dir_fd)
            dir_fd = deeper
        return _open_leaf(parts[-1], dir_fd, shown)
    finally:
        os.close(dir_fd)


def _hash_and_size(sources_dir: Path, rel: Path) -> tuple[str, int]:
    """sha256 y tamaño de la fuente, leídos de una sola apertura.

    Antes eran dos aperturas por nombre —`_hash_file(path)` y `path.stat()`— con
    la ruta viva entre medias: bastaba con sustituir el fichero después del hash
    para que el manifest anotase el sha256 de un contenido y el tamaño de otro. El
    tamaño se cuenta sobre los mismos bytes que alimentan el hash, así que las dos
    columnas del manifest describen siempre lo mismo.

    El `fstat` de ese mismo descriptor es también donde se decide si la fuente es
    ingerible: fichero regular y con **un solo nombre**. Ambas cosas se miran
    sobre el fd ya abierto, no sobre la ruta, para que la respuesta describa
    exactamente los bytes que se van a hashear.
    """
    shown = sources_dir / rel
    fd = _open_source(sources_dir, rel)
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise UnsafeSourceError(
                f"{shown} ya no es un fichero regular ({stat_mod.filemode(st.st_mode)}): "
                f"ha cambiado bajo el propio `scan`.\n"
                f"Vuelve a lanzarlo con la dropzone quieta."
            )
        if st.st_nlink > 1:
            raise UnsafeSourceError(_hardlink_message(shown, st.st_nlink))
        h = hashlib.sha256()
        size = 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            h.update(block)
            size += len(block)
    finally:
        os.close(fd)
    return h.hexdigest(), size


def _hardlink_message(shown: Path, links: int) -> str:
    return (
        f"{shown} es un enlace duro: el mismo contenido tiene {links} nombres en el "
        f"sistema de ficheros.\n"
        f"`scan` no puede saber dónde están los otros —`st_nlink` los cuenta, pero no "
        f"dice dónde—, así que no puede afirmar que esta fuente esté contenida en "
        f"`{SOURCES_DIRNAME}/`: un `ln /etc/passwd {SOURCES_DIRNAME}/doc.md` mete por "
        f"la puerta de al lado justo lo que se rechaza cuando llega por un enlace "
        f"simbólico, y encima sin destino que enseñar.\n"
        f"Copia el documento en vez de enlazarlo (`cp <origen> "
        f"{SOURCES_DIRNAME}/…`). Si los dos nombres ya estaban dentro de la dropzone, "
        f"quédate con uno o duplica el contenido de verdad."
    )


def _is_within(path: Path, root: Path) -> bool:
    """¿`path` cae dentro de `root`? Las dos rutas deben venir ya resueltas."""
    return path == root or root in path.parents


def _link_target(link: Path) -> Path:
    """Destino real de un enlace de la dropzone; los ciclos son error de ingesta.

    No se usa `Path.resolve()` justamente por los ciclos (`a → b → a`, o un enlace
    a sí mismo): hasta 3.12 lanza `RuntimeError`, que ni es un `IngestError` ni lo
    captura el CLI —la skill recibía un traceback en vez de un mensaje—, y desde
    3.13 devuelve la ruta sin expandir, con lo que el enlace pasa el control de
    contención, no es ni fichero ni carpeta y **se salta en silencio**. Ninguna de
    las dos sirve, y además dependen de la versión de Python.

    `os.path.realpath(strict=True)` da el mismo `ELOOP` en las dos, y así se
    distingue el ciclo del enlace simplemente roto (`ENOENT`), que sí tiene
    destino que enseñar: ese se resuelve de nuevo sin exigir que exista, porque
    apunte a donde apunte hay que poder decir a dónde.
    """
    try:
        return Path(os.path.realpath(link, strict=True))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeSourceError(_cycle_message(link)) from exc
        if exc.errno != errno.ENOENT:
            raise IngestError(
                f"No se pudo resolver el enlace {link} de la dropzone: "
                f"{exc.strerror or exc}. Arréglalo o bórralo: dejarlo sin mirar "
                f"significa no saber qué entra en la wiki."
            ) from exc
    return Path(os.path.realpath(link, strict=False))


def _cycle_message(link: Path) -> str:
    return (
        f"La dropzone contiene un enlace simbólico circular: {link} acaba "
        f"apuntándose a sí mismo.\n"
        f"`scan` no puede decir qué fuente es —no hay fichero al final de la "
        f"cadena— ni comprobar si sale de `{SOURCES_DIRNAME}/`, así que no lo "
        f"ingiere ni se lo salta callando.\n"
        f"Bórralo, o reemplázalo por el documento que debía representar."
    )


def _hidden_parts(rel: Path) -> bool:
    """¿Alguna carpeta o el nombre de `rel` empieza por punto?"""
    return any(part.startswith(".") for part in rel.parts)


def _collect_sources(current: Path, sources_dir: Path, root: Path, out: list) -> None:
    """Recorre `current` acumulando `(rel, real_rel)`, sin salir nunca de `root`.

    `rel` es la ruta relativa a la dropzone —la que ve el manifest— y `real_rel`
    la ruta, también relativa a la dropzone, que se abre para leer: la misma que
    `rel` salvo cuando la entrada es un enlace interno, en cuyo caso es su destino
    ya resuelto. Las dos son relativas a propósito: `_open_source` baja por ellas
    componente a componente desde la dropzone, y una ruta absoluta invitaría a
    volver a recorrer desde `/` lo que ya se comprobó.

    Los enlaces simbólicos se tratan aquí y no en el bucle de `scan` porque el
    recorrido en sí era parte del problema: `rglob` resolvía las carpetas
    enlazadas (y con `sources/x → sources/` daba vueltas para siempre).

    - Enlace que apunta fuera de la dropzone → `UnsafeSourceError`, sea a fichero
      o a carpeta, y también si aún está roto: en cuanto el destino exista se
      ingeriría material de fuera de la instancia.
    - Enlace a una carpeta de dentro → no se desciende: su contenido ya se
      recorre por su ruta real, y descender duplicaría las fuentes (o entraría en
      bucle si el enlace apunta a un ancestro).
    - Enlace a un fichero de dentro → se acepta como antes: no saca nada de la
      dropzone. Solo el enlace *por encima* de la dropzone (`/tmp` en macOS, un
      worktree enlazado) es indiferente, y por eso `root` se compara resuelto.
    - Enlace a una ruta oculta de dentro, o a algo que no es fichero ni carpeta
      (roto, FIFO, socket) → `UnsafeSourceError`: ver `_unusable_link_message`.
      Antes se caían del recorrido sin decir nada, que es la forma de fallo que
      este módulo evita en todas partes.

    El recorrido baja por ruta (`iterdir`), no por descriptores, y no pasa nada:
    aquí no se lee ni un byte de ninguna fuente. Quien cambie la dropzone
    mientras tanto sólo consigue que se listen nombres que luego no se pueden
    abrir, porque cada componente se vuelve a comprobar al leer (`_open_source`)
    — y `scan` es todo o nada: si una fuente falla, no se emite ningún JSON.
    """
    try:
        children = sorted(current.iterdir())
    except OSError as exc:
        # `rglob` se comía los directorios ilegibles sin decir nada, así que una
        # carpeta sin permisos se leía como "no hay nada nuevo que ingerir".
        raise IngestError(
            f"No se pudo leer {current} dentro de la dropzone: "
            f"{exc.strerror or exc}. Arregla los permisos o saca esa carpeta de "
            f"`{SOURCES_DIRNAME}/`: saltársela en silencio deja la wiki incompleta."
        ) from exc
    for child in children:
        if child.name.startswith("."):  # ocultos y el manifest `.ingest-state.json`
            continue
        if child.is_symlink():
            target = _link_target(child)
            if not _is_within(target, root):
                raise UnsafeSourceError(
                    f"La dropzone contiene un enlace simbólico que sale de ella: "
                    f"{child} → {target}.\n"
                    f"`scan` no lo sigue: lo que hay al otro lado no está en la "
                    f"instancia, así que la wiki citaría como fuente una ruta "
                    f"`{SOURCES_DIRNAME}/…` que no existe.\n"
                    f"Copia el documento dentro de `{SOURCES_DIRNAME}/` si quieres "
                    f"ingerirlo, o borra el enlace."
                )
            target_rel = target.relative_to(root)
            if _hidden_parts(target_rel):
                raise UnsafeSourceError(_hidden_link_message(child, target_rel))
            if child.is_dir():  # a carpeta enlazada de dentro: no se desciende
                continue
            if not child.is_file():
                raise UnsafeSourceError(_unusable_link_message(child, target))
            # Se guarda el destino ya resuelto, no el enlace: leerlo por el
            # nombre del enlace volvería a seguirlo, y para entonces puede
            # apuntar a otro sitio (ver `_open_source`).
            out.append((child.relative_to(sources_dir), target_rel))
            continue
        if child.is_dir():
            _collect_sources(child, sources_dir, root, out)
        elif child.is_file():
            rel = child.relative_to(sources_dir)
            out.append((rel, rel))
        else:
            # FIFO, socket o dispositivo puesto directamente en la dropzone. Se
            # rechaza por lo mismo que el enlace que apunta a uno: no hay bytes
            # que hashear, y omitirlo dejaría la ingesta incompleta en silencio.
            raise UnsafeSourceError(
                f"La dropzone contiene algo que no es un documento: {child} "
                f"({stat_mod.filemode(child.lstat().st_mode)}).\n"
                f"`scan` sólo ingiere ficheros regulares: de un FIFO, un socket o un "
                f"dispositivo no hay contenido estable que hashear ni que sintetizar.\n"
                f"Sácalo de `{SOURCES_DIRNAME}/`, o sustitúyelo por el documento que "
                f"debía representar."
            )


def _hidden_link_message(link: Path, target_rel: Path) -> str:
    return (
        f"La dropzone contiene un enlace simbólico a una ruta oculta: {link} → "
        f"{SOURCES_DIRNAME}/{target_rel.as_posix()}.\n"
        f"`scan` excluye del recorrido todo lo que empieza por punto (ahí vive el "
        f"manifest, y ahí se deja lo que no se quiere ingerir), así que el enlace "
        f"dice una cosa y la exclusión la contraria: por el nombre entraría en la "
        f"wiki material que la dropzone da por descartado.\n"
        f"Saca el documento de la carpeta oculta si quieres ingerirlo, o borra el "
        f"enlace."
    )


def _unusable_link_message(link: Path, target: Path) -> str:
    roto = not os.path.exists(target)
    detalle = (
        "no existe: el enlace está roto" if roto
        else "no es un fichero regular (FIFO, socket o dispositivo)"
    )
    return (
        f"La dropzone contiene un enlace simbólico que no se puede ingerir: "
        f"{link} → {target}, que {detalle}.\n"
        f"`scan` no se lo salta en silencio: quien lo puso espera que ese material "
        f"entre en la wiki, y omitirlo deja una ingesta incompleta que se lee como "
        f"completa.\n"
        f"Apúntalo al documento que debía representar, o bórralo."
    )


def _iter_sources(sources_dir: Path):
    """Pares `(rel, real_rel)` de la dropzone: ver `_collect_sources`.

    Lanza `UnsafeSourceError` en cuanto encuentra un enlace que apunta fuera, que
    da vueltas sobre sí mismo, o que no se puede ingerir.
    """
    entries: list[tuple[Path, Path]] = []
    _collect_sources(sources_dir, sources_dir, sources_dir.resolve(), entries)
    # Por `rel`, no por la ruta real: dos entradas pueden compartir destino (un
    # enlace de dentro y su fichero), y lo que ordena la salida es lo que se ve
    # desde la dropzone.
    yield from sorted(entries, key=lambda pair: pair[0])


def load_state(bundle: Path) -> dict:
    """Lee el manifest. Si existe pero está corrupto, lanza `CorruptStateError`.

    Solo se devuelve el estado vacío cuando el manifest realmente no existe
    (instancia recién creada). Un fichero ilegible se reporta para que el
    operador lo repare o lo borre a conciencia, en vez de reingerir todo.
    """
    state_path = Path(bundle) / SOURCES_DIRNAME / STATE_FILENAME
    if not state_path.exists():
        return {"files": {}}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise CorruptStateError(
            f"Manifest ilegible: {state_path} ({e}). "
            "Repáralo o bórralo para reingerir la dropzone entera."
        ) from e
    if not isinstance(state, dict) or not isinstance(state.get("files", {}), dict):
        raise CorruptStateError(
            f"Manifest con forma inesperada: {state_path} "
            "(se esperaba un objeto con la clave 'files')."
        )
    return state


def scan(bundle: Path) -> dict:
    """Compara `sources/` contra el manifest. Devuelve listas por categoría.

    Usa sha256 (no mtime) → idempotente aunque git/copias cambien la fecha.

    Lanza `MissingSourcesError` si la instancia o su dropzone no existen: sin
    `sources/` no hay nada que comparar y devolver todo como `deleted` borraría
    el manifest en el siguiente `commit_state`.
    """
    bundle = Path(bundle)
    sources_dir = resolve_dropzone(bundle)
    state = load_state(bundle)
    known = state.get("files", {})

    result = {"new": [], "changed": [], "unchanged": [], "deleted": []}
    seen: set[str] = set()

    for rel, real_rel in _iter_sources(sources_dir):
        rel_str = rel.as_posix()
        seen.add(rel_str)
        # Hash y tamaño salen de la misma apertura, y la apertura baja desde la
        # dropzone componente a componente: ver `_hash_and_size` y `_open_source`.
        digest, size = _hash_and_size(sources_dir, real_rel)
        # La extensión es la de la dropzone (`rel`), no la del destino real: es la
        # que decide cómo se extrae el texto y la que la skill ve en el manifest.
        ext = rel.suffix.lower()
        entry = {
            "path": rel_str,
            "sha256": digest,
            "size": size,
            "ext": ext,
            "extract": _extract_mode(ext),
        }
        prev = known.get(rel_str)
        if prev is None:
            result["new"].append(entry)
        elif prev.get("sha256") != digest:
            result["changed"].append(entry)
        else:
            result["unchanged"].append(entry)

    for rel_str in known:
        if rel_str not in seen:
            result["deleted"].append({"path": rel_str})

    return result


def _extract_mode(ext: str) -> str:
    if ext in OFFICE_EXTS:
        return "office2text"
    if ext in HTML_EXTS:
        return "html2text"
    if ext in NATIVE_READ_EXTS:
        return "native-read"
    return "unsupported"


# --------------------------------------------------------------------------- #
# office2text: extracción Office con stdlib (zipfile + xml)                    #
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(data: bytes, wanted: set[str], block_tags: set[str] | None = None) -> str:
    """Extrae texto de nodos cuyo local-name esté en `wanted`.

    Inserta saltos de línea al cerrar cualquier tag de `block_tags`
    (p.ej. párrafos `p`, filas `tr`) para preservar algo de estructura.
    """
    block_tags = block_tags or set()
    out: list[str] = []
    for event, elem in ET.iterparse(_bytes_io(data), events=("end",)):
        name = _local(elem.tag)
        if name in wanted and elem.text:
            out.append(elem.text)
        elif name in block_tags:
            out.append("\n")
    text = "".join(out)
    # colapsar saltos triples
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _bytes_io(data: bytes):
    import io
    return io.BytesIO(data)


# Cuánto XML se acepta descomprimir de un solo documento Office. Son límites de
# cordura, no de rendimiento: `_xml_text` carga el miembro entero en memoria y
# `ET.iterparse` construye el árbol encima, así que el pico real es varias veces
# lo que se lee. Un docx/pptx/xlsx de prosa o de tablas cabe de sobra; lo que no
# cabe es una expansión que solo tiene sentido como ataque.
#
# El tope por miembro fue 64 MB y eso **sí** cortaba documentos reales: un xlsx
# legítimo con un `sheet1.xml` de unos 105 MB y un ratio de compresión corriente
# —muy por debajo de `MAX_OFFICE_RATIO`— salía por exit 4 como si fuese una bomba.
# Una hoja de cálculo de unos cientos de miles de filas pasa de los 100 MB de XML
# sin nada raro dentro. Un límite de compatibilidad que rechaza la entrada legítima
# no protege de nada: manda a trocear a mano un xlsx que el código sabe leer.
# 128 MB deja pasar esa hoja y sigue muy por debajo de lo que pide un ataque, que
# no apunta a los 128 MB sino a los gigabytes.
#
# Subirlo no ensancha el agujero, porque el tamaño por miembro no es lo único que
# se mira: en el tramo nuevo (64–128 MB) sigue atando el ratio —una bomba que
# declara 100 MB desde unos KB multiplica por miles, no por 200— y sigue atando el
# presupuesto del documento entero, que no se toca.
MAX_OFFICE_MEMBER_BYTES = 128 * 1024 * 1024  # por miembro del zip
MAX_OFFICE_TOTAL_BYTES = 256 * 1024 * 1024   # sumando todos los que se leen
# Cuántas veces puede crecer un miembro al descomprimirse. El XML de OOXML es
# redundante y comprime bien —x3 en prosa, x10 en una hoja de 60k filas—, así que
# el umbral va muy por encima de lo que produce cualquier generador: caza la
# amplificación de x1000 de una bomba, no un documento repetitivo.
MAX_OFFICE_RATIO = 200
# Por debajo de esto el ratio es ruido (una plantilla de 300 bytes que expande a
# 30 KB no es una bomba): manda el límite absoluto.
#
# Esa exención sólo es segura porque `OFFICE_ZIP_METHODS` deja fuera BZIP2 y LZMA:
# DEFLATE no pasa de x1032 por diseño, así que un miembro por debajo de 4 KB
# comprimidos no llega ni a 4,2 MB de XML aunque nadie le mire el ratio. Ese techo
# es de DEFLATE, no del límite por miembro: no cambia al subir
# `MAX_OFFICE_MEMBER_BYTES`. Si se admitiese otro método, esos mismos 4 KB darían
# los 128 MB que admite un miembro.
MIN_RATIO_COMPRESSED_BYTES = 4096

# Métodos de compresión que se aceptan dentro de un OOXML. La lista es corta a
# propósito: es lo único que escriben Word, Excel, PowerPoint y LibreOffice
# —DEFLATE para el XML, STORED para lo que ya viene comprimido (imágenes)—, así
# que no cerrar la puerta a los demás no gana compatibilidad y sí abre el agujero
# de arriba. Con BZIP2, un `.docx` de **235 bytes** declaraba los 64 MB de XML que
# admitía entonces un miembro: cabía en el límite de tamaño y se saltaba el ratio
# por quedar por debajo de `MIN_RATIO_COMPRESSED_BYTES`, así que se descomprimía
# entero y el comando salía con 0 tras multiplicar por 280.000 el tamaño del
# fichero. Con el tope por miembro en 128 MB el mismo truco pide el doble desde
# unos cientos de bytes, que es justo por qué la lista blanca va antes que
# cualquier cuenta de tamaño y no depende de ella.
OFFICE_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

_ZIP_METHOD_NAMES = {
    zipfile.ZIP_STORED: "STORED",
    zipfile.ZIP_DEFLATED: "DEFLATE",
    zipfile.ZIP_BZIP2: "BZIP2",
    zipfile.ZIP_LZMA: "LZMA",
}


def _method_name(code: int) -> str:
    """Nombre del método de compresión del zip, o su número si no se conoce.

    El código lo escribe el fichero y puede ser cualquier entero de 16 bits
    (incluido el 99 del cifrado propietario), así que el nombre es un extra y el
    número es lo que siempre se imprime.
    """
    nombre = _ZIP_METHOD_NAMES.get(code)
    return f"{nombre} (método {code})" if nombre else f"el método {code}"

# Cuánta rejilla puede pedir un xlsx. Es un límite aparte de los de arriba porque
# no lo cubren: el coste no lo paga el XML que se lee, sino las celdas **vacías**
# que hay que materializar para dejar cada valor en su columna. Unos cientos de
# bytes de XML bastan para pedir miles de millones de posiciones.
#
# 16.384 columnas (A…XFD) es el ancho máximo de una hoja en el propio formato
# (ECMA-376 / SpreadsheetML), así que una referencia que se sale no es un
# documento grande: es una hoja imposible.
MAX_XLSX_COLUMNS = 16384
# Y con referencias dentro de la hoja el producto sigue creciendo: 400 filas con
# una celda en `XFD` son 6,5 millones de posiciones salidas de 8 KB de zip. El
# techo es del documento entero —filas x columnas, sumando hojas—, y da de sobra
# para cualquier hoja real (una de 100.000 filas x 20 columnas son 2 millones).
MAX_XLSX_CELLS = 4_000_000

# Cuánto de un dato del documento se cita en un mensaje de error. Las referencias
# de celda (`r="C7"`) y los nombres de miembro del zip los escribe quien fabrica
# el fichero, así que sin acotarlos la longitud del mensaje también la elige él:
# un xlsx de 2 KB con `r` de 2 MB de letras imprimía 2 MB por stderr —x900 el
# fichero— y quien lee el fallo (la skill, un log, la CI) se lo comía entero.
# 40 caracteres identifican de sobra a la culpable: la última columna que existe
# de verdad es `XFD`, y todo lo que pasa por aquí es ya una referencia imposible.
MAX_QUOTED_CHARS = 40


def quoted(value: str, limit: int = MAX_QUOTED_CHARS) -> str:
    """`repr` acotado de un dato que viene del documento.

    Recorta a `limit` caracteres y dice cuántos había, para que el mensaje siga
    nombrando lo que sobra sin que el fichero decida cuánto stderr ocupa. El
    `repr` de un recorte de `limit` caracteres tiene un techo propio (escapa lo
    no imprimible, no lo expande sin fin), así que el resultado está acotado.

    Es pública porque el CLI la necesita para lo mismo con el texto de las
    excepciones que no redacta este módulo (ver `cli._extractor_detail`), con su
    propio `limit`: ahí lo que se cita es un mensaje entero, no una referencia.
    """
    if len(value) <= limit:
        return repr(value)
    return f"{value[:limit]!r}… ({len(value)} caracteres)"


class OfficeInputError(ValueError):
    """Base de los rechazos de `office2text` que redacta este proyecto.

    Los agrupa una razón operativa, no taxonómica: sus mensajes están escritos
    para stderr —acotados, con lo que pone el documento citado por `quoted` y
    con qué hacer a continuación—, así que el CLI los imprime tal cual. El texto
    de una excepción ajena (`zipfile.BadZipFile` interpola el nombre del miembro
    y los bytes de la cabecera sin ningún techo) lo tiene que acotar él, y para
    distinguir unos de otros necesita un tipo común.

    Hereda de `ValueError` a propósito: el CLI ya traduce los `ValueError` de los
    extractores a su código de "entrada que no sirve" con un mensaje y sin
    traceback, y esto es exactamente eso —un problema del fichero, no de la
    instancia—, así que `office2text fichero.docx` lo reporta como cualquier otro
    contenido que no corresponde a la extensión.
    """


class OfficeTooLargeError(OfficeInputError):
    """El docx/pptx/xlsx expande mucho más de lo que un documento necesita.

    Cubre las dos formas de pedir demasiado desde un fichero diminuto: el miembro
    del zip que descomprime sin freno, y la hoja de cálculo cuya referencia de
    columna obliga a reservar una rejilla que no cabe en memoria.
    """


class OfficeCompressionError(OfficeInputError):
    """El miembro del zip usa un método de compresión que no trae un OOXML.

    Es un rechazo por *cómo* está comprimido, no por cuánto ocupa: un miembro de
    diez bytes en BZIP2 también sale por aquí. Va aparte de `OfficeTooLargeError`
    porque lo que hay que hacer es distinto —volver a guardar el documento, no
    partirlo— y porque el motivo es previo a cualquier medida: con un método que
    expande miles de veces más que DEFLATE, los límites por tamaño y por ratio
    dejan de acotar lo que puede pedir un fichero diminuto (ver
    `OFFICE_ZIP_METHODS`).
    """


class OfficeStructureError(OfficeInputError):
    """El zip trae algo que `zipfile` no rechaza: avisa y sigue leyendo.

    Son dos casos, y los dos los detecta `warnings.warn`: dos entradas del
    directorio central sobre la misma cabecera local —"possible zip bomb", cuando
    el solape es parcial la rama de al lado sí levanta `BadZipFile`— y el campo
    extra Unicode (0x7075) con el nombre vacío. Ninguno lo escribe una aplicación
    de Office, así que se tratan como documento inválido y no como advertencia
    (ver `_zip_warnings_as_errors`).
    """


# Cuánto se cita del texto de un aviso de `zipfile`. 80 caracteres es lo que mide
# el más largo de los dos con un nombre de miembro realista —"Overlapped entries:
# 'word/document.xml' (possible zip bomb)"—, así que en un documento corriente se
# cita entero, incluido el diagnóstico del final. Lo que corta es el nombre que ese
# aviso interpola, que lo escribe el fichero y no tiene techo (ver
# `MAX_QUOTED_CHARS`, que hace lo mismo con el nombre que ya se cita aparte).
MAX_ZIP_WARNING_CHARS = 80


@contextlib.contextmanager
def _zip_warnings_as_errors():
    """Convierte en excepción cualquier aviso que emita `zipfile` aquí dentro.

    Un aviso no sirve para lo que hace este módulo, por dos motivos:

    - **Lo redacta el fichero.** El de entradas solapadas interpola el nombre del
      miembro, y ese campo admite 65.535 bytes: un `.pptx` de 120 KB imprimía 60 KB
      de aviso por stderr y salía con 0. Es el mismo defecto que ya se acotó en las
      excepciones ajenas (ver `quoted` y `cli._extractor_detail`), por la puerta de
      un aviso.
    - **Qué pasa con él no lo decide este código.** Con `PYTHONWARNINGS=error` o
      `-W error` —habitual en CI— el aviso escapaba como `UserWarning`, y el CLI
      salía con traceback y exit 1: para la skill `okf-ingest` eso no es "documento
      que no sirve" sino "el CLI se ha caído".

    Se filtra por acción y no por mensaje (`simplefilter("error")`) a propósito: lo
    que se rechaza es que un aviso sobre la estructura del zip pase inadvertido, no
    una lista de textos concretos de una versión de CPython. El alcance es mínimo
    —abrir el zip y leer un miembro, nada de XML— para que el filtro global que
    instala `catch_warnings` no tape avisos de otro código.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


def _zip_structure_problem(path: Path, exc: Warning, member: str | None = None) -> str:
    """Mensaje del rechazo por un aviso de `zipfile`, acotado y accionable."""
    donde = f"al leer el miembro {quoted(member)}" if member else "al abrir el fichero"
    return (
        f"Estructura de zip no admitida en {path}: {donde}, `zipfile` avisa de "
        f"{quoted(str(exc), MAX_ZIP_WARNING_CHARS)}.\n"
        "El aviso se trata como documento inválido: su texto lo redacta el fichero "
        "—el nombre de un miembro admite 65.535 bytes—, así que ni puede elegir "
        "cuánto stderr ocupa ni puede depender de cómo esté configurado Python "
        "para que este comando salga con un código estable.\n"
        "Un docx/pptx/xlsx real no reutiliza los bytes comprimidos de una entrada "
        "en otra ni declara campos extra vacíos: si el documento es legítimo, "
        "ábrelo con su aplicación y vuelve a guardarlo antes de dejarlo en "
        "`sources/`."
    )


def _open_ooxml(path: Path) -> zipfile.ZipFile:
    """`zipfile.ZipFile(path)` con los avisos del propio zip como rechazo.

    El directorio central se lee en el constructor (`_RealGetContents`), y ahí es
    donde `ZipInfo._decodeExtra` avisa del campo extra Unicode vacío: la guarda no
    puede vivir sólo en la lectura de los miembros.
    """
    with _zip_warnings_as_errors():
        try:
            return zipfile.ZipFile(path)
        except Warning as exc:
            raise OfficeStructureError(_zip_structure_problem(path, exc)) from exc


class _BoundedZipReader:
    """Lee miembros de un OOXML con un techo de bytes descomprimidos.

    Existe porque `zf.read(nombre)` no tiene freno: un zip de 70 KB puede
    declarar gigabytes de XML (zip bomb), y `office2text` los descomprimía
    enteros en memoria antes de que nadie pudiera mirar el tamaño. Ahora se
    comprueba lo que declara la cabecera —barato, y así no se descomprime nada— y
    además se acota la lectura real, que no depende de que la cabecera sea
    sincera. El presupuesto es por documento, no por miembro, para que un pptx
    con mil slides o un xlsx con mil hojas tampoco sumen sin límite.

    Antes que ninguna cuenta de bytes va el método de compresión
    (`OFFICE_ZIP_METHODS`): con BZIP2 o LZMA los dos topes de arriba dejan de
    acotar lo que puede pedir un fichero de unos cientos de bytes, así que la
    lista blanca es lo que sostiene al resto.
    """

    def __init__(self, zf: zipfile.ZipFile, path: Path) -> None:
        self._zf = zf
        self._path = path
        self._remaining = MAX_OFFICE_TOTAL_BYTES

    def read(self, name: str) -> bytes:
        """Bytes del miembro `name`. `KeyError` si no está (lo traduce el CLI)."""
        info = self._zf.getinfo(name)
        self._check_method(info)
        cap, limite = self._cap()
        self._check_declared(info, cap, limite)
        # `ZipFile.open` avisa —no falla— cuando los bytes comprimidos del miembro
        # se solapan con los de otra entrada, y ese aviso lo escribe el fichero.
        with _zip_warnings_as_errors():
            try:
                with self._zf.open(name) as fh:
                    data = fh.read(cap + 1)  # un byte más: para saber que se ha pasado
            except Warning as exc:
                raise OfficeStructureError(
                    _zip_structure_problem(self._path, exc, member=name)
                ) from exc
        if len(data) > cap:
            raise OfficeTooLargeError(
                self._problem(name, f"descomprime más de {cap} bytes de XML ({limite})")
            )
        self._remaining -= len(data)
        return data

    def _cap(self) -> tuple[int, str]:
        """Techo de la próxima lectura y de dónde sale.

        Son dos límites distintos y el mensaje tiene que decir cuál ha saltado: el
        de miembro es una constante, el del presupuesto depende de lo que ya se
        haya leído en este documento. Anunciar "máximo 4000" cuando 4000 es lo que
        queda del presupuesto manda a subir una constante que no es la que corta,
        y en un pptx esconde lo único que importa: que el problema es la suma de
        las slides, no la slide que aparece en el mensaje.
        """
        if self._remaining < MAX_OFFICE_MEMBER_BYTES:
            return self._remaining, (
                f"quedan {self._remaining} bytes de los {MAX_OFFICE_TOTAL_BYTES} "
                "que puede descomprimir el documento entero"
            )
        return MAX_OFFICE_MEMBER_BYTES, (
            f"el máximo por miembro es {MAX_OFFICE_MEMBER_BYTES} bytes"
        )

    def _check_method(self, info: zipfile.ZipInfo) -> None:
        if info.compress_type in OFFICE_ZIP_METHODS:
            return
        raise OfficeCompressionError(
            f"Compresión no admitida en {self._path}: el miembro "
            f"{quoted(info.filename)} usa {_method_name(info.compress_type)}, y un "
            "docx/pptx/xlsx sólo trae STORED o DEFLATE.\n"
            "Se rechaza sin descomprimirlo porque los demás métodos expanden miles "
            "de veces más que DEFLATE: con BZIP2 o LZMA bastan unos cientos de "
            "bytes —por debajo del mínimo a partir del cual se mira el ratio— para "
            f"pedir los {MAX_OFFICE_MEMBER_BYTES} bytes de XML que admite un "
            "miembro.\n"
            "Si el documento es legítimo, ábrelo con su aplicación y vuelve a "
            "guardarlo antes de dejarlo en `sources/`."
        )

    def _check_declared(self, info: zipfile.ZipInfo, cap: int, limite: str) -> None:
        if info.file_size > cap:
            raise OfficeTooLargeError(
                self._problem(
                    info.filename,
                    f"declara {info.file_size} bytes sin comprimir ({limite})",
                )
            )
        if (
            info.compress_size >= MIN_RATIO_COMPRESSED_BYTES
            and info.file_size > info.compress_size * MAX_OFFICE_RATIO
        ):
            raise OfficeTooLargeError(
                self._problem(
                    info.filename,
                    f"multiplica por {info.file_size // info.compress_size} su tamaño "
                    f"comprimido ({info.compress_size} → {info.file_size} bytes, "
                    f"máximo x{MAX_OFFICE_RATIO})",
                )
            )

    def _problem(self, name: str, detail: str) -> str:
        # El nombre del miembro también lo pone el zip, así que se cita acotado.
        return (
            f"Expansión desproporcionada en {self._path}: el miembro {quoted(name)} "
            f"{detail}.\n"
            "Se rechaza sin descomprimirlo entero: un zip de unos KB puede producir "
            "gigabytes de XML y `office2text` lo carga en memoria.\n"
            "Si el documento es legítimo, ábrelo con su aplicación y exporta el texto "
            "(o divídelo) antes de dejarlo en `sources/`."
        )


def office2text(path: Path) -> str:
    path = Path(path)
    ext = path.suffix.lower()
    with _open_ooxml(path) as zf:
        names = zf.namelist()
        # Todas las lecturas pasan por el mismo presupuesto: un solo documento no
        # puede descomprimir sin límite ni miembro a miembro ni sumando miembros.
        reader = _BoundedZipReader(zf, path)
        if ext == ".docx":
            return _xml_text(reader.read("word/document.xml"), {"t"}, block_tags={"p"})
        if ext == ".pptx":
            slides = sorted(
                n for n in names
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            parts = []
            for n in slides:
                parts.append(_xml_text(reader.read(n), {"t"}, block_tags={"p"}))
            return "\n\n---\n\n".join(p for p in parts if p)
        if ext == ".xlsx":
            return _xlsx_text(reader, names)
    raise ValueError(f"Formato Office no soportado: {ext}")


def _col_index(ref: str | None) -> int | None:
    """Referencia de celda → índice de columna 0-based (`"C7"` → 2).

    `None` si la celda no trae `r` utilizable (hoja generada a mano).

    Puede devolver un índice **fuera de la hoja** (`MAX_XLSX_COLUMNS`), y ahí se
    para: en cuanto la cuenta se sale, seguir multiplicando por 26 sólo sirve para
    fabricar el entero de 54 cifras que pide `r="AAAA…1"`. Quien llame decide qué
    hacer con una columna que no existe; esta función no calcula lo que no se va a
    usar.
    """
    if not ref:
        return None
    idx = 0
    seen = False
    for ch in ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
        seen = True
        if idx > MAX_XLSX_COLUMNS:  # ya está fuera; el valor exacto da igual
            return MAX_XLSX_COLUMNS
    return idx - 1 if seen else None


def _cell_text(cell, shared: list[str]) -> str:
    """Texto de una celda `<c>`: sharedStrings, inlineStr o valor literal."""
    ctype = cell.get("t")
    if ctype == "inlineStr":
        node = next((x for x in cell if _local(x.tag) == "is"), None)
        if node is None:
            return ""
        return "".join(t.text or "" for t in node.iter() if _local(t.tag) == "t")
    v = next((x for x in cell if _local(x.tag) == "v"), None)
    if v is None or v.text is None:
        return ""
    if ctype == "s":  # índice a sharedStrings
        try:
            idx = int(v.text)
        except ValueError:
            return ""
        return shared[idx] if 0 <= idx < len(shared) else ""
    return v.text


def _column_problem(sheet: str, ref: "str | None") -> str:
    # `ref` y `sheet` los escribe el documento: se citan acotados (ver `quoted`).
    causa = (
        f"la celda {quoted(ref)} apunta a una columna que se sale de la hoja"
        if ref else
        "una fila usa más columnas de las que caben en la hoja"
    )
    return (
        f"Columna fuera de la hoja en {quoted(sheet)}: {causa}, y una hoja de cálculo "
        f"acaba en la columna {MAX_XLSX_COLUMNS} (`XFD`).\n"
        "Se rechaza sin reservar la fila: colocar un valor en su columna obliga a "
        "materializar todos los huecos que quedan a su izquierda, así que unos cientos "
        "de bytes de XML pueden pedir miles de millones de celdas vacías.\n"
        "Si la hoja es legítima, ábrela con su aplicación y vuelve a guardarla (o exporta "
        "el rango con datos a CSV) antes de dejarla en `sources/`."
    )


def _grid_problem(sheet: str, cells: int) -> str:
    return (
        f"Rejilla desproporcionada en {quoted(sheet)}: el documento pide materializar "
        f"{cells} celdas y el máximo es {MAX_XLSX_CELLS}.\n"
        "Se corta a medio recorrido: cada fila se rellena hasta la última columna que "
        "usa, así que miles de filas con una celda en la columna `XFD` multiplican sin "
        "que el tamaño del XML lo delate.\n"
        "Si la hoja es legítima, quédate con el rango que de verdad tiene datos (o "
        "expórtalo a CSV) antes de dejarla en `sources/`."
    )


def _iter_freed(data: bytes, tag: str):
    """Genera los elementos `tag` de un XML liberando cada uno antes del siguiente.

    `ET.iterparse` sólo es incremental a medias: emite los eventos mientras lee,
    pero va construyendo el árbol completo debajo y no suelta nada por su cuenta.
    Recorrer una hoja sin liberar mantiene vivas a la vez todas las filas ya
    procesadas —cada `<c>`, cada `<v>`—, y eso es el documento entero en memoria,
    no el trozo que se está mirando: una hoja de 10 MB de XML (500 KB de zip,
    holgadamente dentro de todos los límites) costaba 173 MB de pico para producir
    700 KB de texto, y `MAX_OFFICE_MEMBER_BYTES` admite 128 MB de XML por miembro.

    Así que en cuanto quien consume termina con un elemento se le vacía y se le
    desengancha de su padre: `clear()` suelta el contenido, y quitarlo del padre
    evita que éste acumule una entrada por cada elemento leído. Lo que queda vivo
    es una fila, no la hoja.

    Contrapartida: el elemento hay que consumirlo **dentro** del bucle. En la
    iteración siguiente ya está vacío.
    """
    abiertos: list = []
    for event, elem in ET.iterparse(_bytes_io(data), events=("start", "end")):
        if event == "start":
            abiertos.append(elem)
            continue
        abiertos.pop()  # el `end` cierra siempre el último que se abrió
        if _local(elem.tag) != tag:
            continue
        yield elem
        elem.clear()
        if abiertos:
            abiertos[-1].remove(elem)


def _xlsx_text(reader: "_BoundedZipReader", names: list[str]) -> str:
    # Celdas materializadas en todo el documento: ver `MAX_XLSX_CELLS`. No lo cubre
    # el techo de bytes descomprimidos, porque las celdas que cuestan memoria aquí
    # son las que **no** están en el XML.
    spent_cells = 0
    # tabla de cadenas compartidas
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        # Lo que se guarda es el texto de cada `<si>`, no el nodo: la tabla vive
        # mientras se leen las hojas, y su árbol XML no tiene por qué acompañarla.
        for elem in _iter_freed(reader.read("xl/sharedStrings.xml"), "si"):
            shared.append("".join(
                t.text or "" for t in elem.iter() if _local(t.tag) == "t"
            ))
    sheets = sorted(
        n for n in names
        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
    )
    out: list[str] = []
    for sheet in sheets:
        out.append(f"# {sheet.rsplit('/', 1)[-1]}")
        rows: list[list[str]] = []
        width = 0
        sheet_cells = 0
        # Cada fila se lee, se convierte en texto y se libera antes de la siguiente
        # (`_iter_freed`): lo que se acumula es `rows`, ya acotado por `MAX_XLSX_CELLS`.
        for row in _iter_freed(reader.read(sheet), "row"):
            # xlsx omite las celdas vacías: la posición real la da `r` ("C7"),
            # así que se coloca cada celda en su columna y se rellenan los huecos.
            by_col: dict[int, str] = {}
            cursor = 0
            for c in row:
                if _local(c.tag) != "c":
                    continue
                ref = c.get("r")
                col = _col_index(ref)
                if col is None or col < cursor:
                    col, ref = cursor, None  # la posición manda, no la referencia
                if col >= MAX_XLSX_COLUMNS:
                    raise OfficeTooLargeError(_column_problem(sheet, ref))
                cursor = col + 1
                by_col[col] = _cell_text(c, shared)
            if not by_col or not any(by_col.values()):
                continue
            # Se cuenta la fila **antes** de rellenarla, no al emitirla: es lo que
            # corta a tiempo la hoja hostil que pediría la memoria de la máquina.
            sheet_cells += max(by_col) + 1
            if spent_cells + sheet_cells > MAX_XLSX_CELLS:
                raise OfficeTooLargeError(_grid_problem(sheet, spent_cells + sheet_cells))
            cells = [by_col.get(i, "") for i in range(max(by_col) + 1)]
            width = max(width, len(cells))
            rows.append(cells)
        # Al emitir, las filas cortas se alinean a la anchura de la hoja, así que lo
        # que ocupa de verdad es el rectángulo `anchura x filas` — nunca menos que la
        # suma de anchuras ya contada.
        spent_cells += width * len(rows)
        if spent_cells > MAX_XLSX_CELLS:
            raise OfficeTooLargeError(_grid_problem(sheet, spent_cells))
        for cells in rows:  # misma anchura en toda la hoja → columnas alineadas
            out.append(" | ".join(cells + [""] * (width - len(cells))))
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# html2text: HTML → markdown limpio                                           #
# --------------------------------------------------------------------------- #
# Elementos cuyo contenido es código, no prosa: se borran enteros.
RAW_TEXT_TAGS = {"script", "style"}


def strip_raw_text_elements(html: str) -> str:
    """Elimina `<script>`/`<style>` **con su contenido**, conservando el resto.

    `markdownify(strip=[...])` solo quita las etiquetas: el JS y el CSS se
    colaban como texto en la wiki. Se reserializa el markup con `html.parser`
    (que ya trata script/style como CDATA) saltándose esas regiones.
    """
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)  # no perder entidades
            self.depth = 0
            self.out: list[str] = []

        def _emit(self, text: str) -> None:
            if not self.depth:
                self.out.append(text)

        def handle_starttag(self, tag, attrs):
            if tag in RAW_TEXT_TAGS:
                self.depth += 1
                return
            self._emit(self.get_starttag_text() or "")

        def handle_startendtag(self, tag, attrs):
            if tag not in RAW_TEXT_TAGS:  # `<script/>` no abre contenido
                self._emit(self.get_starttag_text() or "")

        def handle_endtag(self, tag):
            if tag in RAW_TEXT_TAGS:
                self.depth = max(0, self.depth - 1)
                return
            self._emit(f"</{tag}>")

        def handle_data(self, data):
            self._emit(data)

        def handle_entityref(self, name):
            self._emit(f"&{name};")

        def handle_charref(self, name):
            self._emit(f"&#{name};")

        def handle_comment(self, data):
            self._emit(f"<!--{data}-->")

        def handle_decl(self, decl):
            self._emit(f"<!{decl}>")

        def handle_pi(self, data):
            self._emit(f"<?{data}>")

        def unknown_decl(self, data):
            self._emit(f"<![{data}]>")

    p = _Stripper()
    p.feed(html)
    p.close()
    return "".join(p.out)


def html2text(path: Path) -> str:
    """Convierte un .html/.htm a markdown legible.

    Usa `markdownify` si está disponible (viene con el motor OKF); si no, cae a
    un extractor de texto con la stdlib (`html.parser`). En ambos casos el
    script/style se elimina antes de convertir.
    """
    html = strip_raw_text_elements(
        Path(path).read_text(encoding="utf-8", errors="replace")
    )
    try:
        from markdownify import markdownify as _md
        return _md(html, strip=["script", "style", "link", "meta"]).strip()
    except ImportError:
        from html.parser import HTMLParser

        class _Text(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.buf: list[str] = []

            def handle_starttag(self, tag, attrs):
                if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3"):
                    self.buf.append("\n")

            def handle_data(self, data):
                if data.strip():
                    self.buf.append(data)

        p = _Text()
        p.feed(html)
        text = "".join(p.buf)
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip()


# --------------------------------------------------------------------------- #
# verify: valida OKF (usa el motor reference_agent)                           #
# --------------------------------------------------------------------------- #
def verify(instance: Path) -> dict:
    """Comprueba que todo .md del bundle (salvo index.md) valida como OKF.

    Resuelve el bundle en modo estricto: sin páginas que comprobar no se devuelve
    `ok: true` con `checked: 0`, se lanza `MissingBundleError`.

    `purpose.md` y `schema.md` de la raíz quedan fuera vía `is_bundle_page`: son
    contexto de la instancia, no páginas, también cuando el bundle es plano.
    """
    from reference_agent.bundle.document import OKFDocument  # import perezoso

    bundle = resolve_bundle(instance)
    errors: list[dict] = []
    checked = 0
    for md in sorted(bundle.rglob("*.md")):
        if md.name == "index.md":
            continue
        if not is_bundle_page(md.relative_to(bundle)):
            continue
        checked += 1
        rel = md.relative_to(bundle).as_posix()
        try:
            doc = OKFDocument.parse(md.read_text(encoding="utf-8"))
            doc.validate()
        except Exception as e:  # OKFDocumentError u otros
            errors.append({"path": rel, "error": str(e).splitlines()[0]})
    return {"checked": checked, "errors": errors, "ok": not errors}


# --------------------------------------------------------------------------- #
# commit_state: reescribe el manifest tras una ingesta                        #
# --------------------------------------------------------------------------- #
class InvalidScanPayload(Exception):
    """El JSON que se pasa a `commit_state` no tiene la forma que produce `scan`.

    No hereda de `IngestError` a propósito: no es un problema de la instancia en
    disco sino de la entrada que llega por stdin, y el CLI lo traduce a su propio
    código de "entrada ilegible".
    """


# Categorías que produce `scan`, más el mapa opcional fuente → páginas.
SCAN_CATEGORIES = ("new", "changed", "unchanged", "deleted")
_PAYLOAD_KEYS = (*SCAN_CATEGORIES, "concepts")
# `scan` hashea con `hashlib.sha256().hexdigest()`: siempre 64 hex en minúscula.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _kind(value: object) -> str:
    """Nombre legible del tipo JSON de `value`, para los mensajes de error."""
    return {
        type(None): "null", bool: "un booleano", int: "un número",
        float: "un número", str: "un texto", list: "una lista", dict: "un objeto",
    }.get(type(value), type(value).__name__)


def _entry_problems(category: str, position: int, entry: object) -> list[str]:
    """Valida una entrada de una categoría de `scan`. Nunca lanza."""
    where = f"`{category}[{position}]`"
    if not isinstance(entry, dict):
        return [f"{where} debe ser un objeto con `path`, no {_kind(entry)}."]
    problems: list[str] = []
    path = entry.get("path")
    if not isinstance(path, str) or not path.strip():
        problems.append(f"{where}.path debe ser un texto no vacío, no {_kind(path)}.")
    # `deleted` solo identifica la fuente que desaparece: no arrastra hash.
    if category != "deleted":
        digest = entry.get("sha256")
        if not isinstance(digest, str):
            problems.append(
                f"{where}.sha256 es obligatorio y debe ser un texto, no {_kind(digest)}."
            )
        elif not _SHA256_RE.match(digest):
            problems.append(
                f"{where}.sha256 no es un sha256 (64 hex en minúscula): {digest!r}."
            )
    size = entry.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
        problems.append(f"{where}.size debe ser un número entero, no {_kind(size)}.")
    ext = entry.get("ext")
    if ext is not None and not isinstance(ext, str):
        problems.append(f"{where}.ext debe ser un texto, no {_kind(ext)}.")
    return problems


def _concepts_problems(concepts: object) -> list[str]:
    """Valida `concepts` = {ruta_fuente: [rutas_de_páginas]}. Nunca lanza."""
    if concepts is None:
        return []
    if not isinstance(concepts, dict):
        return [f"`concepts` debe ser un objeto {{fuente: [páginas]}}, no {_kind(concepts)}."]
    problems: list[str] = []
    for key, pages in concepts.items():
        if not isinstance(pages, list) or any(not isinstance(p, str) for p in pages):
            problems.append(
                f"`concepts[{key!r}]` debe ser una lista de rutas de página, "
                f"no {_kind(pages)}."
            )
    return problems


def validate_scan_payload(processed: object) -> dict:
    """Comprueba que `processed` es una salida de `scan` y devuelve el objeto.

    Se valida **entero y antes de tocar el manifest**, así que un payload
    equivocado no deja el estado a medias. Antes, un `path` o un `sha256` ausente
    reventaba con `KeyError`/`TypeError` a mitad del recorrido, y la salida de
    `okf-wiki now --json` —que no tiene ninguna categoría— se aceptaba en
    silencio imprimiendo "manifest actualizado".
    """
    if not isinstance(processed, dict):
        raise InvalidScanPayload(
            f"Se esperaba un objeto JSON como el que imprime `scan`, no {_kind(processed)}."
        )
    problems: list[str] = []
    unknown = sorted(k for k in processed if k not in _PAYLOAD_KEYS)
    if unknown:
        problems.append(
            f"claves que `scan` no produce: {', '.join(repr(k) for k in unknown)} "
            f"(se admiten {', '.join(_PAYLOAD_KEYS)})."
        )
    if not any(cat in processed for cat in SCAN_CATEGORIES):
        problems.append(
            "no hay ninguna categoría de `scan`: hace falta al menos una de "
            f"{', '.join(SCAN_CATEGORIES)}."
        )
    for category in SCAN_CATEGORIES:
        if category not in processed:
            continue
        entries = processed[category]
        if not isinstance(entries, list):
            problems.append(f"`{category}` debe ser una lista, no {_kind(entries)}.")
            continue
        for position, entry in enumerate(entries):
            problems += _entry_problems(category, position, entry)
    problems += _concepts_problems(processed.get("concepts"))
    if problems:
        raise InvalidScanPayload(
            "El JSON de stdin no es una salida de `scan`:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    return processed


def commit_state(bundle: Path, processed: dict) -> Path:
    """`processed` = salida de `scan` (o subconjunto) ya ingerida.

    Marca new+changed+unchanged como conocidos con su hash; elimina deleted.
    Acepta opcionalmente `processed["concepts"]` = {source_path: [concept_paths]}.

    Falla **sin crear ni modificar nada**:
    - `MissingSourcesError` si la instancia o su dropzone no existen. Se comprueba
      antes que el payload porque una ruta equivocada es la causa raíz: arreglar el
      JSON no ayudaría. Y se comprueba con el mismo criterio que `scan`, así que no
      hay ninguna ruta que acepte uno y rechace el otro.
    - `InvalidScanPayload` si `processed` no tiene la forma de `scan`.
    - `CorruptStateError` si el manifest actual existe pero es ilegible.
    """
    bundle = Path(bundle)
    sources_dir = resolve_dropzone(bundle)
    validate_scan_payload(processed)
    state = load_state(bundle)
    files = state.get("files", {})
    concept_map = processed.get("concepts", {})

    for cat in ("new", "changed", "unchanged"):
        for entry in processed.get(cat, []):
            rel = entry["path"]
            files[rel] = {
                "sha256": entry["sha256"],
                "size": entry.get("size"),
                "ext": entry.get("ext"),
                "concepts": concept_map.get(rel, files.get(rel, {}).get("concepts", [])),
            }
    for entry in processed.get("deleted", []):
        files.pop(entry["path"], None)

    state["files"] = files
    # Sin `mkdir`: `resolve_dropzone` ya garantiza el directorio, y crearlo aquí es
    # precisamente lo que convertía una ruta inexistente en un árbol inventado.
    state_path = sources_dir / STATE_FILENAME
    _atomic_write(
        state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    )
    return state_path


def _atomic_write(path: Path, payload: str) -> None:
    """Escribe `payload` en `path` sin dejar nunca el fichero a medias.

    Temporal en el mismo directorio + `os.replace` (atómico en POSIX y NTFS):
    un corte a mitad de ingesta deja el manifest anterior intacto, no un JSON
    truncado que `load_state` tendría que rechazar.
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
