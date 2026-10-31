"""Unit tests for klein.recorder — the recording session, one call at a time,
against a ``FakeRobot`` link and a fake clock: arming, drains, overflow,
outages, swaps, a publisher that can't record and blackboard samples.

No pollers run here; when the gateway calls the recorder is tested end to end
in test_gateway.py (``RecordingGatewayTest``).
"""
import contextlib
import io
import unittest

from klein.groot2_protocol import TRANSITION_BUFFER_MAX
from klein.layout import parse_layout
from klein.recorder import Recorder
from klein.recording import Recording
from klein.robot_link import RobotTimeout
from tests.helpers import MODEL_LESS_XML, FakeRobot, status_record


class RecorderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fresh()

    def fresh(self, **caps):
        """A recorder of a new ``Recording(**caps)``, over a new robot."""
        self.robot = FakeRobot(limit=10_000)
        self.now = 50.0                     # klein's monotonic clock, s
        self.rec = Recording(**caps)
        self.recorder = Recorder(self.rec, self.robot, clock=lambda: self.now)
        self.tree = parse_layout(self.robot.xml, 1)

    async def arm(self, tree=None):
        with contextlib.redirect_stderr(io.StringIO()):     # "robot cannot record"
            await self.recorder.arm(tree or self.tree)

    async def drain(self, *drains):
        """One ``drain`` per entry, the robot answering ``t`` with it."""
        self.robot.drains = list(drains)
        for _ in drains:
            await self.recorder.drain()

    def seq(self):
        return "".join(self.robot.sent)

    def runs(self):
        return [[s.id for s in run] for run in self.rec.runs()]

    async def test_arming_takes_a_baseline_and_each_drain_appends_on_robot_time(self):
        # uid 99 is past the tree: not in the baseline.
        self.robot.status = status_record(1, 2) + status_record(13, 1) + status_record(99, 1)
        # The `r start` round trip takes 2 ms: its reply is taken to be read
        # at the midpoint, klein's 50.001 s.
        self.robot.on_letter["r"] = lambda _r: setattr(self, "now", self.now + 0.002)
        await self.arm()
        self.assertEqual(self.seq(), "rS")
        self.assertTrue(self.recorder.armed)
        self.assertEqual(self.recorder.state, "on")
        (segment,) = self.rec.segments
        start = self.robot.starts[0]
        self.assertEqual(segment.t_begin, start)
        self.assertIs(segment.layout, self.tree)
        self.assertEqual(segment.baseline, bytes([0, 2] + [0] * 11 + [1]))  # max uid 13, plus 0
        await self.drain([(10, 1, 1), (15, 2, 1)])
        self.assertEqual(self.rec.head, start + 1_000)      # klein's 50.002 s
        self.now = 51.501
        await self.drain([(40, 2, 2), (41, 2, 0)])
        self.assertEqual(self.seq(), "rStt")
        self.assertEqual(list(s[1:] for s in segment.iter_records(0, segment.head_seq)),
                         [(start + 10, 1, 1), (start + 15, 2, 1),
                          (start + 40, 2, 2), (start + 41, 2, 0)])
        self.assertEqual(segment.state[2], 12)          # IDLE after SUCCESS
        self.assertEqual(self.rec.head, start + 1_500_000)
        self.assertEqual(self.rec.gaps, [])

    async def test_a_full_drain_records_an_overflow_gap_and_re_arms(self):
        await self.arm()
        burst = [(1000 + i, 1 + i % 13, 1 + i % 3) for i in range(TRANSITION_BUFFER_MAX)]
        await self.drain([(5, 1, 1)], burst, [(3, 4, 1)])
        self.assertEqual(self.seq(), "rS" "t" "t" "rS" "t")
        first, second = self.rec.segments
        start = self.robot.starts[0]
        self.assertEqual(first.head_seq, 1 + TRANSITION_BUFFER_MAX)
        self.assertEqual(self.rec.gaps, [(start + 5, start + 1000, "overflow")])
        self.assertEqual(first.t_end, second.t_begin)
        self.assertEqual(second.t_begin, self.robot.starts[1])
        self.assertEqual(self.runs(), [[0, 1]])         # the same tree: one run
        # From the re-arm's own baseline, on its own t_begin.
        self.assertEqual(list(s[1:] for s in second.iter_records(0, second.head_seq)),
                         [(self.robot.starts[1] + 3, 4, 1)])

    async def test_an_outage_ends_the_segment_at_the_head_and_the_next_arm_adds_a_gap(self):
        async def lost(recorder):
            recorder.robot_lost()

        async def timeout(recorder):
            recorder.note_timeout()

        async def good_poll(recorder):
            await recorder.drain()

        cases = [
            ("a status poll timed out", [lost], False, True),
            # A blackboard poll timed out, and the robot is back as a new
            # publisher before any status poll did.
            ("a timeout, then the next handshake", [timeout], True, True),
            ("a timeout, then a good status poll", [timeout, good_poll], True, False),
            ("a restart quick enough that nothing timed out", [], True, False),
        ]
        for label, steps, armed, outage in cases:
            with self.subTest(label):
                self.fresh()
                await self.arm()
                self.now += 0.25
                await self.drain([(5, 1, 1)])
                head = self.rec.head
                for step in steps:
                    await step(self.recorder)
                self.assertEqual(self.recorder.armed, armed)
                await self.arm()                        # the re-handshake
                first, second = self.rec.segments
                self.assertIs(first.layout, second.layout)
                self.assertEqual(self.runs(), [[0, 1]])
                if outage:
                    self.assertEqual(first.t_end, head)
                    self.assertEqual(self.rec.gaps, [(head, second.t_begin, "outage")])
                else:
                    self.assertEqual(first.t_end, second.t_begin)
                    self.assertEqual(self.rec.gaps, [])

    async def test_a_new_tree_starts_a_new_run(self):
        await self.arm()
        await self.drain([(5, 1, 1)])
        swap = parse_layout(MODEL_LESS_XML, 2)
        self.robot.status = b"".join(status_record(uid, 0) for uid in range(1, 6))
        await self.arm(swap)
        first, second = self.rec.segments
        self.assertIs(second.layout, swap)
        self.assertEqual(first.t_end, second.t_begin)
        self.assertEqual(len(second.baseline), 6)
        self.assertEqual(self.runs(), [[0], [1]])
        self.assertEqual(self.rec.gaps, [])

    async def test_a_publisher_that_cannot_record_gets_no_t_until_the_next_handshake(self):
        await self.arm()
        self.now += 0.5
        await self.drain([(5, 1, 1)])
        self.robot.supports_recording = False           # a new publisher, BT.CPP < 4.3.3
        await self.arm()
        self.assertEqual(self.recorder.state, "unsupported")
        self.assertFalse(self.recorder.armed)
        (first,) = self.rec.segments
        self.assertEqual(first.t_end, self.robot.starts[0] + 500_000)  # the robot's now
        await self.drain([(9, 1, 2)])
        self.assertEqual(self.seq(), "rSt" "r")
        self.robot.supports_recording = True
        await self.arm()
        self.assertEqual(self.recorder.state, "on")
        self.assertEqual(self.rec.gaps, [(first.t_end, self.robot.starts[1], "outage")])
        off = Recorder(None, self.robot)                # --record-buffer 0
        await off.arm(self.tree)
        self.assertEqual((off.state, self.seq()), ("off", "rSt" "r" "rS"))

    async def test_an_arm_that_fails_leaves_the_recorder_unarmed(self):
        cases = [("baseline refused", "refusals", 2, None),
                 ("r timed out", "timeouts", 1, RobotTimeout),
                 ("baseline timed out", "timeouts", 2, RobotTimeout)]
        for label, fault, request, raises in cases:
            with self.subTest(label):
                self.fresh()
                getattr(self.robot, fault).add(request)
                if raises is None:
                    await self.arm()
                else:
                    with self.assertRaises(raises):
                        await self.arm()
                self.assertFalse(self.recorder.armed)
                self.assertEqual(self.recorder.state, "on")
                self.assertEqual(self.rec.segments, [])
                await self.drain([(5, 1, 1)])
                self.assertNotIn("t", self.seq())

    async def test_blackboard_samples_are_on_robot_time_and_evicted_at_once(self):
        cap = 6                             # bytes: the base "n" and a change or two
        self.fresh(bb_max_bytes=cap)
        self.recorder.add_blackboard(self.now, {"MainTree": {"n": -1}})    # not armed: dropped
        self.assertEqual(self.rec.bytes_used()[1], 0)
        await self.arm()
        for i in range(20):
            self.now += 0.5
            self.recorder.add_blackboard(self.now, {"MainTree": {"n": i}})
            # At once, not at the next drain.
            self.assertLessEqual(self.rec.bytes_used()[1], cap, i)
        self.assertTrue(self.rec.blackboard_capped)
        changes = self.rec.segments[0].blackboard.changes()
        self.assertEqual(changes[-1], (self.robot.starts[0] + 10_000_000, "MainTree", "n", "19"))


if __name__ == "__main__":
    unittest.main()
