"""A restart continues the open sessions (DB.resume), also a finished one whose cell is still in the slot.

Run: python -m unittest discover tests
"""
import os
import tempfile
import unittest

from battbench.db import DB
from battbench.device import Sample
from battbench.model import FINISHED, Tracker


def sample(t, slot, mode, ma, mah=0):
    return Sample(t=t, slot=slot, mode=mode, mode_str='', chem='NiMH', size='AA', mv=1400, ma=ma, res=180, mah=mah,
                  secs=int(t), temp=30, itemp=0, progress=0, power=0, energy=0, raw='df00', dev=1)


class Resume(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.db = DB(self.path)
        self.db.con.execute("INSERT INTO devices (id, key, model, name, slots) VALUES (1, 'k', 'N8', 'N8', 8)")
        self.db.con.execute("INSERT INTO batteries (id, name) VALUES (7, 'eneloop')")

    def tearDown(self):
        self.db.con.close()
        for ext in ('', '-wal', '-shm'):
            if os.path.exists(self.path + ext):
                os.remove(self.path + ext)

    def run_app(self, tracker, t0, t1, mode, ma, slot=0):
        samples = [sample(t, slot, mode, ma, t - t0) for t in range(t0, t1, 10)]
        for s in samples:
            tracker.feed(s)
        self.db.add_samples(samples)
        self.db.save_dirty(tracker)

    def test_finished_cell_left_in_the_slot(self):
        tr = Tracker()
        self.run_app(tr, 1000, 2000, 3, 500)              # charging
        self.run_app(tr, 2000, 3000, 4, 0)                # done, the cell stays in the slot
        self.run_app(tr, 1000, 1500, 3, 500, slot=1)      # another slot: charged earlier, taken out
        self.run_app(tr, 1500, 1600, 0, 0, slot=1)
        sid = tr.current[(1, 0)].db_id
        self.db.set_session_meta(sid, battery_id=7)
        self.db.con.commit()
        self.assertEqual(self.db.con.execute('SELECT status FROM sessions WHERE id=?', (sid,)).fetchone()[0],
                         FINISHED)
        tr = Tracker()                                    # restart
        self.db.resume(tr)
        self.run_app(tr, 3100, 3500, 4, 0)                # still "done" after the restart
        rows = self.db.con.execute('SELECT slot, battery_id FROM sessions ORDER BY slot').fetchall()
        self.assertEqual(rows, [(0, 7), (1, None)])       # no second session, battery kept, nothing doubled


if __name__ == '__main__':
    unittest.main()
