"""Eviction as the dashboard shows it.

A Python ``Recording`` with a short window is streamed through
``klein.streaming`` straight into a real browser's store (the gateway runs
with ``--record-buffer 0``, so nothing else feeds it), then grown until
eviction drops its oldest chunk:

* the Log's first line and the Timeline's left edge say that history older
  than the kept span was dropped, with the span the recording really keeps
  (head back to the oldest record, as the chip);
* a paused cursor on a dropped record moves to the oldest kept record (its
  Log row selected) with a pill in the banner stack;
* scrolled up in the Log, the rows in view stay in view when older rows go;
* two million rows (more than a browser lets one element be tall at 20 px a
  row) still scroll end to end, and a row far down is revealed and selected.

Skipped when Playwright is missing.
"""
import random
import unittest

from klein.recording import Recording
from klein.streaming import Streamer
from tests.harness.model import fmt_span, names
from tests.helpers import layout
from tests.ui import DashboardCase

SHOTS = "eviction"
ROW = 20
T0 = 1_759_300_000_000_000
KEEP_US = 4_000_000                 # a 4 s window: 1 record per ms seals a chunk per ~1 s

# The Log as shown: the dropped line (if any) and every row element in view,
# top to bottom, with where each sits on screen.
_LOG_VIEW = """() => {
  const body = document.getElementById('drawer-body').getBoundingClientRect();
  const head = document.querySelector('.log-head').getBoundingClientRect().bottom;
  const visible = (r) => r.bottom > head + 1 && r.top < body.bottom - 1;
  const dropped = document.getElementById('log-dropped');
  const d = dropped.getBoundingClientRect();
  const rows = [...document.querySelectorAll('#log-rows .log-row:not([hidden])')]
    .map(el => ({ key: [Number(el.dataset.seg), Number(el.dataset.seq)],
                  top: el.getBoundingClientRect().top, bottom: el.getBoundingClientRect().bottom,
                  selected: el.classList.contains('selected') }))
    .filter(visible).sort((a, b) => a.top - b.top);
  return { dropped: dropped.hidden ? null : dropped.textContent,
           droppedTop: dropped.hidden ? null : d.top - head, headBottom: head, rows };
}"""

# Two million rows built in the page itself (the records frame of
# klein/streaming.py, one chunk start per 1024 records), so the test doesn't
# ship 18 MB through Playwright.
_SYNTH_JS = """([n, uids]) => {
  const T0 = 1759300000000000;
  recordingStore.ingest({ type: 'rec', source: 'robot', name: 'synthetic',
                          keep_us: 6e8 });
  const tree = window.__synthTree;
  const size = Math.max(...uids) + 1;
  recordingStore.ingest({ type: 'segment', seg: 0, layout_id: 1, t_begin: T0,
                          max_uid: size - 1, uids, start_seq: 0, state: new Array(size).fill(0),
                          layout: tree });
  for (let seq0 = 0; seq0 < n; seq0 += 1024) {
    const count = Math.min(1024, n - seq0);
    const buf = new ArrayBuffer(23 + size + 4 + 9 * count);
    const v = new DataView(buf);
    v.setUint8(0, 1); v.setUint32(1, 0, true);
    v.setUint32(5, seq0, true); v.setUint32(9, seq0, true);
    v.setBigInt64(13, BigInt(T0 + seq0 * 100), true); v.setUint16(21, size, true);
    v.setUint32(23 + size, count, true);
    for (let i = 0; i < count; i++) {
      const at = 23 + size + 4 + 9 * i;
      v.setUint32(at, i * 100, true);
      v.setUint16(at + 6, uids[(seq0 + i) % uids.length], true);
      v.setUint8(at + 8, 1 + (seq0 + i) % 3);
    }
    recordingStore.ingest(buf);
  }
  recordingStore.ingest({ type: 'head', seg: 0, t: T0 + (n - 1) * 100,
                          bytes: [n * 11, 0], capped: [] });
  recordingStore.ingest({ type: 'backfill_done' });
  requestRender({ boards: true });
}"""


class Synthetic:
    """A seeded recording of the mock's tree, streamed as the gateway would."""

    def __init__(self, tree):
        self.layout = layout(tree=tree, uids=names(tree))
        self.rec = Recording(keep_us=KEEP_US)
        self.frames = []
        self.streamer = Streamer(self.rec, lambda clients, f: self.frames.append(f),
                                 name="synthetic")
        self.rng = random.Random(12)
        self.t = T0
        self.rec.begin_segment(self.layout, T0, bytes(self.layout.size))
        self.rec.add_blackboard(T0, {"MainTree": {"n": 0}})
        self.grow(3000)
        self.streamer.subscribe(object())   # the backfill, then every change

    def grow(self, n):
        """n more records, 1 ms apart, then a drain's eviction and head."""
        records = []
        for _ in range(n):
            self.t += 1000
            records.append((self.t, self.rng.choice(self.layout.uids), self.rng.choice((1, 2, 3))))
        self.rec.append(records)
        self.rec.add_blackboard(self.t, {"MainTree": {"n": self.t}})
        self.rec.evict(self.t)
        self.rec.advance_head(self.t)

    def restart(self):
        """The same tree armed again: a new segment."""
        self.rec.begin_segment(self.layout, self.t, bytes(self.layout.size))

    def idle(self, us):
        """Time passes with no transitions: drains find nothing."""
        self.t += us
        self.rec.evict(self.t)
        self.rec.advance_head(self.t)

    def take(self):
        out, self.frames = self.frames, []
        return out


class EvictionTest(DashboardCase):
    GATEWAY = {"extra_args": ("--record-buffer", "0")}     # the frames come from the test

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with cls.gw.watch() as ws:
            cls.tree = ws.wait_for(lambda w: w.of_type("layout"))[0]["data"]
        # The chip as with a recording robot.
        cls.page.evaluate("() => { recordingSupport = 'on'; }")

    def setUp(self):
        self.page.click("#drawer-tab-log")
        self.browser.go_live()
        self.frame()

    def feed(self, synth):
        self.browser.feed(synth.take())
        self.frame()

    def zoom_to(self, span):
        """The Timeline's window at ``span`` µs (a step of its ladder), by its buttons."""
        for _ in range(30):
            now = self.page.evaluate("KleinTimeline.debug().span")
            if now == span:
                return
            self.page.click("#tl-zoom-in" if now > span else "#tl-zoom-out")
        self.fail(f"the window never reached {span} µs")

    def log_view(self):
        self.frame()
        return self.page.evaluate(_LOG_VIEW)

    def start(self):
        """A fresh recording in the page: 3 s kept of a 4 s window, nothing dropped."""
        synth = Synthetic(self.tree)
        self.feed(synth)
        self.assertEqual(self.page.evaluate("KleinDrawer.debugRows().count"), 3000)
        return synth

    def expected_note(self, synth):
        return (f"Transitions older than the kept {fmt_span(synth.rec.head - synth.rec.t_min)}"
                " were dropped")

    # ------------------------------------------------------------------ #
    def test_markers_and_the_rows_in_view_stay(self):
        synth = self.start()
        self.assertIsNone(self.log_view()["dropped"], "nothing dropped yet: no marker")
        # Scrolled up: row 1500 at the top.
        self.page.evaluate(f"() => {{ document.getElementById('drawer-body').scrollTop = {1500 * ROW}; }}")
        before = self.log_view()["rows"]
        top_key = before[0]["key"]
        self.assertEqual(top_key, [0, 1500])

        synth.grow(3000)                        # the window passes chunk 0's last record
        self.assertGreater(synth.rec.segments[0].start_seq, 0, "a chunk was evicted")
        self.assertLessEqual(synth.rec.segments[0].start_seq, 1500)
        self.feed(synth)
        after = self.log_view()
        self.assertEqual(after["rows"][0]["key"], top_key, "the same rows stay in view")
        self.assertAlmostEqual(after["rows"][0]["top"], before[0]["top"], delta=1)

        # The Log's first line says what was dropped, with the real kept span.
        self.page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; }")
        top = self.log_view()
        note = self.expected_note(synth)
        self.assertEqual(top["dropped"], note)
        self.assertAlmostEqual(top["droppedTop"], 0, delta=1)
        start = synth.rec.segments[0].start_seq
        self.assertEqual(top["rows"][0]["key"], [0, start], "the oldest kept row follows it")
        self.assertAlmostEqual(top["rows"][0]["top"] - top["headBottom"], ROW, delta=1)
        self.browser.screenshot(SHOTS, "log_dropped_marker")

        # The Timeline at a 5 s window, longer than the 4 s kept: the window
        # starts at the oldest record, and the dropped edge is the axis's left end.
        self.page.click("#drawer-tab-timeline")
        self.zoom_to(5_000_000)
        self.frame()
        mark = self.page.evaluate("""() => { const m = document.querySelector('#tl-bands .tl-dropped');
            if (!m) return null; const track = document.getElementById('tl-ruler-track').getBoundingClientRect();
            const note = document.getElementById('tl-dropped-note');
            return { text: note.hidden ? null : note.textContent, title: m.title,
                     x: m.getBoundingClientRect().left - track.left }; }""")
        self.assertIsNotNone(mark, "the Timeline marks the dropped edge")
        self.assertEqual(mark["text"], f"⇤ {note}", "its note, in the controls strip")
        self.assertEqual(mark["title"], note)
        self.assertAlmostEqual(mark["x"], 0, delta=1)
        debug = self.page.evaluate("KleinTimeline.debug()")
        self.assertEqual(debug["t0"], synth.rec.t_min)
        self.browser.screenshot(SHOTS, "timeline_dropped_marker")
        # Zoomed in to 1 s while live, the oldest record is off screen: no mark.
        self.zoom_to(1_000_000)
        self.frame()
        self.assertEqual(self.page.evaluate("document.querySelectorAll('#tl-bands .tl-dropped').length"), 0)
        self.assertTrue(self.page.evaluate("document.getElementById('tl-dropped-note').hidden"))

    def test_a_whole_dropped_segment_is_marked_too(self):
        """Eviction dropping an ended segment, and nothing of the next one."""
        synth = self.start()
        synth.restart()
        synth.grow(10)
        synth.idle(KEEP_US)                     # the first segment ends before the cutoff
        self.assertEqual([s.id for s in synth.rec.segments], [1])
        self.assertEqual(synth.rec.segments[0].t_start, synth.rec.segments[0].t_begin)
        self.feed(synth)
        self.page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; }")
        self.assertEqual(self.log_view()["dropped"], self.expected_note(synth))

    def test_a_paused_cursor_on_a_dropped_record_is_clamped(self):
        synth = self.start()
        self.page.evaluate("() => seek(KleinCursor.pause(0, 101))")     # just after record 100
        view = self.log_view()
        self.assertIn([0, 100], [r["key"] for r in view["rows"] if r["selected"]])
        self.assertFalse(self.page.is_visible("#bb-empty"), "a kept blackboard sample shows")
        self.assertEqual(self.page.evaluate("document.querySelectorAll('#banner-stack [data-key=\"evicted\"]').length"), 0)

        synth.grow(3000)
        start = synth.rec.segments[0].start_seq
        self.assertGreater(start, 101, "record 100 was evicted")
        self.feed(synth)
        clock = self.page.evaluate("clock")
        self.assertEqual(clock, {"mode": "paused", "seg": 0, "seq": start + 1},
                         "paused just after the oldest kept record")
        view = self.log_view()
        self.assertEqual([r["key"] for r in view["rows"] if r["selected"]], [[0, start]],
                         "its Log row is selected and in view")
        self.assertAlmostEqual(view["droppedTop"], 0, delta=1, msg="under the dropped line")
        pill = self.page.evaluate("""() => { const b = document.querySelector('#banner-stack [data-key="evicted"]');
            return b && b.querySelector('.banner-text').textContent; }""")
        self.assertEqual(pill, f"Older than the kept {fmt_span(synth.rec.head - synth.rec.t_min)}: "
                               "moved to the oldest kept transition")
        # Its blackboard was cut at the exact cutoff, after that record: dropped.
        self.assertEqual(self.page.text_content("#bb-empty"),
                         "Blackboard history from this moment was dropped.")
        viewing = self.page.evaluate("""() => document.querySelector('#banner-stack [data-key="viewing"] .banner-text').textContent""")
        self.assertTrue(viewing.startswith("Viewing t = "))
        self.browser.screenshot(SHOTS, "clamped_cursor_pill")
        # Once clamped, the cursor stays put: no second notice as more is dropped
        # while it is still kept.
        self.page.evaluate("() => hideBanner('evicted')")
        synth.grow(10)
        self.feed(synth)
        self.assertEqual(self.page.evaluate("clock"), clock)
        self.assertEqual(self.page.evaluate("document.querySelectorAll('#banner-stack [data-key=\"evicted\"]').length"), 0)

    def test_a_busy_live_timeline_still_reaches_the_head(self):
        """Following live, a window of 20k records (the most bars are drawn
        for) is rebuilt only as often as it can afford; a head that moves
        while a rebuild waits is still drawn (the wait's timer), with no
        further frame to ask for it."""
        uids = sorted(names(self.tree))
        self.page.evaluate("(tree) => { window.__synthTree = tree; }", self.tree)
        self.page.evaluate(_SYNTH_JS, [40_000, uids])       # 4 s at 10k/s
        self.page.click("#drawer-tab-timeline")
        self.zoom_to(2_000_000)                 # 20k records in the window
        self.frame()
        head = 1_759_300_000_000_000 + 3_000_000           # 3 s in: the window full
        # Nothing else renders from here on (the mock's status frames would).
        self.page.evaluate("() => { window.__requestRender = requestRender; requestRender = () => {}; }")
        self.addCleanup(self.page.evaluate, "() => { requestRender = window.__requestRender; }")
        # Two head moves in one task: the second lands inside the first's wait.
        drawn = self.page.evaluate("""(head) => {
            for (const dt of [0, 5e5]) {
                recordingStore.ingest({ type: 'head', seg: 0, t: head + dt,
                                        bytes: [0, 0], capped: [] });
                render();
            }
            const d = KleinTimeline.debug();
            return [d.t1, d.barsT1]; }""", head)
        self.assertEqual(drawn[0], head + 500_000, "the window follows the head")
        self.assertNotEqual(drawn[1], head + 500_000, "the bars wait their turn")
        # Generous: the wait is 8x a rebuild, which a loaded machine slows.
        self.page.wait_for_function(f"KleinTimeline.debug().barsT1 === {head + 500_000}",
                                    timeout=20000)

    def test_a_window_with_too_many_records_asks_to_zoom_in(self):
        """300k records in the default 30 s window: no bars, a note instead,
        while the axis and the playhead work; at 2 s (20k) the bars return."""
        uids = sorted(names(self.tree))
        self.page.evaluate("(tree) => { window.__synthTree = tree; }", self.tree)
        self.page.evaluate(_SYNTH_JS, [300_000, uids])      # 30 s at 10k/s
        self.page.click("#drawer-tab-timeline")
        self.page.evaluate("() => seek(KleinCursor.pause(0, 150001))")
        self.zoom_to(30_000_000)
        self.frame()
        read = """() => ({ note: document.getElementById('tl-too-many').hidden ? null
                                : document.getElementById('tl-too-many').textContent,
                         shapes: document.querySelectorAll('#tl-rows i').length,
                         ticks: document.querySelectorAll('#tl-ruler-track .tl-tick').length,
                         playhead: !document.getElementById('tl-playhead').hidden,
                         span: KleinTimeline.debug().span })"""
        wide = self.page.evaluate(read)
        self.assertEqual(wide["span"], 30_000_000)
        self.assertTrue(wide["note"].startswith("Zoom in to see bars: "), wide["note"])
        self.assertEqual(wide["shapes"], 0)
        self.assertGreater(wide["ticks"], 3)
        self.assertTrue(wide["playhead"])
        self.browser.screenshot(SHOTS, "timeline_zoom_in_to_see_bars")
        self.zoom_to(2_000_000)
        self.frame()
        narrow = self.page.evaluate(read)
        self.assertEqual(narrow["span"], 2_000_000)
        self.assertIsNone(narrow["note"])
        self.assertGreater(narrow["shapes"], 100)
        self.assertTrue(narrow["playhead"])

    def test_two_million_rows_scroll_end_to_end(self):
        n = 2_000_000
        uids = sorted(names(self.tree))
        self.page.evaluate("(tree) => { window.__synthTree = tree; }", self.tree)
        self.page.evaluate(_SYNTH_JS, [n, uids])
        self.page.wait_for_function("document.querySelectorAll('#log-rows .log-row:not([hidden])').length > 0")
        self.frame()
        spacer = self.page.evaluate("document.getElementById('log-rows').offsetHeight")
        self.assertLessEqual(spacer, 10_000_000, "the spacer stays under browser limits")
        self.assertLess(spacer, n * ROW)
        # Bottom: the newest row is the last one in view, flush with the bottom.
        self.page.evaluate("() => { const b = document.getElementById('drawer-body'); b.scrollTop = b.scrollHeight; }")
        view = self.log_view()
        self.assertEqual(view["rows"][-1]["key"], [0, n - 1])
        body_bottom = self.page.evaluate("document.getElementById('drawer-body').getBoundingClientRect().bottom")
        self.assertAlmostEqual(view["rows"][-1]["bottom"], body_bottom, delta=2)
        # Consecutive rows in view, 20 px apart.
        seqs = [r["key"][1] for r in view["rows"]]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
        # Halfway down the scrollbar: about halfway down the rows.
        self.page.evaluate("() => { const b = document.getElementById('drawer-body');"
                           " b.scrollTop = (b.scrollHeight - b.clientHeight) / 2; }")
        middle = self.log_view()["rows"][0]["key"][1]
        self.assertAlmostEqual(middle, n / 2, delta=50)
        # Top: the first row under the head.
        self.page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; }")
        self.assertEqual(self.log_view()["rows"][0]["key"], [0, 0])
        # A cursor far down (a Timeline seek, say) reveals and selects its row.
        self.page.evaluate("() => seek(KleinCursor.pause(0, 1500001))")
        view = self.log_view()
        self.assertIn([0, 1500000], [r["key"] for r in view["rows"] if r["selected"]])
        # ↑ steps to the row above, still in view.
        self.page.keyboard.press("ArrowUp")
        view = self.log_view()
        self.assertIn([0, 1499999], [r["key"] for r in view["rows"] if r["selected"]])
        # From the top to a row near the end: the overscan rows below it must
        # not grow the scroll range (that would change the scale).
        self.page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; }")
        height = self.page.evaluate("document.getElementById('drawer-body').scrollHeight")
        self.page.evaluate(f"() => seek(KleinCursor.pause(0, {n - 5 + 1}))")
        view = self.log_view()
        self.assertIn([0, n - 5], [r["key"] for r in view["rows"] if r["selected"]])
        seqs = [r["key"][1] for r in view["rows"]]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
        self.assertEqual(self.page.evaluate("document.getElementById('drawer-body').scrollHeight"),
                         height)


if __name__ == "__main__":
    unittest.main()
