# JK Monitor: building the exe and the installer

This folder turns `jk_monitor.pyw` into a standalone `JK Monitor.exe`.
That exe needs no Python on the target PC.
It also wraps the exe into `JKMonitor-Setup.exe`, a per-user installer.

## Files

| File | Purpose |
|---|---|
| `jk_monitor.spec` | PyInstaller spec: one folder build, no console window, adds `pystray._win32`, leaves out the `test_*` modules. |
| `jk_monitor.iss` | Inno Setup script: installs to `%LOCALAPPDATA%\Programs\JK Monitor` without admin rights, has an optional autostart task and an uninstaller. |
| `build_installer.bat` | Runs PyInstaller, then Inno Setup. |
| `.gitignore` | Keeps `build/`, `dist/` and `Output/` out of git. |

## 1. One-time setup on the build PC

1. Install Python 3.13 from python.org with Tcl/Tk enabled (the default).
2. Install the app dependencies and PyInstaller:
   ```
   py -3.13 -m pip install --user -r ..\requirements.txt
   py -3.13 -m pip install --user pyinstaller
   ```
3. Install Inno Setup 6:
   ```
   winget install --id JRSoftware.InnoSetup -e --scope user
   ```
   It lands in `%LOCALAPPDATA%\Programs\Inno Setup 6\`, where `build_installer.bat` looks first.

Versions used for the first build: PyInstaller 6.22.3, bleak 3.0.2, pystray 0.19.5, pillow 12.2.0, Inno Setup 6.

## 2. Build

Run `installer\build_installer.bat`.
It can be started from any folder, because it changes into its own folder first.

The bat runs two steps:
1. `py -3.13 -m PyInstaller --noconfirm --clean jk_monitor.spec` writes `dist\JK Monitor\JK Monitor.exe` and its `_internal\` folder.
2. `ISCC.exe /Q jk_monitor.iss` packs `dist\JK Monitor\` into `Output\JKMonitor-Setup.exe` (about 23 MB).

The bat stops with exit code 1 if either step fails.

## 3. Check the build

1. Read `build\jk_monitor\warn-jk_monitor.txt`.
   Missing `objc`, `Foundation`, `AppKit`, `gi` and `Xlib` are expected: they are the macOS and Linux backends of bleak and pystray.
   Any missing `winrt`, `bleak.backends.winrt`, `PIL` or `tkinter` module is a real problem.
2. Check that `dist\JK Monitor\_internal\` contains `winrt`, `PIL`, `tcl86t.dll` and `_tcl_data`.
3. Start `dist\JK Monitor\JK Monitor.exe`.
   The `JK BMS Monitor` window must open without an error dialog.
4. Test the installer silently and remove it again:
   ```
   Output\JKMonitor-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /TASKS=autostart
   "%LOCALAPPDATA%\Programs\JK Monitor\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES
   ```
   After installing, `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` has a `JK Monitor` value
   that runs the exe with `--tray`, and the Start menu has a `JK Monitor` shortcut.
   The uninstaller removes both, the program folder and the entry in the Windows list of installed programs.

## Installer options

- Language: English or Ukrainian, chosen at start.
- Task "Start JK Monitor with Windows (in the tray)", on by default.
  It adds the `Run` value with `--tray`.
- Task "Create a desktop shortcut", off by default.
- "Launch JK Monitor" on the last page; skipped in silent mode.

The app writes `jk_config.json` next to `jk_monitor.pyw`, which in the built app is `...\JK Monitor\_internal\` (unverified).
A reinstall keeps it.
The uninstaller does not delete it, because the installer did not create it.

## Known problem: Windows Defender

On 2026-11-26 Windows Defender blocked the installed exe as `Behavior:Win32/Persistence.A!ml`.
That result is a machine-learning detection, not a signature match.

| How the exe got there | Autostart | Result |
|---|---|---|
| `dist\JK Monitor\` (build output) | none | runs |
| Inno Setup into a temp folder | Run value | blocked |
| Inno Setup into `%LOCALAPPDATA%\Programs` | none | blocked |
| same, then `reg add` to the Run key | Run value | blocked |
| plain `robocopy` of `dist\JK Monitor\`, no installer | none | blocked |

Every new copy of the same exe was blocked, with or without autostart.
Defender then refused even to read the file (`Get-FileHash` failed).
The most likely cause is that Defender flagged this unsigned exe after the first test (unverified).
A PC with Defender cloud protection will probably block it the same way.

Ways around it:
1. Submit `JK Monitor.exe` as a false positive at https://www.microsoft.com/wdsi/filesubmission.
2. Sign the exe and the installer with a code-signing certificate.
3. Ship the `.pyw` sources with a bat installer that installs Python and runs the app through the signed `pythonw.exe`.
