# klein

klein lets you watch a running BehaviorTree.CPP v4 tree live in your browser.
You see which nodes are running, succeeding or failing, plus the current
blackboard values, as the robot ticks.

![klein streaming a live CrossDoor behavior tree to the dashboard](assets/klein-demo.gif)

## Install

klein isn't on PyPI yet, so install it from GitHub with [pipx](https://pipx.pypa.io):

```bash
pipx install git+https://github.com/johanubbink/klein-bt.git
```

This gives you two commands: `klein-bt` (the dashboard) and `klein-bt-mock`
(a fake robot for trying it out).

## Run it

```bash
klein-bt                                          # robot on 127.0.0.1:1667
klein-bt --robot-host 10.0.0.5 --robot-port 1667  # robot somewhere else
klein-bt --port 8080 --no-browser                 # don't open a browser
```

| Flag           | Default     | Meaning                              |
| -------------- | ----------- | ------------------------------------ |
| `--robot-host` | `127.0.0.1` | IP address of the robot              |
| `--robot-port` | `1667`      | Groot2 publisher port on the robot   |
| `--port`       | `8080`      | port the dashboard is served on      |
| `--no-browser` | off         | don't open the browser automatically |

klein opens `http://localhost:8080` for you. You can start it before the robot
is up; it keeps retrying until the robot appears.

## Using the dashboard

- Node colours show status: green is SUCCESS, pulsing amber is RUNNING, red is
  FAILURE, grey is IDLE.
- Scroll to zoom and drag to pan. Click a node to fold or unfold its children.
- Press `R` to see the whole tree again, or `F` to jump to whatever is running.
- Hover a node to see its ports.
- **Layout** switches between a vertical and a horizontal tree.
- The **Blackboards** list shows every subtree's values. Click a row to expand
  it, or hover a board to find its node in the tree. See
  [docs/blackboards.md](docs/blackboards.md) for how values are shown.
- Hide the side panel with `«` if you want more room.

If the robot loads a different tree, the dashboard switches to it by itself.

## Set up your robot

klein talks to the Groot2 publisher that comes with BehaviorTree.CPP v4. If
Groot2 can connect to your robot, klein can too. If not, add a publisher after
you create the tree:

```cpp
#include "behaviortree_cpp/loggers/groot2_publisher.h"

auto tree = factory.createTreeFromFile("my_tree.xml");
BT::Groot2Publisher publisher(tree, 1667);
```

The publisher also uses the next port up (1668), so leave that one free.

## Try it without a robot

```bash
klein-bt-mock --port 1777        # in one terminal
klein-bt --robot-port 1777       # in another
```

Add `--switch-every 200` to the mock to make it swap between two trees every
20 seconds or so.

## Docs

- [docs/blackboards.md](docs/blackboards.md): reading blackboard values, and
  adding a renderer for your own message types.
- [docs/architecture.md](docs/architecture.md): how klein works inside, and how
  to run the tests.
- [docs/protocol.md](docs/protocol.md): the Groot2 wire protocol klein speaks.

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
