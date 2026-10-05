"""Backups: writing a copy while the database is open, and which old ones are kept (db.prune_backups).

Run: python -m unittest discover tests
"""
import datetime
import gzip
import os
import shutil
import sqlite3
import tempfile
import unittest

from battbench.db import backup_files, prune_backups, write_backup


class Prune(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, 'battbench.db')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def make(self, times):
        for t in times:
            open(f"{self.db}-{t:%Y%m%d-%H%M%S}.gz", 'wb').close()

    def kept(self):
        return [t for t, _f in backup_files(self.db)]

    def test_rules(self):
        start = datetime.datetime(2026, 1, 1, 0, 0, 0)
        times = [start + datetime.timedelta(hours=6 * i) for i in range(4 * 400)]     # every 6 h for 400 days
        self.make(times)
        prune_backups(self.db, keep=10, daily=20, weekly=8, monthly=24)
        kept = self.kept()
        newest = sorted(times, reverse=True)
        self.assertEqual(kept[:10], newest[:10])                                       # the last 10
        days = {t.date() for t in kept}
        self.assertTrue(all((newest[0] - datetime.timedelta(days=d)).date() in days for d in range(20)))
        weeks = {t.isocalendar()[:2] for t in kept}
        self.assertGreaterEqual(len(weeks), 8)
        months = {(t.year, t.month) for t in kept}
        self.assertEqual(len(months), 14)                                              # all 14 months there are
        for t in kept:                                                                 # a period keeps its newest
            if t.date() < newest[9].date():
                self.assertEqual(t.hour, 18)
        self.assertLess(len(kept), 10 + 20 + 8 + 24)

    def test_keep_only_last(self):
        start = datetime.datetime(2026, 3, 1, 12, 0, 0)
        self.make([start + datetime.timedelta(minutes=i) for i in range(30)])
        prune_backups(self.db, keep=5, daily=0, weekly=0, monthly=0)
        self.assertEqual(len(self.kept()), 5)


class Write(unittest.TestCase):
    def test_copy_while_open(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, 'x.db')
            con = sqlite3.connect(path)
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('CREATE TABLE t (v)')
            con.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(1000)])
            con.commit()                                        # still open, data partly only in the WAL
            target = write_backup(path)
            out = os.path.join(d, 'restored.db')
            with gzip.open(target, 'rb') as a, open(out, 'wb') as b:
                shutil.copyfileobj(a, b)
            r = sqlite3.connect(out)
            self.assertEqual(r.execute('SELECT COUNT(*) FROM t').fetchone()[0], 1000)
            r.close()
            con.close()
            self.assertEqual(os.listdir(d).count(os.path.basename(target) + '.tmp'), 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
