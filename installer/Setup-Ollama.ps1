param(
  [string] $Model = "",
  [string] $InstallWebView2 = "1",
  [string] $InstallNode = "1",
  [string] $InstallCodex = "1",
  [string] $InstallOpenCode = "1",
  [string] $InstallClaudeCode = "1",
  [string] $InstallOllama = "1",
  [string] $InstallQwenProfiles = "1",
  [string] $GgufUrl = "",
  [string] $GgufFileName = "",
  [string] $GgufSizeBytes = "0",
  [string] $ModelsDirectory = ""
)

$ErrorActionPreference = "Continue"
$logPath = Join-Path $env:LOCALAPPDATA "LOUPe\beta-0.1.3-install.log"
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Log([string] $Message) {
  $line = "$(Get-Date -Format o) $Message"
  Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
  Write-Host $line
}

function Is-Enabled([string] $Value) {
  return $Value -in @("1", "true", "yes", "on")
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
  $needsNode = (Is-Enabled $InstallNode) -or (Is-Enabled $InstallCodex) -or (Is-Enabled $InstallOpenCode)
  $nodeReady = $false
  if ($needsNode) { $nodeReady = Install-NodeIfNeeded }

  if (Is-Enabled $InstallCodex) {
    if ($nodeReady) { [void](Install-NpmCli "@openai/codex@0.147.0" "codex" "Codex") }
    else { Log "Codex demandé mais Node.js/npm ne sont pas disponibles." }
  } else { Log "Codex non sélectionné : installation ignorée." }

  if (Is-Enabled $InstallOpenCode) {
    if ($nodeReady) { [void](Install-NpmCli "opencode-ai@1.18.15" "opencode" "OpenCode") }
    else { Log "OpenCode demandé mais Node.js/npm ne sont pas disponibles." }
  } else { Log "OpenCode non sélectionné : installation ignorée." }

  if (Is-Enabled $InstallClaudeCode) { [void](Install-ClaudeCode) }
  else { Log "Claude Code non sélectionné : installation ignorée." }
}

function Convert-ToInt64([string] $Value) {
  $parsed = 0L
  if ([long]::TryParse($Value, [ref]$parsed)) { return $parsed }
  return 0L
}

function Get-ModelsDirectory {
  $directory = $ModelsDirectory.Trim()
  if ([string]::IsNullOrWhiteSpace($directory)) {
    $directory = Join-Path $env:LOCALAPPDATA "LOUPe\beta-0.1.3\models"
  }
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
  return (Resolve-Path -LiteralPath $directory).Path
}

function Test-ModelDiskSpace([string] $Directory, [long] $RequiredBytes) {
  if ($RequiredBytes -le 0) { return $true }
  try {
    $root = [IO.Path]::GetPathRoot($Directory)
    if ([string]::IsNullOrWhiteSpace($root)) { return $true }
    $drive = New-Object System.IO.DriveInfo($root)
    $reserve = 256MB
    if ($drive.AvailableFreeSpace -lt ($RequiredBytes + $reserve)) {
      Log "Espace disque insuffisant pour le modèle : $([math]::Round($RequiredBytes / 1GB, 2)) Go requis, $([math]::Round($drive.AvailableFreeSpace / 1GB, 2)) Go disponibles."
      return $false
    }
  } catch {
    Log "Impossible de vérifier l'espace disque : $($_.Exception.Message). Le téléchargement continue."
  }
  return $true
}

function Download-GgufModel {
  $url = $GgufUrl.Trim()
  if ([string]::IsNullOrWhiteSpace($url)) {
    Log "Aucun modèle GGUF sélectionné : téléchargement llama.cpp ignoré."
    return $true
  }
  if ($url -notmatch '^https://') {
    Log "URL GGUF refusée : seules les URL HTTPS sont acceptées."
    return $false
  }

  $name = $GgufFileName.Trim()
  if ($name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.gguf$') {
    Log "Nom GGUF refusé : $name."
    return $false
  }
  $directory = Get-ModelsDirectory
  $target = Join-Path $directory $name
  $partial = "$target.partial"
  $expected = Convert-ToInt64 $GgufSizeBytes

  if ((Test-Path -LiteralPath $target) -and (($expected -le 0) -or ((Get-Item -LiteralPath $target).Length -eq $expected))) {
    Log "Modèle GGUF déjà présent : $target."
    return $true
  }
  if (-not (Test-ModelDiskSpace $directory $expected)) { return $false }

  Log "Téléchargement GGUF vers $target. Les poids restent sur le disque et peuvent dépasser plusieurs Go."
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromHours(12)
    $response = $client.GetAsync($url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
      Log "Téléchargement GGUF refusé par le serveur : HTTP $([int]$response.StatusCode)."
      return $false
    }
    $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    $fileStream = [IO.File]::Open($partial, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $buffer = New-Object byte[] (1024 * 1024)
    $total = 0L
    $lastLog = 0L
    try {
      while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $fileStream.Write($buffer, 0, $read)
        $total += $read
        if (($total - $lastLog) -ge 256MB) {
          Log "GGUF téléchargé : $([math]::Round($total / 1GB, 2)) Go."
          $lastLog = $total
        }
      }
    } finally {
      $fileStream.Dispose()
      $stream.Dispose()
      $response.Dispose()
      $client.Dispose()
    }
    if (($expected -gt 0) -and ($total -ne $expected)) {
      Log "Taille GGUF inattendue : $total octets reçus, $expected attendus."
      return $false
    }
    Move-Item -LiteralPath $partial -Destination $target -Force
    Log "Modèle GGUF prêt : $target ($([math]::Round($total / 1GB, 2)) Go)."
    return $true
  } catch {
    Log "Échec du téléchargement GGUF : $($_.Exception.Message)"
    return $false
  } finally {
    if (Test-Path -LiteralPath $partial) {
      Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    }
  }
}

function Start-OllamaApi([string] $OllamaPath) {
  try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
    return $true
  } catch {
    Log "Démarrage du serveur Ollama."
    Start-Process -FilePath $OllamaPath -ArgumentList "serve" -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
      Start-Sleep -Seconds 1
      try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
        return $true
      } catch { }
    }
  }
  return $false
}

function Ollama-HasModel([string] $ModelTag) {
  try {
    $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
    return @($tags.models | Where-Object { $_.name -eq $ModelTag -or $_.model -eq $ModelTag }).Count -gt 0
  } catch {
    Log "Impossible de lire les modèles Ollama : $($_.Exception.Message)"
    return $false
  }
}

function Install-OllamaModel([string] $OllamaPath, [string] $ModelTag) {
  $tag = $ModelTag.Trim()
  if ([string]::IsNullOrWhiteSpace($tag)) { return $true }
  if (Ollama-HasModel $tag) {
    Log "Modèle Ollama déjà présent : $tag."
    return $true
  }
  Log "Téléchargement du modèle Ollama : $tag."
  & $OllamaPath pull $tag *>&1 | ForEach-Object { Log $_.ToString() }
  if ($LASTEXITCODE -ne 0) {
    Log "Le téléchargement de $tag a échoué; LOUPe pourra utiliser un autre profil."
    return $false
  }
  return $true
}

function Create-OllamaProfile([string] $OllamaPath, [string] $Tag, [string] $FileName) {
  $modelfile = Join-Path $scriptRoot $FileName
  if (-not (Test-Path -LiteralPath $modelfile)) {
    Log "Modelfile absent : $FileName. Profil $Tag non créé."
    return $false
  }
  if (Ollama-HasModel $Tag) {
    Log "Profil Ollama déjà présent : $Tag."
    return $true
  }
  Log "Création du profil Ollama $Tag à partir de $FileName."
  & $OllamaPath create $Tag -f $modelfile *>&1 | ForEach-Object { Log $_.ToString() }
  if ($LASTEXITCODE -ne 0) {
    Log "La création du profil $Tag a échoué."
    return $false
  }
  return $true
}

function Prepare-Ollama {
  if (-not (Is-Enabled $InstallOllama)) {
    Log "Ollama non sélectionné : installation et téléchargement ignorés."
    return
  }

  $ollama = Find-Ollama
  if (-not $ollama) {
    if (Install-With-Winget "Ollama.Ollama" "Ollama") { $ollama = Find-Ollama }
  }
  if (-not $ollama) {
    Log "Ollama absent après l'installation. Les profils locaux ne seront pas disponibles."
    return
  }

  Log "Ollama trouvé : $ollama"
  if (-not (Start-OllamaApi $ollama)) {
    Log "API Ollama indisponible; les profils seront préparés au prochain lancement."
    return
  }

  if (Is-Enabled $InstallQwenProfiles) {
    # One download per base model; the two Flash profiles only change the
    # chat template and therefore reuse these same weights.
    [void](Install-OllamaModel $ollama "qwen3:1.7b")
    [void](Install-OllamaModel $ollama "qwen3:4b")
    [void](Create-OllamaProfile $ollama "qwen3:1.7b-flash" ".qwen3-1.7b-flash.Modelfile")
    [void](Create-OllamaProfile $ollama "qwen3:4b-flash" ".qwen3-4b-flash.Modelfile")
  } else {
    Log "Profils Qwen3 natifs non sélectionnés."
  }

  # Keep the old -Model parameter useful for scripted installations and
  # custom tags, without forcing it in the interactive installer.
  if (-not [string]::IsNullOrWhiteSpace($Model)) {
    [void](Install-OllamaModel $ollama $Model)
  }
}

Log "LOUPe beta 0.1.3 : préparation des composants sélectionnés."

if (Is-Enabled $InstallWebView2) {
  if (-not (Test-WebView2)) {
    if (-not (Install-With-Winget "Microsoft.EdgeWebView2Runtime" "WebView2 Runtime")) {
      Log "WebView2 Runtime non installé automatiquement. La fenêtre navigateur de secours restera disponible."
    }
  } else { Log "WebView2 Runtime déjà présent." }
} else { Log "WebView2 non sélectionné : installation ignorée." }

Prepare-CommandLineTools
Prepare-Ollama
[void](Download-GgufModel)

Log "Préparation des composants terminée. Aucun identifiant Gmail n'a été copié ou modifié."
exit 0
