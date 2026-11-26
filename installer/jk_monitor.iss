; Inno Setup script: per-user installer for the PyInstaller build in dist\JK Monitor\, with an optional autostart task.
; Build with build_installer.bat; the result is Output\JKMonitor-Setup.exe.

#define AppName "JK Monitor"
#define AppVersion "1.0"
#define AppExe "JK Monitor.exe"

[Setup]
AppId={{6C1F3B2E-7A4D-4E51-9B8C-2D0F5A9E4C71}
AppName={#AppName}
AppVersion={#AppVersion}
; Per-user install: no admin rights, and the app can write jk_config.json next to itself.
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=JKMonitor-Setup
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[CustomMessages]
en.AutostartTask=Start {#AppName} with Windows (in the tray)
uk.AutostartTask=Запускати {#AppName} разом з Windows (у треї)

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExe}"" --tray"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: postinstall nowait skipifsilent
