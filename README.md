# klein

A lightweight, self-contained CLI that streams live **BehaviorTree.CPP v4**
telemetry to an interactive browser dashboard.

klein connects to a running robot node over ZeroMQ — speaking the Groot2
publisher wire protocol that ships with BehaviorTree.CPP — recursively unrolls
nested subtrees into a single tree, and streams 10 Hz status telemetry plus live
blackboard values to an interactive **D3.js** dashboard in your browser.

![klein streaming a live CrossDoor behavior tree to the dashboard](assets/klein-demo.gif)

## Install

klein is a command-line tool, so [pipx](https://pipx.pypa.io) is the tidiest way
to install it into its own isolated environment. It isn't on PyPI yet, so
install it straight from GitHub:

```bash
pipx install git+https://github.com/johanubbink/klein-bt.git
```

This installs two commands: `klein-bt` (the dashboard) and `klein-bt-mock`
(a fake robot for testing).

## Usage

```bash
klein-bt                    # connect to a robot on 127.0.0.1:1667, open the dashboard
klein-bt --robot-host 10.0.0.5 --robot-port 1667
klein-bt --port 8080 --no-browser
```

| Flag            | Default     | Meaning                                        |
| --------------- | ----------- | ---------------------------------------------- |
| `--robot-host`  | `127.0.0.1` | IP of the C++ robot node                       |
| `--robot-port`  | `1667`      | ZeroMQ REQ/REP port on the robot               |
| `--port`        | `8080`      | klein's dashboard + telemetry port (HTTP & WS) |
| `--no-browser`  | off         | don't auto-open the system browser             |

klein prints a clickable `http://localhost:<port>` link and opens it
automatically. The dashboard supports zoom/pan, click-to-collapse subtrees, and
live per-node status coloring (green SUCCESS, pulsing amber RUNNING, red
FAILURE, slate IDLE). The **Layout** control switches between a vertical tree
(root at the top, the BT convention) and a horizontal one. D3.js is vendored
locally, so the dashboard works on air-gapped robot networks with no internet
access.

The side pane collapses with the `«` button when you want the canvas to itself,
and comes back with the handle it leaves behind.

### Blackboards

The pane's **Blackboards** section lists every subtree's board, refreshed at 2 Hz
while the tree runs. The list mirrors the tree: boards appear in tree order,
nested ones indented under their parent, each named by its subtree and badged
with that node's UID. The root board — the mission's own state — is expanded on
load; the others open on click.

Because each board is bound to a node, the panel and the canvas stay connected:
hovering a board highlights its card in the tree, and clicking the `uid NN` badge
flies the camera to it, reopening any subtree you had collapsed on the way.

A row flashes amber when its value changes. Clicking a row unwraps a long value
and, for a message klein has a renderer for, reveals a labelled breakdown of its
fields. A subtree whose ports are all remapped to its parent owns no values; its
row stays, dimmed, so the list still matches the tree.

#### How values are shown

Values arrive as BehaviorTree.CPP serializes them — bools as `0`/`1`, structs as
JSON tagged with the registered type name — and klein renders the ones it
recognizes as a single readable line:

| blackboard value | shown as |
| --- | --- |
| `nav_msgs::msg::Path` | `151 poses · map · 12.4 m` |
| `geometry_msgs::msg::PoseStamped` | `map · x 3.20  y -0.75  yaw 90.0°` |
| `geometry_msgs::msg::Quaternion` | `yaw 90.0°` |
| `builtin_interfaces::msg::Duration` | `100 ms` |
| a double carrying `DBL_MAX` | `∞ (DBL_MAX)` |
| `1.2999999999999985` | `1.3` |

Numbers are trimmed to six significant digits so float noise doesn't fill the
row; hovering any value shows exactly what the robot sent. Very large values (a
multi-hundred-pose path serializes to tens of kilobytes) are capped for display
with their full size noted.

An entry reads `(not shown)` when the robot sent no value for it — either nothing
was ever written, or its type has no JSON converter (a ROS node handle, a TF
buffer, a timeout). The protocol can't distinguish the two; registering a
converter with `BT::RegisterJsonDefinition<T>()` makes such a value visible.

**Adding a renderer.** Message types klein doesn't recognize still render — the
type name with its scalar fields inline, expanding to pretty-printed JSON — but a
type you look at often deserves better. Add one entry to `REGISTRY` in
[`klein/static/renderers.js`](klein/static/renderers.js), keyed on the `__type`
string, returning `{summary, detail}`. Renderers are pure functions and compose,
so a wrapper type can reuse the renderer for what it wraps.

## Documentation

- [docs/architecture.md](docs/architecture.md) — how klein is put together:
  the ZeroMQ→WebSocket gateway, subtree unrolling, and the single-port design.
- [docs/protocol.md](docs/protocol.md) — the Groot2 publisher wire protocol
  klein speaks: ports (including the implicit `port + 1`), framing, and every
  request type.

## Testing without a robot

`klein-bt-mock` is a small fake publisher that speaks just enough of the wire
protocol to drive the dashboard with no real robot (it ships with the package):

```bash
klein-bt-mock --port 1777             # in one shell
klein-bt --robot-port 1777            # in another
```

Run the unit tests (no extra dependencies — stdlib `unittest`):

```bash
python -m unittest discover -s tests -t .
```

## License

klein is released under the MIT License — see [LICENSE](LICENSE).

The dashboard bundles [D3.js](https://d3js.org) v7 (ISC License, © Mike
Bostock), served locally so it works on air-gapped networks.

## Disclaimer

klein is an independent tool. It is not affiliated with, endorsed by, or
sponsored by the BehaviorTree.CPP project or the authors of Groot / Groot2.
"BehaviorTree.CPP", "Groot", and "Groot2" are the property of their respective
owners. klein interoperates over the Groot2 publisher wire protocol that is part
of the open-source, MIT-licensed BehaviorTree.CPP library; it contains no code
copied from those projects.
