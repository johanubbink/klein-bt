# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-10-03

### Added
- **klein records every transition**, not just 10 Hz snapshots, so a retry
  that takes 60 µs still shows. It keeps the last 10 minutes in memory,
  blackboard included. Needs BehaviorTree.CPP 4.3.3 or newer. Don't log from
  Groot2 against the same robot at the same time.
- **`--record-buffer DURATION`** sets how much history to keep (default
  `10m`); `0` turns recording off.
- **A drawer under the canvas** with **Log** and **Timeline** tabs, a chip
  saying what klein keeps, and a filter shared by both tabs. Drag its edge
  to resize it, or fold it away.
- **The Log tab** lists every transition. Click a row to see the tree and
  blackboard at that moment; ↑/↓ step, Esc goes back to live.
- **The Timeline tab** draws each node's RUNNING spans as bars, capped by
  how they ended, in sections per subtree that fold with the tree. Drag the
  playhead to scrub, step with |◀ ▶|, play with ▶, zoom from 200 µs to 1 h.
- **Save** downloads everything kept as one `.zip`: a Groot2-compatible
  `.btlog` per tree run, with its blackboard in a `.bb.jsonl` beside it. The
  same files are served at `/log/runs`, `/log.btlog`, `/log.bb.jsonl` and
  `/log.zip`.
- **`klein-bt --open FILE.btlog`** shows a saved recording (from klein,
  Groot2 or a robot's `FileLogger2`) without a robot, blackboard included
  when its `.bb.jsonl` is beside it.
- The Log and Timeline mark where the kept history starts once older history
  was dropped, and a cursor on a dropped moment moves to the oldest kept one.
- **Two camera keys**: <kbd>F</kbd> frames whatever is running, <kbd>R</kbd>
  fits the whole tree into the visible canvas.
- `klein-bt-mock` answers TOGGLE_RECORDING and GET_TRANSITIONS like the real
  publisher, so Groot2's logging works against it.
- **Development**: a test harness with unit, smoke, integration and UI tiers
  (`tests/run.py`), mock test modes (`--truth-log`, `--replay`,
  `--log-requests`), `--debug` for a JSON dump of the recording, and opt-in
  scripts for worst-case measurements and the README GIFs. See
  `docs/testing.md`.

### Changed
- **The sidebar behaves like the drawer**: drag its edge to resize it, fold
  it to a thin strip. Its width is remembered.
- **One banner stack at the top of the canvas** for every message, with a
  coloured dot for its kind.
- **The dashboard WebSocket also streams the recording**, and the dashboard
  paints from it while klein records. The `robot` frame gains `recording`.
  See `docs/protocol.md`.
- **klein polls the robot even with no dashboard open**, so the recording has
  no holes. `--record-buffer 0` brings the pause back.
- With `prefers-reduced-motion`, the camera moves at once instead of flying.

### Fixed
- **A new tree opens fitted to the visible canvas**, as <kbd>R</kbd> does.
- **Cards revealed by unfolding a subtree show the current status at once.**
- **The mock's "was …" statuses follow the publisher's rule.**
- **"was SUCCESS" no longer spills out of its status pill.**

## [0.5.0] - 2026-09-18

### Added
- **klein follows the robot to a new tree.** When the tree UUID changes, or
  telemetry resumes after an outage, klein reloads the tree and says so in a
  brief note. A robot that comes back on the same tree is kept without a redraw.
- **Node categories and types on the canvas.** Each node reports its category
  (`Control`, `Decorator`, `Condition`, `Action`, `SubTree`) from the robot's
  `<TreeNodesModel>`, and each card shows a tinted glyph for it.
- **Subtrees read as a region**: a framed subtree card, and a lighter, tinted
  fill for every node under it, one step more per nesting level.
- **A legend** in the sidebar for the status colours (including "was …"),
  subtree tints and node glyphs.

### Changed
- **A disconnected tree stops pulsing.** RUNNING cards freeze as soon as
  telemetry stops; the colours stay, so the last state is still readable.
- **Node ids are unique across trees**, so a new tree never reuses the old
  tree's cards and labels.
- **A card names its type once** when the node has no name of its own.
- Every node has a hover tooltip, not only nodes with ports.
- `klein-bt-mock` publishes a `<TreeNodesModel>`, carries a second tree
  (`--tree`) and can swap between them (`--switch-every`).

### Fixed
- **The mission's blackboard is listed once**, even when the robot adds the
  root board to every dump under the name `ROOT`.

## [0.4.0] - 2026-09-16

### Added
- **Node ports on the card.** The attributes the author wrote on a node
  (`num_attempts=5`, `if=…`, scripting hooks like `_skipIf`) show along the
  bottom of its card, with the full set on hover.

### Changed
- `klein-bt-mock`'s tree carries ports, remappings and scripting hooks, so
  the port display can be seen without a robot.

## [0.3.0] - 2026-09-01

### Added
- The controls live in a **full-height, collapsible side pane** instead of a
  floating card.
- **Layout** is a segmented control showing which of Vertical / Horizontal is
  active.
- **Renderers for ROS 2 message types** (`Path`, `PoseStamped`, `Pose`,
  `Point`, `Vector3`, `Quaternion`, `Twist`, `Header`, `Time`, `Duration`):
  one readable line each, expanding to their fields.
- Numbers are formatted for reading: float noise trimmed, `DBL_MAX` shown as
  `∞ (DBL_MAX)`, the exact value on hover.
- Boards are paired with their subtree card: hovering a board highlights the
  node, and clicking its uid badge flies the camera there.

### Changed
- **Blackboards** lists every subtree's board, nested under its parent and
  badged with its node's uid; the root board starts expanded. A board with no
  values of its own keeps a dimmed row.
- `klein-bt-mock` publishes ROS-shaped values, so every renderer can be seen
  without a robot.

## [0.2.0] - 2026-07-31

### Added
- **Live blackboard values**, polled at 2 Hz: one group per subtree in tree
  order, rows that flash on change and expand on click. Private `_` keys are
  hidden.
- A subtree that stores nothing itself (all ports remapped) still shows, as
  an empty board.
- Values the robot can't serialize read `(not shown)`, with a tooltip saying
  why; very large values are truncated with their size noted.
- `klein-bt-mock` publishes changing blackboard values.

### Changed
- New runtime dependency: `msgpack`.

## [0.1.0] - 2026-07-08

Initial release.

### Added
- `klein-bt` CLI: connects to a BehaviorTree.CPP v4 robot over the Groot2
  publisher protocol, unrolls nested subtrees, and streams 10 Hz status to a
  D3.js browser dashboard.
- One port serves the dashboard (with D3 vendored, for air-gapped networks)
  and the WebSocket.
- `klein-bt-mock`: a fake Groot2 publisher for running without a robot.

[Unreleased]: https://github.com/johanubbink/klein-bt/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/johanubbink/klein-bt/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/johanubbink/klein-bt/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/johanubbink/klein-bt/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/johanubbink/klein-bt/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/johanubbink/klein-bt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/johanubbink/klein-bt/releases/tag/v0.1.0
