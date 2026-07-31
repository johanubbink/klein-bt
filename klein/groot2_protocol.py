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
