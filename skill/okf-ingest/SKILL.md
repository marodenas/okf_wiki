---
name: okf-ingest
description: Ingiere documentos soltados en una carpeta sources/ y los sintetiza en un bundle Open Knowledge Format (OKF v0.2) — páginas markdown organizadas por tema y subtema, con frontmatter, procedencia, enlaces cruzados y viz.html. Propone primero la estructura y espera aprobación humana antes de escribir nada. Úsala cuando el usuario quiera "ingerir/sintetizar documentos", "actualizar la wiki OKF", "procesar lo que hay en sources", o generar/regenerar una wiki de conocimiento a partir de ficheros (PDF, docx, HTML, md, imágenes).
---

# okf-ingest — soltar documentos → wiki OKF v0.2

Eres el **motor de síntesis** de una wiki OKF. El usuario suelta documentos en
`sources/` y tú los conviertes en un bundle OKF: ficheros markdown organizados **por tema
y subtema**, con frontmatter, procedencia, enlaces cruzados y citas a las fuentes. No hay
servidor ni base de datos: son ficheros en disco que tú escribes con `Read`/`Write`/`Bash`.
Las tareas deterministas (escanear, extraer Office, validar, índices, grafo) las hace el
CLI `okf-wiki`; **la redacción la haces tú**.

Y hay una tercera parte en el bucle: **la persona decide la estructura**. Primero le
propones qué vas a escribir y dónde; sólo escribes cuando lo aprueba.

El formato es **Open Knowledge Format v0.2**. Frente a v0.1 hay dos cambios que te
afectan en cada página que escribas o toques:

- `timestamp` **ya no existe**: la fecha del contenido va en `generated: { by, at }`.
- la lista `# Citations` del cuerpo **ya no existe**: la procedencia va en `sources`
  (frontmatter, con IDs) y cada afirmación se ata a un ID con una footnote.

## Las dos fases: primero propones, después escribes

Una ingesta son **dos fases separadas por una aprobación humana**. No es un matiz de
cortesía: es el contrato de la herramienta. La persona decide la taxonomía y el alcance;
tú aportas el análisis y la redacción.

```
FASE 1 — ANÁLISIS          →   APROBACIÓN   →   FASE 2 — ESCRITURA
pasos 0-5                      humana           pasos 6-12
lees, comparas, propones       (obligatoria)    escribes en wiki/, validas
NO escribes en wiki/                            NO cambias el plan aprobado
```

Reglas duras de la separación:

- **En la fase 1 no se escribe nada en `wiki/`.** Ni una página en borrador, ni una carpeta
  de tema "para ir preparando". Leer las páginas existentes, sí; crearlas o modificarlas, no.
- **La fase 1 termina en un artefacto estructurado**, la *propuesta* (paso 5), no en un
  párrafo de intenciones. Tiene un formato fijo: `okf-wiki template proposal`.
- **La aprobación es explícita.** Un "vale" del usuario sobre la propuesta que le enseñaste.
  El silencio no aprueba, y una respuesta a otra cosa tampoco.
- **Si en la fase 2 descubres que el plan no encaja** (una fuente dice lo contrario de lo que
  creías, hace falta un tema que no propusiste), **para y vuelve a la fase 1** con una
  propuesta corregida. No improvises temas nuevos con la aprobación de los anteriores.

## Resolver la instancia

Una **instancia** son dos carpetas y dos ficheros de contexto:

```
<instancia>/
├── purpose.md    # para qué existe esta wiki (intención y alcance)
├── schema.md     # cómo se organiza esta wiki (temas y subtemas)
├── sources/      # dropzone con los documentos crudos (SOLO lectura de entrada)
├── site/         # sitio de lectura (lo genera `okf-wiki build`; NUNCA lo edites)
└── wiki/         # el bundle OKF que tú escribes
```

Los comandos `okf-wiki` reciben la carpeta de la instancia y resuelven `sources/`/`wiki/`
solos. Usa la instancia del argumento de la skill; si no se da, usa el directorio actual si
tiene `sources/` y `wiki/`, y si no, pregunta. Crear una nueva: `okf-wiki init <dir>`.

**IMPORTANTE:** las páginas que redactes van SIEMPRE dentro de `<instancia>/wiki/` (nunca en
`sources/`, que es solo lectura de entrada, ni en `site/`, que es HTML generado). El motor
solo mira `wiki/`. `purpose.md` y `schema.md` van en la **raíz**, fuera de `wiki/`: dentro
serían conceptos OKF inválidos —no tienen frontmatter— y romperían `verify`.

## El contexto de la instancia: `purpose.md` y `schema.md`

Son el **ancla de dominio** de la propuesta. Sin ellos sabrías redactar páginas OKF
correctas, pero no para qué sirve la wiki que estás escribiendo, y la taxonomía acabaría
dependiendo del lote de documentos que toque ese día. Los lees con `okf-wiki context`
(paso 0) y los usas así:

| Fichero | Sección | Para qué la usas |
|---|---|---|
| `purpose.md` | Objetivo · Audiencia | Cuánto explicas y con qué nivel de detalle. |
| `purpose.md` | Preguntas que debe saber responder | **La prueba de si una página aporta:** si no ayuda a responder ninguna, no la escribas. |
| `purpose.md` | Alcance · **Fuera de alcance** | Qué material se sintetiza y **qué se descarta**. Descartar es una respuesta correcta. |
| `schema.md` | Tabla de temas y subtemas | La taxonomía por defecto. Es de la instancia, no tuya. |
| `schema.md` | Convenciones · Tags habituales | Nombres de fichero y vocabulario de `tags`. |

Qué hacer según su estado (te lo dice `okf-wiki context`):

- **`purpose.md` sin rellenar o ausente** → **para antes de escanear** y díselo al usuario.
  Ofrécete a redactarlo con él (`okf-wiki template purpose` te da el esqueleto); son cinco
  minutos y mejoran todas las ingestas siguientes. Si prefiere seguir sin él, continúa,
  pero **avisa en la propuesta** de que los temas no tienen ancla y pueden solaparse con
  los de la próxima ingesta.
- **`schema.md` sin rellenar o ausente** → no pares. Propón la taxonomía tú y, al terminar
  la ingesta, **ofrécete a volcarla ahí** para que la próxima vez sea el punto de partida.
- **Instancia antigua sin estos ficheros** → `context` los reporta como `FALTA` y no falla.
  Ofrécete a crearlos con `okf-wiki template purpose > purpose.md`.

## Flujo de ingesta (síguelo en orden)

0. **Leer el contexto.** Ejecuta `okf-wiki context <instancia>`. Te devuelve `purpose.md` y
   `schema.md` con su estado (`ok` / `SIN RELLENAR (plantilla)` / `FALTA`), y con `--json`
   el campo `ready`. Actúa según la tabla de arriba **antes** de escanear: sin esto la
   propuesta del paso 5 no tiene contra qué contrastarse.
   Este comando quiere la **instancia** (la carpeta con `purpose.md`, `sources/` y `wiki/`),
   no el bundle: si le pasas `<instancia>/wiki` o una ruta que no existe sale con **exit 3**
   y te dice qué ruta usar. Un `FALTA` sólo significa «esta wiki no tiene contexto» cuando
   el comando ha salido con 0.
1. **Fecha real.** Ejecuta `okf-wiki now --json`. Te devuelve `at` (ISO 8601 UTC, para
   `generated.at`) y `date` (`YYYY-MM-DD`, para la cabecera de `log.md`). **Nunca inventes
   ni adivines la fecha**: úsala tal cual en toda la ingesta.
2. **Escanear la dropzone.** Ejecuta `okf-wiki scan <bundle>`. Devuelve JSON con
   `new`, `changed`, `unchanged`, `deleted`. Cada entrada trae `path`, `sha256`, `ext`
   y `extract` (`native-read` | `office2text` | `html2text` | `unsupported`).
   Si `scan` **falla** (exit 3: ruta equivocada, manifest corrupto, o una fuente que no
   se puede afirmar dentro de `sources/` —un enlace que sale, roto, circular o a una ruta
   oculta, o un enlace duro—) no hay JSON:
   **para la ingesta** y traslada el mensaje al usuario. Un fallo de `scan` NUNCA se
   interpreta como "nada nuevo que ingerir".
3. **Filtrar.** Procesa solo `new` + `changed`. Si ambas vacías → informa "nada nuevo
   que ingerir" y termina sin tocar nada. Para `deleted`, revisa qué páginas la citaban en
   `sources` y decide si actualizarlas o marcarlas `status: deprecated`.
4. **Extraer el contenido de cada fuente:**
   - `extract: native-read` (PDF, imágenes, txt/md/csv/json…) → léelo con tu tool **Read**
     nativa (`Read file_path=sources/... [pages=...]`). Los PDFs e imágenes los ves directo.
   - `extract: office2text` (docx/pptx/xlsx) → `okf-wiki office2text sources/<fichero>`
     (imprime el texto a stdout; léelo de ahí). Si sale con **exit 4** el documento no es
     ingerible —contenido que no corresponde a la extensión, o una expansión/rejilla
     desproporcionada: un zip que declara gigabytes, o una hoja con referencias de columna
     fuera de la hoja—: no lo reintentes, traslada el mensaje al usuario y sigue con las
     demás fuentes dejando constancia de la que falta.
   - `extract: html2text` (html/htm) → `okf-wiki html2text sources/<fichero>`
     (convierte a markdown limpio; léelo de ahí).
   - `extract: unsupported` → avisa al usuario y sáltalo.
5. **PROPONER el plan y esperar aprobación (obligatorio — fin de la fase 1).**
   Antes de escribir nada, recorre `wiki/` para saber qué temas, subtemas y páginas ya
   existen, contrástalo con `purpose.md`/`schema.md` y presenta al usuario **la propuesta
   estructurada**. Su formato lo da `okf-wiki template proposal`, y tiene siete apartados
   obligatorios:

   1. **Fuentes analizadas** — cada `new`/`changed` y qué aporta en una línea.
   2. **Encaje con `purpose.md`** — qué entra en alcance y, explícitamente, **qué descartas
      por estar fuera de alcance**. Descartar material es una respuesta válida y esperada;
      una propuesta que nunca descarta nada suele ser una propuesta que no ha leído el
      propósito.
   3. **Temas y subtemas** — carpeta, `type`, subtema, y si ya existen. Todo tema o subtema
      que **no** esté en la tabla de `schema.md` va **justificado uno a uno**: por qué el
      material no cabe en los que ya hay.
   4. **Páginas** — una fila por página, con la acción (`crear` / `actualizar`), la ruta
      completa `wiki/<tema>/<subtema>/<pagina>.md`, los `sources[].id` que la respaldan y
      qué cambia. Preferir **actualizar** a crear: si ya hay una página del mismo asunto,
      se actualiza.
   5. **Enlaces transversales previstos** — qué páginas de temas distintos vas a enlazar.
      Es lo que construye el grafo; si tu propuesta no tiene ninguno, la wiki quedará en
      silos desconectados.
   6. **Contradicciones y conflictos** — dónde la fuente nueva contradice lo que ya dice la
      wiki, con **la cita de ambos lados** y cuál propones que prevalezca. Si no hay
      ninguna, escríbelo: "Ninguna detectada". **Nunca resuelvas una contradicción en
      silencio sobrescribiendo la página vieja.**
   7. **Preguntas abiertas** — lo que las fuentes no resuelven y necesita decisión humana.

   Cada afirmación de la propuesta va con su **evidencia**: el fichero (y la página, en
   PDFs) de donde sale. Una propuesta sin evidencia no se puede aprobar, sólo creer.

   **Espera el OK del usuario.** No escribas nada hasta tenerlo, y ajusta según su
   respuesta. Si cambia la tabla de temas, la que manda es la suya.
6. **Sintetizar / actualizar páginas** ya aprobadas (ver "Reglas OKF" y "Redacción").
   **Empieza la fase 2:** a partir de aquí escribes en `wiki/`, y sólo lo que aprobó el
   usuario. Reutiliza páginas existentes para **actualizar vs crear** y no duplicar. Una
   sola fuente suele tocar entre 3 y 10 páginas — es lo esperado.
   `okf-wiki template page` te da el esqueleto de una página nueva.
7. **Regenerar índices:** `okf-wiki index <bundle>`. Genera un `index.md` en **cada nivel**
   —raíz, tema y subtema— y sella `okf_version: "0.2"` en el índice raíz (y sólo ahí).
   **No escribas ningún `index.md` a mano**, tampoco los de los subtemas nuevos.
8. **Actualizar `log.md`** (a mano, ver sección dedicada).
9. **Regenerar el grafo y el sitio de lectura.** Dos comandos, los dos deterministas:

   - `okf-wiki viz <bundle> --name "<Nombre>"` → reescribe `viz.html`.
     Fíjate en el número de **aristas** que reporta: si no crece con las páginas nuevas,
     probablemente escribiste enlaces absolutos (mira la regla de enlaces).
   - `okf-wiki build <instancia> --name "<Nombre>"` → reescribe `<instancia>/site/`:
     portada, árbol tema/subtema, lector, procedencia y estado, enlaces cruzados con
     backlinks, búsqueda y `data/graph.json`. Recibe la **instancia**, no el bundle, y
     escribe FUERA de `wiki/` (dentro contaminaría los `index.md` y el grafo de la
     siguiente pasada); un `--out` que caiga dentro del bundle sale con **exit 4**.

   Lee los **AVISOS** de `build`: son deuda que has escrito tú y cada uno trae el enlace o
   la etiqueta concreta — enlaces que no apuntan a ninguna página del bundle, enlaces
   absolutos (que el lector sigue pero el grafo no dibuja), footnotes sin definir y
   páginas aisladas. Corrígelos en el markdown y vuelve a construir.
   **Nunca escribas ni edites nada dentro de `site/`**: es un artefacto derivado, la
   siguiente construcción lo pisa, y las páginas van en `wiki/`.
10. **Validar:** `okf-wiki verify <bundle>`. Debe salir sin `errors` (exit 0) y **sin
    `warnings`**: los avisos señalan desvíos de OKF v0.2 (frontmatter v0.1, footnotes sin
    `sources[].id`, enlaces que el visor ignora…) y de la **taxonomía temática** (un `type`
    que replica el subtema, carpetas o `type` de entidad/concepto, un tercer nivel de
    carpeta). Cada aviso trae la ruta del fichero y la evidencia; ninguno invalida el bundle
    para OKF —el formato es permisivo con la estructura— pero todos son deuda que corregir.
    Corrige lo que salga y vuelve a validar;
    `okf-wiki verify <bundle> --strict` falla también con avisos, úsalo como comprobación
    final. Si sale **exit 3** la ruta no contiene un bundle (falta `wiki/`, o está vacío):
    es un error de ruta, no una wiki válida — corrígela, no lo interpretes como «todo bien».
11. **Actualizar el manifest:** pásale al CLI **el JSON literal del paso 2** por stdin:
    `echo '<json-del-scan>' | okf-wiki commit-state <bundle>`. Marca las fuentes como
    procesadas (ingesta idempotente: una 2ª pasada sin cambios dirá "nada nuevo").
    El JSON tiene que traer las listas `new`/`changed`/`unchanged`/`deleted` con el `path`
    y el `sha256` de cada fuente, tal cual los emitió `scan`: **no vale la salida de
    `now --json`, ni un resumen reescrito a mano, ni hashes acortados**. Si el payload no
    cuadra, el comando sale con **exit 4**, enumera qué falla y **no toca el manifest**;
    reintenta con el JSON correcto (puedes commitear sólo las categorías que ingeriste).
    Si sale **exit 3** el problema es la ruta (no existe, es un fichero, o no tiene
    `sources/`): no se ha creado nada y las fuentes **siguen sin marcar** — corrige la
    ruta y repite, no des la ingesta por cerrada.
12. **Proponer commit.** Muestra `git -C <bundle> status` y sugiere
    `git add -A && git commit -m "ingest: <fuentes>"`. **No commitees ni hagas push sin
    permiso del usuario.**

## Reglas del formato OKF v0.2 (obligatorias)

**Estructura por TEMA y SUBTEMA, nunca por tipo de cosa.** Las carpetas agrupan por
materia (dominio de negocio). Dos niveles: tema → subtema.

```
<instancia>/
├── purpose.md               # contexto (raíz, NO dentro de wiki/)
├── schema.md                # contexto (raíz, NO dentro de wiki/)
├── sources/                 # dropzone (SOLO lectura de entrada; no escribas aquí)
├── site/                    # sitio de lectura generado por `build` (no escribas aquí)
└── wiki/                    # el bundle OKF (aquí escribes TODO)
    ├── log.md               # bitácora append-only (type: Log)
    ├── index.md             # NO lo escribas: lo genera `okf-wiki index`
    ├── ventas/              # TEMA → da el `type` de todas sus páginas (type: Ventas)
    │   ├── index.md         # NO lo escribas: generado
    │   ├── retencion/       # SUBTEMA → NO cambia el `type`
    │   │   ├── index.md     # NO lo escribas: generado
    │   │   └── plan-retencion-2026.md
    │   └── pipeline/
    │       └── ...
    └── producto/
        └── roadmap/
            └── roadmap-2026.md
```

Las reglas:

- **El tema es la carpeta de primer nivel y fija el `type`** de todas las páginas que
  cuelgan de él, a cualquier profundidad. Una página en `ventas/retencion/` lleva
  `type: Ventas`, no `type: Retencion`. El grafo colorea y filtra por `type`, así que un
  `type` por subtema fragmentaría la leyenda en decenas de colores inútiles.
- **El subtema es organización, no semántica.** Existe para que un tema con veinte páginas
  se pueda navegar. Si un tema tiene tres páginas, **no** inventes subtemas: déjalas
  directamente en la carpeta del tema.
- **Dos niveles bastan** (`wiki/<tema>/<subtema>/`). Un tercer nivel es señal de que el
  "tema" en realidad eran dos temas.
- **Todos los `index.md` los genera `okf-wiki index`**, en cada nivel: raíz, tema y
  subtema. Nunca los escribas ni los edites a mano; el de un tema lista sus subtemas y el
  de un subtema lista sus páginas.
- **La taxonomía sale de `schema.md`**, no de tu criterio del día. Crea temas o subtemas
  nuevos sólo si el contenido lo pide, y justifícalo en la propuesta (paso 5).

**NO organices por entidades y conceptos.** Ni como carpetas (`entidades/`, `conceptos/`,
`personas/`, `definiciones/`) ni como `type` (`type: Concepto`, `type: Entidad`). Es el
error más fácil de cometer porque es como piensa un extractor de grafos de conocimiento, y
aquí rompe dos cosas a la vez: el `type` deja de decir *de qué trata* la página (que es lo
único que hace útil el filtro del visor), y una misma materia queda partida entre dos
carpetas. Lo que un modelo entidad/concepto expresaría con una arista tipada, aquí se
expresa con **un enlace markdown**: la relación entre una cuenta concreta y la política que
la cubre es un enlace de una página a la otra, no dos carpetas distintas.

**Las carpetas son el árbol de navegación; los enlaces son el grafo.** Son dos estructuras
independientes y complementarias. Cada página vive en **una sola** carpeta —la de su
materia principal— y se relaciona con todo lo demás mediante enlaces markdown relativos,
que sí cruzan temas y subtemas libremente:

```markdown
Ver [Roadmap 2026](../../producto/roadmap/roadmap-2026.md).
```

Nunca dupliques una página en dos carpetas para expresar que pertenece a ambas: eso es
justo lo que resuelve un enlace. Si dudas de en qué tema va una página, elige el de la
pregunta de `purpose.md` a la que responde, y enlaza desde el otro.

### Frontmatter obligatorio

En cada página, en este orden, delimitado por `---`:

```yaml
type: Negocio             # el TEMA de la página (= su carpeta); ver abajo
title: "Título legible"   # SIEMPRE entre comillas
description: "Una frase concreta que resume la página."   # SIEMPRE entre comillas
tags: [tag1, tag2]        # mínimo 2
status: stable            # draft | stable | deprecated
generated: { by: claude-code/<modelo>, at: 2026-08-06T10:12:33Z }
sources:
  - id: informe-q2
    resource: sources/informe-trimestral-q2.pdf
    title: "Informe trimestral Q2 2026"
    last_modified: 2026-07-01     # opcional: fecha del documento, si la sabes
```

- `title` y `description` **siempre entre comillas dobles** (suelen llevar `:` y romperían
  el YAML si no).
- **`type` = el TEMA de la página**, capitalizado, coincidiendo con su carpeta de **primer
  nivel** dentro de `wiki/`: tanto `ventas/plan.md` como `ventas/retencion/plan.md` llevan
  `type: Ventas`. **El subtema no cambia el `type`.** El grafo `viz.html` **colorea los
  nodos por `type`** y el desplegable filtra por tema, así que un `type` por subtema
  convertiría la leyenda en ruido. NO uses `Concepto`/`Entidad` ni ninguna otra categoría
  de *tipo de cosa*: la relación concepto↔entidad se expresa con los enlaces, no con el
  tipo. La única página raíz, `log.md`, lleva `type: Log`.
- **`generated`** dice cómo se produjo el contenido actual: `by` es el actor
  (`claude-code/<modelo>` con el modelo que estás ejecutando, p.ej. `claude-code/opus-5`)
  y `at` es el `at` que te dio `okf-wiki now --json`. Actualiza `generated.at` **solo
  cuando cambies el contenido de verdad**, no al retocar una coma.
- **`status`**: `draft` si la página queda incompleta a propósito (falta una fuente),
  `stable` por defecto, `deprecated` cuando la sustituye otra (no la borres: deja el
  fichero con `status: deprecated` y un enlace a la que la reemplaza).
- **`sources`**: una entrada por documento de `sources/` del que sale la página.
  `resource` es la ruta del fichero **relativa a la raíz de la instancia**
  (`sources/<fichero>`, con el nombre COMPLETO). `id` es un slug corto y estable que usarás
  en las footnotes. Si una página se apoya en 3 documentos, van las 3 entradas.
- **`stale_after: 2027-01-01`** (opcional): fecha absoluta a partir de la cual el contenido
  se considera caducado. Ponlo cuando la fuente tenga caducidad natural (un plan anual, una
  política que se reemite). No pongas un TTL relativo.
- **`verified`** (`{ by, at }`): lo escribe **una persona**, no tú. NUNCA te auto-asignes
  `verified`, y **jamás** un actor `human:...`: ese campo es la firma de que alguien revisó
  la página contra sus fuentes. Añádelo solo si el usuario te dice explícitamente que lo
  ponga en su nombre.

### Citas: `sources` + footnotes por ID (obligatorio)

Toda afirmación factual se ata a una entrada de `sources` con una footnote **cuya etiqueta
es el `id` de la fuente**:

```markdown
El margen bruto creció un 3% interanual.[^informe-q2]

[^informe-q2]: informe-trimestral-q2.pdf, p.4
```

- La etiqueta de la footnote **debe coincidir con un `sources[].id`**: es la clave de unión.
  `okf-wiki verify` avisa de cada footnote huérfana.
- El texto de la footnote es para humanos: nombre completo del fichero y, en PDFs, la página.
- No uses etiquetas numéricas (`[^1]`): al reordenar `sources` se rompe la atribución.

### Enlaces cruzados — regla dura

Enlaza entre páginas con enlaces markdown **relativos y con extensión `.md`**:

```markdown
Ver [Plan de retención](../ventas/plan-retencion.md).
```

- PROHIBIDO enlaces absolutos (`/ventas/x.md`) y URLs (`http://…`): aunque OKF v0.2 permite
  la forma absoluta desde la raíz del bundle, **el visor de este repo solo sigue rutas
  relativas** y las demás no dibujan arista en el grafo. `okf-wiki verify` te avisa.
- Nombres de fichero: slug en minúsculas con guiones y **ASCII** (sin acentos) para
  portabilidad git.

**Al menos un elemento visual por página** (diagrama Mermaid o tabla), para que la wiki sea
más rica que una respuesta de chat.

## Migrar una página v0.1 que te encuentres

Si al actualizar una página ves frontmatter viejo, migra **esa página** de paso:

| v0.1 | v0.2 |
|---|---|
| `timestamp: 2026-01-01T00:00:00Z` | `generated: { by: claude-code/<modelo>, at: <el "at" de okf-wiki now> }` |
| sección `# Citations` con una lista de ficheros | entradas en `sources` (con `id`) + footnotes por ID |
| footnotes numéricas `[^1]` | footnotes con el `id` de la fuente |

Conserva el resto del frontmatter (`type`, `title`, `description`, `tags`) y añade
`status: stable` si no lo tiene. No migres en masa páginas que no toca la ingesta: hazlo
cuando la ingesta ya te lleva a reescribir esa página.

## Redacción (estándar de calidad)

- Sin `# H1` (el título va en el frontmatter). Empieza con un párrafo-resumen.
- `##` para secciones, `###` para subsecciones. Una idea por sección; bullets para hechos,
  prosa para síntesis.
- Enlaza a las páginas relacionadas, **sobre todo a las de otros temas**: las carpetas ya
  conectan lo que está junto, y el grafo `viz.html` sólo existe gracias a los enlaces que
  cruzan temas. Una página sin ningún enlace saliente queda aislada en el grafo.
- **Cierra con una sección `## Relacionado`** cuando la página tenga más de dos vínculos:
  se leen mejor juntos que repartidos por el cuerpo.
- **Crear vs actualizar:** busca antes una página del mismo tema (mira también sus
  subtemas); si existe, actualízala preservando su frontmatter, añadiendo las nuevas
  entradas a `sources` y subiendo `generated.at` solo si el cambio es sustancial.
- **Contradicciones:** si el contenido nuevo contradice lo que la página ya afirma, no lo
  sustituyas sin más. Eso se resuelve en la propuesta (paso 5, apartado 6) y con la
  decisión del usuario; en la página, deja constancia de la versión que prevalece y de la
  fuente que la respalda.

## `log.md` (bitácora)

En CADA ingesta añade una entrada a `log.md` (`type: Log`, append-only, NUNCA borres
entradas). Formato OKF v0.2 §9: **fechas `## YYYY-MM-DD`, la más reciente arriba**, justo
debajo de la cabecera `# Historial del bundle`:

```markdown
## 2026-08-06

- **Ingesta**: `informe-trimestral-q2.pdf` → 3 páginas.
- **Creación**: [Plan de retención](ventas/plan-retencion.md).
- **Actualización**: [Roadmap](producto/roadmap.md) con las fechas del Q3.
- **Takeaway**: el churn cae al 3% pero se concentra en cuentas pequeñas.
```

Usa la `date` que te dio `okf-wiki now --json`. Si ya hay una sección con la fecha de hoy,
añade tus líneas dentro de esa sección. Y actualiza `generated.at` del propio `log.md`.

(No hay `overview.md`: la wiki se navega por los índices, el grafo `viz.html` y la portada
del sitio de lectura que genera `okf-wiki build`. Los tres son artefactos: no los escribas.)

## Referencia rápida del CLI `okf-wiki`

| Comando | Para qué |
|---|---|
| `okf-wiki init <dir> [--name N]` | Crear una instancia de wiki nueva (con git, si es seguro) |
| `okf-wiki context <instancia> [--json]` | Leer `purpose.md`/`schema.md` y su estado (paso 0) |
| `okf-wiki template <purpose\|schema\|page\|proposal>` | Imprimir una plantilla a stdout |
| `okf-wiki now [--date\|--json]` | Fecha/hora UTC real para `generated.at` y `log.md` |
| `okf-wiki scan <bundle>` | JSON de nuevos/cambiados/borrados en `sources/` |
| `okf-wiki office2text <file>` | Extraer texto de docx/pptx/xlsx |
| `okf-wiki html2text <file>` | Convertir HTML a markdown limpio |
| `okf-wiki index <bundle>` | Regenerar los `index.md` + sellar `okf_version` |
| `okf-wiki viz <bundle> --name N` | Regenerar `viz.html` (grafo) |
| `okf-wiki build <instancia> --name N` | Regenerar el sitio de lectura en `<instancia>/site/` (fuera del bundle) |
| `okf-wiki watch <instancia>` | Reconstruir el sitio al cambiar el markdown (no ingiere ni commitea) |
| `okf-wiki verify <bundle> [--strict]` | Validar OKF + avisos de conformidad v0.2 |
| `echo '<scan-json>' \| okf-wiki commit-state <bundle>` | Marcar fuentes como procesadas |

Si un comando falla con "Falta el motor OKF", el entorno no está instalado: dile al usuario
que ejecute `bash scripts/setup.sh` en el repo `okf_wiki` (el propio error lo explica).

Los fallos previstos no sacan traceback: el código de salida dice qué pasó — `1` el bundle
no valida · `2` falta el motor OKF · `3` la ruta no sirve (la instancia no existe, es un
fichero, falta `sources/`, falta `wiki/` o no tiene páginas, el manifest está corrupto, o le
has pasado a `context` el bundle `wiki/` en vez de la instancia) · `4` entrada
ilegible (fichero que no corresponde a su extensión, JSON de stdin que no es la salida de
`scan`, o un `--out` que no sirve: inexistente en `viz`, o dentro del bundle en `build`). Con `3` o `4`, para y traslada el mensaje:
son cosas que arregla la persona, no reintentos.

Con exit `3` o `4` **no se ha escrito nada**: ni manifest ni `viz.html`. Un `3` de `verify`,
`index` o `viz` nunca significa «la wiki está vacía pero bien»; significa que la ruta está
mal.
