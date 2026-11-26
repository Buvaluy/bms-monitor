; Inno Setup script: per-user installer for the PyInstaller build in dist\JBD Monitor\, with an optional autostart task.
; Build with build_installer.bat, which passes /DAppVersion from version.py; the result is Output\JBDMonitor-Setup-<version>.exe.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=x.y.z bms_monitor.iss (build_installer.bat does it)
#endif
#define AppName "JBD Monitor"
#define AppExe "JBD Monitor.exe"

[Setup]
AppId={{FDB3DE11-1FC9-4EA4-9F17-DBDE1440F4B4}
AppName={#AppName}
AppVersion={#AppVersion}
; Per-user install: no admin rights, and the app can write bms_config.json next to itself.
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=JBDMonitor-Setup-{#AppVersion}
VersionInfoVersion={#AppVersion}
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
