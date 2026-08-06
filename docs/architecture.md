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
should come alive the moment the robot appears. The XML is parsed once into an
unrolled tree (below) and cached as a single JSON frame that every connecting
client receives verbatim.

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

## Subtree unrolling

FULLTREE returns one `<BehaviorTree>` block per subtree definition. The
gateway stitches `<SubTree>` references in place — the reference node keeps its
own UID and gains the referenced block as its child — so the dashboard renders
one seamless tree and *every* UID in a STATUS packet, reference and inner nodes
alike, lands on a visible node. A guard set prevents cyclic references from
recursing forever.

Each subtree instance owns a blackboard, registered under the instance path
that BehaviorTree.CPP stamps as `_fullpath` (the root registers under its tree
ID). The gateway collects these names in tree order at handshake time; that
order restores meaning to the robot's unordered reply, and each board is
attached to the subtree node that owns it so the panel and the canvas stay
linked.

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

## Testing without a robot

[`klein/mock_robot.py`](../klein/mock_robot.py) (`klein-bt-mock`) is a fake
publisher that encodes the same protocol module the gateway decodes, driving
the full pipeline — handshake, unrolling, both pollers, renderers — with no
C++ in the loop. The unit tests under [`tests/`](../tests) cover the protocol
encode/decode round-trip, the gateway's parsing, and the mock itself.
