"""The Timeline tab.

``TimelineTest`` runs in a real browser (Playwright) against the mock robot,
with the gateway's own model (``GET /debug/state``, ``--debug``) as the
reference:

* dragging the playhead to 5 times shows, on every card, the model's state
  at each time;
* |◀ ▶| visit consecutive seqs;
* folding a card folds its rows and a row's chevron folds the card;
  Alt-click folds every other subtree;
* a Log row click moves the playhead, and dragging the playhead scrolls the
  Log to its row;
* F with the drawer open scrolls the timeline to the running rows;
* the filter keeps the rows the Log keeps;
* zooming keeps the playhead in place, a paused window rebuilds nothing,
  and a drag on the lanes pans.

The robot keeps running: windows are read well behind the head (1 s), where
the browser's mirror and the gateway's dump read after it agree.

``TimelineAcrossASwapTest``: across a tree swap the other tree's stretch is a
band, the playhead dragged into it shows that tree's rows, and playing from
the past reaches the head and goes live.

``TimelineReplayTest``: on real t11 timing (the mock replaying the t11
FileLogger2 fixture), every row's bars, outcome caps and single marks sit
where ``intervals()`` puts them, section headers carry their FAILURE marks
and tint, a folded row marks the changes it hides, and a nested section's
header sticks only inside its section.

Skipped when Playwright is missing.
"""
import signal
import time
import unittest
from pathlib import Path

from tests.harness.model import Model, fmt_time, names
from tests.harness.probes import BACK_TO_LIVE
from tests.harness.targets import ReplayTarget
from tests.ui import DashboardCase

SHOTS = "timeline"
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "t11_filelogger2.btlog"
WIDTH, HEIGHT = 1400, 900
PICKLOCK = 11
DOOR_CLOSED = 7
SETTLED_US = 1_000_000          # how far behind the head the browser surely has everything
RUNNING, OUTCOMES = 1, (2, 3, 4)
MARK_SETTLED_US = 300_000       # on t11: how long until a single mark is surely drawn

# The cards, the banner and the Timeline's cursor, read in one task.
_READ = """() => ({
  cards: [...document.querySelectorAll('#canvas g.node')].map(g => [
    g.__data__.data.uid, g.querySelector('.node-status-text').textContent]),
  past: document.body.classList.contains('viewing-past'),
  banner: (v => v ? v.querySelector('.banner-text').textContent : null)(
          document.querySelector('#banner-stack [data-key="viewing"]')),
  tl: KleinTimeline.debug(),
})"""

# The rows listed: [id, uid, header, context, chevron text] in order.
_ROWS = """() => [...document.querySelectorAll('#tl-rows .tl-row')].map(r => [
  r.dataset.id, r.dataset.uid === undefined ? null : Number(r.dataset.uid),
  r.classList.contains('tl-header'), r.classList.contains('context'),
  (r.querySelector('button.tl-chevron') || {}).textContent || ''])"""

# Every row's shapes on screen ([left, right, class] by kind) and its background.
_ROW_SHAPES = """() => [...document.querySelectorAll('#tl-rows .tl-row')].map(r => {
  const shapes = (sel) => [...r.querySelectorAll(sel)].map(e => {
    const b = e.getBoundingClientRect(); return [b.left, b.right, e.className]; });
  return { uid: r.dataset.uid === undefined ? null : Number(r.dataset.uid), name: r.dataset.id,
           header: r.classList.contains('tl-header'), bg: getComputedStyle(r).backgroundColor,
           bars: shapes('.tl-bar'), caps: shapes('.tl-cap'), marks: shapes('.tl-mark'),
           subs: shapes('.tl-sub') };
})"""


def shot(browser, name):
    """A screenshot with the whole tree in view (R) and every card settled."""
    browser.page.evaluate("() => document.activeElement && document.activeElement.blur()")
    browser.page.keyboard.press("r")
    browser.wait_for_nodes(1)
    browser.screenshot(SHOTS, name)
    browser.wait_for_nodes(1)


def expected_shapes(ivs):
    """What a row draws for intervals ``ivs``: RUNNING bars, outcome caps
    ending a RUNNING bar, single marks (an outcome not after RUNNING), and
    the FAILURE starts its section header marks."""
    caps, marks, failures = [], [], []
    for i, (a, b, v) in enumerate(ivs):
        after = ivs[i + 1][2] if i + 1 < len(ivs) else None
        if v == RUNNING and after in OUTCOMES:
            caps.append((b, after))
        if i and v in OUTCOMES and ivs[i - 1][2] != RUNNING:
            marks.append((a, v))
        if i and v == 3:
            failures.append(a)
    bars = [(a, b) for a, b, v in ivs if v == RUNNING]
    return bars, caps, marks, failures


class TimelineTest(DashboardCase):
    GATEWAY = {"debug": True, "poll_interval": 0.05}
    VIEWPORT = (WIDTH, HEIGHT)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with cls.gw.watch() as ws:
            cls.layout = ws.wait_for(lambda w: w.of_type("layout"))[0]["data"]
        b = cls.browser
        b.wait_connected()
        b.page.click("#drawer-tab-timeline")
        # A whole run of PickLock's five attempts, settled behind the head.
        deadline = time.monotonic() + 30
        while cls.attempts() is None:
            assert time.monotonic() < deadline, "no full PickLock run recorded"
            b.page.wait_for_timeout(200)
        shot(b, "live")

    @classmethod
    def attempts(cls):
        """(model, [5 PickLock RUNNING intervals]) of its last run fully in the
        live window and settled, else None."""
        tl = cls.page.evaluate("() => KleinTimeline.debug()")
        model = Model(cls.gw.require_debug_state())
        seg = max(model.segments)
        t1 = min(tl["t1"], model.state["head"] - SETTLED_US)
        ivs = model.intervals(seg, PICKLOCK, tl["t0"], t1)
        runs = [i for i, iv in enumerate(ivs) if iv[2] == 1 and i + 1 < len(ivs)]
        for k in range(len(runs) - 5, -1, -1):
            group = runs[k:k + 5]
            ends = [ivs[i + 1][2] for i in group]
            if group[0] > 0 and ends == [3, 3, 3, 3, 2]:
                return model, [ivs[i] for i in group]
        return None

    def setUp(self):
        self.browser.go_live()
        if self.page.get_attribute("#drawer-tab-timeline", "aria-selected") != "true":
            self.page.click("#drawer-tab-timeline")
        self.page.fill("#drawer-filter", "")
        self.frame()

    # -- helpers ---------------------------------------------------------- #
    def read(self):
        self.frame()
        return self.page.evaluate(_READ)

    def x_of(self, t, tl=None):
        tl = tl or self.page.evaluate("() => KleinTimeline.debug()")
        return tl["left"] + (t - tl["t0"]) * tl["width"] / tl["span"]

    def ruler_y(self):
        box = self.page.locator("#tl-ruler").bounding_box()
        return box["y"] + box["height"] / 2

    def click_time(self, t):
        """Press the ruler at robot time t (pausing there)."""
        self.page.mouse.click(self.x_of(t), self.ruler_y())
        self.frame()

    def rows(self):
        return self.page.evaluate(_ROWS)

    def chevron(self, uid, **kwargs):
        self.page.click(f'#tl-rows .tl-row[data-uid="{uid}"] button.tl-chevron', **kwargs)
        self.frame()

    def card_uids(self):
        self.browser.wait_for_nodes(1)
        return sorted(n["uid"] for n in self.browser.nodes())

    def assert_cards(self, view, expected, what):
        cards = dict(view["cards"])
        self.assertEqual(cards, {uid: expected[uid] for uid in cards}, what)

    # ------------------------------------------------------------------ #
    def test_dragging_the_playhead_shows_state_at_t(self):
        tl = self.page.evaluate("() => KleinTimeline.debug()")
        head = self.page.evaluate("() => kleinDebug().head")
        t_end = min(tl["t1"], head) - SETTLED_US
        targets = [t_end - f * (t_end - tl["t0"]) for f in (0.9, 0.7, 0.5, 0.3, 0.1)]
        mouse = self.page.mouse
        mouse.move(self.x_of(targets[0], tl), self.ruler_y())
        mouse.down()
        seen = []
        for t in targets:
            x = self.x_of(t, tl)
            mouse.move(x, self.ruler_y(), steps=4)
            view = self.read()
            seen.append((x, view))
        mouse.up()
        model = Model(self.gw.require_debug_state())
        for x, view in seen:
            t = view["tl"]["pos"]["t"]
            self.assertAlmostEqual(self.x_of(t, tl), x, delta=1, msg="the playhead follows the pointer")
            self.assertTrue(view["past"])
            self.assertEqual(view["banner"], f"Viewing t = {fmt_time(t)}")
            self.assert_cards(view, model.labels(*model.seq_at(t), dict(view["cards"])), f"at {t}")
        self.assertEqual(len({v["tl"]["pos"]["seq"] for _x, v in seen}), 5, "five moments")

    def test_stepping_visits_consecutive_seqs(self):
        self.click_time(self.page.evaluate("() => kleinDebug().head") - 2 * SETTLED_US)
        start = self.read()["tl"]["pos"]
        model = Model(self.gw.require_debug_state())
        seqs = [start["seq"]]
        for button, n in (("#tl-next", 4), ("#tl-prev", 6)):
            for _ in range(n):
                self.page.click(button)
                view = self.read()
                seqs.append(view["tl"]["pos"]["seq"])
                cards = dict(view["cards"])
                self.assert_cards(view, model.labels(view["tl"]["pos"]["seg"], seqs[-1], cards),
                                  f"step to {seqs[-1]}")
        s = start["seq"]
        self.assertEqual(seqs, [s, s + 1, s + 2, s + 3, s + 4, s + 3, s + 2, s + 1, s, s - 1, s - 2])
        self.assertTrue(view["past"])

    def test_fold_is_shared_with_the_tree(self):
        inside = {8, 9, 10, 11, 12}
        try:
            # A card folds its rows...
            self.browser.click(uid=DOOR_CLOSED)
            self.frame()
            rows = self.rows()
            listed = {uid for _id, uid, *_ in rows}
            self.assertFalse(inside & listed)
            door = next(r for r in rows if r[1] == DOOR_CLOSED)
            self.assertEqual(door[4], "▸")
            self.assertEqual(self.page.text_content(
                f'#tl-rows .tl-row[data-uid="{DOOR_CLOSED}"] .tl-meta'), "5 nodes")
            shot(self.browser, "folded_section")
            # ...and a chevron folds the card: unfold, then fold the Inverter.
            self.chevron(DOOR_CLOSED)
            self.assertTrue(inside <= set(self.card_uids()))
            self.assertTrue(inside <= {uid for _id, uid, *_ in self.rows()})
            self.chevron(5)
            self.assertNotIn(6, self.card_uids())
            inverter = self.page.text_content('#tl-rows .tl-row[data-uid="5"] .tl-meta')
            self.assertEqual(inverter, "+1")
            self.chevron(5)
            self.assertIn(6, self.card_uids())
            # Alt-click: every other subtree folds (the main tree's: DoorClosed)...
            self.chevron(1, modifiers=["Alt"])
            self.assertFalse(inside & set(self.card_uids()))
            self.assertIn(13, self.card_uids())
            # ...and on a folded subtree, it opens with its ancestors.
            self.chevron(DOOR_CLOSED, modifiers=["Alt"])
            self.assertTrue(inside <= set(self.card_uids()))
        finally:                        # leave the whole tree unfolded for the other tests
            listed = {uid for _id, uid, *_ in self.rows()}
            if PICKLOCK not in listed:
                self.chevron(DOOR_CLOSED)
            if 6 not in listed:
                self.chevron(5)

    def test_log_click_moves_the_playhead_and_dragging_scrolls_the_log(self):
        # A Log row click...
        self.page.click("#drawer-tab-log")
        self.frame()
        self.page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 100; }")
        self.frame()
        # Found and clicked in one task: the rows in view are reused as
        # records arrive, so a row found first may be another record when clicked.
        seg, seq = self.page.evaluate("""() => {
          const body = document.getElementById('drawer-body').getBoundingClientRect();
          const rows = [...document.querySelectorAll('#log-rows .log-row:not([hidden])')]
            .filter(r => { const b = r.getBoundingClientRect();
                           return b.top >= body.top + 20 && b.bottom <= body.bottom; })
            .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
          const row = rows[Math.min(8, rows.length - 1)];
          row.click();
          return [Number(row.dataset.seg), Number(row.dataset.seq)];
        }""")
        model = Model(self.gw.require_debug_state())
        t = model.record(seg, seq)[0]
        # ...puts the playhead on its time.
        self.page.click("#drawer-tab-timeline")
        view = self.read()
        self.assertEqual(view["tl"]["pos"]["t"], t)
        self.assertEqual(view["tl"]["pos"]["seq"], seq + 1)
        box = self.page.locator("#tl-playhead").bounding_box()
        self.assertFalse(self.page.locator("#tl-playhead").is_hidden())
        self.assertAlmostEqual(box["x"] + box["width"] / 2, self.x_of(t), delta=1)
        # Dragging the playhead 1 s on scrolls the Log to that moment's row.
        mouse = self.page.mouse
        mouse.move(self.x_of(t), self.ruler_y())
        mouse.down()
        mouse.move(self.x_of(t + 1_000_000), self.ruler_y(), steps=6)
        mouse.up()
        pos = self.read()["tl"]["pos"]
        self.page.click("#drawer-tab-log")
        self.frame()
        sel = self.page.evaluate("""() => {
          const r = document.querySelector('#log-rows .log-row.selected:not([hidden])');
          if (!r) return null;
          const b = r.getBoundingClientRect(), body = document.getElementById('drawer-body')
            .getBoundingClientRect();
          return [Number(r.dataset.seg), Number(r.dataset.seq), b.top >= body.top + 20,
                  b.bottom <= body.bottom];
        }""")
        self.assertEqual(sel, [pos["seg"], pos["seq"] - 1, True, True])
        self.page.click("#drawer-tab-timeline")

    def test_f_scrolls_the_timeline_to_the_running_rows(self):
        _model, runs = self.attempts()
        self.click_time((runs[2][0] + runs[2][1]) / 2)          # PickLock running
        body = "document.getElementById('drawer-body')"
        self.page.evaluate(f"() => {{ {body}.scrollTop = 0; document.activeElement.blur(); }}")
        self.frame()
        self.page.keyboard.press("f")
        self.frame()
        box = self.page.evaluate(f"""() => {{
          const r = document.querySelector('#tl-rows .tl-row[data-uid="{PICKLOCK}"]')
            .getBoundingClientRect();
          const b = {body}.getBoundingClientRect();
          const head = document.querySelector('.tl-head').getBoundingClientRect();
          return {{ top: r.top, bottom: r.bottom, below: head.bottom, end: b.bottom,
                    scrolled: {body}.scrollTop }};
        }}""")
        self.assertGreater(box["scrolled"], 0)
        self.assertGreaterEqual(box["top"], box["below"] + 24 - 1, "under the sticky header")
        self.assertLessEqual(box["bottom"], box["end"])

    def test_the_filter_keeps_the_logs_rows(self):
        table = names(self.layout)
        for needle in ("pick", "DoorClosed", "door", "zzz"):
            self.page.fill("#drawer-filter", needle)
            self.frame()
            rows = self.rows()
            kept = {uid for _id, uid, _h, context, _c in rows if not context}
            expected = {uid for uid, (name, subtree) in table.items()
                        if needle.lower() in name.lower() or needle.lower() in subtree.lower()}
            self.assertEqual(kept, expected, needle)
            if needle == "pick":
                self.assertEqual([(uid, context) for _id, uid, _h, context, _c in rows],
                                 [(1, True), (DOOR_CLOSED, True), (PICKLOCK, False)])
                shot(self.browser, "filtered")
        self.assertTrue(self.page.is_visible("#tl-empty"))
        self.assertEqual(self.page.text_content("#tl-empty"), "No nodes match “zzz”.")

    def test_zoom_keeps_the_playhead_and_pans(self):
        _model, runs = self.attempts()
        self.click_time(runs[2][0])
        # 30 s → 20 → 10 s: the 30 s default is longer than the mock's
        # recording, so the window starts at its oldest record until here.
        self.page.click("#tl-zoom-in")
        self.page.click("#tl-zoom-in")
        before = self.page.evaluate("() => KleinTimeline.debug()")
        self.page.click("#tl-zoom-in")
        self.page.click("#tl-zoom-in")
        tl = self.page.evaluate("() => KleinTimeline.debug()")
        self.assertEqual(tl["span"], 2_000_000)
        self.assertEqual(self.page.text_content("#tl-window"), "2 s window")
        # The playhead keeps its place on screen.
        x = lambda d: d["left"] + (d["pos"]["t"] - d["t0"]) * d["width"] / d["span"]
        self.assertAlmostEqual(x(tl), x(before), delta=1)
        # Paused on a still window behind the head, the robot's new records
        # (past the window's right edge) rebuild no track.
        self.assertLess(tl["t1"], self.page.evaluate("() => kleinDebug().head"))
        rebuilt = self.page.evaluate("""() => new Promise(done => {
          let n = 0;
          const obs = new MutationObserver(ms => { n += ms.length; });
          obs.observe(document.getElementById('tl-rows'), { childList: true, subtree: true });
          setTimeout(() => { obs.disconnect(); done(n); }, 800);
        })""")
        self.assertEqual(rebuilt, 0, "track rebuilds over 0.8 s while paused")
        # Pan with a drag on the lanes: the window moves, the cursor does not.
        lanes = self.page.locator("#tl-lanes").bounding_box()
        y = lanes["y"] + 40
        self.browser.drag((lanes["x"] + 700, y), (lanes["x"] + 600, y))
        panned = self.page.evaluate("() => KleinTimeline.debug()")
        self.assertAlmostEqual(panned["t0"] - tl["t0"], 100 * tl["span"] / tl["width"],
                               delta=tl["span"] / tl["width"])
        self.assertEqual(panned["pos"], tl["pos"])
        self.browser.drag((lanes["x"] + 600, y), (lanes["x"] + 700, y))
        for _ in range(4):
            self.page.click("#tl-zoom-out")
        self.assertEqual(self.page.evaluate("() => KleinTimeline.debug().span"), 30_000_000)


class TimelineAcrossASwapTest(DashboardCase):
    """After a tree swap (mock ``--switch-every``): the earlier tree's stretch
    is a band naming it, and the playhead dragged into it switches the rows
    (and the canvas) to that tree; back to live shows the new tree again.
    Then playing reaches the head (here, so the frozen robot's outage can't
    disturb TimelineTest)."""

    POLL = 0.05
    ROBOT = {"switch_every": 40}
    GATEWAY = {"debug": True, "poll_interval": POLL}
    VIEWPORT = (WIDTH, HEIGHT)
    OPEN = 5

    def test_rows_follow_the_tree_at_the_playhead(self):
        b, page = self.browser, self.page
        page.click("#drawer-tab-timeline")
        page.wait_for_function("kleinDebug().segments && kleinDebug().segments.length >= 2"
                               " && kleinDebug().head - kleinDebug().segments.at(-1).tBegin"
                               " > 1000000", timeout=20000)
        self.frame()
        bands = page.evaluate("() => [...document.querySelectorAll('#tl-bands .tl-band')]"
                              ".map(b => b.textContent)")
        self.assertTrue(any(text.startswith("tree: ") for text in bands), bands)
        shot(b, "after_tree_swap")
        # Drag into the previous tree's stretch: its rows (and cards) show.
        dbg = page.evaluate("() => kleinDebug()")
        prev, after = dbg["segments"][-2], dbg["segments"][-1]
        roots = page.evaluate(
            "(ids) => ids.map(id => recordingStore.recording.segment(id)"
            ".layout.root_tree_id)", [prev["id"], after["id"]])
        self.assertNotEqual(roots[0], roots[1])
        tl = page.evaluate("() => KleinTimeline.debug()")
        target = max(prev["tStart"], tl["t0"]) + 200_000
        ruler = page.locator("#tl-ruler").bounding_box()
        x = tl["left"] + (target - tl["t0"]) * tl["width"] / tl["span"]
        page.mouse.click(x, ruler["y"] + 10)
        page.wait_for_function(f"KleinTimeline.debug().pos.seg === {prev['id']}")
        b.wait_for_nodes(1)
        self.frame()
        self.assertEqual(page.text_content("#tl-rows .tl-name"), roots[0])
        cards = page.evaluate("() => document.querySelectorAll('#canvas g.node').length")
        self.assertGreater(cards, 0)
        shot(b, "paused_in_the_previous_tree")
        page.click(BACK_TO_LIVE)
        page.wait_for_function("!document.body.classList.contains('viewing-past')")
        # Live again: the rows are the head segment's tree (which may have
        # swapped again meanwhile).
        page.wait_for_function("document.querySelector('#tl-rows .tl-name').textContent"
                               " === recordingStore.recording.segments.at(-1)"
                               ".layout.root_tree_id")
        self.play_to_the_head()

    def play_to_the_head(self):
        """Playing from 0.6 s back reaches the head and goes live. At 1x it
        keeps its distance from a head that moves at 1x too, so the robot is
        frozen (SIGSTOP) first: the head stops, as when the robot goes away."""
        page, robot = self.page, self.robot
        robot.proc.send_signal(signal.SIGSTOP)
        try:
            self.wait_for_the_last_drain()
            tl = page.evaluate("() => KleinTimeline.debug()")
            head = page.evaluate("() => kleinDebug().head")
            ruler = page.locator("#tl-ruler").bounding_box()
            x = tl["left"] + (head - 600_000 - tl["t0"]) * tl["width"] / tl["span"]
            page.mouse.click(x, ruler["y"] + 10)
            self.frame()
            self.assertTrue(page.evaluate("() => document.body.classList.contains('viewing-past')"))
            started = time.monotonic()
            page.click("#tl-play")
            self.frame()
            self.assertEqual(page.get_attribute("#tl-play", "aria-label"), "Pause")
            self.assertTrue(page.evaluate("() => document.body.classList.contains('viewing-past')"),
                            "playing is still the past")
            page.wait_for_function("!document.body.classList.contains('viewing-past')",
                                   timeout=3000)
            took = time.monotonic() - started
            self.frame()
            self.assertTrue(page.evaluate("() => KleinTimeline.debug().pos.live"))
            self.assertTrue(page.is_disabled("#tl-play"))
            self.assertFalse(page.evaluate("() => banners.has('viewing')"))
            self.assertGreater(took, 0.4, "played at 1x, not jumped")
        finally:
            robot.proc.send_signal(signal.SIGCONT)

    def wait_for_the_last_drain(self):
        """Once the robot is frozen: until the gateway's head stays put and
        the browser holds it (a reply in flight still lands)."""
        head = None
        while True:
            now = self.gw.require_debug_state()["head"]
            self.page.wait_for_function("(h) => kleinDebug().head === h", arg=now, timeout=5000)
            if now == head:
                return
            head = now
            time.sleep(self.POLL)


class TimelineReplayTest(DashboardCase):
    """t11's real timing: µs bursts, retries inside one poll, and nodes that
    finish in the tick they start (IsDoorClosed: single marks; the plain mock
    has none). One gateway, one browser, paused near the head."""

    ROBOT_CLASS = ReplayTarget
    ROBOT = {"btlog_path": FIXTURE}
    GATEWAY = {"debug": True, "poll_interval": 0.05}
    VIEWPORT = (WIDTH, HEIGHT)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        page = cls.page
        page.click("#drawer-tab-timeline")
        # PickLock's caps (its last is ~3.2 s into a mission) and a single
        # mark recorded: arming can land after the first mission's opening
        # marks, and the next mission starts ~5.7 s in.
        page.wait_for_function("kleinDebug().head - kleinDebug().segments[0].tBegin > 3600000"
                               " && document.querySelector('#tl-rows .tl-mark')",
                               timeout=15000, polling=100)
        # That mark settled: the head well past it.
        page.wait_for_function("(h) => kleinDebug().head >= h",
                               arg=page.evaluate("kleinDebug().head") + 2 * MARK_SETTLED_US,
                               timeout=10000, polling=50)
        # Pause near the head: the window holds still from here on.
        tl = page.evaluate("() => KleinTimeline.debug()")
        ruler = page.locator("#tl-ruler").bounding_box()
        page.mouse.click(tl["left"] + tl["width"] - 3, ruler["y"] + 10)
        cls.frame()
        cls.tl = page.evaluate("() => KleinTimeline.debug()")
        cls.rows = page.evaluate(_ROW_SHAPES)
        cls.dump = cls.gw.require_debug_state()
        cls.model = Model(cls.dump)

    def x(self, t):
        tl = self.tl
        return tl["left"] + (t - tl["t0"]) * tl["width"] / tl["span"]

    def intervals(self, uid):
        seg = self.dump["segments"][0]
        self.assertEqual(len(self.dump["segments"]), 1)
        return self.model.intervals(seg["id"], uid, max(self.tl["t0"], seg["t_begin"]),
                                    self.tl["t1"])

    def test_every_rows_bars_caps_and_single_marks_match_intervals(self):
        settled = self.x(self.dump["head"] - MARK_SETTLED_US)
        edge = self.x(max(self.tl["t0"], self.dump["segments"][0]["t_begin"]))
        left = edge + 1                 # a bar starting there may be cut by the window
        counts = {"bar": 0, "cap": 0, "mark": 0}
        for row in self.rows:
            if row["uid"] is None:
                continue
            bars, caps, marks, _ = expected_shapes(self.intervals(row["uid"]))
            for t0, _t1 in bars:
                if left < self.x(t0) < settled:
                    counts["bar"] += 1
                    self.assertTrue(any(abs(l - self.x(t0)) <= 1 for l, _r, _c in row["bars"]),
                                    f"{row['name']}: bar at {t0}")
            for t, status in caps:
                if edge < self.x(t) < settled:
                    counts["cap"] += 1
                    self.assertTrue(any(abs(r - self.x(t)) <= 1 and f"s-{status}" in c
                                        for _l, r, c in row["caps"]), f"{row['name']}: cap at {t}")
            want = [(self.x(t), s) for t, s in marks if self.x(t) < settled]
            got = [((l + r) / 2, c) for l, r, c in row["marks"] if (l + r) / 2 < settled]
            counts["mark"] += len(want)
            for x, status in want:
                self.assertTrue(any(abs(gx - x) <= 1 and f"s-{status}" in c for gx, c in got),
                                f"{row['name']}: single mark at x={x:.1f}, got {got}")
            for gx, _c in got:              # and no mark intervals() doesn't have
                self.assertTrue(any(abs(gx - x) <= 1 for x, _s in want), f"{row['name']}: extra mark")
        self.assertGreaterEqual(counts["mark"], 1, counts)     # IsDoorClosed's, at least
        self.assertGreaterEqual(counts["cap"], 5, counts)

    def test_section_header_failure_marks_and_tint(self):
        door = next(r for r in self.rows if r["header"] and r["uid"] == 7)
        inside = [r["uid"] for r in self.rows[self.rows.index(door) + 1:]
                  if r["uid"] is not None][:6]               # tryOpen .. SmashDoor
        settled = self.x(self.dump["head"] - MARK_SETTLED_US)
        want = sorted({round(self.x(t)) for uid in inside
                       for t in expected_shapes(self.intervals(uid))[3] if self.x(t) < settled})
        got = sorted({round((l + r) / 2) for l, r, c in door["subs"]
                      if "s-3" in c and (l + r) / 2 < settled})
        self.assertGreaterEqual(len(want), 4)
        self.assertEqual(len(got), len(want), (got, want))
        for g, w in zip(got, want):
            self.assertLessEqual(abs(g - w), 1)
        fill = self.page.evaluate("""() => getComputedStyle([...document.querySelectorAll('#canvas g.node')]
            .find(g => g.__data__.data.uid === 7).querySelector('.node-rect')).fill""")
        self.assertEqual(door["bg"], fill, "the header is tinted like its canvas region")

    def test_a_folded_row_marks_each_change_below_it(self):
        """Inverter folded: its row marks every change of IsDoorClosed (uid 6)."""
        chevron = '#tl-rows .tl-row[data-uid="5"] button.tl-chevron'
        self.page.click(chevron)
        try:
            self.page.wait_for_selector('#tl-rows .tl-row[data-uid="6"]', state="detached")
            got = sorted(round((l + r) / 2) for l, r, c in next(
                row for row in self.page.evaluate(_ROW_SHAPES) if row["uid"] == 5)["subs"] if "kid" in c)
        finally:
            self.page.click(chevron)
        settled = self.x(self.dump["head"] - MARK_SETTLED_US)
        want = sorted({round(self.x(a)) for a, _b, _v in self.intervals(6)[1:]})
        got = [g for g in got if g < settled]
        want = [w for w in want if w < settled]
        self.assertGreaterEqual(len(want), 1)
        self.assertEqual(len(got), len(want), (got, want))
        for g, w in zip(got, want):
            self.assertLessEqual(abs(g - w), 1)

    def test_nested_header_sticks_only_inside_its_section(self):
        page = self.page
        height = page.evaluate("() => document.documentElement.style.getPropertyValue('--drawer-height')")
        page.evaluate("() => document.documentElement.style.setProperty('--drawer-height', '140px')")
        self.frame()
        geometry = """() => {
          const body = document.getElementById('drawer-body');
          const top = document.querySelector('#drawer-timeline .tl-head').getBoundingClientRect().bottom;
          const box = (uid) => document.querySelector(`#tl-rows .tl-row[data-uid="${uid}"]`)
                                .getBoundingClientRect();
          const hit = document.elementFromPoint(box(1).left + 120, top + 8);
          return { scroll: body.scrollTop, max: body.scrollHeight - body.clientHeight, top,
                   door: box(7).top, pass: box(13).top, main: box(1).top,
                   onTop: hit && hit.closest('.tl-row') && hit.closest('.tl-row').dataset.uid };
        }"""
        try:
            # Inside DoorClosed (PickLock's row at the top): its header is the stuck one.
            page.evaluate("""() => { const b = document.getElementById('drawer-body');
              const r = document.querySelector('#tl-rows .tl-row[data-uid="11"]');
              b.scrollTop += r.getBoundingClientRect().top - b.getBoundingClientRect().top - 84; }""")
            self.frame()
            g = page.evaluate(geometry)
            self.assertAlmostEqual(g["door"], g["top"], delta=1, msg=g)
            self.assertEqual(g["onTop"], "7", g)
            # Scrolled past DoorClosed (back in the main tree): the main header again.
            page.evaluate("() => { const b = document.getElementById('drawer-body');"
                          " b.scrollTop = b.scrollHeight; }")
            self.frame()
            g = page.evaluate(geometry)
            self.assertGreater(g["scroll"], 0, g)
            self.assertLess(g["pass"] - g["top"], 24 + 20, f"scrolled past DoorClosed: {g}")
            self.assertAlmostEqual(g["main"], g["top"], delta=1, msg=f"main header stuck: {g}")
            self.assertLessEqual(g["door"] + 24, g["pass"] + 1, f"DoorClosed's header let go: {g}")
        finally:
            page.evaluate("(h) => document.documentElement.style.setProperty('--drawer-height', h)", height)


if __name__ == "__main__":
    unittest.main()
