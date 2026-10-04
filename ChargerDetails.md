# Charger details

How BattBench talks to each charger and what it gets back. BattBench sends **read-only queries only** – no command
that changes a setting, starts, stops or restarts a task.

## General
- **USB**: all ISDT chargers share the USB id `28e9:028a` (HID). BattBench looks for new devices every 3 s and asks
  each one for its device info (`0xE0` → `0xE1`); the model id in the answer selects the driver. Every charger gets
  its own reading thread, so a hanging or unplugged charger does not slow down the others; when it comes back,
  BattBench reconnects.
- **Bluetooth LE**: a scan every 15 s finds ISDT Air and SkyRC chargers by their advertised name.
- **Identity**: an N8 is recognised by the serial number in its device info (last 6 bytes of the `0xE1` answer).
  The A4 Air has no serial number over USB; it is recognised by its USB port, over Bluetooth by its address.
- **One charger over USB and Bluetooth** (A4 Air): both connections first show up as separate chargers. While two
  chargers of the same model are connected over different ways, BattBench compares the voltages of their occupied
  slots on every poll. If 7 of the last 10 polls match within ±60 mV, they are the same charger: the younger entry is
  merged into the older one (readings and sessions; duplicate sessions are dropped) and keeps its key as second key.
  Nothing is compared while all slots are empty. The charger tab shows the USB and / or Bluetooth symbol.
- **Gaps** (app closed, charger unplugged): if the charger still runs the same task and its task timer kept counting,
  the session is continued instead of starting a new one. On start, sessions of the last 24 h are joined this way.
- A charge or discharge phase counts from 10 mA (a full cell on the A4 Air shows −3…+7 mA noise).
- Sessions shorter than 10 s are not stored (e.g. an empty A4 Air slot probing for a cell).

## ISDT N8 / N16 / N24 (USB)
- Once per second per slot `0xDE <slot>` → 32-byte metrics packet `0xDF` (layout as in isdttool): mode, chemistry,
  size, voltage, current, mAh, internal resistance, temperature, progress, task time. Plus `0xFE 00` for the input
  voltage. An answer for another slot (late, after a timeout) is skipped.
- **Size**: AA and AAA are reported the same, so BattBench shows “AA/AAA”.
- **Modes**: 3 charging, 11 analysis (one task; the phase follows the sign of the current). The N8 has no cycle task:
  it reports activation as mode 9/10 (cycle on the other chargers); BattBench stores it as activation (13/14).
- **Progress** (%) is the value shown on the charger.
- **N16 / N24** (untested): same commands as the N8. Each charging board has 8 channels; channels of a missing board
  answer all zero while a real slot reports at least size and temperature. BattBench probes channel 8 and 16 to tell
  N8, N16 and N24 apart. The N16 shows 16 narrow tiles in one row, the N24 two rows of 12.
- On a PC USB port (≈ 5 V) the charge current drops to about 100 mA per slot once about 4 slots charge (discharging
  stays at 500 mA).

## ISDT C4, C4 EVO, A4, UC4 (USB, untested)
- `0xDE <slot>` → 26-byte metrics packet, 4 channels.
- **C4 EVO**: own mode / chemistry tables, no size; `0xE4 <ch>` (read only) gives the input voltage.
- **A4, UC4**: bytes 18–21 do not hold the mAh, so BattBench adds them up from current × time.

## ISDT A4 Air
### Bluetooth (recommended)
- Read-only connection: device info, bind like the ISD Link app, start the data stream, then per slot every ~2 s
  state (`13 E6`), values (`12 E4`) and internal resistance (`13 FA`, unit 0.01 mΩ).
- Delivers state (charging / full / error), charge level %, mAh and mWh from the charger, the charger's own internal
  resistance, voltage, current and input voltage. No temperature.
- The A8 Air and C4 Air use the same protocol (untested).

### USB
- `0xE4 <ch>` per slot → input voltage, cell voltage, current, status flag, temperature. The channels count backwards
  (channel 0 = slot 4); BattBench shows the slot numbers printed on the charger.
- **State**: a slot is charging while more than 15 mA flow. 30 s without charge current (the charging pauses every
  6 s last only 1.1 s) mean the cell is charged. The mAh are added up from current × time.
- **Empty or occupied**: an empty slot alternates every second between flag −1 (≈ 4.35 V, −20…−37 mA) and ≈ 1 V / 0 mA.
  A full cell also reports flag −1 now and then, but with only −2…−3 mA. About every 2 min an empty slot probes for a
  cell for ~10 s: 4.66 V / 0 mA / flag 0 (open output) alternating with ≈ 260 mV / −1…−6 mA / flag −1. So a reading
  counts as “empty” with flag −1 and ≤ −10 mA or below 0.5 V, and any reading above 4.4 V (no cell gives more).
  Occupied = no “empty” in the last 6 readings; removed = 3 of the last 6 “empty”.
- **Internal resistance (≈, estimated)**: in the charging pauses the slot is read again after 120 ms (the voltage
  follows the current only after ≈ 80 ms). R = (U under load − U in the pause) / I, median of the last 9 pauses, only
  from 200 mA. It is clearly higher than the charger's own value (≈ 178 instead of 137 mΩ), so Bluetooth is the
  better source. State, internal resistance and capacity of the charger itself are only available over Bluetooth.
- On a PC USB port (≈ 4.5 V) the A4 Air charges 4 cells with only 23–45 mA per slot, a single cell with ≈ 480 mA.

### How BattBench reads it (tab “+”)
- **Automatic** (default): connected over USB and Bluetooth; readings come from Bluetooth (more values). If Bluetooth
  drops, USB takes over without a gap.
- **Bluetooth only**: USB is never opened for the A4 Air.
- **USB only**: no Bluetooth connection to the A4 Air; other Bluetooth chargers are still read. No Bluetooth at all:
  `--no-bt`.

## SkyRC MC3000 / MC5000 (Bluetooth, untested)
- Service `FFE0`, characteristic `FFE1` (write without response + notify), sum checksum.
- **MC5000**: `0F 03 91 <bit> cs` per slot → current, voltage, temperature, mAh, seconds, internal resistance,
  status (0 idle, 1 starting, 2 charging, 3 discharging, 4 pause, 5/6 done), operation, error, battery type.
- **MC3000**: `0F 55 <slot> 00 … cs` (20 bytes) → battery type, operation, status (0 idle, 1 charging,
  2 discharging, 3 pause, 4 done, from 128 error), time, voltage, current, mAh, temperature, internal resistance.
- Only this status query is sent. **Never sent**: the handshake / init sequence of the MC5000 app (`57` / `74` / `65` /
  `FE` – it restarts running tasks with reset counters, `FE` even stops them), start / stop (`93`), settings (`94`,
  MC3000 `11`).
- Operation → task: charge, discharge, storage; cycle / refresh → cycle; break-in → activation. While discharging,
  current and mAh are negative as on the N8.
- Recognised by the advertised name (`MC3000`, `MC5000`); with a generic name (`#Charger…`, `Charger FF-…`) BattBench
  tries both status queries and keeps the one the charger answers.
- The MC3000's USB port (own driver, profiles only) is not used.

## Database
SQLite, one file (see README for its location).

| Table | Content |
|---|---|
| `devices` | chargers: key (`alt_key` for the second connection), model, name, slots, firmware, last seen |
| `samples` | every reading per charger, slot and second, including the raw packet |
| `sessions` | sessions: charger, slot, time, task, status, result, rating, battery |
| `phases` | charge and discharge phases of each session |
| `batteries` | batteries: id, name, maker, capacity, type, description, model |
| `models` | maker / model list |

Task, status, rating and phase are stored as English words (`charge`, `done`, `very good`, …) and translated when
shown. Older databases are upgraded automatically on start.
