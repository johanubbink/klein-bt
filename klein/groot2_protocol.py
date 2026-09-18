"""klein.groot2_protocol — the Groot2 publisher wire protocol.

Single source of truth for the constants BehaviorTree.CPP defines in
``groot2_protocol.h`` / ``basic_types.h``. Both the gateway (which *decodes*
robot replies) and the test ``mock_robot`` (which *encodes* them) import from
here, so the encode and decode halves can never drift out of sync.
"""

import struct
from enum import IntEnum

# --------------------------------------------------------------------------- #
# Request framing
# --------------------------------------------------------------------------- #
PROTOCOL_ID = 2                 # groot2_protocol.h :: kProtocolID
REQ_FULLTREE = ord("T")         # RequestType::FULLTREE — returns the tree XML
REQ_STATUS = ord("S")           # RequestType::STATUS   — returns the status buffer
REQ_BLACKBOARD = ord("B")       # RequestType::BLACKBOARD — returns msgpack {bb: {key: value}}

# Request header, little-endian: protocol_id (u8) | request_type (u8) | unique_id (u32)
HEADER_FORMAT = "<BBI"
REQUEST_HEADER_SIZE = struct.calcsize(HEADER_FORMAT)         # 6

# Reply header (groot2_protocol.h :: ReplyHeader): the request's 6-byte header
# echoed back, then the tree UUID as 16 raw bytes — SerializeHeader() memcpys
# the array in, so it is not a hex string and must not be decoded as one.
TREE_UUID_OFFSET = REQUEST_HEADER_SIZE                       # 6
TREE_UUID_SIZE = 16
REPLY_HEADER_SIZE = REQUEST_HEADER_SIZE + TREE_UUID_SIZE     # 22

# Status buffer: consecutive fixed records, node_uid (u16) | status_int (u8)
STATUS_RECORD_FORMAT = "<HB"
STATUS_RECORD_SIZE = struct.calcsize(STATUS_RECORD_FORMAT)   # 3


# --------------------------------------------------------------------------- #
# Node status
# --------------------------------------------------------------------------- #
class NodeStatus(IntEnum):
    """NodeStatus values from basic_types.h."""
    IDLE = 0
    RUNNING = 1
    SUCCESS = 2
    FAILURE = 3
    SKIPPED = 4


# The publisher encodes "just became IDLE, previously X" as (IDLE_TRANSITION + X),
# so any status int >= IDLE_TRANSITION means the node is now IDLE, having just
# transitioned from status (int - IDLE_TRANSITION).
IDLE_TRANSITION = 10

_STATUS_NAMES = {int(s): s.name for s in NodeStatus}


def decode_status(value):
    """Decode a raw NodeStatus int into ``(status_name, transitioned_from)``.

    * A live status decodes to ``(name, None)`` — e.g. ``1 -> ("RUNNING", None)``.
    * An idle-transition marker (``value >= IDLE_TRANSITION``) decodes to
      ``("IDLE", previous_name)`` — e.g. ``12 -> ("IDLE", "SUCCESS")``.
    * Anything unrecognized decodes to ``("UNKNOWN", None)``.
    """
    if value >= IDLE_TRANSITION:
        previous = value - IDLE_TRANSITION
        if previous in _STATUS_NAMES:
            return NodeStatus.IDLE.name, _STATUS_NAMES[previous]
        return "UNKNOWN", None
    return _STATUS_NAMES.get(value, "UNKNOWN"), None


def decode_tree_uuid(header_frame):
    """Return the 16-byte tree UUID from a reply's frame 0, or ``None``.

    A different UUID means a different published tree — see docs/protocol.md.

    ``None`` means the frame carries no UUID to read: an error reply (whose
    frame 0 is ``b"error"``) or a header too short to hold one. Absence of
    information must never be reported as a change.
    """
    if header_frame is None or len(header_frame) < REPLY_HEADER_SIZE:
        return None
    # bytes(), not a bare slice: frames can arrive as memoryview/bytearray, and
    # the result is compared against a stored bytes object.
    return bytes(header_frame[TREE_UUID_OFFSET:REPLY_HEADER_SIZE])


# --------------------------------------------------------------------------- #
# Node categories
# --------------------------------------------------------------------------- #
# basic_types.h :: NodeType, spelled exactly as toStr<NodeType>() writes it
# (basic_types.cpp). These are the element *tags* of the <TreeNodesModel>
# entries in a FULLTREE reply; each entry's `ID` is the registration name.
# Unlike NodeStatus these never cross the wire as integers, so no IntEnum:
# the XML carries the words, and the words are the constant.
CATEGORY_ACTION = "Action"
CATEGORY_CONDITION = "Condition"
CATEGORY_CONTROL = "Control"
CATEGORY_DECORATOR = "Decorator"
CATEGORY_SUBTREE = "SubTree"
CATEGORY_UNDEFINED = "Undefined"        # toStr(NodeType::UNDEFINED)

NODE_CATEGORIES = frozenset({
    CATEGORY_ACTION,
    CATEGORY_CONDITION,
    CATEGORY_CONTROL,
    CATEGORY_DECORATOR,
    CATEGORY_SUBTREE,
})

# Every node BehaviorTree.CPP registers on itself, transcribed from the
# BehaviorTreeFactory constructor (src/bt_factory.cpp). The fallback when a
# reply carries no <TreeNodesModel>: BT.CPP refuses a duplicate registration ID,
# so no user node can shadow one of these. Custom names are not guessed at —
# see CATEGORY_UNDEFINED.
BUILTIN_CATEGORIES = {
    # ControlNode
    "Fallback": CATEGORY_CONTROL,
    "AsyncFallback": CATEGORY_CONTROL,
    "Sequence": CATEGORY_CONTROL,
    "AsyncSequence": CATEGORY_CONTROL,
    "SequenceWithMemory": CATEGORY_CONTROL,
    "SequenceStar": CATEGORY_CONTROL,           # only under USE_BTCPP3_OLD_NAMES
    "Parallel": CATEGORY_CONTROL,
    "ParallelAll": CATEGORY_CONTROL,
    "ReactiveSequence": CATEGORY_CONTROL,
    "ReactiveFallback": CATEGORY_CONTROL,
    "IfThenElse": CATEGORY_CONTROL,
    "WhileDoElse": CATEGORY_CONTROL,
    "TryCatch": CATEGORY_CONTROL,
    "Switch2": CATEGORY_CONTROL,
    "Switch3": CATEGORY_CONTROL,
    "Switch4": CATEGORY_CONTROL,
    "Switch5": CATEGORY_CONTROL,
    "Switch6": CATEGORY_CONTROL,
    # DecoratorNode
    "Inverter": CATEGORY_DECORATOR,
    "RetryUntilSuccessful": CATEGORY_DECORATOR,
    "RetryUntilSuccesful": CATEGORY_DECORATOR,  # the v3 typo, deprecated
    "KeepRunningUntilFailure": CATEGORY_DECORATOR,
    "Repeat": CATEGORY_DECORATOR,
    "Timeout": CATEGORY_DECORATOR,
    "Delay": CATEGORY_DECORATOR,
    "RunOnce": CATEGORY_DECORATOR,
    "ForceSuccess": CATEGORY_DECORATOR,
    "ForceFailure": CATEGORY_DECORATOR,
    "Precondition": CATEGORY_DECORATOR,
    "LoopInt": CATEGORY_DECORATOR,
    "LoopBool": CATEGORY_DECORATOR,
    "LoopDouble": CATEGORY_DECORATOR,
    "LoopString": CATEGORY_DECORATOR,
    "SkipUnlessUpdated": CATEGORY_DECORATOR,
    "WaitValueUpdate": CATEGORY_DECORATOR,
    # ActionNode
    "AlwaysSuccess": CATEGORY_ACTION,
    "AlwaysFailure": CATEGORY_ACTION,
    "Script": CATEGORY_ACTION,
    "SetBlackboard": CATEGORY_ACTION,
    "Sleep": CATEGORY_ACTION,
    "UnsetBlackboard": CATEGORY_ACTION,
    "WasEntryUpdated": CATEGORY_ACTION,
    # ConditionNode
    "ScriptCondition": CATEGORY_CONDITION,
    # SubTreeNode — a DecoratorNode subclass that reports NodeType::SUBTREE
    "SubTree": CATEGORY_SUBTREE,
}
