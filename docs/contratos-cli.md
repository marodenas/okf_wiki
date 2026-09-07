# Contratos del CLI: códigos de salida, límites y avisos

Lo que el CLI garantiza y por qué. La tabla resumida está en el README; aquí, el detalle.

## Códigos de salida

Ningún fallo previsto sale como traceback: cada familia tiene su propio código de salida,
para que la skill pueda distinguirlos sin leer el mensaje.

| Código | Significado | Qué hacer |
|---|---|---|
| `0` | ok | — |
| `1` | el bundle no valida (`verify`, o `--strict` con avisos) | corregir las páginas señaladas |
| `2` | falta el motor OKF (`reference_agent`) | `bash scripts/setup.sh` |
| `3` | ruta de trabajo inservible: la instancia no existe o es un fichero, falta `sources/`, falta `wiki/` (o no tiene páginas), el manifest está corrupto, `context` ha recibido el bundle `wiki/` en vez de la instancia, o la dropzone tiene fuentes que `scan` no puede afirmar contenidas en `sources/` (enlaces que salen, circulares, rotos o a rutas ocultas, y enlaces duros) | comprobar la ruta, crear la instancia con `init`, reparar/borrar `sources/.ingest-state.json`, o copiar el documento a `sources/` en vez de enlazarlo |
| `4` | entrada ilegible: fichero no extraíble (contenido que no corresponde a la extensión, o un Office que se pasa de los [límites de abajo](#límites-de-los-documentos-office)), JSON de stdin que no es la salida de `scan`, o un `--out` que no sirve (un directorio inexistente en `viz`; una carpeta dentro del bundle o con contenido ajeno en `build`/`watch`) | comprobar que el contenido corresponde a la extensión, repasar la tubería `scan \| commit-state`, o elegir otra carpeta de salida |

El código `3` existe para que un error de ruta **no** se lea como «nada nuevo que
ingerir», y para que un manifest corrupto no dispare una reingesta completa que
duplicaría conceptos. Por el mismo motivo `scan` sale con `3` —sin imprimir JSON— cuando
la dropzone contiene algo que no puede afirmar contenido en `sources/`: un enlace que
apunta fuera (o a una ruta oculta, o roto, o circular) y un enlace duro, que es el mismo
agujero sin destino que enseñar. Todo lo que `scan` lee tiene que estar dentro de
`sources/` **y alcanzarse por un camino sin enlaces**; lo que no, se rechaza con un
mensaje en vez de omitirse de un JSON que se leería como una dropzone en orden. Por eso `verify`, `index` y `viz` **fallan** sobre una ruta que no
contiene un bundle en vez de tratarla como un bundle vacío: antes `verify /ruta/mala`
respondía `{"checked": 0, "errors": [], "ok": true}` con salida `0`, así que un typo se
leía como «la wiki está impecable».


#### Límites de los documentos Office

Un docx/pptx/xlsx es un zip de XML, y `office2text` lo carga en memoria para recorrerlo.
Eso deja varias maneras de que un fichero de unos KB se lleve la memoria de la máquina, y
cada una tiene su tope; pasarse es una entrada inservible (**exit 4**), no un fallo de la
instancia:

| Límite | Valor | Qué corta |
|---|---|---|
| Métodos de compresión admitidos | `STORED`, `DEFLATE` | el miembro en BZIP2 o LZMA. Es lo único que escriben Word, Excel, PowerPoint y LibreOffice, y los demás métodos no tienen el techo de expansión de DEFLATE: en BZIP2, el límite de la fila siguiente cabe en unos **cientos de bytes** de zip, por debajo de todos los límites de tamaño |
| Bytes sin comprimir por miembro del zip | 128 MB | el `word/document.xml` que declara gigabytes (*zip bomb*) |
| Bytes sin comprimir por documento | 256 MB | el pptx de mil slides o el xlsx de mil hojas, que miembro a miembro no se pasan pero sumando sí |
| Factor de expansión de un miembro | ×200 | la amplificación que sólo tiene sentido como ataque (el XML de OOXML comprime ×3–×10). Sólo se mira a partir de 4 KB comprimidos: por debajo el ratio es ruido, y con DEFLATE —que no pasa de ×1032— esos 4 KB no dan ni para 4,2 MB |
| Columnas de una hoja `xlsx` | 16.384 (`XFD`) | la referencia de celda que se sale de la hoja (`r="AAAA…1"`), que obligaría a reservar todos los huecos hasta esa columna |
| Celdas materializadas por `xlsx` | 4.000.000 | miles de filas con una celda en la última columna: cada fila se rellena hasta la suya, y el producto crece sin que el tamaño del XML lo delate |

Los cuatro primeros se comprueban **en la cabecera del zip**, así que el miembro
desproporcionado se rechaza sin descomprimirlo (y la lectura real va acotada igual, porque
la cabecera la escribe quien genera el fichero). Cuando el que corta es el presupuesto del
documento, el mensaje lo dice —cuánto queda y de cuánto— en vez de anunciar como «máximo»
un número que no es ninguna de las dos constantes. Los dos de la hoja de cálculo se
comprueban **mientras se recorre**, antes de reservar cada fila: el coste que vigilan no
está en el XML que se lee, sino en las celdas **vacías** que hay que materializar para
dejar cada valor en su columna — un xlsx de 263 bytes con una sola celda en `ZZZZZ1`
producía 37 MB de texto y 321 MB de memoria, y con la referencia un poco más larga no
terminaba nunca.

Son límites de cordura, y los valores viven en `helpers.MAX_OFFICE_*` y
`helpers.MAX_XLSX_*` por si una instancia necesita otros. El único que un documento
real roza es el de bytes por miembro, y por eso está en 128 MB y no en los 64 MB de
antes: un xlsx legítimo con un `sheet1.xml` de unos 105 MB y un ratio de compresión
corriente —una hoja de cálculo de unos cientos de miles de filas pasa de los 100 MB
de XML sin nada raro dentro— salía por **exit 4** como si fuese una bomba, mandando
a trocear a mano un xlsx que el código sabe leer. Subirlo no ensancha nada: en el
tramo nuevo (64–128 MB) el que ata es el factor de expansión —una bomba que declara
100 MB desde unos KB multiplica por miles, no por ×200— y el presupuesto del
documento entero sigue en 256 MB.

Con **exit 4** salen también los dos zips que `zipfile` no rechaza sino que **avisa**, y
que por tanto se colaban: dos entradas del directorio central sobre la misma cabecera local
(«possible zip bomb») y el campo extra Unicode (`0x7075`) con el nombre vacío. Un aviso no
sirve aquí por dos motivos. El texto lo redacta el fichero —el del solape interpola el
nombre del miembro, y ese campo admite 65.535 bytes—, así que un `.pptx` de 120 KB escribía
60 KB por stderr y el comando salía con `0` como si el documento estuviese bien. Y qué
ocurre con el aviso no lo decide este código: con `PYTHONWARNINGS=error` o `-W error`
—habitual en CI— escapaba como `UserWarning`, con traceback y exit `1`, que para la skill
no es «documento que no sirve» sino «el CLI se ha caído». Ahora los dos son
`OfficeStructureError`, con el nombre del miembro y el texto del aviso citados acotados: el
mismo contrato que el resto de los rechazos, y el mismo independientemente de cómo esté
configurado Python.

`commit-state` valida el JSON de stdin **completo antes de escribir**: exige las listas de
`scan` con el `path` y el `sha256` de cada fuente, así que un payload equivocado —la salida
de `now --json`, por ejemplo— sale con `4` sin tocar el manifest, en vez de anunciar
«manifest actualizado» sin haber marcado nada. Y comprueba la ruta con el mismo criterio
que `scan`: sobre una instancia que no existe, un fichero, o una carpeta sin `sources/`
sale con `3` **sin crear nada**. Antes fabricaba `<ruta>/sources/.ingest-state.json` con
un `mkdir -p`, así que un typo respondía «manifest actualizado» y dejaba la ruta inventada
lo bastante formada como para que el `scan` siguiente también pasase —devolviendo las
fuentes reales como `deleted`— mientras el manifest de verdad seguía sin marcarse.

`verify` devuelve JSON con `errors` (rompen el formato → exit 1) y `warnings` (desvíos de
v0.2: frontmatter v0.1, footnotes sin `sources[].id`, enlaces que el visor ignora…). Los
avisos no fallan por defecto para que un bundle v0.1 siga siendo válido; `--strict` los
convierte en fallo.

Los `warnings` incluyen también los desvíos de la **taxonomía temática**, con la ruta del
fichero y la evidencia:

| Aviso | Qué señala |
|---|---|
| `type` que replica el subtema | `ventas/retencion/plan.md` con `type: Retencion`: el `type` lo fija el tema de primer nivel (`type: Ventas`), y un `type` por subtema fragmenta la leyenda del grafo |
| carpeta o `type` de entidad/concepto | `conceptos/`, `entidades/`, `personas/`, `definiciones/`, `type: Concepto`, `type: Entidad`: la wiki se organiza por materia, y esas relaciones son enlaces |
| profundidad mayor que tema/subtema | un tercer nivel de carpeta: el "tema" eran dos temas |

Van por el canal de avisos **a propósito**: OKF es deliberadamente permisivo con la
estructura del bundle, así que ninguno de los tres invalida un bundle ajeno con otra
taxonomía. Es `--strict` —la comprobación final de la ingesta— quien los convierte en fallo.
Son las mismas reglas que la plantilla de `schema.md` deja escritas en cada instancia.
