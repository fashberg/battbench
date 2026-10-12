"""MC5000 program / stop packets against packets the SkyRC app sent (btsnoop captures documented by
rssdev10/skyrc-mc-rs, docs/PROTOCOL.md, with their checksums).

Run: python -m unittest discover tests
"""
import unittest

from battbench import mc5000
from battbench.mc5000 import Program


def hx(s):
    return bytes.fromhex(s.replace(' ', ''))


CAPTURED = [
    # Li-Ion charge, slot 3, 3 A, 5000 mAh
    (0x04, Program(chem='LiIon', mode='Charge', charge_ma=3000, discharge_ma=2000, capacity=5000, charge_mv=4200,
                   discharge_mv=3200),
     '0f 2a 94 04 00 0b b8 07 d0 13 88 10 68 0c 80 00 64 00 64 00 0a 00 0a 01 00 06 00 00 00 3c 00 00 01 2c 00 00 '
     '0e d8 00 00 00 00 00 03'),
    # NiMH charge, slot 1, 1 A, 3000 mAh
    (0x01, Program(chem='NiMH', mode='Charge', charge_ma=1000, discharge_ma=1000, capacity=3000),
     '0f 2a 94 01 00 03 e8 03 e8 0b b8 06 72 03 84 00 64 00 64 00 0a 00 0a 01 00 06 00 00 00 3c 00 00 01 2c 00 03 '
     '0c e4 00 00 00 00 00 6c'),
    # NiMH charge with trickle 50 mA, restart 1.3 V, timer 90 min
    (0x01, Program(chem='NiMH', mode='Charge', charge_ma=300, discharge_ma=1000, capacity=3000, trickle_ma=50,
                   restart_mv=1300, timer=90),
     '0f 2a 94 01 00 01 2c 03 e8 0b b8 06 72 03 84 00 64 00 64 00 0a 00 0a 01 00 06 05 05 14 3c 00 5a 01 2c 00 03 '
     '0c e4 00 00 00 00 00 26'),
    # Li-Ion storage, slot 4, timer 120 min, discharge end 90 mA
    (0x08, Program(chem='LiIon', mode='Storage', charge_ma=700, discharge_ma=800, capacity=5000, charge_mv=4200,
                   discharge_mv=3200, discharge_end_ma=90, timer=120),
     '0f 2a 94 08 01 02 bc 03 20 13 88 10 68 0c 80 00 64 00 5a 00 0a 00 0a 01 00 06 00 00 00 3c 00 78 01 2c 00 00 '
     '0e d8 00 00 00 00 00 bd'),
    # Li-Ion charge for all slots (mask 0)
    (0x00, Program(chem='LiIon', mode='Charge', charge_ma=300, discharge_ma=2000, capacity=5000, charge_mv=4200,
                   discharge_mv=3200),
     '0f 2a 94 00 00 01 2c 07 d0 13 88 10 68 0c 80 00 64 00 64 00 0a 00 0a 01 00 06 00 00 00 3c 00 00 01 2c 00 00 '
     '0e d8 00 00 00 00 00 69'),
    # RAM charge, slot 4
    (0x08, Program(chem='RAM', mode='Charge', charge_ma=1000, discharge_ma=1000, capacity=3000),
     '0f 2a 94 08 00 03 e8 03 e8 0b b8 06 72 03 84 00 64 00 64 00 0a 00 0a 01 00 06 00 00 00 3c 00 00 01 2c 00 07 '
     '0c e4 00 00 00 00 00 77'),
]


class Packets(unittest.TestCase):
    def test_captured_programs(self):
        for mask, prog, want in CAPTURED:
            with self.subTest(prog=prog):
                self.assertEqual(mc5000._program(mask, prog).hex(' '), hx(want).hex(' '))
                if mask:
                    self.assertEqual(mc5000.program_packet(mask, prog), mc5000._program(mask, prog))

    def test_cycle_direction_and_mode(self):
        p = Program(chem='NiMH', mode='Cycle', cycles=2, direction='D>C')
        b = mc5000.program_packet(0x04, p)
        self.assertEqual((b[4], b[23], b[24]), (4, 2, 1))     # mode byte as in the status answer, cycles, D>C
        self.assertEqual(mc5000.program_packet(0x04, Program(chem='LiIon', mode='Cycle', charge_mv=4200,
                                                             discharge_mv=3200))[4], 3)

    def test_stop_one_slot(self):
        self.assertEqual(mc5000.stop_packet(0x04).hex(' '), '0f 03 fe 04 02')
        for bad in (0, 0x03, 0x10):           # 00 = all slots; with more bits the charger takes the lowest only
            with self.assertRaises(ValueError):
                mc5000.stop_packet(bad)
            with self.assertRaises(ValueError):
                mc5000.program_packet(bad, Program(chem='NiMH', mode='Charge'))

    def test_status(self):
        idle = hx('0f 16 91 01 00 00 05 14 00 00 00 00 00 00 00 00 00 00 00 00 00 03 00 00')
        charging = hx('0f 16 91 01 02 b8 05 52 00 00 00 82 00 00 02 ed 00 2a 02 04 00 03 00 47')
        stopped = hx('0f 16 91 01 00 00 05 14 00 00 00 00 00 00 00 00 00 00 00 00 04 03 00 00')
        self.assertFalse(mc5000.working(idle))
        self.assertTrue(mc5000.working(charging))
        self.assertTrue(mc5000.working(stopped))         # error shown: cleared by the stop
        self.assertTrue(mc5000.started(charging))
        self.assertFalse(mc5000.started(idle))

    def test_summary(self):                   # answers of an MC5000 2.02, slot 4 cycling with 1.0 A / 0.5 A
        self.assertEqual(mc5000.summary_packet(0x08).hex(' '), '0f 03 5f 08 67')
        d = mc5000.parse_summary(hx('0f0c5f08020405040a0500000085'))
        self.assertEqual(d, dict(status=2, mode=4, charge_ma=1000, discharge_ma=500))
        self.assertIsNone(mc5000.parse_summary(hx('0f0c5f08020405040a0500000086')))     # checksum
        p = Program(chem='NiMH', mode='Cycle', charge_ma=1050, discharge_ma=500)
        self.assertTrue(mc5000.same_currents(p, d))
        p.discharge_ma = 600
        self.assertFalse(mc5000.same_currents(p, d))

    def test_settings(self):                  # read before / after changing them in the SkyRC app
        before = mc5000.parse_settings(hx('0f099608000000010100a0'))
        self.assertEqual(before, mc5000.Settings(0, 0, 0, 1, 1, 0))
        after = mc5000.parse_settings(hx('0f099608020100020103a7'))
        self.assertEqual(after, mc5000.Settings(key_beep=2, notify_beep=1, done_tone=0, backlight=2, other=1,
                                                timeout=3))
        self.assertEqual(mc5000.settings_query().hex(' '), '0f 03 96 04 9a')
        self.assertEqual(mc5000.settings_packet(after).hex(' '), '0f 09 95 04 02 01 00 02 01 03 a2')
        with self.assertRaises(ValueError):
            mc5000.settings_packet(mc5000.Settings(timeout=2))      # not a choice of the charger

    def test_ack(self):
        self.assertTrue(mc5000.acked(hx('0f 04 94 01 01 96'), 0x94))
        self.assertTrue(mc5000.acked(hx('0f 04 94 10 01 a5'), 0x94))
        self.assertFalse(mc5000.acked(hx('0f 04 94 01 00 95'), 0x94))
        self.assertFalse(mc5000.acked(hx('0f 04 94 01 01 97'), 0x94))

    def test_defaults_and_problems(self):
        for chem in mc5000.CHEMISTRIES:
            p = mc5000.defaults(chem)
            self.assertEqual(p.problems(), [], chem)
            self.assertEqual(len(mc5000.program_packet(1, p)), 44)
        self.assertIn('task not available for this battery type', Program(chem='NiZn', mode='Storage').problems())
        d = Program(chem='NiMH', cycles=3).to_dict()
        self.assertEqual(Program.from_dict(dict(d, unknown=1)), Program(chem='NiMH', cycles=3))


    def test_app_defaults(self):            # as the SkyRC app fills in a new program
        p = mc5000.defaults('Eneloop', 'Charge', 3)                             # Pro/XX AAA
        self.assertEqual((p.capacity, p.charge_ma, p.discharge_ma, p.eneloop), (900, 450, 200, 3))
        self.assertEqual(mc5000.program_packet(1, p)[3 + 35], 3)                # Eneloop type byte
        p = mc5000.defaults('Eneloop', 'Break in', 0)                           # Std AA: 0.1 C / 0.2 C
        self.assertEqual((p.capacity, p.charge_ma, p.discharge_ma), (2000, 200, 400))
        p = mc5000.defaults('NiMH', 'Break in')
        self.assertEqual((p.capacity, p.charge_ma, p.discharge_ma), (3000, 300, 600))
        p = mc5000.defaults('LiIon')
        self.assertEqual((p.capacity, p.charge_ma, p.discharge_ma, p.storage_mv), (5000, 3000, 2000, 3800))
        self.assertEqual(mc5000.program_packet(1, mc5000.defaults('NiMH'))[3 + 35], 0)

    def test_choices(self):
        self.assertEqual(mc5000.choices('restart_mv', 'NiMH')[:3], [0, 1300, 1310])     # off, then from 1.3 V
        self.assertEqual(mc5000.choices('charge_ma', 'NiMH')[13:16], [700, 750, 800])
        self.assertEqual(mc5000.choices('discharge_mv', 'NiMH'), [600, 700, 800, 900, 1000])
        for chem in mc5000.CHEMISTRIES:                 # the defaults are among the choices
            d = mc5000.defaults(chem)
            for attr in ('charge_ma', 'discharge_ma', 'capacity', 'charge_mv', 'discharge_mv', 'restart_mv',
                         'cycles', 'delta_peak', 'trickle_ma', 'timer'):
                self.assertIn(getattr(d, attr), mc5000.choices(attr, chem), (chem, attr))


class Status(unittest.TestCase):
    def test_from_status(self):
        # real MC5000 answers: NiMH cycle (charging), empty slot
        self.assertEqual(mc5000.from_status(hx('0f16910102b8055200000082000002ed002a020400030047')), ('NiMH', 'Cycle'))
        self.assertIsNone(mc5000.from_status(hx('0f0406010108')))

    def test_same(self):
        a = Program(name='a', chem='NiMH', cycles=2)
        self.assertTrue(mc5000.same(a, Program(name='b', chem='NiMH', cycles=2)))
        self.assertFalse(mc5000.same(a, Program(name='a', chem='NiMH', cycles=3)))


if __name__ == '__main__':
    unittest.main()
