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
| TOGGLE_RECORDING / GET_TRANSITIONS | `r` / `t` | wall-clock time / packed transitions | ✅ `r start` on every handshake, `t` after every STATUS |
| BREAKPOINT_REACHED | `N` | *pushed on the PUB socket* | ✖ |

The hook requests are Groot2's breakpoint debugger, which klein doesn't use.
Transition recording carries what a poller misses; see below.

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

### Transition recording (`r` / `t`)

STATUS is a snapshot, so a node that starts and finishes between two polls
never shows up in one. Recording catches those. While it is on, the
publisher's status-change callback appends every transition to a buffer, and
GET_TRANSITIONS drains it (`groot2_publisher.cpp :: callback`, `serverLoop`).

TOGGLE_RECORDING takes `start` or `stop` in frame 1. Without frame 1 it is an
error (`must be 2 parts message`).

- `start` turns recording on, clears the buffer and notes the start time. The
  reply payload is the robot's wall-clock time as a decimal string of
  microseconds since the epoch. That's the anchor for the timestamps below.
- `stop` turns recording off. The reply has no payload frame. Any other word
  does nothing and also replies with the header alone.

GET_TRANSITIONS replies with a list of 9-byte little-endian records:

| field | size | value |
| --- | --- | --- |
| `timestamp_usec` | u48 | microseconds since `start` |
| `node_uid` | u16 | as in STATUS |
| `status` | u8 | the new `NodeStatus`, 0–4 |

Things to know:

- The buffer is emptied by every GET_TRANSITIONS, so only one client can drain
  it. A second client gets whatever the first left over.
- It holds at most 1000 transitions and silently drops the oldest beyond that.
  A drain of exactly 1000 records probably lost some.
- IDLE is a plain `0` here, not the `+10` form STATUS uses.
- Recording ignores the heartbeat. A client that disappears leaves it on.
  That costs the robot nothing more than the capped buffer, so klein never
  sends `stop`.
- A new publisher (new tree UUID) starts with recording off.
- Recording arrived in BehaviorTree.CPP 4.3.3. Older publishers answer `r`
  with an error frame.

#### How klein records

klein records by default (`--record-buffer 0` turns it off). This is the wire
side; what klein keeps is in [architecture.md](architecture.md#recording).
Per publisher:

1. **Arming**, after every successful handshake (the first, a tree swap, and
   telemetry resuming after an outage, even when the XML is unchanged): `T`
   when needed, then `r start`, then `S`. That `S` buffer is the new
   segment's **baseline** state; it isn't sent to dashboards. `r start`
   clears the publisher's buffer, so a publisher another client left
   recording starts clean. In this order no transition is lost: what runs
   after `r start` is in the buffer, and what ran before the `S` is also in
   the baseline, so the first drain replays it on top of the baseline.
   klein's model ignores an IDLE on an already idle node, which a robot
   never sends, so the replay ends on the baseline's bytes.
2. **Each 10 Hz poll**: `S`, then `t`. On the wire that's `T r S S t S t …`,
   with the 2 Hz `B` interleaved.
3. **Time base**: a record's absolute time is the `r start` reply plus its
   offset, so klein's times are on the robot's own clock and equal its
   FileLogger2's (to the µs it took between the two clock reads). Blackboard
   samples, which carry no robot time, are stamped by mapping klein's clock
   onto the robot's, taking the `r start` reply as the midpoint of its round
   trip.
4. **Overflow**: a drain of exactly 1000 records means the publisher dropped
   some. klein keeps the drain and re-arms (`r start`, `S` baseline), so the
   next segment starts from a fresh snapshot rather than a patched one.
5. **No recording support** (`r` answers with an error): klein sends no `t`
   to that publisher. It tries again on the next handshake.

**Only one client can drain a publisher.** Every `t` empties the buffer, so
if Groot2's logging (or a second klein) runs against the same robot, each
gets only some of the transitions. Don't log from Groot2 while klein is
recording, or start klein with `--record-buffer 0`.

## Recorded files (`.btlog`)

Not part of the wire protocol, but made of the same records: the file
BehaviorTree.CPP's `FileLogger2` writes and Groot2 opens and saves. Layout,
all little-endian (`bt_file_logger_v2.cpp`, constructor and `writerLoop`):

| field | size | value |
| --- | --- | --- |
| magic | 18 bytes | `BTCPP4-FileLogger2` |
| version | u8 | `1` |
| `xml_len` | i32 | length of the next field, in bytes |
| xml | `xml_len` | the tree XML, UTF-8, as in a FULLTREE reply |
| `first_timestamp` | i64 | microseconds since the epoch |
| records | 9 bytes each | `timestamp_usec` (u48, µs since `first_timestamp`) \| `node_uid` (u16) \| `status` (u8) |

The header comment in `bt_file_logger_v2.h` leaves out the magic and the
version byte; the `.cpp` is what gets written. Each record is exactly a
GET_TRANSITIONS record, IDLE a plain `0`. A robot stopped mid-write can leave
a partial last record; readers ignore those bytes.

klein reads and writes these in [`klein/btlog.py`](../klein/btlog.py)
(`read_btlog`, `write_btlog`), which the mock's `--replay` and `klein-bt
--open` use too.

### Saving a recording

A `.btlog` holds one tree XML, so klein saves **one file per tree run**: the
consecutive segments that share a layout (`Recording.runs()`). A same-XML
restart, an outage resume and an overflow re-arm stay in their run; a tree
swap starts a new one. `export_run(run, xml)` writes a run:

- `first_timestamp` is the run's **retained start**: its first segment's
  `t_start` (`t_begin`, or later once old chunks were evicted).
- Groot2 and klein both start a file from all-IDLE. So the records begin with
  a **prefix** at offset 0: the transitions (`diff_to_transitions`) from
  all-IDLE to the state at the retained start.
- Then every retained record of the run's segments, at its offset from
  `first_timestamp`. At each segment boundary inside the run, the next
  segment's records are preceded, at its `t_start`, by the transitions from
  the previous segment's last state to the next one's starting state (its
  fresh baseline). A record that changes nothing in klein's model (an IDLE
  on an already idle node: mostly the arm overlap, rarely a real IDLE whose
  RUNNING was lost to an overflow) is left out, so a reader with the plain
  publisher rule never shows "was IDLE".
- Replayed from all-IDLE, the file therefore shows the recorded state at every
  retained time. Where the recording has a 0 (a node that never ran), the
  replay may show "IDLE, was X"; the two look the same. During an outage gap
  the file holds the last state before it.

The blackboard isn't part of a `.btlog` (Groot2 doesn't log it), so
`export_blackboard(run, tree_id)` writes a **sidecar** `<name>.bb.jsonl`, one
JSON object per line:

```
{"klein_blackboard": 1, "first_timestamp": 1760000000000000, "tree_id": "MainTree"}
{"t": 0, "board": "MainTree", "key": "door_open", "value": false}
{"t": 1250000, "board": "MainTree", "key": "door_open", "value": true}
{"t": 2400000, "board": "DoorClosed::7", "key": "attempts", "removed": true}
```

- The header line's `first_timestamp` is the `.btlog`'s.
- Each later line is one change, in time order: `t` is the µs offset from
  `first_timestamp`, `value` the key's JSON value, or `"removed": true`.
- Each segment opens with lines that turn the previous values (none, for the
  first) into its values at its retained start, at that offset: every key's
  value as of the run's start is at offset 0, so the file stands on its own
  after eviction. A segment whose blackboard was sampled only later opens at
  its first sample instead.
- A key's value at time `t` is its latest line at or before `t`. A board with
  no keys has no lines.

The gateway serves both files, and all runs in one `.zip`, as downloads; see
[architecture.md](architecture.md#browser-side-one-port).

### Opening a file

`klein-bt --open FILE.btlog` reads a file back (`load_btlog(log, layout,
boards, sidecar)` in [`klein/btlog.py`](../klein/btlog.py)) into the same
`Recording` the gateway records into
([architecture.md](architecture.md#opening-a-file)):

- One segment, `t_begin` = `first_timestamp`, starting from all-IDLE (as
  Groot2 starts a file), with every whole record at `first_timestamp +
  offset`, in file order. A partial last record is ignored. The segment ends,
  and the head is, at the last record's time (`first_timestamp` if there is
  none). No gaps; nothing is evicted.
- The sidecar is `FILE.bb.jsonl` beside the file (`Path.with_suffix`), if it
  exists. Its lines are replayed into samples at `first_timestamp + t` (the
  header's `first_timestamp`), one per distinct `t`, each holding every key's
  latest value and **every board of the layout**
  (`layout.extract_blackboard_names`), so a board with no lines is shown empty, as
  the robot reports it live. The first sample is at the first line's `t`, or
  at offset 0 for a sidecar with only its header. Without a sidecar the
  recording has no blackboard.
- Errors: no `BTCPP4-FileLogger2` magic, a version byte other than `1`, an
  `xml_len` past the end, XML with no usable `<BehaviorTree>`, a record whose
  uid the tree doesn't have, or a sidecar that isn't JSON lines with a header
  and `t`/`board`/`key` fields.
- `--open` doesn't read a `.zip`; unzip it first. Each `.btlog` and its
  `.bb.jsonl` share a stem, so the sidecar is found.

## Streaming the recording (klein ↔ dashboard)

Not part of the Groot2 protocol either: how klein sends its recording to the
browser over the dashboard's WebSocket (`/ws`), so the browser can keep a full
mirror of it (see [architecture.md](architecture.md#browser-model)).
Encoders are in [`klein/streaming.py`](../klein/streaming.py), the decoder in
[`klein/static/recording.js`](../klein/static/recording.js). These frames go
alongside `layout`/`status`/`blackboard`/`robot`/`notice` (the frame table in
[architecture.md](architecture.md#browser-side-one-port)); with
`--record-buffer 0` none are sent. Dashboards send nothing.

A gateway streams one recording: the robot's, or with `--open` the file's.

**Order.** When a dashboard connects, klein sends it a **backfill**, the whole
retained recording as the same messages the live stream uses: `rec`, every
`gap`, then per segment `segment`, its `bb` history, one chunk-start records
frame per retained chunk, and `segment_end` if it ended; then `head` (once
there is one), one `evict` (the current extent) and `backfill_done`. Only
after that is the dashboard subscribed to **incremental** frames, each sent
as the recording changes. Backfill and subscription happen in one
synchronous step (no `await`; the frames are written with
`websockets.broadcast`), so no incremental frame can come before or inside
the backfill. A reconnect gets a fresh backfill, and its `rec` tells the
browser to drop its old copy.

While arming, the `robot` frame (which carries `recording`) is published
after the new segment begins, or for a robot that can't record, after the
old segment ends. So a dashboard told `"on"` already has the segment klein
records into, and one told `"unsupported"` has the end of the last one.

**Text frames** are JSON with a `type`. Times are absolute robot µs.

| type | when | fields |
| --- | --- | --- |
| `rec` | first frame of a backfill | `source` (`"robot"`, or `"file"` with `--open`), `name` (the robot endpoint, or the file's name), `keep_us` |
| `segment` | a segment begins (arm, swap, resume, re-arm) | `seg`, `layout_id`, `t_begin`, `max_uid`, `uids`, `start_seq`, `state` (the state at `start_seq` as STATUS bytes, `max_uid + 1` of them: the baseline for a new segment), and `layout`, the same tree the `layout` frame carries, **left out when the previous segment has the same layout** (the browser then shares that one's) |
| `segment_end` | the segment ends | `seg`, `t_end` |
| `gap` | an outage or overflow | `t_from`, `t_to`, `kind` (`"outage"`/`"overflow"`) |
| `bb` | a blackboard sample changed something, and a segment's first sample | `seg`, `t`, `removed` (`[[board, key]]`), `boards` (boards first seen at `t`, so an empty one still shows), `changes` (`[[board, key, value]]`, always the last key) |
| `head` | every drain | `seg`, `t`: the recording's head time; `bytes`: `[transitions, blackboard]`, the storage estimate (`Recording.bytes_used()`) after this drain's eviction; `capped`: which of `"transitions"`/`"blackboard"` a size cap has cut short |
| `evict` | eviction changed the recording's extent, and at the end of every backfill | `t_min` (earliest retained time, or `null`), `segments`: `[[seg, start_seq, t_start, bb_t_start]]` for every retained segment (`Recording.extent()`). The browser drops segments not listed, chunks before `start_seq`, blackboard changes before `bb_t_start` (keeping each key's base, as `BlackboardTrack.evict_before`; `null` clears it), and gaps ending before `t_min`. Nothing is recomputed |
| `backfill_done` | end of a backfill | — |

**The layout tree** (the `layout` frame's `data`, a `segment`'s `layout`) is
one node object per unrolled node, built by
[`klein/layout.py`](../klein/layout.py): `id`, `uid`, `type`, `category`,
`name`, `ports` (the attributes the author wrote) and `children`. A `<SubTree>`
node also has `subtree_id`, `is_subtree_root` and `board` (its instance's
blackboard); the root has `board` and `root_tree_id`. Every node has
`bindings`, the blackboard entries its ports reach:

```json
"bindings": [
  {"port": "_onSuccess", "dir": "out", "board": "MainTree", "key": "door_open", "at": [[0, 9]]}
]
```

- `board` is a blackboard name exactly as the `blackboard` frame names it
  (the `_fullpath`, or the tree ID for the root), and `board` + `key` is the
  entry the robot actually holds, after remapping (see
  [architecture.md](architecture.md#subtree-unrolling)). Private (`_`) keys can
  appear, though the panel hides them.
- `dir` is `"in"`, `"out"` or `"inout"`. It comes from the robot's
  `<TreeNodesModel>`: each entry's `input_port`, `output_port` and
  `inout_port` children (`addNodeModelToXML` in `src/xml_parsing.cpp`). A port
  the model lacks is `"inout"`.
- There is one binding per (`port`, `board`, `key`); a script that reads and
  writes a key gets one `"inout"` binding. A port set to a literal has none.
- `at` is `[[start, end], …]`, sorted: the character offsets in the port's
  value of the text naming the key. A `{key}` (and a `<SubTree>` remap's
  `{outer}`): the braces; `=` or `{=}`: the value; a key-name port: the value
  trimmed; a script: each name (`door_open` in `door_open:=true`), as written
  inside a subtree even when it is remapped to another name outside.

**Binary records frame**, little-endian; one per chunk touched by a drain (a
drain that fills a chunk and starts the next sends two). The header is
`<BIIIqH`, 23 bytes:

| field | size | value |
| --- | --- | --- |
| kind | u8 | `1` starts a chunk (carries its keyframe), `2` appends to the segment's current chunk |
| seg | u32 | segment id |
| seq0 | u32 | the chunk's first seq |
| first seq | u32 | this frame's first record's seq (`seq0` for kind 1; the segment's head seq so far for kind 2) |
| base | i64 | µs; the frame's earliest record time |
| keyframe length | u16 | `max_uid + 1` for kind 1, `0` for kind 2 |
| keyframe | that many bytes | the state before the chunk's first record, STATUS bytes |
| n | u32 | record count |
| records | 9 bytes each | `offset` (u48, µs since base) \| `node_uid` (u16) \| `status` (u8): the GET_TRANSITIONS / `.btlog` record |

Chunk boundaries come from these frames, so the browser never assumes a chunk
size; its chunks, seqs and keyframes are the gateway's.

## Where the constants live

klein encodes all of this in
[`klein/groot2_protocol.py`](../klein/groot2_protocol.py), used by both the
gateway and the mock robot; the `.btlog` layout is in
[`klein/btlog.py`](../klein/btlog.py), which packs its records with
`groot2_protocol`'s transition encoding, as does the WebSocket records frame
in [`klein/streaming.py`](../klein/streaming.py) (`RECORDS_HEADER_FORMAT`).
On the C++ side, see `include/behaviortree_cpp/loggers/groot2_protocol.h` and
`src/loggers/groot2_publisher.cpp`.
