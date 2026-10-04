"""ISDT Air chargers over Bluetooth LE (read-only). Protocol from mtheli/isdt_air_ble and
DittelHome/ISDT-Charge-Utility: service AF00, AF02 for hardware info / bind / stream start, AF01 for
the per-channel queries; every answer starts with 0x31.

  12 E4 ch -> 31 E5 ch, input mV (u16), input mA (u32), output mV (u16), charge mA (u32), ...
  13 E6 ch -> 31 E7 ch, state, %, mAh (u32), mWh (u32), period ms (u32), battery type, ...
  13 FA ch -> 31 FB ch, internal resistance (u16, A4 Air: 0.01 mOhm)

Over USB the A4 Air answers none of E6 / FA, so only Bluetooth gives the charger's own resistance,
state and capacity. Only these queries plus the bind handshake (as the ISD Link app does) are sent,
never a WorkTasks / setting command. Temperature is not available over Bluetooth."""
import asyncio
import struct
import sys
import threading
import time
import uuid
from typing import Optional

from . import device_skyrc as skyrc
from .device import Sample, UsbInfo
from .i18n import tr

AF00 = '0000af00-0000-1000-8000-00805f9b34fb'
AF01 = '0000af01-0000-1000-8000-00805f9b34fb'
AF02 = '0000af02-0000-1000-8000-00805f9b34fb'
STATE = {0: 'idle', 1: 'pre-charge', 2: 'CC', 3: 'charging', 4: 'CV', 5: 'error', 6: 'done'}
CHEM = {0: 'LiHV', 1: 'LiIon', 2: 'LiFe', 3: 'NiZn', 4: 'NiMH', 5: 'LiIon 1.5V', 6: 'auto'}
# name in the advertisement (spaces removed) -> label, slots, channels counted backwards, resistance unit in mOhm.
# From isdt_air_ble / ISDT-Charge-Utility; only the A4 Air is checked on a real charger.
MODELS = {'A4Air': ('A4 Air', 4, True, 0.01),       # channel 0 = slot 4
          'A8Air': ('A8 Air', 8, False, 0.01),
          'C4Air': ('C4 Air', 6, False, 0.1)}
SCAN_SECS = 15


class BleCharger:
    """Same interface the worker uses for USB chargers (key, model, label, slots, version, seed)."""
    def __init__(self, address, model, e1: bytes):
        self.address = address
        self.model = model
        self.label, self.slots, self.reverse, self.res_unit = MODELS[model]
        fw = f'{e1[3]}.{e1[4]}' if len(e1) >= 5 else '?'
        self.version = f'{self.label} FW {fw} (Bluetooth)'
        self.key = f'{self.model}-ble-{address}'

    def seed(self, slot, last):
        """The charger counts mAh itself."""


class BleReader:
    """Looks like a USB Reader to the worker: .usb.path, .charger, .dev, .ready, .running."""

    def __init__(self, address, out, model=''):
        self.usb = UsbInfo(path=f'ble:{address}'.encode(), product='Bluetooth', serial='')
        self.address, self.out, self.model = address, out, model
        self.charger: Optional[BleCharger] = None
        self.dev = None
        self.ready = threading.Event()
        self.running = True


class BleManager(threading.Thread):
    """Own asyncio loop: scans for ISDT Air chargers, keeps one connection per charger and polls
    all slots continuously. Posts ('open'|'data'|'lost'|'fail', reader, ...) like the USB readers."""

    def __init__(self, out, status):
        super().__init__(daemon=True)
        self.out = out
        self.status = status          # callable(str) for messages
        self.running = True
        self.active = {}              # address -> BleReader
        self.seen = {}                # address -> dict(name, rssi, t) of ISDT chargers found
        self.state = tr('starting …')  # short text for the info dialog
        self.last_scan = 0
        self.scan_now = threading.Event()
        self.ignore = set()           # models not to connect to (A4 Air when it is to be read over USB only)

    def stop(self):
        self.running = False
        for r in self.active.values():
            r.running = False

    def run(self):
        try:
            import bleak  # noqa: F401
        except ImportError:
            self.state = tr('off: Python package “bleak” is missing')
            self.status(tr('Bluetooth off (package bleak missing)'))
            return
        try:
            asyncio.run(self._main())
        except Exception as e:                  # no adapter etc.
            self.state = tr('not available: {}').format(e)
            self.status(tr('Bluetooth not available: {}').format(e))

    async def _main(self):
        from bleak import BleakScanner
        while self.running:
            self.state = tr('searching …')
            self.scan_now.clear()
            try:
                found = await BleakScanner.discover(timeout=5, return_adv=True)
                self.state = tr('ready')
            except Exception as e:
                self.state = tr('search failed: {}').format(e)
                self.status(tr('Bluetooth search failed: {}').format(e))
                found = {}
            self.last_scan = time.time()
            for addr, (d, adv) in found.items():
                raw_name = d.name or adv.local_name or ''
                name = raw_name.replace(' ', '')
                sky = skyrc.is_skyrc(raw_name)               # 'MC3000' / 'MC5000' / '?' (generic name)
                model = next((m for m in MODELS if m in name), None) or (sky and 'SkyRC')
                if model:
                    self.seen[addr] = dict(name=sky if sky in ('MC3000', 'MC5000') else model,
                                           rssi=adv.rssi, t=time.time())
                if model and addr not in self.active and model not in self.ignore:
                    r = BleReader(addr, self.out, model)
                    self.active[addr] = r
                    asyncio.create_task(skyrc.serve(self, r, sky) if sky else self._serve(r, model))
            end = time.time() + SCAN_SECS
            while self.running and time.time() < end and not self.scan_now.is_set():
                await asyncio.sleep(0.3)

    async def _serve(self, r: BleReader, model):
        from bleak import BleakClient
        kw = {}
        if sys.platform == 'win32':
            try:                                # A4/A8 rebuild their GATT table when a cell is inserted
                from bleak.args.winrt import WinRTClientArgs
                kw['winrt'] = WinRTClientArgs(use_cached_services=False)
            except ImportError:
                pass
        inbox = asyncio.Queue()

        def note(_h, data: bytearray):
            inbox.put_nowait(bytes(data))

        async def ask(char, cmd, match, timeout=1.5):
            await client.write_gatt_char(char, cmd, response=False)
            end = time.time() + timeout
            while time.time() < end:
                try:
                    d = await asyncio.wait_for(inbox.get(), max(0.01, end - time.time()))
                except asyncio.TimeoutError:
                    break
                if match(d):
                    return d
            return None

        opened = False
        try:
            async with BleakClient(r.address, timeout=20, **kw) as client:
                await asyncio.sleep(1.0)
                await client.start_notify(AF01, note)
                await client.start_notify(AF02, note)
                await asyncio.sleep(0.5)
                e1 = await ask(AF02, bytes([0xE0]), lambda d: d[:1] == b'\xe1' or d[1:2] == b'\xe1', 3)
                if e1 is None:
                    raise RuntimeError(tr('no device info'))
                e1 = e1[1:] if e1[:1] == b'\x31' else e1
                await ask(AF02, bytes([0x18]) + uuid.uuid4().bytes + b'\x00\x00', lambda d: b'\x19' in d[:2], 2)
                await client.write_gatt_char(AF02, bytes([0xE2]), response=False)     # start data stream
                await asyncio.sleep(0.2)
                r.charger = c = BleCharger(r.address, model, e1)
                self.out.put(('open', r))
                opened = True
                for _ in range(100):                                       # worker assigns the device id
                    if r.ready.is_set() or not r.running:
                        break
                    await asyncio.sleep(0.1)
                while r.running and self.running and client.is_connected:
                    t0 = time.time()
                    batch, in_mv = [], None
                    for slot in range(c.slots):
                        ch = c.slots - 1 - slot if c.reverse else slot
                        ws = await ask(AF01, bytes([0x13, 0xE6, ch]), lambda d: d[1:3] == bytes([0xE7, ch]))
                        el = await ask(AF01, bytes([0x12, 0xE4, ch]), lambda d: d[1:3] == bytes([0xE5, ch]))
                        ir = await ask(AF01, bytes([0x13, 0xFA, ch]), lambda d: d[1:3] == bytes([0xFB, ch]))
                        s = self._sample(r, slot, ws, el, ir)
                        if s:
                            batch.append(s)
                            if el and len(el) >= 5:
                                in_mv = struct.unpack_from('<H', el, 3)[0]
                    self.out.put(('data', r, batch, in_mv))
                    await asyncio.sleep(max(0.2, 2.0 - (time.time() - t0)))
        except Exception as e:
            if not opened:
                self.out.put(('fail', r, f'Bluetooth {r.address}: {e}'))
        finally:
            self.active.pop(r.address, None)
            if opened:
                self.out.put(('lost', r, tr('Bluetooth disconnected')))

    @staticmethod
    def _sample(r, slot, ws, el, ir) -> Optional[Sample]:
        if not ws or len(ws) < 18 or not el or len(el) < 15:
            return None
        state, pct, mah, mwh, period, btype = struct.unpack_from('<BBIIIB', ws, 3)
        _in_mv, _in_ma, mv, ma = struct.unpack_from('<HIHI', el, 3)
        ma = struct.unpack('<i', struct.pack('<I', ma))[0]
        res = 0
        if ir and len(ir) >= 5:
            raw = struct.unpack_from('<H', ir, 3)[0]
            res = round(raw * r.charger.res_unit) if 0 < raw < 0xFFFF else 0
        if state == 0:                                                   # nothing in the slot / idle
            mode, mode_str, size = 0, 'empty', 'empty'
        elif state == 6:
            mode, mode_str, size = 4, 'charged', 'AA/AAA'
        elif state == 5:
            mode, mode_str, size = 20, 'error', 'AA/AAA'
        else:
            mode, mode_str, size = 3, STATE.get(state, f'state {state}'), 'AA/AAA'
        return Sample(t=time.time(), slot=slot, mode=mode, mode_str=mode_str, chem=CHEM.get(btype, ''),
                      size=size, mv=mv, ma=ma, res=res, mah=mah if mode else 0, secs=period // 1000 if mode else 0,
                      temp=0, itemp=0, progress=pct, power=0, energy=mwh,
                      raw=(ws + b'|' + el + b'|' + (ir or b'')).hex(), dev=r.dev or 0)
