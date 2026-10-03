"""The Save button.

Against the mock robot, in a real browser (Playwright), with the robot then
stopped so the recording stops growing and the zip can equal the per-run
routes read afterwards:

* with one tree, it offers one tree run, one click gives exactly one download,
  ``klein_<date>_<time>.zip``, and the "Saved …zip" pill names it;
  ``klein-bt --open`` on the unzipped ``.btlog`` gives the original's states
  and blackboard at 10 moments, and Save works there too;
* after tree swaps (mock ``--switch-every``), the zip holds one ``.btlog`` per
  tree run (plus its ``.bb.jsonl``), each equal to its route and opening to its
  own tree with the original's states; a ``.btlog`` opened without its sidecar
  says there is no blackboard (live and paused) and saves a zip with no
  ``.bb.jsonl``;
* a failed fetch (gateway gone, or an error status) shows "Save failed";
* disabled while nothing is recorded, the tooltip saying so; the tooltip
  counts tree runs, not segments. (Greyed out without a recording:
  ``test_render``.)

The saved zips go to a temporary folder. Skipped when Playwright is missing.
"""
import io
import json
import re
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from tests.harness.probes import GatewayProbe
from tests.harness.targets import free_port
from tests.ui import DashboardCase

SHOTS = "save"                      # screenshot group (KLEIN_SHOTS=1)
NODES = 13                      # the mock's default tree
POLL = 0.02                     # s: the mock steps once per status poll
MOMENTS = 10
ZIP_NAME = re.compile(r"klein_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.zip")

# The Save button as shown.
_SAVE = """() => { const b = document.getElementById('drawer-save');
  return { shown: b.getClientRects().length > 0, disabled: b.disabled, title: b.title,
           chip: document.getElementById('drawer-chip').dataset.state }; }"""

# Per segment: its tree and the span of its records, and its blackboard start.
_SEGMENTS = """() => { const R = KleinRecording, rec = shownRecording();
  return rec.segments.map(s => ({ tree: s.layout.root_tree_id, tEnd: s.tEnd,
    first: s.headSeq > s.startSeq ? R.timeAtSeq(s, s.startSeq + 1) : s.tStart,
    last: R.timeAtSeq(s, s.headSeq),
    records: s.headSeq - s.startSeq, bbStart: s.bb.tStart })); }"""

# [[segment index, t]] -> the mirror's state and blackboard there.
_AT = """(ask) => { const R = KleinRecording, rec = shownRecording();
  return ask.map(([i, t]) => { const s = rec.segments[i];
    return { state: Array.from(R.stateAt(s, t)), bb: R.bbAt(s, t) }; }); }"""

# The blackboard panel: its empty-state text, its boards, the past-moment note.
_PANEL = """() => ({
  empty: (e => e.hidden ? null : e.textContent)(document.getElementById('bb-empty')),
  groups: document.querySelectorAll('#bb-groups .bb-group').length,
  pastNote: getComputedStyle(document.getElementById('bb-past-note')).display !== 'none' })"""
NO_BOARD = {"empty": "No blackboard in this file.", "groups": 0, "pastNote": False}

_PILLS = """() => [...document.querySelectorAll('#banner-stack .banner')]
  .map(b => ({ key: b.dataset.key, text: b.textContent }))"""


def moments(lo, hi, n=MOMENTS):
    """``n`` whole-µs times from ``lo`` to ``hi``, both included."""
    return [lo + (hi - lo) * i // (n - 1) for i in range(n)]


def routes(gw, runs):
    """``{filename: bytes}`` from the per-run routes: what the zip must hold."""
    want = {}
    for run in runs:
        for kind in ("btlog", "bb.jsonl"):
            status, headers, body = gw.fetch(f"/log.{kind}?run={run['run']}")
            if kind == "btlog" or run["blackboard"]:
                assert status == 200, (kind, run, status)
                want[headers["Content-Disposition"].split('"')[1]] = body
            else:
                assert status == 404, (kind, run, status)
    return want


class _SaveCase(DashboardCase):
    """One browser for the class; gateways come and go. The saved files go to
    ``files``, a temporary folder."""

    ROBOT = GATEWAY = OPEN = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.files = Path(tempfile.mkdtemp(prefix="klein-save-"))
        cls.addClassCleanup(shutil.rmtree, cls.files, ignore_errors=True)

    @classmethod
    def show(cls, gw, nodes=NODES):
        cls.browser.base_url = gw.url
        cls.browser.open(wait_nodes=nodes)
        cls.frame()

    @classmethod
    def record_then_stop(cls, mock, gw, until, shot):
        """Show the live recording until ``until(runs)`` (screenshot ``shot``
        on the way), then stop the robot and wait until the gateway and the
        page both see the last segment end."""
        cls.show(gw)
        cls.page.wait_for_function("!document.getElementById('drawer-save').disabled")
        cls.browser.screenshot(SHOTS, shot)
        deadline = time.monotonic() + 30
        while not until(json.loads(gw.fetch("/log/runs")[2] or b"[]")):
            if time.monotonic() > deadline:
                raise AssertionError("the recording never got there")
            time.sleep(0.1)
        mock.stop()
        cls.page.wait_for_function(
            "(s => s.length && s[s.length - 1].tEnd !== null)(shownRecording().segments)",
            timeout=10000)
        runs = json.loads(gw.fetch("/log/runs")[2])
        assert runs[-1]["t_end"] is not None, runs
        return runs

    @classmethod
    def save(cls, name, shot=None):
        """Click Save once. Returns (the downloads' file names, the zip's
        ``{entry: bytes}``, the pills shown). The zip is kept as
        ``<files>/<name>.zip`` and unzipped into ``<files>/<name>/``."""
        got = []
        def on_download(download):
            got.append(download)
        cls.page.on("download", on_download)
        try:
            cls.page.click("#drawer-save")
            deadline = time.monotonic() + 10
            while not got and time.monotonic() < deadline:
                cls.page.wait_for_timeout(50)
            cls.page.wait_for_selector("#banner-stack .banner[data-key='saved']")
            pills = cls.page.evaluate(_PILLS)
            if shot:
                cls.browser.screenshot(SHOTS, shot)
            cls.page.wait_for_timeout(500)         # any second download would be here by now
        finally:
            cls.page.remove_listener("download", on_download)
        names = [download.suggested_filename for download in got]
        assert len(got) == 1, names
        assert got[0].failure() is None, got[0].failure()
        path = cls.files / f"{name}.zip"
        got[0].save_as(path)
        unzipped = cls.files / name
        shutil.rmtree(unzipped, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
            archive.extractall(unzipped)
            entries = {entry: archive.read(entry) for entry in archive.namelist()}
        return names, entries, pills

    def open_file(self, path):
        """``klein-bt --open path``, shown in the browser."""
        gw = GatewayProbe(1, extra_args=["--open", str(path)]).start()
        self.addCleanup(gw.stop)
        self.show(gw, nodes=1)
        self.page.wait_for_function("KleinDrawer.debugRows().count > 0")
        self.frame()
        return gw


class SaveOneTreeTest(_SaveCase):
    ROBOT = {}
    GATEWAY = {"poll_interval": POLL}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.runs = cls.record_then_stop(
            cls.robot, cls.gw, lambda runs: runs and runs[0]["t_begin"] < time.time() * 1e6 - 3e6,
            "live_one_tree")
        cls.segments = cls.page.evaluate(_SEGMENTS)
        cls.controls = cls.page.evaluate(_SAVE)
        cls.names, cls.entries, cls.pills = cls.save("one_tree", shot="saved_pill")

    def test_a_failed_save_says_so(self):
        """The gateway gone (fetch rejects) or an error status: a pill, no page error."""
        for stub in ("() => Promise.reject(new TypeError('Failed to fetch'))",
                     "() => Promise.resolve(new Response('', {status: 500}))"):
            with self.subTest(stub):
                self.page.evaluate(
                    f"() => {{ window._fetch = window.fetch; window.fetch = {stub}; }}")
                try:
                    self.page.click("#drawer-save")
                    self.page.wait_for_function(
                        "[...document.querySelectorAll('#banner-stack .banner')]"
                        ".some(b => b.textContent === 'Save failed')")
                finally:
                    self.page.evaluate("() => { window.fetch = window._fetch; hideBanner('saved'); }")

    def test_one_click_is_one_zip_named_in_the_pill(self):
        self.assertEqual(self.controls, {
            "shown": True, "disabled": False, "chip": "on",
            "title": "Save everything kept as one .zip · 1 tree run"})
        self.assertEqual(len(self.names), 1)
        self.assertRegex(self.names[0], ZIP_NAME)
        self.assertIn({"key": "saved", "text": f"Saved {self.names[0]}"}, self.pills)
        self.assertEqual(len(self.runs), 1)
        stem = self.runs[0]["filename"].removesuffix(".btlog")
        self.assertEqual(sorted(self.entries), [f"{stem}.bb.jsonl", f"{stem}.btlog"])

    def test_the_unzipped_file_reopens_with_the_same_states_and_blackboard(self):
        self.assertEqual(len(self.segments), 1)
        seg = self.segments[0]
        self.assertGreater(seg["records"], 50)
        self.assertIsNotNone(seg["bbStart"])
        ask = [[0, t] for t in moments(seg["first"], seg["last"])]
        ask_bb = [[0, t] for t in moments(max(seg["first"], seg["bbStart"]), seg["last"])]
        want = self.page.evaluate(_AT, ask + ask_bb)

        gw = self.open_file(self.files / "one_tree" / self.runs[0]["filename"])
        got = self.page.evaluate(_AT, ask + ask_bb)
        for (_, t), w, g in zip(ask, want, got):
            self.assertEqual(g["state"], w["state"], f"state at {t}")
        for (_, t), w, g in zip(ask_bb, want[MOMENTS:], got[MOMENTS:]):
            self.assertIsNotNone(w["bb"])
            self.assertEqual(g["bb"], w["bb"], f"blackboard at {t}")
        # In file mode, Save just works.
        self.assertEqual(self.page.evaluate(_SAVE), {
            "shown": True, "disabled": False, "chip": "file",
            "title": "Save everything kept as one .zip · 1 tree run"})
        names, entries, pills = self.save("one_tree_resaved_from_file", shot="saved_pill_file_mode")
        self.assertIn({"key": "saved", "text": f"Saved {names[0]}"}, pills)
        self.assertEqual(entries, routes(gw, json.loads(gw.fetch("/log/runs")[2])))
        self.assertEqual(len(entries), 2)


class SaveTreeSwapTest(_SaveCase):
    """Tree A, B, then A again, ~0.7 s each (mock ``--switch-every``), the
    robot then stopped. Two runs of one tree start >= 1.4 s apart, so in
    different seconds (the file names have whole seconds)."""

    RUNS = 3
    ROBOT = {"switch_every": 35}
    GATEWAY = {"poll_interval": POLL}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.runs = cls.record_then_stop(        # the last run too ran for a while
            cls.robot, cls.gw,
            lambda runs: (len(runs) >= cls.RUNS
                          and runs[cls.RUNS - 1]["t_begin"] < time.time() * 1e6 - 6e5),
            f"live_{cls.RUNS}_runs")
        cls.want = routes(cls.gw, cls.runs)
        cls.segments = cls.page.evaluate(_SEGMENTS)
        cls.asks = [[[i, t] for t in moments(seg["first"], seg["last"])]
                    for i, seg in enumerate(cls.segments)]
        cls.wants = [cls.page.evaluate(_AT, ask) for ask in cls.asks]
        cls.title = cls.page.evaluate(_SAVE)["title"]
        cls.names, cls.entries, _pills = cls.save("tree_swap")

    def test_one_btlog_per_tree_run_equal_to_its_route(self):
        self.assertEqual(len(self.names), 1)
        self.assertRegex(self.names[0], ZIP_NAME)
        self.assertEqual(self.entries, self.want)
        self.assertEqual(sorted(n for n in self.entries if n.endswith(".btlog")),
                         sorted(run["filename"] for run in self.runs))
        self.assertGreaterEqual(len(self.runs), 3)      # one more if the stop came late
        self.assertEqual([run["tree_id"] for run in self.runs],
                         ["MainTree", "PatrolTree"] * (len(self.runs) // 2)
                         + ["MainTree"] * (len(self.runs) % 2))
        self.assertEqual(self.title,
                         f"Save everything kept as one .zip · {len(self.runs)} tree runs")

    def test_each_opens_to_its_tree_and_states(self):
        """The first two runs, one of each tree."""
        self.assertEqual(len(self.segments), len(self.runs))
        for run, seg, ask, want in list(zip(self.runs, self.segments, self.asks, self.wants))[:2]:
            self.assertEqual(seg["tree"], run["tree_id"])
            self.open_file(self.files / "tree_swap" / run["filename"])
            reopened = self.page.evaluate(_SEGMENTS)
            self.assertEqual(len(reopened), 1)
            self.assertEqual(reopened[0]["tree"], run["tree_id"])
            got = self.page.evaluate(_AT, [[0, t] for _, t in ask])
            for (_, t), w, g in zip(ask, want, got):
                self.assertEqual(g["state"], w["state"], f"{run['filename']}: state at {t}")

    def test_a_file_opened_without_its_sidecar_has_no_blackboard(self):
        """The panel says so, live and paused, and Save leaves the sidecar out."""
        alone = self.files / "btlog_alone"
        alone.mkdir()
        shutil.copy(self.files / "tree_swap" / self.runs[0]["filename"], alone)
        self.open_file(alone / self.runs[0]["filename"])
        self.assertEqual(self.page.evaluate(_PANEL), NO_BOARD)
        _names, entries, _pills = self.save("btlog_alone_resaved")
        self.assertEqual(list(entries), [self.runs[0]["filename"]])
        self.page.click("#drawer-tab-log")
        self.page.evaluate("() => { document.getElementById('drawer-body').scrollTop = 0; }")
        self.frame()
        self.page.click('#log-rows .log-row[data-seq="3"]:not([hidden])')
        self.frame()
        self.assertTrue(self.page.evaluate("document.body.classList.contains('viewing-past')"))
        self.assertEqual(self.page.evaluate(_PANEL), NO_BOARD)
        self.browser.screenshot(SHOTS, "no_sidecar_paused")


class SaveEmptyTest(_SaveCase):
    def test_disabled_while_nothing_is_recorded_and_the_tooltip_counts_runs(self):
        """No robot: greyed out, "Nothing recorded yet". Then a hand-made mirror:
        segments of one layout (a restart or an outage) are one run, as
        Recording.runs(): A, A (restart), B, A is 3 runs in 4 segments."""
        gw = GatewayProbe(free_port()).start()          # no robot there
        self.addCleanup(gw.stop)
        self.show(gw, nodes=0)
        self.assertEqual(self.page.evaluate(_SAVE), {
            "shown": True, "disabled": True, "chip": "on", "title": "Nothing recorded yet"})
        title = self.page.evaluate("""() => {
          const a = {root_tree_id: 'A'}, b = {root_tree_id: 'B'};
          const seg = (layout) => ({layout, startSeq: 0, headSeq: 5, bb: {tStart: null}});
          KleinDrawer.showRecording({segments: [seg(a), seg(a), seg(b), seg(a)], head: 9e6,
                                     tMin: 1e6, bytes: [1, 1], capped: []}, "on");
          return document.getElementById('drawer-save').title; }""")
        self.assertEqual(title, "Save everything kept as one .zip · 3 tree runs")


if __name__ == "__main__":
    unittest.main()
