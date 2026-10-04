"""SkyRC MC3000 and MC5000 over Bluetooth LE, read only. Both use service FFE0 / characteristic FFE1
(write without response + notify) and a sum checksum. Protocol from kolinger/skyrc-mc3000,
cross-checked with rssdev10/skyrc-mc-rs (BLE captures of the MC5000).

MC5000: 0F 03 91 <bit> cs (bit 01/02/04/08 = slot 1-4), cs = sum(91, bit)
  -> 0F 15 91 bit, mA (u16 BE, no sign), mV (u16 BE), temp (u16 BE, 1/1000 deg C), mAh (u16 BE),
     seconds (u32 BE), mOhm (u16 BE), status, mode, error, battery type, cs
MC3000: 0F 55 <slot 0-3> 00 ... (20 bytes), last byte = sum of the others
  -> 0F 55 slot, type, mode, cycle count, status, seconds (u16 BE), mV, mA, mAh (u16 BE), temp (deg C),
     mOhm (u16 BE), LED bits, ..., cs
Only these status queries are sent. Never: the MC5000 app's handshake / init sequence 57 / 74 / 65 / FE
(restarts running tasks with reset counters, FE even stops them), start/stop (93), settings (94, MC3000 11).
Not checked on a real charger."""
import asyncio
import time
from typing import Optional

from .device import Sample
from .i18n import QT_TRANSLATE_NOOP as N, tr

FFE1 = '0000ffe1-0000-1000-8000-00805f9b34fb'
SLOTS = 4
POLL_SECS = 2.0

# operation (the charger's mode name) -> task mode id of the app (model.TASKS) and its "done" id (model.DONE)
TASK = {'Charge': 3, 'Discharge': 5, 'Storage': 7, 'Cycle': 9, 'Refresh': 9, 'Break in': 13}
DONE = {3: 4, 5: 6, 7: 8, 9: 10, 13: 10}

MC5000_TYPES = {0: 'LiIon', 1: 'LiHV', 2: 'LiFe', 3: 'NiMH', 4: 'NiCd', 5: 'Eneloop', 6: 'NiZn', 7: 'RAM',
                8: 'LTO', 9: 'NaIon'}
MC5000_MODES = [((0, 1, 2, 8, 9), ('Charge', 'Storage', 'Discharge', 'Cycle')),
                ((3, 4, 5), ('Charge', 'Refresh', 'Break in', 'Discharge', 'Cycle')),
                ((6, 7), ('Charge', 'Discharge', 'Cycle'))]
# error texts end up in Sample.mode_str (stored in English, translated when shown)
MC5000_ERRORS = {1: N('data', 'input voltage too low'), 2: N('data', 'input voltage too high'),
                 3: N('data', 'contact lost'), 4: N('data', 'capacity limit reached'),
                 5: N('data', 'time limit reached'), 6: N('data', 'internal temperature too high'),
                 7: N('data', 'calibration failed'), 8: N('data', 'internal resistance too high'),
                 9: N('data', 'contact lost'), 10: N('data', 'wrong battery type'), 11: N('data', 'overload'),
                 12: N('data', 'reversed polarity')}                   # 13 'fully charged' = done
MC3000_TYPES = {0: 'LiIon', 1: 'LiFe', 2: 'LiHV', 3: 'NiMH', 4: 'NiCd', 5: 'NiZn', 6: 'Eneloop', 7: 'RAM',
                8: 'LTO'}
MC3000_MODES = [((0, 1, 2, 8), ('Charge', 'Refresh', 'Storage', 'Discharge', 'Cycle')),
                ((5, 7), ('Charge', 'Refresh', 'Discharge', 'Cycle')),
                ((3, 4, 6), ('Charge', 'Refresh', 'Break in', 'Discharge', 'Cycle'))]
MC3000_ERRORS = {128: N('data', 'input voltage too low'), 129: N('data', 'input voltage too high'),
                 130: N('data', 'ADC error 1'), 131: N('data', 'ADC error 2'), 132: N('data', 'contact lost'),
                 133: N('data', 'check voltage'), 134: N('data', 'capacity limit reached'),
                 135: N('data', 'time limit reached'), 136: N('data', 'charger too hot'),
                 137: N('data', 'battery too hot'), 138: N('data', 'short circuit'),
                 139: N('data', 'reversed polarity'), 140: N('data', 'internal resistance too high')}


def checksum(b) -> int:
    return sum(b) & 0xFF


def is_skyrc(name: str) -> Optional[str]:
    """'MC3000' / 'MC5000' when the advertised name says so, '?' for the generic names the chargers use
    (skyrc-mc-rs: '#Charger…', 'Charger FF-…'), else None."""
    n = name.lower()
    if 'mc3000' in n:
        return 'MC3000'
    if 'mc5000' in n:
        return 'MC5000'
    if n.startswith('#charger') or (n.startswith('charger') and ('ff-' in n or 'ff34' in n)):
        return '?'
    return None


def _operation(modes, btype, mode):
    names = next((m for types, m in modes if btype in types), ())
    return names[mode] if mode < len(names) else ''


def _sample(slot, dev, raw, *, mv, ma, mah, secs, res, temp, chem, op, state, error=''):
    """state: 'empty' / 'idle' / 'run' / 'down' (discharging) / 'done' / 'error'."""
    task = TASK.get(op, 3)
    if state == 'empty':
        mode, mode_str, size = 0, 'empty', 'empty'
    elif state == 'idle':
        mode, mode_str, size = 0, 'idling', ''
    elif state == 'done':
        mode, mode_str, size = DONE[task], f'{op or "task"} done', ''
    elif state == 'error':
        mode, mode_str, size = 20, error or 'error', ''
    else:
        mode, mode_str, size = task, op or 'running', ''
    down = state == 'down'
    return Sample(t=time.time(), slot=slot, mode=mode, mode_str=mode_str, chem=chem, size=size, mv=mv,
                  ma=-ma if down else ma, res=0 if res in (1, 0xFFFF) else res, mah=-mah if down else mah,
                  secs=secs if mode else 0, temp=temp, itemp=0, progress=0, power=0, energy=0, raw=raw.hex(),
                  dev=dev or 0)


class SkyCharger:
    """Same interface the worker uses for USB chargers (key, model, label, slots, version, seed)."""
    slots = SLOTS

    def __init__(self, address, model):
        self.address = address
        self.model = self.label = model
        self.version = f'{model} (Bluetooth)'
        self.key = f'{model}-ble-{address}'

    def seed(self, slot, last):
        """The charger counts mAh and time itself."""

    def request(self, slot) -> bytes:
        if self.model == 'MC5000':
            bit = 1 << slot
            return bytes([0x0F, 0x03, 0x91, bit, checksum([0x91, bit])])
        p = bytearray([0x0F, 0x55, slot]) + bytes(16)
        return bytes(p + bytes([checksum(p)]))

    def matches(self, slot, p: bytes) -> bool:
        if self.model == 'MC5000':
            # notifications may be cut to 20 bytes (default MTU); then status / mode / error / type are
            # missing and there is no checksum to check
            return len(p) >= 18 and p[0] == 0x0F and p[2] == 0x91 and p[3] == 1 << slot and \
                (len(p) < 23 or checksum(p[2:22]) == p[22])
        return len(p) >= 19 and p[0] == 0x0F and p[1] == 0x55 and p[2] == slot and checksum(p[:-1]) == p[-1]

    def sample(self, slot, p: bytes, dev) -> Optional[Sample]:
        if not self.matches(slot, p):
            return None
        if self.model == 'MC5000':
            ma, mv, temp, mah = (int.from_bytes(p[i:i + 2], 'big') for i in (4, 6, 8, 10))
            secs, res = int.from_bytes(p[12:16], 'big'), int.from_bytes(p[16:18], 'big')
            status, mode, err, btype = (p + bytes(4))[18:22] if len(p) < 22 else p[18:22]
            if len(p) < 21:                      # cut notification: no status -> charging when current flows
                status = 2 if ma else 0
            op = _operation(MC5000_MODES, btype, mode)
            state = ('empty' if mv == 0 else 'idle') if status == 0 else \
                'done' if status in (5, 6) or err == 13 else 'error' if err else \
                'down' if status == 3 else 'run'
            return _sample(slot, dev, p, mv=mv, ma=ma, mah=mah, secs=secs, res=res, temp=temp // 1000,
                           chem=MC5000_TYPES.get(btype, ''), op=op, state=state, error=MC5000_ERRORS.get(err, ''))
        btype, mode, status = p[3], p[4], p[6]
        secs, mv, ma, mah = (int.from_bytes(p[i:i + 2], 'big') for i in (7, 9, 11, 13))
        temp, res = p[15], int.from_bytes(p[16:18], 'big')
        op = _operation(MC3000_MODES, btype, mode)
        state = ('empty' if mv == 0 else 'idle') if status == 0 else 'done' if status == 4 else \
            'error' if status >= 128 else 'down' if status == 2 else 'run'             # 3 = pause
        return _sample(slot, dev, p, mv=mv, ma=ma, mah=mah, secs=secs, res=res, temp=temp,
                       chem=MC3000_TYPES.get(btype, ''), op=op, state=state, error=MC3000_ERRORS.get(status, ''))


async def serve(manager, r, hint):
    """Like BleManager._serve for the ISDT Air chargers: connect, (if the name does not tell) find out whether
    it is an MC3000 or MC5000 by which status query it answers, then poll all slots."""
    from bleak import BleakClient
    inbox = asyncio.Queue()

    def note(_h, data: bytearray):
        inbox.put_nowait(bytes(data))

    async def ask(c, slot, timeout=1.5):
        await client.write_gatt_char(FFE1, c.request(slot), response=False)
        end = time.time() + timeout
        while time.time() < end:
            try:
                d = await asyncio.wait_for(inbox.get(), max(0.01, end - time.time()))
            except asyncio.TimeoutError:
                break
            if c.matches(slot, d):
                return d
        return None

    opened = False
    try:
        async with BleakClient(r.address, timeout=20) as client:
            await asyncio.sleep(1.0)
            await client.start_notify(FFE1, note)
            await asyncio.sleep(0.3)
            c = None
            for model in ([hint] if hint in ('MC3000', 'MC5000') else ['MC5000', 'MC3000']):
                probe = SkyCharger(r.address, model)
                if await ask(probe, 0, 2.5):
                    c = probe
                    break
            if c is None:
                raise RuntimeError(tr('no answer to the status query (not an MC3000 / MC5000?)'))
            r.charger = c
            r.out.put(('open', r))
            opened = True
            for _ in range(100):                 # worker assigns the device id
                if r.ready.is_set() or not r.running:
                    break
                await asyncio.sleep(0.1)
            while r.running and manager.running and client.is_connected:
                t0 = time.time()
                batch = []
                for slot in range(SLOTS):
                    p = await ask(c, slot)
                    s = c.sample(slot, p, r.dev) if p else None
                    if s:
                        batch.append(s)
                    await asyncio.sleep(0.1)     # the MC3000 app waits 100 ms between queries
                r.out.put(('data', r, batch, None))
                await asyncio.sleep(max(0.2, POLL_SECS - (time.time() - t0)))
    except Exception as e:
        if not opened:
            r.out.put(('fail', r, f'SkyRC {r.address}: {e}'))
    finally:
        manager.active.pop(r.address, None)
        if opened:
            r.out.put(('lost', r, tr('Bluetooth disconnected')))
