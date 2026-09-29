# The Groot2 publisher wire protocol

This is the protocol klein uses to talk to a robot. It's the Groot2 publisher
protocol from BehaviorTree.CPP v4 (`Groot2Publisher`), the same one the Groot2
editor uses. Everything here was checked against the BehaviorTree.CPP source.
File references point there.

## Ports

`Groot2Publisher` takes one port (default `1667`) and binds two ZeroMQ sockets
(`src/loggers/groot2_publisher.cpp`):

| socket | port | direction | purpose |
| --- | --- | --- | --- |
| `ZMQ_REP` | `port` | client asks, robot replies | everything: tree, status, blackboards, hooks |
| `ZMQ_PUB` | `port + 1` | robot pushes | only "breakpoint reached" |

The second port is always `port + 1` and can't be set separately. That's why
two publishers can't use neighbouring ports (Nav2 uses 1667 and 1669).

klein only connects to the REP socket. The PUB socket is only for Groot2's
breakpoint debugger, which klein doesn't implement. For a firewall, opening the
REP port is enough.

> Guides that mention `groot_zmq_publisher_port` / `groot_zmq_server_port`
> (often 5555/5556) describe Groot v1 on BehaviorTree.CPP v3. That protocol was
> removed in v4, and klein doesn't speak it.

## Request framing

A request is a ZeroMQ multipart message. Frame 0 is a 6-byte little-endian
`<BBI` header (`groot2_protocol.h :: RequestHeader`):

| field | size | value |
| --- | --- | --- |
| `protocol_id` | u8 | `2` |
| `request_type` | u8 | an ASCII letter, see below |
| `unique_id` | u32 | echo token chosen by the client |

Some requests put an argument in frame 1 (BLACKBOARD does).

A reply has two frames. Frame 0 is a 22-byte header: the 6-byte request header
echoed back, then a 16-byte tree UUID (`groot2_protocol.h :: ReplyHeader`).
Frame 1 is the payload. A malformed request gets `[b"error", <message>]`.

#### The tree UUID

The UUID is 16 raw bytes, not a hex string. `Groot2Publisher` draws it once at
the start of `serverLoop()` and puts it on every reply. The tree XML is captured
in the constructor, so a different UUID means a different publisher, and so a
different tree. It's the only sign a client gets that the tree changed. A
ZeroMQ `REQ` socket reconnects by itself, so a robot restart may not even cause
a timeout.

Two things to keep in mind:

- The UUID identifies the publisher, not the tree's content. Restarting a robot
  on the same tree also changes it.
- It tells you the tree is different, not what changed.

What klein does with it is in [architecture.md](architecture.md#tree-swaps).

## Request types

From `groot2_protocol.h :: RequestType`:

| type | letter | reply payload | used by klein |
| --- | --- | --- | --- |
| FULLTREE | `T` | tree XML (UTF-8) | ✅ on connect and on tree change |
| STATUS | `S` | packed status records | ✅ every 100 ms (10 Hz) |
| BLACKBOARD | `B` | msgpack board dump | ✅ every 500 ms (2 Hz) |
| HOOK_INSERT / HOOK_REMOVE | `I` / `R` | — | ✖ breakpoint debugging |
| BREAKPOINT_UNLOCK | `U` | — | ✖ breakpoint debugging |
| HOOKS_DUMP / REMOVE_ALL_HOOKS / DISABLE_ALL_HOOKS | `D` / `A` / `X` | — | ✖ breakpoint debugging |
| TOGGLE_RECORDING / GET_TRANSITIONS | `r` / `t` | — | ✖ transition recording |
| BREAKPOINT_REACHED | `N` | *pushed on the PUB socket* | ✖ |

The unused requests are Groot2's debugging features. They don't carry extra
data a client would miss.

### FULLTREE (`T`)

Returns the full tree as XML:

- One `<BehaviorTree ID="...">` block per (sub)tree definition. The `<root>`
  element may name `main_tree_to_execute`; otherwise the first block is the
  entry point.
- Every node has a `_uid`, the runtime ID used in STATUS records. On a
  `<SubTree>`, `ID` is the name of the referenced tree, not a UID.
- `<BehaviorTree>` blocks and `<SubTree>` references have `_fullpath`, the
  subtree instance path. Its blackboard is registered under that name. The root's
  `_fullpath` is empty, and its board uses the tree ID.
- A `<TreeNodesModel>` section declares the node types. These are models, not
  instances (its `<SubTree>` entry is not a real subtree), so skip it when
  walking the tree. It is the source of each node's category.

#### Node categories

In `<TreeNodesModel>`, each entry's tag is the category and its `ID` is the
registration name. Instances use that registration name as their tag:

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

So `{ID -> tag}` maps a registration name to its category directly.
`Groot2Publisher` always includes this section, builtins included
(`WriteTreeToXML(tree, true, true)`).

Categories are `basic_types.h :: NodeType`:

| tag | what it is |
| --- | --- |
| `Control` | many children: sequences, fallbacks, parallels, switches |
| `Decorator` | one child: retries, timeouts, inverters, preconditions |
| `Condition` | a leaf that checks something and returns quickly |
| `Action` | a leaf that does work |
| `SubTree` | a reference to another `<BehaviorTree>` block |

FULLTREE always writes instances as `<OpenDoor name="OpenDoor" _uid="9"/>`
(`addTreeToXML` in `src/xml_parsing.cpp`). The Groot2 editor saves files in the
other style, `<Action ID="OpenDoor"/>`. klein reads both.

If the model section is missing or lacks an entry, klein falls back to the nodes
BT.CPP registers itself (`src/bt_factory.cpp`). That covers every builtin
Control and Decorator. Anything else is `Undefined`. klein doesn't guess from
the tree's shape, because a leaf can be either an Action or a Condition.

### STATUS (`S`)

The payload is a list of 3-byte little-endian `<HB` records: `node_uid` (u16)
then a status byte (`basic_types.h :: NodeStatus`):

| value | meaning |
| --- | --- |
| 0–4 | IDLE, RUNNING, SUCCESS, FAILURE, SKIPPED |
| ≥ 10 | just became IDLE, coming from status `value − 10` |

The `+10` form lets a poller see how a node's last run ended, even though it
only sees snapshots.

### BLACKBOARD (`B`)

Frame 1 of the request is the board names to dump, joined with `;`. These are
the `_fullpath` names from FULLTREE, or the tree ID for the root. The reply is
msgpack: `board name → {key → JSON-encoded value}`.

Things to know:

- The reply is msgpack nil if no requested name matched. Nil is also used for a
  subtree whose ports are all remapped to its parent, because it stores nothing
  itself.
- Keys starting with `_` are private in BT.CPP. klein hides them.
- Only types with a JSON converter (`BT::RegisterJsonDefinition<T>()`) show up.
  A type without one is missing, which looks the same as never written.
- Order is arbitrary, because the robot walks an unordered map.
- The reply can include boards you didn't ask for. Real BT.CPP robots have been
  seen adding the root board to every dump under the name `ROOT`, duplicating
  the tree ID's entries. (Asking for `ROOT` directly matches nothing.) klein
  keeps only the names it requested; see
  [architecture.md](architecture.md#subtree-unrolling).

## Where the constants live

klein encodes all of this in
[`klein/groot2_protocol.py`](../klein/groot2_protocol.py), used by both the
gateway and the mock robot. On the C++ side, see
`include/behaviortree_cpp/loggers/groot2_protocol.h` and
`src/loggers/groot2_publisher.cpp`.
