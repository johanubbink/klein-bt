"""Unit tests for klein.mock_robot — the fake Groot2 publisher used to drive
klein in tests. Verifies the status animation, blackboard content and reply
framing, and round-trips its output through the real gateway parser."""
import json
import struct
import sys
import unittest
import xml.etree.ElementTree as ET

from klein import mock_robot
from klein.gateway import KleinGateway
from klein.groot2_protocol import (
    HEADER_FORMAT,
    PROTOCOL_ID,
    REQ_BLACKBOARD,
    REQ_FULLTREE,
    REQ_STATUS,
    STATUS_RECORD_SIZE,
)


def _request(req_type, unique_id=424242):
    return struct.pack(HEADER_FORMAT, PROTOCOL_ID, req_type, unique_id)


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


class BlackboardTest(unittest.TestCase):
    """The mock's blackboards: type coverage, evolution, and reply encoding."""

    def board(self, tick, name="MainTree"):
        return mock_robot.build_blackboard(tick)[name]

    def values_over_cycle(self, key, name="MainTree"):
        return {self.board(t, name)[key] for t in range(mock_robot.CYCLE_TICKS)}

    def test_serves_the_names_the_layout_advertises(self):
        self.assertEqual(
            list(mock_robot.build_blackboard(0)),
            mock_robot.BLACKBOARD_NAMES,
        )
        # ...and those are exactly the names klein derives from the same XML.
        self.assertEqual(
            KleinGateway.extract_blackboard_names(ET.fromstring(mock_robot.TREE_XML)),
            mock_robot.BLACKBOARD_NAMES,
        )

    def test_covers_the_value_shapes_a_robot_can_send(self):
        board = self.board(0)
        self.assertIsInstance(board["door_open"], int)           # bools arrive as ints
        self.assertIsInstance(board["mission_phase"], str)
        self.assertIsInstance(board["robot_position"], list)     # vector
        self.assertEqual(board["target_pose"]["__type"], "Pose2D")   # registered struct
        self.assertIsNone(board["last_error"])                   # declared but unset

    def test_values_evolve_across_the_mission(self):
        self.assertEqual(self.values_over_cycle("door_open"), {0, 1})
        self.assertEqual(
            self.values_over_cycle("pick_attempts", "DoorClosed::7"),
            {0, 1, 2, 3, 4, 5},
        )
        self.assertEqual(self.values_over_cycle("lock_status", "DoorClosed::7"),
                         {"locked", "picked"})
        self.assertNotEqual(self.board(0)["tick"], self.board(1)["tick"])

    def test_blackboard_tracks_the_status_animation(self):
        # door_open flips exactly when the tryOpen Fallback (uid 8) reports
        # SUCCESS — the blackboard and the node colors read the same frame.
        for tick in range(mock_robot.CYCLE_TICKS):
            status = KleinGateway.parse_status(mock_robot.build_status_buffer(tick))
            if status[8]["status"] == "SUCCESS":
                self.assertEqual(self.board(tick)["door_open"], 1, f"tick {tick}")

    def test_reply_round_trips_through_the_gateway_parser(self):
        request = [b"header", b"MainTree;DoorClosed::7"]
        boards = KleinGateway.parse_blackboard(mock_robot.build_blackboard_reply(request, 0))
        self.assertEqual(sorted(boards), ["DoorClosed::7", "MainTree"])
        # The gateway strips the mock's private key before the browser sees it.
        self.assertIn("_debug_internal", mock_robot.build_blackboard(0)["MainTree"])
        self.assertNotIn("_debug_internal", boards["MainTree"])

    def test_unknown_names_are_dropped_silently(self):
        boards = KleinGateway.parse_blackboard(
            mock_robot.build_blackboard_reply([b"header", b"MainTree;Nope"], 0)
        )
        self.assertEqual(list(boards), ["MainTree"])

    def test_no_match_yields_msgpack_nil(self):
        raw = mock_robot.build_blackboard_reply([b"header", b"Nope;AlsoNope"], 0)
        self.assertEqual(raw, b"\xc0")                       # msgpack nil
        self.assertEqual(KleinGateway.parse_blackboard(raw), {})

    def test_missing_argument_frame_is_tolerated(self):
        raw = mock_robot.build_blackboard_reply([b"header"], 0)
        self.assertEqual(KleinGateway.parse_blackboard(raw), {})

    def test_carries_ros_messages_the_renderers_summarize(self):
        board = self.board(0)
        path = board["path"]
        self.assertEqual(path["__type"], "nav_msgs::msg::Path")
        # Nested all the way down, as BehaviorTree.CPP serializes it: the
        # dashboard's renderers key on the tag at every level.
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

    def test_carries_both_unreadable_float_cases(self):
        # DBL_MAX is what a "no limit" double port reports; the dashboard shows
        # it as a sentinel rather than 1.7976931348623157e+308.
        self.assertEqual(self.board(0)["distance_to_end_of_route"], sys.float_info.max)
        # And an accumulated distance keeps the noise the formatting hides.
        noisy = [self.board(t)["distance_to_goal"] for t in range(mock_robot.CYCLE_TICKS)]
        self.assertTrue(any(len(repr(value)) > 6 for value in noisy), noisy[:5])

    def test_the_path_shortens_as_the_robot_advances(self):
        def remaining(tick):
            poses = self.board(tick)["path"]["poses"]
            return poses[-1]["pose"]["position"]["x"] - poses[0]["pose"]["position"]["x"]
        self.assertLess(remaining(20), remaining(1))

    def test_ros_messages_survive_the_gateway_json_conversion(self):
        raw = mock_robot.build_blackboard_reply([b"header", b"MainTree"], 0)
        board = KleinGateway.parse_blackboard(raw)["MainTree"]
        json.dumps(board)      # must not raise: this is what reaches the browser
        self.assertEqual(board["path"]["__type"], "nav_msgs::msg::Path")
        self.assertEqual(board["distance_to_end_of_route"], sys.float_info.max)


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

    def test_carries_the_publisher_uuid_it_is_given(self):
        header = mock_robot.reply_header(_request(REQ_STATUS), tree_uuid=b"\x07" * 16)
        self.assertEqual(header[6:], b"\x07" * 16)


class MockPublisherTest(unittest.TestCase):
    """A MockPublisher stands for one Groot2Publisher instance: its own UUID,
    its own tree, UIDs from 1 — so swapping publishers is what a tree switch on
    Nav2's bt_navigator looks like on the wire."""

    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    def test_each_instance_mints_its_own_uuid(self):
        a = mock_robot.MockPublisher(*mock_robot.TREES[0])
        b = mock_robot.MockPublisher(*mock_robot.TREES[0])
        self.assertEqual(len(a.tree_uuid), 16)
        self.assertTrue(any(a.tree_uuid))                  # never the null UUID
        self.assertNotEqual(a.tree_uuid, b.tree_uuid)

    def test_default_run_keeps_the_stable_uuid(self):
        pub = mock_robot.MockPublisher(*mock_robot.TREES[0], tree_uuid=mock_robot.DEFAULT_TREE_UUID)
        reply = pub.reply([_request(REQ_STATUS)], 0)
        self.assertEqual(KleinGateway.reply_uuid(reply), bytes(range(16)))

    def test_every_reply_carries_the_instance_uuid(self):
        pub = mock_robot.MockPublisher(*mock_robot.TREES[1])
        for frames in ([_request(REQ_FULLTREE)], [_request(REQ_STATUS)],
                       [_request(REQ_BLACKBOARD), b"PatrolTree"]):
            reply = pub.reply(frames, 3)
            self.assertEqual(KleinGateway.reply_uuid(reply), pub.tree_uuid)
        self.assertEqual(pub.reply([_request(REQ_FULLTREE)], 0)[1].decode(), mock_robot.PATROL_TREE_XML)

    def test_unknown_request_type_yields_the_error_reply(self):
        pub = mock_robot.MockPublisher(*mock_robot.TREES[0])
        self.assertEqual(pub.reply([_request(ord("?"))], 0)[0], b"error")
        self.assertEqual(pub.reply([], 0)[0], b"error")

    def test_switching_publishers_changes_uuid_and_restarts_uids(self):
        first = mock_robot.MockPublisher(*mock_robot.TREES[0])
        second = mock_robot.MockPublisher(*mock_robot.TREES[1])
        shapes = []
        for pub in (first, second):
            self.gw._parse_layout(pub.reply([_request(REQ_FULLTREE)], 0)[1].decode())
            uids = KleinGateway.parse_status(pub.reply([_request(REQ_STATUS)], 0)[1])
            self.assertEqual(min(uids), 1)                 # UIDs restart with the publisher
            shapes.append((self.gw.tree_structure["root_tree_id"], self.gw._node_seq))
        self.assertNotEqual(shapes[0][0], shapes[1][0])    # different root trees...
        self.assertNotEqual(shapes[0][1], shapes[1][1])    # ...of different size
        # The decision klein's reload rests on, fed with the mock's real headers.
        self.gw._layout_uuid = KleinGateway.reply_uuid(first.reply([_request(REQ_STATUS)], 0))
        self.assertFalse(self.gw._tree_changed(KleinGateway.reply_uuid(first.reply([_request(REQ_STATUS)], 1))))
        self.assertTrue(self.gw._tree_changed(KleinGateway.reply_uuid(second.reply([_request(REQ_STATUS)], 0))))


class PatrolTreeTest(unittest.TestCase):
    def parse(self, tick):
        return KleinGateway.parse_status(mock_robot.build_patrol_status_buffer(tick))

    def test_status_covers_every_uid_and_decodes(self):
        self.assertEqual(set(self.parse(0)), set(mock_robot.PATROL_UIDS))
        for t in range(mock_robot.PATROL_CYCLE_TICKS * 2):
            for entry in self.parse(t).values():
                self.assertNotEqual(entry["status"], "UNKNOWN")

    def test_root_decorator_runs_throughout_the_lap(self):
        for t in range(mock_robot.PATROL_CYCLE_TICKS):
            self.assertEqual(self.parse(t)[1]["status"], "RUNNING")

    def test_blackboard_serves_the_names_the_layout_advertises(self):
        names = KleinGateway.extract_blackboard_names(ET.fromstring(mock_robot.PATROL_TREE_XML))
        self.assertEqual(names, mock_robot.PATROL_BLACKBOARD_NAMES)
        self.assertEqual(set(mock_robot.build_patrol_blackboard(0)), set(names))

    def test_values_advance_across_laps(self):
        first = mock_robot.build_patrol_blackboard(0)["PatrolTree"]
        later = mock_robot.build_patrol_blackboard(mock_robot.PATROL_CYCLE_TICKS)["PatrolTree"]
        self.assertEqual((first["lap"], later["lap"]), (0, 1))
        self.assertNotEqual(first["waypoint"], later["waypoint"])
        self.assertLess(later["battery_pct"], first["battery_pct"])

    def test_reply_round_trips_through_the_gateway_parser(self):
        raw = mock_robot.build_blackboard_reply(
            [_request(REQ_BLACKBOARD), b"PatrolTree;Inspect::5"], 5, mock_robot.build_patrol_blackboard)
        boards = KleinGateway.parse_blackboard(raw, mock_robot.PATROL_BLACKBOARD_NAMES)
        self.assertEqual(list(boards), mock_robot.PATROL_BLACKBOARD_NAMES)
        json.dumps(boards)


if __name__ == "__main__":
    unittest.main()
