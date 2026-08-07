[Setup]
AppId={{B3A1B3E0-0F1C-4D36-9D7C-3A5D2E0B1001}
AppName=LOUPe beta 0.1
AppVersion=0.1.0
AppPublisher=LOUPe
DefaultDirName={localappdata}\LOUPe\beta-0.1
DefaultGroupName=LOUPe beta 0.1
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=Setup_LOUPe_beta_0.1
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=LOUPe beta 0.1
UninstallDisplayIcon={app}\app\3loop.exe
SetupIconFile=..\web\assets\pixel_researcher.ico
VersionInfoDescription=LOUPe beta 0.1
VersionInfoProductName=LOUPe beta 0.1
VersionInfoProductVersion=0.1.0.0

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le bureau"; GroupDescription: "Raccourcis :"; Flags: unchecked

[Files]
Source: "..\dist\3loop\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Setup-Ollama.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\LOUPe beta 0.1"; Filename: "{app}\app\3loop.exe"; WorkingDir: "{app}\app"; Comment: "LOUPe beta 0.1"
Name: "{autodesktop}\LOUPe beta 0.1"; Filename: "{app}\app\3loop.exe"; WorkingDir: "{app}\app"; Tasks: desktopicon; Comment: "LOUPe beta 0.1"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Setup-Ollama.ps1"""; StatusMsg: "Installation de WebView2, Node.js, des CLI et d'Ollama..."; Flags: waituntilterminated
Filename: "{app}\app\3loop.exe"; Description: "Lancer LOUPe beta 0.1"; Flags: nowait postinstall skipifsilent
