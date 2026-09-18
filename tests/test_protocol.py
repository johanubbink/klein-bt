"""Unit tests for klein.groot2_protocol — the shared wire-protocol constants,
the NodeStatus decode rule and the node-category vocabulary."""
import struct
import unittest

from klein.groot2_protocol import (
    BUILTIN_CATEGORIES,
    CATEGORY_UNDEFINED,
    HEADER_FORMAT,
    IDLE_TRANSITION,
    NODE_CATEGORIES,
    PROTOCOL_ID,
    REQ_BLACKBOARD,
    REQ_FULLTREE,
    REQ_STATUS,
    REPLY_HEADER_SIZE,
    REQUEST_HEADER_SIZE,
    STATUS_RECORD_FORMAT,
    STATUS_RECORD_SIZE,
    TREE_UUID_OFFSET,
    TREE_UUID_SIZE,
    NodeStatus,
    decode_status,
    decode_tree_uuid,
)


class ConstantsTest(unittest.TestCase):
    def test_wire_constants_match_btcpp(self):
        self.assertEqual(PROTOCOL_ID, 2)
        self.assertEqual(REQ_FULLTREE, ord("T"))
        self.assertEqual(REQ_STATUS, ord("S"))
        self.assertEqual(REQ_BLACKBOARD, ord("B"))

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


class ReplyHeaderTest(unittest.TestCase):
    """The 16-byte tree UUID — the only signal that the robot swapped trees."""

    UUID = bytes(range(16))

    def header(self, uuid=None):
        return (struct.pack(HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, 7)
                + (self.UUID if uuid is None else uuid))

    def test_sizes_match_btcpp(self):
        # Literals on purpose: these pin the wire format against
        # groot2_protocol.h, so deriving them here would assert nothing.
        self.assertEqual(REQUEST_HEADER_SIZE, 6)
        self.assertEqual(TREE_UUID_OFFSET, 6)
        self.assertEqual(TREE_UUID_SIZE, 16)
        self.assertEqual(REPLY_HEADER_SIZE, 22)

    def test_round_trips_the_uuid(self):
        self.assertEqual(decode_tree_uuid(self.header()), self.UUID)
        other = b"\xaa" * 16
        self.assertEqual(decode_tree_uuid(self.header(other)), other)

    def test_a_memoryview_frame_decodes_to_comparable_bytes(self):
        # zmq hands frames over as buffers; the result is compared against a
        # stored bytes object, so it must not come back as a memoryview.
        decoded = decode_tree_uuid(memoryview(self.header()))
        self.assertIsInstance(decoded, bytes)
        self.assertEqual(decoded, self.UUID)

    def test_frames_with_no_uuid_decode_to_none(self):
        # An error reply's frame 0 is b"error", and a truncated header has
        # nothing to read. Neither is a tree change; both must say so.
        for frame in (None, b"", b"error", self.header()[:REPLY_HEADER_SIZE - 1]):
            with self.subTest(frame=frame):
                self.assertIsNone(decode_tree_uuid(frame))


if __name__ == "__main__":
    unittest.main()


class NodeCategoryTest(unittest.TestCase):
    """The category vocabulary and the builtin fallback table."""

    def test_vocabulary_matches_btcpp_spelling(self):
        # toStr<NodeType>() in basic_types.cpp — these are the <TreeNodesModel>
        # element tags, so a typo here silently stops matching real robots.
        self.assertEqual(
            NODE_CATEGORIES,
            frozenset({"Action", "Condition", "Control", "Decorator", "SubTree"}),
        )
        self.assertEqual(CATEGORY_UNDEFINED, "Undefined")
        self.assertNotIn(CATEGORY_UNDEFINED, NODE_CATEGORIES)

    def test_builtin_table_values_are_all_real_categories(self):
        self.assertTrue(set(BUILTIN_CATEGORIES.values()) <= NODE_CATEGORIES)

    def test_every_builtin_control_and_decorator_is_covered(self):
        # A robot too old to publish <TreeNodesModel> must still get its whole
        # control skeleton right — every Control and Decorator in BT.CPP is builtin.
        for name in ("Sequence", "ReactiveFallback", "Switch3", "IfThenElse",
                     "Parallel", "TryCatch", "SequenceWithMemory"):
            self.assertEqual(BUILTIN_CATEGORIES[name], "Control", name)
        for name in ("Inverter", "RetryUntilSuccessful", "Timeout", "Precondition",
                     "LoopString", "ForceSuccess", "SkipUnlessUpdated"):
            self.assertEqual(BUILTIN_CATEGORIES[name], "Decorator", name)

    def test_the_leaf_categories_are_distinguished(self):
        self.assertEqual(BUILTIN_CATEGORIES["Script"], "Action")
        self.assertEqual(BUILTIN_CATEGORIES["ScriptCondition"], "Condition")
        self.assertEqual(BUILTIN_CATEGORIES["SubTree"], "SubTree")
