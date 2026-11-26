# PyInstaller spec: bundles bms_monitor.pyw (JBD BMS) with Python and its dependencies into dist\JBD Monitor\ (onedir, no console).
# Build with build_installer.bat, which also runs Inno Setup on the result.
import os

ROOT = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(ROOT, "bms_monitor.pyw")],
    pathex=[ROOT],
    hiddenimports=["pystray._win32"],
    excludes=["test_jk_bms", "test_jbd_bms", "test_monitors", "test_alarm_history"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JBD Monitor",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="JBD Monitor")
