"""Unit tests for klein.gateway: status and blackboard decoding, static-asset
serving, the robot frame, tree swaps, recording, and the CLI. The tree it
unrolls is klein.layout's, tested in test_layout.py.

``TreeSwapTest`` and ``RecordingGatewayTest`` drive the real pollers, since the
re-handshake and the recording live inside them, against a ``FakeRobot`` link
rather than a socket.
"""
import asyncio
import contextlib
import io
import json
import re
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import msgpack

from klein import cli, gateway
from klein.cli import _port_available
from klein.gateway import KleinGateway
from klein.groot2_protocol import TRANSITION_BUFFER_MAX, parse_blackboard, parse_status
from klein.layout import parse_layout
from klein.recording import Recording
from klein.robot_link import RobotTimeout
from tests.helpers import (MODEL_LESS_XML, UUID_A, UUID_B, FakeRobot, answer, collect_ids,
                           collect_uids, new_gateway, status_record)


class GatewayTestCase(unittest.TestCase):
    def setUp(self):
        self.gw = new_gateway(self)


class ParseStatusTest(unittest.TestCase):
    def test_parse_status(self):
        cases = [
            ("structured", status_record(1, 1) + status_record(2, 12) + status_record(3, 0), {
                1: {"status": "RUNNING", "from": None},
                2: {"status": "IDLE", "from": "SUCCESS"},
                3: {"status": "IDLE", "from": None},
            }),
            # one valid record + a stray byte
            ("trailing partial", status_record(1, 1) + b"\x99", {1: {"status": "RUNNING", "from": None}}),
            ("unknown int", status_record(9, 7), {9: {"status": "UNKNOWN", "from": None}}),
            ("empty", b"", {}),
        ]
        for label, buf, expected in cases:
            with self.subTest(label):
                self.assertEqual(parse_status(buf), expected)


class StaticAssetsTest(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.gw._load_static()

    def request(self, path):
        return answer(self.gw, path)

    def test_every_static_file_served(self):
        # Every file in static/ is served at "/<name>" with its type, so a new
        # script needs no list; the package ships the whole directory.
        files = sorted(gateway.STATIC_DIR.iterdir())
        self.assertGreaterEqual({f.name for f in files},
                                {"index.html", "styles.css", "d3.v7.min.js", "app.js"})
        for f in files:
            with self.subTest(f.name):
                r = self.request(f"/{f.name}")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.body, f.read_bytes())
                self.assertEqual(r.headers["Content-Type"], gateway._CONTENT_TYPES[f.suffix])
        pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
        self.assertIn('klein = ["static/*"]', pyproject)

    def test_every_script_reference_resolves(self):
        # The page's <script src>s and every relative import between the
        # dashboard's modules name a file in static/.
        static = gateway.STATIC_DIR
        refs = [("index.html", src) for src in
                re.findall(r'<script[^>]*\ssrc="/([^"]+)"', (static / "index.html").read_text())]
        for js in sorted(static.glob("*.js")):
            refs += [(js.name, name) for name in
                     re.findall(r'^\s*(?:import|export)\b[^;]*?\bfrom\s+"\./([^"]+)"',
                                js.read_text(), re.M)]
        self.assertIn(("index.html", "app.js"), refs)
        for where, name in refs:
            with self.subTest(f"{where} -> {name}"):
                self.assertTrue((static / name).is_file())

    def test_routes(self):
        self.assertIsNone(self.request("/ws"))          # deferred to the upgrade
        cases = [
            ("root serves index", "/", 200, self.gw._static["/index.html"][0]),
            ("query string stripped", "/index.html?v=2", 200,
             self.gw._static["/index.html"][0]),
            ("unknown path", "/nope", 404, b"not found"),
        ]
        for label, path, status, body in cases:
            with self.subTest(label):
                r = self.request(path)
                self.assertEqual(r.status_code, status)
                self.assertEqual(r.body, body)
        self.assertEqual(self.request("/").headers["Content-Type"], "text/html; charset=utf-8")

    def test_missing_files_fall_back(self):
        # Simulate a package where none of the static assets are bundled.
        with mock.patch.object(gateway, "_load_asset", return_value=None):
            fresh = new_gateway(self)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                fresh._load_static()
        self.assertEqual(fresh._static["/index.html"][0], gateway._INDEX_FALLBACK)
        self.assertNotIn("/styles.css", fresh._static)
        self.assertNotIn("/app.js", fresh._static)
        self.assertNotIn("/d3.v7.min.js", fresh._static)
        # Both render-critical assets are reported missing.
        self.assertIn("d3.v7.min.js not bundled", err.getvalue())
        self.assertIn("app.js not bundled", err.getvalue())


class RobotStateTest(GatewayTestCase):
    def test_initial_state_is_disconnected(self):
        msg = json.loads(self.gw._robot_state_json)
        self.assertEqual(msg["type"], "robot")
        self.assertFalse(msg["connected"])
        self.assertFalse(self.gw._robot_connected)
        self.assertEqual(msg["recording"], "off")       # --record-buffer 0
        recording = new_gateway(self, recording=Recording())
        self.assertEqual(json.loads(recording._robot_state_json)["recording"], "on")

    def test_a_change_rebuilds_the_cached_message_and_a_repeat_does_not(self):
        self.gw._set_robot_state(True, "Connected to robot at tcp://x")
        self.assertTrue(self.gw._robot_connected)
        msg = json.loads(self.gw._robot_state_json)
        self.assertTrue(msg["connected"])
        self.assertEqual(msg["detail"], "Connected to robot at tcp://x")
        cached = self.gw._robot_state_json
        self.gw._set_robot_state(True, "Connected to robot at tcp://x")   # not rebroadcast
        self.assertIs(self.gw._robot_state_json, cached)


class BlackboardTest(unittest.TestCase):
    """Decoding the robot's msgpack blackboards."""

    def test_decoded_values(self):
        cases = [
            # The publisher replies msgpack nil when no requested name matched.
            ("nil payload", None, {}),
            ("private keys filtered",
             {"MainTree": {"speed": 1, "_debug_internal": "x", "_scratch": 4}},
             {"MainTree": {"speed": 1}}),
            ("value types survive", {"MainTree": {
                "flag": 1,                                   # BT.CPP stores bools as ints
                "name": "gripper",
                "speed": 0.25,
                "vec": [1.1, 2.2],
                "pos": {"__type": "Position2D", "x": 1.0},
                "unset": None,                               # declared but never written
            }}, {"MainTree": {"flag": 1, "name": "gripper", "speed": 0.25, "vec": [1.1, 2.2],
                              "pos": {"__type": "Position2D", "x": 1.0}, "unset": None}}),
            # A subtree whose only port is remapped to its parent holds nothing
            # locally and the publisher sends nil; the dashboard still lists it.
            ("nil board is empty, not dropped", {"MainTree": {"a": 1}, "DoorClosed::7": None},
             {"MainTree": {"a": 1}, "DoorClosed::7": {}}),
            ("unexpected board shape", {"Good": {"a": 1}, "Bad": "not a board"},
             {"Good": {"a": 1}, "Bad": {}}),
            ("integer keys stringified", {"MainTree": {1: "one"}}, {"MainTree": {"1": "one"}}),
        ]
        for label, payload, expected in cases:
            with self.subTest(label):
                board = parse_blackboard(msgpack.packb(payload))
                self.assertEqual(board, expected)
                json.dumps(board)      # must not raise

    def test_board_selection_and_order(self):
        cases = [
            # The robot walks an unordered map, so it may answer in any order;
            # the dashboard should still list the root tree first.
            ("requested order", {"DoorClosed::7": {"a": 1}, "MainTree": {"b": 2}},
             ["MainTree", "DoorClosed::7"], ["MainTree", "DoorClosed::7"]),
            # Some publishers attach the root board under "ROOT", duplicating the
            # board listed under the tree ID. A board klein never asked for
            # matches no node in the layout, so there is nowhere to put it.
            ("unrequested dropped", {"ROOT": {"b": 2}, "MainTree": {"b": 2}},
             ["MainTree", "Absent"], ["MainTree"]),
            ("nothing requested keeps all", {"Surprise": {"a": 1}, "MainTree": {"b": 2}},
             None, ["Surprise", "MainTree"]),
        ]
        for label, payload, order, expected in cases:
            with self.subTest(label):
                parsed = parse_blackboard(msgpack.packb(payload), order)
                self.assertEqual(list(parsed), expected)

    def test_output_is_always_browser_parseable_json(self):
        # NaN/Infinity would serialize to bare NaN/Infinity, which JSON.parse
        # rejects — killing the dashboard's whole message stream. Bytes and
        # non-string keys are not JSON-serializable at all.
        raw = msgpack.packb({"MainTree": {
            "nan": float("nan"),
            "inf": float("inf"),
            "blob": b"\xff\xfe",
            "nested": [float("-inf"), {"deep": float("nan")}],
        }}, use_bin_type=True)
        board = parse_blackboard(raw)
        encoded = json.dumps({"type": "blackboard", "data": board})
        for token in ("NaN", "Infinity"):
            self.assertNotIn(token, encoded.replace('"', ""))   # not as bare literals
        decoded = json.loads(encoded)["data"]["MainTree"]
        self.assertEqual(decoded["nan"], "nan")
        self.assertEqual(decoded["inf"], "inf")
        self.assertEqual(decoded["nested"][1]["deep"], "nan")
        self.assertIsInstance(decoded["blob"], str)


class HttpResponseTest(unittest.TestCase):
    def test_headers_and_body(self):
        r = KleinGateway._http_response(200, b"hello", "text/plain; charset=utf-8")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.body, b"hello")
        self.assertEqual(r.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(r.headers["Content-Length"], "5")
        self.assertEqual(r.headers["Cache-Control"], "no-store")


class PortAvailableTest(unittest.TestCase):
    def test_rejects_live_listener_then_frees(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 0))           # wildcard, matching the probe
        port = listener.getsockname()[1]
        listener.listen(1)
        try:
            self.assertFalse(_port_available(port))   # a live listener holds it
        finally:
            listener.close()
        self.assertTrue(_port_available(port))        # freed (never connected -> no TIME_WAIT)


class ConstructionTest(unittest.TestCase):
    def test_no_event_loop_required(self):
        # Every sync test in this suite builds a gateway, and unittest runs them
        # after IsolatedAsyncioTestCase has closed its loop. On Python 3.9 an
        # asyncio.Lock() built here would reach for get_event_loop() and raise,
        # so nothing in __init__ may touch the loop.
        asyncio.set_event_loop(None)
        try:
            new_gateway(self)               # and its link, which makes its lock on first use
        finally:
            asyncio.set_event_loop(asyncio.new_event_loop())


class TreeIdentityTest(GatewayTestCase):
    """Deciding, from a reply's tree UUID alone, whether the robot swapped trees."""

    def test_tree_changed(self):
        # Before the first handshake there is nothing to differ from.
        self.assertIsNone(self.gw._tree_uuid)
        cases = [
            ("nothing recorded yet", None, UUID_A, False),
            # Absence of information must never be reported as a change, or a
            # header too short to carry a UUID would send the gateway into a
            # handshake loop. (An error reply never gets here: RobotRefused.)
            ("no uuid in the reply", UUID_A, None, False),
            ("same tree", UUID_A, UUID_A, False),
            ("swapped", UUID_A, UUID_B, True),
        ]
        for label, recorded, uuid, changed in cases:
            with self.subTest(label):
                self.gw._tree_uuid = recorded
                self.assertEqual(self.gw._tree_changed(uuid), changed)

    def test_the_detector_never_records(self):
        # fetch_layout is the only writer, and it takes the UUID from the same
        # reply that carried the XML. If detection adopted the new UUID here and
        # the re-handshake then failed, the next status frame would compare
        # equal and paint the new tree's UIDs onto the old tree's cards.
        self.gw._tree_uuid = UUID_A
        self.assertTrue(self.gw._tree_changed(UUID_B))
        self.assertEqual(self.gw._tree_uuid, UUID_A)


class NoticeTest(GatewayTestCase):
    """The transient frame that tells a dashboard why its canvas just changed."""

    def test_broadcast_once_to_every_client_and_never_cached(self):
        # Unlike layout/blackboard/robot frames, which ws_handler sends to
        # every client that connects. A notice reports something that already
        # happened, so replaying it would describe an event the client missed.
        client = object()
        self.gw.clients = {client}
        before = (self.gw._blackboard_json, self.gw._robot_state_json)
        with mock.patch.object(gateway.websockets, "broadcast") as bcast:
            self.gw._broadcast_notice("hello")
        bcast.assert_called_once()
        clients, payload = bcast.call_args[0]
        self.assertEqual(clients, {client})
        self.assertEqual(json.loads(payload), {"type": "notice", "text": "hello"})
        self.assertEqual((self.gw._blackboard_json, self.gw._robot_state_json), before)


class TreeSwapTest(unittest.IsolatedAsyncioTestCase):
    """A swapped tree, end to end through status_poller, against a
    ``FakeRobot`` link that handed over CROSSDOOR under ``UUID_A``."""

    async def asyncSetUp(self):
        await self.fresh_gateway()

    async def fresh_gateway(self):
        self.robot = FakeRobot(limit=1)
        self.gw = new_gateway(self, link=self.robot)
        # Handshaken before any client exists, so the poller's own
        # _mark_connected stays a no-op and the broadcast log holds only the
        # frames these tests are about.
        with contextlib.redirect_stdout(io.StringIO()):
            await self.gw.fetch_layout()
        self.gw.clients = {object()}        # else both pollers idle
        self.robot.sent = []                # requests the poller issued
        self.frames = []                    # frames broadcast to dashboards

    async def run_poller(self, limit, poller=None):
        """Run one poller until the robot has answered ``limit`` requests and
        parked on the next, then stop it."""
        self.robot.limit = limit
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.object(gateway, "POLL_INTERVAL", 0), \
             mock.patch.object(gateway, "BLACKBOARD_POLL_INTERVAL", 0), \
             mock.patch.object(gateway.websockets, "broadcast",
                               side_effect=lambda _c, m: self.frames.append(json.loads(m))):
            task = asyncio.create_task((poller or self.gw.status_poller)())
            try:
                await asyncio.wait_for(self.robot.done.wait(), 2)
                await asyncio.sleep(0)      # let the poller finish the iteration
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def seq(self):
        return "".join(self.robot.sent)

    def types(self):
        return [f["type"] for f in self.frames]

    def lose_robot(self):
        """Mark the robot unreachable, as a timed-out poll leaves it, before
        the broadcast log starts."""
        clients, self.gw.clients = self.gw.clients, set()
        self.gw._set_robot_state(False, "Lost connection to robot")
        self.gw.clients = clients

    async def test_matching_uuid_just_streams_status(self):
        await self.run_poller(limit=1)
        self.assertEqual(self.seq(), "S")               # and then the next S
        self.assertEqual(self.types(), ["status"])

    async def test_a_new_uuid_re_handshakes_and_sends_only_a_layout_then_a_notice(self):
        old_ids = set(collect_ids(self.gw.tree.tree))
        self.gw._blackboard_json = '{"type": "blackboard", "data": {"stale": {}}}'
        self.robot.uuid, self.robot.xml = UUID_B, MODEL_LESS_XML
        await self.run_poller(limit=3)
        self.assertEqual(self.seq(), "STS")
        layout = self.frames[0]["data"]
        self.assertEqual(layout["root_tree_id"], "MainTree")
        self.assertEqual(sorted(collect_uids(layout)), [1, 2, 3, 4, 5])
        # The next generation's ids: none can match the old tree's cards.
        self.assertEqual(set(collect_ids(layout)) & old_ids, set())
        self.assertIsNone(self.gw._blackboard_json)    # the old tree's values are stale
        # No "status" for the buffer that carried the new UUID: it indexes a
        # tree the dashboard has not been sent, so broadcasting it would land
        # those UIDs on the old tree's cards. The note comes after the layout,
        # so the canvas has already redrawn by the time it explains why. Only
        # the next poll's status, under the UUID the handshake adopted, streams.
        self.assertEqual(self.types(), ["layout", "notice", "status"])
        self.assertIn("new behaviour tree", self.frames[1]["text"])

    async def test_the_same_tree_again_is_absorbed_quietly(self):
        # A robot restart yields a fresh publisher UUID even when the tree is
        # unchanged, and a resume after an outage re-handshakes too. Re-rendering
        # the identical canvas would be a flash that reports nothing.
        cases = [("restart under a new uuid", True, UUID_B),
                 ("reconnect after an outage", False, UUID_A)]
        for label, connected, uuid in cases:
            with self.subTest(label):
                await self.fresh_gateway()
                if not connected:
                    self.lose_robot()
                tree = self.gw.tree
                self.robot.uuid = uuid
                await self.run_poller(limit=3)
                self.assertEqual(self.seq(), "STS")
                self.assertIs(self.gw.tree, tree)                   # not re-parsed
                self.assertNotIn("layout", self.types())
                self.assertNotIn("notice", self.types())
                self.assertEqual(self.types()[-1], "status")       # the UUID adopted

    async def test_telemetry_resuming_after_an_outage_re_handshakes(self):
        # Killing a robot and starting another on the same port looks like this
        # from here. Detection must not rest on the new process having drawn a
        # fresh UUID — here it deliberately reuses its predecessor's, and the
        # swap still has to be caught.
        self.lose_robot()
        self.robot.xml = MODEL_LESS_XML
        await self.run_poller(limit=2)
        self.assertEqual(self.seq(), "ST")
        self.assertEqual(self.types(), ["robot", "layout", "notice"])
        layout = self.frames[1]["data"]
        self.assertEqual(layout["root_tree_id"], "MainTree")
        self.assertEqual(sorted(collect_uids(layout)), [1, 2, 3, 4, 5])

    async def test_an_error_reply_does_not_re_handshake(self):
        self.robot.refusals = {1}
        await self.run_poller(limit=1)
        self.assertEqual(self.seq(), "S")               # and then the next S, not a T
        self.assertEqual(self.frames, [])

    async def test_blackboard_values_from_a_retired_tree_are_dropped(self):
        # The blackboard poller does not detect swaps, so one can land while its
        # request is in flight. See the guard in blackboard_poller for why the
        # reply must then be dropped rather than cached.
        swap = parse_layout(MODEL_LESS_XML, 2)
        self.robot.on_request[1] = lambda _r: setattr(self.gw, "tree", swap)
        await self.run_poller(limit=1, poller=self.gw.blackboard_poller)
        self.assertIsNone(self.gw._blackboard_json)
        self.assertEqual(self.types(), [])


class RecordingGatewayTest(unittest.IsolatedAsyncioTestCase):
    """Arming, `t` drains, swaps, outages and overflow, through the real
    pollers against a ``FakeRobot`` link. No dashboard is connected in any of
    these: a recording gateway polls regardless."""

    def make_gateway(self, robot=None, record=True, **caps):
        patch = mock.patch.object(gateway, "POLL_INTERVAL", 0)
        patch.start()
        self.addCleanup(patch.stop)
        return new_gateway(self, link=robot, recording=Recording(**caps) if record else None)

    async def run_gateway(self, gw, blackboard=False):
        """Handshake, then the status poller (and optionally the blackboard
        poller), until the robot has answered ``robot.limit`` requests."""
        robot = gw.link
        frames = []

        async def main():
            await gw.fetch_layout()
            pollers = [gw.status_poller()] + ([gw.blackboard_poller()] if blackboard else [])
            await asyncio.gather(*pollers)

        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.object(gateway, "BLACKBOARD_POLL_INTERVAL", 0), \
             mock.patch.object(gateway.websockets, "broadcast",
                               side_effect=lambda _c, m: frames.append(json.loads(m))):
            task = asyncio.create_task(main())
            try:
                await asyncio.wait_for(robot.done.wait(), 2)
                await asyncio.sleep(0)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return frames

    def seq(self, robot):
        return "".join(robot.sent)

    async def test_startup_arms_then_pairs_every_status_poll_with_a_drain(self):
        robot = FakeRobot(limit=9)
        gw = self.make_gateway(robot)
        robot.drains = [[(10, 1, 1), (15, 2, 1)], [(40, 2, 2), (41, 2, 0)]]
        await self.run_gateway(gw)
        self.assertEqual(gw.clients, set())             # no dashboard, still polling
        self.assertEqual(self.seq(robot), "TrSStStSt")
        rec = gw.recording
        self.assertEqual(len(rec.segments), 1)
        segment = rec.segments[0]
        self.assertEqual(segment.t_begin, robot.starts[0])
        self.assertIs(segment.layout, gw.tree)
        self.assertEqual(len(segment.baseline), 14)     # max uid 13, plus index 0
        start = robot.starts[0]
        self.assertEqual(list(s[1:] for s in segment.iter_records(0, segment.head_seq)),
                         [(start + 10, 1, 1), (start + 15, 2, 1),
                          (start + 40, 2, 2), (start + 41, 2, 0)])
        self.assertEqual(segment.state[2], 12)          # IDLE after SUCCESS
        self.assertIsNotNone(rec.head)
        self.assertEqual(rec.gaps, [])

    async def test_blackboard_polls_interleave_and_are_recorded(self):
        robot = FakeRobot(limit=30)
        gw = self.make_gateway(robot)
        gw.clients = {object()}
        frames = await self.run_gateway(gw, blackboard=True)
        seq = self.seq(robot)
        self.assertIn("B", seq)
        self.assertRegex(seq.replace("B", ""), r"^TrS(St)+S?$")
        track = gw.recording.segments[0].blackboard
        self.assertIsNotNone(track.t_start)
        self.assertEqual(set(track.at(gw.recording.head + 10**6)), {"MainTree"})
        # Each frame is stamped with klein's clock when its reply arrived.
        ts = [f["t"] for f in frames if f["type"] == "blackboard"]
        self.assertGreater(len(ts), 1)
        self.assertEqual(ts, sorted(ts))
        self.assertLess(ts[0], ts[-1])
        self.assertLessEqual(ts[-1], time.monotonic() * 1e6)

    async def test_a_blackboard_sample_is_evicted_at_once(self):
        """Over the blackboard cap, a sample is evicted when it is stored, not
        at the next drain: whatever runs between (a dashboard connecting and
        getting its backfill's sizes) never sees more than the cap."""
        cap = 6                             # bytes: the base "n" and a change or two
        robot = FakeRobot(limit=40)
        gw = self.make_gateway(robot, bb_max_bytes=cap)
        seen = []
        robot.on_request = {n: lambda _r: seen.append(gw.recording.bytes_used()[1])
                            for n in range(1, 41)}
        await self.run_gateway(gw, blackboard=True)
        self.assertTrue(gw.recording.blackboard_capped)
        self.assertLessEqual(max(seen), cap, seen)

    async def test_a_tree_swap_starts_a_segment_with_a_new_layout(self):
        robot = FakeRobot(limit=11)
        gw = self.make_gateway(robot)

        def swap(r):
            r.uuid, r.xml = UUID_B, MODEL_LESS_XML
            r.status = b"".join(status_record(uid, 0) for uid in range(1, 6))
        robot.on_request[6] = swap
        robot.drains = [[(5, 1, 1)], [(7, 3, 1)]]
        gw.clients = {object()}
        frames = await self.run_gateway(gw)
        self.assertEqual(self.seq(robot), "TrSSt" "S" "TrSSt")
        rec = gw.recording
        first, second = rec.segments
        self.assertIsNot(first.layout, second.layout)
        self.assertEqual(first.t_end, second.t_begin)
        self.assertEqual(second.t_begin, robot.starts[1])
        self.assertEqual(len(second.baseline), 6)
        self.assertEqual([[s.id for s in run] for run in rec.runs()], [[0], [1]])
        self.assertEqual(rec.gaps, [])
        self.assertEqual(second.head_seq, 1)
        # Recording adds no frame types for the browser.
        self.assertEqual({f["type"] for f in frames}, {"robot", "status", "layout", "notice"})

    async def test_an_outage_ends_the_segment_and_the_resume_reuses_the_layout(self):
        robot = FakeRobot(limit=12)
        gw = self.make_gateway(robot)
        robot.timeouts = {6}                            # the 2nd poll's S
        heads = []                                      # the head when the robot went
        robot.on_request[6] = lambda r: heads.append(gw.recording.head)
        await self.run_gateway(gw)
        self.assertEqual(self.seq(robot), "TrSSt" "S" "S" "TrSSt")
        rec = gw.recording
        first, second = rec.segments
        self.assertIs(first.layout, second.layout)      # same XML: one tree run
        self.assertEqual([[s.id for s in run] for run in rec.runs()], [[0, 1]])
        # Ended at the last drain: the last time klein heard from the robot.
        self.assertEqual(first.t_end, heads[0])
        self.assertIsNotNone(first.t_end)
        self.assertLess(first.t_end, second.t_begin)
        self.assertEqual(rec.gaps, [(first.t_end, second.t_begin, "outage")])
        self.assertEqual(gw.tree.generation, 1)         # not re-parsed

    async def test_a_quick_restart_on_the_same_tree_stays_in_one_run(self):
        # Back before any poll timed out: only the UUID says it restarted.
        robot = FakeRobot(limit=11)
        gw = self.make_gateway(robot)
        robot.on_request[6] = lambda r: setattr(r, "uuid", UUID_B)
        await self.run_gateway(gw)
        self.assertEqual(self.seq(robot), "TrSSt" "S" "TrSSt")
        first, second = gw.recording.segments
        self.assertIs(first.layout, second.layout)
        self.assertEqual(first.t_end, second.t_begin)
        self.assertEqual([[s.id for s in run] for run in gw.recording.runs()], [[0, 1]])
        self.assertEqual(gw.recording.gaps, [])

    async def test_an_outage_seen_only_by_a_blackboard_timeout_still_gets_a_gap(self):
        # The robot dies while a B is in flight and is back, as a new publisher,
        # before any S times out: B's timeout marks where the segment ends.
        robot = FakeRobot(limit=40)
        gw = self.make_gateway(robot)
        heads = []                          # the head when the re-handshake began

        def die(r):
            r.uuid = UUID_B                 # restarted by the time S gets through
            r.on_letter["T"] = lambda _r: heads.append(gw.recording.head)
            raise RobotTimeout("stub timeout")
        robot.on_letter["B"] = die
        await self.run_gateway(gw, blackboard=True)
        first, second = gw.recording.segments[:2]
        self.assertEqual(first.t_end, heads[0])
        self.assertIs(first.layout, second.layout)
        self.assertLess(first.t_end, second.t_begin)
        self.assertEqual(gw.recording.gaps[0], (first.t_end, second.t_begin, "outage"))

    async def test_a_blackboard_timeout_alone_changes_nothing(self):
        robot = FakeRobot(limit=40)
        gw = self.make_gateway(robot)

        def blip(r):
            raise RobotTimeout("stub timeout")
        robot.on_letter["B"] = blip
        await self.run_gateway(gw, blackboard=True)
        self.assertEqual(len(gw.recording.segments), 1)
        self.assertEqual(gw.recording.gaps, [])
        self.assertFalse(gw._timed_out)

    async def test_a_full_drain_records_an_overflow_gap_and_re_arms(self):
        robot = FakeRobot(limit=9)
        gw = self.make_gateway(robot)
        burst = [(1000 + i, 1 + i % 13, 1 + i % 3) for i in range(TRANSITION_BUFFER_MAX)]
        robot.drains = [[(5, 1, 1)], burst, [(3, 4, 1)]]
        await self.run_gateway(gw)
        self.assertEqual(self.seq(robot), "TrSSt" "St" "rS")
        rec = gw.recording
        first, second = rec.segments
        start = robot.starts[0]
        self.assertEqual(first.head_seq, 1 + TRANSITION_BUFFER_MAX)
        self.assertEqual(rec.gaps, [(start + 5, start + 1000, "overflow")])
        self.assertIs(first.layout, second.layout)
        self.assertEqual(first.t_end, second.t_begin)
        self.assertEqual(second.t_begin, robot.starts[1])
        self.assertEqual(second.head_seq, 0)            # the re-arm's own baseline

    async def test_overflow_re_arm_then_keeps_draining(self):
        robot = FakeRobot(limit=12)
        gw = self.make_gateway(robot)
        burst = [(1000 + i, 1, 1 + i % 2) for i in range(TRANSITION_BUFFER_MAX)]
        robot.drains = [burst, [(3, 4, 1)]]
        await self.run_gateway(gw)
        self.assertEqual(self.seq(robot), "TrSSt" "rS" "St" "St" "S")
        second = gw.recording.segments[1]
        self.assertEqual(list(s[1:] for s in second.iter_records(0, 1)),
                         [(robot.starts[1] + 3, 4, 1)])

    async def test_a_robot_without_recording_gets_no_t_and_the_same_status(self):
        robot = FakeRobot(limit=7)
        robot.supports_recording = False
        gw = self.make_gateway(robot)
        gw.clients = {object()}
        frames = await self.run_gateway(gw)
        self.assertEqual(self.seq(robot), "Tr" "SSSSS")
        self.assertEqual(gw.recording.segments, [])
        self.assertFalse(gw._armed)
        # Status broadcasts are exactly those of a non-recording gateway.
        plain = FakeRobot(limit=6)
        other = self.make_gateway(plain, record=False)
        other.clients = {object()}
        plain_frames = await self.run_gateway(other)
        self.assertEqual(self.seq(plain), "TSSSSS")
        self.assertEqual([f for f in frames if f["type"] == "status"],
                         [f for f in plain_frames if f["type"] == "status"])
        # The drawer is told why there is no recording.
        self.assertEqual([f["recording"] for f in frames if f["type"] == "robot"],
                         ["on", "unsupported"])
        self.assertEqual(json.loads(gw._robot_state_json)["recording"], "unsupported")
        self.assertEqual({f["recording"] for f in plain_frames if f["type"] == "robot"}, {"off"})
        # The robot frame says recording is on again after a capable handshake.
        robot.supports_recording, robot.sent, robot.limit = True, [], 4
        robot.done.clear()
        frames = await self.run_gateway(gw)
        self.assertEqual([f["recording"] for f in frames if f["type"] == "robot"], ["on"])

    async def poll_without_clients(self, gw):
        """Handshake, then give both pollers 50 loop turns with no dashboard."""
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            await gw.fetch_layout()
        with mock.patch.object(gateway, "BLACKBOARD_POLL_INTERVAL", 0):
            tasks = [asyncio.create_task(gw.status_poller()),
                     asyncio.create_task(gw.blackboard_poller())]
            for _ in range(50):
                await asyncio.sleep(0)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_idle_without_clients(self):
        cases = [("recording off", False, True, "T"),
                 ("robot cannot record", True, False, "Tr")]
        for label, record, supports, expected in cases:
            with self.subTest(label):
                robot = FakeRobot(limit=20)
                robot.supports_recording = supports
                gw = self.make_gateway(robot, record=record)
                await self.poll_without_clients(gw)
                self.assertEqual(self.seq(robot), expected)     # then nothing polled
                if supports:
                    continue
                self.assertTrue(gw._cannot_record)
                # The next handshake tries again, and a robot that can record is polled.
                robot.supports_recording, robot.sent = True, []
                await self.poll_without_clients(gw)
                self.assertFalse(gw._cannot_record)
                self.assertTrue(self.seq(robot).startswith("TrSSt"), self.seq(robot))

    async def test_debug_state_reports_the_recording(self):
        robot = FakeRobot(limit=7)
        robot.drains = [[(10, 1, 1)], [(20, 1, 2)]]
        gw = self.make_gateway(robot)
        gw.debug = True
        await self.run_gateway(gw)
        response = answer(gw, "/debug/state")
        self.assertEqual(response.status_code, 200)
        state = json.loads(response.body)
        start = robot.starts[0]
        self.assertTrue(state["recording"])
        self.assertEqual(state["records"], [[start + 10, 1, 1], [start + 20, 1, 2]])
        self.assertEqual(state["segments"], [{"id": 0, "t_begin": start, "t_end": None,
                                              "start_seq": 0, "head_seq": 2,
                                              "start_state": [0] * 14,
                                              "layout_id": 1}])
        self.assertEqual(state["runs"], [[0]])
        self.assertEqual(state["gaps"], [])
        self.assertEqual(state["state"]["1"], {"status": "SUCCESS", "from": None})
        self.assertEqual(state["state"]["2"], {"status": "IDLE", "from": None})
        self.assertEqual(state["t_min"], start)
        self.assertEqual(len(state["bytes"]), 2)
        self.assertEqual(state["blackboard"], [{"seg": 0, "t_start": None, "boards": {},
                                               "changes": []}])
        gw.recording.add_blackboard(start + 30, {"MainTree": {"x": 1}})
        response = answer(gw, "/debug/state")
        self.assertEqual(json.loads(response.body)["blackboard"],
                         [{"seg": 0, "t_start": start + 30, "boards": {"MainTree": start + 30},
                           "changes": [[start + 30, "MainTree", "x", "1"]]}])
        quiet = self.make_gateway()                     # without --debug
        response = answer(quiet, "/debug/state")
        self.assertEqual(response.status_code, 404)


class CliTest(unittest.TestCase):
    def test_record_buffer_durations(self):
        self.assertEqual(cli._duration_us("90s"), 90_000_000)
        self.assertEqual(cli._duration_us("30m"), 1_800_000_000)
        self.assertEqual(cli._duration_us("1h"), 3_600_000_000)
        self.assertEqual(cli._duration_us("30"), 30_000_000)
        self.assertEqual(cli._duration_us("0"), 0)          # recording off
        for bad in ("abc", "-5m", "", "inf", "nan", "infm"):
            with self.subTest(bad), self.assertRaises(Exception):
                cli._duration_us(bad)

    def gateway_for(self, *argv):
        """The ``recording`` and ``debug`` main_cli would hand the gateway."""
        with mock.patch.object(sys, "argv", ["klein-bt", "--no-browser", *argv]), \
             mock.patch.object(cli, "_port_available", return_value=True), \
             mock.patch.object(cli, "KleinGateway") as cls, \
             mock.patch.object(cli.asyncio, "run", side_effect=lambda coro: coro), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.main_cli()
        return cls.call_args.kwargs

    def test_flags(self):
        cases = [
            ("defaults", (), 600_000_000, False),
            ("--record-buffer 90s", ("--record-buffer", "90s"), 90_000_000, False),
            ("--record-buffer 0", ("--record-buffer", "0"), None, False),
            ("--debug", ("--debug",), 600_000_000, True),
        ]
        for label, argv, keep_us, debug in cases:
            with self.subTest(label):
                kwargs = self.gateway_for(*argv)
                recording = kwargs["recording"]
                self.assertEqual(None if recording is None else recording.keep_us, keep_us)
                self.assertEqual(kwargs["debug"], debug)


if __name__ == "__main__":
    unittest.main()
