"""Unit tests for klein.mock_robot — the fake publisher used to drive
klein in tests. Verifies the status animation, blackboard content and reply
framing, and round-trips its output through the real gateway parser.

Most classes here are about CrossDoor, the mission the mock publishes by
default. ``EveryTreeTest`` holds the invariants that must be true of *any* tree
the mock can publish, so the patrol tree it swaps to stays as honest as the one
it swaps from.
"""
import json
import os
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from klein import layout, mock_robot
from tests.harness import btlog_ref
from tests.helpers import collect_uids, new_gateway
from klein.groot2_protocol import (
    HEADER_FORMAT,
    NODE_CATEGORIES,
    PROTOCOL_ID,
    REQ_STATUS,
    STATUS_RECORD_SIZE,
    TRANSITION_BUFFER_MAX,
    NodeStatus,
    decode_transitions,
    parse_blackboard,
    parse_status,
)


class StatusBufferTest(unittest.TestCase):
    def parse(self, tick):
        return parse_status(mock_robot.build_status_buffer(tick, mock_robot.CROSSDOOR))

    def statuses_over_cycle(self, uid):
        """Every status ``uid`` takes across one full mission cycle."""
        return {self.parse(t)[uid]["status"] for t in range(mock_robot.CROSSDOOR.cycle_ticks)}

    def test_crossdoor_story(self):
        parsed = self.parse(0)                       # first frame: Script + its Sequence parent
        self.assertEqual(parsed[2]["status"], "RUNNING")     # Script
        self.assertEqual(parsed[1]["status"], "RUNNING")     # mission Sequence
        self.assertEqual(parsed[13]["status"], "IDLE")       # PassThroughDoor not yet reached

        self.assertIn("FAILURE", self.statuses_over_cycle(9))    # OpenDoor: locked -> FAILURE
        pick = self.statuses_over_cycle(11)                      # PickLock retries...
        self.assertIn("RUNNING", pick)
        self.assertIn("FAILURE", pick)                           # ...failing early attempts...
        self.assertIn("SUCCESS", pick)                           # ...then cracking it
        self.assertEqual(self.statuses_over_cycle(12), {"IDLE"})  # SmashDoor: PickLock always wins first

        # The reset frame is the 6-tick window just before the final idle pause.
        reset_tick = mock_robot.CROSSDOOR.cycle_ticks - 12 - 1
        parsed = self.parse(reset_tick)
        self.assertEqual(parsed[1], {"status": "IDLE", "from": "SUCCESS"})   # mission succeeded
        self.assertEqual(parsed[9], {"status": "IDLE", "from": "FAILURE"})   # OpenDoor had failed


class BlackboardTest(unittest.TestCase):
    """The mock's blackboards: type coverage, evolution, and reply encoding."""

    def board(self, tick, name="MainTree"):
        return mock_robot.CROSSDOOR.blackboard(tick)[name]

    def values_over_cycle(self, key, name="MainTree"):
        return {self.board(t, name)[key] for t in range(mock_robot.CROSSDOOR.cycle_ticks)}

    def test_covers_the_value_shapes_a_robot_can_send(self):
        board = self.board(0)
        self.assertIsInstance(board["door_open"], int)           # bools arrive as ints
        self.assertIsInstance(board["mission_phase"], str)
        self.assertIsInstance(board["robot_position"], list)     # vector
        self.assertEqual(board["target_pose"]["__type"], "Pose2D")   # registered struct
        self.assertIsNone(board["last_error"])                   # declared but unset

        # ROS messages the renderers summarize, nested all the way down as
        # BehaviorTree.CPP serializes them: the renderers key on the tag at every level.
        path = board["path"]
        self.assertEqual(path["__type"], "nav_msgs::msg::Path")
        self.assertEqual(path["header"]["__type"], "std_msgs::msg::Header")
        self.assertEqual(path["header"]["stamp"]["__type"], "builtin_interfaces::msg::Time")
        self.assertGreater(len(path["poses"]), 1)     # a length needs two points
        first = path["poses"][0]
        self.assertEqual(first["__type"], "geometry_msgs::msg::PoseStamped")
        self.assertEqual(first["pose"]["position"]["__type"], "geometry_msgs::msg::Point")
        self.assertEqual(first["pose"]["orientation"]["__type"],
                         "geometry_msgs::msg::Quaternion")
        self.assertEqual(board["goal"]["__type"], "geometry_msgs::msg::PoseStamped")
        self.assertEqual(board["heading"]["__type"], "geometry_msgs::msg::Quaternion")

        # DBL_MAX is what a "no limit" double port reports; the dashboard shows
        # it as a sentinel rather than 1.7976931348623157e+308.
        self.assertEqual(board["distance_to_end_of_route"], sys.float_info.max)
        # And an accumulated distance keeps the noise the formatting hides.
        noisy = [self.board(t)["distance_to_goal"] for t in range(mock_robot.CROSSDOOR.cycle_ticks)]
        self.assertTrue(any(len(repr(value)) > 6 for value in noisy), noisy[:5])

    def test_values_evolve_across_the_mission(self):
        self.assertEqual(self.values_over_cycle("door_open"), {0, 1})
        self.assertEqual(
            self.values_over_cycle("pick_attempts", "DoorClosed::7"),
            {0, 1, 2, 3, 4, 5},
        )
        self.assertEqual(self.values_over_cycle("lock_status", "DoorClosed::7"),
                         {"locked", "picked"})
        self.assertNotEqual(self.board(0)["tick"], self.board(1)["tick"])

        def remaining(tick):
            poses = self.board(tick)["path"]["poses"]
            return poses[-1]["pose"]["position"]["x"] - poses[0]["pose"]["position"]["x"]
        self.assertLess(remaining(20), remaining(1))   # the path shortens as the robot advances

    def test_blackboard_tracks_the_status_animation(self):
        # door_open flips exactly when the tryOpen Fallback (uid 8) reports
        # SUCCESS — the blackboard and the node colors read the same frame.
        for tick in range(mock_robot.CROSSDOOR.cycle_ticks):
            status = parse_status(mock_robot.build_status_buffer(tick, mock_robot.CROSSDOOR))
            if status[8]["status"] == "SUCCESS":
                self.assertEqual(self.board(tick)["door_open"], 1, f"tick {tick}")

    def test_reply_round_trips_through_the_gateway_parser(self):
        def reply(request):
            return mock_robot.build_blackboard_reply(request, 0, mock_robot.CROSSDOOR)

        cases = [
            ("both boards", [b"header", b"MainTree;DoorClosed::7"], ["DoorClosed::7", "MainTree"]),
            ("unknown names dropped", [b"header", b"MainTree;Nope"], ["MainTree"]),
            ("no match", [b"header", b"Nope;AlsoNope"], []),
            ("no argument frame", [b"header"], []),
        ]
        for label, request, names in cases:
            with self.subTest(label):
                self.assertEqual(sorted(parse_blackboard(reply(request))), names)
        with self.subTest("no match is msgpack nil"):
            self.assertEqual(reply([b"header", b"Nope;AlsoNope"]), b"\xc0")
        with self.subTest("private key stripped"):
            # The gateway strips the mock's private key before the browser sees it.
            self.assertIn("_debug_internal", mock_robot.CROSSDOOR.blackboard(0)["MainTree"])
            boards = parse_blackboard(reply([b"header", b"MainTree"]))
            self.assertNotIn("_debug_internal", boards["MainTree"])
        with self.subTest("ros messages survive json"):
            board = parse_blackboard(reply([b"header", b"MainTree"]))["MainTree"]
            json.dumps(board)      # must not raise: this is what reaches the browser
            self.assertEqual(board["path"]["__type"], "nav_msgs::msg::Path")
            self.assertEqual(board["distance_to_end_of_route"], sys.float_info.max)


class ReplyHeaderTest(unittest.TestCase):
    def test_echoes_request_and_appends_uuid(self):
        # main() draws a fresh random UUID on every tree swap, the way a
        # restarted Groot2Publisher does — that is the signal klein watches.
        swapped = b"\xaa" * 16
        self.assertNotEqual(swapped, mock_robot.DEFAULT_TREE_UUID)
        cases = [
            ("echo", struct.pack(HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, 424242), None,
             (PROTOCOL_ID, REQ_STATUS, 424242), bytes(range(16))),
            ("overridden uuid", struct.pack(HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, 1), swapped,
             (PROTOCOL_ID, REQ_STATUS, 1), swapped),
            ("short request", b"", None, (PROTOCOL_ID, 0, 0), bytes(range(16))),
        ]
        for label, request, uuid, fields, expected_uuid in cases:
            with self.subTest(label):
                if uuid is None:
                    header = mock_robot.reply_header(request)
                else:
                    header = mock_robot.reply_header(request, uuid)
                self.assertEqual(len(header), 22)            # 6-byte echo + 16-byte tree UUID
                self.assertEqual(struct.unpack(HEADER_FORMAT, header[:6]), fields)
                self.assertEqual(header[6:], expected_uuid)


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def run_mission(recorder, clock, ticks):
    """Feed ``ticks`` CrossDoor frames to ``recorder``, 100 ms apart."""
    tree = mock_robot.CROSSDOOR
    for tick in range(ticks):
        clock.now += 0.1
        recorder.record(mock_robot._frame_at(tick, tree)[0],
                        mock_robot._frame_at(tick + 1, tree)[0], tree.uids)


class TransitionRecorderTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.recorder = mock_robot.TransitionRecorder(clock=self.clock,
                                                      wall_clock=lambda: 1_700_000_000.5)

    def test_start_drain_stop_lifecycle(self):
        run_mission(self.recorder, self.clock, mock_robot.CROSSDOOR.cycle_ticks)
        self.assertEqual(self.recorder.drain(), b"")                    # nothing until started
        self.recorder.start()
        run_mission(self.recorder, self.clock, 10)
        self.assertNotEqual(self.recorder.drain(), b"")
        self.assertEqual(self.recorder.drain(), b"")                    # drain empties the buffer
        run_mission(self.recorder, self.clock, 10)
        self.recorder.start()
        self.assertEqual(self.recorder.drain(), b"")                    # start clears
        self.recorder.stop()
        run_mission(self.recorder, self.clock, 10)
        self.assertEqual(self.recorder.drain(), b"")                    # stop halts

    def test_a_full_cycle_records_every_node_the_mission_touches(self):
        self.recorder.start()
        run_mission(self.recorder, self.clock, mock_robot.CROSSDOOR.cycle_ticks)
        records = decode_transitions(self.recorder.drain())
        uids = {uid for _, uid, _ in records}
        self.assertEqual(uids, set(mock_robot.CROSSDOOR.uids) - {12})   # SmashDoor never runs
        pick_lock = [st for _, uid, st in records if uid == 11]
        self.assertEqual(pick_lock.count(NodeStatus.FAILURE), 4)        # four failed attempts
        self.assertEqual(pick_lock[-1], NodeStatus.IDLE)                # the reset, as plain 0
        # Timestamps are relative to start and ordered.
        stamps = [ts for ts, _, _ in records]
        self.assertEqual(stamps, sorted(stamps))
        self.assertLess(stamps[-1], mock_robot.CROSSDOOR.cycle_ticks * 100_000 + 1000)

    def test_finishing_nodes_come_deepest_first_then_starting_root_first(self):
        # PickLock's failed attempt -> its retry: the tick order a robot records.
        self.recorder.start()
        self.recorder.record({8: 1, 10: 1, 11: 3}, {8: 1, 10: 1, 11: 1, 12: 1}, range(1, 14))
        self.recorder.record({1: 1, 4: 1, 9: 3}, {1: 2, 4: 2, 9: 12, 13: 1, 14: 1}, range(1, 15))
        records = decode_transitions(self.recorder.drain())
        self.assertEqual([(uid, st) for _, uid, st in records],
                         [(11, 1), (12, 1),                         # only starts: root-first
                          (9, 0), (4, 2), (1, 2), (13, 1), (14, 1)])  # ends deepest-first, then starts

    def test_the_buffer_drops_the_oldest_beyond_its_cap(self):
        self.recorder.start()
        before, after = {1: 1}, {1: 2}
        for i in range(TRANSITION_BUFFER_MAX + 50):
            self.clock.now += 0.001
            self.recorder.record(before, after, [1])
            before, after = after, before
        records = decode_transitions(self.recorder.drain())
        self.assertEqual(len(records), TRANSITION_BUFFER_MAX)
        self.assertGreater(records[0][0], 50 * 1000)                    # first 50 were dropped


class ToggleRecordingTest(unittest.TestCase):
    def test_toggle_recording(self):
        cases = [
            # label, started, request, reply, recording afterwards
            ("start replies with the time as a decimal string",
             False, [b"hdr", b"start"], [b"12000000"], True),
            ("stop replies with the header alone", True, [b"hdr", b"stop"], [], False),
            ("a missing argument is an error", False, [b"hdr"], None, None),
        ]
        for label, started, request, reply, recording in cases:
            with self.subTest(label):
                # A frozen monotonic clock too: the reply is the wall-clock anchor
                # advanced by it, and a real one would tick between the two reads.
                recorder = mock_robot.TransitionRecorder(clock=FakeClock(),
                                                         wall_clock=lambda: 12.0)
                if started:
                    recorder.start()
                self.assertEqual(mock_robot.toggle_recording(request, recorder), reply)
                if recording is not None:
                    self.assertEqual(recorder.recording, recording)


class TruthLogTest(unittest.TestCase):
    """``--truth-log`` sees every transition, on the recording's own clock."""

    def test_truth_is_the_recording_offset_by_the_start_reply(self):
        clock, truth = FakeClock(), []
        recorder = mock_robot.TransitionRecorder(clock=clock, wall_clock=lambda: 1000.0,
                                                 truth=truth.extend)
        run_mission(recorder, clock, 10)
        self.assertTrue(truth)                         # recorded whether or not a client records
        self.assertEqual(recorder.drain(), b"")
        clock.now += 0.05
        start = recorder.start()
        before = len(truth)
        run_mission(recorder, clock, 20)
        drained = [(start + ts, uid, st) for ts, uid, st in decode_transitions(recorder.drain())]
        self.assertEqual(drained, truth[before:])

    def test_truth_log_writes_parseable_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "truth.log")
            log = mock_robot.TruthLog(path)
            log.publisher("crossdoor", bytes(16), 5)
            log([(10, 1, 1), (15, 2, 2)])
            log.close()
            with open(path) as f:
                lines = f.read().splitlines()
        self.assertEqual(lines, ["# publisher crossdoor uuid=" + "00" * 16 + " t=5",
                                 "10 1 1", "15 2 2"])


REPLAY_XML = ('<root BTCPP_format="4"><BehaviorTree ID="M" _fullpath="M">'
              '<Sequence _uid="1"><AlwaysSuccess _uid="2"/></Sequence>'
              '</BehaviorTree></root>')
REPLAY_RECORDS = [(1000, 1, 1), (1000, 2, 2), (1010, 2, 0), (500_000, 1, 2), (500_020, 1, 0)]
# Built by the harness's reference writer, so ``--replay`` is checked against
# the format rather than against klein.btlog's writer.
REPLAY_FILE = btlog_ref.build(REPLAY_XML, 7, REPLAY_RECORDS)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.truth = []
        self.recorder = mock_robot.TransitionRecorder(clock=self.clock,
                                                      wall_clock=lambda: 1000.0,
                                                      truth=self.truth.extend)
        self.replay = mock_robot.Replay(REPLAY_FILE, self.recorder)
        self.t0 = self.recorder.now_us()

    def status(self):
        return parse_status(self.replay.status_buffer())

    def test_reads_the_file_and_ignores_a_partial_trailing_record(self):
        self.assertEqual(self.replay.xml, REPLAY_XML)
        self.assertEqual(self.replay.uids, [1, 2])
        replay = mock_robot.Replay(REPLAY_FILE + b"\x01\x02", self.recorder)
        self.assertEqual(replay._records, REPLAY_RECORDS)

    def test_rejects_what_is_not_a_btlog(self):
        with self.assertRaises(ValueError):
            mock_robot.Replay(b"BTCPP4-FileLogger1\x01", self.recorder)
        with self.assertRaises(ValueError):
            mock_robot.Replay(btlog_ref.build(REPLAY_XML, 7, []), self.recorder)

    def test_status_follows_the_publisher_rule(self):
        self.clock.now += 0.0011                     # past the first three records
        self.replay.advance()
        self.assertEqual(self.status(), {1: {"status": "RUNNING", "from": None},
                                         2: {"status": "IDLE", "from": "SUCCESS"}})
        self.clock.now += 0.5
        self.replay.advance()
        self.assertEqual(self.status()[1], {"status": "IDLE", "from": "SUCCESS"})

    def test_emits_the_file_in_real_time_and_loops(self):
        period = REPLAY_RECORDS[-1][0] + mock_robot.Replay.LOOP_GAP_USEC
        self.clock.now += (period + 1500) / 1e6      # one lap, then the next lap's first two
        self.replay.advance()
        expected = ([(self.t0 + ts, uid, st) for ts, uid, st in REPLAY_RECORDS]
                    + [(self.t0 + period + ts, uid, st) for ts, uid, st in REPLAY_RECORDS[:3]])
        self.assertEqual(self.truth, expected)

    def test_a_client_recording_drains_what_the_truth_saw(self):
        self.clock.now += 0.0005
        self.replay.advance()
        start = self.recorder.start()
        self.clock.now += 0.6
        self.replay.advance()
        drained = [(start + ts, uid, st)
                   for ts, uid, st in decode_transitions(self.recorder.drain())]
        self.assertEqual(drained, self.truth)        # nothing was due before the start


class EveryTreeTest(unittest.TestCase):
    """Invariants that hold for every tree the mock can publish.

    Written over ``TREES`` rather than over CrossDoor, so the second tree — the
    one that exists to let a tree *swap* be watched without a real robot — is
    held to the same standard, and so a third would be too.
    """

    def test_status_buffers_are_well_formed(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                buf = mock_robot.build_status_buffer(0, tree)
                self.assertEqual(len(buf), len(tree.uids) * STATUS_RECORD_SIZE)
                self.assertEqual(set(parse_status(buf)), set(tree.uids))
                # Over two laps, so the wrap from one lap into the next is covered.
                prev = buf
                for tick in range(1, 2 * tree.cycle_ticks):
                    cur = mock_robot.build_status_buffer(tick, tree)
                    for entry in parse_status(cur).values():
                        self.assertNotEqual(entry["status"], "UNKNOWN", f"tick {tick}")
                    # A was-flag never falls back to plain IDLE.
                    for (uid, a), (_, b) in zip(struct.iter_unpack("<HB", prev),
                                                struct.iter_unpack("<HB", cur)):
                        self.assertFalse(a >= 10 and b == 0, f"uid {uid} at tick {tick}")
                    prev = cur

    def test_layout_matches_the_declarations(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                gw = new_gateway(self)
                gw._parse_layout(tree.xml)
                uids = collect_uids(gw.tree_structure)
                self.assertEqual(sorted(uids), sorted(tree.uids))

                root = ET.fromstring(tree.xml)
                self.assertEqual(list(tree.blackboard(0)), tree.blackboard_names)
                self.assertEqual(layout.extract_blackboard_names(root),
                                 tree.blackboard_names)
                # Each key a node reads or writes is served on the board BT.CPP
                # would keep it on (remapped keys on the parent's).
                boards = tree.blackboard(0)
                nodes = [gw.tree_structure]
                while nodes:
                    node = nodes.pop()
                    nodes.extend(node["children"])
                    for b in node["bindings"]:
                        self.assertIn(b["key"], boards[b["board"]], (node["uid"], b))

                # Every node type is declared in the model, in the shared vocabulary.
                model = layout.parse_node_categories(root)
                self.assertTrue(set(model.values()) <= NODE_CATEGORIES)
                used = {
                    node.tag
                    for block in root.findall(".//BehaviorTree")
                    for node in block.iter()
                    if node.tag != "BehaviorTree"
                }
                self.assertEqual(used - set(model), set())
                if tree is mock_robot.CROSSDOOR:
                    # All five, so a robot-free run exercises every style the dashboard draws.
                    self.assertEqual(set(model.values()), NODE_CATEGORIES)

    def test_the_swap_is_meaningful(self):
        # If the two trees were the same, a swap between them would prove nothing.
        crossdoor, patrol = mock_robot.CROSSDOOR, mock_robot.PATROL
        self.assertNotEqual(crossdoor.xml, patrol.xml)
        self.assertNotEqual(crossdoor.blackboard_names, patrol.blackboard_names)
        self.assertNotEqual(len(crossdoor.uids), len(patrol.uids))
        # ...while still reusing UIDs, so a status painted on the wrong tree's
        # cards would show.
        self.assertTrue(set(crossdoor.uids) & set(patrol.uids))
        # Swapping *back* is a new publisher too, so it must be a real switch.
        self.assertIs(mock_robot._next_tree(crossdoor), patrol)
        self.assertIs(mock_robot._next_tree(patrol), crossdoor)

    def test_status_is_the_recorded_transitions_under_the_publisher_rule(self):
        # What a robot would report: every transition the mock records, applied
        # with the publisher's rule (the harness's independent implementation),
        # must give the STATUS bytes at every tick of three laps.
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                truth = []
                recorder = mock_robot.TransitionRecorder(clock=FakeClock(),
                                                         truth=truth.extend)
                state = {uid: 0 for uid in tree.uids}
                before = {}                             # the tree starts all IDLE
                mismatches = []
                for tick in range(3 * tree.cycle_ticks):
                    after = mock_robot._frame_at(tick, tree)[0]
                    del truth[:]
                    recorder.record(before, after, tree.uids)
                    for _, uid, status in truth:
                        btlog_ref.apply(state, uid, status)
                    reported = dict(struct.iter_unpack(
                        "<HB", mock_robot.build_status_buffer(tick, tree)))
                    if reported != state:
                        mismatches.append((tick, reported, dict(state)))
                    before = after
                self.assertEqual(len(mismatches), 0, mismatches[:3])


if __name__ == "__main__":
    unittest.main()
