# Architecture

klein is a gateway: it translates between the robot's ZeroMQ request/reply
world and the browser's push world, and serves the dashboard that consumes the
result.

```
   robot (BT.CPP)          klein gateway              browser (D3.js)
   ZMQ_REP :1667  <──REQ──  status @10Hz  ──WS /ws push──>  one port :8080
                            blackboard @2Hz
                            serve dashboard ──HTTP GET──>   (HTTP + WebSocket)
```

Three pieces, one per column:

- [`klein/groot2_protocol.py`](../klein/groot2_protocol.py) — the wire-protocol
  constants and status decoding, shared by the gateway (decode) and the mock
  robot (encode) so the halves cannot drift. The protocol itself is documented
  in [protocol.md](protocol.md).
- [`klein/gateway.py`](../klein/gateway.py) — the asyncio process everything
  runs in: ZMQ client, pollers, HTTP + WebSocket server.
- [`klein/static/`](../klein/static/) — the dashboard: `index.html`,
  `app.js` (state, WebSocket, layout), `renderers.js` (blackboard value
  renderers), and a vendored D3.js so air-gapped robot networks need no
  internet.

## Robot side: one REQ socket, two pollers

The gateway holds a single ZeroMQ REQ socket to the robot's REP port. REQ/REP
is strictly send-then-receive, so all requests are serialized through one
`asyncio.Lock`; a request that times out leaves the socket stuck in the wrong
half of its state machine, so the socket is thrown away and recreated rather
than reused.

On startup the gateway performs the FULLTREE handshake, retrying with backoff
forever — klein may legitimately start before the robot, and the dashboard
should come alive the moment the robot appears. It runs again whenever the
robot's tree changes (below). Each handshake parses the XML into an unrolled
tree and replaces the single cached JSON frame that every connecting client
receives verbatim.

Two pollers then share the socket:

- **status** at 10 Hz — the packed status records, decoded and broadcast as
  `{uid: {status, from}}`.
- **blackboard** at 2 Hz — one dump of every subtree's board per request;
  values change slower than status, and the msgpack payload is the heavier of
  the two.

Both pollers idle when no dashboard is connected, so an unattended klein costs
the robot nothing. Robot reachability is owned by the status poller alone (a
second reporter on a different cadence would make the indicator flap) and is
pushed to dashboards only on change.

## Tree swaps

A robot can load a different behaviour tree while klein is watching. Nothing in
the status stream says so — the UIDs simply start meaning other nodes — so
without detection the dashboard paints the new tree's telemetry onto the old
tree's cards.

The status poller re-runs the FULLTREE handshake on two triggers, and discards
the status buffer that raised either — its UIDs may index a tree the dashboard
has not been sent, so broadcasting it would land them on the previous tree's
cards. When the handshake returns a tree that really is different, the new layout
goes out followed by a one-off `notice` frame.

1. **The reply's tree UUID no longer matches the loaded layout.** A new UUID
   means a new tree (see [protocol.md](protocol.md#the-tree-uuid)).
2. **Telemetry resumed after an outage.** Stopping a robot and starting another
   on the same port is the ordinary way a tree changes, and it is what the first
   trigger would miss if a publisher failed to draw a fresh UUID per process.
   klein does not stake the dashboard's correctness on that: an outage is
   evidence it owns, so it re-handshakes on that too. (A ZeroMQ `REQ` socket
   reconnects by itself, so the outage is visible only as a timed-out poll —
   which is exactly what this trigger watches for.)

The second trigger is affordable only because of the third rule below: a
reconnect to an unchanged tree costs one FULLTREE and changes nothing on screen.

Three rules make that safe:

- **Only a successful FULLTREE reply records the UUID**, taken from the reply
  that carried the XML; the comparison never records. A failed re-handshake
  therefore keeps mismatching instead of leaving the gateway believing a tree it
  never loaded.
- **The status poller alone detects**, for the same single-owner reason as
  reachability, and because one owner means two handshakes cannot overlap. The
  blackboard poller has a different hazard — the board *names* it asked for go
  stale, which no UUID check would catch — so it captures the layout generation
  before its request and drops a reply that arrives after a swap.
- **An unchanged tree is absorbed quietly.** Both triggers fire on a plain
  restart, and the UUID identifies the publisher rather than the tree's content,
  so "changed" is over-reported by design. The gateway compares the XML it gets
  back and keeps the layout when it is identical, rather than flashing the canvas
  through a rebuild of the same picture. That is what makes it safe to
  re-handshake on weak evidence.

## Subtree unrolling

FULLTREE returns one `<BehaviorTree>` block per subtree definition. The
gateway stitches `<SubTree>` references in place — the reference node keeps its
own UID and gains the referenced block as its child — so the dashboard renders
one seamless tree and *every* UID in a STATUS packet, reference and inner nodes
alike, lands on a visible node. A guard set prevents cyclic references from
recursing forever.

Every node carries its **ports** through to the dashboard: the attributes the
tree author wrote in the XML, minus the structural ones klein renders or wires
up itself (`name`, `ID`, `_uid`, `_fullpath`). BehaviorTree.CPP's scripting
hooks (`_skipIf`, `_while`, `_onSuccess`, …) are the author's too, so they are
kept. Ports are fixed for the life of a tree, so they ride in the cached layout
frame rather than being polled.

Each subtree instance owns a blackboard, registered under the instance path
that BehaviorTree.CPP stamps as `_fullpath` (the root registers under its tree
ID). The gateway collects these names in tree order at handshake time; that
order restores meaning to the robot's unordered reply, and each board is
attached to the subtree node that owns it so the panel and the canvas stay
linked.

Each node's `id` carries a per-handshake generation counter, so the ids of two
different trees are disjoint. The dashboard keys its d3 join on that id, and a
card's contents are written when it enters; without the generation, ids
restarting at 1 per tree would match a new tree's nodes onto the old tree's
cards and leave them showing the previous tree's labels. A *reconnect* replays
the cached layout with the same ids, so d3 still matches there and nothing
needlessly re-enters.

## Node categories

Every node in the layout carries a `category` — `Control`, `Decorator`,
`Condition`, `Action` or `SubTree` — alongside the `type` it already carried
(the registration name the tree author wrote). `type` says *which* node this is;
`category` says *what kind*, which is what the dashboard styles on, so a reader
can follow a tree's control flow without knowing the robot's node library.

The categories are not klein's opinion. A FULLTREE reply's `<TreeNodesModel>`
section is written by the robot from its own registry: each entry's tag is the
category, its `ID` is the registration name. The gateway builds that
`{name -> category}` map once per handshake, before unrolling, and stamps each
node as it goes. When the robot is too old to send the section, klein falls back
to the nodes BehaviorTree.CPP registers on itself — that alone gets every
builtin Control and Decorator right — and labels anything left `Undefined`
rather than guessing from the tree's shape. Inference would be wrong exactly
where it matters: a `Sequence` with one child is not a decorator, and a
childless node is as likely a Condition as an Action.

Nothing else about a node moved. `is_subtree_root` still marks the expansion
boundary, and how deep a node sits inside nested subtrees stays a browser-side
derivation from the hierarchy the canvas already walks — the gateway would only
be duplicating a number, and would get it wrong for a collapsed subtree.

## Browser side: one port for everything

A single `websockets` server owns `--port`. Its `process_request` hook serves
the static dashboard over plain HTTP (`/`, `/styles.css`, `/app.js`,
`/renderers.js`, `/d3.v7.min.js`) and lets `/ws` upgrade to a WebSocket.
Serving both from one origin means the page just opens
`ws://<same-host:port>/ws` — no second port to configure, inject, or firewall.

Clients are receive-only. On connect a dashboard is immediately sent the
cached layout, the last blackboard frame (so it needn't wait half a second for
values), and the current robot reachability; after that it receives the
broadcast stream. Frame types on the wire:

| type | cadence | content |
| --- | --- | --- |
| `layout` | on connect / re-handshake | the unrolled tree |
| `status` | 10 Hz | `{uid: {status, from}}` |
| `blackboard` | 2 Hz | `{board: {key: value}}`, tree order |
| `robot` | on change | `{connected, detail}` |
| `notice` | on a tree swap | `{text}` — transient |

Every frame but `notice` is cached, which is what lets a connecting dashboard be
brought up to date in one go. A notice reports something that just happened on
screen, so it is never cached and never replayed — a client connecting a minute
later did not witness the reload.

A node card encodes three separate questions on three separate channels, so no
two can be confused for each other:

| question | channel | fed by |
| --- | --- | --- |
| what is it doing? | colour of the card outline and the status pill | `status` frames |
| is that still true? | a RUNNING card pulses only while telemetry is arriving | `robot` frames |
| what region is it in? | a pink ring around the card, and a fill one step lighter and pinker per nesting level | `is_subtree_root`, walked in the browser |
| what kind of node is it? | a glyph before the card's label, tinted per category | `category` |

The pulse is the one cue that makes a claim about *now* rather than about the
last frame, so it is the one that has to stop when the robot or the gateway goes
unreachable — a tree still pulsing over a dead connection is the most convincing
thing on the canvas and the only untrue one. The colours stay, because the last
known state is worth reading; only the motion goes.

A card shows its type once, not twice. BehaviorTree.CPP writes `name="Inverter"`
on an `<Inverter>` the author never named, so klein prints the type as the card's
primary label in that case and drops the small caption above it; only a name that
says something the type does not — `tryOpen` on a `Fallback` — gets both rows.

Colour is the *secondary* cue for type: the glyph and the registration name
carry the meaning on their own, so the card still reads under any colour vision
deficiency.

Subtree membership is marked three ways at once, because it is the question the
canvas gets asked most. Each step of the fill is both lighter and tinted further
toward the subtree's own pink: the lightness is what survives a colour vision
deficiency, the hue is what makes the region obvious to everyone else, and a
whole-card tint is the only cue still legible when the tree is zoomed out far
enough that the captions are gone. On top of that, every card in a region wears
a pink ring. The ring is its own element drawn outside the card, deliberately
not the card's own border — that border is the status channel, so a node inside
a subtree still shows whether it succeeded or failed.

## Testing without a robot

[`klein/mock_robot.py`](../klein/mock_robot.py) (`klein-bt-mock`) is a fake
publisher that encodes the same protocol module the gateway decodes, driving
the full pipeline — handshake, unrolling, both pollers, renderers — with no
C++ in the loop. It carries two quite different trees and can swap between them
mid-run (`--switch-every`), publishing a fresh tree UUID each time exactly as a
restarted publisher does, so the re-handshake above can be watched too.

The unit tests under [`tests/`](../tests) cover the protocol encode/decode
round-trip, the gateway's parsing, and the mock itself.
