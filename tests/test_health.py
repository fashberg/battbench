"""Health index of NiMH cells (model.health) and the voltage curve the tracker evaluates.

Run: python -m unittest discover tests
"""
import unittest

from battbench.device import Sample
from battbench.model import (CATEGORIES, FINISHED, Tracker, discharge_curve, health, score_capacity, score_efficiency,
                             score_resistance, score_voltage)

A, B, C, D = CATEGORIES


class Scores(unittest.TestCase):
    def test_capacity(self):
        self.assertEqual(score_capacity(95), 100)
        self.assertEqual(score_capacity(80), 75)
        self.assertEqual(score_capacity(60), 30)
        self.assertEqual(score_capacity(40), 0)

    def test_resistance_on_charger_scale(self):
        self.assertEqual(score_resistance(140, 'NiMH'), 100)
        self.assertEqual(score_resistance(300, 'NiMH'), 75)
        self.assertEqual(score_resistance(450, 'NiMH'), 40)
        self.assertEqual(score_resistance(600, 'NiMH'), 0)
        self.assertEqual(score_resistance(900, 'NiMH'), 0)
        self.assertEqual(score_resistance(75, 'LiIon'), 87.5)

    def test_efficiency(self):
        self.assertEqual(score_efficiency(80), 100)
        self.assertEqual(score_efficiency(70), 85)
        self.assertEqual(score_efficiency(55), 30)
        self.assertEqual(score_efficiency(95), 50)

    def test_voltage(self):
        self.assertEqual(score_voltage(1300, 1250, False), 100)
        self.assertAlmostEqual(score_voltage(1100, 1150, True), 100 - 10 - 7.5 - 30)


class Health(unittest.TestCase):
    def test_new_eneloop(self):
        """Specification case 1: new eneloop, 2000 mAh, 20 mOhm (4-wire) - about 140 mOhm on a charger."""
        h = health(1950, 2000, 2440, 140, 'NiMH', 1350, 1260, False)
        self.assertEqual(h['category'], A)
        self.assertGreaterEqual(h['ohi'], 95)

    def test_aged_high_resistance(self):
        """Case 2: aged cell, 150 mOhm (4-wire) - about 520 mOhm on a charger: no longer high drain."""
        h = health(1700, 2000, 2100, 520, 'NiMH', 1250, 1180, False)
        self.assertIn(h['category'], (B, C))
        self.assertLess(h['scores']['resistance'], 40)

    def test_poor_efficiency(self):
        """Case 3: high self-discharge / poor efficiency (below 60 %)."""
        h = health(1500, 2000, 2600, 250, 'NiMH', 1300, 1220, False)
        self.assertAlmostEqual(h['scores']['efficiency'], 70 - (65 - 100 * 1500 / 2600) * 4)    # 57.7 % -> 40.8
        self.assertNotEqual(h['category'], A)

    def test_worn_out(self):
        h = health(600, 2000, 700, 400, 'NiMH', 1200, 1050, True)
        self.assertEqual(h['category'], D)

    def test_missing_scores_are_left_out(self):
        h = health(1900, 2000, None, None, 'NiMH')
        self.assertEqual(set(h['scores']), {'capacity'})
        self.assertEqual(h['ohi'], 100)
        self.assertEqual(h['category'], B)       # high drain needs a known low resistance

    def test_no_nominal(self):
        self.assertIsNone(health(1900, 0, 2200, 150, 'NiMH'))


def sample(t, mode, mv, ma, mah, res=180, raw='df00'):
    return Sample(t=t, slot=0, mode=mode, mode_str='', chem='NiMH', size='AA', mv=mv, ma=ma, res=res, mah=mah,
                  secs=int(t), temp=30, itemp=0, progress=0, power=0, energy=0, raw=raw, dev=1)


class Curve(unittest.TestCase):
    def test_discharge_curve(self):
        # 500 mA for 4 h: 2000 mAh, voltage falling linearly from 1.35 V to 1.10 V
        pts = [(t, 1350 - 250 * t // 14400, -500) for t in range(0, 14401, 10)]
        c = discharge_curve(pts)
        self.assertAlmostEqual(c['v_start'], 1338, delta=2)
        self.assertAlmostEqual(c['v_mid'], 1225, delta=2)
        self.assertFalse(c['early_drop'])
        self.assertIsNone(discharge_curve(pts[:5]))

    def test_tracker_evaluates_the_last_discharge(self):
        tr = Tracker()
        t = 0
        for _ in range(360):                     # charge 1 h
            tr.feed(sample(t, 11, 1400, 1000, t // 4))
            t += 10
        for i in range(1440):                    # discharge 4 h at 500 mA
            tr.feed(sample(t, 11, 1350 - 250 * i // 1440, -500, -(i * 10 * 500 // 3600)))
            t += 10
        for _ in range(360):                     # charge again, then done and taken out
            tr.feed(sample(t, 11, 1420, 1000, (t % 3600) // 4))
            t += 10
        tr.feed(sample(t, 12, 1420, 0, 2400))
        tr.feed(sample(t + 10, 0, 0, 0, 0, res=0))
        s = tr.closed[-1]
        self.assertEqual(s.status, FINISHED)
        self.assertAlmostEqual(s.v_start, 1338, delta=3)
        self.assertAlmostEqual(s.v_mid, 1225, delta=3)
        self.assertFalse(s.early_drop)
        self.assertFalse(s.res_est)
        self.assertEqual(s.points, [])

    def test_estimated_resistance_is_not_rated(self):
        tr = Tracker()
        for t in range(0, 100, 10):
            tr.feed(sample(t, 3, 1400, 400, t, res=900, raw='e502'))
        s = tr.current[(1, 0)]
        self.assertTrue(s.res_est)
        self.assertNotEqual(s.rating()[0], 'suspicious')


if __name__ == '__main__':
    unittest.main()
