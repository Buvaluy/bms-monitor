@rem Builds installer\Output\JKMonitor-Setup-<ver>.exe and JBDMonitor-Setup-<ver>.exe: PyInstaller bundles, then Inno Setup.
@rem Needs: Python 3.13 with requirements.txt and pyinstaller (PYTHON overrides "py -3.13"); Inno Setup 6 (winget install JRSoftware.InnoSetup).
@echo off
setlocal
cd /d "%~dp0"
if not defined PYTHON set "PYTHON=py -3.13"

pushd ..
for /f %%v in ('%PYTHON% -c "from version import __version__; print(__version__)"') do set "VERSION=%%v"
popd
if not defined VERSION (
    echo Cannot read __version__ from ..\version.py
    exit /b 1
)

%PYTHON% -m PyInstaller --noconfirm --clean jk_monitor.spec || exit /b 1
%PYTHON% -m PyInstaller --noconfirm --clean bms_monitor.spec || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 not found; install it with: winget install JRSoftware.InnoSetup
    exit /b 1
)
"%ISCC%" /Q /DAppVersion=%VERSION% jk_monitor.iss || exit /b 1
"%ISCC%" /Q /DAppVersion=%VERSION% bms_monitor.iss || exit /b 1
echo Done: %~dp0Output\JKMonitor-Setup-%VERSION%.exe and JBDMonitor-Setup-%VERSION%.exe
