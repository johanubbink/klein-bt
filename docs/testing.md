# Testing

klein's tests are stdlib `unittest`, in one directory per tier:

| Tier | What | Needs | Runs in |
| --- | --- | --- | --- |
| `tests/unit/` | in-process: the protocol, gateway logic (a stubbed robot), the recording model, `.btlog` files, streaming, the mock, the harness's own oracles, and the dashboard's JS models under gjs | pyzmq; gjs for the JS models | CI, ~3 s |
| `tests/smoke/` | one end-to-end run: the mock and `klein-bt` as processes, from the page and live frames to a downloaded `.btlog` | — | CI, ~2 s |
| `tests/integration/` | real processes against ground truth: what the gateway records from the mock and from BT.CPP's real `t11`, the wire tap, the mock target | g++ and a BehaviorTree.CPP build for t11 | locally |
| `tests/ui/` | the dashboard in a real browser: cards, drawer and sidebar, camera, Log, Timeline, eviction, Save and `--open` | Playwright + Chrome (or Playwright's Chromium) | locally |

```bash
pip install -e '.[dev]'                         # adds playwright (drives installed Chrome)
python tests/run.py                             # every tier in parallel, ~25 s on 16 cores
python tests/run.py --tier ci                   # tests/unit + tests/smoke, what CI runs
python tests/run.py tests/ui                    # one tier
python tests/run.py tests.ui.test_log tests.unit.test_save.LogRoutesTest   # modules or classes
python -m unittest discover -s tests/unit -t .  # CI's own command (then tests/smoke)
KLEIN_SHOTS=1 python tests/run.py tests/ui      # also save screenshots, see below
```

CI runs the unit and smoke tiers on every supported Python, with gjs on one of
them. Run the integration and UI tiers before you push a change to the
gateway, the recording or the dashboard.

`tests/run.py` (stdlib only) runs each test class in its own worker process,
`-j N` at once (default: one per CPU); `setUpClass` still runs once per class.
Most of the time is spent waiting on robots, gateways and browsers, so classes
side by side finish much sooner. It prints one summary (failures with their
tracebacks, skips, the slowest classes) and `-v` also prints each class's
output. Parallel classes can't collide: every process binds a free port and
works in its own temp dir. The two real-time checks that still fail on a
loaded machine are listed in `ALONE` and run after the rest, one at a time:
the t11 recording (its mission runs on its own clock) and the mock recording
(an outage within one 5 ms poll). `run.py` warns when an `ALONE` entry no
longer names a class.

Checks that need something optional **skip, never fail**, and say why:

| Needs | Used by | Skip reason when missing |
| --- | --- | --- |
| g++ and a built `BehaviorTree.CPP` (`$KLEIN_BTCPP_DIR`, or a `BehaviorTree.CPP` dir beside the repo or any parent) | `T11Target` | "no BehaviorTree.CPP checkout found", "build incomplete", "g++ not installed" |
| Playwright + Chrome (or Playwright's Chromium) | `BrowserProbe` | "Playwright not installed", "no browser Playwright can launch" |
| `gjs` | the JS model tests in `tests/unit/` | "gjs … not installed" |

The first run that needs t11 builds it (~4 s, cached in `tests/harness/.build/`).

## Changing tests

- Add a case to an existing table (`subTest`) or scenario before adding a test
  class; name tests by the behaviour they guard. Tests don't depend on the
  order they run in: a check that needs the scenario changed (a robot resumed,
  say) gets its own class.
- Shared code lives in one place, not in another test module: `tests/helpers.py`
  (in-process gateways and `FakeRobot`, `layout`, `random_records`,
  `.bb.jsonl` sidecars), `tests/harness/` (targets, probes, oracles, the
  debug-dump `Model`), `tests/make_vectors.py` (vector scenarios) and
  `tests/ui/__init__.py` (`DashboardCase`).
- Don't loosen an oracle, widen a tolerance or add a skip to make a test pass
  without saying why in the commit. A check that is genuinely timing-bound
  belongs in `scripts/measure_worst_case.py`, not in the suite.
- The harness decides what is correct independently of klein: `tests/harness/`
  never imports klein's code to decide what is correct, and
  `btlog_ref.py` shares no code with `klein/btlog.py`.
- No assertion depends on a screenshot.

## The harness — `tests/harness/`

Every process a target or probe starts runs this checkout (`python -m
klein.…` with the repo on `PYTHONPATH`, never the `klein-bt` console scripts,
which may point at another tree), binds a free port (retried on a fresh one, up
to 3 times, if another socket took it first), and is killed on `stop()`, on
leaving its `with` block, and at interpreter exit. Targets and `GatewayProbe`
share one base, `targets.Process` (`port`, `proc`, `log()`, `stop()`, a work
directory that outlives the `with` block).

### Targets — `targets.py`

One interface: `start()`, `stop()`, `port`, `ground_truth()`, context manager.
Ground truth is always what the robot did, from the robot's side, as
`[(absolute_us, uid, live_status)]`, never anything klein reported.

| Target | What runs | Ground truth |
| --- | --- | --- |
| `MockTarget(tree, switch_every)` | `klein.mock_robot`; `restart()` gives a new publisher on the same port | the mock's `--truth-log` (every transition, recording or not, plus a `# publisher` line per start/swap/restart) |
| `ReplayTarget(path)` | the mock in `--replay FILE.btlog` mode: the file's tree and transitions, re-emitted in real time, looping | the same truth log |
| `T11Target()` | BehaviorTree.CPP's real `t11_groot_howto`, port-argument copy built by `build_t11.sh` into `tests/harness/.build/` (cached), in a temp cwd | its own FileLogger2 `t11_groot_howto.btlog`, whole records only |

The live mock only advances on STATUS requests, so it makes no transitions
unless something polls it. The mock's `r start` reply and its truth share one
clock, so a correct client sees offset 0; t11's reply and FileLogger2 differ by
one clock read (a few µs, constant). `robot_request(port, "S")` sends one
Groot2 request on its own socket, for driving a target without klein.

### Probes — `probes.py`

- **`WireTap(upstream_port)`**: a transparent TCP proxy that decodes ZMTP 3
  both ways. Point klein at `tap.port`. Records every request (type, argument,
  id, time, connection) with its reply (UUID, sizes, and decoded `status`,
  `transitions` or `payload`). `request_sequence(ignore="B")` gives e.g.
  `"TrSSt"`; `requests("r")` has each `arg`; `transitions()`, `wait_for()`.
- **`GatewayProbe(robot_port, debug=False)`**: `python -m klein.cli` on a
  free port. `fetch(path)`; `watch()` holds a dashboard WebSocket open and
  collects frames; `debug_state()` reads `GET /debug/state`, which needs
  `debug=True` (it 404s without `--debug`, and then returns `None`);
  `require_debug_state()` skips instead. `poll_interval=0.01` launches the
  same CLI with a faster status poll; the mock steps once per poll, so its
  missions and swaps run ~10x faster while request timeouts stay real time.
- **`BrowserProbe(url)`**: Playwright against the dashboard. `open()`,
  `wait_for_nodes(count)` (that many cards, and no card or link left in a d3
  transition), `wait_connected()`, `nodes()` (each card's uid from its d3
  datum, name, pill label, classes, stroke), `state()` (`{uid: {status, from}}`
  as painted), `snapshot()` (cards, the last `status` frame the page received,
  `statusCount`, the connection dot, and `kleinDebug`'s
  `displayed`/`displaySource`, read in one JS task so they agree;
  `painted=True` reads once no render is queued, so `displayed` is painted
  from every frame `statusCount` counts, not an animation frame behind),
  `status_frames(first, last)` (status frames by number; the page keeps the
  latest 50, for checks that allow a poll of lag), `klein_debug()`
  (`window.kleinDebug()`), `click`, `drag`, `key`, and
  `screenshot(group, name)`. Also `open(fresh=True)` (localStorage cleared
  first: the default panels and tab), `next_frame()` (the next frame
  painted), `go_live()`, `feed(frames)` (recording frames, as
  `klein.streaming` makes them, straight into the page's store),
  `value_summaries(boards)` (how the blackboard panel prints each value) and
  `page_errors()`.

`tests/ui/__init__.py`'s **`DashboardCase`** is the UI tests' base: class
options `ROBOT` (`MockTarget` options, or another `ROBOT_CLASS`; `None` for
none), `GATEWAY` (`GatewayProbe` options, or `make_gateway()`; `None` for
none), `VIEWPORT` and `OPEN` (cards to wait for on a fresh open; `None`
leaves the page blank). `setUpClass` starts them with class cleanups, so a
failing set-up leaks no process, and every test fails on an uncaught page
error. Its `assert_hover_links(layout, folded=())` hovers every key row and
card and checks the outlines, port-line accents and linked rows against
`links(layout)`, which reads them from the layout's `bindings` on its own;
`PULSES` reads the cards' writer pulses.

### The debug dump — `model.py`

`Model(dump)` reads a gateway's `GET /debug/state` independently: each
segment's records, `state_at_seq`, `seq_at(t)`, the cards' `labels`,
`bb_at(seg, t)`, a node's `intervals`, the tree `runs`, and (given each
segment's layout) the Log's `rows`. With it, the dashboard's wording
written out again (`fmt_time`, `fmt_delta`, `fmt_span`, `card_label`,
`names`). The UI tests compare the browser with it, the integration tests a
saved file.

**Screenshots and kept files.** With `KLEIN_SHOTS=1`, `screenshot()` saves a
PNG to `tests/ui/artifacts/<group>/<name>.png` (gitignored; groups are named by
feature: `drawer`, `log`, `blackboard`, `overview`, …), and the recording
tests keep their downloaded `.btlog` files in `tests/ui/artifacts/groot2/` for
opening in Groot2. Without it nothing is written. Use it to look at a change.

### Oracles — `oracles.py`

Pure functions returning an `OracleResult` (truthy on pass, with a `detail`
that explains a failure). `tests/unit/test_oracles.py` checks each one fails
on known-bad input, since an oracle that wrongly passes would let every test
that uses it pass silently.

- `transitions_match(klein, truth, max_offset_us=50, from_time=None)`: same
  (uid, status) sequence from klein's first record to its last, offset within
  50 µs and constant (to 5 µs). `from_time` = klein's arm time: truth records
  after it are owed, so a record dropped at the start fails too.
- `state_matches(observed, reference, allow_was_vs_idle=True)`: node by node on
  `{uid: {status, from}}`; "was X" and plain IDLE count as equal when allowed.
- `state_in_frames(observed, frames)`: strictly equal to at least one of
  `frames` (e.g. `status_frames(n - 1, n + 1)`, one poll either side).
- `btlog_equivalent(a, b)`: version, first timestamp and XML equal, and
  identical replayed state at every record time of either.
- `request_sequence_matches(seq, pattern)`: full regex match over request
  letters, e.g. `r"TrS(St)+"`.

`btlog_ref.py` is an independent `.btlog` reader/writer/replayer written from
the BT.CPP sources, importing nothing from klein; `klein/btlog.py` is checked
against it.

### JavaScript — `tests/js/run.js`

`gjs -m tests/js/run.js VECTORS.json` loads DOM-free plain `<script>` files
(`"module"`, one or a list, loaded in order; the dashboard has no build step,
so no `export`), runs the JSON cases (`call`, `args`, `expect`, and `save` +
`$ref` for stateful objects; `{"$base64": "..."}` in `args` stands for those
bytes as an `ArrayBuffer`), and exits 0 only if all pass. Maps and Sets are
compared by content, and a case with no `expect` fails unless it only `save`s
a setup value. `tests.harness.js.run_js_vectors()` is the Python entry point;
`tests/unit/test_js_model.py` checks the runner fails on bad vectors.

The vectors are built from the Python model when the tests run:
`tests/make_vectors.py` builds a recording (a swap, a same-tree restart after
an outage, duplicate timestamps, evicted chunks, blackboard changes and
removals), streams it, and writes the frames (incremental and backfill) plus
the Python model's answers, for `recording.js` and `cursor.js` (tree runs
are checked against the harness `Model`);
`test_log_rows.py` and `test_timeline_model.py` do the same for the Log's rows
and the Timeline's helpers, on `make_vectors.build_named` (trees with named
nodes and subtrees) fed in by `make_vectors.store_cases`. `python -m
tests.make_vectors FILE.json` writes the replay vectors out, to read a
failure.

### Fixtures — `tests/fixtures/`

`groot2_mock.btlog` (written by Groot2 against the mock, 616 records) and
`t11_filelogger2.btlog` (the real t11's own FileLogger2, 90 records, two
missions). See [`tests/fixtures/README.md`](../tests/fixtures/README.md).

## Worst-case measurement — `scripts/measure_worst_case.py`

Opt-in and slow (a few minutes per part), so not part of the suite; run it
after changes to the recording's storage, streaming, the Log, the Timeline or
Save:

```bash
python scripts/measure_worst_case.py                      # all parts, ~10 min
python scripts/measure_worst_case.py --parts file,zip     # 10 min at 10k/s, opened
python scripts/measure_worst_case.py --parts file,zip --minutes 32   # the 200 MiB cap
python scripts/measure_worst_case.py --parts live --live-rates 500,9000
```

It repeats the CrossDoor transitions of `groot2_mock.btlog`, retimed to
`--rate` per second (default 10k/s, the ceiling klein can observe: 1000 per
drain × 10 drains/s). `file` opens `--minutes` of it with `klein-bt --open`
(whose backfill is exactly a live recording's) in Chrome and reports the
backfill time on load and reload, the JS heap plus ArrayBuffers after a GC
(the recording's typed arrays live outside the V8 heap), scrub frames on the
Timeline at its 30 s window and zoomed out, a pan, and Log scrolling; plus,
in-process, `KleinGateway.open_file` on it and how long building one
dashboard's backfill blocks the gateway. `zip` times `GET /log.zip` and
probes `GET /` every 20 ms meanwhile, whose longest wait is the event loop's
longest block. `live` replays it through `ReplayTarget` at each rate and
reports the main thread's busy share (CDP `TaskDuration`) with the Timeline
(after its 30 s window has filled), the Log and the drawer collapsed. It prints what it measured, with no pass or fail:
the rule is that nothing crashes or becomes too slow to use (the caps
themselves stay at 200 MiB, 64 MiB of it blackboard).

Typical results on a 16-core development machine with Chrome:

| | 10 min at 10k/s (6M transitions, the default window at the ceiling) | 32 min at 10k/s (19.2M, the 200 MiB cap) |
| --- | --- | --- |
| browser memory | 72 MB (5 MB heap + 67 MB ArrayBuffers) | 224 MB (12 + 212) |
| backfill, load / reload | 5.9 s / 3.0 s | 18.9 s / 9.6 s |
| gateway blocked per connecting dashboard | 0.5 s | 1.7 s |
| scrub frames (30 s window, and zoomed out) | 16.7 ms frame gaps, max 16.8; 0.7 ms of script per step | the same |
| Log wheel scrolling | 16.7 ms frame gaps | the same |
| Timeline pan or zoom | ~35 ms of script and layout at the 20k-record limit (a 2 s window); wider windows draw no bars (<1 ms) | the same |
| `GET /log.zip` | 4.5 s, event loop blocked 2.6 s | 14.3 s, blocked 8.3 s |

Live, the Timeline following the head with its 30 s window full: 10% of the
main thread at 10 transitions/s, 23% at 500/s (15k records in the window,
under the 20k limit, rebuilds paced), 5% at 2k/s and 9k/s (past the limit:
"Zoom in to see bars"). The Log 5–6%, the drawer collapsed 4–6%.

## Demo GIFs — `scripts/make_gifs.py`

The README's two GIFs are made by a script, so they can be remade after a UI
change. Opt-in and dev-only (Playwright from the dev extras, plus `ffmpeg`),
not part of the suite; about 1 min 20 s:

```bash
python scripts/make_gifs.py                 # both, into assets/
python scripts/make_gifs.py --only rewind   # just one (hero, rewind); --keep keeps the PNGs
```

- `assets/klein-demo.gif` (the hero): the mock's CrossDoor tree live on
  port 1777, with the drawer on the Timeline tab, following live. Once the
  20 s window has filled, it films from the start of a mission until the
  start of the next-but-one: two whole laps, so the GIF loops cleanly.
- `assets/klein-rewind.gif`: the same live mock, rewound while it runs.
  Once the window has filled, the playhead is dragged back 6 to 14 s into
  one of PickLock's failures among its retries; on the Log tab it is
  stepped with |◀ |◀ |◀ ▶|, each step moving the selected row and flipping
  PickLock's card between RUNNING and FAILURE; then a Log row is clicked,
  `door_open` is hovered in the blackboard (its writers and reader outlined
  on the tree), ▶ plays, and **Jump to live** brings it back to `● Live`.

Chrome draws klein in an iframe inside a made-up browser outline (a tab, ←
→ ↻ and an address bar reading `http://localhost:8080`), styled from
`styles.css`'s own `:root` block. It runs at 1.5× the GIF's 900 px width and
takes a screenshot after every step (about 20 a second), each stamped with the
time it was taken. ffmpeg lays them out at that pace, resamples to 12 fps,
scales to 900 px, and quantises in two passes (palettegen, then paletteuse)
with one palette and no dithering, so nothing flickers. Headless screenshots
have no cursor; the rewind GIF has a pointer drawn by the outline page. The
scenes are the same each run, apart from the wall-clock times on the axis
and in the drawer's clock, and which failed PickLock the rewind lands on.

