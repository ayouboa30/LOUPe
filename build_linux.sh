#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'Ce script doit être exécuté sous Linux.\n' >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf 'python3 est requis.\n' >&2
  exit 2
fi
if ! python3 -c 'import PyInstaller' >/dev/null 2>&1; then
  printf 'PyInstaller est requis : python3 -m pip install pyinstaller\n' >&2
  exit 2
fi

# Build the same onedir layout as the Windows script, but use Linux's ':'
# data separator and collect only packages installed in this environment.
pyinstaller_args=(
  --noconfirm
  --clean
  --onedir
  --windowed
  --name 3loop
  --distpath "$ROOT/dist"
  --workpath "$ROOT/build/3loop_linux"
  --add-data "$ROOT/web:web"
  --add-data "$ROOT/skills:skills"
  --hidden-import psutil
  "$ROOT/desktop_app.py"
)

if python3 -c 'import webview' >/dev/null 2>&1; then
  pyinstaller_args+=(--collect-all webview)
fi
if python3 -c 'import pypdf' >/dev/null 2>&1; then
  pyinstaller_args+=(--collect-all pypdf)
fi
if python3 -c 'import llama_cpp' >/dev/null 2>&1; then
  pyinstaller_args+=(--collect-all llama_cpp)
fi

python3 -m PyInstaller "${pyinstaller_args[@]}"

BUNDLE="$ROOT/dist/3loop"
if [[ ! -x "$BUNDLE/3loop" ]]; then
  printf 'Build incomplet : %s/3loop est absent.\n' "$BUNDLE" >&2
  exit 1
fi

printf 'Bundle Linux créé : %s\n' "$BUNDLE"
printf 'Lancement : %s/3loop\n' "$BUNDLE"
printf 'Dépendances OCR optionnelles : tesseract-ocr, tesseract-ocr-fra, tesseract-ocr-eng.\n'
