$ErrorActionPreference = "Stop"

# --onedir instead of --onefile: a onefile exe re-extracts its entire
# ~300MB+ payload (webview, llama-cpp, winrt, torch-free deps) to a fresh
# %TEMP% folder on *every* launch, which is what made startup crawl once
# the winrt/winocr/katex additions pushed the bundle bigger. --onedir
# unpacks once at build time and then just execs directly, so repeat
# launches are near-instant. The tradeoff is dist\3loop\ is a folder
# instead of a single file - 3loop.exe inside it is still what users run.
#
# Build into a staging directory first. Ollama or an already-running 3loop
# process can have loaded a DLL from the previous dist folder; asking
# PyInstaller to delete that folder directly then fails with WinError 5 and
# (before this check existed) the script still printed a false success line.
$target = "dist\3loop"
$staging = "dist\3loop_staging"
$backup = "dist\3loop_previous"

# Icone de l'exe: le chercheur pixel kawaii, genere par
# tools/generate_pixel_researcher.py (donc reproductible, pas un binaire opaque).
# L'ancien art web\assets\pixel_slime.ico reste sur le disque, il n'est
# simplement plus l'icone de l'executable.
$icon = "web\assets\pixel_researcher.ico"

# Tailles que l'ICO doit contenir. 256 sert aux grandes vignettes de
# l'Explorateur, 16/32 a la barre des taches et aux listes: un ICO qui n'a
# qu'une seule taille laisse Windows redimensionner lui-meme et l'icone
# ressort baveuse ou vide selon le contexte.
$iconSizes = @(16, 32, 64, 128, 256)

# PyInstaller accepte un --icon pointant vers un fichier absent sans erreur
# visible: l'exe sort alors avec l'icone Python par defaut. On echoue ici
# plutot que de livrer un build silencieusement faux.
if (-not (Test-Path -LiteralPath $icon)) {
  throw "Icone manquante: $icon. Lancer 'python tools\generate_pixel_researcher.py' pour la generer."
}

# Test-Path ne suffit pas. Un ICO tronque, mono-taille ou dont l'en-tete est
# faux passe sans un mot cote PyInstaller ("Copying icon to EXE" s'affiche
# quand meme) et l'exe sort avec l'icone par defaut: echec silencieux, et
# c'est exactement le symptome "le logo de l'app n'est pas le perso". On
# valide donc le fichier avec Pillow AVANT de depenser 6 minutes de build.
$validateIcon = @'
import sys

try:
    from PIL import Image
except ImportError:
    print("icon_lint: FAIL Pillow indisponible, impossible de valider l'ICO")
    raise SystemExit(2)

path = sys.argv[1]
required = sorted(int(x) for x in sys.argv[2].split(",") if x)

try:
    im = Image.open(path)
except Exception as exc:
    print("icon_lint: FAIL %s illisible par Pillow (%s: %s)" % (path, type(exc).__name__, exc))
    raise SystemExit(3)

if im.format != "ICO":
    print("icon_lint: FAIL %s n'est pas un ICO (format detecte: %s)" % (path, im.format))
    raise SystemExit(3)

sizes = sorted(im.ico.sizes())
print("icon_lint: %s tailles=%s mode=%s" % (path, sizes, im.mode))

widths = set()
for size in sizes:
    # getimage + load force le decodage reel de chaque sous-image: un ICO
    # tronque annonce ses tailles dans le repertoire mais explose ici.
    try:
        frame = im.ico.getimage(size)
        frame.load()
    except Exception as exc:
        print("icon_lint: FAIL sous-image %s illisible (%s: %s)" % (size, type(exc).__name__, exc))
        raise SystemExit(3)
    if frame.size != size:
        print("icon_lint: FAIL sous-image annoncee %s mais decodee %s" % (size, frame.size))
        raise SystemExit(3)
    widths.add(size[0])

missing = [s for s in required if s not in widths]
if missing:
    print("icon_lint: FAIL tailles manquantes %s (presentes: %s)" % (missing, sorted(widths)))
    raise SystemExit(4)

print("icon_lint: OK")
'@

$validateIcon | & python - $icon ($iconSizes -join ",")
if ($LASTEXITCODE -ne 0) {
  throw "Icone invalide: $icon (detail ci-dessus). Regenerer avec 'python tools\generate_pixel_researcher.py'."
}

if (Test-Path -LiteralPath $staging) {
  Remove-Item -LiteralPath $staging -Recurse -Force
}

$buildErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --name 3loop `
  --icon $icon `
  --distpath $staging `
  --workpath "build\3loop_staging" `
  --add-data "web;web" `
  --add-data "skills;skills" `
  --collect-all webview `
  --collect-all llama_cpp `
  --collect-all winrt `
  --hidden-import winocr `
  --hidden-import winrt.windows.media.ocr `
  --hidden-import winrt.windows.media.speechrecognition `
  --collect-all pypdf `
  --hidden-import psutil `
  desktop_app.py

$pyInstallerExitCode = $LASTEXITCODE
$ErrorActionPreference = $buildErrorActionPreference
if ($pyInstallerExitCode -ne 0) {
  throw "PyInstaller a echoue avec le code $pyInstallerExitCode."
}
$stagedDir = Join-Path $staging "3loop"
$stagedExe = Join-Path $stagedDir "3loop.exe"
if (-not (Test-Path -LiteralPath $stagedExe)) {
  throw "PyInstaller n'a pas produit $stagedExe."
}

# L'EXE placé dans le workpath par PyInstaller est seulement une étape de
# construction : il n'a aucun dossier _internal adjacent et échoue donc avec
# une erreur LoadLibrary si on le lance depuis l'Explorateur. Le supprimer
# évite de le confondre avec le livrable complet sous dist\3loop.
$intermediateExe = "build\3loop_staging\3loop\3loop.exe"
if (Test-Path -LiteralPath $intermediateExe) {
  Remove-Item -LiteralPath $intermediateExe -Force
}

if (Test-Path -LiteralPath $backup) {
  Remove-Item -LiteralPath $backup -Recurse -Force
}
if (Test-Path -LiteralPath $target) {
  Move-Item -LiteralPath $target -Destination $backup -Force
}
try {
  Move-Item -LiteralPath $stagedDir -Destination $target -Force
  if (Test-Path -LiteralPath $staging) {
    Remove-Item -LiteralPath $staging -Recurse -Force
  }
} catch {
  if (-not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $backup)) {
    Move-Item -LiteralPath $backup -Destination $target -Force
  }
  throw
}

# A locked old DLL may prevent cleanup, but the new target is already valid;
# leave the backup for a later cleanup instead of hiding a successful build.
try {
  if (Test-Path -LiteralPath $backup) {
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction Stop
  }
} catch {
  Write-Warning "Ancien build conserve dans $backup car certains fichiers sont encore utilises."
}

# Verification de livrable: on relit les ressources Win32 de l'exe livre.
# PyInstaller ecrit l'icone via UpdateResource en toute derniere etape et
# n'echoue pas si ca ne prend pas, donc la seule preuve serieuse est de
# rouvrir le PE et d'y retrouver un RT_GROUP_ICON plus les RT_ICON.
# CopyIcons_FromIco recopie les images de l'ICO octet pour octet, donc on
# compare aussi les payloads: ca prouve que c'est bien CET art embarque et
# pas une icone par defaut ou un reste de build precedent.
$targetExe = Join-Path $target "3loop.exe"
$verifyEmbeddedIcon = @'
import ctypes
import struct
import sys
from ctypes import wintypes

RT_ICON = 3
RT_GROUP_ICON = 14
# LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE: on mappe le PE
# pour ses seules ressources, sans executer ni resoudre ses imports.
LOAD_FLAGS = 0x00000002 | 0x00000020
# ERROR_RESOURCE_{TYPE,NAME,DATA}_NOT_FOUND: absence, pas panne.
RESOURCE_ABSENT = (1812, 1813, 1814)

exe_path = sys.argv[1]
ico_path = sys.argv[2]


def read_ico_payloads(path):
    """Images brutes d'un ICO, telles que PyInstaller les recopie en RT_ICON."""
    with open(path, "rb") as handle:
        blob = handle.read()
    if len(blob) < 6:
        print("icon_check: FAIL %s trop court pour un ICO" % path)
        raise SystemExit(3)
    reserved, kind, count = struct.unpack_from("<hhh", blob, 0)
    if reserved != 0 or kind != 1 or count <= 0:
        print("icon_check: FAIL en-tete ICO invalide dans %s" % path)
        raise SystemExit(3)
    images = []
    for index in range(count):
        entry = struct.unpack_from("<BBBBHHII", blob, 6 + index * 16)
        width, height, nbytes, offset = entry[0], entry[1], entry[6], entry[7]
        if offset + nbytes > len(blob):
            print("icon_check: FAIL %s tronque (entree %d)" % (path, index))
            raise SystemExit(3)
        # 0 encode 256 dans le repertoire ICO.
        images.append(((width or 256, height or 256), blob[offset:offset + nbytes]))
    return images


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.LoadLibraryExW.restype = wintypes.HMODULE
kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
kernel32.FindResourceW.restype = ctypes.c_void_p
kernel32.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
kernel32.SizeofResource.restype = wintypes.DWORD
kernel32.SizeofResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
kernel32.LoadResource.restype = ctypes.c_void_p
kernel32.LoadResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
kernel32.LockResource.restype = ctypes.c_void_p
kernel32.LockResource.argtypes = [ctypes.c_void_p]
ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)
kernel32.EnumResourceNamesW.restype = wintypes.BOOL
kernel32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ENUM_PROC, ctypes.c_void_p]

module = kernel32.LoadLibraryExW(exe_path, None, LOAD_FLAGS)
if not module:
    print("icon_check: FAIL ressources illisibles dans %s (code %d)" % (exe_path, ctypes.get_last_error()))
    raise SystemExit(2)


def enumerate_resources(res_type):
    names = []
    payloads = []

    def callback(handle, _res_type, name_ptr, _param):
        raw = name_ptr or 0
        # Un nom de ressource est soit un ID entier (poids fort nul), soit un
        # pointeur vers une chaine large.
        names.append(raw if raw < 0x10000 else ctypes.wstring_at(name_ptr))
        found = kernel32.FindResourceW(handle, ctypes.c_void_p(raw), ctypes.c_void_p(res_type))
        if found:
            size = kernel32.SizeofResource(handle, found)
            address = kernel32.LockResource(kernel32.LoadResource(handle, found))
            if address and size:
                # On copie pendant l'enumeration: le pointeur de nom n'est
                # valide que dans le callback.
                payloads.append(ctypes.string_at(address, size))
        return True

    ok = kernel32.EnumResourceNamesW(module, ctypes.c_void_p(res_type), ENUM_PROC(callback), None)
    if not ok:
        err = ctypes.get_last_error()
        if err not in RESOURCE_ABSENT:
            print("icon_check: FAIL EnumResourceNames(type=%d) code %d" % (res_type, err))
            raise SystemExit(2)
    return names, payloads


groups, _ = enumerate_resources(RT_GROUP_ICON)
icon_names, icon_payloads = enumerate_resources(RT_ICON)
kernel32.FreeLibrary(module)

print("icon_check: %s RT_GROUP_ICON=%s RT_ICON=%s" % (exe_path, groups, icon_names))
if not groups or not icon_payloads:
    print("icon_check: FAIL aucune icone embarquee dans %s" % exe_path)
    raise SystemExit(3)

embedded = set(icon_payloads)
wanted = read_ico_payloads(ico_path)
missing = [size for size, data in wanted if data not in embedded]
print("icon_check: attendu depuis %s %s / absentes %s" % (ico_path, [s for s, _ in wanted], missing))
if missing:
    print("icon_check: FAIL l'exe ne porte pas l'art de %s" % ico_path)
    raise SystemExit(4)

print("icon_check: OK")
'@

$verifyEmbeddedIcon | & python - $targetExe $icon
if ($LASTEXITCODE -ne 0) {
  throw "L'exe livre $targetExe ne porte pas l'icone attendue ($icon, detail ci-dessus). Ne pas distribuer ce build."
}

Write-Host "EXE cree: dist\3loop\3loop.exe"
Write-Host "Icone embarquee verifiee depuis: $icon"

# Cause numero deux du "le logo n'est toujours pas le perso": l'exe est bon
# mais Windows sert une vignette perimee. Le cache est indexe par chemin, et
# on reconstruit toujours au meme chemin, donc le cas est la regle plutot que
# l'exception. Rien ne le signale a l'ecran, d'ou ce rappel.
Write-Host ""
Write-Host "Icone encore ancienne a l'ecran ? C'est le cache d'icones de Windows, pas le build :"
Write-Host '  1. ie4uinit.exe -show'
Write-Host '  2. si ca persiste, purger le cache puis relancer l''Explorateur :'
Write-Host '       taskkill /f /im explorer.exe'
Write-Host '       del /a /q "%LocalAppData%\IconCache.db"'
Write-Host '       del /a /q "%LocalAppData%\Microsoft\Windows\Explorer\iconcache_*.db"'
Write-Host '       start explorer.exe'
Write-Host '  3. un raccourci .lnk et une epingle barre des taches / menu Demarrer gardent'
Write-Host '     leur propre copie de l''icone : les supprimer et les recreer depuis'
Write-Host '     dist\3loop\3loop.exe, la purge du cache ne les met pas a jour.'
