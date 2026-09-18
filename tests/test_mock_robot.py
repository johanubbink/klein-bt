"""Unit tests for klein.mock_robot — the fake publisher used to drive
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
    NODE_CATEGORIES,
    PROTOCOL_ID,
    REQ_STATUS,
    STATUS_RECORD_SIZE,
)


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


class NodeCategoryTest(unittest.TestCase):
    """The mock's <TreeNodesModel>: what a real CrossDoor robot declares."""

    def setUp(self):
        self.model = KleinGateway.parse_node_categories(
            ET.fromstring(mock_robot.TREE_XML)
        )

    def test_categories_use_the_shared_vocabulary(self):
        self.assertTrue(set(self.model.values()) <= NODE_CATEGORIES)
        # All five, so a robot-free run exercises every style the dashboard draws.
        self.assertEqual(set(self.model.values()), NODE_CATEGORIES)

    def test_every_node_type_in_the_tree_is_declared(self):
        root = ET.fromstring(mock_robot.TREE_XML)
        used = {
            node.tag
            for block in root.findall(".//BehaviorTree")
            for node in block.iter()
            if node.tag != "BehaviorTree"
        }
        self.assertEqual(used - set(self.model), set())


if __name__ == "__main__":
    unittest.main()
