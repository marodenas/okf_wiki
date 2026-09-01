"""Plantillas de texto que produce `okf-wiki`: contexto de instancia y andamiaje.

Aquí vive todo el texto que el CLI escribe en disco o imprime para que lo copie
la skill. Está separado de `cli.py` por dos motivos:

- **Son contrato con la skill.** `okf-ingest` describe en prosa lo que estas
  plantillas materializan (contexto, propuesta, página de subtema). Si divergen,
  el agente redacta contra una estructura que no existe. Tenerlas en un módulo
  propio permite que los tests las comprueben contra `SKILL.md` y el `README`.
- **Los stubs se detectan por marcador, no "a ojo".** Cada plantilla que el
  usuario tiene que rellenar empieza por `STUB_MARKER`; el usuario borra esa
  línea al rellenarla y el CLI decide con una comprobación de subcadena si el
  contexto está listo. La alternativa —que el LLM juzgue si un texto "parece"
  una plantilla— es justo el tipo de decisión que sale inconsistente.

Las plantillas no llevan frontmatter YAML **a propósito**: `purpose.md` y
`schema.md` viven en la raíz de la instancia, fuera de `wiki/`, así que el motor
OKF no los ve. Meterlos dentro del bundle los convertiría en conceptos inválidos
y rompería `verify`.
"""
from __future__ import annotations

# Primera línea de toda plantilla que el usuario debe rellenar. Se comprueba por
# subcadena (no por igualdad) para que sobreviva a que alguien retoque el texto
# que la acompaña sin borrar la línea entera.
STUB_MARKER = "<!-- okf-wiki:stub"

# Token que sustituye `render()`. No se usa `str.format` porque las plantillas
# llevan llaves literales (el flow mapping `{ by: …, at: … }` del frontmatter).
_NAME_TOKEN = "<<NOMBRE>>"


PURPOSE_STUB = """\
<!-- okf-wiki:stub — Rellena este fichero y borra esta línea. -->
# Propósito de <<NOMBRE>>

<!--
Guía de INTENCIÓN y ALCANCE de esta wiki concreta.

`okf-ingest` lee este fichero ANTES de cada ingesta y lo usa para decidir qué se
sintetiza y qué se descarta. Sin él, la taxonomía que proponga el agente depende
del lote de documentos que le toque ese día, y de ahí salen temas solapados y
decisiones "crear vs actualizar" incoherentes entre ingestas.

Este fichero vive en la raíz de la instancia, FUERA de `wiki/`: no es una página
OKF, no lleva frontmatter y no aparece en los índices ni en el grafo.

Cuando lo hayas rellenado, borra la primera línea (la del marcador `okf-wiki:stub`).
Comprueba el estado con `okf-wiki context <instancia>`.
-->

## Objetivo

<!-- Una o dos frases: para qué existe esta wiki y qué decisión ayuda a tomar. -->

## Preguntas que debe saber responder

<!-- La prueba de si una página aporta: si no ayuda a responder ninguna de estas
     preguntas, no se escribe. Sé concreto; tres o cuatro bastan para empezar. -->

-
-

## Alcance

<!-- Qué materia entra: dominios, periodo cubierto, tipos de documento. -->

-

## Fuera de alcance

<!-- La sección que más trabaja. Es lo que permite al agente DESCARTAR material
     en vez de crear páginas por inercia. Si la dejas vacía, se sintetiza todo. -->

-

## Audiencia

<!-- Quién lee esto y cuánto contexto hay que dar por sabido. -->
"""


SCHEMA_STUB = """\
<!-- okf-wiki:stub — Rellena este fichero y borra esta línea. -->
# Estructura de <<NOMBRE>>

<!--
Taxonomía de ESTA instancia: los temas y subtemas en los que se organiza `wiki/`,
más las convenciones propias.

Existe porque las reglas de taxonomía no pueden estar cableadas en la skill: la
skill es global (la comparten todas tus wikis) y cada wiki necesita su propia
tabla de temas. Lo que pongas aquí manda sobre lo que la skill supondría.

Cuando lo hayas rellenado, borra la primera línea (la del marcador `okf-wiki:stub`).
Si lo dejas sin rellenar la ingesta sigue: el agente propondrá una taxonomía y se
ofrecerá a volcarla aquí al terminar.
-->

## Temas y subtemas

<!--
Un TEMA es una carpeta de primer nivel dentro de `wiki/` y da el valor de `type`
de todas sus páginas (`ventas/` → `type: Ventas`), que es lo que colorea y filtra
el grafo.

Un SUBTEMA es una carpeta dentro de un tema: agrupa páginas para poder navegarlas.
El subtema NO cambia el `type`, que sigue siendo el del tema padre.

Las carpetas son jerarquía de navegación. Las relaciones transversales —una
página de un tema que habla de otra de un tema distinto— se expresan SIEMPRE con
enlaces markdown relativos, nunca duplicando la página en dos carpetas.
-->

| Tema (carpeta) | `type` | Subtemas (carpetas) | Qué contiene |
|---|---|---|---|
| `ejemplo/` | `Ejemplo` | `sub-a/`, `sub-b/` | Sustituye esta fila por tus temas reales. |

### Prohibido (esto no lo decide la instancia)

<!--
Las cuatro reglas de abajo son del formato, no de tu taxonomía: la tabla de arriba
es tuya, estas no. `okf-wiki verify` las comprueba y las saca como avisos con la
ruta del fichero; `okf-wiki verify --strict` falla con ellas.

1. NO organizar por TIPO DE COSA. Ni como carpeta (`entidades/`, `conceptos/`,
   `personas/`, `definiciones/`, `glosario/`) ni como `type` (`type: Entidad`,
   `type: Concepto`). El `type` dice DE QUÉ TRATA la página, que es lo único que
   hace útil el filtro y el color del grafo.
2. El `type` lo fija el TEMA (la carpeta de primer nivel), a cualquier
   profundidad: `ventas/retencion/plan.md` lleva `type: Ventas`, nunca
   `type: Retencion`. Un `type` por subtema fragmenta la leyenda del grafo en
   decenas de colores inútiles.
3. Como máximo DOS niveles de carpeta: `<tema>/<subtema>/`. Un tercer nivel es
   señal de que el "tema" en realidad eran dos temas.
4. Cada página vive en UNA sola carpeta. Que una materia pertenezca a dos temas se
   expresa con un enlace markdown relativo entre las dos páginas, NUNCA duplicando
   el fichero en dos carpetas.
-->

## Convenciones propias

<!-- Nombres de fichero, granularidad de página, qué NO merece página propia… -->

-

## Tags habituales

<!-- Vocabulario de `tags` para que no se inventen sinónimos en cada ingesta. -->

-
"""


# Andamiaje de una página. No es un stub del usuario (no lleva `STUB_MARKER`):
# lo consume el agente al redactar, y los `<…>` son huecos que debe rellenar.
PAGE_TEMPLATE = """\
---
type: <Tema>
title: "<Título legible>"
description: "<Una frase concreta que resume la página.>"
tags: [<tag1>, <tag2>]
status: draft
generated: { by: claude-code/<modelo>, at: <el at de okf-wiki now --json> }
sources:
  - id: <slug-corto-y-estable>
    resource: sources/<fichero-completo.pdf>
    title: "<Título del documento>"
---

<Párrafo-resumen: qué es esto y por qué importa. Sin `# H1`: el título ya está
en el frontmatter.>

## <Sección>

<Prosa para la síntesis, bullets para los hechos. Cada afirmación factual se ata
a una fuente con una footnote cuya etiqueta es el `id`.>[^<slug-corto-y-estable>]

| <Columna> | <Columna> |
|---|---|
| <Al menos un elemento visual por página: esta tabla o un diagrama Mermaid.> | |

## Relacionado

<Los enlaces son el grafo: enlaza a las páginas de OTROS temas con las que esta
se relaciona, con rutas relativas y extensión `.md`.>

- [<Título de la otra página>](../../<otro-tema>/<su-subtema>/<su-pagina>.md)

[^<slug-corto-y-estable>]: <fichero-completo.pdf>, p.<n>
"""


# Artefacto del paso de ANÁLISIS. Se enseña al usuario y se espera su aprobación
# antes de escribir nada; no se guarda en `wiki/`.
PROPOSAL_TEMPLATE = """\
# Propuesta de ingesta — <fecha>

## 1. Fuentes analizadas

| Fuente | Estado | Qué aporta |
|---|---|---|
| `sources/<fichero>` | nuevo / cambiado | <en una línea> |

## 2. Encaje con `purpose.md`

- **Dentro de alcance:** <qué material se sintetiza y a qué pregunta responde>
- **Descartado por estar fuera de alcance:** <qué material NO se sintetiza y por qué>

## 3. Temas y subtemas

| Tema (carpeta) | `type` | Subtema | ¿Existe ya? | Justificación si es nuevo |
|---|---|---|---|---|
| `<tema>/` | `<Tema>` | `<subtema>/` | sí / NO | <por qué no cabe en la tabla de `schema.md`> |

## 4. Páginas

| Acción | Ruta | Fuentes (`sources[].id`) | Qué cambia |
|---|---|---|---|
| crear | `wiki/<tema>/<subtema>/<pagina>.md` | <ids> | <resumen> |
| actualizar | `wiki/<tema>/<subtema>/<otra>.md` | <ids> | <qué se añade o corrige> |

## 5. Enlaces transversales previstos

- `<tema-a>/<sub>/<pagina>.md` → `<tema-b>/<sub>/<otra>.md` — <por qué se relacionan>

## 6. Contradicciones y conflictos con lo ya escrito

| Afirmación en la wiki | Lo que dice la fuente nueva | Propuesta |
|---|---|---|
| <cita y página> | <cita y fuente> | <cuál prevalece y por qué> |

<Si no hay ninguna, dilo explícitamente: "Ninguna detectada".>

## 7. Preguntas abiertas

- <lo que la fuente no resuelve y hace falta decidir>

---

**Nada de esto se escribe hasta que lo apruebes.** Responde con el OK, o con los
cambios que quieras en la tabla de temas o en la lista de páginas.
"""


# Nombre → plantilla, para `okf-wiki template <nombre>`.
TEMPLATES: dict[str, str] = {
    "purpose": PURPOSE_STUB,
    "schema": SCHEMA_STUB,
    "page": PAGE_TEMPLATE,
    "proposal": PROPOSAL_TEMPLATE,
}


def render(name: str, wiki_name: str = "la wiki") -> str:
    """Devuelve la plantilla `name` con el nombre de la instancia sustituido.

    Lanza `KeyError` con los nombres válidos si `name` no existe: el CLI lo
    traduce a un error accionable en vez de a un traceback.
    """
    try:
        template = TEMPLATES[name]
    except KeyError:
        raise KeyError(
            f"No existe la plantilla {name!r}. Disponibles: {', '.join(sorted(TEMPLATES))}."
        ) from None
    return template.replace(_NAME_TOKEN, str(wiki_name))


def is_stub(text: str) -> bool:
    """¿El texto sigue siendo la plantilla sin rellenar?

    Se mira sólo la cabecera del fichero: el marcador va en la primera línea, y
    limitar la búsqueda evita que un `okf-wiki:stub` citado dentro del cuerpo
    (por ejemplo, en un ejemplo de documentación) marque como vacío un fichero
    que sí está relleno.
    """
    head = text.lstrip().split("\n", 1)[0]
    return STUB_MARKER in head
