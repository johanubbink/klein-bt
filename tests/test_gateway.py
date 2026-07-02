"""Unit tests for klein.gateway — the pure, socket-free logic: UID extraction,
status parsing, subtree unrolling, layout caching, and static-asset serving."""
import contextlib
import io
import json
import os
import socket
import struct
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

# Ensure the repo root is importable so `import mock_robot` (a repo-root sibling
# used here as a tree fixture) resolves regardless of how the tests are run.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from klein import gateway
from klein.gateway import KleinGateway, _port_available
from klein.groot2_protocol import STATUS_RECORD_FORMAT
import mock_robot


def _rec(uid, status_int):
    return struct.pack(STATUS_RECORD_FORMAT, uid, status_int)


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
        subtree_ref = _find(self.gw.tree_structure, 4)   # <SubTree ID="PickSub" _uid="4"/>
        self.assertTrue(subtree_ref["is_subtree_root"])
        self.assertEqual(subtree_ref["subtree_id"], "PickSub")
        # its single child is the PickSub definition root (Sequence _uid="20")
        self.assertEqual(subtree_ref["children"][0]["uid"], 20)

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
        self.assertIn("/d3.v7.min.js", self.gw._static)
        html_body, html_ct = self.gw._static["/index.html"]
        self.assertIn(html_ct, "text/html; charset=utf-8")
        self.assertIn(b"klein", html_body)
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

    def test_d3_served_with_js_type(self):
        r = self.request("/d3.v7.min.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "text/javascript; charset=utf-8")

    def test_unknown_path_404(self):
        r = self.request("/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.body, b"not found")

    def test_missing_files_fall_back(self):
        # Simulate a package where neither static file is bundled.
        with mock.patch.object(gateway, "_load_asset", return_value=None):
            fresh = KleinGateway("127.0.0.1", 1667, 8080)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                fresh._load_static()
            try:
                self.assertEqual(fresh._static["/index.html"][0], gateway._INDEX_FALLBACK)
                self.assertNotIn("/d3.v7.min.js", fresh._static)
                self.assertIn("d3.v7.min.js not bundled", err.getvalue())
            finally:
                fresh.ctx.destroy(linger=0)


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
