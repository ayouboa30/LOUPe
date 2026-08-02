$ErrorActionPreference = "Stop"

# --onedir instead of --onefile: a onefile exe re-extracts its entire
# ~300MB+ payload (webview, llama-cpp, winrt, torch-free deps) to a fresh
# %TEMP% folder on *every* launch, which is what made startup crawl once
# the winrt/winocr/katex additions pushed the bundle bigger. --onedir
# unpacks once at build time and then just execs directly, so repeat
# launches are near-instant. The tradeoff is dist\3loop\ is a folder
# instead of a single file - 3loop.exe inside it is still what users run.
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --name 3loop `
  --add-data "web;web" `
  --add-data "skills;skills" `
  --collect-all webview `
  --collect-all llama_cpp `
  --collect-all winrt `
  --collect-all winocr `
  --hidden-import psutil `
  desktop_app.py

Write-Host "EXE cree: dist\3loop\3loop.exe"
