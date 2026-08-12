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

# Pillow is imported unconditionally by six modules on desktop_app.py's own
# import chain (see pyproject.toml's core `dependencies`) - unlike psutil
# (imported lazily inside try/except in backend.py, with a working fallback
# either way) and webview/pypdf/llama_cpp below, which are all genuinely
# optional. This build previously assumed Pillow was "already installed in
# this environment" the same way the optional ones still are, with nothing
# checking that assumption: the bundle built and started, then crashed at the
# first request with ModuleNotFoundError: No module named 'PIL'. Failing here
# instead turns a missing *required* dependency into a build-time error with
# the exact fix, rather than a runtime crash a user finds first.
if ! python3 -c 'import PIL' >/dev/null 2>&1; then
  printf 'PIL est requis : python3 -m pip install pillow\n' >&2
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
  # Same exclusions as build_exe.ps1, for the same measured reason:
  # PyInstaller's analysis reaches optional imports inside mediapipe's
  # submodules (tensorflow/jax) that never run. Verified by constructing the
  # exact FaceMesh the eye tracker uses and listing loaded modules; only
  # matplotlib among the heavy packages is touched. Keeping the two build
  # scripts in step matters - a Linux bundle that quietly ships 300 MB of
  # torch would undo this on the platform where downloads hurt most.
  --exclude-module torch
  --exclude-module jax
  --exclude-module jaxlib
  --exclude-module tensorflow
  --exclude-module pyarrow
  --exclude-module scipy
  --exclude-module pandas
  --exclude-module numba
  --exclude-module llvmlite
  --exclude-module jedi
  --exclude-module IPython
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
if python3 -c 'import psutil' >/dev/null 2>&1; then
  pyinstaller_args+=(--hidden-import psutil)
fi
# Eye tracking is optional, but when its packages *are* present they must be
# bundled with their data files, not just their code: mediapipe's .tflite
# models and .binarypb graphs are plain data, so a bundle that collects only
# the package would import cleanly and then fail the moment the user starts
# tracking. --collect-data (not --collect-all) avoids re-collecting the
# already-detected code and native extensions.
if python3 -c 'import mediapipe' >/dev/null 2>&1; then
  pyinstaller_args+=(--collect-data mediapipe)
fi
if python3 -c 'import cv2' >/dev/null 2>&1; then
  pyinstaller_args+=(--collect-data cv2)
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
