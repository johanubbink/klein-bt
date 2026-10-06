"""The Log tab and the shared cursor.

Checked in a real browser (Playwright) against the gateway's own model
(``GET /debug/state``, ``--debug``), re-derived by ``tests.harness.model``.
``LogTest`` runs one mock mission through tree swaps (``--switch-every``), a
same-tree resume after an outage and eviction of the oldest records
(``--record-buffer``), then freezes the robot (SIGSTOP) so browser and
gateway hold exactly the same records:

* every Log row's text across those segments (time, Δ or "new tree" /
  "resumed", subtree, node, from, to);
* random row clicks: cards = ``state_at_seq``, blackboard panel =
  ``BlackboardTrack.at(t)``, the row's "to" on its card, nothing fading
  and the static change mark on exactly the keys the Model calls fresh, and
  the writer pulse on exactly those keys' writers (the layout's bindings); the
  transport row's clock and its "Jump to live" button, and the canvas's
  amber edge;
* the filter across both trees (every cell, Δ to the row above as listed),
  and a subtree-name click fills it without pausing;
* playing from a row before a blackboard change fades the key in, and its
  writers' pulse, as the change passes, and nothing at the start; a jump to
  it from a paused moment does not fade either;
* ↑/↓ across a segment boundary, filtered ↓ to the next match, ↑ from live
  to the newest row, and the keyboard guards (sidebar, filter input); the
  transport's |◀ ▶| and Jump to live, and their keys ←/→ and End, step the
  same filtered rows from either tab and the keys don't scroll the drawer;
  Space plays and pauses, live too (a focused button keeps it), Home pauses at the
  model's state at the oldest kept moment, and no key acts with a modifier,
  in the filter or in the sidebar.

``BackToLiveTest``: back to live by the Live button and by Esc, the robot
running: cards = the gateway's head, the Log follows the newest row, no note;
and from live, ❚❚ and Space pause at the moment shown (cards, blackboard and
clock unchanged, = the model there) and stay there as the head moves on.
``AfterATreeSwapTest``: a row older than its segment's first blackboard
sample shows no values, and the swap's notice centres over the canvas.
``HundredThousandRowsTraceTest``: 100k rows scroll without freezing the page.

Skipped when Playwright is missing.
"""
import json
import random
import signal
import time
import unittest

from klein.recording import Recording
from klein.streaming import backfill_frames
from tests.harness.model import STATUS, Model, card_label, fmt_time, names
from tests.harness.probes import BACK_TO_LIVE, NEXT_FRAME
from tests.helpers import layout
from tests.ui import OVERVIEW, PULSES, DashboardCase, links

SHOTS = "log"
WIDTH, HEIGHT = 1400, 900
ROW = 20
TIMEOUT = 0.5                   # s: the gateway's REQUEST_TIMEOUT here (2 s in klein)
OUTAGE = 1.0                    # s: each robot pause, twice TIMEOUT

# Every row as drawn, by scrolling the whole list one view at a time.
_ALL_ROW_TEXT = """async () => {
  const body = document.getElementById('drawer-body');
  const next = () => new Promise(r => requestAnimationFrame(r));
  const out = {};
  for (let y = 0; ; y += body.clientHeight - 2 * 20) {
    body.scrollTop = y; await next(); await next();
    for (const r of body.querySelectorAll('.log-row:not([hidden])')) {
      const c = r.querySelectorAll(':scope > span');
      out[r.dataset.seg + ':' + r.dataset.seq] = [c[0].textContent, c[1].textContent,
        c[2].textContent, c[3].textContent, r.querySelector('.log-from').textContent,
        r.querySelector('.log-to').textContent];
    }
    if (y >= body.scrollHeight - body.clientHeight) break;
  }
  return out;
}"""

# The cursor as the page shows it, read in one task: the selected row, the
# transport's clock and Live / Jump to live button, the cards and the
# blackboard panel (each row's text by board/key).
_VIEW = """() => {
  const sel = document.querySelector('#log-rows .log-row.selected:not([hidden])');
  const boards = {}, marked = [];
  for (const [name, g] of Object.entries(bbGroupEls)) {
    boards[name] = {};
    for (const [key, row] of Object.entries(g.rows)) {
      boards[name][key] = row.value.textContent;
      if (row.row.classList.contains('bb-mark')) marked.push([name, key]);
    }
  }
  const body = document.getElementById('drawer-body');
  return {
    selected: sel ? [Number(sel.dataset.seg), Number(sel.dataset.seq)] : null,
    past: document.body.classList.contains('viewing-past'),
    clock: document.getElementById('tr-clock').textContent,
    state: (j => [j.dataset.state, j.textContent, j.disabled])(document.getElementById('tr-jump')),
    edge: getComputedStyle(document.getElementById('past-edge')).display !== 'none',
    note: getComputedStyle(document.getElementById('bb-past-note')).display !== 'none',
    fading: document.querySelectorAll('#bb-panel .bb-fresh').length, marked,
    pulses: (""" + PULSES + """)(),
    atEnd: body.scrollTop + body.clientHeight >= body.scrollHeight - 1,
    cards: [...document.querySelectorAll('#canvas g.node')].map(g => [
      g.__data__.data.uid, g.querySelector('.node-status-text').textContent]),
    boards, segments: kleinDebug().segments,
  };
}"""

# Open a board (a closed one's rows never fade), now or once it is listed.
_OPEN_BOARD = """(board) => {
  const g = bbGroupEls[board];
  if (!g) bbGroupOpen[board] = true;
  else if (g.body.hidden) g.toggle.click();
}"""

# Paint the panel at t0 in mode `before` (null: no paint, the rows built
# afresh), then live at t1, with the key's board open: does the key fade, and
# which cards' pulses fade?
_JUMP = """([seg, t0, t1, before, board, key]) => {
  const s = recordingStore.recording.segment(seg);
  for (const c of document.querySelectorAll('#canvas .node-pulse.fire')) c.classList.remove('fire');
  if (before === null) resetBlackboards();
  else renderBlackboards(KleinRecording.bbAt(s, t0), { track: s.bb, t: t0, mode: before });
  (""" + _OPEN_BOARD + """)(board);
  renderBlackboards(KleinRecording.bbAt(s, t1), { track: s.bb, t: t1, mode: 'live' });
  return [bbGroupEls[board].rows[key].row.classList.contains('bb-fresh'), (""" + PULSES + """)()[1]];
}"""

class LogView:
    """Reading and driving the Log tab, for a ``DashboardCase``."""

    def open_log(self):
        """The Log tab, live, unfiltered, nothing focused."""
        self.page.click("#drawer-tab-log")      # the drawer opens on the Timeline
        self.page.evaluate("() => document.activeElement && document.activeElement.blur()")
        self.browser.go_live()
        self.page.fill("#drawer-filter", "")
        self.frame()

    def view(self, painted=False):
        self.frame()
        self.browser.wait_for_nodes(1)          # a tree swap's exiting cards are gone
        if not painted:
            return self.page.evaluate(_VIEW)
        # Read once no render is queued and no card is moving: the cards are
        # then painted from every recording frame received, a new tree's too.
        return self.page.wait_for_function(
            "() => !renderQueued && ![...document.querySelectorAll("
            "'#canvas g.node, #canvas path.link')].some(el => el.__transition)"
            f" && ({_VIEW})()", polling=1, timeout=10000).json_value()

    def listed(self):
        return [tuple(k) for k in self.page.evaluate("() => KleinDrawer.debugRows().keys")]

    def scroll_to(self, index):
        self.page.evaluate(f"() => {{ document.getElementById('drawer-body').scrollTop = "
                           f"{index * ROW}; }}")
        self.frame()

    def click(self, key):
        """Scroll row ``key`` (seg, seq) to the middle of the view and click it.

        The rows are a pooled list, so nothing may scroll between Playwright
        finding the row and clicking it. The index is looked up in the same
        task that scrolls (eviction shifts rows), and the view stays a row
        short of the bottom, where the Log would follow the newest row."""
        self.frame()
        self.page.evaluate("""([seg, seq, row]) => {
          const keys = KleinDrawer.debugRows().keys;
          const i = keys.findIndex(([a, b]) => a === seg && b === seq);
          const body = document.getElementById('drawer-body');
          const short = body.scrollHeight - body.clientHeight - row;
          const middle = i * row - (body.clientHeight - row) / 2;
          body.scrollTop = i === keys.length - 1 ? i * row : Math.min(middle, short); }""",
                           [*key, ROW])
        self.frame()
        self.page.click(f'#log-rows .log-row[data-seg="{key[0]}"][data-seq="{key[1]}"]:not([hidden])')


class LogTest(LogView, DashboardCase):
    """One run: tree swaps, an outage and eviction, then frozen for every test."""

    ROBOT = {"switch_every": 30}
    GATEWAY = {"debug": True, "poll_interval": 0.05, "extra_args": ("--record-buffer", "5s")}
    VIEWPORT = (WIDTH, HEIGHT)
    OPEN = 5

    @classmethod
    def make_gateway(cls, robot_port):
        gw = super().make_gateway(robot_port)
        # The same launcher with a 0.5 s request timeout, so a 1 s pause is an outage.
        gw.launcher[1] = (f"from klein import gateway; gateway.REQUEST_TIMEOUT = {TIMEOUT}; "
                          + gw.launcher[1])
        return gw

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(cls.resume_robot)       # before it is stopped
        cls.browser.wait_connected()
        cls.page.wait_for_function("KleinDrawer.debugRows().count > 10", timeout=20000)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:          # two trees seen, oldest evicted
            st = cls.gw.require_debug_state()
            if len({s["layout_id"] for s in st["segments"]}) >= 2 and st["segments"][0]["id"] > 0:
                break
            time.sleep(0.2)
        # An outage (the same tree resumes), then frozen once the kept window
        # holds everything the tests need. On a loaded machine the 5 s window
        # can drop the resumed pair before the freeze: then try another outage.
        deadline = time.monotonic() + 45
        while True:
            cls.after_a_swap()
            cls.pause_robot(OUTAGE)
            cls.resume_robot()
            wait = time.monotonic() + 15
            while time.monotonic() < wait and not cls.ready(cls.gw.require_debug_state()):
                time.sleep(0.1)
            cls.state = cls.pause_robot(OUTAGE)     # frozen from here on
            if cls.ready(cls.state) or time.monotonic() > deadline:
                break
            cls.resume_robot()
        layouts = cls.page.evaluate(
            "() => Object.fromEntries(recordingStore.recording.segments"
            ".map(s => [s.id, s.layout]))")
        cls.layouts = layouts
        cls.model = Model(cls.state, layouts)

    @classmethod
    def after_a_swap(cls):
        """Return just after the robot swapped trees and recorded a row of
        the new one. An outage started then resumes with most of that tree's
        run, rows to check, where one started a poll before a swap resumes
        with none."""
        first = cls.gw.require_debug_state()["segments"][-1]["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            segs = cls.gw.require_debug_state()["segments"]
            if (segs[-1]["id"] != first and len(segs) > 1
                    and segs[-1]["layout_id"] != segs[-2]["layout_id"]
                    and segs[-1]["head_seq"] > segs[-1]["start_seq"]):
                return
            time.sleep(0.02)

    @staticmethod
    def ready(state):
        """The kept window holds a tree swap, a same-tree resume with rows
        on both sides, two blackboard samples, and an evicted oldest segment."""
        segs = state["segments"]
        pairs = list(zip(segs, segs[1:]))
        return (segs[0]["id"] > 0
                and sum(b["t_start"] is not None for b in state["blackboard"]) >= 2
                and any(a["layout_id"] != b["layout_id"] for a, b in pairs)
                and any(a["layout_id"] == b["layout_id"] and a["head_seq"] > a["start_seq"]
                        and b["head_seq"] - b["start_seq"] > 3 for a, b in pairs))

    @classmethod
    def pause_robot(cls, seconds):
        """SIGSTOP the robot for longer than the request timeout (an outage),
        then wait until the browser holds the gateway's segments exactly."""
        cls.robot.proc.send_signal(signal.SIGSTOP)
        time.sleep(seconds)
        state = cls.gw.require_debug_state()
        want = [[s["id"], s["start_seq"], s["head_seq"]] for s in state["segments"]]
        cls.page.wait_for_function(
            "(want) => JSON.stringify(kleinDebug().segments.map(s => [s.id, s.startSeq, s.headSeq]))"
            " === JSON.stringify(want)", arg=want, timeout=10000)
        return state

    @classmethod
    def resume_robot(cls):
        if cls.robot.proc is not None:
            cls.robot.proc.send_signal(signal.SIGCONT)

    def setUp(self):
        self.open_log()

    def assert_at(self, row, log=True):
        """The page shows the moment just after Log row ``row`` (a Model.rows
        item); its row selected, unless ``log`` is false (the Timeline shown)."""
        key, cells, t, uid = row
        to = cells[5]
        v = self.view()
        seg, seq = key
        if log:
            self.assertEqual(v["selected"], list(key))
        self.assertTrue(v["past"] and v["note"], "paused, with the blackboard note")
        self.assertEqual(v["fading"], 0, "nothing fades in the past")
        self.assertEqual(v["clock"], fmt_time(t))
        self.assertEqual(v["state"], ["past", "Jump to live ⏭︎", False])
        self.assertTrue(v["edge"], "the canvas's amber edge")
        st = self.model.state_at_seq(seg, seq + 1)
        cards = dict(v["cards"])
        self.assertEqual(cards, {u: card_label(st[u]) for u in cards}, key)
        # Off by one: the card of the row's node shows the row's "to".
        if uid in cards:
            self.assertIn(cards[uid], ([to] if to != "IDLE" else ["IDLE"]) +
                          ([f"was {s}" for s in STATUS] if to == "IDLE" else []))
        boards = self.model.bb_at(seg, t)
        if boards is None:
            return v
        self.assertEqual(v["boards"], self.browser.value_summaries(boards), key)
        fresh = self.model.bb_fresh(seg, t)
        self.assertEqual({tuple(m) for m in v["marked"]}, fresh, key)
        roles = links(self.layouts[str(seg)])[0]
        writers = {u for ref in fresh for u, role in roles.get(ref, {}).items() if role == "write"}
        self.assertEqual([set(v["pulses"][0]), v["pulses"][1]], [writers, []], key)
        return v

    # ------------------------------------------------------------------ #
    def test_rows_across_segments_and_eviction(self):
        segs = self.state["segments"]
        self.assertGreater(segs[0]["id"], 0, "a segment was evicted")
        layouts = [s["layout_id"] for s in segs]
        self.assertTrue(any(a != b for a, b in zip(layouts, layouts[1:])), "a tree swap")
        self.assertTrue(any(a == b for a, b in zip(layouts, layouts[1:])),
                        f"a same-tree resume: {segs} {self.state['gaps']}")
        expected = self.model.rows()
        self.assertEqual(self.listed(), [k for k, *_ in expected])
        drawn = self.page.evaluate(_ALL_ROW_TEXT)
        self.assertEqual(len(drawn), len(expected))
        for key, cells, _t, _u in expected:
            self.assertEqual(drawn[f"{key[0]}:{key[1]}"], cells, key)
        boundaries = [c[1] for _k, c, _t, _u in expected if c[1] in ("new tree", "resumed")]
        self.assertIn("new tree", boundaries)
        self.assertIn("resumed", boundaries)
        i = next(i for i, (_k, c, *_r) in enumerate(expected) if c[1] == "resumed")
        self.scroll_to(i - 4)
        self.browser.screenshot(SHOTS, "segments_boundary")

    def test_the_overview_marks_the_kept_recording(self):
        """The overview spans what is kept (eviction moved its left edge), with
        the Model's outage gaps and tree-run starts; the knob is at
        the right edge while live, and a click on the track pauses there."""
        self.frame()
        v, x = self.assert_overview(self.model, self.state["gaps"])
        ov = v["ov"]
        self.assertEqual((ov["tMin"], ov["tMax"]), (self.state["t_min"], self.model.head_time()))
        self.assertGreater(self.state["segments"][0]["id"], 0, "eviction dropped the oldest")
        self.assertTrue(v["gaps"] and v["runs"], v)
        self.assertAlmostEqual((v["knob"]["left"] + v["knob"]["right"]) / 2, x(ov["tMax"]), delta=1)
        self.browser.screenshot("overview", "outage")
        box = self.page.locator("#overview").bounding_box()
        at = ov["left"] + 0.37 * ov["width"]
        self.page.mouse.click(at, box["y"] + box["height"] / 2)
        view = self.view()
        pos = self.page.evaluate("() => KleinCursor.cursorPos(clock, performance.now(), "
                                 "shownRecording())")
        self.assertTrue(view["past"])
        self.assertAlmostEqual(x(pos["t"]), at, delta=1)
        st = self.model.state_at_seq(*self.model.seq_at(pos["t"]))
        cards = dict(view["cards"])
        self.assertEqual(cards, {u: card_label(st[u]) for u in cards}, pos)
        knob = self.page.evaluate(OVERVIEW)["knob"]
        self.assertAlmostEqual((knob["left"] + knob["right"]) / 2, at, delta=1)
        # Folded to the transport row alone, the overview and |◀ still move
        # the tree; unfolding gives the height back.
        height = self.page.evaluate("() => document.getElementById('drawer').offsetHeight")
        self.page.click("#drawer-collapse")
        try:
            self.assertFalse(self.page.is_visible("#drawer-toolbar"))
            box = self.page.locator("#overview").bounding_box()
            at = ov["left"] + 0.61 * ov["width"]
            self.page.mouse.click(at, box["y"] + box["height"] / 2)
            view = self.view()
            pos = self.page.evaluate("() => KleinCursor.cursorPos(clock, performance.now(), "
                                     "shownRecording())")
            self.assertAlmostEqual(x(pos["t"]), at, delta=1)
            st = self.model.state_at_seq(*self.model.seq_at(pos["t"]))
            cards = dict(view["cards"])
            self.assertEqual(cards, {u: card_label(st[u]) for u in cards}, "folded, at the click")
            knob = self.page.evaluate(OVERVIEW)["knob"]
            self.assertAlmostEqual((knob["left"] + knob["right"]) / 2, at, delta=1,
                                   msg="folded, the overview still paints the knob")
            # The row before the cursor's record (or before where it would be).
            rows = self.model.rows()
            here = next(i for i, (k, *_r) in enumerate(rows)
                        if k >= (pos["seg"], pos["seq"] - 1))
            self.page.click("#tr-prev")
            self.assert_at(rows[here - 1], log=False)
            self.browser.screenshot(SHOTS, "folded_past")
        finally:
            self.page.click("#drawer-collapse")
        self.assertEqual(self.page.evaluate("() => document.getElementById('drawer').offsetHeight"),
                         height)

    def test_a_row_click_shows_its_moment(self):
        rows = self.model.rows()
        cands = [r for r in rows if self.model.bb_at(r[0][0], r[2]) is not None]
        rng = random.Random(3)
        # One per segment with a sample, the rest anywhere.
        picks = [rng.choice([r for r in cands if r[0][0] == seg])
                 for seg in sorted({r[0][0] for r in cands})]
        picks += rng.sample([r for r in cands if r not in picks], 10 - len(picks))
        # And both sides of where a mark comes or goes: consecutive rows whose
        # fresh keys differ.
        fresh = [self.model.bb_fresh(r[0][0], r[2]) for r in cands]
        edges = [i for i in range(len(cands) - 1) if fresh[i] != fresh[i + 1]
                 and (fresh[i] or fresh[i + 1])]
        self.assertTrue(edges, "no Log row just after a blackboard change")
        for i in rng.sample(edges, min(3, len(edges))):
            picks += [cands[i], cands[i + 1]]
        for row in picks:
            self.click(row[0])
            self.assert_at(row)
        marked = next(cands[k] for i in edges for k in (i, i + 1) if fresh[k])
        self.click(marked[0])
        self.assert_at(marked)
        self.browser.screenshot("blackboard", "paused_after_a_change")
        # Nothing fades stepping between two rows of one tree whose boards differ.
        tree = lambda r: self.model.segments[r[0][0]][0]["layout_id"]
        pair = next(((a, b) for a in cands for b in cands if tree(a) == tree(b)
                     and self.model.bb_at(a[0][0], a[2]) != self.model.bb_at(b[0][0], b[2])), None)
        if pair is not None:
            a, b = pair
            self.click(a[0])
            self.click(b[0])
            self.assertEqual(self.assert_at(b)["fading"], 0)
        self.browser.screenshot(SHOTS, "paused")

    def test_the_filter_across_trees(self):
        subs = sorted({sub for table in self.model.names.values() for _n, sub in table.values()})
        for needle in ["door", subs[-1], subs[0][:4].upper(), "zzz"]:
            self.page.fill("#drawer-filter", needle)
            self.frame()
            rows = self.model.rows(needle)
            exp = [k for k, *_ in rows]
            self.assertEqual(self.listed(), exp, needle)
            if exp:             # every cell, Δ to the row above as listed
                self.assertFalse(self.page.is_visible("#log-empty"), needle)
                drawn = self.page.evaluate(_ALL_ROW_TEXT)
                self.assertEqual(drawn, {f"{k[0]}:{k[1]}": c for k, c, *_ in rows}, needle)
            else:
                self.assertTrue(self.page.is_visible("#log-empty"), needle)
        # A subtree name click fills the filter, and only filters.
        self.page.fill("#drawer-filter", "")
        self.frame()
        key = next(k for k, c, *_ in self.model.rows() if c[2] not in subs[:1])
        self.scroll_to(self.listed().index(key))
        cell = f'.log-row[data-seg="{key[0]}"][data-seq="{key[1]}"] .log-subtree'
        sub = self.page.text_content(cell)
        self.page.click(cell)
        self.frame()
        self.assertEqual(self.page.input_value("#drawer-filter"), sub)
        self.assertEqual(self.listed(), [k for k, *_ in self.model.rows(sub)])
        self.assertFalse(self.view()["past"], "a subtree click only filters")
        self.browser.screenshot(SHOTS, "filtered")

    def test_a_change_fades_only_as_it_passes(self):
        """Played from a row before a (not streaming) change, the key fades
        in when the change time passes, and nothing fades at the start. A
        jump to the change from a paused moment does not fade it; moving onto
        it while live does."""
        rows, bb = self.model.rows(), self.state["blackboard"]
        writers = lambda seg, board, key: {
            u for u, role in links(self.layouts[str(seg)])[0].get((board, key), {}).items()
            if role == "write"}
        # A change of a key already shown 0.6 s before, with a row before
        # that; one a node writes if there is one.
        found = []
        for e in bb:
            seg = e["seg"]
            for t, board, key, text in e["changes"]:
                t0 = max(t - 600_000, e["t_start"])
                start = [r for r in rows if r[0][0] == seg and t - 1.5e6 < r[2] < t0]
                if (text is not None and start and (board, key) in self.model.bb_fresh(seg, t)
                        and key in (self.model.bb_at(seg, t0) or {}).get(board, {})):
                    found.append((start[0], board, key, t, t0))
        if not found:
            self.skipTest("no row shortly before a change that is not streaming")
        row, board, key, t, t0 = max(found, key=lambda f: bool(writers(f[0][0][0], *f[1:3])))
        wrote = writers(row[0][0], board, key)
        self.click(row[0])
        self.frame()                # its tree drawn (its boards' open state reset)
        self.page.evaluate(_OPEN_BOARD, board)
        # ▶ pressed and the page read in one task, on the frame painted after
        # it: at 1x, a read a few round trips later can be past the change.
        started = self.page.evaluate("""async () => {
          document.getElementById('tr-play').click();             // from the Log tab
          await (""" + NEXT_FRAME + """)();
          return { mode: clock.mode, state: document.getElementById('tr-jump').dataset.state,
                   play: document.getElementById('tr-play').textContent,
                   fading: document.querySelectorAll('#bb-panel .bb-fresh').length,
                   t: KleinCursor.cursorPos(clock, performance.now(), shownRecording()).t };
        }""")
        self.assertEqual([started["mode"], started["state"], started["play"]],
                         ["playing", "past", "❚❚"], "playing is the past too")
        self.assertLess(started["t"], t, "read before the change")
        self.assertEqual(started["fading"], 0, "starting to play is not a change")
        fired = self.page.wait_for_function(
            "([b, k]) => bbGroupEls[b] && bbGroupEls[b].rows[k]"
            " && bbGroupEls[b].rows[k].row.classList.contains('bb-fresh')"
            " && (" + PULSES + ")()[1]",
            arg=[board, key], timeout=5000).json_value()
        self.assertLessEqual(wrote, set(fired), "the writers pulse as the change passes")
        # The panel painted at t0, then live at the change, in one task
        # (nothing repaints in between).
        for before, fades in (("paused", False), ("live", True), (None, False)):
            with self.subTest(before=before):
                fading, fired = self.page.evaluate(_JUMP, [row[0][0], t0, t, before, board, key])
                self.assertEqual(fading, fades)
                self.assertEqual(set(fired) & wrote, wrote if fades else set(), fired)
        # Live, a change first painted once it is no longer fresh (a sample
        # late) is not news: it does not fade.
        e = next(e for e in bb if e["seg"] == row[0][0])
        late = min([c[0] - 1 for c in e["changes"] if (c[1], c[2]) == (board, key) and c[0] > t]
                   + [t + 600_000])
        if late - t >= 500_000:
            with self.subTest(before="live", late=True):
                self.assertFalse(self.page.evaluate(
                    _JUMP, [row[0][0], t0, late, "live", board, key])[0])

    def test_keys_across_a_boundary_and_guards(self):
        rows = self.model.rows()
        b = next(i for i in range(1, len(rows)) if rows[i][0][0] != rows[i - 1][0][0])
        self.click(rows[b - 1][0])
        self.page.keyboard.press("ArrowDown")
        self.assert_at(rows[b])
        self.page.keyboard.press("ArrowUp")
        self.assert_at(rows[b - 1])
        # Guards: a sidebar control and the filter keep the keys.
        self.page.focus("#sidebar input, #sidebar button")
        self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("Escape")
        self.assertEqual(self.view()["selected"], list(rows[b - 1][0]))
        self.page.focus("#drawer-filter")
        self.page.keyboard.press("ArrowUp")
        self.page.keyboard.press("Escape")
        self.assertEqual(self.view()["selected"], list(rows[b - 1][0]))
        self.page.evaluate("() => document.activeElement.blur()")
        self.page.keyboard.press("Escape")
        self.assertFalse(self.view()["past"])
        # From live, ↑ takes the newest row.
        self.page.keyboard.press("ArrowUp")
        self.assert_at(rows[-1])
        self.page.keyboard.press("Escape")
        # Filtered, ↓ goes to the next matching record, not the next seq.
        for needle in sorted({c[3] for _k, c, *_ in rows}):      # a node with a gap
            picks = self.model.rows(needle)
            i = next((i for i in range(len(picks) - 1)
                      if picks[i + 1][0] != (picks[i][0][0], picks[i][0][1] + 1)), None)
            if i is not None:
                break
        self.page.fill("#drawer-filter", needle)
        self.frame()
        self.assertEqual(self.listed(), [k for k, *_ in picks], needle)
        self.click(picks[i][0])
        self.page.evaluate("() => document.activeElement && document.activeElement.blur()")
        self.page.keyboard.press("ArrowDown")
        self.assert_at(picks[i + 1])
        # The transport's |◀ ▶| and Jump to live, and their keys ←/→ and
        # End, step the same filtered rows from either tab; from live, ◀
        # takes the newest one and ▶ is off. A key that acts doesn't scroll
        # the drawer (a listener added after the page's sees it prevented).
        self.page.evaluate("""() => { window.__keys = [];
            window.addEventListener('keydown', e => __keys.push([e.key, e.defaultPrevented])); }""")
        for tab in ("timeline", "log"):
            for how in ("button", "key"):
                with self.subTest(tab=tab, how=how):
                    press = (self.page.click if how == "button"
                             else lambda b: self.page.keyboard.press(
                                 {"#tr-prev": "ArrowLeft", "#tr-next": "ArrowRight",
                                  "#tr-jump": "End"}[b]))
                    self.page.click("#drawer-tab-log")
                    self.click(picks[i + 1][0])
                    self.page.click(f"#drawer-tab-{tab}")
                    self.assertEqual(self.page.is_visible("#tl-zoom"), tab == "timeline",
                                     "the zoom is the Timeline's")
                    press("#tr-prev")
                    self.assert_at(picks[i], log=tab == "log")
                    press("#tr-next")
                    self.assert_at(picks[i + 1], log=tab == "log")
                    press("#tr-jump")
                    v = self.view()
                    self.assertFalse(v["past"] or v["edge"])
                    self.assertEqual(v["state"], ["live", "Live", True])
                    self.assertTrue(self.page.is_disabled("#tr-next"))
                    press("#tr-prev")
                    self.assert_at(picks[-1], log=tab == "log")
        self.assertEqual(self.page.evaluate("__keys"),
                         [[k, True] for k in ["ArrowLeft", "ArrowRight", "End", "ArrowLeft"] * 2])
        # Paused on a record the filter then hides (between two matches):
        # ▶| takes the next match, |◀ the one before, nothing skipped.
        keys = [k for k, *_ in rows]
        j, mid = next((j, k) for j in range(len(picks) - 1) for k in keys
                      if picks[j][0] < k < picks[j + 1][0])
        for button, want in (("#tr-next", picks[j + 1]), ("#tr-prev", picks[j])):
            self.page.fill("#drawer-filter", "")
            self.frame()
            self.click(mid)
            self.page.fill("#drawer-filter", needle)
            self.frame()
            self.page.click(button)
            self.assert_at(want)
        self.assertNotEqual(self.page.evaluate("() => document.activeElement.id"), "tr-prev",
                            "a mouse click lets go of the focus, so the keys work right away")

        # Space plays and pauses as ▶ does, twice in a row too. A focused
        # button keeps it: ▶ toggles once, not twice; |◀ steps, not plays.
        # Live it pauses at the head.
        self.page.fill("#drawer-filter", "")
        self.frame()
        mode = lambda: self.page.evaluate("clock.mode")
        for on in ("body", "#tr-play", "#tr-prev"):
            with self.subTest(space_on=on):
                self.click(rows[b - 1][0])
                self.page.evaluate("() => document.activeElement.blur()")
                if on != "body":
                    self.page.focus(on)
                self.page.keyboard.press(" ")
                if on == "#tr-prev":
                    self.assert_at(rows[b - 2])
                    self.assertEqual(mode(), "paused")
                    continue
                self.assertEqual(mode(), "playing")
                self.page.keyboard.press(" ")
                self.assertEqual(mode(), "paused")
        self.page.evaluate("() => document.activeElement.blur()")
        head = lambda: self.page.evaluate("() => { const s = kleinDebug().segments.at(-1);"
                                          " return [s.id, s.headSeq]; }")
        at = lambda: self.page.evaluate("() => { const p = KleinCursor.cursorPos(clock, 0,"
                                        " shownRecording()); return [p.seg, p.seq, p.live]; }")
        self.browser.go_live()
        self.page.keyboard.press(" ")
        self.assertEqual(mode(), "paused")
        self.assertEqual(at(), [*head(), False])
        self.page.keyboard.press("Escape")
        # Back to back, in one task (no frame between): End then Space
        # pauses at the head; on the newest row, Esc then ← takes it again,
        # not the one before it.
        keys = """(keys) => { for (const key of keys) document.body.dispatchEvent(
            new KeyboardEvent('keydown', { key, bubbles: true })); }"""
        self.page.keyboard.press("ArrowUp")
        self.page.evaluate(keys, ["End", " "])
        self.assertEqual(mode(), "paused")
        self.assertEqual(at(), [*head(), False])
        self.page.keyboard.press("ArrowUp")
        self.page.evaluate(keys, ["Escape", "ArrowLeft"])
        self.assert_at(rows[-1])
        # Space twice from live, in one task: pauses, then plays (by the clock,
        # the position still being the live one painted).
        self.browser.go_live()
        self.assertEqual(self.page.evaluate(f"(keys) => {{ ({keys})(keys); return clock.mode; }}",
                                            [" ", " "]), "playing")
        # Home: paused at the oldest kept moment, state_at(t_min); ▶| from
        # there is the first kept row after it.
        self.page.keyboard.press("Home")
        t_min = self.state["t_min"]
        seg, seq = self.model.seq_at(t_min)
        pos = self.page.evaluate("KleinCursor.cursorPos(clock, 0, shownRecording())")
        self.assertEqual((pos["seg"], pos["seq"], pos["t"], pos["live"]), (seg, seq, t_min, False))
        v = self.view()
        self.assertEqual(v["clock"], fmt_time(t_min))
        cards = dict(v["cards"])
        self.assertEqual(cards, self.model.labels(seg, seq, cards))
        self.page.keyboard.press("ArrowRight")
        self.assert_at(rows[seq - self.state["segments"][0]["start_seq"]])
        # Guards: with a modifier, in the filter, or in the sidebar (its own
        # Space opens the legend) no key moves the cursor or plays.
        at = self.page.evaluate("clock")
        legend = "document.getElementById('legend').open"
        was_open = self.page.evaluate(legend)
        keys = ["ArrowLeft", "ArrowRight", "Home", "End", " ", "\\", "Escape"]
        for where in ("modifier", "#drawer-filter", "#legend summary"):
            with self.subTest(guard=where):
                self.page.evaluate("() => document.activeElement && document.activeElement.blur()")
                if where == "modifier":
                    for k in keys:
                        for m in ("Shift", "Control", "Alt"):
                            if (m, k) not in (("Alt", "Home"), ("Alt", "ArrowLeft"),
                                              ("Alt", "ArrowRight")):     # browser history
                                self.page.keyboard.press(f"{m}+{'Space' if k == ' ' else k}")
                else:
                    self.page.focus(where)
                    for k in keys:
                        self.page.keyboard.press(k)
                self.frame()
                self.assertEqual(self.page.evaluate("clock"), at)
        self.assertEqual(self.page.evaluate(legend), not was_open,
                         "Space in the sidebar is the sidebar's")
        self.page.evaluate(f"() => {{ {legend} = {str(was_open).lower()}; }}")
        self.page.fill("#drawer-filter", "")



class BackToLiveTest(LogView, DashboardCase):
    """Paused on a Log row while the robot runs, swaps trees and older
    segments are evicted, then back to live."""

    ROBOT = {"switch_every": 30}
    GATEWAY = {"debug": True, "poll_interval": 0.05, "extra_args": ("--record-buffer", "5s")}
    VIEWPORT = (WIDTH, HEIGHT)

    def setUp(self):
        self.open_log()

    def test_back_to_live_by_button_and_esc(self):
        # The first segment evicted, and the newest one with rows to click.
        self.page.wait_for_function(
            "() => { const s = kleinDebug().segments;"
            " return s[0].id > 0 && s.at(-1).headSeq - s.at(-1).startSeq > 3; }",
            timeout=30000)
        for how in ("button", "Escape"):
            self.click(self.listed()[-4])
            self.assertTrue(self.view()["past"])
            if how == "button":
                self.page.click(BACK_TO_LIVE)
            else:
                self.page.keyboard.press("Escape")
            # Cards = the gateway's state at the browser's head, read painted:
            # a records frame (a new tree's segment, say) lands between paint
            # and read otherwise.
            v = self.view(painted=True)
            gw = self.gw.require_debug_state()
            last = v["segments"][-1]
            g = next(s for s in gw["segments"] if s["id"] == last["id"])
            lag = g["head_seq"] - last["headSeq"]
            st = Model(gw).state_at_seq(last["id"], last["headSeq"])
            cards = dict(v["cards"])
            self.assertEqual(cards, {u: card_label(st[u]) for u in cards},
                             f"{how}: cards vs gateway at browser head (lag {lag})")
            self.assertLessEqual(lag, 20, how)
            self.assertTrue(v["atEnd"], f"{how}: the Log follows the newest row while live")
            self.assertFalse(v["past"], how)
            self.assertFalse(v["edge"], how)
            self.assertEqual(v["state"], ["live", "Live", True], how)
            self.assertIsNone(v["selected"], how)
            self.assertFalse(v["note"], f"{how}: no blackboard note while live")
        # From live, ❚❚ and Space pause at the moment shown, read and pressed
        # in one task: the cards, the blackboard (its newest sample too) and
        # the clock stay, and are the model's there; a second later, with
        # the head moved on, they still are. On the Timeline zoomed to 1 s,
        # following the head, the playhead stays on the track (the window
        # ends at the newest moment shown, not the head's own time), and ▶ is
        # as wide as ❚❚ (nothing beside it moves).
        self.page.click("#drawer-tab-timeline")
        while self.page.evaluate("KleinTimeline.debug().span") > 1e6:
            self.page.click("#tl-zoom-in")
        self.assertEqual(self.page.evaluate("KleinTimeline.debug().span"), 1e6)
        press = {"button": "document.getElementById('tr-play').click()",
                 "Space": "document.body.dispatchEvent("
                          "new KeyboardEvent('keydown', { key: ' ', bubbles: true }))"}
        for how, action in press.items():
            with self.subTest(pause=how):
                self.page.wait_for_function(
                    "Object.keys(bbGroupEls).length > 0 && !document.body.classList"
                    ".contains('viewing-past') && shownRecording().segments.at(-1).bb.tStart !== null")
                self.view()                     # painted live (Esc's repaint too)
                # Pressed on a frame whose clock shows a blackboard sample newer
                # than the head, so pausing at the head's own time would jump.
                live = self.page.evaluate(f"""async () => {{
                  for (let i = 0; i < 900; i++) {{
                    await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));
                    const at = KleinTimeline.debug().pos;
                    if (!at || !at.live || document.getElementById('tr-clock').textContent
                        === KleinDrawer.formatTime(at.t) || [...document.querySelectorAll(
                        '#canvas g.node, #canvas path.link')].some((el) => el.__transition))
                        continue;           // (and no swap's cards still leaving)
                    const v = ({_VIEW})(); {action};
                    return {{ ...v, at, playWidth: document.getElementById('tr-play').offsetWidth }};
                  }}
                  return null; }}""")
                self.assertIsNotNone(live, "the clock never showed a sample newer than the head")
                pos = self.page.evaluate("() => ({ ...KleinCursor.cursorPos(clock, 0,"
                                         " shownRecording()), head: kleinDebug().head })")
                at = live["at"]                 # the head's position, as painted
                self.assertTrue(at["live"])
                self.assertEqual((pos["seg"], pos["seq"], pos["live"]),
                                 (at["seg"], at["seq"], False))
                self.assertTrue(0 < pos["t"] - at["t"] < 500_000, "the newest sample's time")
                for when in ("now", "a second later"):
                    v = self.view()
                    self.assertEqual(v["state"], ["past", "Jump to live ⏭︎", False], when)
                    self.assertEqual([v["cards"], v["boards"], v["clock"]],
                                     [live["cards"], live["boards"], live["clock"]], when)
                    model = Model(self.gw.require_debug_state())
                    self.assertEqual(v["clock"], fmt_time(pos["t"]), when)
                    self.assertEqual(self.page.evaluate(_PLAYHEAD), [True, True], when)
                    self.assertEqual(self.page.evaluate(
                        "document.getElementById('tr-play').offsetWidth"), live["playWidth"])
                    cards = dict(v["cards"])
                    self.assertEqual(cards, model.labels(pos["seg"], pos["seq"], cards), when)
                    boards = model.bb_at(pos["seg"], pos["t"])
                    if boards is None:          # a new tree's first moments: none yet
                        self.assertFalse(any(v["boards"].values()), when)
                    else:
                        self.assertEqual(v["boards"], self.browser.value_summaries(boards), when)
                    if when == "now":
                        self.page.wait_for_function(f"kleinDebug().head > {pos['head']}",
                                                    timeout=10000)
                self.assertGreater(self.page.evaluate("kleinDebug().head"), pos["head"],
                                   "the head moved on meanwhile")
                self.page.keyboard.press("Escape")


class AfterATreeSwapTest(LogView, DashboardCase):
    """Paused just after a tree swap: a row older than the new segment's first
    blackboard sample shows no values (not the panel's values from another
    moment), and the swap's notice heads the banner stack, centred over the
    canvas with the sidebar open or not."""

    ROBOT = {"switch_every": 40}
    GATEWAY = {"debug": True, "poll_interval": 0.05}
    VIEWPORT = (WIDTH, HEIGHT)
    OPEN = 5

    _STACK = """() => {
      const r = document.getElementById('banner-stack').getBoundingClientRect();
      return { keys: [...document.querySelectorAll('#banner-stack .banner')]
                 .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
                 .map(e => e.dataset.key),
               centre: (r.left + r.right) / 2, top: r.top, left: sidebarWidth(),
               width: innerWidth };
    }"""

    # The swap's notice goes after 4 s; once a swap is caught, hold it up (no
    # timeout) so the stack's layout is checked with it there, at any speed.
    _HOLD_NOTICE = """() => { const n = banners.get('notice');
      if (!n || n.el.classList.contains('leaving')) return false;
      showBanner('notice', n.text.textContent, { kind: 'info' }); return true; }"""

    def assert_centred(self, stack):
        canvas = (stack["left"] + stack["width"]) / 2
        self.assertAlmostEqual(stack["centre"], canvas, delta=1, msg=stack)
        self.assertAlmostEqual(stack["top"], 12, delta=1)

    def test_paused_after_a_swap(self):
        b, gw = self.browser, self.gw
        self.page.click("#drawer-tab-log")      # the drawer opens on the Timeline
        deadline = time.monotonic() + 30
        while True:
            # A swap's new segment with a row before its first sample and
            # a later one after it, and the swap's notice still up.
            rows = self.listed()
            model = Model(gw.require_debug_state())
            last = max(model.segments)
            start = next(e["t_start"] for e in model.state["blackboard"] if e["seg"] == last)
            ok = [(key, model.record(*key)[0] >= start) for key in rows
                  if key[0] == last and start is not None]
            pair = next(((a, b_) for (a, a_ok), (b_, b_ok) in zip(ok, ok[1:])
                         if not a_ok and b_ok and b_ != rows[-1]), None)    # see click()
            if pair and last > 0 and self.page.evaluate(self._HOLD_NOTICE):
                break
            self.assertLess(time.monotonic(), deadline, "no swap with a row before a sample")
            time.sleep(0.1)
        early, later = pair
        self.click(later)                       # a moment with values
        self.assertTrue(self.view()["boards"])
        self.click(early)
        view = self.view()
        self.assertTrue(view["past"])
        self.assertEqual(view["boards"], {})
        self.assertEqual(self.page.text_content("#bb-empty"),
                         "No blackboard sample yet at this moment.")
        self.assertTrue(self.page.is_visible("#bb-empty"))
        stack = self.page.evaluate(self._STACK)
        self.assertEqual(stack["keys"][:1], ["notice"])
        self.assert_centred(stack)
        b.screenshot(SHOTS, "paused_after_swap_with_notice")
        self.page.click("#sidebar-collapse")
        stack = self.page.evaluate(self._STACK)
        self.assertEqual(stack["left"], 49)     # the collapsed strip
        self.assert_centred(stack)
        b.screenshot(SHOTS, "paused_sidebar_collapsed")
        self.page.click("#sidebar-collapse")    # the same button expands it
        self.page.click(BACK_TO_LIVE)
        self.page.wait_for_function("Object.keys(bbGroupEls).length > 0")


# The Timeline's playhead and the overview's knob: each shown and on its track.
_PLAYHEAD = """() => {
  const tl = KleinTimeline.debug(), p = document.getElementById('tl-playhead');
  const x = p.getBoundingClientRect().left;
  const knob = document.getElementById('ov-knob').getBoundingClientRect();
  const track = document.getElementById('ov-track').getBoundingClientRect();
  const k = (knob.left + knob.right) / 2;
  return [!p.hidden && x >= tl.left - 1 && x <= tl.left + tl.width + 1,
          k >= track.left - 1 && k <= track.right + 1];
}"""


class HundredThousandRowsTraceTest(DashboardCase):
    """100k rows scrolled by real wheel input and scrollbar-style jumps under a
    Chrome performance trace (CDP Tracing): the page never freezes, i.e. no
    main-thread task over 100 ms and no requestAnimationFrame gap over 200 ms.
    Whole frames, paint included, not only the scroll handler. A stall the
    page's thread mostly didn't run through (a loaded machine) is traced
    again."""

    ROWS = 100_000
    GATEWAY = {"extra_args": ("--record-buffer", "0")}     # the rows come from the test
    VIEWPORT = (WIDTH, HEIGHT)

    @classmethod
    def synthetic(cls, tree):
        """The backfill of a seeded 100k-record recording of ``tree``, as the page's frames."""
        tree_layout = layout(tree=tree, uids=names(tree))
        rec = Recording(keep_us=10 ** 15)
        t = 1_759_300_000_000_000
        rec.begin_segment(tree_layout, t, bytes(tree_layout.size))
        rng = random.Random(1)
        for _ in range(cls.ROWS // 1000):
            records = []
            for _ in range(1000):
                t += rng.choice((0, 5, 40, 2000))
                records.append((t, rng.choice(tree_layout.uids), rng.choice((1, 2, 3))))
            rec.append(records)
        rec.advance_head(t)
        return backfill_frames("robot", "synthetic", rec)

    def test_scrolling_100k_rows_never_freezes(self):
        with self.gw.watch() as ws:
            tree = ws.wait_for(lambda w: w.of_type("layout"))[0]["data"]
        page = self.page
        page.click("#drawer-tab-log")           # the drawer opens on the Timeline
        self.browser.feed(self.synthetic(tree))
        page.wait_for_function(f"KleinDrawer.debugRows().count === {self.ROWS}")
        box = page.locator("#drawer-body").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.evaluate("() => { const loop = () => requestAnimationFrame(loop); loop(); }")
        # A freeze is the page's own work. If its thread ran for under half of
        # a long task or frame gap, the machine stalled Chrome: trace again.
        for attempt in range(2):
            frames, tasks, wheeled = self.traced_scroll()
            a, b = max(zip(frames, frames[1:]), key=lambda g: g[1] - g[0])
            ran = sum(cpu * max(0, min(t + d, b) - max(t, a)) / d for t, d, cpu in tasks if d)
            _t, d, cpu = max(tasks, key=lambda task: task[1])
            if (b - a <= 200_000 or ran >= (b - a) / 2) and (d <= 100_000 or cpu >= d / 2):
                break
            print(f"\n[log] 100k trace: a {(b - a) / 1000:.1f} ms frame gap with the page"
                  f" running {ran / 1000:.1f} ms of it, the longest task {d / 1000:.1f} ms"
                  f" with {cpu / 1000:.1f} ms of CPU: traced again")
        gap = (b - a) / 1000
        longest = max(d for _t, d, _cpu in tasks) / 1000
        print(f"\n[log] 100k trace: {len(frames)} frames, max rAF gap {gap:.1f} ms, "
              f"{len(tasks)} main-thread tasks, max {longest:.2f} ms; wheel moved {wheeled}px")
        self.assertGreater(wheeled, 2000)
        self.assertGreater(len(tasks), 100)
        self.assertLess(longest, 100.0, "a main-thread task froze the page")
        self.assertLess(gap, 200.0, "a frame gap froze the page")

    def traced_scroll(self):
        """Scroll from the top under a trace: the renderer main thread's
        animation frames and tasks ((start, wall duration, thread CPU time)),
        in µs on the trace's clock, and how far the wheel moved."""
        page = self.page
        page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; }")
        cdp = page.context.new_cdp_session(page)
        cdp.send("Tracing.start", {"traceConfig": {"includedCategories": [
            "toplevel", "devtools.timeline", "disabled-by-default-devtools.timeline"]},
            "transferMode": "ReturnAsStream"})
        # 40 notches of 300 px: the same 12000 px as 100 of 120 px, in under half the time.
        for _ in range(40):
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(16)
        wheeled = page.evaluate("document.getElementById('drawer-body').scrollTop")
        for i in range(12):
            page.evaluate(f"() => {{ const b = document.getElementById('drawer-body');"
                          f" b.scrollTop = {(i * 7919) % 97 / 97} * b.scrollHeight; }}")
            page.wait_for_timeout(16)
        done = {}
        cdp.on("Tracing.tracingComplete", lambda e: done.setdefault("stream", e["stream"]))
        cdp.send("Tracing.end")
        while "stream" not in done:
            page.wait_for_timeout(20)
        chunks = []
        while True:
            r = cdp.send("IO.read", {"handle": done["stream"]})
            chunks.append(r["data"])
            if r.get("eof"):
                break
        cdp.detach()
        trace = json.loads("".join(chunks))
        events = trace["traceEvents"] if isinstance(trace, dict) else trace
        main = {(e["pid"], e["tid"]) for e in events if e.get("name") == "thread_name"
                and e.get("args", {}).get("name") == "CrRendererMain"}
        on_main = [e for e in events if (e.get("pid"), e.get("tid")) in main]
        tasks = [(e["ts"], e["dur"], e.get("tdur", e["dur"])) for e in on_main
                 if e.get("ph") == "X" and e.get("name") == "ThreadControllerImpl::RunTask"]
        frames = sorted(e["ts"] for e in on_main if e.get("name") == "FireAnimationFrame")
        return frames, tasks, wheeled


if __name__ == "__main__":
    unittest.main()
