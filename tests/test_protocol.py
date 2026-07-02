"""Unit tests for klein.groot2_protocol — the shared wire-protocol constants
and the NodeStatus decode rule."""
import struct
import unittest

from klein.groot2_protocol import (
    HEADER_FORMAT,
    IDLE_TRANSITION,
    PROTOCOL_ID,
    REQ_FULLTREE,
    REQ_STATUS,
    STATUS_RECORD_FORMAT,
    STATUS_RECORD_SIZE,
    NodeStatus,
    decode_status,
)


class ConstantsTest(unittest.TestCase):
    def test_wire_constants_match_btcpp(self):
        self.assertEqual(PROTOCOL_ID, 2)
        self.assertEqual(REQ_FULLTREE, ord("T"))
        self.assertEqual(REQ_STATUS, ord("S"))

    def test_struct_formats(self):
        self.assertEqual(struct.calcsize(HEADER_FORMAT), 6)         # u8 u8 u32
        self.assertEqual(struct.calcsize(STATUS_RECORD_FORMAT), 3)  # u16 u8
        self.assertEqual(STATUS_RECORD_SIZE, 3)

    def test_node_status_values(self):
        self.assertEqual(
            [NodeStatus.IDLE, NodeStatus.RUNNING, NodeStatus.SUCCESS,
             NodeStatus.FAILURE, NodeStatus.SKIPPED],
            [0, 1, 2, 3, 4],
        )


class DecodeStatusTest(unittest.TestCase):
    def test_live_statuses(self):
        self.assertEqual(decode_status(0), ("IDLE", None))
        self.assertEqual(decode_status(1), ("RUNNING", None))
        self.assertEqual(decode_status(2), ("SUCCESS", None))
        self.assertEqual(decode_status(3), ("FAILURE", None))
        self.assertEqual(decode_status(4), ("SKIPPED", None))

    def test_idle_transitions_cover_full_range(self):
        # 10..14 all mean "now IDLE, previously X" for X in 0..4.
        self.assertEqual(decode_status(IDLE_TRANSITION + 0), ("IDLE", "IDLE"))
        self.assertEqual(decode_status(IDLE_TRANSITION + 1), ("IDLE", "RUNNING"))
        self.assertEqual(decode_status(IDLE_TRANSITION + 2), ("IDLE", "SUCCESS"))
        self.assertEqual(decode_status(IDLE_TRANSITION + 3), ("IDLE", "FAILURE"))
        self.assertEqual(decode_status(IDLE_TRANSITION + 4), ("IDLE", "SKIPPED"))

    def test_unknown_values(self):
        self.assertEqual(decode_status(7), ("UNKNOWN", None))     # 5..9 gap
        self.assertEqual(decode_status(99), ("UNKNOWN", None))    # transition of a non-status
        self.assertEqual(decode_status(255), ("UNKNOWN", None))


if __name__ == "__main__":
    unittest.main()
