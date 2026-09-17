"""Unit tests for klein.gateway — the pure, socket-free logic: UID extraction,
status parsing, subtree unrolling, layout caching, blackboard decoding, and
static-asset serving."""
import contextlib
import io
import json
import socket
import struct
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import msgpack

from klein import gateway, mock_robot
from klein.gateway import KleinGateway, _port_available
from klein.groot2_protocol import (
    HEADER_FORMAT,
    PROTOCOL_ID,
    REQ_FULLTREE,
    REQ_STATUS,
    STATUS_RECORD_FORMAT,
)


def _rec(uid, status_int):
    return struct.pack(STATUS_RECORD_FORMAT, uid, status_int)


UUID_A = bytes([0xA1] * 16)
UUID_B = bytes([0xB2] * 16)


def _reply(uuid, payload=b"", req_type=REQ_STATUS, unique_id=7):
    """A two-frame robot reply whose header carries ``uuid``."""
    header = struct.pack(HEADER_FORMAT, PROTOCOL_ID, req_type, unique_id) + uuid
    return [header, payload]


def _collect_uids(node, acc=None):
    acc = [] if acc is None else acc
    if node.get("uid") is not None:
        acc.append(node["uid"])
    for child in node.get("children", []):
        _collect_uids(child, acc)
    return acc


def _find(node, uid):
    if node.get("uid") == uid:
        return node
    for child in node.get("children", []):
        hit = _find(child, uid)
        if hit is not None:
            return hit
    return None


class ExtractUidTest(unittest.TestCase):
    def uid(self, xml):
        return KleinGateway.extract_uid(ET.fromstring(xml))

    def test_reads_underscore_uid(self):
        self.assertEqual(self.uid('<Action _uid="5"/>'), 5)

    def test_falls_back_to_uid(self):
        self.assertEqual(self.uid('<Action uid="7"/>'), 7)

    def test_id_attribute_never_shadows_uid(self):
        # ID is a subtree *name*, not a numeric UID.
        self.assertIsNone(self.uid('<SubTree ID="PickSub"/>'))
        self.assertEqual(self.uid('<SubTree ID="PickSub" _uid="4"/>'), 4)

    def test_negative_uid(self):
        self.assertEqual(self.uid('<Action _uid="-3"/>'), -3)

    def test_non_numeric_and_missing(self):
        self.assertIsNone(self.uid('<Action _uid="abc"/>'))
        self.assertIsNone(self.uid('<Action/>'))


class ParseStatusTest(unittest.TestCase):
    def test_structured_output(self):
        buf = _rec(1, 1) + _rec(2, 12) + _rec(3, 0)
        self.assertEqual(
            KleinGateway.parse_status(buf),
            {
                1: {"status": "RUNNING", "from": None},
                2: {"status": "IDLE", "from": "SUCCESS"},
                3: {"status": "IDLE", "from": None},
            },
        )

    def test_trailing_partial_record_ignored(self):
        buf = _rec(1, 1) + b"\x99"          # one valid record + a stray byte
        self.assertEqual(KleinGateway.parse_status(buf), {1: {"status": "RUNNING", "from": None}})

    def test_unknown_status_int(self):
        self.assertEqual(KleinGateway.parse_status(_rec(9, 7)), {9: {"status": "UNKNOWN", "from": None}})

    def test_empty_buffer(self):
        self.assertEqual(KleinGateway.parse_status(b""), {})


class ReplyUuidTest(unittest.TestCase):
    def test_extracts_the_uuid_from_a_22_byte_header(self):
        self.assertEqual(KleinGateway.reply_uuid(_reply(UUID_A)), UUID_A)

    def test_ignores_bytes_beyond_the_header(self):
        header = struct.pack(HEADER_FORMAT, PROTOCOL_ID, REQ_STATUS, 1) + UUID_A + b"trailing"
        self.assertEqual(KleinGateway.reply_uuid([header, b""]), UUID_A)

    def test_short_or_missing_header_is_unknown(self):
        self.assertIsNone(KleinGateway.reply_uuid([b"error", b"unsupported request"]))
        self.assertIsNone(KleinGateway.reply_uuid([_reply(UUID_A)[0][:21], b""]))
        self.assertIsNone(KleinGateway.reply_uuid([]))
        self.assertIsNone(KleinGateway.reply_uuid(None))

    def test_zero_uuid_is_unknown(self):
        # An all-zero UUID carries no identity, so it can never signal a change.
        self.assertIsNone(KleinGateway.reply_uuid(_reply(bytes(16))))


class TreeChangeTest(unittest.TestCase):
    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    def test_layout_uuid_starts_unknown(self):
        self.assertIsNone(self.gw._layout_uuid)

    def test_parse_layout_leaves_the_binding_to_the_handshake(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        self.assertIsNone(self.gw._layout_uuid)

    def test_same_uuid_is_no_change(self):
        self.gw._layout_uuid = UUID_A
        self.assertFalse(self.gw._tree_changed(UUID_A))

    def test_other_uuid_is_a_change(self):
        self.gw._layout_uuid = UUID_A
        self.assertTrue(self.gw._tree_changed(UUID_B))

    def test_unknown_on_either_side_is_no_change(self):
        self.assertFalse(self.gw._tree_changed(UUID_B))          # layout UUID unknown
        self.gw._layout_uuid = UUID_A
        self.assertFalse(self.gw._tree_changed(None))            # reply UUID unknown
        self.assertFalse(self.gw._tree_changed(KleinGateway.reply_uuid(_reply(bytes(16)))))


class ReloadLayoutTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    async def asyncTearDown(self):
        self.gw.ctx.destroy(linger=0)

    async def test_reload_drops_stale_boards_and_handshakes_again(self):
        self.gw._blackboard_json = '{"type":"blackboard","data":{}}'
        self.gw.fetch_layout = mock.AsyncMock()
        with contextlib.redirect_stdout(io.StringIO()):
            await self.gw._reload_layout()
        self.gw.fetch_layout.assert_awaited_once()
        self.assertIsNone(self.gw._blackboard_json)
        self.assertTrue(self.gw._robot_connected)            # the robot did answer
        self.assertIn("Tree changed", self.gw._robot_detail)


class FetchLayoutBindsUuidTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    async def asyncTearDown(self):
        self.gw.ctx.destroy(linger=0)

    async def fetch(self, uuid, xml):
        self.gw._request = mock.AsyncMock(
            return_value=_reply(uuid, xml.encode("utf-8"), req_type=REQ_FULLTREE))
        with contextlib.redirect_stdout(io.StringIO()):
            await self.gw.fetch_layout()

    async def test_binds_the_layout_to_the_fulltree_reply_uuid(self):
        await self.fetch(UUID_A, mock_robot.TREE_XML)
        self.assertEqual(self.gw._layout_uuid, UUID_A)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "MainTree")
        self.assertTrue(self.gw._robot_connected)

    async def test_zero_uuid_leaves_the_layout_unbound(self):
        await self.fetch(bytes(16), mock_robot.TREE_XML)
        self.assertIsNone(self.gw._layout_uuid)
        self.assertIsNotNone(self.gw._layout_json)            # the tree itself still loads

    async def test_second_handshake_rebinds_and_swaps_the_tree(self):
        await self.fetch(UUID_A, mock_robot.TREE_XML)
        self.assertTrue(self.gw._tree_changed(UUID_B))
        await self.fetch(UUID_B, mock_robot.PATROL_TREE_XML)
        self.assertEqual(self.gw._layout_uuid, UUID_B)
        self.assertFalse(self.gw._tree_changed(UUID_B))
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "PatrolTree")
        self.assertEqual(self.gw._blackboard_names, mock_robot.PATROL_BLACKBOARD_NAMES)

    async def test_identical_tree_is_not_rebroadcast(self):
        # A robot process restarted with the same tree carries a new UUID; the
        # dashboards keep their camera and boards since nothing they show changed.
        sent = []
        self.gw.clients = {object()}
        with mock.patch.object(gateway.websockets, "broadcast",
                               side_effect=lambda clients, msg: sent.append(msg)):
            await self.fetch(UUID_A, mock_robot.TREE_XML)
            await self.fetch(UUID_B, mock_robot.TREE_XML)
            await self.fetch(UUID_A, mock_robot.PATROL_TREE_XML)
        layouts = [m for m in sent if json.loads(m)["type"] == "layout"]
        self.assertEqual(len(layouts), 2)
        self.assertEqual(json.loads(layouts[0])["data"]["root_tree_id"], "MainTree")
        self.assertEqual(json.loads(layouts[1])["data"]["root_tree_id"], "PatrolTree")


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    def test_unrolls_mock_tree_with_all_uids(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "MainTree")
        uids = _collect_uids(self.gw.tree_structure)
        self.assertEqual(sorted(uids), sorted(mock_robot.ALL_UIDS))
        self.assertEqual(len(uids), self.gw._node_seq)   # every node got a stable id + uid

    def test_subtree_stitched_in_place(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        subtree_ref = _find(self.gw.tree_structure, 7)   # <SubTree ID="DoorClosed" _uid="7"/>
        self.assertTrue(subtree_ref["is_subtree_root"])
        self.assertEqual(subtree_ref["subtree_id"], "DoorClosed")
        # its single child is the DoorClosed definition root (Fallback "tryOpen" _uid="8")
        self.assertEqual(subtree_ref["children"][0]["uid"], 8)

    def test_stable_ids_are_unique(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        ids = []

        def walk(n):
            ids.append(n["id"])
            for c in n["children"]:
                walk(c)

        walk(self.gw.tree_structure)
        self.assertEqual(len(ids), len(set(ids)))

    def test_layout_json_is_cached_and_valid(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        msg = json.loads(self.gw._layout_json)
        self.assertEqual(msg["type"], "layout")
        self.assertEqual(msg["data"]["root_tree_id"], "MainTree")

    def test_main_tree_to_execute_is_honored(self):
        xml = ('<root BTCPP_format="4" main_tree_to_execute="Second">'
               '<BehaviorTree ID="First"><Action _uid="1"/></BehaviorTree>'
               '<BehaviorTree ID="Second"><Action _uid="2"/></BehaviorTree></root>')
        self.gw._parse_layout(xml)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "Second")

    def test_first_block_is_root_without_entrypoint(self):
        xml = ('<root BTCPP_format="4">'
               '<BehaviorTree ID="First"><Action _uid="1"/></BehaviorTree>'
               '<BehaviorTree ID="Second"><Action _uid="2"/></BehaviorTree></root>')
        self.gw._parse_layout(xml)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "First")

    def test_missing_entrypoint_falls_back_to_first(self):
        xml = ('<root BTCPP_format="4" main_tree_to_execute="Nope">'
               '<BehaviorTree ID="First"><Action _uid="1"/></BehaviorTree></root>')
        self.gw._parse_layout(xml)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "First")

    def test_no_behavior_tree_raises(self):
        with self.assertRaises(ValueError):
            self.gw._parse_layout('<root BTCPP_format="4"></root>')

    def test_empty_behavior_tree_raises(self):
        with self.assertRaises(ValueError):
            self.gw._parse_layout('<root BTCPP_format="4"><BehaviorTree ID="X"></BehaviorTree></root>')

    def test_cyclic_subtree_terminates(self):
        xml = ('<root BTCPP_format="4" main_tree_to_execute="A">'
               '<BehaviorTree ID="A"><SubTree ID="A" _uid="1"/></BehaviorTree></root>')
        self.gw._parse_layout(xml)                       # must not recurse forever
        top = self.gw.tree_structure
        self.assertTrue(top["is_subtree_root"])
        # expanded exactly once; the cycle guard stops the inner copy from expanding
        self.assertEqual(top["children"][0]["children"], [])


class PortsTest(unittest.TestCase):
    """Ports ride along with the layout so the dashboard can draw them."""

    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    def test_ports_are_carried_through(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        script = _find(self.gw.tree_structure, 2)        # <Script code="door_open:=false"/>
        self.assertEqual(script["ports"], {"code": "door_open:=false"})
        retry = _find(self.gw.tree_structure, 10)        # <RetryUntilSuccessful num_attempts="5"/>
        self.assertEqual(retry["ports"], {"num_attempts": "5"})

    def test_structural_attributes_are_not_ports(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        sequence = _find(self.gw.tree_structure, 1)      # name + _uid only
        self.assertEqual(sequence["ports"], {})
        open_door = _find(self.gw.tree_structure, 9)     # inside a subtree, still bare
        self.assertEqual(open_door["ports"], {})

    def test_subtree_keeps_its_remapping_and_drops_its_structure(self):
        # A <SubTree>'s remapping is the author's writing, so it shows; ID and
        # _fullpath are how klein stitches and names the instance, so they don't.
        self.gw._parse_layout(mock_robot.TREE_XML)
        subtree_ref = _find(self.gw.tree_structure, 7)
        self.assertEqual(subtree_ref["ports"], {"door_open": "{door_open}"})
        self.assertEqual(subtree_ref["subtree_id"], "DoorClosed")
        self.assertEqual(subtree_ref["board"], "DoorClosed::7")

    def test_ports_keep_document_order(self):
        xml = ('<root BTCPP_format="4" main_tree_to_execute="A"><BehaviorTree ID="A">'
               '<Switch2 name="pick" _uid="1" variable="{mode}" case_1="GO" case_2="STOP"/>'
               '</BehaviorTree></root>')
        self.gw._parse_layout(xml)
        self.assertEqual(
            list(self.gw.tree_structure["ports"]), ["variable", "case_1", "case_2"])

    def test_scripting_hooks_count_as_ports(self):
        # _skipIf and friends are written by the tree author, unlike _uid.
        xml = ('<root BTCPP_format="4" main_tree_to_execute="A"><BehaviorTree ID="A">'
               '<Wait name="hold" _uid="1" _skipIf="done" msec="500"/>'
               '</BehaviorTree></root>')
        self.gw._parse_layout(xml)
        self.assertEqual(
            self.gw.tree_structure["ports"], {"_skipIf": "done", "msec": "500"})

        # Both ends of the hook family, as the mock robot publishes them.
        self.gw._parse_layout(mock_robot.TREE_XML)
        is_door_closed = _find(self.gw.tree_structure, 6)
        self.assertEqual(is_door_closed["ports"], {"_skipIf": "door_open"})
        pick_lock = _find(self.gw.tree_structure, 11)
        self.assertEqual(pick_lock["ports"], {"_onSuccess": "lock_status:='picked'"})


class StaticAssetsTest(unittest.TestCase):
    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)
        self.gw._load_static()

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    def request(self, path):
        return self.gw._process_request(None, types.SimpleNamespace(path=path))

    def test_real_assets_loaded(self):
        self.assertIn("/index.html", self.gw._static)
        self.assertIn("/styles.css", self.gw._static)
        self.assertIn("/app.js", self.gw._static)
        self.assertIn("/d3.v7.min.js", self.gw._static)
        html_body, html_ct = self.gw._static["/index.html"]
        self.assertIn(html_ct, "text/html; charset=utf-8")
        self.assertIn(b"klein", html_body)
        _css_body, css_ct = self.gw._static["/styles.css"]
        self.assertEqual(css_ct, "text/css; charset=utf-8")
        _js_body, js_ct = self.gw._static["/app.js"]
        self.assertEqual(js_ct, "text/javascript; charset=utf-8")
        d3_body, d3_ct = self.gw._static["/d3.v7.min.js"]
        self.assertEqual(d3_ct, "text/javascript; charset=utf-8")
        self.assertGreater(len(d3_body), 200000)

    def test_ws_path_defers_to_upgrade(self):
        self.assertIsNone(self.request("/ws"))

    def test_root_serves_index(self):
        r = self.request("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(r.body, self.gw._static["/index.html"][0])

    def test_query_string_is_stripped(self):
        self.assertEqual(self.request("/index.html?v=2").status_code, 200)

    def test_css_served_with_css_type(self):
        r = self.request("/styles.css")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "text/css; charset=utf-8")

    def test_d3_served_with_js_type(self):
        r = self.request("/d3.v7.min.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "text/javascript; charset=utf-8")

    def test_unknown_path_404(self):
        r = self.request("/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.body, b"not found")

    def test_missing_files_fall_back(self):
        # Simulate a package where none of the static assets are bundled.
        with mock.patch.object(gateway, "_load_asset", return_value=None):
            fresh = KleinGateway("127.0.0.1", 1667, 8080)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                fresh._load_static()
            try:
                self.assertEqual(fresh._static["/index.html"][0], gateway._INDEX_FALLBACK)
                self.assertNotIn("/styles.css", fresh._static)
                self.assertNotIn("/app.js", fresh._static)
                self.assertNotIn("/d3.v7.min.js", fresh._static)
                # Both render-critical assets are reported missing.
                self.assertIn("d3.v7.min.js not bundled", err.getvalue())
                self.assertIn("app.js not bundled", err.getvalue())
            finally:
                fresh.ctx.destroy(linger=0)


class RobotStateTest(unittest.TestCase):
    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    def test_initial_state_is_disconnected(self):
        msg = json.loads(self.gw._robot_state_json)
        self.assertEqual(msg["type"], "robot")
        self.assertFalse(msg["connected"])
        self.assertFalse(self.gw._robot_connected)

    def test_change_updates_cached_message(self):
        self.gw._set_robot_state(True, "Connected to robot at tcp://x")
        self.assertTrue(self.gw._robot_connected)
        msg = json.loads(self.gw._robot_state_json)
        self.assertTrue(msg["connected"])
        self.assertEqual(msg["detail"], "Connected to robot at tcp://x")

    def test_no_op_when_unchanged(self):
        self.gw._set_robot_state(True, "same")
        cached = self.gw._robot_state_json
        self.gw._set_robot_state(True, "same")       # identical -> not rebuilt/rebroadcast
        self.assertIs(self.gw._robot_state_json, cached)

    def test_transition_back_to_disconnected(self):
        self.gw._set_robot_state(True, "up")
        self.gw._set_robot_state(False, "down — retrying…")
        self.assertFalse(self.gw._robot_connected)
        self.assertEqual(json.loads(self.gw._robot_state_json)["detail"], "down — retrying…")


class BlackboardTest(unittest.TestCase):
    """Blackboard name discovery and msgpack decoding."""

    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    def names(self, xml):
        return KleinGateway.extract_blackboard_names(ET.fromstring(xml))

    def test_prefers_fullpath_and_dedupes_subtree_reference(self):
        # The mock's XML carries "DoorClosed::7" on both the <BehaviorTree>
        # block and the <SubTree> element referencing it.
        self.assertEqual(self.names(mock_robot.TREE_XML), ["MainTree", "DoorClosed::7"])

    def test_falls_back_to_tree_id_without_fullpath(self):
        xml = """<root BTCPP_format="4">
          <BehaviorTree ID="MainTree"><Sequence _uid="1"/></BehaviorTree>
          <BehaviorTree ID="Helper"><Sequence _uid="2"/></BehaviorTree>
        </root>"""
        self.assertEqual(self.names(xml), ["MainTree", "Helper"])

    def test_root_tree_has_an_empty_fullpath(self):
        # What a real robot sends: the root subtree's path is "", and its
        # blackboard is registered under the tree ID instead.
        xml = """<root BTCPP_format="4">
          <BehaviorTree ID="MainTree" _fullpath=""><Sequence _uid="1"/></BehaviorTree>
        </root>"""
        self.assertEqual(self.names(xml), ["MainTree"])

    def test_tree_nodes_model_is_not_mistaken_for_an_instance(self):
        # A FULLTREE reply also declares the available node types; the <SubTree>
        # in that section is a model, not a subtree instance with a blackboard.
        xml = """<root BTCPP_format="4">
          <BehaviorTree ID="MainTree" _fullpath=""><Sequence _uid="1"/></BehaviorTree>
          <TreeNodesModel>
            <SubTree ID="SubTree"/>
            <Action ID="OpenDoor"/>
          </TreeNodesModel>
        </root>"""
        self.assertEqual(self.names(xml), ["MainTree"])

    def test_nested_subtree_paths_are_collected(self):
        xml = """<root BTCPP_format="4">
          <BehaviorTree ID="MainTree" _fullpath="MainTree">
            <Sequence _uid="1">
              <SubTree ID="Nav" _uid="2" _fullpath="Nav::2"/>
              <SubTree ID="Nav" _uid="3" _fullpath="second_nav"/>
            </Sequence>
          </BehaviorTree>
          <BehaviorTree ID="Nav" _fullpath="Nav::2"><Sequence _uid="4"/></BehaviorTree>
        </root>"""
        # Two instances of the same subtree have distinct blackboards.
        self.assertEqual(self.names(xml), ["MainTree", "Nav::2", "second_nav"])

    def test_parse_layout_builds_the_request_payload(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        self.assertEqual(self.gw._blackboard_names, ["MainTree", "DoorClosed::7"])
        self.assertEqual(self.gw._blackboard_request, b"MainTree;DoorClosed::7")

    def test_parse_layout_invalidates_cached_values(self):
        self.gw._blackboard_json = '{"type": "blackboard", "data": {"stale": {}}}'
        self.gw._parse_layout(mock_robot.TREE_XML)
        self.assertIsNone(self.gw._blackboard_json)

    def test_nil_payload_means_no_boards(self):
        # The publisher replies msgpack nil when no requested name matched.
        self.assertEqual(KleinGateway.parse_blackboard(msgpack.packb(None)), {})

    def test_private_keys_are_filtered(self):
        raw = msgpack.packb(
            {"MainTree": {"speed": 1, "_debug_internal": "x", "_scratch": 4}})
        self.assertEqual(KleinGateway.parse_blackboard(raw), {"MainTree": {"speed": 1}})

    def test_value_types_survive_decoding(self):
        raw = msgpack.packb({"MainTree": {
            "flag": 1,                                   # BT.CPP stores bools as ints
            "name": "gripper",
            "speed": 0.25,
            "vec": [1.1, 2.2],
            "pos": {"__type": "Position2D", "x": 1.0},
            "unset": None,                               # declared but never written
        }})
        board = KleinGateway.parse_blackboard(raw)["MainTree"]
        self.assertEqual(board["flag"], 1)
        self.assertEqual(board["name"], "gripper")
        self.assertEqual(board["vec"], [1.1, 2.2])
        self.assertEqual(board["pos"]["__type"], "Position2D")
        self.assertIsNone(board["unset"])

    def test_boards_are_ordered_by_the_requested_tree_order(self):
        # The robot walks an unordered map, so it may answer in any order; the
        # dashboard should still list the root tree first.
        raw = msgpack.packb({"DoorClosed::7": {"a": 1}, "MainTree": {"b": 2}})
        order = ["MainTree", "DoorClosed::7"]
        self.assertEqual(list(KleinGateway.parse_blackboard(raw, order)), order)

    def test_unrequested_boards_are_kept_after_the_known_ones(self):
        raw = msgpack.packb({"Surprise": {"a": 1}, "MainTree": {"b": 2}})
        parsed = KleinGateway.parse_blackboard(raw, ["MainTree", "Absent"])
        self.assertEqual(list(parsed), ["MainTree", "Surprise"])

    def test_nil_board_is_reported_empty_not_dropped(self):
        # Real behaviour: a subtree whose only port is remapped to its parent
        # holds nothing locally, and the publisher sends nil for that board.
        # The dashboard must still list the subtree.
        raw = msgpack.packb({"MainTree": {"a": 1}, "DoorClosed::7": None})
        parsed = KleinGateway.parse_blackboard(raw, ["MainTree", "DoorClosed::7"])
        self.assertEqual(parsed, {"MainTree": {"a": 1}, "DoorClosed::7": {}})

    def test_unexpected_board_shape_degrades_to_empty(self):
        raw = msgpack.packb({"Good": {"a": 1}, "Bad": "not a board"})
        self.assertEqual(KleinGateway.parse_blackboard(raw),
                         {"Good": {"a": 1}, "Bad": {}})

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
        board = KleinGateway.parse_blackboard(raw)
        encoded = json.dumps({"type": "blackboard", "data": board})
        for token in ("NaN", "Infinity"):
            self.assertNotIn(token, encoded.replace('"', ""))   # not as bare literals
        decoded = json.loads(encoded)["data"]["MainTree"]
        self.assertEqual(decoded["nan"], "nan")
        self.assertEqual(decoded["inf"], "inf")
        self.assertEqual(decoded["nested"][1]["deep"], "nan")
        self.assertIsInstance(decoded["blob"], str)

    def test_integer_keys_are_stringified(self):
        raw = msgpack.packb({"MainTree": {1: "one"}})
        board = KleinGateway.parse_blackboard(raw)
        self.assertEqual(board["MainTree"], {"1": "one"})
        json.dumps(board)      # must not raise


# A trimmed copy of a real ROS 2 mission tree's FULLTREE reply. It carries the
# three shapes that matter: one subtree ID defined more than once with a
# different _fullpath per instance, a nested instance path, and an instance
# named in the XML whose path therefore carries no ::uid at all.
REAL_SHAPED_XML = """<root BTCPP_format="4" main_tree_to_execute="MissionBehaviorTree">
  <BehaviorTree ID="Pick_SubTree" _fullpath="Pick_SubTree::12">
    <Sequence _uid="13">
      <SubTree ID="MoveLift" _uid="17"
               _fullpath="Pick_SubTree::12/MoveLift::17"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="MissionBehaviorTree" _fullpath="">
    <Sequence _uid="1">
      <SubTree ID="Pick_SubTree" _uid="12" _fullpath="Pick_SubTree::12"/>
      <SubTree ID="MoveLift" _uid="46" _fullpath="park_sequence"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="MoveLift" _fullpath="park_sequence">
    <Action ID="SetLiftHeight" _uid="47"/>
  </BehaviorTree>
</root>"""


class BoardOnLayoutTest(unittest.TestCase):
    """Each layout node carries the blackboard its subtree instance owns.

    The dashboard pairs a board with the node that owns it — to indent nested
    boards under their parent, and to fly the camera to a board's card. The name
    alone cannot do that (``park_sequence`` carries no uid), so the pairing has
    to come from the layout.
    """

    def setUp(self):
        self.gw = KleinGateway("127.0.0.1", 1667, 8080)

    def tearDown(self):
        self.gw.ctx.destroy(linger=0)

    @staticmethod
    def boards(node, depth=0):
        """Walk the layout the way the dashboard does: [(board, depth, uid)]."""
        found = []
        board = node.get("board")
        if board:
            found.append((board, depth, node.get("uid")))
        for child in node.get("children", []):
            found.extend(BoardOnLayoutTest.boards(child, depth + 1 if board else depth))
        return found

    def test_subtree_node_carries_its_instance_path(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        subtree = self.gw.tree_structure["children"][2]["children"][1]
        self.assertEqual(subtree["type"], "SubTree")
        self.assertEqual(subtree["board"], "DoorClosed::7")

    def test_subtree_node_falls_back_to_its_id(self):
        xml = """<root BTCPP_format="4">
          <BehaviorTree ID="MainTree">
            <Sequence _uid="1"><SubTree ID="Nav" _uid="2"/></Sequence>
          </BehaviorTree>
          <BehaviorTree ID="Nav"><Action ID="Go" _uid="3"/></BehaviorTree>
        </root>"""
        self.gw._parse_layout(xml)
        self.assertEqual(self.gw.tree_structure["children"][0]["board"], "Nav")

    def test_root_board_comes_from_its_own_block(self):
        self.gw._parse_layout(mock_robot.TREE_XML)
        self.assertEqual(self.gw.tree_structure["board"], "MainTree")

    def test_root_board_falls_back_to_the_tree_id(self):
        # What a real robot sends: the root block's _fullpath is empty.
        self.gw._parse_layout(REAL_SHAPED_XML)
        self.assertEqual(self.gw.tree_structure["board"], "MissionBehaviorTree")

    def test_root_board_ignores_a_non_first_entrypoint(self):
        # main_tree_to_execute names the *second* block in REAL_SHAPED_XML, so
        # taking the first block's path would label the root "Pick_SubTree::12".
        self.gw._parse_layout(REAL_SHAPED_XML)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "MissionBehaviorTree")
        self.assertEqual(self.gw.tree_structure["board"], "MissionBehaviorTree")

    def test_repeated_subtree_id_keeps_per_instance_paths(self):
        self.gw._parse_layout(REAL_SHAPED_XML)
        found = self.boards(self.gw.tree_structure)
        self.assertEqual(found, [
            ("MissionBehaviorTree", 0, 1),
            ("Pick_SubTree::12", 1, 12),
            ("Pick_SubTree::12/MoveLift::17", 2, 17),
            ("park_sequence", 1, 46),           # named instance: no ::uid in the path
        ])

    def test_every_requested_board_resolves_to_exactly_one_node(self):
        # The invariant the whole panel rests on: what klein asks the robot for
        # and what it can place in the tree are the same set, with no board
        # claimed twice.
        for xml in (mock_robot.TREE_XML, REAL_SHAPED_XML):
            with self.subTest(xml=xml[:40]):
                self.gw._parse_layout(xml)
                found = [board for board, _depth, _uid in self.boards(self.gw.tree_structure)]
                self.assertEqual(sorted(found), sorted(self.gw._blackboard_names))
                self.assertEqual(len(found), len(set(found)))


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


if __name__ == "__main__":
    unittest.main()
