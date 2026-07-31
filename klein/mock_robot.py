"""mock_robot.py — a fake BehaviorTree.CPP Groot2 publisher for testing klein.

Implements just enough of the Groot2 wire protocol (FULLTREE + STATUS +
BLACKBOARD over a ZeroMQ REP socket) to drive the klein dashboard with no real
robot. It replays the *CrossDoor* mission from BehaviorTree.CPP's
``examples/t11_groot_howto.cpp`` — the canonical Groot2 tutorial — so the
dashboard shows the same tree a real robot running that example would publish.
Watch RUNNING (pulsing amber), SUCCESS (green), FAILURE (red), and
IDLE-transition ("was …") states live, plus blackboard values that evolve with
the mission.

Usage:
    klein-bt-mock                       # bind tcp://*:1667 (Groot2 default)
    klein-bt-mock --port 1777           # use another port (e.g. real robot on 1667)
    python -m klein.mock_robot          # equivalent, without the console script

Then, in another shell:
    klein-bt --robot-port <same-port>
"""
import argparse
import math
import struct
import sys

import msgpack
import zmq

from .groot2_protocol import (
    HEADER_FORMAT,
    IDLE_TRANSITION,
    PROTOCOL_ID,
    REQ_BLACKBOARD,
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
#
# ``_fullpath`` is the subtree *instance path*, which doubles as the blackboard
# name in a BLACKBOARD request. Real robots stamp it on every <BehaviorTree>
# block and on the <SubTree> element that references it (hence the duplicate
# "DoorClosed::7" below, which klein dedupes).
TREE_XML = """<root BTCPP_format="4" main_tree_to_execute="MainTree">
  <BehaviorTree ID="MainTree" _fullpath="MainTree">
    <Sequence name="Sequence" _uid="1">
      <Script name="Script" code="door_open:=false" _uid="2"/>
      <UpdatePosition name="UpdatePosition" _uid="3"/>
      <Fallback name="Fallback" _uid="4">
        <Inverter name="Inverter" _uid="5">
          <IsDoorClosed name="IsDoorClosed" _uid="6"/>
        </Inverter>
        <SubTree ID="DoorClosed" _uid="7" _fullpath="DoorClosed::7"/>
      </Fallback>
      <PassThroughDoor name="PassThroughDoor" _uid="13"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="DoorClosed" _fullpath="DoorClosed::7">
    <Fallback name="tryOpen" _uid="8">
      <OpenDoor name="OpenDoor" _uid="9"/>
      <RetryUntilSuccessful name="RetryUntilSuccessful" num_attempts="5" _uid="10">
        <PickLock name="PickLock" _uid="11"/>
      </RetryUntilSuccessful>
      <SmashDoor name="SmashDoor" _uid="12"/>
    </Fallback>
  </BehaviorTree>
</root>"""

# Blackboard names the mock serves, in FULLTREE order (what klein derives from
# the _fullpath attributes above).
BLACKBOARD_NAMES = ["MainTree", "DoorClosed::7"]

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
    """Build the mission as ``(duration_ticks, {uid: status}, {bb_key: value})``.

    Nodes absent from a frame are IDLE. This mirrors the deterministic CrossDoor
    run: the door starts closed and locked, so OpenDoor fails, PickLock retries
    (failing four times, cracking it on the fifth), and the robot then passes
    through. Completed nodes keep their SUCCESS/FAILURE color — a non-reactive
    Sequence/Fallback holds a finished child's result — so a colored trail grows
    as the mission advances; a final reset flips everything to "was …" and idle.

    Each frame also carries the mission's blackboard values at that moment, so
    the values the dashboard shows stay in lockstep with the node colors rather
    than being reconstructed from hard-coded tick ranges.
    """
    frames = []
    done = {}   # uid -> solid terminal status, accumulated as nodes complete
    bb = {"mission_phase": "init", "door_open": 0,
          "pick_attempts": 0, "lock_status": "locked"}

    def frame(dur, running, failed=()):
        status = dict(done)
        for uid in running:
            status[uid] = RUNNING
        for uid in failed:
            status[uid] = FAILURE
        frames.append((dur, status, dict(bb)))

    frame(3, [1, 2])                    # Script sets a blackboard flag
    done[2] = SUCCESS
    frame(3, [1, 3])                    # UpdatePosition
    done[3] = SUCCESS
    bb["mission_phase"] = "check_door"
    frame(3, [1, 4, 5, 6])             # IsDoorClosed under Inverter/Fallback: door IS closed
    done[6] = SUCCESS                   # IsDoorClosed -> SUCCESS (closed)
    done[5] = FAILURE                   # ...which the Inverter negates: the door is not open

    bb["mission_phase"] = "unlocking"
    frame(5, [1, 4, 7, 8, 9])          # into the DoorClosed subtree: OpenDoor tries first
    done[9] = FAILURE                   # OpenDoor -> FAILURE (the door is locked)

    for attempt in range(1, 6):        # RetryUntilSuccessful drives PickLock, 5 attempts
        bb["pick_attempts"] = attempt
        frame(4, [1, 4, 7, 8, 10, 11]) # PickLock working (pulsing amber)
        if attempt < 5:
            frame(1, [1, 4, 7, 8, 10], failed=[11])   # this attempt failed (red flash)
    done[11] = SUCCESS                  # PickLock cracked it on the fifth try
    done[10] = SUCCESS                  # RetryUntilSuccessful -> SUCCESS
    done[8] = SUCCESS                   # Fallback "tryOpen" -> SUCCESS (door_open:=true)
    done[7] = SUCCESS                   # DoorClosed subtree -> SUCCESS
    done[4] = SUCCESS                   # the outer Fallback -> SUCCESS

    bb["lock_status"] = "picked"         # the lock gave way...
    bb["door_open"] = 1                  # ...so the Script's flag is now true
    bb["mission_phase"] = "passing_through"
    frame(5, [1, 13])                   # PassThroughDoor: the door is open now
    done[13] = SUCCESS
    done[1] = SUCCESS                   # the mission Sequence -> SUCCESS

    bb["mission_phase"] = "done"
    frames.append((15, dict(done), dict(bb)))   # hold the completed mission to be read
    # Reset: the whole tree drops back to IDLE, each node flagged with its last result.
    frames.append((6, {uid: (WAS_SUCCESS if st == SUCCESS else WAS_FAILURE)
                        for uid, st in done.items()}, dict(bb)))
    # Idle pause before the next lap: the tree is torn down, so the blackboard
    # reverts to its initial state (the next lap's Script re-runs door_open:=false).
    frames.append((12, {}, {"mission_phase": "idle", "door_open": 0,
                            "pick_attempts": 0, "lock_status": "locked"}))
    return frames


TIMELINE = _build_timeline()
CYCLE_TICKS = sum(dur for dur, _, _ in TIMELINE)


def _frame_at(tick):
    """Return the ``({uid: status}, {bb_key: value})`` frame for a given tick.

    Walks the looping mission TIMELINE. Shared by the status and blackboard
    builders so the two can never disagree about where the mission is.
    """
    t = tick % CYCLE_TICKS
    for dur, status, bb in TIMELINE:
        if t < dur:
            return status, bb
        t -= dur
    return TIMELINE[-1][1], TIMELINE[-1][2]     # unreachable: the durations sum to CYCLE_TICKS


def build_status_buffer(tick):
    """Return the 3-byte-per-node status buffer for the current tick.

    Nodes not named in the current frame are reported IDLE.
    """
    status, _bb = _frame_at(tick)
    buf = bytearray()
    for uid in ALL_UIDS:
        buf += struct.pack(STATUS_RECORD_FORMAT, uid, status.get(uid, IDLE))
    return bytes(buf)


# --------------------------------------------------------------------------- #
# ROS-shaped values
# --------------------------------------------------------------------------- #
# A real ROS 2 robot puts whole messages on its blackboard, and BehaviorTree.CPP
# serializes each one to JSON tagged with its C++ type name — nested all the way
# down, so a Path carries a Header which carries a Time. The dashboard's
# renderers key on those tags, so the mock has to produce the same shapes.
DOOR_X = 4.0                # where the door is; the mission drives toward it
PATH_POSES = 8              # waypoints between the robot and the door
MOCK_EPOCH_SEC = 1761922    # an arbitrary fixed wall-clock second


def _ros_time(seconds):
    sec = int(seconds)
    return {
        "__type": "builtin_interfaces::msg::Time",
        "sec": sec,
        "nanosec": int(round((seconds - sec) * 1e9)),
    }


def _header(seconds, frame_id="map"):
    return {
        "__type": "std_msgs::msg::Header",
        "frame_id": frame_id,
        "stamp": _ros_time(seconds),
    }


def _quaternion(yaw):
    return {
        "__type": "geometry_msgs::msg::Quaternion",
        "x": 0.0, "y": 0.0,
        "z": math.sin(yaw / 2), "w": math.cos(yaw / 2),
    }


def _pose(x, y, yaw):
    return {
        "__type": "geometry_msgs::msg::Pose",
        "position": {"__type": "geometry_msgs::msg::Point", "x": x, "y": y, "z": 0.0},
        "orientation": _quaternion(yaw),
    }


def _pose_stamped(x, y, yaw, seconds):
    return {
        "__type": "geometry_msgs::msg::PoseStamped",
        "header": _header(seconds),
        "pose": _pose(x, y, yaw),
    }


def _accumulated(step, times):
    """Sum ``step`` ``times`` over, keeping the float noise a real robot has.

    Repeated addition is how a pose integrator actually advances, and it is what
    turns a tidy 1.3 into 1.2999999999999985 — the noise the dashboard's number
    formatting exists to hide.
    """
    total = 0.0
    for _ in range(times):
        total += step
    return total


def build_blackboard(tick):
    """Return ``{blackboard_name: {key: value}}`` for the current tick.

    Covers the value shapes a real robot can send, so the dashboard's rendering
    is exercised end to end: ints (BT.CPP stores bools as 0/1), strings, floats,
    a vector, a JSON-registered struct (tagged with ``__type``), and an entry
    that is declared but never written (``None``). ``_debug_internal`` is a
    private key: the gateway must filter it out before it reaches the browser.

    It also carries the ROS 2 messages the renderers know how to summarize — a
    live ``nav_msgs::msg::Path``, a ``PoseStamped``, a ``Quaternion`` — plus the
    two float cases that are unreadable raw: accumulated noise, and the DBL_MAX
    sentinel BT.CPP ports use for "no limit".
    """
    _status, bb = _frame_at(tick)
    t = tick % CYCLE_TICKS
    seconds = MOCK_EPOCH_SEC + t * 0.1
    # The robot creeps toward the door, so the path ahead of it shortens and the
    # distance-to-goal ticks down — both visibly live in the panel.
    robot_x = _accumulated(0.05, t)
    remaining = DOOR_X - robot_x
    step = remaining / PATH_POSES
    return {
        "MainTree": {
            "door_open": bb["door_open"],
            "mission_phase": bb["mission_phase"],
            "tick": t,
            # Advances every tick, so at least one row always flashes on update.
            "robot_position": [round(robot_x, 2), 0.5, 1.57],
            # Static: proves unchanged rows stay quiet while their neighbours flash.
            "target_pose": {"__type": "Pose2D", "x": 3.0, "y": 0.5, "theta": 1.57},
            "last_error": None,             # unset -> renders "(not shown)"
            "path": {
                "__type": "nav_msgs::msg::Path",
                "header": _header(seconds),
                "poses": [
                    _pose_stamped(robot_x + step * i, 0.5, 0.0, seconds)
                    for i in range(PATH_POSES)
                ],
            },
            # Stamped once at mission start, so this row stays quiet too.
            "goal": _pose_stamped(DOOR_X, 0.5, math.pi / 2, MOCK_EPOCH_SEC),
            "heading": _quaternion(robot_x * 0.1),
            "distance_to_goal": remaining,
            # DBL_MAX: what a "no limit" double port reports until something sets it.
            "distance_to_end_of_route": sys.float_info.max,
            "_debug_internal": "must never reach the dashboard",
        },
        "DoorClosed::7": {
            "pick_attempts": bb["pick_attempts"],
            "lock_status": bb["lock_status"],
        },
    }


def build_blackboard_reply(request_frames, tick):
    """Encode the msgpack payload for a BLACKBOARD request.

    The request's second frame is a ``;``-separated list of blackboard names.
    Like the real publisher, unknown names are silently dropped, and a request
    that matches nothing yields msgpack nil rather than an empty map.
    """
    raw_names = request_frames[1].decode("utf-8", errors="replace") if len(request_frames) >= 2 else ""
    names = [name for name in raw_names.split(";") if name]
    boards = build_blackboard(tick)
    payload = {name: boards[name] for name in names if name in boards}
    return msgpack.packb(payload or None, use_bin_type=True)


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
            elif req_type == REQ_BLACKBOARD:
                sock.send_multipart([reply_header(header), build_blackboard_reply(frames, tick)])
            else:
                sock.send_multipart([b"error", b"unsupported request"])
    except KeyboardInterrupt:
        print("\n[mock_robot] shutting down.")
    finally:
        sock.close(linger=0)
        ctx.term()


if __name__ == "__main__":
    main()
