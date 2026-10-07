"""Session/phase detection and battery rating.

A *session* is one charging task of one battery in one slot of one charger (insert -> remove, or a
new task). Slots are keyed by (device id, slot).
A *phase* is a part of a session with constant current direction (charge / discharge).
The N8 resets the mAh counter at the start of every phase, so a phase's capacity is the last
non-zero counter value before the next phase starts.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .device import Sample, res_estimated
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
# Rating = category of the health index (see health()); a cell without a capacity measurement but with a very
# high internal resistance is "suspicious".
CATEGORIES = (QT_TRANSLATE_NOOP('data', 'A · high drain'), QT_TRANSLATE_NOOP('data', 'B · medium drain'),
              QT_TRANSLATE_NOOP('data', 'C · low drain'), QT_TRANSLATE_NOOP('data', 'D · recycle'))
USES = dict(zip(CATEGORIES, (QT_TRANSLATE_NOOP('data', 'flash units, RC models, motorised toys'),
                             QT_TRANSLATE_NOOP('data', 'LED torches, computer mice, bicycle lights'),
                             QT_TRANSLATE_NOOP('data', 'remote controls, wall clocks, solar lights'),
                             QT_TRANSLATE_NOOP('data', 'no longer usable: high self-discharge or risk of failure'))))
GRADES = CATEGORIES + (QT_TRANSLATE_NOOP('data', 'suspicious'),)
NO_GRADE = '–'
# Health index: weights of the four scores (each 0..100); scores that can't be computed are left out and the
# weights of the others scaled up.
OHI_WEIGHTS = {'capacity': 0.40, 'resistance': 0.30, 'voltage': 0.20, 'efficiency': 0.10}
V_START_OK, V_MID_OK, V_KNEE = 1150, 1200, 1000      # mV under load: after 5 %, average 20-80 %, early drop below
# Rating notes: (text with {placeholders}, values). The DB keeps them in English, the app translates the text.
NOTE_CAPACITY = QT_TRANSLATE_NOOP('data', '{mah} mAh = {pct} % of {nominal} mAh')
NOTE_EFFICIENCY = QT_TRANSLATE_NOOP('data', 'charge efficiency {pct} %')
NOTE_RES = QT_TRANSLATE_NOOP('data', 'internal resistance {res} mΩ: {quality} (lower is better)')
NOTE_RES_EST = QT_TRANSLATE_NOOP('data', 'internal resistance only estimated (A4 Air over USB), not rated')
NOTE_VOLTAGE = QT_TRANSLATE_NOOP('data', 'under load {v_start} V after 5 %, {v_mid} V on average from 20 to 80 %')
NOTE_KNEE = QT_TRANSLATE_NOOP('data', 'below 1.0 V before 80 % of the discharge')
NOTE_HEALTH = QT_TRANSLATE_NOOP('data', 'health index {ohi} of 100')
NOTE_USE = QT_TRANSLATE_NOOP('data', 'for: {use}')
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
    res_est: bool = False             # internal resistance only estimated (A4 Air over USB): not rated
    v_start: Optional[int] = None     # voltage curve of the last discharge (see discharge_curve), mV
    v_mid: Optional[int] = None
    early_drop: Optional[bool] = None
    points: list = field(default_factory=list, repr=False)    # (t, mV, mA) of the running discharge phase

    def finish_curve(self):
        """The last discharge phase is over: evaluate its voltage curve."""
        c = discharge_curve(self.points)
        if c:
            self.v_start, self.v_mid, self.early_drop = c['v_start'], c['v_mid'], c['early_drop']
        self.points = []

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
    """Returns (grade, note in English, health index or None)."""
    grade, notes, ohi = rate_values(s.discharge_mah, s.nominal or NOMINAL.get(s.size, 0), s.charge_mah, s.res_min,
                               s.status, measuring(s.status, [p.kind for p in s.phases]), s.chem,
                               s.v_start, s.v_mid, s.early_drop, s.res_est)
    return grade, note_text(notes), ohi


def discharge_curve(points):
    """Voltage under load of one discharge: points (t, mV, mA) in time order. The charge taken out is added up
    from the current (independent of the charger's counter); positions are fractions of the whole discharge.
    Returns dict(v_start = mV after 5 %, v_mid = mean mV from 20 to 80 %, early_drop = below 1.0 V before 80 %,
    t_start / t_20 / t_80 = times of these positions), or None for a discharge too short to judge."""
    q, prev, rows = 0.0, None, []
    for t, mv, ma in points:
        if ma <= -PHASE_MIN_MA:               # pauses (0 mA) show the resting voltage, not the one under load
            if prev is not None:
                q += -ma * min(t - prev, 120) / 3600
            rows.append((q, t, mv))
        prev = t
    if len(rows) < 10 or q < 50:
        return None

    def at(frac):
        return next(r for r in rows if r[0] >= frac * q)

    mid = [r[2] for r in rows if 0.2 * q <= r[0] <= 0.8 * q]
    return dict(v_start=at(0.05)[2], v_mid=round(sum(mid) / len(mid)) if mid else at(0.5)[2],
                early_drop=any(r[2] < V_KNEE for r in rows if r[0] < 0.8 * q),
                t_start=at(0.05)[1], t_20=at(0.2)[1], t_80=at(0.8)[1])


def score_capacity(pct):
    """Capacity in % of nominal -> 0..100."""
    if pct >= 90:
        return 100
    if pct >= 70:
        return 100 - (90 - pct) * 2.5
    if pct >= 50:
        return 50 - (70 - pct) * 2.0
    return 0


def score_resistance(res, chem):
    """Internal resistance as the charger reports it -> 0..100. The NiMH steps of the specification (4-wire
    meter: 30 / 60 / 120 / 250 mOhm) are moved to the charger scale (contacts included): the RES_LIMITS."""
    b0, b1, b2, b3 = RES_LIMITS[res_family(chem)]
    if res < b0:
        return 100
    if res <= b1:
        return 100 - (res - b0) / (b1 - b0) * 25
    if res <= b2:
        return 75 - (res - b1) / (b2 - b1) * 35
    if res <= b3:
        return max(0, 40 - (res - b2) / (b3 - b2) * 40)
    return 0


def score_efficiency(eta):
    """Charge efficiency (discharge / charge after it, %) -> 0..100. 75-85 % is normal for NiMH; less means
    losses (heat), more means a charge that may have ended early."""
    if 75 <= eta <= 85:
        return 100
    if 65 <= eta < 75:
        return 100 - (75 - eta) * 3
    if eta < 65:
        return max(0, 70 - (65 - eta) * 4)
    return 50


def score_voltage(v_start, v_mid, early_drop):
    """Voltage under load (mV) -> 0..100: penalties for a low start, a low plateau and an early drop."""
    p_start = (V_START_OK - v_start) / 1000 * 200 if v_start < V_START_OK else 0
    p_plat = (V_MID_OK - v_mid) / 1000 * 150 if v_mid < V_MID_OK else 0
    return max(0, 100 - p_start - p_plat - (30 if early_drop else 0))


def health(dis, nominal, chg, res, chem='', v_start=None, v_mid=None, early_drop=None):
    """Health index of a measured cell, or None without discharge capacity and nominal capacity.
    chg: charge after the discharge (only when complete), res: None if unknown or only estimated.
    Returns dict(ohi, category, pct, eta, scores) - scores {capacity, resistance, voltage, efficiency}, each
    0..100; missing ones are left out and the weights of the others scaled up."""
    if not (dis and nominal):
        return None
    pct = 100 * dis / nominal
    eta = 100 * dis / chg if chg else None
    scores = {'capacity': score_capacity(pct)}
    if res:
        scores['resistance'] = score_resistance(res, chem)
    if res_family(chem) == 'ni' and v_start and v_mid:   # the voltage limits are those of a NiMH cell
        scores['voltage'] = score_voltage(v_start, v_mid, early_drop)
    if eta:
        scores['efficiency'] = score_efficiency(eta)
    ohi = sum(OHI_WEIGHTS[k] * v for k, v in scores.items()) / sum(OHI_WEIGHTS[k] for k in scores)
    high_drain = bool(res) and res < RES_LIMITS[res_family(chem)][1]      # the specification's 60 mOhm
    if ohi < 50 or scores['capacity'] < 50 or scores.get('resistance') == 0:
        category = CATEGORIES[3]
    elif ohi >= 85 and high_drain:
        category = CATEGORIES[0]
    elif ohi >= 70:
        category = CATEGORIES[1]
    else:
        category = CATEGORIES[2]
    return dict(ohi=round(ohi), category=category, pct=pct, eta=eta, scores=scores)


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


def rate_values(dis, nominal, chg, res, status, still_measuring=False, chem='', v_start=None, v_mid=None,
                early_drop=None, res_est=False):
    """Returns (grade, notes, health index or None), notes = [(text, values)] (see note_text). The grade is the
    category of the health index (health()); the charge efficiency counts only once the charge after the discharge is
    complete."""
    notes = []
    grade = NO_GRADE
    if res_est:
        res_note, res = (NOTE_RES_EST, {}), None
    else:
        res_note = (NOTE_RES, dict(res=res, quality=RES_QUALITY[res_level(res, chem)])) if res else None
    if still_measuring:
        if dis:
            notes.append((NOTE_MEASURING, dict(mah=dis)))
        if res_note:
            notes.append(res_note)
        return grade, notes, None
    h = health(dis, nominal, chg if status == FINISHED else None, res, chem, v_start, v_mid, early_drop)
    if h:
        grade = h['category']
        notes.append((NOTE_HEALTH, dict(ohi=h['ohi'])))
        notes.append((NOTE_CAPACITY, dict(mah=dis, pct=round(h['pct']), nominal=nominal)))
    if res_note:
        notes.append(res_note)
    if h and 'voltage' in h['scores']:
        notes.append((NOTE_VOLTAGE, dict(v_start=f'{v_start / 1000:.3f}', v_mid=f'{v_mid / 1000:.3f}')))
        if early_drop:
            notes.append((NOTE_KNEE, {}))
    if h and h['eta']:
        notes.append((NOTE_EFFICIENCY, dict(pct=round(h['eta']))))
    if h:
        notes.append((NOTE_USE, dict(use=USES[grade])))
    elif res and res >= RES_SUSPICIOUS[res_family(chem)]:
        grade = GRADES[4]
    if status == RUNNING:
        notes.append((NOTE_RUNNING, {}))
    elif status != FINISHED:
        notes.append((NOTE_INCOMPLETE, dict(status=status)))
    return grade, notes, h['ohi'] if h else None


def note_text(notes, translate=lambda text: text):
    """notes from rate_values -> one line. translate: e.g. i18n.tr_data (applied to the texts, status and quality)."""
    return '; '.join(translate(text).format(**{k: translate(v) if k in ('status', 'quality', 'use') else v
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
            cur.res_est = cur.res_est or res_estimated(s)
            cur.res_first = cur.res_first or s.res
            cur.res_min = min(cur.res_min or s.res, s.res)
            cur.res_last = s.res
        if s.mode in DONE:
            cur.status = FINISHED
        kind = CHARGE if s.ma >= PHASE_MIN_MA else DISCHARGE if s.ma <= -PHASE_MIN_MA else None
        ph = cur.phases[-1] if cur.phases else None
        if kind:
            if ph is None or ph.kind != kind:
                if ph and ph.kind == DISCHARGE:
                    cur.finish_curve()
                ph = Phase(kind=kind, start=s.t, end=s.t, mv_start=s.mv)
                cur.phases.append(ph)
                cur.points = []
            ph.end, ph.mv_end = s.t, s.mv
        if ph and ph.kind == DISCHARGE:
            cur.points.append((s.t, s.mv, s.ma))
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
        if cur.phases and cur.phases[-1].kind == DISCHARGE and cur.points:
            cur.finish_curve()
        cur.status = status if cur.status != FINISHED else FINISHED
        cur.dirty = True
        self.closed.append(cur)
        self.current.pop((cur.dev, cur.slot), None)

    def sessions(self):
        return self.closed + list(self.current.values())
