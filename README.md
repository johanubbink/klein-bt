# klein

A lightweight, self-contained CLI that streams live **BehaviorTree.CPP v4**
telemetry to an interactive browser dashboard.

klein connects to a running robot node over ZeroMQ — speaking the Groot2
publisher wire protocol that ships with BehaviorTree.CPP — recursively unrolls
nested subtrees into a single tree, and streams 10 Hz status telemetry to an
interactive **D3.js** dashboard in your browser.

## Install

```bash
pip install .
```

## Usage

```bash
klein                       # connect to a robot on 127.0.0.1:1667, open the dashboard
klein --robot-host 10.0.0.5 --robot-port 1667
klein --port 8080 --no-browser
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

## Architecture

```
   robot (BT.CPP)          klein gateway              browser (D3.js)
   ZMQ_REP :1667  <──REQ──  poll @10Hz    ──WS /ws push──>  one port :8080
                            serve dashboard ──HTTP GET──>   (HTTP + WebSocket)
```

The browser cannot speak ZeroMQ, so klein translates the robot's request/reply
channel into a WebSocket push stream and serves the static dashboard itself —
both over a single port, so the dashboard connects to `ws://<same-origin>/ws`
with nothing to configure.

## Testing without a robot

`mock_robot.py` is a small fake publisher that speaks just enough of the wire
protocol to drive the dashboard with no real robot:

```bash
python mock_robot.py --port 1777      # in one shell
klein --robot-port 1777               # in another
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
