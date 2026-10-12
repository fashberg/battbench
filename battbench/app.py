"""BattBench: live view of all slots of all connected chargers (N8, A4Air; several at once), full curves,
session database and battery rating.

Start:  .venv\\Scripts\\python -m battbench [--offline] [--db battbench.db]   (or run.bat / run.sh)
  (default) read all chargers directly (USB / Bluetooth), samples go straight into the database
  --offline only view the database
  --a4 usb|bt|both  how the A4 Air is read (default: last choice in the app, else both); --no-bt: no Bluetooth at all
  --lang auto|en|de user interface language (default: last choice in the app, else the system language)
  --debug           detailed log (battbench.log next to the database), also to stderr
"""
import argparse
import json
import logging
import os
import sqlite3
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from html import escape as html_escape
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import (QByteArray, QEvent, QLocale, QObject, QPointF, QRect, QRectF, QRegularExpression,
                            QSettings, QSize, QSortFilterProxyModel, QStandardPaths, Qt, QThread, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QRegularExpressionValidator, QShortcut)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView,
                               QProgressDialog,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit,
                               QProxyStyle, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QSplitter,
                               QStyle,
                               QStyledItemDelegate, QStyleFactory, QStyleOptionViewItem, QTabWidget, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from . import __version__
from .version import AUTHOR, SOURCE, WEBSITE, full_version
from .autofilter import FILTER_ROLE, SEARCH_ROLE, SORT_ROLE, AutoFilter, SortItem
from .db import (BACKUP_DEFAULTS, COMPRESS_AFTER, DB, SESSION_FIELDS, SampleThinner, close_and_backup, prune_backups,
                 write_backup)
from .device import UsbInfo, open_charger, present, usb_devices
from .device_ble import BleManager
from .device_net import EspFinder, NetHost
from . import mc5000, report
from . import i18n, logs
from .i18n import LANGUAGES, install, pick_language, tr, tr_data
from .model import DONE as DONE_MODES
from .model import (ABORTED, CATEGORIES, CHARGE, DISCHARGE, FINISHED, GAP_SECS, GRADES, IDLE, MODE_NAMES, NO_GRADE,
                    NOMINAL, PAUSE, PHASE_MIN_MA, REMOVED, RES_QUALITY, RUNNING, TASKS, USES, Session, Tracker,
                    discharge_curve, health, measuring, note_text, rate_values, res_level)

PKG = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger('battbench.app')
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
SKYRC_MODES = ('auto', 'esp', 'direct')


def a4_mode_names():
    return {'both': tr('Automatic (Bluetooth preferred, else USB)'), 'bt': tr('Bluetooth only'),
            'usb': tr('USB only')}


def skyrc_mode_names():
    return {'auto': tr('Automatic (BattBench-ESP or Bluetooth of this PC)'), 'esp': tr('Only through a BattBench-ESP'),
            'direct': tr('Only over Bluetooth of this PC')}


def skyrc_unused_names():
    """Why this PC's Bluetooth plays no part for SkyRC chargers (Worker._local_bt)."""
    return {'esp': tr('not used (SkyRC chargers only through a BattBench-ESP)'),
            'off': tr('off (started with --no-bt)'), 'not ready': tr('not ready (Bluetooth search fails), ignored')}


COLORS = {'charge': '#e0a000', 'discharge': '#d0342c', 'analysis': '#3a7bd5', 'done': '#2e9e4f',
          'empty': '#9a9a9a', 'idle': '#f28c28', 'error': '#d0342c'}
BLINK_MS = 500                    # error slots blink (title bar, status bar, LED in the charger tab)
GRADE_COLORS = dict(zip(GRADES + (NO_GRADE,), ('#2e9e4f', '#6aa84f', '#e69138', '#d0342c', '#d0342c', '#888888')))
PHASE_COLORS = {CHARGE: COLORS['charge'], DISCHARGE: COLORS['discharge']}


def is_idle(s):
    """Live sample of an occupied slot without a task (SkyRC: cell in, ready); mode 0 otherwise means empty."""
    return s['mode'] == 0 and s['size'] != 'empty' and s['mv'] > 0


def sample_color(s):
    return COLORS['idle'] if is_idle(s) else mode_color(s['mode'], s['ma'])


def mode_color(mode, ma):
    if mode == 0:
        return COLORS['empty']
    if mode in (2, 20):
        return COLORS['error']
    if mode in (4, 6, 8, 10, 12, 14):
        return COLORS['done']
    if mode in (9, 11, 13):                       # discharging is red in every task
        return COLORS['discharge'] if ma < 0 else COLORS['analysis']
    return COLORS['discharge'] if ma < 0 else COLORS['charge']


def fmt_t(t):
    return datetime.fromtimestamp(t).strftime(tr('%b %d, %H:%M')) if t else ''


def hms(t):
    return datetime.fromtimestamp(t).strftime('%H:%M:%S') if t else ''


# connection symbols, drawn in the text colour of the current palette (light and dark theme)
VIA_SVG = {
    'usb': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v15M12 14l-5-3V8M12 16l5-3v-3"/>'
           '<path d="M10 5.5 12 2.5l2 3z" fill="{c}"/><circle cx="7" cy="6.5" r="1.6" fill="{c}"/>'
           '<rect x="15.5" y="7" width="3" height="3" fill="{c}"/><circle cx="12" cy="19.5" r="2" fill="{c}"/></svg>',
    'ble': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"><path d="M7 7l10 10-5 5V2l5 5L7 17"/></svg>',
    'net': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0'
           'M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1.4" fill="{c}"/></svg>',
}
VIA_NAME = {'usb': 'USB', 'ble': 'Bluetooth', 'net': 'BattBench-ESP'}


def via_text(on):
    """Connections of a charger for tooltips and tables, e.g. "BattBench-ESP (10.10.200.168)"."""
    return ' + '.join(VIA_NAME[v] + (f" ({on['net_host']})" if v == 'net' and on.get('net_host') else '')
                      for v in on.get('vias', ()))


def via_pixmap(vias, color, size=14):
    """USB / Bluetooth / network symbols side by side."""
    pm = QPixmap(size * len(vias) + 2 * max(len(vias) - 1, 0), size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    for i, v in enumerate(vias):
        QSvgRenderer(QByteArray(VIA_SVG[v].replace('{c}', color).encode())).render(
            p, QRectF(i * (size + 2), 0, size, size))
    p.end()
    return pm


def session_notes(deleted, compressed):
    """' (4 deleted, 60 compressed)' after the number of sessions, or ''."""
    notes = ([tr('{} deleted').format(deleted)] if deleted else []) + \
        ([tr('{} compressed').format(compressed)] if compressed else [])
    return f" ({', '.join(notes)})" if notes else ''


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
                                 is_measuring(d), d['chem'], d.get('v_start'), d.get('v_mid'), d.get('early_drop'),
                                 d.get('res_est'), d.get('stop_reason') or '')[1], tr_data)


def grade_cell(grade, ohi):
    """Rating in a table: (text, sort key, filter value), e.g. ("98 · A · high", 98, "A · high drain"). Sorted by
    the health index; suspicious below the rated ones, no rating empty."""
    short = dict(zip(CATEGORIES, (tr('high'), tr('medium'), tr('low'), tr('recycle'))))
    text = tr_data(grade or NO_GRADE)
    if grade in short and ohi is not None:
        return f'{ohi} · {grade[0]} · {short[grade]}', ohi, text
    return text, (-1 if grade == GRADES[4] else None), text


def health_of(d):
    """Health index of a session dict (model.health), None while measuring or without capacities."""
    if is_measuring(d):
        return None
    return health(d['discharge_mah'], d['nominal'] or NOMINAL.get(d['size'], 0),
                  d['charge_mah'] if d['status'] == FINISHED else None,
                  None if d.get('res_est') else d['res_min'], d['chem'], d.get('v_start'), d.get('v_mid'),
                  d.get('early_drop'))


def session_info(s: Session):
    """Plain dict of a live session, safe to send to the GUI thread."""
    grade, note, ohi = s.rating()
    return dict(id=s.db_id, dev=s.dev, slot=s.slot, start=s.start, end=s.end, task=s.task, chem=s.chem, size=s.size,
                status=s.status, nominal=s.nominal or NOMINAL.get(s.size, 0), discharge_mah=s.discharge_mah,
                charge_mah=s.charge_mah, res_first=s.res_first, res_min=s.res_min, res_last=s.res_last,
                temp_max=s.temp_max, grade=grade, note=note, ohi=ohi, v_start=s.v_start, v_mid=s.v_mid,
                early_drop=s.early_drop, res_est=s.res_est, stop_reason=s.stop_reason, paused=s.paused,
                phases=[(p.kind, p.start, p.end, p.mah) for p in s.phases])


# ======================================================================= worker
STALE_SECS = 30           # no readings from a connected charger for this long -> shown as offline
VIA_ORDER = ('ble', 'net', 'usb')    # data is taken from the first one of a charger's connections that delivers


def via_of(r):
    """'ble' for a Bluetooth reader, 'net' for a charger behind a BattBench-ESP, 'usb' for a USB one."""
    return 'ble' if r.usb.path.startswith(b'ble:') else 'net' if r.usb.path.startswith(b'net:') else 'usb'


class Reader(threading.Thread):
    """One per connected charger: opens it, then reads all slots once per second and hands the
    samples to the worker. Only USB I/O here; the worker owns tracker and database.
    A charger that stops answering while still plugged in posts empty batches (the worker shows it as offline);
    after REOPEN_SECS its HID handle is opened again, and again every REOPEN_SECS."""
    REOPEN_SECS = 60

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
            log.warning('opening %s (%s) failed: %s', self.usb.product, self.usb.path.decode(errors='replace'), e)
            self.out.put(('fail', self, str(e)))
            return
        self.out.put(('open', self))
        if not self.ready.wait(10):
            self.charger.close()
            return
        c = self.charger
        self.name = threading.current_thread().name = f'{c.label}-usb'
        log.info('%s opened: %s, key %s, HID %s', c.label, c.version, c.key, self.usb.path.decode(errors='replace'))
        last_ok = next_reopen = time.time()
        quiet = False                                   # the charger answers nothing at all
        missed, next_report = {}, time.time() + 60      # slot -> [count, last reason], summed up once a minute
        while self.running:
            t0 = time.time()
            batch, in_mv = [], None
            try:
                silent = 0
                for slot in range(c.slots):
                    for _ in range(2):                  # one retry for a lost answer
                        s = c.read_slot(slot)
                        if s:
                            s.dev = self.dev
                            batch.append(s)
                            break
                        log.debug('%s slot %d: %s', c.label, slot + 1, c.why)
                    if s:
                        silent = 0
                        continue
                    m = missed.setdefault(slot + 1, [0, ''])
                    m[0], m[1] = m[0] + 1, c.why
                    silent += 1
                    if not batch and silent >= 2:       # charger silent: don't wait for every slot
                        break
                if batch:
                    in_mv = c.input_mv()
            except Exception as e:
                if not present(self.usb.path):          # unplugged -> worker reconnects when it is back
                    log.warning('%s unplugged: %r', c.label, e)
                    self.out.put(('lost', self, repr(e)))
                    break
                log.warning('%s: error while reading (still plugged in): %r', c.label, e)
            if batch:
                if quiet:
                    log.warning('%s answers again after %.0f s', c.label, t0 - last_ok)
                last_ok, quiet = t0, False
            elif not quiet and t0 - last_ok > 5:
                log.warning('%s: no answer since %s (%s)', c.label, hms(last_ok), c.why)
                quiet = True
            self.out.put(('data', self, batch, in_mv))
            if t0 >= next_report:
                if missed:
                    log.info('%s: unanswered queries in the last minute: %s', c.label,
                             ', '.join(f'slot {k} {n}x ({why})' for k, (n, why) in sorted(missed.items())))
                missed, next_report = {}, t0 + 60
            if quiet and t0 - last_ok > self.REOPEN_SECS and t0 >= next_reopen:
                next_reopen = t0 + self.REOPEN_SECS     # USB link still there: open the HID handle again
                try:
                    c.reopen()
                    log.warning('%s: HID handle reopened', c.label)
                except Exception as e:
                    log.warning('%s: reopening the HID handle failed: %r', c.label, e)
            end = t0 + 1.0
            while self.running and time.time() < end:
                time.sleep(0.05)
        c.close()
        log.info('%s reader stopped', c.label)


class Worker(QObject):
    """Runs in its own thread: finds the chargers (one Reader each), stores samples, tracks sessions.
    Owns its own DB connection."""
    cycle = Signal(object)        # dict(live={(dev, slot): sample}, sessions={(dev, slot): info}, devices={dev: …})
    status = Signal(str)
    sessions_changed = Signal()
    data_changed = Signal()       # readings compressed: statistics and curves are out of date
    command_result = Signal(bool, str, object)   # charger command done: ok, message, dict(dev, slots, prog, pid)
    charger_info = Signal(object)    # MC5000 answer: dict(kind 'summary' / 'settings', dev, slot, data, error, text)
                                                  # of a started program (else None)

    skyrc_changed = Signal(str)   # SKYRC_MODES key: switched to 'esp' by itself (first BattBench-ESP connection)

    def __init__(self, db_path, source, a4='both', bt=True, esp=(), esp_auto=True, skyrc='auto', skyrc_chosen=True):
        super().__init__()
        self.db_path = db_path
        self.skyrc = skyrc        # SKYRC_MODES key: how SkyRC chargers are connected
        self.skyrc_chosen = skyrc_chosen   # False: still the default, the first BattBench-ESP makes it 'esp'
        self.source = source      # 'device' / 'offline'
        self.a4 = a4              # A4_MODES key: how the A4 Air is read
        self.bt = bt              # look for Bluetooth chargers (A4 / A8 / C4 Air, MC5000)
        self.ble = None
        self.esp = list(esp)      # host names / addresses of BattBench-ESP bridges entered by hand
        self.esp_auto = esp_auto  # also the ones found by mDNS
        self.finder = None        # EspFinder
        self.found = {}           # ESP id -> host of the bridges found so far (kept when one search misses it)
        self.nets = {}            # host -> NetHost
        self.running = True
        self.cmds = queue.Queue()
        self.inbox = queue.Queue()
        self.readers = {}         # HID path -> Reader
        self.skip = {}            # HID path -> reason (unsupported model / open failed) until unplugged
        self.online = {}          # dev -> dict(version, in_mv, via, vias) of connected chargers
        self.conns = {}           # dev -> {'usb' / 'ble': reader}: a charger may be connected both ways
        self.same = {}            # (dev, dev) -> deque of reading comparisons (True = same readings)
        self.merged = []          # devices merged into another one since the last emit (for the GUI)
        self.bt_addrs, self.bt_addrs_t = {}, 0     # dev -> Bluetooth address, read at
        self.bt_mirrored = {}     # address -> pause taken over from a BattBench-ESP (_bt_mirror)
        self.gaps = {}            # (dev, slot) -> [(t0, t1)] without readings, to fill from the charger's curve
        self.gap_asked = {}       # (dev, slot) -> time the curve was last asked for

    def stop(self):
        self.running = False

    # ---------------------------------------------------------------- setup
    def run(self):
        threading.current_thread().name = 'worker'     # in the log
        self.db = DB(self.db_path)
        self.tracker = Tracker()
        self.status.emit(tr('Loading database …'))
        self.db.resume(self.tracker)
        if self.db.rate_missing():                # e.g. analysed before the health index existed
            self.sessions_changed.emit()
        self.latest = dict(self.tracker.last)
        self.sessions_changed.emit()
        self._emit()
        self.next_compress = 0
        self.last_backup = time.time()            # the app makes one on exit; the next one after the interval
        self.next_backup_check = 0
        if self.source == 'offline':
            self.status.emit(tr('No chargers (database only)'))
            while self.running:
                self._commands()
                self._auto_compress()
                self._auto_backup()
                time.sleep(0.2)
        else:
            self._run()
        self.db.con.close()

    # ---------------------------------------------------------------- main loop
    def _run(self):
        """Reads every charger directly."""
        nxt_scan = nxt_save = last_emit = 0
        thinner = SampleThinner()                       # keep the DB small: one reading per slot every 5 s
        dirty = False
        self._status()
        self._apply_a4()
        self._apply_esp()
        while self.running:
            self._commands()
            self._auto_compress()
            self._auto_backup()
            self._fill_gaps()
            now = time.time()
            if now >= nxt_scan:
                nxt_scan = now + 3
                self._scan()
                if self.finder and any(self.found.get(k) != v['host'] for k, v in self.finder.found.items()):
                    self._apply_esp()
            try:
                msg = self.inbox.get(timeout=0.2)
            except queue.Empty:
                msg = None
            if msg and msg[0] == 'cursor':           # a BattBench-ESP: readings up to here are stored
                self.db.set_setting(msg[1], msg[2])
                dirty = True
            elif msg and msg[0] == 'history':        # voltages from the charger for a gap in the readings
                self.db.add_history(*msg[1:])
                dirty = True
                self.data_changed.emit()
            elif msg:
                kind, r = msg[0], msg[1]
                if kind == 'open':
                    c = r.charger
                    r.dev = self.db.device_for(c.key, c.model, c.label, c.slots, c.version)
                    for slot in range(c.slots):
                        last = self.tracker.last.get((r.dev, slot))
                        if last and now - last.t < GAP_SECS:
                            c.seed(slot, last)
                    r.last_ok = now
                    self.conns.setdefault(r.dev, {})[via_of(r)] = r
                    self._update_online(r.dev)
                    log.info('device %d: %s connected via %s', r.dev, c.version, via_of(r))
                    if via_of(r) == 'net' and self.skyrc == 'auto' and not self.skyrc_chosen:
                        self.skyrc, self.skyrc_chosen = 'esp', True     # from now on only through the ESP
                        log.info('SkyRC chargers: BattBench-ESP only from now on')
                        self._apply_a4()
                        self.skyrc_changed.emit('esp')
                    r.ready.set()
                    self._status()
                    self.sessions_changed.emit()        # new device name in the tables
                elif kind == 'data':
                    _, _, batch, in_mv = msg
                    if r.dev is None or self.conns.get(r.dev, {}).get(via_of(r)) is not r:
                        continue                        # reader of a device that was merged / dropped
                    if via_of(r) == 'net':              # never back in time (readings fetched again)
                        batch = [s for s in batch if not (last := self.latest.get((s.dev, s.slot))) or s.t > last.t]
                    if batch:
                        r.last_ok = now
                    self._compare(r.dev, batch)
                    if self.online[r.dev]['via'] != via_of(r):
                        continue                        # second connection: data comes from the better one
                    store = []
                    mc5000_conn = r.charger.model == 'MC5000' and via_of(r) in ('ble', 'net')
                    for s in batch:
                        prev = self.latest.get((s.dev, s.slot))
                        if mc5000_conn and prev and s.t - prev.t > self.GAP_FILL_SECS and s.mode in TASKS:
                            self.gaps.setdefault((s.dev, s.slot), []).append((prev.t, s.t))
                        self.latest[(s.dev, s.slot)] = s
                        self.tracker.feed(s)
                        store += thinner.add(s)
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
                    log.warning('%s %s via %s: %s', r.charger.label if r.charger else r.usb.product, kind,
                                via_of(r), msg[2])
                    self._status(f"{r.charger.label if r.charger else r.usb.product}: {why}")
                    dirty = True
            if self._check_stale(now):
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
        for h in self.nets.values():
            h.stop()
        if self.finder:
            self.finder.running = False
        self.db.add_samples(thinner.flush())
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
            for addr in self._bt_off():                 # switched off for the SkyRC app, kept over a restart
                self.ble.hold_charger(addr, None)
            self.ble.start()
        if self.ble:
            self.ble.ignore = ({'A4Air'} if self.a4 == 'usb' else set()) | \
                ({'SkyRC'} if self.skyrc == 'esp' else set())   # SkyRC only through the BattBench-ESP
            for r in list(self.ble.active.values()):
                if r.model in self.ble.ignore:
                    r.running = False                   # its connection posts 'lost' when it ends
        if self.a4 == 'bt':
            for r in [x for x in self.readers.values() if x.usb.product == 'CH57x']:
                r.running = False
                self.readers.pop(r.usb.path, None)
                self._drop(r)
        self._status()

    def _apply_esp(self):
        """One NetHost per BattBench-ESP entered by hand and per one found by mDNS (unless it is one of those entered,
        same id); removed ones end their chargers' connections. A found one whose address changed moves along."""
        if self.skyrc == 'direct':                      # SkyRC only over this PC's Bluetooth: no ESP
            if self.finder:
                self.finder.running = False
                self.finder, self.found = None, {}
            for host in list(self.nets):
                self.nets.pop(host).stop()
            return
        if self.esp_auto and not (self.finder and self.finder.is_alive()):
            self.finder = EspFinder()
            self.finder.start()
        elif not self.esp_auto and self.finder:
            self.finder.running = False
            self.finder, self.found = None, {}
        if self.finder:
            for k, v in self.finder.found.items():
                if self.found.get(k) != v['host']:
                    log.info('BattBench-ESP %s found at %s', k, v['host'])
                    self.found[k] = v['host']
        manual = {n.esp_id for h, n in self.nets.items() if h in self.esp and n.esp_id}
        auto = {host for k, host in self.found.items() if k not in manual and host not in self.esp}
        wanted = set(self.esp) | auto
        for host in [h for h in self.nets if h not in wanted]:
            self.nets.pop(host).stop()
        cursors = {k: int(v) for k, v in self.db.con.execute("SELECT key, value FROM settings WHERE key LIKE 'esp:%'")}
        for host in sorted(wanted):
            if host not in self.nets:
                self.nets[host] = NetHost(host, self.inbox, cursors)
                self.nets[host].auto = host in auto
                self.nets[host].start()

    # ---------------------------------------------------------------- connections (USB / Bluetooth / network)
    def _update_online(self, dev, now=None):
        """online[dev] from its connections; data is taken from Bluetooth when there is both (more values), unless
        only the USB connection still delivers readings."""
        conns = self.conns.get(dev)
        if not conns:
            self.online.pop(dev, None)
            self.conns.pop(dev, None)
            return
        fresh = [v for v, r in conns.items() if not self._stale(r, now)]
        via = next(v for v in VIA_ORDER if v in (fresh or conns))
        old = self.online.get(dev, {})
        self.online[dev] = dict(version=conns[via].charger.version, in_mv=old.get('in_mv'), via=via,
                                vias=sorted(conns), stale=None if fresh else max(r.last_ok for r in conns.values()),
                                net_host=conns['net'].host if 'net' in conns else None)

    def _stale(self, r, now=None):
        """No readings from this connection for STALE_SECS (charger does not answer, or its reader hangs)."""
        return (now or time.time()) - r.last_ok > STALE_SECS

    def _check_stale(self, now):
        """Update online[dev]['stale'] (time of the last reading, None = answering). True if one changed."""
        changed = False
        for dev in list(self.online):
            old = self.online[dev].get('stale')
            self._update_online(dev, now)
            new = self.online[dev].get('stale')
            if (old is None) != (new is None):
                changed = True
                label = self.conns[dev][self.online[dev]['via']].charger.label
                if new is None:
                    log.warning('device %d (%s): readings again (offline since %s)', dev, label, hms(old))
                    self._status(tr('{}: answering again').format(label))
                else:
                    log.warning('device %d (%s): offline, no readings since %s', dev, label, hms(new))
                    self._status(tr('{}: no answer since {}').format(label, hms(new)))
        return changed

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
                             merged=self.merged, bt_holds=self._bt_holds()))
        self.merged = []

    def _auto_backup(self):
        """Setting 'backup_hours': a backup every so many hours while running (checked once a minute)."""
        now = time.time()
        if now < self.next_backup_check:
            return
        self.next_backup_check = now + 60
        policy = backup_policy()
        if policy['hours'] > 0 and now - self.last_backup >= policy['hours'] * 3600:
            self.last_backup = now
            threading.Thread(target=self._backup, args=(policy,), name='backup').start()

    def _backup(self, policy):
        """In its own thread with its own connection: copying and packing a large database takes a while."""
        try:
            target = write_backup(self.db_path)
            prune_backups(self.db_path, policy['keep'], policy['daily'], policy['weekly'], policy['monthly'])
            self.status.emit(tr('Backup saved: {}').format(os.path.basename(target)))
        except (OSError, sqlite3.Error) as e:
            self.status.emit(tr('Backup failed: {}').format(repr(e)))

    def _auto_compress(self):
        """Setting 'compress': readings older than two weeks down to one per minute, at start and once a day."""
        if time.time() < self.next_compress:
            return
        self.next_compress = time.time() + 86400
        if QSettings('battbench', 'battbench').value('compress', True, type=bool):
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
            elif cmd[0] == 'a4':
                self.a4 = cmd[1]
                if self.source != 'offline':
                    self._apply_a4()
            elif cmd[0] == 'esp':
                self.esp, self.esp_auto = list(cmd[1]), cmd[2]
                if self.source != 'offline':
                    self._apply_esp()
            elif cmd[0] == 'skyrc':
                self.skyrc, self.skyrc_chosen = cmd[1], True
                if self.source != 'offline':
                    self._apply_a4()
                    self._apply_esp()
            elif cmd[0] == 'label':
                self.db.set_session_meta(cmd[1], label=cmd[2])
            elif cmd[0] == 'mc5000':
                self._mc5000(*cmd[1:])
            elif cmd[0] == 'mc5000_ask':
                self._mc5000_ask(*cmd[1:])
            elif cmd[0] == 'bt':
                self._bt(*cmd[1:])
            elif cmd[0] == 'battery':
                b = self.db.battery(cmd[1])
                if b and b['capacity']:
                    for r in self.db.battery_sessions(cmd[1]):
                        self._set_nominal(r[0], b['capacity'])
            changed = True
        if changed:
            self.sessions_changed.emit()
            self._emit()

    GAP_FILL_SECS = 120                   # readings this far apart in a running MC5000 task: ask for its curve
    GAP_ASK_SECS = 60                     # at most so often per slot

    def _fill_gaps(self):
        """Gaps in the readings of a running MC5000 task (BattBench and the ESP were off, Bluetooth paused ...):
        the charger keeps the voltage curve of the slot's current phase (98); its values inside a gap are stored
        apart (DB.add_history) and shown in the chart. Earlier phases are gone on the charger."""
        now = time.time()
        for key in list(self.gaps):
            if now - self.gap_asked.get(key, 0) < self.GAP_ASK_SECS:
                continue
            conn = self._mc5000_conn(key[0])
            gaps = self.gaps.pop(key)
            if conn is None:
                continue
            self.gap_asked[key] = now
            threading.Thread(target=self._fill_gaps_run, args=(conn, key, gaps), daemon=True,
                             name='mc5000-history').start()

    def _fill_gaps_run(self, conn, key, gaps):
        try:
            reply = conn.command(mc5000.history_packet(key[1]))
            t_end = time.time()
        except Exception as e:
            log.warning('MC5000 voltage curve of slot %d: %s', key[1] + 1, e)
            return
        curve = mc5000.parse_history(mc5000.split_frames(reply))
        if not curve:
            log.info('MC5000 voltage curve of slot %d: none (%s)', key[1] + 1, reply[:8].hex(' ') or 'no answer')
            return
        interval, volts = curve
        points = [(t, mv) for t, mv in mc5000.history_points(interval, volts, t_end)
                  if any(a + interval / 2 < t < b - interval / 2 for a, b in gaps)]
        log.info('MC5000 voltage curve of slot %d: %d values every %d s, %d fill gaps %s', key[1] + 1, len(volts),
                 interval, len(points), ', '.join(f'{hms(a)}-{hms(b)}' for a, b in gaps))
        if points:
            self.inbox.put(('history', key[0], key[1], points))

    def _mc5000_conn(self, dev):
        conns = self.conns.get(dev, {})
        return next((conns[v] for v in ('ble', 'net') if hasattr(conns.get(v), 'command')), None)

    BT_ACTIONS = {'status': None, 'pause': 5, 'off': None, 'on': 0}      # action -> minutes for hold_charger

    def _bt_off(self):
        try:
            return set(json.loads(self.db.setting('bt_off') or '[]'))
        except ValueError:
            return set()

    def _bt_addr(self, d):
        """Bluetooth address of a device (dict from db.devices), None for USB-only chargers."""
        keys = [k for k in (d.get('key'), d.get('alt_key')) if k and '-ble-' in k]
        return keys[0].split('-ble-', 1)[1].upper() if keys else None

    def _bt_holds(self):
        """dev -> time until which its Bluetooth is paused (inf = switched off), from BattBench's own Bluetooth and
        the BattBench-ESPs; devices without a pause are left out."""
        now = time.time()
        if now - self.bt_addrs_t > 30:                      # device list: read again now and then
            self.bt_addrs = {d['id']: a for d in self.db.devices() if (a := self._bt_addr(d))}
            self.bt_addrs_t = now
        out = {}
        local = self._local_bt() is None
        for dev, addr in self.bt_addrs.items():
            if any(n.ble_connected(addr) for n in self.nets.values()):
                self.bt_mirrored.pop(addr, None)            # switched on again on the ESP: no pause here either
                if self.ble and self.ble.held(addr) is not None:
                    self.ble.hold_charger(addr, 0)
                    self._bt_forget_off(addr)
                continue
            esp = [h for h in (n.ble_until(addr) for n in self.nets.values()) if h is not None and h > now]
            if local:
                self._bt_mirror(addr, max(esp) if esp else None)
            holds = esp + [h for h in [self.ble.held(addr) if local else None] if h is not None and h > now]
            if holds:
                out[dev] = max(holds)
        return out

    def _local_bt(self):
        """Why this PC's Bluetooth plays no part for SkyRC chargers ('esp': setting, 'off': --no-bt or not running,
        'not ready': its search fails), None when it does."""
        if self.skyrc == 'esp':
            return 'esp'
        if not (self.ble and self.ble.is_alive()):
            return 'off'
        return None if self.ble.ok else 'not ready'

    def _bt_forget_off(self, addr):
        off = self._bt_off()
        if addr in off:
            self.db.set_setting('bt_off', json.dumps(sorted(off - {addr})))

    def _bt_mirror(self, addr, until):
        """Paused / switched off on a BattBench-ESP (e.g. on its web page): keep BattBench's own Bluetooth away from
        the charger as long, else it would take the charger at once; let it go again when the ESP does."""
        if not self.ble:
            return
        mine, held = self.bt_mirrored.get(addr), self.ble.held(addr)
        if until is not None and held in (None, mine):
            if mine is None or abs(until - mine) > 5:        # the ESP's countdown, asked every few seconds
                self.ble.hold_until(addr, until)
                self.bt_mirrored[addr] = until
        elif until is None and mine is not None:
            if held == mine:
                self.ble.hold_charger(addr, 0)
            del self.bt_mirrored[addr]

    def _bt(self, dev, action):
        """Bluetooth of a SkyRC charger (it takes only one connection): action 'pause' (5 min), 'off', 'on' or
        'status'. Applies to BattBench's own Bluetooth and to every BattBench-ESP that knows the charger, otherwise
        the other one would take it at once. The state comes as charger_info kind 'bt'."""
        d = next((x for x in self.db.devices() if x['id'] == dev), None)
        addr = self._bt_addr(d) if d else None
        if addr is None:
            self.charger_info.emit(dict(kind='bt', dev=dev, data=None, error=tr('No Bluetooth charger.')))
            return
        local = self._local_bt()
        if action != 'status':
            off = self._bt_off()
            off = off | {addr} if action == 'off' and local is None else off - {addr}
            self.db.set_setting('bt_off', json.dumps(sorted(off)))
            if self.ble:
                self.ble.hold_charger(addr, self.BT_ACTIONS[action] if local is None else 0)
            log.info('Bluetooth of %s: %s', addr, action)
        until = self.ble.held(addr) if self.ble and local is None else None
        direct = dict(unused=local, until=until, connected='ble' in self.conns.get(dev, {}))
        hosts = [h for h in self.nets.values()
                 if any(c.get('addr', '').upper() == addr for c in (h.info or {}).get('chargers', []))]
        if action != 'status':
            self.bt_mirrored.pop(addr, None)
            for h in hosts:
                h.ble = None                        # old state until _bt_run asked again: not mirrored back
        threading.Thread(target=self._bt_run, args=(dev, action, direct, hosts), daemon=True, name='bt').start()

    def _bt_run(self, dev, action, direct, hosts):
        esp, errors = [], []
        # switching on ends a pause; a pause of a switched off ESP switches it on afterwards
        bodies = {'pause': [{'on': True}, {'pause': 5}], 'off': [{'on': False}],
                  'on': [{'pause': 0}, {'on': True}]}.get(action, [])
        for h in hosts:
            try:
                for body in bodies:
                    h._post('/api/ble', body)
                ble = h.ask_ble()                       # also for the notice on the tiles
                esp.append(dict(host=h.host, url=h.base, state=ble.get('state', ''), on=ble.get('on', True),
                                paused_secs=ble.get('paused_secs', 0)))
            except Exception as e:
                log.warning('BattBench-ESP %s: Bluetooth %s failed: %s', h.host, action, e)
                errors.append(f'BattBench-ESP {h.host}: {e}')
                esp.append(dict(host=h.host, url=h.base, state='', on=None, paused_secs=0))
        self.charger_info.emit(dict(kind='bt', dev=dev, data=dict(direct=direct, esp=esp),
                                    error='; '.join(errors) or None))

    def _mc5000_ask(self, dev, kind, arg=None):
        """kind 'summary': slot summary of slot arg (read only); 'settings': read the system settings, or with arg
        (mc5000.Settings as dict) set them first. The answer comes as charger_info."""
        conn = self._mc5000_conn(dev)
        if conn is None:
            self.charger_info.emit(dict(kind=kind, dev=dev, slot=arg if kind == 'summary' else None, data=None,
                                        error=tr('The charger is not connected over Bluetooth or a BattBench-ESP.')))
            return
        threading.Thread(target=self._mc5000_ask_run, args=(conn, dev, kind, arg), daemon=True,
                         name='mc5000-ask').start()

    def _mc5000_ask_run(self, conn, dev, kind, arg):
        out = dict(kind=kind, dev=dev, slot=None, data=None, error=None, text=None)
        try:
            if kind == 'summary':
                out['slot'] = arg
                reply = conn.command(mc5000.summary_packet(mc5000.SLOT_MASK[arg]))
                out['data'] = mc5000.parse_summary(reply)
            else:
                if arg is not None:
                    pkt = mc5000.settings_packet(mc5000.Settings(**arg))
                    reply = conn.command(pkt)
                    log.info('MC5000 command %s -> %s', pkt.hex(' '), reply.hex(' ') or 'no answer')
                    if not mc5000.acked(reply, 0x95):
                        raise RuntimeError(tr('the charger did not take the settings ({})').format(
                            reply.hex(' ') or tr('no answer')))
                reply = conn.command(mc5000.settings_query())
                got = mc5000.parse_settings(reply)
                out['data'] = asdict(got) if got else None
                if arg is not None:
                    if got is None or asdict(got) != arg:
                        raise RuntimeError(tr('the charger reports other settings than were sent'))
                    out['text'] = tr('Charger settings saved.')
            if out['data'] is None:
                out['error'] = tr('no valid answer ({})').format(reply.hex(' ') or tr('no answer'))
        except Exception as e:
            log.warning('MC5000 %s failed: %s', kind, e)
            out['error'] = str(e)
        self.charger_info.emit(out)

    def _mc5000(self, dev, slots, prog, pid=None):
        """Program and start (prog = mc5000.Program as dict) or stop (prog None) slots of an MC5000, over its
        Bluetooth connection or the BattBench-ESP. Runs in its own thread: the charger answers between its status
        queries, which takes a few seconds."""
        conn = self._mc5000_conn(dev)
        if conn is None:
            self.command_result.emit(False, tr('The charger is not connected over Bluetooth or a BattBench-ESP.'),
                                     None)
            return
        threading.Thread(target=self._mc5000_run, args=(conn, dev, slots, prog, pid), daemon=True,
                         name='mc5000-command').start()

    START_WAIT = 30                       # s: until the charger shows a new program and has started it
    CLOCK_SLACK = 3                       # s: readings over a BattBench-ESP carry the ESP's time

    def _raw(self, dev, slot):
        """(raw status answer, time) of the newest reading of a slot."""
        smp = self.latest.get((dev, slot))
        try:
            return bytes.fromhex(smp.raw), smp.t
        except (AttributeError, TypeError, ValueError):
            return b'', 0

    def _mc5000_run(self, conn, dev, slots, prog, pid):
        names = ', '.join(str(s + 1) for s in slots)

        def send(pkt):
            reply = conn.command(pkt)
            log.info('MC5000 command %s -> %s', pkt.hex(' '), reply.hex(' ') or 'no answer')
            return reply
        try:
            if prog is None:
                for s in slots:
                    send(mc5000.stop_packet(mc5000.SLOT_MASK[s]))
                self.command_result.emit(True, tr('Slot {} stopped.').format(names), None)
                return
            p = mc5000.Program.from_dict(prog)
            sent = {}
            for s in slots:
                if mc5000.working(self._raw(dev, s)[0]):     # a working slot would skip the program
                    send(mc5000.stop_packet(mc5000.SLOT_MASK[s]))
                reply = send(mc5000.program_packet(mc5000.SLOT_MASK[s], p))
                if not mc5000.acked(reply, 0x94):
                    raise RuntimeError(tr('slot {}: the charger did not take the program ({})').format(
                        s + 1, reply.hex(' ') or tr('no answer')))
                sent[s] = time.time()
            # the answer is 01 even for a skipped slot: wait for the status to show the program's battery type and
            # task; with a battery in, the charger starts by itself after a check and a resistance measurement
            want, taken, running = (p.chem, p.mode), set(), set()
            end = time.time() + self.START_WAIT
            while time.time() < end and len(running) < len(slots):
                time.sleep(1)
                for s in slots:
                    raw, t = self._raw(dev, s)
                    if t > sent[s] - self.CLOCK_SLACK and mc5000.from_status(raw) == want:
                        taken.add(s)
                        if mc5000.started(raw):
                            running.add(s)
            lost = [s for s in slots if s not in taken]
            if lost:
                raise RuntimeError(tr('slot {}: the charger does not show the program').format(
                    ', '.join(str(s + 1) for s in lost)))
            text = tr('Profile "{}" started on slot {}.').format(p.name, ', '.join(str(s + 1) for s in sorted(running)))
            waiting = sorted(set(slots) - running)
            if waiting:
                text = (text + ' ' if running else '') + tr(
                    'Slot {}: programmed, starts when a battery is inserted.').format(
                    ', '.join(str(s + 1) for s in waiting))
            self.command_result.emit(True, text, dict(dev=dev, slots=slots, prog=prog, pid=pid))
        except Exception as e:
            log.warning('MC5000 command failed: %s', e)
            self.command_result.emit(False, tr('The charger command failed: {}').format(e), None)

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
        self.text = None
        self.setFixedHeight(16)
        self.timer = QTimer(self)
        self.timer.setInterval(1000 // self.FPS)
        self.timer.timeout.connect(self._tick)

    def set_state(self, value, color, direction, text=None):
        """direction: +1 charging, -1 discharging, 0 no animation. text instead of "n %" (status bar of chargers
        without a progress value)."""
        self.value, self.color, self.direction = max(0, min(100, value)), QColor(color), direction
        self.text = text
        self._run()
        self.update()

    def set_color(self, color):
        self.color = QColor(color)
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
        text = self.text if self.text is not None else f'{self.value} %'
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


def status_word(s):
    """Short state of a running slot for the status bar: charging, discharging, rest, done or the error."""
    if s['mode'] in DONE_MODES:
        return tr_data(FINISHED)
    if s['mode'] in (2, 20):
        return mode_name(s)
    if s['mode_str'] == PAUSE:
        return tr_data(PAUSE)
    d = 1 if s['ma'] >= PHASE_MIN_MA else -1 if s['ma'] <= -PHASE_MIN_MA else 0
    return tr_data(MODE_NAMES[3] if d > 0 else MODE_NAMES[5]) if d else mode_name(s)


class SlotTile(QFrame):
    clicked = Signal(object)              # (device id, slot)
    menu = Signal(object, object)         # (device id, slot), global position: right click

    def __init__(self, key, compact=False):
        super().__init__()
        self.key = key
        self.compact = compact            # narrow tile (N16 / N24): values one per line
        self.approx_res = False           # resistance is only an estimate (A4Air)
        self.status_bar = False           # charger without progress %: the bar shows state and voltage instead
        self.color = COLORS['empty']
        self.blinking = False             # error: blinks (MainWindow's blink timer)
        self.chosen = False               # chosen in the charger control panel
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)     # all columns equally wide
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(2)
        self.head = QLabel()
        self.head.setFixedHeight(HEAD_HEIGHT)
        self.set_battery(None)
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

    def set_battery(self, bid, rating=''):
        """Title: slot number, plus the ID of the battery assigned to the session in the slot and its rating
        (as in the tables, e.g. "Slot 4 – #5 · 78 · B · medium")."""
        slot = self.key[1] + 1
        text = f'{slot}' if self.compact else tr('Slot {}').format(slot)
        self.head.setText(text + (f' – #{bid}' if bid is not None else '') + (f' · {rating}' if rating else ''))

    def set_color(self, color, blink=False):
        self.color = color
        self.blinking = blink
        self._head_style()

    def blink(self, dark):
        """Error slots: title bar and status bar switch between the colour and a darker shade."""
        if self.blinking:
            self._head_style(dark)
            self.bar.set_color(QColor(self.color).darker(220) if dark else QColor(self.color))

    def set_chosen(self, chosen):
        """Chosen in the charger control panel: title bar in the control colour instead of the slot colour."""
        self.chosen = chosen
        self._head_style()

    def _head_style(self, dark=False):
        color = CONTROL_COLOR if self.chosen else QColor(self.color).darker(220).name() if dark else self.color
        self.head.setStyleSheet(f'background:{color}; color:white; padding:0 3px;')

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
        self.set_color(sample_color(s), s['mode'] in (2, 20))
        if s['mode'] == 0:
            idle = is_idle(s)                               # cell in, no task: state, type and voltage
            typ = ' '.join(x for x in (s['chem'], s['size']) if x and x not in ('auto', 'unknown', 'empty'))
            self.mode.setText(f"{mode_name(s)}\n{typ}" if idle else mode_name(s))
            self.big.setText(f"{s['mv'] / 1000:.3f} V" if idle else '')
            self.lines.setText(f"{s['temp']} °C" if s['temp'] else '')
            if self.status_bar:
                self.bar.show()
                self.bar.set_state(100 if idle else 0, self.color, 0,
                                   f"{mode_name(s)} · {s['mv'] / 1000:.3f} V" if idle else mode_name(s))
            else:
                self.bar.hide()
            return
        phase = ''
        if s['mode_str'] == PAUSE and s['mode'] in TASKS:       # SkyRC: rest between the phases of a task
            phase = ' · ' + tr_data(PAUSE)
        elif s['mode'] in (9, 11, 13) and s['ma']:
            phase = ' · ' + tr_data(MODE_NAMES[3] if s['ma'] > 0 else MODE_NAMES[5])
        typ = ' '.join(x for x in (s['chem'], s['size']) if x and x not in ('auto', 'unknown', 'empty'))
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
        if self.status_bar and not s['progress']:           # no %: full bar with state and voltage
            self.bar.show()
            self.bar.set_state(100, self.color, bar_direction(s) if s['mode_str'] != PAUSE else 0,
                               f"{status_word(s)} · {s['mv'] / 1000:.3f} V")
        elif s['progress'] or not self.bar.isHidden():      # chargers without % never show the bar
            self.bar.show()
            self.bar.set_state(s['progress'] or 0, self.color, bar_direction(s))

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.menu.emit(self.key, e.globalPosition().toPoint())
        else:
            self.clicked.emit(self.key)


CONTROL_COLOR = '#3949ab'         # charger control panel and the slots chosen there (indigo)
HEAD_HEIGHT = 24                  # title bars of the tiles and of the control panel


# home symbol of the chart's reset button
HOME_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="48" height="48" fill="none" '
            'stroke="#444" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">'
            '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9.5h5v-6h3v6h5V10"/></svg>')


def fmt_hms(secs):
    secs = int(round(secs))
    return f'{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}' if secs >= 0 else ''


class RightAxis(pg.AxisItem):
    """Right-hand value axis with its title left of the axis line (numbers right of it): next to its own line instead
    of next to the line of the following axis."""
    GAP = 3

    def __init__(self):
        super().__init__('right')

    def resizeEvent(self, ev=None):
        super().resizeEvent(ev)
        if self.label is not None:
            br = self.label.boundingRect()
            self.label.setPos(QPointF(-br.height() - self.GAP, int(self.size().height() / 2 + br.width() / 2)))


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
    GAP_BREAK = 300                   # s without readings: the curves break off (no straight line across the gap)
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
                axis = RightAxis()
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
        # voltage the charger kept itself (MC5000) for a gap in the readings: dashed, in the voltage colour
        self.hist_curve = pg.PlotDataItem(pen=pg.mkPen(self.SERIES[0][1], width=1.6, style=Qt.DashLine),
                                          connect='finite')
        self.vb.addItem(self.hist_curve)
        self._hist_raw = []
        self.zero_lines = {}
        for k in self.ZERO:
            line = pg.InfiniteLine(pos=0, angle=0, movable=False, pen=pg.mkPen('#666666', width=1))
            self.vbs[k].addItem(line, ignoreBounds=True)
            self.zero_lines[k] = line
        p.layout.setContentsMargins(1, 1, 22, 1)       # room for the title of the first right-hand axis
        self.vb.sigResized.connect(self._sync)
        self.vb.sigXRangeChanged.connect(lambda *_: self._align_zero())
        self.legend = pg.LegendItem(colCount=len(self.SERIES), brush=pg.mkBrush(255, 255, 255, 210),
                                    labelTextColor='#222')
        self.legend.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)      # as wide as its entries, centred
        self.ci.addItem(self.legend, row=1, col=0, colspan=len(self.SERIES))
        self.ci.layout.setAlignment(self.legend, Qt.AlignHCenter)
        self.axes = [p.getAxis('left')] + [self.ci.getItem(2, i) for i in range(1, len(self.SERIES))]
        for i in range(len(self.SERIES)):
            # like Grafana: hovering a legend entry highlights the value, a click shows / hides it; its axis does the same
            self._hook(self.axes[i], i)
        self.focus = None                     # series under the mouse (curve or legend entry): the others are dimmed
        hidden = QSettings('battbench', 'battbench').value('hidden_series', '')
        self.shown = [unit not in str(hidden).split(',') for unit, _c in self.SERIES]
        self.avail = [True] * len(self.SERIES)   # the charger reports the value (e.g. no temperature from an MC5000)
        self.entries = {}                     # series -> (sample, label) of its legend entry
        self._build_legend()
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
        self.show_curve = False               # mark the voltage curve of the last discharge (not while measuring)
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

    def _hook(self, item, i):
        item.mouseClickEvent = lambda ev, i=i: (ev.accept(), self.toggle(i))
        item.setCursor(Qt.PointingHandCursor)
        item.setAcceptHoverEvents(True)
        item.hoverEvent = lambda ev, i=i: self.highlight(None if ev.isExit() else i)

    def _build_legend(self):
        """Legend entries of the values the charger reports, in the order of SERIES."""
        self.legend.clear()
        self.entries = {}
        for i, (curve, name) in enumerate(zip(self.curves, self.names)):
            if self.avail[i]:
                self.legend.addItem(curve, name)
                self.entries[i] = self.legend.items[-1]
                for item in self.entries[i]:
                    self._hook(item, i)
        self.legend.setColumnCount(max(len(self.entries), 1))
        for i in range(len(self.SERIES)):
            self._show_series(i)

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
        Dimmed while another series is highlighted. A value the charger does not report: no curve, axis or entry."""
        on = self.shown[i] and self.avail[i]
        dim = self.DIM if self.focus not in (None, i) else 1
        for item in (self.curves[i], self.axes[i]):
            item.setOpacity(dim)
        self.curves[i].setVisible(on)
        if i == 0:
            self.hist_curve.setVisible(on)
            self.hist_curve.setOpacity(dim)
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
        if i not in self.entries:
            return
        sample, label = self.entries[i]
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

    def set_data(self, rows, phases, title='', keep_view=False, hist=None):
        """rows: (t, mv, ma, res, mah, temp, mode) from DB.samples; phases: (kind, start, end, mah); hist: (t, mV) the
        charger kept for gaps in the readings (DB.history), None = as before (same session)."""
        start = rows[0][0] if rows else None
        if hist is not None:
            self._hist_raw = list(hist)
        elif start != self.session_start:
            self._hist_raw = []
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
            cut = np.where(np.diff(a[:, 0]) > self.GAP_BREAK)[0] + 1
            if len(cut):                              # a row of NaN in the middle of each gap breaks the lines
                fill = np.full((len(cut), a.shape[1]), np.nan)
                fill[:, 0] = (a[cut - 1, 0] + a[cut, 0]) / 2
                a = np.insert(a, cut, fill, axis=0)
            if not vr:
                self.t0 = phase[1] if phase else a[0, 0]
            x = a[:, 0] - self.t0
            res = a[:, 3].copy()
            res[res <= 0] = np.nan
            temp = a[:, 5].copy()
            temp[temp <= 0] = np.nan                  # 0 = not reported (e.g. A4 Air over Bluetooth)
            self.data = (x, a[:, 1] / 1000, a[:, 2], a[:, 4], res, temp)
            avail = [True, True, True, bool(np.isfinite(res).any()), bool(np.isfinite(temp).any())]
        else:
            self.data = None
            avail = [True] * len(self.SERIES)
        if avail != self.avail:
            self.avail = avail
            self._build_legend()
        follow = vr is not None and self.data is not None and vr[1] >= self._last_x - 5
        for i, curve in enumerate(self.curves):
            if self.data is None:
                curve.setData([], [])
            else:
                curve.setData(self.data[0], self.data[i + 1])
        hist = [(t, mv) for t, mv in self._hist_raw if not phase or phase[1] <= t <= phase[2]]
        if self.data is not None and hist:
            self.hist_curve.setData([t - self.t0 for t, _mv in hist], [mv / 1000 for _t, mv in hist])
        else:
            self.hist_curve.setData([], [])
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
        if self.show_curve:
            self._curve_marks(*self._raw[:2])
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

    def _curve_marks(self, rows, phases):
        """Last discharge of the session: its plateau (20-80 %) shaded darker, the average voltage there as a dashed
        line, the voltage after 5 % as a point - the values of the voltage score (model.discharge_curve)."""
        dis = [ph for ph in phases if ph[0] == DISCHARGE]
        if not dis:
            return
        _kind, a, b, _mah = dis[-1]
        c = discharge_curve([(r[0], r[1], r[2]) for r in rows if a <= r[0] <= b])
        if not c:
            return
        col = QColor(COLORS['discharge'])
        col.setAlpha(70)
        x20, x80, x5 = c['t_20'] - self.t0, c['t_80'] - self.t0, c['t_start'] - self.t0
        r = pg.LinearRegionItem(values=(x20, x80), movable=False, brush=QBrush(col), pen=pg.mkPen(None))
        r.setZValue(-9)
        line = pg.PlotDataItem([x20, x80], [c['v_mid'] / 1000] * 2,
                               pen=pg.mkPen('#8b0000', width=1.5, style=Qt.DashLine))
        dot = pg.ScatterPlotItem([x5], [c['v_start'] / 1000], size=9, brush=pg.mkBrush('#8b0000'), pen=pg.mkPen('w'))
        label = pg.TextItem(tr('20–80 %: Ø {} V').format(f"{c['v_mid'] / 1000:.3f}"), color='#8b0000', anchor=(0.5, 1))
        label.setPos((x20 + x80) / 2, c['v_mid'] / 1000)
        label5 = pg.TextItem(tr('5 %: {} V').format(f"{c['v_start'] / 1000:.3f}"), color='#8b0000', anchor=(0, 1))
        label5.setPos(x5, c['v_start'] / 1000)
        for item in (r, line, dot, label, label5):
            self.vb.addItem(item, ignoreBounds=True)
            self.regions.append(item)

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
            if not self.shown[k] or not self.avail[k] or not len(ys):
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
        values = [f'– {unit}' if np.isnan(self.data[k + 1][i]) else f'{self.data[k + 1][i]:{fmt}} {unit}'
                  for k, (fmt, unit) in enumerate((('.3f', 'V'), ('.0f', 'mA'), ('.0f', 'mAh'), ('.0f', 'mΩ'),
                                                   ('.0f', '°C')))]
        lines = ''.join(f"<br><span style='color:{color}'>{f'<b>{v}</b>' if k == self.focus else v}</span>"
                        for k, (v, (_u, color), on) in enumerate(zip(values, self.SERIES, self.shown))
                        if on and self.avail[k])
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
    if is_idle(sample):
        return tr_data(IDLE)
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
        comp = QCompleter(self.model(), self)     # popup instead of inline completion (subclasses replace it)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setMaxVisibleItems(20)
        plain_selection(comp.popup())
        self.setCompleter(comp)

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
        self.maker = EditCombo()
        self.maker.addItems([''] + db.makers())
        self.maker.setCurrentText(self.m['maker'])
        self.maker.lineEdit().setPlaceholderText(tr('e.g. Panasonic'))
        self.name = QLineEdit(self.m['name'])
        self.name.setPlaceholderText(tr('e.g. eneloop pro AA'))
        self.type = EditCombo()
        self.type.addItems(BATTERY_TYPES)
        self.type.setCurrentText(self.m['type'] or 'NiMH AA')
        self.capacity = NominalCombo()
        self.capacity.set_value(self.m['capacity'] or 0, tr('not set'))
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
    (new only: several batteries with consecutive IDs), description. Maker, capacity and type can only be
    entered by hand for a special battery without a model (second dropdown entry). There is no name field: the
    stored name is the model name (without model: "maker type")."""
    CUSTOM = -1                       # dropdown data of "special battery without a model"

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
        self.em = QPushButton(tr('Edit …'))
        self.em.setToolTip(tr('Edit the chosen model'))
        self.em.clicked.connect(self._edit_model)
        row.addWidget(self.em)
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
        self.maker = EditCombo()
        self.maker.addItems([''] + db.makers())
        self.maker.setCurrentText(b['maker'])
        self.capacity = NominalCombo()
        self.capacity.set_value(b['capacity'] or 0, tr('not set'))
        self.type = EditCombo()
        self.type.addItems(BATTERY_TYPES)
        self.type.setCurrentText(b['type'] or 'NiMH AA')
        self.desc = QPlainTextEdit(b['description'])
        self.desc.setPlaceholderText(tr('Date of purchase, device, anything notable …'))
        self.desc.setFixedHeight(90)
        form.addRow(tr('Maker:'), self.maker)
        form.addRow(tr('Nominal capacity:'), self.capacity)
        form.addRow(tr('Type:'), self.type)
        form.addRow(tr('ID:'), self.id)
        if battery is None:
            form.addRow(tr('Count:'), self.count)
        form.addRow(tr('Description:'), self.desc)
        form.addRow(dialog_buttons(self, self._ok))
        chain = [self.model, self.em, nm, self.maker, self.capacity, self.type, self.id] +                 ([self.count] if battery is None else []) + [self.desc]       # count: only for new batteries
        for w1, w2 in zip(chain, chain[1:]):  # Tab in the order shown (default: order of creation)
            QWidget.setTabOrder(w1, w2)
        for w in (self.maker, self.capacity, self.type):   # a click on them while disabled explains why
            w.installEventFilter(self)
            w.lineEdit().installEventFilter(self)
        self.load_models(b.get('model_id') if battery is None or b.get('model_id') is not None else self.CUSTOM)
        self.model.setFocus()
        self.model.lineEdit().selectAll()
        self.resize(480, 0)

    def load_models(self, select=None):
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem(tr('– please choose a model –'), None)
        self.model.addItem(tr('– special battery without a model (enter values by hand) –'), self.CUSTOM)
        for mid, maker, name, typ, cap, _note, _n, _deleted in self.db.models():
            self.model.addItem(model_text(maker, name, cap, typ), mid)
        self.model.setCurrentIndex(max(self.model.findData(select), 0))
        self.model.blockSignals(False)
        self._model_picked()

    def eventFilter(self, obj, e):
        if e.type() == QEvent.MouseButtonPress and not obj.isEnabled() and self.model.currentData() is None:
            QTimer.singleShot(0, self._choose_model_first)
            return True
        return super().eventFilter(obj, e)

    def _choose_model_first(self):
        QMessageBox.information(self, tr('Battery'), tr('Please choose an existing model, or a special battery '
                                                        'without a model.'))
        self.model.setFocus()
        self.model.lineEdit().selectAll()
        self.model.open_list()

    def _model_id(self):
        mid = self.model.currentData()
        return None if mid == self.CUSTOM else mid

    def _model_picked(self):
        m = self.db.model(self._model_id()) if self._model_id() is not None else None
        for w in (self.maker, self.capacity, self.type):
            w.setEnabled(self.model.currentData() == self.CUSTOM)
        self.em.setEnabled(m is not None)         # only a real model (not "choose" / special battery)
        if m:
            self.maker.setCurrentText(m['maker'])
            self.capacity.set_value(m['capacity'] or 0, tr('not set'))
            self.type.setCurrentText(m['type'])
        self._count_changed()

    def _count_changed(self):
        n = self.count.value()
        self.count.setSuffix(f'   (IDs {self.id.value()}–{self.id.value() + n - 1})' if n > 1 else '')

    def _new_model(self):
        dlg = ModelDialog(self, self.db, prefill=dict(type=self.type.currentText().strip(),
                                                      maker=self.maker.currentText().strip(),
                                                      capacity=self.capacity.value()))
        if dlg.exec():
            self.model_changes.append((dlg.result_id, dlg.batteries))
            self.load_models(dlg.result_id)

    def _edit_model(self):
        mid = self._model_id()
        if mid is None:
            return
        dlg = ModelDialog(self, self.db, model=self.db.model(mid))
        if dlg.exec():
            self.model_changes.append((dlg.result_id, dlg.batteries))
            self.load_models(dlg.result_id)

    def value(self):
        m = self.db.model(self._model_id()) if self._model_id() is not None else None
        maker, typ = self.maker.currentText().strip(), self.type.currentText().strip()
        return dict(id=self.id.value(), name=m['name'] if m else ' '.join(x for x in (maker, typ) if x),
                    maker=maker, capacity=self.capacity.value(), type=typ,
                    description=self.desc.toPlainText().strip(), model_id=self._model_id())

    def _ok(self):
        if self.model.currentData() is None:
            QMessageBox.warning(self, tr('Battery'), tr('Please choose a model above, or a special battery '
                                                        'without a model.'))
            self.model.setFocus()
            return
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
    save_label = Signal(int, str)         # session id, note
    phase_clicked = Signal(object)        # start time of the phase to show alone, None = whole run
    new_battery = Signal()
    export_report = Signal()              # "Export report …" of the session shown

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.sid = None
        self.cur = None
        self.shown = (None, 0)            # battery id / nominal last filled into the edit fields
        self.picked = {}                  # session id -> model chosen there without a battery (not stored)
        self.dev_name = lambda dev: ''    # set by the main window
        self.busy = lambda: set()         # set by the main window: ids of batteries in a slot right now
        lay = QVBoxLayout(self)
        self.title = QLabel(tr('No session selected'))
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.title, 1)
        self.export = QPushButton(tr('Export report …'))
        self.export.setToolTip(tr('Report of this session as HTML (optionally PDF): rating, battery, charger, '
                                  'chart, details and the charge / discharge phases'))
        self.export.clicked.connect(self.export_report)
        row.addWidget(self.export, 0, Qt.AlignBottom)
        lay.addLayout(row)
        self.grade = QLabel('')
        self.grade.setAlignment(Qt.AlignCenter)
        self.grade.setWordWrap(True)          # long rating line: two lines rather than cut off
        fg = QFont()
        fg.setPointSize(15)
        fg.setBold(True)
        self.grade.setFont(fg)
        lay.addWidget(self.grade)
        self.note = QLabel('')
        self.note.setWordWrap(True)
        fn = QFont()
        fn.setBold(True)
        self.note.setFont(fn)
        lay.addWidget(self.note)
        fields = QWidget()                    # details and phases scroll together instead of being squeezed
        flay = QVBoxLayout(fields)
        flay.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        flay.addLayout(form)
        self.f = {}
        tips = self.field_tips()
        for k, name in [('task', tr('Task')), ('status', tr('Status')), ('type', tr('Detected')), ('time', tr('Time')),
                        ('health', tr('Health index')), ('use', tr('Suited for')),
                        ('dis', tr('Discharge capacity')), ('chg', tr('Charge capacity')),
                        ('res', tr('Internal resistance')), ('volt', tr('Voltage under load')),
                        ('temp', tr('Max. temperature'))]:
            self.f[k] = QLabel('')
            self.f[k].setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.f[k].setWordWrap(True)
            label = QLabel(name + ':')
            for w in (label, self.f[k]):
                w.setToolTip(tips.get(k, ''))
            form.addRow(label, self.f[k])
        flay.addWidget(QLabel('<b>' + tr('Phases') + '</b>'))
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
        self.phases.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)    # as high as its rows (see _fit_phases)
        self.phases.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        flay.addWidget(self.phases)
        flay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(fields)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(scroll, 1)
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
        self.label = QLineEdit()              # the session's note (sessions.label), saved on Enter / leaving the field
        self.label.setPlaceholderText(tr('e.g. new contacts, after long storage, from the remote control …'))
        self.label.setClearButtonEnabled(True)
        self.label.editingFinished.connect(self._save_label)
        self.shown_label = ''
        edit.addRow(tr('Note:'), self.label)
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
        busy = self.busy() - {keep}       # a battery in another slot can't be this one
        for r in self.db.batteries():
            if mid is not None and r[9] != mid or r[0] in busy:
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
        if self.sid is not None:
            self.picked[self.sid] = mid
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
                (ABORTED, tr('the task was stopped or replaced by another one, the charger stopped it with an '
                             'error (see the rating note), or the data broke off'))]),
            'dis': tr('<p>The capacity the battery delivered during the (last) discharge, measured by the charger; '
                      'in brackets in % of the nominal capacity. It is the largest part of the health index: from '
                      '90 % the capacity score is full, below 70 % the cell is rated D.</p>'),
            'chg': tr('<p>The charge put into the battery after the discharge (or during a plain charge). In '
                      'brackets the charge efficiency, discharge ÷ charge: 75–85 % is normal for NiMH; less means '
                      'losses (heat, self-discharge), more means a charge that may have ended early. It is shown '
                      'once the charge after the discharge is finished.</p>'),
            'res': tr('<p>The internal resistance as the charger measures it – <b>lower is better</b>. It rises with '
                      'age and wear; a high value lets the voltage drop under load, so the device switches off '
                      'earlier. "min." is the lowest value of the session, first and last are the values at its '
                      'start and end.</p>'),
            'health': tr('<p>Health index 0–100 from four scores, each 0–100: capacity (weight 40 %), internal '
                         'resistance (30 %), voltage under load (20 %) and charge efficiency (10 %). A score that '
                         'cannot be worked out is left out and the others weighted up. The index gives the '
                         'category A–D; the user guide (Help) explains the calculation.</p>'),
            'use': tr('<p>What the cell is still good for, from its category: A high drain (flash units, RC '
                      'models), B medium drain (LED torches, mice), C low drain (remote controls, clocks), '
                      'D recycle.</p>'),
            'volt': tr('<p>The voltage while discharging: after 5 % of the discharge, and the average from 20 to '
                       '80 % (the plateau, shaded darker in the chart). A healthy NiMH cell stays above 1.15 V and '
                       '1.20 V at a discharge current of about 0.2 C; falling below 1.0 V before 80 % is an early '
                       'drop.</p>'),
            'temp': tr('<p>The highest temperature of the battery during the session. NiMH cells get warm towards '
                       'the end of charging, which is normal. Above about 45 °C the cell is stressed: check the '
                       'contacts and the charging current.</p>'),
        }

    def _fit_phases(self):
        """The phase table as high as its rows: the surrounding scroll area scrolls, not the table."""
        t = self.phases
        h = t.horizontalHeader().sizeHint().height() + 2 * t.frameWidth()     # height() is 0 before it is shown
        h += sum(t.rowHeight(r) for r in range(t.rowCount()))
        t.setFixedHeight(h)

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

    def _save_label(self):
        text = self.label.text().strip()
        if self.sid is not None and text != self.shown_label:
            self.shown_label = text
            self.save_label.emit(self.sid, text)

    @staticmethod
    def _nominal_hint(d):
        default = NOMINAL.get(d['size'], 0) if d else 0
        return tr('default ({} mAh)').format(default) if default else tr('not set')

    def show_session(self, d, battery_id=None):
        """d: dict like session_info() / DB.session_dict()."""
        # another session: keep a note typed without Enter (a tile click doesn't take the focus, so editingFinished
        # never came)
        if (d.get('id') if d else None) != self.sid:
            self._save_label()
        self.cur = d
        self.export.setEnabled(d is not None)
        enabled = d is not None and d.get('id') is not None
        for w in (self.battery, self.nominal, self.nb, self.label):
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
            self._fit_phases()
            self._set_model(None)
            self.reload_batteries(select=-1)
            self.nominal.set_value(0, '')
            self.label.setText('')
            self.shown_label = ''
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
        h = health_of(d)
        pct = ''
        if h and g in USES:                   # e.g. "B · medium drain • index 78 • Capa 92 % • Ω poor"
            pct = ' • ' + tr('index {}').format(h['ohi']) + ' • ' + tr('Capa {} %').format(round(h['pct']))
            if 'resistance' in h['scores']:
                pct += ' • Ω ' + tr_data(RES_QUALITY[res_level(d['res_min'], d['chem'])])
        if d['task'] == CHARGE and not d['discharge_mah']:       # a plain charge measures no capacity
            g, unrated = NO_GRADE, False
            self.grade.setText(tr('Charging only, no analysis'))
        elif is_measuring(d) or d['status'] == RUNNING and g == NO_GRADE:
            g, unrated = NO_GRADE, False      # capacity not final yet: e.g. "Analyse läuft" in grey
            self.grade.setText(tr_data(d['task']) + ' ' + tr_data(RUNNING) +
                               (' · ' + tr_data(PAUSE) if d.get('paused') else ''))
        else:
            self.grade.setText(tr('Set the nominal capacity for a rating') if unrated else tr_data(g) + pct)
        self.grade.setStyleSheet(f"background:{GRADE_COLORS.get(g, '#888')}; color:white; padding:6px;"
                                 'border-radius:6px;' + ('font-size: 11pt;' if unrated else ''))
        self.note.setText('' if h else note_of(d))     # with a health index the fields below show it all
        self.note.setVisible(bool(self.note.text()))     # empty: no gap between the rating and the fields
        self.f['task'].setText(tr_data(d['task']))
        status = tr_data(d['status'])
        if d['status'] == RUNNING and d.get('paused'):          # running: what it is doing right now
            status += ' · ' + tr_data(PAUSE)
        elif d['status'] == RUNNING and d.get('phases'):
            status += ' · ' + tr_data(MODE_NAMES[3] if d['phases'][-1][0] == CHARGE else MODE_NAMES[5])
        self.f['status'].setText(status)
        self.f['type'].setText(' '.join(x for x in (d['chem'], d['size']) if x) +
                               (' · ' + tr('nominal {} mAh').format(d['nominal']) if d['nominal'] else ''))
        self.f['time'].setText(f"{fmt_t(d['start'])} – {datetime.fromtimestamp(d['end']):%H:%M} "
                               f"({fmt_dur(d['end'] - d['start'])})")
        # "2479 mAh (92 %)": % of the nominal capacity; charge: "3001 mAh (83 %)": the charge efficiency
        nominal = d['nominal'] or NOMINAL.get(d['size'], 0)
        dis, chg = d['discharge_mah'], d['charge_mah']
        self.f['dis'].setText(f'{dis} mAh' + (f' ({100 * dis / nominal:.0f} %)' if nominal else '') if dis else '–')
        self.f['chg'].setText(f'{chg} mAh' + (f" ({h['eta']:.0f} %)" if h and h['eta'] else '') if chg else '–')
        names = {'capacity': tr('capacity'), 'resistance': tr('resistance'), 'voltage': tr('voltage'),
                 'efficiency': tr('efficiency')}
        self.f['health'].setText(tr('{} of 100').format(h['ohi']) + '  (' + ' · '.join(
            f'{names[k]} {round(v)}' for k, v in h['scores'].items()) + ')' if h else '–')
        self.f['use'].setText(tr_data(USES[g]) if h and g in USES else '–')
        if d.get('v_mid'):
            volt = (tr('{} V after 5 %').format(f"{d['v_start'] / 1000:.3f}") + ' · ' +
                    tr('{} V on average 20–80 %').format(f"{d['v_mid'] / 1000:.3f}"))
            if d.get('early_drop'):
                volt += ' · ' + tr('below 1.0 V before 80 %')
            self.f['volt'].setText(volt)
        else:
            self.f['volt'].setText('–')
        if d.get('res_first'):
            self.f['res'].setText(tr('min. {} · first {} · last {} mΩ').format(d['res_min'], d['res_first'],
                                                                                d['res_last']))
        else:
            self.f['res'].setText(tr('min. {} mΩ').format(d['res_min']) if d['res_min'] else '–')
        if d.get('res_est') and d['res_min']:   # A4 Air over USB: worked out by BattBench, not rated
            self.f['res'].setText(self.f['res'].text() + '  –  ' + tr('estimated, not rated'))
        elif d['res_min']:                    # rating of the value; lower is better (not obvious to everyone)
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
        self._fit_phases()
        # refill the edit fields for a new session, or when the stored values changed and the user
        # hasn't touched the fields (don't overwrite what is being selected / typed)
        if not same or self.battery.currentData() == self.shown[0]:
            if not same or battery_id != self.shown[0]:
                if not same:                  # the panel is shared: don't carry the model over to another session
                    self._set_model(self.picked.get(self.sid))
                self.reload_batteries(select=battery_id if battery_id is not None else -1)
        if not same or (not self.nominal.hasFocus() and self.nominal.value() == self.shown[1]):
            self.nominal.set_value(d['nominal'] or 0, self._nominal_hint(d))
        if not same or not self.label.hasFocus():   # don't overwrite what is being typed
            row = self.db.con.execute('SELECT label FROM sessions WHERE id=?', (self.sid,)).fetchone()
            self.shown_label = (row[0] if row else '') or ''
            if self.label.text() != self.shown_label:
                self.label.setText(self.shown_label)
        self.shown = (battery_id, d['nominal'] or 0)
        self._lock_model()


def session_headers():
    """Column key -> header of the session tables."""
    return {'start': tr('Start'), 'dev': tr('Charger'), 'slot': tr('Slot'), 'task': tr('Task'),
            'status': tr('Status'), 'battery': tr('Battery'), 'detected': tr('Detected'), 'nominal': tr('Nominal'),
            'dis': tr('Disch. mAh'), 'pct': '%', 'chg': tr('Charge mAh'), 'rmin': tr('R min'), 'tmax': tr('T max'),
            'grade': tr('Rating'), 'dur': tr('Duration'), 'label': tr('Note')}




def make_table(headers, autofilter=False, stretch=True):
    """autofilter: sortable, with a filter button in every column header (table.autofilter). stretch: the last
    column takes the rest of the width (for a text column; not for e.g. a duration)."""
    t = QTableWidget(0, len(headers))
    t.autofilter = AutoFilter(t) if autofilter else None
    t.setHorizontalHeaderLabels(headers)
    plain_selection(t)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    header = t.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)       # the user can drag the column widths
    header.setStretchLastSection(stretch)
    # Widths follow the contents (measured once after a refill) until the user drags a column; from then on the
    # table keeps the user's widths, they are stored with the layout (MainWindow.save_layout).
    t.user_widths = False
    t.fitting = False
    t.header_drag = HeaderDrag(header.viewport())
    t.fit_timer = QTimer(t)
    t.fit_timer.setSingleShot(True)
    t.fit_timer.timeout.connect(lambda: fit_columns(t))
    m = t.model()
    for sig in (m.dataChanged, m.rowsInserted, m.rowsRemoved, m.modelReset):
        sig.connect(lambda *_: t.fit_timer.start(0))
    header.sectionResized.connect(lambda *_: _user_resized(t))
    return t


def fit_columns(t):
    if not t.user_widths:
        t.fitting = True
        t.resizeColumnsToContents()
        t.fitting = False


class HeaderDrag(QObject):
    """Watches a header: is a mouse button held down on it (the user drags a column border)?"""

    def __init__(self, viewport):
        super().__init__(viewport)
        self.pressed = False
        viewport.installEventFilter(self)

    def eventFilter(self, obj, e):
        if e.type() == QEvent.MouseButtonPress:
            self.pressed = True
        elif e.type() == QEvent.MouseButtonRelease:
            self.pressed = False
        return False


def _user_resized(t):
    """A column was resized: by the user if it happens while the mouse is pressed on the header (not by
    fit_columns, a refill or a window resize)."""
    if not t.fitting and t.header_drag.pressed:
        t.user_widths = True


def scrolled(widget):
    """widget in a frameless scroll area: its height no longer sets the minimum height of the tab pane."""
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    return area


def backup_policy():
    """Backup settings (QSettings 'backup_<key>', defaults db.BACKUP_DEFAULTS)."""
    s = QSettings('battbench', 'battbench')
    return {k: s.value('backup_' + k, v, type=int) for k, v in BACKUP_DEFAULTS.items()}


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


def row_menu(table, edit, delete, enter=None, keys=True, extra=()):
    """Right click on a row: Edit … (if edit is given), the extra (text, callable) / Delete; with several rows
    selected (Ctrl / Shift) only Delete. Keys: Enter = enter (default: edit, one row selected), Del = delete.
    keys=False: the caller handles the keys - two visible tables with the same shortcuts make them ambiguous for Qt,
    then none of them fires."""
    table.setContextMenuPolicy(Qt.CustomContextMenu)
    enter = enter or (lambda: edit() if len(selected_ids(table)) == 1 else None)
    for key, fn in ((Qt.Key_Return, enter), (Qt.Key_Enter, enter), (Qt.Key_Delete, delete)) if keys else ():
        QShortcut(QKeySequence(key), table, fn, context=Qt.WidgetShortcut)

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
        for text, fn in extra if n == 1 else ():
            menu.addAction(text, fn)
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
    # Columns are Interactive (never ResizeToContents: that measures all rows again on every setItem, refilling
    # 500 sessions took 77 s); make_table fits them once after the refill.
    # Sorting is switched off while filling, otherwise rows move between the setItem calls.
    sorting = table.isSortingEnabled()
    table.setSortingEnabled(False)
    try:
        _fill_session_rows(table, rows, cols)
    finally:
        table.setSortingEnabled(sorting)
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
                'grade': ((tr_data(RUNNING), None) if is_measuring(d) else grade_cell(d['grade'], d['ohi'])[:2]),
                'dur': (fmt_dur(d['end'] - d['start']), d['end'] - d['start']),
                'label': (d['label'] or '', (d['label'] or '').lower())}
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
            if c == 'label':
                it.setToolTip(d['label'] or '')
            if c == 'grade':
                it.setData(FILTER_ROLE, tr_data(RUNNING) if is_measuring(d) else grade_cell(d['grade'], d['ohi'])[2])
                it.setBackground(QColor(GRADE_COLORS.get(d['grade'] or NO_GRADE, '#888')))
                it.setForeground(QColor('white'))
                it.setToolTip(note)
            table.setItem(i, j, it)


class BatteryTab(QWidget):
    """All batteries (add / edit / delete) and the measurement history of the selected one."""
    open_session = Signal(int)
    changed = Signal(int)                 # battery id added/edited, -1 = deleted
    models_changed = Signal(list)         # a model was added/edited: ids of its batteries
    sessions_deleted = Signal()           # sessions deleted from the history
    export_session = Signal(int)          # "Export report …" on a session of the history
    HIST_COLS = ['start', 'dev', 'slot', 'task', 'status', 'dis', 'pct', 'chg', 'rmin', 'tmax', 'grade', 'dur', 'label']

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        lay = QHBoxLayout(self)
        self.split = QSplitter(Qt.Horizontal)          # batteries | history, position stored with the layout
        self.split.setChildrenCollapsible(False)
        lay.addWidget(self.split)
        lw = QWidget()
        left = QVBoxLayout(lw)
        left.setContentsMargins(0, 0, 0, 0)
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
        self.table.autofilter.header.desc_first = {5, 7, 8, 9}   # first click: highest first (count, rating, dates)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)      # Ctrl / Shift: several, to delete
        self.table.itemSelectionChanged.connect(self._battery_picked)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit())
        row_menu(self.table, self.edit, self.delete, keys=False)
        left.addWidget(self.table)
        self.split.addWidget(lw)
        rw = QWidget()
        right = QVBoxLayout(rw)
        right.setContentsMargins(0, 0, 0, 0)
        self.hist_title = QLabel()
        right.addWidget(self.hist_title)
        heads = session_headers()
        self.hist = make_table([heads[c] for c in self.HIST_COLS], autofilter=True)    # last: the note
        self.hist.autofilter.header.desc_first = {self.HIST_COLS.index('grade')}     # best first
        self.hist.sortByColumn(0, Qt.DescendingOrder)
        self.hist.setSelectionMode(QAbstractItemView.ExtendedSelection)      # Ctrl / Shift: several, to delete
        # a click (or arrow key) on a session shows it at once; a click on the same row again too
        self.hist.currentCellChanged.connect(lambda r, _c, pr, _pc: self._open_hist_row(r) if r != pr else None)
        self.hist.cellClicked.connect(lambda r, _c: self._open_hist_row(r))
        row_menu(self.hist, None, self.delete_sessions, keys=False, extra=[
            (tr('Export report …'), lambda: self.export_session.emit(selected_ids(self.hist)[0]))])
        # Enter / Del: for the battery list or the history, whichever has the focus
        for key, fn in ((Qt.Key_Return, self._enter_key), (Qt.Key_Enter, self._enter_key),
                        (Qt.Key_Delete, self._delete_key)):
            QShortcut(QKeySequence(key), self, fn, context=Qt.WidgetWithChildrenShortcut)
        right.addWidget(self.hist)
        self.split.addWidget(rw)
        self.split.setSizes([600, 400])
        self.shown_once = False           # sizes() of a never shown splitter are meaningless: don't store them
        self.load()

    def showEvent(self, e):
        super().showEvent(e)
        self.shown_once = True

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
                t_chg, deleted, ohi) in enumerate(rows):
            pct = f' ({100 * last / cap:.0f} %)' if last and cap else ''
            rating = grade_cell(grade, ohi) if grade else ('', None, '')
            cells = [(f"{bid} {tr('(deleted)')}" if deleted else bid, bid), (maker, maker.lower()),
                     (mname or '', (mname or '').lower()), (f'{cap} mAh' if cap else '', cap or None),
                     (typ, typ.lower()), (n, n),
                     (f'{last}{pct}' if last else '', last), rating[:2],
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
                    it.setData(FILTER_ROLE, rating[2])
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

    def show_battery(self, bid):
        """Select this battery (and show its history); clears search and filters if they hide it."""
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == bid:
                if self.table.isRowHidden(r):
                    self.search.clear()
                    self.table.autofilter.clear()
                self.table.selectRow(r)
                self.table.scrollToItem(self.table.item(r, 0))
                return

    def show_session(self, bid, sid):
        """Select the battery and, in its history, the session."""
        self.show_battery(bid)
        select_by_id(self.hist, sid)
        r = self.hist.currentRow()
        if r >= 0:
            self.hist.scrollToItem(self.hist.item(r, 0))

    def filter_model(self, mid):
        """Only the batteries of this model (column filters on maker and model; search cleared)."""
        m = self.db.model(mid)
        if not m:
            return
        self.search.clear()
        af = self.table.autofilter
        af.filters.clear()
        af.filters.update({1: {m['maker']}, 2: {m['name']}})
        af.apply()

    def _matches(self, row):
        """Search field: every word in ID, maker, model, type, description or name."""
        words = self.search.text().lower().split()
        text = ' '.join([self.table.item(row, j).text() for j in (0, 1, 2, 4, 10)] +
                        [self.table.item(row, 0).data(SEARCH_ROLE) or '']).lower()
        return all(w in text for w in words)

    def _filter(self):
        self.table.autofilter.apply()

    def show_history(self):
        """Fill the history of the selected battery; a session selected there stays selected (refills)."""
        ids = selected_ids(self.table)
        self.edit_btn.setEnabled(len(ids) <= 1)
        bid = ids[0] if len(ids) == 1 else None
        b = self.db.battery(bid) if bid is not None else None
        self.hist_title.setText('<b>' + tr('History') + (f" #{b['id']} {b['name']}</b>" if b else
                                                         '</b> ' + tr('(choose a battery)')))
        keep = selected_ids(self.hist)
        self.hist.blockSignals(True)              # refilling / reselecting must not open a session
        fill_session_table(self.hist, self.db.battery_sessions(bid, deleted=show_deleted()) if b else [],
                           self.HIST_COLS)
        if keep:
            select_by_id(self.hist, keep[0])
        self.hist.blockSignals(False)

    def _battery_picked(self):
        """A battery chosen in the list: its history, and its latest session shown at once (chart and result)."""
        self.hist.clearSelection()
        self.show_history()
        rows = [r for r in range(self.hist.rowCount()) if not self.hist.isRowHidden(r)]
        if len(selected_ids(self.table)) == 1 and rows:
            latest = max(rows, key=lambda r: self.hist.item(r, 0).data(SORT_ROLE))   # column 0: start
            self.hist.blockSignals(True)
            self.hist.selectRow(latest)
            self.hist.blockSignals(False)
            self._open_hist_row(latest)

    def _open_hist_row(self, row):
        if row >= 0 and self.hist.item(row, 0) is not None:
            self.open_session.emit(self.hist.item(row, 0).data(Qt.UserRole))

    def _enter_key(self):
        """Battery list: edit the battery; history: open the session."""
        table = self.hist if self.hist.hasFocus() else self.table if self.table.hasFocus() else None
        ids = selected_ids(table) if table else []
        if len(ids) == 1:
            self.open_session.emit(ids[0]) if table is self.hist else self.edit()

    def _delete_key(self):
        if self.hist.hasFocus():
            self.delete_sessions()
        elif self.table.hasFocus():
            self.delete()

    def delete_sessions(self):
        """Selected sessions of the history: soft delete (already deleted ones for good)."""
        if delete_rows(self, self.hist, (tr('session'), tr('sessions')), self.db.delete_sessions,
                       self.db.hard_delete_sessions):
            self.load()
            self.sessions_deleted.emit()

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
    show_batteries = Signal(int)          # click on the battery count: show the batteries of this model

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
        self.table.cellClicked.connect(self._clicked)
        row_menu(self.table, self.edit, self.delete)
        lay.addWidget(self.table)
        self.load()

    def _clicked(self, row, col):
        if col == 4 and self.table.item(row, 4).data(SORT_ROLE):          # number of batteries
            self.show_batteries.emit(self.table.item(row, 0).data(Qt.UserRole))

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
class OfflineOverlay(QWidget):
    """Big notice over the tiles of a charger that is gone or no longer answers. Clicks go through to the tiles."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QVBoxLayout(self)
        self.box = box = QFrame()
        box.setObjectName('offline')
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        blay = QHBoxLayout(box)                             # wide and low: the tiles are only one row high
        blay.setContentsMargins(24, 10, 24, 10)
        blay.setSpacing(24)
        self.title = QLabel(tr('Charger offline'))
        f = QFont()
        f.setPointSize(22)
        f.setBold(True)
        self.title.setFont(f)
        blay.addWidget(self.title)
        self.text = QLabel()
        self.text.setWordWrap(True)
        f = QFont()
        f.setPointSize(11)
        self.text.setFont(f)
        blay.addWidget(self.text, 1)
        self.room = QWidget()                               # keeps the place of TilePage.action free
        self.room.hide()
        blay.addWidget(self.room)
        lay.setContentsMargins(40, 8, 40, 8)
        lay.addWidget(box, 0, Qt.AlignVCenter)
        self.style_now = None
        self.set_style(tr('Charger offline'), COLORS['error'])
        self.hide()

    def set_style(self, title, color):
        if self.style_now == (title, color):
            return
        self.style_now = (title, color)
        self.title.setText(title)
        self.title.setStyleSheet(f'color: {color};')
        self.box.setStyleSheet(f'#offline {{ background: palette(base); border: 3px solid {color};'
                               ' border-radius: 10px; }')

    def paintEvent(self, e):
        c = self.palette().window().color()
        c.setAlpha(150)                                     # tiles stay readable underneath
        QPainter(self).fillRect(self.rect(), c)


class TilePage(QWidget):
    """Tiles of one charger with the offline notice on top."""

    def __init__(self):
        super().__init__()
        self.offline = OfflineOverlay(self)
        self.action = QPushButton(self)                    # on the notice, e.g. "Resume now" for a Bluetooth pause
        self.action.hide()
        self.on_action = None
        self.action.clicked.connect(lambda: self.on_action and self.on_action())

    def resizeEvent(self, e):
        self.offline.setGeometry(self.rect())
        super().resizeEvent(e)
        self._place_action()

    def _place_action(self):
        if self.action.isHidden():
            return
        self.offline.layout().activate()
        box = self.offline.box.geometry()
        self.action.move(box.right() - 24 - self.action.width(), box.center().y() - self.action.height() // 2)
        self.action.raise_()

    def set_offline(self, text, title=None, color=None, action=None):
        """text = what happened (None: hide the notice); title / color instead of "Charger offline" in red;
        action = (button text, callable) for a button on the notice."""
        if action:
            if self.action.text() != action[0]:
                self.action.setText(action[0])
                self.action.adjustSize()
                self.offline.room.setFixedWidth(self.action.width())
            self.on_action = action[1]
        else:
            self.on_action = None
        self.offline.room.setVisible(bool(action) and text is not None)
        if text is None:
            self.offline.hide()
            self.action.hide()
            return
        self.offline.set_style(title or tr('Charger offline'), color or COLORS['error'])
        self.offline.text.setText(text)
        self.offline.setGeometry(self.rect())
        self.offline.show()
        self.offline.raise_()
        self.action.setVisible(bool(action))
        self._place_action()


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
        self.lamp_state = {}                  # slot -> (colour, blinking)
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

    def set_lamp(self, slot, color, tip, blink=False):
        """Round LED: bright spot top left, darker rim. blink: error, switched by blink()."""
        lamp = self.lamps[slot]
        if self.lamp_state.get(slot) != (color, blink):
            self.lamp_state[slot] = (color, blink)
            self._paint_lamp(lamp, color)
        lamp.setToolTip(tip)

    def blink(self, dark):
        for slot, (color, blink) in self.lamp_state.items():
            if blink:
                self._paint_lamp(self.lamps[slot], QColor(color).darker(220).name() if dark else color)

    def _paint_lamp(self, lamp, color):
        c = QColor(color)
        lamp.setStyleSheet(
            f'border-radius:{self.lamp_size // 2}px; border:1px solid {c.darker(160).name()};'
            f'background: qradialgradient(cx:0.5, cy:0.5, radius:0.6, fx:0.32, fy:0.3, stop:0 {c.lighter(170).name()},'
            f' stop:0.45 {c.name()}, stop:1 {c.darker(130).name()});')

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
                'mAh, time, temperature, internal resistance. MC5000: start / stop with profiles, charger settings, '
                'Bluetooth pause (over Bluetooth or a BattBench-ESP). MC3000: read only.')),
            ('', 'BattBench-ESP',
             tr('The same values, through an ESP32 that keeps the Bluetooth connection and records while BattBench '
                'is not running; BattBench fetches the missed readings when it starts.')),
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
        hint = QLabel(tr('<b>Bluetooth charger not found?</b> Quit the app on your phone – <b>ISD Link</b> for ISDT '
                         'Air chargers, the <b>SkyRC</b> app for the MC3000 / MC5000 – or switch off Bluetooth on the '
                         'phone. A charger accepts only one Bluetooth connection and cannot be seen by others while the '
                         'phone is connected. In turn, the app cannot reach it while BattBench or a BattBench-ESP is '
                         'connected.'))
        hint.setWordWrap(True)
        hint.setStyleSheet('background: palette(alternate-base); border: 1px solid #e69138; border-radius: 6px; padding: 8px;')
        lay.addWidget(hint)
        lay.addWidget(QLabel('<b>BattBench-ESP</b>'))
        row = QHBoxLayout()
        row.addWidget(QLabel(tr('Host names or IP addresses:')))
        self.esp = QLineEdit(' '.join(parent.esp))
        self.esp.setPlaceholderText('battbench-esp.local')
        self.esp.setToolTip(tr('An ESP32 with the BattBench-ESP firmware keeps the Bluetooth connection to a SkyRC '
                               'charger and records while BattBench is not running. Several separated by spaces.'))
        self.esp.setEnabled(parent.source != 'offline')
        self.esp.returnPressed.connect(self._set_esp)
        row.addWidget(self.esp, 1)
        apply = QPushButton(tr('Apply'))
        apply.setEnabled(parent.source != 'offline')
        apply.clicked.connect(self._set_esp)
        row.addWidget(apply)
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr('Connect SkyRC chargers:')))
        self.skyrc = QComboBox()
        for k, text in skyrc_mode_names().items():
            self.skyrc.addItem(text, k)
        self.skyrc.setCurrentIndex(max(0, self.skyrc.findData(parent.skyrc)))
        self.skyrc.setEnabled(parent.source != 'offline')
        self.skyrc.setToolTip(tr('An MC5000 takes only one Bluetooth connection. Automatic switches to '
                                 '"BattBench-ESP" once a BattBench-ESP was connected.'))
        self.skyrc.currentIndexChanged.connect(self._set_skyrc)
        row.addWidget(self.skyrc)
        row.addStretch(1)
        lay.addLayout(row)
        self.esp_auto = QCheckBox(tr('Find BattBench-ESP in the network automatically (mDNS)'))
        self.esp_auto.setChecked(parent.esp_auto)
        self.esp_auto.setEnabled(parent.source != 'offline')
        self.esp_auto.toggled.connect(self._set_esp)
        lay.addWidget(self.esp_auto)
        self.esp_state = QLabel()
        self.esp_state.setWordWrap(True)
        lay.addWidget(self.esp_state)
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

    def _set_skyrc(self):
        self.main.set_skyrc(self.skyrc.currentData())

    def _set_esp(self):
        hosts = self.esp.text().replace(',', ' ').split()
        self.main.esp, self.main.esp_auto = hosts, self.esp_auto.isChecked()
        settings = QSettings('battbench', 'battbench')
        settings.setValue('esp_hosts', ' '.join(hosts))
        settings.setValue('esp_auto', self.main.esp_auto)
        self.main.worker.cmds.put(('esp', hosts, self.main.esp_auto))

    def _scan(self):
        ble = self.main.worker.ble
        if ble:
            ble.scan_now.set()

    def refresh(self):
        m = self.main
        lines = []
        for dev, on in sorted(m.online.items()):
            d = m.devs.get(dev, {})
            lines.append(tr('{} – {} via {}').format(d.get('name', dev), on.get('version', ''), via_text(on)))
        self.connected.setText('<br>'.join(lines) or tr('none'))
        found = tr(' (found automatically)')
        self.esp_state.setText('<br>'.join(f'{host}{found if n.auto else ""}: {n.state}'
                                           for host, n in sorted(m.worker.nets.items())) or
                               (tr('none found yet') if m.esp_auto else ''))
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
        self.compress.setChecked(QSettings('battbench', 'battbench').value('compress', True, type=bool))
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
        settings = QSettings('battbench', 'battbench')
        policy = backup_policy()
        rows = [('hours', tr('Every'), tr('h (and on exit; 0 = only on exit)'), 0, 24 * 7),
                ('keep', tr('Keep the last'), tr('backups'), 1, 999),
                ('daily', tr('plus the last'), tr('daily'), 0, 999),
                ('weekly', '', tr('weekly'), 0, 999),
                ('monthly', '', tr('monthly backups'), 0, 999)]
        tips = {'hours': tr('A backup is saved when BattBench is closed and, while it runs, every so many hours.'),
                'keep': tr('The newest backups, whatever their age.'),
                'daily': tr('The newest backup of each of the last days that have one.'),
                'weekly': tr('The newest backup of each of the last weeks that have one.'),
                'monthly': tr('The newest backup of each of the last months that have one.')}
        line = QHBoxLayout()
        for i, (key, before, after, lo, hi) in enumerate(rows):
            if key == 'keep':                 # second line: what is kept
                line.addStretch(1)
                form.addRow(tr('Backups:'), line)
                line = QHBoxLayout()
            spin = QSpinBox()
            spin.setRange(lo, hi)
            spin.setValue(policy[key])
            spin.setToolTip(tips[key])
            spin.valueChanged.connect(lambda v, key=key: settings.setValue('backup_' + key, v))
            if before:
                line.addWidget(QLabel(before))
            line.addWidget(spin)
            line.addWidget(QLabel(after + (',' if key in ('keep', 'daily', 'weekly') else '')))
        line.addStretch(1)
        form.addRow('', line)
        where = QLabel(tr('Saved next to the database as {}').format(os.path.basename(db.path) + '-YYYYMMDD-HHMMSS.gz'))
        where.setStyleSheet('color: palette(placeholder-text);')
        form.addRow('', where)
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
        rows = [(tr('File size'), fmt_size(self.db.size())), (tr('Readings'), fmt_int(n['samples']) + (' ' + tr('(compressed: {})').format(fmt_int(n['compressed']))
                                                        if n['compressed'] else '')),
                (tr('Sessions'), fmt_int(n['sessions']) + session_notes(ds, n['compressed_sessions'])),
                (tr('Phases'), fmt_int(n['phases'])),
                (tr('Batteries'), fmt_int(n['batteries']) + (' ' + tr('({} deleted)').format(db_) if db_ else '')),
                (tr('Models'), fmt_int(n['models']) + (' ' + tr('({} deleted)').format(dm) if dm else '')),
                (tr('Chargers'), fmt_int(n['devices']))]
        self.stats.setText('<table cellspacing="2">' + ''.join(
            f'<tr><td>{a}</td><td align="right">&nbsp;&nbsp;{b}</td></tr>' for a, b in rows) + '</table>')

    compressed = Signal()                 # readings compressed: curves are out of date

    def _compress_now(self):
        """Compress everything except the last hour and running sessions, in the foreground with a progress
        window, then optimise and show what it did."""
        if QMessageBox.question(self, tr('Compress readings'), tr(
                'Compress all readings (except the last hour and running sessions) to one per minute and optimise '
                'the database? Sessions and ratings stay; the fine resolution of the curves is lost irrevocably.')
                ) != QMessageBox.Yes:
            return
        n0, size0 = self.db.stats(), self.db.size()
        dlg = QProgressDialog(tr('Compressing readings …'), tr('Cancel'), 0, 100, self)
        dlg.setWindowTitle(tr('Compress readings'))
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)

        def progress(done, n):
            dlg.setMaximum(n)
            dlg.setValue(done)
            QApplication.processEvents()
            return dlg.wasCanceled()

        n_in, n_out = self.db.compress_samples(time.time() - 3600, progress)
        dlg.setLabelText(tr('Optimising database …'))
        dlg.setCancelButton(None)
        QApplication.processEvents()
        note = ''
        try:
            self.db.optimize()
        except sqlite3.Error as e:                          # busy: the worker is writing right now
            note = '<p>' + tr('Optimising was not possible right now ({}); use “Optimise database” later.').format(e)
        dlg.close()
        n = self.db.stats()
        self.refresh_stats()
        self.compressed.emit()
        rows = [(tr('Sessions in total'), fmt_int(n['sessions'])),
                (tr('Compressed now'), tr('{} sessions ({} → {} readings)').format(
                    fmt_int(n['compressed_sessions'] - n0['compressed_sessions']), fmt_int(n_in), fmt_int(n_out))),
                (tr('Compressed in total'), tr('{} sessions').format(fmt_int(n['compressed_sessions']))),
                (tr('File size'), tr('before {} / after {}').format(fmt_size(size0), fmt_size(self.db.size())))]
        QMessageBox.information(self, tr('Compress readings'), '<table cellspacing="3">' + ''.join(
            f'<tr><td>{a}:</td><td>&nbsp;{b}</td></tr>' for a, b in rows) + '</table>' + note)

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
                (tr('Database:'), os.path.abspath(db_path)),
                (tr('Log file:'), "<a href='{}'>{}</a>".format(QUrl.fromLocalFile(logs.log_path(db_path)).toString(),
                                                              logs.log_path(db_path)))]
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


# ======================================================================= charge profiles (MC5000)
def mc_mode_names():
    """Mode names of the MC5000 programs in the UI language (the program stores the English ones)."""
    return {'Charge': tr('Charge'), 'Storage': tr('Storage'), 'Discharge': tr('Discharge'), 'Cycle': tr('Cycle'),
            'Refresh': tr('Refresh'), 'Break in': tr('Break in')}


def profile_fields():
    """(attribute, label, unit, shown(program)); the values to choose come from mc5000.choices()."""
    nickel = lambda p: p.chem in ('NiMH', 'NiCd', 'Eneloop')                       # noqa: E731
    lithium = lambda p: mc5000.CHEM[p.chem][3] > 0                                  # noqa: E731
    cycling = lambda p: p.mode in ('Cycle', 'Refresh', 'Break in')                  # noqa: E731
    return [('charge_ma', tr('Charge current'), 'mA', lambda p: p.mode != 'Discharge'),
            ('discharge_ma', tr('Discharge current'), 'mA', lambda p: p.mode != 'Charge'),
            ('capacity', tr('Capacity limit'), 'mAh', lambda p: True),
            ('charge_mv', tr('Charge voltage'), 'mV', lambda p: p.mode != 'Discharge'),
            ('discharge_mv', tr('Discharge cut-off'), 'mV', lambda p: p.mode != 'Charge'),
            ('storage_mv', tr('Storage voltage'), 'mV', lambda p: lithium(p) and p.mode == 'Storage'),
            ('charge_end_ma', tr('Charge end current'), 'mA', lambda p: lithium(p) and p.mode != 'Discharge'),
            ('discharge_end_ma', tr('Discharge end current'), 'mA', lambda p: p.mode != 'Charge'),
            ('cycles', tr('Cycles'), '', cycling),
            ('rest_after_charge', tr('Rest after charging'), 'min', cycling),
            ('rest_after_discharge', tr('Rest after discharging'), 'min', cycling),
            ('delta_peak', tr('-ΔV end of charge'), 'mV', lambda p: nickel(p) and p.mode != 'Discharge'),
            ('trickle_ma', tr('Trickle current afterwards'), 'mA', lambda p: nickel(p) and p.mode != 'Discharge'),
            ('restart_mv', tr('Recharge below'), 'mV', lambda p: nickel(p) and p.mode != 'Discharge'),
            ('timer', tr('Time limit'), 'min', lambda p: True),
            # sent as the SkyRC app does, but without visible effect on the charger: not shown
            ('temp_limit', tr('Temperature limit'), '°C', lambda p: False),
            ('max_time', tr('Maximum time'), 'min', lambda p: False)]


def field_text(attr, unit, v):
    """A value of a program field as shown: "off" for 0 where the charger has an off."""
    if attr in mc5000.OFF and v == 0:
        return tr_data('off')
    return f'{v} {unit}'.strip()


def profile_text(p: mc5000.Program):
    """Short summary, e.g. "NiMH · Cycle D>C × 2 · 1000 / 500 mA · 2500 mAh"."""
    mode = mc_mode_names().get(p.mode, p.mode)
    if p.mode in ('Cycle', 'Refresh', 'Break in'):
        mode += f' {p.direction} × {p.cycles}' if p.mode == 'Cycle' else f' × {p.cycles}'
    cur = f'{p.charge_ma} mA' if p.mode == 'Charge' else f'{p.discharge_ma} mA' if p.mode == 'Discharge' else \
        f'{p.charge_ma} / {p.discharge_ma} mA'
    return f'{p.chem} · {mode} · {cur} · ≤ {p.capacity} mAh'


class ProgramEditor(QWidget):
    """Form for an MC5000 program: name, battery type, task, Eneloop type, order and the values that apply to the
    task, each chosen from the values the SkyRC app offers. A new battery type, task or Eneloop type fills in the
    app's defaults for it."""

    def __init__(self):
        super().__init__()
        self.extra = {}                       # values of the program the form does not show (kept as they are)
        right = QVBoxLayout(self)
        right.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name = QLineEdit()
        form.addRow(tr('Name'), self.name)
        self.chem = QComboBox()
        self.chem.setMaximumWidth(220)
        self.chem.addItems(mc5000.CHEMISTRIES)
        self.chem.currentTextChanged.connect(lambda _t: self._reset())
        form.addRow(tr('Battery type'), self.chem)
        self.mode = QComboBox()
        self.mode.setMaximumWidth(220)
        self.mode.currentIndexChanged.connect(lambda _i: self._reset())
        form.addRow(tr('Task'), self.mode)
        self.eneloop = QComboBox()
        self.eneloop.setMaximumWidth(220)
        for k, e in enumerate(mc5000.ENELOOP):
            self.eneloop.addItem(e[0], k)
        self.eneloop.currentIndexChanged.connect(lambda _i: self._reset())
        self.eneloop_label = QLabel(tr('Eneloop type'))
        form.addRow(self.eneloop_label, self.eneloop)
        self.direction = QComboBox()
        self.direction.setMaximumWidth(220)
        self.direction.addItems(mc5000.DIRECTIONS)
        self.dir_label = QLabel(tr('Order'))
        form.addRow(self.dir_label, self.direction)
        self.fields = {}
        for attr, label, unit, _shown in profile_fields():
            c = QComboBox()
            c.setMaximumWidth(160)
            c.setMaxVisibleItems(15)
            c.currentIndexChanged.connect(lambda _i: self._update())
            lab = QLabel(label)
            form.addRow(lab, c)
            self.fields[attr] = (lab, c, unit)
        right.addLayout(form)
        self.problems = QLabel()
        self.problems.setStyleSheet(f'color: {COLORS["error"]};')
        self.problems.setWordWrap(True)
        right.addWidget(self.problems)

    def _set_choices(self, attr, chem, value):
        """The app's values for this field and battery type; a stored value outside them is kept as a choice."""
        _lab, c, unit = self.fields[attr]
        vals = mc5000.choices(attr, chem)
        if value not in vals:
            vals = sorted(vals + [value])
        c.blockSignals(True)
        c.clear()
        for v in vals:
            c.addItem(field_text(attr, unit, v), v)
        c.setCurrentIndex(max(0, c.findData(value)))
        c.blockSignals(False)

    def fill(self, p, name):
        for w in (self.chem, self.mode, self.eneloop):
            w.blockSignals(True)
        self.name.setText(name)
        self.chem.setCurrentText(p.chem)
        self._set_modes(p.chem, p.mode)
        self.eneloop.setCurrentIndex(max(0, self.eneloop.findData(p.eneloop)))
        self.direction.setCurrentText(p.direction)
        for w in (self.chem, self.mode, self.eneloop):
            w.blockSignals(False)
        self._set_values(p)

    def _set_values(self, p):
        self.extra = dict(storage_mv=p.storage_mv)
        for attr in self.fields:
            value = getattr(p, attr)
            if attr == 'storage_mv' and not value:
                value = mc5000.CHEM[p.chem][3]
            self._set_choices(attr, p.chem, value)
        self._update()

    def _set_modes(self, chem, mode):
        names = mc_mode_names()
        self.mode.blockSignals(True)
        self.mode.clear()
        for m in mc5000.CHEM[chem][0]:
            self.mode.addItem(names.get(m, m), m)
        i = self.mode.findData(mode)
        self.mode.setCurrentIndex(max(0, i))
        self.mode.blockSignals(False)

    def _reset(self):
        """Another battery type, task or Eneloop type: the SkyRC app's defaults for it (as the app does)."""
        chem = self.chem.currentText()
        self._set_modes(chem, self.mode.currentData())    # the battery type's tasks (the same one if it has it)
        d = mc5000.defaults(chem, self.mode.currentData() or 'Charge', self.eneloop.currentData() or 0)
        self.direction.setCurrentText(d.direction)
        self._set_values(d)

    def program(self) -> mc5000.Program:
        p = mc5000.Program(name=self.name.text().strip(), chem=self.chem.currentText(),
                           mode=self.mode.currentData() or 'Charge', direction=self.direction.currentText(),
                           eneloop=self.eneloop.currentData() or 0)
        for attr, (_lab, c, _u) in self.fields.items():
            if c.currentData() is not None:
                setattr(p, attr, c.currentData())
        if mc5000.CHEM[p.chem][3] == 0:
            p.storage_mv = 0                  # nickel-like: no storage voltage
        return p

    def _update(self):
        p = self.program()
        for attr, label, _u, shown in profile_fields():
            lab, c, _unit = self.fields[attr]
            lab.setVisible(shown(p))
            c.setVisible(shown(p))
        self.dir_label.setVisible(p.mode == 'Cycle')
        self.direction.setVisible(p.mode == 'Cycle')
        self.eneloop_label.setVisible(p.chem == 'Eneloop')
        self.eneloop.setVisible(p.chem == 'Eneloop')
        self.problems.setText('; '.join(tr_data(x) for x in p.problems()))

    def check(self, need_name=True):
        """The program if it can be used (and saved: with a name), else None after telling why."""
        p = self.program()
        if need_name and not p.name:
            QMessageBox.information(self, tr('Profiles'), tr('Please give the profile a name.'))
            return None
        if p.problems():
            QMessageBox.warning(self, tr('Profiles'), '; '.join(tr_data(x) for x in p.problems()))
            return None
        return p


class ProfileTab(QWidget):
    """Charge profiles for the SkyRC MC5000, stored in the database. Started on slots from the tiles (right
    click); nothing goes to the charger from here."""

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.pid = None
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda cur, _old: self.show_profile(cur.data(Qt.UserRole) if cur
                                                                                 else None))
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in ((tr('New'), self.new), (tr('Copy'), self.copy), (tr('Delete'), self.delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        left.addLayout(row)
        hint = QLabel(tr('Right click on a slot of an MC5000 to start a profile there, also on several slots.'))
        hint.setWordWrap(True)
        left.addWidget(hint)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        self.editor = ProgramEditor()
        right.addWidget(self.editor)
        save = QPushButton(tr('Save'))
        save.clicked.connect(self.save)
        right.addWidget(save, 0, Qt.AlignLeft)
        right.addStretch()
        box = QWidget()
        box.setLayout(right)
        lay.addWidget(scrolled(box), 2)
        self.load()

    def load(self, select=None):
        if select is None:
            select = self.pid
        self.list.blockSignals(True)
        self.list.clear()
        for pid, name, data in self.db.profiles():
            it = QListWidgetItem(f'{name}\n{profile_text(mc5000.Program.from_dict(data))}')
            it.setData(Qt.UserRole, pid)
            self.list.addItem(it)
            if pid == select:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.show_profile(self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None)

    def show_profile(self, pid):
        self.pid = pid
        row = next(((n, d) for i, n, d in self.db.profiles() if i == pid), None)
        p = mc5000.Program.from_dict(row[1]) if row else mc5000.defaults('NiMH')
        self.editor.fill(p, row[0] if row else p.name)

    def save(self):
        p = self.editor.check()
        if p:
            self.pid = self.db.save_profile(self.pid, p.name, p.to_dict())
            self.load(self.pid)

    def new(self):
        """New profile in its own window, with the defaults of the battery type shown now."""
        self._create(mc5000.defaults(self.editor.chem.currentText() or 'NiMH'), '')

    def copy(self):
        p = self.editor.program()
        self._create(p, p.name + ' ' + tr('(copy)'))

    def _create(self, p, name):
        dlg = ProfileDialog(self, 'new', p, name)
        if dlg.exec() == QDialog.Accepted:
            q = dlg.program()
            self.pid = self.db.save_profile(None, q.name, q.to_dict())
            self.load(self.pid)

    def delete(self):
        if self.pid is None:
            return
        if QMessageBox.question(self, tr('Profiles'),
                                tr('Delete the profile "{}"?').format(self.editor.name.text())) == QMessageBox.Yes:
            self.db.delete_profile(self.pid)
            self.pid = None
            self.load()


class ProfileDialog(QDialog):
    """Edit an MC5000 program in a dialog. kind 'new': a new profile (Save creates it); 'edit': a saved profile (Save
    updates it, Clone saves a copy as a new one); 'current': the current settings of slots, kept only for their next
    start (Use), or saved as a new profile. action() tells what was chosen: 'save', 'new' or 'use'."""

    def __init__(self, parent, kind, p, name='', note=''):
        super().__init__(parent)
        self._action = None
        self.setWindowTitle({'new': tr('New profile'), 'edit': tr('Edit profile'),
                             'current': tr('Current settings')}[kind])
        lay = QVBoxLayout(self)
        if note:
            lab = QLabel(note)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        self.editor = ProgramEditor()
        self.editor.fill(p, name)
        lay.addWidget(self.editor)
        box = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons = {'new': [(tr('Save as new profile'), 'new')],
                   'edit': [(tr('Save'), 'save'), (tr('Clone'), 'new')],
                   'current': [(tr('Use'), 'use'), (tr('Create new profile'), 'new')]}[kind]
        for text, action in buttons:
            b = box.addButton(text, QDialogButtonBox.ActionRole)
            b.clicked.connect(lambda _c=False, a=action: self._done(a))
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _done(self, action):
        if self.editor.check(need_name=action != 'use'):
            self._action = action
            self.accept()

    def action(self):
        return self._action

    def program(self):
        return self.editor.program()


class ControlPanel(QFrame):
    """Right of the tiles of a charger BattBench can control (MC5000): the chosen slots (tile click, Ctrl + click for
    more), what runs there and with which program, start a profile, stop, save the program as a profile."""

    def __init__(self, main, dev, slots):
        super().__init__()
        self.main, self.dev, self.slots = main, dev, slots
        self.chosen = []
        self.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 6)                 # title bar level with the tiles' title bars
        bar = QFrame()
        bar.setStyleSheet(f'background:{CONTROL_COLOR}; color:white;')
        bar.setFixedHeight(HEAD_HEIGHT)
        head = QHBoxLayout(bar)
        head.setContentsMargins(6, 0, 6, 0)
        title = QLabel('<b>' + tr('Charger control') + '</b>')
        head.addWidget(title)
        head.addStretch()
        self.buttons = []
        for s in range(slots):
            b = QToolButton()
            b.setText(str(s + 1))
            b.setCheckable(True)
            b.setStyleSheet('QToolButton { background: white; color: black; border: 1px solid #888; padding: 0 6px; }'
                            'QToolButton:checked { background: #1a237e; color: white; border: 1px solid white;'
                            ' font-weight: bold; }')
            b.setFixedHeight(HEAD_HEIGHT - 6)
            b.setToolTip(tr('Slot {}: a click chooses it, another click leaves it out again').format(s + 1))
            b.clicked.connect(lambda _c, s=s: self._toggle(s))
            head.addWidget(b)
            self.buttons.append(b)
        self.settings = QToolButton()
        self.settings.setText('⚙')
        self.settings.setToolTip(tr('Charger settings (sounds, backlight, screen timeout) and Bluetooth connection'))
        self.settings.setStyleSheet('QToolButton { background: white; color: black; border: 1px solid #888;'
                                    ' padding: 0 4px; }')
        self.settings.setFixedHeight(HEAD_HEIGHT - 6)
        self.settings.clicked.connect(lambda: self.main.charger_settings(self.dev))
        head.addWidget(self.settings)
        lay.addWidget(bar)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        self.info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        box = QScrollArea()                   # long program details scroll: the panel stays as low as the tiles
        box.setWidgetResizable(True)
        box.setFrameShape(QFrame.NoFrame)
        box.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        box.setMinimumHeight(3 * self.fontMetrics().lineSpacing())
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        box.setWidget(self.info)
        lay.addWidget(box, 1)
        row = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(12)
        self.combo.currentIndexChanged.connect(lambda _i: self.refresh(self.chosen))
        row.addWidget(self.combo, 1)
        self.start = QPushButton(tr('Start'))
        self.start.clicked.connect(lambda: self.main.start_slots(self.dev, self.chosen, self.combo.currentData()))
        row.addWidget(self.start)
        self.stop = QPushButton(tr('Stop'))
        self.stop.clicked.connect(lambda: self.main.stop_slots(self.dev, self.chosen))
        row.addWidget(self.stop)
        menu = QMenu(self)                    # profile actions in one button: one row less
        menu.setToolTipsVisible(True)
        self.edit = menu.addAction(tr('Edit …'))
        self.edit.setToolTip(tr('Edit the chosen profile; with "Current settings": change them for the next start'))
        self.edit.triggered.connect(lambda: self.main.edit_profile(self.dev, self.chosen, self.combo.currentData()))
        self.new = menu.addAction(tr('New …'))
        self.new.setToolTip(tr('New profile with the defaults of the SkyRC app'))
        self.new.triggered.connect(lambda: self.main.new_profile(self.dev))
        self.from_slot = menu.addAction(tr('Create new from current settings …'))
        self.from_slot.setToolTip(tr('New profile with the current settings of the slot as template'))
        self.from_slot.triggered.connect(lambda: self.main.new_profile_from_slot(self.dev, self.chosen))
        prof = QPushButton(tr('Profile'))
        prof.setMenu(menu)
        row.addWidget(prof)
        lay.addLayout(row)

    def select_profile(self, pid):
        self._profiles()
        self.combo.setCurrentIndex(max(0, self.combo.findData(pid)))

    def _toggle(self, slot):
        chosen = set(self.main.ctrl_sel.get(self.dev, ())) ^ {slot}
        self.main.ctrl_sel[self.dev] = chosen or {slot}
        self.main.follow_ctrl(self.dev)

    def _profiles(self):
        last = self.main.db.setting('last_profile')
        cur = self.combo.currentData()
        items = [(tr('Current settings'), 'current')] + [(n, i) for i, n, _d in self.main.db.profiles()]
        if [(self.combo.itemText(k), self.combo.itemData(k)) for k in range(self.combo.count())] != items:
            self.combo.blockSignals(True)
            self.combo.clear()
            for n, i in items:
                self.combo.addItem(n, i)
            want = cur if cur is not None else (int(last) if last and str(last).isdigit() else 'current')
            self.combo.setCurrentIndex(max(0, self.combo.findData(want)))
            self.combo.blockSignals(False)

    def program_of(self, slot):
        """(text about the slot's program, mc5000.Program or None, key to compare slots)."""
        smp = self.main.live.get((self.dev, slot))
        st = mc5000.from_status(bytes.fromhex(smp['raw'])) if smp and smp.get('raw') else None
        summ = self.main.slot_summary(self.dev, slot)      # currents as the charger reports them, or None
        info = self.main.slot_program(self.dev, slot)
        p = self.main.known_program(self.dev, slot)
        profiles = [(n, mc5000.Program.from_dict(d)) for _i, n, d in self.main.db.profiles()]
        names = mc_mode_names()
        if p:
            match = next((n for n, q in profiles if mc5000.same(p, q)), None)
            when = fmt_t(info['t']) if info.get('t') else ''
            head = (tr('Profile <b>{}</b>').format(html_escape(match)) if match else
                    tr('Started from BattBench, no saved profile matches it.')) + (f' · {when}' if when else '')
            return head + '<br>' + profile_details(p), p, ('p', json.dumps(dict(p.to_dict(), name='')))
        if st is None:
            return tr('No data from this slot yet.'), None, ('none',)
        chem, mode = st
        cands = [n for n, q in profiles if (q.chem, q.mode) == st and (not summ or mc5000.same_currents(q, summ))]
        vals = [chem, names.get(mode, mode or '?')]
        key = ('s', chem, mode)
        if summ:
            vals += [tr('charge {} A').format(QLocale(i18n.current).toString(summ['charge_ma'] / 1000, 'f', 1)),
                     tr('discharge {} A').format(QLocale(i18n.current).toString(summ['discharge_ma'] / 1000, 'f', 1))]
            key += (summ['charge_ma'], summ['discharge_ma'])
        text = '<b>' + tr('Set on the charger:') + '</b> ' + html_escape(' · '.join(vals)) + \
            '<br><span style="color:gray">' + tr('The charger does not report voltages, rests, cycles and the other '
                                                 'values.') + '</span>'
        if cands:
            text += '<br>' + tr('Fits: {}').format(html_escape(', '.join(cands)))
        return text, None, key

    def refresh(self, chosen):
        self.chosen = [s for s in chosen if s < self.slots]
        for s, b in enumerate(self.buttons):
            b.setChecked(s in self.chosen)
        self._profiles()
        on = self.main.online.get(self.dev) or {}
        ready = bool(set(on.get('vias', ())) & {'ble', 'net'}) and not on.get('stale')
        if ready:
            for s in self.chosen:
                self.main.want_summary(self.dev, s)
        parts = []
        if not ready:
            parts.append('<span style="color:%s">%s</span>' % (COLORS['error'], tr(
                'Not connected over Bluetooth or a BattBench-ESP: starting and stopping is not possible.')))
        if len(self.chosen) == 1:
            s = self.chosen[0]
            smp = self.main.live.get((self.dev, s))
            state = mode_name(smp) if smp else tr('no data')
            text, _p, _key = self.program_of(s)
            parts.append('<b>' + tr('Slot {}').format(s + 1) + f'</b> · {html_escape(state)}')
            parts.append(text)
        elif self.chosen:
            progs = [self.program_of(s) for s in self.chosen]
            parts.append('<b>' + tr('Slots {}').format(', '.join(str(s + 1) for s in self.chosen)) + '</b>')
            if len({k for _t, _p, k in progs}) == 1:
                parts.append(progs[0][0])
            else:
                parts.append(tr('Different settings – choose only one slot to see them.'))
        else:
            parts.append(tr('Choose a slot: click its tile or its number above.'))
        temp = [self.main.temp_progs[(self.dev, s)] for s in self.chosen if (self.dev, s) in self.main.temp_progs]
        if self.combo.currentData() == 'current' and temp:
            parts.append('<br><i>' + tr('Changed current settings, used at the next start:') + '</i><br>' +
                         profile_details(temp[0]))
        self.info.setText('<br>'.join(parts))
        self.start.setEnabled(ready and bool(self.chosen))
        self.stop.setEnabled(ready and bool(self.chosen))
        self.from_slot.setEnabled(bool(self.chosen))
        self.edit.setEnabled(self.combo.currentData() != 'current' or bool(self.chosen))


def profile_details(p: mc5000.Program):
    """Every value of a program that applies to its task, as HTML lines."""
    lines = [html_escape(profile_text(p))]
    vals = []
    if p.mode == 'Cycle':
        vals.append(tr('Order') + ' ' + p.direction)
    if p.chem == 'Eneloop':
        vals.append(tr('Eneloop type') + ' ' + mc5000.ENELOOP[p.eneloop][0])
    for attr, label, unit, shown in profile_fields():
        if shown(p) and attr not in ('charge_ma', 'discharge_ma', 'capacity'):
            vals.append(f'{label} {field_text(attr, unit, getattr(p, attr))}')
    lines.append('<span style="color:gray">' + html_escape(' · '.join(vals)) + '</span>')
    return '<br>'.join(lines)


class ChargerSettingsDialog(QDialog):
    """System settings of an MC5000 (sounds, backlight, screen timeout): read from the charger when it opens. Below
    them its Bluetooth connection: pause / switch off / on (to use the SkyRC app), the BattBench-ESP's web page."""
    BT_POLL_MS = 5000

    def __init__(self, parent, dev):
        super().__init__(parent)
        self.dev, self.read, self.cmds = dev, None, parent.worker.cmds
        self.web_url = None
        self.setWindowTitle(tr('Charger settings'))
        lay = QVBoxLayout(self)
        self.state = QLabel(tr('Reading the settings from the charger …'))
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        form = QFormLayout()
        self.combos = {}
        rows = [('key_beep', tr('Key beep'), [(tr_data(t), i) for i, t in enumerate(mc5000.VOLUMES)]),
                ('notify_beep', tr('Notification sound'), [(tr_data(t), i) for i, t in enumerate(mc5000.VOLUMES)]),
                ('done_tone', tr('Completion tone'), [(tr_data(t), i) for i, t in enumerate(mc5000.DONE_TONES)]),
                ('backlight', tr('Backlight'), [(tr_data(t), i + 1) for i, t in enumerate(mc5000.LIGHTS)]),
                ('timeout', tr('Screen timeout'),
                 [(tr_data('off') if m == 0 else tr('{} min').format(m), m) for m in mc5000.TIMEOUTS])]
        for attr, label, items in rows:
            c = QComboBox()
            for text, v in items:
                c.addItem(text, v)
            c.setEnabled(False)
            form.addRow(label, c)
            self.combos[attr] = c
        lay.addLayout(form)
        grp = QGroupBox(tr('Bluetooth connection'))
        g = QVBoxLayout(grp)
        note = QLabel(tr('The charger takes only one Bluetooth connection. Pause or switch it off here to use the '
                         'SkyRC app; this applies to BattBench and every BattBench-ESP.'))
        note.setWordWrap(True)
        note.setStyleSheet('color: palette(placeholder-text);')
        g.addWidget(note)
        self.bt_state = QLabel(tr('Asking …'))
        self.bt_state.setWordWrap(True)
        self.bt_state.setTextFormat(Qt.RichText)
        g.addWidget(self.bt_state)
        row = QHBoxLayout()
        for action, text in (('pause', tr('Pause 5 min')), ('off', tr('Switch off')), ('on', tr('Switch on'))):
            b = QPushButton(text)
            b.clicked.connect(lambda _c, a=action: self.bt(a))
            row.addWidget(b)
        row.addStretch()
        self.web = QPushButton(tr('BattBench-ESP web page …'))
        self.web.setToolTip(tr('Opens the web page of the BattBench-ESP in the browser'))
        self.web.clicked.connect(lambda: self.web_url and QDesktopServices.openUrl(QUrl(self.web_url)))
        self.web.hide()
        row.addWidget(self.web)
        g.addLayout(row)
        lay.addWidget(grp)
        self.bt_timer = QTimer(self)                          # pause countdown, connection coming back
        self.bt_timer.timeout.connect(lambda: self.bt('status'))
        self.bt_timer.start(self.BT_POLL_MS)
        self.bt('status')
        self.box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.box.accepted.connect(self.accept)
        self.box.rejected.connect(self.reject)
        self.box.button(QDialogButtonBox.Save).setEnabled(False)
        lay.addWidget(self.box)

    def answer(self, d):
        if d.get('error') or not d.get('data'):
            self.state.setText('<span style="color:%s">%s</span>' % (COLORS['error'], html_escape(
                d.get('error') or tr('no answer'))))
            return
        self.read = d['data']
        for attr, c in self.combos.items():
            c.setCurrentIndex(max(0, c.findData(self.read[attr])))
            c.setEnabled(True)
        self.state.setText(tr('Settings of the charger. Save sends them to it.'))
        self.box.button(QDialogButtonBox.Save).setEnabled(True)

    def bt(self, action):
        self.cmds.put(('bt', self.dev, action))

    def bt_answer(self, d):
        """State of the Bluetooth connections from the worker (charger_info kind 'bt')."""
        def left(secs):
            secs = max(0, int(secs))
            return tr('paused, {} left').format(f'{secs // 60}:{secs % 60:02d} min')
        lines = []
        data = d.get('data') or {}
        direct = data.get('direct')
        if direct:
            until = direct['until']
            state = (skyrc_unused_names().get(direct['unused'], direct['unused']) if direct['unused'] else
                     tr('switched off') if until == float('inf') else
                     left(until - time.time()) if until else
                     tr('connected') if direct['connected'] else tr('may connect'))
            lines.append('<b>' + tr('BattBench (Bluetooth of this PC):') + '</b> ' + html_escape(state))
        for e in data.get('esp', ()):
            st = e['state']
            state = (tr('not reachable') if e['on'] is None else tr('switched off') if st == 'off' else
                     left(e['paused_secs']) if st == 'paused' else
                     tr('connected') if st == 'connected' else st)
            lines.append('<b>' + 'BattBench-ESP ' + html_escape(e['host']) + ':</b> ' + html_escape(state))
        if d.get('error'):
            lines.append('<span style="color:%s">%s</span>' % (COLORS['error'], html_escape(d['error'])))
        self.bt_state.setText('<br>'.join(lines) or tr('No Bluetooth connection known.'))
        esp = data.get('esp') or []
        self.web_url = esp[0]['url'] if esp else None
        self.web.setVisible(bool(esp))

    def values(self):
        out = dict(self.read)
        out.update({attr: c.currentData() for attr, c in self.combos.items()})
        return out

    def changed(self):
        return self.read is not None and self.values() != self.read


class ReportDialog(QDialog):
    """Choices for "Export report …": language and format (HTML or PDF), both remembered; a chart per phase (on)."""

    def __init__(self, parent, has_phases):
        super().__init__(parent)
        self.setWindowTitle(tr('Export report'))
        settings = QSettings('battbench', 'battbench')
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.language = QComboBox()
        self.language.addItem(tr('Language of the app ({})').format(LANGUAGES.get(i18n.current, i18n.current)), '')
        for k, name in LANGUAGES.items():
            self.language.addItem(name, k)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.value('report_lang', ''))))
        form.addRow(tr('Language:'), self.language)
        lay.addLayout(form)
        self.phases = QCheckBox(tr('A chart for each charge / discharge phase'))
        self.phases.setChecked(has_phases)    # on by default, every time (not remembered)
        self.phases.setEnabled(has_phases)
        if not has_phases:
            self.phases.setToolTip(tr('This session has only one phase.'))
        lay.addWidget(self.phases)
        self.format = QComboBox()
        self.format.addItem(tr('HTML page (charts embedded, opens in any browser)'), 'html')
        self.format.addItem(tr('PDF (DIN A4)'), 'pdf')
        self.format.setCurrentIndex(max(0, self.format.findData(settings.value('report_format', 'html'))))
        form.addRow(tr('Format:'), self.format)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr('Export …'))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def lang(self):
        return self.language.currentData() or i18n.current

    def _ok(self):
        settings = QSettings('battbench', 'battbench')
        settings.setValue('report_lang', self.language.currentData())
        settings.setValue('report_format', self.format.currentData())
        self.accept()


class StartDialog(QDialog):
    """Start a profile on a slot of an MC5000, optionally on further slots with the same profile."""

    def __init__(self, parent, profiles, slot, states, running, last=None):
        super().__init__(parent)
        self.setWindowTitle(tr('Start a profile'))
        self.setMinimumWidth(380)
        self.profiles = profiles
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.combo = QComboBox()
        for pid, name, _d in profiles:
            self.combo.addItem(name, pid)
        if last is not None:
            self.combo.setCurrentIndex(max(0, self.combo.findData(int(last)) if str(last).isdigit() else 0))
        form.addRow(tr('Profile'), self.combo)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        form.addRow('', self.summary)
        lay.addLayout(form)
        lay.addWidget(QLabel(tr('Slots (the same profile on each):')))
        self.checks = []
        for i, state in enumerate(states):
            c = QCheckBox(tr('Slot {}').format(i + 1) + f'  ({state})')
            c.setChecked(i == slot)
            c.toggled.connect(self._update)
            lay.addWidget(c)
            self.checks.append(c)
        self.running = running
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet(f'color: {COLORS["error"]};')
        lay.addWidget(self.warn)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.start = self.buttons.addButton(tr('Start'), QDialogButtonBox.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.combo.currentIndexChanged.connect(self._update)
        self._update()

    def program(self):
        pid = self.combo.currentData()
        data = next((d for i, _n, d in self.profiles if i == pid), {})
        return mc5000.Program.from_dict(data)

    def chosen(self):
        return [i for i, c in enumerate(self.checks) if c.isChecked()]

    def _update(self, *_):
        p = self.program()
        self.summary.setText(profile_text(p))
        problems = p.problems()
        busy = [i + 1 for i in self.chosen() if self.running[i]]
        texts = []
        if problems:
            texts.append('; '.join(tr_data(x) for x in problems))
        if busy:
            texts.append(tr('Slot {} is working: its task is replaced and starts again from zero.').format(
                ', '.join(map(str, busy))))
        self.warn.setText('\n'.join(texts))
        self.start.setEnabled(bool(self.chosen()) and not problems)

    def result_values(self):
        return self.combo.currentData(), self.program(), self.chosen()


class DeviceTab(QWidget):
    """Known chargers: name (only stored here, never written to the charger), model, identity, last seen."""
    changed = Signal()
    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.online = {}                  # dev -> (connections ('usb', 'ble', 'net'), their text)
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
            vias, text = self.online.get(d['id'], (None, ''))
            on = vias is not None
            vals = [d['name'], d['model'], d['slots'], d['version'], d['key'] + (f" / {d['alt_key']}" if d['alt_key'] else ''),
                    fmt_t(d['last_seen']), tr('connected ({})').format(text) if on else '–']
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
        now = {dev: (tuple(on.get('vias', ())), via_text(on)) for dev, on in online.items()}
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
            'tmax', 'grade', 'dur', 'label']

    def __init__(self, db_path, source, a4='both', bt=True, esp=(), esp_auto=True, skyrc='auto', skyrc_chosen=True):
        super().__init__()
        self.setWindowTitle(f'BattBench {__version__}')
        self.source, self.a4 = source, a4
        self.esp = list(esp)           # BattBench-ESP hosts entered by hand
        self.esp_auto = esp_auto       # and the ones found by mDNS
        self.skyrc = skyrc             # SKYRC_MODES key
        self.t_start = time.time()     # tiles only for chargers connected / sending data in this run
        self.auto_pick = True          # after the start: jump to the first charging (else full) slot once
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
        self.ctrl_sel = {}             # dev -> slots chosen for the control panel (Ctrl + click: several)
        self.summaries = {}            # (dev, slot) -> dict(t, key, data) of the last 5F answer (MC5000)
        self.asking = set()            # (dev, slot) with a 5F query under way
        self.settings_dlg = None       # open ChargerSettingsDialog
        self.temp_progs = {}           # (dev, slot) -> mc5000.Program: changed current settings for the next start

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

        self.hsplit = hsplit = QSplitter(Qt.Horizontal)
        self.plot = CurvePlot()
        hsplit.addWidget(self.plot)
        self.result = ResultPanel(self.db)
        self.result.dev_name = self.dev_name
        self.result.busy = self.busy_batteries
        self.result.save.connect(self.save_meta)
        self.result.save_label.connect(lambda sid, text: self.worker.cmds.put(('label', sid, text)))
        self.result.new_battery.connect(self.new_battery_for_session)
        self.result.export_report.connect(self.export_shown)
        self.result.phase_clicked.connect(self.plot.show_phase)          # phase list and chart stay in step
        self.plot.phase_changed.connect(self.result.mark_phase)
        hsplit.addWidget(self.result)
        hsplit.setSizes([1100, 400])
        vsplit.addWidget(hsplit)

        self.tabs = QTabWidget()
        heads = session_headers()
        self.table = make_table([heads[c] for c in self.COLS], autofilter=True)         # last: the note
        self.table.autofilter.header.desc_first = {self.COLS.index('grade')}         # best first
        self.table.sortByColumn(0, Qt.DescendingOrder)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)      # Ctrl / Shift: several, to delete
        row_menu(self.table, None, self.delete_sessions, enter=self.result.battery.setFocus, extra=[
            (tr('Export report …'), lambda: self.export_session(selected_ids(self.table)[0]))])
        self.table.currentCellChanged.connect(self._row_changed)    # click or arrow keys
        self.table.cellClicked.connect(self._row_clicked)           # same row again (e.g. after a slot view)
        self.table.cellDoubleClicked.connect(self._row_double_clicked)
        self.tabs.addTab(self.table, tr('Sessions'))
        self.btab = BatteryTab(self.db)
        self.btab.open_session.connect(self.open_session)
        self.btab.export_session.connect(self.export_session)
        self.btab.changed.connect(self.battery_changed)
        self.btab.models_changed.connect(self.models_changed)
        self.btab.sessions_deleted.connect(lambda: (self.load_tables(), self.stab.refresh_stats()))
        self.tabs.addTab(self.btab, tr('Batteries'))
        self.mtab = ModelTab(self.db)
        self.mtab.changed.connect(self.models_changed)
        self.mtab.show_batteries.connect(self.show_model_batteries)
        self.tabs.addTab(self.mtab, tr('Models'))
        self.dtab = DeviceTab(self.db)
        self.dtab.changed.connect(self.devices_renamed)
        self.tabs.addTab(self.dtab, tr('Chargers'))
        self.ptab = ProfileTab(self.db)
        self.tabs.addTab(self.ptab, tr('Profiles'))
        self.stab = SettingsTab(self.db)
        self.stab.deleted_changed.connect(self.deleted_changed)
        self.stab.compressed.connect(self.data_changed)
        self.tabs.addTab(scrolled(self.stab), tr('Settings'))   # long pages scroll: the pane can be made low
        self.tabs.addTab(HelpTab(), tr('Help'))
        self.tabs.addTab(scrolled(InfoTab(db_path)), tr('Info'))
        vsplit.addWidget(self.tabs)
        vsplit.setSizes([200, 535, 215])          # tiles, chart + result, tables
        vsplit.setStretchFactor(0, 0)             # a taller window goes to the chart and the tables
        vsplit.setCollapsible(0, False)           # the tiles never get cut off
        self.tiles_user = False                   # the user made the tile area higher: keep it (see _fit_tiles)
        vsplit.splitterMoved.connect(self._tiles_dragged)
        root.addWidget(vsplit, 1)
        self.setCentralWidget(central)

        self.st_esp_icon, self.st_esp = QLabel(), QLabel()      # BattBench-ESP bridges and their address
        self.st_esp_icon.setPixmap(via_pixmap(('net',), self.palette().text().color().name()))
        for w in (self.st_esp_icon, self.st_esp):
            w.hide()
            self.statusBar().addPermanentWidget(w)
        self.st_conn = QLabel('')
        self.statusBar().addPermanentWidget(self.st_conn)
        self.st_conn.setText(tr('Starting …') if source == 'device' else tr('No chargers'))

        self.thread = QThread()
        self.worker = Worker(db_path, source, a4, bt, esp, esp_auto, skyrc, skyrc_chosen)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.cycle.connect(self.on_cycle)
        self.worker.command_result.connect(self.command_result)
        self.worker.charger_info.connect(self.charger_info)
        self.worker.skyrc_changed.connect(lambda mode: self.set_skyrc(mode, tell=False))
        self.worker.status.connect(self.st_conn.setText)
        self.worker.sessions_changed.connect(self.load_tables)
        self.worker.data_changed.connect(self.data_changed)
        self.thread.start()
        self.offline_timer = QTimer(self)                  # elapsed time on the offline notices
        self.offline_timer.timeout.connect(self._update_offline)
        self.offline_timer.start(1000)                     # also the countdown of a Bluetooth pause
        self.bt_holds = {}
        self.blink_dark = False
        self.blink_timer = QTimer(self)                    # error slots blink
        self.blink_timer.timeout.connect(self._blink)
        self.blink_timer.start(BLINK_MS)

        self.load_tables()
        self.select_slot(None)
        self.restore_layout()

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
        page = TilePage()
        control = self.devs[dev]['model'] == 'MC5000'      # chargers BattBench can start / stop
        tile_box = QWidget()
        grid = QGridLayout(tile_box)
        grid.setContentsMargins(4, 6, 4, 0)
        n = slots if control else self.per_row(slots)
        for c in range(n):
            grid.setColumnStretch(c, 1)                            # all tiles of a charger equally wide
        tiles = []
        for slot in range(slots):
            t = SlotTile((dev, slot), compact=slots > 8)
            t.approx_res = self.devs[dev]['model'] == 'A4Air'
            t.clicked.connect(self._tile_clicked)
            t.menu.connect(self._tile_menu)
            if control:
                t.setMinimumWidth(170)
                t.setToolTip(tr('Click: show this slot and choose it in the charger control; Ctrl + click: choose '
                                'several slots there'))
            grid.addWidget(t, slot // n, slot % n)
            tiles.append(t)
        grid.setRowStretch(grid.rowCount(), 1)
        panel = scroll = None
        if control:                       # tiles scroll, the control panel takes the room on the right
            lay = QHBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setWidget(tile_box)
            lay.addWidget(scroll, 3)
            panel = ControlPanel(self, dev, slots)
            lay.addWidget(panel, 2)
        else:
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(tile_box)
        self.stack.addWidget(page)
        self.rows[dev] = dict(tab=tab, page=page, tiles=tiles, panel=panel, scroll=scroll)
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

    def _tiles_need(self):
        """Height of the tile area for the shown charger's tiles (and control panel), None without one."""
        if self.shown_dev not in self.rows:
            return None
        row = self.rows[self.shown_dev]
        page, scroll = row['page'], row['scroll']
        if scroll is not None:            # never lower than a tile, plus the scroll bar when the tiles scroll
            bar = scroll.horizontalScrollBar()
            h = scroll.widget().sizeHint().height() + (bar.sizeHint().height() if bar.maximum() > 0 else 0)
            if scroll.minimumHeight() != h:
                scroll.setMinimumHeight(h)
        page_h = max(page.sizeHint().height(), page.minimumSizeHint().height())
        return self.top.sizeHint().height() - self.stack.sizeHint().height() + page_h

    def _fit_tiles(self):
        """The tile area gets just the room the tiles need, the chart the rest - grown when the tiles need more,
        lowered only while the user has not made it higher by hand."""
        need = self._tiles_need()
        sizes = self.vsplit.sizes()
        if need is None or abs(sizes[0] - need) <= 4 or sizes[0] > need and self.tiles_user:
            return                        # (a few px off: the splitter's own minimum, not worth moving)
        if sizes[1] > need - sizes[0]:
            self.vsplit.setSizes([need, sizes[1] - (need - sizes[0]), *sizes[2:]])

    def _tiles_dragged(self, _pos, index):
        """Splitter dragged by the user: higher than needed = keep it; dragged back to the tiles = fit again."""
        if index == 1:
            need = self._tiles_need()
            self.tiles_user = need is not None and self.vsplit.sizes()[0] > need + 4
            self.db.set_setting('tiles_user', '1' if self.tiles_user else '')

    def show_charger(self, dev):
        """Click on a charger tab: its tiles, and the slot last selected there (else the first running one)."""
        self.auto_pick = False
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
            row['tab'].via.setToolTip(via_text(on) if on else '')
            if on and on.get('stale'):
                row['tab'].info.setText(tr('no answer'))
            elif on:
                row['tab'].info.setText(f"{on['in_mv'] / 1000:.2f} V" if on.get('in_mv') else '')
            else:
                row['tab'].info.setText(tr('disconnected'))
            row['tab'].info.setStyleSheet(f'color: {COLORS["error"]}; font-weight: bold;' if not on or on.get('stale')
                                          else 'color: palette(placeholder-text);')
            row['tab'].setToolTip(tr('{} · via {}').format(on['version'], via_text(on)) if on else '')
            for slot in range(len(row['tab'].lamps)):
                smp = self.live.get((dev, slot))
                if smp is None:
                    row['tab'].set_lamp(slot, COLORS['empty'], tr('Slot {}: no data').format(slot + 1))
                else:
                    row['tab'].set_lamp(slot, sample_color(smp), tr('Slot {}: {}').format(slot + 1, mode_name(smp)),
                                        smp['mode'] in (2, 20))

    def _blink(self):
        self.blink_dark = not self.blink_dark
        for row in self.rows.values():
            row['tab'].blink(self.blink_dark)
            for t in row['tiles']:
                t.blink(self.blink_dark)

    def _update_offline(self):
        """Offline notice on the tiles of chargers connected in this run that are gone or no longer answer; it goes
        away by itself when readings come in again. Called on every cycle and every few seconds."""
        now = time.time()
        for dev, row in self.rows.items():
            on = self.online.get(dev)
            until = self.bt_holds.get(dev)
            if until is not None:                           # paused / switched off for the SkyRC app
                if until == float('inf'):
                    title, text, button = tr('Bluetooth deactivated'), '', tr('Switch on')
                else:
                    secs = max(0, int(until - now))
                    title, text = tr('Bluetooth paused'), tr('Connects again in {}.').format(
                        f'{secs // 60}:{secs % 60:02d} min') + '\n'
                    button = tr('Resume now')
                text += tr('The charger is free for the SkyRC app; nothing is recorded.')
                row['page'].set_offline(text, title, COLORS['idle'], (button, lambda dev=dev: self.bt_resume(dev)))
                continue
            if on and not on.get('stale'):
                row['page'].set_offline(None)
                continue
            if on:
                since = on['stale']
                text = tr('No answer since {} ({}).').format(hms(since), fmt_dur(now - since)) + '\n' + \
                    tr('The charger may still be charging, but nothing is recorded. BattBench keeps trying to '
                       'reach it.')
                if on.get('via') == 'usb':
                    text += ' ' + tr('If this does not end, unplug the USB cable and plug it in again.')
            else:
                since = max((v['t'] for k, v in self.live.items() if k[0] == dev), default=None)
                text = (tr('Disconnected since {} ({}).').format(hms(since), fmt_dur(now - since)) if since else
                        tr('Disconnected.')) + '\n' + \
                    tr('Running tasks go on in the charger but are not recorded. BattBench reconnects as soon as '
                       'it is back.')
            row['page'].set_offline(text)

    def _update_esp(self):
        """Status bar: the BattBench-ESP bridges BattBench talks to (address, tooltip: state)."""
        nets = sorted(self.worker.nets.items())
        text = 'BattBench-ESP ' + ', '.join(host for host, _n in nets) if nets else ''
        if text != self.st_esp.text():
            self.st_esp.setText(text)
        self.st_esp.setToolTip('<br>'.join(f'{host}: {n.state}' for host, n in nets))
        self.st_esp_icon.setToolTip(self.st_esp.toolTip())
        for w in (self.st_esp_icon, self.st_esp):
            w.setVisible(bool(nets))

    # ---------------------------------------------------------------- worker events
    def on_cycle(self, d):
        for dev in d.get('merged', ()):
            self._remove_row(dev)
        self.online = d['devices']
        self.bt_holds = d.get('bt_holds', {})
        self.live.update(d['live'])
        self.cur = d['sessions']
        if set(self.online) - set(self.devs):
            self.devs = {x['id']: x for x in self.db.devices()}
        shown = set(self.online) | {k[0] for k, v in self.live.items() if v['t'] >= self.t_start}
        for dev in sorted(shown):
            self._ensure_row(dev)
        self.dtab.set_online(self.online)
        self._update_esp()
        self._update_heads()
        self._update_offline()
        for dev, row in self.rows.items():
            approx = self.devs.get(dev, {}).get('model') == 'A4Air' and self.online.get(dev, {}).get('via') != 'ble'
            status_bar = self.devs.get(dev, {}).get('model') in ('MC5000', 'MC3000')
            for slot, t in enumerate(row['tiles']):
                t.approx_res = approx                  # A4 Air over USB: estimated; over Bluetooth: measured
                t.status_bar = status_bar              # no progress %: state and voltage in the bar
                smp = self.live.get((dev, slot))
                t.update_sample(smp)
                s = self.cur.get((dev, slot)) if smp and smp['mode'] else None     # battery in the slot
                h = health_of(s) if s and s['grade'] in USES else None
                t.set_battery(self.db.battery_of(s['id']) if s and s['id'] else None,
                              grade_cell(s['grade'], h['ohi'])[0] if h else '')
        self._update_panels()
        self._fit_tiles()
        self._pick_start_slot()
        if self.hist_id is None:
            s = self.cur.get(self.sel)
            self.result.show_session(s, self.db.battery_of(s['id']) if s and s['id'] else None)
            if time.time() - self.last_plot > 5:     # curves are re-read from the DB, so not every second
                self.refresh_plot(keep_view=True)

    PICK_SECS = 20                     # s after the start in which the first readings may choose the slot

    def _pick_start_slot(self):
        """Right after the start: select the first slot that is charging, else the first one that is done (chargers
        in tab order). Only once, and not after the user chose something."""
        if not self.auto_pick:
            return
        if time.time() - self.t_start > self.PICK_SECS or self.hist_id is not None:
            self.auto_pick = False
            return
        keys = [(dev, slot) for dev, row in self.rows.items() for slot in range(len(row['tiles']))]
        fresh = {k: v for k, v in ((k, self.live.get(k)) for k in keys) if v and v['t'] >= self.t_start - 60}
        charging = [k for k, v in fresh.items() if v['mode'] in TASKS and v['ma'] >= PHASE_MIN_MA]
        done = [k for k, v in fresh.items() if v['mode'] in DONE_MODES]
        key = (charging or done or [None])[0]
        if key is None:
            return
        self.auto_pick = False
        self._show(key[0])
        self.ctrl_sel[key[0]] = {key[1]}
        self.select_slot(key)

    def busy_batteries(self):
        """Ids of the batteries assigned to the sessions of occupied slots (they are in a charger right now). Only for
        the live view of a slot: a past session may be given any battery."""
        out = set()
        if self.hist_id is not None:
            return out
        for key, s in self.cur.items():
            if s and s['id'] and self.live.get(key, {}).get('mode'):
                bid = self.db.battery_of(s['id'])
                if bid is not None:
                    out.add(bid)
        return out

    # ---------------------------------------------------------------- selection
    def _mark(self, key):
        if key:
            self.ctrl_sel.setdefault(key[0], {key[1]})
        for dev, row in self.rows.items():
            for t in row['tiles']:          # blue frame: the slot of the chart; indigo title: chosen for control
                t.set_selected(t.key == key)
                t.set_chosen(row['panel'] is not None and t.key[1] in self.ctrl_sel.get(dev, ()))
        self._update_panels()

    def _update_panels(self):
        for dev, row in self.rows.items():
            if row['panel'] is not None:
                row['panel'].refresh(sorted(self.ctrl_sel.get(dev, ())))

    SUMMARY_SECS = 120                 # s: ask a chosen slot for its summary again after this long

    def _summary_key(self, dev, slot):
        """What the 5F answer depends on: battery type, task and status in the newest reading."""
        smp = self.live.get((dev, slot))
        raw = bytes.fromhex(smp['raw']) if smp and smp.get('raw') else b''
        return (mc5000.from_status(raw), raw[18] if len(raw) > 18 else None)

    def slot_summary(self, dev, slot):
        """The charger's summary of a slot (dict status, mode, charge_ma, discharge_ma) if it is still current."""
        e = self.summaries.get((dev, slot))
        return e['data'] if e and e['key'] == self._summary_key(dev, slot) else None

    def want_summary(self, dev, slot):
        """Ask the MC5000 for the summary of a chosen slot unless a current one is known or a query runs."""
        e = self.summaries.get((dev, slot))
        if (dev, slot) in self.asking or (e and e['key'] == self._summary_key(dev, slot) and
                                          time.time() - e['t'] < self.SUMMARY_SECS):
            return
        self.asking.add((dev, slot))
        self.worker.cmds.put(('mc5000_ask', dev, 'summary', slot))

    def charger_info(self, d):
        if d['kind'] == 'summary':
            key = (d['dev'], d['slot'])
            self.asking.discard(key)
            # no answer: data None, asked again after SUMMARY_SECS
            self.summaries[key] = dict(t=time.time(), key=self._summary_key(*key), data=d['data'])
            self._update_panels()
            return
        dlg = self.settings_dlg
        if d['kind'] == 'bt':
            if dlg is not None and dlg.dev == d['dev']:
                dlg.bt_answer(d)
            return
        if dlg is not None and dlg.dev == d['dev']:
            dlg.answer(d)
        elif d.get('error'):
            QMessageBox.warning(self, tr('Charger settings'), d['error'])
        if d.get('text'):
            self.statusBar().showMessage(d['text'], 10000)

    def export_shown(self):
        """Report of the session in the result panel (finished or running)."""
        d = self.result.cur
        if d:
            self._export(dict(d), self.db.battery_of(d['id']) if d.get('id') is not None else None)

    def export_session(self, sid):
        d = self.db.session_dict(sid)
        if not d:
            return
        live = self.cur.get((d['dev'], d['slot']))
        if live and live['id'] == sid:              # still running: its live state
            d = dict(live)
        else:
            d['nominal'] = d['nominal'] or NOMINAL.get(d['size'], 0)
            d['phases'] = self.db.phases(sid)
        self._export(d, self.db.battery_of(sid))

    def _export(self, d, battery_id):
        """Ask language / phases / PDF and the file, then write the report (see report.py)."""
        dlg = ReportDialog(self, len(d.get('phases') or []) > 1)
        if dlg.exec() != QDialog.Accepted:
            return
        settings = QSettings('battbench', 'battbench')
        b = self.db.battery(battery_id, deleted=True) if battery_id is not None else None
        name = (f"{b['id']}_{b['name']}" if b else f"{self.dev_name(d.get('dev'))}_{d['slot'] + 1}")
        name = re.sub(r'[^\w.-]+', '_', f"BattBench_{name}_{datetime.fromtimestamp(d['start']):%Y-%m-%d_%H%M}")
        folder = settings.value('report_dir', '') or QStandardPaths.writableLocation(
            QStandardPaths.DocumentsLocation)
        pdf = dlg.format.currentData() == 'pdf'
        ext = '.pdf' if pdf else '.html'
        path, _f = QFileDialog.getSaveFileName(self, tr('Export report'), os.path.join(folder, name + ext),
                                               tr('PDF (*.pdf)') if pdf else tr('HTML page (*.html)'))
        if not path:
            return
        if not path.lower().endswith(('.pdf',) if pdf else ('.html', '.htm')):
            path += ext
        settings.setValue('report_dir', os.path.dirname(path))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rep = report.build(self.db, d, battery_id, self.dev_name, dlg.lang(), dlg.phases.isChecked(),
                               self._esp_of(d.get('dev')))
            (report.write_pdf if pdf else report.write_html)(path, rep)
        except OSError as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, tr('Export report'), tr('Could not write the report: {}').format(e))
            return
        QApplication.restoreOverrideCursor()
        log.info('report written: %s', path)
        if QMessageBox.question(self, tr('Export report'), tr('Report saved: {}\n\nOpen it now?').format(
                os.path.basename(path))) == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _esp_of(self, dev):
        """For the report: firmware and address of the BattBench-ESP the charger is read through ('' = not known
        right now), None = not through one."""
        d = self.devs.get(dev) or {}
        on = self.online.get(dev) or {}
        if 'net' not in on.get('vias', ()) and 'BattBench-ESP' not in (d.get('version') or ''):
            return None
        addr = self.worker._bt_addr(d) if d else None
        for host, n in sorted(self.worker.nets.items()):
            info = n.info or {}
            if addr and any(c.get('addr', '').upper() == addr for c in info.get('chargers', [])):
                return ' · '.join(x for x in (info.get('fw', ''), host) if x)
        return ''

    def bt_resume(self, dev):
        """Button on the notice of a paused / switched off Bluetooth."""
        self.worker.cmds.put(('bt', dev, 'on'))
        self.statusBar().showMessage(tr('Switching Bluetooth on again …'), 5000)

    def set_skyrc(self, mode, tell=True):
        """How SkyRC chargers are connected (SKYRC_MODES); tell=False: the worker switched by itself."""
        self.skyrc = mode
        QSettings('battbench', 'battbench').setValue('skyrc', mode)
        if tell:
            self.worker.cmds.put(('skyrc', mode))
        else:
            self.statusBar().showMessage(tr('BattBench-ESP connected: SkyRC chargers are only connected through it '
                                            'from now on (see "+").'), 15000)

    def charger_settings(self, dev):
        """Read the MC5000 system settings, let the user change them, write them back."""
        dlg = self.settings_dlg = ChargerSettingsDialog(self, dev)
        self.worker.cmds.put(('mc5000_ask', dev, 'settings', None))
        try:
            if dlg.exec() == QDialog.Accepted and dlg.changed():
                self.worker.cmds.put(('mc5000_ask', dev, 'settings', dlg.values()))
                self.statusBar().showMessage(tr('Sending to the charger …'), 5000)
        finally:
            self.settings_dlg = None

    def slot_program(self, dev, slot):
        """What BattBench knows about the program of a slot: dict(prog, pid, t) of the last start from here, or
        None."""
        raw = self.db.setting(f'mc5000:{dev}:{slot}')
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def start_slots(self, dev, slots, pid):
        """Start profile pid on these slots after asking; pid 'current': each slot with its current settings."""
        if pid == 'current':
            return self._start_current(dev, slots)
        prof = next(((n, d) for i, n, d in self.db.profiles() if i == pid), None)
        if prof is None or not slots:
            return
        p = mc5000.Program.from_dict(prof[1])
        if p.problems():
            QMessageBox.warning(self, tr('Profiles'), '; '.join(tr_data(x) for x in p.problems()))
            return
        busy = [s + 1 for s in slots if self.live.get((dev, s), {}).get('mode') in TASKS]
        text = tr('Start profile "{}" on slot {}?').format(prof[0], ', '.join(str(s + 1) for s in slots)) + \
            '\n\n' + profile_text(p)
        if busy:
            text += '\n\n' + tr('Slot {} is working: its task is replaced and starts again from zero.').format(
                ', '.join(map(str, busy)))
        if QMessageBox.question(self, tr('Start'), text) != QMessageBox.Yes:
            return
        self.db.set_setting('last_profile', pid)
        self.worker.cmds.put(('mc5000', dev, list(slots), p.to_dict(), pid))
        self.statusBar().showMessage(tr('Sending to the charger …'), 5000)

    def _start_current(self, dev, slots):
        progs = {s: self.current_program(dev, s) for s in slots}
        missing = [s + 1 for s, (p, _c) in progs.items() if p is None]
        if missing or not slots:
            QMessageBox.information(self, tr('Start'), tr('No data from slot {} yet.').format(
                ', '.join(map(str, missing))))
            return
        bad = ['; '.join(tr_data(x) for x in p.problems()) for p, _c in progs.values() if p.problems()]
        if bad:
            QMessageBox.warning(self, tr('Profiles'), bad[0])
            return
        text = tr('Start slot {} again with its current settings?').format(', '.join(str(s + 1) for s in slots))
        for s, (p, complete) in progs.items():
            text += '\n\n' + tr('Slot {}').format(s + 1) + ': ' + profile_text(p)
            if not complete:
                text += '\n' + self._partly_known()
        busy = [s + 1 for s in slots if self.live.get((dev, s), {}).get('mode') in TASKS]
        if busy:
            text += '\n\n' + tr('Slot {} is working: its task is replaced and starts again from zero.').format(
                ', '.join(map(str, busy)))
        if QMessageBox.question(self, tr('Start'), text) != QMessageBox.Yes:
            return
        for s, (p, _c) in progs.items():
            self.worker.cmds.put(('mc5000', dev, [s], p.to_dict(), None))
        self.statusBar().showMessage(tr('Sending to the charger …'), 5000)

    def stop_slots(self, dev, slots):
        names = ', '.join(str(s + 1) for s in slots)
        if slots and QMessageBox.question(self, tr('Stop'), tr('Stop slot {}? The task there ends.').format(names)) \
                == QMessageBox.Yes:
            self.worker.cmds.put(('mc5000', dev, list(slots), None))
            self.statusBar().showMessage(tr('Sending to the charger …'), 5000)

    def known_program(self, dev, slot):
        """The program BattBench started on a slot (mc5000.Program) while the charger still shows it, else None."""
        info = self.slot_program(dev, slot)
        p = mc5000.Program.from_dict(info['prog']) if info else None
        smp = self.live.get((dev, slot))
        st = mc5000.from_status(bytes.fromhex(smp['raw'])) if smp and smp.get('raw') else None
        summ = self.slot_summary(dev, slot)
        if p and st and (p.chem, p.mode) != st or p and summ and not mc5000.same_currents(p, summ):
            return None                               # changed on the charger since
        return p

    def current_program(self, dev, slot):
        """(program, complete) of the current settings of a slot: changed ones kept for the next start, the one
        BattBench started, or battery type, task and currents from the charger with defaults for the rest
        (complete False). (None, False) without data."""
        if (dev, slot) in self.temp_progs:
            return self.temp_progs[(dev, slot)], True
        p = self.known_program(dev, slot)
        if p:
            return p, True
        smp = self.live.get((dev, slot))
        st = mc5000.from_status(bytes.fromhex(smp['raw'])) if smp and smp.get('raw') else None
        if st is None or st[1] is None:
            return None, False
        p = mc5000.defaults(*st)
        summ = self.slot_summary(dev, slot)
        if summ:
            p.charge_ma, p.discharge_ma = summ['charge_ma'] or p.charge_ma, summ['discharge_ma'] or p.discharge_ma
        p.name = ''
        return p, False

    def _partly_known(self):
        return tr('Only battery type, task and the currents come from the charger; the other values are defaults – '
                  'please check them.')

    def new_profile(self, dev):
        """New profile with the SkyRC app's defaults, as New in the tab Profiles."""
        dlg = ProfileDialog(self, 'new', mc5000.defaults('NiMH'), '')
        if dlg.exec() == QDialog.Accepted:
            self._store_profile(dev, None, dlg.program())

    def new_profile_from_slot(self, dev, slots):
        """New profile with the current settings of the (first) chosen slot as template."""
        p, complete = self.current_program(dev, slots[0]) if slots else (None, True)
        if p is None:
            p, complete = mc5000.defaults('NiMH'), True
        dlg = ProfileDialog(self, 'new', p, '', '' if complete else self._partly_known())
        if dlg.exec() == QDialog.Accepted:
            self._store_profile(dev, None, dlg.program())

    def edit_profile(self, dev, slots, sel):
        """Edit the profile chosen in the control panel; 'current': the current settings of the chosen slots, used
        at their next start (or saved as a new profile)."""
        if sel == 'current':
            if not slots:
                return
            p, complete = self.current_program(dev, slots[0])
            if p is None:
                QMessageBox.information(self, tr('Current settings'), tr('No data from this slot yet.'))
                return
            note = tr('Changes are used at the next start of slot {} and not saved as a profile.').format(
                ', '.join(str(s + 1) for s in slots))
            if not complete:
                note += ' ' + self._partly_known()
            dlg = ProfileDialog(self, 'current', p, '', note)
            if dlg.exec() != QDialog.Accepted:
                return
            if dlg.action() == 'use':
                q = dlg.program()
                q.name = ''
                for s in slots:
                    self.temp_progs[(dev, s)] = q
                self._update_panels()
            else:
                self._store_profile(dev, None, dlg.program())
            return
        row = next(((n, d) for i, n, d in self.db.profiles() if i == sel), None)
        if row is None:
            return
        dlg = ProfileDialog(self, 'edit', mc5000.Program.from_dict(row[1]), row[0])
        if dlg.exec() == QDialog.Accepted:
            self._store_profile(dev, sel if dlg.action() == 'save' else None, dlg.program())

    def _store_profile(self, dev, pid, p):
        """Save (pid None: as a new profile) and choose it in the control panel."""
        if pid is None and any(n == p.name for _i, n, _d in self.db.profiles()):
            if QMessageBox.question(self, tr('Profiles'), tr('A profile "{}" exists already. Save another one with '
                                                             'the same name?').format(p.name)) != QMessageBox.Yes:
                return
        pid = self.db.save_profile(pid, p.name, p.to_dict())
        self.ptab.load(pid)
        panel = (self.rows.get(dev) or {}).get('panel')
        if panel is not None:
            panel.select_profile(pid)
        self._update_panels()

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
            self.plot.show_curve = True
            title = ' · '.join((self.dev_name(dev), tr('Slot {}').format(slot + 1), fmt_t(t0), tr_data(d['task'])))
        else:
            s = self.cur.get(self.sel)
            if not s:
                return self.plot.clear_data(' · '.join((self.dev_name(self.sel[0]), tr('Slot {}').format(self.sel[1] + 1)))
                                            + ': ' + tr('no running session') if self.sel else '')
            dev, slot, t0, t1, phases = s['dev'], s['slot'], s['start'], s['end'], s['phases']
            self.plot.show_curve = not is_measuring(s)          # a running discharge is not over yet
            title = ' · '.join((self.dev_name(dev), tr('Slot {}').format(slot + 1), fmt_t(t0), tr_data(s['task']))) + ' (live)'
        self.plot.set_data(self.db.samples(dev, slot, t0, t1 + 1), phases, title, keep_view,
                           self.db.history(dev, slot, t0, t1 + 1))

    def _row_changed(self, row, _col, prev_row, _prev_col):
        if row >= 0 and row != prev_row:
            self.open_row(row, _col)

    def _row_clicked(self, row, _col):
        if self.table.item(row, 0).data(Qt.UserRole) != self.row_sid:   # not already opened by the selection
            self.open_row(row, _col)

    def open_row(self, row, _col):
        self.row_sid = self.table.item(row, 0).data(Qt.UserRole)
        self.open_session(self.row_sid)

    def _row_double_clicked(self, row, col):
        """Double click on the battery of a session: tab Batteries with that battery and the session."""
        bid = self.table.item(row, col).data(SORT_ROLE) if self.COLS[col] == 'battery' else None
        if bid is not None:
            self.tabs.setCurrentWidget(self.btab)
            self.btab.show_session(bid, self.table.item(row, 0).data(Qt.UserRole))

    def show_model_batteries(self, mid):
        self.tabs.setCurrentWidget(self.btab)
        self.btab.filter_model(mid)

    def _tile_menu(self, key, pos):
        """Right click on a slot: MC5000 programs (start a profile, stop)."""
        dev, slot = key
        if (self.devs.get(dev) or {}).get('model') != 'MC5000':
            return
        on = self.online.get(dev) or {}
        ready = bool(set(on.get('vias', ())) & {'ble', 'net'}) and not on.get('stale')
        m = QMenu(self)
        a_start = m.addAction(tr('Start a profile …'))
        a_stop = m.addAction(tr('Stop slot {}').format(slot + 1))
        for a in (a_start, a_stop):
            a.setEnabled(ready)
        if not ready:
            m.addSeparator()
            m.addAction(tr('Charger not connected over Bluetooth / BattBench-ESP')).setEnabled(False)
        a = m.exec(pos)
        if a is a_start:
            self.start_profile_dialog(dev, slot)
        elif a is a_stop:
            self.stop_slots(dev, [slot])

    def start_profile_dialog(self, dev, slot):
        profiles = self.db.profiles()
        if not profiles:
            QMessageBox.information(self, tr('Profiles'), tr('No profile yet: create one in the tab Profiles.'))
            self.tabs.setCurrentWidget(self.ptab)
            return
        slots = self.devs[dev]['slots'] or 4
        states = [mode_name(self.live[(dev, s)]) if self.live.get((dev, s)) else tr('no data') for s in range(slots)]
        running = [bool(self.live.get((dev, s), {}).get('mode') in TASKS) for s in range(slots)]
        dlg = StartDialog(self, profiles, slot, states, running, self.db.setting('last_profile'))
        if dlg.exec() != QDialog.Accepted:
            return
        pid, prog, chosen = dlg.result_values()
        self.db.set_setting('last_profile', pid)
        self.worker.cmds.put(('mc5000', dev, chosen, prog.to_dict(), pid))
        self.statusBar().showMessage(tr('Sending to the charger …'), 5000)

    def command_result(self, ok, text, started):
        self.statusBar().showMessage(text, 15000)
        for k in [k for k in self.summaries if started and k[0] == started['dev']]:
            self.summaries.pop(k)
        for sl in (started or {}).get('slots', ()):
            self.temp_progs.pop((started['dev'], sl), None)
        if started:                                  # remember the program of these slots
            for s in started['slots']:
                self.db.set_setting(f"mc5000:{started['dev']}:{s}",
                                    json.dumps(dict(prog=started['prog'], pid=started['pid'], t=time.time())))
            self._update_panels()
        if not ok:
            QMessageBox.warning(self, tr('Charger'), text)

    def _tile_clicked(self, key):
        self.auto_pick = False
        dev, slot = key
        chosen = self.ctrl_sel.setdefault(dev, set())
        if QApplication.keyboardModifiers() & Qt.ControlModifier and chosen:
            chosen ^= {slot}                         # Ctrl: more slots for the control panel
            if not chosen:
                chosen.add(slot)
            self.follow_ctrl(dev)
            self._update_panels()
            return
        self.ctrl_sel[dev] = {slot}
        self.row_sid = None
        self.select_slot(key)
        s = self.cur.get(key) if self.live.get(key, {}).get('mode') else None
        bid = self.db.battery_of(s['id']) if s and s['id'] else None
        if bid is not None:                          # battery in the slot: show its history
            self.tabs.setCurrentWidget(self.btab)
            self.btab.show_battery(bid)

    def follow_ctrl(self, dev):
        """Slots chosen in the charger control changed: with just one left that is not the slot shown (chart, details),
        show that one; else only mark the choice."""
        chosen = self.ctrl_sel.get(dev, set())
        if len(chosen) == 1 and self.sel != (dev, next(iter(chosen))):
            self.select_slot((dev, next(iter(chosen))))
        else:
            self._mark(self.sel)

    def open_session(self, sid):
        self.auto_pick = False
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
        if self.result.model.currentData() is not None:     # the model chosen in the panel
            prefill['model_id'] = self.result.model.currentData()
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

    LAYOUT = ('window', 'split_v', 'split_h')

    def save_layout(self):
        """Window position / size and the splitter positions, stored in the database."""
        g = self.normalGeometry() if self.isMaximized() else self.geometry()
        self.db.set_setting('window', f'{g.x()},{g.y()},{g.width()},{g.height()},{int(self.isMaximized())}')
        self.db.set_setting('split_v', ','.join(map(str, self.vsplit.sizes())))
        self.db.set_setting('split_h', ','.join(map(str, self.hsplit.sizes())))
        for key, t in self.width_tables().items():
            if t.user_widths:
                self.db.set_setting('cols_' + key, ','.join(str(t.columnWidth(c)) for c in range(t.columnCount())))
        if self.btab.shown_once:
            self.db.set_setting('split_bat', ','.join(map(str, self.btab.split.sizes())))

    def restore_layout(self):
        """Back to the stored layout; a window that is not completely on one screen (monitor gone, moved off the
        edge) gets the default size, centred on the primary screen."""
        try:
            x, y, w, h, maximized = (int(v) for v in (self.db.setting('window') or '').split(','))
            rect = QRect(x, y, w, h)
        except ValueError:
            rect, maximized = None, 0
        if rect is not None and any(s.availableGeometry().contains(rect) for s in QGuiApplication.screens()):
            self.setGeometry(rect)
            if maximized:
                self.setWindowState(Qt.WindowMaximized)
        else:                                  # first start, or the window would not be completely visible
            area = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(1500, area.width() - 40), min(950, area.height() - 60))
            self.move(area.center() - self.rect().center())
        for split, key in ((self.vsplit, 'split_v'), (self.hsplit, 'split_h'),
                           (self.btab.split, 'split_bat')):
            try:
                sizes = [int(x) for x in (self.db.setting(key) or '').split(',')]
            except ValueError:
                continue
            if len(sizes) == split.count() and all(x >= 0 for x in sizes) and sum(sizes) > 0:
                split.setSizes(sizes)
        self.tiles_user = bool(self.db.setting('tiles_user'))
        self._fit_tiles()
        for key, t in self.width_tables().items():
            try:
                widths = [int(x) for x in (self.db.setting('cols_' + key) or '').split(',')]
            except ValueError:
                continue
            if len(widths) == t.columnCount() and all(x > 0 for x in widths):
                t.user_widths = True
                for c, x in enumerate(widths):
                    t.setColumnWidth(c, x)

    def width_tables(self):
        """Tables whose column widths the user may set (stored as setting 'cols_<key>')."""
        return {'sessions': self.table, 'batteries': self.btab.table, 'history': self.btab.hist,
                'models': self.mtab.table, 'chargers': self.dtab.table}

    def closeEvent(self, e):
        if self.result.sid is not None and self.result.label.text().strip() != self.result.shown_label:
            try:                               # a note still being typed (the worker is stopped below)
                self.db.set_session_meta(self.result.sid, label=self.result.label.text().strip())
            except sqlite3.Error as err:
                print(f'saving the note failed: {err!r}', file=sys.stderr)
        try:
            self.save_layout()
        except sqlite3.Error as err:
            print(f'saving the layout failed: {err!r}', file=sys.stderr)
        self.worker.stop()
        self.thread.quit()
        self.thread.wait(5000)
        self.hide()                            # the backup takes a few seconds for a large database
        QApplication.processEvents()
        self.db.con.close()
        try:
            close_and_backup(self.db.path, backup_policy())
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
    ap.add_argument('--debug', action='store_true', help='detailed log (every unanswered query), also to stderr')
    ap.add_argument('--console', action='store_true', help='run.bat: keep the console window (no effect here)')
    a = ap.parse_args()
    logs.setup(a.db, a.debug)
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
    esp = [h for h in (settings.value('esp_hosts', '') or '').split() if h]
    skyrc = settings.value('skyrc', 'auto')
    w = MainWindow(a.db, source, a4=a4 if a4 in A4_MODES else 'both', bt=not a.no_bt, esp=esp,
                   esp_auto=settings.value('esp_auto', True, type=bool),
                   skyrc=skyrc if skyrc in SKYRC_MODES else 'auto', skyrc_chosen=settings.contains('skyrc'))
    w.show()
    code = app.exec()
    log.info('BattBench closed')
    sys.exit(code)


if __name__ == '__main__':
    main()
