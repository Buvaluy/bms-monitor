@rem Builds installer\Output\JKMonitor-Setup.exe: PyInstaller bundle of jk_monitor.pyw, then the Inno Setup installer.
@rem Needs: py -3.13 with requirements.txt and pyinstaller installed; Inno Setup 6 (winget install JRSoftware.InnoSetup).
@echo off
setlocal
cd /d "%~dp0"

py -3.13 -m PyInstaller --noconfirm --clean jk_monitor.spec || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 not found; install it with: winget install JRSoftware.InnoSetup
    exit /b 1
)
"%ISCC%" /Q jk_monitor.iss || exit /b 1
echo Done: %~dp0Output\JKMonitor-Setup.exe
