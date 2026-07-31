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
FAILURE, slate IDLE). D3.js is vendored locally, so the dashboard works on
air-gapped robot networks with no internet access.

### Blackboards

The **Blackboards** section of the control card lists every subtree's blackboard,
refreshed at 2 Hz while the tree runs. Groups are collapsed by default — expand
the ones you care about. A row flashes amber when its value changes, and clicking
a row unwraps a value too long for the panel.

Values arrive as BehaviorTree.CPP serializes them, so bools read as `0`/`1`, and
structs show as JSON tagged with the registered type name. An entry reads
`(not shown)` when the robot sent no value for it — either nothing was ever
written, or its type has no JSON converter (a ROS node handle, a TF buffer, a
timeout); the protocol can't distinguish the two, and registering a converter
with `BT::RegisterJsonDefinition<T>()` makes such a value visible. Very large
values (a multi-hundred-pose path can serialize to tens of kilobytes) are
truncated for display with their full size noted.

A subtree whose ports are all remapped to its parent has no entries of its own
and says so — its values live on the parent's board.

## Architecture

```
   robot (BT.CPP)          klein gateway              browser (D3.js)
   ZMQ_REP :1667  <──REQ──  status @10Hz  ──WS /ws push──>  one port :8080
                            blackboard @2Hz
                            serve dashboard ──HTTP GET──>   (HTTP + WebSocket)
```

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
