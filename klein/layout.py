"""klein.layout — a FULLTREE reply's XML, unrolled into the dashboard's tree.

Pure parsing with no gateway state: the node categories and port directions
from ``<TreeNodesModel>``, every ``<SubTree>`` reference stitched in place of
its ``<BehaviorTree>`` definition (``unroll_tree``), each node's uid, ports and
blackboard bindings, and the blackboard names to ask the robot for. The
gateway calls it once per handshake (and once per ``--open``) and keeps the
result.
"""

import re

from .groot2_protocol import (
    BUILTIN_CATEGORIES,
    CATEGORY_SUBTREE,
    CATEGORY_UNDEFINED,
    NODE_CATEGORIES,
)

# Structural attributes: klein renders these itself (name, ID) or uses them
# to wire the tree up (_uid, _fullpath). Everything else the robot stamped
# on the element is a port the tree author wrote — a Precondition's `if`, a
# RetryUntilSuccessful's `num_attempts`, a Switch's cases, a subtree's port
# remapping — and is what the node card shows. That includes the scripting
# hooks BT.CPP serializes out of a node's pre/post-conditions (`_skipIf`,
# `_while`, `_onSuccess`, …): underscored, but the author's writing.
STRUCTURAL_ATTRS = frozenset({"name", "ID", "uid", "_uid", "_fullpath"})


def collect_uids(node):
    """Every non-null node uid in an unrolled tree."""
    uids = [] if node.get("uid") is None else [node["uid"]]
    for child in node.get("children", []):
        uids.extend(collect_uids(child))
    return uids


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


def extract_ports(element):
    """Return the element's port attributes, in document order."""
    return {
        key: value
        for key, value in element.attrib.items()
        if key not in STRUCTURAL_ATTRS
    }


def _model_entries(root):
    """``(registration name, entry)`` for each ``<TreeNodesModel>`` entry with an ``ID``."""
    for model in root.findall("TreeNodesModel"):
        for entry in model:
            registration_id = entry.get("ID")
            if registration_id:
                yield registration_id, entry


def parse_node_categories(root):
    """Return ``{registration name: category}`` from ``<TreeNodesModel>``.

    An entry's tag is the category and its ``ID`` is the registration name
    instance elements use as their own tag — see docs/protocol.md. Entries
    with no ``ID``, and tags that are not categories (``<MetadataFields>``),
    are skipped rather than trusted.
    """
    return {registration_id: entry.tag
            for registration_id, entry in _model_entries(root)
            if entry.tag in NODE_CATEGORIES}


# --------------------------------------------------------------------------- #
# Blackboard bindings: which keys each node reads and writes, on which board
# --------------------------------------------------------------------------- #
# A <TreeNodesModel> port element's tag is its direction
# (xml_parsing.cpp :: addNodeModelToXML).
PORT_DIRECTIONS = {"input_port": "in", "output_port": "out", "inout_port": "inout"}

# The pre/post-condition hooks, run as scripts on the node's own board
# (tree_node.h :: PreCondNames, PostCondNames; bt_factory.cpp :: AssignConditions).
SCRIPT_HOOKS = frozenset({"_failureIf", "_successIf", "_skipIf", "_while",
                          "_onHalted", "_onFailure", "_onSuccess", "_post"})

# Builtin ports whose value is a script (script_node.h, script_condition.h,
# script_precondition.h :: loadExecutor -> ParseScript).
SCRIPT_PORTS = frozenset({("Script", "code"), ("ScriptCondition", "code"),
                          ("Precondition", "if")})

# Builtin ports whose plain value is a key *name*, not a literal: SetBlackboard
# writes `output_key` (set_blackboard_node.h :: tick), UnsetBlackboard removes
# `key` (unset_blackboard_node.h), and the entry-updated nodes watch `entry`
# ({key} or key: updated_action.cpp, updated_decorator.cpp).
KEY_NAME_PORTS = {
    ("SetBlackboard", "output_key"): "out",
    ("UnsetBlackboard", "key"): "out",
    ("WasEntryUpdated", "entry"): "in",
    ("SkipUnlessUpdated", "entry"): "in",
    ("WaitValueUpdate", "entry"): "in",
}

# A port with no model entry (a robot without <TreeNodesModel>): it may do
# either.
UNKNOWN_DIRECTION = "inout"

# BT.CPP's script tokens (script_tokenizer.cpp): 'strings', numbers (a number
# swallows trailing identifier characters as garbage), identifiers starting
# with a letter, `_` or `@`, the two-character operators, then anything else.
_SCRIPT_TOKEN = re.compile(r"""
      '[^']*'?
    | \d[\w.]*
    | (?P<name>[A-Za-z_@]\w*)
    | (?P<op>:=|\+=|-=|\*=|/=|==|!=|<=|>=|&&|\|\||\.\.|=)
    | \S
""", re.VERBOSE)
_ENUM_LIKE = re.compile(r"[A-Z][A-Z0-9_]+")    # registered enums are not keys


def parse_port_directions(root):
    """Return ``{registration name: {port: "in"|"out"|"inout"}}`` from
    ``<TreeNodesModel>`` (the ``input_port``/``output_port``/``inout_port``
    children that ``addNodeModelToXML`` writes)."""
    directions = {}
    for registration_id, entry in _model_entries(root):
        ports = directions.setdefault(registration_id, {})
        for port in entry:
            if port.tag in PORT_DIRECTIONS and port.get("name"):
                ports[port.get("name")] = PORT_DIRECTIONS[port.tag]
    return directions


def script_references(code):
    """Best-effort ``[(key, dir, start, end)]`` a script reads and writes, in
    order, with each name's offsets in ``code``.

    A name before ``:=`` or ``=`` is written; before ``+=`` and friends it is
    read and written; any other name is read. ``==`` compares. Strings,
    numbers, ``true``/``false`` and ALL_CAPS names (BT.CPP's convention for
    registered scripting enums, which shadow keys: operators.hpp :: ExprName)
    are not keys. No grammar: a malformed script still yields its names.
    """
    tokens = list(_SCRIPT_TOKEN.finditer(code))
    refs = []
    for i, m in enumerate(tokens):
        name = m.group("name")
        if not name or name in ("true", "false") or _ENUM_LIKE.fullmatch(name):
            continue
        following = tokens[i + 1].group("op") if i + 1 < len(tokens) else None
        if following in (":=", "="):
            direction = "out"
        elif following in ("+=", "-=", "*=", "/="):
            direction = "inout"
        else:
            direction = "in"
        refs.append((name, direction, m.start("name"), m.end("name")))
    return refs


def blackboard_pointer(value):
    """The key inside ``{key}`` (outer spaces allowed), else None
    (tree_node.cpp :: isBlackboardPointer)."""
    value = value.strip(" ")
    if len(value) >= 3 and value[0] == "{" and value[-1] == "}":
        return value[1:-1]
    return None


def _trimmed(value, chars=None):
    """``(start, end)`` of ``value`` without its outer ``chars`` (whitespace)."""
    return len(value) - len(value.lstrip(chars)), len(value.rstrip(chars))


def _merge_direction(a, b):
    return a if a == b else "inout"


def _merge_into(directions, key, direction):
    """Record ``direction`` for ``key``, merged with any direction already there."""
    prev = directions.get(key)
    directions[key] = direction if prev is None else _merge_direction(prev, direction)


class _Scope:
    """One blackboard as BT.CPP builds it for a subtree instance
    (xml_parsing.cpp :: recursivelyCreateSubtree): its name, its parent, the explicit remaps
    ``{internal: external}``, the ports set to a literal, and ``_autoremap``."""

    def __init__(self, board, parent=None, remap=None, literals=(), autoremap=False):
        self.board = board
        self.parent = parent
        self.remap = remap or {}
        self.literals = frozenset(literals)
        self.autoremap = autoremap

    def resolve(self, key):
        """``(board, key)`` of the entry ``key`` reaches from this board,
        following Blackboard::getEntry."""
        scope = self
        while True:
            if key.startswith("@"):                     # always the root board
                while scope.parent is not None:
                    scope = scope.parent
                return scope.board, key[1:]
            if scope.parent is None or key in scope.literals:
                return scope.board, key                 # stored locally
            if key in scope.remap:
                scope, key = scope.parent, scope.remap[key]
            elif scope.autoremap and not key.startswith("_"):
                scope = scope.parent                    # IsPrivateKey keys stay
            else:
                return scope.board, key


class _Bindings:
    """Accumulates one node's bindings, one per (port, board, key), with the
    directions of repeated references merged (``x := x + 1`` is inout) and
    the spans of the port's value that name the key."""

    def __init__(self):
        self.by_ref = {}
        self.spans = {}

    def add(self, port, direction, board_key, span):
        ref = (port, *board_key)
        _merge_into(self.by_ref, ref, direction)
        self.spans.setdefault(ref, set()).add(span)

    def merge_boards_into(self, directions):
        """Merge these bindings' directions into ``{(board, key): dir}``."""
        for (_port, board, key), direction in self.by_ref.items():
            _merge_into(directions, (board, key), direction)

    def as_list(self):
        return [{"port": port, "dir": direction, "board": board, "key": key,
                 "at": [list(span) for span in sorted(self.spans[port, board, key])]}
                for (port, board, key), direction in self.by_ref.items()]


def _category_for(element, node_categories):
    """Return one instance element's category, most authoritative source first:
    a tag that is itself a category (the explicit ``<Action ID="OpenDoor"/>``
    spelling), then the robot's ``<TreeNodesModel>`` (``node_categories``),
    then the nodes BehaviorTree.CPP registers on itself, else ``Undefined``.

    Never guessed from the tree's shape — see docs/protocol.md.
    """
    tag = element.tag
    if tag in NODE_CATEGORIES:
        return tag
    return (node_categories.get(tag)
            or BUILTIN_CATEGORIES.get(tag)
            or CATEGORY_UNDEFINED)


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


class _Unroller:
    """One unrolling of one tree: the ``<BehaviorTree>`` blocks to stitch in,
    the node categories and port directions, and the node counter behind the ids."""

    def __init__(self, all_behavior_trees, node_categories, port_directions, generation):
        self.all_behavior_trees = all_behavior_trees    # tree_id -> root <element> of that block
        self.node_categories = node_categories          # registration name -> category
        self.port_directions = port_directions          # registration name -> {port: dir}
        self.generation = generation                    # prefixes node ids
        self.node_seq = 0                               # per-tree node counter (uid may be null)

    def _next_id(self):
        """A node id unique across handshakes, not just within one tree.

        ``node_seq`` restarts at 1 per tree (it doubles as the unrolled node
        count), so the generation prefix is what keeps two trees' id sets
        disjoint for the dashboard's keyed join — see docs/architecture.md.
        """
        self.node_seq += 1
        return f"{self.generation}:{self.node_seq}"

    def _bindings(self, registration_id, ports, scope, bindings):
        """Add a (non-SubTree) node's port bindings, resolved from ``scope``."""
        model = self.port_directions.get(registration_id, {})
        for port, value in ports.items():
            pointer = blackboard_pointer(value)
            if pointer is None and (port in SCRIPT_HOOKS
                                    or (registration_id, port) in SCRIPT_PORTS):
                for key, direction, start, end in script_references(value):
                    bindings.add(port, direction, scope.resolve(key), (start, end))
                continue
            if value in ("{=}", "="):                   # tree_node.cpp :: getRemappedKey
                key, span = port, (0, len(value))
            elif pointer is not None:
                key, span = pointer, _trimmed(value, " ")
            elif (registration_id, port) in KEY_NAME_PORTS and value.strip():
                bindings.add(port, KEY_NAME_PORTS[registration_id, port],
                             scope.resolve(value.strip()), _trimmed(value))
                continue
            else:
                continue                                # a literal: no entry
            bindings.add(port, model.get(port, UNKNOWN_DIRECTION), scope.resolve(key), span)

    def unroll_node(self, element, scope, beneath, expanding=frozenset()):
        """Recursively convert a layout element into a nested dict.

        ``<SubTree ID="X">`` references are stitched in place: the SubTree node
        keeps its own UID and gains the matching ``<BehaviorTree>`` definition
        as its child, so *every* UID — the reference and all inner nodes — maps
        cleanly onto incoming status packets. ``expanding`` guards against
        cyclic subtree references. ``scope`` is the blackboard the element's
        ports are read against. ``beneath`` collects ``{(board, key): dir}``
        merged over this node's bindings and all its descendants'.
        """
        node_type = element.tag
        ports = extract_ports(element)
        bindings = _Bindings()

        if node_type == "SubTree":
            subtree_id = element.get("ID")
            board = element.get("_fullpath") or subtree_id
            child_scope = self._subtree_scope(board, ports, scope)
            # The SubTree node itself sits on the parent's board: its hooks run
            # there, and each remap names a parent key.
            hooks = {p: v for p, v in ports.items() if p in SCRIPT_HOOKS}
            self._bindings("SubTree", hooks, scope, bindings)
            node = {
                "id": self._next_id(),
                "uid": extract_uid(element),
                "type": "SubTree",
                "category": CATEGORY_SUBTREE,
                "name": element.get("name") or subtree_id or "SubTree",
                "subtree_id": subtree_id,
                "is_subtree_root": True,
                # This instance's blackboard, named exactly as
                # extract_blackboard_names asks the robot for it — the dashboard
                # pairs each board with the node that owns it.
                "board": board,
                "ports": ports,
                "children": [],
            }
            inner = {}
            subtree_root = self.all_behavior_trees.get(subtree_id)
            if subtree_root is not None and subtree_id not in expanding:
                node["children"] = [self.unroll_node(
                    subtree_root, child_scope, inner, expanding | {subtree_id})]
            # A remap's direction is what the subtree does with it: the merged
            # directions of the inner bindings that land on the same entry.
            for port, key in child_scope.remap.items():
                target = scope.resolve(key)
                bindings.add(port, inner.get(target, UNKNOWN_DIRECTION), target,
                             _trimmed(ports[port], " "))
            bindings.merge_boards_into(beneath)
            for board_key, direction in inner.items():
                _merge_into(beneath, board_key, direction)
            node["bindings"] = bindings.as_list()
            return node

        registration_id = element.get("ID") if node_type in NODE_CATEGORIES else node_type
        self._bindings(registration_id, ports, scope, bindings)
        bindings.merge_boards_into(beneath)
        return {
            "id": self._next_id(),
            "uid": extract_uid(element),
            "type": node_type,
            "category": _category_for(element, self.node_categories),
            "name": element.get("name") or node_type,
            "ports": ports,
            "bindings": bindings.as_list(),
            "children": [self.unroll_node(child, scope, beneath, expanding)
                         for child in element],
        }

    @staticmethod
    def _subtree_scope(board, ports, parent):
        """The subtree instance's blackboard, as recursivelyCreateSubtree sets
        it up: ``{=}`` is ``{port}``; ``_autoremap`` is a bool; a ``{key}``
        value remaps to the parent's key, anything else is a literal stored on
        the subtree's own board. Hooks and other non-port names (IsAllowedPortName) are skipped.
        FULLTREE never carries ``_autoremap``, see docs/architecture.md."""
        remap, literals, autoremap = {}, [], False
        for port, value in ports.items():
            if value == "{=}":
                value = "{" + port + "}"
            if port == "_autoremap":
                autoremap = value in ("1", "true", "TRUE", "True")
                continue
            if not port[:1].isalpha():
                continue
            key = blackboard_pointer(value)
            if key is None:
                literals.append(port)
            else:
                remap[port] = key
        return _Scope(board, parent, remap, literals, autoremap)


def unroll_tree(root, node_categories, generation):
    """Build the unrolled tree structure from a parsed FULLTREE ``root``.

    ``node_categories`` is ``parse_node_categories(root)``; ``generation``
    prefixes every node id. Returns ``(tree, node_count)``. Raises
    ``ValueError`` when the XML has no usable ``<BehaviorTree>`` block.
    """
    all_behavior_trees = {}
    block_paths = {}            # tree ID -> that block's _fullpath, for the root's board
    first_tree_id = None
    for bt_block in root.findall(".//BehaviorTree"):
        tree_id = bt_block.get("ID")
        if not tree_id:
            continue
        children = list(bt_block)
        all_behavior_trees[tree_id] = children[0] if children else None
        block_paths[tree_id] = bt_block.get("_fullpath")
        if first_tree_id is None:
            first_tree_id = tree_id

    # Prefer an explicit entrypoint if the XML declares one; otherwise the
    # first <BehaviorTree> block is the main tree.
    main_tree_id = root.get("main_tree_to_execute") or first_tree_id
    if main_tree_id not in all_behavior_trees:
        main_tree_id = first_tree_id

    if main_tree_id is None or all_behavior_trees.get(main_tree_id) is None:
        raise ValueError("layout XML contains no usable <BehaviorTree> block")

    # The root's own blackboard, taken from *its* block rather than the first
    # one — main_tree_to_execute need not point at the first <BehaviorTree>.
    # Real robots leave the root's _fullpath empty, so this falls through to
    # the tree ID, exactly as extract_blackboard_names does.
    root_board = block_paths.get(main_tree_id) or main_tree_id
    unroller = _Unroller(all_behavior_trees, node_categories,
                         parse_port_directions(root), generation)
    tree = unroller.unroll_node(all_behavior_trees[main_tree_id], _Scope(root_board), {})
    tree["root_tree_id"] = main_tree_id
    tree["board"] = root_board
    return tree, unroller.node_seq
