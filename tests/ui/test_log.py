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
  ``BlackboardTrack.at(t)``, the row's "to" on its card, no flashing;
* the filter across both trees (every cell, Δ to the row above as listed),
  and a subtree-name click fills it without pausing;
* ↑/↓ across a segment boundary, filtered ↓ to the next match, ↑ from live
  to the newest row, and the keyboard guards (sidebar, filter input).

``BackToLiveTest``: back to live by the banner's button and by Esc, the robot
running: cards = the gateway's head, the Log follows the newest row, no note.
``AfterATreeSwapTest``: a row older than its segment's first blackboard
sample shows no values, and the "Viewing" pill heads the banner stack.
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
from tests.harness.probes import BACK_TO_LIVE
from tests.helpers import layout
from tests.ui import DashboardCase

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
# banner, the cards and the blackboard panel (each row's text by board/key).
_VIEW = """() => {
  const sel = document.querySelector('#log-rows .log-row.selected:not([hidden])');
  const boards = {};
  for (const [name, g] of Object.entries(bbGroupEls)) {
    boards[name] = {};
    for (const [key, row] of Object.entries(g.rows)) boards[name][key] = row.value.textContent;
  }
  const body = document.getElementById('drawer-body');
  return {
    selected: sel ? [Number(sel.dataset.seg), Number(sel.dataset.seq)] : null,
    past: document.body.classList.contains('viewing-past'),
    banner: (v => v ? v.querySelector('.banner-text').textContent : null)(
            document.querySelector('#banner-stack [data-key="viewing"]')),
    note: getComputedStyle(document.getElementById('bb-past-note')).display !== 'none',
    flashing: document.querySelectorAll('#bb-panel .bb-changed').length,
    atEnd: body.scrollTop + body.clientHeight >= body.scrollHeight - 1,
    cards: [...document.querySelectorAll('#canvas g.node')].map(g => [
      g.__data__.data.uid, g.querySelector('.node-status-text').textContent]),
    boards, segments: kleinDebug().segments,
  };
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

    def view(self):
        self.frame()
        self.browser.wait_for_nodes(1)          # a tree swap's exiting cards are gone
        return self.page.evaluate(_VIEW)

    def listed(self):
        return [tuple(k) for k in self.page.evaluate("() => KleinDrawer.debugRows().keys")]

    def scroll_to(self, index):
        self.page.evaluate(f"() => {{ document.getElementById('drawer-body').scrollTop = "
                           f"{index * ROW}; }}")
        self.frame()

    def click(self, key):
        """Scroll row ``key`` (seg, seq) into view and click it."""
        self.scroll_to(self.listed().index(key))
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
        cls.model = Model(cls.state, layouts)

    @staticmethod
    def ready(state):
        """The kept window holds a tree swap, a same-tree resume with rows,
        two blackboard samples, and an evicted oldest segment."""
        segs = state["segments"]
        pairs = list(zip(segs, segs[1:]))
        return (segs[0]["id"] > 0
                and sum(b["t_start"] is not None for b in state["blackboard"]) >= 2
                and any(a["layout_id"] != b["layout_id"] for a, b in pairs)
                and any(a["layout_id"] == b["layout_id"] and b["head_seq"] - b["start_seq"] > 3
                        for a, b in pairs))

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

    def assert_at(self, row):
        """The page shows the moment just after Log row ``row`` (a Model.rows item)."""
        key, cells, t, uid = row
        to = cells[5]
        v = self.view()
        seg, seq = key
        self.assertEqual(v["selected"], list(key))
        self.assertTrue(v["past"] and v["note"], "paused, with the blackboard note")
        self.assertEqual(v["flashing"], 0, "flash off in the past")
        self.assertEqual(v["banner"], f"Viewing t = {fmt_time(t)}")
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

    def test_a_row_click_shows_its_moment(self):
        rows = self.model.rows()
        cands = [r for r in rows if self.model.bb_at(r[0][0], r[2]) is not None]
        rng = random.Random(3)
        # One per segment with a sample, the rest anywhere.
        picks = [rng.choice([r for r in cands if r[0][0] == seg])
                 for seg in sorted({r[0][0] for r in cands})]
        picks += rng.sample([r for r in cands if r not in picks], 10 - len(picks))
        for row in picks:
            self.click(row[0])
            self.assert_at(row)
        # Flash off: two rows of one tree whose boards differ.
        tree = lambda r: self.model.segments[r[0][0]][0]["layout_id"]
        pair = next(((a, b) for a in cands for b in cands if tree(a) == tree(b)
                     and self.model.bb_at(a[0][0], a[2]) != self.model.bb_at(b[0][0], b[2])), None)
        if pair is not None:
            a, b = pair
            self.click(a[0])
            self.click(b[0])
            self.assertEqual(self.assert_at(b)["flashing"], 0)
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
            # Cards = the gateway's state at the browser's head (a records
            # frame can land between paint and read, so a few tries).
            for _ in range(10):
                v = self.view()
                gw = self.gw.require_debug_state()
                last = v["segments"][-1]
                g = next(s for s in gw["segments"] if s["id"] == last["id"])
                lag = g["head_seq"] - last["headSeq"]
                st = Model(gw).state_at_seq(last["id"], last["headSeq"])
                cards = dict(v["cards"])
                if 0 <= lag <= 20 and cards == {u: card_label(st[u]) for u in cards} \
                        and v["atEnd"]:
                    break
            self.assertEqual(cards, {u: card_label(st[u]) for u in cards},
                             f"{how}: cards vs gateway at browser head (lag {lag})")
            self.assertLessEqual(lag, 20, how)
            self.assertTrue(v["atEnd"], f"{how}: the Log follows the newest row while live")
            self.assertFalse(v["past"], how)
            self.assertIsNone(v["banner"], how)
            self.assertIsNone(v["selected"], how)
            self.assertFalse(v["note"], f"{how}: no blackboard note while live")


class AfterATreeSwapTest(LogView, DashboardCase):
    """Paused just after a tree swap: a row older than the new segment's first
    blackboard sample shows no values (not the panel's values from another
    moment), and the "Viewing t = ..." pill heads the one banner stack, above
    the swap's notice, centred over the canvas with the sidebar open or not."""

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
                         if not a_ok and b_ok), None)
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
        self.assertEqual(stack["keys"][:2], ["viewing", "notice"])
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


class HundredThousandRowsTraceTest(DashboardCase):
    """100k rows scrolled by real wheel input and scrollbar-style jumps under a
    Chrome performance trace (CDP Tracing): the page never freezes, i.e. no
    main-thread task over 100 ms and no requestAnimationFrame gap over 200 ms.
    Whole frames, paint included, not only the scroll handler. (Frame times
    are for ``scripts/measure_worst_case.py``; under a loaded test run they flake.)"""

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
        page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; window.__raf = [];"
                      " const loop = (t) => { __raf.push(t); requestAnimationFrame(loop); };"
                      " requestAnimationFrame(loop); }")
        cdp = page.context.new_cdp_session(page)
        cdp.send("Tracing.start", {"traceConfig": {"includedCategories": [
            "toplevel", "devtools.timeline", "disabled-by-default-devtools.timeline"]},
            "transferMode": "ReturnAsStream"})
        t0 = page.evaluate("performance.now()")
        # 40 notches of 300 px: the same 12000 px as 100 of 120 px, in under half the time.
        for _ in range(40):
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(16)
        wheeled = page.evaluate("document.getElementById('drawer-body').scrollTop")
        for i in range(12):
            page.evaluate(f"() => {{ const b = document.getElementById('drawer-body');"
                          f" b.scrollTop = {(i * 7919) % 97 / 97} * b.scrollHeight; }}")
            page.wait_for_timeout(16)
        t1 = page.evaluate("performance.now()")
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
        trace = json.loads("".join(chunks))
        events = trace["traceEvents"] if isinstance(trace, dict) else trace
        main = {(e["pid"], e["tid"]) for e in events if e.get("name") == "thread_name"
                and e.get("args", {}).get("name") == "CrRendererMain"}
        tasks = [e["dur"] / 1000 for e in events if (e.get("pid"), e.get("tid")) in main
                 and e.get("ph") == "X" and e.get("name") == "ThreadControllerImpl::RunTask"]
        raf = [t for t in page.evaluate("__raf") if t0 <= t <= t1]
        gaps = [b2 - a for a, b2 in zip(raf, raf[1:])]
        print(f"\n[log] 100k trace: {len(raf)} frames, max rAF gap {max(gaps):.1f} ms, "
              f"{len(tasks)} main-thread tasks, max {max(tasks):.2f} ms; wheel moved {wheeled}px")
        self.assertGreater(wheeled, 2000)
        self.assertGreater(len(tasks), 100)
        self.assertLess(max(tasks), 100.0, "a main-thread task froze the page")
        self.assertLess(max(gaps), 200.0, "a frame gap froze the page")


if __name__ == "__main__":
    unittest.main()
