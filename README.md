# BMS Monitor

Windows tray monitors for lithium battery BMS boards over Bluetooth Low Energy.
There are two separate apps:

| App | BMS | Source | Access |
|---|---|---|---|
| **JK Monitor** | JK (Jikong), JK02_32S protocol, hardware 19.x | `jk_monitor.pyw`, `jk_bms.py` | read-only |
| **JBD Monitor** | JBD (Xiaoxiang) | `bms_monitor.pyw`, `jbd_bms.py` | read, plus charge and output MOSFET switches behind a "Dev mode" switch |

Both apps:
- show the state of charge, voltage and current in the tray icon and its tooltip;
- show all BMS alarms or protections in the window;
- keep a session history of alarms, threshold warnings and lost links (the "History" button);
- show a tray notification when the link stays lost for 5 minutes;
- start hidden in the tray with `--tray`; the installers use it for autostart;
- have a "Scan" dialog to choose a BMS nearby; JBD Monitor lists only devices that answer a JBD read,
  because other devices advertise the same Bluetooth service.

JK Monitor also has a "Details" tab.

## Install

1. Open [Releases](../../releases) and download `JKMonitor-Setup-<version>.exe` or `JBDMonitor-Setup-<version>.exe`.
2. Run it. It installs for the current user only and needs no admin rights.
   The installer can add autostart with Windows and a desktop shortcut.

**Windows Defender warning.** The exe files are not code-signed.
Defender has blocked them as `Behavior:Win32/Persistence.A!ml` on the author's PC.
That is a machine-learning detection, not a known virus signature.
If you do not trust the binaries, run the apps from source (below); the build steps are public in `.github/workflows/release.yml`.

## Run from source

Needs Windows and Python 3.13 with Tcl/Tk.

```
py -3.13 -m pip install --user -r requirements.txt
py -3.13 jk_monitor.pyw
py -3.13 bms_monitor.pyw
```

Each app saves the chosen BMS address to `jk_config.json` or `bms_config.json` next to itself.
`jk_bms.py` and `jbd_bms.py` also run as console readouts.

## Tests

```
py -3.13 -m unittest discover -p "test_*.py"
```

The tests use fake BLE devices, so no BMS is needed.

## Build the installers

See [installer/README.md](installer/README.md).

## Releases and versions

The version lives in one place: `__version__` in `version.py`.
- The window title of each app shows it, for example `JK BMS Monitor 1.0.0`.
- The installer file name and the entry in Windows "Installed apps" show it.
- The release tag is `v<version>`, for example `v1.0.0`.

To publish a release:
1. Change `__version__` in `version.py` and commit it.
2. Tag the commit and push the tag:
   ```
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. The `release` workflow runs the tests, builds both installers on Windows and publishes them as a GitHub Release.
   It fails if the tag does not match `version.py`.

## License

[MIT](LICENSE).

---

## Українською

Два застосунки для Windows, які показують стан BMS літієвої батареї через Bluetooth LE у системному треї: **JK Monitor** для JK (Jikong) і **JBD Monitor** для JBD (Xiaoxiang).
Інсталятори лежать у [Releases](../../releases); вони ставлять програму лише для поточного користувача, без прав адміністратора.
Exe не підписані, тому Windows Defender може їх заблокувати; тоді запускайте з вихідного коду (розділ «Run from source»).
Версія записана в `version.py` і видна в заголовку вікна, у назві інсталятора та в тегу релізу `v<версія>`.
