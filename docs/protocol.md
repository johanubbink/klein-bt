# The Groot2 publisher wire protocol

This documents the protocol klein speaks to a robot: the Groot2 publisher
protocol that ships with **BehaviorTree.CPP v4** (`Groot2Publisher`). It is the
same protocol the Groot2 editor uses. Everything here was verified against the
BehaviorTree.CPP sources — file references point there — and klein's Python
encoding of it lives in [`klein/groot2_protocol.py`](../klein/groot2_protocol.py).

## Transport and ports

`Groot2Publisher` is constructed with a **single port** (default `1667`) and
binds **two** ZeroMQ sockets (`src/loggers/groot2_publisher.cpp`):

| socket | port | direction | purpose |
| --- | --- | --- | --- |
| `ZMQ_REP` | `port` | client asks, robot replies | everything: tree, status, blackboards, hooks |
| `ZMQ_PUB` | `port + 1` | robot pushes | one message only: "breakpoint reached" |

The second port is always derived — it cannot be configured independently, and
the port you pick must leave `port + 1` free (two publishers cannot sit on
consecutive ports; Nav2 gives its two navigators 1667 and 1669 for exactly this
reason). Groot2's UI likewise asks for a single port and connects its
subscriber to `port + 1` silently.

klein connects only to the REP socket (`--robot-port`). The PUB socket carries
nothing but breakpoint notifications for Groot2's interactive debugger, a
feature klein does not implement, so klein never opens it. If you firewall for
klein only, the one REP port suffices.

> **Old two-port configs.** Guides mentioning `groot_zmq_publisher_port` /
> `groot_zmq_server_port` (often 5555/5556) describe the **old Groot v1**
> protocol of BehaviorTree.CPP **v3**, whose `PublisherZMQ(tree,
> max_msg_per_second, publisher_port = 1666, server_port = 1667)` really did
> take two independent ports — a PUB stream of transitions plus a REP server
> for the tree. That protocol is gone in v4; klein does not speak it.

## Request framing

Requests are ZeroMQ multipart messages on the REQ socket. Frame 0 is a 6-byte
header, little-endian `<BBI` (`groot2_protocol.h :: RequestHeader`):

| field | size | value |
| --- | --- | --- |
| `protocol_id` | u8 | `2` |
| `request_type` | u8 | an ASCII letter, see below |
| `unique_id` | u32 | echo token chosen by the client |

Some requests carry an argument as frame 1 (BLACKBOARD does; see below).

Replies are two frames. Frame 0 is a 22-byte reply header — the request's
6-byte header echoed back, followed by a 16-byte tree UUID
(`groot2_protocol.h :: ReplyHeader`). Frame 1 is the payload. On a malformed
request the publisher replies `[b"error", <message>]` instead.

## Request types

The full vocabulary, from `groot2_protocol.h :: RequestType`:

| type | letter | payload of the reply | klein |
| --- | --- | --- | --- |
| FULLTREE | `T` | tree XML (UTF-8) | ✅ once per handshake |
| STATUS | `S` | packed status records | ✅ polled at 10 Hz |
| BLACKBOARD | `B` | msgpack board dump | ✅ polled at 2 Hz |
| HOOK_INSERT / HOOK_REMOVE | `I` / `R` | — | ✖ breakpoint debugging |
| BREAKPOINT_UNLOCK | `U` | — | ✖ breakpoint debugging |
| HOOKS_DUMP / REMOVE_ALL_HOOKS / DISABLE_ALL_HOOKS | `D` / `A` / `X` | — | ✖ breakpoint debugging |
| TOGGLE_RECORDING / GET_TRANSITIONS | `r` / `t` | — | ✖ transition recording |
| BREAKPOINT_REACHED | `N` | *pushed on the PUB socket* | ✖ |

Because the protocol is strict request/reply, there is no telemetry a passive
client can miss by not subscribing: the hooks and recording requests are
Groot2's interactive debugging features, not extra data.

### FULLTREE (`T`)

Returns the composed tree as XML. Relevant structure:

- One `<BehaviorTree ID="...">` block per (sub)tree definition; the root
  `<root>` element may carry `main_tree_to_execute`, otherwise the first block
  is the entry point.
- Every node element carries `_uid` — the runtime UID that keys the STATUS
  records. `ID` on a `<SubTree>` is the referenced tree's *name*, not a UID.
- `<BehaviorTree>` blocks and `<SubTree>` references carry `_fullpath`, the
  subtree *instance* path — this is the name its blackboard registers under
  (the root's `_fullpath` is empty; its board registers under the tree ID).
- A `<TreeNodesModel>` section declares the node types. Its entries are
  **models, not instances** — the `<SubTree>` entry there is a declaration, not
  a subtree with a blackboard — so it must be skipped when walking the tree. It
  is, however, the authoritative source for a node's **category**.

#### Node categories

Each `<TreeNodesModel>` entry's element *tag* is the node's category and its
`ID` attribute is the registration name — which is the same string instance
elements use as *their* tag:

```xml
<TreeNodesModel>
  <Control ID="Fallback"/>
  <Condition ID="IsDoorClosed"/>
  <Decorator ID="RetryUntilSuccessful">
    <input_port name="num_attempts" type="int">Repeat a failed child up to N times</input_port>
  </Decorator>
  <Action ID="OpenDoor"/>
</TreeNodesModel>
```

so `{ID -> tag}` is an exact registration-name-to-category lookup, with nothing
inferred. `Groot2Publisher` builds its reply with
`WriteTreeToXML(tree, /*add_metadata=*/true, /*add_builtin_models=*/true)`, so
the section is always present and always covers the builtins.

Categories are `basic_types.h :: NodeType`, spelled as `toStr<NodeType>()`
writes them:

| tag | what it is |
| --- | --- |
| `Control` | many children; sequences, fallbacks, parallels, switches |
| `Decorator` | exactly one child; retries, timeouts, inverters, preconditions |
| `Condition` | a leaf that answers a question and never runs long |
| `Action` | a leaf that does work |
| `SubTree` | a reference to another `<BehaviorTree>` block |

A publisher always writes an instance with its registration name as the tag
(`<OpenDoor name="OpenDoor" _uid="9"/>`; a `<SubTree>` gets `ID` instead of
`name`) — `addTreeToXML` in `src/xml_parsing.cpp` has no other branch. The
explicit spelling that puts the category in the tag (`<Action ID="OpenDoor"/>`)
is what the Groot2 *editor* saves to a file, not something FULLTREE returns;
klein reads it anyway, so a hand-written or exported tree still categorises.

A robot that predates the model section, or one whose reply omits an entry,
leaves klein without an answer. It then falls back to the table of nodes
BehaviorTree.CPP registers on itself (`src/bt_factory.cpp`) — which covers every
builtin Control and Decorator, so the tree's control skeleton still reads
correctly — and reports anything still unresolved as `Undefined`. Nothing is
inferred from the tree's shape: a `Sequence` with one child is still a Control,
and a childless node may be an Action or a Condition (BT.CPP's own CrossDoor
example registers `SmashDoor` as a Condition and the equally childless
`OpenDoor` as an Action).

### STATUS (`S`)

The payload is a sequence of fixed 3-byte records, little-endian `<HB`:
`node_uid` (u16) followed by a status byte. Status values come from
`basic_types.h :: NodeStatus`:

| value | meaning |
| --- | --- |
| 0–4 | IDLE, RUNNING, SUCCESS, FAILURE, SKIPPED |
| ≥ 10 | node just became IDLE; it transitioned from status `value − 10` |

The `+10` encoding lets a poller that only sees snapshots still learn how a
node's last activation ended.

### BLACKBOARD (`B`)

The request carries frame 1: the board names to dump, joined with `;`
(these are the `_fullpath` instance names from FULLTREE, or the tree ID for
the root). The reply payload is msgpack: a map of
`board name → {key → JSON-encoded value}`.

Semantics to know:

- msgpack **nil** replaces the map when no requested name matched a live
  subtree — and also stands in for a subtree whose every port is remapped to
  its parent (it owns no storage of its own).
- Keys starting with `_` are private by BehaviorTree.CPP's own convention:
  autoremapping skips them, so a subtree keeps them to itself. klein drops
  them rather than showing them as user values.
- Values appear only for types with a registered JSON converter
  (`BT::RegisterJsonDefinition<T>()`); a type without one is silently absent,
  indistinguishable from "never written".
- The robot walks an unordered map, so reply order is arbitrary.

## Where the constants live

klein encodes all of this once, in
[`klein/groot2_protocol.py`](../klein/groot2_protocol.py) — the request framing,
the `NodeStatus` decode rule, the `NodeType` category names and the
builtin-category fallback table. The gateway decodes with it and the mock robot
([`klein/mock_robot.py`](../klein/mock_robot.py)) encodes with it, so the two
halves cannot drift. The C++ side is
`include/behaviortree_cpp/loggers/groot2_protocol.h` and
`src/loggers/groot2_publisher.cpp` in BehaviorTree.CPP.
