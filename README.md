# BattBench

**Battery test bench for your PC: track every rechargeable cell over its whole life and see how it ages.**

BattBench reads ISDT and SkyRC chargers over **USB** and **Bluetooth**, records every reading, detects charge and
discharge runs, rates each battery against its nominal capacity and keeps a **history per battery**. Several chargers
can be connected at the same time. BattBench only ever *reads* from the chargers; it never changes a setting and never
starts or stops anything.

The user interface is available in **English** and **German**.

## Features
- **Live view** of every slot of every connected charger: mode, voltage, current, mAh, internal resistance,
  temperature, progress.
- **Full curves** of each run (voltage, current, mAh, internal resistance) with charge / discharge phases and a
  crosshair; the live view keeps your zoom and scrolls along.
- **Automatic session detection**: a session runs from inserting a cell to removing it (or to the next task).
  If the app was closed in between and the charger kept going, the session simply continues.
- **Rating** of each discharge against the nominal capacity: *very good* ≥ 90 %, *good* ≥ 80 %, *fair* ≥ 60 %,
  *worn out* below that; a high internal resistance lowers the rating.
- **Battery tracking**: give each cell a number (write it on the cell), assign sessions to it and see all its
  measurements in one place – across chargers and slots. Spot ageing cells, weak cells in a set and cells that should
  be retired.
- **Battery models**: a list of common cells (eneloop, IKEA LADDA, Varta, GP, 18650 / 21700, …) supplies maker,
  type and nominal capacity; add your own.
- Light and dark Windows theme.

## Supported chargers
| Charger | Connection | Slots | Tested |
|---|---|---|---|
| ISDT **N8** | USB | 8 | ✔ |
| ISDT **A4 Air** | Bluetooth (recommended) or USB | 4 | ✔ |
| ISDT N16 / N24 | USB | 16 / 24 | – |
| ISDT C4, C4 EVO, A4, UC4 | USB | 4 | – |
| ISDT A8 Air, C4 Air | Bluetooth | 8 / 6 | – |
| SkyRC MC3000, MC5000 | Bluetooth | 4 | – |

Chargers marked “–” are supported by protocol but have not been tried on real hardware yet – reports are welcome.
What exactly is read from each charger is described in [ChargerDetails.md](ChargerDetails.md).

## Installation
### Windows
Download `BattBench-<version>-setup.exe` from the releases and run it. It installs for the current user (no
administrator rights needed), adds BattBench to the Start menu and, if you like, to the desktop. Your measurements are
stored in `%LOCALAPPDATA%\BattBench\battbench.db`; uninstalling asks whether to keep them.

### From source (Windows, Linux, macOS)
Requires Python 3.10 or newer.
```
git clone https://git.ashberg.de/folke/battbench.git
cd battbench
run.bat          (Windows)
./run.sh         (Linux / macOS)
```
The first start creates a virtual environment in `.venv` and installs the requirements. Run from source, the database
is `battbench.db` in the project folder. On Linux, USB access to the chargers needs a udev rule for the ISDT USB id,
e.g. `/etc/udev/rules.d/70-isdt.rules`:
```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="28e9", ATTRS{idProduct}=="028a", TAG+="uaccess"
```

### Command line options
| Option | Effect |
|---|---|
| `--offline` | only view the database, no chargers |
| `--db FILE` | use another database file |
| `--lang auto\|en\|de` | user interface language (also selectable in the app, tab “+”) |
| `--a4 usb\|bt\|both` | how the A4 Air is read (also selectable in the app) |
| `--no-bt` | no Bluetooth at all |

## Before you start
- Close the official **ISD Go** updater: it opens the chargers over USB exclusively.
- Quit the **ISD Link** phone app (or switch off Bluetooth on the phone). An ISDT Air charger accepts only one
  Bluetooth connection and is invisible to others while the phone is connected.
- A PC USB port delivers little power. The N8 then charges with only about 100 mA per slot when several slots are
  busy, so a full analysis of 8 cells can take two days. A QC 3.0 / USB-PD power supply avoids this.

## Using BattBench
- **Charger tabs** at the top work like browser tabs: name, input voltage, connection (USB / Bluetooth symbol) and
  one LED per slot in the slot colour – grey empty, orange charging, pink discharging, blue analysis / activation /
  cycle, green done, red error. Click a tab to see its slot tiles.
- **Tab “+”**: supported chargers, what is connected, Bluetooth search, how to read the A4 Air.
- **Slot tile**: click it to see the running session with its curves and result.
- **Result panel**: rating, capacities, internal resistance, temperature and phases of the selected session. Assign
  the battery here (searchable list, or *New …*) or enter the nominal capacity.
- **Tabs at the bottom**
  - *Sessions*: all sessions with charger, slot, result and rating; click one to open it.
  - *Batteries*: your batteries with number of sessions, last measured capacity and rating, last measurement and
    last charge, with a search field; on the right the **history** of the selected battery. Double-click a session
    to open it.
  - *Models*: maker / model list. Changing a model updates all its batteries and rates their sessions again.
  - *Chargers*: known chargers; rename them (the name is stored only in BattBench, never in the charger).
  - *Settings*: language. *Info*: version, author, links, location of the database.
- **Tables** sort by a click on a column title and filter like a spreadsheet: the funnel in each column title opens
  the list of the column's values (with search) – tick the ones to show. Dates are filtered by day.
- **Battery field**: clicking into it selects the text, so you can simply type to search (“ene pro” finds
  eneloop pro).

### Rating
The rating compares the discharge capacity with the nominal capacity. The nominal capacity comes from the assigned
battery (or its model) or is entered in the result panel. The N8 does not tell AA from AAA, so without a battery or a
nominal capacity a session from the N8 is not rated.

## Versions
Releases are numbered by year and month: `2026.10`, bug fix releases `2026.10.1`, `2026.10.2`, … Run from a git
checkout, BattBench also shows the commit it runs from (tab *Info*, `--help`).

## Development
See [CLAUDE.md](CLAUDE.md) for the code layout and conventions.
- Run from source: `run.bat` / `run.sh` (arguments are passed on).
- Translations: texts in the code are English and wrapped in `tr()`. After changing texts run
  `.venv\Scripts\python battbench\translations\update.py`, translate the new entries in
  `battbench/translations/battbench_de.ts` (e.g. with `pyside6-linguist`) and run the script again.
- Windows installer: install [NSIS 3](https://nsis.sourceforge.io), then
  `powershell -ExecutionPolicy Bypass -File packaging\build.ps1` → `build\BattBench-<version>-setup.exe`.

## Credits
- USB protocol library: [isdttool](https://github.com/maxried/isdttool) (patched fork used here).
- ISDT Air Bluetooth protocol: [isdt_air_ble](https://github.com/mtheli/isdt_air_ble),
  [ISDT-Charge-Utility](https://github.com/DittelHome/ISDT-Charge-Utility).
- SkyRC Bluetooth protocol: [skyrc-mc3000](https://github.com/kolinger/skyrc-mc3000),
  [skyrc-mc-rs](https://github.com/rssdev10/skyrc-mc-rs).

BattBench is not affiliated with ISDT or SkyRC. Chargers and batteries can be dangerous – never leave them
unattended. This software comes without any warranty.

## Author and license
Folke Ashberg, [www.ashberg.de](https://www.ashberg.de). GPLv3, see [LICENSE](LICENSE).
