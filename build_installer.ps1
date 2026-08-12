$ErrorActionPreference = "Stop"

# Build the public Windows installer. This script deliberately packages only
# dist\3loop and the dependency bootstrapper: Gmail OAuth files live under the
# user's profile and are never copied into a release.
$bundle = Join-Path $PSScriptRoot "dist\3loop"
$iss = Join-Path $PSScriptRoot "installer\LOUPe_beta_0.1.3.iss"
$setupScript = Join-Path $PSScriptRoot "installer\Setup-Ollama.ps1"
$release = Join-Path $PSScriptRoot "release"

if (-not (Test-Path -LiteralPath (Join-Path $bundle "3loop.exe"))) {
  throw "Bundle absent: $bundle. Lance d'abord build_exe.ps1."
}
if (-not (Test-Path -LiteralPath $iss)) { throw "Script Inno Setup absent: $iss" }
if (-not (Test-Path -LiteralPath $setupScript)) { throw "Bootstrapper absent: $setupScript" }

$forbiddenNames = @("gmail_token.json", "gmail_client.json", "token.json", ".env", "api_keys.json")
$forbidden = Get-ChildItem -LiteralPath $bundle -Recurse -Force -File -ErrorAction SilentlyContinue |
  Where-Object { $forbiddenNames -contains $_.Name.ToLowerInvariant() }
if ($forbidden) {
  throw "Secret potentiel détecté dans le bundle public: $($forbidden.FullName -join ', ')"
}

$isccCandidates = @(
  (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source,
  (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
  (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
  (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$iscc = $isccCandidates | Select-Object -First 1
if (-not $iscc) {
  throw "ISCC.exe introuvable. Installe Inno Setup 6 avant de construire Setup_LOUPe_beta_0.1.3.exe."
}

New-Item -ItemType Directory -Force -Path $release | Out-Null
& $iscc /Qp $iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup a échoué avec le code $LASTEXITCODE." }

$output = Join-Path $release "Setup_LOUPe_beta_0.1.3.exe"
if (-not (Test-Path -LiteralPath $output)) { throw "Installateur non produit: $output" }
$hash = (Get-FileHash -Algorithm SHA256 $output).Hash
Write-Host "LOUPe beta 0.1.3 installer: $output"
Write-Host "SHA256: $hash"
