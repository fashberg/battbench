"""Binary answer of a BattBench-ESP's /api/records?fmt=bin (device_net.decode_records)."""
import struct
import unittest

from battbench.device_net import decode_records


class DecodeRecords(unittest.TestCase):
    def test_segments_full_and_delta(self):
        data = (b'BBR1' + struct.pack('<IIBI', 0x1234ABCD, 300, 1, 4)
                + b'\x80' + struct.pack('<HBqII', 2, 1, 17_900_000_000, 10, 50)
                + bytes([0x41, 0, 0, 3, 1, 2, 3])                 # slot 1: full packet
                + bytes([0x01, 1, 20, 0b100, 9])                  # +1, +2 s: byte 2 changed
                + bytes([0x01, 0xC8, 0x01, 0, 0b001, 7])          # +200 (two byte varint): byte 0 changed
                + b'\x80' + struct.pack('<HBqII', 3, 0, 0, 250, 5)   # next boot, clock unknown
                + bytes([0x49, 0, 0, 2, 0xAA, 0xBB])              # charger 1, slot 1: full packet
                + b'\xff')
        d = decode_records(data)
        self.assertEqual((d['logid'], d['seq_next'], d['more']), ('1234abcd', 300, True))
        self.assertEqual(d['records'], [
            [10, 1790000005.0, 0, 1, bytes([1, 2, 3])],
            [11, 1790000007.0, 0, 1, bytes([1, 2, 9])],
            [211, 1790000007.0, 0, 1, bytes([7, 2, 9])],
            [250, None, 1, 1, bytes([0xAA, 0xBB])]])

    def test_not_binary(self):
        with self.assertRaises(ValueError):
            decode_records(b'{"logid": "1"}')


if __name__ == '__main__':
    unittest.main()
