#!/usr/bin/env bash
set -euo pipefail

# Uso:
#   ./create_venv.sh
# oppure:
#   ./create_venv.sh /percorso/del/progetto

PROJECT_DIR="${1:-.}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
REQ_FILE="$PROJECT_DIR/requirements.txt"

if [[ ! -f "$REQ_FILE" ]]; then
  echo "Errore: requirements.txt non trovato in:"
  echo "  $REQ_FILE"
  exit 1
fi

echo "Progetto: $PROJECT_DIR"

if [[ -d "$VENV_DIR" ]]; then
  echo "Attenzione: esiste già $VENV_DIR"
  echo "Lo rinomino in ${VENV_DIR}.backup.$(date +%Y%m%d_%H%M%S)"
  mv "$VENV_DIR" "${VENV_DIR}.backup.$(date +%Y%m%d_%H%M%S)"
fi

echo "Creo il virtualenv..."
python3 -m venv "$VENV_DIR"

echo "Aggiorno pip/setuptools/wheel..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

echo "Installo i pacchetti da requirements.txt..."
"$VENV_DIR/bin/python" -m pip install -r "$REQ_FILE"

echo
echo "Fatto."
echo "Python del venv:"
"$VENV_DIR/bin/python" -c "import sys; print(sys.executable)"

echo
echo "Pacchetti installati:"
"$VENV_DIR/bin/python" -m pip list

echo
echo "Test rapido:"
echo "  $VENV_DIR/bin/python Screenshot_grabber.py -h"