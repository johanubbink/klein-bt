"""Regenerate the README's demo GIFs. Opt-in and dev-only, not part of the test
suite; see docs/testing.md, "Demo GIFs".

    .venv/bin/python scripts/make_gifs.py               # both GIFs
    .venv/bin/python scripts/make_gifs.py --only replay  # just one (hero, replay)

* ``assets/klein-demo.gif`` (the hero): the mock's CrossDoor tree live, the
  blackboard sidebar, and the drawer on the Timeline tab following live. Two
  whole mission laps, so it loops cleanly.
* ``assets/klein-replay.gif``: a recording of the mock, saved with
  ``GET /log.zip`` and opened with ``klein-bt --open`` (no robot): the
  playhead dragged back through PickLock's failed attempts, |◀ ▶| steps, a
  Log row clicked, then ▶ play.

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
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.harness import btlog_ref                                  # noqa: E402
from tests.harness.probes import _READ_NODES, BrowserProbe, GatewayProbe  # noqa: E402
from tests.harness.targets import MockTarget              # noqa: E402

ASSETS = REPO_ROOT / "assets"
STYLES = REPO_ROOT / "klein" / "static" / "styles.css"

GIF_WIDTH, GIF_HEIGHT = 900, 580
SCALE = 1.5                  # Chrome draws at 1.5x that, and ffmpeg scales it down
VIEWPORT = (round(GIF_WIDTH * SCALE), round(GIF_HEIGHT * SCALE))
FPS = 12
MOCK_PORT = 1777             # the README's mock port, so the sidebar reads :1777
RECORDED = 100               # transitions: two CrossDoor laps and the start of a third
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


def make_recording(workdir):
    """Two laps of the mock and the start of a third, saved as klein's Save
    does, as ``crossdoor.btlog`` and ``crossdoor.bb.jsonl`` (a fixed name, so
    the chip reads the same). The mock stops after a fixed number of
    transitions, so every run records the same ones."""
    robot = start_mock()
    gw = GatewayProbe(robot.port).start()      # it records with no dashboard open
    while len(robot.ground_truth()) < RECORDED:
        time.sleep(0.02)
    time.sleep(0.03)                           # the gateway drains right after each poll
    robot.stop()
    status, _headers, body = gw.fetch("/log.zip")
    gw.stop()
    assert status == 200, status
    zip_path = workdir / "saved.zip"
    zip_path.write_bytes(body)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        btlog = next(n for n in names if n.endswith(".btlog"))
        (workdir / "crossdoor.btlog").write_bytes(z.read(btlog))
        (workdir / "crossdoor.bb.jsonl").write_bytes(z.read(btlog[:-len(".btlog")] + ".bb.jsonl"))
    return workdir / "crossdoor.btlog"


def make_replay(workdir):
    path = make_recording(workdir)
    records = btlog_ref.absolute(btlog_ref.read(path))
    failures = [t for t, uid, status in records if uid == PICKLOCK and status == FAILURE]
    gw = GatewayProbe(1, extra_args=["--open", str(path)]).start()
    rec = Recorder(gw.url, workdir)
    try:
        rec.app.locator("#drawer-tab-timeline").click()
        rec.app.locator("#tl-zoom-in").click()          # 30 s -> 20 s: the file fills it
        rec.show_doorclosed()
        rec.page.wait_for_timeout(300)
        rec.show_pointer()
        rec.film(1.2)                                   # the chip: "file: … · no robot"
        tl = rec.tl()
        y = rec.ruler_y()
        # Drag the playhead from the end back to lap 2's third failed PickLock.
        rec.move_to(rec.x_of(tl["t1"] - 300_000, tl), y)
        rec.page.mouse.down()
        rec.film(0.2)
        rec.move_to(rec.x_of(failures[-2] + 30_000, tl), y, steps=36)
        rec.page.mouse.up()
        rec.film(0.8)
        for button in ("#tl-prev", "#tl-prev", "#tl-next"):
            rec.click(button, hold=0.5)
        # The Log: click the row two below the one shown (on its time: a
        # subtree cell would filter), then back to the Timeline to play.
        rec.click("#drawer-tab-log", hold=0.6)
        cells = rec.app.locator("#log-rows .log-row:not([hidden]) > :first-child")
        boxes = sorted((cells.nth(i).bounding_box() for i in range(cells.count())),
                       key=lambda b: b["y"])
        shown = rec.app.locator("#log-rows .log-row.selected").bounding_box()
        row = next(b for b in boxes if b["y"] > shown["y"] + 1.5 * shown["height"])
        rec.click(at=(row["x"] + row["width"] / 2, row["y"] + row["height"] / 2), hold=1.0)
        rec.click("#drawer-tab-timeline", hold=0)
        rec.show_doorclosed()                           # the tab scrolled to the playhead
        rec.film(0.3)
        rec.click("#tl-play", hold=0)
        rec.move_to(rec.mouse[0] + 40, rec.mouse[1] + 60)
        rec.film(3.0)
        rec.encode(ASSETS / "klein-replay.gif", fade=0.3)
    finally:
        rec.close()
        gw.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", choices=["hero", "replay"], help="make just this GIF")
    ap.add_argument("--keep", action="store_true", help="keep the screenshots (prints where)")
    args = ap.parse_args()
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is not installed")
    for name, make in (("hero", make_hero), ("replay", make_replay)):
        if args.only in (None, name):
            workdir = Path(tempfile.mkdtemp(prefix=f"klein-gif-{name}-"))
            make(workdir)
            if args.keep:
                print(f"screenshots kept in {workdir}")
            else:
                shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
