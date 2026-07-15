---
name: okf-ingest
description: Ingiere documentos soltados en una carpeta sources/ y los sintetiza en un bundle Open Knowledge Format (OKF) — páginas markdown por tema con frontmatter, enlaces cruzados y viz.html. Úsala cuando el usuario quiera "ingerir/sintetizar documentos", "actualizar la wiki OKF", "procesar lo que hay en sources", o generar/regenerar una wiki de conocimiento a partir de ficheros (PDF, docx, HTML, md, imágenes).
---

# okf-ingest — soltar documentos → wiki OKF

Eres el **motor de síntesis** de una wiki OKF. El usuario suelta documentos en
`sources/` y tú los conviertes en un bundle OKF: una carpeta de ficheros markdown
organizados **por tema**, con frontmatter, enlaces cruzados y citas a las fuentes.
No hay servidor ni base de datos: son ficheros en disco que tú escribes con `Read`/
`Write`/`Bash`. Las tareas deterministas (escanear, extraer Office, validar, índices,
grafo) las hace el CLI `okf-wiki`; **la redacción la haces tú**.

## Resolver la instancia

Una **instancia** tiene dos carpetas: `sources/` (dropzone con los documentos crudos) y
`wiki/` (el bundle OKF que tú escribes). Los comandos `okf-wiki` reciben la carpeta de la
instancia y resuelven `sources/`/`wiki/` solos. Usa la instancia del argumento de la skill;
si no se da, usa el directorio actual si tiene `sources/` y `wiki/`, y si no, pregunta.
Crear una nueva: `okf-wiki init <dir>`.

**IMPORTANTE:** las páginas que redactes van SIEMPRE dentro de `<instancia>/wiki/` (nunca en
`sources/`, que es solo lectura de entrada). El motor solo mira `wiki/`.

## Flujo de ingesta (síguelo en orden)

1. **Escanear la dropzone.** Ejecuta `okf-wiki scan <bundle>`. Devuelve JSON con
   `new`, `changed`, `unchanged`, `deleted`. Cada entrada trae `path`, `sha256`, `ext`
   y `extract` (`native-read` | `office2text` | `unsupported`).
2. **Filtrar.** Procesa solo `new` + `changed`. Si ambas vacías → informa "nada nuevo
   que ingerir" y termina sin tocar nada. Para `deleted`, revisa qué páginas citaban esa
   fuente y decide si actualizarlas/retirarlas.
3. **Extraer el contenido de cada fuente:**
   - `extract: native-read` (PDF, imágenes, txt/md/csv/json…) → léelo con tu tool **Read**
     nativa (`Read file_path=sources/... [pages=...]`). Los PDFs e imágenes los ves directo.
   - `extract: office2text` (docx/pptx/xlsx) → `okf-wiki office2text sources/<fichero>`
     (imprime el texto a stdout; léelo de ahí).
   - `extract: html2text` (html/htm) → `okf-wiki html2text sources/<fichero>`
     (convierte a markdown limpio; léelo de ahí).
   - `extract: unsupported` → avisa al usuario y sáltalo.
4. **PROPONER el plan y esperar aprobación (obligatorio).** Antes de escribir nada, lee las
   páginas y carpetas de tema existentes y presenta al usuario una propuesta: qué páginas vas
   a crear/actualizar y **en qué tema (carpeta)** irá cada una. Si vas a **crear un tema/
   carpeta nueva**, dilo explícitamente y justifícalo. **Espera el OK del usuario** antes de
   seguir; no inventes temas ni escribas páginas sin confirmación. Ajusta según su respuesta.
5. **Sintetizar / actualizar páginas** ya aprobadas (ver "Reglas OKF" y "Redacción").
   Reutiliza páginas existentes para **actualizar vs crear** y no duplicar. Una sola fuente
   suele tocar entre 3 y 10 páginas — es lo esperado.
6. **Regenerar índices:** `okf-wiki index <bundle>` (genera los `index.md`; no los escribas
   a mano).
7. **Actualizar `log.md`** (a mano, ver sección dedicada).
8. **Regenerar el grafo:** `okf-wiki viz <bundle> --name "<Nombre>"` → reescribe `viz.html`.
   Fíjate en el número de **aristas** que reporta: si no crece con las páginas nuevas,
   probablemente escribiste enlaces absolutos (mira la regla de enlaces).
9. **Validar:** `okf-wiki verify <bundle>`. Debe salir sin errores (exit 0). Si falla,
   corrige el frontmatter del fichero señalado.
10. **Actualizar el manifest:** pásale al CLI el JSON del scan por stdin:
   `echo '<json-del-scan>' | okf-wiki commit-state <bundle>`. Marca las fuentes como
   procesadas (ingesta idempotente: una 2ª pasada sin cambios dirá "nada nuevo").
11. **Proponer commit.** Muestra `git -C <bundle> status` y sugiere
    `git add -A && git commit -m "ingest: <fuentes>"`. **No commitees ni hagas push sin
    permiso del usuario.**

## Reglas del formato OKF (obligatorias)

**Estructura por TEMA, no por tipo.** Las carpetas agrupan por materia (dominio de
negocio), no por "concepto vs entidad":

```
<instancia>/
├── sources/             # dropzone (SOLO lectura de entrada; no escribas aquí)
└── wiki/                # el bundle OKF (aquí escribes TODO)
    ├── log.md           # bitácora append-only (type: Log)
    ├── <tema-a>/        # p.ej. ventas/, producto/, soporte/
    │   ├── pagina.md
    │   └── ...
    ├── <tema-b>/
    └── index.md         # NO lo escribas: lo genera `okf-wiki index`
```

Crea las carpetas de tema que pida el contenido. No impongas una taxonomía fija.

**Frontmatter obligatorio** en cada página (este orden), delimitado por `---`:
```yaml
type: Negocio             # el TEMA de la página (= su carpeta); ver abajo
title: "Título legible"   # SIEMPRE entre comillas
description: "Una frase concreta que resume la página."   # SIEMPRE entre comillas
timestamp: 2026-07-15T00:00:00Z   # ISO 8601 (fecha de hoy). NUNCA uses 'date'
tags: [tag1, tag2]        # mínimo 2
```
- `title` y `description` **siempre entre comillas dobles** (suelen llevar `:` y romperían
  el YAML si no).
- `timestamp` en **ISO 8601** (no `date`). Usa la fecha de hoy.
- **`type` = el tema de la página**, capitalizado, coincidiendo con su carpeta (p.ej. una
  página en `ventas/` lleva `type: Ventas`). El grafo `viz.html` **colorea los nodos por
  `type`** y el desplegable filtra por tema. NO uses `Concepto`/`Entidad` (la relación
  concepto↔entidad se expresa con los enlaces, no con el tipo). La única página raíz,
  `log.md`, lleva `type: Log`.

**Enlaces cruzados — regla dura:** enlaza entre páginas con enlaces markdown **relativos y
con extensión `.md`**:
```
Ver [Plan de retención](../ventas/plan-retencion.md).
```
- PROHIBIDO enlaces absolutos (`/ventas/x.md`) y URLs (`http://…`): el visor los IGNORA y
  no se dibujarían aristas en el grafo. Siempre rutas relativas desde la página actual.
- Nombres de fichero: slug en minúsculas con guiones y **ASCII** (sin acentos) para
  portabilidad git.

**Citas a las fuentes — obligatorias.** Toda afirmación factual cita su fuente con
footnotes markdown, usando el nombre COMPLETO del fichero de `sources/` (+ página en PDFs):
```
El margen bruto creció un 3% interanual.[^1]

[^1]: informe-trimestral.pdf, p.4
```

**Al menos un elemento visual por página** (diagrama Mermaid o tabla), para que la wiki sea
más rica que una respuesta de chat.

## Redacción (estándar de calidad)

- Sin `# H1` (el título va en el frontmatter). Empieza con un párrafo-resumen.
- `##` para secciones, `###` para subsecciones. Una idea por sección; bullets para hechos,
  prosa para síntesis.
- Enlaza a las páginas relacionadas (concepto ↔ entidad se expresa con enlaces, no con
  carpetas). El grafo `viz.html` se construye a partir de esos enlaces.
- **Crear vs actualizar:** busca antes una página del mismo tema; si existe, actualízala
  preservando su frontmatter y subiendo `timestamp` solo si el cambio es sustancial.

## `log.md` (bitácora)

En CADA ingesta añade una entrada a `log.md` (`type: Log`, append-only, NUNCA borres
entradas) con cabecera parseable:
```
## [2026-07-15] ingest | <Título o nombre de las fuentes>
- Página creada: [Título](tema/pagina.md)
- Página actualizada: [Título](tema/otra.md)
- Takeaway: una frase resumen
```

(No hay `overview.md`: la wiki se navega por temas/índices y el grafo `viz.html`.)

## Referencia rápida del CLI `okf-wiki`

| Comando | Para qué |
|---|---|
| `okf-wiki init <dir> [--name N]` | Crear una instancia de wiki nueva |
| `okf-wiki scan <bundle>` | JSON de nuevos/cambiados/borrados en `sources/` |
| `okf-wiki office2text <file>` | Extraer texto de docx/pptx/xlsx |
| `okf-wiki html2text <file>` | Convertir HTML a markdown limpio |
| `okf-wiki index <bundle>` | Regenerar los `index.md` (sin Gemini) |
| `okf-wiki viz <bundle> --name N` | Regenerar `viz.html` (grafo) |
| `okf-wiki verify <bundle>` | Validar que todo `.md` es OKF válido |
| `echo '<scan-json>' \| okf-wiki commit-state <bundle>` | Marcar fuentes como procesadas |
