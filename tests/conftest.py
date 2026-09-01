"""Fixtures compartidas: una instancia OKF v0.2 pequeña pero representativa.

La usan las pruebas del sitio estático (`build`/`watch`). Tiene a propósito un
ejemplar de cada cosa que el sitio tiene que saber tratar: dos temas con
subtema, una página en la raíz del bundle (`log.md`), un enlace cruzado
relativo, uno absoluto (que el visor no sigue), uno roto, una footnote atada a
`sources`, una página en borrador sin fuentes y una con `stale_after` y
`verified`.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

PLAN = """\
---
type: Ventas
title: "Plan de retención 2026"
description: "Cómo se retiene a las cuentas grandes tras el churn del Q2."
tags: [ventas, retencion]
status: stable
generated: { by: claude-code/opus-5, at: 2026-08-06T10:12:33Z }
verified: { by: human:ana, at: 2026-08-06T11:00:00Z }
stale_after: 2027-01-01
sources:
  - id: informe-q2
    resource: sources/informe-trimestral-q2.pdf
    title: "Informe trimestral Q2 2026"
---

El churn cayó al **3%** interanual.[^informe-q2] Ver
[Roadmap 2026](../../producto/roadmap/roadmap-2026.md) y un
[enlace roto](../no-existe.md).

## Palancas

| Palanca | Impacto |
|---|---:|
| Onboarding | Alto |

[^informe-q2]: informe-trimestral-q2.pdf, p.4
"""

ROADMAP = """\
---
type: Producto
title: "Roadmap 2026"
description: "Qué se construye en 2026 y en qué orden."
tags: [producto, roadmap]
status: stable
generated: { by: claude-code/opus-5, at: 2026-08-06T10:20:00Z }
sources:
  - id: acta
    resource: sources/acta-comite.docx
    title: "Acta del comité"
---

Prioridades del año.

- Enlace absoluto al [plan](/ventas/retencion/plan-retencion-2026.md)
"""

FORECAST = """\
---
type: Ventas
title: "Forecast del pipeline"
description: "Previsión de cierre por trimestre."
tags: [ventas, pipeline]
status: draft
generated: { by: claude-code/opus-5, at: 2026-08-06T10:30:00Z }
---

Pendiente de la fuente definitiva.
"""

PURPOSE = """\
# Propósito de la wiki

## Objetivo

Decidir dónde invertir en retención.

<!-- Este comentario no debe aparecer en la portada. -->

## Fuera de alcance

- Nóminas.
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    """Instancia completa: contexto + dropzone + bundle con cuatro páginas."""
    root = tmp_path / "mi_wiki"
    write(root / "purpose.md", PURPOSE)
    write(root / "sources" / "informe-trimestral-q2.pdf", "pdf falso")
    write(root / "wiki" / "log.md", """\
        ---
        type: Log
        title: "Registro de Mi Wiki"
        description: "Bitácora append-only."
        tags: [log, registro]
        status: stable
        generated: { by: okf-wiki/0.2.0, at: 2026-08-06T09:00:00Z }
        ---

        # Historial del bundle

        ## 2026-08-06

        - **Inicialización**: instancia creada.
        """)
    write(root / "wiki" / "ventas" / "retencion" / "plan-retencion-2026.md", PLAN)
    write(root / "wiki" / "producto" / "roadmap" / "roadmap-2026.md", ROADMAP)
    write(root / "wiki" / "ventas" / "pipeline" / "forecast.md", FORECAST)
    return root
