"""Opening a file: ``klein-bt --open FILE.btlog``.

The gateway runs with no robot on the t11 FileLogger2 fixture, copied next to
a ``.bb.jsonl`` sidecar (``tests.helpers.t11_sidecar``), and the dashboard is
checked in a real browser against the file itself, read by the harness's
independent reader, and against Python's ``state_at`` on the same file:

* the chip says "file: … · no robot", with no connection warning;
* the Log lists exactly the file's records (time, node, from → to);
* every position a stepped cursor reaches shows Python's state on the cards,
  and the sidecar's values on the blackboard panel; the board with no lines
  (DoorClosed::7) shows "—";
* dragging the sidebar redraws the Timeline, though a file sends nothing more.

A file opened without its sidecar is checked in ``test_save_ui``. Skipped when
Playwright is missing.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from klein.recording import decode_state
from tests.harness import btlog_ref
from tests.harness.probes import GatewayProbe
from tests.helpers import T11_BOARDS, T11_SIDECAR, opened, sidecar_at, t11_sidecar
from tests.ui import DashboardCase

SHOTS = "open"                      # screenshot group (KLEIN_SHOTS=1)
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
T11 = FIXTURES / "t11_filelogger2.btlog"

# The cursor and what the page shows for it, in one task.
_READ_VIEW = """() => {
  const rec = shownRecording();
  const pos = KleinCursor.cursorPos(clock, performance.now(), rec);
  const boards = {}, counts = {};
  for (const [name, g] of Object.entries(bbGroupEls)) {
    boards[name] = {};
    for (const [key, row] of Object.entries(g.rows)) boards[name][key] = row.value.textContent;
    counts[name] = g.count.textContent;
  }
  const empty = document.getElementById('bb-empty');
  return { pos, boards, counts, empty: empty.hidden ? null : empty.textContent,
           pastNote: getComputedStyle(document.getElementById('bb-past-note')).display !== 'none',
           banner: (v => v ? (v.querySelector('.banner-action') || {textContent: 'no button'})
                             .textContent : null)(
                   document.querySelector('#banner-stack [data-key="viewing"]')) };
}"""

# Every Log row, as the Log computes it (the DOM holds only the rows in view).
_ALL_ROWS = """() => { const R = KleinRecording, rows = R.logRows(shownRecording());
  return Array.from({length: rows.count}, (_, i) => { const r = R.logRowAt(rows, i);
    return [r.seg, r.seq, r.t, r.uid, r.from, r.to]; }); }"""

_CHIP = """() => ({ text: document.getElementById('drawer-chip-text').textContent,
  state: document.getElementById('drawer-chip').dataset.state,
  conn: document.getElementById('conn-text').textContent,
  dot: document.getElementById('conn-dot').className,
  banners: [...document.querySelectorAll('#banner-stack [data-key]')].map(b => b.dataset.key) })"""

STATUS = {0: "IDLE", 1: "RUNNING", 2: "SUCCESS", 3: "FAILURE", 4: "SKIPPED"}


class OpenFileTest(DashboardCase):
    ROBOT = None

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="klein-open-"))
        cls.addClassCleanup(shutil.rmtree, cls.dir, ignore_errors=True)
        cls.path = cls.dir / T11.name
        shutil.copyfile(T11, cls.path)
        cls.ref = btlog_ref.read(T11)
        cls.records = btlog_ref.absolute(cls.ref)
        cls.first = cls.ref.first_timestamp_us
        (cls.dir / "t11_filelogger2.bb.jsonl").write_text(t11_sidecar(cls.first))
        cls.segment = opened(cls.path).recording.segments[0]       # as Python loads it
        cls.uids = cls.segment.layout.uids
        super().setUpClass()
        cls.page.click("#drawer-tab-log")       # the drawer opens on the Timeline
        cls.page.wait_for_function("KleinDrawer.debugRows().count > 0")
        cls.frame()
        cls.browser.screenshot(SHOTS, "opened_log")

    @classmethod
    def make_gateway(cls, robot_port):
        # --robot-port names nothing: with --open klein never talks to a robot.
        return GatewayProbe(robot_port, extra_args=["--open", str(cls.path)])

    def view(self):
        self.frame()
        return self.page.evaluate(_READ_VIEW)

    def assert_view_at(self, view):
        """Cards = Python's state at the cursor; panel = the sidecar's values."""
        pos = view["pos"]
        seq, t = pos["seq"], pos["t"]
        want = decode_state(self.segment.state_at_seq(seq), self.uids)
        self.assertEqual(self.browser.state(), want, f"cards at seq {seq}")
        if self.segment.seq_at_time(t) == seq:      # not inside a burst sharing one µs
            self.assertEqual(self.segment.state_at(t), self.segment.state_at_seq(seq))
        boards = sidecar_at(T11_SIDECAR, t - self.first, T11_BOARDS)
        self.assertEqual(view["boards"], self.browser.value_summaries(boards),
                         f"panel at +{t - self.first} us")
        self.assertEqual(view["counts"]["DoorClosed::7"], "—")

    # -- the chip -------------------------------------------------------- #
    def test_the_chip_says_file_and_no_robot_and_nothing_warns(self):
        chip = self.page.evaluate(_CHIP)
        span = (self.records[-1][0] - self.first) // 1_000_000
        self.assertEqual(chip["text"], f"file: {T11.name} · {span} s · "
                                       f"{len(self.records)} transitions · no robot")
        self.assertEqual(chip["state"], "file")
        self.assertEqual(chip["conn"], f"No robot — viewing {T11.name}")
        self.assertEqual(chip["dot"], "dot file")
        self.assertNotIn("connection", chip["banners"])

    # -- the Log --------------------------------------------------------- #
    def test_the_log_lists_exactly_the_files_records(self):
        rows = self.page.evaluate(_ALL_ROWS)
        self.assertEqual(len(rows), len(self.records))
        state = btlog_ref.replay([], btlog_ref.tree_uids(self.ref.xml))
        for i, ((seg, seq, t, uid, frm, to), (ft, fuid, fst)) in enumerate(
                zip(rows, self.records)):
            before = btlog_ref.decode(state)[fuid]["status"]
            self.assertEqual((seg, seq, t, uid, frm, to),
                             (0, i, ft, fuid, before, STATUS[fst]), f"row {i}")
            btlog_ref.apply(state, fuid, fst)

    # -- a stepped cursor ------------------------------------------------ #
    def test_a_stepped_cursor_shows_python_state_at_every_position(self):
        self.browser.go_live()
        self.page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; }")
        self.frame()
        self.page.click('#log-rows .log-row[data-seq="0"]:not([hidden])')
        view = self.view()
        self.assertEqual(view["pos"]["seq"], 1)
        self.assertEqual(view["banner"], "no button")
        self.assertTrue(view["pastNote"])
        seen = []
        while not seen or view["pos"]["seq"] != seen[-1]:   # the last row: it stays put
            seen.append(view["pos"]["seq"])
            self.assert_view_at(view)
            self.browser.key("ArrowDown")
            view = self.view()
        self.assertEqual(seen, list(range(1, len(self.records) + 1)))
        # Back to the end (Esc): "live" is the file's last state.
        self.browser.key("Escape")
        view = self.view()
        self.assertTrue(view["pos"]["live"])
        self.assertEqual(view["pos"]["seq"], len(self.records))
        self.assertIsNone(view["banner"])
        self.assert_view_at(view)

    def test_a_sidebar_drag_redraws_the_timeline(self):
        # A file sends nothing more, so only the drag itself can ask for the
        # repaint: without it the axis keeps its old pixel positions.
        ticks = """() => { const t = document.getElementById('tl-ruler-track').getBoundingClientRect();
          return [...document.querySelectorAll('.tl-tick')]
            .map(e => (e.getBoundingClientRect().left - t.left) / t.width); }"""
        width = "() => document.getElementById('sidebar').offsetWidth"
        self.page.click("#drawer-tab-timeline")
        before = None
        for _ in range(50):                 # until two frames agree: laid out
            if before == (before := self.page.evaluate(ticks)):
                break
            self.frame()
        self.assertTrue(before, "no ticks")
        start = self.page.evaluate(width)
        try:
            self.browser.drag((start, 300), (start + 280, 300))
            # The repaint can take a frame or two under load; without it the
            # first tick never comes back to its place on the narrower track.
            self.page.wait_for_function(
                f"(() => {{ const t = ({ticks})(); return t.length"
                f" && Math.abs(t[0] - {before[0]}) < 0.005; }})()", timeout=5000)
            self.assertLessEqual(max(self.page.evaluate(ticks)), 1.0)
        finally:
            self.browser.drag((self.page.evaluate(width), 300), (start, 300))
            self.page.click("#drawer-tab-log")
            self.frame()


if __name__ == "__main__":
    unittest.main()
