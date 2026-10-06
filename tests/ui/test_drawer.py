"""The two panels around the canvas, the bottom drawer and the left sidebar, and
the camera that frames the canvas between them; and the drawer's recorder pill.

Checked in a real browser (Playwright) against the mock robot. Each panel
drags to resize within its clamps, keeps its size and collapsed state across a
reload, collapses by double-click or its fold button, and falls back to its
default when storage throws; R and F frame the space the panels leave, and
banners stay over it. The recorder pill shows the kept recording (its tooltip
the whole summary), its size-limit notes, and keeps its numbers in a narrow
window. Skipped when Playwright is missing.
"""
import re
import unittest

from tests.harness.model import fmt_span
from tests.harness.probes import GatewayProbe
from tests.harness.targets import MockTarget
from tests.ui import DashboardCase


SHOTS = "drawer"                    # screenshot group (KLEIN_SHOTS=1)
WIDTH, HEIGHT = 1400, 900
MIN_HEIGHT = 49                     # the 48 px transport row plus the drawer's top border: collapsed
TAB_ROW = 48                        # the tab row under it, shown only expanded
STRIP = 49                          # the collapsed sidebar: as wide as that row is tall

# Per panel: its default, sizes to drag to, the clamps (and a drag past each),
# a smaller window and the clamp there, its handle, fold button and collapsed size.
PANELS = {
    "drawer": dict(default=300, sizes=(200, 450, 650), min=MIN_HEIGHT + TAB_ROW, under=10,
                   max=int(HEIGHT * 0.8), over=HEIGHT - 5, small=(WIDTH, 600), small_max=480,
                   handle="#drawer-handle", button="#drawer-collapse", collapsed=MIN_HEIGHT,
                   kept=350, dragged=250),
    "sidebar": dict(default=320, sizes=(450, 250, 600), min=200, under=20,
                    max=WIDTH // 2, over=WIDTH - 20, small=(1000, HEIGHT), small_max=500,
                    handle="#sidebar-handle", button="#sidebar-collapse", collapsed=STRIP,
                    kept=420, dragged=300),
}

# Everything the panels move, read in one task: the drawer's box, the sidebar's
# width, the camera's visible viewport, the banner, and every card's box.
_GEOMETRY = """() => {
  const d = document.getElementById('drawer').getBoundingClientRect();
  const b = document.querySelector('#banner-stack .banner');
  return {
    drawerTop: d.top, drawerHeight: d.height, drawerLeft: d.left,
    sidebar: document.getElementById('sidebar').getBoundingClientRect().width,
    left: sidebarWidth(), view: visibleViewport(), width: innerWidth, height: innerHeight,
    watermark: document.getElementById('watermark').getBoundingClientRect().toJSON(),
    banner: b ? b.getBoundingClientRect().toJSON() : null,
    collapsed: {
      drawer: document.getElementById('drawer').classList.contains('collapsed'),
      sidebar: document.getElementById('sidebar').classList.contains('collapsed'),
    },
    k: d3.zoomTransform(svg.node()).k,
    cards: [...document.querySelectorAll('#canvas g.node')].map(g => {
      const r = g.querySelector('.node-rect').getBoundingClientRect();
      return { uid: g.__data__.data.uid, x: r.left, y: r.top, right: r.right, bottom: r.bottom };
    }),
  };
}"""

# F, pressed on the window as the camera keys are, returning the frontier it
# framed (from the displayed state, in the press's own task).
_PRESS_F = ("() => { window.dispatchEvent(new KeyboardEvent('keydown', {key: 'f'}));"
            " return runningFrontier().map(n => n.data.uid); }")
_PRESS_R = "() => window.dispatchEvent(new KeyboardEvent('keydown', {key: 'r'}))"
_TRANSFORM = "() => document.querySelector('#canvas g.draw-group').getAttribute('transform')"
_RUNNING = "Object.values(window.kleinDebug().displayed).some(e => e.status === 'RUNNING')"


class _PanelsCase(DashboardCase):
    """One mock, gateway and browser for the class; every test starts from
    the dashboard as first opened."""

    GATEWAY = {"poll_interval": 0.05}
    VIEWPORT = (WIDTH, HEIGHT)
    OPEN = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # No 500 ms camera flights (reduced motion): where R and F land is
        # checked, not the animation.
        cls.page.emulate_media(reduced_motion="reduce")

    def setUp(self):
        self.fresh()

    # -- helpers ---------------------------------------------------------- #
    def fresh(self):
        """The dashboard as first opened: default panels, connected, still."""
        b = self.browser
        b.base_url = self.gw.url
        b.page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
        b.open(wait_nodes=13, fresh=True)
        b.wait_connected()
        self.settle()

    def settle(self):
        """No card, link or camera transition left. Every camera move is a d3
        transition on #canvas, so that also means the camera is still (no
        ``wait_settled``, whose two reads cost 150 ms a call)."""
        b = self.browser
        b.page.wait_for_function("!document.getElementById('canvas').__transition", timeout=5000)
        b.wait_for_nodes(1)

    def geometry(self):
        return self.browser.page.evaluate(_GEOMETRY)

    def drag_to(self, panel, size):
        """Drag the drawer's top edge to ``size`` px above the window's bottom,
        or the sidebar's right edge to ``size`` px from the window's left."""
        g = self.geometry()
        if panel == "drawer":
            x = g["left"] + (g["width"] - g["left"]) / 2
            self.browser.drag((x, g["drawerTop"]), (x, g["height"] - size))
        else:
            self.browser.drag((g["sidebar"], g["height"] / 3), (size, g["height"] / 3))

    def assert_size(self, panel, expected, msg=None):
        """The panel's size; for the sidebar, the camera's and the drawer's
        left edges agree with it."""
        g = self.geometry()
        if panel == "drawer":
            self.assertAlmostEqual(g["drawerHeight"], expected, delta=1, msg=msg)
        else:
            for got in (g["sidebar"], g["left"], g["view"]["left"], g["drawerLeft"]):
                self.assertAlmostEqual(got, expected, delta=1, msg=msg)
        return g

    def assert_visible(self, g, uids, what):
        """Each card lies wholly in the visible viewport: right of the sidebar,
        below the banner strip, above the drawer and the hint line riding on it."""
        cards = {c["uid"]: c for c in g["cards"]}
        for uid in uids:
            c = cards[uid]
            self.assertGreaterEqual(c["x"], g["left"] - 1, f"{what}: uid {uid} under the sidebar")
            self.assertLessEqual(c["right"], g["width"] + 1, f"{what}: uid {uid} off the right")
            self.assertGreaterEqual(c["y"], g["view"]["top"] - 1, f"{what}: uid {uid} off the top")
            self.assertLessEqual(c["bottom"], g["drawerTop"] + 1,
                                 f"{what}: uid {uid} under the drawer ({c} vs {g['drawerTop']})")
            self.assertLessEqual(c["bottom"], g["watermark"]["top"] + 1,
                                 f"{what}: uid {uid} under the hint line")

    def assert_framed(self, what, centred=False):
        """After R the whole tree fits the visible viewport and fills it one
        way (and, with ``centred``, is centred across it); after F the running
        frontier lies in it."""
        b = self.browser
        b.page.evaluate(_PRESS_R)
        self.settle()
        g = self.geometry()
        self.assertEqual(len(g["cards"]), 13, what)
        self.assert_visible(g, [c["uid"] for c in g["cards"]], f"R, {what}")
        xs = [c["x"] for c in g["cards"]] + [c["right"] for c in g["cards"]]
        ys = [c["y"] for c in g["cards"]] + [c["bottom"] for c in g["cards"]]
        fill = max((max(xs) - min(xs)) / (g["width"] - g["left"]),
                   (max(ys) - min(ys)) / g["view"]["height"])
        self.assertGreater(fill, 0.6, f"R, {what}: not fitted")
        if centred:
            self.assertAlmostEqual((min(xs) + max(xs)) / 2, (g["left"] + g["width"]) / 2,
                                   delta=2, msg=f"R, {what}: not centred")
        frontier = self.press_f()
        self.assertTrue(frontier, what)
        self.settle()
        self.assert_visible(self.geometry(), frontier, f"F, {what}")

    def press_f(self):
        """F while something runs; the frontier it framed. A lap can end
        between the wait and the press, so a press that framed nothing is
        tried again."""
        for _ in range(10):
            self.browser.page.wait_for_function(_RUNNING)
            frontier = self.browser.page.evaluate(_PRESS_F)
            if frontier:
                return frontier
        return []

    def set_layout(self, value):
        self.browser.page.click(f'input[name="layout"][value="{value}"] + span')
        self.settle()


class PanelsTest(_PanelsCase):
    """The drawer and the sidebar, each in a subTest of the same checks."""

    def test_dragging_resizes_within_the_clamps(self):
        b = self.browser
        for panel, p in PANELS.items():
            with self.subTest(panel):
                self.fresh()
                self.assert_size(panel, p["default"], "the default")
                for size in p["sizes"]:
                    self.drag_to(panel, size)
                    self.assert_size(panel, size)
                self.drag_to(panel, p["under"])             # below the minimum
                self.assert_size(panel, p["min"])
                self.drag_to(panel, p["over"])              # past the maximum
                self.assert_size(panel, p["max"])
                # A smaller window clamps it again, and the window's own size
                # gives the chosen size back.
                w, h = p["small"]
                b.page.set_viewport_size({"width": w, "height": h})
                try:
                    self.browser.page.wait_for_function(
                        "(n) => Math.abs(document.getElementById(n[0]).getBoundingClientRect()"
                        "[n[1]] - n[2]) < 1", arg=[panel, "height" if panel == "drawer"
                                                   else "width", p["small_max"]])
                    if panel == "sidebar":
                        self.assertAlmostEqual(self.geometry()["drawerLeft"], p["small_max"],
                                               delta=1)
                finally:
                    b.page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
                if panel == "sidebar":
                    b.page.wait_for_function(
                        f"document.getElementById('sidebar').offsetWidth === {p['max']}")
                b.page.evaluate(_PRESS_R)
                self.settle()
                b.screenshot(SHOTS, f"{panel}_at_max")

    def test_the_size_survives_a_reload_and_collapse_toggles(self):
        b = self.browser
        for panel, p in PANELS.items():
            with self.subTest(panel):
                self.fresh()
                self.drag_to(panel, p["kept"])
                b.open(wait_nodes=13)
                self.assert_size(panel, p["kept"], "after a reload")

                b.page.locator(p["handle"]).dblclick()
                g = self.assert_size(panel, p["collapsed"], "collapsed")
                self.assertTrue(g["collapsed"][panel])
                self.assertEqual(b.page.get_attribute(p["button"], "aria-expanded"), "false")
                if panel == "drawer":               # the strip is the transport row alone
                    self.assertTrue(b.page.is_visible("#drawer-transport"))
                    self.assertFalse(b.page.is_visible("#drawer-toolbar"))
                if panel == "sidebar":              # the strip keeps only the fold button
                    self.assertFalse(b.page.is_visible("#sidebar-header"))
                    self.assertFalse(b.page.is_visible("#bb-panel"))
                    self.assertEqual(b.page.get_attribute(p["button"], "aria-label"),
                                     "Expand panel")
                b.open(wait_nodes=13)
                self.assertTrue(self.geometry()["collapsed"][panel], "collapsed after a reload")
                self.assert_size(panel, p["collapsed"])
                b.page.locator(p["handle"]).dblclick()
                self.assertFalse(self.geometry()["collapsed"][panel])
                self.assert_size(panel, p["kept"], "expanding restores the size")
                self.assertEqual(b.page.get_attribute(p["button"], "aria-expanded"), "true")

                b.page.click(p["button"])           # the fold button does the same
                self.assert_size(panel, p["collapsed"])
                b.page.click(p["button"])
                self.assert_size(panel, p["kept"])
                self.assertNotEqual(b.page.evaluate("() => document.activeElement.id"),
                                    p["button"][1:],
                                    "a mouse click lets go of the focus, so R/F work right away")

                b.page.locator(p["handle"]).dblclick()      # dragging a collapsed panel
                self.drag_to(panel, p["dragged"])           # opens it at the dragged size
                self.assertFalse(self.geometry()["collapsed"][panel])
                self.assert_size(panel, p["dragged"])
                b.open(wait_nodes=13)
                self.assert_size(panel, p["dragged"], "the dragged size after a reload")

    def test_storage_that_throws_leaves_the_default_panels(self):
        page = self.browser.browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.add_init_script("Object.defineProperty(window, 'localStorage',"
                                 " { get() { throw new Error('storage blocked'); } });")
            page.goto(self.gw.url)
            page.wait_for_function("document.querySelectorAll('#canvas g.node').length === 13")
            for panel, p in PANELS.items():
                with self.subTest(panel):
                    size = (f"() => document.getElementById('{panel}')"
                            f".{'offsetHeight' if panel == 'drawer' else 'offsetWidth'}")
                    self.assertEqual(page.evaluate(size), p["default"])
                    page.dblclick(p["handle"])              # saving throws too
                    self.assertEqual(page.evaluate(size), p["collapsed"])
        finally:
            page.close()
        self.assertEqual(errors, [])

    def test_banners_stay_over_the_visible_canvas(self):
        """At every panel size the banner and the hint line lie above the
        drawer, and the banner is centred over the canvas right of the sidebar."""
        b = self.browser
        for panel, p in PANELS.items():
            with self.subTest(panel):
                self.fresh()
                b.page.evaluate("() => showBanner('test', 'A banner over the canvas')")
                b.page.wait_for_function("document.querySelector('#banner-stack .banner')"
                                         ".getAnimations().length === 0")     # its slide in
                try:
                    for size in (p["min"], *p["sizes"][:2], p["max"], None):
                        if size is None:
                            b.page.click(p["button"])
                        else:
                            self.drag_to(panel, size)
                        g = self.geometry()
                        where = f"{panel} at {size or 'collapsed'}"
                        for name in ("banner", "watermark"):
                            box = g[name]
                            self.assertIsNotNone(box, where)
                            self.assertGreater(box["height"], 0, f"{name}, {where}")
                            self.assertGreaterEqual(box["top"], 0, f"{name}, {where}")
                            self.assertLessEqual(box["bottom"], g["drawerTop"],
                                                 f"{name} under the drawer, {where}")
                        self.assertAlmostEqual((g["banner"]["left"] + g["banner"]["right"]) / 2,
                                               (g["left"] + g["width"]) / 2, delta=1, msg=where)
                finally:
                    b.page.evaluate("() => hideBanner('test')")


class CameraTest(_PanelsCase):
    """R and F frame the canvas the panels leave."""

    def test_r_and_f_frame_the_space_the_panels_leave(self):
        b = self.browser
        with self.subTest("F focuses the frontier, R goes back"):
            loaded = b.page.evaluate(_TRANSFORM)
            g = self.geometry()                             # as loaded: the same fit as R
            self.assertEqual(len(g["cards"]), 13)
            self.assert_visible(g, [c["uid"] for c in g["cards"]], "on load")
            frontier = self.press_f()
            self.assertTrue(frontier)
            focused = b.page.evaluate(
                "() => [...document.querySelectorAll('#canvas g.node')]"
                ".filter(g => g.querySelector('.node-rect.focused'))"
                ".map(g => g.__data__.data.uid)")
            self.assertEqual(sorted(focused), sorted(frontier))
            self.settle()
            self.assertNotEqual(b.page.evaluate(_TRANSFORM), loaded)
            self.assert_visible(self.geometry(), frontier, "F")
            b.key("r")
            self.settle()
            self.assertEqual(b.page.evaluate(_TRANSFORM), loaded)

        with self.subTest("drawer"):
            self.fresh()
            for layout in ("vertical", "horizontal"):
                self.set_layout(layout)
                for height in (200, 400, 600):
                    self.drag_to("drawer", height)
                    self.assert_size("drawer", height)
                    self.assert_framed(f"{layout}, drawer {height} px")
                    b.screenshot(SHOTS, f"{layout}_drawer_{height}")
            self.set_layout("vertical")
            b.page.click("#drawer-collapse")
            self.assertTrue(self.geometry()["collapsed"]["drawer"])
            self.assert_framed("drawer collapsed")
            b.screenshot(SHOTS, "drawer_collapsed")

        with self.subTest("sidebar"):
            self.fresh()
            p = PANELS["sidebar"]
            for width in (p["min"], 450, p["max"]):
                self.drag_to("sidebar", width)
                self.assert_size("sidebar", width)
                self.assert_framed(f"sidebar {width} px", centred=True)
            # Folding nudges the camera by half the change, so the tree stays
            # centred in the space left.
            b.page.evaluate(_PRESS_R)
            self.settle()
            centre = "() => { const c = [...document.querySelectorAll('#canvas .node-rect')]" \
                     ".map(e => e.getBoundingClientRect()); return (Math.min(...c.map(r => r.left))" \
                     " + Math.max(...c.map(r => r.right))) / 2; }"
            before = b.page.evaluate(centre)
            b.page.click("#sidebar-collapse")
            self.settle()
            self.assertAlmostEqual(b.page.evaluate(centre) - before, (STRIP - p["max"]) / 2,
                                   delta=2)
            self.assert_size("sidebar", STRIP)
            self.assert_framed("sidebar collapsed", centred=True)
            b.screenshot(SHOTS, "sidebar_collapsed")

    def test_r_stops_at_one_to_one(self):
        """The small patrol tree, with both panels collapsed, would fit at ~1.4:
        R never zooms in past 1:1."""
        b = self.browser
        with MockTarget(tree="patrol") as robot, GatewayProbe(robot.port) as gw:
            b.base_url = gw.url
            b.open(wait_nodes=1, fresh=True)
            self.settle()
            b.page.click("#drawer-collapse")
            b.page.click("#sidebar-collapse")
            self.settle()
            b.page.evaluate(_PRESS_R)
            self.settle()
            g = self.geometry()
            self.assertAlmostEqual(g["k"], 1.0, places=6)
            self.assert_visible(g, [c["uid"] for c in g["cards"]], "R, patrol")
            b.screenshot(SHOTS, "patrol_r_one_to_one")

    def test_camera_keys_pressed_in_the_drawer_belong_to_the_drawer(self):
        b = self.browser
        before = b.page.evaluate(_TRANSFORM)
        b.page.focus("#drawer-tab-log")
        b.key("f")
        b.key("r")
        b.page.wait_for_timeout(200)
        self.assertEqual(b.page.evaluate(_TRANSFORM), before)


class ChipTest(_PanelsCase):
    """The drawer's recorder pill (#drawer-chip and Save)."""

    def test_the_chip_shows_the_kept_recording(self):
        b = self.browser
        b.page.wait_for_function("document.getElementById('drawer-chip').title"
                                 ".includes('transitions')")
        text, shown, count, note = b.page.evaluate(
            "() => [document.getElementById('drawer-chip').title,"
            " [...document.querySelectorAll('#drawer-chip-text > span')].map(e => e.textContent),"
            " window.kleinDebug().segments.reduce((n, s) => n + s.headSeq - s.startSeq, 0),"
            " document.getElementById('drawer-note').hidden]")
        self.assertRegex(text, r"^Recording · last \d+ s · \d+(\.\d)?k? transitions · \d+ kB$")
        # The pill itself: the span kept and the size, as in the tooltip.
        span, size = re.match(r"Recording · last (\d+ s) · .* · (\d+ kB)$", text).groups()
        self.assertEqual(shown, ["Recording", f"{span} · {size}"])
        shown = re.search(r"· ([\d.]+)(k?) transitions", text)
        self.assertAlmostEqual(float(shown.group(1)) * (1000 if shown.group(2) else 1), count,
                               delta=max(30, count * 0.06))     # a few polls apart
        self.assertTrue(note, "no size cap has cut anything")
        self.assertEqual(b.page.get_attribute("#drawer-chip", "data-state"), "on")

    def test_chip_wording(self):
        """``KleinDrawer.summary`` on hand-made mirrors: spans, counts, sizes, caps."""
        cases = """() => {
          const seg = (startSeq, headSeq, bbStart) => ({startSeq, headSeq, bb: {tStart: bbStart}});
          const rec = (o) => Object.assign({segments: [seg(0, 4800, 1e6)], head: 601e6,
                                            tMin: 1e6, bytes: [60000, 40000], capped: []}, o);
          const s = (r, support = "on") => KleinDrawer.summary(r, support);
          return [
            s(rec({})),
            s(rec({tMin: 601e6 - 14 * 60e6})),                       // a slow tree keeps more
            s(rec({segments: [seg(0, 1000000, 1e6), seg(0, 2500000, 2e6)],
                   bytes: [180e6, 64e6], capped: ["blackboard"], head: 601e6, tMin: 1e6})),
            s(rec({capped: ["transitions", "blackboard"], tMin: 421e6,
                   segments: [seg(10, 20, 541e6)]})),
            s(rec({head: 3600e6 + 3.9e8, tMin: 0})),
            s(rec({bytes: null})),
            s(rec({bytes: [300, 100]})), s(rec({bytes: [400, 100]})),
            s(rec({}), "off"), s(rec({}), "unsupported"), s(undefined, null),
            s(rec({segments: [], head: null, tMin: null})),
          ];
        }"""
        got = self.browser.page.evaluate(cases)
        self.assertEqual(got, [
            {"state": "on", "text": "Recording · last 10 min · 4.8k transitions · 100 kB",
             "note": ""},
            {"state": "on", "text": "Recording · last 14 min · 4.8k transitions · 100 kB",
             "note": ""},
            {"state": "on", "text": "Recording · last 10 min · 3.5M transitions · 244 MB",
             "note": "blackboard history: last 10 min (size limit)"},
            {"state": "on", "text": "Recording · last 3 min · 10 transitions · 100 kB",
             "note": "transitions: last 3 min (size limit) · "
                     "blackboard history: last 1 min (size limit)"},
            {"state": "on", "text": "Recording · last 1 h 7 min · 4.8k transitions · 100 kB",
             "note": ""},
            {"state": "on", "text": "Recording · last 10 min · 4.8k transitions", "note": ""},
            {"state": "on", "text": "Recording · last 10 min · 4.8k transitions · <1 kB",
             "note": ""},
            {"state": "on", "text": "Recording · last 10 min · 4.8k transitions · 1 kB",
             "note": ""},
            {"state": "off", "text": "Recording off (--record-buffer 0)", "note": ""},
            {"state": "off", "text": "Recording needs BehaviorTree.CPP ≥ 4.3.3", "note": ""},
            {"state": "on", "text": "Recording · waiting for the robot", "note": ""},
            {"state": "on", "text": "Recording · waiting for the robot", "note": ""},
        ])

    def test_a_narrow_window_keeps_the_chips_numbers(self):
        """At 1000 px with the sidebar shown, a capped pill loses its note first
        and then "Recording", never the numbers; the titles hold it all. Both
        drawer rows stay on one line, nothing overlapping."""
        b = self.browser
        b.page.set_viewport_size({"width": 1000, "height": HEIGHT})
        try:
            # Every render shows this mirror instead of the live one.
            b.page.evaluate("""() => {
              const show = KleinDrawer.showRecording;
              const seg = {startSeq: 0, headSeq: 3500000, bb: {tStart: 421e6}};
              const fake = {segments: [seg], head: 601e6, tMin: 1e6, bytes: [180e6, 64e6],
                            capped: ["transitions", "blackboard"]};
              KleinDrawer.showRecording = () => show(fake, "on");
              show(fake, "on");
            }""")
            b.page.evaluate(_PRESS_R)       # the camera doesn't follow a resize by itself
            self.settle()
            chip = b.page.evaluate("""() => {
              const box = (el) => el.getBoundingClientRect();
              const stats = document.querySelector('#drawer-chip .drawer-chip-stats');
              const note = document.getElementById('drawer-note');
              return {
                statsText: stats.textContent, statsCut: stats.scrollWidth > stats.clientWidth,
                chipRight: box(document.getElementById('drawer-chip')).right,
                drawerRight: box(document.getElementById('drawer')).right,
                chipTitle: document.getElementById('drawer-chip').title,
                noteTitle: note.title, noteHidden: note.hidden,
                text: document.getElementById('drawer-chip-text').textContent,
                // Every item of both rows: its box, in order.
                rows: ['drawer-transport', 'drawer-toolbar'].map(id =>
                  [...document.getElementById(id).querySelectorAll(':scope > :not([hidden])')]
                    .map(e => box(e).toJSON())),
              };
            }""")
            b.screenshot(SHOTS, "narrow_1000_capped")
            # At 700 px (a 380 px drawer) both rows still fit, in both tabs,
            # live and in the past: the page never scrolls sideways.
            b.page.set_viewport_size({"width": 700, "height": HEIGHT})
            fits = b.page.evaluate(_FITS)
        finally:
            b.page.reload()
            b.page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
        for tab, past, page_width, window, rows in fits:
            with self.subTest(tab=tab, past=past):
                self.assertLessEqual(page_width, window, "the page scrolls sideways")
                for content, room in rows:
                    self.assertLessEqual(content, room, "a drawer row runs past its edge")
        self.assertEqual(chip["statsText"], "10 min · 244 MB")
        self.assertFalse(chip["statsCut"], "the numbers were cut off")
        self.assertLessEqual(chip["chipRight"], chip["drawerRight"])
        self.assertEqual(chip["text"], "Recording10 min · 244 MB")
        self.assertEqual(chip["chipTitle"], "Recording · last 10 min · 3.5M transitions · 244 MB")
        for row in chip["rows"]:
            for a, b_ in zip(row, row[1:]):
                self.assertLessEqual(a["right"], b_["left"] + 0.5, "side by side, no overlap")
                self.assertAlmostEqual(a["top"] + a["height"] / 2, b_["top"] + b_["height"] / 2,
                                       delta=1, msg="on one line")
            self.assertLessEqual(row[-1]["right"], chip["drawerRight"])
        self.assertFalse(chip["noteHidden"])
        self.assertEqual(chip["noteTitle"], "transitions: last 10 min (size limit) · "
                                            "blackboard history: last 3 min (size limit)")


# Per tab, live and paused: the page's scroll width, the window's, and each
# drawer row's content width (its last item's right edge too) and room.
_FITS = """async () => {
  const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const out = [];
  for (const tab of ['timeline', 'log']) {
    document.getElementById('drawer-tab-' + tab).click();
    for (const past of [false, true]) {
      const s = recordingStore.recording.segments.at(-1);
      seek(past ? KleinCursor.pause(s.id, s.headSeq - 1) : KleinCursor.live());
      await frame();
      out.push([tab, past, document.scrollingElement.scrollWidth, innerWidth,
        ['drawer-transport', 'drawer-toolbar'].map(id => {
          const row = document.getElementById(id), r = row.getBoundingClientRect();
          const items = [...row.querySelectorAll(':scope > :not([hidden])')];
          const right = Math.max(...items.map(e => e.getBoundingClientRect().right));
          return [Math.max(row.scrollWidth, right - r.left), row.clientWidth];
        })]);
    }
  }
  return out;
}"""


# -- size limits ------------------------------------------------------------ #
_CHIP = """() => {
  const rec = recordingStore.recording;
  return {
    text: document.getElementById('drawer-chip').title,
    note: document.getElementById('drawer-note').textContent,
    noteHidden: document.getElementById('drawer-note').hidden,
    head: rec ? rec.head : null, tMin: rec ? rec.tMin : null,
    bbStarts: rec ? rec.segments.map(s => s.bb.tStart) : null,
  };
}"""


class CappedGatewayProbe(GatewayProbe):
    """The gateway with tiny size caps and 64-record chunks, so the caps bind
    within seconds. Only the gateway's Python model is patched."""

    MAX_BYTES = 6_000
    BB_MAX_BYTES = 400
    _CODE = ("import sys, functools; from klein import cli, gateway, recording; "
             "recording.CHUNK_SIZE = 64; "
             "cli.Recording = functools.partial(recording.Recording, "
             "max_bytes={mb}, bb_max_bytes={bb}); "
             "gateway.POLL_INTERVAL = float(sys.argv.pop(1)); cli.main_cli()")

    def __init__(self, robot_port, **kwargs):
        super().__init__(robot_port, poll_interval=0.02, **kwargs)
        self.launcher = ["-c", self._CODE.format(mb=self.MAX_BYTES, bb=self.BB_MAX_BYTES),
                         "0.02"]


class ForcedCapTest(DashboardCase):
    """Tiny caps: both "(size limit)" notes appear, worded from the mirror,
    and each head frame reports the size after that drain's eviction."""

    VIEWPORT = (WIDTH, HEIGHT)
    OPEN = None

    @classmethod
    def make_gateway(cls, robot_port):
        return CappedGatewayProbe(robot_port, debug=True)

    @staticmethod
    def expected_note(chip):
        """Both "(size limit)" notes, worded from the store as ``_CHIP`` read it."""
        bb_since = min(t for t in chip["bbStarts"] if t is not None)
        return (f"transitions: last {fmt_span(chip['head'] - chip['tMin'])} (size limit) · "
                f"blackboard history: last {fmt_span(chip['head'] - bb_since)} (size limit)")

    def test_caps_show_notes_and_head_bytes_are_post_eviction(self):
        b, gw = self.browser, self.gw
        with gw.watch() as ws:
            b.open(wait_nodes=13)
            b.page.wait_for_function(
                "(() => { const r = recordingStore.recording;"
                " return r && r.capped.includes('transitions') && r.capped.includes('blackboard'); })()",
                timeout=40000)
            # The pill shows what the last render painted; the store may have
            # moved on since (a head, an eviction). Read again for a few frames
            # until what is painted matches the store read in the same task.
            for _ in range(20):
                chip = b.page.evaluate(_CHIP)
                if chip["note"] == self.expected_note(chip):
                    break
                b.next_frame()
            b.screenshot(SHOTS, "chip_capped")
            heads = ws.of_type("head")
            self.assertTrue(heads)
            for frame in heads:
                self.assertIn("bytes", frame)
                self.assertIsInstance(frame["capped"], list)
                self.assertTrue(set(frame["capped"]) <= {"transitions", "blackboard"})
                # After eviction the blackboard is within its sub-cap unless only
                # bases are left, and the total within the cap whenever a sealed
                # chunk could have gone (64-record chunks: < 1 kB each).
                self.assertLessEqual(sum(frame["bytes"]), CappedGatewayProbe.MAX_BYTES,
                                     f"head frame reports pre-eviction size: {frame}")
            self.assertTrue(any("transitions" in f["capped"] for f in heads))
            self.assertTrue(any("blackboard" in f["capped"] for f in heads))

            span = fmt_span(chip["head"] - chip["tMin"])
            bb_since = min(t for t in chip["bbStarts"] if t is not None)
            self.assertFalse(chip["noteHidden"])
            self.assertEqual(chip["note"], self.expected_note(chip))
            self.assertTrue(chip["text"].startswith(f"Recording · last {span} · "), chip)
            # The browser's blackboard start equals the gateway's.
            state = gw.require_debug_state()
            gw_bb = min(s["t_start"] for s in state["blackboard"] if s["t_start"] is not None)
            self.assertLessEqual(abs(gw_bb - bb_since), 2_000_000)


if __name__ == "__main__":
    unittest.main()
