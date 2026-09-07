# El sitio de lectura: `build` y `watch`

La construcción es **explícita y bajo demanda**. Son tres comandos, todos sin LLM y todos
regenerables desde el markdown:

```bash
okf-wiki index <instancia>    # los index.md de cada nivel, dentro del bundle
okf-wiki viz   <instancia>    # viz.html, el grafo interactivo, dentro del bundle
okf-wiki build <instancia>    # site/ — el sitio de lectura, FUERA del bundle
okf-wiki watch <instancia>    # lo anterior, cada vez que cambie el markdown
```

`index` y `viz` los lanza la skill al final de cada ingesta (pasos 7 y 9), así que en el
flujo normal no hay que acordarse de nada. `build` es el que convierte el bundle en algo
que se lee cómodamente.

**Qué produce `okf-wiki build`** en `<instancia>/site/` (una sola pasada, sin servidor):

| Pieza | Qué es |
|---|---|
| **Portada** (`index.html`) | Nombre de la wiki, el propósito de `purpose.md`, contadores, y los listados accionables: borradores, obsoletas, con caducidad, sin `sources` y aisladas en el grafo |
| **Árbol tema/subtema** | La navegación por carpetas: cada tema con el `type` que comparten sus páginas —o el nombre de la carpeta si no todas declaran el mismo, incluido el caso de una página sin `type`— sus subtemas y sus páginas con descripción |
| **Lector** | Una página HTML por página del bundle: markdown renderizado (tablas, listas, código, Mermaid, footnotes) con el título y la descripción del frontmatter |
| **Procedencia y estado** | Panel lateral con `generated`, `verified`, `status`, `tags`, el fichero de origen y cada entrada de `sources` enlazada al documento real de `sources/` |
| **Enlaces cruzados** | Los `.md` relativos se reescriben a `.html`, **cada página lista quién la enlaza** (backlinks), y los enlaces rotos se marcan en rojo en vez de desaparecer |
| **Búsqueda** | Buscador en la cabecera de todas las páginas (tecla `/`), sobre título, descripción, tags y texto |
| **Datos de grafo** | `data/graph.json` (nodos y aristas, con los mismos `id` que `viz.html`) y `data/search.json`, para consumirlos desde fuera |

Cuatro reglas que definen el comando:

1. **La salida vive fuera del bundle.** El sitio es un artefacto derivado: dentro de
   `wiki/` lo recorrería el motor OKF en la siguiente pasada y los índices y el grafo
   acabarían describiendo su propio HTML. `build` **se niega** a escribir dentro del
   bundle, aunque se lo pidas con `--out`. Por defecto escribe en `<instancia>/site/`,
   hermana de `wiki/`. Y como la entrada es la **instancia** —de ahí salen el propósito de
   la portada y los enlaces a `sources/`, que viven fuera del bundle—, pasarle `wiki/` sale
   con **exit 3** y te dice que lo repitas un nivel más arriba, en vez de construir un
   sitio sin propósito y con las fuentes apuntando a rutas que no existen. Un bundle
   **plano** (una carpeta de páginas sin `wiki/` dentro) sigue valiendo: en ese caso hay
   que darle un `--out` fuera de esa carpeta.
2. **La salida es reproducible.** Mismo bundle → mismos bytes: no hay fecha de
   construcción ni rutas absolutas en el HTML. Dos `build` seguidos no producen ningún
   cambio en git, así que el sitio se puede versionar sin ruido (o ignorar, si prefieres).
   Por eso la caducidad de `stale_after` se evalúa **al leer**, en el navegador, y no al
   construir: si se calculase aquí, una página caducaría sin que nadie reconstruyese nada.
3. **Reconstruir limpia lo que sobra, y sólo eso.** `build` anota lo que escribió en
   `<out>/.okf-site.json` y en la pasada siguiente borra de ahí las páginas que ya no
   existen. Una carpeta de salida con contenido ajeno se **rechaza** en vez de vaciarse
   (`--force` la adopta). Ese manifest es un fichero como otro cualquiera, así que sus
   entradas se validan antes de borrar: nada que se salga de `--out` —rutas absolutas, `..`
   en cualquier tramo, enlaces simbólicos— se sigue ni se borra, y `build` avisa de lo que
   ha rechazado para que puedas mirar el fichero.
4. **No necesita el motor OKF.** `build` y `watch` leen el markdown con `pyyaml` y lo
   renderizan con un renderizador propio. Un problema instalando el motor te puede dejar
   sin `viz.html`, pero no sin poder leer tu propia wiki.

**`okf-wiki watch`** hace lo mismo en bucle: construye una vez y reconstruye cada vez que
cambia el markdown del bundle (sondea por hash cada `--interval` segundos, 2 por defecto).
Su límite es el contrato del proyecto, no una limitación técnica: **no sintetiza, no borra
páginas y no commitea**. Si lo que cambia es la dropzone `sources/`, lo dice y para ahí:

```
sources/ ha cambiado (nuevo: informe-q3.pdf). Ejecuta `/okf-ingest` para
sintetizarlo: watch no ingiere, porque la propuesta la tiene que aprobar una persona.
```

No hay servidor de desarrollo y no hace falta: tanto `site/index.html` como `viz.html` se
abren con doble clic desde el sistema de ficheros, sin `http://` y sin instalar nada. El
lector se lee entero sin JavaScript; el JS sólo añade la búsqueda, el filtro del árbol y el
distintivo de caducidad.
