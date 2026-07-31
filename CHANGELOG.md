# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/johanubbink/klein-bt/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/johanubbink/klein-bt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/johanubbink/klein-bt/releases/tag/v0.1.0
