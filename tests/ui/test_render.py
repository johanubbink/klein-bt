"""One render path: the dashboard paints what the cursor shows.

While klein records, the cards show the browser's recording mirror at the
cursor (live = the head); without a recording (``--record-buffer 0``, or a
robot that answers ``r`` with an error) they show the latest status frames,
the recorder pill says why, and Save is greyed out. Live, the blackboard panel marks
a change softly: a key changing at every sample settles into a steady
streaming mark, others fade in once (or, with reduced motion, hold a static
mark), and no mark fills its row; paused, the writers of exactly the marked
keys hold the writer pulse. Hovering a key row or a card links exactly what
the layout's bindings name, a folded subtree standing for what it hides.
Checked in a real browser (Playwright) against the mock; skipped when
Playwright is missing.
"""
import contextlib
import random
import time
import unittest

from tests.harness.oracles import state_in_frames, state_matches
from tests.harness.model import BB_STREAMING, Model
from tests.harness.probes import SHOTS as SHOTS_ON, GatewayProbe
from tests.harness.targets import PYTHON, MockTarget, _ready_robot, launch
from tests.ui import CARD_EVENT, LINKS, PULSES, DashboardCase, links


SHOTS = "render"                    # screenshot group (KLEIN_SHOTS=1)


class NoRecordingMock(MockTarget):
    """The mock as a publisher older than BehaviorTree.CPP 4.3.3: ``r`` errors."""

    _CODE = ("import sys; from klein import mock_robot; "
             "mock_robot.toggle_recording = lambda frames, recorder: None; "
             "mock_robot.main()")

    def _start(self, pick_port):
        argv = lambda port: [PYTHON, "-c", self._CODE, "--host", "127.0.0.1",
                             "--port", str(port), "--truth-log", str(self.truth_path),
                             *self.args]
        self.xml = launch(self, argv, pick_port, ready=_ready_robot(self))


# Every card's running animation, and whether the body is frozen.
_PULSE = """() => ({
  animations: [...document.querySelectorAll('#canvas .node-rect.running')]
                .map(el => getComputedStyle(el).animationName),
  stale: document.body.classList.contains('telemetry-stale'),
})"""

# The recorder pill's state and the Save button, read in one task.
_CHIP_AND_SAVE = """() => { const save = document.getElementById('drawer-save');
  return { state: document.getElementById('drawer-chip').dataset.state,
           disabled: save.disabled, title: save.title }; }"""

# Log the watched blackboard rows after every change the panel makes, in the
# task that made it (a MutationObserver's callback runs before any animation
# could end): [ms, key, text, classes, background, segment, the change time the
# row was painted for].
_WATCH_MARKS = """() => {
  if (window.__bbObserver) window.__bbObserver.disconnect();
  window.__bbMarks = [];
  window.__bbObserver = new MutationObserver(() => {
    const rows = bbGroupEls.MainTree.rows, now = performance.now();
    for (const key of ['tick', 'robot_position', 'mission_phase']) {
      const r = rows[key];
      if (r) window.__bbMarks.push([now, key, r.value.textContent, [...r.row.classList],
                                    getComputedStyle(r.row).backgroundColor,
                                    recordingStore.recording?.segments.at(-1)?.id,
                                    r.tChange, frameTrack.tLast]);
    }
  });
  window.__bbObserver.observe(document.getElementById('bb-groups'), {
    subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['class'] });
}"""

# The cursor and the blackboard rows' marks, read in one task.
_PAUSED_MARKS = """() => {
  const p = KleinCursor.cursorPos(clock, performance.now(), recordingStore.recording);
  const marked = [];
  for (const [n, g] of Object.entries(bbGroupEls))
    for (const [k, r] of Object.entries(g.rows)) if (r.row.classList.contains('bb-mark')) marked.push([n, k]);
  return { seg: p.seg, t: p.t, mode: clock.mode, marked, pulses: (""" + PULSES + """)(),
           fading: document.querySelectorAll('#bb-panel .bb-fresh').length };
}"""

# Click a card through d3's handler and read the cards back in the same task, so
# no status frame or animation frame can repaint in between.
_CLICK = ("const g = [...document.querySelectorAll('#canvas g.node')]"
          ".find(n => n.__data__.data.uid === {uid});"
          "g.dispatchEvent(new MouseEvent('click', {{bubbles: true}}))")


class RenderTest(DashboardCase):
    """The live checks share one recording mock and gateway (``shared()``, fast
    poll); the checks that need another set-up start their own."""

    GATEWAY = {"poll_interval": 0.05, "debug": True}
    VIEWPORT = (1680, 1000)     # the camera's 0.8 scale shows the whole CrossDoor tree
    OPEN = None

    def shared(self):
        return contextlib.nullcontext(self.gw)

    def _open(self, gw, source):
        b = self.browser
        b.base_url = gw.url
        b.open(wait_nodes=1)
        b.wait_connected()
        b.wait_for_status_frames(3)
        b.page.wait_for_function(
            f"window.kleinDebug().displaySource === '{source}'", timeout=10000)
        return b

    def _near_status(self, b, snap):
        """The displayed state is a STATUS frame within one poll either way."""
        n = snap["statusCount"]
        near = state_in_frames(snap["displayed"], b.status_frames(n - 1, n + 1))
        self.assertTrue(near, near.detail)

    def _frames_parity(self, b, samples=3):
        """Without a recording: the cards are the last status frame (read
        once the page has painted every frame it received)."""
        for _ in range(samples):
            snap = b.snapshot(painted=True)
            self.assertEqual(snap["displaySource"], "frames")
            painted = state_matches(snap["state"], snap["displayed"], allow_was_vs_idle=False)
            self.assertTrue(painted, painted.detail)
            n = snap["statusCount"]
            frames = b.status_frames(n, n)
            self.assertTrue(any(state_matches(snap["displayed"], f, allow_was_vs_idle=False)
                                for f in frames), (snap["displayed"], frames))
            time.sleep(random.uniform(0.02, 0.1))

    # -- live parity ---------------------------------------------------- #
    def test_live_parity_with_recording(self):
        """At 20 moments every card equals the displayed state (the recording's
        head), and that equals a STATUS frame within one poll either way."""
        with self.shared() as gw:
            b = self._open(gw, "recording")
            # Start in the part of the lap where something runs and a card says
            # "was …" (the 20 samples span about a second of a 3.8 s lap).
            b.page.wait_for_function(
                "Object.values(window.kleinDebug().displayed).some(e => e.status === 'RUNNING')"
                " && Object.values(window.kleinDebug().displayed).some(e => e.from)",
                timeout=10000)
            was_seen = running_seen = 0
            for _ in range(20):
                snap = b.snapshot(painted=True)
                self.assertEqual(snap["displaySource"], "recording")
                painted = state_matches(snap["state"], snap["displayed"],
                                        allow_was_vs_idle=False)
                self.assertTrue(painted, painted.detail)
                self.assertEqual(len(snap["nodes"]), 13)
                # The mock answers S and then steps, so the drain after a reply
                # already holds the next tick: the head is up to one poll ahead.
                self._near_status(b, snap)
                labels = [node["label"] for node in snap["nodes"]]
                was_seen += any(label.startswith("was ") for label in labels)
                running_seen += "RUNNING" in labels
                time.sleep(random.uniform(0, 0.04))
            b.screenshot(SHOTS, "after_live")
        self.assertGreater(was_seen, 0, "no sample showed a 'was …' label")
        self.assertGreater(running_seen, 0, "no sample showed a RUNNING card")

    def test_without_a_recording_the_status_frames_are_painted(self):
        """``--record-buffer 0``, and a robot too old to record: the cards are
        the status frames, the recorder pill says why, Save is greyed out saying the
        same, and the transport's Live button, clock and zoom are gone."""
        for name, robot_cls, args, chip in (
                ("record_buffer_0", MockTarget, ["--record-buffer", "0"],
                 "Recording off (--record-buffer 0)"),
                ("cannot_record", NoRecordingMock, [], "Recording needs BehaviorTree.CPP ≥ 4.3.3")):
            with self.subTest(name):
                with robot_cls() as robot, \
                     GatewayProbe(robot.port, extra_args=args, poll_interval=0.05) as gw:
                    b = self._open(gw, "frames")
                    self._frames_parity(b)
                    self.assertEqual(len(b.nodes()), 13)
                    if robot_cls is NoRecordingMock:
                        self.assertEqual(b.klein_debug()["segments"], [])
                    b.page.wait_for_function(
                        "(t) => document.getElementById('drawer-chip-text').textContent === t",
                        arg=chip, timeout=10000)
                    self.assertEqual(b.page.evaluate(_CHIP_AND_SAVE),
                                     {"state": "off", "disabled": True, "title": chip})
                    # The drawer's overview is a bare grey track.
                    self.assertEqual(b.page.evaluate(
                        "() => [document.getElementById('overview').className,"
                        " document.querySelectorAll('#ov-marks i').length, KleinOverview.debug()]"),
                        ["empty", 0, None])
                    # ...and the transport has nothing to say: no Live
                    # button, no clock, no zoom.
                    self.assertEqual(b.page.evaluate(
                        "() => ['tr-jump', 'tr-clock', 'tl-zoom'].map(id =>"
                        " document.getElementById(id).getClientRects().length > 0)"),
                        [False, False, False])
                    b.screenshot(SHOTS, name)
                    # Keys changing at every sample settle into the streaming
                    # mark: every row painted > 3 s into the watch is
                    # bb-stream. Watch until two samples were painted that
                    # late; if the gateway's samples stalled (their own `t`
                    # > 1 s apart), watch once more.
                    for attempt in range(2):
                        b.page.evaluate(_WATCH_MARKS)
                        b.page.wait_for_function(
                            "() => new Set(__bbMarks.filter(m => m[1] === 'tick'"
                            " && m[0] > __bbMarks[0][0] + 3000).map(m => m[0])).size >= 2",
                            timeout=15000)
                        log = b.page.evaluate("() => window.__bbMarks")
                        sampled = sorted({m[7] for m in log} - {None})
                        if max((y - x for x, y in zip(sampled, sampled[1:])), default=0) <= 1e6:
                            break
                    late = [cls for ms, key, _t, cls, *_r in log
                            if key in ("tick", "robot_position") and ms > log[0][0] + 3000]
                    self.assertTrue(late and all("bb-stream" in cls and "bb-fresh" not in cls
                                                 for cls in late), late)
                if robot_cls is NoRecordingMock:
                    self.assertIn("cannot record", gw.log())

    # -- layout --------------------------------------------------------- #
    def test_a_tree_swap_redraws_and_a_restart_does_not(self):
        with MockTarget(switch_every=20) as robot, \
             GatewayProbe(robot.port, poll_interval=0.05) as gw:
            b = self._open(gw, "recording")
            # Count redraws: a swap sends both a `segment` and a `layout` frame.
            # The tree shown is read in the same task, so no swap falls between.
            first = b.page.evaluate("() => { const draw = showTree; window.__drawn = [];"
                                    " window.showTree = (tree) => { __drawn.push(tree.id);"
                                    " draw(tree); }; return rootNodeSnapshot.data.id.split(':')[0]; }")
            b.page.wait_for_function(
                "[...document.querySelectorAll('#canvas g.node')].every("
                f"g => !g.__data__.data.id.startsWith('{first}:'))", timeout=10000)
            b.wait_for_nodes(1)                     # the new cards' enter transitions
            nodes = b.nodes()
            generations = {n["id"].split(":")[0] for n in nodes}
            debug = b.klein_debug()
            last = debug["segments"][-1]
            self.assertEqual(generations, {str(last["layoutId"])}, "old cards remain")
            drawn = b.page.evaluate("() => __drawn")
            self.assertTrue(drawn)
            self.assertEqual(len(drawn), len(set(drawn)), f"a tree was drawn twice: {drawn}")
            self.assertEqual(sorted(n["uid"] for n in nodes if n["uid"] is not None),
                             sorted(int(u) for u in debug["stateAtHead"]))
            snap = b.snapshot(painted=True)
            painted = state_matches(snap["state"], snap["displayed"], allow_was_vs_idle=False)
            self.assertTrue(painted, painted.detail)
            self._near_status(b, snap)              # the new tree's own status frames
            b.screenshot(SHOTS, "swapped_tree")

        with MockTarget() as robot, GatewayProbe(robot.port, poll_interval=0.05) as gw:
            b = self._open(gw, "recording")
            segments = len(b.klein_debug()["segments"])
            # Mark every card and blackboard group, and move the camera off its
            # reset position: a redraw would replace the board groups and reset
            # the camera (the cards' ids match, so d3 keeps them either way).
            b.page.evaluate("() => { document.querySelectorAll('#canvas g.node, .bb-group')"
                            ".forEach(el => { el.__kept = true; });"
                            " svg.call(zoomBehavior.transform,"
                            " d3.zoomIdentity.translate(30, 40).scale(0.5)); }")
            self.assertTrue(b.page.evaluate("() => document.querySelectorAll('.bb-group').length"))
            transform = b.page.evaluate(
                "() => document.querySelector('#canvas g.draw-group').getAttribute('transform')")
            robot.restart()
            b.page.wait_for_function(
                f"window.kleinDebug().segments.length > {segments}"
                " && window.kleinDebug().displaySource === 'recording'", timeout=15000)
            b.wait_connected()
            b.page.wait_for_timeout(600)
            kept = b.page.evaluate(
                "() => [...document.querySelectorAll('#canvas g.node')].map(g => g.__kept === true)")
            self.assertEqual(len(kept), 13)
            self.assertTrue(all(kept), "a same-tree restart redrew the cards")
            boards = b.page.evaluate(
                "() => [...document.querySelectorAll('.bb-group')].map(g => g.__kept === true)")
            self.assertTrue(boards and all(boards), "a same-tree restart reset the blackboards")
            self.assertEqual(transform, b.page.evaluate(
                "() => document.querySelector('#canvas g.draw-group').getAttribute('transform')"),
                "a same-tree restart reset the camera")
            snap = b.snapshot()
            painted = state_matches(snap["state"], snap["displayed"], allow_was_vs_idle=False)
            self.assertTrue(painted, painted.detail)

    def test_unfolded_cards_show_the_displayed_state_at_once(self):
        with self.shared() as gw:
            b = self._open(gw, "recording")
            root = b.page.evaluate("() => rootNodeSnapshot.data.uid")
            folded = b.snapshot(_CLICK.format(uid=root))     # fold: only the root stays
            hidden = {uid: s for uid, s in folded["displayed"].items() if uid != root}
            # The folded cards leave (their exit transitions end), the root stays.
            b.page.wait_for_function("document.querySelectorAll('#canvas g.node').length <= 1",
                                     timeout=5000)
            self.assertEqual(len(b.nodes()), 1)
            # Wait while folded until something inside it has changed.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                now = b.klein_debug()["displayed"]
                if any(now[str(uid)] != s for uid, s in hidden.items()):
                    break
                b.page.wait_for_timeout(100)
            else:
                self.fail("nothing changed inside the folded tree")
            snap = b.snapshot(_CLICK.format(uid=root))       # unfold, read in the same task
        self.assertEqual(len(snap["nodes"]), 13)
        painted = state_matches(snap["state"], snap["displayed"], allow_was_vs_idle=False)
        self.assertTrue(painted, painted.detail)
        self.assertTrue(any(s != {"status": "IDLE", "from": None}
                            for uid, s in snap["state"].items() if uid != root),
                        "every revealed card is plain IDLE: nothing to tell apart")

    # -- blackboard change marks ---------------------------------------- #
    def _watch_marks(self, b, gw, seconds):
        """The watched MainTree rows as each blackboard paint left them, for
        ``seconds`` and until mission_phase has changed at a moment the Model
        says it is not streaming (at a fast poll the mission runs fast, and
        a stretch of its changes can all be streaming):
        ``({key: [(ms, text, classes, background, seg, tChange, frames' tLast)]}, Model)``."""
        b.page.evaluate(_WATCH_MARKS)
        deadline = time.monotonic() + seconds + 20
        while time.monotonic() < deadline:
            b.page.wait_for_timeout(250)
            log = b.page.evaluate("() => window.__bbMarks")
            if not log or log[-1][0] - log[0][0] < seconds * 1000:
                continue
            model = Model(gw.require_debug_state())
            rows = {}
            for ms, key, *rest in log:
                rows.setdefault(key, []).append((ms, *rest))
            if any(recent < BB_STREAMING for *_x, recent in self._phase_changes(rows, model)):
                break
        return rows, model

    @staticmethod
    def _phase_changes(rows, model):
        """mission_phase's painted changes as ``(classes, tChange, recent)``,
        with the Model's count of changes in the 3 s up to it."""
        phase = rows.get("mission_phase", [])
        return [(cls, t, model.bb_change(seg, "MainTree", "mission_phase", t)[1])
                for (_m, a, *_r), (_n, text, cls, _bg, seg, t, _s) in zip(phase, phase[1:])
                if a != text and t is not None]

    def test_blackboard_changes_are_marked_softly_live(self):
        """Live: a key that changes at every sample (tick, robot_position)
        settles into the steady streaming mark within 3 s and never fades
        again; mission_phase fades in on each change the Model says is not
        streaming, and has the streaming mark on the others; no mark fills the
        row. With reduced motion nothing fades: a change is marked statically
        instead."""
        with self.shared() as gw:
            b = self._open(gw, "recording")
            b.page.wait_for_function("bbGroupEls.MainTree && bbGroupEls.MainTree.rows.mission_phase")
            for motion, fade in (("no-preference", "bb-fresh"), ("reduce", "bb-mark")):
                with self.subTest(motion=motion):
                    b.page.emulate_media(reduced_motion=motion)
                    rows, model = self._watch_marks(b, gw, 3.5)
                    start = rows["tick"][0][0]
                    for key in ("tick", "robot_position"):
                        after = [cls for ms, _t, cls, *_r in rows[key] if ms > start + 3000]
                        self.assertTrue(after and all("bb-stream" in cls for cls in after), key)
                        self.assertFalse(any("bb-fresh" in cls or "bb-mark" in cls
                                             for cls in after), key)
                    changes = self._phase_changes(rows, model)
                    self.assertTrue(any(recent < BB_STREAMING for *_x, recent in changes),
                                    changes)
                    for cls, t, recent in changes:
                        if recent < BB_STREAMING:
                            self.assertIn(fade, cls, (t, recent))
                        else:
                            self.assertIn("bb-stream", cls, (t, recent))
                            self.assertNotIn(fade, cls, (t, recent))
                    if motion == "reduce":
                        self.assertFalse(any("bb-fresh" in r[2] for r in sum(rows.values(), [])))
                    self.assertEqual({r[3] for r in sum(rows.values(), [])},
                                     {"rgba(0, 0, 0, 0)"})
            b.page.emulate_media(reduced_motion="no-preference")
            if SHOTS_ON:
                b.page.wait_for_function("!document.querySelector('#bb-panel .bb-fresh')",
                                         timeout=15000)
                b.screenshot("blackboard", "streaming")
                b.page.wait_for_function(
                    "bbGroupEls.MainTree.rows.mission_phase.row.classList.contains('bb-fresh')",
                    timeout=15000)
                b.screenshot("blackboard", "fresh_live")

    def test_a_change_in_a_closed_board_does_not_fade_on_opening(self):
        """A key that changed while its board was closed is old news by the
        time the board opens (2 s on): nothing fades then."""
        with self.shared() as gw:
            b = self._open(gw, "recording")
            page = b.page
            page.wait_for_function("bbGroupEls.MainTree && bbGroupEls.MainTree.rows.mission_phase")
            page.evaluate("() => { const g = bbGroupEls.MainTree;"
                          " if (!g.body.hidden) g.toggle.click();"
                          " window.__closedAt = Object.fromEntries(Object.entries(g.rows)"
                          ".map(([k, r]) => [k, r.tChange])); }")
            page.wait_for_function(
                "Object.entries(bbGroupEls.MainTree.rows).some(([k, r]) =>"
                " r.tChange !== window.__closedAt[k] && !r.row.classList.contains('bb-stream'))",
                timeout=20000)
            page.wait_for_timeout(2000)
            fading = page.evaluate("() => { const g = bbGroupEls.MainTree; g.toggle.click();"
                                   " return [...g.body.querySelectorAll('.bb-fresh')]"
                                   ".map(r => r.firstChild.textContent); }")
            self.assertEqual(fading, [])

    def test_paused_on_the_ruler_the_marks_are_the_models_fresh_keys(self):
        """Paused by the Timeline ruler at 20 moments of a run long enough for
        keys to stream, the static marks are exactly the keys the Model calls
        fresh and nothing fades. Half the moments are just after a change of a
        key with exactly BB_STREAMING changes in the last 3 s: streaming, so
        unmarked. The writers of the marked keys hold the writer pulse, the
        Fallback folded so that the ones it hides pulse on its card."""
        with self.shared() as gw:
            b = self._open(gw, "recording")
            page = b.page
            if page.get_attribute("#drawer-tab-timeline", "aria-selected") != "true":
                page.click("#drawer-tab-timeline")
            b.go_live()
            page.wait_for_function("KleinTimeline.debug().t1 - recordingStore.recording"
                                   ".segments.at(-1).bb.tStart > 8e6", timeout=15000)
            model = Model(gw.require_debug_state())
            e = model.state["blackboard"][-1]
            tl = page.evaluate("() => KleinTimeline.debug()")
            lo, hi = max(tl["t0"], e["t_start"]), model.state["head"] - 100_000
            edges = sorted({c[0] + 200_000 for c in e["changes"] if lo < c[0] + 200_000 < hi
                            and model.bb_change(e["seg"], c[1], c[2], c[0] + 200_000)[1]
                            == BB_STREAMING})
            self.assertTrue(edges, "no change with exactly BB_STREAMING recent ones")
            rng = random.Random(5)
            times = rng.sample(edges, min(10, len(edges)))
            times += [rng.uniform(lo, hi) for _ in range(20 - len(times))]
            ruler = page.locator("#tl-ruler").bounding_box()
            # The Fallback folded: the pulses of the writers it hides
            # (DoorClosed, PickLock) are on its card.
            self.fold(4)
            roles = links(page.evaluate("() => rootNodeSnapshot.data"), folded=(4,))[0]
            pulsed = 0
            for t in times:
                tl = page.evaluate("() => KleinTimeline.debug()")
                page.mouse.click(tl["left"] + (t - tl["t0"]) * tl["width"] / tl["span"],
                                 ruler["y"] + ruler["height"] / 2)
                b.next_frame()
                v = page.evaluate(_PAUSED_MARKS)
                with self.subTest(t=v["t"]):
                    self.assertEqual((v["mode"], v["fading"]), ("paused", 0))
                    fresh = model.bb_fresh(v["seg"], v["t"])
                    self.assertEqual({tuple(m) for m in v["marked"]}, fresh)
                    writers = {uid for ref in fresh for uid, role in roles.get(ref, {}).items()
                               if role == "write"}
                    self.assertEqual([set(v["pulses"][0]), v["pulses"][1]], [writers, []])
                    pulsed += bool(writers)
            self.assertTrue(pulsed, "no moment with a writer pulse")
            self.fold(4)
            b.go_live()

    # -- hover links ----------------------------------------------------- #
    def test_hovering_links_keys_and_cards(self):
        """Every key row and card; then again with DoorClosed folded inside
        the folded Fallback: PickLock (under both) is outlined on the
        Fallback, the outermost, which also hides a reader (IsDoorClosed)
        and a writer (DoorClosed) of door_open. A hovered card folded by its
        click marks the keys it now hides too. A row opens a breakdown only
        for a value with fields: a short one opens nothing."""
        with self.shared() as gw:
            b = self._open(gw, "recording")
            b.page.wait_for_function("bbGroupEls['DoorClosed::7']"
                                     " && bbGroupEls['DoorClosed::7'].rows.lock_status")
            layout = b.page.evaluate("() => rootNodeSnapshot.data")
            self.assert_hover_links(layout)
            roles = links(layout, folded=(7, 4))[0]
            self.assertEqual(roles[("DoorClosed::7", "lock_status")], {4: "write"})
            self.assertEqual(roles[("MainTree", "door_open")], {2: "write", 4: "write"})
            self.assert_hover_links(layout, folded=(7, 4))
            page = b.page
            page.evaluate(CARD_EVENT, [4, "mouseenter"])
            self.fold(4)                                    # still hovered
            linked = {tuple(r) for r in page.evaluate(LINKS)["linked"]}
            self.assertEqual(linked, links(layout, folded=(4,))[1][4])
            self.fold(4)
            page.evaluate(CARD_EVENT, [4, "mouseleave"])
            for key, fields in (("door_open", False), ("target_pose", True)):
                with self.subTest(breakdown=key):
                    row = page.evaluate_handle(
                        "(k) => bbGroupEls.MainTree.rows[k].row", key).as_element()
                    row.click()
                    got = page.evaluate("(k) => { const d = bbGroupEls.MainTree.rows[k].detail;"
                                        " return [d.hidden, d.children.length > 0]; }",
                                        key)
                    self.assertEqual(got, [not fields, fields])
                    row.click()

    # -- pulse, outage --------------------------------------------------- #
    def test_the_pulse_freezes_and_an_outage_keeps_the_last_state(self):
        with MockTarget() as robot:
            with GatewayProbe(robot.port) as gw:
                b = self._open(gw, "recording")
                b.page.wait_for_function(
                    "document.querySelectorAll('#canvas .node-rect.running').length > 0")
                live = b.page.evaluate(_PULSE)
                self.assertTrue(live["animations"])
                self.assertEqual(set(live["animations"]), {"klein-pulse"})
                self.assertFalse(b.page.evaluate(
                    "() => document.body.classList.contains('viewing-past')"))

                past = b.page.evaluate("() => { document.body.classList.add('viewing-past');"
                                       f" return ({_PULSE})(); }}")
                self.assertEqual(set(past["animations"]), {"none"})
                b.page.evaluate("() => document.body.classList.remove('viewing-past')")

                # Record every state painted from here on.
                b.page.evaluate("() => { const paint = paintStatus; window.__painted = [];"
                                " window.paintStatus = (m) => {"
                                " __painted.push(JSON.stringify(m)); paint(m); }; }")
                robot.stop()
                # The status poll times out (2 s): the robot is marked gone and
                # the segment ends; nothing more arrives after that.
                b.page.wait_for_function(
                    "document.body.classList.contains('telemetry-stale')"
                    " && window.kleinDebug().segments.at(-1).tEnd !== null", timeout=15000)
                mark = b.page.evaluate("() => __painted.length")
                b.page.wait_for_timeout(300)        # a while into the outage
                stale = b.page.evaluate(_PULSE)
                self.assertTrue(stale["stale"])
                self.assertEqual(set(stale["animations"]) - {"none"}, set(), "still pulsing")
                # No step back: the last state painted stays painted, and it is
                # the recording's head.
                painted = b.page.evaluate(f"() => __painted.slice({max(mark - 1, 0)})")
                self.assertTrue(painted)
                self.assertEqual(len(set(painted)), 1, "the display changed during the outage")
                snap = b.snapshot()
                self.assertEqual(snap["displaySource"], "recording")
                head = {int(k): v for k, v in b.klein_debug()["stateAtHead"].items()}
                self.assertEqual(snap["displayed"], head)
                same = state_matches(snap["state"], head, allow_was_vs_idle=False)
                self.assertTrue(same, same.detail)
                # The RUNNING cards keep their colour, just not the pulse.
                self.assertEqual(len(stale["animations"]),
                                 sum(e["status"] == "RUNNING" for e in head.values()),
                                 "the RUNNING cards lost their colour")
                b.screenshot(SHOTS, "disconnected")


if __name__ == "__main__":
    unittest.main()
