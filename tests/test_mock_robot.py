"""Unit tests for mock_robot — the fake Groot2 publisher used to drive klein in
tests. Verifies the status animation and reply framing, and round-trips its
output through the real gateway parser."""
import os
import struct
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import mock_robot
from klein.gateway import KleinGateway
from klein.groot2_protocol import HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, STATUS_RECORD_SIZE


class StatusBufferTest(unittest.TestCase):
    def parse(self, tick):
        return KleinGateway.parse_status(mock_robot.build_status_buffer(tick))

    def test_buffer_covers_every_uid(self):
        buf = mock_robot.build_status_buffer(0)
        self.assertEqual(len(buf), len(mock_robot.ALL_UIDS) * STATUS_RECORD_SIZE)
        self.assertEqual(set(self.parse(0)), set(mock_robot.ALL_UIDS))

    def test_cursor_leaf_and_ancestors_run(self):
        parsed = self.parse(0)                       # EXEC_ORDER[0] == 2, ancestors == [1]
        self.assertEqual(parsed[2]["status"], "RUNNING")
        self.assertEqual(parsed[1]["status"], "RUNNING")
        self.assertEqual(parsed[12]["status"], "IDLE")   # not yet reached

    def test_failing_leaf_reports_failure(self):
        fail_tick = mock_robot.EXEC_ORDER.index(5)   # the "Retry" node fails on its lap
        self.assertEqual(self.parse(fail_tick)[5]["status"], "FAILURE")

    def test_finished_leaves_show_transition(self):
        parsed = self.parse(5)                       # cursor past nodes 2 and 5
        self.assertEqual(parsed[2], {"status": "IDLE", "from": "SUCCESS"})
        self.assertEqual(parsed[5], {"status": "IDLE", "from": "FAILURE"})


class ReplyHeaderTest(unittest.TestCase):
    def test_echoes_request_and_appends_uuid(self):
        request = struct.pack(HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, 424242)
        header = mock_robot.reply_header(request)
        self.assertEqual(len(header), 22)            # 6-byte echo + 16-byte tree UUID
        proto, req_type, unique_id = struct.unpack(HEADER_FORMAT, header[:6])
        self.assertEqual((proto, req_type, unique_id), (PROTOCOL_ID, REQ_STATUS, 424242))
        self.assertEqual(header[6:], bytes(range(16)))

    def test_short_request_frame_is_tolerated(self):
        header = mock_robot.reply_header(b"")
        self.assertEqual(len(header), 22)
        proto, req_type, unique_id = struct.unpack(HEADER_FORMAT, header[:6])
        self.assertEqual((proto, req_type, unique_id), (PROTOCOL_ID, 0, 0))


if __name__ == "__main__":
    unittest.main()
