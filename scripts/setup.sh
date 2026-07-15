#!/usr/bin/env bash
# okf_wiki — instalación. Crea el venv, instala el motor OKF + este paquete,
# y enlaza la skill okf-ingest en ~/.claude/skills para que /okf-ingest funcione
# desde cualquier carpeta.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# 1) Localizar (o clonar) el motor OKF: GoogleCloudPlatform/knowledge-catalog (subdir okf)
OKF_ENGINE="${OKF_ENGINE:-$REPO_DIR/../knowledge-catalog/okf}"
if [ ! -f "$OKF_ENGINE/pyproject.toml" ]; then
  echo "→ Motor OKF no encontrado en $OKF_ENGINE; clonando knowledge-catalog…"
  git clone --depth 1 https://github.com/GoogleCloudPlatform/knowledge-catalog.git \
    "$REPO_DIR/../knowledge-catalog"
  OKF_ENGINE="$REPO_DIR/../knowledge-catalog/okf"
fi
echo "→ Motor OKF: $OKF_ENGINE"

# 2) venv con Python 3.13 (vía uv)
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: se necesita 'uv' (https://docs.astral.sh/uv/). Instálalo y reintenta." >&2
  exit 1
fi
uv venv --python 3.13 .venv
PY="$REPO_DIR/.venv/bin/python"

# 3) Instalar el motor OKF (Apache-2.0) y nuestro paquete (MIT)
echo "→ Instalando motor OKF y okf-wiki…"
uv pip install --python "$PY" -e "$OKF_ENGINE"
uv pip install --python "$PY" -e "$REPO_DIR"

# 4) Enlazar la skill en ~/.claude/skills/okf-ingest
SKILL_SRC="$REPO_DIR/skill/okf-ingest"
SKILL_DST="$HOME/.claude/skills/okf-ingest"
mkdir -p "$HOME/.claude/skills"
rm -rf "$SKILL_DST"
ln -s "$SKILL_SRC" "$SKILL_DST"
echo "→ Skill enlazada: $SKILL_DST → $SKILL_SRC"

# 5) Smoke test
echo "→ Comprobando imports…"
"$PY" -c "import okf_wiki, reference_agent; from reference_agent.viewer import generate_visualization; print('OK')"

echo ""
echo "✅ Listo. Añade el venv al PATH o usa:"
echo "   $PY -m okf_wiki.cli --help"
echo "   (o el ejecutable) $REPO_DIR/.venv/bin/okf-wiki --help"
echo ""
echo "Crea una wiki:   okf-wiki init /ruta/a/mi_wiki --name 'Mi Wiki'"
echo "Y desde Claude Code, dentro de esa carpeta:  /okf-ingest"
