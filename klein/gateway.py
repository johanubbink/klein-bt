"""klein.gateway — ZeroMQ→WebSocket telemetry gateway for BehaviorTree.CPP v4.

klein connects to a running BehaviorTree.CPP robot node exposing the Groot2
publisher protocol (a ``ZMQ_REP`` socket, default port 1667), performs the
FULLTREE handshake, recursively unrolls nested subtrees into a single tree, and
then streams 10 Hz status telemetry — plus 2 Hz blackboard values, one board per
subtree — to browser dashboards over WebSockets. A robot that loads a
*different* tree is noticed on the next poll, from the tree UUID every reply
carries, and the handshake is re-run. Unless ``--record-buffer 0``, every
transition the robot reports is also drained after each status poll into an
in-memory ``Recording`` (see docs/architecture.md, "Recording").

Everything the browser needs is served from a **single port** (``--port``):

* plain HTTP for the dashboard (``/`` and ``/index.html``), its ``/styles.css``,
  ``/app.js``, ``/renderers.js``, ``/recording.js``, ``/cursor.js``,
  ``/drawer.js`` and ``/timeline.js``, and the bundled D3.js (``/d3.v7.min.js``), and
* a WebSocket endpoint (``/ws``) that pushes the unrolled tree layout on connect
  and then broadcasts live status and blackboard frames — and, while recording,
  streams the recording itself (``klein/streaming.py``), and
* while recording, the recorded tree runs as downloads: ``/log/runs``,
  ``/log.btlog?run=N``, ``/log.bb.jsonl?run=N``, and all of them in one
  ``/log.zip`` (the Save button).

``--open FILE.btlog`` shows a saved recording instead, with no robot: the file
(and its ``FILE.bb.jsonl`` blackboard sidecar, if present) is loaded into the
same ``Recording`` and streamed exactly as a live one.

Serving both from one origin means the dashboard just opens
``ws://<same-host:port>/ws`` — no port to configure, inject, or firewall twice.

The browser can only speak HTTP/WebSocket — never ZeroMQ — so klein is the
translator between the robot's REQ/REP world and the browser's push world.
"""

import asyncio
import io
import json
import logging
import random
import struct
import sys
import time
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import zmq
import zmq.asyncio
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from .groot2_protocol import (
    HEADER_FORMAT,
    PROTOCOL_ID,
    RECORDING_START,
    REQ_BLACKBOARD,
    REQ_FULLTREE,
    REQ_GET_TRANSITIONS,
    REQ_STATUS,
    REQ_TOGGLE_RECORDING,
    TRANSITION_BUFFER_MAX,
    decode_transitions,
    decode_tree_uuid,
    iter_status,
    parse_blackboard,
    parse_status,
)
from .btlog import BtlogError, export_blackboard, export_run, load_btlog, read_btlog
from .layout import collect_uids, extract_blackboard_names, parse_node_categories, unroll_tree
from .recording import Layout, RobotClock, decode_state, snapshot_run
from .streaming import Streamer

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"    # dashboard web assets live here, not beside the .py

# Timeouts / cadence
# 10 Hz status poll. Read on every poll: the tests set it lower, since the mock
# steps once per STATUS and so runs its missions and swaps faster. Not a flag.
POLL_INTERVAL = 0.1
BLACKBOARD_POLL_INTERVAL = 0.5  # 2 Hz blackboard poll — values change slower than status
REQUEST_TIMEOUT = 2.0           # seconds to wait for a robot reply
LAYOUT_RETRY_MAX = 5.0          # cap on handshake retry backoff

# Static files served over HTTP at "/<name>" ("/" is "/index.html"). The
# scripts are all the dashboard's own, and it needs every one of them to render.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
_STATIC_FILES = ("index.html", "styles.css", "d3.v7.min.js", "renderers.js", "recording.js",
                 "cursor.js", "drawer.js", "timeline.js", "app.js")
_STATIC_ROUTES = {f"/{name}": (name, _CONTENT_TYPES[Path(name).suffix])
                  for name in _STATIC_FILES}
_INDEX_FALLBACK = b"<!doctype html><h1>klein: index.html missing from package</h1>"

# The recording's downloads (docs/architecture.md, "Browser side: one port"):
# a run's files by suffix (None: all of them in one zip) and their types.
_LOG_SUFFIXES = (".btlog", ".bb.jsonl")
_LOG_TYPES = {".btlog": "application/octet-stream", ".bb.jsonl": "application/x-ndjson",
              None: "application/zip"}
_LOG_ROUTES = ("/log/runs", "/log.zip", *(f"/log{suffix}" for suffix in _LOG_SUFFIXES))


class RobotTimeout(Exception):
    """Raised when a robot request times out or the REQ socket faults."""


def _load_asset(name):
    """Read a packaged static asset as bytes, or None if it is missing."""
    try:
        return (STATIC_DIR / name).read_bytes()
    except OSError:
        return None


class KleinGateway:
    """Bridges the robot's ZeroMQ REQ/REP channel to browser WebSockets, and
    serves the dashboard + telemetry from a single HTTP/WebSocket port."""

    def __init__(self, robot_host, robot_port, port, recording=None, debug=False):
        self.robot_endpoint = f"tcp://{robot_host}:{robot_port}"
        self.port = port
        self.debug = debug                  # serve GET /debug/state

        # Transition recording (docs/architecture.md, "Recording"). None is
        # --record-buffer 0: no r/t on the wire, and the pollers idle without clients.
        self.recording = recording
        # Mirrors the recording into every dashboard (docs/protocol.md,
        # "Streaming the recording"): backfill on connect, then each change.
        self.streamer = (None if recording is None else
                         Streamer(recording, websockets.broadcast, name=self.robot_endpoint))
        self._clock = RobotClock()          # klein's monotonic clock -> robot µs
        self._recorded_tree = None          # the segments' layout object; new per XML
        self._armed = False                 # an open segment is taking `t` drains
        self._cannot_record = False         # this publisher answered `r` with an error
        self._file = None                   # the opened .btlog's name (--open): no robot
        # A request (any poller's, or the handshake's) timed out since the last
        # good status poll: if the robot turns out to have gone, the open
        # segment ends at the head, the last time klein heard from it.
        self._timed_out = False

        self.ctx = zmq.asyncio.Context()
        self.socket = None                  # created lazily / recreated on fault
        # Both created lazily, inside the loop that uses them: constructing a
        # gateway must not require a running event loop. On Python 3.9
        # asyncio.Lock() reaches for get_event_loop() and raises without one,
        # which would make the gateway unconstructible from plain sync code.
        self._req_lock = None               # REQ/REP is strictly send→recv

        self._node_categories = {}          # registration name -> category, from <TreeNodesModel>
        self.tree_structure = None          # unrolled nested dict sent to clients
        self._layout_json = None            # cached layout frame, rebuilt each handshake
        self.clients = set()

        # Blackboards: one per subtree instance, named by the paths in the layout.
        self._blackboard_names = []          # subtree instance paths, in tree order
        self._blackboard_request = None      # pre-encoded b"name1;name2" request payload
        self._blackboard_json = None         # cached last frame, for late-joining clients

        self._node_seq = 0                  # node count of the loaded tree
        self._layout_generation = 0         # bumped per handshake; prefixes node ids
        self._tree_uuid = None              # publisher UUID the loaded layout came from
        self._layout_xml = None             # raw FULLTREE XML, to spot an unchanged tree

        # Robot reachability, mirrored to dashboards so a blank canvas is never
        # ambiguous. Starts "not connected" until the first successful handshake.
        self._robot_connected = False
        self._robot_detail = f"Connecting to robot at {self.robot_endpoint}…"
        self._robot_state_json = None
        self._publish_robot_state()

        # request path -> (body_bytes, content_type); populated once in run().
        self._static = {}

    # ------------------------------------------------------------------ #
    # ZeroMQ request/reply
    # ------------------------------------------------------------------ #
    def _new_socket(self):
        """(Re)create the REQ socket. A timed-out REQ socket is stuck in the
        wrong half of its send/recv state machine and cannot be reused, so we
        throw it away and start clean."""
        if self.socket is not None:
            self.socket.close(linger=0)
        self.socket = self.ctx.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.connect(self.robot_endpoint)

    async def _request(self, request_type, payload=None):
        """Send one request and return the multipart reply frames.

        Some requests carry an argument frame after the header (BLACKBOARD wants
        the list of boards to dump); pass it as ``payload``. Replies are 2-frame
        multipart messages: frame 0 is a 22-byte reply header, frame 1 is the
        payload (tree XML, status buffer, or msgpack blackboards). On any
        timeout/fault the socket is recreated and ``RobotTimeout`` is raised.
        """
        if self._req_lock is None:
            # No await between the check and the assignment, so concurrent
            # callers on the one event loop cannot both make a lock.
            self._req_lock = asyncio.Lock()
        async with self._req_lock:
            if self.socket is None:
                self._new_socket()
            unique_id = random.randint(1, 0xFFFFFFFF)
            header = struct.pack(HEADER_FORMAT, PROTOCOL_ID, request_type, unique_id)
            frames = [header] if payload is None else [header, payload]
            try:
                await self.socket.send_multipart(frames)
                return await asyncio.wait_for(
                    self.socket.recv_multipart(), timeout=REQUEST_TIMEOUT
                )
            except asyncio.TimeoutError as exc:
                self._new_socket()
                raise RobotTimeout(f"no reply within {REQUEST_TIMEOUT:.0f}s") from exc
            except zmq.ZMQError as exc:
                self._new_socket()
                raise RobotTimeout(str(exc) or "socket error") from exc

    # ------------------------------------------------------------------ #
    # Layout: FULLTREE handshake + recursive subtree unrolling
    # ------------------------------------------------------------------ #
    def _parse_layout(self, xml_str):
        """Parse FULLTREE XML and build the unrolled tree structure."""
        root = ET.fromstring(xml_str)
        self._layout_xml = xml_str      # what the current layout was built from

        # Built before unrolling, because unroll_tree stamps each node's
        # category from it. Rebuilt per handshake, so re-handshaking against a
        # different tree never carries the previous tree's node types over.
        self._node_categories = parse_node_categories(root)

        self.tree_structure, self._node_seq = unroll_tree(
            root, self._node_categories, self._layout_generation + 1)
        self._layout_generation += 1
        # Serialize once: the layout is immutable until the next handshake, so
        # every connecting (or reconnecting) client is sent this same frame.
        self._layout_json = json.dumps({"type": "layout", "data": self.tree_structure})
        # One object per parsed XML: segments recorded while the tree is
        # unchanged share it, which is what groups them into one run.
        self._recorded_tree = Layout(self._layout_generation, xml_str, self.tree_structure,
                                     sorted(collect_uids(self.tree_structure)))

        self._blackboard_names = extract_blackboard_names(root)
        self._blackboard_request = ";".join(self._blackboard_names).encode("utf-8")
        self._blackboard_json = None    # values from the previous tree are stale

    def _broadcast(self, frame):
        """Send one pre-serialized frame to every dashboard, if any is watching."""
        if self.clients:
            websockets.broadcast(self.clients, frame)

    def _mark_connected(self):
        """Report the robot reachable. One spelling of the detail string, because
        ``_set_robot_state`` suppresses re-broadcasts by comparing it."""
        self._set_robot_state(True, f"Connected to robot at {self.robot_endpoint}")

    def _set_robot_state(self, connected, detail):
        """Record robot reachability and push it to dashboards on change."""
        self._robot_connected = connected
        self._robot_detail = detail
        self._publish_robot_state()

    def _publish_robot_state(self):
        """Push the ``robot`` frame (reachability and ``_recording_state()``)
        to dashboards when it changed.

        The latest frame is cached so a dashboard that connects later is told
        immediately whether the robot is reachable (see ``ws_handler``). Only
        real changes are broadcast, so this is safe to call on the 10 Hz path.
        """
        frame = json.dumps({"type": "robot", "connected": self._robot_connected,
                            "detail": self._robot_detail,
                            "recording": self._recording_state()})
        if frame != self._robot_state_json:
            self._robot_state_json = frame
            self._broadcast(frame)

    def _recording_state(self):
        """What the drawer says about recording: ``"off"`` (--record-buffer 0),
        ``"unsupported"`` (the publisher answered ``r`` with an error), ``"on"``,
        or ``"file"`` (``--open``: no robot)."""
        if self._file is not None:
            return "file"
        if self.recording is None:
            return "off"
        return "unsupported" if self._cannot_record else "on"

    def _tree_changed(self, header_frame):
        """True when a reply came from a different tree than the loaded layout.

        Deliberately *pure*: ``fetch_layout`` is the only writer of
        ``_tree_uuid``, so a re-handshake that fails keeps mismatching instead of
        leaving the gateway believing a tree it never loaded.
        """
        uuid = decode_tree_uuid(header_frame)
        return (uuid is not None
                and self._tree_uuid is not None
                and uuid != self._tree_uuid)

    def _broadcast_notice(self, text):
        """Push a one-off message to dashboards. Never cached, unlike every other
        frame type — see the frame table in docs/architecture.md."""
        self._broadcast(json.dumps({"type": "notice", "text": text}))

    async def _reload_tree(self, why):
        """Re-run the handshake because the tree on the robot may have changed.

        Only ``status_poller`` calls this, and only after its own ``_request``
        has released the non-reentrant ``_req_lock``, so two handshakes can never
        overlap and this cannot deadlock. ``fetch_layout`` owns the retry and
        backoff; parking here while the robot is away is correct, since every
        status buffer would have to be discarded until the new layout lands.
        """
        print(f"[klein] {why}; re-running the handshake.")
        if await self.fetch_layout():   # False when the tree came back unchanged
            # After the layout, so the canvas has already redrawn by the time the
            # note explains why.
            self._broadcast_notice(
                "The robot loaded a new behaviour tree — reloaded.")

    def open_file(self, path):
        """Show a saved ``.btlog`` instead of a robot (``--open``). Its
        ``<stem>.bb.jsonl`` sidecar beside it, if any, is the blackboard.

        Raises ``OSError``, ``BtlogError`` (not a FileLogger2 file),
        ``ET.ParseError`` or ``ValueError`` (unusable XML or sidecar). Returns
        the bytes of a trailing partial record, which are ignored.
        """
        path = Path(path)
        log = read_btlog(path.read_bytes())
        self._parse_layout(log.xml)
        sidecar = path.with_suffix(".bb.jsonl")
        try:
            text = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else None
            self.recording = load_btlog(log, self._recorded_tree, self._blackboard_names, text)
        except IndexError:                  # a record's uid past the state's end
            raise BtlogError("a record names a node uid the file's tree doesn't have")
        except (ValueError, KeyError, TypeError) as exc:     # the sidecar, not UTF-8 or not ours
            raise ValueError(f"{sidecar.name} is not a klein blackboard sidecar ({exc!r})")
        self._file = path.name
        self.streamer = Streamer(self.recording, websockets.broadcast, source="file",
                                 name=path.name)
        self._set_robot_state(False, f"No robot — viewing {path.name}")
        return log.trailing

    async def fetch_layout(self):
        """Handshake with the robot to load the tree, retrying until it works.

        klein may be started before the robot; rather than crash we back off and
        keep trying so the dashboard comes alive as soon as the robot appears.
        Any dashboards already connected are pushed the layout once it loads.

        Returns True when the tree that came back differs from the one already
        loaded, so a caller re-handshaking after a UUID change can tell a real
        swap from a robot that merely restarted with the same tree.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                reply = await self._request(REQ_FULLTREE)
                if reply and reply[0] == b"error":
                    raise ValueError("robot replied with an error frame")
                if len(reply) < 2 or not reply[1]:
                    raise ValueError("reply missing tree payload frame")
                xml_str = reply[1].decode("utf-8", errors="ignore")
                # A restarted robot publishes a fresh UUID even when it is running
                # the very same tree, and re-parsing then would retire every node
                # id and rebuild the identical canvas — a flash reporting nothing.
                changed = xml_str != self._layout_xml
                if changed:
                    self._parse_layout(xml_str)
                # Read from this same reply, so the loaded tree and the UUID the
                # status poller compares against are always one publisher's. After
                # _parse_layout, so a parse failure leaves the previous UUID in
                # place and the mismatch keeps driving the retry.
                self._tree_uuid = decode_tree_uuid(reply[0])
                self._mark_connected()
                await self._arm()           # every handshake, the unchanged-XML one too
                if not changed:
                    print(f"[klein] robot restarted with the same tree "
                          f"({self._node_seq} nodes); layout kept.")
                    return False
                print(
                    f"[klein] tree layout loaded from {self.robot_endpoint} "
                    f"({self._node_seq} nodes unrolled)."
                )
                if not self._node_categories:
                    # Once per handshake: otherwise grey nodes read as a klein bug.
                    print(
                        "[klein] robot sent no <TreeNodesModel>; node categories "
                        "fall back to the BehaviorTree.CPP builtin table.",
                        file=sys.stderr,
                    )
                self._broadcast(self._layout_json)  # dashboards that connected while we waited
                return True
            except (RobotTimeout, ValueError, ET.ParseError) as exc:
                if isinstance(exc, RobotTimeout):
                    self._timed_out = True
                wait = min(float(attempt), LAYOUT_RETRY_MAX)
                print(
                    f"[klein] waiting for robot at {self.robot_endpoint} "
                    f"({exc}); retrying in {wait:.0f}s...",
                    file=sys.stderr,
                )
                self._set_robot_state(
                    False, f"Waiting for robot at {self.robot_endpoint}…"
                )
                await asyncio.sleep(wait)

    # ------------------------------------------------------------------ #
    # Recording: arm (r start + S baseline), then one `t` drain per status poll
    # ------------------------------------------------------------------ #
    async def _arm(self):
        """Begin a recording segment: ``r start``, then a STATUS baseline.

        Run on every handshake and after an overflow. The ``r start`` reply is
        the robot's wall-clock µs, the base every drained offset is added to.
        In that order nothing is lost between the two: whatever ran after
        ``r start`` is in the robot's buffer, and what ran before the ``S`` is
        in the baseline too, so the first drain replays it on top of the
        baseline (``apply_transition`` makes that replay end on the baseline).
        A robot without recording (BT.CPP < 4.3.3) answers ``r`` with an error;
        klein then records nothing from this publisher and behaves as with
        ``--record-buffer 0``, until the next handshake tries again.
        """
        if self.recording is None:
            return
        self._armed = False
        self._cannot_record = False         # a new handshake may be a new publisher
        sent = time.monotonic()
        reply = await self._request(REQ_TOGGLE_RECORDING, RECORDING_START.encode())
        received = time.monotonic()
        self._cannot_record = len(reply) < 2 or reply[0] == b"error"
        try:
            if self._cannot_record:
                if self.recording.open_segment is not None:
                    self.recording.end_segment(self._clock.robot_us(received))
                print("[klein] robot cannot record transitions (needs BehaviorTree.CPP "
                      ">= 4.3.3); not recording.", file=sys.stderr)
                return
            start_us = int(reply[1])
            reply = await self._request(REQ_STATUS)
            if not reply or len(reply) < 2 or reply[0] == b"error":
                return
            tree = self._recorded_tree
            baseline = bytearray(tree.size)
            for uid, status in iter_status(reply[1]):
                if uid < tree.size:
                    baseline[uid] = status
            if self._timed_out and self.recording.open_segment is not None:
                # A request timed out (a blackboard poll, say) and the robot came
                # back as a new publisher before any status poll timed out: that
                # was an outage too.
                self._end_segment_at_head()
            self._timed_out = False
            previous = self.recording.segments[-1] if self.recording.segments else None
            if previous is not None and previous.t_end is not None:
                # Ended when the robot went away.
                self.recording.add_gap(previous.t_end, start_us, "outage")
            self._clock.arm(start_us, sent, received)
            self.recording.begin_segment(tree, start_us, baseline)
            self._armed = True
        finally:
            # The drawer says whether klein records. Last, so that a dashboard
            # told "on" already has the segment it records into, and one told
            # "unsupported" the end of the last one.
            self._publish_robot_state()

    async def _drain_transitions(self):
        """One ``t`` drain into the open segment. A drain of exactly the
        publisher's cap means the oldest were dropped: record an overflow gap
        and re-arm, so the new segment starts from a fresh baseline."""
        reply = await self._request(REQ_GET_TRANSITIONS)
        if len(reply) < 2 or reply[0] == b"error":
            return
        segment = self.recording.open_segment
        records = [(segment.t_begin + offset, uid, status)
                   for offset, uid, status in decode_transitions(reply[1])]
        if len(records) == TRANSITION_BUFFER_MAX:
            self.recording.add_gap(segment.last_ts, records[0][0], "overflow")
            self.recording.append(records)
            await self._arm()
        else:
            self.recording.append(records)
        now = self._clock.robot_us(time.monotonic())
        self.recording.evict(now)           # first, so the head frame has its sizes
        self.recording.advance_head(now)

    def _end_segment_at_head(self):
        """End the open segment when the robot went away: at the head, the last
        drain, which is the last time klein heard from it."""
        segment = self.recording.open_segment
        self.recording.end_segment(max(self.recording.head or segment.t_begin,
                                       segment.t_begin))

    def debug_state(self):
        """The read-only dump behind ``GET /debug/state`` (``--debug`` only)."""
        rec = self.recording
        if rec is None:
            return {"recording": False}
        segment = rec.open_segment
        return {
            "recording": self._armed,
            "head": rec.head,
            "t_min": rec.t_min,
            "segments": [{"id": s.id, "t_begin": s.t_begin, "t_end": s.t_end,
                          "start_seq": s.start_seq, "head_seq": s.head_seq,
                          "start_state": list(s.state_at_seq(s.start_seq)),
                          "layout_id": s.layout.generation} for s in rec.segments],
            "runs": [[s.id for s in run] for run in rec.runs()],
            "gaps": [list(gap) for gap in rec.gaps],
            "records": [[ts, uid, status] for s in rec.segments
                        for _seq, ts, uid, status in s.iter_records(s.start_seq, s.head_seq)],
            "state": (decode_state(segment.state, segment.layout.uids)
                      if segment is not None else None),
            "blackboard": [{"seg": s.id, "t_start": s.blackboard.t_start,
                            "boards": dict(s.blackboard.boards),
                            "changes": [list(c) for c in s.blackboard.changes()]}
                           for s in rec.segments],
            "bytes": list(rec.bytes_used()),
        }

    def _idle(self):
        """True when the pollers should wait: no dashboard is watching and there
        is nothing to record (recording off, or a publisher that can't). Not
        while an outage interrupts recording, so the robot's return is noticed."""
        return not self.clients and (self.recording is None or self._cannot_record)

    # ------------------------------------------------------------------ #
    # Telemetry: STATUS poll -> parse -> broadcast
    # ------------------------------------------------------------------ #
    async def blackboard_poller(self):
        """Poll every subtree's blackboard at 2 Hz (without clients only while
        recording), broadcast the values and add them to the recording.

        Robot reachability is deliberately *not* reported here: ``status_poller``
        already owns that at 10 Hz, and a second reporter on a different cadence
        would make the connection indicator flap. Tree-change detection is the
        status poller's alone for the same reason, plus one more: a single owner
        means two handshakes can never overlap.
        """
        while True:
            if self._idle() or not self._blackboard_request:
                await asyncio.sleep(BLACKBOARD_POLL_INTERVAL)
                continue
            try:
                # Captured before the await: a swap can land while this request
                # is in flight, and a reply for the retired tree must be dropped —
                # caching it would undo the _blackboard_json = None that
                # _parse_layout just wrote, leaving the next dashboard to connect
                # a list of dead boards that no later frame would ever clear.
                names = self._blackboard_names
                generation = self._layout_generation
                reply = await self._request(REQ_BLACKBOARD, self._blackboard_request)
                if (self._layout_generation == generation
                        and reply and len(reply) >= 2 and reply[0] != b"error"):
                    boards = parse_blackboard(reply[1], names)
                    # Broadcast even when empty, so the dashboard can say so.
                    self._blackboard_json = json.dumps(
                        {"type": "blackboard", "data": boards}
                    )
                    self._broadcast(self._blackboard_json)
                    if self._armed:
                        self.recording.add_blackboard(
                            self._clock.robot_us(time.monotonic()), boards)
            except RobotTimeout:
                # status_poller reports the outage; values just stop updating.
                self._timed_out = True
            except Exception as exc:  # never let the poller die
                print(f"[klein] blackboard poller error: {exc}", file=sys.stderr)
            await asyncio.sleep(BLACKBOARD_POLL_INTERVAL)

    def _broadcast_status(self, buffer):
        """Decode one status buffer and push it to dashboards."""
        updates = parse_status(buffer)
        if updates:
            self._broadcast(json.dumps({"type": "status", "data": updates}))

    async def status_poller(self):
        """Poll the robot at 10 Hz (without clients only while recording),
        broadcast parsed status frames, and drain transitions after each.

        Also the sole owner of tree-change detection: the handshake is re-run,
        before any further status is believed, when a reply's tree UUID no longer
        matches the loaded layout, and again whenever telemetry resumes after an
        outage — a robot that went away and came back may be a different process
        running a different tree.
        """
        while True:
            if self._idle():
                await asyncio.sleep(POLL_INTERVAL)
                continue
            try:
                reply = await self._request(REQ_STATUS)
                if reply and len(reply) >= 2 and reply[0] != b"error":
                    # Either way the buffer is dropped rather than broadcast: its
                    # UIDs index a tree the dashboard may not have been sent, so
                    # they could land on the previous tree's cards.
                    if self._tree_changed(reply[0]):
                        await self._reload_tree("robot published a different tree")
                    elif not self._robot_connected:
                        # Telemetry resumed after an outage, which is what killing
                        # a robot and starting another looks like from here. The
                        # UUID check above should already have caught a swap, but
                        # it trusts the robot to draw a fresh UUID per process;
                        # an outage is evidence klein owns, so re-handshake on it
                        # too rather than let one implementation detail decide
                        # whether the dashboard is showing the right tree. Costs
                        # one FULLTREE, and fetch_layout keeps the layout — no
                        # re-render, no notice — when the XML comes back the same.
                        await self._reload_tree("robot telemetry resumed")
                    else:
                        self._broadcast_status(reply[1])
                        self._timed_out = False     # any timeout before this was a blip
                        if self._armed:
                            await self._drain_transitions()
            except RobotTimeout:
                self._timed_out = True
                if self._robot_connected:
                    print("[klein] robot status poll timed out; retrying...",
                          file=sys.stderr)
                if self.recording is not None and self.recording.open_segment is not None:
                    # The resume re-handshake opens the next segment and the gap.
                    self._end_segment_at_head()
                    self._armed = False
                self._set_robot_state(
                    False, f"Lost connection to robot at {self.robot_endpoint} — retrying…"
                )
            except Exception as exc:  # never let the poller die
                print(f"[klein] poller error: {exc}", file=sys.stderr)
            await asyncio.sleep(POLL_INTERVAL)

    # ------------------------------------------------------------------ #
    # HTTP + WebSocket on one port
    # ------------------------------------------------------------------ #
    @staticmethod
    def _http_response(status, body, content_type, attachment=None):
        """``attachment`` is a filename: the browser saves the body under it."""
        headers = Headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        headers["Cache-Control"] = "no-store"
        if attachment is not None:
            headers["Content-Disposition"] = f'attachment; filename="{attachment}"'
        return Response(status, HTTPStatus(status).phrase, headers, body)

    def log_runs(self):
        """``[(run, entry)]``: each tree run of the recording with its
        ``GET /log/runs`` entry. Run indices count from the oldest retained run,
        so they shift when eviction drops one. ``blackboard`` is False when the
        run has no blackboard sample (a file opened without its sidecar): its
        ``/log.bb.jsonl`` is then a 404, not a header-only sidecar that would
        reopen as empty boards. Two runs of one tree that start in the same
        second get ``_2``, ``_3``… after the time, so every name (and the
        zip's entries) stays unique."""
        out = []
        used = set()
        for i, run in enumerate(self.recording.runs()):
            start = run[0].t_start
            tree_id = run[0].layout.tree["root_tree_id"]
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(start / 1e6))
            stem = base = f"{tree_id}_{stamp}"
            n = 1
            while stem in used:
                n += 1
                stem = f"{base}_{n}"
            used.add(stem)
            out.append((run, {"run": i, "tree_id": tree_id, "t_begin": start,
                              "t_end": run[-1].t_end,
                              "filename": f"{stem}.btlog",
                              "blackboard": any(s.blackboard.t_start is not None
                                                for s in run)}))
        return out

    @staticmethod
    def _log_files(run, entry, suffixes=_LOG_SUFFIXES):
        """Run ``run``'s downloads among ``suffixes``, ``{suffix: (filename,
        bytes)}``: its ``.btlog`` and, when it has a blackboard, its
        ``.bb.jsonl`` under the same stem."""
        stem = entry["filename"].removesuffix(".btlog")
        files = {}
        if ".btlog" in suffixes:
            files[".btlog"] = (entry["filename"], export_run(run, run[0].layout.xml))
        if ".bb.jsonl" in suffixes and entry["blackboard"]:
            files[".bb.jsonl"] = (stem + ".bb.jsonl", export_blackboard(run, entry["tree_id"]))
        return files

    async def _download(self, runs, suffix, name=None):
        """Export ``runs`` (``[(run, entry)]``, snapshots) in a worker thread:
        the one run's ``suffix`` file, or with ``suffix`` None every run's
        files as one deflated ``.zip`` named ``name``. The ``process_request``
        hook awaits it."""
        def build():
            if suffix is not None:
                (run, entry), = runs
                return self._log_files(run, entry, (suffix,))[suffix]
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for run, entry in runs:
                    for filename, data in self._log_files(run, entry).values():
                        archive.writestr(filename, data)
            return name, buffer.getvalue()
        filename, body = await asyncio.to_thread(build)
        return self._http_response(200, body, _LOG_TYPES[suffix], filename)

    def _log_response(self, path, query):
        """``GET /log/runs``, ``/log.zip``, ``/log.btlog?run=N`` and
        ``/log.bb.jsonl?run=N``. Each request reads the runs once and
        snapshots them in one go (no await), so eviction can't change them
        halfway through; a download returns a coroutine (``_download``) that
        exports the snapshots off the event loop."""
        runs = self.log_runs()
        if path == "/log/runs":
            body = json.dumps([entry for _run, entry in runs]).encode()
            return self._http_response(200, body, "application/json")
        if path == "/log.zip":
            name = time.strftime("klein_%Y-%m-%d_%H-%M-%S.zip")
            return self._download([(snapshot_run(run), entry) for run, entry in runs],
                                  None, name)
        index = parse_qs(query).get("run", [""])[0]
        if not index.isdigit() or int(index) >= len(runs):
            return self._http_response(404, b"no such run", "text/plain; charset=utf-8")
        run, entry = runs[int(index)]
        suffix = path.removeprefix("/log")
        if suffix == ".bb.jsonl" and not entry["blackboard"]:  # e.g. a file opened without one
            return self._http_response(404, b"no blackboard in this run",
                                       "text/plain; charset=utf-8")
        return self._download([(snapshot_run(run), entry)], suffix)

    def _process_request(self, connection, request):
        """Serve the dashboard's static files (HTML, CSS, JS, D3) and the
        recording's downloads over plain HTTP; let ``/ws`` upgrade to a WebSocket. Runs for every incoming
        connection before the handshake."""
        url = urlsplit(request.path)
        path = url.path
        if path == "/ws":
            return None  # not an HTTP response -> proceed with the WS upgrade
        if path == "/debug/state" and self.debug:
            body = json.dumps(self.debug_state()).encode()
            return self._http_response(200, body, "application/json")
        if path in _LOG_ROUTES and self.recording is not None:
            return self._log_response(path, url.query)
        if path == "/":
            path = "/index.html"
        asset = self._static.get(path)
        if asset is None:
            return self._http_response(404, b"not found", "text/plain; charset=utf-8")
        body, content_type = asset
        return self._http_response(200, body, content_type)

    async def ws_handler(self, websocket):
        """Push the recording's backfill and the layout on connect (if loaded),
        then keep the connection open for broadcasts."""
        self.clients.add(websocket)
        print(f"[klein] dashboard connected ({len(self.clients)} active).")
        try:
            if self.streamer is not None:
                self.streamer.subscribe(websocket)  # synchronous: nothing can overtake it
            if self._layout_json is not None:
                await websocket.send(self._layout_json)
            # else: fetch_layout() will broadcast the layout to us once it loads.
            if self._blackboard_json is not None:
                # Don't make a new dashboard wait half a second for its first values.
                await websocket.send(self._blackboard_json)
            await websocket.send(self._robot_state_json)  # tell it the robot's reachability now
            async for _message in websocket:
                pass  # clients are receive-only
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            if self.streamer is not None:
                self.streamer.unsubscribe(websocket)
            print(f"[klein] dashboard disconnected ({len(self.clients)} active).")

    def _load_static(self):
        """Load the packaged static files into memory once, keyed by URL path.

        A missing ``index.html`` falls back to a stub page; a missing script
        (D3 or one of the dashboard's) just means that route 404s (and the
        dashboard can't render).
        """
        for path, (filename, content_type) in _STATIC_ROUTES.items():
            body = _load_asset(filename)
            if body is not None:
                self._static[path] = (body, content_type)
        self._static.setdefault(
            "/index.html", (_INDEX_FALLBACK, "text/html; charset=utf-8")
        )
        # The dashboard needs D3 and all of its own scripts to render at all.
        for asset in _STATIC_FILES:
            if asset.endswith(".js") and f"/{asset}" not in self._static:
                print(f"[klein] warning: {asset} not bundled; dashboard will not render.",
                      file=sys.stderr)

    async def run(self, open_browser=False):
        """Serve HTTP+WebSocket on one port; load the layout and poll forever."""
        # Stray non-WebSocket TCP connections to the port (health checks, port
        # scans) otherwise dump noisy handshake tracebacks; we report real
        # client connects/disconnects ourselves, so silence the library log.
        logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

        self._load_static()

        # The server starts accepting connections here, so the dashboard page
        # loads (and shows "connecting…") even while we wait for the robot.
        async with websockets.serve(
            self.ws_handler, "0.0.0.0", self.port,
            process_request=self._process_request,
            # An HTTP answer is part of the opening handshake, which this
            # bounds (default 10 s): a Save of a full 200 MiB recording takes
            # ~15 s to export and compress, and would be cut off unanswered.
            open_timeout=60,
        ):
            print(f"[klein] dashboard + telemetry live on http://localhost:{self.port}")
            # The server is now listening, so it's safe to open the browser — do
            # it off-thread so a slow launcher can't stall the event loop.
            if open_browser:
                url = f"http://localhost:{self.port}"
                asyncio.get_running_loop().run_in_executor(None, _open_browser, url)
            if self._file is not None:
                await asyncio.Future()      # an opened file: nothing to poll
            await self.fetch_layout()      # retries until the robot answers
            # Both pollers run forever, sharing the REQ socket via _req_lock.
            await asyncio.gather(self.status_poller(), self.blackboard_poller())


# --------------------------------------------------------------------------- #
# Browser launch (the CLI's --no-browser turns it off)
# --------------------------------------------------------------------------- #
def _open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless / no browser available — the printed link still works
