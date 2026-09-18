"""klein.gateway — ZeroMQ→WebSocket telemetry gateway for BehaviorTree.CPP v4.

klein connects to a running BehaviorTree.CPP robot node exposing the Groot2
publisher protocol (a ``ZMQ_REP`` socket, default port 1667), performs the
FULLTREE handshake, recursively unrolls nested subtrees into a single tree, and
then streams 10 Hz status telemetry — plus 2 Hz blackboard values, one board per
subtree — to browser dashboards over WebSockets. A robot that loads a
*different* tree is noticed on the next poll, from the tree UUID every reply
carries, and the handshake is re-run.

Everything the browser needs is served from a **single port** (``--port``):

* plain HTTP for the dashboard (``/`` and ``/index.html``), its ``/styles.css``,
  ``/app.js`` and ``/renderers.js``, and the bundled D3.js (``/d3.v7.min.js``), and
* a WebSocket endpoint (``/ws``) that pushes the unrolled tree layout on connect
  and then broadcasts live status and blackboard frames.

Serving both from one origin means the dashboard just opens
``ws://<same-host:port>/ws`` — no port to configure, inject, or firewall twice.

The browser can only speak HTTP/WebSocket — never ZeroMQ — so klein is the
translator between the robot's REQ/REP world and the browser's push world.
"""

import argparse
import asyncio
import json
import logging
import math
import random
import socket
import struct
import sys
import webbrowser
import xml.etree.ElementTree as ET
from http import HTTPStatus
from pathlib import Path

import msgpack
import zmq
import zmq.asyncio
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from .groot2_protocol import (
    BUILTIN_CATEGORIES,
    CATEGORY_SUBTREE,
    CATEGORY_UNDEFINED,
    HEADER_FORMAT,
    NODE_CATEGORIES,
    PROTOCOL_ID,
    REQ_BLACKBOARD,
    REQ_FULLTREE,
    REQ_STATUS,
    STATUS_RECORD_FORMAT,
    STATUS_RECORD_SIZE,
    decode_status,
    decode_tree_uuid,
)

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"    # dashboard web assets live here, not beside the .py

# Timeouts / cadence
POLL_INTERVAL = 0.1             # 10 Hz status poll
BLACKBOARD_POLL_INTERVAL = 0.5  # 2 Hz blackboard poll — values change slower than status
REQUEST_TIMEOUT = 2.0           # seconds to wait for a robot reply
LAYOUT_RETRY_MAX = 5.0          # cap on handshake retry backoff

# Static files served over HTTP, keyed by request path ("/" -> "/index.html").
_STATIC_ROUTES = {
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/renderers.js": ("renderers.js", "text/javascript; charset=utf-8"),
    "/d3.v7.min.js": ("d3.v7.min.js", "text/javascript; charset=utf-8"),
}
_INDEX_FALLBACK = b"<!doctype html><h1>klein: index.html missing from package</h1>"


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

    def __init__(self, robot_host, robot_port, port):
        self.robot_endpoint = f"tcp://{robot_host}:{robot_port}"
        self.port = port

        self.ctx = zmq.asyncio.Context()
        self.socket = None                  # created lazily / recreated on fault
        self._req_lock = asyncio.Lock()     # REQ/REP is strictly send→recv

        self.all_behavior_trees = {}        # tree_id -> root <element> of that block
        self._node_categories = {}          # registration name -> category, from <TreeNodesModel>
        self.tree_structure = None          # unrolled nested dict sent to clients
        self._layout_json = None            # cached layout frame, rebuilt each handshake
        self.clients = set()

        # Blackboards: one per subtree instance, named by the paths in the layout.
        self._blackboard_names = []          # subtree instance paths, in tree order
        self._blackboard_request = None      # pre-encoded b"name1;name2" request payload
        self._blackboard_json = None         # cached last frame, for late-joining clients

        self._node_seq = 0                  # per-tree node counter (uid may be null)
        self._layout_generation = 0         # bumped per handshake; prefixes node ids
        self._tree_uuid = None              # publisher UUID the loaded layout came from
        self._layout_xml = None             # raw FULLTREE XML, to spot an unchanged tree

        # Robot reachability, mirrored to dashboards so a blank canvas is never
        # ambiguous. Starts "not connected" until the first successful handshake.
        self._robot_connected = False
        self._robot_detail = f"Connecting to robot at {self.robot_endpoint}…"
        self._robot_state_json = json.dumps(
            {"type": "robot", "connected": False, "detail": self._robot_detail}
        )

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
    def _next_id(self):
        """A node id unique across handshakes, not just within one tree.

        ``_node_seq`` restarts at 1 per tree (it doubles as the unrolled node
        count), so the generation prefix is what keeps two trees' id sets
        disjoint for the dashboard's keyed join — see docs/architecture.md.
        """
        self._node_seq += 1
        return f"{self._layout_generation}:{self._node_seq}"

    @staticmethod
    def extract_uid(element):
        """Return the integer node UID from a layout element, or None.

        BehaviorTree.CPP embeds the runtime UID as the ``_uid`` attribute
        (``uid`` is accepted as a fallback). The ``ID`` attribute is a subtree
        *name*, not a UID, so it must not shadow ``_uid``.
        """
        uid_str = element.get("_uid") or element.get("uid")
        if uid_str is not None and uid_str.lstrip("-").isdigit():
            return int(uid_str)
        return None

    # Structural attributes: klein renders these itself (name, ID) or uses them
    # to wire the tree up (_uid, _fullpath). Everything else the robot stamped
    # on the element is a port the tree author wrote — a Precondition's `if`, a
    # RetryUntilSuccessful's `num_attempts`, a Switch's cases, a subtree's port
    # remapping — and is what the node card shows. That includes the scripting
    # hooks BT.CPP serializes out of a node's pre/post-conditions (`_skipIf`,
    # `_while`, `_onSuccess`, …): underscored, but the author's writing.
    STRUCTURAL_ATTRS = frozenset({"name", "ID", "uid", "_uid", "_fullpath"})

    @classmethod
    def extract_ports(cls, element):
        """Return the element's port attributes, in document order."""
        return {
            key: value
            for key, value in element.attrib.items()
            if key not in cls.STRUCTURAL_ATTRS
        }

    def unroll_node(self, element, expanding=frozenset()):
        """Recursively convert a layout element into a nested dict.

        ``<SubTree ID="X">`` references are stitched in place: the SubTree node
        keeps its own UID and gains the matching ``<BehaviorTree>`` definition
        as its child, so *every* UID — the reference and all inner nodes — maps
        cleanly onto incoming status packets. ``expanding`` guards against
        cyclic subtree references.
        """
        node_type = element.tag

        if node_type == "SubTree":
            subtree_id = element.get("ID")
            node = {
                "id": self._next_id(),
                "uid": self.extract_uid(element),
                "type": "SubTree",
                "category": CATEGORY_SUBTREE,
                "name": element.get("name") or subtree_id or "SubTree",
                "subtree_id": subtree_id,
                "is_subtree_root": True,
                # This instance's blackboard, named exactly as
                # extract_blackboard_names asks the robot for it — the dashboard
                # pairs each board with the node that owns it.
                "board": element.get("_fullpath") or subtree_id,
                "ports": self.extract_ports(element),
                "children": [],
            }
            subtree_root = self.all_behavior_trees.get(subtree_id)
            if subtree_root is not None and subtree_id not in expanding:
                node["children"] = [
                    self.unroll_node(subtree_root, expanding | {subtree_id})
                ]
            return node

        return {
            "id": self._next_id(),
            "uid": self.extract_uid(element),
            "type": node_type,
            "category": self._category_for(element),
            "name": element.get("name") or node_type,
            "ports": self.extract_ports(element),
            "children": [self.unroll_node(child, expanding) for child in element],
        }

    @staticmethod
    def parse_node_categories(root):
        """Return ``{registration name: category}`` from ``<TreeNodesModel>``.

        An entry's tag is the category and its ``ID`` is the registration name
        instance elements use as their own tag — see docs/protocol.md. Entries
        with no ``ID``, and tags that are not categories (``<MetadataFields>``),
        are skipped rather than trusted.
        """
        categories = {}
        for model in root.findall("TreeNodesModel"):
            for entry in model:
                registration_id = entry.get("ID")
                if registration_id and entry.tag in NODE_CATEGORIES:
                    categories[registration_id] = entry.tag
        return categories

    def _category_for(self, element):
        """Return one instance element's category, most authoritative source first:
        a tag that is itself a category (the explicit ``<Action ID="OpenDoor"/>``
        spelling), then the robot's ``<TreeNodesModel>``, then the nodes
        BehaviorTree.CPP registers on itself, else ``Undefined``.

        Never guessed from the tree's shape — see docs/protocol.md.
        """
        tag = element.tag
        if tag in NODE_CATEGORIES:
            return tag
        return (self._node_categories.get(tag)
                or BUILTIN_CATEGORIES.get(tag)
                or CATEGORY_UNDEFINED)

    @staticmethod
    def extract_blackboard_names(root):
        """Return the blackboard names to ask the robot for, in tree order.

        Every subtree instance owns a blackboard, and the publisher registers it
        under the subtree's *instance path* — which BehaviorTree.CPP stamps as
        ``_fullpath`` on each ``<BehaviorTree>`` block and on the ``<SubTree>``
        element referencing it. The root subtree's path is empty (it registers
        under its tree ID instead), and older robots omit ``_fullpath``
        altogether, hence the ``ID`` fallback. Names may repeat across a block
        and its reference, so duplicates are dropped.

        Only nodes *inside* ``<BehaviorTree>`` blocks are considered: a FULLTREE
        reply also carries a ``<TreeNodesModel>`` section that declares the node
        types, and its ``<SubTree>`` entry is a model, not an instance.
        """
        blocks = root.findall(".//BehaviorTree")
        elements = list(blocks)
        for block in blocks:
            elements.extend(block.iter("SubTree"))

        names = []
        seen = set()
        for element in elements:
            name = element.get("_fullpath") or element.get("ID")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def _parse_layout(self, xml_str):
        """Parse FULLTREE XML and build the unrolled tree structure."""
        root = ET.fromstring(xml_str)
        self._layout_xml = xml_str      # what the current layout was built from

        # Built before unrolling, because unroll_node stamps each node's
        # category from it. Rebuilt per handshake, so re-handshaking against a
        # different tree never carries the previous tree's node types over.
        self._node_categories = self.parse_node_categories(root)

        self.all_behavior_trees = {}
        block_paths = {}            # tree ID -> that block's _fullpath, for the root's board
        first_tree_id = None
        for bt_block in root.findall(".//BehaviorTree"):
            tree_id = bt_block.get("ID")
            if not tree_id:
                continue
            children = list(bt_block)
            self.all_behavior_trees[tree_id] = children[0] if children else None
            block_paths[tree_id] = bt_block.get("_fullpath")
            if first_tree_id is None:
                first_tree_id = tree_id

        # Prefer an explicit entrypoint if the XML declares one; otherwise the
        # first <BehaviorTree> block is the main tree.
        main_tree_id = root.get("main_tree_to_execute") or first_tree_id
        if main_tree_id not in self.all_behavior_trees:
            main_tree_id = first_tree_id

        if main_tree_id is None or self.all_behavior_trees.get(main_tree_id) is None:
            raise ValueError("layout XML contains no usable <BehaviorTree> block")

        self._layout_generation += 1
        self._node_seq = 0
        self.tree_structure = self.unroll_node(self.all_behavior_trees[main_tree_id])
        self.tree_structure["root_tree_id"] = main_tree_id
        # The root's own blackboard, taken from *its* block rather than the first
        # one — main_tree_to_execute need not point at the first <BehaviorTree>.
        # Real robots leave the root's _fullpath empty, so this falls through to
        # the tree ID, exactly as extract_blackboard_names does.
        self.tree_structure["board"] = block_paths.get(main_tree_id) or main_tree_id
        # Serialize once: the layout is immutable until the next handshake, so
        # every connecting (or reconnecting) client is sent this same frame.
        self._layout_json = json.dumps({"type": "layout", "data": self.tree_structure})

        self._blackboard_names = self.extract_blackboard_names(root)
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
        """Record robot reachability and push it to dashboards on change.

        The latest state is cached so a dashboard that connects later is told
        immediately whether the robot is reachable (see ``ws_handler``). Only
        real changes are broadcast, so this is safe to call on the 10 Hz path.
        """
        if connected == self._robot_connected and detail == self._robot_detail:
            return
        self._robot_connected = connected
        self._robot_detail = detail
        self._robot_state_json = json.dumps(
            {"type": "robot", "connected": connected, "detail": detail}
        )
        self._broadcast(self._robot_state_json)

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
    # Telemetry: STATUS poll -> parse -> broadcast
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_status(buffer):
        """Unpack a status buffer into ``{node_uid: {"status", "from"}}``.

        ``from`` is the previous status name for an idle-transition record, else
        ``None`` (see ``groot2_protocol.decode_status``). A trailing partial
        record is ignored.
        """
        usable = len(buffer) - (len(buffer) % STATUS_RECORD_SIZE)
        records = struct.iter_unpack(STATUS_RECORD_FORMAT, memoryview(buffer)[:usable])
        updates = {}
        for node_uid, status_int in records:
            status, transitioned_from = decode_status(status_int)
            updates[node_uid] = {"status": status, "from": transitioned_from}
        return updates

    @staticmethod
    def _json_safe(value):
        """Coerce a decoded msgpack value into something ``json.dumps`` accepts.

        Robots can hand us values Python will serialize into JSON the browser
        then refuses: a non-finite float becomes bare ``NaN``/``Infinity``, which
        ``JSON.parse`` rejects — killing not just this frame but the dashboard's
        whole message stream. Raw bytes are equally unserializable.
        """
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {str(k): KleinGateway._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [KleinGateway._json_safe(v) for v in value]
        return value

    @staticmethod
    def parse_blackboard(buffer, order=None):
        """Decode a msgpack blackboard dump into ``{board_name: {key: value}}``.

        The publisher replies with msgpack nil when no requested name matched a
        live subtree, which decodes to ``None`` — reported here as no boards at
        all. Keys starting with ``_`` are private by BehaviorTree.CPP's own
        convention — autoremapping skips them, so a subtree keeps them to itself
        — and they are dropped here rather than in the browser so the 2 Hz frame
        stays small and the dashboard needs no knowledge of that convention.

        The robot walks an unordered map, so its reply order is arbitrary.
        ``order`` (the names we asked for, in tree order) restores a stable,
        meaningful order — root tree first — that the dashboard renders as-is.

        A subtree whose every port is remapped to its parent holds nothing of
        its own, and the publisher sends nil for it rather than an empty map.
        Such a board is reported as empty, not dropped: the dashboard should say
        the subtree has no entries rather than omit the subtree.
        """
        boards = msgpack.unpackb(buffer, raw=False, strict_map_key=False)
        if not isinstance(boards, dict):
            return {}
        boards = {str(name): entries for name, entries in boards.items()}
        names = [name for name in (order or ()) if name in boards]
        names += [name for name in boards if name not in names]   # anything extra
        parsed = {}
        for name in names:
            entries = boards[name]
            parsed[name] = {} if not isinstance(entries, dict) else {
                str(key): KleinGateway._json_safe(value)
                for key, value in entries.items()
                if not str(key).startswith("_")
            }
        return parsed

    async def blackboard_poller(self):
        """Poll every subtree's blackboard at 2 Hz (only while clients are
        watching) and broadcast the values.

        Robot reachability is deliberately *not* reported here: ``status_poller``
        already owns that at 10 Hz, and a second reporter on a different cadence
        would make the connection indicator flap. Tree-change detection is the
        status poller's alone for the same reason, plus one more: a single owner
        means two handshakes can never overlap.
        """
        while True:
            if not self.clients or not self._blackboard_request:
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
                    boards = self.parse_blackboard(reply[1], names)
                    # Broadcast even when empty, so the dashboard can say so.
                    self._blackboard_json = json.dumps(
                        {"type": "blackboard", "data": boards}
                    )
                    self._broadcast(self._blackboard_json)
            except RobotTimeout:
                pass  # status_poller reports the outage; values just stop updating
            except Exception as exc:  # never let the poller die
                print(f"[klein] blackboard poller error: {exc}", file=sys.stderr)
            await asyncio.sleep(BLACKBOARD_POLL_INTERVAL)

    def _broadcast_status(self, buffer):
        """Decode one status buffer and push it to dashboards."""
        updates = self.parse_status(buffer)
        if updates:
            self._broadcast(json.dumps({"type": "status", "data": updates}))

    async def status_poller(self):
        """Poll the robot at 10 Hz (only while clients are watching) and
        broadcast parsed status frames.

        Also the sole owner of tree-change detection: the handshake is re-run,
        before any further status is believed, when a reply's tree UUID no longer
        matches the loaded layout, and again whenever telemetry resumes after an
        outage — a robot that went away and came back may be a different process
        running a different tree.
        """
        while True:
            if not self.clients:
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
            except RobotTimeout:
                if self._robot_connected:
                    print("[klein] robot status poll timed out; retrying...",
                          file=sys.stderr)
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
    def _http_response(status, body, content_type):
        headers = Headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        headers["Cache-Control"] = "no-store"
        return Response(status, HTTPStatus(status).phrase, headers, body)

    def _process_request(self, connection, request):
        """Serve the dashboard's static files (HTML, CSS, JS, D3) over plain
        HTTP; let ``/ws`` upgrade to a WebSocket. Runs for every incoming
        connection before the handshake."""
        path = request.path.split("?", 1)[0]
        if path == "/ws":
            return None  # not an HTTP response -> proceed with the WS upgrade
        if path == "/":
            path = "/index.html"
        asset = self._static.get(path)
        if asset is None:
            return self._http_response(404, b"not found", "text/plain; charset=utf-8")
        body, content_type = asset
        return self._http_response(200, body, content_type)

    async def ws_handler(self, websocket):
        """Push the layout on connect (if loaded), then keep the connection open
        for broadcasts."""
        self.clients.add(websocket)
        print(f"[klein] dashboard connected ({len(self.clients)} active).")
        try:
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
            print(f"[klein] dashboard disconnected ({len(self.clients)} active).")

    def _load_static(self):
        """Load the packaged static files into memory once, keyed by URL path.

        A missing ``index.html`` falls back to a stub page; a missing
        render-critical asset (D3 or the dashboard script) just means that route
        404s (and the dashboard can't render).
        """
        for path, (filename, content_type) in _STATIC_ROUTES.items():
            body = _load_asset(filename)
            if body is not None:
                self._static[path] = (body, content_type)
        self._static.setdefault(
            "/index.html", (_INDEX_FALLBACK, "text/html; charset=utf-8")
        )
        # The dashboard needs D3 and both of its own scripts to render at all.
        for asset in ("d3.v7.min.js", "app.js", "renderers.js"):
            if f"/{asset}" not in self._static:
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
        ):
            print(f"[klein] dashboard + telemetry live on http://localhost:{self.port}")
            # The server is now listening, so it's safe to open the browser — do
            # it off-thread so a slow launcher can't stall the event loop.
            if open_browser:
                url = f"http://localhost:{self.port}"
                asyncio.get_running_loop().run_in_executor(None, _open_browser, url)
            await self.fetch_layout()      # retries until the robot answers
            # Both pollers run forever, sharing the REQ socket via _req_lock.
            await asyncio.gather(self.status_poller(), self.blackboard_poller())


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def _open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless / no browser available — the printed link still works


def _port_available(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Match the asyncio server's bind semantics (SO_REUSEADDR): reject a port
    # only when a live listener holds it, not when harmless TIME_WAIT sockets
    # linger from a just-closed session (which would block an immediate restart).
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main_cli():
    parser = argparse.ArgumentParser(
        prog="klein-bt",
        description="Live BehaviorTree.CPP v4 telemetry dashboard over ZeroMQ.",
    )
    parser.add_argument("--robot-host", default="127.0.0.1",
                        help="IP address of the C++ robot node (default: 127.0.0.1)")
    parser.add_argument("--robot-port", type=int, default=1667,
                        help="ZeroMQ REQ/REP port on the robot (default: 1667)")
    parser.add_argument("--port", type=int, default=8080,
                        help="port for the klein dashboard + telemetry server (default: 8080)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not auto-open the system browser")
    args = parser.parse_args()

    if not _port_available(args.port):
        print(f"[klein] port {args.port} is already in use — try a different --port.",
              file=sys.stderr)
        sys.exit(1)

    print()
    print("  klein — BehaviorTree.CPP telemetry")
    print(f"  robot     : tcp://{args.robot_host}:{args.robot_port}")
    print(f"  dashboard : http://localhost:{args.port}")
    print()

    gateway = KleinGateway(args.robot_host, args.robot_port, args.port)
    try:
        # The browser is opened from inside run(), once the server is listening,
        # so it never races ahead of the socket being ready.
        asyncio.run(gateway.run(open_browser=not args.no_browser))
    except KeyboardInterrupt:
        print("\n[klein] shutting down.")


if __name__ == "__main__":
    main_cli()
