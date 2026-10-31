"""Unit tests for klein.layout: ``parse_layout`` of a FULLTREE XML into the
dashboard's tree (subtree unrolling, uids and ids, ports and their blackboard
bindings, node categories, and the blackboards to ask for)."""
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from klein import btlog, layout, mock_robot
from klein.layout import parse_layout
from tests.helpers import MODEL_LESS_XML, collect_ids, collect_uids


def _find(node, uid):
    if node.get("uid") == uid:
        return node
    for child in node.get("children", []):
        hit = _find(child, uid)
        if hit is not None:
            return hit
    return None


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


class LayoutTest(unittest.TestCase):
    def test_crossdoor_unrolls_with_unique_string_ids(self):
        parsed = parse_layout(mock_robot.CROSSDOOR.xml, 1)
        self.assertEqual(parsed.tree["root_tree_id"], "MainTree")
        uids = collect_uids(parsed.tree)
        self.assertEqual(parsed.uids, sorted(mock_robot.CROSSDOOR.uids))
        self.assertEqual(sorted(uids), parsed.uids)
        self.assertEqual(len(uids), parsed.node_count)  # every node got a stable id + uid
        ids = collect_ids(parsed.tree)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(isinstance(i, str) for i in ids))
        # The BLACKBOARD request's argument frame: every board, in tree order.
        self.assertEqual(parsed.blackboard_request, b"MainTree;DoorClosed::7")

    def test_each_generation_gets_its_own_node_ids(self):
        # Ids that restarted per tree would let the dashboard's keyed join match
        # a new tree's nodes onto the old tree's cards, keeping the old labels.
        # The gateway parses each handshake's new tree with the next generation.
        first = set(collect_ids(parse_layout(mock_robot.CROSSDOOR.xml, 1).tree))
        second = set(collect_ids(parse_layout(MODEL_LESS_XML, 2).tree))
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
                self.assertEqual(parse_layout(xml, 1).tree["root_tree_id"], expected)

    def test_unusable_xml_raises(self):
        cases = [
            ("no BehaviorTree", '<root BTCPP_format="4"></root>'),
            ("empty BehaviorTree",
             '<root BTCPP_format="4"><BehaviorTree ID="X"></BehaviorTree></root>'),
        ]
        for label, xml in cases:
            with self.subTest(label), self.assertRaises(ValueError):
                parse_layout(xml, 1)

    def test_subtree_stitched_in_place(self):
        tree = parse_layout(mock_robot.CROSSDOOR.xml, 1).tree
        subtree_ref = _find(tree, 7)                    # <SubTree ID="DoorClosed" _uid="7"/>
        self.assertTrue(subtree_ref["is_subtree_root"])
        self.assertEqual(subtree_ref["subtree_id"], "DoorClosed")
        self.assertEqual(subtree_ref["category"], "SubTree")
        # its single child is the DoorClosed definition root (Fallback "tryOpen" _uid="8")
        self.assertEqual(subtree_ref["children"][0]["uid"], 8)

    def test_cyclic_subtree_terminates(self):
        xml = ('<root BTCPP_format="4" main_tree_to_execute="A">'
               '<BehaviorTree ID="A"><SubTree ID="A" _uid="1"/></BehaviorTree></root>')
        top = parse_layout(xml, 1).tree                 # must not recurse forever
        self.assertTrue(top["is_subtree_root"])
        # expanded exactly once; the cycle guard stops the inner copy from expanding
        self.assertEqual(top["children"][0]["children"], [])


class PortsTest(unittest.TestCase):
    """Ports ride along with the layout so the dashboard can draw them."""

    def test_crossdoor_ports(self):
        tree = parse_layout(mock_robot.CROSSDOOR.xml, 1).tree
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
                self.assertEqual(_find(tree, uid)["ports"], expected)
        subtree_ref = _find(tree, 7)
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
                tree = parse_layout(
                    '<root BTCPP_format="4" main_tree_to_execute="A"><BehaviorTree ID="A">'
                    + node + '</BehaviorTree></root>', 1).tree
                self.assertEqual(list(tree["ports"].items()), expected)

    def bindings(self, xml):
        """``{uid: [(port, dir, board, key)]}`` for every node with a binding."""
        tree = parse_layout(xml, 1).tree
        found = {}

        def walk(node):
            if node["bindings"]:
                found[node["uid"]] = [(b["port"], b["dir"], b["board"], b["key"])
                                      for b in node["bindings"]]
            for child in node["children"]:
                walk(child)
        walk(tree)
        return found

    def test_bindings_resolve_to_the_board_holding_the_key(self):
        # References below are BehaviorTree.CPP files. The directions come from
        # each model's input_port/output_port/inout_port (xml_parsing.cpp :: addNodeModelToXML).
        t11 = (Path(__file__).parents[1] / "fixtures" / "t11_filelogger2.btlog").read_bytes()
        cases = {
            "crossdoor (mock)": (mock_robot.CROSSDOOR.xml, {
                # Script code: `x:=` writes x (script_node.h :: loadExecutor, operators.hpp :: ExprAssignment).
                2: [("code", "out", "MainTree", "door_open")],
                3: [("pos", "out", "MainTree", "robot_position")],
                # A hook reads its bare names on the node's own board (bt_factory.cpp :: instantiateTreeNode).
                6: [("_skipIf", "in", "MainTree", "door_open")],
                # A {key} remap names the parent's key (xml_parsing.cpp :: recursivelyCreateSubtree);
                # nothing inside writes or reads it, so the direction is unknown.
                7: [("door_open", "inout", "MainTree", "door_open")],
                # Not remapped: created on the subtree's own board (Blackboard::createEntryImpl).
                11: [("_onSuccess", "out", "DoorClosed::7", "lock_status")],
                # speed="0.35", timeout_ms="2500" are literals: no entry.
                13: [("goal", "in", "MainTree", "goal")],
            }),
            "patrol (mock)": (mock_robot.PATROL.xml, {
                # The remap's direction is what the subtree does with it: MoveTo reads.
                3: [("waypoint", "in", "PatrolTree", "next_waypoint")],
                # {waypoint} is remapped, so it lives on the parent (Blackboard::createEntryImpl).
                5: [("goal", "in", "PatrolTree", "next_waypoint")],
                # dwell_msec="1500" on the SubTree is a literal stored on the
                # subtree's board (recursivelyCreateSubtree), and found there first
                # (Blackboard::getEntry).
                6: [("msec", "in", "VisitWaypoints::3", "dwell_msec")],
            }),
            "t11 (real FULLTREE)": (btlog.read_btlog(t11).xml, {
                2: [("code", "out", "MainTree", "door_open")],
                3: [("pos", "out", "MainTree", "pos_2D"),
                    ("vec_double", "out", "MainTree", "doubles"),
                    ("vec_string", "out", "MainTree", "strings"),
                    ("waypoints", "out", "MainTree", "waypoints")],
                # The inner _onSuccess writes door_open, so the remap is out.
                7: [("door_open", "out", "MainTree", "door_open")],
                8: [("_onSuccess", "out", "MainTree", "door_open")],
            }),
            "remap kinds": (REMAP_KINDS_XML, {
                2: [("code", "out", "Main", k) for k in ("a", "b", "c", "root_k", "target", "speed")],
                3: [("x", "inout", "Main", "a")],
                # x := x + 1 reads and writes; x is remapped to {a}.
                5: [("code", "inout", "Main", "a"),
                    # _autoremap sends b to the parent (Blackboard::getEntry, createEntryImpl)...
                    ("code", "inout", "Main", "b"),
                    # ...but never a private key (blackboard.cpp :: IsPrivateKey).
                    ("code", "out", "Inner::3", "_p"),
                    ("code", "out", "Main", "lit2"),
                    # A literal port stays local even with _autoremap (recursivelyCreateSubtree).
                    ("code", "in", "Inner::3", "lit")],
                # `=` assigns; `==` compares (script_tokenizer.cpp matchTwoCharOp).
                6: [("code", "out", "Main", "c"), ("code", "in", "Inner::3", "num"),
                    ("_skipIf", "in", "Main", "b")],
                # {@k} goes to the root board (Blackboard::getEntry); {=} is {port}
                # (recursivelyCreateSubtree); z="=" is a literal on a SubTree.
                7: [("w", "out", "Main", "root_k"), ("y", "out", "Main", "y")],
                9: [("code", "out", "Main", "y"), ("code", "out", "Main", "root_k"),
                    ("code", "out", "Main", "g"), ("code", "out", "Inner2::7", "target"),
                    ("code", "out", "Inner2::7", "speed")],
                # {=} and = on an ordinary node are the port's own name
                # (TreeNode::getRemappedKey); label="plain" is a literal.
                10: [("target", "in", "Inner2::7", "target"),
                     ("speed", "in", "Inner2::7", "speed"),
                     ("note", "out", "Main", "g")],
                # output_key holds a key name (set_blackboard_node.h tick).
                11: [("output_key", "out", "Main", "sb"), ("value", "in", "Main", "a")],
            }),
            "hooks and key-name ports": (HOOKS_XML, {
                2: [("code", "out", "Main", k) for k in ("k", "e", "u", "f1", "f2")],
                # A SubTree's hooks run on the parent's board: they go into its
                # config, whose blackboard is the parent's (XMLParser::PImpl::createNodeFromXML).
                3: [("_skipIf", "in", "Main", "k"), ("_while", "in", "Main", "k"),
                    ("_onSuccess", "out", "Main", "done")],
                4: [("code", "out", "S::3", "inner")],
                5: [("code", "in", "Main", "k")],
                6: [("if", "in", "Main", "e")],           # else="FAILURE" is a literal
                7: [("_failureIf", "in", "Main", "f1"), ("_successIf", "in", "Main", "f2"),
                    ("_onHalted", "out", "Main", "hh"), ("_onFailure", "out", "Main", "ff"),
                    ("_post", "out", "Main", "pp")],
                # Plain key names (unset_blackboard_node.h, updated_action.cpp,
                # updated_decorator.cpp).
                8: [("key", "out", "Main", "u")],
                10: [("entry", "in", "Main", "k")],
                11: [("entry", "in", "Main", "e")],
                15: [("entry", "in", "Main", "k")],
            }),
        }
        for label, (xml, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.bindings(xml), expected)

    def test_script_references(self):
        cases = [
            ("door_open:=false", [("door_open", "out")]),
            ("a = b == c", [("a", "out"), ("b", "in"), ("c", "in")]),
            ("n += 1; m != 2 && m >= 0", [("n", "inout"), ("m", "in"), ("m", "in")]),
            # Strings, numbers, booleans and ALL_CAPS enums are not keys.
            ("msg := 'x := y'; ok := true", [("msg", "out"), ("ok", "out")]),
            ("state == RUNNING ? 0x1F : 2.5e3", [("state", "in")]),
            ("@g := 3foo", [("@g", "out")]),
        ]
        for code, expected in cases:
            with self.subTest(code):
                self.assertEqual([r[:2] for r in layout.script_references(code)], expected)

    def test_each_binding_says_where_its_port_names_the_key(self):
        """``at``: the braces of a {key} or a remap's {outer}, a whole `=` or
        {=}, a key-name port trimmed, and each name in a script, also inside a
        subtree whose two names are remapped to other keys outside."""
        tree = parse_layout(SPANS_XML, 1).tree
        found = {}

        def walk(node):
            for b in node["bindings"]:
                found.setdefault(node["uid"], []).append((b["port"], b["key"], b["at"]))
            for child in node["children"]:
                walk(child)
        walk(tree)
        self.assertEqual(found, {
            2: [("p", "a", [[1, 4]]), ("q", "b", [[0, 3]])],
            3: [("code", "a", [[0, 1], [9, 10]]), ("code", "b", [[5, 6]])],
            4: [("output_key", "sb", [[1, 3]]), ("value", "a", [[0, 3]])],
            5: [("target", "target", [[0, 1]]), ("speed", "speed", [[0, 3]])],
        })

    def test_binding_spans_name_their_key_in_every_tree(self):
        """In every mock tree and fixture: each span lies within its port's
        value and reads as a name, {name}, @name, = or {=}; on the node's own
        board that name (less an @) is the key (= and {=}: the port)."""
        trees = {name: tree.xml for name, tree in mock_robot.TREES.items()}
        for path in sorted((Path(__file__).parents[1] / "fixtures").glob("*.btlog")):
            trees[path.name] = btlog.read_btlog(path.read_bytes()).xml
        trees.update(remap_kinds=REMAP_KINDS_XML, hooks=HOOKS_XML, spans=SPANS_XML)
        for label, xml in trees.items():
            tree = parse_layout(xml, 1).tree
            spans = 0

            def walk(node, board):
                nonlocal spans
                for b in node["bindings"]:
                    value = node["ports"][b["port"]]
                    for start, end in b["at"]:
                        spans += 1
                        with self.subTest(label, uid=node["uid"], binding=b):
                            self.assertTrue(0 <= start < end <= len(value))
                            name = value[start:end]
                            if name[:1] == "{" and name[-1:] == "}":
                                name = name[1:-1]
                            self.assertRegex(name, r"^(=|@?[A-Za-z_]\w*)$")
                            if b["board"] == board:
                                self.assertEqual(b["key"], b["port"] if name == "=" else name.lstrip("@"))
                inner = node.get("board", board) if node.get("is_subtree_root") else board
                for child in node["children"]:
                    walk(child, inner)
            walk(tree, tree["board"])
            self.assertGreater(spans, 0, label)


# Real BehaviorTree.CPP 4 FULLTREE output (WriteTreeToXML, after createTree;
# names dropped) for a tree using every remap kind. Running it, BT.CPP held
# exactly these keys: "" (Main): a b c g lit2 root_k sb speed target y;
# Inner::3: _p lit num; Inner2::7: speed target z (z is an unread literal).
# Two edits: FULLTREE drops `_autoremap` (a reserved attribute,
# basic_types.cpp :: IsReservedAttribute), so it is put back here, and the SetBlackboard model is
# copied from the builtins t11 publishes. Root Script's target:= and speed:=
# land on Main, Inner2's on Inner2::7: no autoremap there.
REMAP_KINDS_XML = """<root BTCPP_format="4">
  <BehaviorTree ID="Main" _fullpath="">
    <Sequence _uid="1">
      <Script _uid="2" code="a:=1; b:=2; c:=3; root_k:=7; target:='t'; speed:='s'"/>
      <SubTree ID="Inner" _fullpath="Inner::3" _uid="3" _autoremap="true" num="5" lit="hello" x="{a}"/>
      <SubTree ID="Inner2" _fullpath="Inner2::7" _uid="7" w="{@root_k}" z="=" y="{=}"/>
      <SetBlackboard _uid="11" output_key="sb" value="{a}"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="Inner" _fullpath="Inner::3">
    <Sequence _uid="4">
      <Script _uid="5" code="x := x + 1; b := b + 1; _p := 1; lit2 := lit"/>
      <Script _uid="6" code="c = num" _skipIf="b == 99"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="Inner2" _fullpath="Inner2::7">
    <Sequence _uid="8">
      <Script _uid="9" code="y := 1; w := 4; @g := 3; target := 'u'; speed := 'v'"/>
      <Report _uid="10" target="{=}" speed="=" label="plain" note="{@g}"/>
    </Sequence>
  </BehaviorTree>
  <TreeNodesModel>
    <Action ID="Report">
      <input_port name="label" type="std::string"/>
      <input_port name="speed" type="std::string"/>
      <output_port name="note" type="int"/>
      <input_port name="target" type="std::string"/>
    </Action>
    <Action ID="SetBlackboard">
      <inout_port name="output_key" type="BT::AnyTypeAllowed"/>
      <input_port name="value" type="BT::AnyTypeAllowed"/>
    </Action>
  </TreeNodesModel>
</root>"""


# A subtree's script names two keys remapped to other names outside, beside a
# {key} with an outer space, a key-name port with spaces, `=` and {=}.
SPANS_XML = """<root BTCPP_format="4">
  <BehaviorTree ID="Main" _fullpath="">
    <Sequence _uid="1">
      <SubTree ID="S" _fullpath="S::2" _uid="2" p=" {a}" q="{b}"/>
      <SetBlackboard _uid="4" output_key=" sb " value="{a}"/>
      <Report _uid="5" target="=" speed="{=}"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="S" _fullpath="S::2">
    <Script _uid="3" code="p := q + p"/>
  </BehaviorTree>
</root>"""


# Real BehaviorTree.CPP 4 FULLTREE output for every hook and the builtins whose
# port holds a script or a key name (names and <TreeNodesModel> dropped).
# After a run BT.CPP held "" (Main): done e f1 f2 k pp; S::3: inner (u was
# unset; _onHalted and _onFailure never ran).
HOOKS_XML = """<root BTCPP_format="4">
  <BehaviorTree ID="Main" _fullpath="">
    <Sequence _uid="1">
      <Script _uid="2" code="k:=1; e:=0; u:=1; f1:=false; f2:=false"/>
      <SubTree ID="S" _fullpath="S::3" _uid="3" _skipIf="k == 9" _while="k &lt; 5" _onSuccess="done := true"/>
      <ScriptCondition _uid="5" code="k != 0"/>
      <Precondition _uid="6" if="e == 0" else="FAILURE">
        <AlwaysSuccess _uid="7" _failureIf="f1" _successIf="f2" _onHalted="hh := 1" _onFailure="ff := 1" _post="pp := 1"/>
      </Precondition>
      <UnsetBlackboard _uid="8" key="u"/>
      <ForceSuccess _uid="9"><WasEntryUpdated _uid="10" entry="k"/></ForceSuccess>
      <SkipUnlessUpdated _uid="11" entry="e"><AlwaysSuccess _uid="12"/></SkipUnlessUpdated>
      <ForceSuccess _uid="13">
        <Timeout _uid="14" msec="5">
          <WaitValueUpdate _uid="15" entry="k"><AlwaysSuccess _uid="16"/></WaitValueUpdate>
        </Timeout>
      </ForceSuccess>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="S" _fullpath="S::3">
    <Script _uid="4" code="inner := 1"/>
  </BehaviorTree>
</root>"""


class NodeCategoryTest(unittest.TestCase):
    """Every node carries the category the robot declared for it, so the
    dashboard can style Controls, Decorators, Conditions and Actions apart."""

    def categories(self, node, acc=None):
        acc = {} if acc is None else acc
        acc[node["uid"]] = node["category"]
        for child in node["children"]:
            self.categories(child, acc)
        return acc

    def test_model_section_decides_every_category(self):
        tree = parse_layout(mock_robot.CROSSDOOR.xml, 1).tree
        found = self.categories(tree)
        self.assertEqual(tree["type"], "Sequence")     # the registration name, untouched
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
        parsed = parse_layout(MODEL_LESS_XML, 1)
        self.assertEqual(parsed.categories, {})
        found = self.categories(parsed.tree)
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
        tree = parse_layout(REAL_SHAPED_XML, 1).tree
        self.assertEqual(_find(tree, 47)["category"], "Action")

    def test_malformed_model_entries_are_skipped(self):
        xml = """<root BTCPP_format="4">
          <BehaviorTree ID="MainTree"><Dock _uid="1"/></BehaviorTree>
          <TreeNodesModel>
            <Action/>
            <NotACategory ID="Dock"/>
            <MetadataFields><Metadata author="x"/></MetadataFields>
          </TreeNodesModel>
        </root>"""
        parsed = parse_layout(xml, 1)
        self.assertEqual(parsed.categories, {})
        self.assertEqual(parsed.tree["category"], "Undefined")


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
    """The blackboards klein asks the robot for, and each layout node carrying
    the blackboard its subtree instance owns.

    The dashboard pairs a board with the node that owns it — to indent nested
    boards under their parent, and to fly the camera to a board's card. The name
    alone cannot do that (``park_sequence`` carries no uid), so the pairing has
    to come from the layout.
    """

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
                self.assertEqual(parse_layout(xml, 1).blackboard_names, expected)

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
                tree = parse_layout(xml, 1).tree
                self.assertEqual(tree["root_tree_id"], root_tree_id)
                self.assertEqual(node(tree)["board"], board)

    def test_repeated_subtree_id_keeps_per_instance_paths(self):
        found = self.boards(parse_layout(REAL_SHAPED_XML, 1).tree)
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
                parsed = parse_layout(xml, 1)
                found = [board for board, _depth, _uid in self.boards(parsed.tree)]
                self.assertEqual(sorted(found), sorted(parsed.blackboard_names))
                self.assertEqual(len(found), len(set(found)))


if __name__ == "__main__":
    unittest.main()
