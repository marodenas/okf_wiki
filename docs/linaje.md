# De dónde viene okf_wiki

`okf_wiki` es la **mezcla** de tres ideas, tomando lo mejor de cada una:

1. **[El "LLM Wiki" de Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)**
   — el gist que popularizó la idea: en vez de re-derivar respuestas de los documentos en
   cada consulta (RAG), un LLM **compila** las fuentes una vez en una wiki markdown
   persistente e interconectada, y luego consulta la wiki. `raw/` + `wiki/` + `index.md`.

2. **[LLM Wiki (implementación con MCP)](https://github.com/lucasastorian/llmwiki)** — llevó
   el patrón a una app: servidor + base de datos + MCP donde subes documentos y Claude
   escribe la wiki. De aquí viene la **ergonomía "soltar documentos → sintetizar"** y la
   idea de una *receta* en prosa (el prompt-guía) que enseña al LLM a redactar buenas
   páginas. (Dato clave: la app **no** sintetiza sola; la síntesis siempre la hace un
   agente externo como Claude.)

3. **[Open Knowledge Format (OKF), de Google](https://github.com/GoogleCloudPlatform/knowledge-catalog)**
   ([blog](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing))
   — formalizó el patrón en un **formato abierto**: un bundle es una carpeta de `.md` con
   frontmatter YAML, portable, versionable y consumible por cualquier herramienta. Trae un
   **visor** (`viz.html`) que dibuja el grafo de conceptos. Su **v0.2** añade lo que hace
   falta cuando el corpus lo escribe un agente: procedencia, confianza y caducidad como
   campos del frontmatter.

**`okf_wiki` = la ergonomía de (2) + el formato y el visor de (3), con Claude como motor
(1), y sin servidor/MCP/BD.** Tú sueltas ficheros; Claude los lee (PDFs e imágenes de
forma nativa), sintetiza páginas OKF por tema con enlaces y citas, y regenera el grafo.
Todo son ficheros: lo subes a un repo git y cualquiera lo consume sin instalar nada.
