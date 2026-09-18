"""Unit tests for klein.mock_robot — the fake publisher used to drive
klein in tests. Verifies the status animation, blackboard content and reply
framing, and round-trips its output through the real gateway parser.

Most classes here are about CrossDoor, the mission the mock publishes by
default. ``EveryTreeTest`` holds the invariants that must be true of *any* tree
the mock can publish, so the patrol tree it swaps to stays as honest as the one
it swaps from.
"""
import json
import struct
import sys
import unittest
import xml.etree.ElementTree as ET

from klein import mock_robot
from klein.gateway import KleinGateway
from tests.helpers import collect_uids
from klein.groot2_protocol import (
    HEADER_FORMAT,
    NODE_CATEGORIES,
    PROTOCOL_ID,
    REQ_STATUS,
    STATUS_RECORD_SIZE,
)


class StatusBufferTest(unittest.TestCase):
    def parse(self, tick):
        return KleinGateway.parse_status(mock_robot.build_status_buffer(tick, mock_robot.CROSSDOOR))

    def statuses_over_cycle(self, uid):
        """Every status ``uid`` takes across one full mission cycle."""
        return {self.parse(t)[uid]["status"] for t in range(mock_robot.CROSSDOOR.cycle_ticks)}

    def test_mission_starts_with_script_running(self):
        parsed = self.parse(0)                       # first frame: Script + its Sequence parent
        self.assertEqual(parsed[2]["status"], "RUNNING")     # Script
        self.assertEqual(parsed[1]["status"], "RUNNING")     # mission Sequence
        self.assertEqual(parsed[13]["status"], "IDLE")       # PassThroughDoor not yet reached

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
        for tick in range(mock_robot.CROSSDOOR.cycle_ticks):
            status = KleinGateway.parse_status(mock_robot.build_status_buffer(tick, mock_robot.CROSSDOOR))
            if status[8]["status"] == "SUCCESS":
                self.assertEqual(self.board(tick)["door_open"], 1, f"tick {tick}")

    def test_reply_round_trips_through_the_gateway_parser(self):
        request = [b"header", b"MainTree;DoorClosed::7"]
        boards = KleinGateway.parse_blackboard(mock_robot.build_blackboard_reply(request, 0, mock_robot.CROSSDOOR))
        self.assertEqual(sorted(boards), ["DoorClosed::7", "MainTree"])
        # The gateway strips the mock's private key before the browser sees it.
        self.assertIn("_debug_internal", mock_robot.CROSSDOOR.blackboard(0)["MainTree"])
        self.assertNotIn("_debug_internal", boards["MainTree"])

    def test_unknown_names_are_dropped_silently(self):
        boards = KleinGateway.parse_blackboard(
            mock_robot.build_blackboard_reply([b"header", b"MainTree;Nope"], 0, mock_robot.CROSSDOOR)
        )
        self.assertEqual(list(boards), ["MainTree"])

    def test_no_match_yields_msgpack_nil(self):
        raw = mock_robot.build_blackboard_reply([b"header", b"Nope;AlsoNope"], 0, mock_robot.CROSSDOOR)
        self.assertEqual(raw, b"\xc0")                       # msgpack nil
        self.assertEqual(KleinGateway.parse_blackboard(raw), {})

    def test_missing_argument_frame_is_tolerated(self):
        raw = mock_robot.build_blackboard_reply([b"header"], 0, mock_robot.CROSSDOOR)
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
        noisy = [self.board(t)["distance_to_goal"] for t in range(mock_robot.CROSSDOOR.cycle_ticks)]
        self.assertTrue(any(len(repr(value)) > 6 for value in noisy), noisy[:5])

    def test_the_path_shortens_as_the_robot_advances(self):
        def remaining(tick):
            poses = self.board(tick)["path"]["poses"]
            return poses[-1]["pose"]["position"]["x"] - poses[0]["pose"]["position"]["x"]
        self.assertLess(remaining(20), remaining(1))

    def test_ros_messages_survive_the_gateway_json_conversion(self):
        raw = mock_robot.build_blackboard_reply([b"header", b"MainTree"], 0, mock_robot.CROSSDOOR)
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

    def test_the_uuid_is_overridable(self):
        # main() draws a fresh random UUID on every tree swap, the way a
        # restarted Groot2Publisher does — that is the signal klein watches.
        swapped = b"\xaa" * 16
        header = mock_robot.reply_header(
            struct.pack(HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, 1), swapped)
        self.assertEqual(header[6:], swapped)
        self.assertNotEqual(swapped, mock_robot.DEFAULT_TREE_UUID)

    def test_short_request_frame_is_tolerated(self):
        header = mock_robot.reply_header(b"")
        self.assertEqual(len(header), 22)
        proto, req_type, unique_id = struct.unpack(HEADER_FORMAT, header[:6])
        self.assertEqual((proto, req_type, unique_id), (PROTOCOL_ID, 0, 0))


class EveryTreeTest(unittest.TestCase):
    """Invariants that hold for every tree the mock can publish.

    Written over ``TREES`` rather than over CrossDoor, so the second tree — the
    one that exists to let a tree *swap* be watched without a real robot — is
    held to the same standard, and so a third would be too.
    """

    def test_status_buffer_matches_the_declared_uids(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                buf = mock_robot.build_status_buffer(0, tree)
                self.assertEqual(len(buf), len(tree.uids) * STATUS_RECORD_SIZE)
                self.assertEqual(set(KleinGateway.parse_status(buf)), set(tree.uids))

    def test_every_status_int_decodes(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                for t in range(tree.cycle_ticks):
                    parsed = KleinGateway.parse_status(
                        mock_robot.build_status_buffer(t, tree))
                    for entry in parsed.values():
                        self.assertNotEqual(entry["status"], "UNKNOWN")

    def test_the_xml_unrolls_to_the_declared_uids(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                gw = KleinGateway("127.0.0.1", 1667, 8080)
                try:
                    gw._parse_layout(tree.xml)
                    uids = collect_uids(gw.tree_structure)
                finally:
                    gw.ctx.destroy(linger=0)
                self.assertEqual(sorted(uids), sorted(tree.uids))

    def test_boards_are_the_names_the_layout_advertises(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                self.assertEqual(list(tree.blackboard(0)),
                                 tree.blackboard_names)
                self.assertEqual(
                    KleinGateway.extract_blackboard_names(ET.fromstring(tree.xml)),
                    tree.blackboard_names)

    def test_every_node_type_is_declared_in_the_model(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                root = ET.fromstring(tree.xml)
                model = KleinGateway.parse_node_categories(root)
                self.assertTrue(set(model.values()) <= NODE_CATEGORIES)
                used = {
                    node.tag
                    for block in root.findall(".//BehaviorTree")
                    for node in block.iter()
                    if node.tag != "BehaviorTree"
                }
                self.assertEqual(used - set(model), set())

    def test_cycle_ticks_cannot_drift_from_the_timeline(self):
        for tree in mock_robot.TREES.values():
            with self.subTest(tree=tree.name):
                self.assertEqual(tree.cycle_ticks,
                                 sum(dur for dur, _, _ in tree.timeline))

    def test_the_two_trees_are_genuinely_different(self):
        # If they were not, a swap between them would prove nothing.
        crossdoor, patrol = mock_robot.CROSSDOOR, mock_robot.PATROL
        self.assertNotEqual(crossdoor.xml, patrol.xml)
        self.assertNotEqual(crossdoor.blackboard_names, patrol.blackboard_names)
        self.assertNotEqual(len(crossdoor.uids), len(patrol.uids))
        # ...while still reusing UIDs, which is what made the bug visible: these
        # statuses used to land on the other tree's cards.
        self.assertTrue(set(crossdoor.uids) & set(patrol.uids))

    def test_the_switch_cycles_back_round(self):
        # Swapping *back* is a new publisher too, so it must be a real switch.
        self.assertIs(mock_robot._next_tree(mock_robot.CROSSDOOR), mock_robot.PATROL)
        self.assertIs(mock_robot._next_tree(mock_robot.PATROL), mock_robot.CROSSDOOR)


class NodeCategoryTest(unittest.TestCase):
    """The mock's <TreeNodesModel>: what a real CrossDoor robot declares."""

    def setUp(self):
        self.model = KleinGateway.parse_node_categories(
            ET.fromstring(mock_robot.CROSSDOOR.xml)
        )

    def test_categories_use_the_shared_vocabulary(self):
        self.assertTrue(set(self.model.values()) <= NODE_CATEGORIES)
        # All five, so a robot-free run exercises every style the dashboard draws.
        self.assertEqual(set(self.model.values()), NODE_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
