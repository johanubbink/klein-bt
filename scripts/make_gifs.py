"""Regenerate the README's demo GIFs. Opt-in and dev-only, not part of the test
suite; see docs/testing.md, "Demo GIFs".

    .venv/bin/python scripts/make_gifs.py               # both GIFs
    .venv/bin/python scripts/make_gifs.py --only rewind  # just one (hero, rewind)

* ``assets/klein-demo.gif`` (the hero): the mock's CrossDoor tree live, the
  blackboard sidebar, and the drawer on the Timeline tab following live. Two
  whole mission laps, so it loops cleanly.
* ``assets/klein-rewind.gif``: the same live mock, rewound while it runs:
  the playhead dragged back into PickLock's retries, then on the Log tab
  |◀ |◀ |◀ ▶| steps (each flips PickLock's card), a Log row clicked,
  ``door_open`` hovered in the blackboard (its writers and reader outlined
  on the tree), ▶ play, then Jump to live, back to ● Live.

Needs Playwright (``pip install -e '.[dev]'``) and ``ffmpeg``. Chrome draws
klein inside a plain, made-up browser outline (a wrapper page with a tab and
an address bar, coloured from klein's own ``:root`` palette, klein in an
iframe), and takes a screenshot after every step. Each screenshot keeps the
time it was taken, so ffmpeg can lay them out at their real pace, resample to
a steady frame rate and quantise with one palette for the whole GIF
(palettegen, then paletteuse, no dithering: no flicker). Headless screenshots
have no mouse cursor; where the mouse matters, the wrapper draws a pointer.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.harness.probes import _READ_NODES, BrowserProbe, GatewayProbe  # noqa: E402
from tests.harness.targets import MockTarget              # noqa: E402

ASSETS = REPO_ROOT / "assets"
STYLES = REPO_ROOT / "klein" / "static" / "styles.css"

GIF_WIDTH, GIF_HEIGHT = 900, 580
SCALE = 1.5                  # Chrome draws at 1.5x that, and ffmpeg scales it down
VIEWPORT = (round(GIF_WIDTH * SCALE), round(GIF_HEIGHT * SCALE))
FPS = 12
MOCK_PORT = 1777             # the README's mock port, so the sidebar reads :1777
PICKLOCK, ROOT = 11, 1       # uids in the mock's CrossDoor tree
FAILURE = 3
DRAWER = 240                 # px: the drawer's height in both GIFs

# The outline: a tab strip and a toolbar above an iframe, all in klein's palette.
WRAPPER = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
%(root)s
html, body { margin: 0; height: 100%%; overflow: hidden; background: var(--bg-color);
  font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: var(--text-main); }
body { display: flex; flex-direction: column; }
.tabs { height: 36px; display: flex; align-items: flex-end; padding: 0 10px;
  background: var(--bg-color); }
.tab { height: 30px; width: 230px; display: flex; align-items: center; gap: 8px;
  padding: 0 12px; box-sizing: border-box; border-radius: 8px 8px 0 0;
  background: var(--panel-bg); color: var(--text-main); }
.tab .icon { width: 10px; height: 10px; border-radius: 3px; background: var(--accent); }
.tab .close { margin-left: auto; color: var(--text-muted); }
.bar { height: 40px; display: flex; align-items: center; gap: 4px; padding: 0 10px;
  background: var(--panel-bg); border-bottom: 1px solid var(--border-color); }
.btn { width: 28px; text-align: center; color: var(--text-muted); font-size: 16px; }
.btn.off { opacity: .45; }
.url { flex: 1; height: 26px; margin-left: 8px; border-radius: 13px; display: flex;
  align-items: center; padding: 0 14px; background: var(--bg-color);
  border: 1px solid var(--border-color); color: var(--text-main); }
.url .scheme { color: var(--text-muted); }
iframe { flex: 1; border: 0; width: 100%%; display: block; }
#pointer { position: fixed; left: 0; top: 0; width: 18px; height: 26px; display: none;
  pointer-events: none; z-index: 9; filter: drop-shadow(0 1px 2px var(--bg-color)); }
</style></head><body>
<div class="tabs"><div class="tab"><span class="icon"></span>klein — BT viewer
  <span class="close">×</span></div></div>
<div class="bar"><span class="btn">←</span><span class="btn off">→</span>
  <span class="btn">↻</span>
  <div class="url"><span class="scheme">http://</span>localhost:8080</div></div>
<iframe id="app" src="%(url)s"></iframe>
<svg id="pointer" viewBox="0 0 18 26"><path d="M1 1 L1 20 L6 15.5 L9.5 23.5 L12.5 22
  L9 14.5 L15.5 14.5 Z" fill="var(--text-main)" stroke="var(--bg-color)"
  stroke-width="1.4" stroke-linejoin="round"/></svg>
</body></html>
"""

# The drawer's height, as klein remembers it, set before klein loads.
REMEMBER = """try {
  localStorage.setItem("klein.drawer", JSON.stringify({height: %d, collapsed: false}));
} catch (e) { /* no storage: the default height */ }""" % DRAWER


def palette():
    """The ``:root { … }`` block of klein's stylesheet, verbatim."""
    return re.search(r":root\s*\{.*?\n\}", STYLES.read_text(), re.S).group(0)


class Recorder:
    """The browser on the wrapper page, and the screenshots taken so far."""

    def __init__(self, url, workdir):
        self.dir = workdir
        self.shots = []                         # (time, path)
        wrapper = workdir / "wrapper.html"
        wrapper.write_text(WRAPPER % {"root": palette(), "url": url})
        self.probe = BrowserProbe(url, viewport=VIEWPORT).start()
        self.page = self.probe.page
        self.page.add_init_script(REMEMBER)
        self.page.goto(wrapper.as_uri())
        self.app = self.page.frame_locator("#app")
        self.frame = self.page.wait_for_selector("#app").content_frame()
        self.frame.wait_for_function("document.querySelectorAll('#canvas g.node').length >= 13")
        self.offset = self.page.locator("#app").bounding_box()
        self.mouse = (VIEWPORT[0] * 0.75, VIEWPORT[1] * 0.85)

    def close(self):
        self.probe.stop()

    def js(self, script, arg=None):
        return self.frame.evaluate(script, arg)

    def shot(self):
        path = self.dir / f"f{len(self.shots):05d}.png"
        self.page.screenshot(path=str(path))
        self.shots.append((time.monotonic(), path))

    def film(self, seconds, until=None):
        """Screenshots for ``seconds``, or until ``until()`` is true."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (until and until()):
            self.shot()

    def box(self, selector):
        """The centre of ``selector`` in klein, in page coordinates."""
        b = self.app.locator(selector).first.bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    def show_pointer(self):
        self.page.evaluate("document.getElementById('pointer').style.display = 'block'")
        self.move_to(*self.mouse, steps=1)

    def move_to(self, x, y, steps=8):
        """Move the mouse (and the drawn pointer) in ``steps``, a frame each."""
        x0, y0 = self.mouse
        for i in range(1, steps + 1):
            px, py = x0 + (x - x0) * i / steps, y0 + (y - y0) * i / steps
            self.page.mouse.move(px, py)
            self.page.evaluate(f"document.getElementById('pointer').style.transform"
                               f" = 'translate({px - 1}px, {py - 1}px)'")
            self.shot()
        self.mouse = (x, y)

    def click(self, selector=None, at=None, hold=0.6):
        self.move_to(*(at or self.box(selector)))
        self.page.mouse.down()
        self.page.mouse.up()
        self.film(hold)

    def show_doorclosed(self):
        """Scroll the Timeline to DoorClosed's section, just under MainTree's
        header, so PickLock's row shows; and drop the focus the clicks left."""
        self.js("""() => { const body = document.getElementById('drawer-body');
          const head = document.querySelector('#drawer-timeline .tl-head');
          const row = document.querySelector('#tl-rows .tl-header[data-uid="7"]');
          body.scrollTop = 0;                   // nothing stuck: the row where it lies
          body.scrollTop = row.getBoundingClientRect().top
                            - head.getBoundingClientRect().bottom - row.offsetHeight;
          document.activeElement.blur(); }""")

    def tl(self):
        """The Timeline's window and axis, with the axis in page coordinates."""
        tl = self.js("() => KleinTimeline.debug()")
        tl["left"] += self.offset["x"]
        return tl

    def x_of(self, t, tl):
        return tl["left"] + (t - tl["t0"]) * tl["width"] / tl["span"]

    def ruler_y(self):
        return self.box("#tl-ruler")[1]

    def status(self, uid):
        nodes = self.js(_READ_NODES)
        return next((n["label"] for n in nodes if n["uid"] == uid), None)

    def encode(self, out, fade=0.0):
        """The screenshots at their real pace -> a GIF of ``FPS`` frames per second."""
        listing = self.dir / "frames.txt"
        lines = []
        ends = [t for t, _path in self.shots[1:]] + [self.shots[-1][0] + 1 / FPS]
        for (t, path), t_next in zip(self.shots, ends):
            lines += [f"file '{path.name}'", f"duration {t_next - t:.4f}"]
        lines.append(f"file '{self.shots[-1][1].name}'")
        listing.write_text("\n".join(lines) + "\n")
        length = self.shots[-1][0] - self.shots[0][0] + 1 / FPS
        vf = f"fps={FPS},scale={GIF_WIDTH}:-1:flags=lanczos"
        if fade:                                # to and from klein's background
            bg = re.search(r"--bg-color:\s*(#[0-9a-fA-F]+)", palette()).group(1)
            vf += (f",fade=t=in:st=0:d={fade}:color={bg}"
                   f",fade=t=out:st={length - fade:.3f}:d={fade}:color={bg}")
        source = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        palette_png = self.dir / "palette.png"
        ffmpeg = ["ffmpeg", "-v", "error", "-y"]
        subprocess.run(ffmpeg + source + ["-vf", vf + ",palettegen=max_colors=256:stats_mode=full",
                                          str(palette_png)], check=True)
        subprocess.run(ffmpeg + source + ["-i", str(palette_png), "-lavfi",
                                          f"{vf}[v];[v][1:v]paletteuse=dither=none:diff_mode=rectangle",
                                          "-loop", "0", str(out)], check=True)
        print(f"{out.relative_to(REPO_ROOT)}: {len(self.shots)} screenshots over {length:.1f} s,"
              f" {out.stat().st_size / 1e6:.2f} MB")


def start_mock():
    """The mock on MOCK_PORT when it is free (so the sidebar reads the same every
    run), else on any free port."""
    robot = MockTarget()
    robot.port = MOCK_PORT                  # the harness retries on a free port if taken
    robot.start()
    if robot.port != MOCK_PORT:
        print(f"port {MOCK_PORT} is taken; the sidebar shows {robot.port} instead")
    return robot


def at_lap_start(rec, previous):
    """True on the first frame of a mission: the root just left IDLE."""
    now = rec.status(ROOT)
    started = now == "RUNNING" and previous[0] not in (None, "RUNNING")
    previous[0] = now
    return started


def make_hero(workdir):
    robot = start_mock()
    gw = GatewayProbe(robot.port).start()
    rec = Recorder(gw.url, workdir)
    try:
        rec.app.locator("#drawer-tab-timeline").click()
        rec.app.locator("#tl-zoom-in").click()          # 30 s -> 20 s: bigger bars
        rec.show_doorclosed()
        rec.page.mouse.move(60, VIEWPORT[1] - 30)       # the sidebar's empty foot: no hover
        # Fill the Timeline's window first, so it scrolls the same all through.
        time.sleep(21)
        seen = [None]
        while not at_lap_start(rec, seen):
            time.sleep(0.02)
        laps = [0]

        def two_laps():
            laps[0] += at_lap_start(rec, seen)
            return laps[0] == 2
        rec.film(30, until=two_laps)
        rec.shots.pop()                 # that frame is the next lap's first: the loop's start
        rec.encode(ASSETS / "klein-demo.gif")
    finally:
        rec.close()
        gw.stop()
        robot.stop()


def make_rewind(workdir):
    robot = start_mock()
    gw = GatewayProbe(robot.port, debug=True).start()
    rec = Recorder(gw.url, workdir)
    try:
        rec.app.locator("#drawer-tab-timeline").click()
        rec.app.locator("#tl-zoom-in").click()          # 30 s -> 20 s: bigger bars
        rec.show_doorclosed()
        rec.page.mouse.move(60, VIEWPORT[1] - 30)       # the sidebar's empty foot: no hover
        time.sleep(21)                                  # fill the Timeline's window first
        rec.show_pointer()
        rec.film(0.6)                                   # live: ● Live, the window following
        tl = rec.tl()
        y = rec.ruler_y()
        # Drag the playhead back 6 to 14 s, into one of PickLock's FAILUREs
        # within a run of PickLock transitions (its retries), so that every
        # step below flips that card between RUNNING and FAILURE.
        dump = gw.debug_state()
        records = dump["records"]
        head = records[-1][0]
        picks = [i for i in range(4, len(records) - 1)
                 if records[i][1:] == [PICKLOCK, FAILURE] and 6e6 < head - records[i][0] < 14e6
                 and all(records[j][1] == PICKLOCK for j in range(i - 4, i + 2))]
        target = records[picks[len(picks) // 2]][0]
        rec.move_to(rec.x_of(tl["t1"] - 300_000, tl), y)
        rec.page.mouse.down()
        rec.film(0.2)
        tl = rec.tl()                                   # paused: the window stops here
        rec.move_to(rec.x_of(target + 30_000, tl), y, steps=20)
        rec.page.mouse.up()
        rec.film(0.4)
        # The Log, then step: each press moves the selected row and flips
        # PickLock's card. Then click the row two above (on its time: a
        # subtree cell would filter).
        rec.click("#drawer-tab-log", hold=0.5)
        for button in ("#tr-prev", "#tr-prev", "#tr-prev", "#tr-next"):
            rec.click(button, hold=0.7)
        seg, seq = rec.js("""() => { const r = document.querySelector('#log-rows .log-row.selected');
          return [r.dataset.seg, Number(r.dataset.seq)]; }""")
        row = rec.app.locator(f'#log-rows .log-row[data-seg="{seg}"][data-seq="{seq - 2}"]'
                              ':not([hidden]) > :first-child').bounding_box()
        rec.click(at=(row["x"] + row["width"] / 2, row["y"] + row["height"] / 2), hold=0.6)
        # Hover door_open in the blackboard: its writers (the Script, and the
        # subtree remapping it) are outlined solid, its reader dashed.
        key = rec.app.locator("#bb-groups .bb-row", has_text="door_open").first
        b = key.bounding_box()
        rec.move_to(b["x"] + 50, b["y"] + b["height"] / 2, steps=10)
        rec.film(1.0)
        rec.click("#drawer-tab-timeline", hold=0)
        rec.show_doorclosed()                           # the tab scrolled to the playhead
        rec.film(0.2)
        rec.click("#tr-play", hold=1.4)
        # Back to live: the button turns into ● Live, the window follows again.
        rec.click("#tr-jump", hold=0)
        rec.move_to(rec.mouse[0] + 40, rec.mouse[1] + 60)
        rec.film(1.2)
        assert rec.js("() => document.getElementById('tr-jump').dataset.state") == "live"
        rec.encode(ASSETS / "klein-rewind.gif", fade=0.3)
    finally:
        rec.close()
        gw.stop()
        robot.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", choices=["hero", "rewind"], help="make just this GIF")
    ap.add_argument("--keep", action="store_true", help="keep the screenshots (prints where)")
    args = ap.parse_args()
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is not installed")
    for name, make in (("hero", make_hero), ("rewind", make_rewind)):
        if args.only in (None, name):
            workdir = Path(tempfile.mkdtemp(prefix=f"klein-gif-{name}-"))
            make(workdir)
            if args.keep:
                print(f"screenshots kept in {workdir}")
            else:
                shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
