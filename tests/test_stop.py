"""A task the charger stops with an error (MC5000: capacity limit) ends as 'aborted' with the reason, not as 'removed'.

Run: python -m unittest discover tests
"""
import os
import tempfile
import unittest

from battbench.db import DB
from battbench.device_skyrc import SkyCharger, checksum
from battbench.model import ABORTED, CAPACITY_LIMIT, PAUSE, REMOVED, TASKS, Tracker, note_text, rate_values


def mc5000(slot, ma, mv, mah, secs, status, mode=4, err=0, btype=3):
    """MC5000 status answer as a real charger sends it (24 bytes, 0F 16 91 ...)."""
    p = bytearray([0x0F, 0x16, 0x91, 1 << slot]) + ma.to_bytes(2, 'big') + mv.to_bytes(2, 'big') + bytes(2) + \
        mah.to_bytes(2, 'big') + secs.to_bytes(4, 'big') + (47).to_bytes(2, 'big') + bytes([status, mode, err, btype, 0])
    return bytes(p + bytes([checksum(p[2:])]))


class Stop(unittest.TestCase):
    def setUp(self):
        self.ch = SkyCharger('DC:23:53:64:2A:08', 'MC5000')

    def feed(self, tracker, packets, t0=1000.0):
        for i, p in enumerate(packets):
            s = self.ch.sample(0, p, 1)
            self.assertIsNotNone(s)
            s.t = t0 + i * 10
            tracker.feed(s)

    def discharge(self, n=20):
        return [mc5000(0, 1000, 1200 - i, 100 * i, 12000 + 10 * i, 3) for i in range(1, n)]

    def test_capacity_limit(self):
        # seen on a real MC5000 (cycle, NiMH): status 0 + error 4 with mAh / time reset, stale current; then idle
        stopped = mc5000(0, 128, 909, 0, 0, 0, err=4)
        s = self.ch.sample(0, stopped, 1)
        self.assertEqual((s.mode, s.mode_str, s.ma), (20, CAPACITY_LIMIT, 0))
        tr = Tracker()
        self.feed(tr, self.discharge() + [stopped, stopped, mc5000(0, 0, 1058, 0, 0, 0)])
        (sess,) = tr.closed
        self.assertEqual((sess.status, sess.stop_reason, sess.discharge_mah), (ABORTED, CAPACITY_LIMIT, 1900))
        self.assertEqual([p.kind for p in sess.phases], ['discharge'])
        note = sess.rating()[1]
        self.assertIn('stopped by the charger: capacity limit reached', note)
        self.assertIn('more than 1900 mAh', note)

    def test_pause_between_phases(self):
        # MC5000 status 4: the rest set between charge and discharge; the task goes on
        s = self.ch.sample(0, mc5000(0, 0, 946, 2391, 13136, 4), 1)
        self.assertEqual((TASKS.get(s.mode), s.mode_str, s.ma), ('cycle', PAUSE, 0))
        tr = Tracker()
        self.feed(tr, self.discharge() + [mc5000(0, 0, 946, 2391, 13136 + i, 4) for i in range(3)])
        self.assertEqual((tr.closed, len(tr.current)), ([], 1))

    def test_taken_out(self):
        tr = Tracker()
        self.feed(tr, self.discharge() + [mc5000(0, 0, 0, 0, 0, 0)])
        (sess,) = tr.closed
        self.assertEqual((sess.status, sess.stop_reason), (REMOVED, ''))

    def test_contact_lost_is_removed(self):
        # seen on a real MC5000: the cell taken out during the rest -> error 9 "contact lost", then empty
        tr = Tracker()
        self.feed(tr, self.discharge() + [mc5000(0, 0, 0, 0, 0, 0, err=9)] * 2 + [mc5000(0, 0, 0, 0, 0, 0)])
        (sess,) = tr.closed
        self.assertEqual((sess.status, sess.stop_reason), (REMOVED, ''))

    def test_error_without_task_opens_no_session(self):
        tr = Tracker()
        self.feed(tr, [mc5000(0, 128, 909, 0, 0, 0, err=4)] * 3)
        self.assertEqual(tr.sessions(), [])

    def test_stored_and_upgraded(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            db = DB(path)
            db.con.execute("INSERT INTO devices (id, key, model, name, slots) VALUES (1, 'k', 'MC5000', 'MC5000', 4)")
            tr = Tracker()
            self.feed(tr, self.discharge() + [mc5000(0, 0, 909, 0, 0, 0, err=4)] * 2 + [mc5000(0, 0, 1058, 0, 0, 0)])
            db.save_dirty(tr)
            d = db.session_dict(db.sessions()[0][0])
            self.assertEqual((d['status'], d['stop_reason']), (ABORTED, CAPACITY_LIMIT))
            # a session stored as 'removed' by an older version is found by its error readings (upgrade 13)
            sid = d['id']
            db.con.execute("UPDATE sessions SET status=?, stop_reason='' WHERE id=?", (REMOVED, sid))
            err = self.ch.sample(0, mc5000(0, 0, 909, 0, 0, 0, err=4), 1)
            err.t = 1015.0
            db.add_samples([err])
            db.con.execute('PRAGMA user_version = 12')
            db.con.commit()
            db.con.close()
            db = DB(path)
            d = db.session_dict(sid)
            self.assertEqual((d['status'], d['stop_reason']), (ABORTED, CAPACITY_LIMIT))
            self.assertIn('stopped by the charger', d['note'])
            db.con.close()
        finally:
            for ext in ('', '-wal', '-shm'):
                if os.path.exists(path + ext):
                    os.remove(path + ext)

    def test_note_translated_keys(self):
        notes = rate_values(2400, 2000, None, 47, ABORTED, stop_reason=CAPACITY_LIMIT)[1]
        self.assertIn('stopped by the charger: CAPACITY', note_text(notes, lambda x: x.upper() if x == CAPACITY_LIMIT
                                                                     else x))


if __name__ == '__main__':
    unittest.main()
