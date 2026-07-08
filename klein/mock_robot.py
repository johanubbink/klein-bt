"""mock_robot.py — a fake BehaviorTree.CPP Groot2 publisher for testing klein.

Implements just enough of the Groot2 wire protocol (FULLTREE + STATUS over a
ZeroMQ REP socket) to drive the klein dashboard with no real robot. It replays
the *CrossDoor* mission from BehaviorTree.CPP's ``examples/t11_groot_howto.cpp``
— the canonical Groot2 tutorial — so the dashboard shows the same tree a real
robot running that example would publish. Watch RUNNING (pulsing amber),
SUCCESS (green), FAILURE (red), and IDLE-transition ("was …") states live.

Usage:
    klein-bt-mock                       # bind tcp://*:1667 (Groot2 default)
    klein-bt-mock --port 1777           # use another port (e.g. real robot on 1667)
    python -m klein.mock_robot          # equivalent, without the console script

Then, in another shell:
    klein-bt --robot-port <same-port>
"""
import argparse
import struct

import zmq

from .groot2_protocol import (
    HEADER_FORMAT,
    IDLE_TRANSITION,
    PROTOCOL_ID,
    REQ_FULLTREE,
    REQ_STATUS,
    STATUS_RECORD_FORMAT,
    NodeStatus,
)

# The CrossDoor tree from examples/t11_groot_howto.cpp, with the integer _uid
# attributes BehaviorTree.CPP stamps on every node in a FULLTREE reply. UIDs are
# assigned in creation order (depth-first), exactly as BT.CPP does, so they line
# up with the status records below. The DoorClosed subtree is stitched in place
# by klein, unrolling to 13 nodes total — matching the real example.
TREE_XML = """<root BTCPP_format="4" main_tree_to_execute="MainTree">
  <BehaviorTree ID="MainTree">
    <Sequence name="Sequence" _uid="1">
      <Script name="Script" code="door_open:=false" _uid="2"/>
      <UpdatePosition name="UpdatePosition" _uid="3"/>
      <Fallback name="Fallback" _uid="4">
        <Inverter name="Inverter" _uid="5">
          <IsDoorClosed name="IsDoorClosed" _uid="6"/>
        </Inverter>
        <SubTree ID="DoorClosed" _uid="7"/>
      </Fallback>
      <PassThroughDoor name="PassThroughDoor" _uid="13"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="DoorClosed">
    <Fallback name="tryOpen" _uid="8">
      <OpenDoor name="OpenDoor" _uid="9"/>
      <RetryUntilSuccessful name="RetryUntilSuccessful" num_attempts="5" _uid="10">
        <PickLock name="PickLock" _uid="11"/>
      </RetryUntilSuccessful>
      <SmashDoor name="SmashDoor" _uid="12"/>
    </Fallback>
  </BehaviorTree>
</root>"""

# Every UID in the tree, in order (the publisher reports status for all of them).
# SmashDoor (12) is the never-taken branch: PickLock always cracks it first.
ALL_UIDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]

# Status values used by the animation, derived from the shared protocol enum.
IDLE = NodeStatus.IDLE
RUNNING = NodeStatus.RUNNING
SUCCESS = NodeStatus.SUCCESS
FAILURE = NodeStatus.FAILURE
WAS_SUCCESS = IDLE_TRANSITION + NodeStatus.SUCCESS   # 12: "now IDLE, previously SUCCESS"
WAS_FAILURE = IDLE_TRANSITION + NodeStatus.FAILURE   # 13: "now IDLE, previously FAILURE"


def _build_timeline():
    """Build the mission as a list of ``(duration_ticks, {uid: status})`` frames.

    Nodes absent from a frame are IDLE. This mirrors the deterministic CrossDoor
    run: the door starts closed and locked, so OpenDoor fails, PickLock retries
    (failing four times, cracking it on the fifth), and the robot then passes
    through. Completed nodes keep their SUCCESS/FAILURE color — a non-reactive
    Sequence/Fallback holds a finished child's result — so a colored trail grows
    as the mission advances; a final reset flips everything to "was …" and idle.
    """
    frames = []
    done = {}   # uid -> solid terminal status, accumulated as nodes complete

    def frame(dur, running):
        status = dict(done)
        for uid in running:
            status[uid] = RUNNING
        frames.append((dur, status))

    frame(3, [1, 2])                    # Script sets a blackboard flag
    done[2] = SUCCESS
    frame(3, [1, 3])                    # UpdatePosition
    done[3] = SUCCESS
    frame(3, [1, 4, 5, 6])             # IsDoorClosed under Inverter/Fallback: door IS closed
    done[6] = SUCCESS                   # IsDoorClosed -> SUCCESS (closed)
    done[5] = FAILURE                   # ...which the Inverter negates: the door is not open

    frame(5, [1, 4, 7, 8, 9])          # into the DoorClosed subtree: OpenDoor tries first
    done[9] = FAILURE                   # OpenDoor -> FAILURE (the door is locked)

    for attempt in range(1, 6):        # RetryUntilSuccessful drives PickLock, 5 attempts
        frame(4, [1, 4, 7, 8, 10, 11]) # PickLock working (pulsing amber)
        if attempt < 5:
            frames.append((1, {**done, 10: RUNNING, 11: FAILURE,  # this attempt failed (red flash)
                                1: RUNNING, 4: RUNNING, 7: RUNNING, 8: RUNNING}))
    done[11] = SUCCESS                  # PickLock cracked it on the fifth try
    done[10] = SUCCESS                  # RetryUntilSuccessful -> SUCCESS
    done[8] = SUCCESS                   # Fallback "tryOpen" -> SUCCESS (door_open:=true)
    done[7] = SUCCESS                   # DoorClosed subtree -> SUCCESS
    done[4] = SUCCESS                   # the outer Fallback -> SUCCESS

    frame(5, [1, 13])                   # PassThroughDoor: the door is open now
    done[13] = SUCCESS
    done[1] = SUCCESS                   # the mission Sequence -> SUCCESS

    frames.append((15, dict(done)))     # hold the completed mission so it can be read
    # Reset: the whole tree drops back to IDLE, each node flagged with its last result.
    frames.append((6, {uid: (WAS_SUCCESS if st == SUCCESS else WAS_FAILURE)
                        for uid, st in done.items()}))
    frames.append((12, {}))             # idle pause before the next lap
    return frames


TIMELINE = _build_timeline()
CYCLE_TICKS = sum(dur for dur, _ in TIMELINE)


def build_status_buffer(tick):
    """Return the 3-byte-per-node status buffer for the current tick.

    Walks the looping mission TIMELINE; nodes not named in the current frame are
    reported IDLE.
    """
    t = tick % CYCLE_TICKS
    status = TIMELINE[-1][1]
    for dur, frame_status in TIMELINE:
        if t < dur:
            status = frame_status
            break
        t -= dur

    buf = bytearray()
    for uid in ALL_UIDS:
        buf += struct.pack(STATUS_RECORD_FORMAT, uid, status.get(uid, IDLE))
    return bytes(buf)


def reply_header(request_first_frame):
    """Build the 22-byte Groot2 reply header (echo request + 16-byte tree UUID)."""
    # request frame is protocol(u8) type(u8) unique_id(u32); echo it back.
    if len(request_first_frame) >= 6:
        _proto, req_type, unique_id = struct.unpack(HEADER_FORMAT, request_first_frame[:6])
    else:
        req_type, unique_id = 0, 0
    tree_uuid = bytes(range(16))  # any stable 16-byte id
    return struct.pack(HEADER_FORMAT, PROTOCOL_ID, req_type, unique_id) + tree_uuid


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
    print(f"[mock_robot] tree: CrossDoor ({len(ALL_UIDS)} nodes, 1 subtree)")
    print(f"[mock_robot] run:  klein-bt --robot-port {args.port}")

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
