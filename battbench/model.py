"""Session/phase detection and battery rating.

A *session* is one charging task of one battery in one slot of one charger (insert -> remove, or a
new task). Slots are keyed by (device id, slot).
A *phase* is a part of a session with constant current direction (charge / discharge).
The N8 resets the mAh counter at the start of every phase, so a phase's capacity is the last
non-zero counter value before the next phase starts.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .device import Sample
from .i18n import QT_TRANSLATE_NOOP

# Tasks, statuses, grades and phase kinds are stored in English and translated only when shown (i18n.tr_data).
TASKS = {3: QT_TRANSLATE_NOOP('data', 'charge'), 5: QT_TRANSLATE_NOOP('data', 'discharge'),
         7: QT_TRANSLATE_NOOP('data', 'storage'), 9: QT_TRANSLATE_NOOP('data', 'cycle'),
         11: QT_TRANSLATE_NOOP('data', 'analysis'), 13: QT_TRANSLATE_NOOP('data', 'activation')}
DONE = {mode + 1: task for mode, task in TASKS.items()}
MODE_NAMES = {0: QT_TRANSLATE_NOOP('data', 'empty'), 1: QT_TRANSLATE_NOOP('data', 'waiting'),
              2: QT_TRANSLATE_NOOP('data', 'reversed polarity'), 3: QT_TRANSLATE_NOOP('data', 'charging'),
              4: QT_TRANSLATE_NOOP('data', 'charged'), 5: QT_TRANSLATE_NOOP('data', 'discharging'),
              6: QT_TRANSLATE_NOOP('data', 'discharged'), 7: QT_TRANSLATE_NOOP('data', 'storage'),
              8: QT_TRANSLATE_NOOP('data', 'stored'), 9: QT_TRANSLATE_NOOP('data', 'cycle'),
              10: QT_TRANSLATE_NOOP('data', 'cycle done'), 11: QT_TRANSLATE_NOOP('data', 'analysis'),
              12: QT_TRANSLATE_NOOP('data', 'analysis done'), 13: QT_TRANSLATE_NOOP('data', 'activation'),
              14: QT_TRANSLATE_NOOP('data', 'activation done'), 20: QT_TRANSLATE_NOOP('data', 'error')}
CHARGE, DISCHARGE = TASKS[3], TASKS[5]                     # phase kinds
RUNNING = QT_TRANSLATE_NOOP('data', 'running')             # session statuses
FINISHED = QT_TRANSLATE_NOOP('data', 'done')
REMOVED = QT_TRANSLATE_NOOP('data', 'removed')
ABORTED = QT_TRANSLATE_NOOP('data', 'aborted')
GRADES = (QT_TRANSLATE_NOOP('data', 'very good'), QT_TRANSLATE_NOOP('data', 'good'),
          QT_TRANSLATE_NOOP('data', 'fair'), QT_TRANSLATE_NOOP('data', 'worn out'),
          QT_TRANSLATE_NOOP('data', 'suspicious'))
NO_GRADE = '–'
# Rating notes: (text with {placeholders}, values). The DB keeps them in English, the app translates the text.
NOTE_CAPACITY = QT_TRANSLATE_NOOP('data', '{mah} mAh = {pct} % of {nominal} mAh')
NOTE_EFFICIENCY = QT_TRANSLATE_NOOP('data', 'charge efficiency {pct} %')
NOTE_RES = QT_TRANSLATE_NOOP('data', 'internal resistance {res} mΩ: {quality} (lower is better)')
# Internal resistance as the chargers report it (contact resistance included, so higher than a 4-wire meter):
# upper limits in mOhm for very good / good / medium / poor, above: very poor; and from where a cell without a
# capacity measurement counts as suspicious.
RES_QUALITY = (QT_TRANSLATE_NOOP('data', 'very good'), QT_TRANSLATE_NOOP('data', 'good'),
               QT_TRANSLATE_NOOP('data', 'medium'), QT_TRANSLATE_NOOP('data', 'poor'),
               QT_TRANSLATE_NOOP('data', 'very poor'))
RES_LIMITS = {'ni': (150, 300, 450, 600), 'li': (50, 100, 150, 200)}
RES_SUSPICIOUS = {'ni': 1000, 'li': 400}
NOTE_RUNNING = QT_TRANSLATE_NOOP('data', 'task not finished yet')
NOTE_MEASURING = QT_TRANSLATE_NOOP('data', 'discharge running: {mah} mAh so far')
NOTE_INCOMPLETE = QT_TRANSLATE_NOOP('data', 'task {status}, result incomplete')

GAP_SECS = 300            # no data for this long -> session closed
MIN_SECS = 10             # shorter sessions are not stored (e.g. an empty slot probing for a cell)
PHASE_MIN_MA = 10         # a full cell on the A4 Air shows -3..+7 mA noise; real currents are >= 23 mA
NOMINAL = {'AA': 2000, 'AAA': 800}


@dataclass
class Phase:
    kind: str             # CHARGE / DISCHARGE
    start: float
    end: float
    mah: int = 0          # absolute capacity of this phase
    mv_start: int = 0
    mv_end: int = 0


@dataclass
class Session:
    slot: int
    start: float
    end: float
    task: str
    dev: int = 0
    chem: str = ''
    size: str = ''
    status: str = RUNNING
    phases: List[Phase] = field(default_factory=list)
    res_first: int = 0
    res_min: int = 0
    res_last: int = 0
    temp_max: int = 0
    nominal: int = 0
    db_id: Optional[int] = None
    dirty: bool = True

    # ---------------------------------------------------------------- results
    def _real_phases(self):
        """Phases that count for the capacities: a phase of a single reading is a glitch (e.g. the probe pulse of a
        slot when the cell is taken out), its mAh is just the counter of the phase before."""
        return [p for p in self.phases if p.end > p.start]

    @property
    def discharge_mah(self) -> Optional[int]:
        d = [p for p in self._real_phases() if p.kind == DISCHARGE and p.mah > 0]
        return d[-1].mah if d else None

    @property
    def charge_mah(self) -> Optional[int]:
        """Charge put in after the (last) discharge, or the only charge phase."""
        phases = self._real_phases()
        last_d = max((i for i, p in enumerate(phases) if p.kind == DISCHARGE), default=-1)
        c = [p for p in phases[last_d + 1:] if p.kind == CHARGE and p.mah > 0]
        return c[-1].mah if c else None

    def rating(self):
        return rate(self)


def rate(s: Session):
    """Returns (grade, note in English)."""
    grade, notes = rate_values(s.discharge_mah, s.nominal or NOMINAL.get(s.size, 0), s.charge_mah, s.res_min,
                               s.status, measuring(s.status, [p.kind for p in s.phases]), s.chem)
    return grade, note_text(notes)


def res_family(chem):
    """'li' for lithium chemistries (LiIon, LiHV, LiFe, Li-Ion …), else 'ni' (NiMH, NiCd, NiZn, unknown)."""
    return 'li' if (chem or '').lower().startswith('li') else 'ni'


def res_level(res, chem):
    """0 very good … 4 very poor (index into RES_QUALITY)."""
    return sum(res >= limit for limit in RES_LIMITS[res_family(chem)])


def measuring(status, kinds):
    """The discharge capacity is still being measured (running session whose last phase is a discharge): no
    rating yet. kinds: phase kinds in order."""
    return status == RUNNING and bool(kinds) and kinds[-1] == DISCHARGE


def rate_values(dis, nominal, chg, res, status, still_measuring=False, chem=''):
    """Returns (grade, notes), notes = [(text, values)] (see note_text).
    Capacity rule from the SkyRC manuals: < 60 % of nominal = worn out. The internal resistance caps the rating:
    poor -> at most good, very poor -> at most fair."""
    notes = []
    grade = NO_GRADE
    level = res_level(res, chem) if res else None
    res_note = (NOTE_RES, dict(res=res, quality=RES_QUALITY[level])) if res else None
    if still_measuring:
        if dis:
            notes.append((NOTE_MEASURING, dict(mah=dis)))
        if res_note:
            notes.append(res_note)
        return grade, notes
    if dis and nominal:
        pct = 100 * dis / nominal
        grade = GRADES[0 if pct >= 90 else 1 if pct >= 80 else 2 if pct >= 60 else 3]
        notes.append((NOTE_CAPACITY, dict(mah=dis, pct=round(pct), nominal=nominal)))
        if chg and status == FINISHED:
            notes.append((NOTE_EFFICIENCY, dict(pct=round(100 * dis / chg))))
    if res_note:
        notes.append(res_note)
        if level == 4 and grade in GRADES[:2]:
            grade = GRADES[2]
        elif level == 3 and grade == GRADES[0]:
            grade = GRADES[1]
        if res >= RES_SUSPICIOUS[res_family(chem)] and grade == NO_GRADE:
            grade = GRADES[4]
    if status == RUNNING:
        notes.append((NOTE_RUNNING, {}))
    elif status != FINISHED:
        notes.append((NOTE_INCOMPLETE, dict(status=status)))
    return grade, notes


def note_text(notes, translate=lambda text: text):
    """notes from rate_values -> one line. translate: e.g. i18n.tr_data (applied to the texts, status and quality)."""
    return '; '.join(translate(text).format(**{k: translate(v) if k in ('status', 'quality') else v
                                               for k, v in values.items()})
                     for text, values in notes)


class Tracker:
    """Feeds samples (per device and slot, in time order) and maintains sessions/phases."""

    def __init__(self):
        self.current: Dict[Tuple[int, int], Session] = {}
        self.last: Dict[Tuple[int, int], Sample] = {}
        self.closed: List[Session] = []

    def feed(self, s: Sample) -> Optional[Session]:
        key = (s.dev, s.slot)
        cur, prev = self.current.get(key), self.last.get(key)
        self.last[key] = s
        if cur and prev and s.t - prev.t > GAP_SECS and not self._continues(cur, prev, s):
            self._close(cur, ABORTED)
            cur = None
        if s.mode == 0:                                   # slot empty / idle
            if cur:
                self._close(cur, FINISHED if cur.status == FINISHED else REMOVED)
            return None
        task = TASKS.get(s.mode) or DONE.get(s.mode, f'mode {s.mode}')
        if cur and s.mode in TASKS and TASKS[s.mode] != cur.task:
            self._close(cur, ABORTED)                     # new task started on the same battery
            cur = None
        if cur is None:
            cur = Session(slot=s.slot, start=s.t, end=s.t, task=task, dev=s.dev, chem=s.chem, size=s.size,
                          nominal=NOMINAL.get(s.size, 0))
            self.current[key] = cur
        cur.end = s.t
        cur.dirty = True
        if s.chem not in ('auto', 'unknown'):
            cur.chem = s.chem
        if s.size not in ('empty', 'unknown'):
            cur.size = s.size
        cur.temp_max = max(cur.temp_max, s.temp)
        if s.res > 0:
            cur.res_first = cur.res_first or s.res
            cur.res_min = min(cur.res_min or s.res, s.res)
            cur.res_last = s.res
        if s.mode in DONE:
            cur.status = FINISHED
        kind = CHARGE if s.ma >= PHASE_MIN_MA else DISCHARGE if s.ma <= -PHASE_MIN_MA else None
        ph = cur.phases[-1] if cur.phases else None
        if kind:
            if ph is None or ph.kind != kind:
                ph = Phase(kind=kind, start=s.t, end=s.t, mv_start=s.mv)
                cur.phases.append(ph)
            ph.end, ph.mv_end = s.t, s.mv
        if ph and s.mah != 0:
            ph.mah = max(ph.mah, abs(s.mah))
        return cur

    @staticmethod
    def _continues(cur: Session, prev: Sample, s: Sample) -> bool:
        """After a gap in the data (app was not running): same task and the charger's task
        timer kept counting -> it is still the same session."""
        task = TASKS.get(s.mode) or DONE.get(s.mode)
        return task == cur.task and 0 < prev.secs <= s.secs

    def _close(self, cur: Session, status: str):
        if len(cur.phases) > 1 and cur.phases[-1].end <= cur.phases[-1].start:
            cur.phases.pop()                              # a last phase of a single reading is a glitch
        cur.status = status if cur.status != FINISHED else FINISHED
        cur.dirty = True
        self.closed.append(cur)
        self.current.pop((cur.dev, cur.slot), None)

    def sessions(self):
        return self.closed + list(self.current.values())
