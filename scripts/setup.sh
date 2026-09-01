#!/usr/bin/env bash
# okf_wiki — instalación. Crea el venv, instala el motor OKF + este paquete,
# y enlaza la skill okf-ingest en ~/.claude/skills para que /okf-ingest funcione
# desde cualquier carpeta.
#
# El motor OKF (GoogleCloudPlatform/knowledge-catalog, Apache-2.0) se fija a una
# revisión concreta: es la que define Open Knowledge Format v0.2 y la que este
# repo asume (frontmatter `generated`/`verified`/`status`/`stale_after`/`sources`).
# `main` se mueve; un SHA no. Para probar otra revisión: OKF_REV=<sha> bash scripts/setup.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Revisión fijada del motor OKF: "okf: migrate format and tooling to Open Knowledge
# Format v0.2" (#227) + la actualización de SPEC.md a v0.2. Es el commit más reciente
# que toca okf/ en esa serie.
OKF_REV="${OKF_REV:-3fcbb9f828c2f23d109c855ee403c3a4c81f3a96}"
OKF_REPO="${OKF_REPO:-https://github.com/GoogleCloudPlatform/knowledge-catalog.git}"
KC_DIR="${KC_DIR:-$REPO_DIR/../knowledge-catalog}"

# 1) Localizar (o clonar) el motor OKF y dejarlo en la revisión fijada.
if [ -n "${OKF_ENGINE:-}" ]; then
  # El usuario aporta su propio clon: se respeta tal cual, sin tocar su HEAD.
  echo "→ OKF_ENGINE fijado a mano: $OKF_ENGINE (no se fija revisión)"
  if [ ! -f "$OKF_ENGINE/pyproject.toml" ]; then
    echo "ERROR: $OKF_ENGINE no parece el subdirectorio 'okf' de knowledge-catalog." >&2
    exit 1
  fi
  if [ -f "$OKF_ENGINE/SPEC.md" ] && ! grep -q '^\*\*Version 0\.2' "$OKF_ENGINE/SPEC.md"; then
    echo "AVISO: $OKF_ENGINE/SPEC.md no declara OKF v0.2; este repo asume v0.2." >&2
  fi
else
  if [ ! -d "$KC_DIR/.git" ]; then
    echo "→ Motor OKF no encontrado en $KC_DIR; clonando knowledge-catalog…"
    git clone --quiet --filter=blob:none "$OKF_REPO" "$KC_DIR" \
      || git clone --quiet "$OKF_REPO" "$KC_DIR"
  fi
  if [ "$(git -C "$KC_DIR" rev-parse HEAD)" != "$OKF_REV" ]; then
    if [ -n "$(git -C "$KC_DIR" status --porcelain)" ]; then
      echo "ERROR: $KC_DIR tiene cambios locales sin guardar y está en otra revisión." >&2
      echo "       Guárdalos o pasa OKF_ENGINE=<ruta>/okf para usarlo tal cual." >&2
      exit 1
    fi
    echo "→ Fijando knowledge-catalog en ${OKF_REV:0:12} (OKF v0.2)…"
    git -C "$KC_DIR" cat-file -e "${OKF_REV}^{commit}" 2>/dev/null \
      || git -C "$KC_DIR" fetch --quiet origin "$OKF_REV" 2>/dev/null \
      || git -C "$KC_DIR" fetch --quiet origin
    git -C "$KC_DIR" checkout --quiet --detach "$OKF_REV"
  fi
  if [ "$(git -C "$KC_DIR" rev-parse HEAD)" != "$OKF_REV" ]; then
    echo "ERROR: no se pudo dejar $KC_DIR en $OKF_REV." >&2
    exit 1
  fi
  OKF_ENGINE="$KC_DIR/okf"
fi
echo "→ Motor OKF: $OKF_ENGINE"

# 2) venv con Python 3.13 (vía uv)
# 3.13 es el intérprete *de este entorno*, no el mínimo soportado: tanto okf-wiki
# como el motor OKF declaran `requires-python = ">=3.11"`. Se fija para que todo el
# mundo reproduzca el mismo entorno; para probar otro: UV_PYTHON=3.11 no basta,
# edita esta línea a conciencia.
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: se necesita 'uv' (https://docs.astral.sh/uv/). Instálalo y reintenta." >&2
  exit 1
fi
# `--allow-existing` para que el script sea re-ejecutable (p.ej. al mover OKF_REV);
# para rehacer el venv desde cero: UV_VENV_CLEAR=1 bash scripts/setup.sh
uv venv --python 3.13 --allow-existing .venv
PY="$REPO_DIR/.venv/bin/python"

# 3) Instalar el motor OKF (Apache-2.0) y nuestro paquete (MIT), con extras de test
echo "→ Instalando motor OKF y okf-wiki…"
uv pip install --python "$PY" -e "$OKF_ENGINE"
uv pip install --python "$PY" -e "$REPO_DIR[dev]"

# 4) Enlazar la skill en ~/.claude/skills/okf-ingest
SKILL_SRC="$REPO_DIR/skill/okf-ingest"
SKILL_DST="$HOME/.claude/skills/okf-ingest"
mkdir -p "$HOME/.claude/skills"
rm -rf "$SKILL_DST"
ln -s "$SKILL_SRC" "$SKILL_DST"
echo "→ Skill enlazada: $SKILL_DST → $SKILL_SRC"

# 5) Smoke test: imports + API de OKF v0.2 (trust tiers, staleness, verified)
echo "→ Comprobando imports y API v0.2…"
if ! "$PY" - <<'PY'
import okf_wiki
from okf_wiki.cli import OKF_VERSION
from reference_agent.viewer import generate_visualization
from reference_agent.bundle.document import is_stale, normalize_verified, trust_tier

assert OKF_VERSION == "0.2", OKF_VERSION
assert trust_tier({"verified": {"by": "human:x", "at": "2026-01-01T00:00:00Z"}}) == "human-reviewed"
assert normalize_verified({"verified": {"by": "process:p"}}) == [{"by": "process:p"}]
assert is_stale({"stale_after": "2000-01-01"}) is True
print("OK — motor OKF v0.2 disponible")
PY
then
  echo "" >&2
  echo "ERROR: el motor instalado desde $OKF_ENGINE no expone la API de OKF v0.2" >&2
  echo "       (trust_tier / normalize_verified / is_stale). Suele ser un clon en una" >&2
  echo "       revisión v0.1. Deja que el script fije la revisión (no pases OKF_ENGINE)" >&2
  echo "       o lleva tu clon a $OKF_REV." >&2
  exit 1
fi

echo ""
echo "✅ Listo. Añade el venv al PATH o usa:"
echo "   $PY -m okf_wiki.cli --help"
echo "   (o el ejecutable) $REPO_DIR/.venv/bin/okf-wiki --help"
echo ""
echo "Tests:           $REPO_DIR/.venv/bin/python -m pytest"
echo "Crea una wiki:   okf-wiki init /ruta/a/mi_wiki --name 'Mi Wiki'"
echo "Rellena:         /ruta/a/mi_wiki/purpose.md  (alcance y fuera de alcance)"
echo "Compruébalo:     okf-wiki context /ruta/a/mi_wiki"
echo "Y desde Claude Code, dentro de esa carpeta:  /okf-ingest"
