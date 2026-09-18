"""Helpers shared by the test modules: walking an unrolled layout, and building
reply headers through the mock's encoder rather than restating the wire format.
"""
import struct

from klein import mock_robot
from klein.groot2_protocol import HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS

# The UUID the mock publishes by default, and one that is deliberately not it.
UUID_A = mock_robot.DEFAULT_TREE_UUID
UUID_B = b"\xaa" * len(UUID_A)


def reply_header(uuid=UUID_A, request_type=REQ_STATUS):
    """A reply header stamped with a publisher's tree UUID.

    Built with the mock's own encoder, so a test can never pass against framing
    the robot does not actually send.
    """
    request = struct.pack(HEADER_FORMAT, PROTOCOL_ID, request_type, 1)
    return mock_robot.reply_header(request, uuid)


def _collect(node, key, acc=None):
    """Depth-first list of one field across an unrolled layout, skipping nulls."""
    acc = [] if acc is None else acc
    if node.get(key) is not None:
        acc.append(node[key])
    for child in node.get("children", []):
        _collect(child, key, acc)
    return acc


def collect_uids(node):
    return _collect(node, "uid")


def collect_ids(node):
    return _collect(node, "id")
