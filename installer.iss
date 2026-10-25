; Inno Setup script — compile with Inno Setup 6 (https://jrsoftware.org/isinfo.php) after build.bat
#define AppName "Darkmoon"
#define AppVersion "1.2.1"

[Setup]
AppId={{9D3C1E7B-2F4A-4B8E-A1D6-5C7E9F0B2A4D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=You
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputBaseFilename=Darkmoon-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\Darkmoon.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Darkmoon (in the tray) when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\Darkmoon\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Darkmoon.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\Darkmoon.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\Darkmoon.exe"" --hidden"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\Darkmoon.exe"; Description: "Launch Darkmoon"; Flags: nowait postinstall skipifsilent
