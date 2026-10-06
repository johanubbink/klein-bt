"""The dashboard in a real browser. ``DashboardCase`` is the shared set-up."""
import unittest

from tests.harness.probes import BrowserProbe, GatewayProbe
from tests.harness.targets import MockTarget

# The drawer's overview as drawn, in one task: its own debug (left edge, span
# and width), the Timeline's, the thumb's and the knob's boxes (null when
# hidden), and every mark's [left, right] by kind.
OVERVIEW = """() => {
  const box = (id) => { const e = document.getElementById(id);
                        return e.hidden ? null : e.getBoundingClientRect().toJSON(); };
  const marks = (kind) => [...document.querySelectorAll('#ov-marks .ov-' + kind)].map(e => {
    const b = e.getBoundingClientRect(); return [b.left, b.right]; });
  return { ov: KleinOverview.debug(), tl: KleinTimeline.debug(), thumb: box('ov-thumb'),
           knob: box('ov-knob'), gaps: marks('gap'), runs: marks('run') };
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
    """The x of time ``t`` on the overview ``v`` (``OVERVIEW``'s result)."""
    ov = v["ov"]
    return ov["left"] + (t - ov["tMin"]) * ov["width"] / (ov["tMax"] - ov["tMin"])


def overview_t(v, x):
    """The time at ``x`` on the overview ``v``: ``overview_x`` inverted."""
    ov = v["ov"]
    return ov["tMin"] + (x - ov["left"]) * (ov["tMax"] - ov["tMin"]) / ov["width"]


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
        ``Model``) and its ``gaps`` (``[t_from, t_to, kind]``): a hatched band
        per gap and a line where each tree run after the first starts, to
        ±1 px, its marks drawn for the span shown. Returns the view and its
        time -> x."""
        for _ in range(20):         # live, the marks may be a frame behind the knob
            v = self.page.evaluate(OVERVIEW)
            ov = v["ov"]
            if ov["marks"] == {k: ov[k] for k in ("tMin", "tMax", "width")}:
                break
            self.frame()
        self.assertEqual(ov["marks"], {k: ov[k] for k in ("tMin", "tMax", "width")}, what)
        lo, hi = ov["tMin"], ov["tMax"]
        x = lambda t: overview_x(v, t)
        want = [(x(max(a, lo)), x(min(b, hi))) for a, b, _k in gaps if b >= lo and a <= hi]
        self.assertEqual(len(v["gaps"]), len(want), what)
        for got, w in zip(v["gaps"], want):
            self.assertAlmostEqual(got[0], w[0], delta=1, msg=what)
            self.assertAlmostEqual(got[1], max(w[1], w[0] + 1), delta=1, msg=what)
        starts = [x(t) for t in (model.segments[run[0]][0]["t_begin"] for run in model.runs()[1:])
                  if t <= hi]
        self.assertEqual(len(v["runs"]), len(starts), what)
        for (left, right), w in zip(v["runs"], starts):
            self.assertAlmostEqual((left + right) / 2, w, delta=1, msg=what)
        return v, x

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
