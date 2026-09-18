"""mock_robot.py — a fake BehaviorTree.CPP Groot2 publisher for testing klein.

Implements just enough of that wire protocol (FULLTREE + STATUS + BLACKBOARD
over a ZeroMQ REP socket) to drive the klein dashboard with no real robot. It
replays the *CrossDoor* mission from BehaviorTree.CPP's
``examples/t11_groot_howto.cpp`` — the canonical tutorial — so the dashboard
shows the same tree a real robot running that example would publish.
Watch RUNNING (pulsing amber), SUCCESS (green), FAILURE (red), and
IDLE-transition ("was …") states live, plus blackboard values that evolve with
the mission.

It can also publish a *second*, quite different tree, and swap between the two
mid-run the way a robot loading a new mission does — a fresh publisher UUID and
all — so klein's re-handshake can be watched with no C++ in the loop.

Usage:
    klein-bt-mock                       # bind tcp://*:1667 (BT.CPP's default)
    klein-bt-mock --port 1777           # use another port (e.g. real robot on 1667)
    klein-bt-mock --tree patrol         # publish the other tree instead
    klein-bt-mock --switch-every 200    # swap trees every 200 status polls (~20s)
    python -m klein.mock_robot          # equivalent, without the console script

Then, in another shell:
    klein-bt --robot-port <same-port>
"""
import argparse
import collections
import math
import os
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
    REQUEST_HEADER_SIZE,
    STATUS_RECORD_FORMAT,
    TREE_UUID_SIZE,
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
#
# The nodes also carry ports, the way a real robot serializes them next to the
# _uid, so the dashboard's node cards have the same variety to draw: an output
# port bound to a blackboard key (UpdatePosition's ``pos``), the scripting hooks
# BT.CPP writes out of a node's pre/post-conditions (IsDoorClosed's ``_skipIf``,
# PickLock's ``_onSuccess``), a <SubTree> remapping a child key onto its
# parent's board (``door_open`` — remapped keys live in the parent, which is why
# the DoorClosed board further down does not list it), and a node with more
# ports than fit on a card (PassThroughDoor), for the truncation and the hover
# tooltip. Deliberately left bare: the control nodes, OpenDoor and SmashDoor.
#
# The <TreeNodesModel> at the end declares each node's category. A real reply
# lists every registered node, ~45 builtins included; the mock lists only the
# types this tree uses. These are the real CrossDoor registrations from
# BehaviorTree.CPP/sample_nodes/crossdoor_nodes.cpp:63-74 — SmashDoor is a
# Condition while the equally childless OpenDoor is an Action, which is why a
# category cannot be inferred from the tree's shape. All five categories appear,
# so a robot-free run exercises every style the dashboard draws.
CROSSDOOR_XML = """<root BTCPP_format="4" main_tree_to_execute="MainTree">
  <BehaviorTree ID="MainTree" _fullpath="MainTree">
    <Sequence name="Sequence" _uid="1">
      <Script name="Script" code="door_open:=false" _uid="2"/>
      <UpdatePosition name="UpdatePosition" pos="{robot_position}" _uid="3"/>
      <Fallback name="Fallback" _uid="4">
        <Inverter name="Inverter" _uid="5">
          <IsDoorClosed name="IsDoorClosed" _skipIf="door_open" _uid="6"/>
        </Inverter>
        <SubTree ID="DoorClosed" door_open="{door_open}" _uid="7" _fullpath="DoorClosed::7"/>
      </Fallback>
      <PassThroughDoor name="PassThroughDoor" goal="{goal}" speed="0.35" timeout_ms="2500" _uid="13"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="DoorClosed" _fullpath="DoorClosed::7">
    <Fallback name="tryOpen" _uid="8">
      <OpenDoor name="OpenDoor" _uid="9"/>
      <RetryUntilSuccessful name="RetryUntilSuccessful" num_attempts="5" _uid="10">
        <PickLock name="PickLock" _onSuccess="lock_status:='picked'" _uid="11"/>
      </RetryUntilSuccessful>
      <SmashDoor name="SmashDoor" _uid="12"/>
    </Fallback>
  </BehaviorTree>
  <TreeNodesModel>
    <Control ID="Fallback"/>
    <Condition ID="IsDoorClosed"/>
    <Decorator ID="Inverter"/>
    <Action ID="OpenDoor"/>
    <Action ID="PassThroughDoor">
      <input_port name="goal" type="geometry_msgs::msg::PoseStamped"/>
      <input_port name="speed" type="double"/>
      <input_port name="timeout_ms" type="unsigned int"/>
    </Action>
    <Action ID="PickLock"/>
    <Decorator ID="RetryUntilSuccessful">
      <input_port name="num_attempts" type="int">Repeat a failed child up to N times</input_port>
    </Decorator>
    <Action ID="Script">
      <input_port name="code" type="std::string">Piece of code that can be parsed</input_port>
    </Action>
    <Control ID="Sequence"/>
    <Condition ID="SmashDoor"/>
    <SubTree ID="SubTree">
      <input_port name="_autoremap" type="bool" default="false">If true, all the ports with the same name will be remapped</input_port>
    </SubTree>
    <Action ID="UpdatePosition">
      <output_port name="pos" type="Position2D"/>
    </Action>
  </TreeNodesModel>
</root>"""

# Blackboard names the mock serves, in FULLTREE order (what klein derives from
# the _fullpath attributes above).
CROSSDOOR_BLACKBOARD_NAMES = ["MainTree", "DoorClosed::7"]

# Every UID in the tree, in order (the publisher reports status for all of them).
# SmashDoor (12) is the never-taken branch: PickLock always cracks it first.
CROSSDOOR_UIDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]

# Status values used by the animation, derived from the shared protocol enum.
IDLE = NodeStatus.IDLE
RUNNING = NodeStatus.RUNNING
SUCCESS = NodeStatus.SUCCESS
FAILURE = NodeStatus.FAILURE
WAS_SUCCESS = IDLE_TRANSITION + NodeStatus.SUCCESS   # 12: "now IDLE, previously SUCCESS"
WAS_FAILURE = IDLE_TRANSITION + NodeStatus.FAILURE   # 13: "now IDLE, previously FAILURE"


def _build_crossdoor_timeline():
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


def _cycle_ticks(timeline):
    return sum(dur for dur, _, _ in timeline)


CROSSDOOR_TIMELINE = _build_crossdoor_timeline()


def _frame_at(tick, tree):
    """Return the ``({uid: status}, {bb_key: value})`` frame for a given tick.

    Walks the tree's looping mission timeline. Shared by the status and
    blackboard builders so the two can never disagree about where the mission is.
    """
    t = tick % tree.cycle_ticks
    for dur, status, bb in tree.timeline:
        if t < dur:
            return status, bb
        t -= dur
    return tree.timeline[-1][1], tree.timeline[-1][2]   # unreachable: durations sum to cycle_ticks


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


def _crossdoor_blackboard(tick):
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
    _status, bb = _frame_at(tick, CROSSDOOR)
    t = tick % CROSSDOOR.cycle_ticks
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


# --------------------------------------------------------------------------- #
# A second tree, so a tree *swap* can be watched without a real robot
# --------------------------------------------------------------------------- #
# Deliberately small — its job is to prove the reload works, not to be a second
# showpiece mission. What matters is that it is unmistakably *not* CrossDoor:
# different node names, types and count, different board names, and a different
# root. It reuses UIDs 1-6, which is the point: before klein learned to watch
# the tree UUID, these UIDs' statuses landed on CrossDoor's cards, so Script and
# UpdatePosition lit up with a patrol robot's telemetry. It keeps one <SubTree>
# so nested boards and the subtree region rendering stay exercised.
PATROL_XML = """<root BTCPP_format="4" main_tree_to_execute="PatrolTree">
  <BehaviorTree ID="PatrolTree" _fullpath="PatrolTree">
    <ReactiveSequence name="ReactiveSequence" _uid="1">
      <BatteryOk name="BatteryOk" min_percent="20" _uid="2"/>
      <SubTree ID="VisitWaypoints" waypoint="{next_waypoint}" _uid="3" _fullpath="VisitWaypoints::3"/>
    </ReactiveSequence>
  </BehaviorTree>
  <BehaviorTree ID="VisitWaypoints" _fullpath="VisitWaypoints::3">
    <SequenceWithMemory name="visitAll" _uid="4">
      <MoveTo name="MoveTo" goal="{waypoint}" speed="0.4" _uid="5"/>
      <Wait name="Dwell" msec="1500" _uid="6"/>
    </SequenceWithMemory>
  </BehaviorTree>
  <TreeNodesModel>
    <Control ID="ReactiveSequence"/>
    <Control ID="SequenceWithMemory"/>
    <Condition ID="BatteryOk">
      <input_port name="min_percent" type="int">Fail below this charge</input_port>
    </Condition>
    <Action ID="MoveTo">
      <input_port name="goal" type="Position2D"/>
      <input_port name="speed" type="double"/>
    </Action>
    <Action ID="Wait">
      <input_port name="msec" type="unsigned int"/>
    </Action>
    <SubTree ID="SubTree">
      <input_port name="_autoremap" type="bool" default="false">If true, all the ports with the same name will be remapped</input_port>
    </SubTree>
  </TreeNodesModel>
</root>"""

PATROL_UIDS = [1, 2, 3, 4, 5, 6]
PATROL_BLACKBOARD_NAMES = ["PatrolTree", "VisitWaypoints::3"]
PATROL_WAYPOINTS = ["dock", "corridor", "lab", "atrium"]


def _build_patrol_timeline():
    """One lap of the patrol: check the battery, then drive-and-dwell per waypoint.

    Generated rather than hand-choreographed like CrossDoor's — this tree exists
    to be visibly different, not to be a second tutorial.
    """
    frames = []
    for leg, waypoint in enumerate(PATROL_WAYPOINTS):
        bb = {"patrol_leg": leg, "next_waypoint": waypoint,
              "battery_pct": 95 - leg * 7}
        checking = {1: RUNNING, 2: RUNNING}
        driving = {1: RUNNING, 2: SUCCESS, 3: RUNNING, 4: RUNNING, 5: RUNNING}
        dwelling = {1: RUNNING, 2: SUCCESS, 3: RUNNING, 4: RUNNING,
                    5: SUCCESS, 6: RUNNING}
        frames.append((2, checking, dict(bb)))
        frames.append((6, driving, dict(bb)))
        frames.append((3, dwelling, dict(bb)))
    # The lap completes, then the whole tree drops back to IDLE flagged with its
    # last result — the same "was …" reset CrossDoor ends on.
    done = {uid: SUCCESS for uid in PATROL_UIDS}
    finished = {"patrol_leg": len(PATROL_WAYPOINTS), "next_waypoint": "dock",
                "battery_pct": 95 - len(PATROL_WAYPOINTS) * 7}
    frames.append((6, done, dict(finished)))
    frames.append((4, {uid: WAS_SUCCESS for uid in PATROL_UIDS}, dict(finished)))
    return frames


PATROL_TIMELINE = _build_patrol_timeline()


def _patrol_blackboard(tick):
    """The patrol's two boards. Small on purpose — the ROS-shaped value zoo
    above belongs to CrossDoor, which is still the tree that exercises it."""
    _status, bb = _frame_at(tick, PATROL)
    return {
        "PatrolTree": {
            "battery_pct": bb["battery_pct"],
            "patrol_leg": bb["patrol_leg"],
            "waypoints": list(PATROL_WAYPOINTS),
            "_debug_internal": "must never reach the dashboard",
        },
        "VisitWaypoints::3": {
            "next_waypoint": bb["next_waypoint"],
            "dwell_msec": 1500,
        },
    }


# --------------------------------------------------------------------------- #
# The trees the mock can publish
# --------------------------------------------------------------------------- #
MockTree = collections.namedtuple(
    "MockTree", "name xml uids blackboard_names timeline cycle_ticks blackboard")


def _tree(name, xml, uids, blackboard_names, timeline, blackboard):
    """Bundle one tree's mission state. ``cycle_ticks`` is derived rather than
    passed, so it can never drift from the timeline it counts."""
    return MockTree(name, xml, uids, blackboard_names, timeline,
                    _cycle_ticks(timeline), blackboard)


CROSSDOOR = _tree("crossdoor", CROSSDOOR_XML, CROSSDOOR_UIDS,
                  CROSSDOOR_BLACKBOARD_NAMES, CROSSDOOR_TIMELINE,
                  _crossdoor_blackboard)
PATROL = _tree("patrol", PATROL_XML, PATROL_UIDS,
               PATROL_BLACKBOARD_NAMES, PATROL_TIMELINE, _patrol_blackboard)

TREES = {tree.name: tree for tree in (CROSSDOOR, PATROL)}


def _next_tree(tree):
    """The tree ``--switch-every`` moves to next, cycling."""
    order = list(TREES.values())
    return order[(order.index(tree) + 1) % len(order)]


def build_status_buffer(tick, tree):
    """Return the 3-byte-per-node status buffer for the current tick.

    Nodes not named in the current frame are reported IDLE.
    """
    status, _bb = _frame_at(tick, tree)
    buf = bytearray()
    for uid in tree.uids:
        buf += struct.pack(STATUS_RECORD_FORMAT, uid, status.get(uid, IDLE))
    return bytes(buf)


def build_blackboard_reply(request_frames, tick, tree):
    """Encode the msgpack payload for a BLACKBOARD request.

    The request's second frame is a ``;``-separated list of blackboard names.
    Like the real publisher, unknown names are silently dropped, and a request
    that matches nothing yields msgpack nil rather than an empty map.
    """
    raw_names = request_frames[1].decode("utf-8", errors="replace") if len(request_frames) >= 2 else ""
    names = [name for name in raw_names.split(";") if name]
    boards = tree.blackboard(tick)
    payload = {name: boards[name] for name in names if name in boards}
    return msgpack.packb(payload or None, use_bin_type=True)


# A fixed UUID for the unit tests, which want reply framing to be reproducible.
# A *running* mock never uses it — see main(), which draws a random one per
# process exactly as Groot2Publisher::serverLoop does.
DEFAULT_TREE_UUID = bytes(range(TREE_UUID_SIZE))


def reply_header(request_first_frame, tree_uuid=DEFAULT_TREE_UUID):
    """Build the reply header: the request's header echoed back, then the tree UUID.

    ``main()`` draws a *fresh random* UUID on every switch rather than keeping
    one per tree — swapping back to the first tree is a new publisher too, and
    klein must still detect it. See docs/protocol.md for why that is the signal.
    """
    # request frame is protocol(u8) type(u8) unique_id(u32); echo it back.
    if len(request_first_frame) >= REQUEST_HEADER_SIZE:
        _proto, req_type, unique_id = struct.unpack(
            HEADER_FORMAT, request_first_frame[:REQUEST_HEADER_SIZE])
    else:
        req_type, unique_id = 0, 0
    return struct.pack(HEADER_FORMAT, PROTOCOL_ID, req_type, unique_id) + tree_uuid


def main():
    ap = argparse.ArgumentParser(description="Fake BehaviorTree.CPP publisher for testing klein.")
    ap.add_argument("--host", default="*", help="bind address (default: * = all interfaces)")
    ap.add_argument("--port", type=int, default=1667, help="ZeroMQ REP port (default: 1667)")
    ap.add_argument("--tree", choices=sorted(TREES), default="crossdoor",
                    help="which tree to publish (default: crossdoor)")
    ap.add_argument("--switch-every", type=int, default=0, metavar="TICKS",
                    help="swap to the other tree every N status polls, publishing "
                         "a fresh tree UUID — what a robot loading a different "
                         "tree looks like on the wire. A tick is one STATUS "
                         "request, so at klein's 10 Hz poll N=200 is about 20s. "
                         "Ticks only advance while a dashboard is connected, "
                         "since klein idles its pollers otherwise. 0 = never "
                         "(default).")
    args = ap.parse_args()

    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    endpoint = f"tcp://{args.host}:{args.port}"
    sock.bind(endpoint)
    tree = TREES[args.tree]
    # Drawn per process, like the CreateRandomUUID() at the top of
    # Groot2Publisher::serverLoop. A constant here would make two runs of the
    # mock indistinguishable on the wire, so restarting it onto a different
    # tree would look to a client exactly like the tree never changing — which
    # is the one thing this mock exists to let you test.
    tree_uuid = os.urandom(TREE_UUID_SIZE)
    print(f"[mock_robot] publisher listening on {endpoint}")
    print(f"[mock_robot] tree: {tree.name} ({len(tree.uids)} nodes, 1 subtree)")
    if args.switch_every > 0:
        print(f"[mock_robot] swapping trees every {args.switch_every} status polls")
    print(f"[mock_robot] run:  klein-bt --robot-port {args.port}")

    tick = 0
    try:
        while True:
            frames = sock.recv_multipart()
            header = frames[0] if frames else b""
            req_type = header[1] if len(header) >= 2 else 0

            if req_type == REQ_FULLTREE:
                sock.send_multipart([reply_header(header, tree_uuid),
                                     tree.xml.encode("utf-8")])
            elif req_type == REQ_STATUS:
                sock.send_multipart([reply_header(header, tree_uuid),
                                     build_status_buffer(tick, tree)])
                tick += 1
                if args.switch_every and tick % args.switch_every == 0:
                    # Swapped *after* the reply, so the next reply of any type is
                    # the first to carry the new UUID — exactly what a restarted
                    # publisher looks like from the client's side.
                    tree = _next_tree(tree)
                    tree_uuid = os.urandom(TREE_UUID_SIZE)
                    tick = 0            # the new mission starts at its beginning
                    print(f"[mock_robot] swapped to the {tree.name} tree "
                          f"({len(tree.uids)} nodes, new publisher UUID)")
            elif req_type == REQ_BLACKBOARD:
                sock.send_multipart([reply_header(header, tree_uuid),
                                     build_blackboard_reply(frames, tick, tree)])
            else:
                sock.send_multipart([b"error", b"unsupported request"])
    except KeyboardInterrupt:
        print("\n[mock_robot] shutting down.")
    finally:
        sock.close(linger=0)
        ctx.term()


if __name__ == "__main__":
    main()
