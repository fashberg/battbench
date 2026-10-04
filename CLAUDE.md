# BattBench – notes for Claude

PC app (PySide6 + pyqtgraph + SQLite) that reads ISDT and SkyRC chargers over USB / Bluetooth, records every reading,
detects sessions and tracks batteries. User docs: `README.md`; protocol details: `ChargerDetails.md`.

## Language
- README, `ChargerDetails.md`, this file, code comments, docstrings, commit messages: **English**.
- UI texts: English in the source, wrapped in `tr()`; German in `battbench/translations/battbench_de.ts`.
- This is a public repository: no local paths, no notes about files that are "not in the repo". Protocol details are
  described as observed behaviour or credited to the projects listed in the README.

## Layout
- `battbench/app.py` – GUI, worker thread (owns tracker + its own DB connection), one reader thread per USB charger.
- `battbench/device.py` – USB drivers (N8/N16/N24, C4, C4 EVO, A4, UC4, A4 Air), uses the `isdttool` library.
- `battbench/device_ble.py` – Bluetooth (asyncio thread): ISDT Air chargers; `device_skyrc.py` – SkyRC MC3000/MC5000.
- `battbench/model.py` – session / phase detection and rating; `battbench/db.py` – storage and DB upgrades.
- `battbench/i18n.py` – `tr()`, `tr_data()`, language choice; `battbench/translations/` – `.ts` / `.qm`, `update.py`.
- `packaging/` – PyInstaller spec, NSIS installer, icon, `build.ps1`; `run.bat` / `run.sh` – start from source.

## Run and test
- `run.bat [--offline] [--db FILE] [--lang en|de]` (creates `.venv` on first run). Source runs use `battbench.db` in
  the project folder; the installed app uses `%LOCALAPPDATA%\BattBench\battbench.db`.
- The real database may be in use by a running app: open it read-only
  (`sqlite3.connect('file:battbench.db?mode=ro', uri=True)`) and test changes / migrations on a copy (`.backup()`).
- GUI without a window: `QT_QPA_PLATFORM=offscreen` (+ `QT_QPA_FONTDIR=C:/Windows/Fonts` for screenshots),
  `MainWindow(db_copy, 'offline')`, clicks via `QtTest`; call `i18n.install(app, 'de')` to test German.
- Static check: `pyflakes`. Hanging app: `py-spy dump --pid <pid>`.

## Translations
- `tr('text')` for UI texts and messages. `lupdate` does **not** see `tr()` inside f-strings: build the text outside,
  e.g. `'<b>' + tr('History') + '</b>'`, and use `tr('… {} …').format(x)` for values. No plural forms.
- Values that are stored or passed on (task, status, grade, phase kind, mode names, SkyRC error texts) are English
  constants marked with `QT_TRANSLATE_NOOP('data', …)` and shown with `tr_data()`. Never store translated text.
- `model.py`, `db.py`, `device*.py` must work without Qt (scripts); `i18n.tr` falls back to English there.
- After changing texts: `.venv\Scripts\python battbench\translations\update.py`, translate the new entries in the `.ts`
  (all entries must be finished), run it again to build the `.qm`. Commit `.ts` and `.qm`.

## Conventions and pitfalls
- **DB upgrades** in `DB.__init__` via `PRAGMA user_version` (currently 4): 1 N8 size "AA/AAA", 2 N8 mode 9/10 →
  13/14, 3 drop sessions < 10 s, 4 German stored values → English. New upgrade = next number, own method.
- **Mode ids**: the N8 reports activation as 9/10; `device.N8_MODES` maps it to 13/14 when reading (9 stays "cycle" for
  SkyRC / A4). Analysis = 11 throughout, the phase follows the sign of the current.
- Sessions shorter than `MIN_SECS` (10 s) are not stored (`DB.save_dirty`).
- **A4 Air over USB** reports no mode: empty / occupied is debounced over 6 readings (`A4Air.read_slot`); readings
  above 4.4 V or flag −1 below 0.5 V count as empty (probe pulses of empty slots).
- **Refilling tables**: switch off `ResizeToContents` while filling (see `fill_session_table`), otherwise quadratic
  run time; block signals while refilling programmatically (`blockSignals`).
- **Only read-only commands** go to the chargers (ISDT: `0xE0`, `0xDE`, `0xE4`, `0xFE 00`; SkyRC: status queries).
  Commands like `57` / `74` / `65` and `FE` other than `FE 00` restart or stop running tasks.
- Settings: `QSettings('battbench', 'battbench')` keys `a4`, `lang`.
- Version: `battbench/__init__.py` (`__version__`), used by the window, `--help` and the installer.

## Release build (Windows)
`powershell -ExecutionPolicy Bypass -File packaging\build.ps1` → `build\BattBench-<version>-setup.exe` (needs NSIS 3).
The installer is per-user (no admin), adds a Start menu entry and an optional desktop shortcut; uninstall asks whether
to delete the database. Silent: `/S`, target folder `/D=…`.
