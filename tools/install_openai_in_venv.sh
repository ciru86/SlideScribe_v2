#!/usr/bin/env bash
set -euo pipefail

# Uso:
#   ./tools/install_openai_in_venv.sh
# oppure:
#   ./tools/install_openai_in_venv.sh /percorso/progetto

PROJECT_DIR="${1:-.}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Errore: virtualenv non trovata oppure python non eseguibile:"
  echo "  $VENV_PYTHON"
  exit 1
fi

echo "Progetto: $PROJECT_DIR"
echo "Python venv: $VENV_PYTHON"
echo "Installo/aggiorno il pacchetto openai..."
"$VENV_PYTHON" -m pip install --upgrade "openai>=1.0.0"

echo
echo "Versione installata:"
"$VENV_PYTHON" - <<'PY'
import openai
print(openai.__version__)
PY
