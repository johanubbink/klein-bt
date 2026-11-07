# klein

A Python CLI (`klein-bt`) that connects to a running BehaviorTree.CPP v4 robot
over ZeroMQ and streams live tree status + blackboard values to a D3.js
browser dashboard.

## Read these first

- `docs/protocol.md` — the Groot2 publisher wire protocol (ports, framing,
  request types). The robot binds `port` (REP, all data) and `port + 1` (PUB,
  breakpoints only — klein doesn't use it). Old two-port configs found online
  (`groot_zmq_*_port`, 5555/5556) are Groot v1 / BT.CPP v3, a different protocol.
- `docs/architecture.md` — gateway design: one REQ socket + two pollers,
  subtree unrolling, single-port HTTP + WebSocket serving.
- `docs/testing.md` — the test tiers, the harness, and the lint and
  complexity gate.

## Layout

- `klein/groot2_protocol.py` — protocol constants + reply decoding (status,
  transitions, blackboards); the single source of truth shared by gateway
  (decode) and mock robot (encode).
- `klein/layout.py` — FULLTREE XML -> `parse_layout` -> one frozen `TreeLayout`
  (the unrolled tree, uids, ports, categories, blackboard names); pure.
- `klein/robot_link.py` — `RobotLink`, the one REQ socket to the robot (lock,
  timeout, re-creation, request headers): a `Reply`, or `RobotTimeout` /
  `RobotRefused`. Tests pass a fake as `KleinGateway(..., link=)`.
- `klein/gateway.py` — the gateway process: the handshake, the two pollers
  driving the recorder, the robot frame, the WebSocket and `run()`.
- `klein/recorder.py` — the recording session: `Recorder` arms segments
  (`r start` + S baseline), drains `t`, overflow/outage gaps, a publisher that
  can't record.
- `klein/routes.py` — the plain-HTTP side of the port: every file in `static/`,
  the `/log*` downloads incl. `/log.zip`, `/debug/state`.
- `klein/cli.py` — the `klein-bt` CLI (`--record-buffer`, `--open`, …).
- `klein/recording.py` — the recording model: segments, 1024-record chunks with
  keyframes, `state_at`, blackboard track, tree runs, eviction (named
  policies) and size caps.
- `klein/btlog.py` — FileLogger2 `.btlog` read/write, per-run export,
  `.bb.jsonl` blackboard sidecar, `load_btlog` for `--open`.
- `klein/streaming.py` — streams the recording to browsers (backfill, then
  incremental binary record frames).
- `klein/mock_robot.py` — fake publisher (`klein-bt-mock`) for robot-free runs:
  `ScriptedPublisher` / `ReplayPublisher` and a socket-free `handle()`.
- `klein/static/` — dashboard, native ES modules, no build step: `index.html`
  loads D3, then `app.js` (the root: WebSocket, `render`, banners, and the one
  "Page globals" block the tests and scripts use), which imports the rest:
  `tree.js` (the canvas), `blackboard.js` (the panel), `renderers.js`, `ui.js`
  (toolkit: drag, panes, formatters), `drawer.js` (drawer, recorder pill,
  Save), `log.js` (Log tab), `transport.js` (buttons, clock, keys),
  `timeline.js` (Timeline tab), `overview.js` (the drawer's overview). Pure,
  DOM-free and checked by gjs vectors: `recording.js` + `cursor.js` (browser
  mirror of the recording and the shared cursor), `window.js` (the Timeline's
  window), `view.js` (what a render shows, blackboard marks, the Timeline's
  window contents, the Log's anchor). `styles.css` (the `:root` palette: all
  UI colours come from it), vendored D3.
- `scripts/` — dev tools: `check.py` (lint + complexity gate), and the opt-in
  `measure_worst_case.py`, `make_gifs.py`.
- `tests/` — stdlib unittest, one directory per tier: `unit/` (in-process),
  `smoke/` (one end-to-end run), `integration/` (real robots and gateways),
  `ui/` (Playwright + Chrome); `harness/` (robot targets, probes, oracles),
  `run.py` (parallel runner).
- `.claude/` — shared Claude Code setup: the `implementer` and `reviewer`
  agents, and a hook that runs ruff on every Python file Claude edits.

## Working on it

- Setup: `pip install -e '.[dev]'` (Playwright for the UI tests; ruff,
  complexipy and lizard for `scripts/check.py`).
- Tests: `python tests/run.py` runs every tier in parallel; `--tier ci` runs
  what CI runs (`tests/unit` + `tests/smoke`, a few seconds); `tests/ui` or
  `tests.ui.test_log` runs just those. Stdlib unittest, no pytest. See
  `docs/testing.md`.
- Checks: `python scripts/check.py --diff main` before a PR: ruff must be
  clean, and no changed function may get more complex than its limit allows
  (Python cognitive complexity 15, JS cyclomatic 15). Plain
  `python scripts/check.py` lists the repo's hotspots.
- Tests are few, real regression tests: extend an existing table or scenario
  before adding a class, and name tests by the behaviour they guard.
- UI colours come from the `:root` tokens in `klein/static/styles.css`; never
  hard-code a hex value.
- Manual run without a robot: `klein-bt-mock --port 1777` in one shell,
  `klein-bt --robot-port 1777` in another.
- Protocol questions are settled by the BehaviorTree.CPP source
  (`src/loggers/groot2_publisher.cpp`), not by guessing.
- When the protocol or gateway design changes, update `docs/` in the same
  change; `groot2_protocol.py` and the docs must not drift apart.
- User-visible changes get a line under `## [Unreleased]` in `CHANGELOG.md`.
