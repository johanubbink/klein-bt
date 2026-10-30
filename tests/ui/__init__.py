"""The dashboard in a real browser. ``DashboardCase`` is the shared set-up."""
import unittest

from tests.harness.probes import BrowserProbe, GatewayProbe
from tests.harness.targets import MockTarget

# The drawer's overview as drawn, in one task: its own debug (the strip's
# place and scale, the scrollbar's), the Timeline's, the box's, the cursor
# line's, the thumb's and the tick's boxes (null when hidden), every mark's
# [left, right] on the strip and on the scrollbar, and the scrollbar's opacity.
OVERVIEW = """() => {
  const box = (id) => { const e = document.getElementById(id);
                        return e.hidden ? null : e.getBoundingClientRect().toJSON(); };
  const marks = (sel) => [...document.querySelectorAll(sel)].map(e => {
    const b = e.getBoundingClientRect(); return [b.left, b.right]; });
  return { ov: KleinOverview.debug(), tl: KleinTimeline.debug(), box: box('ov-box'),
           cursor: box('ov-cursor'), thumb: box('ov-thumb'), tick: box('ov-tick'),
           gaps: marks('#ov-strip-marks .ov-gap'), runs: marks('#ov-strip-marks .ov-run'),
           sbGaps: marks('#ov-marks .ov-gap'), sbRuns: marks('#ov-marks .ov-run'),
           opacity: Number(getComputedStyle(document.getElementById('ov-scrollbar')).opacity) };
}"""

# The Timeline's start edge as drawn, in one task, or null while a render is
# queued or the bars are drawn for another window: its debug, the newest
# moment and the oldest, the edge's x (null when not drawn) and title, the
# note's text (null when hidden) and title, and the playhead's middle.
TIMELINE_START = """() => {
  const tl = KleinTimeline.debug(), rec = shownRecording();
  if (renderQueued || tl.barsT1 !== tl.t1) return null;
  const edge = document.querySelector('#tl-bands .tl-dropped');
  const note = document.getElementById('tl-dropped-note');
  const p = document.getElementById('tl-playhead').getBoundingClientRect();
  const body = document.getElementById('drawer-body');
  return { tl, head: KleinCursor.headTime(rec), tMin: rec.tMin,
           edge: edge && edge.getBoundingClientRect().left, title: edge && edge.title,
           note: note.hidden ? null : note.textContent, noteTitle: note.title,
           playhead: (p.left + p.right) / 2, overflow: body.scrollWidth - body.clientWidth };
}"""

# The hover links as drawn, in one task: per card its uid, outline role
# (write/read/null), the outline's dash, its port line's text and accented
# tokens; and the rows a card hover marked.
LINKS = """() => ({
  cards: [...document.querySelectorAll('#canvas g.node')].map(g => {
    const o = g.querySelector('.node-link');
    return [g.__data__.data.uid,
            o.classList.contains('write') ? 'write' : o.classList.contains('read') ? 'read' : null,
            getComputedStyle(o).strokeDasharray, g.querySelector('.node-ports').textContent,
            [...g.querySelectorAll('.node-ports tspan.hl')].map(t => t.textContent)]; }),
  linked: Object.entries(bbGroupEls).flatMap(([b, g]) => Object.entries(g.rows)
    .filter(([k, r]) => r.row.classList.contains('linked')).map(([k]) => [b, k])),
})"""

# The cards' writer pulses: [uids with the static mark, uids fading].
PULSES = """() => ['mark', 'fire'].map(c => [...document.querySelectorAll('#canvas g.node')]
  .filter(g => g.querySelector('.node-pulse').classList.contains(c))
  .map(g => g.__data__.data.uid))"""

# A mouse event on the card of a uid, through d3's handler.
CARD_EVENT = """([uid, type]) => [...document.querySelectorAll('#canvas g.node')]
  .find(g => g.__data__.data.uid === uid).dispatchEvent(new MouseEvent(type, {bubbles: true}))"""

# Every key row, then every card, entered and left in one task: per row its
# ref, LINKS' cards while hovered and the uids still outlined or accented
# after; per card its uid, the rows LINKS marks while hovered and after.
_HOVER_SWEEP = """() => {
  const links = """ + LINKS + """, card = """ + CARD_EVENT + """;
  const rows = Object.entries(bbGroupEls).flatMap(([b, g]) => Object.entries(g.rows).map(([k, r]) => {
    r.row.dispatchEvent(new MouseEvent('mouseenter'));
    const on = links().cards;
    r.row.dispatchEvent(new MouseEvent('mouseleave'));
    return [b, k, on, links().cards.filter(c => c[1] || c[4].length).map(c => c[0])]; }));
  const cards = [...document.querySelectorAll('#canvas g.node')].map(g => {
    const uid = g.__data__.data.uid;
    card([uid, 'mouseenter']);
    const on = links().linked;
    card([uid, 'mouseleave']);
    return [uid, on, links().linked]; });
  return { rows, cards };
}"""

MAX_PORT_CHARS = 36            # app.js: (220 px card - 2 * 12 px) / (9 px * 0.6) monospace


def port_line(ports):
    """A card's port line: every ``port=value``, truncated as app.js does."""
    text = "  ".join(f"{p}={v}" for p, v in (ports or {}).items())
    return text if len(text) <= MAX_PORT_CHARS else text[:MAX_PORT_CHARS - 1] + "…"


def overview_x(v, t):
    """The x of time ``t`` on the overview's strip in ``v`` (``OVERVIEW``'s result)."""
    ov = v["ov"]
    return ov["left"] + (t - ov["m0"]) * ov["width"] / ov["S"]


def overview_t(v, x):
    """The time at ``x`` on the overview's strip: ``overview_x`` inverted."""
    ov = v["ov"]
    return ov["m0"] + (x - ov["left"]) * ov["S"] / ov["width"]


def scrollbar_x(v, t):
    """The x of time ``t`` on the overview's scrollbar in ``v``."""
    sb = v["ov"]["sb"]
    return sb["left"] + (t - sb["wa"]) * sb["width"] / (sb["wb"] - sb["wa"])


def scrollbar_t(v, x):
    """The time at ``x`` on the overview's scrollbar: ``scrollbar_x`` inverted."""
    sb = v["ov"]["sb"]
    return sb["wa"] + (x - sb["left"]) * (sb["wb"] - sb["wa"]) / sb["width"]


def links(layout, folded=()):
    """What a layout's ``bindings`` link, with the cards of ``folded`` uids
    folded. ``roles``: ``{(board, key): {uid: "write"|"read"}}`` by the card
    that stands for each node (one hidden by folds: its outermost folded
    ancestor), a writer (out, inout) winning over a reader (in). ``keys``:
    ``{uid: {(board, key)}}`` per card, a folded card's including those it
    hides. ``tokens``: ``{(board, key): {uid}}``, the cards whose port line
    shows a bound port whole. ``lines``: ``{uid: port line}`` for every node."""
    roles, keys, tokens, lines = {}, {}, {}, {}

    def walk(node, card):
        uid = node["uid"]
        shown = uid if card is None else card
        line = lines[uid] = port_line(node.get("ports"))
        mine = {}
        for b in node.get("bindings", []):
            ref = (b["board"], b["key"])
            mine[ref] = "write" if b["dir"] != "in" or mine.get(ref) == "write" else "read"
            if f"{b['port']}={node['ports'][b['port']]}" in line:
                tokens.setdefault(ref, set()).add(uid)
        keys.setdefault(shown, set()).update(mine)
        for ref, role in mine.items():
            if roles.setdefault(ref, {}).get(shown) != "write":
                roles[ref][shown] = role
        inner = card if card is not None else (uid if uid in folded else None)
        for child in node["children"]:
            walk(child, inner)

    walk(layout, None)
    return roles, keys, tokens, lines


class DashboardCase(unittest.TestCase):
    """A robot, a gateway and a browser for the class, set by class options:

    * ``ROBOT``: ``ROBOT_CLASS(**ROBOT)`` (a ``MockTarget`` by default), or
      ``None`` for no robot;
    * ``GATEWAY``: ``GatewayProbe`` options, against the robot (or a port
      nothing answers on), or ``None`` for no gateway; ``make_gateway`` builds
      it, for a class that needs another one;
    * ``VIEWPORT``, and ``OPEN``: how many cards to wait for on opening the
      dashboard fresh (``BrowserProbe.open(fresh=True)``), or ``None`` to
      leave the page blank.

    Each one stops with the class, even when ``setUpClass`` fails half way.
    Every test fails on an uncaught page error.
    """

    ROBOT = {}
    ROBOT_CLASS = MockTarget
    GATEWAY = {}
    VIEWPORT = (1400, 900)
    OPEN = 13

    @classmethod
    def setUpClass(cls):
        cls.robot = cls.gw = None
        if cls.ROBOT is not None:
            cls.robot = cls.ROBOT_CLASS(**cls.ROBOT)
            cls.addClassCleanup(cls.robot.stop)
            cls.robot.start()
        if cls.GATEWAY is not None:
            cls.gw = cls.make_gateway(cls.robot.port if cls.robot else 1)
            cls.addClassCleanup(cls.gw.stop)
            cls.gw.start()
        cls.browser = BrowserProbe(cls.gw.url if cls.gw else "", viewport=cls.VIEWPORT)
        cls.addClassCleanup(cls.browser.stop)
        cls.browser.start()
        cls.page = cls.browser.page
        if cls.OPEN is not None:
            cls.browser.open(wait_nodes=cls.OPEN, fresh=True)

    @classmethod
    def make_gateway(cls, robot_port):
        """The class's gateway, not yet started."""
        return GatewayProbe(robot_port, **cls.GATEWAY)

    def tearDown(self):
        errors = self.browser.page_errors()
        self.assertFalse(errors, errors)

    def assert_overview(self, model, gaps=(), what=""):
        """The overview as drawn (``OVERVIEW``) against ``model`` (a harness
        ``Model``) and its ``gaps`` (``[t_from, t_to, kind]``), on the strip
        and on the scrollbar: a hatched band per gap and a line where each
        tree run after the first starts, to ±1 px, each set of marks drawn
        for the span shown. Returns the view and the strip's time -> x."""
        v = self.page.evaluate(OVERVIEW)
        ov = v["ov"]
        starts = [model.segments[run[0]][0]["t_begin"] for run in model.runs()[1:]]
        for where, drawn, (lo, hi), x, least, gaps_got, runs_got in (
                ("strip", ov["marks"], (ov["m0"], ov["m0"] + ov["S"]), lambda t: overview_x(v, t),
                 1, v["gaps"], v["runs"]),
                ("scrollbar", ov["sb"]["marks"], (ov["sb"]["wa"], ov["sb"]["wb"]),
                 lambda t: scrollbar_x(v, t), 2, v["sbGaps"], v["sbRuns"])):
            msg = f"{what}: the {where}"
            self.assertEqual(drawn, {"t0": lo, "t1": hi, "width": ov["width"]}, msg)
            want = [(x(max(a, lo)), x(min(b, hi))) for a, b, _k in gaps if b >= lo and a <= hi]
            self.assertEqual(len(gaps_got), len(want), msg)
            for got, w in zip(gaps_got, want):
                self.assertAlmostEqual(got[0], w[0], delta=1, msg=msg)
                self.assertAlmostEqual(got[1], max(w[1], w[0] + least), delta=1, msg=msg)
            want = [x(t) for t in starts if lo <= t <= hi]
            self.assertEqual(len(runs_got), len(want), msg)
            for (left, right), w in zip(runs_got, want):
                self.assertAlmostEqual((left + right) / 2, w, delta=1, msg=msg)
        return v, lambda t: overview_x(v, t)

    def assert_box(self, v, what=""):
        """The overview in ``v`` (``OVERVIEW``) shows the Timeline's window:
        the box spans it on the strip, an eighth of the strip wide, and while
        the recording is longer than the window the scrollbar's thumb spans
        it too (at least 18 px, centred on it, kept on the track)."""
        ov, tl, box, sb = v["ov"], v["tl"], v["box"], v["ov"]["sb"]
        self.assertIsNotNone(box, what)
        self.assertAlmostEqual(box["width"], ov["width"] / 8, delta=1, msg=what)
        self.assertAlmostEqual(box["left"], overview_x(v, tl["t0"]), delta=1, msg=what)
        self.assertAlmostEqual(box["right"], overview_x(v, tl["t1"]), delta=1, msg=what)
        if ov["tMax"] - ov["tMin"] <= tl["span"]:
            self.assertIsNone(v["thumb"], f"{what}: nothing to scroll")
            return
        left, right = scrollbar_x(v, tl["t0"]), scrollbar_x(v, tl["t1"])
        w = max(18, right - left)
        left = min(max((left + right - w) / 2, sb["left"]), sb["left"] + sb["width"] - w)
        self.assertAlmostEqual(v["thumb"]["left"], left, delta=1, msg=what)
        self.assertAlmostEqual(v["thumb"]["width"], w, delta=1, msg=what)

    def assert_timeline_starts(self, note, what=""):
        """The Timeline, its window longer than the recording, as drawn
        (``TIMELINE_START``): the window ends at the newest moment and starts
        before the oldest, the start edge sits at the oldest (±1 px) with
        ``note`` as its title and its note's (``⇤ note``), live the playhead
        is the axis's right end (±1 px), and nothing (an axis label, the
        edge) widens the drawer. Returns the view."""
        v = self.page.wait_for_function(TIMELINE_START, polling="raf", timeout=10000).json_value()
        tl = v["tl"]
        self.assertEqual(v["overflow"], 0, f"the drawer scrolls sideways: {what}")
        self.assertEqual(tl["t1"], v["head"], what)
        self.assertLess(tl["t0"], v["tMin"], what)
        self.assertIsNotNone(v["edge"], what)
        self.assertAlmostEqual(v["edge"], tl["left"] + (v["tMin"] - tl["t0"]) * tl["width"] / tl["span"],
                               delta=1, msg=what)
        self.assertEqual((v["note"], v["noteTitle"], v["title"]), (f"⇤ {note}", note, note), what)
        if tl["pos"]["live"]:
            self.assertAlmostEqual(v["playhead"], tl["left"] + tl["width"], delta=1, msg=what)
        return v

    def fold(self, uid):
        """Fold or unfold the card of ``uid`` (its click), the cards settled."""
        self.page.evaluate(CARD_EVENT, [uid, "click"])
        self.browser.wait_for_nodes(1)

    def assert_hover_links(self, layout, folded=()):
        """Hovering every key row outlines exactly the cards ``links`` names
        (writers solid, readers dashed) and accents the key on the port
        lines that show it, every port line's text unchanged; leaving clears
        it all. Hovering every card marks exactly its keys' rows. ``folded``:
        those uids' cards are folded first, in order (and unfolded after,
        in reverse). One row is hovered by the mouse, the rest by events."""
        for uid in folded:
            self.fold(uid)
        roles, keys, tokens, lines = links(layout, folded)
        self.page.evaluate("() => { for (const g of Object.values(bbGroupEls))"
                           " if (g.body.hidden && !g.toggle.disabled) g.toggle.click(); }")

        def check_row(ref, cards, left):
            with self.subTest(key=ref, folded=folded):
                drawn = {uid: role for uid, role, *_ in cards if role}
                self.assertEqual(drawn, roles.get(ref, {}))
                for uid, role, dash, text, _hl in cards:
                    self.assertEqual(text, lines[uid], uid)
                    if role:
                        self.assertEqual(dash == "none", role == "write", (uid, dash))
                self.assertEqual({uid for uid, *_x, hl in cards if hl},
                                 tokens.get(ref, set()) & set(drawn))
                self.assertEqual(left, [], "unhovered")

        sweep = self.page.evaluate(_HOVER_SWEEP)
        rows = [(b, k) for b, k, *_ in sweep["rows"]]
        self.assertTrue(rows)
        self.page.evaluate_handle("([b, k]) => bbGroupEls[b].rows[k].row",
                                  list(rows[0])).as_element().hover()
        cards = self.page.evaluate(LINKS)["cards"]
        self.page.mouse.move(5, 5)                  # the sidebar's title: no row
        check_row(rows[0], cards, [c[0] for c in self.page.evaluate(LINKS)["cards"]
                                   if c[1] or c[4]])
        for b, k, cards, left in sweep["rows"]:
            check_row((b, k), cards, left)
        for uid, on, off in sweep["cards"]:
            with self.subTest(card=uid, folded=folded):
                self.assertEqual({tuple(r) for r in on}, keys.get(uid, set()) & set(rows))
                self.assertEqual(off, [])
        for uid in reversed(folded):
            self.fold(uid)

    @classmethod
    def frame(cls):
        """Wait until the page has painted its next frame."""
        cls.browser.next_frame()
