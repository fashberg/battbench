"""ISDT charger access via the (patched) isdttool library. Read-only: only version (0xE0), metrics
(0xDE), input voltage (0xFE 00 on the N series) and the channel query 0xE4 (A4Air, C4 EVO) are ever sent.

All ISDT chargers use the same USB id (28e9:028a); the model is told apart by the answer to 0xE0.
Supported: N8 / N16 / N24 (NXHOSTP, NXHOST), C4, C4 EVO, A4, UC4 and A4Air (USB). Tested on real hardware:
N8 and A4 Air; the others are expected to answer the same way (see ChargerDetails.md)."""
import hashlib
import statistics
import struct
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

import hid
from isdttool.charger.charger import get_device
from isdttool.charger.representation import parse_packet

from .i18n import tr

VID, PID = 0x28E9, 0x028A


@dataclass
class Sample:
    t: float            # unix time
    slot: int           # 0-based
    mode: int
    mode_str: str
    chem: str
    size: str
    mv: int
    ma: int
    res: int            # internal resistance (display unit, likely mOhm)
    mah: int            # phase counter, negative while discharging
    secs: int           # task time
    temp: int
    itemp: int
    progress: int
    power: int
    energy: int
    raw: str
    dev: int = 0        # devices.id in the database (set by the worker)

    @property
    def empty(self) -> bool:
        return self.mode == 0 and self.size == 'empty'


@dataclass
class UsbInfo:
    path: bytes
    product: str
    serial: str


def usb_devices() -> List[UsbInfo]:
    return [UsbInfo(d['path'], d.get('product_string') or '', d.get('serial_number') or '')
            for d in hid.enumerate(VID, PID)]


class Charger:
    """One connected charger. key = stable identity (same device -> same key across restarts)."""
    model = ''            # model id from 0xE0
    label = ''            # default display name
    slots = 0

    def __init__(self, usb: UsbInfo, dev, e1: bytes):
        self.usb = usb
        self.dev = dev                                 # isdttool Charger
        self.version = f'{self.label} FW {".".join(str(b) for b in e1[17:21])}'
        self.key = self.make_key(e1)

    def make_key(self, e1: bytes) -> str:
        if self.usb.serial and self.usb.serial.strip('0'):
            return f'{self.model}-usb-{self.usb.serial}'
        # no serial number: the HID path identifies the USB port
        return f'{self.model}-port-{hashlib.sha1(self.usb.path).hexdigest()[:10]}'

    def close(self):
        try:
            self.dev.__device__.close()
        except Exception:
            pass

    def read_slot(self, slot: int) -> Optional[Sample]:
        raise NotImplementedError

    def input_mv(self) -> Optional[int]:
        return None

    def seed(self, slot: int, last: Sample):
        """Continue from the last stored sample of a slot (after a restart / reconnect)."""


class Metrics(Charger):
    """Chargers answering 0xDE <ch> with a 0xDF metrics packet (layout as in isdttool: 32 bytes on the
    N series, 26 bytes on C4 / C4 EVO / A4 / UC4)."""
    tables = ''           # isdttool model whose mode / chemistry / size tables apply

    def read_raw(self, slot: int):
        self.dev.metrics(slot)
        # a late answer to an earlier request (e.g. after a read timeout) may still be queued: it would end up
        # under this slot (seen: a whole poll cycle shifted by one slot). Skip answers for other slots.
        for _ in range(3):
            p = self.dev.read_packet()
            if not p or (len(p) >= 2 and p[0] == 0xDF and p[1] == slot):
                return p
        return None

    def read_slot(self, slot: int) -> Optional[Sample]:
        p = self.read_raw(slot)
        if not p:
            return None
        r = parse_packet(p, self.tables or self.model)
        if not r.get('_channel exists') or r.get('_malformed'):
            return None
        return Sample(t=time.time(), slot=slot, mode=r['mode id'], mode_str=r['mode string'],
                      chem=r['chemistry string'], size=r['dimensions string'],
                      mv=r['charging voltage'], ma=r['charging current'], res=r['resistance'],
                      mah=r['capacity or peak voltage'], secs=r['time'], temp=r['temperature'],
                      itemp=r['internal_temperature'], progress=r['progress'], power=r['power'],
                      energy=r['energy'], raw=p.hex())


# The N8 has no cycle task: it reports activation as mode 9/10 (cycle on other chargers); stored as 13/14
N8_MODES = {9: (13, 'activation'), 10: (14, 'activation done')}


class N8(Metrics):
    """ISDT N8 / N16 / N24: model id NXHOSTP (current) or NXHOST (first generation); the answer to 0xE0 does
    not tell them apart. Each charging board has 8 channels; channels of a missing board answer all zero, a real
    slot reports at least size and temperature -> probe channel 8 and 16."""
    model, label, slots = 'NXHOSTP', 'N8', 8
    tables = 'NXHOSTP'

    def __init__(self, usb, dev, e1):
        super().__init__(usb, dev, e1)
        for ch in (8, 16):
            if not any(self._probe(ch)):
                break
            self.slots = ch + 8
        self.label = f'N{self.slots}'
        self.version = self.version.replace('N8 ', f'{self.label} ', 1)

    def _probe(self, ch):
        for _ in range(3):                        # a board may still be starting: look a few times
            p = self.read_raw(ch)
            if p and any(p[2:]):
                return p[2:]
            time.sleep(0.2)
        return b''

    def read_slot(self, slot: int) -> Optional[Sample]:
        s = super().read_slot(slot)
        if s and s.mode in N8_MODES:
            s.mode, s.mode_str = N8_MODES[s.mode]
        return s

    def make_key(self, e1: bytes) -> str:
        # the 39-byte answer ends with 6 bytes serial / production date (written by 0xAC at the factory)
        if len(e1) >= 39 and any(e1[33:39]):
            return f'{self.model}-{e1[33:39].hex()}'
        return super().make_key(e1)

    def input_mv(self) -> Optional[int]:
        """Input voltage via 0xFE 00 (read-only variant)."""
        self.dev.write_to_charger(bytearray([0xFE, 0x00]))
        p = self.dev.read_packet()
        if p and len(p) >= 5 and p[0] == 0xFF:
            return p[3] | p[4] << 8
        return None


class NOld(N8):
    """First N8 / N16 / N24 generation: same commands and packet."""
    model = 'NXHOST'


class C4(Metrics):
    """ISDT C4: 4 slots, sizes AAA / AA / 18650 / 26650. No input voltage."""
    model, label, slots = 'C4', 'C4', 4


class C4Evo(Metrics):
    """ISDT C4 EVO: 4 slots, own mode / chemistry tables, no size. 0xE4 <ch> (read only) -> 0xE5 ch,
    input mV, cell mV, |mA|, 0, 0, temperature."""
    model, label, slots = 'C4EVO', 'C4 EVO', 4

    def input_mv(self) -> Optional[int]:
        self.dev.write_to_charger(bytearray([0xE4, 0x00]))
        p = self.dev.read_packet()
        if p and len(p) >= 4 and p[0] == 0xE5:
            return p[2] | p[3] << 8
        return None


class A4(Metrics):
    """ISDT A4 (without Air) and UC4: 4 slots. Bytes 18-21 of the metrics packet (mAh on the other models)
    are not the mAh here, so they are added up from current x time (reset when a slot is empty or its mode
    changes)."""
    model, label, slots = 'A4', 'A4', 4
    tables = 'A4'

    def __init__(self, usb, dev, e1):
        super().__init__(usb, dev, e1)
        self.acc: Dict[int, list] = {}            # slot -> [mode, t, mAh]

    def read_slot(self, slot):
        s = super().read_slot(slot)
        if s is None:
            return None
        a = self.acc.get(slot)
        if s.mode == 0 or a is None or a[0] != s.mode:
            a = self.acc[slot] = [s.mode, s.t, 0.0]
        a[2] += s.ma * min(s.t - a[1], 5.0) / 3600
        a[1] = s.t
        s.mah = int(a[2])
        return s

    def seed(self, slot, last):
        if last.mode:
            self.acc[slot] = [last.mode, last.t, float(last.mah)]


class UC4(A4):
    model, label = 'UC4', 'UC4'
    tables = 'C4'         # not in isdttool; like the A4 with C4-like sizes (unverified)


class A4Air(Charger):
    """0xE4 <ch> -> e5, ch, input mV, cell mV, mA, flag, temp °C. flag: -1 empty, 0 occupied, 1 seen under load
    with a very old NiMH (estimated ~1.5 Ohm; the ISDT app showed no resistance for it). The channels count backwards:
    channel 0 is slot 4 on the charger (slot numbers here are the charger's). No mode, chemistry,
    mAh or resistance over USB: charging = current above CHARGE_MA, the mAh are added up here.
    Internal resistance is estimated from the current pauses the charger makes every ~6 s (1.08 s each,
    measured with 14 ms polling): the reported voltage follows the current only after ~80 ms, then stays
    flat. So on the first reading of a pause the slot is read again after PAUSE_SETTLE seconds and
    R = (U load, last reading before - U pause, settled) / I load; median of the last IR_PAUSES pauses,
    only while at least IR_MIN_MA flow.
    This is the plain dU/I; the charger's own value (only via Bluetooth, not available over USB) is lower:
    137 vs. 159-172 mOhm for the same cell."""
    model, label, slots = 'A4Air', 'A4Air', 4
    CHARGE_MA = 15          # with 4 cells on a weak supply it charges with only 23-45 mA per slot;
                            # a full cell shows -3..+7 mA
    EMPTY_MA = -10          # "empty" (flag -1) of an empty slot comes with -20..-37 mA (4.35 V open);
                            # a full cell also reports flag -1 now and then, but with only -2..-3 mA
    OPEN_MV = 4400          # an empty slot probes every ~2 min: 4.66 V / 0 mA / flag 0 (open output, above any
    LOW_MV = 500            # cell) alternating with ~260 mV / -1..-6 mA / flag -1 -> both count as "empty"
    IR_MIN_MA = 200         # below this the voltage step is not ohmic (30 mA: ~1.3 Ohm for a ~0.17 Ohm cell);
                            # the charger itself shows no resistance then either
    HOLD_SECS = 30          # charging pauses (every ~6 s, 1.1 s) don't end "charging"; 30 s without -> full
    WINDOW = 6              # an empty slot flips between "empty" and ~1 V / 0 mA every second (probing):
                            # occupied = no "empty" in the last 6 readings, removed = 3 of the last 6 "empty"
    IR_PAUSES = 9
    PAUSE_SETTLE = 0.12

    def __init__(self, usb, dev, e1):
        super().__init__(usb, dev, e1)
        self.in_mv = None
        self.st: Dict[int, dict] = {}
        self.hist: Dict[int, deque] = {}    # last WINDOW readings per slot: True = reported empty

    def seed(self, slot, last):
        if last.mode:
            # a charging cell counts as charging from (re)connect on, not from the last stored sample
            # (that may be minutes old after a restart and would make it look full for a moment)
            self.st[slot] = self._new_state(last.t, float(last.mah), last.t - last.secs,
                                            time.time() if last.mode == 3 else 0)

    def _new_state(self, t, mah=0.0, start=None, charging_t=0):
        return dict(t=t, mah=mah, start=t if start is None else start, charging_t=charging_t,
                    load=None, paused=False, ir=deque(maxlen=self.IR_PAUSES))

    def _query(self, ch):
        self.dev.write_to_charger(bytearray([0xE4, ch]))
        p = self.dev.read_packet()
        if not p or len(p) < 11 or p[0] != 0xE5 or p[1] != ch:
            return None
        return p, struct.unpack_from('<BBHHhhB', bytes(p))[2:]

    def read_slot(self, slot: int) -> Optional[Sample]:
        ch = self.slots - 1 - slot                          # USB channel 0 = slot 4 on the charger
        q = self._query(ch)
        if q is None:
            return None
        p, (in_mv, mv, ma, flag, temp) = q
        self.in_mv = in_mv
        now = time.time()
        h = self.hist.setdefault(slot, deque(maxlen=self.WINDOW))
        h.append(mv > self.OPEN_MV or flag == -1 and (ma <= self.EMPTY_MA or mv < self.LOW_MV))
        if ma > self.CHARGE_MA:
            empty = False
        elif slot in self.st:
            empty = sum(h) >= self.WINDOW // 2
        else:
            empty = len(h) < self.WINDOW or any(h)
        if empty:
            self.st.pop(slot, None)
            mode, mode_str, size, mah, secs, res = 0, 'empty', 'empty', 0, 0, 0
        else:
            st = self.st.setdefault(slot, self._new_state(now))
            dt = min(now - st['t'], 5.0)
            st['t'] = now
            if ma > self.CHARGE_MA:
                st['mah'] += ma * dt / 3600
                st['charging_t'] = now
            if ma > self.CHARGE_MA:
                st['load'], st['paused'] = (now, mv, ma), False
            elif st['load'] and now - st['load'][0] < 2.5 and not st['paused']:
                st['paused'] = True                         # first reading of a pause: measure once
                time.sleep(self.PAUSE_SETTLE)
                q2 = self._query(ch) if st['load'][2] >= self.IR_MIN_MA else None
                if q2 and q2[1][2] <= self.CHARGE_MA:
                    r = (st['load'][1] - q2[1][1]) * 1000 / st['load'][2]
                    if 10 <= r <= 3000:
                        st['ir'].append(r)
            charging = now - st['charging_t'] < self.HOLD_SECS
            mode, mode_str = (3, 'charging') if charging else (4, 'charged')
            size, mah, secs = 'AA/AAA', int(st['mah']), int(now - st['start'])
            res = int(statistics.median(st['ir'])) if len(st['ir']) >= 3 else 0
            if h[-1] and ma < 0:            # probe pulse of a slot just emptied (not debounced yet): no discharge
                ma = 0
        return Sample(t=now, slot=slot, mode=mode, mode_str=mode_str, chem='', size=size, mv=mv, ma=ma,
                      res=res, mah=mah, secs=secs, temp=temp, itemp=0, progress=0, power=0, energy=0,
                      raw=p.hex())

    def input_mv(self) -> Optional[int]:
        return self.in_mv


DRIVERS = {d.model: d for d in (N8, NOld, C4, C4Evo, A4, UC4, A4Air)}


def open_charger(usb: UsbInfo) -> Charger:
    """Opens the HID device and identifies it. Raises ValueError for unknown models."""
    dev = get_device('ignore', 'app', path=usb.path.decode())
    try:
        dev.version()
        e1 = bytes(dev.read_packet() or b'')
        if len(e1) < 31 or e1[0] != 0xE1:
            raise ValueError(tr('no device info (0xE0) from {}').format(usb.product))
        model = e1[21:31].split(b'\0')[0].decode('ascii', 'replace').strip()
        drv = DRIVERS.get(model)
        if drv is None:
            raise ValueError(tr('model {} is not supported').format(repr(model)))
        return drv(usb, dev, e1)
    except Exception:
        try:
            dev.__device__.close()
        except Exception:
            pass
        raise
