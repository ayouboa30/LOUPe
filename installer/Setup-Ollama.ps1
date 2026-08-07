$ErrorActionPreference = "Continue"
$logPath = Join-Path $env:LOCALAPPDATA "LOUPe\beta-0.1-install.log"
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null

function Log([string] $Message) {
  $line = "$(Get-Date -Format o) $Message"
  Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
  Write-Host $line
}

function Find-Ollama {
  $command = Get-Command ollama -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  $candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
    (Join-Path $env:ProgramFiles "Ollama\ollama.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Ollama\ollama.exe")
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
  }
  return $null
}

function Test-WebView2 {
  $keys = @(
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
  )
  foreach ($key in $keys) {
    if (Test-Path -LiteralPath $key) { return $true }
  }
  return $false
}

function Install-With-Winget([string] $Id, [string] $Label) {
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if (-not $winget) { Log "winget absent: impossible d'installer automatiquement $Label."; return $false }
  Log "Installation de $Label via winget ($Id)."
  & $winget.Source install --id $Id --exact --accept-package-agreements --accept-source-agreements --silent *>&1 | ForEach-Object { Log $_.ToString() }
  return ($LASTEXITCODE -eq 0)
}

function Refresh-UserPath {
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $paths = @($machinePath, $userPath) | Where-Object { $_ }
  if ($paths.Count -gt 0) { $env:Path = $paths -join ";" }
}

function Find-CommandPath([string] $Name) {
  $command = Get-Command $Name -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  return $null
}

function Install-NodeIfNeeded {
  Refresh-UserPath
  if ((Find-CommandPath "node") -and (Find-CommandPath "npm")) {
    Log "Node.js et npm déjà disponibles dans le PATH."
    return $true
  }

  if (-not (Install-With-Winget "OpenJS.NodeJS.LTS" "Node.js LTS")) {
    Log "Node.js LTS non installé automatiquement; les CLI npm seront ignorées."
    return $false
  }
  Refresh-UserPath
  if ((Find-CommandPath "node") -and (Find-CommandPath "npm")) {
    Log "Node.js LTS et npm sont prêts."
    return $true
  }
  Log "Node.js LTS a été installé mais node/npm restent introuvables; les CLI npm seront ignorées."
  return $false
}

function Install-NpmCli([string] $Package, [string] $Binary, [string] $Label) {
  Refresh-UserPath
  if (Find-CommandPath $Binary) {
    Log "$Label déjà disponible dans le PATH."
    return $true
  }

  $npm = Find-CommandPath "npm"
  if (-not $npm) {
    Log "npm absent: impossible d'installer $Label ($Package)."
    return $false
  }

  Log "Installation de $Label via npm ($Package)."
  & $npm install --global $Package --no-fund --no-audit *>&1 | ForEach-Object { Log $_.ToString() }
  $exitCode = $LASTEXITCODE
  Refresh-UserPath
  if ($exitCode -eq 0 -and (Find-CommandPath $Binary)) {
    Log "$Label installé et disponible dans le PATH."
    return $true
  }
  Log "Échec de l'installation de $Label; le reste de la préparation continue."
  return $false
}

function Install-ClaudeCode {
  Refresh-UserPath
  if (Find-CommandPath "claude") {
    Log "Claude Code déjà disponible dans le PATH."
    return $true
  }

  $powershell = Find-CommandPath "powershell.exe"
  if (-not $powershell) {
    Log "Windows PowerShell absent: impossible d'installer Claude Code automatiquement."
    return $false
  }

  Log "Installation de Claude Code via l'installeur officiel."
  & $powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex" *>&1 | ForEach-Object { Log $_.ToString() }
  $exitCode = $LASTEXITCODE
  Refresh-UserPath
  if ($exitCode -eq 0 -and (Find-CommandPath "claude")) {
    Log "Claude Code installé et disponible dans le PATH."
    return $true
  }
  Log "Claude Code n'est pas disponible après l'installation; le reste de la préparation continue."
  return $false
}

function Prepare-CommandLineTools {
  if (Install-NodeIfNeeded) {
    [void](Install-NpmCli "@openai/codex@0.147.0" "codex" "Codex")
    [void](Install-NpmCli "opencode-ai@1.18.15" "opencode" "OpenCode")
  }
  [void](Install-ClaudeCode)
}

Log "LOUPe beta 0.1 : préparation des dépendances locales."

if (-not (Test-WebView2)) {
  if (-not (Install-With-Winget "Microsoft.EdgeWebView2Runtime" "WebView2 Runtime")) {
    Log "WebView2 Runtime non installé automatiquement. La fenêtre navigateur de secours restera disponible."
  }
} else {
  Log "WebView2 Runtime déjà présent."
}

Prepare-CommandLineTools

$ollama = Find-Ollama
if (-not $ollama) {
  if (Install-With-Winget "Ollama.Ollama" "Ollama") {
    $ollama = Find-Ollama
  }
}
if (-not $ollama) {
  Log "Ollama absent après l'installation. LOUPe démarrera en mode heuristique si nécessaire."
  exit 0
}

Log "Ollama trouvé : $ollama"
$apiReady = $false
try {
  $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
  $apiReady = $true
} catch {
  Log "Démarrage du serveur Ollama."
  Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden | Out-Null
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
      $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
      $apiReady = $true
      break
    } catch { }
  }
}

if (-not $apiReady) {
  Log "API Ollama indisponible; téléchargement du modèle reporté au prochain lancement."
  exit 0
}

$model = "qwen3:1.7b-flash"
$hasModel = $false
try {
  $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
  $hasModel = @($tags.models | Where-Object { $_.name -eq $model }).Count -gt 0
} catch {
  Log "Impossible de lire les modèles Ollama : $($_.Exception.Message)"
}

if (-not $hasModel) {
  Log "Téléchargement du modèle $model. Cette étape peut prendre quelques minutes."
  & $ollama pull $model *>&1 | ForEach-Object { Log $_.ToString() }
  if ($LASTEXITCODE -ne 0) { Log "Le téléchargement de $model a échoué; LOUPe pourra utiliser le fallback heuristique." }
} else {
  Log "Modèle $model déjà présent."
}

Log "Préparation des dépendances terminée. Aucun identifiant Gmail n'a été copié ou modifié."
exit 0
