"""Unit tests for klein.btlog — FileLogger2 ``.btlog`` read/write.

Checked against the two real fixtures (one written by Groot2, one by
BehaviorTree.CPP's own FileLogger2) and against the harness's independent
reference reader and writer, which share no code with klein (and which the
fixtures check in turn).
"""
import os
import unittest

from klein import mock_robot
from klein.btlog import BtlogError, read_btlog, write_btlog
from tests.harness import btlog_ref

FIXTURES = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


GROOT2 = _fixture("groot2_mock.btlog")
T11 = _fixture("t11_filelogger2.btlog")
FIXTURE_FILES = {"groot2_mock.btlog": GROOT2, "t11_filelogger2.btlog": T11}


class FixtureTest(unittest.TestCase):
    def test_reading_a_fixture_agrees_with_the_reference_and_rewrites_byte_for_byte(self):
        for name, data in FIXTURE_FILES.items():
            with self.subTest(name):
                log, ref = read_btlog(data), btlog_ref.parse(data)
                self.assertEqual(log.xml, ref.xml)
                self.assertEqual(log.first_timestamp, ref.first_timestamp_us)
                self.assertEqual(log.records, ref.records)
                self.assertEqual(log.trailing, ref.trailing)
                self.assertEqual(write_btlog(log.xml, log.first_timestamp, log.records), data)
                # The reference writer rebuilds it byte for byte too.
                self.assertEqual(btlog_ref.build(ref.xml, ref.first_timestamp_us, ref.records,
                                                 version=ref.version), data)
        ref = btlog_ref.parse(GROOT2)               # Groot2's file of the mock's tree
        self.assertEqual((ref.version, len(ref.xml), len(ref.records), ref.trailing),
                         (1, 2183, 616, 0))
        self.assertEqual(ref.xml, mock_robot.CROSSDOOR_XML)


class DamagedFileTest(unittest.TestCase):
    def test_a_partial_last_record_is_counted_and_a_bad_xml_length_rejected(self):
        with self.subTest("partial last record"):
            log = read_btlog(GROOT2[:-4])
            self.assertEqual(len(log.records), 615)
            self.assertEqual(log.records, read_btlog(GROOT2).records[:615])
            self.assertEqual(log.trailing, 5)
        with self.subTest("xml length past the end"):
            with self.assertRaises(BtlogError):
                read_btlog(GROOT2[:19] + len(GROOT2).to_bytes(4, "little") + GROOT2[23:])


class RoundTripTest(unittest.TestCase):
    def test_extreme_fields_survive_a_round_trip(self):
        records = [(0, 0, 1), (1, 65535, 2), (2 ** 48 - 1, 65535, 0), (2 ** 48 - 1, 7, 4)]
        xml = '<root BTCPP_format="4"><BehaviorTree ID="é"/></root>'   # non-ASCII: bytes != chars
        data = write_btlog(xml, 1_790_000_000_000_000, records)
        self.assertEqual(read_btlog(data), (xml, 1_790_000_000_000_000, records, 0))
        self.assertEqual(btlog_ref.parse(data).records, records)


if __name__ == "__main__":
    unittest.main()
