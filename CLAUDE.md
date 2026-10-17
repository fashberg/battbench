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
- `battbench/mc5000.py` – MC5000 programs (charge profiles): `Program`, packets for program / start / stop / history
  (no I/O, no Qt); readers send them with `command(packet)` (`device_skyrc.serve` between two status queries,
  `device_net.NetReader` via the ESP). Profiles are stored in the table `profiles` (JSON).
- `battbench/report.py` – "Export report …": HTML with inline SVG (QSvgGenerator; resolution = screen dpi, else
  pyqtgraph's axis QPictures shrink; two-point lines joined) or PDF (`write_pdf` sets blocks itself: a block -
  heading with its content - that does not fit starts a new page; QTextDocument per block, 96 dpi, charts as images
  with devicePixelRatio; drawn with a fixed light palette, the app's dark one made the text white). *Rating
  details* = the help section "### Rating" / "### Bewertung" (heading = `tr('Rating')`), paragraph by paragraph.
  Built from an unseen `ResultPanel` / `CurvePlot` inside `i18n.language(lang)`; render the scene
  (`scene().render`), not the widget. No `tr()` in f-strings there either.
- `battbench/i18n.py` – `tr()`, `tr_data()`, language choice; `battbench/translations/` – `.ts` / `.qm`, `update.py`.
- `battbench/logs.py` – `battbench.log` next to the database (rotating, `--debug` = every unanswered query). Log texts
  are English and not translated; loggers `battbench.app` / `battbench.usb`.
- `battbench/autofilter.py` – sortable tables with spreadsheet-like column filters (`make_table(..., autofilter=True)`);
  cells are `SortItem`s with `SORT_ROLE` (sort key, `None` = empty, never mix numbers and text in one column) and
  optionally `FILTER_ROLE` (value in the filter list, e.g. the day of a date).
- `battbench/version.py` – version text with git commit, author and links.
- Built-in help (tab *Help*): the README section between `<!-- help … -->` and `<!-- /help -->`; `update.py` copies it
  to `battbench/help/help_en.md`. `help_de.md` is translated by hand – after changing the README user guide, update
  it and run `update.py --help-translated` (otherwise update.py prints a NOTE).
- `packaging/` – PyInstaller spec, NSIS installer, icon, `build.ps1`; `run.bat` / `run.sh` – start from source.

## Run and test
- `run.bat [--offline] [--db FILE] [--lang en|de] [--debug] [--console]` (creates `.venv` on first run; without
  `--debug` / `--console` it starts `pythonw`, no console window). Log: `battbench.log`
  next to the database (`--debug`: every unanswered query, also to stderr). Database: `--db`, else
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
- **Charger's own voltage curve** (MC5000 `98`, current phase only): `Worker` notes gaps over `GAP_FILL_SECS` in a
  running task (`self.gaps`), `_fill_gaps` asks for the curve (`mc5000.parse_history` / `history_points`, last value
  = now) and stores the values inside the gaps in the table `history` (`DB.add_history`) – never as readings, they
  have no current / mAh / mode. `CurvePlot` draws them dashed (`hist`), its curves break at gaps over `GAP_BREAK`.
- **DB upgrades** in `DB.__init__` via `PRAGMA user_version` (currently 15): 1 N8 size "AA/AAA", 2 N8 mode 9/10 →
  13/14, 3 drop sessions < 10 s, 4 German stored values → English, 5 / 6 soft delete, 8 thin readings
  (`thin_samples`; 7 was an unreleased first try), 10 drop glitch phases of a single reading and re-rate all
  sessions (poor resistance caps at good; 9 unreleased), 11 health index columns (`_health_columns`), 12 `sessions.ohi`
  (the index itself, tables show and sort by it), 13 `sessions.stop_reason` (error a charger stopped the task with;
  such sessions end as "aborted", not "removed"), 14 MC5000 idle readings with an error → mode 20 from their raw answer
  (`_skyrc_stops`); "contact lost" counts as removed. 15 `sessions.avg_charge_ma / avg_discharge_ma` (from the readings)
  and `sessions.profile`.
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
- **Battery of a new session** (`DB.save_session` on insert): a battery planned for the slot (`DB.plan_battery`,
  settings `plan_<dev>_<slot>`, chosen in the result panel of a slot without a session) if chosen before the start,
  else the battery and nominal capacity of the slot's session before (`Session.follows`, set by `Tracker`) unless the
  slot was empty for `EMPTY_SECS` in between or the readings have a gap. `save_dirty` stores closed sessions first;
  `resume` repeats the take-over after restoring the stored batteries.
- **Average currents** (`model.CurrentAverage`): time-weighted, each reading counts until the next (at most
  `CURRENT_DT`), |current| below `PHASE_MIN_MA` left out – live and thinned readings give the same value.
- **Program of a session** (`sessions.profile`, JSON `prog` / `pid` / `source`): `Worker._find_profiles` once per MC5000
  session after `PROFILE_SECS` asks the read-only `5F` and takes the program BattBench started on the slot (setting
  `mc5000:<dev>:<slot>`) if status and currents still match it, else what the charger reports (`source` 'charger').
  Shown by `session_profile_text` (result panel, so also the report).
- **Offline**: the worker keeps `last_ok` per connection (reader); none for `STALE_SECS` -> `online[dev]['stale']`
  = time of the last reading, the GUI covers the tiles (`TilePage.set_offline`). A USB reader posts empty batches
  while the charger is silent and calls `Charger.reopen()` every `Reader.REOPEN_SECS`; only an exception while the
  HID path is gone (`device.present`) ends it with 'lost'. Writes go through `device.CheckedHid` (hidapi returns -1).
- **A4 Air over USB** reports no mode: empty / occupied is debounced over 6 readings (`A4Air.read_slot`); readings
  above 4.4 V or flag −1 below 0.5 V count as empty (probe pulses of empty slots).
- **Refilling tables**: switch off `ResizeToContents` while filling (see `fill_session_table`), otherwise quadratic
  run time; block signals while refilling programmatically (`blockSignals`).
- **Only read-only commands** go to the chargers by themselves (ISDT: `0xE0`, `0xDE`, `0xE4`, `0xFE 00`; SkyRC:
  status queries, MC5000 also the handshake `57`, without which it does not answer). Commands like `74` / `65` and
  `FE` other than `FE 00` restart or stop running tasks. Exception: MC5000 profiles (`mc5000.py`, tab *Profiles*,
  right click on a slot) send `FE <mask>` (stop, also before programming a working slot) and `94` (program; it
  starts by itself when a battery is in), one slot per packet, only after the user confirmed a start or stop in
  the dialog; never mask `00` (all slots). Success is checked on the status, not the answer. Over a BattBench-ESP
  they go through `POST /api/command`, which lets only `94` / `FE` / `98` / `5F` / `96` / `95` with a one-slot mask
  through. Read only and sent without asking: `5F` (currents of the slots chosen in the control panel, and once per new
  session for its program), `96` (system
  settings when their dialog opens); `95` only from that dialog's Save. The same dialog pauses / switches off / on
  the charger's Bluetooth (worker command `bt`): `BleManager.hold_charger` (switched off: setting `bt_off`) and
  `POST /api/ble` on every BattBench-ESP that knows the charger's address – both, or the other one takes it.
  `NetHost` asks `/api/status` every `BLE_SECS`; a pause / off set on the ESP's web page is mirrored to
  `BleManager` (`Worker._bt_mirror`) and shown on the tiles (`bt_holds` in the cycle, `_update_offline`, button
  *Resume now*). This PC's Bluetooth counts only when it is used and its search works (`Worker._local_bt`); an ESP
  connected to the charger ends a local pause. Setting `skyrc` (`SKYRC_MODES` auto / esp / direct, "+" dialog):
  esp = `BleManager.ignore` gets 'SkyRC', direct = no `NetHost`s; unset 'auto' becomes 'esp' at the first ESP
  connection (`Worker.skyrc_changed`).
- Settings: `QSettings('battbench', 'battbench')` keys `a4`, `skyrc`, `esp_hosts`, `esp_auto`, `lang`, `show_deleted`,
  `compress`, `hidden_series`, `report_lang` / `report_format` / `report_dir`, `backup_hours` /
  `backup_keep` / `backup_daily` / `backup_weekly` / `backup_monthly`.
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
**Branches**: work happens on `devel` (pushed to `origin` only, every commit); `main` holds one squash commit per
release and is identical on `origin` and `github`. Never push `devel` to `github`, never commit on `main` by hand.
`packaging/release.py [--version 2026.10] [--dry-run] [--build]` on a clean `devel`: sets `__version__` and commits
"Release …" on `devel`, adds a commit with exactly `devel`'s files to `main` (message: the `devel` commits since the
last release), tags it `v…` and asks before pushing (`devel` → origin, `main` + tag → origin and github). The tag on
github starts `.github/workflows/release.yml` (Windows runner: `.venv`, NSIS, build.ps1 from the tagged commit, so the
version shows the public commit) which publishes the GitHub release with the installer; notes = the release commit's
message + the install text. `--build` also builds the installer locally. Interactive – the user runs it, or asks
Claude to.
The installer is per-user (no admin), adds a Start menu entry and an optional desktop shortcut; uninstall asks whether
to delete the database. Silent: `/S`, target folder `/D=…`.
