"""SkyRC MC5000 / MC3000 through a BattBench-ESP bridge (ESP32 on the network): the ESP keeps the Bluetooth
connection to the charger and records every answer to the status query; BattBench fetches them over HTTP, also those
it missed while it was not running.

  GET /api/info                     -> logid, now (ESP clock, 0 = not set), src (charger index), chargers
                                       [{src, model, addr}]
  GET /api/records?since=N&max=M    -> {logid, more, records: [[seq, unix time or null, src, slot, packet hex], ...]}
  POST /api/time {"t": unix}        -> sets the ESP clock if it has none (no internet for SNTP)

Sequence numbers only grow; the newest one stored is kept in the database per log id (setting 'esp:<logid>'),
so a restart of BattBench continues where it stopped. The packets are the charger's own answers and are decoded
by device_skyrc, the charger gets the same key as over a direct Bluetooth connection (same device)."""
import json
import socket
import struct
import threading
import time
import urllib.request
from typing import Dict, Optional

from . import device_skyrc as skyrc
from .device import UsbInfo
from .i18n import tr

POLL_SECS = 2.0
TIMEOUT = 5
LOST_SECS = 300           # a charger no longer behind the ESP: its connection ends after this long without readings
BLE_SECS = 10             # how often the ESP's Bluetooth state (paused / off) is asked for
BATCH = 1000


def cursor_key(logid):
    return f'esp:{logid}'


class NetReader:
    """One charger behind an ESP. Looks like a USB Reader to the worker: .usb.path, .charger, .dev, .ready, .running."""

    def __init__(self, host, src, charger):
        self.usb = UsbInfo(path=f'net:{host}/{src}'.encode(), product='BattBench-ESP', serial='')
        self.host, self.src, self.charger = host, src, charger
        self.dev = None
        self.ready = threading.Event()
        self.running = True
        self.last_reading = time.time()

    def command(self, pkt: bytes, timeout=15) -> bytes:
        """Sends a charger command (program / start / stop / history, see mc5000) through the ESP, which sends it
        between two status queries; returns the charger's answer (b'' = none). Raises OSError when the ESP refuses
        it or the charger is not connected."""
        base = self.host if '://' in self.host else 'http://' + self.host
        req = urllib.request.Request(base + '/api/command', data=json.dumps({'hex': pkt.hex()}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return bytes.fromhex(json.loads(f.read().decode()).get('reply', ''))


class NetHost(threading.Thread):
    """Polls one ESP. Posts ('open', reader), ('data', reader, batch, None) and ('lost', reader, why) like the other
    readers, then ('cursor', key, seq) to store how far it got; readings come in the order the ESP took them."""

    def __init__(self, host, out, cursors: Dict[str, int]):
        super().__init__(daemon=True, name=f'esp-{host}')
        self.host = host.strip().rstrip('/')
        self.base = self.host if '://' in self.host else 'http://' + self.host
        self.out = out
        self.cursors = dict(cursors)      # cursor key -> last sequence number stored (from the database)
        self.running = True
        self.readers: Dict[int, NetReader] = {}
        self.info: Optional[dict] = None
        self.esp_id: Optional[str] = None   # MAC, as in the mDNS TXT item "id"
        self.auto = False                   # found by mDNS (not entered by hand)
        self.state = tr('connecting …')
        self.backlog = 0                  # readings fetched in the last round (> BATCH: catching up)
        self.ble: Optional[dict] = None   # Bluetooth part of /api/status (state, on, paused_secs) and t = asked at

    def stop(self):
        self.running = False

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=TIMEOUT) as f:
            return json.loads(f.read().decode())

    def _post(self, path, data):
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            return json.loads(f.read().decode())

    def _reader(self, src) -> Optional[NetReader]:
        """The reader of charger src, opened (device id from the worker) on first use."""
        r = self.readers.get(src)
        if r:
            return r
        c = next((c for c in (self.info or {}).get('chargers', []) if c['src'] == src), None)
        if not c or c['model'] not in ('MC5000', 'MC3000'):
            return None
        ch = skyrc.SkyCharger(c['addr'], c['model'])
        ch.version = f"{c['model']} (BattBench-ESP)"     # the address is shown with the connection
        r = NetReader(self.host, src, ch)
        self.out.put(('open', r))
        if not r.ready.wait(10):
            return None
        self.readers[src] = r
        return r

    def run(self):
        while self.running:
            t0 = time.time()
            try:
                self._round()
            except Exception as e:                      # ESP not reachable: the worker shows the chargers offline
                self.state = tr('not reachable: {}').format(e)
                self.info = None
                for r in self.readers.values():
                    self.out.put(('data', r, [], None))
            if self.backlog < BATCH:
                end = t0 + POLL_SECS
                while self.running and time.time() < end:
                    time.sleep(0.1)
        for r in self.readers.values():
            r.running = False
            self.out.put(('lost', r, tr('BattBench-ESP removed')))

    def ble_until(self, addr) -> Optional[float]:
        """Time until which this ESP leaves the charger with this address alone (inf = Bluetooth off), else None."""
        b = self.ble
        if not b or not any(c.get('addr', '').upper() == addr.upper() for c in (self.info or {}).get('chargers', [])):
            return None
        if b.get('state') == 'off' or b.get('on') is False:
            return float('inf')
        if b.get('state') == 'paused':
            return b['t'] + b.get('paused_secs', 0)
        return None

    def ble_connected(self, addr) -> bool:
        """The ESP holds the Bluetooth connection to the charger with this address."""
        info, b = self.info or {}, self.ble or {}
        cur = next((c for c in info.get('chargers', []) if c['src'] == info.get('src')), None)
        return b.get('state') == 'connected' and bool(cur) and cur.get('addr', '').upper() == addr.upper()

    def ask_ble(self):
        b = self._get('/api/status').get('ble', {})
        self.ble = dict(b, t=time.time())
        return b

    def _round(self):
        self.info = info = self._get('/api/info')
        if not self.ble or time.time() - self.ble['t'] >= BLE_SECS:
            try:
                self.ask_ble()
            except Exception:
                self.ble = None
        self.esp_id = info.get('id')
        key = cursor_key(info['logid'])
        if not info.get('now'):
            self._post('/api/time', {'t': time.time()})     # no SNTP in this network: BattBench sets the clock
        if info.get('connected') and info.get('src', -1) >= 0:
            self._reader(info['src'])                       # shown as connected even before its first reading
        since = self.cursors.get(key, 0)
        d = self._get(f'/api/records?since={since}&max={BATCH}')
        if d['logid'] != info['logid']:
            return                                          # cleared in between
        batches: Dict[int, list] = {}
        last = since
        for seq, t, src, slot, hexdata in d['records']:
            if t is None:
                if not info.get('now'):
                    break               # wait until the clock is set, then these readings get their time
                last = seq              # boot that never had the clock: cannot be placed in time
                continue
            r = self._reader(src)
            if r is None:
                last = seq
                continue
            s = r.charger.sample(slot, bytes.fromhex(hexdata), r.dev)
            if s:
                s.t = t
                batches.setdefault(src, []).append(s)
            last = seq
        self.backlog = len(d['records']) if d.get('more') else 0
        n = sum(len(b) for b in batches.values())
        now = time.time()
        for src, r in list(self.readers.items()):
            b = batches.get(src, [])
            if b:
                r.last_reading = now
            elif src != info.get('src') and now - r.last_reading > LOST_SECS:
                del self.readers[src]
                r.running = False
                self.out.put(('lost', r, tr('no longer connected to the BattBench-ESP')))
                continue
            self.out.put(('data', r, b, None))
        if last != since:
            self.cursors[key] = last
            self.out.put(('cursor', key, last))             # after the data: stored once it is in the database
        self.state = (tr('catching up ({} readings)').format(n) if d.get('more') else
                      tr('connected') if info.get('connected') else tr('ESP connected, charger not'))


# ----------------------------------------------------------------------------------------------- discovery (mDNS)
SERVICE = '_battbench._tcp.local'
FIND_SECS = 30


def _name(data, off):
    """DNS name at off (with compression) -> (name, offset after it)."""
    labels, end, jumps = [], None, 0
    while off < len(data):
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | data[off + 1]
            jumps += 1
            if jumps > 20:
                break
            continue
        labels.append(data[off + 1:off + 1 + n].decode(errors='replace'))
        off += 1 + n
    return '.'.join(labels), end if end is not None else off


def _query(name, qtype=12):
    q = struct.pack('>HHHHHH', 0, 0, 1, 0, 0, 0)
    for part in name.split('.'):
        q += bytes([len(part)]) + part.encode()
    return q + b'\0' + struct.pack('>HH', qtype, 0x8001)          # unicast response wanted (QU)


def _sweep_targets(ips):
    """Every address of the /24 networks of the private IPv4 addresses (for the unicast queries)."""
    out = []
    for ip in ips:
        a = socket.inet_aton(ip)
        if not (a[0] == 10 or (a[0] == 172 and 16 <= a[1] < 32) or (a[0] == 192 and a[1] == 168)):
            continue
        out += [socket.inet_ntoa(a[:3] + bytes([n])) for n in range(1, 255) if n != a[3]]
    return out


def discover(timeout=2.0, sweep=True):
    """BattBench-ESP bridges in the local network: {id: dict(host='ip:port', name=instance, fw=...)}.
    Sends an mDNS query for _battbench._tcp and reads the answers (PTR -> SRV / TXT / A). Multicast alone is not
    reliable: some routers filter it towards Wi-Fi clients or flood the network with it, and an ESP then misses the
    query. sweep: also ask every address of the own /24 networks directly (port 5353, answered by unicast)."""
    found = {}
    recs = {'srv': {}, 'txt': {}, 'a': {}, 'ptr': set()}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.bind(('', 0))
        s.settimeout(0.3)
        # on every network interface: the default one for multicast may be a virtual one (WSL, Hyper-V, VPN)
        try:
            ips = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        except OSError:
            ips = set()
        q = _query(SERVICE)
        for ip in sorted(ips) or [None]:
            try:
                if ip:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.sendto(q, ('224.0.0.251', 5353))
            except OSError:
                continue
        for target in _sweep_targets(ips) if sweep else ():
            try:
                s.sendto(q, (target, 5353))
            except OSError:
                continue
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, src = s.recvfrom(9000)
            except socket.timeout:
                continue
            try:
                _parse(data, src[0], recs)
            except (IndexError, struct.error, UnicodeError):
                continue
    finally:
        s.close()
    for inst in recs['ptr']:
        srv = recs['srv'].get(inst)
        txt = recs['txt'].get(inst, {})
        if not srv:
            continue
        target, port, sender = srv
        ip = recs['a'].get(target, sender)
        dev_id = txt.get('id') or f'{ip}:{port}'
        found[dev_id] = dict(host=f'{ip}:{port}' if port != 80 else ip, name=inst.split('.')[0], fw=txt.get('fw', ''))
    return found


def _parse(data, sender, recs):
    _id, flags, qd, an, ns, ar = struct.unpack('>HHHHHH', data[:12])
    if not flags & 0x8000:
        return                                          # a query, not an answer
    off = 12
    for _ in range(qd):
        _, off = _name(data, off)
        off += 4
    for _ in range(an + ns + ar):
        name, off = _name(data, off)
        rtype, _cls, _ttl, rdlen = struct.unpack('>HHIH', data[off:off + 10])
        off += 10
        rd = data[off:off + rdlen]
        if rtype == 12 and name.lower() == SERVICE:                     # PTR -> instance
            recs['ptr'].add(_name(data, off)[0])
        elif rtype == 33:                                               # SRV
            _prio, _weight, port = struct.unpack('>HHH', rd[:6])
            recs['srv'][name] = (_name(data, off + 6)[0], port, sender)
        elif rtype == 16:                                               # TXT
            items, i = {}, 0
            while i < len(rd):
                n = rd[i]
                k, _, v = rd[i + 1:i + 1 + n].decode(errors='replace').partition('=')
                items[k] = v
                i += 1 + n
            recs['txt'][name] = items
        elif rtype == 1 and rdlen == 4:                                 # A
            recs['a'][name] = socket.inet_ntoa(rd)
        off += rdlen


class EspFinder(threading.Thread):
    """Looks for BattBench-ESP bridges every FIND_SECS; found: {id: dict(host, name, fw)} of the last search."""

    def __init__(self):
        super().__init__(daemon=True, name='esp-finder')
        self.running = True
        self.found: Dict[str, dict] = {}
        self.last = 0.0

    def run(self):
        while self.running:
            try:
                self.found = discover()
            except OSError:
                self.found = {}
            self.last = time.time()
            end = time.time() + FIND_SECS
            while self.running and time.time() < end:
                time.sleep(0.2)
