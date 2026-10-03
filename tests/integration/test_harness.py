"""The wire tap and the mock target, against real processes.

``WireTap`` must decode recording traffic the same way the client receiving it
does, and ``MockTarget``'s truth log must be what the mock actually served,
across tree swaps and when its first port was taken. The oracles' own tests
are in ``tests/unit/test_oracles.py``; the browser probe's in
``tests/ui/test_probes.py``.
"""
import socket
import struct
import unittest

from klein.groot2_protocol import decode_transitions
from tests.harness import btlog_ref
from tests.harness.oracles import transitions_match
from tests.harness.probes import WireTap
from tests.harness.targets import MockTarget, read_truth_log, robot_request


class WireTapTest(unittest.TestCase):
    def test_decodes_recording_traffic_both_ways(self):
        with MockTarget() as robot, WireTap(robot.port) as tap:
            reply = robot_request(tap.port, "r", b"start")
            for _ in range(15):
                robot_request(tap.port, "S")
            drained = robot_request(tap.port, "t")
            robot_request(tap.port, "r", b"stop")
        self.assertEqual(tap.request_sequence(), "r" + "S" * 15 + "tr")
        start = tap.requests("r")[0]
        self.assertEqual(start["arg"], "start")
        self.assertEqual(start["reply"]["payload"], reply[1].decode())
        # The tap's own ZMTP decoding agrees with what the client received.
        self.assertEqual(tap.transitions(), decode_transitions(drained[1]))
        self.assertTrue(tap.transitions())
        # ...and with the robot's ground truth, record for record.
        klein_view = [(int(reply[1]) + ts, uid, st) for ts, uid, st in tap.transitions()]
        result = transitions_match(klein_view, robot.ground_truth(), max_offset_us=0,
                                   from_time=int(reply[1]))
        self.assertTrue(result, result.detail)


class MockTargetTest(unittest.TestCase):
    def _status_versus_truth(self, robot, polls):
        """Poll S ``polls`` times; before each, replay the truth written so far —
        from all-IDLE at the latest publisher marker — and compare. Returns the
        mismatches. A B request after each S is a barrier: the mock records a
        step's transitions after its S reply, before it answers anything else."""
        mismatches = []
        for k in range(polls):
            records, markers = read_truth_log(robot.truth_path)
            reply = robot_request(robot.port, "S")
            reported = dict(struct.iter_unpack("<HB", reply[1]))
            state = {uid: 0 for uid in reported}
            for _, uid, st in records[markers[-1]["index"]:]:
                btlog_ref.apply(state, uid, st)
            if state != reported:
                mismatches.append((k, reported, state))
            robot_request(robot.port, "B", b"MainTree")
        return mismatches

    def test_truth_reproduces_status_across_swaps_after_a_port_retry(self):
        # What free_port's release-then-bind window allows: someone else holds
        # the port by the time the mock binds it, so it retries on a fresh one.
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            taken = holder.getsockname()[1]
            robot = MockTarget(switch_every=20)
            robot.port = taken
            with robot:
                self.assertNotEqual(robot.port, taken)
                mismatches = self._status_versus_truth(robot, 70)
                names = [m["name"] for m in robot.publishers()]
        self.assertIn("already in use", robot.log())
        self.assertEqual(mismatches, [], mismatches[:2])
        self.assertEqual(names, ["crossdoor", "patrol", "crossdoor", "patrol"])


if __name__ == "__main__":
    unittest.main()
