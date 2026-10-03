"""Unit tests for klein.gateway: UID extraction, status parsing, subtree
unrolling, ports, node categories, layout caching, blackboard decoding,
static-asset serving and the CLI.

``TreeSwapTest`` and ``RecordingGatewayTest`` drive the real pollers, since the
re-handshake and the recording live inside them; they stub ``_request`` rather
than opening a socket.
"""
import asyncio
import contextlib
import io
import json
import socket
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import msgpack

from klein import cli, gateway, layout, mock_robot
from klein.cli import _port_available
from klein.gateway import KleinGateway
from klein.groot2_protocol import (REQ_FULLTREE, REQ_STATUS, TRANSITION_BUFFER_MAX,
                                   parse_blackboard, parse_status)
from klein.recording import Recording
from tests.helpers import (UUID_A, UUID_B, FakeRobot, answer, collect_ids, collect_uids,
                           new_gateway, reply_header, status_record)


def _find(node, uid):
    if node.get("uid") == uid:
        return node
    for child in node.get("children", []):
        hit = _find(child, uid)
        if hit is not None:
            return hit
    return None


class GatewayTestCase(unittest.TestCase):
    def setUp(self):
        self.gw = new_gateway(self)


class ExtractUidTest(unittest.TestCase):
    def test_extract_uid(self):
        cases = [
            ("underscore uid", '<Action _uid="5"/>', 5),
            ("plain uid fallback", '<Action uid="7"/>', 7),
            # ID is a subtree *name*, not a numeric UID.
            ("ID alone", '<SubTree ID="PickSub"/>', None),
            ("ID beside _uid", '<SubTree ID="PickSub" _uid="4"/>', 4),
            ("non-numeric", '<Action _uid="abc"/>', None),
            ("missing", '<Action/>', None),
        ]
        for label, xml, expected in cases:
            with self.subTest(label):
                self.assertEqual(layout.extract_uid(ET.fromstring(xml)), expected)


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


class LayoutTest(GatewayTestCase):
    def test_crossdoor_unrolls_with_unique_string_ids(self):
        self.gw._parse_layout(mock_robot.CROSSDOOR.xml)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "MainTree")
        uids = collect_uids(self.gw.tree_structure)
        self.assertEqual(sorted(uids), sorted(mock_robot.CROSSDOOR.uids))
        self.assertEqual(len(uids), self.gw._node_seq)   # every node got a stable id + uid
        ids = collect_ids(self.gw.tree_structure)
        self.assertEqual(len(ids), len(set(ids)))
        msg = json.loads(self.gw._layout_json)
        self.assertEqual(msg["type"], "layout")
        self.assertEqual(msg["data"]["root_tree_id"], "MainTree")
        self.assertTrue(all(isinstance(i, str) for i in collect_ids(msg["data"])))

    def test_a_rehandshake_gets_fresh_node_ids(self):
        # Ids that restarted per tree would let the dashboard's keyed join match
        # a new tree's nodes onto the old tree's cards, keeping the old labels.
        self.gw._parse_layout(mock_robot.CROSSDOOR.xml)
        first = set(collect_ids(self.gw.tree_structure))
        self.gw._parse_layout(MODEL_LESS_XML)
        second = set(collect_ids(self.gw.tree_structure))
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(first & second, set())

    def test_root_tree_selection(self):
        two_blocks = ('<BehaviorTree ID="First"><Action _uid="1"/></BehaviorTree>'
                      '<BehaviorTree ID="Second"><Action _uid="2"/></BehaviorTree></root>')
        cases = [
            ("main_tree_to_execute honoured",
             '<root BTCPP_format="4" main_tree_to_execute="Second">' + two_blocks, "Second"),
            ("first block without entrypoint", '<root BTCPP_format="4">' + two_blocks, "First"),
            ("missing entrypoint falls back to first",
             '<root BTCPP_format="4" main_tree_to_execute="Nope">'
             '<BehaviorTree ID="First"><Action _uid="1"/></BehaviorTree></root>', "First"),
        ]
        for label, xml, expected in cases:
            with self.subTest(label):
                self.gw._parse_layout(xml)
                self.assertEqual(self.gw.tree_structure["root_tree_id"], expected)

    def test_unusable_xml_raises(self):
        cases = [
            ("no BehaviorTree", '<root BTCPP_format="4"></root>'),
            ("empty BehaviorTree",
             '<root BTCPP_format="4"><BehaviorTree ID="X"></BehaviorTree></root>'),
        ]
        for label, xml in cases:
            with self.subTest(label), self.assertRaises(ValueError):
                self.gw._parse_layout(xml)

    def test_subtree_stitched_in_place(self):
        self.gw._parse_layout(mock_robot.CROSSDOOR.xml)
        subtree_ref = _find(self.gw.tree_structure, 7)   # <SubTree ID="DoorClosed" _uid="7"/>
        self.assertTrue(subtree_ref["is_subtree_root"])
        self.assertEqual(subtree_ref["subtree_id"], "DoorClosed")
        self.assertEqual(subtree_ref["category"], "SubTree")
        # its single child is the DoorClosed definition root (Fallback "tryOpen" _uid="8")
        self.assertEqual(subtree_ref["children"][0]["uid"], 8)

    def test_cyclic_subtree_terminates(self):
        xml = ('<root BTCPP_format="4" main_tree_to_execute="A">'
               '<BehaviorTree ID="A"><SubTree ID="A" _uid="1"/></BehaviorTree></root>')
        self.gw._parse_layout(xml)                       # must not recurse forever
        top = self.gw.tree_structure
        self.assertTrue(top["is_subtree_root"])
        # expanded exactly once; the cycle guard stops the inner copy from expanding
        self.assertEqual(top["children"][0]["children"], [])


class PortsTest(GatewayTestCase):
    """Ports ride along with the layout so the dashboard can draw them."""

    def test_crossdoor_ports(self):
        self.gw._parse_layout(mock_robot.CROSSDOOR.xml)
        cases = [
            (2, {"code": "door_open:=false"}),           # <Script code="door_open:=false"/>
            (10, {"num_attempts": "5"}),                 # <RetryUntilSuccessful num_attempts="5"/>
            (1, {}),                                     # name + _uid only
            (9, {}),                                     # inside a subtree, still bare
            # A <SubTree>'s remapping is the author's writing, so it shows; ID
            # and _fullpath are how klein stitches and names the instance.
            (7, {"door_open": "{door_open}"}),
            # Both ends of the scripting-hook family, as the mock publishes them.
            (6, {"_skipIf": "door_open"}),
            (11, {"_onSuccess": "lock_status:='picked'"}),
        ]
        for uid, expected in cases:
            with self.subTest(uid=uid):
                self.assertEqual(_find(self.gw.tree_structure, uid)["ports"], expected)
        subtree_ref = _find(self.gw.tree_structure, 7)
        self.assertEqual(subtree_ref["subtree_id"], "DoorClosed")
        self.assertEqual(subtree_ref["board"], "DoorClosed::7")

    def test_inline_ports(self):
        cases = [
            ("document order",
             '<Switch2 name="pick" _uid="1" variable="{mode}" case_1="GO" case_2="STOP"/>',
             [("variable", "{mode}"), ("case_1", "GO"), ("case_2", "STOP")]),
            # _skipIf and friends are written by the tree author, unlike _uid.
            ("scripting hook",
             '<Wait name="hold" _uid="1" _skipIf="done" msec="500"/>',
             [("_skipIf", "done"), ("msec", "500")]),
        ]
        for label, node, expected in cases:
            with self.subTest(label):
                self.gw._parse_layout(
                    '<root BTCPP_format="4" main_tree_to_execute="A"><BehaviorTree ID="A">'
                    + node + '</BehaviorTree></root>')
                self.assertEqual(list(self.gw.tree_structure["ports"].items()), expected)


MODEL_LESS_XML = """<root BTCPP_format="4" main_tree_to_execute="MainTree">
  <BehaviorTree ID="MainTree">
    <Sequence _uid="1">
      <Inverter _uid="2"><Dock _uid="3" pad="1"/></Inverter>
      <Sequence _uid="4"><Wait _uid="5" msec="500"/></Sequence>
    </Sequence>
  </BehaviorTree>
</root>"""


class NodeCategoryTest(GatewayTestCase):
    """Every node carries the category the robot declared for it, so the
    dashboard can style Controls, Decorators, Conditions and Actions apart."""

    def categories(self, node, acc=None):
        acc = {} if acc is None else acc
        acc[node["uid"]] = node["category"]
        for child in node["children"]:
            self.categories(child, acc)
        return acc

    def parse(self, xml):
        self.gw._parse_layout(xml)
        return self.categories(self.gw.tree_structure)

    def test_model_section_decides_every_category(self):
        found = self.parse(mock_robot.CROSSDOOR.xml)
        cases = [
            (1, "Control"),       # Sequence
            (5, "Decorator"),     # Inverter
            (6, "Condition"),     # IsDoorClosed
            (7, "SubTree"),       # the <SubTree> reference
            (10, "Decorator"),    # Retry, inside the subtree
            # OpenDoor and SmashDoor are both childless; the robot registers one
            # as an Action and the other as a Condition. Only the model knows.
            (9, "Action"),
            (12, "Condition"),
        ]
        for uid, expected in cases:
            with self.subTest(uid=uid):
                self.assertEqual(found[uid], expected)

    def test_a_robot_with_no_model_section(self):
        # The builtin table covers the standard nodes; a custom one is
        # Undefined, never guessed.
        found = self.parse(MODEL_LESS_XML)
        cases = [
            (1, "Control"),
            (2, "Decorator"),
            (4, "Control"),       # a one-child Sequence is still Control
            (3, "Undefined"),     # custom Dock, 0 children
            (5, "Undefined"),     # custom Wait, 0 children
        ]
        for uid, expected in cases:
            with self.subTest(uid=uid):
                self.assertEqual(found[uid], expected)

    def test_explicit_category_tag_is_believed(self):
        # The editor spelling: the tag IS the category and ID is the
        # registration name. See docs/protocol.md.
        self.parse(REAL_SHAPED_XML)
        self.assertEqual(_find(self.gw.tree_structure, 47)["category"], "Action")

    def test_model_map_is_rebuilt_per_handshake(self):
        self.parse(mock_robot.CROSSDOOR.xml)
        found = self.parse(MODEL_LESS_XML)      # a different robot, no model
        self.assertEqual(self.gw._node_categories, {})
        self.assertEqual(found[3], "Undefined")

    def test_malformed_model_entries_are_skipped(self):
        xml = """<root BTCPP_format="4">
          <BehaviorTree ID="MainTree"><Dock _uid="1"/></BehaviorTree>
          <TreeNodesModel>
            <Action/>
            <NotACategory ID="Dock"/>
            <MetadataFields><Metadata author="x"/></MetadataFields>
          </TreeNodesModel>
        </root>"""
        self.parse(xml)
        self.assertEqual(self.gw._node_categories, {})
        self.assertEqual(self.gw.tree_structure["category"], "Undefined")

    def test_category_rides_in_the_cached_layout_frame(self):
        self.parse(mock_robot.CROSSDOOR.xml)
        data = json.loads(self.gw._layout_json)["data"]
        self.assertEqual(data["category"], "Control")
        self.assertEqual(data["type"], "Sequence")   # registration name untouched


class StaticAssetsTest(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.gw._load_static()

    def request(self, path):
        return answer(self.gw, path)

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

    def test_recording_model_scripts_served_and_packaged(self):
        pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
        html = self.gw._static["/index.html"][0].decode()
        for name in ("recording.js", "cursor.js", "drawer.js", "timeline.js"):
            with self.subTest(name):
                r = self.request(f"/{name}")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["Content-Type"], "text/javascript; charset=utf-8")
                self.assertIn(f'"static/{name}"', pyproject)
                # Before app.js, which builds its store at load time.
                self.assertLess(html.index(f'src="/{name}"'), html.index('src="/app.js"'))

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


class BlackboardTest(GatewayTestCase):
    """Blackboard name discovery and msgpack decoding."""

    def test_blackboard_names(self):
        cases = [
            # The mock's XML carries "DoorClosed::7" on both the <BehaviorTree>
            # block and the <SubTree> element referencing it.
            ("prefers _fullpath, dedupes the reference", mock_robot.CROSSDOOR.xml,
             ["MainTree", "DoorClosed::7"]),
            ("tree ID without _fullpath", """<root BTCPP_format="4">
              <BehaviorTree ID="MainTree"><Sequence _uid="1"/></BehaviorTree>
              <BehaviorTree ID="Helper"><Sequence _uid="2"/></BehaviorTree>
            </root>""", ["MainTree", "Helper"]),
            # What a real robot sends: the root subtree's path is "", and its
            # blackboard is registered under the tree ID instead.
            ("root with empty _fullpath", """<root BTCPP_format="4">
              <BehaviorTree ID="MainTree" _fullpath=""><Sequence _uid="1"/></BehaviorTree>
            </root>""", ["MainTree"]),
            # A FULLTREE reply also declares the available node types; the
            # <SubTree> there is a model, not an instance with a blackboard.
            ("TreeNodesModel is not an instance", """<root BTCPP_format="4">
              <BehaviorTree ID="MainTree" _fullpath=""><Sequence _uid="1"/></BehaviorTree>
              <TreeNodesModel>
                <SubTree ID="SubTree"/>
                <Action ID="OpenDoor"/>
              </TreeNodesModel>
            </root>""", ["MainTree"]),
            # Two instances of the same subtree have distinct blackboards.
            ("nested instance paths", """<root BTCPP_format="4">
              <BehaviorTree ID="MainTree" _fullpath="MainTree">
                <Sequence _uid="1">
                  <SubTree ID="Nav" _uid="2" _fullpath="Nav::2"/>
                  <SubTree ID="Nav" _uid="3" _fullpath="second_nav"/>
                </Sequence>
              </BehaviorTree>
              <BehaviorTree ID="Nav" _fullpath="Nav::2"><Sequence _uid="4"/></BehaviorTree>
            </root>""", ["MainTree", "Nav::2", "second_nav"]),
        ]
        for label, xml, expected in cases:
            with self.subTest(label):
                self.assertEqual(
                    layout.extract_blackboard_names(ET.fromstring(xml)), expected)

    def test_parse_layout_resets_the_request_and_the_cache(self):
        self.gw._blackboard_json = '{"type": "blackboard", "data": {"stale": {}}}'
        self.gw._parse_layout(mock_robot.CROSSDOOR.xml)
        self.assertEqual(self.gw._blackboard_names, ["MainTree", "DoorClosed::7"])
        self.assertEqual(self.gw._blackboard_request, b"MainTree;DoorClosed::7")
        self.assertIsNone(self.gw._blackboard_json)

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


class BoardOnLayoutTest(GatewayTestCase):
    """Each layout node carries the blackboard its subtree instance owns.

    The dashboard pairs a board with the node that owns it — to indent nested
    boards under their parent, and to fly the camera to a board's card. The name
    alone cannot do that (``park_sequence`` carries no uid), so the pairing has
    to come from the layout.
    """

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

    def test_board_fallbacks(self):
        cases = [
            ("subtree without _fullpath uses its ID", """<root BTCPP_format="4">
              <BehaviorTree ID="MainTree">
                <Sequence _uid="1"><SubTree ID="Nav" _uid="2"/></Sequence>
              </BehaviorTree>
              <BehaviorTree ID="Nav"><Action ID="Go" _uid="3"/></BehaviorTree>
            </root>""", lambda tree: tree["children"][0], "MainTree", "Nav"),
            ("root board from its own block", mock_robot.CROSSDOOR.xml,
             lambda tree: tree, "MainTree", "MainTree"),
            # The root block's _fullpath is empty, and main_tree_to_execute names
            # the *second* block: taking the first block's path would label the
            # root "Pick_SubTree::12".
            ("root board is the entrypoint's tree ID", REAL_SHAPED_XML,
             lambda tree: tree, "MissionBehaviorTree", "MissionBehaviorTree"),
        ]
        for label, xml, node, root_tree_id, board in cases:
            with self.subTest(label):
                self.gw._parse_layout(xml)
                self.assertEqual(self.gw.tree_structure["root_tree_id"], root_tree_id)
                self.assertEqual(node(self.gw.tree_structure)["board"], board)

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
        for xml in (mock_robot.CROSSDOOR.xml, REAL_SHAPED_XML):
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


class ConstructionTest(unittest.TestCase):
    def test_no_event_loop_required(self):
        # Every sync test in this suite builds a gateway, and unittest runs them
        # after IsolatedAsyncioTestCase has closed its loop. On Python 3.9 an
        # asyncio.Lock() built here would reach for get_event_loop() and raise,
        # so nothing in __init__ may touch the loop.
        asyncio.set_event_loop(None)
        try:
            gw = new_gateway(self)
        finally:
            asyncio.set_event_loop(asyncio.new_event_loop())
        self.assertIsNone(gw._req_lock)     # made on first use, inside the loop


class TreeIdentityTest(GatewayTestCase):
    """Deciding, from a reply header alone, whether the robot swapped trees."""

    def test_tree_changed(self):
        # Before the first handshake there is nothing to differ from.
        self.assertIsNone(self.gw._tree_uuid)
        cases = [
            ("nothing recorded yet", None, reply_header(UUID_A), False),
            # Absence of information must never be reported as a change, or an
            # error reply would send the gateway into a handshake loop.
            ("error frame", UUID_A, b"error", False),
            ("empty frame", UUID_A, b"", False),
            ("truncated header", UUID_A, reply_header(UUID_B)[:21], False),
            # The mock stamps the header, the gateway reads it.
            ("mock publisher, same tree", mock_robot.DEFAULT_TREE_UUID, reply_header(), False),
            ("mock publisher, swapped", mock_robot.DEFAULT_TREE_UUID, reply_header(UUID_B), True),
        ]
        for label, recorded, frame, changed in cases:
            with self.subTest(label):
                self.gw._tree_uuid = recorded
                self.assertEqual(self.gw._tree_changed(frame), changed)

    def test_the_detector_never_records(self):
        # fetch_layout is the only writer, and it takes the UUID from the same
        # reply that carried the XML. If detection adopted the new UUID here and
        # the re-handshake then failed, the next status frame would compare
        # equal and paint the new tree's UIDs onto the old tree's cards.
        self.gw._tree_uuid = UUID_A
        self.assertTrue(self.gw._tree_changed(reply_header(UUID_B)))
        self.assertEqual(self.gw._tree_uuid, UUID_A)


class NoticeTest(GatewayTestCase):
    """The transient frame that tells a dashboard why its canvas just changed."""

    def test_broadcast_once_to_every_client_and_never_cached(self):
        # Unlike layout/blackboard/robot frames, which ws_handler replays to
        # every client that connects. A notice reports something that already
        # happened, so replaying it would describe an event the client missed.
        client = object()
        self.gw.clients = {client}
        before = (self.gw._layout_json, self.gw._blackboard_json,
                  self.gw._robot_state_json)
        with mock.patch.object(gateway.websockets, "broadcast") as bcast:
            self.gw._broadcast_notice("hello")
        bcast.assert_called_once()
        clients, payload = bcast.call_args[0]
        self.assertEqual(clients, {client})
        self.assertEqual(json.loads(payload), {"type": "notice", "text": "hello"})
        self.assertEqual(
            (self.gw._layout_json, self.gw._blackboard_json,
             self.gw._robot_state_json), before)


class TreeSwapTest(unittest.IsolatedAsyncioTestCase):
    """A swapped tree, end to end through status_poller.

    ``_request`` is stubbed, so no socket is ever opened.
    """

    def setUp(self):
        self.fresh_gateway()

    def fresh_gateway(self):
        self.gw = new_gateway(self)
        # Marked connected before any client exists, so the poller's own
        # _mark_connected stays a no-op and the broadcast log holds only the
        # frames these tests are about.
        self.gw._mark_connected()
        self.gw.clients = {object()}        # else both pollers idle
        self.gw._parse_layout(mock_robot.CROSSDOOR.xml)
        self.gw._tree_uuid = UUID_A
        self.sent = []                      # request types the poller issued
        self.frames = []                    # frames broadcast to dashboards

    async def _run_poller(self, queue, poller=None, before_reply=None):
        """Run one poller against a queue of canned replies, then stop it.

        The stub parks once the queue is drained; the poller is cancelled from
        here rather than left to the loop. ``before_reply`` runs just before each
        reply is handed back, for tests that need something to happen while a
        request is in flight.
        """
        drained = asyncio.Event()

        async def request(request_type, payload=None):
            self.sent.append(request_type)
            if not queue:
                drained.set()
                await asyncio.sleep(3600)   # cancelled below
            if before_reply is not None:
                before_reply()
            return queue.pop(0)

        self.gw._request = request
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.object(gateway, "POLL_INTERVAL", 0), \
             mock.patch.object(gateway, "BLACKBOARD_POLL_INTERVAL", 0), \
             mock.patch.object(gateway.websockets, "broadcast",
                               side_effect=lambda _c, m: self.frames.append(json.loads(m))):
            task = asyncio.create_task((poller or self.gw.status_poller)())
            try:
                await asyncio.wait_for(drained.wait(), 2)
                await asyncio.sleep(0)      # let the poller finish the iteration
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def types(self):
        return [f["type"] for f in self.frames]

    def lose_robot(self):
        """Mark the robot unreachable, as a timed-out poll leaves it, before
        the broadcast log starts."""
        clients, self.gw.clients = self.gw.clients, set()
        self.gw._set_robot_state(False, "Lost connection to robot")
        self.gw.clients = clients

    async def test_matching_uuid_just_streams_status(self):
        await self._run_poller([[reply_header(UUID_A), status_record(1, 1)]])
        self.assertEqual(self.sent, [REQ_STATUS, REQ_STATUS])   # 2nd drains
        self.assertEqual(self.types(), ["status"])

    async def test_a_new_uuid_re_handshakes_and_sends_only_a_layout_then_a_notice(self):
        await self._run_poller([
            [reply_header(UUID_B), status_record(1, 1)],
            [reply_header(UUID_B, REQ_FULLTREE), MODEL_LESS_XML.encode()],
        ])
        self.assertIn(REQ_FULLTREE, self.sent)
        self.assertEqual(self.gw._tree_uuid, UUID_B)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "MainTree")
        self.assertEqual(sorted(collect_uids(self.gw.tree_structure)), [1, 2, 3, 4, 5])
        # No "status": the buffer that carried the new UUID indexes a tree the
        # dashboard has not been sent, so broadcasting it would land those UIDs
        # on the old tree's cards. And the note comes after the layout, so the
        # canvas has already redrawn by the time it explains why.
        self.assertEqual(self.types(), ["layout", "notice"])
        self.assertIn("new behaviour tree", self.frames[1]["text"])

    async def test_the_same_tree_again_is_absorbed_quietly(self):
        # A robot restart yields a fresh publisher UUID even when the tree is
        # unchanged, and a resume after an outage re-handshakes too. Re-rendering
        # the identical canvas would be a flash that reports nothing.
        cases = [("restart under a new uuid", True, UUID_B),
                 ("reconnect after an outage", False, UUID_A)]
        for label, connected, uuid in cases:
            with self.subTest(label):
                self.fresh_gateway()
                if not connected:
                    self.lose_robot()
                layout_before = self.gw._layout_json
                await self._run_poller([
                    [reply_header(uuid), status_record(1, 1)],
                    [reply_header(uuid, REQ_FULLTREE), mock_robot.CROSSDOOR.xml.encode()],
                ])
                self.assertEqual(self.gw._tree_uuid, uuid)
                self.assertEqual(self.gw._layout_generation, 1)     # not re-parsed
                self.assertIs(self.gw._layout_json, layout_before)
                self.assertNotIn("layout", self.types())
                self.assertNotIn("notice", self.types())

    async def test_telemetry_resuming_after_an_outage_re_handshakes(self):
        # Killing a robot and starting another on the same port looks like this
        # from here. Detection must not rest on the new process having drawn a
        # fresh UUID — here it deliberately reuses its predecessor's, and the
        # swap still has to be caught.
        self.lose_robot()
        await self._run_poller([
            [reply_header(UUID_A), status_record(1, 1)],
            [reply_header(UUID_A, REQ_FULLTREE), MODEL_LESS_XML.encode()],
        ])
        self.assertIn(REQ_FULLTREE, self.sent)
        self.assertEqual(self.gw.tree_structure["root_tree_id"], "MainTree")
        self.assertEqual(sorted(collect_uids(self.gw.tree_structure)), [1, 2, 3, 4, 5])
        self.assertEqual(self.types(), ["robot", "layout", "notice"])

    async def test_an_error_reply_does_not_re_handshake(self):
        await self._run_poller([[b"error", b"boom"]])
        self.assertNotIn(REQ_FULLTREE, self.sent)
        self.assertEqual(self.gw._tree_uuid, UUID_A)
        self.assertEqual(self.frames, [])

    async def test_blackboard_values_from_a_retired_tree_are_dropped(self):
        # The blackboard poller does not detect swaps, so one can land while its
        # request is in flight. See the guard in blackboard_poller for why the
        # reply must then be dropped rather than cached.
        await self._run_poller(
            [[reply_header(UUID_B), msgpack.packb({"MainTree": {"x": 1}})]],
            poller=self.gw.blackboard_poller,
            before_reply=lambda: self.gw._parse_layout(MODEL_LESS_XML),
        )
        self.assertIsNone(self.gw._blackboard_json)
        self.assertEqual(self.types(), [])


class RecordingGatewayTest(unittest.IsolatedAsyncioTestCase):
    """Arming, `t` drains, swaps, outages and overflow, through the real
    pollers with ``_request`` stubbed by ``FakeRobot``. No dashboard is
    connected in any of these: a recording gateway polls regardless."""

    def make_gateway(self, record=True):
        patch = mock.patch.object(gateway, "POLL_INTERVAL", 0)
        patch.start()
        self.addCleanup(patch.stop)
        return new_gateway(self, recording=Recording() if record else None)

    async def run_gateway(self, gw, robot, blackboard=False):
        """Handshake, then the status poller (and optionally the blackboard
        poller), until the robot has answered ``robot.limit`` requests."""
        gw._request = robot
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
        gw = self.make_gateway()
        robot = FakeRobot(limit=9)
        robot.drains = [[(10, 1, 1), (15, 2, 1)], [(40, 2, 2), (41, 2, 0)]]
        await self.run_gateway(gw, robot)
        self.assertEqual(gw.clients, set())             # no dashboard, still polling
        self.assertEqual(self.seq(robot), "TrSStStSt")
        rec = gw.recording
        self.assertEqual(len(rec.segments), 1)
        segment = rec.segments[0]
        self.assertEqual(segment.t_begin, robot.starts[0])
        self.assertIs(segment.layout, gw._recorded_tree)
        self.assertEqual(len(segment.baseline), 14)     # max uid 13, plus index 0
        start = robot.starts[0]
        self.assertEqual(list(s[1:] for s in segment.iter_records(0, segment.head_seq)),
                         [(start + 10, 1, 1), (start + 15, 2, 1),
                          (start + 40, 2, 2), (start + 41, 2, 0)])
        self.assertEqual(segment.state[2], 12)          # IDLE after SUCCESS
        self.assertIsNotNone(rec.head)
        self.assertEqual(rec.gaps, [])

    async def test_blackboard_polls_interleave_and_are_recorded(self):
        gw = self.make_gateway()
        robot = FakeRobot(limit=30)
        await self.run_gateway(gw, robot, blackboard=True)
        seq = self.seq(robot)
        self.assertIn("B", seq)
        self.assertRegex(seq.replace("B", ""), r"^TrS(St)+S?$")
        track = gw.recording.segments[0].blackboard
        self.assertIsNotNone(track.t_start)
        self.assertEqual(set(track.at(gw.recording.head + 10**6)), {"MainTree"})

    async def test_a_tree_swap_starts_a_segment_with_a_new_layout(self):
        gw = self.make_gateway()
        robot = FakeRobot(limit=11)

        def swap(r):
            r.uuid, r.xml = UUID_B, MODEL_LESS_XML
            r.status = b"".join(status_record(uid, 0) for uid in range(1, 6))
        robot.on_request[6] = swap
        robot.drains = [[(5, 1, 1)], [(7, 3, 1)]]
        gw.clients = {object()}
        frames = await self.run_gateway(gw, robot)
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
        gw = self.make_gateway()
        robot = FakeRobot(limit=12)
        robot.timeouts = {6}                            # the 2nd poll's S
        heads = []                                      # the head when the robot went
        robot.on_request[6] = lambda r: heads.append(gw.recording.head)
        await self.run_gateway(gw, robot)
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
        self.assertEqual(gw._layout_generation, 1)      # not re-parsed

    async def test_a_quick_restart_on_the_same_tree_stays_in_one_run(self):
        # Back before any poll timed out: only the UUID says it restarted.
        gw = self.make_gateway()
        robot = FakeRobot(limit=11)
        robot.on_request[6] = lambda r: setattr(r, "uuid", UUID_B)
        await self.run_gateway(gw, robot)
        self.assertEqual(self.seq(robot), "TrSSt" "S" "TrSSt")
        first, second = gw.recording.segments
        self.assertIs(first.layout, second.layout)
        self.assertEqual(first.t_end, second.t_begin)
        self.assertEqual([[s.id for s in run] for run in gw.recording.runs()], [[0, 1]])
        self.assertEqual(gw.recording.gaps, [])

    async def test_an_outage_seen_only_by_a_blackboard_timeout_still_gets_a_gap(self):
        # The robot dies while a B is in flight and is back, as a new publisher,
        # before any S times out: B's timeout marks where the segment ends.
        gw = self.make_gateway()
        robot = FakeRobot(limit=40)
        heads = []                          # the head when the re-handshake began

        def die(r):
            r.uuid = UUID_B                 # restarted by the time S gets through
            r.on_letter["T"] = lambda _r: heads.append(gw.recording.head)
            raise gateway.RobotTimeout("stub timeout")
        robot.on_letter["B"] = die
        await self.run_gateway(gw, robot, blackboard=True)
        first, second = gw.recording.segments[:2]
        self.assertEqual(first.t_end, heads[0])
        self.assertIs(first.layout, second.layout)
        self.assertLess(first.t_end, second.t_begin)
        self.assertEqual(gw.recording.gaps[0], (first.t_end, second.t_begin, "outage"))

    async def test_a_blackboard_timeout_alone_changes_nothing(self):
        gw = self.make_gateway()
        robot = FakeRobot(limit=40)

        def blip(r):
            raise gateway.RobotTimeout("stub timeout")
        robot.on_letter["B"] = blip
        await self.run_gateway(gw, robot, blackboard=True)
        self.assertEqual(len(gw.recording.segments), 1)
        self.assertEqual(gw.recording.gaps, [])
        self.assertFalse(gw._timed_out)

    async def test_a_full_drain_records_an_overflow_gap_and_re_arms(self):
        gw = self.make_gateway()
        robot = FakeRobot(limit=9)
        burst = [(1000 + i, 1 + i % 13, 1 + i % 3) for i in range(TRANSITION_BUFFER_MAX)]
        robot.drains = [[(5, 1, 1)], burst, [(3, 4, 1)]]
        await self.run_gateway(gw, robot)
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
        gw = self.make_gateway()
        robot = FakeRobot(limit=12)
        burst = [(1000 + i, 1, 1 + i % 2) for i in range(TRANSITION_BUFFER_MAX)]
        robot.drains = [burst, [(3, 4, 1)]]
        await self.run_gateway(gw, robot)
        self.assertEqual(self.seq(robot), "TrSSt" "rS" "St" "St" "S")
        second = gw.recording.segments[1]
        self.assertEqual(list(s[1:] for s in second.iter_records(0, 1)),
                         [(robot.starts[1] + 3, 4, 1)])

    async def test_a_robot_without_recording_gets_no_t_and_the_same_status(self):
        robot = FakeRobot(limit=7)
        robot.supports_recording = False
        gw = self.make_gateway()
        gw.clients = {object()}
        frames = await self.run_gateway(gw, robot)
        self.assertEqual(self.seq(robot), "Tr" "SSSSS")
        self.assertEqual(gw.recording.segments, [])
        self.assertFalse(gw._armed)
        # Status broadcasts are exactly those of a non-recording gateway.
        plain = FakeRobot(limit=6)
        other = self.make_gateway(record=False)
        other.clients = {object()}
        plain_frames = await self.run_gateway(other, plain)
        self.assertEqual(self.seq(plain), "TSSSSS")
        self.assertEqual([f for f in frames if f["type"] == "status"],
                         [f for f in plain_frames if f["type"] == "status"])
        # The drawer is told why there is no recording.
        self.assertEqual([f["recording"] for f in frames if f["type"] == "robot"],
                         ["on", "unsupported"])
        self.assertEqual(json.loads(gw._robot_state_json)["recording"], "unsupported")
        self.assertEqual({f["recording"] for f in plain_frames if f["type"] == "robot"}, {"off"})
        # The robot frame says recording is on again after a capable handshake.
        frames = await self.run_gateway(gw, FakeRobot(limit=4))
        self.assertEqual([f["recording"] for f in frames if f["type"] == "robot"], ["on"])

    async def poll_without_clients(self, gw, robot):
        """Handshake, then give both pollers 50 loop turns with no dashboard."""
        gw._request = robot
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
                gw = self.make_gateway(record=record)
                await self.poll_without_clients(gw, robot)
                self.assertEqual(self.seq(robot), expected)     # then nothing polled
                if supports:
                    continue
                self.assertTrue(gw._cannot_record)
                # The next handshake tries again, and a robot that can record is polled.
                robot.supports_recording, robot.sent = True, []
                await self.poll_without_clients(gw, robot)
                self.assertFalse(gw._cannot_record)
                self.assertTrue(self.seq(robot).startswith("TrSSt"), self.seq(robot))

    async def test_debug_state_reports_the_recording(self):
        gw = self.make_gateway()
        gw.debug = True
        robot = FakeRobot(limit=7)
        robot.drains = [[(10, 1, 1)], [(20, 1, 2)]]
        await self.run_gateway(gw, robot)
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
