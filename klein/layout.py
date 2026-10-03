"""klein.layout — a FULLTREE reply's XML, unrolled into the dashboard's tree.

Pure parsing with no gateway state: the node categories from
``<TreeNodesModel>``, every ``<SubTree>`` reference stitched in place of its
``<BehaviorTree>`` definition (``unroll_tree``), each node's uid and ports, and
the blackboard names to ask the robot for. The gateway calls it once per
handshake (and once per ``--open``) and keeps the result.
"""

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
    the node categories, and the node counter behind the ids."""

    def __init__(self, all_behavior_trees, node_categories, generation):
        self.all_behavior_trees = all_behavior_trees    # tree_id -> root <element> of that block
        self.node_categories = node_categories          # registration name -> category
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
                "uid": extract_uid(element),
                "type": "SubTree",
                "category": CATEGORY_SUBTREE,
                "name": element.get("name") or subtree_id or "SubTree",
                "subtree_id": subtree_id,
                "is_subtree_root": True,
                # This instance's blackboard, named exactly as
                # extract_blackboard_names asks the robot for it — the dashboard
                # pairs each board with the node that owns it.
                "board": element.get("_fullpath") or subtree_id,
                "ports": extract_ports(element),
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
            "uid": extract_uid(element),
            "type": node_type,
            "category": _category_for(element, self.node_categories),
            "name": element.get("name") or node_type,
            "ports": extract_ports(element),
            "children": [self.unroll_node(child, expanding) for child in element],
        }


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

    unroller = _Unroller(all_behavior_trees, node_categories, generation)
    tree = unroller.unroll_node(all_behavior_trees[main_tree_id])
    tree["root_tree_id"] = main_tree_id
    # The root's own blackboard, taken from *its* block rather than the first
    # one — main_tree_to_execute need not point at the first <BehaviorTree>.
    # Real robots leave the root's _fullpath empty, so this falls through to
    # the tree ID, exactly as extract_blackboard_names does.
    tree["board"] = block_paths.get(main_tree_id) or main_tree_id
    return tree, unroller.node_seq
