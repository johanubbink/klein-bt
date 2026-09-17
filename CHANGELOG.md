# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Follows the robot when it swaps trees.** Every Groot2 reply names the
  publisher instance it came from (the tree UUID in the reply header). When a
  reply carries a UUID other than the one the layout was fetched under — Nav2's
  `bt_navigator` recreates its publisher, node UIDs restarting from 1, whenever
  a goal names a different BT XML — klein fetches the new tree and pushes it to
  every dashboard instead of colouring the old layout with the new tree's
  status records. Frames from the other tree are dropped while the reload is in
  flight, the dashboard reads "Tree changed on the robot — reloading…"
  meanwhile, and a restart of the same tree leaves the dashboard undisturbed.
- `klein-bt-mock --switch-tree-every SECONDS` recreates the fake publisher with
  a second, differently shaped tree and a fresh UUID on that cadence — and
  leaves the port unbound for a moment in between, as Nav2 does — so the
  relayout can be watched without a real robot.

## [0.4.0] - 2026-09-16

### Added
- **Node ports on the card.** Each node now shows the attributes the tree author
  wrote on it — a `Precondition`'s `if=critical == true`, a
  `RetryUntilSuccessful`'s `num_attempts=5`, a `Switch`'s cases — along the
  bottom of its card, with the full set one hover away. A tree whose leaves are
  six `Dock` actions distinguished only by their ports is readable on the canvas
  instead of only in the source XML. Structural attributes klein already draws
  or uses to wire the tree up (`name`, `ID`, `_uid`, `_fullpath`) are not
  repeated; BehaviorTree.CPP's scripting hooks (`_skipIf`, `_while`,
  `_onSuccess`) are the author's writing too, so they are shown.

### Changed
- `klein-bt-mock`'s tree now carries ports of its own — an output port bound to
  a blackboard key, a subtree remapping, the scripting hooks BT.CPP writes out
  of pre/post-conditions, and a node with more ports than fit on a card — so the
  card rendering, the truncation and the hover tooltip can all be seen without a
  real robot.

## [0.3.0] - 2026-09-01

### Added
- The dashboard's controls now live in a **full-height, collapsible side pane**
  instead of a floating card, so a real robot's blackboards get the whole screen
  rather than 40% of it. Collapsing it slides the pane away and hands the canvas
  back; the camera stays centred on what is actually visible either way.
- **Layout** is now a segmented control that shows which of Vertical / Horizontal
  is active, rather than a button whose label had to be read to be understood.
- **Renderers for ROS 2 message types**, in `klein/static/renderers.js`: `Path`,
  `PoseStamped`, `Pose`, `Point`, `Vector3`, `Quaternion`, `Twist`, `Header`,
  `Time` and `Duration` each collapse to one readable line — a 151-pose path
  reads `151 poses · map · 12.4 m` — and expand to a labelled breakdown of their
  fields. Adding a type is one entry in the registry.
- Numbers are formatted for reading: float noise is trimmed to six significant
  digits (`1.2999999999999985` → `1.3`) and the `DBL_MAX` sentinel a "no limit"
  double port reports reads `∞ (DBL_MAX)`. The exact value the robot sent is
  always on hover.
- Each subtree node in the layout now carries the blackboard it owns, so a board
  can be paired with its card on the canvas: hovering a board highlights that
  node, and clicking its uid badge flies the camera there, reopening any
  collapsed subtree on the way.

### Changed
- **Blackboards** is a heading rather than a disclosure button, with every
  subtree's board always listed beneath it — nested boards indented under their
  parent, named by their last path segment, badged with their node's uid. The
  root board is expanded on load; the rest open on click.
- A board with no values of its own keeps its row, dimmed and unexpandable with a
  dash instead of a count, so the list still mirrors the tree.
- `klein-bt-mock` now publishes ROS-shaped values — a live `nav_msgs::msg::Path`,
  a `PoseStamped`, a `Quaternion`, an accumulated noisy float and a `DBL_MAX`
  sentinel — so every renderer can be driven without a real robot.

## [0.2.0] - 2026-07-31

### Added
- Live blackboard values in the dashboard: a collapsible **Blackboards** section
  lists one group per subtree — in tree order, root first — polled at 2 Hz, with
  rows that flash when a value changes and unwrap when clicked. Groups start
  collapsed and remember their state across updates. Private `_`-prefixed
  entries are hidden.
- `BLACKBOARD` ('B') support in the wire protocol client, and in `klein-bt-mock`
  (whose mission now carries evolving, type-diverse blackboard values) so the
  panel can be driven without a real robot.
- A subtree whose ports are all remapped to its parent holds nothing locally;
  such a board is shown as empty rather than omitted, so the subtree stays
  visible in the panel.
- Values the robot cannot serialize (no registered JSON converter) read
  `(not shown)`, with a tooltip explaining how to make them visible. Very large
  values are truncated for display with their full size noted, so a
  multi-hundred-pose path can't swamp the panel.

### Changed
- New runtime dependency: `msgpack` (the robot sends blackboards as MessagePack).

## [0.1.0] - 2026-07-08

Initial release.

### Added
- `klein-bt` CLI: connects to a BehaviorTree.CPP v4 robot node over the Groot2
  ZeroMQ publisher protocol, unrolls nested subtrees into a single tree, and
  streams 10 Hz status telemetry to an interactive D3.js browser dashboard.
- Single-port HTTP + WebSocket server: serves the dashboard and pushes telemetry
  from one `--port`, with the vendored D3.js so it works on air-gapped networks.
- `klein-bt-mock`: a fake Groot2 publisher that drives the dashboard with no real
  robot, for demos and testing.

[Unreleased]: https://github.com/johanubbink/klein-bt/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/johanubbink/klein-bt/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/johanubbink/klein-bt/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/johanubbink/klein-bt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/johanubbink/klein-bt/releases/tag/v0.1.0
