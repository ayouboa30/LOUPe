[Setup]
AppId={{B3A1B3E0-0F1C-4D36-9D7C-3A5D2E0B1001}
AppName=LOUPe beta 0.1.2
AppVersion=0.1.2
AppPublisher=LOUPe
AppPublisherURL=https://www.ayoubouladali.com
AppSupportURL=https://github.com/ayouboa30/LOUPe/issues
AppUpdatesURL=https://www.ayoubouladali.com/projects.html#loupe-beta
DefaultDirName={localappdata}\LOUPe\beta-0.1.2
DefaultGroupName=LOUPe beta 0.1.2
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=Setup_LOUPe_beta_0.1.2
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=LOUPe beta 0.1.2
UninstallDisplayIcon={app}\app\3loop.exe
SetupIconFile=..\web\assets\pixel_researcher.ico
VersionInfoDescription=LOUPe beta 0.1.2
VersionInfoProductName=LOUPe beta 0.1.2
VersionInfoProductVersion=0.1.2.0

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le bureau"; GroupDescription: "Raccourcis :"; Flags: unchecked

[Files]
Source: "..\dist\3loop\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Setup-Ollama.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\.qwen3-1.7b-flash.Modelfile"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\.qwen3-4b-flash.Modelfile"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\models"

[Icons]
Name: "{autoprograms}\LOUPe beta 0.1.2"; Filename: "{app}\app\3loop.exe"; WorkingDir: "{app}\app"; Comment: "LOUPe beta 0.1.2"
Name: "{autodesktop}\LOUPe beta 0.1.2"; Filename: "{app}\app\3loop.exe"; WorkingDir: "{app}\app"; Tasks: desktopicon; Comment: "LOUPe beta 0.1.2"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Setup-Ollama.ps1"" -InstallWebView2 ""{code:GetInstallWebView2}"" -InstallNode ""{code:GetInstallNode}"" -InstallCodex ""{code:GetInstallCodex}"" -InstallOpenCode ""{code:GetInstallOpenCode}"" -InstallClaudeCode ""{code:GetInstallClaudeCode}"" -InstallOllama ""{code:GetInstallOllama}"" -InstallQwenProfiles ""{code:GetInstallQwenProfiles}"" -GgufUrl ""{code:GetGgufUrl}"" -GgufFileName ""{code:GetGgufFileName}"" -GgufSizeBytes ""{code:GetGgufSizeBytes}"" -ModelsDirectory ""{app}\models"""; StatusMsg: "Préparation des composants et modèles sélectionnés..."; Flags: waituntilterminated
Filename: "{app}\app\3loop.exe"; Description: "Lancer LOUPe beta 0.1.2"; Flags: nowait postinstall skipifsilent

[Code]
const
  IDX_WEBVIEW2 = 0;
  IDX_NODE = 1;
  IDX_CODEX = 2;
  IDX_OPENCODE = 3;
  IDX_CLAUDE = 4;
  IDX_OLLAMA = 5;
  IDX_QWEN = 6;
  IDX_GGUF = 7;
  GB = 1073741824;
  QWEN_BASE_BYTES = 5000000000;

var
  ComponentsPage: TInputOptionWizardPage;
  BudgetPage: TInputQueryWizardPage;
  GgufPage: TInputOptionWizardPage;
  CustomGgufPage: TInputQueryWizardPage;

function BoolParam(Value: Boolean): String;
begin
  if Value then Result := '1' else Result := '0';
end;

function ParsedGb(const Value: String): Int64;
var
  Parsed: Integer;
begin
  Parsed := StrToIntDef(Trim(Value), -1);
  if Parsed < 0 then Result := -1 else Result := Int64(Parsed) * GB;
end;

function ModelBudgetBytes: Int64;
begin
  Result := ParsedGb(BudgetPage.Values[0]);
end;

function SelectedGgufSizeBytes: Int64;
begin
  Result := 0;
  if not ComponentsPage.Values[IDX_GGUF] then exit;
  case GgufPage.SelectedValueIndex of
    1: Result := 1834426016;
    2: Result := 2497280256;
    3: Result := 5027783488;
    4: Result := 9001752960;
    5: Result := 19762149024;
    6: Result := ParsedGb(CustomGgufPage.Values[2]);
  end;
end;

function RequiredModelBytes: Int64;
begin
  Result := SelectedGgufSizeBytes;
  if ComponentsPage.Values[IDX_QWEN] then Result := Result + QWEN_BASE_BYTES;
end;

function ValidateModelBudget: Boolean;
var
  Budget: Int64;
  Required: Int64;
begin
  Result := False;
  Budget := ModelBudgetBytes;
  if Budget < 0 then begin
    MsgBox('Indique un nombre entier de Go pour la limite de stockage (0 = aucun téléchargement de modèle).', mbError, MB_OK);
    exit;
  end;
  Required := RequiredModelBytes;
  if Required > Budget then begin
    MsgBox('Les modèles choisis nécessitent environ ' + IntToStr((Required + GB - 1) div GB) +
      ' Go, mais la limite est de ' + IntToStr(Budget div GB) + ' Go. Augmente la limite ou choisis un GGUF plus petit.', mbError, MB_OK);
    exit;
  end;
  if (Required > 0) and (Budget = 0) then begin
    MsgBox('Une limite de 0 Go interdit le téléchargement des profils sélectionnés.', mbError, MB_OK);
    exit;
  end;
  Result := True;
end;

function ValidGgufUrl(const Value: String): Boolean;
begin
  Result := (Pos('https://', Lowercase(Trim(Value))) = 1) and
    (Pos('"', Value) = 0) and (Pos(#13, Value) = 0) and (Pos(#10, Value) = 0);
end;

function ValidGgufFileName(const Value: String): Boolean;
begin
  Result := (Trim(Value) <> '') and
    (CompareText(ExtractFileExt(Trim(Value)), '.gguf') = 0) and
    (Pos('\', Value) = 0) and (Pos('/', Value) = 0) and (Pos('"', Value) = 0);
end;

function GetInstallWebView2(Param: String): String;
begin Result := BoolParam(ComponentsPage.Values[IDX_WEBVIEW2]); end;
function GetInstallNode(Param: String): String;
begin Result := BoolParam(ComponentsPage.Values[IDX_NODE]); end;
function GetInstallCodex(Param: String): String;
begin Result := BoolParam(ComponentsPage.Values[IDX_CODEX]); end;
function GetInstallOpenCode(Param: String): String;
begin Result := BoolParam(ComponentsPage.Values[IDX_OPENCODE]); end;
function GetInstallClaudeCode(Param: String): String;
begin Result := BoolParam(ComponentsPage.Values[IDX_CLAUDE]); end;
function GetInstallOllama(Param: String): String;
begin Result := BoolParam(ComponentsPage.Values[IDX_OLLAMA]); end;
function GetInstallQwenProfiles(Param: String): String;
begin Result := BoolParam(ComponentsPage.Values[IDX_QWEN]); end;

function GetGgufUrl(Param: String): String;
begin
  Result := '';
  if not ComponentsPage.Values[IDX_GGUF] then exit;
  case GgufPage.SelectedValueIndex of
    1: Result := 'https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf?download=true';
    2: Result := 'https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf?download=true';
    3: Result := 'https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf?download=true';
    4: Result := 'https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf?download=true';
    5: Result := 'https://huggingface.co/Qwen/Qwen3-32B-GGUF/resolve/main/Qwen3-32B-Q4_K_M.gguf?download=true';
    6: Result := Trim(CustomGgufPage.Values[0]);
  end;
end;

function GetGgufFileName(Param: String): String;
begin
  Result := '';
  if not ComponentsPage.Values[IDX_GGUF] then exit;
  case GgufPage.SelectedValueIndex of
    1: Result := 'Qwen3-1.7B-Q8_0.gguf';
    2: Result := 'Qwen3-4B-Q4_K_M.gguf';
    3: Result := 'Qwen3-8B-Q4_K_M.gguf';
    4: Result := 'Qwen3-14B-Q4_K_M.gguf';
    5: Result := 'Qwen3-32B-Q4_K_M.gguf';
    6: Result := Trim(CustomGgufPage.Values[1]);
  end;
end;

function GetGgufSizeBytes(Param: String): String;
begin Result := IntToStr(SelectedGgufSizeBytes); end;
function GetGgufBudget(Param: String): String;
begin Result := IntToStr(ModelBudgetBytes); end;

procedure InitializeWizard;
begin
  ComponentsPage := CreateInputOptionPage(wpSelectDir,
    'Composants optionnels', 'Choisis ce que LOUPe peut installer',
    'Chaque case peut être refusée. Les modèles sont téléchargés séparément.', False, False);
  ComponentsPage.Add('WebView2 Runtime (fenêtre native)');
  ComponentsPage.Add('Node.js LTS');
  ComponentsPage.Add('Codex CLI (nécessite Node.js)');
  ComponentsPage.Add('OpenCode CLI (nécessite Node.js)');
  ComponentsPage.Add('Claude Code CLI');
  ComponentsPage.Add('Ollama (serveur local)');
  ComponentsPage.Add('Profils Qwen3 natifs : 1.7B + 4B et leurs variantes Flash');
  ComponentsPage.Add('Modèle GGUF via llama.cpp, chargé directement depuis le disque');
  ComponentsPage.Values[IDX_WEBVIEW2] := True;
  ComponentsPage.Values[IDX_NODE] := True;
  ComponentsPage.Values[IDX_CODEX] := True;
  ComponentsPage.Values[IDX_OPENCODE] := True;
  ComponentsPage.Values[IDX_CLAUDE] := True;
  ComponentsPage.Values[IDX_OLLAMA] := True;
  ComponentsPage.Values[IDX_QWEN] := True;
  ComponentsPage.Values[IDX_GGUF] := False;

  BudgetPage := CreateInputQueryPage(ComponentsPage.ID,
    'Limite de stockage', 'Quel espace disque maximal réserver aux modèles ?',
    'La limite couvre les profils Qwen3 et le GGUF choisi. Elle ne remplace pas la RAM nécessaire à leur exécution.');
  BudgetPage.Add('Maximum en Go (0 = aucun téléchargement de modèle) :', False);
  BudgetPage.Values[0] := '6';

  GgufPage := CreateInputOptionPage(BudgetPage.ID,
    'Modèle GGUF llama.cpp', 'Choisis un fichier quantifié chargé depuis le disque',
    'Les tailles proviennent des fichiers GGUF officiels Qwen. Les modèles lourds demandent beaucoup de RAM/VRAM.', True, True);
  GgufPage.Add('Ne pas télécharger de GGUF');
  GgufPage.Add('Qwen3 1.7B Q8 · ~2 Go · CPU 8 Go RAM');
  GgufPage.Add('Qwen3 4B Q4_K_M · ~3 Go · CPU 8 Go RAM recommandé');
  GgufPage.Add('Qwen3 8B Q4_K_M · ~6 Go · 16 Go RAM ou GPU conseillé');
  GgufPage.Add('Qwen3 14B Q4_K_M · ~10 Go · GPU ou 16–24 Go RAM conseillé');
  GgufPage.Add('Qwen3 32B Q4_K_M · ~20 Go · GPU ou 32 Go RAM fortement conseillé');
  GgufPage.Add('URL GGUF personnalisée');
  GgufPage.SelectedValueIndex := 0;

  CustomGgufPage := CreateInputQueryPage(GgufPage.ID,
    'GGUF personnalisé', 'Fournis un téléchargement HTTPS',
    'Le fichier doit être un .gguf. Vérifie sa licence et sa source.');
  CustomGgufPage.Add('URL HTTPS :', False);
  CustomGgufPage.Add('Nom du fichier (.gguf) :', False);
  CustomGgufPage.Add('Taille estimée en Go :', False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = GgufPage.ID then Result := not ComponentsPage.Values[IDX_GGUF];
  if PageID = CustomGgufPage.ID then Result := (not ComponentsPage.Values[IDX_GGUF]) or (GgufPage.SelectedValueIndex <> 6);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ComponentsPage.ID then begin
    if ComponentsPage.Values[IDX_QWEN] and (not ComponentsPage.Values[IDX_OLLAMA]) then begin
      MsgBox('Les profils Qwen3 nécessitent le composant Ollama.', mbError, MB_OK);
      Result := False;
    end;
    exit;
  end;
  if CurPageID = BudgetPage.ID then begin
    Result := ValidateModelBudget;
    exit;
  end;
  if CurPageID = GgufPage.ID then begin
    if GgufPage.SelectedValueIndex <> 6 then Result := ValidateModelBudget;
    exit;
  end;
  if CurPageID = CustomGgufPage.ID then begin
    if not ValidGgufUrl(CustomGgufPage.Values[0]) then begin
      MsgBox('L''URL GGUF doit commencer par https:// et ne pas contenir de guillemets.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    if not ValidGgufFileName(CustomGgufPage.Values[1]) then begin
      MsgBox('Le nom doit être un fichier .gguf simple, sans dossier.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    if ParsedGb(CustomGgufPage.Values[2]) <= 0 then begin
      MsgBox('Indique une taille estimée positive en Go pour le fichier personnalisé.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    Result := ValidateModelBudget;
  end;
end;
