# Architecture

klein is a gateway. It polls the robot over ZeroMQ, turns the replies into
JSON, and pushes them to the browser over a WebSocket. It also serves the
dashboard itself.

```
   robot (BT.CPP)          klein gateway              browser (D3.js)
   ZMQ_REP :1667  <──REQ──  status @10Hz  ──WS /ws push──>  one port :8080
                            blackboard @2Hz
                            serve dashboard ──HTTP GET──>   (HTTP + WebSocket)
```

The code is in three places:

- [`klein/groot2_protocol.py`](../klein/groot2_protocol.py): protocol constants
  and status decoding. The gateway decodes with it and the mock robot encodes
  with it. The protocol is described in [protocol.md](protocol.md).
- [`klein/gateway.py`](../klein/gateway.py): the asyncio process. It holds the
  ZeroMQ client, the pollers and the HTTP + WebSocket server.
- [`klein/static/`](../klein/static/): the dashboard. `app.js` handles state,
  the WebSocket and layout, `renderers.js` formats blackboard values, and D3 is
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
- **blackboard** at 2 Hz: a dump of every subtree's board. Values change more
  slowly than status, and the payload is bigger.

Both pollers pause when no dashboard is connected, so an idle klein puts no
load on the robot. Only the status poller decides whether the robot is
reachable, and that state is pushed to dashboards only when it changes.

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

## Browser side: one port

One `websockets` server owns `--port`. It serves the static files over HTTP
(`/`, `/styles.css`, `/app.js`, `/renderers.js`, `/d3.v7.min.js`) and upgrades
`/ws` to a WebSocket. Because both come from the same origin, the page just
opens `ws://<same host:port>/ws`.

Dashboards only receive. On connect, a dashboard gets the cached layout, the
last blackboard frame and the robot's reachability, then the live stream.

| type | when | content |
| --- | --- | --- |
| `layout` | on connect / re-handshake | the unrolled tree |
| `status` | 10 Hz | `{uid: {status, from}}` |
| `blackboard` | 2 Hz | `{board: {key: value}}`, tree order |
| `robot` | on change | `{connected, detail}` |
| `notice` | on a tree swap | `{text}` |

Every frame type except `notice` is cached for new clients. A notice is about
something that just happened, so a late client doesn't get it.

A node card shows each piece of information in its own place:

| question | shown by | from |
| --- | --- | --- |
| what is it doing? | outline colour and status pill | `status` |
| is that still live? | RUNNING cards pulse only while telemetry arrives | `robot` |
| which subtree is it in? | a pink ring, and a fill that gets lighter and pinker per nesting level | `is_subtree_root` |
| what kind of node is it? | a tinted glyph before the label | `category` |

When the robot is unreachable, the pulse stops but the colours stay, so you can
still read the last known state. Type is shown by glyph and name as well as
colour, so cards read fine with colour vision deficiency. If a node's name is
just its type (BT.CPP writes `name="Inverter"` on an unnamed `<Inverter>`), the
card shows it once.

## The camera

`R` fits the whole tree. `F` frames the running frontier: every RUNNING node
with no RUNNING node below it. Parents like `Sequence` are RUNNING whenever a
child is, so only the frontier tells you what the robot is actually doing. A
`Parallel` can have several, so `F` frames all of them.

The frontier is computed from the last `status` frame over the full hierarchy
(`children` and `_children`), so it still finds nodes inside collapsed subtrees.
Framing only zooms out, never in. With nothing running, `F` does the same as `R`.

## Development

[`klein/mock_robot.py`](../klein/mock_robot.py) (`klein-bt-mock`) is a fake
publisher built on the same protocol module. It runs the whole pipeline with no
C++ involved. It has two different trees and can swap between them
(`--switch-every`), drawing a fresh UUID each time, so you can test tree swaps.

Run the tests (stdlib only, no extra packages):

```bash
python -m unittest discover -s tests -t .
```

They cover the protocol encode/decode round trip, the gateway's parsing, and
the mock.
