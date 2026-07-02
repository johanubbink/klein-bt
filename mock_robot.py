"""mock_robot.py — a fake BehaviorTree.CPP Groot2 publisher for testing klein.

Implements just enough of the Groot2 wire protocol (FULLTREE + STATUS over a
ZeroMQ REP socket) to drive the klein dashboard with no real robot. It serves a
small tree with nested subtrees and animates a "running cursor" so you can watch
RUNNING (pulsing amber), SUCCESS (green), FAILURE (red), and IDLE transitions
live.

Usage:
    python mock_robot.py                # bind tcp://*:1667 (Groot2 default)
    python mock_robot.py --port 1777    # use another port (e.g. real robot on 1667)

Then, in another shell:
    klein --robot-port <same-port>
"""
import argparse
import random
import struct

import zmq

PROTOCOL_ID = 2
REQ_FULLTREE = ord("T")
REQ_STATUS = ord("S")

# A tree with two nested subtrees; every node carries an integer _uid, exactly
# as BehaviorTree.CPP emits when add_metadata=true.
TREE_XML = """<root BTCPP_format="4" main_tree_to_execute="MainTree">
  <BehaviorTree ID="MainTree">
    <Sequence name="mission" _uid="1">
      <Action ID="Initialize" name="Initialize" _uid="2"/>
      <Fallback name="pick_or_retry" _uid="3">
        <SubTree ID="PickSub" name="pick" _uid="4"/>
        <Action ID="Retry" name="Retry" _uid="5"/>
      </Fallback>
      <SubTree ID="DropSub" name="drop" _uid="8"/>
      <Action ID="Finish" name="Finish" _uid="12"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="PickSub">
    <Sequence name="pick_seq" _uid="20">
      <Action ID="Approach" name="Approach" _uid="21"/>
      <Action ID="Grasp" name="Grasp" _uid="22"/>
      <Condition ID="HasObject" name="HasObject" _uid="23"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="DropSub">
    <Sequence name="drop_seq" _uid="30">
      <Action ID="MoveToBin" name="MoveToBin" _uid="31"/>
      <Action ID="Release" name="Release" _uid="32"/>
    </Sequence>
  </BehaviorTree>
</root>"""

# Every UID that appears in the tree (the publisher reports status for all).
ALL_UIDS = [1, 2, 3, 4, 5, 8, 12, 20, 21, 22, 23, 30, 31, 32]

# Ordered "execution" of leaf/near-leaf nodes the cursor walks through.
EXEC_ORDER = [2, 21, 22, 23, 5, 31, 32, 12]

# Parents that are "running" while one of their descendants runs.
PARENTS = {
    2: [1], 21: [1, 3, 4, 20], 22: [1, 3, 4, 20], 23: [1, 3, 4, 20],
    5: [1, 3], 31: [1, 8, 30], 32: [1, 8, 30], 12: [1],
}

# NodeStatus ints
IDLE, RUNNING, SUCCESS, FAILURE = 0, 1, 2, 3
IDLE_FROM_SUCCESS, IDLE_FROM_FAILURE = 12, 13


def build_status_buffer(tick):
    """Return the 3-byte-per-node status buffer for the current tick.

    A cursor walks EXEC_ORDER; the cursor node is RUNNING (with its ancestors),
    already-visited nodes show their finished (IDLE_FROM_*) state, and not-yet-
    reached nodes are IDLE. One node fails occasionally to exercise red/FAILURE.
    """
    cursor = tick % len(EXEC_ORDER)
    running_leaf = EXEC_ORDER[cursor]
    fail_this_lap = (running_leaf == 5)  # "Retry" node fails now and then

    status = {uid: IDLE for uid in ALL_UIDS}

    # Finished leaves (before the cursor) -> IDLE_FROM_SUCCESS/FAILURE
    for leaf in EXEC_ORDER[:cursor]:
        status[leaf] = IDLE_FROM_FAILURE if leaf == 5 else IDLE_FROM_SUCCESS

    # Current leaf + its ancestors -> RUNNING (or FAILURE for the failing leaf)
    status[running_leaf] = FAILURE if fail_this_lap else RUNNING
    for parent in PARENTS.get(running_leaf, []):
        status[parent] = RUNNING

    buf = bytearray()
    for uid in ALL_UIDS:
        buf += struct.pack("<HB", uid, status[uid])
    return bytes(buf)


def reply_header(request_first_frame):
    """Build the 22-byte Groot2 reply header (echo request + 16-byte tree UUID)."""
    # request frame is protocol(u8) type(u8) unique_id(u32); echo it back.
    if len(request_first_frame) >= 6:
        _proto, req_type, unique_id = struct.unpack("<BBI", request_first_frame[:6])
    else:
        req_type, unique_id = 0, 0
    tree_uuid = bytes(range(16))  # any stable 16-byte id
    return struct.pack("<BBI", PROTOCOL_ID, req_type, unique_id) + tree_uuid


def main():
    ap = argparse.ArgumentParser(description="Fake BehaviorTree.CPP Groot2 publisher for testing klein.")
    ap.add_argument("--host", default="*", help="bind address (default: * = all interfaces)")
    ap.add_argument("--port", type=int, default=1667, help="ZeroMQ REP port (default: 1667)")
    args = ap.parse_args()

    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    endpoint = f"tcp://{args.host}:{args.port}"
    sock.bind(endpoint)
    print(f"[mock_robot] Groot2 publisher listening on {endpoint}")
    print(f"[mock_robot] tree: MainTree ({len(ALL_UIDS)} nodes, 2 subtrees)")
    print(f"[mock_robot] run:  klein --robot-port {args.port}")

    tick = 0
    try:
        while True:
            frames = sock.recv_multipart()
            header = frames[0] if frames else b""
            req_type = header[1] if len(header) >= 2 else 0

            if req_type == REQ_FULLTREE:
                sock.send_multipart([reply_header(header), TREE_XML.encode("utf-8")])
            elif req_type == REQ_STATUS:
                sock.send_multipart([reply_header(header), build_status_buffer(tick)])
                tick += 1
            else:
                sock.send_multipart([b"error", b"unsupported request"])
    except KeyboardInterrupt:
        print("\n[mock_robot] shutting down.")
    finally:
        sock.close(linger=0)
        ctx.term()


if __name__ == "__main__":
    main()
