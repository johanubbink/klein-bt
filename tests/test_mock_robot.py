"""Unit tests for klein.mock_robot — the fake Groot2 publisher used to drive
klein in tests. Verifies the status animation and reply framing, and round-trips
its output through the real gateway parser."""
import struct
import unittest

from klein import mock_robot
from klein.gateway import KleinGateway
from klein.groot2_protocol import HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, STATUS_RECORD_SIZE


class StatusBufferTest(unittest.TestCase):
    def parse(self, tick):
        return KleinGateway.parse_status(mock_robot.build_status_buffer(tick))

    def statuses_over_cycle(self, uid):
        """Every status ``uid`` takes across one full mission cycle."""
        return {self.parse(t)[uid]["status"] for t in range(mock_robot.CYCLE_TICKS)}

    def test_buffer_covers_every_uid(self):
        buf = mock_robot.build_status_buffer(0)
        self.assertEqual(len(buf), len(mock_robot.ALL_UIDS) * STATUS_RECORD_SIZE)
        self.assertEqual(set(self.parse(0)), set(mock_robot.ALL_UIDS))

    def test_mission_starts_with_script_running(self):
        parsed = self.parse(0)                       # first frame: Script + its Sequence parent
        self.assertEqual(parsed[2]["status"], "RUNNING")     # Script
        self.assertEqual(parsed[1]["status"], "RUNNING")     # mission Sequence
        self.assertEqual(parsed[13]["status"], "IDLE")       # PassThroughDoor not yet reached

    def test_every_status_int_decodes(self):
        # No frame should ever emit an UNKNOWN status across the whole cycle.
        for t in range(mock_robot.CYCLE_TICKS):
            for entry in self.parse(t).values():
                self.assertNotEqual(entry["status"], "UNKNOWN")

    def test_opendoor_fails_then_picklock_runs_and_succeeds(self):
        self.assertIn("FAILURE", self.statuses_over_cycle(9))    # OpenDoor: locked -> FAILURE
        pick = self.statuses_over_cycle(11)                      # PickLock retries...
        self.assertIn("RUNNING", pick)
        self.assertIn("FAILURE", pick)                           # ...failing early attempts...
        self.assertIn("SUCCESS", pick)                           # ...then cracking it

    def test_smashdoor_branch_never_taken(self):
        self.assertEqual(self.statuses_over_cycle(12), {"IDLE"})  # PickLock always wins first

    def test_reset_frame_flags_last_result(self):
        # The reset frame is the 6-tick window just before the final idle pause.
        reset_tick = mock_robot.CYCLE_TICKS - 12 - 1
        parsed = self.parse(reset_tick)
        self.assertEqual(parsed[1], {"status": "IDLE", "from": "SUCCESS"})   # mission succeeded
        self.assertEqual(parsed[9], {"status": "IDLE", "from": "FAILURE"})   # OpenDoor had failed


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
