"""SQLite storage: chargers, every sample, every session (charging task) with phases and result,
batteries and models."""
import os
import sqlite3
import time
from typing import List

from .device import N8_MODES, Sample
from .model import ABORTED, GAP_SECS, MIN_SECS, RUNNING, Phase, Session, Tracker

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS samples (
  t REAL, slot INTEGER, mode INTEGER, mode_str TEXT, chem TEXT, size TEXT,
  mv INTEGER, ma INTEGER, res INTEGER, mah INTEGER, secs INTEGER,
  temp INTEGER, itemp INTEGER, progress INTEGER, power INTEGER, energy INTEGER, raw TEXT,
  dev INTEGER DEFAULT 1, PRIMARY KEY (dev, slot, t));
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, slot INTEGER, start REAL, end REAL, task TEXT, chem TEXT, size TEXT,
  status TEXT, nominal INTEGER, label TEXT DEFAULT '',
  discharge_mah INTEGER, charge_mah INTEGER, res_first INTEGER, res_min INTEGER, res_last INTEGER,
  temp_max INTEGER, grade TEXT, note TEXT, battery_id INTEGER, dev INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, model TEXT, name TEXT, slots INTEGER, version TEXT DEFAULT '',
  last_seen REAL);
CREATE TABLE IF NOT EXISTS phases (
  session_id INTEGER, idx INTEGER, kind TEXT, start REAL, end REAL, mah INTEGER,
  mv_start INTEGER, mv_end INTEGER, PRIMARY KEY (session_id, idx));
CREATE TABLE IF NOT EXISTS batteries (
  id INTEGER PRIMARY KEY, name TEXT, maker TEXT DEFAULT '', capacity INTEGER DEFAULT 0, type TEXT DEFAULT '',
  description TEXT DEFAULT '', created REAL, model_id INTEGER);
CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY, maker TEXT, name TEXT, type TEXT DEFAULT '', capacity INTEGER DEFAULT 0,
  note TEXT DEFAULT '', UNIQUE (maker, name));
CREATE TABLE IF NOT EXISTS imported (file TEXT PRIMARY KEY, at REAL, size INTEGER, rows INTEGER);
"""
SESSION_FIELDS = ['id', 'slot', 'start', 'end', 'task', 'chem', 'size', 'status', 'nominal', 'label',
                  'discharge_mah', 'charge_mah', 'res_min', 'temp_max', 'grade', 'note', 'battery_id',
                  'battery_name', 'dev', 'dev_name', 'battery_deleted', 'deleted']
SESSION_SELECT = ('SELECT s.id, s.slot, s.start, s.end, s.task, s.chem, s.size, s.status, s.nominal, s.label, '
                  's.discharge_mah, s.charge_mah, s.res_min, s.temp_max, s.grade, s.note, s.battery_id, b.name, '
                  's.dev, d.name, b.deleted IS NOT NULL, s.deleted '
                  'FROM sessions s LEFT JOIN batteries b ON b.id = s.battery_id LEFT JOIN devices d ON d.id = s.dev')
DEVICE_FIELDS = ['id', 'key', 'model', 'name', 'slots', 'version', 'last_seen', 'alt_key']
LEGACY_N8 = 'NXHOSTP-legacy'      # data recorded before chargers were told apart
BATTERY_FIELDS = ['id', 'name', 'maker', 'capacity', 'type', 'description', 'model_id']
MODEL_FIELDS = ['id', 'maker', 'name', 'type', 'capacity', 'note']
LSD_AA, LSD_AAA = 'NiMH LSD AA (eneloop type)', 'NiMH LSD AAA (eneloop type)'
# Starting list, written once into an empty `models` table (editable in the app, tab "Modelle").
# Capacity = rated capacity of the cell (where typ. and min. are printed: the minimum value).
DEFAULT_MODELS = [
    ('Panasonic', 'eneloop AA', LSD_AA, 1900), ('Panasonic', 'eneloop AAA', LSD_AAA, 750),
    ('Panasonic', 'eneloop pro AA', LSD_AA, 2500), ('Panasonic', 'eneloop pro AAA', LSD_AAA, 930),
    ('Panasonic', 'eneloop lite AA', LSD_AA, 950), ('Panasonic', 'eneloop lite AAA', LSD_AAA, 550),
    ('IKEA', 'LADDA AA 2450', LSD_AA, 2450), ('IKEA', 'LADDA AA 1900', LSD_AA, 1900),
    ('IKEA', 'LADDA AAA 900', LSD_AAA, 900), ('IKEA', 'LADDA AAA 750', LSD_AAA, 750),
    ('Fujitsu', 'HR-3UTC AA (white)', LSD_AA, 1900), ('Fujitsu', 'HR-3UTHC AA (black)', LSD_AA, 2450),
    ('Fujitsu', 'HR-4UTC AAA (white)', LSD_AAA, 750), ('Fujitsu', 'HR-4UTHC AAA (black)', LSD_AAA, 900),
    ('Ansmann', 'maxE plus AA 2100', LSD_AA, 2000), ('Ansmann', 'maxE plus AAA 800', LSD_AAA, 750),
    ('Ansmann', 'AA 2850', 'NiMH AA', 2650), ('Ansmann', 'AA 2500', 'NiMH AA', 2400),
    ('Ansmann', 'AAA 1100', 'NiMH AAA', 1050),
    ('Varta', 'Recharge Accu Power AA 2100', LSD_AA, 2100), ('Varta', 'Recharge Accu Power AA 2600', LSD_AA, 2600),
    ('Varta', 'Recharge Accu Power AAA 800', LSD_AAA, 800),
    ('Varta', 'Recharge Accu Power AAA 1000', LSD_AAA, 1000),
    ('GP', 'ReCyko AA 2100', LSD_AA, 2000), ('GP', 'ReCyko AA 2600', LSD_AA, 2500),
    ('GP', 'ReCyko AAA 850', LSD_AAA, 800), ('GP', 'ReCyko AAA 950', LSD_AAA, 900),
    ('Duracell', 'Recharge Ultra AA 2500', LSD_AA, 2400), ('Duracell', 'Recharge Ultra AAA 900', LSD_AAA, 850),
    ('Duracell', 'Recharge Plus AA 1300', LSD_AA, 1300), ('Duracell', 'Recharge Plus AAA 750', LSD_AAA, 700),
    ('Amazon Basics', 'AA 2000', LSD_AA, 1900), ('Amazon Basics', 'AA 2400 (High Capacity)', LSD_AA, 2400),
    ('Amazon Basics', 'AAA 800', LSD_AAA, 750), ('Amazon Basics', 'AAA 850 (High Capacity)', LSD_AAA, 850),
    ('EBL', 'AA 2800', 'NiMH AA', 2800), ('EBL', 'AAA 1100', 'NiMH AAA', 1100),
    ('Samsung', 'INR18650-25R', 'Li-Ion 18650', 2500), ('Samsung', 'INR18650-30Q', 'Li-Ion 18650', 3000),
    ('Samsung', 'INR18650-35E', 'Li-Ion 18650', 3500), ('Samsung', 'INR21700-40T', 'Li-Ion 21700', 4000),
    ('Samsung', 'INR21700-50E', 'Li-Ion 21700', 5000), ('LG', 'INR18650-MJ1', 'Li-Ion 18650', 3500),
    ('Sony/Murata', 'US18650VTC6', 'Li-Ion 18650', 3000), ('Molicel', 'INR18650-P28A', 'Li-Ion 18650', 2800),
    ('Molicel', 'INR21700-P42A', 'Li-Ion 21700', 4200),
]
COLS = ('t', 'slot', 'mode', 'mode_str', 'chem', 'size', 'mv', 'ma', 'res', 'mah', 'secs',
        'temp', 'itemp', 'progress', 'power', 'energy', 'raw', 'dev')


class DB:
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.executescript(SCHEMA)
        cols = [r[1] for r in self.con.execute('PRAGMA table_info(sessions)')]
        if 'battery_id' not in cols:                          # older database
            self.con.execute('ALTER TABLE sessions ADD COLUMN battery_id INTEGER')
        if 'dev' not in cols:                                 # before multi-charger support: all N8
            self.con.execute('ALTER TABLE sessions ADD COLUMN dev INTEGER DEFAULT 1')
        if 'dev' not in [r[1] for r in self.con.execute('PRAGMA table_info(samples)')]:
            self.con.executescript('''
                ALTER TABLE samples RENAME TO samples_old;
                CREATE TABLE samples (
                  t REAL, slot INTEGER, mode INTEGER, mode_str TEXT, chem TEXT, size TEXT,
                  mv INTEGER, ma INTEGER, res INTEGER, mah INTEGER, secs INTEGER,
                  temp INTEGER, itemp INTEGER, progress INTEGER, power INTEGER, energy INTEGER, raw TEXT,
                  dev INTEGER DEFAULT 1, PRIMARY KEY (dev, slot, t));
                INSERT INTO samples SELECT *, 1 FROM samples_old;
                DROP TABLE samples_old;''')
        if 'alt_key' not in [r[1] for r in self.con.execute('PRAGMA table_info(devices)')]:
            self.con.execute('ALTER TABLE devices ADD COLUMN alt_key TEXT')
        if (self.con.execute('SELECT COUNT(*) FROM devices').fetchone()[0] == 0 and
                self.con.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]):
            self.con.execute("INSERT INTO devices VALUES (1, ?, 'NXHOSTP', 'N8', 8, '', NULL)", (LEGACY_N8,))
        if 'model_id' not in [r[1] for r in self.con.execute('PRAGMA table_info(batteries)')]:
            self.con.execute('ALTER TABLE batteries ADD COLUMN model_id INTEGER')
        if self.con.execute('PRAGMA user_version').fetchone()[0] < 1:
            self._n8_size_fix()
            self.con.execute('PRAGMA user_version = 1')
        if self.con.execute('PRAGMA user_version').fetchone()[0] < 2:
            self._n8_activation_fix()
            self.con.execute('PRAGMA user_version = 2')
        if self.con.execute('PRAGMA user_version').fetchone()[0] < 3:
            self._drop_short_sessions()
            self.con.execute('PRAGMA user_version = 3')
        if self.con.execute('PRAGMA user_version').fetchone()[0] < 4:
            self._english_values()
            self.con.execute('PRAGMA user_version = 4')
        if self.con.execute('PRAGMA user_version').fetchone()[0] < 5:
            self._soft_delete()
            self.con.execute('PRAGMA user_version = 5')
        if self.con.execute('PRAGMA user_version').fetchone()[0] < 6:
            self._soft_delete('sessions')
            self.con.execute('PRAGMA user_version = 6')
        if not self.con.execute('SELECT COUNT(*) FROM models').fetchone()[0]:
            self.con.executemany('INSERT INTO models (maker,name,type,capacity) VALUES (?,?,?,?)', DEFAULT_MODELS)
        self.con.commit()

    def _n8_size_fix(self):
        """The N8 does not tell AA from AAA; older data called every cell "AA" and rated it against
        2000 mAh. Rename to "AA/AAA"; sessions with that automatic 2000 mAh and no battery lose the
        nominal capacity and are rated anew (a battery or a typed nominal capacity is needed now)."""
        n8 = "(SELECT id FROM devices WHERE model='NXHOSTP')"
        self.con.execute(f"UPDATE samples SET size='AA/AAA' WHERE size='AA' AND dev IN {n8}")
        ids = [r[0] for r in self.con.execute(f"SELECT id FROM sessions WHERE size='AA' AND dev IN {n8}")]
        for sid in ids:
            self.con.execute("UPDATE sessions SET size='AA/AAA', nominal = CASE WHEN battery_id IS NULL "
                             "AND nominal = 2000 THEN 0 ELSE nominal END WHERE id=?", (sid,))
        for sid in ids:
            self.rerate(sid)

    def _n8_activation_fix(self):
        """The N8 reports activation as mode 9/10 (cycle on other chargers); older data was stored that way.
        (Task names were German then; _english_values translates them afterwards.)"""
        n8 = "(SELECT id FROM devices WHERE model IN ('NXHOSTP', 'NXHOST'))"
        for old, (new, text) in N8_MODES.items():
            self.con.execute(f"UPDATE samples SET mode=?, mode_str=? WHERE mode=? AND dev IN {n8}", (new, text, old))
        self.con.execute(f"UPDATE sessions SET task='Aktivierung' WHERE task='Zyklus' AND dev IN {n8}")

    def _drop_short_sessions(self):
        """Sessions shorter than MIN_SECS (an empty A4 Air slot probing showed up as a full cell) are no longer
        stored; remove the old ones unless the user gave them a battery or label. Their samples stay."""
        short = f"SELECT id FROM sessions WHERE end - start < {MIN_SECS} AND battery_id IS NULL AND COALESCE(label, '') = ''"
        self.con.execute(f'DELETE FROM phases WHERE session_id IN ({short})')
        self.con.execute(f'DELETE FROM sessions WHERE id IN ({short})')

    OLD_VALUES = {
        ('sessions', 'task'): {'Laden': 'charge', 'Entladen': 'discharge', 'Lagern': 'storage', 'Zyklus': 'cycle',
                               'Analyse': 'analysis', 'Aktivierung': 'activation'},
        ('sessions', 'status'): {'läuft': 'running', 'fertig': 'done', 'entnommen': 'removed',
                                 'abgebrochen': 'aborted'},
        ('sessions', 'grade'): {'sehr gut': 'very good', 'gut': 'good', 'mäßig': 'fair', 'verbraucht': 'worn out',
                                'verdächtig': 'suspicious'},
        ('phases', 'kind'): {'Laden': 'charge', 'Entladen': 'discharge'},
        ('models', 'type'): {'NiMH LSD AA (Eneloop o. ä.)': LSD_AA, 'NiMH LSD AAA (Eneloop o. ä.)': LSD_AAA,
                             'Sonstiges': 'Other'},
        ('batteries', 'type'): {'NiMH LSD AA (Eneloop o. ä.)': LSD_AA, 'NiMH LSD AAA (Eneloop o. ä.)': LSD_AAA,
                                'Sonstiges': 'Other'},
        ('models', 'name'): {'HR-3UTC AA (weiß)': 'HR-3UTC AA (white)', 'HR-3UTHC AA (schwarz)': 'HR-3UTHC AA (black)',
                             'HR-4UTC AAA (weiß)': 'HR-4UTC AAA (white)',
                             'HR-4UTHC AAA (schwarz)': 'HR-4UTHC AAA (black)'},
    }

    def _soft_delete(self, *tables):
        """Batteries and models (version 5) and sessions (version 6) are no longer removed: 'deleted' holds the
        time they were deleted (NULL = in use). Deleted ones are hidden; sessions of a deleted battery stay assigned."""
        for table in tables or ('batteries', 'models'):
            if 'deleted' not in [r[1] for r in self.con.execute(f'PRAGMA table_info({table})')]:
                self.con.execute(f'ALTER TABLE {table} ADD COLUMN deleted REAL')

    def _english_values(self):
        """Up to version 3 task, status, grade and phase were stored as German words (and the default model list
        was German). Now they are stored in English and translated when shown; rating notes are written anew."""
        for (table, col), names in self.OLD_VALUES.items():
            for old, new in names.items():
                self.con.execute(f'UPDATE {table} SET {col}=? WHERE {col}=?', (new, old))
        self.con.execute("UPDATE batteries SET name=(SELECT m.name FROM models m WHERE m.id=batteries.model_id) "
                         "WHERE model_id IS NOT NULL")
        self.con.execute("UPDATE sessions SET task='mode ' || substr(task, 7) WHERE task LIKE 'Modus %'")
        for (sid,) in self.con.execute('SELECT id FROM sessions').fetchall():
            self.rerate(sid, commit=False)

    # ------------------------------------------------------------- samples
    def add_samples(self, samples: List[Sample]):
        self.con.executemany(
            f"INSERT OR IGNORE INTO samples VALUES ({','.join('?' * len(COLS))})",
            [tuple(getattr(s, c) for c in COLS) for s in samples])

    def samples(self, dev, slot, t0, t1):
        cur = self.con.execute(
            'SELECT t, mv, ma, res, mah, temp, mode FROM samples WHERE dev=? AND slot=? AND t BETWEEN ? AND ? '
            'ORDER BY t', (dev, slot, t0, t1))
        return cur.fetchall()

    # ------------------------------------------------------------- sessions
    def save_session(self, s: Session):
        grade, note = s.rating()
        vals = (s.slot, s.start, s.end, s.task, s.chem, s.size, s.status, s.nominal,
                s.discharge_mah, s.charge_mah, s.res_first, s.res_min, s.res_last, s.temp_max, grade, note, s.dev)
        if s.db_id is None:
            cur = self.con.execute(
                'INSERT INTO sessions (slot,start,end,task,chem,size,status,nominal,discharge_mah,charge_mah,'
                'res_first,res_min,res_last,temp_max,grade,note,dev) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                vals)
            s.db_id = cur.lastrowid
        else:
            self.con.execute(
                'UPDATE sessions SET slot=?,start=?,end=?,task=?,chem=?,size=?,status=?,nominal=?,discharge_mah=?,'
                'charge_mah=?,res_first=?,res_min=?,res_last=?,temp_max=?,grade=?,note=?,dev=? WHERE id=?',
                vals + (s.db_id,))
        self.con.execute('DELETE FROM phases WHERE session_id=?', (s.db_id,))
        self.con.executemany('INSERT INTO phases VALUES (?,?,?,?,?,?,?,?)',
                             [(s.db_id, i, p.kind, p.start, p.end, p.mah, p.mv_start, p.mv_end)
                              for i, p in enumerate(s.phases)])
        s.dirty = False

    def save_dirty(self, tracker: Tracker):
        """Stores changed sessions. Sessions shorter than MIN_SECS are not stored: a running one waits,
        a closed one is dropped (and deleted if it was stored before)."""
        for s in tracker.closed:
            if s.end - s.start < MIN_SECS:
                if s.db_id is not None:
                    self.con.execute('DELETE FROM phases WHERE session_id=?', (s.db_id,))
                    self.con.execute('DELETE FROM sessions WHERE id=?', (s.db_id,))
                s.dirty = False
        for s in tracker.current.values():
            if s.dirty and s.end - s.start >= MIN_SECS:
                self.save_session(s)
        for s in tracker.closed:
            if s.dirty:
                self.save_session(s)
        tracker.closed = [s for s in tracker.closed if s.dirty]   # keep only unsaved ones
        self.con.commit()

    def sessions(self, limit=500, deleted=False):
        """The latest sessions; deleted: include soft-deleted ones."""
        return self.con.execute(SESSION_SELECT + ('' if deleted else ' WHERE s.deleted IS NULL')
                                + ' ORDER BY s.start DESC LIMIT ?', (limit,)).fetchall()

    def battery_sessions(self, battery_id, deleted=False):
        return self.con.execute(SESSION_SELECT + ' WHERE s.battery_id=?' + ('' if deleted else ' AND s.deleted IS NULL')
                                + ' ORDER BY s.start DESC', (battery_id,)).fetchall()

    def delete_sessions(self, session_ids):
        """Soft delete: hidden from now on (also from the battery statistics); readings and phases stay."""
        self.con.executemany('UPDATE sessions SET deleted=? WHERE id=?', [(time.time(), i) for i in session_ids])
        self.con.commit()

    def hard_delete_sessions(self, session_ids):
        """Remove sessions with their phases for good; the readings (samples) of the slot stay."""
        for i in session_ids:
            self.con.execute('DELETE FROM phases WHERE session_id=?', (i,))
            self.con.execute('DELETE FROM sessions WHERE id=?', (i,))
        self.con.commit()

    def phases(self, session_id):
        return self.con.execute('SELECT kind, start, end, mah FROM phases WHERE session_id=? ORDER BY idx',
                                (session_id,)).fetchall()

    def set_session_meta(self, session_id, label=None, nominal=None, battery_id=False):
        if battery_id is not False:                       # None = no battery
            self.con.execute('UPDATE sessions SET battery_id=? WHERE id=?', (battery_id, session_id))
        if label is not None:
            self.con.execute('UPDATE sessions SET label=? WHERE id=?', (label, session_id))
        if nominal is not None:
            self.con.execute('UPDATE sessions SET nominal=? WHERE id=?', (nominal, session_id))
        self.con.commit()

    def rerate(self, session_id, commit=True):
        """Recompute grade after the nominal capacity was changed."""
        row = self.con.execute('SELECT slot,start,end,task,chem,size,status,nominal,res_first,res_min,'
                               'res_last,temp_max FROM sessions WHERE id=?', (session_id,)).fetchone()
        s = Session(slot=row[0], start=row[1], end=row[2], task=row[3], chem=row[4], size=row[5],
                    status=row[6], nominal=row[7], res_first=row[8], res_min=row[9], res_last=row[10],
                    temp_max=row[11], db_id=session_id)
        s.phases = [Phase(kind=k, start=a, end=b, mah=m) for k, a, b, m in self.phases(session_id)]
        grade, note = s.rating()
        self.con.execute('UPDATE sessions SET grade=?, note=? WHERE id=?', (grade, note, session_id))
        if commit:
            self.con.commit()

    def session_row(self, session_id):
        return self.con.execute(SESSION_SELECT + ' WHERE s.id=?', (session_id,)).fetchone()

    def session_dict(self, session_id):
        row = self.session_row(session_id)
        return dict(zip(SESSION_FIELDS, row)) if row else None

    def battery_of(self, session_id):
        row = self.con.execute('SELECT battery_id FROM sessions WHERE id=?', (session_id,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------- batteries
    def batteries(self, deleted=False):
        """All batteries with number of sessions, the latest measured discharge capacity / grade, the model
        (id, maker, name), the end of the last measurement (session with a discharge capacity) / last charge and
        the time it was deleted (None). deleted: include soft-deleted batteries."""
        return self.con.execute(
            'SELECT b.id, b.name, b.maker, b.capacity, b.type, b.description, '
            ' (SELECT COUNT(*) FROM sessions s WHERE s.battery_id=b.id AND s.deleted IS NULL), '
            ' (SELECT s.discharge_mah FROM sessions s WHERE s.battery_id=b.id AND s.discharge_mah > 0 AND s.deleted IS NULL '
            '  ORDER BY s.start DESC LIMIT 1), '
            ' (SELECT s.grade FROM sessions s WHERE s.battery_id=b.id AND s.discharge_mah > 0 AND s.deleted IS NULL '
            '  ORDER BY s.start DESC LIMIT 1), '
            ' b.model_id, m.maker, m.name, '
            ' (SELECT MAX(s.end) FROM sessions s WHERE s.battery_id=b.id AND s.discharge_mah > 0 AND s.deleted IS NULL), '
            ' (SELECT MAX(s.end) FROM sessions s WHERE s.battery_id=b.id AND s.charge_mah > 0 AND s.deleted IS NULL), '
            ' b.deleted '
            'FROM batteries b LEFT JOIN models m ON m.id = b.model_id '
            + ('' if deleted else 'WHERE b.deleted IS NULL ') + 'ORDER BY b.id').fetchall()

    def battery(self, battery_id, deleted=False):
        """One battery as dict (with 'deleted': time or None); deleted ones only with deleted=True."""
        row = self.con.execute(f"SELECT {','.join(BATTERY_FIELDS)}, deleted FROM batteries WHERE id=?"
                               + ('' if deleted else ' AND deleted IS NULL'), (battery_id,)).fetchone()
        return dict(zip(BATTERY_FIELDS + ['deleted'], row)) if row else None

    def next_battery_id(self):
        return (self.con.execute('SELECT MAX(id) FROM batteries').fetchone()[0] or 0) + 1

    def save_battery(self, b: dict, old_id=None):
        """Insert or update (old_id = id before editing; the id itself may be changed)."""
        vals = tuple(b[k] for k in BATTERY_FIELDS)
        if old_id is None:
            self.con.execute(f"INSERT INTO batteries ({','.join(BATTERY_FIELDS)},created) "
                             f"VALUES ({','.join('?' * len(vals))},?)", vals + (time.time(),))
        else:
            self.con.execute(f"UPDATE batteries SET {','.join(k + '=?' for k in BATTERY_FIELDS)} WHERE id=?",
                             vals + (old_id,))
            if b['id'] != old_id:
                self.con.execute('UPDATE sessions SET battery_id=? WHERE battery_id=?', (b['id'], old_id))
        self.con.commit()

    def delete_batteries(self, battery_ids):
        """Soft delete: hidden from now on; the id stays taken and the sessions stay assigned."""
        self.con.executemany('UPDATE batteries SET deleted=? WHERE id=?', [(time.time(), b) for b in battery_ids])
        self.con.commit()

    def hard_delete_batteries(self, battery_ids):
        """Remove for good: their sessions lose the assignment (the sessions and readings stay)."""
        for b in battery_ids:
            self.con.execute('UPDATE sessions SET battery_id=NULL WHERE battery_id=?', (b,))
            self.con.execute('DELETE FROM batteries WHERE id=?', (b,))
        self.con.commit()

    def purge_deleted(self):
        """Remove all soft-deleted sessions, batteries and models for good."""
        sids = [r[0] for r in self.con.execute('SELECT id FROM sessions WHERE deleted IS NOT NULL')]
        bids = [r[0] for r in self.con.execute('SELECT id FROM batteries WHERE deleted IS NOT NULL')]
        mids = [r[0] for r in self.con.execute('SELECT id FROM models WHERE deleted IS NOT NULL')]
        self.hard_delete_sessions(sids)
        self.hard_delete_batteries(bids)
        self.hard_delete_models(mids)

    def deleted_counts(self):
        """Soft-deleted (sessions, batteries, models)."""
        return tuple(self.con.execute(f'SELECT COUNT(*) FROM {t} WHERE deleted IS NOT NULL').fetchone()[0]
                     for t in ('sessions', 'batteries', 'models'))

    def size(self):
        """Bytes of the database file including its write-ahead log."""
        return sum(os.path.getsize(self.path + x) for x in ('', '-wal') if os.path.exists(self.path + x))

    def stats(self):
        """Number of entries: readings, sessions, phases, batteries, models, chargers (deleted ones included)."""
        return {t: self.con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                for t in ('samples', 'sessions', 'phases', 'batteries', 'models', 'devices')}

    def optimize(self):
        """VACUUM (rewrite the file without free pages) and shrink the write-ahead log."""
        self.con.commit()
        self.con.execute('VACUUM')
        self.con.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def makers(self):
        return [r[0] for r in self.con.execute(
            "SELECT maker FROM models WHERE maker != '' AND deleted IS NULL UNION "
            "SELECT maker FROM batteries WHERE maker != '' AND deleted IS NULL "
            'ORDER BY 1 COLLATE NOCASE')]

    # ------------------------------------------------------------- devices
    def devices(self):
        return [dict(zip(DEVICE_FIELDS, r)) for r in self.con.execute(
            f"SELECT {','.join(DEVICE_FIELDS)} FROM devices ORDER BY id")]

    def device_for(self, key, model, label, slots, version):
        """Id of a connected charger (created on first contact). The first real N8 takes over the
        data recorded before chargers were told apart. A charger reachable over USB and Bluetooth (A4 Air) has
        a key for each; the worker finds out that two devices are the same one by comparing their readings and
        then joins them (merge), the second key goes to alt_key."""
        row = self.con.execute('SELECT id FROM devices WHERE key=? OR alt_key=?', (key, key)).fetchone()
        if row is None and model == 'NXHOSTP':
            row = self.con.execute('SELECT id FROM devices WHERE key=?', (LEGACY_N8,)).fetchone()
            if row:
                self.con.execute('UPDATE devices SET key=? WHERE id=?', (key, row[0]))
        if row:
            self.con.execute('UPDATE devices SET slots=?, version=?, last_seen=? WHERE id=?',
                             (slots, version, time.time(), row[0]))
            self.con.commit()
            return row[0]
        names = {r[0] for r in self.con.execute('SELECT name FROM devices')}
        name, n = label, 2
        while name in names:
            name, n = f'{label} {n}', n + 1
        dev = self.con.execute('INSERT INTO devices (key,model,name,slots,version,last_seen) VALUES (?,?,?,?,?,?)',
                               (key, model, name, slots, version, time.time())).lastrowid
        self.con.commit()
        return dev

    def merge_devices(self, src, dst):
        """src turned out to be the same charger as dst (same readings over the other connection): move its
        samples and sessions to dst (sessions overlapping one of dst on the same slot are duplicates and are
        dropped), keep its key as dst's alt_key and delete it."""
        key = self.con.execute('SELECT key FROM devices WHERE id=?', (src,)).fetchone()
        if key is None:
            return
        self.con.execute('UPDATE OR IGNORE samples SET dev=? WHERE dev=?', (dst, src))
        self.con.execute('DELETE FROM samples WHERE dev=?', (src,))
        dup = [r[0] for r in self.con.execute(
            'SELECT a.id FROM sessions a JOIN sessions b ON b.dev=? AND b.slot=a.slot AND b.start<=a.end '
            'AND b.end>=a.start WHERE a.dev=?', (dst, src))]
        for sid in dup:
            self.con.execute('DELETE FROM phases WHERE session_id=?', (sid,))
            self.con.execute('DELETE FROM sessions WHERE id=?', (sid,))
        self.con.execute('UPDATE sessions SET dev=? WHERE dev=?', (dst, src))
        self.con.execute('UPDATE devices SET alt_key=COALESCE(alt_key, ?) WHERE id=?', (key[0], dst))
        self.con.execute('DELETE FROM devices WHERE id=?', (src,))
        self.con.commit()

    def set_device_name(self, dev, name):
        self.con.execute('UPDATE devices SET name=? WHERE id=?', (name, dev))
        self.con.commit()

    # ------------------------------------------------------------- models
    def models(self, deleted=False):
        """All models with the number of batteries of that model and the time it was deleted (None).
        deleted: include soft-deleted models."""
        return self.con.execute(
            f"SELECT {','.join('m.' + k for k in MODEL_FIELDS)}, "
            '(SELECT COUNT(*) FROM batteries b WHERE b.model_id=m.id AND b.deleted IS NULL), m.deleted '
            'FROM models m ' + ('' if deleted else 'WHERE m.deleted IS NULL ')
            + 'ORDER BY m.maker COLLATE NOCASE, m.name COLLATE NOCASE').fetchall()

    def model(self, model_id):
        row = self.con.execute(f"SELECT {','.join(MODEL_FIELDS)} FROM models WHERE id=?", (model_id,)).fetchone()
        return dict(zip(MODEL_FIELDS, row)) if row else None

    def find_model(self, maker, name):
        """(id, deleted) of the model with this maker and name, or (None, None)."""
        row = self.con.execute('SELECT id, deleted FROM models WHERE maker=? AND name=?', (maker, name)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def save_model(self, m: dict):
        """Insert (m['id'] None) or update. Batteries of the model take over name / maker / type / capacity.
        Returns the model id and the ids of its batteries (their sessions need a new rating)."""
        vals = (m['maker'], m['name'], m['type'], m['capacity'], m['note'])
        if m.get('id') is None:
            mid = self.con.execute('INSERT INTO models (maker,name,type,capacity,note) VALUES (?,?,?,?,?)',
                                   vals).lastrowid
        else:
            mid = m['id']
            self.con.execute('UPDATE models SET maker=?,name=?,type=?,capacity=?,note=?,deleted=NULL WHERE id=?',
                             vals + (mid,))                    # saving a deleted model brings it back
        self.con.execute('UPDATE batteries SET name=?, maker=?, type=?, capacity=? WHERE model_id=?',
                         (m['name'], m['maker'], m['type'], m['capacity'], mid))
        self.con.commit()
        return mid, [r[0] for r in self.con.execute('SELECT id FROM batteries WHERE model_id=?', (mid,))]

    def delete_models(self, model_ids):
        """Soft delete: hidden from now on; its batteries keep their values (maker / type / capacity)."""
        self.con.executemany('UPDATE models SET deleted=? WHERE id=?', [(time.time(), m) for m in model_ids])
        self.con.commit()

    def hard_delete_models(self, model_ids):
        """Remove for good: its batteries lose the link but keep maker / type / capacity."""
        for m in model_ids:
            self.con.execute('UPDATE batteries SET model_id=NULL WHERE model_id=?', (m,))
            self.con.execute('DELETE FROM models WHERE id=?', (m,))
        self.con.commit()

    # ------------------------------------------------------------- resume
    def resume(self, tracker: Tracker, since=None):
        """Rebuild running sessions (and everything after `since`) from the stored samples, so a
        restart of the app continues the open charging tasks instead of starting new ones.
        Sessions of the same slot that were cut off by a gap in the data during the 24 h before are
        rebuilt as well (they are joined again if the task kept running, see Tracker._continues).
        Label, nominal capacity and battery entered by the user are kept."""
        cands = [since] if since is not None else []
        for dev, slot, start in self.con.execute('SELECT dev, slot, start FROM sessions WHERE status=?',
                                                 (RUNNING,)).fetchall():
            row = self.con.execute('SELECT MIN(start) FROM sessions WHERE dev=? AND slot=? AND status=? '
                                   'AND end BETWEEN ? AND ?', (dev, slot, ABORTED, start - 86400, start)).fetchone()
            cands += [t for t in (start, row[0]) if t is not None]
        if not cands:
            return
        t0 = min(cands)
        old = self.con.execute('SELECT id, dev, slot, start, nominal, label, battery_id, deleted FROM sessions '
                               'WHERE end >= ?', (t0 - GAP_SECS,)).fetchall()
        if old:
            t0 = min(t0, min(r[3] for r in old))
        meta = {(r[1], r[2], round(r[3])): (r[4], r[5], r[6], r[7]) for r in old}
        for r in old:
            self.con.execute('DELETE FROM phases WHERE session_id=?', (r[0],))
            self.con.execute('DELETE FROM sessions WHERE id=?', (r[0],))
        cur = self.con.execute(f"SELECT {','.join(COLS)} FROM samples WHERE t >= ? ORDER BY t, dev, slot", (t0,))
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for r in rows:
                tracker.feed(Sample(*r))
        restored = []
        for s in tracker.sessions():
            m = meta.get((s.dev, s.slot, round(s.start)))
            if not m or m[2] is None:                 # joined session: take over a battery given to a part
                m = next((v for (dev, slot, st), v in meta.items()
                          if dev == s.dev and slot == s.slot and s.start <= st <= s.end and v[2] is not None), m)
            if m:
                s.nominal = m[0] or s.nominal
                restored.append((s, m[1], m[2], m[3]))
        self.save_dirty(tracker)
        for s, label, bid, deleted in restored:
            self.con.execute('UPDATE sessions SET label=?, battery_id=?, deleted=? WHERE id=?',
                             (label or '', bid, deleted, s.db_id))
        self.con.commit()
