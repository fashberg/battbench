# BattBench – notes for Claude

PC app (PySide6 + pyqtgraph + SQLite) that reads ISDT and SkyRC chargers over USB / Bluetooth, records its readings,
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
- `battbench/autofilter.py` – sortable tables with spreadsheet-like column filters (`make_table(..., autofilter=True)`);
  cells are `SortItem`s with `SORT_ROLE` (sort key, `None` = empty, never mix numbers and text in one column) and
  optionally `FILTER_ROLE` (value in the filter list, e.g. the day of a date).
- `battbench/version.py` – version text with git commit, author and links.
- Built-in help (tab *Help*): the README section between `<!-- help … -->` and `<!-- /help -->`; `update.py` copies it
  to `battbench/help/help_en.md`. `help_de.md` is translated by hand – after changing the README user guide, update
  it and run `update.py --help-translated` (otherwise update.py prints a NOTE).
- `packaging/` – PyInstaller spec, NSIS installer, icon, `build.ps1`; `run.bat` / `run.sh` – start from source.

## Run and test
- `run.bat [--offline] [--db FILE] [--lang en|de]` (creates `.venv` on first run). Database: `--db`, else
  `$BATTBENCH_DB`, else `%LOCALAPPDATA%\BattBench\battbench.db` (installed app; source runs too if it exists), else
  `battbench.db` in the project folder. Backups `<db>-YYYYMMDD-HHMMSS.gz`: on exit (`db.close_and_backup`, checkpoints
  the WAL) and every `backup_hours` while running (`Worker._auto_backup`, own thread and connection); `prune_backups`
  keeps the last n plus the newest of the last n days / weeks / months (`BACKUP_DEFAULTS`, settings in the app).
- `DB.compress_samples` (setting `compress`, button in *Settings*): readings → one per minute / mode / current
  direction, `raw = '1m'`; never readings from `DB.resume_window()` on: a restart rebuilds the sessions of *all*
  slots from there, compressed readings would change them (short ones would vanish).
- The real database may be in use by a running app: open it read-only
  (`sqlite3.connect('file:battbench.db?mode=ro', uri=True)`) and test changes / migrations on a copy (`.backup()`).
- GUI without a window: `QT_QPA_PLATFORM=offscreen` (+ `QT_QPA_FONTDIR=C:/Windows/Fonts` for screenshots),
  `MainWindow(db_copy, 'offline')`, clicks via `QtTest`; call `i18n.install(app, 'de')` to test German.
- Unit tests (rating): `.venv\Scripts\python -m unittest discover tests`. Static check: `pyflakes`. Hanging app:
  `py-spy dump --pid <pid>`.

## Translations
- `tr('text')` for UI texts and messages. `lupdate` does **not** see `tr()` inside f-strings: build the text outside,
  e.g. `'<b>' + tr('History') + '</b>'`, and use `tr('… {} …').format(x)` for values. No plural forms.
- Values that are stored or passed on (task, status, grade, phase kind, mode names, SkyRC error texts) are English
  constants marked with `QT_TRANSLATE_NOOP('data', …)` and shown with `tr_data()`. Never store translated text.
- `model.py`, `db.py`, `device*.py` must work without Qt (scripts); `i18n.tr` falls back to English there.
- After changing texts: `.venv\Scripts\python battbench\translations\update.py`, translate the new entries in the `.ts`
  (all entries must be finished), run it again to build the `.qm`. Commit `.ts` and `.qm`.

## Conventions and pitfalls
- **Stored readings**: chargers are polled once a second, but `db.SampleThinner` stores at most one reading per slot
  every `SAMPLE_SECS` (10 s); a change of mode, current direction, chemistry, size or empty / occupied is stored at
  once together with the reading before it and the last one under load (resume rebuilds the same sessions and
  phases). Pauses of pulsed charging (A4 Air, 0 mA) are no change and are skipped. The worker flushes the
  held-back readings on exit. `DB.samples` thins older 1 s data the same way for the chart (`thin_rows`).
- **DB upgrades** in `DB.__init__` via `PRAGMA user_version` (currently 12): 1 N8 size "AA/AAA", 2 N8 mode 9/10 →
  13/14, 3 drop sessions < 10 s, 4 German stored values → English, 5 / 6 soft delete, 8 thin readings
  (`thin_samples`; 7 was an unreleased first try), 10 drop glitch phases of a single reading and re-rate all
  sessions (poor resistance caps at good; 9 unreleased), 11 health index columns (`_health_columns`), 12 `sessions.ohi`
  (the index itself, tables show and sort by it).
  New upgrade = next number, own method.
- **Rating** = NiMH health index (`model.health`, explained in the README section *Rating*): scores for capacity,
  resistance (charger scale = `RES_LIMITS`), voltage under load and charge efficiency, weighted 40/30/20/10, missing
  ones left out; the category A–D is the stored `grade`. The voltage curve of the last discharge is evaluated by
  `Tracker` (`Session.points` → `finish_curve`) and kept in `sessions.v_start / v_mid / early_drop`; `res_est` marks an
  estimated resistance (A4 Air over USB, `device.res_estimated`), which is not rated. `DB.rate_missing` (worker start)
  evaluates finished sessions without a curve from their stored readings.
- **Mode ids**: the N8 reports activation as 9/10; `device.N8_MODES` maps it to 13/14 when reading (9 stays "cycle" for
  SkyRC / A4). Analysis = 11 throughout, the phase follows the sign of the current.
- Sessions shorter than `MIN_SECS` (10 s) are not stored (`DB.save_dirty`).
- **A4 Air over USB** reports no mode: empty / occupied is debounced over 6 readings (`A4Air.read_slot`); readings
  above 4.4 V or flag −1 below 0.5 V count as empty (probe pulses of empty slots).
- **Refilling tables**: switch off `ResizeToContents` while filling (see `fill_session_table`), otherwise quadratic
  run time; block signals while refilling programmatically (`blockSignals`).
- **Only read-only commands** go to the chargers (ISDT: `0xE0`, `0xDE`, `0xE4`, `0xFE 00`; SkyRC: status queries).
  Commands like `57` / `74` / `65` and `FE` other than `FE 00` restart or stop running tasks.
- Settings: `QSettings('battbench', 'battbench')` keys `a4`, `lang`, `show_deleted`, `compress`, `hidden_series`,
  `backup_hours` / `backup_keep` / `backup_daily` / `backup_weekly` / `backup_monthly`.
  Window position / size, splitter positions and column widths the user dragged (`cols_<table>`, see
  `MainWindow.width_tables`) are stored in the database (`settings` table, `save_layout` / `restore_layout`); a window
  not completely on a screen is reset to the default size. Columns are `Interactive` and fitted to the contents after
  each refill (`make_table`) until the user drags one.
- **Shortcuts**: two visible tables with the same `WidgetShortcut` keys (Enter / Del) make them ambiguous for Qt and
  none fires. Side by side tables (tab *Batteries*) use `row_menu(..., keys=False)` and one shortcut for the tab that
  acts on the table with the focus.
- **Version** `battbench/__init__.py` `__version__`: year.month (`2026.10`), bug fixes `2026.10.1`. `version.git_info()`
  adds the commit (from git, or from `battbench/_build.py` written by `build.ps1`). Author: Folke Ashberg,
  www.ashberg.de.
- Tables refill every few seconds (`load_tables`): keep sorting / filters (`fill_session_table` does) and select rows
  by id (`select_by_id`), not by row number.

## Release build (Windows)
`powershell -ExecutionPolicy Bypass -File packaging\build.ps1` → `build\BattBench-<version>-setup.exe` (needs NSIS 3).
`packaging/release.py [--version 2026.10] [--dry-run] [--no-build]`: clean tree required; sets `__version__`, commits
"Release …", tags `v…`, runs build.ps1, asks before pushing. Interactive – the user runs it, not Claude.
The installer is per-user (no admin), adds a Start menu entry and an optional desktop shortcut; uninstall asks whether
to delete the database. Silent: `/S`, target folder `/D=…`.
