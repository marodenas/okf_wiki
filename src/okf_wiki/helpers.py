"""okf_wiki helpers — utilidades sin LLM para la skill `okf-ingest`.

Todo lo "determinista" del pipeline vive aquí (la síntesis la hace Claude):
- scan:         detecta qué es nuevo/cambiado/borrado en la dropzone `sources/`.
- office2text:  extrae texto de docx/pptx/xlsx con la stdlib (sin dependencias).
- verify:       valida que todos los .md del bundle son OKF válidos.
- commit_state: reescribe el manifest de ingesta.

El motor OKF (reference_agent) solo se importa de forma perezosa en `verify`.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

STATE_FILENAME = ".ingest-state.json"
SOURCES_DIRNAME = "sources"
WIKI_DIRNAME = "wiki"


def wiki_root(instance: "Path | str") -> Path:
    """Devuelve el bundle OKF de una instancia: `<instancia>/wiki` si existe.

    Separar `sources/` (crudo) de `wiki/` (bundle) evita que el motor OKF meta
    los documentos originales en los índices y en el grafo. Si no hay subcarpeta
    `wiki/`, se asume que la ruta ya es un bundle plano (compatibilidad).
    """
    w = Path(instance) / WIKI_DIRNAME
    return w if w.is_dir() else Path(instance)

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
def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _iter_sources(sources_dir: Path):
    """Ficheros de la dropzone, ignorando ocultos y el propio manifest."""
    for p in sorted(sources_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(sources_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        yield rel, p


def load_state(bundle: Path) -> dict:
    state_path = bundle / SOURCES_DIRNAME / STATE_FILENAME
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"files": {}}


def scan(bundle: Path) -> dict:
    """Compara `sources/` contra el manifest. Devuelve listas por categoría.

    Usa sha256 (no mtime) → idempotente aunque git/copias cambien la fecha.
    """
    bundle = Path(bundle)
    sources_dir = bundle / SOURCES_DIRNAME
    state = load_state(bundle)
    known = state.get("files", {})

    result = {"new": [], "changed": [], "unchanged": [], "deleted": []}
    seen: set[str] = set()

    if sources_dir.exists():
        for rel, path in _iter_sources(sources_dir):
            rel_str = rel.as_posix()
            seen.add(rel_str)
            digest = _hash_file(path)
            entry = {
                "path": rel_str,
                "sha256": digest,
                "size": path.stat().st_size,
                "ext": path.suffix.lower(),
                "extract": _extract_mode(path.suffix.lower()),
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


def office2text(path: Path) -> str:
    path = Path(path)
    ext = path.suffix.lower()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if ext == ".docx":
            return _xml_text(zf.read("word/document.xml"), {"t"}, block_tags={"p"})
        if ext == ".pptx":
            slides = sorted(
                n for n in names
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            parts = []
            for n in slides:
                parts.append(_xml_text(zf.read(n), {"t"}, block_tags={"p"}))
            return "\n\n---\n\n".join(p for p in parts if p)
        if ext == ".xlsx":
            return _xlsx_text(zf, names)
    raise ValueError(f"Formato Office no soportado: {ext}")


def _xlsx_text(zf: zipfile.ZipFile, names: list[str]) -> str:
    # tabla de cadenas compartidas
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for _event, elem in ET.iterparse(
            _bytes_io(zf.read("xl/sharedStrings.xml")), events=("end",)
        ):
            if _local(elem.tag) == "si":
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
        for _event, row in ET.iterparse(_bytes_io(zf.read(sheet)), events=("end",)):
            if _local(row.tag) != "row":
                continue
            cells: list[str] = []
            for c in row:
                if _local(c.tag) != "c":
                    continue
                v = next((x for x in c if _local(x.tag) == "v"), None)
                if v is None or v.text is None:
                    cells.append("")
                    continue
                if c.get("t") == "s":  # índice a sharedStrings
                    idx = int(v.text)
                    cells.append(shared[idx] if idx < len(shared) else "")
                else:
                    cells.append(v.text)
            if any(cells):
                out.append(" | ".join(cells))
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# html2text: HTML → markdown limpio                                           #
# --------------------------------------------------------------------------- #
def html2text(path: Path) -> str:
    """Convierte un .html/.htm a markdown legible.

    Usa `markdownify` si está disponible (viene con el motor OKF); si no, cae a
    un extractor de texto con la stdlib (`html.parser`).
    """
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        from markdownify import markdownify as _md
        return _md(html, strip=["script", "style", "link", "meta"]).strip()
    except ImportError:
        from html.parser import HTMLParser

        class _Text(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.skip = 0
                self.buf: list[str] = []

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style"):
                    self.skip += 1
                elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3"):
                    self.buf.append("\n")

            def handle_endtag(self, tag):
                if tag in ("script", "style") and self.skip:
                    self.skip -= 1

            def handle_data(self, data):
                if not self.skip and data.strip():
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
    """Comprueba que todo .md del bundle (salvo index.md) valida como OKF."""
    from reference_agent.bundle.document import OKFDocument  # import perezoso

    bundle = wiki_root(instance)
    errors: list[dict] = []
    checked = 0
    for md in sorted(bundle.rglob("*.md")):
        if md.name == "index.md":
            continue
        if SOURCES_DIRNAME in md.relative_to(bundle).parts:
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
def commit_state(bundle: Path, processed: dict) -> Path:
    """`processed` = salida de `scan` (o subconjunto) ya ingerida.

    Marca new+changed+unchanged como conocidos con su hash; elimina deleted.
    Acepta opcionalmente `processed["concepts"]` = {source_path: [concept_paths]}.
    """
    bundle = Path(bundle)
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
    state_path = bundle / SOURCES_DIRNAME / STATE_FILENAME
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return state_path
