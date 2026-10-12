"""SkyRC MC5000: programs (charge profiles), start and stop - the packets only, no I/O and no Qt.

Packets as the SkyRC app sends them (layout from the btsnoop captures documented by rssdev10/skyrc-mc-rs; the
captured packets are the unit test vectors), framed like the status query: 0F len cmd data cs, cs = sum(cmd, data).

  94 <40 bytes>  program a slot -> 0F 04 94 <mask> 01 cs. With a battery in the slot the charger then starts the
                 task by itself (after a short check and a resistance measurement). A working slot is skipped,
                 and the answer is 01 all the same: stop it first and check the status afterwards.
  FE <mask>      stop a slot (the app's stop button); FE 00 stops all slots
  98 <mask>      the charger's voltage curve of the slot's task (pages; read only)
  5F <mask>      slot summary -> 0F 0C 5F mask status type ? task charge/100mA discharge/100mA 0 0 key cs (read only)
  96 04          system settings -> 0F 09 96 ? keys notify done light ? timeout cs (read only)
  95 04 <6>      system settings, the same 6 bytes -> 0F 04 95 04 01 cs

With more than one bit in the mask the charger only takes the lowest one (mask 00 = all slots), so BattBench sends
one packet per slot. The start command 93 of older captures does nothing on firmware 2.02; it is not sent.

The mode byte of a program is the index in the chemistry's mode list (device_skyrc.MC5000_MODES), the same number
the status answer reports. Nothing here is sent by itself: the GUI builds program / stop / settings packets on an
explicit start, stop or save, and asks the read-only 5F / 96 for what it shows."""
from dataclasses import asdict, astuple, dataclass, fields

from .i18n import QT_TRANSLATE_NOOP

CHEMISTRIES = ['LiIon', 'LiHV', 'LiFe', 'NiMH', 'NiCd', 'Eneloop', 'NiZn', 'RAM', 'LTO', 'NaIon']
# per chemistry: modes (index = mode byte), charge voltage, discharge cut-off, storage voltage (lithium-like) in mV
_LI = ('Charge', 'Storage', 'Discharge', 'Cycle')
_NI = ('Charge', 'Refresh', 'Break in', 'Discharge', 'Cycle')
_ZN = ('Charge', 'Discharge', 'Cycle')
CHEM = {'LiIon': (_LI, 4200, 3200, 3800), 'LiHV': (_LI, 4350, 3400, 3900), 'LiFe': (_LI, 3650, 2900, 3300),
        'NiMH': (_NI, 1650, 900, 0), 'NiCd': (_NI, 1650, 900, 0), 'Eneloop': (_NI, 1650, 900, 0),
        'NiZn': (_ZN, 1900, 1100, 0), 'RAM': (_ZN, 1650, 900, 0), 'LTO': (_LI, 2850, 1800, 2400),
        'NaIon': (_LI, 4000, 2000, 3500)}
DIRECTIONS = ['C>D', 'D>C', 'C>D>C', 'D>C>D']       # cycle direction byte 0..3
# Eneloop type (byte 35): (name, capacity mAh, charge / discharge mA, the same for break-in = 0.1 C / 0.2 C)
ENELOOP = [('Std AA', 2000, 1000, 500, 200, 400), ('Std AAA', 800, 400, 200, 50, 100),
           ('Pro/XX AA', 2500, 1250, 600, 250, 500), ('Pro/XX AAA', 900, 450, 200, 50, 100),
           ('Lite AA', 1000, 500, 250, 100, 200), ('Lite AAA', 600, 300, 150, 50, 100),
           ('Plus AA', 2200, 1100, 550, 200, 400), ('Std C', 3200, 1600, 800, 300, 600),
           ('Std D', 6000, 3000, 1500, 600, 1200)]
SLOT_MASK = (0x01, 0x02, 0x04, 0x08)
# what the SkyRC app offers, per battery type (CHEMISTRIES order), mV: charge voltage, discharge cut-off (0.1 V
# steps), recharge ("hold") voltage, storage voltage
_RANGES = {'charge_mv': ([4050, 4250, 3580, 1470, 1470, 1470, 1200, 1400, 2600, 3200],
                         [4250, 4400, 3700, 1800, 1800, 1800, 2000, 1700, 2900, 4150], 10),
           'discharge_mv': ([2900, 2600, 2600, 600, 600, 600, 900, 500, 1500, 1500],
                            [3300, 3800, 3000, 1000, 1000, 1000, 1600, 1300, 2300, 3500], 100),
           'restart_mv': ([3980, 4080, 3380, 1300, 1300, 1300, 1500, 1400, 2580, 3180],
                          [4180, 4330, 3580, 1450, 1450, 1450, 1880, 1500, 2830, 3980], 10),
           'storage_mv': ([3700, 3850, 3250, 3250, 3250, 3250, 3250, 3250, 2250, 3400],
                          [3850, 3950, 3400, 3400, 3400, 3400, 3400, 3400, 2650, 3600], 10)}
# fields where the app offers "off" (0) before its range
OFF = {'capacity', 'restart_mv', 'charge_end_ma', 'discharge_end_ma', 'trickle_ma', 'timer'}


def choices(attr: str, chem: str):
    """The values the SkyRC app offers for a field of a program (0 = off for the fields in OFF)."""
    def r(lo, hi, step):
        return list(range(lo, hi + 1, step))
    i = CHEMISTRIES.index(chem) if chem in CHEMISTRIES else 3
    if attr in _RANGES:
        lo, hi, step = _RANGES[attr]
        vals = r(lo[i], hi[i], step)
    else:
        vals = {'charge_ma': r(50, 5000, 50), 'discharge_ma': r(50, 2000, 50), 'capacity': r(100, 10000, 100),
                'charge_end_ma': r(10, 990, 10), 'discharge_end_ma': r(10, 990, 10), 'delta_peak': r(3, 12, 1),
                'trickle_ma': r(50, 300, 10), 'rest_after_charge': r(1, 120, 1),
                'rest_after_discharge': r(1, 120, 1), 'cycles': r(1, 3, 1), 'timer': r(30, 1440, 30),
                'temp_limit': [60], 'max_time': [300]}[attr]
    return ([0] if attr in OFF else []) + vals


@dataclass
class Program:
    """One charge profile. Currents in mA, voltages in mV, times in minutes."""
    name: str = ''
    chem: str = 'NiMH'
    mode: str = 'Charge'
    charge_ma: int = 1000
    discharge_ma: int = 500
    capacity: int = 2500              # capacity limit (the task stops there), mAh
    charge_mv: int = 1650             # charge end / CV voltage
    discharge_mv: int = 900           # discharge cut-off
    charge_end_ma: int = 100          # CV end current (lithium)
    discharge_end_ma: int = 100       # discharge end current (the current is turned down to this at the cut-off)
    rest_after_charge: int = 10
    rest_after_discharge: int = 10
    cycles: int = 1
    direction: str = 'C>D'
    delta_peak: int = 6               # -dV end of charge (NiMH / NiCd), mV
    trickle_ma: int = 0               # after the charge (NiMH), steps of 10 mA
    restart_mv: int = 0               # recharge below this voltage (NiMH, with trickle)
    temp_limit: int = 60              # deg C (the app always sent 60)
    timer: int = 0                    # 0 = off
    max_time: int = 300
    eneloop: int = 0                  # Eneloop type, index in ENELOOP (only for chem 'Eneloop')
    storage_mv: int = 0               # storage voltage (lithium-like), 0 = the battery type's default

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})

    def modes(self):
        return CHEM.get(self.chem, CHEM['NiMH'])[0]

    def problems(self):
        """Values the charger would not take or that make no sense; [] = fine. English texts (QT_TRANSLATE_NOOP
        'data' in the GUI)."""
        p = []
        if self.chem not in CHEM:
            p.append(QT_TRANSLATE_NOOP('data', 'unknown battery type'))
        elif self.mode not in self.modes():
            p.append(QT_TRANSLATE_NOOP('data', 'task not available for this battery type'))
        if self.direction not in DIRECTIONS:
            p.append(QT_TRANSLATE_NOOP('data', 'unknown cycle order'))
        if not 50 <= self.charge_ma <= 5000 and self.mode != 'Discharge':
            p.append(QT_TRANSLATE_NOOP('data', 'charge current 50 - 5000 mA'))
        if not 50 <= self.discharge_ma <= 2000 and self.mode != 'Charge':
            p.append(QT_TRANSLATE_NOOP('data', 'discharge current 50 - 2000 mA'))
        if not 1 <= self.cycles <= 99:
            p.append(QT_TRANSLATE_NOOP('data', 'cycles 1 - 99'))
        if not 0 < self.discharge_mv < self.charge_mv <= 4400:
            p.append(QT_TRANSLATE_NOOP('data', 'the cut-off voltage must be below the charge voltage'))
        if not (self.capacity == 0 or 100 <= self.capacity <= 50000):
            p.append(QT_TRANSLATE_NOOP('data', 'capacity limit off or 100 - 50000 mAh'))
        if not 0 <= self.eneloop < len(ENELOOP):
            p.append(QT_TRANSLATE_NOOP('data', 'unknown Eneloop type'))
        return p


def defaults(chem: str, mode='Charge', eneloop=0) -> Program:
    """A program with the SkyRC app's defaults for battery type, task and (Eneloop) type: capacity limit and
    currents by type, 0.1 C / 0.2 C for a break-in; voltages of the type, 100 mA end currents, 10 min rests, one
    cycle C>D, -dV 6 mV."""
    modes, charge_mv, discharge_mv, _ = CHEM[chem]
    mode = mode if mode in modes else modes[0]
    p = Program(chem=chem, mode=mode, capacity=3000, charge_ma=1000, discharge_ma=1000, charge_mv=charge_mv,
                discharge_mv=discharge_mv, charge_end_ma=100, discharge_end_ma=100, rest_after_charge=10,
                rest_after_discharge=10, cycles=1, direction='C>D', delta_peak=6, trickle_ma=0, restart_mv=0,
                temp_limit=60, timer=0, eneloop=0, storage_mv=CHEM[chem][3])
    if chem in ('LiIon', 'LiHV', 'LiFe'):
        p.capacity, p.charge_ma, p.discharge_ma = 5000, 3000, 2000
    elif chem == 'NaIon':
        p.capacity, p.charge_ma, p.discharge_ma = 5000, 2000, 2000
    elif chem in ('NiMH', 'NiCd') and mode == 'Break in':
        p.charge_ma, p.discharge_ma = 300, 600
    elif chem == 'Eneloop':
        p.eneloop = eneloop if 0 <= eneloop < len(ENELOOP) else 0
        _n, p.capacity, p.charge_ma, p.discharge_ma, brk_c, brk_d = ENELOOP[p.eneloop]
        if mode == 'Break in':
            p.charge_ma, p.discharge_ma = brk_c, brk_d
    return p


def _cs(b):
    return sum(b) & 0xFF


def packet(cmd: int, data: bytes) -> bytes:
    body = bytes([cmd]) + bytes(data)
    return bytes([0x0F, len(body) + 1]) + body + bytes([_cs(body)])


def _u16(v):
    v = max(0, min(0xFFFF, int(v)))
    return bytes([v >> 8, v & 0xFF])


def _one_slot(mask):
    if mask not in SLOT_MASK:
        raise ValueError('one slot: mask 1, 2, 4 or 8')


def program_packet(mask: int, p: Program) -> bytes:
    """94: the program for one slot (mask 1, 2, 4, 8); starts it when a battery is in."""
    _one_slot(mask)
    return _program(mask, p)


def _program(mask, p):
    modes, _, _, storage_mv = CHEM[p.chem]
    nickel_like = storage_mv == 0
    # bytes 33-34: lithium-like = storage voltage, nickel / alkaline = 110 % of the capacity limit (as the app)
    second = int(p.capacity * 1.1) if nickel_like else p.storage_mv or storage_mv
    d = (bytes([mask, modes.index(p.mode)]) + _u16(p.charge_ma) + _u16(p.discharge_ma) + _u16(p.capacity) +
         _u16(p.charge_mv) + _u16(p.discharge_mv) + _u16(p.charge_end_ma) + _u16(p.discharge_end_ma) +
         _u16(p.rest_after_charge) + _u16(p.rest_after_discharge) +
         bytes([p.cycles & 0xFF, DIRECTIONS.index(p.direction), p.delta_peak & 0xFF, (p.trickle_ma // 10) & 0xFF]) +
         _u16(p.restart_mv) + bytes([p.temp_limit & 0xFF]) + _u16(p.timer) + _u16(p.max_time) +
         bytes([0, CHEMISTRIES.index(p.chem)]) + _u16(second) +
         bytes([p.eneloop if p.chem == 'Eneloop' else 0]) + bytes(4))
    assert len(d) == 40
    return packet(0x94, d)


def stop_packet(mask: int) -> bytes:
    """FE: stop one slot (what the SkyRC app sends for its stop button). Never 0: that stops all slots."""
    _one_slot(mask)
    return packet(0xFE, [mask])


def history_packet(slot: int) -> bytes:
    """98: the charger's stored voltage curve of the slot's (last) task, page by page."""
    return packet(0x98, [SLOT_MASK[slot]])


def acked(reply: bytes, cmd: int) -> bool:
    """0F 04 <cmd> <mask> 01 cs: the charger took it."""
    return (len(reply) >= 6 and reply[0] == 0x0F and reply[2] == cmd and reply[4] == 1 and
            _cs(reply[2:-1]) == reply[-1])


def parse_history(pages):
    """98 answers (0F len 98 mask total page interval_s mV LE ...) -> (interval s, [mV]) or None. Layout as the
    app reads it; not yet checked on a real charger."""
    volts, interval = [], 0
    for p in sorted(pages, key=lambda p: p[5] if len(p) > 6 else 0):
        if len(p) < 8 or p[2] != 0x98:
            continue
        interval = p[6]
        body = p[7:-1]
        volts += [body[i] | body[i + 1] << 8 for i in range(0, len(body) - 1, 2)]
    return (interval, volts) if volts else None


def from_status(raw: bytes):
    """(battery type, task) the charger reports for a slot in its status answer (0F 16 91 ...), or None. The task
    stays set after the end of a task; a changed program cannot be read back."""
    if len(raw) < 22 or raw[0] != 0x0F or raw[2] != 0x91 or raw[21] >= len(CHEMISTRIES):
        return None
    chem = CHEMISTRIES[raw[21]]
    modes = CHEM[chem][0]
    return chem, modes[raw[19]] if raw[19] < len(modes) else None


def working(raw: bytes) -> bool:
    """The slot runs a task (status charging / discharging / rest / done but not yet cleared) or shows an error:
    a program sent now would be skipped, so it is stopped first."""
    return len(raw) >= 22 and raw[0] == 0x0F and raw[2] == 0x91 and (raw[18] != 0 or raw[20] != 0)


def started(raw: bytes) -> bool:
    """The slot is charging, discharging or resting."""
    return len(raw) >= 22 and raw[0] == 0x0F and raw[2] == 0x91 and raw[18] in (2, 3, 4)


def same(a: Program, b: Program) -> bool:
    """The same program for the charger (the name does not count)."""
    da, db = a.to_dict(), b.to_dict()
    da.pop('name', None)
    db.pop('name', None)
    return da == db


def summary_packet(mask: int) -> bytes:
    """5F: summary of one slot (read only)."""
    _one_slot(mask)
    return packet(0x5F, [mask])


def _valid(reply, cmd, n):
    return (len(reply) >= n and reply[0] == 0x0F and reply[1] + 2 == len(reply) and reply[2] == cmd and
            _cs(reply[2:-1]) == reply[-1])


def parse_summary(reply: bytes):
    """5F answer -> dict(status, mode, charge_ma, discharge_ma) or None. The currents are the slot's programmed
    ones for its battery type, in steps of 100 mA (rounded down); mode = index as in the status answer."""
    if not _valid(reply, 0x5F, 14):
        return None
    return dict(status=reply[4], mode=reply[7], charge_ma=reply[8] * 100, discharge_ma=reply[9] * 100)


def same_currents(p: Program, summary) -> bool:
    """The program's currents as the charger reports them (100 mA steps)."""
    return (p.charge_ma // 100 * 100 == summary['charge_ma'] and
            p.discharge_ma // 100 * 100 == summary['discharge_ma'])


TIMEOUTS = (0, 1, 3, 5, 10, 15, 30, 60, 120)        # screen timeout choices in minutes, 0 = off
VOLUMES = (QT_TRANSLATE_NOOP('data', 'off'), QT_TRANSLATE_NOOP('data', 'low'), QT_TRANSLATE_NOOP('data', 'medium'),
           QT_TRANSLATE_NOOP('data', 'high'))
LIGHTS = VOLUMES[1:]                                # backlight 1..3
DONE_TONES = (QT_TRANSLATE_NOOP('data', 'once'), QT_TRANSLATE_NOOP('data', 'repeated'))


@dataclass
class Settings:
    """The charger's system settings as 96 reports and 95 sets them."""
    key_beep: int = 0       # 0 off .. 3 high
    notify_beep: int = 0    # 0 off .. 3 high
    done_tone: int = 0      # 0 once, 1 repeated
    backlight: int = 2      # 1 low .. 3 high
    other: int = 1          # unknown, sent back unchanged
    timeout: int = 0        # screen timeout in minutes (TIMEOUTS), 0 = off

    def valid(self):
        return (0 <= self.key_beep <= 3 and 0 <= self.notify_beep <= 3 and 0 <= self.done_tone <= 1 and
                1 <= self.backlight <= 3 and 0 <= self.other <= 255 and self.timeout in TIMEOUTS)


def settings_query() -> bytes:
    """96: read the system settings (the byte after it is not used; 04 as the app)."""
    return packet(0x96, [0x04])


def parse_settings(reply: bytes):
    if not _valid(reply, 0x96, 11):
        return None
    s = Settings(*reply[4:10])
    return s if s.valid() else None


def settings_packet(s: Settings) -> bytes:
    """95: set the system settings (all six at once: read them first and change only what the user changed)."""
    if not s.valid():
        raise ValueError('settings out of range')
    return packet(0x95, [0x04] + list(astuple(s)))
