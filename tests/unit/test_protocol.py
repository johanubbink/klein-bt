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
    REQ_GET_TRANSITIONS,
    REQ_STATUS,
    REQ_TOGGLE_RECORDING,
    REPLY_HEADER_SIZE,
    REQUEST_HEADER_SIZE,
    STATUS_RECORD_FORMAT,
    STATUS_RECORD_SIZE,
    TRANSITION_BUFFER_MAX,
    TRANSITION_RECORD_SIZE,
    TREE_UUID_OFFSET,
    TREE_UUID_SIZE,
    NodeStatus,
    decode_status,
    decode_transitions,
    decode_tree_uuid,
    encode_transition,
)


class ConstantsTest(unittest.TestCase):
    def test_wire_format_matches_btcpp(self):
        # Literals on purpose: these pin the wire format against
        # groot2_protocol.h, so deriving them here would assert nothing.
        self.assertEqual(PROTOCOL_ID, 2)
        self.assertEqual(REQ_FULLTREE, ord("T"))
        self.assertEqual(REQ_STATUS, ord("S"))
        self.assertEqual(REQ_BLACKBOARD, ord("B"))
        self.assertEqual(REQ_TOGGLE_RECORDING, ord("r"))
        self.assertEqual(REQ_GET_TRANSITIONS, ord("t"))
        self.assertEqual(TRANSITION_BUFFER_MAX, 1000)
        self.assertEqual(struct.calcsize(HEADER_FORMAT), 6)         # u8 u8 u32
        self.assertEqual(struct.calcsize(STATUS_RECORD_FORMAT), 3)  # u16 u8
        self.assertEqual(STATUS_RECORD_SIZE, 3)
        self.assertEqual(TRANSITION_RECORD_SIZE, 9)                 # u48 u16 u8
        self.assertEqual(REQUEST_HEADER_SIZE, 6)
        self.assertEqual(TREE_UUID_OFFSET, 6)
        self.assertEqual(TREE_UUID_SIZE, 16)
        self.assertEqual(REPLY_HEADER_SIZE, 22)


class DecodeStatusTest(unittest.TestCase):
    def test_decode_status(self):
        cases = [
            (0, ("IDLE", None)),
            (1, ("RUNNING", None)),
            (2, ("SUCCESS", None)),
            (3, ("FAILURE", None)),
            (4, ("SKIPPED", None)),
            # 10..14 all mean "now IDLE, previously X" for X in 0..4.
            (IDLE_TRANSITION + 0, ("IDLE", "IDLE")),
            (IDLE_TRANSITION + 1, ("IDLE", "RUNNING")),
            (IDLE_TRANSITION + 2, ("IDLE", "SUCCESS")),
            (IDLE_TRANSITION + 3, ("IDLE", "FAILURE")),
            (IDLE_TRANSITION + 4, ("IDLE", "SKIPPED")),
            (7, ("UNKNOWN", None)),       # 5..9 gap
            (99, ("UNKNOWN", None)),      # transition of a non-status
            (255, ("UNKNOWN", None)),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(decode_status(value), expected)


class TransitionRecordTest(unittest.TestCase):
    def test_layout_matches_the_publishers_memcpys(self):
        record = encode_transition(0x0102030405, 0x0A0B, NodeStatus.FAILURE)
        self.assertEqual(record, bytes([5, 4, 3, 2, 1, 0, 0x0B, 0x0A, 3]))

    def test_round_trips_and_ignores_a_trailing_partial_record(self):
        records = [(0, 1, 1), (2**48 - 1, 65535, 2), (123456, 9, 0)]   # incl. a 48-bit timestamp
        payload = b"".join(encode_transition(*r) for r in records)
        self.assertEqual(decode_transitions(payload), records)
        payload = encode_transition(7, 3, 1) + b"\x00" * 5
        self.assertEqual(decode_transitions(payload), [(7, 3, 1)])
        self.assertEqual(decode_transitions(b""), [])


class ReplyHeaderTest(unittest.TestCase):
    """The 16-byte tree UUID — the only signal that the robot swapped trees."""

    UUID = bytes(range(16))

    def header(self, uuid=None):
        return (struct.pack(HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, 7)
                + (self.UUID if uuid is None else uuid))

    def test_round_trips_the_uuid(self):
        self.assertEqual(decode_tree_uuid(self.header()), self.UUID)
        other = b"\xaa" * 16
        self.assertEqual(decode_tree_uuid(self.header(other)), other)
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

    def test_builtin_table(self):
        self.assertTrue(set(BUILTIN_CATEGORIES.values()) <= NODE_CATEGORIES)
        # A robot too old to publish <TreeNodesModel> must still get its whole
        # control skeleton right — every Control and Decorator in BT.CPP is
        # builtin — and the leaf categories distinguished.
        cases = [
            ("Sequence", "Control"), ("ReactiveFallback", "Control"),
            ("Switch3", "Control"), ("IfThenElse", "Control"),
            ("Parallel", "Control"), ("TryCatch", "Control"),
            ("SequenceWithMemory", "Control"),
            ("Inverter", "Decorator"), ("RetryUntilSuccessful", "Decorator"),
            ("Timeout", "Decorator"), ("Precondition", "Decorator"),
            ("LoopString", "Decorator"), ("ForceSuccess", "Decorator"),
            ("SkipUnlessUpdated", "Decorator"),
            ("Script", "Action"), ("ScriptCondition", "Condition"),
            ("SubTree", "SubTree"),
        ]
        for name, category in cases:
            with self.subTest(name):
                self.assertEqual(BUILTIN_CATEGORIES[name], category)


if __name__ == "__main__":
    unittest.main()
