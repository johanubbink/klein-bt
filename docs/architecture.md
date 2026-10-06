# Architecture

klein is a gateway. It polls the robot over ZeroMQ, turns the replies into
JSON, and pushes them to the browser over a WebSocket. It also serves the
dashboard itself.

```
   robot (BT.CPP)          klein gateway              browser (D3.js)
   ZMQ_REP :1667  <──REQ──  status @10Hz  ──WS /ws push──>  one port :8080
                            + transitions
                            blackboard @2Hz
                            recording (memory)
                            serve dashboard ──HTTP GET──>   (HTTP + WebSocket)
```

The code:

- [`klein/groot2_protocol.py`](../klein/groot2_protocol.py): protocol constants
  and the decoding of reply payloads (status, transitions, blackboards). The
  gateway decodes with it and the mock robot encodes with it. The protocol is
  described in [protocol.md](protocol.md).
- [`klein/layout.py`](../klein/layout.py): a FULLTREE reply's XML unrolled into
  the dashboard's tree: subtrees stitched in, node uids, ports and categories,
  and the blackboard names to ask for.
- [`klein/gateway.py`](../klein/gateway.py): the asyncio process. It holds the
  ZeroMQ client, the pollers and the HTTP + WebSocket server.
- [`klein/cli.py`](../klein/cli.py): the `klein-bt` command, which parses the
  flags and runs the gateway.
- [`klein/recording.py`](../klein/recording.py): the recording model
  ([Recording model](#recording-model)). [`klein/streaming.py`](../klein/streaming.py)
  streams it to dashboards, and [`klein/btlog.py`](../klein/btlog.py) saves and
  loads it as `.btlog` files.
- [`klein/static/`](../klein/static/): the dashboard. `app.js` handles state,
  the WebSocket, the tree and the sidebar; `renderers.js` formats blackboard
  values; `recording.js` and `cursor.js` mirror the recording (see
  [Browser model](#browser-model)); `drawer.js`, `timeline.js` and
  `overview.js` are the drawer, its Timeline tab and its overview. D3 is
  vendored so it works without internet.

## Robot side: one socket, two pollers

The gateway keeps one ZeroMQ REQ socket to the robot. REQ/REP is strictly
send-then-receive, so every request goes through one `asyncio.Lock`. If a
request times out, the socket is stuck mid-exchange, so klein throws it away
and opens a new one.

At startup the gateway sends a FULLTREE request, retrying with backoff until the
robot answers. It parses the XML into one unrolled tree and caches the result as
a JSON frame that every new dashboard gets.

Two pollers then share the socket:

- **status** at 10 Hz: the packed status records, sent on as `{uid: {status, from}}`.
  While recording, each STATUS is followed by a `t` drain of the transitions.
- **blackboard** at 2 Hz: a dump of every subtree's board. Values change more
  slowly than status, and the payload is bigger. While recording, each dump is
  also added to the recording.

While recording (the default), both pollers run whether or not a dashboard is
connected, so the recording has no holes. With `--record-buffer 0`, or a robot
that can't record, they pause when no dashboard is connected, so an idle klein
puts no load on the robot. Only the status poller decides whether the robot is
reachable, and that state is pushed to dashboards only when it changes.

The poll interval is the module global `gateway.POLL_INTERVAL` (0.1 s), not a
flag; the tests lower it.

## Tree swaps

A robot can load a new tree while klein is watching. The status stream doesn't
say so; the UIDs just start pointing at different nodes. So the status poller
re-runs FULLTREE when either of these happens:

1. The tree UUID in a reply differs from the loaded one. A new UUID means a new
   tree (see [protocol.md](protocol.md#the-tree-uuid)).
2. Telemetry comes back after an outage. Restarting the robot on the same port
   is the usual way a tree changes, and this catches it even if a publisher
   reuses its UUID. ZeroMQ reconnects silently, so the outage only shows up as a
   timed-out poll.

The status buffer that triggered the check is dropped, since its UIDs may
belong to a tree the dashboard doesn't have yet. If the new tree really is
different, klein sends the new layout and a short `notice`.

Rules that keep this safe:

- Only a successful FULLTREE reply updates the stored UUID. If a re-handshake
  fails, the next poll still mismatches and tries again.
- Only the status poller triggers a re-handshake, so two can't overlap. The
  blackboard poller notes the layout generation before each request and drops
  any reply that lands after a swap.
- If the XML is unchanged (a plain restart), klein keeps the current layout and
  the canvas doesn't flash.

## Recording

The gateway owns one `Recording` (see [Recording model](#recording-model)) and
feeds it from the robot's transition buffer. The request order on the wire,
and why it loses nothing, is in [protocol.md](protocol.md#how-klein-records).

- **Arming** runs at the end of every successful handshake, including one
  that found the XML unchanged: `r start` (its reply is the segment's
  `t_begin` and anchors `RobotClock`), the baseline `S` (a uid-indexed state
  of length max uid + 1), then `begin_segment`, which opens a new
  **segment**.
- **The layout** a segment carries is a `recording.Layout` (generation, XML,
  unrolled tree, uids), one per parsed XML. A handshake that keeps the layout
  (a same-tree restart or an outage resume) reuses it, so `Recording.runs()`
  keeps those segments in one tree run; a new XML gets a new `Layout` and
  starts a new run.
- **Each status cycle** while armed: `S` is broadcast, then `t` is drained.
  The drained offsets plus `t_begin` are appended, eviction runs, and the head
  moves to the robot's time now. Each blackboard sample stored runs eviction
  too, so a dashboard connecting between two drains is never told sizes
  over the caps.
- **Overflow**: a drain of exactly 1000 adds an `overflow` gap from the
  previous record to the drain's first, appends the drain and re-arms with the
  same `Layout`: a new segment, same run.
- **Outage**: a request timeout from either poller or the handshake (`T`)
  marks that the robot may have gone; a good `S` clears the mark, so a lone
  timed-out `B` changes nothing. A timed-out `S` ends the open segment at the
  head, the last drain and so the last time klein heard from the robot; the
  re-handshake when telemetry resumes re-arms and adds an `outage` gap from
  that end to the new `t_begin`. If only a `B` timed out and the robot is
  back as a new publisher by the next `S`, the re-arm ends the segment the
  same way and adds the same gap. A restart quick enough that nothing times
  out arrives as a new UUID: a new segment in the same run, with no gap.
- **Tree swap**: the re-handshake re-arms; `begin_segment` ends the old
  segment at the new one's `t_begin`.
- **Blackboard**: each successful `B` is added to the open segment at
  `RobotClock`'s robot time.
- **No recording support** (`r` errors, BT.CPP < 4.3.3): the open segment
  ends, no new one begins and no `t` is sent to that publisher; the next
  handshake tries again.

Beside the `layout`, `status`, `blackboard`, `robot` and `notice` frames, a
`Streamer(recording, send, source, name)` (`klein/streaming.py`) sends each
dashboard the recording itself. It is the `Recording`'s listener: each
change (a segment beginning or ending, records appended, the head moving, a
gap, a blackboard change, eviction changing the extent) becomes frames for
the subscribed dashboards. A dashboard that connects gets a backfill of the
whole retained recording first, in the same synchronous step that subscribes
it ([protocol.md](protocol.md#streaming-the-recording-klein--dashboard)).
The browser keeps a mirror of it ([Browser model](#browser-model)).

Flags:

| flag | default | effect |
| --- | --- | --- |
| `--record-buffer DURATION` | `10m` | how much recent history to keep (`90s`, `30m`, `1h`). `0` turns recording off: no `r`/`t` on the wire, and the pollers pause without a dashboard |
| `--open FILE.btlog` | — | show a saved recording instead of a robot; see [Opening a file](#opening-a-file) |
| `--debug` | off | for the test harness: serve `GET /debug/state` (404 otherwise), a read-only JSON dump of the recording (segments, runs, gaps, every retained record, the head state, the blackboard history, bytes used) |

The size caps are fixed: 200 MiB for the whole recording, of which the
blackboard may use at most 64 MiB (`Recording`'s defaults).

### Opening a file

`klein-bt --open FILE.btlog` shows a FileLogger2 file (Groot2's, a robot's
own, or one klein saved) with **no robot**: no ZeroMQ request is ever sent,
and `--robot-*` and `--record-buffer` are ignored. `KleinGateway.open_file`
reads the file (`read_btlog`), parses its XML with the same `_parse_layout` a
handshake uses, and `load_btlog` turns it and its `FILE.bb.jsonl` sidecar, if
there is one, into an ordinary `Recording` (how the file maps onto it is in
[protocol.md](protocol.md#opening-a-file)). A `Streamer` with `source:
"file"` and the file's name streams it, so the dashboard's Log, Timeline,
cursor and blackboard work as for a robot. The pollers never start; the
`robot` frame says `connected: false`, `recording: "file"`, and its detail
`No robot — viewing FILE`. Nothing evicts, and the `/log` downloads save the
file's recording again.

A file klein can't read is an error on the command line, before anything is
served (exit status 1), e.g. `[klein] cannot open FILE: not a FileLogger2
.btlog (no BTCPP4-FileLogger2 magic)`. A partial last record (the robot
stopped mid-write) is ignored, with a note.

## Subtree unrolling

FULLTREE returns one `<BehaviorTree>` block per subtree definition. The gateway
puts each block in place of the `<SubTree>` node that references it. The
reference keeps its own UID and gets the block as its child, so every UID in a
STATUS reply maps to a node on screen. Cyclic references are guarded against.

Each node carries its ports: the XML attributes the author wrote, minus `name`,
`ID`, `_uid` and `_fullpath`. Scripting hooks like `_skipIf` and `_onSuccess`
are kept. Ports don't change during a tree's life, so they're sent with the
layout and not polled.

Each subtree instance owns a blackboard, registered under its `_fullpath` (the
root uses its tree ID). The gateway collects these names in tree order during
the handshake and uses that order to sort the robot's unordered reply. Boards
klein didn't ask for are dropped, because they have no node to attach to (see
[protocol.md](protocol.md#blackboard-b)).

Each node also gets `bindings`: the `(board, key)` each port reaches, with the
port's direction (format in
[protocol.md](protocol.md#streaming-the-recording-klein--dashboard)). While
unrolling, klein keeps a scope per subtree instance the way BT.CPP builds its
blackboard (`recursivelyCreateSubtree` in `src/xml_parsing.cpp`) and resolves a
key as `Blackboard::getEntry` does (`src/blackboard.cpp`):

- `{key}` names `key`; `{=}` and `=` name the port itself
  (`TreeNode::getRemappedKey`). On a `<SubTree>` only `{=}` does: `=` there is
  a literal.
- `@key` is always on the root board.
- A `<SubTree>` port set to `{outer}` remaps the inner key to the parent's
  `outer`, recursively upward. A port set to anything else is a literal BT.CPP
  stores on the subtree's own board, which wins even under `_autoremap`.
- `_autoremap` sends every other non-`_` key to the parent. A robot's FULLTREE
  never carries it, though: klein honours it only in hand-written XML, and
  against a live robot an autoremapped subtree's keys resolve to its own
  board.
- Scripts (`Script`/`ScriptCondition` `code`, `Precondition` `if` and the
  `_skipIf` … `_post` hooks) are scanned with BT.CPP's tokens: a name before
  `:=` or `=` is written, before `+=` and friends read and written, any other
  name read. Strings, numbers, `true`/`false` and ALL_CAPS names (enums) are
  skipped. There is no grammar.
- Builtins whose plain value is a key name: `SetBlackboard` `output_key` and
  `UnsetBlackboard` `key` write it, the entry-updated nodes' `entry` reads it.
- A `<SubTree>` binding's direction is what the nodes inside do with that
  entry, merged; `"inout"` if none of them touches it.

Node `id`s include a per-handshake generation counter, so two different trees
never share ids. The dashboard's D3 join is keyed on `id`, so without this a
new tree's nodes could reuse the old tree's cards and labels. A reconnect
replays the cached layout with the same ids, so nothing is redrawn.

## Node categories

Each node has a `type` (its registration name, like `OpenDoor`) and a
`category` (`Control`, `Decorator`, `Condition`, `Action` or `SubTree`). The
dashboard styles on `category`.

Categories come from the robot's own `<TreeNodesModel>` section, read once per
handshake. If a robot is too old to send it, klein falls back to BT.CPP's
builtin node list and marks anything else `Undefined`. It never guesses from the
tree's shape. Details are in [protocol.md](protocol.md#node-categories).

## Recording model

[`klein/recording.py`](../klein/recording.py) holds every transition klein has
seen, as a pure data model (no asyncio, no ZeroMQ). The gateway feeds it (see
[Recording](#recording)); anything that shows a past moment reads it back.

- **State** is a `bytearray` indexed by uid in the STATUS byte encoding: 0–4
  live, `10 + X` for "IDLE after X". `apply_transition` is the publisher's
  callback rule (IDLE becomes `10 +` the previous live status), so replayed
  state is byte-identical to a STATUS reply. One addition: an IDLE on a node
  that is already idle (0 or `10 + X`) changes nothing, and
  `apply_transition` returns False for it. A robot sends one only after a
  real change klein missed (an overflow, or a publisher created while the
  tree was already ticking), and then the node keeps its stale value until
  the next re-arm or its next transition. Arming replays the records between
  `r start` and the baseline `S` on top of that baseline; every other status
  is idempotent, so with this rule the replay ends on the baseline's bytes.
  The states at the seqs inside that overlap (a round trip long) are
  approximate. `decode_state` gives the `{uid: {status, from}}` shape the
  dashboard paints. A plain 0 only exists before a node's first run, so
  `equivalent` treats it as equal to any `10 + X`.
- **Segment**: one continuous arm (`r start`), with its `Layout` (segments of
  the same XML share one), a baseline state and `t_begin`/`t_end`. Times are
  absolute robot µs. Records are `(ts, uid, status)`, stored in **chunks** of
  up to 1024 in `array` columns (~11 B per record). Each chunk keeps a
  **keyframe**, the state before its first record.
- **Sequence numbers** count records applied within a segment and are never
  reused, so `state_at_seq(n)` is the state after the first `n` records: copy
  the chunk's keyframe, apply at most 1023 records. `seq_at_time(t)` counts the
  records with `ts <= t`, so records sharing a µs are all included and the
  cursor can still step between them by seq.
- **Recording**: the segments in time order, gaps (`outage`, `overflow`) and
  the head time. `runs()` groups consecutive segments sharing a `Layout` into
  tree runs: a same-XML restart stays in its run, a swap starts a new one.
  One `.btlog` is saved per run (`klein/btlog.py`, `export_run`; see
  [protocol.md](protocol.md#saving-a-recording)). `snapshot_run(run)` copies
  a run (full chunks shared, the partial last one copied) so it can be
  exported off the event loop while recording goes on.
- **Lost transitions** are never patched in from a snapshot. An overflow
  drops some, and a node can then show the wrong status until its next
  transition or the re-arm (a fresh baseline) that follows the overflow
  corrects it. `diff_to_transitions(a, b)` gives the transitions that turn
  one state into another; saving a `.btlog` uses it to re-create a run's
  starting state.
- **Eviction** drops whole sealed chunks, oldest first across segments: first
  those older than the keep window (`--record-buffer`). Next it trims the
  blackboard (see below), and only then drops more chunks while the total
  still exceeds the size cap. After eviction a segment's `t_start` is the
  last evicted record's time, and `state_at` returns None before it. An
  ended segment wholly before the window is dropped. Because only whole
  sealed chunks go, a slow tree can keep up to 1023 records older than the
  window. `extent()` is what eviction can change (the earliest time, and per
  segment its first seq, `t_start` and blackboard start); the listener hears
  `evict` only when it changed.
- **Blackboard track**, one per segment: per `(board, key)`, the times and
  JSON strings of each change (as the robot sent them, so an object's fields
  keep its order), or a removal marker. Unchanged values (compared with
  sorted keys) aren't stored again. `at(t)` gives the latest change at or
  before `t` per key, or None before the first sample. The blackboard is
  evicted by the same keep window and by its own sub-cap. The sub-cap is
  applied before the total cap, so the blackboard can't push out transitions
  that fit beside it. Eviction drops the oldest changes but keeps each key's
  latest change at or before the new start, so `at(t)` stays correct for
  every retained time.
- **Capped flags**: `Recording.transitions_capped` and `blackboard_capped`
  say that a size cap, not the keep window, decides how far back that
  history reaches: set when the cap evicts, cleared once the window's cutoff
  passes the cut. The drawer's recorder pill shows them.
- **`RobotClock`** maps klein's monotonic clock to robot µs, taking the
  `r start` reply's timestamp as the midpoint of its round trip. Blackboard
  samples are timestamped with it.

## Browser side: one port

One `websockets` server owns `--port`. It serves the static files over HTTP
(`/`, `/styles.css`, `/app.js`, `/renderers.js`, `/recording.js`,
`/cursor.js`, `/drawer.js`, `/timeline.js`, `/overview.js`,
`/d3.v7.min.js`) and upgrades `/ws` to a WebSocket. Because both come from
the same origin, the page just opens `ws://<same host:port>/ws`.

While recording, the same port serves the recording as downloads, per tree
run or all in one `.zip` ([protocol.md](protocol.md#saving-a-recording) has
the file formats). All GET; with `--record-buffer 0` they 404.

| path | answer |
| --- | --- |
| `/log/runs` | JSON `[{run, tree_id, t_begin, t_end, filename, blackboard}]`, oldest run first: `t_begin` is the run's retained start (µs), `t_end` its end or `null` while it records, `tree_id` the main tree's ID, `filename` `<tree_id>_<YYYY-MM-DD>_<HH-MM-SS>.btlog` from `t_begin` in the gateway machine's local time (a name already used by an earlier run gets `_2`, `_3`… before `.btlog`), `blackboard` whether the run has any blackboard sample |
| `/log.zip` | every run in `/log/runs` in one zip (`application/zip`, `Content-Disposition: attachment; filename="klein_<YYYY-MM-DD>_<HH-MM-SS>.zip"`, the local time of the request): per run its `filename` and, when `blackboard`, the `.bb.jsonl` beside it, each byte for byte the two routes below |
| `/log.btlog?run=N` | run `N`'s `.btlog` (`application/octet-stream`, `Content-Disposition: attachment` with that filename) |
| `/log.bb.jsonl?run=N` | run `N`'s blackboard sidecar, with the matching `.bb.jsonl` name; a 404 when the run has no blackboard (a file opened without its sidecar, or a run that ended before the first 2 Hz poll), since a header-only sidecar would reopen as empty boards instead of "No blackboard in this file." |

An unknown `N` is a 404. Run indices count from the oldest retained run, so
they shift by one each time eviction drops a whole run: an index means the
same run in `/log/runs` and a download only when both are asked for before
the next eviction.

Each download is a synchronous snapshot of the recording at request time:
the gateway reads the runs and copies them (`snapshot_run`) without yielding
to the event loop, so eviction can't change them halfway and a zip never
mixes two states. All exporting and compressing then runs on the snapshot in
a worker thread (`asyncio.to_thread`), so polling and streaming go on. The
single-file routes export only their own file.

Dashboards only receive. On connect, a dashboard gets the recording's
backfill (while recording), the cached layout, the last blackboard frame and
the robot's reachability, then the live stream.

| type | when | content |
| --- | --- | --- |
| `layout` | on connect / re-handshake | the unrolled tree |
| `status` | 10 Hz | `{uid: {status, from}}` |
| `blackboard` | 2 Hz | `data`: `{board: {key: value}}`, tree order; `t`: klein's monotonic clock (µs, not robot time) when the reply arrived, to time changes without a recording |
| `robot` | on change | `{connected, detail, recording}`; `recording` is `"on"`, `"off"` (`--record-buffer 0`), `"unsupported"` (the robot answered `r` with an error) or `"file"` (`--open`: no robot) |
| `notice` | on a tree swap | `{text}` |
| `rec`, `segment`, `segment_end`, `gap`, `bb`, `head`, `evict`, `backfill_done`, binary records | backfill on connect, then as the recording changes | the recording; see [protocol.md](protocol.md#streaming-the-recording-klein--dashboard) |

Every frame type except `notice` is cached for new clients (the recording's
frames by being rebuilt from the recording as a backfill). A notice is about
something that just happened, so a late client doesn't get it.

## Browser model

The browser **mirrors the recording**: it holds the same segments, chunks,
keyframes, seqs and blackboard history as the gateway's `Recording`, filled
from the frames the gateway streams. Everything that shows a past moment
(state, blackboard, log rows, timeline bars, stepping, playing) is computed
locally from that mirror; **scrubbing never asks the backend anything.** The
mirror holds everything the gateway keeps.

The mirror is two classic scripts with no DOM and no d3, so the tests run
them under `gjs` against vectors `tests/make_vectors.py` builds from the
Python model. That pins the two implementations together.

- [`klein/static/recording.js`](../klein/static/recording.js) →
  `globalThis.KleinRecording`. `createStore()` returns a store whose
  `ingest(message)` takes a parsed text frame or a binary `ArrayBuffer`;
  `store.recording` is the one recording klein streams. The Python model's
  queries have mirrors here (`stateAtSeq`, `seqAtTime`, `stateAt`, `bbAt`,
  `decodeState`), next to what the cursor, the Log (`logRows`, `logRowAt`)
  the Timeline (`intervalsAll`, `timelineMarks`, `timelineSections`) and
  the overview (`treeRuns`) need.
- [`klein/static/cursor.js`](../klein/static/cursor.js) →
  `globalThis.KleinCursor`. A clock is data: `live()`, `pause(segId, seq,
  t?)`, `play(fromSegId, fromSeq, nowMs, t?)` (1× only).
  `cursorPos(clock, nowMs, recording)` gives `{seg, seq, t, live, mode}`,
  `mode` being `"live"`, `"playing"` or `"paused"`: live is the last
  segment's head, and playing moves robot time with wall time until it
  reaches the head, where the mode is `"live"`. `shownTime(pos, recording)`
  is the time a position shows: its own, or live the head's or the newest
  blackboard sample's, whichever is newer. `headTime(recording)` is that time
  at the head; the Timeline's window, the overview and the clock all end
  there. `pauseAt(recording, t)` is a paused clock at robot time `t`, clamped
  to `[tMin, headTime]` and to its segment's `tStart`; the Timeline's axis,
  the overview and Home use it. `evicted` says whether eviction dropped the
  moment a clock shows.

`app.js` routes binary frames and the recording's message types to the store.

### Rendering

There is one render path. A WebSocket message only stores what it carries
(into the store, or as "the latest `layout`/`status`/`blackboard` frame") and
asks for a frame; `render()` runs on the next `requestAnimationFrame` and
decides what is displayed:

- **The display source.** While the recording has a backfilled segment, the
  source is the **recording**: `pos = cursorPos(clock)`, live being the head
  of the last segment; the status is `decodeState(stateAtSeq(segment,
  pos.seq))` and the blackboard `bbAt(segment, t)` (live takes the newest
  sample). That includes an outage: the ended segment's head is the last
  state klein saw, which can be a tick newer than the last `status` frame, so
  the display doesn't step back when the robot goes away. A live, ended
  segment gives way to the `status` frames only when the `robot` frame says
  `recording: "unsupported"` (the robot came back unable to record).
  Otherwise the source is the **frames**: with `--record-buffer 0`, with a
  robot that can't record, and before the backfill is done. The clock is
  live unless the drawer's Log or Timeline paused it (see
  [The drawer](#the-drawer)). While it plays, every render asks for the next
  frame, and once `cursorPos` says live the clock becomes live.
- **Layout.** The canvas shows the source's tree: the cursor's segment's
  layout, or the latest `layout` frame. It is redrawn only when the tree's
  root id changes. Node ids are generation-prefixed, so equal ids are the
  same tree: a swap's `segment` and `layout` frames draw it once, and a
  same-tree restart or re-arm (a new segment sharing the layout) doesn't
  redraw. A WebSocket reconnect forgets the drawn id, so the first tree
  klein names redraws the canvas and resets the camera.
- **Painting.** The same `paintStatus` (strokes and pills, skipping cards
  whose status is unchanged) and `renderBlackboards` draw either source.
  `updateTreeLayout` repaints the displayed state too, so cards revealed by
  unfolding show it at once. `renderBlackboards(boards, {track, t, mode})`
  marks changes from a segment's blackboard `track` at time `t` in mode
  `live`, `playing` or `paused`. `KleinRecording.bbChange(track, board, key,
  t)` gives a key's last change at or before `t` (`tChange`; a value at or
  before the blackboard's start is not a change) and the count of changes in
  `(t − KleinRecording.BB_RECENT, t]` (3 s). A key is *fresh* when it changed
  within 0.5 s (one sample) and *streaming* with 4+ changes in 3 s; streaming
  wins (`bb-stream`: a steady dim edge and a `~`). A fresh key fades once
  (`bb-fresh`, `--fade` in `styles.css`, 1.8 s) live or playing, and is
  marked statically (`bb-mark`) when paused or with reduced motion.
  Switching mode only primes the rows, so a jump never animates; closed or
  hidden rows never start a fade. Without a recording, the same rule runs
  on a local track of the `blackboard` frames (`addBoards`, at their newest
  `t`, kept to the last 3 s): timed by the gateway, so a busy browser
  doesn't change the answer. The board panel is re-rendered only when a
  blackboard input changed or the cursor moved, not on every `head`.
- **Keys and nodes.** `showTree` indexes the layout's `bindings` once:
  `(board, key)` to its writers (`out`, `inout`) and readers (`in`), a node
  that does both counting as a writer. Hovering a key row outlines the
  writers' cards solid and the readers' dashed (`rect.node-link`, outside
  the card, so the status stroke stays readable) and accents the key on
  their port lines: `.node-ports` is split into tspans (`portPieces`, cut
  at each binding's `at`) whose text joins to exactly the truncated line,
  so nothing moves. A node hidden by a fold is outlined on its outermost
  folded ancestor's card, and a fold or unfold while hovering moves the
  outline there. Hovering a card rings its keys' rows (`.linked`; a folded
  card's include the nodes it hides, and folding the hovered card by its
  click re-rings them).
  **The writer pulse** follows the row marks:
  `renderBlackboards` hands `setPulses` the keys it marks statically and
  the ones whose fade it would start (whether or not their board is open),
  and a `--changed` dot at the writers' card corner holds (`mark`) or fades
  once (`fire`, 1.8 s). Streaming keys don't pulse. Only changed cards are
  written.
- **An opened file** (`klein-bt --open`) is the same recording source, with
  three differences in wording only: "live" is the file's end, so the
  drawer says `● End` there and offers **Jump to end** elsewhere (Esc and End
  return to the end too); without a sidecar the board panel says **No
  blackboard in this file.** at every moment (`body.no-blackboard`, which
  also hides the "sampled 2×/s" note); and the recorder pill and the
  connection line say there is no robot (see [The drawer](#the-drawer)).
- **The past.** `body.viewing-past` (the cursor isn't live) stops the
  RUNNING pulse, like `body.telemetry-stale`, shows the blackboard's "sampled
  2×/s" note, and draws a thin amber edge round the visible canvas (right of
  the sidebar, above the drawer: `#past-edge`). Which moment is shown is the
  drawer's clock, beside its **Jump to live** (see
  [The drawer](#the-drawer)).
- **The drawer** (see [The drawer](#the-drawer)): `KleinDrawer.showRecording`
  and `KleinDrawer.update(rec, pos)` run on every render (the drawer reads
  `pos.mode` and keeps no clock of its own), then, once the cursor's tree is
  on the canvas, `KleinTimeline.update(rec, pos, hierarchy)` and
  `KleinOverview.update(rec, pos)`. They move the cursor through one
  `seek(clock)` (`KleinDrawer.onSeek`, `KleinTimeline.connect`; the overview
  goes through the Timeline), which sets the clock and asks for a render; the
  Timeline's chevrons fold the canvas through `toggleFold` / `foldOthers`, the
  same `toggleFold` a card's click uses.

`window.kleinDebug()` is read-only, for the test harness: the mirror's
`describe(rec)` facts (when there is a recording) plus `displaySource`
(`"recording"` or `"frames"`) and `displayed`, the `{uid: {status, from}}`
last painted.

Because the mock answers `S` and then steps, the drain after a reply already
holds the next tick, so live cards can be one poll ahead of the latest
`status` frame (a real robot's head is ahead by whatever ran between `S` and
`t`).

A node card shows each piece of information in its own place:

| question | shown by | from |
| --- | --- | --- |
| what is it doing? | outline colour and status pill | the displayed state (recording or `status`) |
| is that still live? | RUNNING cards pulse only while telemetry arrives | `robot` |
| which subtree is it in? | a pink ring, and a fill that gets lighter and pinker per nesting level | `is_subtree_root` |
| what kind of node is it? | a tinted glyph before the label | `category` |
| what does it read and write? | its port line; hovering a key row outlines it (solid: writes, dashed: reads); a corner dot when a key it writes just changed | `bindings` |

When the robot is unreachable, the pulse stops but the colours stay, so you can
still read the last known state. Type is shown by glyph and name as well as
colour, so cards read fine with colour vision deficiency. If a node's name is
just its type (BT.CPP writes `name="Inverter"` on an unnamed `<Inverter>`), the
card shows it once.

## The sidebar

The sidebar (title, connection, layout, legend and the blackboards) is a
full-height pane over the canvas's left edge, and behaves like the drawer
(`app.js`, "Sidebar"):

- **Width.** Drag the right edge; the width is clamped between 200 px and
  half the window, and re-clamped when the window resizes. Double-clicking
  the edge, or the fold button in its top-right corner, toggles collapsed and
  back to the chosen width. Collapsed, a 49 px strip keeps the fold button
  and the edge, as the collapsed drawer keeps its transport row; dragging the
  strip's edge opens it at the dragged width. Width and collapsed state are
  kept in `localStorage` (`klein.sidebar`); without storage it starts at
  320 px every time.
- **Who follows.** The shown width (the strip's when collapsed) is the
  `--sidebar-width` custom property on `<html>`: the drawer's left edge and
  the banner stack's centre follow it in CSS alone, live while dragging.
  `sidebarWidth()` returns the same width for `visibleViewport()`, so R and F
  frame the space right of it. Folding nudges the camera by half the change
  so the tree stays centred; dragging leaves it be. A change of width also
  asks for a render, so the Timeline redraws its bars for the drawer's new
  width.
- **Shared with the drawer:** `KleinDrawer.pane(...)`, the dragged-edge,
  foldable, remembered panel both are, and the fold button's and the grip's
  CSS. Each panel keeps its own state and storage.

## The drawer

A bottom drawer ([`klein/static/drawer.js`](../klein/static/drawer.js)) holds
the recording views in two 48 px rows over a scrollable body:

- the **transport row**, shared by both tabs: the collapse button,
  **|◀ ▶ ▶|**, `● Live` (or, in the past, **Jump to live ⏭**),
  the overview of the whole recording (taking the space left), then the
  clock;
- the **tab row**: the **Log** and **Timeline** tabs, the compact filter
  right after them, on the Timeline tab its zoom (− window +), and at the far
  end the recorder pill with **Save** as its right half.

The tabs share the body; each keeps its own scroll position. It opens on
the Timeline. It sits beside the sidebar, its left edge following the
sidebar's width (see [The sidebar](#the-sidebar)).

- **Height.** Drag the top edge; the height is clamped between both rows
  (97 px with the border) and 80% of the window, and re-clamped when the
  window shrinks. Double-clicking the edge, or the collapse button, toggles
  collapsed and back to the chosen height. Collapsed, only the transport
  row shows (49 px with the border, as wide as the sidebar's strip): the
  tree gets the whole canvas, and the overview, the step and play buttons
  and Jump to live still move it. Height and collapsed state are kept in
  `localStorage` (`klein.drawer`); without storage the drawer starts at
  300 px every time. The shown height is the `--drawer-height` custom
  property on `<html>`, so the hint line (`#watermark`) and the canvas's
  amber edge follow it in CSS alone.
- **The transport.** **|◀** and **▶|** step to the previous/next row of
  the Log as filtered, whichever tab is shown and folded too (the same rule
  as ↑/↓, below): with no filter that is consecutive seqs, across segment
  boundaries. From live, |◀ takes the newest row and ▶| is off. **▶** plays
  at 1× from the cursor and **❚❚** pauses, from either tab; playing that
  reaches the head goes live. Live, ❚❚ pauses at the moment shown, so the
  cards, blackboard and clock stay put: `pause(seg, headSeq, t)` with `t =
  KleinCursor.shownTime(pos, rec)` as last painted (live shows the newest
  blackboard sample, so `bbAt(seg, t)` keeps it). ▶ plays on from there. At
  an opened file's end the button is off.
  Beside them one button (`#tr-jump`) says where the cursor is: live, a
  green `● Live`, inert (disabled, but tinted as a state, not greyed); in
  the past, paused or playing, an amber **Jump to live ⏭** that goes back.
  For an opened file it reads `● End` at the end and **Jump to end ⏭**
  elsewhere. It has a fixed width so the overview beside it never moves.
  Each has a key (its tooltip names it; see Keys below): ←/→ step, Space
  plays or pauses, End (and Esc) goes live; Home has no button and pauses at
  the oldest kept moment. The **clock** is the cursor's time to the µs
  (`14:03:22.201 030`, `KleinDrawer.formatTime`); live, the head's or the
  newest blackboard sample's, whichever is newer (`shownTime`). Without a
  recording the buttons are off, and Live and the clock are hidden.
- **The overview** ([`klein/static/overview.js`](../klein/static/overview.js))
  is a slim track in the transport row over the whole kept recording,
  `rec.tMin` (the oldest kept moment, which eviction moves) to the head
  (`KleinCursor.headTime`: a blackboard sample newer than the last drain
  counts); for an opened file, its first timestamp to its last record. The
  part up to the cursor is tinted, and a knob sits at the cursor (at the
  right edge while live). On it: each gap hatched like the Timeline's bands,
  and a pink dashed line where each tree run after the first starts
  (`KleinRecording.treeRuns`, as Save counts them); nothing else is marked.
  The marks are rebuilt whenever the span, the width or what is kept
  changes. Pressing or dragging the track moves the shared cursor as the
  Timeline's axis does (`KleinCursor.pauseAt`). On the Timeline
  tab a thumb outlines the Timeline's window, clipped to the track (so a
  window longer than the recording covers all of it) and at least 6 px wide;
  it follows the head while the window does. Dragging its body pans the
  window (a click on it without a drag moves the cursor, as on the track);
  dragging an edge zooms it with the other edge held (the head, for a
  window reaching past it), between the shortest and the longest window of
  − and + and inside the recording. A thumb under 16 px only pans; folded,
  there is no thumb. A mouse press doesn't focus the overview, so R and F
  keep working; it has no keys of its own. Without a recording
  (`--record-buffer 0`, or a robot that can't record) it is a bare grey
  track that ignores presses.
- **The recorder pill** says what klein keeps, from the browser's mirror:
  a red dot, `Recording`, and in muted monospace the span kept and its size,
  `10 min · 60 kB`; its tooltip is the whole summary,
  `Recording · last 10 min · 4.8k transitions · 60 kB`
  (`KleinDrawer.summary`). The span is the one actually kept, head back to
  the oldest retained record (a slow tree keeps up to a chunk more than
  `--record-buffer`, a size cap less). Under 500 bytes the size reads
  `<1 kB`. The size is the `head` frame's `bytes`, and its `capped` adds a
  note left of the pill: `blackboard history: last 3 min (size limit)`, or
  `transitions: …`. From the `robot` frame's `recording`: with
  `--record-buffer 0` the pill is grey and says `Recording off
  (--record-buffer 0)`, and with a robot that can't record `Recording needs
  BehaviorTree.CPP ≥ 4.3.3`. An opened file reads `file: t11.btlog` and its
  span (`9 s`), with an accent-coloured dot (the sidebar's connection dot
  too, and no connection warning: there is no robot by design); its tooltip
  `file: t11.btlog · 9 s · 90 transitions · no robot`.
- **A narrow drawer** (≤ 820 px, a 1140 px window with the sidebar open):
  both rows stay on one line. The tabs and the filter narrow and the clock
  drops its last three digits (the µs); then, as space runs out, the cap note
  gives way first, then the pill's `Recording` label; the pill's numbers,
  Save, the zoom and the transport buttons keep their size, and the
  overview takes what is left. The tooltips keep the full text.
- **A very narrow drawer** (≤ 480 px, an 800 px window with the sidebar
  open; works down to a 700 px window): the clock drops its ms too
  (`14:03:22`), the zoom its window length and the recorder pill its numbers
  (dot and Save stay), and the overview may shrink to 24 px. Neither row runs
  past the drawer's edge, so the page never scrolls sideways.
- **The Log tab** lists one row per retained transition, oldest first:
  time, Δ, subtree, node, from → to. Time is the robot's time of day to the
  µs (`14:03:22.201 030`). Δ is the gap to the row above as listed (`16 µs`,
  `2.5 ms`, `500 ms`, `1.20 s`), dimmed under 1 ms; the first row of a
  segment after another has a dashed top edge and says `new tree` (another
  layout) or `resumed` (the same tree after an outage or an overflow)
  instead. `from` is the node's state just before the record, so an "IDLE
  after X" reads `IDLE`. Rows are 20 px; `#log-rows` is as tall as all of
  them and only those in view plus 4 either side exist, a small pool placed
  by index. Browsers cap an element's height (Chrome near 33.5M px, about
  1.6M rows), so past 10M px the spacer stops growing and the scroll range
  maps linearly onto the rows (`virtualTop()`); `#log-rows` clips the rows
  (`overflow: clip`), so overscan rows can't lengthen the scroll range.
  Below that the mapping is exact. The rows are listed again from the mirror
  whenever records arrive, eviction drops some, or the filter changes, so an
  evicted row is never shown. While live and scrolled to the newest row, the
  log follows it; scrolling up stops that until you scroll back down or go
  back to live. Scrolled up, the row at the top stays at the top when rows
  are listed again; if eviction dropped that row too, the list shows its top.
- **Dropped history.** Once eviction has dropped transitions
  (`historyDropped`), the Log's first line, hatched like the Timeline's
  not-recorded bands, reads "Transitions older than the kept 10 min were
  dropped", with the span actually kept (as the recorder pill's), and the
  rows sit one line lower. The Timeline marks the same edge: its window never
  starts before the oldest record, so when panned or zoomed out to it, a
  dashed edge at the axis's left end, with the same note (`⇤ Transitions
  older…`, cut to fit, the whole text its tooltip) in the name column beside
  the axis. While nothing was dropped neither shows.
- **Save** (the recorder pill's right half, past a hairline) is one
  download of everything kept:
  it fetches `/log.zip` as a blob and saves it through an `<a download>`
  under the name in the gateway's `Content-Disposition`, which is how the
  pill afterwards ("Saved klein_2026-10-02_12-36-26.zip", for 4 s) knows the
  name. One download means no "download multiple files" prompt. Its tooltip
  counts the tree runs in the mirror (segments split where the layout
  changes, as `Recording.runs()`): "Save everything kept as one .zip · 3 tree
  runs". It is disabled until the mirror has a head ("Nothing recorded yet")
  and while the pill is grey (recording off or unsupported; the tooltip is
  then the pill's). If the fetch fails, the pill reads "Save failed".
- **The filter** (tab row, right after the tabs, shared by both) keeps the
  rows whose node name or subtree name contains its text, ignoring case:
  `door` matches the `DoorClosed` subtree's rows and `IsDoorClosed`. Clicking
  a row's subtree name fills it in.
- **The shared cursor.** Clicking a row pauses the cursor just after that
  transition, at `(seg, seq + 1)`: the tree shows `stateAtSeq`, and the
  blackboard `bbAt` at the row's time, nothing fading but the keys fresh at
  that time marked statically (see Painting above), with one note that
  the blackboard is sampled 2×/s. A moment before its segment's first sample
  shows no boards, only "No blackboard sample yet at this moment"; a moment
  whose blackboard history eviction dropped (it is cut at the exact cutoff,
  transitions only by whole chunks, so the oldest kept rows can be older)
  says "Blackboard history from this moment was dropped." instead
  (`Segment.bbDropped` in the mirror). Live keeps what it shows. The row is
  highlighted, the transport row's clock says `14:03:22.201 030` beside
  **Jump to live**, and the canvas gets its amber edge. Recording goes on
  underneath. When eviction drops the moment shown (`KleinCursor.evicted`),
  the cursor pauses just after the oldest kept record, its Log row selected,
  and a pill says so for 6 s: "Older than the kept 10 min: moved to the oldest
  kept transition". **↑/↓** (as ←/→ and the transport's |◀ ▶|) step to the
  previous/next row as listed: with no filter that is consecutive seqs, across
  segment boundaries; with one, the previous/next matching transition. From
  live, ↑ takes the newest row; at either end the cursor stays. **Esc**,
  **End** or **Jump to live** goes back to live and scrolls to the newest row.
  When the cursor moves elsewhere (the Timeline's playhead, stepping,
  playing), the Log scrolls its row into view (the next listed row when the
  filter hides it), at once or when the tab is shown again; going live by any
  route follows the newest row again.
- **The Timeline tab** (`klein/static/timeline.js`) has a row per node of
  the tree on the canvas (the cursor's segment's tree), in tree order, under
  a time axis that stays at the top (its zoom is in the tab row, its
  stepping and playing in the transport row). A 206 px name
  column (chevron, the card's glyph, name; a header adds `N nodes`, a folded
  row `+N`) is followed by the axis.
  - **Sections.** One per subtree: the root opens the main tree's (its header
    reads the tree's ID) and every SubTree node its own, nested where it
    occurs; a row back in the parent after a nested section has a rule above
    it. A section's header is its subtree node's own row, in that region's
    canvas fill (`--subtree-fill-N`), with the rows below it tinted lighter;
    the header of the section you are scrolled into sticks under the axis (a
    nested one covers its parent's). Besides the node's own bar, a header
    carries small red marks for every FAILURE inside its section.
  - **Fold.** The rows are the canvas's own fold: a folded card hides its
    rows, and a row's chevron folds or unfolds the card (every node with
    children has one). A folded row carries small grey marks where a hidden
    node changed. **Alt-click** a chevron to fold every subtree except that
    node's own (it and its ancestors unfold).
  - **Filter.** The shared filter keeps the rows the Log keeps (node or
    subtree name), folded away or not; sections around them stay as dimmed
    context headers. The fold is still shown, and applies again when the
    filter is cleared.
  - **Bars.** From `intervalsAll` and `timelineMarks` over the window, for
    every segment of the drawn tree in it (a resume after an outage is the
    same tree): an amber bar per RUNNING span, capped by a 3 px mark in the
    outcome's colour (green SUCCESS, red FAILURE, blue SKIPPED; none when
    still running or halted), and a single 3 px mark for an outcome reached
    in the tick it started. Shapes on the same pixel are drawn once. Not
    recorded stretches (gaps, `outage`/`overflow`) and stretches where the
    robot ran another tree (`tree: PatrolTree`; a later run of the same XML
    is a new layout too, titled "Earlier run of …") are hatched bands over
    every row; a band's label is cut to fit, and dropped when the band is a
    sliver. Bars are rebuilt when the window, the rows or the recorded part
    inside the window change; the playhead moves on its own. **Bars are
    drawn for at most 20,000 records in the window** (`MAX_WINDOW_RECORDS`);
    a window holding more shows "Zoom in to see bars: N transitions in this
    window…" over the lanes instead, and its axis, bands, dropped edge and
    playhead still work. While following live, a rebuild also waits at least
    8× the last one's script and layout time, so a busy window steps rather
    than taking the main thread; a timer makes the last rebuild when nothing
    else asks. Paused, panned or zoomed, every change rebuilds at once.
  - **The axis** reads robot time of day: `14:03:22` when ticks are whole
    seconds, else minutes, seconds and as many decimals as the step needs
    (`03:22.5`, `03:22.201 030`), ticks 1-2-5 steps about 100 px apart. The
    window is 30 s by default; − and + step it along 200 µs, 500 µs, 1 ms …
    10 s (1-2-5), 20 s, 30 s, 1, 2, 5, 10, 20, 30 min and 1 h, keeping the
    head at the right edge while following live, else the playhead in place
    when it is in view, else the middle; **Ctrl+wheel** zooms around the
    pointer, **dragging the lanes** (or Shift+wheel, or a sideways wheel)
    pans; a plain wheel scrolls the rows. The window never runs past the
    head or before the oldest record; one longer than the recording starts
    at the oldest record and the head fills it. While live it follows the
    head, until you pan or zoom it off the head (back onto the head, or back
    to live, follows again). **⤢** (or `\`) zooms to fit: the window becomes
    everything kept, `rec.tMin` to the head, its length kept within the
    ladder's 200 µs and 1 h (`clampWindow`); while live it then follows the
    head at that length, so the oldest moment drifts off its left edge.
  - **The playhead** is the shared cursor. Pressing the axis pauses there,
    and dragging scrubs: the cursor is paused at that time to the µs,
    `pause(seg, seqAtTime(seg, t), t)` in the segment holding it, so the
    tree shows `state_at(t)` and the clock that time, and the playhead stays
    under the pointer between transitions. A click on the lanes without a
    drag does the same. Stepping and playing are the transport row's (see
    The transport, above). A cursor moved elsewhere (a Log row, a step,
    playing) that leaves the window re-centres the window on it.
  - **F** with the drawer open on the Timeline also scrolls the first row of
    the running frontier into view, under the axis and its section's header.
- **Keys.** The camera keys ignore presses inside the drawer, as inside the
  sidebar (F scrolls the Timeline only when pressed outside it). The
  cursor's keys are one window `keydown` handler in drawer.js, each doing
  what its button does, in either tab and folded:

  | Key | Does | Button |
  |---|---|---|
  | ← / → (and ↑ / ↓) | previous / next transition as filtered | \|◀ / ▶\| |
  | Space | play / pause (live: pause there; an opened file's end: nothing) | ▶ / ❚❚ |
  | Home | pause at the oldest kept moment: `rec.tMin`, the records of that µs applied (an opened file: its first moment) | — |
  | End, Esc | go live (an opened file: its end) | Jump to live / end |
  | `\` | zoom the Timeline to fit; on the Timeline tab only, the Log has no window | ⤢ |

  Unmodified presses only, except `\` typed with AltGr (reported as
  Ctrl+Alt on Windows). Not in the sidebar or text fields. A focused button
  keeps Space. A key that acts is `preventDefault`ed so the drawer doesn't
  scroll; one with nothing to do (→ or ↓ from live, End or Esc while live,
  `\` on the Log) is left to the browser. Without a recording they do
  nothing. A mouse click on any drawer button, or on the sidebar's fold
  button, releases the focus, so the keys (and R, F) work at once.

## Banners

Every message over the canvas is a pill in one stack (`#banner-stack`),
12 px from the top and centred over the visible canvas: right of the
sidebar at its current width (its 49 px strip when collapsed).
`showBanner(key, text, {kind, timeout})` in `app.js` adds or replaces the
pill for `key`; `hideBanner(key)` removes it. A pill's kind is its dot
colour and its place in the stack:

| kind | dot | order | used for |
| --- | --- | --- | --- |
| `error` | red (`--color-FAILURE`) | then | the klein gateway is unreachable; "Save failed" |
| `warn` | amber (`--color-RUNNING`) | then | the robot is unreachable |
| `info` | blue (`--accent`) | last | notices, e.g. "The robot loaded a new behaviour tree — reloaded." (fades after 4 s), "Saved klein_….zip", or the cursor moved off a dropped moment (6 s) |

The stack is an `aria-live="polite"` status region, so new pills are
announced. It lets pointer events through to the canvas. The moment shown
is in the drawer's transport row.

## The camera

`R` fits the whole tree. `F` frames the running frontier: every RUNNING node
with no RUNNING node below it. Parents like `Sequence` are RUNNING whenever a
child is, so only the frontier tells you what the robot is actually doing. A
`Parallel` can have several, so `F` frames all of them.

Both aim at `visibleViewport()`, the part of the canvas you can see: right of
the sidebar (`sidebarWidth()`), below a 56 px strip kept clear for one banner
pill (always, so the tree never sits under a pill and a pill coming or going
doesn't move the camera; a second pill at once may overlap the tree's top for
a moment), and above the drawer (precisely, above the hint line that
rides on the drawer). `R` scales the whole unfolded tree to fit it, never
past 1:1, with the tree's top edge at the top and centred across (vertical
layout), or its left edge at the left and centred down (horizontal). It is
also what a new tree and a layout change do.

The frontier is computed from the displayed state (see [Rendering](#rendering),
so it follows the cursor) over the full hierarchy (`children` and
`_children`), so it still finds nodes inside collapsed subtrees. Framing only
zooms out, never in. With nothing running, `F` frames the frontier of the last
displayed state that had one, so a finished tree shows where it ended. If
nothing has run since the tree loaded, `F` does the same as `R`.

The camera flies to its target in 500 ms; with `prefers-reduced-motion:
reduce` it moves at once.

## Development

[`klein/mock_robot.py`](../klein/mock_robot.py) (`klein-bt-mock`) is a fake
publisher built on the same protocol module. It runs the whole pipeline with no
C++ involved. It has two different trees and can swap between them
(`--switch-every`), drawing a fresh UUID each time, so you can test tree swaps.
For tests it can also write every transition it makes to a file
(`--truth-log`) and publish a recorded `.btlog` in real time (`--replay`).

The tests, their tiers and the harness are described in
[testing.md](testing.md).
