"""BattBench: live view of all slots of all connected chargers (N8, A4Air; several at once), full curves,
session database and battery rating.

Start:  .venv\\Scripts\\python -m battbench [--offline] [--db battbench.db]   (or run.bat / run.sh)
  (default) read all chargers directly (USB / Bluetooth), samples go straight into the database
  --offline only view the database
  --a4 usb|bt|both  how the A4 Air is read (default: last choice in the app, else both); --no-bt: no Bluetooth at all
  --lang auto|en|de user interface language (default: last choice in the app, else the system language)
"""
import argparse
import os
import sqlite3
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import (QByteArray, QEvent, QLocale, QObject, QPointF, QRectF, QRegularExpression, QSettings,
                            QSize, QSortFilterProxyModel, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap,
                           QRegularExpressionValidator)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDialog,
                               QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProxyStyle, QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleFactory, QStyleOptionViewItem, QTabWidget, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from . import __version__
from .version import AUTHOR, SOURCE, WEBSITE, full_version
from .autofilter import FILTER_ROLE, SEARCH_ROLE, SORT_ROLE, AutoFilter, SortItem
from .db import COMPRESS_AFTER, DB, SESSION_FIELDS, close_and_backup
from .device import UsbInfo, open_charger, usb_devices
from .device_ble import BleManager
from . import i18n
from .i18n import LANGUAGES, install, pick_language, tr, tr_data
from .model import DONE as DONE_MODES
from .model import (ABORTED, CHARGE, DISCHARGE, FINISHED, GAP_SECS, GRADES, MODE_NAMES, NO_GRADE, NOMINAL,
                    PHASE_MIN_MA, REMOVED, RES_QUALITY, RUNNING, TASKS, Session, Tracker, measuring, note_text,
                    rate_values, res_level)

PKG = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(PKG, 'resources', 'battbench.svg')


def user_db():
    """The installed app's database: %LOCALAPPDATA%\\BattBench\\battbench.db (~/.local/share/battbench elsewhere)."""
    if sys.platform == 'win32':
        folder = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'BattBench')
    else:
        folder = os.path.join(os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share'), 'battbench')
    return os.path.join(folder, 'battbench.db')


def default_db():
    """Database unless --db is given: $BATTBENCH_DB; else the installed app's one (run from source too, if it
    exists); run from source without it: battbench.db in the project folder."""
    if os.environ.get('BATTBENCH_DB'):
        return os.environ['BATTBENCH_DB']
    path = user_db()
    if getattr(sys, 'frozen', False) or os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path
    return os.path.join(os.path.dirname(PKG), 'battbench.db')


# how the A4 Air is read; 'both': Bluetooth while connected, USB otherwise
A4_MODES = ('both', 'bt', 'usb')


def a4_mode_names():
    return {'both': tr('Automatic (Bluetooth preferred, else USB)'), 'bt': tr('Bluetooth only'),
            'usb': tr('USB only')}


COLORS = {'charge': '#f28c28', 'discharge': '#e75480', 'analysis': '#3a7bd5', 'done': '#2e9e4f',
          'empty': '#9a9a9a', 'error': '#d0342c'}
GRADE_COLORS = dict(zip(GRADES + (NO_GRADE,), ('#2e9e4f', '#6aa84f', '#e69138', '#d0342c', '#d0342c', '#888888')))
PHASE_COLORS = {CHARGE: COLORS['charge'], DISCHARGE: COLORS['discharge']}


def mode_color(mode, ma):
    if mode == 0:
        return COLORS['empty']
    if mode in (2, 20):
        return COLORS['error']
    if mode in (4, 6, 8, 10, 12, 14):
        return COLORS['done']
    if mode in (9, 11, 13):
        return COLORS['analysis']
    return COLORS['discharge'] if ma < 0 else COLORS['charge']


def fmt_t(t):
    return datetime.fromtimestamp(t).strftime(tr('%b %d, %H:%M')) if t else ''


# connection symbols, drawn in the text colour of the current palette (light and dark theme)
VIA_SVG = {
    'usb': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v15M12 14l-5-3V8M12 16l5-3v-3"/>'
           '<path d="M10 5.5 12 2.5l2 3z" fill="{c}"/><circle cx="7" cy="6.5" r="1.6" fill="{c}"/>'
           '<rect x="15.5" y="7" width="3" height="3" fill="{c}"/><circle cx="12" cy="19.5" r="2" fill="{c}"/></svg>',
    'ble': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"><path d="M7 7l10 10-5 5V2l5 5L7 17"/></svg>',
}
VIA_NAME = {'usb': 'USB', 'ble': 'Bluetooth'}


def via_pixmap(vias, color, size=14):
    """USB and / or Bluetooth symbol side by side."""
    pm = QPixmap(size * len(vias) + 2 * max(len(vias) - 1, 0), size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    for i, v in enumerate(vias):
        QSvgRenderer(QByteArray(VIA_SVG[v].replace('{c}', color).encode())).render(
            p, QRectF(i * (size + 2), 0, size, size))
    p.end()
    return pm


def fmt_int(n):
    """Whole number with the thousands separator of the UI language (661,139 / 661.139)."""
    return QLocale(i18n.current).toString(int(n))


def fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else QLocale(i18n.current).toString(float(n), 'f', 1) + ' ' + unit
        n /= 1024


def fmt_day(t):
    return datetime.fromtimestamp(t).strftime(tr('%Y-%m-%d')) if t else ''


def fmt_dur(secs):
    secs = int(secs)
    return f'{secs // 3600}:{secs % 3600 // 60:02d} h'


def is_measuring(d):
    """Discharge of a session dict still running (no rating yet). Without the phases (table rows) the stored
    rating tells: a running session with a discharge capacity but no grade is being measured."""
    if d.get('phases') is not None:
        return measuring(d['status'], [ph[0] for ph in d['phases']])
    return d['status'] == RUNNING and bool(d['discharge_mah']) and (d['grade'] or NO_GRADE) == NO_GRADE


def note_of(d):
    """Rating note of a session dict (live or from the DB) in the UI language."""
    nominal = d['nominal'] or NOMINAL.get(d['size'], 0)
    return note_text(rate_values(d['discharge_mah'], nominal, d['charge_mah'], d['res_min'], d['status'],
                                 is_measuring(d), d['chem'])[1], tr_data)


def session_info(s: Session):
    """Plain dict of a live session, safe to send to the GUI thread."""
    grade, note = s.rating()
    return dict(id=s.db_id, dev=s.dev, slot=s.slot, start=s.start, end=s.end, task=s.task, chem=s.chem, size=s.size,
                status=s.status, nominal=s.nominal or NOMINAL.get(s.size, 0), discharge_mah=s.discharge_mah,
                charge_mah=s.charge_mah, res_first=s.res_first, res_min=s.res_min, res_last=s.res_last,
                temp_max=s.temp_max, grade=grade, note=note,
                phases=[(p.kind, p.start, p.end, p.mah) for p in s.phases])


# ======================================================================= worker
def via_of(r):
    """'ble' for a Bluetooth reader, 'usb' for a USB one."""
    return 'ble' if r.usb.path.startswith(b'ble:') else 'usb'


class Reader(threading.Thread):
    """One per connected charger: opens it, then reads all slots once per second and hands the
    samples to the worker. Only USB I/O here; the worker owns tracker and database."""

    def __init__(self, usb: UsbInfo, out: queue.Queue):
        super().__init__(daemon=True)
        self.usb, self.out = usb, out
        self.running = True
        self.ready = threading.Event()        # set by the worker once the device id is known
        self.charger = None
        self.dev = None

    def run(self):
        try:
            self.charger = open_charger(self.usb)
        except Exception as e:
            self.out.put(('fail', self, str(e)))
            return
        self.out.put(('open', self))
        if not self.ready.wait(10):
            self.charger.close()
            return
        c = self.charger
        while self.running:
            t0 = time.time()
            try:
                batch = []
                for slot in range(c.slots):
                    for _ in range(2):                  # one retry for a lost answer
                        s = c.read_slot(slot)
                        if s:
                            s.dev = self.dev
                            batch.append(s)
                            break
                self.out.put(('data', self, batch, c.input_mv()))
            except Exception as e:                      # unplugged / glitch -> worker reconnects
                self.out.put(('lost', self, repr(e)))
                break
            end = t0 + 1.0
            while self.running and time.time() < end:
                time.sleep(0.05)
        c.close()


class Worker(QObject):
    """Runs in its own thread: finds the chargers (one Reader each), stores samples, tracks sessions.
    Owns its own DB connection."""
    cycle = Signal(object)        # dict(live={(dev, slot): sample}, sessions={(dev, slot): info}, devices={dev: …})
    status = Signal(str)
    sessions_changed = Signal()
    data_changed = Signal()       # readings compressed: statistics and curves are out of date

    def __init__(self, db_path, source, a4='both', bt=True):
        super().__init__()
        self.db_path = db_path
        self.source = source      # 'device' / 'offline'
        self.a4 = a4              # A4_MODES key: how the A4 Air is read
        self.bt = bt              # look for Bluetooth chargers (A4 / A8 / C4 Air, MC5000)
        self.ble = None
        self.running = True
        self.cmds = queue.Queue()
        self.inbox = queue.Queue()
        self.readers = {}         # HID path -> Reader
        self.skip = {}            # HID path -> reason (unsupported model / open failed) until unplugged
        self.online = {}          # dev -> dict(version, in_mv, via, vias) of connected chargers
        self.conns = {}           # dev -> {'usb' / 'ble': reader}: a charger may be connected both ways
        self.same = {}            # (dev, dev) -> deque of reading comparisons (True = same readings)
        self.merged = []          # devices merged into another one since the last emit (for the GUI)

    def stop(self):
        self.running = False

    # ---------------------------------------------------------------- setup
    def run(self):
        self.db = DB(self.db_path)
        self.tracker = Tracker()
        self.status.emit(tr('Loading database …'))
        self.db.resume(self.tracker)
        self.latest = dict(self.tracker.last)
        self.sessions_changed.emit()
        self._emit()
        self.next_compress = 0
        if self.source == 'offline':
            self.status.emit(tr('No chargers (database only)'))
            while self.running:
                self._commands()
                self._auto_compress()
                time.sleep(0.2)
        else:
            self._run()
        self.db.con.close()

    # ---------------------------------------------------------------- main loop
    def _run(self):
        """Reads every charger directly."""
        nxt_scan = nxt_save = last_emit = 0
        stored_empty = {}
        dirty = False
        self._status()
        self._apply_a4()
        while self.running:
            self._commands()
            self._auto_compress()
            now = time.time()
            if now >= nxt_scan:
                nxt_scan = now + 3
                self._scan()
            try:
                msg = self.inbox.get(timeout=0.2)
            except queue.Empty:
                msg = None
            if msg:
                kind, r = msg[0], msg[1]
                if kind == 'open':
                    c = r.charger
                    r.dev = self.db.device_for(c.key, c.model, c.label, c.slots, c.version)
                    for slot in range(c.slots):
                        last = self.tracker.last.get((r.dev, slot))
                        if last and now - last.t < GAP_SECS:
                            c.seed(slot, last)
                    self.conns.setdefault(r.dev, {})[via_of(r)] = r
                    self._update_online(r.dev)
                    r.ready.set()
                    self._status()
                    self.sessions_changed.emit()        # new device name in the tables
                elif kind == 'data':
                    _, _, batch, in_mv = msg
                    if r.dev is None or self.conns.get(r.dev, {}).get(via_of(r)) is not r:
                        continue                        # reader of a device that was merged / dropped
                    self._compare(r.dev, batch)
                    if self.online[r.dev]['via'] != via_of(r):
                        continue                        # second connection: data comes from the better one
                    store = []
                    for s in batch:
                        key = (s.dev, s.slot)
                        self.latest[key] = s
                        self.tracker.feed(s)
                        # keep the DB small: idle slots only once (needed to close sessions on resume)
                        if not s.empty or not stored_empty.get(key):
                            store.append(s)
                        stored_empty[key] = s.empty
                    self.db.add_samples(store)
                    if r.dev in self.online:
                        self.online[r.dev]['in_mv'] = in_mv
                    dirty = True
                else:                                   # 'fail' / 'lost'
                    self.readers.pop(r.usb.path, None)
                    if kind == 'fail':
                        self.skip[r.usb.path] = msg[2]
                    self._drop(r)
                    why = tr('connection lost') if kind == 'lost' else msg[2]
                    self._status(f"{r.charger.label if r.charger else r.usb.product}: {why}")
                    dirty = True
            if self.tracker.closed or now >= nxt_save:  # closed sessions: save at once
                self.db.save_dirty(self.tracker)
                nxt_save = now + 10
                self.sessions_changed.emit()
            if dirty and now - last_emit >= 0.5:
                self.db.con.commit()
                self._emit()
                dirty, last_emit = False, now
        for r in self.readers.values():
            r.running = False
        if self.ble:
            self.ble.stop()
        self.db.save_dirty(self.tracker)

    def _scan(self):
        try:
            found = {u.path: u for u in usb_devices()}
        except Exception as e:
            self.status.emit(tr('USB error: {}').format(repr(e)))
            return
        for path in list(self.skip):                    # unplugged -> try again when it comes back
            if path not in found:
                del self.skip[path]
        for path, usb in found.items():
            if path in self.readers or path in self.skip:
                continue
            if usb.product == 'CH57x' and self.a4 == 'bt':
                continue                                # A4 Air: Bluetooth only
            r = Reader(usb, self.inbox)
            self.readers[path] = r
            r.start()

    def _apply_a4(self):
        """Bluetooth runs for all Bluetooth chargers (unless --no-bt); self.a4 only decides how the A4 Air is
        read: 'usb' -> no Bluetooth connection to it, 'bt' -> no USB reader for it."""
        if self.bt and not (self.ble and self.ble.is_alive()):
            self.ble = BleManager(self.inbox, lambda text: self._status(text))
            self.ble.start()
        if self.ble:
            self.ble.ignore = {'A4Air'} if self.a4 == 'usb' else set()
            for r in list(self.ble.active.values()):
                if r.model in self.ble.ignore:
                    r.running = False                   # its connection posts 'lost' when it ends
        if self.a4 == 'bt':
            for r in [x for x in self.readers.values() if x.usb.product == 'CH57x']:
                r.running = False
                self.readers.pop(r.usb.path, None)
                self._drop(r)
        self._status()

    # ---------------------------------------------------------------- connections (USB / Bluetooth)
    def _update_online(self, dev):
        """online[dev] from its connections; data is taken from Bluetooth when there is both (more values)."""
        conns = self.conns.get(dev)
        if not conns:
            self.online.pop(dev, None)
            self.conns.pop(dev, None)
            return
        via = 'ble' if 'ble' in conns else 'usb'
        old = self.online.get(dev, {})
        self.online[dev] = dict(version=conns[via].charger.version, in_mv=old.get('in_mv'), via=via,
                                vias=sorted(conns))

    def _drop(self, r):
        if r.dev is not None and self.conns.get(r.dev, {}).get(via_of(r)) is r:
            del self.conns[r.dev][via_of(r)]
            self._update_online(r.dev)

    SAME_WINDOW, SAME_HITS, SAME_MV = 10, 7, 60

    def _compare(self, dev, batch):
        """Is a charger connected over one way the same one as a device of the same model connected over the
        other way? Compare the voltages of the occupied slots; 7 of the last 10 polls equal (+-60 mV) -> same
        charger: the younger device is merged into the older one."""
        mine = self.online.get(dev)
        if not mine or len(mine['vias']) != 1 or not batch:
            return
        model = self.conns[dev][mine['via']].charger.model
        for other, on in list(self.online.items()):
            if other == dev or on['vias'] == mine['vias'] or len(on['vias']) != 1 or \
                    self.conns[other][on['via']].charger.model != model:
                continue
            pairs = [(s, self.latest.get((other, s.slot))) for s in batch]
            pairs = [(a, b) for a, b in pairs if b and abs(a.t - b.t) < 10]
            full = [(a, b) for a, b in pairs if not a.empty and not b.empty and a.mv and b.mv]
            if not full:
                continue                                # nothing to compare (all slots empty)
            ok = all(abs(a.mv - b.mv) <= self.SAME_MV for a, b in full)
            hist = self.same.setdefault(tuple(sorted((dev, other))), deque(maxlen=self.SAME_WINDOW))
            hist.append(ok)
            if sum(hist) >= self.SAME_HITS:
                self._merge(max(dev, other), min(dev, other))
                return

    def _merge(self, src, dst):
        """src is the same charger as dst: one device with both connections."""
        self.db.save_dirty(self.tracker)
        for (d, slot) in [k for k in self.tracker.current if k[0] == src]:
            cur = self.tracker.current.pop((d, slot))
            if (dst, slot) not in self.tracker.current:
                cur.dev, cur.dirty = dst, True
                self.tracker.current[(dst, slot)] = cur
        for k in [k for k in self.tracker.last if k[0] == src]:
            self.tracker.last.pop(k)
            self.latest.pop(k, None)
        self.db.merge_devices(src, dst)
        for via, r in self.conns.pop(src, {}).items():
            r.dev = dst
            self.conns.setdefault(dst, {})[via] = r
        self.online.pop(src, None)
        self._update_online(dst)
        self.same = {k: v for k, v in self.same.items() if src not in k}
        self.merged.append(src)
        self._status(tr('{}: recognised over USB and Bluetooth').format(self.online[dst]['version']))
        self.sessions_changed.emit()

    def _status(self, extra=''):
        n = len(self.online)
        text = (tr('1 charger connected') if n == 1 else tr('{} chargers connected').format(n) if n else
                tr('no charger connected'))
        self.status.emit(text + (f' · {extra}' if extra else ''))

    # ---------------------------------------------------------------- helpers
    def _emit(self):
        cur = {k: session_info(s) for k, s in self.tracker.current.items()}
        live = {k: asdict(s) for k, s in self.latest.items()}
        self.cycle.emit(dict(live=live, sessions=cur, devices={k: dict(v) for k, v in self.online.items()},
                             merged=self.merged))
        self.merged = []

    def _auto_compress(self):
        """Setting 'compress': readings older than two weeks down to one per minute, at start and once a day."""
        if time.time() < self.next_compress:
            return
        self.next_compress = time.time() + 86400
        if QSettings('battbench', 'battbench').value('compress', False, type=bool):
            self._compress(time.time() - COMPRESS_AFTER, optimize=False)

    def _compress(self, before, optimize):
        self.db.save_dirty(self.tracker)
        n_in, n_out = self.db.compress_samples(
            before, lambda k, n: self.status.emit(tr('Compressing readings … {} %').format(100 * k // max(n, 1))))
        if optimize:
            self.status.emit(tr('Optimising database …'))
            self.db.optimize()
        self.status.emit(tr('Readings compressed: {} → {}').format(n_in, n_out) if n_in else
                         tr('No readings to compress'))
        self.data_changed.emit()

    def _commands(self):
        """Edits from the GUI, applied here so the running tracker stays consistent:
        ('meta', session id, battery id, nominal) and ('battery', battery id) after a battery was edited."""
        changed = False
        while True:
            try:
                cmd = self.cmds.get_nowait()
            except queue.Empty:
                break
            if cmd[0] == 'meta':
                _, sid, bid, nominal = cmd
                self.db.set_session_meta(sid, battery_id=bid)
                self._set_nominal(sid, nominal)
            elif cmd[0] == 'compress':                    # button "compress all now"
                self._compress(time.time() - 3600, optimize=True)
            elif cmd[0] == 'a4':
                self.a4 = cmd[1]
                if self.source != 'offline':
                    self._apply_a4()
            elif cmd[0] == 'battery':
                b = self.db.battery(cmd[1])
                if b and b['capacity']:
                    for r in self.db.battery_sessions(cmd[1]):
                        self._set_nominal(r[0], b['capacity'])
            changed = True
        if changed:
            self.sessions_changed.emit()
            self._emit()

    def _set_nominal(self, sid, nominal):
        live = next((s for s in self.tracker.sessions() if s.db_id == sid), None)
        if live:
            live.nominal = nominal
            live.dirty = True
            self.db.save_dirty(self.tracker)
        self.db.set_session_meta(sid, nominal=nominal)
        self.db.rerate(sid)


# ======================================================================= widgets
class ProgressBar(QWidget):
    """Charger's progress (%) with the slot colour. While charging, chevrons run towards 100 %, while discharging
    towards 0 %; standing still when done or idle."""
    STEP = 12                             # chevron spacing in px
    FPS = 25

    def __init__(self):
        super().__init__()
        self.value, self.color, self.direction, self.phase = 0, QColor(COLORS['empty']), 0, 0.0
        self.setFixedHeight(16)
        self.timer = QTimer(self)
        self.timer.setInterval(1000 // self.FPS)
        self.timer.timeout.connect(self._tick)

    def set_state(self, value, color, direction):
        """direction: +1 charging, -1 discharging, 0 no animation."""
        self.value, self.color, self.direction = max(0, min(100, value)), QColor(color), direction
        self._run()
        self.update()

    def _run(self):
        on = self.direction != 0 and self.isVisible()
        if on != self.timer.isActive():
            self.timer.start() if on else self.timer.stop()

    def _tick(self):
        self.phase = (self.phase + self.direction * self.STEP / self.FPS) % self.STEP     # one chevron per second
        self.update()

    def showEvent(self, e):
        self._run()

    def hideEvent(self, e):
        self.timer.stop()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        pal = self.palette()
        p.setPen(QColor('#999999'))
        p.setBrush(pal.base())
        p.drawRoundedRect(r, 3, 3)
        fill = QRectF(r.left(), r.top(), r.width() * self.value / 100, r.height())
        p.save()
        p.setClipRect(fill)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawRoundedRect(r, 3, 3)
        if self.direction:                                # chevrons pointing in the running direction
            pen = QPen(QColor(255, 255, 255, 80), 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            h, d = r.height(), self.direction * 3
            x = r.left() - self.STEP + self.phase
            while x < fill.right() + self.STEP:
                p.drawPolyline([QPointF(x - d, r.top() + 4), QPointF(x + d, r.top() + h / 2),
                                QPointF(x - d, r.bottom() - 4)])
                x += self.STEP
        p.restore()
        text = f'{self.value} %'
        f = p.font()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        p.setFont(f)
        for clip, color in ((fill, QColor('white')), (QRectF(fill.right(), r.top(), r.width(), r.height()),
                                                      pal.text().color())):
            p.save()
            p.setClipRect(clip)
            p.setPen(color)
            p.drawText(r, Qt.AlignCenter, text)
            p.restore()
        p.end()


def bar_direction(s):
    """+1 charging, -1 discharging, 0 done / error / no current."""
    if s['mode'] in (2, 20) or s['mode'] in DONE_MODES:
        return 0
    return 1 if s['ma'] >= PHASE_MIN_MA else -1 if s['ma'] <= -PHASE_MIN_MA else 0


class SlotTile(QFrame):
    clicked = Signal(object)              # (device id, slot)

    def __init__(self, key, compact=False):
        super().__init__()
        self.key = key
        self.compact = compact            # narrow tile (N16 / N24): values one per line
        self.approx_res = False           # resistance is only an estimate (A4Air)
        slot = key[1]
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)     # all columns equally wide
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(2)
        self.head = QLabel(f'{slot + 1}' if compact else tr('Slot {}').format(slot + 1))
        self.head.setAlignment(Qt.AlignCenter)
        f = QFont()
        f.setBold(True)
        self.head.setFont(f)
        lay.addWidget(self.head)
        self.mode = QLabel('–')
        self.mode.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.mode)
        self.big = QLabel('')
        self.big.setAlignment(Qt.AlignCenter)
        fb = QFont()
        fb.setPointSize(10 if compact else 13)
        fb.setBold(True)
        self.big.setFont(fb)
        lay.addWidget(self.big)
        self.bar = ProgressBar()              # charger's progress (%), shown once a task reports it
        self.bar.hide()
        bl = QHBoxLayout()
        bl.setContentsMargins(6, 0, 6, 0)
        bl.addWidget(self.bar)
        lay.addLayout(bl)
        self.lines = QLabel('')
        self.lines.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lines)
        self.set_selected(False)
        self.set_color(COLORS['empty'])

    def set_color(self, color):
        self.color = color
        self.head.setStyleSheet(f'background:{color}; color:white; padding:3px;')

    def set_selected(self, sel):
        self.setStyleSheet('SlotTile { border: 3px solid #3a7bd5; }' if sel else
                           'SlotTile { border: 3px solid transparent; }')

    def update_sample(self, s):
        if s is None:
            self.mode.setText(tr('no data'))
            self.big.setText('')
            self.lines.setText('')
            self.bar.hide()
            self.set_color(COLORS['empty'])
            return
        self.set_color(mode_color(s['mode'], s['ma']))
        if s['mode'] == 0:
            self.mode.setText(tr_data(MODE_NAMES[0]))
            self.big.setText('')
            self.lines.setText(f"{s['temp']} °C" if s['temp'] else '')
            self.bar.hide()
            return
        phase = ''
        if s['mode'] in (9, 11, 13) and s['ma']:
            phase = ' · ' + tr_data(MODE_NAMES[3] if s['ma'] > 0 else MODE_NAMES[5])
        typ = ' '.join(x for x in (s['chem'], s['size']) if x not in ('auto', 'unknown', 'empty'))
        self.mode.setText(f"{mode_name(s)}{phase}\n{typ}")
        self.big.setText(f"{s['mv'] / 1000:.3f} V")
        extra = [f"{'≈' if self.approx_res else ''}{s['res']} mΩ" if s['res'] else '– mΩ']
        if s['temp']:
            extra.append(f"{s['temp']} °C")
        if self.compact:
            self.lines.setText('\n'.join([f"{s['ma']} mA", f"{s['mah']} mAh", ' · '.join(extra),
                                          fmt_dur(s['secs'])]))
        else:
            self.lines.setText(f"{s['ma']} mA · {s['mah']} mAh\n{' · '.join(extra + [fmt_dur(s['secs'])])}")
        if s['progress'] or not self.bar.isHidden():        # chargers without % never show the bar
            self.bar.show()
            self.bar.set_state(s['progress'] or 0, self.color, bar_direction(s))

    def mousePressEvent(self, e):
        self.clicked.emit(self.key)


# home symbol of the chart's reset button
HOME_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="48" height="48" fill="none" '
            'stroke="#444" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">'
            '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9.5h5v-6h3v6h5V10"/></svg>')


def fmt_hms(secs):
    secs = int(round(secs))
    return f'{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}' if secs >= 0 else ''


class AxisViewBox(pg.ViewBox):
    """View box of a right-hand value axis: lies over the chart but passes every mouse event on to the chart
    (a plain ViewBox accepts all drags even with its mouse disabled, so the chart would never get them)."""

    def __init__(self):
        super().__init__(enableMenu=False)
        self.setMouseEnabled(x=False, y=False)
        self.setAcceptedMouseButtons(Qt.NoButton)

    def mouseDragEvent(self, ev, axis=None):
        ev.ignore()

    def mouseClickEvent(self, ev):
        ev.ignore()

    def wheelEvent(self, ev, axis=None):
        ev.ignore()


class ChartViewBox(pg.ViewBox):
    """Mouse in the chart: a drag with the left button draws a frame and zooms to its time range on release; once
    zoomed in, a drag in the lower two thirds scrolls through time instead. The value axes follow automatically."""

    def __init__(self):
        super().__init__(enableMenu=False)
        self.setAcceptHoverEvents(True)

    def _zoomed_in(self):
        """Less than the whole data range is shown (only then there is something to scroll)."""
        lo, hi = self.state['limits']['xLimits']
        if lo is None or hi is None:
            return False
        x0, x1 = self.viewRange()[0]
        return (x1 - x0) < (hi - lo) * 0.99

    def _in_frame_zone(self, pos):
        """Frame (zoom) instead of scrolling: in the upper third, or anywhere while fully zoomed out."""
        return pos.y() < self.height() / 3 or not self._zoomed_in()

    def hoverEvent(self, ev):
        if not ev.isExit():
            self.setCursor(Qt.CrossCursor if self._in_frame_zone(ev.pos()) else Qt.OpenHandCursor)

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.RightButton:                       # zoom out by 2 around the mouse (limits apply)
            ev.accept()
            self.scaleBy(x=2, center=self.mapToView(ev.pos()))
        else:
            super().mouseClickEvent(ev)

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() != Qt.LeftButton:
            return super().mouseDragEvent(ev, axis)
        ev.accept()
        start = ev.buttonDownPos()
        if self._in_frame_zone(start):
            a, b = sorted((start.x(), ev.pos().x()))
            if ev.isFinish():
                self.rbScaleBox.hide()
                if b - a > 3:                                   # a real frame, not a slip of the mouse
                    self.setXRange(self.mapToView(QPointF(a, 0)).x(), self.mapToView(QPointF(b, 0)).x(),
                                   padding=0)
            else:                                               # frame over the whole height
                self.updateScaleBox(QPointF(a, 0), QPointF(b, self.height()))
        else:
            self.setCursor(Qt.ClosedHandCursor if not ev.isFinish() else Qt.OpenHandCursor)
            dx = self.mapToView(ev.pos()).x() - self.mapToView(ev.lastPos()).x()
            self.translateBy(x=-dx)


class DurationAxis(pg.AxisItem):
    """x = seconds since the start of the session, labelled h:mm:ss at round steps."""
    STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800)

    def tickSpacing(self, min_val, max_val, size):
        span = max_val - min_val
        if span <= 0:
            return super().tickSpacing(min_val, max_val, size)
        labels = max(size / 90, 1)                                # about 90 px per label
        i = next((i for i, s in enumerate(self.STEPS) if span / s <= labels), len(self.STEPS) - 1)
        major = self.STEPS[i]
        minor = next((s for s in reversed(self.STEPS[:i]) if major % s == 0 and major / s <= 6), major)
        return [(major, 0), (minor, 0)]

    def tickStrings(self, values, scale, spacing):
        return [fmt_hms(v) for v in values]


class CurvePlot(pg.GraphicsLayoutWidget):
    """One chart of the whole session: voltage (left axis), current, mAh counter, internal resistance and
    temperature (axes on the right), each axis in the colour of its curve; x = duration. Phases shaded, legend on
    top, crosshair with values. Zooming / panning works on the time axis, the value axes follow the visible range."""
    phase_changed = Signal(object)        # start time of the phase shown alone, None = whole run (chart click)
    SERIES = [('V', '#d62728'), ('mA', '#2ca02c'), ('mAh', '#1f77b4'), ('mΩ', '#9467bd'), ('°C', '#222222')]
    MIN_SPAN = {'°C': 10}
    ZERO = (1, 2)                     # current and mAh counter: signed, zero line drawn, zeros at the same height
    MIN_ZOOM = 60                     # s: zooming in stops at one minute (zooming out at the length of the data)
    GRID = '#9a9a9a'

    def __init__(self):
        super().__init__()
        self.setBackground('w')
        names = [tr('Voltage'), tr('Current'), tr('mAh counter'), tr('Internal resistance'), tr('Temperature')]
        self.names = names
        self.regions = []
        self.data = None
        self.t0 = 0
        self.plot = p = pg.PlotItem(viewBox=ChartViewBox(), axisItems={'bottom': DurationAxis('bottom')})
        # rows: title, legend (outside the data area, so it never covers a curve), chart with its axes
        self.title_label = pg.LabelItem('', color='#222', size='11pt')
        self.ci.addItem(self.title_label, row=0, col=0, colspan=len(self.SERIES))
        self.ci.addItem(p, row=2, col=0)
        self.vb = p.vb
        self.vb.setMouseEnabled(x=True, y=False)
        self.vb.setAutoVisible(y=True)
        p.setMenuEnabled(False)
        p.hideButtons()
        p.showGrid(x=True, y=True, alpha=0.6)
        grid_pen = pg.mkPen(self.GRID, width=1, style=Qt.DashLine)
        bottom = p.getAxis('bottom')
        bottom.setPen(pg.mkPen('#444'))
        bottom.setTextPen(pg.mkPen('#222'))
        bottom.setTickPen(grid_pen)
        bottom.setLabel(tr('Duration (h:mm:ss)'), color='#222')
        self.vbs, self.curves = [self.vb], []
        for i, ((unit, color), name) in enumerate(zip(self.SERIES, names)):
            if i == 0:
                vb, axis = self.vb, p.getAxis('left')
                axis.setTickPen(grid_pen)
            else:
                vb = AxisViewBox()
                vb.setAutoVisible(y=True)
                vb.setZValue(-1)
                vb.setXLink(self.vb)
                self.scene().addItem(vb)
                axis = pg.AxisItem('right')
                axis.setTickPen(pg.mkPen(color))
                axis.linkToView(vb)
                self.ci.addItem(axis, row=2, col=i)
                self.vbs.append(vb)
            axis.setPen(pg.mkPen(color))
            axis.setTextPen(pg.mkPen(color))
            axis.setLabel(f'{name} ({unit})', color=color)
            axis.setWidth(58)
            axis.enableAutoSIPrefix(False)
            curve = pg.PlotDataItem(pen=pg.mkPen(color, width=1.6), connect='finite')
            curve.setDownsampling(auto=True, method='peak')
            curve.setClipToView(True)
            vb.addItem(curve)
            if unit in self.MIN_SPAN:                         # 1 °C steps must not fill the whole height
                vb.setLimits(minYRange=self.MIN_SPAN[unit])
            self.curves.append(curve)
        self.zero_lines = {}
        for k in self.ZERO:
            line = pg.InfiniteLine(pos=0, angle=0, movable=False, pen=pg.mkPen('#666666', width=1))
            self.vbs[k].addItem(line, ignoreBounds=True)
            self.zero_lines[k] = line
        self.vb.sigResized.connect(self._sync)
        self.vb.sigXRangeChanged.connect(lambda *_: self._align_zero())
        self.legend = pg.LegendItem(colCount=len(self.SERIES), brush=pg.mkBrush(255, 255, 255, 210),
                                    labelTextColor='#222')
        self.legend.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)      # as wide as its entries, centred
        self.ci.addItem(self.legend, row=1, col=0, colspan=len(self.SERIES))
        self.ci.layout.setAlignment(self.legend, Qt.AlignHCenter)
        self.axes = [p.getAxis('left')] + [self.ci.getItem(2, i) for i in range(1, len(self.SERIES))]
        for i, (curve, name) in enumerate(zip(self.curves, names)):
            self.legend.addItem(curve, name)
            sample, label = self.legend.items[i]
            # like Grafana: hovering a legend entry highlights the value, a click shows / hides it; its axis does the same
            for item in (sample, label, self.axes[i]):
                item.mouseClickEvent = lambda ev, i=i: (ev.accept(), self.toggle(i))
                item.setCursor(Qt.PointingHandCursor)
                item.setAcceptHoverEvents(True)
                item.hoverEvent = lambda ev, i=i: self.highlight(None if ev.isExit() else i)
        self.focus = None                     # series under the mouse (curve or legend entry): the others are dimmed
        hidden = QSettings('battbench', 'battbench').value('hidden_series', '')
        self.shown = [unit not in str(hidden).split(',') for unit, _c in self.SERIES]
        for i in range(len(self.SERIES)):
            self._show_series(i)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#555', style=Qt.DashLine))
        self.vb.addItem(self.vline, ignoreBounds=True)
        self.vline.hide()
        self.info = pg.TextItem(anchor=(0, 0), fill=pg.mkBrush(255, 255, 255, 225), border=pg.mkPen('#bbb'))
        self.info.setZValue(20)
        self.vb.addItem(self.info, ignoreBounds=True)
        self.info.hide()
        self.scene().sigMouseMoved.connect(self._mouse)
        self.scene().sigMouseClicked.connect(self._clicked)
        self.phase_sel = None                 # start time of the phase shown alone (click on a shaded phase)
        self.session_start = None
        self._raw = ([], [], '')
        self.reset_btn = QToolButton(self)    # bottom left, below the voltage axis; only while zoomed / hidden
        self.reset_btn.setIcon(QIcon(QPixmap.fromImage(QImage.fromData(QByteArray(HOME_SVG.encode())))))
        self.reset_btn.setIconSize(QSize(20, 20))
        self.reset_btn.setAutoRaise(True)
        self.reset_btn.setToolTip(tr('Reset view'))
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.clicked.connect(self.reset_view)
        self.reset_btn.hide()
        self.vb.sigXRangeChanged.connect(lambda *_: self._update_reset())

    def toggle(self, i):
        self.shown[i] = not self.shown[i]
        self._show_series(i)
        self._align_zero()
        self._update_reset()
        QSettings('battbench', 'battbench').setValue(
            'hidden_series', ','.join(unit for (unit, _c), on in zip(self.SERIES, self.shown) if not on))

    DIM = 0.4

    def highlight(self, k):
        """k: series to emphasise (others at 40 % opacity: curve, axis, legend entry); None = all normal."""
        if k != self.focus:
            self.focus = k
            for i in range(len(self.SERIES)):
                self._show_series(i)

    def _show_series(self, i):
        """Curve and (right-hand) axis visible or hidden; the legend entry stays, greyed out when hidden.
        Dimmed while another series is highlighted."""
        on = self.shown[i]
        dim = self.DIM if self.focus not in (None, i) else 1
        for item in (self.curves[i], self.axes[i]):
            item.setOpacity(dim)
        self.curves[i].setVisible(on)
        if i in self.zero_lines:
            self.zero_lines[i].setVisible(on)
            self.zero_lines[i].setOpacity(dim)
        if i:
            self.axes[i].setVisible(on)
            self.axes[i].setMaximumWidth(58 if on else 0)          # no empty gap for a hidden axis
        else:
            color = self.SERIES[0][1] if on else '#bbbbbb'
            self.axes[0].setTextPen(pg.mkPen(color))
            self.axes[0].setPen(pg.mkPen(color))
        unit, color = self.SERIES[i]
        bold = {'font-weight': 'bold'} if self.focus == i else {}
        self.axes[i].setLabel(f'{self.names[i]} ({unit})', color=color if on else '#bbbbbb', **bold)
        sample, label = self.legend.items[i]
        label.setText(self.names[i], color='#222' if on else '#aaaaaa', bold=self.focus == i)
        label.setOpacity(dim)
        sample.setOpacity((1 if on else 0.3) * dim)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not hasattr(self, 'reset_btn'):    # resized while being built
            return
        self.reset_btn.adjustSize()
        self.reset_btn.move(6, self.height() - self.reset_btn.height() - 6)

    def reset_view(self):
        """Whole time range and all values shown again."""
        for i, on in enumerate(self.shown):
            if not on:
                self.toggle(i)
        self.vb.enableAutoRange()
        for k, vb in enumerate(self.vbs):
            if k not in self.ZERO:
                vb.enableAutoRange(axis='y')
        self._align_zero()
        self._update_reset()

    def _update_reset(self):
        """The home button only while there is something to reset: zoomed in or a value hidden."""
        zoomed = False
        if self.data is not None and len(self.data[0]) > 1:
            x0, x1 = self.vb.viewRange()[0]
            span = self.data[0][-1] - self.data[0][0]
            zoomed = span > 0 and (x1 - x0) < span * 0.99
        self.reset_btn.setVisible(bool(zoomed) or not all(self.shown))

    def _sync(self):
        """The extra view boxes cover exactly the main one."""
        for vb in self.vbs[1:]:
            vb.setGeometry(self.vb.sceneBoundingRect())
            vb.linkedViewChanged(self.vb, vb.XAxis)

    def clear_data(self, title=''):
        self.set_data([], [], title)

    def _clicked(self, ev):
        """Click on a shaded phase: show only that phase (time from 0:00:00); click again: the whole run."""
        if ev.button() != Qt.LeftButton or ev.double() or self.data is None or \
                not self.vb.sceneBoundingRect().contains(ev.scenePos()) or \
                self.legend.sceneBoundingRect().contains(ev.scenePos()):
            return
        if self.phase_sel is not None:
            self.phase_sel = None
        else:
            t = self.vb.mapSceneToView(ev.scenePos()).x() + self.t0
            hit = [ph for ph in self._raw[1] if ph[1] <= t <= ph[2]]
            if not hit:
                return
            self.phase_sel = hit[0][1]
        self.set_data(*self._raw)
        self.phase_changed.emit(self.phase_sel)

    def show_phase(self, start):
        """Show only the phase starting at start (None: the whole run); from the phase list."""
        if start != self.phase_sel:
            self.phase_sel = start
            self.set_data(*self._raw)

    def set_data(self, rows, phases, title='', keep_view=False):
        """rows: (t, mv, ma, res, mah, temp, mode) from DB.samples; phases: (kind, start, end, mah)."""
        start = rows[0][0] if rows else None
        if start != self.session_start:              # another session: whole run again
            self.session_start, self.phase_sel = start, None
            keep_view = False
        self._raw = (rows, phases, title)
        phase = next((ph for ph in phases if ph[1] == self.phase_sel), None) if self.phase_sel is not None else None
        if phase:
            kind, a0, b0, _mah = phase
            rows = [r for r in rows if a0 <= r[0] <= b0]
            phases = [phase]
            title = title + ' · ' + tr('phase: {} (click: whole run)').format(tr_data(kind))
        vr = self.vb.viewRange()[0] if keep_view and self.data is not None else None
        if rows:
            a = np.array(rows, dtype=float)
            if not vr:
                self.t0 = phase[1] if phase else a[0, 0]
            x = a[:, 0] - self.t0
            res = a[:, 3].copy()
            res[res <= 0] = np.nan
            temp = a[:, 5].copy()
            temp[temp <= 0] = np.nan                  # 0 = not reported (e.g. A4 Air over Bluetooth)
            self.data = (x, a[:, 1] / 1000, a[:, 2], a[:, 4], res, temp)
        else:
            self.data = None
        follow = vr is not None and self.data is not None and vr[1] >= self._last_x - 5
        for i, curve in enumerate(self.curves):
            if self.data is None:
                curve.setData([], [])
            else:
                curve.setData(self.data[0], self.data[i + 1])
        for r in self.regions:
            self.vb.removeItem(r)
        self.regions = []
        for kind, a, b, _mah in phases:
            col = QColor(PHASE_COLORS.get(kind, '#999999'))
            col.setAlpha(35)
            r = pg.LinearRegionItem(values=(a - self.t0, b - self.t0), movable=False, brush=QBrush(col),
                                    pen=pg.mkPen(None))
            r.setZValue(-10)
            self.vb.addItem(r, ignoreBounds=True)
            self.regions.append(r)
        self.title_label.setText(title, color='#222', size='11pt')
        if self.data is not None:
            self._last_x = self.data[0][-1]
        if self.data is not None and len(self.data[0]) > 1:
            # zoom and pan only within the data: out to its whole length, in to MIN_ZOOM
            x0, x1 = float(self.data[0][0]), float(self.data[0][-1])
            span = max(x1 - x0, 1.0)
            self.vb.setLimits(xMin=x0, xMax=x1, minXRange=min(self.MIN_ZOOM, span), maxXRange=span)
        else:
            self.vb.setLimits(xMin=None, xMax=None, minXRange=None, maxXRange=None)
        if vr is None:
            self.vb.enableAutoRange()
        elif follow:                                 # live view: keep the zoom width, scroll along
            w = vr[1] - vr[0]
            self.vb.setXRange(self._last_x - w + 1, self._last_x + 1, padding=0)
        for k, vb in enumerate(self.vbs):
            if k not in self.ZERO:
                vb.enableAutoRange(axis='y')
        self._align_zero()

    _last_x = 0

    def _align_zero(self):
        """Value ranges of current and mAh counter (visible part of the run) so that both zeros lie on one height:
        each axis is [-p * s, (1 - p) * s] with a common share p below zero; p is picked to waste least space."""
        if self.data is None:
            return
        xs = self.data[0]
        x0, x1 = self.vb.viewRange()[0]
        a, b = np.searchsorted(xs, [x0, x1])
        spans = []
        for k in self.ZERO:
            ys = self.data[k + 1][max(a - 1, 0):b + 1]
            ys = ys[~np.isnan(ys)]
            if not self.shown[k]:
                continue
            up = max(float(ys.max()), 0) if len(ys) else 0
            down = max(-float(ys.min()), 0) if len(ys) else 0
            spans.append((k, up or (0 if down else 1), down))

        def scales(p):
            return [max(down / p, up / (1 - p)) for _k, up, down in spans]

        shares = [min(max(down / (up + down), 0.05), 0.95) for _k, up, down in spans]
        if not shares:
            return
        p = min(shares, key=lambda p: sum(sc / (up + down) for sc, (_k, up, down) in zip(scales(p), spans)))
        for sc, (k, _up, _down) in zip(scales(p), spans):
            sc *= 1.06                               # a little room above / below
            self.vbs[k].setYRange(-p * sc, (1 - p) * sc, padding=0)

    def _near(self, pos, x, px=6):
        """Series whose curve passes within px pixels of the mouse, or None."""
        xs = self.data[0]
        w = self.vb.viewPixelSize()[0] * px
        a, b = np.searchsorted(xs, [x - w, x + w])
        best, best_d = None, px
        for k in range(len(self.SERIES)):
            ys = self.data[k + 1][max(a - 1, 0):b + 1]           # the segments around the mouse
            ys = ys[~np.isnan(ys)]
            if not self.shown[k] or not len(ys):
                continue
            top = self.vbs[k].mapViewToScene(QPointF(x, ys.max())).y()
            bottom = self.vbs[k].mapViewToScene(QPointF(x, ys.min())).y()
            d = 0 if top <= pos.y() <= bottom else min(abs(pos.y() - top), abs(pos.y() - bottom))
            if d < best_d:
                best, best_d = k, d
        return best

    def _mouse(self, pos):
        if self.data is None or not self.vb.sceneBoundingRect().contains(pos):
            self.info.hide()
            self.vline.hide()
            self.highlight(None)
            return
        x = self.vb.mapSceneToView(pos).x()
        if not self.legend.sceneBoundingRect().contains(pos):      # over the legend its entries decide
            self.highlight(self._near(pos, x))
        xs = self.data[0]
        i = int(np.clip(np.searchsorted(xs, x), 0, len(xs) - 1))
        self.vline.setPos(xs[i])
        self.vline.show()
        stamp = datetime.fromtimestamp(self.t0 + xs[i]).strftime(tr('%b %d, %H:%M:%S'))
        values = [f'{self.data[1][i]:.3f} V', f'{self.data[2][i]:.0f} mA', f'{self.data[3][i]:.0f} mAh',
                  '– mΩ' if np.isnan(self.data[4][i]) else f'{self.data[4][i]:.0f} mΩ',
                  '– °C' if np.isnan(self.data[5][i]) else f'{self.data[5][i]:.0f} °C']
        lines = ''.join(f"<br><span style='color:{color}'>{f'<b>{v}</b>' if k == self.focus else v}</span>"
                        for k, (v, (_u, color), on) in enumerate(zip(values, self.SERIES, self.shown)) if on)
        self.info.setHtml(f"<span style='color:#222'><b>{fmt_hms(xs[i])}</b> &nbsp; {stamp}</span>{lines}")
        self.info.show()
        # next to the mouse: right below it, or left / above it where the chart ends
        gap, box, frame = 14, self.info.boundingRect(), self.vb.sceneBoundingRect()
        sx = pos.x() + gap if pos.x() + gap + box.width() <= frame.right() else pos.x() - gap - box.width()
        sy = pos.y() + gap if pos.y() + gap + box.height() <= frame.bottom() else pos.y() - gap - box.height()
        self.info.setPos(self.vb.mapSceneToView(QPointF(max(sx, frame.left()), max(sy, frame.top()))))


BATTERY_TYPES = ['NiMH AA', 'NiMH AAA', 'NiMH LSD AA (eneloop type)', 'NiMH LSD AAA (eneloop type)',
                 'NiMH C', 'NiMH D', 'NiMH 9V', 'NiCd AA', 'NiCd AAA', 'NiZn AA', 'NiZn AAA',
                 'Li-Ion 14500', 'Li-Ion 18650', 'Li-Ion 21700', 'Li-Ion 26650', 'LiFePO4 14500',
                 'LiFePO4 18650', 'LiHV 18650', 'Other']


def mode_name(sample):
    """Mode of a live sample in the UI language (charger's own text for unknown modes / errors)."""
    return tr_data(MODE_NAMES.get(sample['mode']) if sample['mode'] != 20 or not sample['mode_str'] or
                   sample['mode_str'] == 'error' else sample['mode_str'])


def battery_text(bid, name):
    return f'#{bid} {name}' if bid is not None else ''


def model_text(maker, name, cap=0, typ=''):
    extra = ', '.join(x for x in (f'{cap} mAh' if cap else '', typ) if x)
    return f'{maker} {name}'.strip() + (f'  ({extra})' if extra else '')


def dialog_buttons(dlg, ok):
    bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    bb.accepted.connect(ok)
    bb.rejected.connect(dlg.reject)
    return bb


class _WordFilter(QSortFilterProxyModel):
    """Keeps the rows that contain every typed word (any order, case-insensitive)."""
    words = []

    def set_text(self, text):
        self.words = text.lower().split()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        text = self.sourceModel().index(row, 0, parent).data().lower()
        return all(w in text for w in self.words)


class ViewStyle(QProxyStyle):
    """The application style without its row panel: the Windows 11 style draws an accent bar into every cell of a
    selected row there (ListDelegate draws the selection instead: background and one bar at the row start)."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PE_PanelItemViewRow:
            super().drawPrimitive(element, option, painter, widget)


_view_style = None


def view_style():
    """One shared ViewStyle (a widget does not own the style set on it, so it must be kept alive here)."""
    global _view_style
    if _view_style is None:
        _view_style = ViewStyle(QStyleFactory.create(QApplication.style().name()))
    return _view_style


def plain_selection(view):
    """Lists and tables: selection drawn by ListDelegate only."""
    view.setItemDelegate(ListDelegate(view))
    view.setStyle(view_style())


class ListDelegate(QStyledItemDelegate):
    """Selected entries of lists and table rows: light background and a thin accent bar 1 px from the left edge of
    the first column, with room before the text (the Windows 11 style draws a thick bar right against the text of
    every cell)."""
    BAR, GAP = 2, 1

    def paint(self, p, option, index):
        o = QStyleOptionViewItem(option)
        self.initStyleOption(o, index)
        selected = bool(o.state & QStyle.State_Selected)
        hover = bool(o.state & QStyle.State_MouseOver)
        o.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver | QStyle.State_HasFocus)
        accent = o.palette.highlight().color()
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if selected or hover:
            fill = QColor(accent)
            fill.setAlpha(45 if selected else 22)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(o.rect).adjusted(1, 1, -1, -1), 3, 3)
        if selected and index.column() == 0:
            r = o.rect
            p.setBrush(accent)
            p.drawRoundedRect(QRectF(r.left() + self.GAP, r.top() + r.height() * 0.25, self.BAR, r.height() * 0.5), 1, 1)
        p.restore()
        if index.column() == 0:
            o.rect = o.rect.adjusted(self.GAP + self.BAR + 5, 0, 0, 0)
        widget = option.widget
        (widget.style() if widget else QApplication.style()).drawControl(QStyle.CE_ItemViewItem, o, p, widget)


class EditCombo(QComboBox):
    """Editable dropdown whose text gets selected when the field is entered (click or Tab), so typing replaces it.
    A click into the field opens the list (the completer's popup, so typing still goes to the field)."""

    def __init__(self):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(20)
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, obj, e):
        if obj is self.lineEdit():
            if e.type() == QEvent.FocusIn and e.reason() != Qt.MouseFocusReason:
                QTimer.singleShot(0, obj.selectAll)
            elif e.type() == QEvent.MouseButtonRelease and e.button() == Qt.LeftButton:
                obj.selectAll()                             # every click (after it placed the cursor): whole text
                self.open_list()                            # selected and the list open
        return super().eventFilter(obj, e)

    def open_list(self):
        comp = self.completer()
        if comp and self.isEnabled():
            comp.setCompletionPrefix('')
            comp.complete()


class SearchCombo(EditCombo):
    """Dropdown with all entries; typing filters it (e.g. "ene pro aaa"). Entries keep their item data,
    a typed text that matches nothing is reverted to the current entry."""

    def __init__(self):
        super().__init__()
        self.proxy = _WordFilter(self)
        self.proxy.setSourceModel(self.model())
        self.comp = QCompleter(self.proxy, self)
        self.comp.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.comp.setMaxVisibleItems(20)
        plain_selection(self.comp.popup())
        self.setCompleter(self.comp)          # QComboBox maps a picked popup row back through the proxy itself
        self.lineEdit().textEdited.connect(self._typed)
        self.lineEdit().editingFinished.connect(self._revert)
        self.lineEdit().setPlaceholderText(tr('type to search …'))

    def open_list(self):
        self.proxy.set_text('')                             # all entries
        super().open_list()

    def _typed(self, text):
        self.proxy.set_text(text)
        self.comp.complete()

    def _picked(self, index):
        row = self.proxy.mapToSource(index).row()
        self.proxy.set_text('')
        self.setCurrentIndex(row)
        self.setEditText(self.itemText(row))
        self.lineEdit().selectAll()

    def _revert(self):
        if self.currentText() != self.itemText(self.currentIndex()):
            if self.proxy.rowCount() >= 1 and self.proxy.words:     # Enter: take the first match
                self._picked(self.proxy.index(0, 0))
            else:
                self.setEditText(self.itemText(self.currentIndex()))
        self.proxy.set_text('')


class NominalCombo(EditCombo):
    """Nominal capacity: suggestions 500-3000 mAh in steps of 100, any number can be typed. committed(mAh) is
    emitted when a value is picked or the field is left with Enter / Tab / a click elsewhere; 0 = not set."""
    committed = Signal(int)

    def __init__(self):
        super().__init__()
        for v in range(500, 3001, 100):
            self.addItem(f'{v} mAh', v)
        # click: all suggestions; typing: only matching ones, none preselected, so "24" + Enter stays 24
        self.comp = QCompleter(self.model(), self)
        self.comp.setCompletionMode(QCompleter.PopupCompletion)
        self.comp.setMaxVisibleItems(15)
        plain_selection(self.comp.popup())
        self.setCompleter(self.comp)
        self.comp.activated[str].connect(self._picked)
        self.lineEdit().setValidator(QRegularExpressionValidator(QRegularExpression(r'\s*\d{0,5}\s*(m|mA|mAh)?\s*')))
        self.last = 0
        self.activated.connect(lambda _i: self._commit())
        self.lineEdit().editingFinished.connect(self._commit)

    def open_list(self):
        if self.isEnabled():
            self.comp.setCompletionPrefix('')
            self.comp.complete()

    def _picked(self, text):
        self.setEditText(text)
        self._commit()

    def value(self):
        digits = ''.join(c for c in self.currentText() if c.isdigit())
        return int(digits) if digits else 0

    def set_value(self, mah, placeholder):
        self.last = mah or 0
        self.lineEdit().setPlaceholderText(placeholder)
        self.setCurrentIndex(-1)
        self.setEditText(f'{mah} mAh' if mah else '')

    def _commit(self):
        v = self.value()
        self.setEditText(f'{v} mAh' if v else '')
        if v != self.last:
            self.last = v
            self.committed.emit(v)


class ModelDialog(QDialog):
    """Create / edit one battery model: maker, model, type, capacity, note."""

    def __init__(self, parent, db: DB, model=None, prefill=None):
        super().__init__(parent)
        self.db = db
        self.result_id = None
        self.batteries = []
        self.setWindowTitle(tr('Edit model') if model else tr('New model'))
        self.m = model or {**dict(id=None, maker='', name='', type='NiMH AA', capacity=0, note=''), **(prefill or {})}
        form = QFormLayout(self)
        self.maker = QComboBox()
        self.maker.setEditable(True)
        self.maker.addItems([''] + db.makers())
        self.maker.setCurrentText(self.m['maker'])
        self.maker.lineEdit().setPlaceholderText(tr('e.g. Panasonic'))
        self.name = QLineEdit(self.m['name'])
        self.name.setPlaceholderText(tr('e.g. eneloop pro AA'))
        self.type = QComboBox()
        self.type.setEditable(True)
        self.type.addItems(BATTERY_TYPES)
        self.type.setCurrentText(self.m['type'] or 'NiMH AA')
        self.capacity = QSpinBox()
        self.capacity.setRange(0, 50000)
        self.capacity.setSingleStep(50)
        self.capacity.setSuffix(' mAh')
        self.capacity.setValue(self.m['capacity'] or 0)
        self.note = QLineEdit(self.m['note'])
        form.addRow(tr('Maker:'), self.maker)
        form.addRow(tr('Model:'), self.name)
        form.addRow(tr('Type:'), self.type)
        form.addRow(tr('Nominal capacity:'), self.capacity)
        form.addRow(tr('Note:'), self.note)
        n = db.con.execute('SELECT COUNT(*) FROM batteries WHERE model_id=?', (self.m['id'],)).fetchone()[0]
        if n:
            form.addRow('', QLabel('<i>' + tr('{} battery(s) take over changes to maker, type and capacity.')
                                   .format(n) + '</i>'))
        form.addRow(dialog_buttons(self, self._ok))
        self.resize(420, 0)

    def _ok(self):
        v = dict(id=self.m['id'], maker=self.maker.currentText().strip(), name=self.name.text().strip(),
                 type=self.type.currentText().strip(), capacity=self.capacity.value(), note=self.note.text().strip())
        if not v['name']:
            QMessageBox.warning(self, tr('Model'), tr('Please enter a model name.'))
            return
        other, deleted = self.db.find_model(v['maker'], v['name'])
        if other is not None and other != v['id']:
            if not deleted or v['id'] is not None:
                QMessageBox.warning(self, tr('Model'), tr('{} already exists.').format(f"{v['maker']} {v['name']}"))
                return
            v['id'] = other                                   # a deleted model of that name: bring it back
        self.result_id, self.batteries = self.db.save_model(v)
        self.accept()


class BatteryDialog(QDialog):
    """Create / edit a battery: model (search dropdown; supplies maker, type, capacity), ID, count
    (new only: several batteries with consecutive IDs), description. There is no name field: the stored
    name is the model name (without model: "maker type")."""

    def __init__(self, parent, db: DB, battery=None, prefill=None):
        super().__init__(parent)
        self.db = db
        self.old_id = battery['id'] if battery else None
        self.result_id = None
        self.model_changes = []           # (model id, battery ids) of models created / edited here
        self.setWindowTitle(tr('Edit battery') if battery else tr('New battery'))
        b = battery or {**dict(id=db.next_battery_id(), name='', maker='', capacity=0, type='', description='',
                               model_id=None), **(prefill or {})}
        form = QFormLayout(self)
        row = QHBoxLayout()
        self.model = SearchCombo()
        self.model.currentIndexChanged.connect(self._model_picked)
        row.addWidget(self.model, 1)
        nm = QPushButton(tr('New …'))
        nm.setToolTip(tr('Create a new model'))
        nm.clicked.connect(self._new_model)
        row.addWidget(nm)
        form.addRow(tr('Model:'), row)
        self.id = QSpinBox()
        self.id.setRange(1, 999999)
        self.id.setValue(b['id'])
        self.id.valueChanged.connect(lambda _v: self._count_changed())
        self.count = QSpinBox()
        self.count.setRange(1, 100)
        self.count.setToolTip(tr('Create several identical batteries at once (consecutive IDs)'))
        self.count.valueChanged.connect(lambda _v: self._count_changed())
        self.maker = QComboBox()
        self.maker.setEditable(True)
        self.maker.addItems([''] + db.makers())
        self.maker.setCurrentText(b['maker'])
        self.capacity = QSpinBox()
        self.capacity.setRange(0, 50000)
        self.capacity.setSingleStep(50)
        self.capacity.setSuffix(' mAh')
        self.capacity.setValue(b['capacity'] or 0)
        self.type = QComboBox()
        self.type.setEditable(True)
        self.type.addItems(BATTERY_TYPES)
        self.type.setCurrentText(b['type'] or 'NiMH AA')
        self.desc = QPlainTextEdit(b['description'])
        self.desc.setPlaceholderText(tr('Date of purchase, device, anything notable …'))
        self.desc.setFixedHeight(90)
        form.addRow(tr('ID:'), self.id)
        if battery is None:
            form.addRow(tr('Count:'), self.count)
        form.addRow(tr('Maker:'), self.maker)
        form.addRow(tr('Nominal capacity:'), self.capacity)
        form.addRow(tr('Type:'), self.type)
        form.addRow(tr('Description:'), self.desc)
        form.addRow(dialog_buttons(self, self._ok))
        self.load_models(b.get('model_id'))
        self.model.setFocus()
        self.model.lineEdit().selectAll()
        self.resize(480, 0)

    def load_models(self, select=None):
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem(tr('– no model (enter values by hand) –'), None)
        for mid, maker, name, typ, cap, _note, _n, _deleted in self.db.models():
            self.model.addItem(model_text(maker, name, cap, typ), mid)
        self.model.setCurrentIndex(max(self.model.findData(select), 0))
        self.model.blockSignals(False)
        self._model_picked()

    def _model_picked(self):
        m = self.db.model(self.model.currentData()) if self.model.currentData() is not None else None
        for w in (self.maker, self.capacity, self.type):
            w.setEnabled(m is None)
        if m:
            self.maker.setCurrentText(m['maker'])
            self.capacity.setValue(m['capacity'] or 0)
            self.type.setCurrentText(m['type'])
        self._count_changed()

    def _count_changed(self):
        n = self.count.value()
        self.count.setSuffix(f'   (IDs {self.id.value()}–{self.id.value() + n - 1})' if n > 1 else '')

    def _new_model(self):
        dlg = ModelDialog(self, self.db, prefill=dict(type=self.type.currentText(),
                                                      maker=self.maker.currentText().strip()))
        if dlg.exec():
            self.model_changes.append((dlg.result_id, dlg.batteries))
            self.load_models(dlg.result_id)

    def value(self):
        m = self.db.model(self.model.currentData()) if self.model.currentData() is not None else None
        maker, typ = self.maker.currentText().strip(), self.type.currentText().strip()
        return dict(id=self.id.value(), name=m['name'] if m else ' '.join(x for x in (maker, typ) if x),
                    maker=maker, capacity=self.capacity.value(), type=typ,
                    description=self.desc.toPlainText().strip(), model_id=self.model.currentData())

    def _ok(self):
        v = self.value()
        if not v['name']:
            QMessageBox.warning(self, tr('Battery'), tr('Please choose a model or enter maker / type.'))
            return
        ids = [v['id']] if self.old_id is not None else list(range(v['id'], v['id'] + self.count.value()))
        taken = [i for i in ids if i != self.old_id and self.db.battery(i, deleted=True)]   # also deleted ones
        if taken:
            QMessageBox.warning(self, tr('Battery'), tr('ID {} is already taken.').format(', '.join(map(str, taken))))
            return
        for i in ids:
            self.db.save_battery({**v, 'id': i}, self.old_id)
        self.result_id = v['id']
        self.accept()


class ResultPanel(QWidget):
    save = Signal(int, object, int)       # session id, battery id (None = none), nominal
    phase_clicked = Signal(object)        # start time of the phase to show alone, None = whole run
    new_battery = Signal()

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.sid = None
        self.cur = None
        self.shown = (None, 0)            # battery id / nominal last filled into the edit fields
        self.dev_name = lambda dev: ''    # set by the main window
        lay = QVBoxLayout(self)
        self.title = QLabel(tr('No session selected'))
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.grade = QLabel('')
        self.grade.setAlignment(Qt.AlignCenter)
        fg = QFont()
        fg.setPointSize(18)
        fg.setBold(True)
        self.grade.setFont(fg)
        lay.addWidget(self.grade)
        self.note = QLabel('')
        self.note.setWordWrap(True)
        fn = QFont()
        fn.setBold(True)
        self.note.setFont(fn)
        lay.addWidget(self.note)
        form = QFormLayout()
        self.f = {}
        tips = self.field_tips()
        for k, name in [('task', tr('Task')), ('status', tr('Status')), ('type', tr('Detected')), ('time', tr('Time')),
                        ('dis', tr('Discharge capacity')), ('chg', tr('Charge capacity')),
                        ('res', tr('Internal resistance')), ('temp', tr('Max. temperature'))]:
            self.f[k] = QLabel('')
            self.f[k].setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.f[k].setWordWrap(True)
            label = QLabel(name + ':')
            for w in (label, self.f[k]):
                w.setToolTip(tips.get(k, ''))
            form.addRow(label, self.f[k])
        lay.addLayout(form)
        lay.addWidget(QLabel('<b>' + tr('Phases') + '</b>'))
        self.phases = QTableWidget(0, 4)
        self.phases.setHorizontalHeaderLabels([tr('Phase'), tr('Start'), tr('Duration'), 'mAh'])
        self.phases.verticalHeader().hide()
        self.phases.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.phases.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.phases.setSelectionMode(QAbstractItemView.SingleSelection)
        plain_selection(self.phases)
        self.phases.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.phases.setCursor(Qt.PointingHandCursor)
        self.phases.setToolTip(tr('Click: show only this phase in the chart, click again: the whole run'))
        self.phases.cellClicked.connect(self._phase_clicked)
        self.phase_sel = None                 # start of the phase shown alone in the chart
        lay.addWidget(self.phases, 1)
        edit = QFormLayout()
        self.model = SearchCombo()            # filters the battery list; locked to the battery's model once assigned
        self.model.currentIndexChanged.connect(self._model_picked)
        edit.addRow(tr('Model:'), self.model)
        row = QHBoxLayout()
        self.battery = SearchCombo()
        self.battery.currentIndexChanged.connect(self._battery_picked)
        row.addWidget(self.battery, 1)
        self.nb = QPushButton(tr('New …'))
        self.nb.clicked.connect(self.new_battery)
        row.addWidget(self.nb)
        edit.addRow(tr('Battery:'), row)
        self.binfo = QLabel('')
        self.binfo.setWordWrap(True)
        edit.addRow('', self.binfo)
        self.nominal = NominalCombo()         # changes are saved at once (battery too), the session rated anew
        self.nominal.committed.connect(lambda _v: self._save())
        edit.addRow(tr('Nominal capacity:'), self.nominal)
        lay.addLayout(edit)
        self.reload_models()
        self.reload_batteries()
        self.show_session(None)

    def reload_models(self):
        keep = self.model.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem(tr('– all models –'), None)
        for mid, maker, name, typ, cap, _note, _n, _deleted in self.db.models():
            self.model.addItem(model_text(maker, name, cap, typ), mid)
        self.model.setCurrentIndex(max(self.model.findData(keep), 0))
        self.model.blockSignals(False)

    def _set_model(self, mid):
        self.model.blockSignals(True)
        self.model.setCurrentIndex(max(self.model.findData(mid), 0))
        self.model.blockSignals(False)

    def _lock_model(self):
        """The model can be chosen only while no battery is assigned (then the battery's model is shown)."""
        self.model.setEnabled(self.sid is not None and self.battery.currentData() is None)

    def reload_batteries(self, select=None):
        """Refill the battery dropdown with the batteries of the chosen model (all without a model);
        select = battery id to pick (else keep the current one). An assigned battery sets the model."""
        keep = self.battery.currentData() if select is None else select
        b = self.db.battery(keep, deleted=True) if keep not in (None, -1) else None
        if b:
            self._set_model(b['model_id'])
        mid = self.model.currentData()
        self.battery.blockSignals(True)
        self.battery.clear()
        self.battery.addItem(tr('– no battery assigned –'), None)
        for r in self.db.batteries():
            if mid is not None and r[9] != mid:
                continue
            extra = ', '.join(x for x in (r[2], f'{r[3]} mAh' if r[3] else '', r[4]) if x)
            self.battery.addItem(f'#{r[0]} {r[1]}' + (f'  ({extra})' if extra else ''), r[0])
        if b and b['deleted']:                # a deleted battery stays shown for its sessions (else it would be lost)
            self.battery.addItem(f"#{b['id']} {b['name']}  " + tr('(deleted)'), b['id'])
        i = self.battery.findData(keep)
        self.battery.setCurrentIndex(max(i, 0))
        self.battery.blockSignals(False)
        self._show_binfo()
        self._lock_model()

    def _model_picked(self):
        self.reload_batteries()
        mid = self.model.currentData()
        m = self.db.model(mid) if mid is not None else None
        if m and m['capacity'] and self.battery.currentData() is None:      # rate against the model's capacity
            self.nominal.set_value(m['capacity'], self.nominal.lineEdit().placeholderText())
            self._save()

    def _show_binfo(self):
        bid = self.battery.currentData()
        b = self.db.battery(bid, deleted=True) if bid is not None else None
        self.binfo.setText(b['description'] if b else '')
        self.binfo.setVisible(bool(b and b['description']))
        return b

    def _battery_picked(self):
        b = self._show_binfo()
        if b:
            self._set_model(b['model_id'])
        self._lock_model()
        if b and b['capacity']:
            self.nominal.set_value(b['capacity'], self.nominal.lineEdit().placeholderText())
        self._save()

    def _save(self):
        if self.sid is not None:
            self.save.emit(self.sid, self.battery.currentData(), self.nominal.value())

    @staticmethod
    def field_tips():
        """Hover texts of the result fields: what the value means; legends for task and status."""
        def legend(intro, rows):
            return (f'<p>{intro}</p><table cellspacing="2">' +
                    ''.join(f'<tr><td><b>{tr_data(k)}</b></td><td>{text}</td></tr>' for k, text in rows) + '</table>')

        return {
            'task': legend(tr('The task set on the charger:'), [
                (TASKS[3], tr('charges the battery')),
                (TASKS[5], tr('empties the battery and measures what it delivers')),
                (TASKS[7], tr('brings the battery to its storage voltage')),
                (TASKS[9], tr('charges and discharges several times')),
                (TASKS[11], tr('charges, discharges (capacity measurement) and charges again')),
                (TASKS[13], tr('several cycles to revive old or long-stored cells'))]),
            'status': legend(tr('State of the session:'), [
                (RUNNING, tr('the charger is still working (optionally shown with whether it is charging or '
                             'discharging right now)')),
                (FINISHED, tr('the charger has finished the task')),
                (REMOVED, tr('the battery was taken out before the task was finished')),
                (ABORTED, tr('the task was stopped or replaced by another one, or the data broke off'))]),
            'dis': tr('<p>The capacity the battery delivered during the (last) discharge, measured by the charger. '
                      'Compared with the nominal capacity it gives the rating: a healthy cell reaches 80 % or '
                      'more.</p>'),
            'chg': tr('<p>The charge put into the battery after the discharge (or during a plain charge). It is '
                      'higher than the discharge capacity because charging has losses; discharge ÷ charge is the '
                      'charge efficiency, typically 70–90 % for NiMH.</p>'),
            'res': tr('<p>The internal resistance as the charger measures it – <b>lower is better</b>. It rises with '
                      'age and wear; a high value lets the voltage drop under load, so the device switches off '
                      'earlier. "min." is the lowest value of the session, first and last are the values at its '
                      'start and end.</p>'),
            'temp': tr('<p>The highest temperature of the battery during the session. NiMH cells get warm towards '
                       'the end of charging, which is normal. Above about 45 °C the cell is stressed: check the '
                       'contacts and the charging current.</p>'),
        }

    def _phase_clicked(self, row, _col):
        start = self.phases.item(row, 0).data(Qt.UserRole)
        start = None if start == self.phase_sel else start
        self.mark_phase(start)
        self.phase_clicked.emit(start)

    def mark_phase(self, start):
        """Highlight the phase shown alone in the chart (None: none)."""
        self.phase_sel = start
        self.phases.clearSelection()
        for i in range(self.phases.rowCount()):
            if self.phases.item(i, 0).data(Qt.UserRole) == start:
                self.phases.selectRow(i)

    @staticmethod
    def _nominal_hint(d):
        default = NOMINAL.get(d['size'], 0) if d else 0
        return tr('default ({} mAh)').format(default) if default else tr('not set')

    def show_session(self, d, battery_id=None):
        """d: dict like session_info() / DB.session_dict()."""
        self.cur = d
        enabled = d is not None and d.get('id') is not None
        for w in (self.battery, self.nominal, self.nb):
            w.setEnabled(enabled)
        if d is None:
            self.sid = None
            self.title.setText(tr('No session in this slot'))
            self.grade.setText('')
            self.grade.setStyleSheet('')
            self.note.setText('')
            for w in self.f.values():
                w.setText('')
            self.phases.setRowCount(0)
            self.reload_batteries(select=-1)
            self.nominal.set_value(0, '')
            self._lock_model()
            return
        same = self.sid == d.get('id')
        self.sid = d.get('id')
        b = self.db.battery(battery_id, deleted=True) if battery_id is not None else None
        slot = tr('Slot {}').format(d['slot'] + 1)
        self.title.setText(f"{self.dev_name(d.get('dev'))} · {slot} · {fmt_t(d['start'])}" +
                           (f" · #{b['id']} {b['name']}" if b else ''))
        g = d['grade'] or NO_GRADE
        nominal = d['nominal'] or NOMINAL.get(d['size'], 0)
        unrated = g == NO_GRADE and not nominal
        pct = f" ({100 * d['discharge_mah'] / nominal:.0f} %)" if d['discharge_mah'] and nominal and g != NO_GRADE else ''
        if is_measuring(d):                   # capacity not final yet: e.g. "Analyse läuft" in grey
            g = NO_GRADE
            self.grade.setText(tr_data(d['task']) + ' ' + tr_data(RUNNING))
        else:
            self.grade.setText(tr('Set the nominal capacity for a rating') if unrated else tr_data(g) + pct)
        self.grade.setStyleSheet(f"background:{GRADE_COLORS.get(g, '#888')}; color:white; padding:6px;"
                                 'border-radius:6px;' + ('font-size: 11pt;' if unrated else ''))
        self.note.setText(note_of(d))
        self.f['task'].setText(tr_data(d['task']))
        status = tr_data(d['status'])
        if d['status'] == RUNNING and d.get('phases'):          # running: what it is doing right now
            status += ' · ' + tr_data(MODE_NAMES[3] if d['phases'][-1][0] == CHARGE else MODE_NAMES[5])
        self.f['status'].setText(status)
        self.f['type'].setText(' '.join(x for x in (d['chem'], d['size']) if x) +
                               (' · ' + tr('nominal {} mAh').format(d['nominal']) if d['nominal'] else ''))
        self.f['time'].setText(f"{fmt_t(d['start'])} – {datetime.fromtimestamp(d['end']):%H:%M} "
                               f"({fmt_dur(d['end'] - d['start'])})")
        self.f['dis'].setText(f"{d['discharge_mah']} mAh" if d['discharge_mah'] else '–')
        self.f['chg'].setText(f"{d['charge_mah']} mAh" if d['charge_mah'] else '–')
        if d.get('res_first'):
            self.f['res'].setText(tr('min. {} · first {} · last {} mΩ').format(d['res_min'], d['res_first'],
                                                                                d['res_last']))
        else:
            self.f['res'].setText(tr('min. {} mΩ').format(d['res_min']) if d['res_min'] else '–')
        if d['res_min']:                      # rating of the value; lower is better (not obvious to everyone)
            self.f['res'].setText(self.f['res'].text() + '  –  ' + tr_data(RES_QUALITY[res_level(d['res_min'], d['chem'])]))
        self.f['temp'].setText(f"{d['temp_max']} °C" if d['temp_max'] else '–')
        if not same:
            self.phase_sel = None
        self.phases.setRowCount(len(d['phases']))
        for i, (kind, a, e, mah) in enumerate(d['phases']):
            for j, txt in enumerate([tr_data(kind), f'{datetime.fromtimestamp(a):%H:%M}', fmt_dur(e - a), str(mah)]):
                it = QTableWidgetItem(txt)
                if j == 0:
                    it.setForeground(QColor(PHASE_COLORS.get(kind, '#000')))
                    it.setData(Qt.UserRole, a)
                self.phases.setItem(i, j, it)
        self.mark_phase(self.phase_sel)
        # refill the edit fields for a new session, or when the stored values changed and the user
        # hasn't touched the fields (don't overwrite what is being selected / typed)
        if not same or self.battery.currentData() == self.shown[0]:
            if not same or battery_id != self.shown[0]:
                self.reload_batteries(select=battery_id if battery_id is not None else -1)
        if not same or (not self.nominal.hasFocus() and self.nominal.value() == self.shown[1]):
            self.nominal.set_value(d['nominal'] or 0, self._nominal_hint(d))
        self.shown = (battery_id, d['nominal'] or 0)
        self._lock_model()


def session_headers():
    """Column key -> header of the session tables."""
    return {'start': tr('Start'), 'dev': tr('Charger'), 'slot': tr('Slot'), 'task': tr('Task'),
            'status': tr('Status'), 'battery': tr('Battery'), 'detected': tr('Detected'), 'nominal': tr('Nominal'),
            'dis': tr('Disch. mAh'), 'pct': '%', 'chg': tr('Charge mAh'), 'rmin': tr('R min'), 'tmax': tr('T max'),
            'grade': tr('Rating'), 'dur': tr('Duration')}


GRADE_RANK = {g: len(GRADES) - i for i, g in enumerate(GRADES)}          # sort key: worst grade first


def make_table(headers, autofilter=False):
    """autofilter: sortable, with a filter button in every column header (table.autofilter)."""
    t = QTableWidget(0, len(headers))
    t.autofilter = AutoFilter(t) if autofilter else None
    t.setHorizontalHeaderLabels(headers)
    plain_selection(t)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def show_deleted():
    """Setting: list soft-deleted batteries and models too (tab Settings)."""
    return QSettings('battbench', 'battbench').value('show_deleted', False, type=bool)


DELETED_ROLE = Qt.UserRole + 4            # first cell: the row is a soft-deleted entry


def mark_deleted(items):
    """Grey italic cells of a soft-deleted entry."""
    for it in items:
        it.setForeground(QColor('#999999'))
        f = it.font()
        f.setItalic(True)
        it.setFont(f)


def delete_rows(parent, table, what, soft, hard):
    """Delete the selected rows: active ones softly, already deleted ones for good (after asking).
    what: (singular, plural) for the question. Returns True if something was deleted."""
    ids = selected_ids(table)
    rows = {table.item(r, 0).data(Qt.UserRole): table.item(r, 0).data(DELETED_ROLE)
            for r in range(table.rowCount())}
    active = [i for i in ids if not rows.get(i)]
    gone = [i for i in ids if rows.get(i)]
    parts = []
    if active:
        parts.append(tr('Delete {} {}? Deleted entries are hidden but kept and can be shown again (Settings).')
                     .format(len(active), what[0] if len(active) == 1 else what[1]))
    if gone:
        parts.append(tr('Delete the already deleted entry for good? This cannot be undone.') if len(gone) == 1 else
                     tr('Delete {} already deleted entries for good? This cannot be undone.').format(len(gone)))
    if not parts or QMessageBox.question(parent, tr('Delete'), '\n\n'.join(parts)) != QMessageBox.Yes:
        return False
    if active:
        soft(active)
    if gone:
        hard(gone)
    return True


def selected_ids(table):
    """Ids (first cell, Qt.UserRole) of the selected rows, in table order."""
    rows = sorted({i.row() for i in table.selectionModel().selectedRows()})
    return [table.item(r, 0).data(Qt.UserRole) for r in rows if not table.isRowHidden(r)]


def row_menu(table, edit, delete):
    """Right click on a row: Edit … (if edit is given) / Delete; with several rows selected (Ctrl / Shift) only
    Delete."""
    table.setContextMenuPolicy(Qt.CustomContextMenu)

    def show(pos):
        row = table.rowAt(pos.y())
        if row < 0:
            return
        if not table.selectionModel().isRowSelected(row):
            table.selectRow(row)
        n = len(selected_ids(table))
        menu = QMenu(table)
        if n == 1 and edit:
            menu.addAction(tr('Edit …'), edit)
        menu.addAction(tr('Delete') if n == 1 else tr('Delete ({})').format(n), delete)
        menu.exec(table.viewport().mapToGlobal(pos))

    table.customContextMenuRequested.connect(show)


def select_by_id(table, ident):
    """Select the row whose first cell carries this id (Qt.UserRole); rows may be sorted."""
    for i in range(table.rowCount()):
        if table.item(i, 0).data(Qt.UserRole) == ident:
            table.selectRow(i)
            return


def fill_session_table(table, rows, cols):
    """rows from DB.sessions() / DB.battery_sessions(); cols: column keys (see session_headers)."""
    # ResizeToContents measures all rows again on every setItem of a shown table: refilling 500 sessions
    # took 77 s and froze the GUI. Measure once at the end instead.
    # Sorting is switched off while filling as well, otherwise rows move between the setItem calls.
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    sorting = table.isSortingEnabled()
    table.setSortingEnabled(False)
    try:
        _fill_session_rows(table, rows, cols)
    finally:
        table.setSortingEnabled(sorting)
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
    if table.autofilter:
        table.autofilter.apply()


def _fill_session_rows(table, rows, cols):
    table.setRowCount(len(rows))
    for i, r in enumerate(rows):
        d = dict(zip(SESSION_FIELDS, r))
        nominal = d['nominal'] or NOMINAL.get(d['size'], 0)
        pct = f"{100 * d['discharge_mah'] / nominal:.0f} %" if d['discharge_mah'] and nominal else ''
        note = note_of(d)
        dis = d['discharge_mah']
        # column -> (text, sort key)
        vals = {'start': (fmt_t(d['start']), d['start']), 'dev': (d['dev_name'] or '', (d['dev_name'] or '').lower()),
                'slot': (d['slot'] + 1, d['slot']), 'task': (tr_data(d['task']), tr_data(d['task']).lower()),
                'status': (tr_data(d['status']), tr_data(d['status']).lower()),
                'battery': (battery_text(d['battery_id'], d['battery_name']) +
                            (' ' + tr('(deleted)') if d['battery_deleted'] and d['battery_id'] is not None else ''),
                            d['battery_id']),
                'detected': (' '.join(x for x in (d['chem'], d['size']) if x),
                             ' '.join(x for x in (d['chem'], d['size']) if x).lower()),
                'nominal': (nominal or '', nominal or None), 'dis': (dis or '', dis or None),
                'pct': (pct, 100 * dis / nominal if dis and nominal else None),
                'chg': (d['charge_mah'] or '', d['charge_mah'] or None), 'rmin': (d['res_min'] or '', d['res_min'] or None),
                'tmax': (d['temp_max'] or '', d['temp_max'] or None),
                'grade': (tr_data(RUNNING) if is_measuring(d) else tr_data(d['grade'] or NO_GRADE),
                          GRADE_RANK.get(d['grade'], 0)),
                'dur': (fmt_dur(d['end'] - d['start']), d['end'] - d['start'])}
        for j, c in enumerate(cols):
            text, key = vals[c]
            it = SortItem(str(text))
            it.setData(SORT_ROLE, key)                                  # None (empty) sorts first
            if c == 'start':
                it.setData(FILTER_ROLE, fmt_day(d['start']))        # filter by day, not by minute
            if j == 0:
                it.setData(Qt.UserRole, d['id'])
                it.setData(DELETED_ROLE, bool(d['deleted']))
                it.setToolTip(note)
            if d['deleted']:
                mark_deleted([it])
                if j == 0:
                    it.setText(f"{text} {tr('(deleted)')}")
            if c == 'grade':
                it.setBackground(QColor(GRADE_COLORS.get(d['grade'] or NO_GRADE, '#888')))
                it.setForeground(QColor('white'))
                it.setToolTip(note)
            table.setItem(i, j, it)


class BatteryTab(QWidget):
    """All batteries (add / edit / delete) and the measurement history of the selected one."""
    open_session = Signal(int)
    changed = Signal(int)                 # battery id added/edited, -1 = deleted
    models_changed = Signal(list)         # a model was added/edited: ids of its batteries
    HIST_COLS = ['start', 'dev', 'slot', 'task', 'status', 'dis', 'pct', 'chg', 'rmin', 'tmax', 'grade', 'dur']

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        btns = QHBoxLayout()
        for text, fn in [(tr('New battery …'), self.add), (tr('Edit …'), self.edit), (tr('Delete'), self.delete)]:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            btns.addWidget(b)
            if fn == self.edit:
                self.edit_btn = b
        btns.addSpacing(20)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Search (ID, maker, model, type, description) …'))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        btns.addWidget(self.search, 1)
        left.addLayout(btns)
        self.table = make_table([tr('ID'), tr('Maker'), tr('Model'), tr('Capacity'), tr('Type'), tr('Sessions'),
                                 tr('Last mAh'), tr('Rating'), tr('Last measured'), tr('Last charged'),
                                 tr('Description')], autofilter=True)
        self.table.autofilter.extra = self._matches
        self.table.autofilter.header.desc_first = {5, 8, 9}      # sessions, last measured, last charged
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)      # Ctrl / Shift: several, to delete
        self.table.itemSelectionChanged.connect(self.show_history)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit())
        row_menu(self.table, self.edit, self.delete)
        left.addWidget(self.table)
        lay.addLayout(left, 3)
        right = QVBoxLayout()
        self.hist_title = QLabel()
        right.addWidget(self.hist_title)
        heads = session_headers()
        self.hist = make_table([heads[c] for c in self.HIST_COLS], autofilter=True)
        self.hist.sortByColumn(0, Qt.DescendingOrder)
        self.hist.cellDoubleClicked.connect(
            lambda r, _c: self.open_session.emit(self.hist.item(r, 0).data(Qt.UserRole)))
        right.addWidget(self.hist)
        lay.addLayout(right, 2)
        self.load()

    def selected(self):
        r = self.table.currentRow()
        it = self.table.item(r, 0) if r >= 0 else None
        return it.data(Qt.UserRole) if it else None

    def load(self, select=None):
        select = select if select is not None else self.selected()
        rows = self.db.batteries(deleted=show_deleted())
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)       # rows would move while being filled
        self.table.setRowCount(len(rows))
        for i, (bid, name, maker, cap, typ, desc, n, last, grade, _mid, _mmaker, mname, t_meas,
                t_chg, deleted) in enumerate(rows):
            pct = f' ({100 * last / cap:.0f} %)' if last and cap else ''
            cells = [(f"{bid} {tr('(deleted)')}" if deleted else bid, bid), (maker, maker.lower()),
                     (mname or '', (mname or '').lower()), (f'{cap} mAh' if cap else '', cap or None),
                     (typ, typ.lower()), (n, n),
                     (f'{last}{pct}' if last else '', last), (tr_data(grade or ''), GRADE_RANK.get(grade)),
                     (fmt_t(t_meas), t_meas), (fmt_t(t_chg), t_chg),
                     ((desc or '').replace('\n', ' '), (desc or '').lower())]
            for j, (text, key) in enumerate(cells):
                it = SortItem(str(text))
                it.setData(SORT_ROLE, key)
                if j == 0:
                    it.setData(Qt.UserRole, bid)
                    it.setData(SEARCH_ROLE, name)             # batteries without a model: "maker type"
                    it.setData(DELETED_ROLE, bool(deleted))
                if deleted:
                    mark_deleted([it])
                if j in (8, 9):
                    it.setData(FILTER_ROLE, fmt_day(key))
                if j == 7 and grade:
                    it.setBackground(QColor(GRADE_COLORS.get(grade, '#888')))
                    it.setForeground(QColor('white'))
                if j == 10:
                    it.setToolTip(desc or '')
                self.table.setItem(i, j, it)
        self.table.setSortingEnabled(True)
        select_by_id(self.table, select)
        self._filter()
        self.table.blockSignals(False)
        self.show_history()

    def _matches(self, row):
        """Search field: every word in ID, maker, model, type, description or name."""
        words = self.search.text().lower().split()
        text = ' '.join([self.table.item(row, j).text() for j in (0, 1, 2, 4, 10)] +
                        [self.table.item(row, 0).data(SEARCH_ROLE) or '']).lower()
        return all(w in text for w in words)

    def _filter(self):
        self.table.autofilter.apply()

    def show_history(self):
        ids = selected_ids(self.table)
        self.edit_btn.setEnabled(len(ids) <= 1)
        bid = ids[0] if len(ids) == 1 else None
        b = self.db.battery(bid) if bid is not None else None
        self.hist_title.setText('<b>' + tr('History') + (f" #{b['id']} {b['name']}</b>" if b else
                                                         '</b> ' + tr('(choose a battery)')))
        fill_session_table(self.hist, self.db.battery_sessions(bid, deleted=show_deleted()) if b else [],
                           self.HIST_COLS)

    def _run(self, dlg):
        ok = dlg.exec()
        for _mid, bids in dlg.model_changes:             # models created / edited from inside the dialog
            self.models_changed.emit(bids)
        if ok:
            self.load(select=dlg.result_id)
            n = dlg.count.value() if dlg.old_id is None else 1
            for bid in range(dlg.result_id, dlg.result_id + n):
                self.changed.emit(bid)
        return dlg.result_id

    def add(self, prefill=None):
        return self._run(BatteryDialog(self, self.db, prefill=prefill))

    def edit(self):
        ids = selected_ids(self.table)
        if len(ids) == 1:
            self._run(BatteryDialog(self, self.db, battery=self.db.battery(ids[0])))

    def delete(self):
        if delete_rows(self, self.table, (tr('battery'), tr('batteries')), self.db.delete_batteries,
                       self.db.hard_delete_batteries):
            self.load()
            self.changed.emit(-1)


class ModelTab(QWidget):
    """Maker / model list (add / edit / delete, search). The capacity of a model is the nominal
    capacity of all its batteries."""
    changed = Signal(list)                # ids of batteries whose values came from an edited model

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        for text, fn in [(tr('New model …'), self.add), (tr('Edit …'), self.edit), (tr('Delete'), self.delete)]:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            top.addWidget(b)
            if fn == self.edit:
                self.edit_btn = b
        top.addSpacing(20)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Search (maker, model, type, note) …'))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search, 1)
        lay.addLayout(top)
        self.table = make_table([tr('Maker'), tr('Model'), tr('Type'), tr('Capacity'), tr('Batteries'), tr('Note')],
                                autofilter=True)
        self.table.autofilter.extra = self._matches
        self.table.autofilter.header.desc_first = {4}
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)      # Ctrl / Shift: several, to delete
        self.table.itemSelectionChanged.connect(
            lambda: self.edit_btn.setEnabled(len(selected_ids(self.table)) <= 1))
        self.table.cellDoubleClicked.connect(lambda *_: self.edit())
        row_menu(self.table, self.edit, self.delete)
        lay.addWidget(self.table)
        self.load()

    def selected(self):
        r = self.table.currentRow()
        it = self.table.item(r, 0) if r >= 0 else None
        return it.data(Qt.UserRole) if it else None

    def load(self, select=None):
        select = select if select is not None else self.selected()
        rows = self.db.models(deleted=show_deleted())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, (mid, maker, name, typ, cap, note, n, deleted) in enumerate(rows):
            cells = [(maker, maker.lower()), (f"{name} {tr('(deleted)')}" if deleted else name, name.lower()),
                     (typ, (typ or '').lower()),
                     (f'{cap} mAh' if cap else '', cap or None), (n or '', n or 0), (note, (note or '').lower())]
            for j, (text, key) in enumerate(cells):
                it = SortItem(str(text))
                it.setData(SORT_ROLE, key)
                if j == 0:
                    it.setData(Qt.UserRole, mid)
                    it.setData(DELETED_ROLE, bool(deleted))
                if deleted:
                    mark_deleted([it])
                self.table.setItem(i, j, it)
        self.table.setSortingEnabled(True)
        select_by_id(self.table, select)
        self._filter()

    def _matches(self, row):
        """Search field: every word in maker, model, type or note."""
        words = self.search.text().lower().split()
        text = ' '.join(self.table.item(row, j).text() for j in (0, 1, 2, 5)).lower()
        return all(w in text for w in words)

    def _filter(self):
        self.table.autofilter.apply()

    def _run(self, dlg):
        if dlg.exec():
            self.load(select=dlg.result_id)
            self.changed.emit(dlg.batteries)

    def add(self):
        self._run(ModelDialog(self, self.db))

    def edit(self):
        ids = selected_ids(self.table)
        if len(ids) == 1:
            self._run(ModelDialog(self, self.db, model=self.db.model(ids[0])))

    def delete(self):
        if delete_rows(self, self.table, (tr('model'), tr('models')), self.db.delete_models,
                       self.db.hard_delete_models):
            self.load()
            self.changed.emit([])


# ======================================================================= main window
class ChargerTab(QFrame):
    """Browser-like tab for one charger: name, short status and one lamp per slot in the slot colour."""
    clicked = Signal(int)
    LAMP = 13

    def __init__(self, dev, slots):
        super().__init__()
        self.dev = dev
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMinimumWidth(120)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 6)
        lay.setSpacing(3)
        top = QHBoxLayout()
        self.name = QLabel()
        f = QFont()
        f.setBold(True)
        self.name.setFont(f)
        top.addWidget(self.name)
        self.via = QLabel()                   # USB / Bluetooth symbols
        top.addWidget(self.via)
        self.info = QLabel()
        self.info.setStyleSheet('color: palette(placeholder-text);')
        top.addWidget(self.info)
        top.addStretch()
        lay.addLayout(top)
        lamps = QHBoxLayout()
        lamps.setSpacing(3 if slots <= 8 else 2)
        self.lamp_size = self.LAMP if slots <= 8 else 9
        self.lamps = []
        for _ in range(slots):
            lamp = QLabel()
            lamp.setFixedSize(self.lamp_size, self.lamp_size)
            lamps.addWidget(lamp)
            self.lamps.append(lamp)
        lamps.addStretch()
        lay.addLayout(lamps)
        self.set_active(False)

    def set_active(self, on):
        self.setStyleSheet(
            'ChargerTab { background: palette(base); border: 1px solid palette(mid); border-bottom: none;'
            ' border-top: 3px solid #3a7bd5; border-top-left-radius: 6px; border-top-right-radius: 6px; }'
            if on else
            'ChargerTab { background: palette(button); border: 1px solid palette(mid); border-bottom: none;'
            ' border-top-left-radius: 6px; border-top-right-radius: 6px; margin-top: 2px; }')   # light + dark theme

    def set_lamp(self, slot, color, tip):
        """Round LED: bright spot top left, darker rim."""
        c = QColor(color)
        lamp = self.lamps[slot]
        lamp.setStyleSheet(
            f'border-radius:{self.lamp_size // 2}px; border:1px solid {c.darker(160).name()};'
            f'background: qradialgradient(cx:0.5, cy:0.5, radius:0.6, fx:0.32, fy:0.3, stop:0 {c.lighter(170).name()},'
            f' stop:0.45 {c.name()}, stop:1 {c.darker(130).name()});')
        lamp.setToolTip(tip)

    def mousePressEvent(self, e):
        self.clicked.emit(self.dev)


class PlusTab(QFrame):
    """'+' at the end of the charger tabs: opens the info / Bluetooth dialog."""
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tr('Supported chargers, Bluetooth search'))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 2, 14, 2)
        lab = QLabel('+')
        f = QFont()
        f.setPointSize(16)
        f.setBold(True)
        lab.setFont(f)
        lay.addWidget(lab, 0, Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setStyleSheet('PlusTab { background: palette(button); border: 1px solid palette(mid); border-bottom: none;'
                           ' border-top-left-radius: 6px; border-top-right-radius: 6px; margin-top: 2px; }')

    def mousePressEvent(self, e):
        self.clicked.emit()


class InfoDialog(QDialog):
    """Supported chargers, what is connected, Bluetooth search."""

    @staticmethod
    def supported():
        return [
            ('ISDT N8 / N16 / N24', 'USB',
             tr('Mode, chemistry, voltage, current, mAh, internal resistance, temperature, progress, input voltage. '
                'They do not tell AA from AAA. The app finds out the number of slots itself.') + ' ' +
             tr('N16 / N24 and the first generation: same protocol as the N8 expected.')),
            ('ISDT C4 / C4 EVO', 'USB',
             tr('Mode, chemistry, voltage, current, mAh, internal resistance, temperature, progress. '
                'C4 EVO also input voltage, but no size.')),
            ('ISDT A4 / UC4', 'USB',
             tr('Like the C4; the app adds up the mAh from current × time.')),
            ('ISDT A8 Air / C4 Air', 'Bluetooth', tr('Like the A4 Air over Bluetooth.')),
            ('SkyRC MC3000 / MC5000', 'Bluetooth',
             tr('Status (charging / discharging / pause / done / error), operation, battery type, voltage, current, '
                'mAh, time, temperature, internal resistance. Read only (no start / stop).')),
            ('ISDT A4 Air', tr('Bluetooth (recommended)'),
             tr('State (charging / full / error), charge level %, mAh and mWh from the charger, measured internal '
                'resistance, voltage, current, input voltage. No temperature.')),
            ('', 'USB',
             tr('Voltage, current, temperature, input voltage. The app works out state and mAh, the internal '
                'resistance is only estimated (≈). With “Automatic” only while Bluetooth is not connected.')),
        ]

    def __init__(self, parent):
        super().__init__(parent)
        self.main = parent
        self.setWindowTitle(tr('Chargers'))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('<b>' + tr('Supported chargers') + '</b>'))
        grid = QGridLayout()
        grid.setColumnStretch(2, 1)
        grid.setHorizontalSpacing(14)
        for r, (name, conn, what) in enumerate(self.supported()):
            for c, text in enumerate((f'<b>{name}</b>', conn, what)):
                lab = QLabel(text)
                lab.setWordWrap(c == 2)
                lab.setAlignment(Qt.AlignLeft | Qt.AlignTop)
                grid.addWidget(lab, r, c)
        lay.addLayout(grid)
        lay.addWidget(QLabel('<i>' + tr('Other ISDT chargers (e.g. Q6, Q8, K2, P20) are not supported.') + '</i>'))
        row = QHBoxLayout()
        row.addWidget(QLabel(tr('Read the A4 Air via:')))
        self.a4 = QComboBox()
        for k, text in a4_mode_names().items():
            self.a4.addItem(text, k)
        self.a4.setCurrentIndex(self.a4.findData(parent.a4))
        self.a4.setEnabled(parent.source != 'offline')
        self.a4.currentIndexChanged.connect(self._set_a4)
        row.addWidget(self.a4)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QLabel('<b>' + tr('Connected') + '</b>'))
        self.connected = QLabel()
        self.connected.setWordWrap(True)
        lay.addWidget(self.connected)
        lay.addWidget(QLabel('<b>Bluetooth</b>'))
        row = QHBoxLayout()
        self.bt_state = QLabel()
        row.addWidget(self.bt_state, 1)
        self.scan = QPushButton(tr('Search now'))
        self.scan.clicked.connect(self._scan)
        row.addWidget(self.scan)
        lay.addLayout(row)
        self.bt_list = QLabel()
        self.bt_list.setWordWrap(True)
        lay.addWidget(self.bt_list)
        hint = QLabel(tr('<b>Air charger not found?</b> Quit the <b>ISD Link</b> app on your phone (or switch off '
                         'Bluetooth on the phone). The charger accepts only one Bluetooth connection and cannot be '
                         'seen by others while the phone is connected. In turn, ISD Link cannot reach it while '
                         'BattBench is connected.'))
        hint.setWordWrap(True)
        hint.setStyleSheet('background: palette(alternate-base); border: 1px solid #e69138; border-radius: 6px; padding: 8px;')
        lay.addWidget(hint)
        lay.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.setMinimumSize(780, 580)           # wrapped labels: Qt does not grow a dialog to their height
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def _set_a4(self):
        mode = self.a4.currentData()
        self.main.a4 = mode
        QSettings('battbench', 'battbench').setValue('a4', mode)
        self.main.worker.cmds.put(('a4', mode))

    def _scan(self):
        ble = self.main.worker.ble
        if ble:
            ble.scan_now.set()

    def refresh(self):
        m = self.main
        lines = []
        for dev, on in sorted(m.online.items()):
            d = m.devs.get(dev, {})
            lines.append(tr('{} – {} via {}').format(d.get('name', dev), on.get('version', ''),
                                                    ' + '.join(VIA_NAME[v] for v in on.get('vias', ()))))
        self.connected.setText('<br>'.join(lines) or tr('none'))
        ble = m.worker.ble
        self.scan.setEnabled(bool(ble and ble.is_alive()))
        if not ble:
            self.bt_state.setText(tr('off (started with --no-bt or database only)'))
            self.bt_list.setText('')
            return
        ago = tr(', last search {} s ago').format(int(time.time() - ble.last_scan)) if ble.last_scan else ''
        self.bt_state.setText(f'{ble.state}{ago}' if ble.is_alive() else ble.state)
        found = []
        for addr, f in sorted(ble.seen.items()):
            st = tr('connected') if addr in ble.active and ble.active[addr].dev is not None else \
                 tr('connecting …') if addr in ble.active else tr('found')
            found.append(tr('{} {} (signal {} dBm) – {}').format(f['name'], addr, f['rssi'], st))
        self.bt_list.setText('<br>'.join(found) or tr('No Bluetooth charger found yet.'))


class HelpTab(QTextBrowser):
    """User guide: the README section, as battbench/help/help_<language>.md (English if not translated)."""

    def __init__(self):
        super().__init__()
        self.setOpenExternalLinks(True)
        folder = os.path.join(PKG, 'help')
        path = next((p for p in (os.path.join(folder, f'help_{i18n.current}.md'), os.path.join(folder, 'help_en.md'))
                     if os.path.exists(p)), None)
        if path:
            with open(path, encoding='utf-8') as f:
                self.setMarkdown(f.read())
        self.document().setDocumentMargin(14)


class SettingsTab(QWidget):
    """User settings (stored with QSettings)."""
    deleted_changed = Signal()            # show-deleted switched or deleted entries removed for good

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        form = QFormLayout(self)
        row = QHBoxLayout()
        self.lang = QComboBox()
        self.lang.addItem(tr('Automatic (system)'), 'auto')
        for k, name in LANGUAGES.items():
            self.lang.addItem(name, k)
        self.lang.setCurrentIndex(max(self.lang.findData(QSettings('battbench', 'battbench').value('lang', 'auto')), 0))
        self.lang.currentIndexChanged.connect(self._set_lang)
        row.addWidget(self.lang)
        self.lang_note = QLabel()
        self.lang_note.setStyleSheet('color: palette(placeholder-text);')
        row.addWidget(self.lang_note)
        row.addStretch(1)
        form.addRow(tr('Language:'), row)
        self.show_del = QCheckBox(tr('Show deleted sessions, batteries and models (grey, “(deleted)”)'))
        self.show_del.setChecked(show_deleted())
        self.show_del.setToolTip(tr('Deleting an entry shown as deleted removes it for good.'))
        self.show_del.toggled.connect(self._set_show_deleted)
        form.addRow(tr('Deleted entries:'), self.show_del)
        purge = QPushButton(tr('Delete all deleted entries for good …'))
        purge.clicked.connect(self._purge)
        row = QHBoxLayout()
        row.addWidget(purge)
        row.addStretch(1)
        form.addRow('', row)
        self.compress = QCheckBox(tr('Compress readings older than two weeks automatically (one per minute)'))
        self.compress.setChecked(QSettings('battbench', 'battbench').value('compress', False, type=bool))
        self.compress.setToolTip(tr('Per minute the median of voltage, current, resistance and temperature and the '
                                    'last counter values are kept. Sessions and ratings stay as they are; curves of '
                                    'old sessions get coarser. Checked at start and once a day.'))
        self.compress.toggled.connect(lambda on: QSettings('battbench', 'battbench').setValue('compress', on))
        form.addRow(tr('Old readings:'), self.compress)
        compress_now = QPushButton(tr('Compress all readings now …'))
        compress_now.clicked.connect(self._compress_now)
        row = QHBoxLayout()
        row.addWidget(compress_now)
        row.addStretch(1)
        form.addRow('', row)
        self.stats = QLabel()
        self.stats.setTextFormat(Qt.RichText)
        form.addRow(tr('Database:'), self.stats)
        optimize = QPushButton(tr('Optimise database'))
        optimize.setToolTip(tr('Rewrites the database file without unused space (SQLite VACUUM). Takes a moment.'))
        optimize.clicked.connect(self._optimize)
        row = QHBoxLayout()
        row.addWidget(optimize)
        row.addStretch(1)
        form.addRow('', row)
        self.refresh_stats()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_stats()

    def refresh_stats(self):
        n = self.db.stats()
        ds, db_, dm = self.db.deleted_counts()
        rows = [(tr('File size'), fmt_size(self.db.size())), (tr('Readings'), fmt_int(n['samples'])),
                (tr('Sessions'), fmt_int(n['sessions']) + (' ' + tr('({} deleted)').format(ds) if ds else '')),
                (tr('Phases'), fmt_int(n['phases'])),
                (tr('Batteries'), fmt_int(n['batteries']) + (' ' + tr('({} deleted)').format(db_) if db_ else '')),
                (tr('Models'), fmt_int(n['models']) + (' ' + tr('({} deleted)').format(dm) if dm else '')),
                (tr('Chargers'), fmt_int(n['devices']))]
        self.stats.setText('<table cellspacing="2">' + ''.join(
            f'<tr><td>{a}</td><td align="right">&nbsp;&nbsp;{b}</td></tr>' for a, b in rows) + '</table>')

    compress_requested = Signal()

    def _compress_now(self):
        if QMessageBox.question(self, tr('Compress readings'), tr(
                'Compress all readings (except the last hour and running sessions) to one per minute and optimise '
                'the database? Sessions and ratings stay; the fine resolution of the curves is lost for good.')
                ) == QMessageBox.Yes:
            self.compress_requested.emit()

    def _optimize(self):
        before = self.db.size()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.db.optimize()
        except sqlite3.Error as e:                          # e.g. busy: the worker is writing right now
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, tr('Optimise database'), tr('Not possible right now: {}').format(e))
            return
        QApplication.restoreOverrideCursor()
        self.refresh_stats()
        QMessageBox.information(self, tr('Optimise database'), tr('Database optimised: {} → {}.').format(
            fmt_size(before), fmt_size(self.db.size())))

    def _set_show_deleted(self, on):
        QSettings('battbench', 'battbench').setValue('show_deleted', on)
        self.deleted_changed.emit()

    def _purge(self):
        ns, nb, nm = self.db.deleted_counts()
        if not ns and not nb and not nm:
            QMessageBox.information(self, tr('Delete'), tr('There are no deleted entries.'))
            return
        if QMessageBox.question(self, tr('Delete'), tr('Delete all deleted entries for good ({} sessions, {} batteries, '
                                                        '{} models)? The readings are kept. This cannot be undone.')
                                .format(ns, nb, nm)) == QMessageBox.Yes:
            self.db.purge_deleted()
            self.deleted_changed.emit()

    def _set_lang(self):
        QSettings('battbench', 'battbench').setValue('lang', self.lang.currentData())
        self.lang_note.setText(tr('takes effect after a restart'))


class InfoTab(QWidget):
    """Version, author, links, database location."""

    def __init__(self, db_path):
        super().__init__()
        lay = QHBoxLayout(self)
        icon = QLabel()
        icon.setPixmap(QIcon(ICON).pixmap(96, 96))
        icon.setAlignment(Qt.AlignTop)
        lay.addWidget(icon)
        lay.addSpacing(16)
        def link(url):
            return f"<a href='{url}'>{url.split('//')[1]}</a>"

        rows = [(tr('Version:'), full_version()), (tr('Author:'), f'{AUTHOR}, {link(WEBSITE)}'),
                (tr('Source code:'), link(SOURCE)), (tr('License:'), 'GNU GPL v3'),
                (tr('Database:'), os.path.abspath(db_path))]
        text = QLabel(
            '<h2>BattBench</h2><p>' + tr('Battery test bench: tracking and analysis of rechargeable batteries') +
            '</p><table cellspacing="4">' + ''.join(f'<tr><td>{a}</td><td>{b}</td></tr>' for a, b in rows) +
            "</table><p style='color: gray'>" +
            tr('Not affiliated with ISDT or SkyRC. Never leave charging batteries unattended.') + '</p>')
        text.setTextFormat(Qt.RichText)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        text.setAlignment(Qt.AlignTop)
        lay.addWidget(text, 1)


class DeviceTab(QWidget):
    """Known chargers: name (only stored here, never written to the charger), model, identity, last seen."""
    changed = Signal()
    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.online = {}                  # dev -> connections ('usb', 'ble')
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        b = QPushButton(tr('Rename …'))
        b.clicked.connect(self.rename)
        top.addWidget(b)
        top.addWidget(QLabel(tr('The name is only stored in the app, never in the charger.')))
        top.addStretch()
        lay.addLayout(top)
        self.table = make_table([tr('Name'), tr('Model'), tr('Slots'), tr('Firmware'), tr('ID'), tr('Last seen'),
                                 tr('Status')])
        self.table.cellDoubleClicked.connect(lambda *_: self.rename())
        lay.addWidget(self.table)
        self.load()

    def load(self):
        devs = self.db.devices()
        self.table.setRowCount(len(devs))
        for i, d in enumerate(devs):
            vias = self.online.get(d['id'])
            on = vias is not None
            vals = [d['name'], d['model'], d['slots'], d['version'], d['key'] + (f" / {d['alt_key']}" if d['alt_key'] else ''),
                    fmt_t(d['last_seen']), tr('connected ({})').format(' + '.join(VIA_NAME[v] for v in vias)) if on else '–']
            for j, v in enumerate(vals):
                it = QTableWidgetItem()
                it.setData(Qt.DisplayRole, v)
                if j == 0:
                    it.setData(Qt.UserRole, d['id'])
                if j == 6 and on:
                    it.setForeground(QColor(COLORS['done']))
                    it.setIcon(QIcon(via_pixmap(vias, self.palette().text().color().name())))
                self.table.setItem(i, j, it)

    def set_online(self, online):
        now = {dev: tuple(on.get('vias', ())) for dev, on in online.items()}
        if now != self.online:
            self.online = now
            self.load()

    def rename(self):
        r = self.table.currentRow()
        if r < 0:
            return
        dev = self.table.item(r, 0).data(Qt.UserRole)
        name, ok = QInputDialog.getText(self, tr('Rename charger'), tr('Name:'), text=self.table.item(r, 0).text())
        if ok and name.strip():
            self.db.set_device_name(dev, name.strip())
            self.load()
            self.changed.emit()


class MainWindow(QMainWindow):
    COLS = ['start', 'dev', 'slot', 'task', 'status', 'battery', 'detected', 'nominal', 'dis', 'pct', 'chg', 'rmin',
            'tmax', 'grade', 'dur']

    def __init__(self, db_path, source, a4='both', bt=True):
        super().__init__()
        self.setWindowTitle(f'BattBench {__version__}')
        self.source, self.a4 = source, a4
        self.t_start = time.time()     # tiles only for chargers connected / sending data in this run
        self.resize(1500, 950)
        self.db = DB(db_path)
        self.devs = {}                 # dev -> row of the devices table
        self.online = {}               # dev -> dict(version, in_mv) of connected chargers
        self.live = {}                 # (dev, slot) -> sample dict
        self.cur = {}                  # (dev, slot) -> running session
        self.sel = None                # selected (dev, slot)
        self.hist_id = None            # session shown from a table (None = live slot)
        self.row_sid = None            # session last opened from the sessions table (a click on it again: no reload)
        self.last_plot = 0
        self.rows = {}                 # dev -> dict(tab, page, tiles) of the chargers shown
        self.shown_dev = None          # charger whose tiles are visible
        self.last_slot = {}            # dev -> slot selected last on that charger

        central = QWidget()
        root = QVBoxLayout(central)
        self.vsplit = vsplit = QSplitter(Qt.Vertical)
        self.top = top = QWidget()
        tlay = QVBoxLayout(top)
        tlay.setContentsMargins(0, 0, 0, 0)
        tlay.setSpacing(0)
        self.strip = QHBoxLayout()
        self.strip.setSpacing(2)
        self.strip.setContentsMargins(4, 0, 0, 0)
        self.no_dev = QLabel(tr('No charger connected.'))
        self.strip.addWidget(self.no_dev)
        self.plus = PlusTab()
        self.plus.clicked.connect(lambda: InfoDialog(self).exec())
        self.strip.addWidget(self.plus)
        self.strip.addStretch()
        tlay.addLayout(self.strip)
        self.stack = QStackedWidget()
        self.stack.setObjectName('tiles')
        self.stack.setStyleSheet('#tiles { border-top: 1px solid palette(mid); }')
        tlay.addWidget(self.stack)
        vsplit.addWidget(top)

        hsplit = QSplitter(Qt.Horizontal)
        self.plot = CurvePlot()
        hsplit.addWidget(self.plot)
        self.result = ResultPanel(self.db)
        self.result.dev_name = self.dev_name
        self.result.save.connect(self.save_meta)
        self.result.new_battery.connect(self.new_battery_for_session)
        self.result.phase_clicked.connect(self.plot.show_phase)          # phase list and chart stay in step
        self.plot.phase_changed.connect(self.result.mark_phase)
        hsplit.addWidget(self.result)
        hsplit.setSizes([1100, 400])
        vsplit.addWidget(hsplit)

        self.tabs = QTabWidget()
        heads = session_headers()
        self.table = make_table([heads[c] for c in self.COLS], autofilter=True)
        self.table.sortByColumn(0, Qt.DescendingOrder)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)      # Ctrl / Shift: several, to delete
        row_menu(self.table, None, self.delete_sessions)
        self.table.currentCellChanged.connect(self._row_changed)    # click or arrow keys
        self.table.cellClicked.connect(self._row_clicked)           # same row again (e.g. after a slot view)
        self.tabs.addTab(self.table, tr('Sessions'))
        self.btab = BatteryTab(self.db)
        self.btab.open_session.connect(self.open_session)
        self.btab.changed.connect(self.battery_changed)
        self.btab.models_changed.connect(self.models_changed)
        self.tabs.addTab(self.btab, tr('Batteries'))
        self.mtab = ModelTab(self.db)
        self.mtab.changed.connect(self.models_changed)
        self.tabs.addTab(self.mtab, tr('Models'))
        self.dtab = DeviceTab(self.db)
        self.dtab.changed.connect(self.devices_renamed)
        self.tabs.addTab(self.dtab, tr('Chargers'))
        self.stab = SettingsTab(self.db)
        self.stab.deleted_changed.connect(self.deleted_changed)
        self.stab.compress_requested.connect(lambda: self.worker.cmds.put(('compress',)))
        self.tabs.addTab(self.stab, tr('Settings'))
        self.tabs.addTab(HelpTab(), tr('Help'))
        self.tabs.addTab(InfoTab(db_path), tr('Info'))
        vsplit.addWidget(self.tabs)
        vsplit.setSizes([200, 500, 250])
        root.addWidget(vsplit, 1)
        self.setCentralWidget(central)

        self.st_conn = QLabel('')
        self.statusBar().addPermanentWidget(self.st_conn)
        self.st_conn.setText(tr('Starting …') if source == 'device' else tr('No chargers'))

        self.thread = QThread()
        self.worker = Worker(db_path, source, a4, bt)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.cycle.connect(self.on_cycle)
        self.worker.status.connect(self.st_conn.setText)
        self.worker.sessions_changed.connect(self.load_tables)
        self.worker.data_changed.connect(self.data_changed)
        self.thread.start()

        self.load_tables()
        self.select_slot(None)

    def dev_name(self, dev):
        d = self.devs.get(dev)
        return d['name'] if d else tr('Charger {}').format(dev)

    # ---------------------------------------------------------------- charger tabs + tiles
    @staticmethod
    def per_row(slots):
        """Tiles per row: 8 like the N8; N16 in one row, N24 in two rows of 12 (narrower tiles)."""
        return {16: 16, 24: 12}.get(slots, 8)

    def _ensure_row(self, dev):
        if dev in self.rows or dev not in self.devs:
            return
        self.no_dev.hide()
        slots = self.devs[dev]['slots'] or 0
        tab = ChargerTab(dev, slots)
        tab.clicked.connect(self.show_charger)
        self.strip.insertWidget(self.strip.count() - 2, tab)       # before "+" and the stretch
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(4, 6, 4, 0)
        n = self.per_row(slots)
        for c in range(n):
            grid.setColumnStretch(c, 1)                            # all tiles of a charger equally wide
        tiles = []
        for slot in range(slots):
            t = SlotTile((dev, slot), compact=slots > 8)
            t.approx_res = self.devs[dev]['model'] == 'A4Air'
            t.clicked.connect(self._tile_clicked)
            grid.addWidget(t, slot // n, slot % n)
            tiles.append(t)
        grid.setRowStretch(grid.rowCount(), 1)
        self.stack.addWidget(page)
        self.rows[dev] = dict(tab=tab, page=page, tiles=tiles)
        if self.shown_dev is None:
            self._show(dev)
            if self.sel is None and self.hist_id is None and tiles:
                self.select_slot((dev, 0))

    def _remove_row(self, dev):
        """dev was merged into another device (same charger over USB and Bluetooth)."""
        self.devs = {x['id']: x for x in self.db.devices()}
        for k in [k for k in self.live if k[0] == dev]:
            self.live.pop(k)
        row = self.rows.pop(dev, None)
        if row is None:
            return
        row['tab'].deleteLater()
        self.stack.removeWidget(row['page'])
        row['page'].deleteLater()
        if self.sel and self.sel[0] == dev:
            self.sel = None
        if self.shown_dev == dev:
            self.shown_dev = None
            if self.rows:
                self._show(next(iter(self.rows)))
        self.dtab.load()

    def _show(self, dev):
        """Make the tiles of this charger visible (selection unchanged)."""
        if dev not in self.rows:
            return
        self.shown_dev = dev
        page = self.rows[dev]['page']
        self.stack.setCurrentWidget(page)
        for d, row in self.rows.items():
            row['tab'].set_active(d == dev)
        self._fit_tiles()

    def _fit_tiles(self):
        """Two rows of tiles (N24) need more room than the default: grow the tile area, never shrink it."""
        if self.shown_dev not in self.rows:
            return
        page = self.rows[self.shown_dev]['page']
        sizes = self.vsplit.sizes()
        need = self.top.sizeHint().height() - self.stack.sizeHint().height() + page.sizeHint().height()
        if sizes[0] < need and sizes[1] > need - sizes[0]:
            self.vsplit.setSizes([need, sizes[1] - (need - sizes[0]), *sizes[2:]])

    def show_charger(self, dev):
        """Click on a charger tab: its tiles, and the slot last selected there (else the first running one)."""
        self._show(dev)
        slot = self.last_slot.get(dev)
        if slot is None:
            slot = min((k[1] for k in self.cur if k[0] == dev), default=0)
        self.select_slot((dev, slot))

    def _update_heads(self):
        for dev, row in self.rows.items():
            d = self.devs.get(dev, {})
            on = self.online.get(dev)
            row['tab'].name.setText(d.get('name', str(dev)))
            vias = tuple(on.get('vias', ())) if on else ()
            if row['tab'].via.property('vias') != vias:
                row['tab'].via.setProperty('vias', vias)
                row['tab'].via.setPixmap(via_pixmap(vias, self.palette().text().color().name()) if vias else QPixmap())
                row['tab'].via.setToolTip(' + '.join(VIA_NAME[v] for v in vias))
            if on:
                row['tab'].info.setText(f"{on['in_mv'] / 1000:.2f} V" if on.get('in_mv') else '')
                row['tab'].setToolTip(tr('{} · via {}').format(on['version'], ' + '.join(VIA_NAME[v] for v in vias)))
            else:
                row['tab'].info.setText(tr('disconnected'))
                row['tab'].setToolTip('')
            for slot in range(len(row['tab'].lamps)):
                smp = self.live.get((dev, slot))
                if smp is None:
                    row['tab'].set_lamp(slot, COLORS['empty'], tr('Slot {}: no data').format(slot + 1))
                else:
                    row['tab'].set_lamp(slot, mode_color(smp['mode'], smp['ma']),
                                        tr('Slot {}: {}').format(slot + 1, mode_name(smp)))

    # ---------------------------------------------------------------- worker events
    def on_cycle(self, d):
        for dev in d.get('merged', ()):
            self._remove_row(dev)
        self.online = d['devices']
        self.live.update(d['live'])
        self.cur = d['sessions']
        if set(self.online) - set(self.devs):
            self.devs = {x['id']: x for x in self.db.devices()}
        shown = set(self.online) | {k[0] for k, v in self.live.items() if v['t'] >= self.t_start}
        for dev in sorted(shown):
            self._ensure_row(dev)
        self.dtab.set_online(self.online)
        self._update_heads()
        for dev, row in self.rows.items():
            approx = self.devs.get(dev, {}).get('model') == 'A4Air' and self.online.get(dev, {}).get('via') != 'ble'
            for slot, t in enumerate(row['tiles']):
                t.approx_res = approx                  # A4 Air over USB: estimated; over Bluetooth: measured
                t.update_sample(self.live.get((dev, slot)))
        self._fit_tiles()
        if self.hist_id is None:
            s = self.cur.get(self.sel)
            self.result.show_session(s, self.db.battery_of(s['id']) if s and s['id'] else None)
            if time.time() - self.last_plot > 5:     # curves are re-read from the DB, so not every second
                self.refresh_plot(keep_view=True)

    # ---------------------------------------------------------------- selection
    def _mark(self, key):
        for row in self.rows.values():
            for t in row['tiles']:
                t.set_selected(t.key == key)

    def select_slot(self, key):
        self.sel = key
        self.hist_id = None
        if key:
            self.last_slot[key[0]] = key[1]
            self._show(key[0])
        self.table.clearSelection()
        self._mark(key)
        s = self.cur.get(key)
        self.result.show_session(s, self.db.battery_of(s['id']) if s and s['id'] else None)
        self.refresh_plot()

    def refresh_plot(self, keep_view=False):
        self.last_plot = time.time()
        if self.hist_id is not None:
            d = self.db.session_dict(self.hist_id)
            if not d:
                return self.plot.clear_data()
            dev, slot, t0, t1 = d['dev'], d['slot'], d['start'], d['end']
            phases = self.db.phases(self.hist_id)
            title = ' · '.join((self.dev_name(dev), tr('Slot {}').format(slot + 1), fmt_t(t0), tr_data(d['task'])))
        else:
            s = self.cur.get(self.sel)
            if not s:
                return self.plot.clear_data(' · '.join((self.dev_name(self.sel[0]), tr('Slot {}').format(self.sel[1] + 1)))
                                            + ': ' + tr('no running session') if self.sel else '')
            dev, slot, t0, t1, phases = s['dev'], s['slot'], s['start'], s['end'], s['phases']
            title = ' · '.join((self.dev_name(dev), tr('Slot {}').format(slot + 1), fmt_t(t0), tr_data(s['task']))) + ' (live)'
        self.plot.set_data(self.db.samples(dev, slot, t0, t1 + 1), phases, title, keep_view)

    def _row_changed(self, row, _col, prev_row, _prev_col):
        if row >= 0 and row != prev_row:
            self.open_row(row, _col)

    def _row_clicked(self, row, _col):
        if self.table.item(row, 0).data(Qt.UserRole) != self.row_sid:   # not already opened by the selection
            self.open_row(row, _col)

    def open_row(self, row, _col):
        self.row_sid = self.table.item(row, 0).data(Qt.UserRole)
        self.open_session(self.row_sid)

    def _tile_clicked(self, key):
        self.row_sid = None
        self.select_slot(key)

    def open_session(self, sid):
        d = self.db.session_dict(sid)
        if not d:
            return
        live = self.cur.get((d['dev'], d['slot']))
        if live and live['id'] == sid:              # still running -> live view of its slot
            return self.select_slot((d['dev'], d['slot']))
        self.hist_id = sid
        self._mark(None)
        self._show(d['dev'])
        d['nominal'] = d['nominal'] or NOMINAL.get(d['size'], 0)
        d['phases'] = self.db.phases(sid)
        self.result.show_session(d, d['battery_id'])
        self.refresh_plot()

    # ---------------------------------------------------------------- tables / batteries
    def load_tables(self):
        self.devs = {x['id']: x for x in self.db.devices()}
        self.dtab.load()
        rows = self.db.sessions(deleted=show_deleted())
        self.table.blockSignals(True)              # refilling / reselecting must not open a session
        fill_session_table(self.table, rows, self.COLS)
        if self.hist_id is not None:
            select_by_id(self.table, self.hist_id)
        self.table.blockSignals(False)
        self.btab.load()

    def save_meta(self, sid, battery_id, nominal):
        self.worker.cmds.put(('meta', sid, battery_id, nominal))
        if self.hist_id is not None:
            QTimer.singleShot(400, lambda: self.reopen(sid))

    def reopen(self, sid):
        self.result.sid = None                     # force reload of the edit fields
        self.open_session(sid)

    def new_battery_for_session(self):
        """'New …' in the result panel: create a battery, prefilled from the detected type, and pick it."""
        d = self.result.cur
        prefill = {}
        if d:
            typ = ' '.join(x for x in (d['chem'], d['size']) if x)
            if typ in BATTERY_TYPES:
                prefill['type'] = typ
            if d.get('nominal'):
                prefill['capacity'] = d['nominal']
        bid = self.btab.add(prefill)
        if bid is not None:
            self.result.reload_batteries(select=bid)
            self.result._battery_picked()

    def battery_changed(self, bid):
        """Battery added / edited / deleted: refresh dropdown, apply its capacity to its sessions."""
        self.result.reload_batteries()
        self.mtab.load()                           # battery count per model
        if bid >= 0:
            self.worker.cmds.put(('battery', bid))
        else:
            self.load_tables()

    def models_changed(self, battery_ids):
        """Model added / edited / deleted: its batteries took over the capacity -> rate their sessions anew."""
        self.mtab.load()
        self.btab.load()
        self.result.reload_models()
        self.result.reload_batteries()
        for bid in battery_ids:
            self.worker.cmds.put(('battery', bid))

    def data_changed(self):
        """Readings were compressed: new statistics and curves."""
        self.stab.refresh_stats()
        self.refresh_plot(keep_view=True)

    def delete_sessions(self):
        """Selected sessions: soft delete (already deleted ones for good)."""
        if delete_rows(self, self.table, (tr('session'), tr('sessions')), self.db.delete_sessions,
                       self.db.hard_delete_sessions):
            self.load_tables()
            self.stab.refresh_stats()

    def deleted_changed(self):
        """Show-deleted switched or deleted entries purged: refill the lists."""
        self.btab.load()
        self.mtab.load()
        self.result.reload_models()
        self.result.reload_batteries()
        self.load_tables()
        self.stab.refresh_stats()

    def devices_renamed(self):
        self.load_tables()
        self._update_heads()
        self.refresh_plot(keep_view=True)

    def closeEvent(self, e):
        self.worker.stop()
        self.thread.quit()
        self.thread.wait(5000)
        self.hide()                            # the backup takes a few seconds for a large database
        QApplication.processEvents()
        self.db.con.close()
        try:
            close_and_backup(self.db.path)
        except (OSError, sqlite3.Error) as err:
            print(f'backup failed: {err!r}', file=sys.stderr)
        super().closeEvent(e)


def main():
    ap = argparse.ArgumentParser(prog='battbench', description=f'BattBench {full_version()}')
    ap.add_argument('--offline', action='store_true', help='only view the database, no chargers')
    ap.add_argument('--db', default=default_db(), help='database file (default: $BATTBENCH_DB, else '
                    '%%LOCALAPPDATA%%\\BattBench\\battbench.db if it exists; now: %(default)s)')
    ap.add_argument('--a4', choices=A4_MODES, help='read the A4 Air over usb, bt or both '
                                                  '(default: last choice in the app, else both)')
    ap.add_argument('--no-bt', action='store_true', help='no Bluetooth (A4 Air only over USB)')
    ap.add_argument('--lang', choices=['auto', *LANGUAGES], help='user interface language '
                                                                '(default: last choice in the app, else auto)')
    a = ap.parse_args()
    source = 'offline' if a.offline else 'device'
    pg.setConfigOptions(antialias=True)
    if sys.platform == 'win32':
        # own taskbar entry with the BattBench icon instead of python.exe's (run from source)
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Ashberg.BattBench')
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName('BattBench')
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(ICON))
    settings = QSettings('battbench', 'battbench')
    install(app, pick_language(a.lang or settings.value('lang', 'auto')))
    # settings were stored under 'isdtgui' before the rename
    a4 = 'usb' if a.no_bt else a.a4 or settings.value('a4', QSettings('isdtgui', 'isdtgui').value('a4', 'both'))
    w = MainWindow(a.db, source, a4=a4 if a4 in A4_MODES else 'both', bt=not a.no_bt)
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
