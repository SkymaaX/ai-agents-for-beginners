#!/usr/bin/env sh
# Lanceur Linux / macOS : installe ce qui manque, diagnostique, puis ouvre le tableau de bord.
#   ./demarrer.sh                       tout faire
#   ./demarrer.sh --installer-seulement préparer sans lancer le tableau de bord
set -e
cd "$(dirname "$0")"

PY=""
for candidate in python3.13 python3.12 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.12 ou plus est requis (https://www.python.org/downloads/)." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  if ! "$PY" -m venv .venv; then
    rm -rf .venv
    echo "Impossible de créer l'environnement Python." >&2
    echo "Debian / Ubuntu : sudo apt install python3-venv python3-pip   (puis relance ce script)" >&2
    echo "Fedora : sudo dnf install python3   —   Arch : sudo pacman -S python" >&2
    exit 1
  fi
fi
. .venv/bin/activate
echo "→ Installation des dépendances…"
pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ Fichier .env créé : ajoute-y tes clés (GEMINI_API_KEY, ANYTHINGLLM_API_KEY)."
fi

echo "→ Diagnostic de ta machine…"
python -m router doctor --write || true

if [ "$1" = "--installer-seulement" ]; then
  echo "Prêt. Lancer le tableau de bord : ./routeur dashboard"
  exit 0
fi
echo "→ Tableau de bord : http://127.0.0.1:8765 (Ctrl+C pour arrêter)"
exec python -m router dashboard
