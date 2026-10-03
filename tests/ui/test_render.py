"""One render path: the dashboard paints what the cursor shows.

While klein records, the cards show the browser's recording mirror at the
cursor (live = the head); without a recording (``--record-buffer 0``, or a
robot that answers ``r`` with an error) they show the latest status frames,
the chip says why, and Save is greyed out. Checked in a real browser
(Playwright) against the mock; skipped when Playwright is missing.
"""
import contextlib
import random
import time
import unittest

from tests.harness.oracles import state_in_frames, state_matches
from tests.harness.probes import GatewayProbe
from tests.harness.targets import PYTHON, MockTarget, _ready_robot, launch
from tests.ui import DashboardCase


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

# The chip's state and the Save button, read in one task.
_CHIP_AND_SAVE = """() => { const save = document.getElementById('drawer-save');
  return { state: document.getElementById('drawer-chip').dataset.state,
           disabled: save.disabled, title: save.title }; }"""

# Click a card through d3's handler and read the cards back in the same task, so
# no status frame or animation frame can repaint in between.
_CLICK = ("const g = [...document.querySelectorAll('#canvas g.node')]"
          ".find(n => n.__data__.data.uid === {uid});"
          "g.dispatchEvent(new MouseEvent('click', {{bubbles: true}}))")


class RenderTest(DashboardCase):
    """The live checks share one recording mock and gateway (``shared()``, fast
    poll); the checks that need another set-up start their own."""

    GATEWAY = {"poll_interval": 0.05}
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
        """Without a recording: the cards are the last status frame (or, one
        animation frame behind, the one before it)."""
        for _ in range(samples):
            snap = b.snapshot()
            self.assertEqual(snap["displaySource"], "frames")
            painted = state_matches(snap["state"], snap["displayed"], allow_was_vs_idle=False)
            self.assertTrue(painted, painted.detail)
            n = snap["statusCount"]
            frames = b.status_frames(n - 1, n)
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
                snap = b.snapshot()
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
        the status frames, the chip says why, and Save is greyed out saying
        the same."""
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
                    b.screenshot(SHOTS, name)
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
            snap = b.snapshot()
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
