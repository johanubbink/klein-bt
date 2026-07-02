"""klein.gateway — ZeroMQ→WebSocket telemetry gateway for BehaviorTree.CPP v4.

klein connects to a running BehaviorTree.CPP robot node exposing the Groot2
publisher protocol (a ``ZMQ_REP`` socket, default port 1667), performs the
FULLTREE handshake, recursively unrolls nested subtrees into a single tree, and
then streams 10 Hz status telemetry to browser dashboards over WebSockets.

Everything the browser needs is served from a **single port** (``--port``):

* plain HTTP for the dashboard (``/`` and ``/index.html``) and the bundled D3.js
  (``/d3.v7.min.js``), and
* a WebSocket endpoint (``/ws``) that pushes the unrolled tree layout on connect
  and then broadcasts live status frames.

Serving both from one origin means the dashboard just opens
``ws://<same-host:port>/ws`` — no port to configure, inject, or firewall twice.

The browser can only speak HTTP/WebSocket — never ZeroMQ — so klein is the
translator between the robot's REQ/REP world and the browser's push world.
"""

import argparse
import asyncio
import json
import logging
import random
import socket
import struct
import sys
import webbrowser
import xml.etree.ElementTree as ET
from http import HTTPStatus
from pathlib import Path

import zmq
import zmq.asyncio
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from .groot2_protocol import (
    HEADER_FORMAT,
    PROTOCOL_ID,
    REQ_FULLTREE,
    REQ_STATUS,
    STATUS_RECORD_FORMAT,
    STATUS_RECORD_SIZE,
    decode_status,
)

PACKAGE_DIR = Path(__file__).resolve().parent

# Timeouts / cadence
POLL_INTERVAL = 0.1             # 10 Hz status poll
REQUEST_TIMEOUT = 2.0           # seconds to wait for a robot reply
LAYOUT_RETRY_MAX = 5.0          # cap on handshake retry backoff

# Static files served over HTTP, keyed by request path ("/" -> "/index.html").
_STATIC_ROUTES = {
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/d3.v7.min.js": ("d3.v7.min.js", "text/javascript; charset=utf-8"),
}
_INDEX_FALLBACK = b"<!doctype html><h1>klein: index.html missing from package</h1>"


class RobotTimeout(Exception):
    """Raised when a robot request times out or the REQ socket faults."""


def _load_asset(name):
    """Read a packaged static asset as bytes, or None if it is missing."""
    try:
        return (PACKAGE_DIR / name).read_bytes()
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
        self.tree_structure = None          # unrolled nested dict sent to clients
        self._layout_json = None            # cached layout frame, rebuilt each handshake
        self.clients = set()

        self._node_seq = 0                  # stable per-node id (uid may be null)

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

    async def _request(self, request_type):
        """Send one request and return the multipart reply frames.

        Replies are 2-frame multipart messages: frame 0 is a 22-byte reply
        header, frame 1 is the payload (tree XML or status buffer). On any
        timeout/fault the socket is recreated and ``RobotTimeout`` is raised.
        """
        async with self._req_lock:
            if self.socket is None:
                self._new_socket()
            unique_id = random.randint(1, 0xFFFFFFFF)
            header = struct.pack(HEADER_FORMAT, PROTOCOL_ID, request_type, unique_id)
            try:
                await self.socket.send_multipart([header])
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
        self._node_seq += 1
        return self._node_seq

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
                "name": element.get("name") or subtree_id or "SubTree",
                "subtree_id": subtree_id,
                "is_subtree_root": True,
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
            "name": element.get("name") or node_type,
            "children": [self.unroll_node(child, expanding) for child in element],
        }

    def _parse_layout(self, xml_str):
        """Parse FULLTREE XML and build the unrolled tree structure."""
        root = ET.fromstring(xml_str)

        self.all_behavior_trees = {}
        first_tree_id = None
        for bt_block in root.findall(".//BehaviorTree"):
            tree_id = bt_block.get("ID")
            if not tree_id:
                continue
            children = list(bt_block)
            self.all_behavior_trees[tree_id] = children[0] if children else None
            if first_tree_id is None:
                first_tree_id = tree_id

        # Prefer an explicit entrypoint if the XML declares one; otherwise the
        # first <BehaviorTree> block is the main tree.
        main_tree_id = root.get("main_tree_to_execute") or first_tree_id
        if main_tree_id not in self.all_behavior_trees:
            main_tree_id = first_tree_id

        if main_tree_id is None or self.all_behavior_trees.get(main_tree_id) is None:
            raise ValueError("layout XML contains no usable <BehaviorTree> block")

        self._node_seq = 0
        self.tree_structure = self.unroll_node(self.all_behavior_trees[main_tree_id])
        self.tree_structure["root_tree_id"] = main_tree_id
        # Serialize once: the layout is immutable until the next handshake, so
        # every connecting (or reconnecting) client is sent this same frame.
        self._layout_json = json.dumps({"type": "layout", "data": self.tree_structure})

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
        if self.clients:
            websockets.broadcast(self.clients, self._robot_state_json)

    async def fetch_layout(self):
        """Handshake with the robot to load the tree, retrying until it works.

        klein may be started before the robot; rather than crash we back off and
        keep trying so the dashboard comes alive as soon as the robot appears.
        Any dashboards already connected are pushed the layout once it loads.
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
                self._parse_layout(reply[1].decode("utf-8", errors="ignore"))
                print(
                    f"[klein] tree layout loaded from {self.robot_endpoint} "
                    f"({self._node_seq} nodes unrolled)."
                )
                self._set_robot_state(True, f"Connected to robot at {self.robot_endpoint}")
                if self.clients:  # push to dashboards that connected while we waited
                    websockets.broadcast(self.clients, self._layout_json)
                return
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

    async def status_poller(self):
        """Poll the robot at 10 Hz (only while clients are watching) and
        broadcast parsed status frames."""
        while True:
            if not self.clients:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            try:
                reply = await self._request(REQ_STATUS)
                if reply and len(reply) >= 2 and reply[0] != b"error":
                    updates = self.parse_status(reply[1])
                    if updates:
                        if not self._robot_connected:
                            print("[klein] robot telemetry resumed.")
                        self._set_robot_state(
                            True, f"Connected to robot at {self.robot_endpoint}"
                        )
                        websockets.broadcast(
                            self.clients,
                            json.dumps({"type": "status", "data": updates}),
                        )
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
        """Serve static files over plain HTTP; let ``/ws`` upgrade to a
        WebSocket. Runs for every incoming connection before the handshake."""
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

        A missing ``index.html`` falls back to a stub page; a missing d3 bundle
        just means that route 404s (and the dashboard can't render).
        """
        for path, (filename, content_type) in _STATIC_ROUTES.items():
            body = _load_asset(filename)
            if body is not None:
                self._static[path] = (body, content_type)
        self._static.setdefault(
            "/index.html", (_INDEX_FALLBACK, "text/html; charset=utf-8")
        )
        if "/d3.v7.min.js" not in self._static:
            print("[klein] warning: d3.v7.min.js not bundled; dashboard will not render.",
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
            await self.status_poller()     # runs forever


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
        prog="klein",
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
