"""Helpers shared by the test modules: reply headers built through the mock's
encoder rather than restating the wire format, in-process gateways and a
scripted robot for them, test layouts and random records, and the
``.bb.jsonl`` blackboard sidecar (the one for the t11 fixture included).
"""
import asyncio
import inspect
import json
import struct
import types

import msgpack

from klein import gateway as gateway_module
from klein import mock_robot
from klein.gateway import KleinGateway
from klein.groot2_protocol import (HEADER_FORMAT, PROTOCOL_ID, REQ_BLACKBOARD, REQ_FULLTREE,
                                   REQ_GET_TRANSITIONS, REQ_STATUS, REQ_TOGGLE_RECORDING,
                                   STATUS_RECORD_FORMAT, encode_transition)
from klein.recording import Layout

# The UUID the mock publishes by default, and one that is deliberately not it.
UUID_A = mock_robot.DEFAULT_TREE_UUID
UUID_B = b"\xaa" * len(UUID_A)

STATE_VALUES = [0, 1, 2, 3, 4, 10, 11, 12, 13, 14]     # every STATUS-encoded value
# What a robot can report: never "was IDLE" (10), which transitions can't make.
REPORTABLE = [v for v in STATE_VALUES if v != 10]


def reply_header(uuid=UUID_A, request_type=REQ_STATUS):
    """A reply header stamped with a publisher's tree UUID.

    Built with the mock's own encoder, so a test can never pass against framing
    the robot does not actually send.
    """
    request = struct.pack(HEADER_FORMAT, PROTOCOL_ID, request_type, 1)
    return mock_robot.reply_header(request, uuid)


def status_record(uid, status):
    """One STATUS reply record."""
    return struct.pack(STATUS_RECORD_FORMAT, uid, status)


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


def layout(tree=None, uids=(1,), generation=1, xml=""):
    """A ``klein.recording.Layout``; each call is a new tree run's."""
    return Layout(generation, xml, tree if tree is not None else {}, sorted(uids))


def random_records(rng, n, uids, t0=1_000_000, idle_on_idle=False):
    """``n`` in-order records on ``uids``, about a third of them sharing the
    previous time. As a robot sends them, an IDLE comes only after a live
    status; ``idle_on_idle`` also lets one land on an idle node (as an arm's
    replayed overlap does)."""
    out, t, live = [], t0, set()
    while len(out) < n:
        uid, status = rng.choice(uids), rng.randrange(5)
        if status == 0 and uid not in live and not idle_on_idle:
            continue
        (live.add if status else live.discard)(uid)
        t += rng.choice((0, 0, 1, 7, 150))
        out.append((t, uid, status))
    return out


def equivalent(a, b):
    """Two STATUS bytes that show the same: 0 (never ran) and any "IDLE, was X"."""
    return a == b or (a == 0 and b >= 10) or (b == 0 and a >= 10)


# --------------------------------------------------------------------------- #
# Gateways in-process
# --------------------------------------------------------------------------- #
def new_gateway(test, **kwargs):
    """A gateway on the default ports, destroyed when ``test`` finishes."""
    gw = KleinGateway("127.0.0.1", 1667, 8080, **kwargs)
    test.addCleanup(gw.ctx.destroy, linger=0)
    return gw


def opened(path):
    """A gateway showing ``path`` (``--open``); a file needs no robot socket."""
    gw = KleinGateway("127.0.0.1", 1, 0)
    gw.ctx.term()
    gw.open_file(path)
    return gw


def answer(gw, path):
    """The gateway's HTTP answer for ``path``, as websockets gets it (the
    ``/log.zip`` coroutine, which compresses in a thread, run to its response)."""
    response = gw._process_request(None, types.SimpleNamespace(path=path))
    return asyncio.run(response) if inspect.isawaitable(response) else response


class FakeRobot:
    """A scripted publisher for ``_request``: answers T/S/B/r/t, logs every
    request letter, and parks (setting ``done``) after ``limit`` requests.

    ``timeouts`` holds 1-based request indices that raise ``RobotTimeout``;
    ``on_request`` maps an index to a callable run just before it is answered.
    """

    START_US = 1_700_000_000_000_000

    def __init__(self, limit):
        self.limit = limit
        self.xml = mock_robot.CROSSDOOR.xml
        self.uuid = UUID_A
        self.status = b"".join(status_record(uid, 0) for uid in range(1, 14))
        self.supports_recording = True
        self.drains = []                # one [(offset_us, uid, status)] per `t`
        self.timeouts = set()
        self.on_request = {}
        self.on_letter = {}             # request letter -> callable, run once
        self.sent = []
        self.starts = []                # every `r start` reply value
        self.done = asyncio.Event()

    async def __call__(self, request_type, payload=None):
        self.sent.append(chr(request_type))
        n = len(self.sent)
        if n > self.limit:
            self.sent.pop()
            self.done.set()
            await asyncio.sleep(3600)   # cancelled by the test
        if n in self.on_request:
            self.on_request[n](self)
        if chr(request_type) in self.on_letter:
            self.on_letter.pop(chr(request_type))(self)
        if n in self.timeouts:
            raise gateway_module.RobotTimeout("stub timeout")
        header = reply_header(self.uuid, request_type)
        if request_type == REQ_FULLTREE:
            return [header, self.xml.encode()]
        if request_type == REQ_STATUS:
            return [header, self.status]
        if request_type == REQ_BLACKBOARD:
            return [header, msgpack.packb({"MainTree": {"n": n}})]
        if request_type == REQ_TOGGLE_RECORDING:
            assert payload == b"start", payload
            if not self.supports_recording:
                return [b"error", b"unsupported request"]
            self.starts.append(self.START_US + 1_000_000 * (len(self.starts) + 1))
            return [header, str(self.starts[-1]).encode()]
        if request_type == REQ_GET_TRANSITIONS:
            records = self.drains.pop(0) if self.drains else []
            return [header, b"".join(encode_transition(*r) for r in records)]
        raise AssertionError(f"unexpected request {chr(request_type)!r}")


# --------------------------------------------------------------------------- #
# Blackboard sidecars
# --------------------------------------------------------------------------- #
def read_sidecar(data):
    """``(header, lines)`` of a ``.bb.jsonl``."""
    lines = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    return lines[0], lines[1:]


def sidecar_at(lines, offset, boards=()):
    """``{board: {key: value}}`` from ``.bb.jsonl`` lines (in time order) at
    ``offset`` µs: each key's latest line at or before it. Every board in
    ``boards`` is there, empty or not; any other only while it holds a key."""
    out = {board: {} for board in boards}
    for line in lines:
        if line["t"] > offset:
            break
        board = out.setdefault(line["board"], {})
        if line.get("removed"):
            del board[line["key"]]
        else:
            board[line["key"]] = line["value"]
    return {board: keys for board, keys in out.items() if keys or board in boards}


# A blackboard sidecar for the t11 fixture (which, written by FileLogger2, has
# none): MainTree's keys as `export_blackboard` writes them, at offsets inside
# the file's ~9.4 s. DoorClosed::7 has no keys of its own (every port is
# remapped), so, as in a real export, it has no lines.
T11_BOARDS = ["MainTree", "DoorClosed::7"]
T11_SIDECAR = [
    {"t": t, "board": "MainTree", "key": key, **({"removed": True} if value is None
                                                  else {"value": value})}
    for t, key, value in [
        (0, "door_open", False), (0, "pos", {"x": 0.0, "y": 0.0}), (0, "note", "start"),
        (0, "waypoints", [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]),
        (2_000_000, "pos", {"x": 1.1, "y": 2.2}),
        (3_500_000, "door_open", True),
        (4_700_000, "note", None),                  # removed
        (6_000_000, "pos", {"x": 3.3, "y": 4.4}), (6_000_000, "door_open", False),
        (8_500_000, "pos", {"x": 5.5, "y": 6.6}),
    ]]


def t11_sidecar(first_timestamp):
    """The ``.bb.jsonl`` text of ``T11_SIDECAR``."""
    header = {"klein_blackboard": 1, "first_timestamp": first_timestamp, "tree_id": "MainTree"}
    return "".join(json.dumps(line) + "\n" for line in [header] + T11_SIDECAR)
