"""Measure klein at the worst-case transition rate. Opt-in, slow, not part
of the test suite; see docs/testing.md, "Worst-case measurement".

    .venv/bin/python scripts/measure_worst_case.py            # all parts
    .venv/bin/python scripts/measure_worst_case.py --parts file,zip --minutes 10

A synthetic recording repeats the CrossDoor transitions of
tests/fixtures/groot2_mock.btlog (Groot2's own recording of the mock), retimed
to ``--rate`` transitions per second (default 10k/s, the observable ceiling:
1000 per drain x 10 drains/s).

* ``file``: ``--minutes`` of it (default 10, the default --record-buffer) as a
  .btlog opened with ``klein-bt --open``, which streams it exactly as a live
  recording's backfill. In Chrome: the backfill time on (re)load, the JS heap
  after it, and scrub frame times with the Timeline at its default 30 s
  window and zoomed out to the whole recording, and the Log at the end of
  its millions of rows. Also, in-process, what blocks the gateway's event
  loop: building one dashboard's backfill frames.
* ``zip``: ``GET /log.zip`` of that recording: its build time, and how long
  the event loop is blocked meanwhile (``GET /`` probed every 20 ms).
* ``live``: the mock replaying ``--live-seconds`` of it at several rates
  (``--live-rates``), the dashboard following live: main-thread busy share
  with the Timeline tab (its 30 s window full), the Log tab and the drawer
  collapsed (CDP Performance.getMetrics TaskDuration over 10 s).

It prints what it measured; reading it is the point (does anything crash, or
get too slow to use?).
"""
import argparse
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from klein.btlog import read_btlog, write_btlog  # noqa: E402
from klein.gateway import KleinGateway  # noqa: E402
from klein.streaming import backfill_frames  # noqa: E402
from tests.harness.probes import BrowserProbe, GatewayProbe  # noqa: E402
from tests.harness.targets import ReplayTarget  # noqa: E402

PATTERN = REPO_ROOT / "tests" / "fixtures" / "groot2_mock.btlog"


def synthetic_btlog(path, rate, seconds):
    """CrossDoor's recorded transitions, looped, ``rate`` per second."""
    log = read_btlog(PATTERN.read_bytes())
    pattern = [(uid, status) for _t, uid, status in log.records]
    n = int(rate * seconds)
    records = [(i * 1_000_000 // rate, *pattern[i % len(pattern)]) for i in range(n)]
    path.write_bytes(write_btlog(log.xml, log.first_timestamp, records))
    return n


def stats(values):
    values = sorted(values)
    if not values:
        return "n/a"
    p = lambda q: values[min(len(values) - 1, int(q * len(values)))]
    return (f"p50 {statistics.median(values):.1f} · p95 {p(0.95):.1f} · "
            f"max {values[-1]:.1f} ms (n={len(values)})")


def heap_mb(page):
    """After a GC: the JS heap in use plus the ArrayBuffers' backing stores
    (the recording's typed arrays live there, outside the V8 heap), in MB."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("HeapProfiler.collectGarbage")
    usage = cdp.send("Runtime.getHeapUsage")
    cdp.detach()
    return usage["usedSize"] / 1e6, usage.get("backingStorageSize", 0) / 1e6


# One scrub step as the Timeline's ruler does it (seekTime), then the
# render it asks for, run synchronously and laid out: the frame's JS cost.
_SCRUB = """async ([steps]) => {
  const rec = shownRecording();
  const dbg = KleinTimeline.debug();
  const out = [];
  for (let i = 0; i < steps; i++) {
    const t = Math.round(dbg.t0 + (dbg.t1 - dbg.t0) * (i + 0.5) / steps);
    let seg = rec.segments[0];
    for (const s of rec.segments) if (s.tBegin <= t) seg = s;
    const a = performance.now();
    seek(KleinCursor.pause(seg.id, KleinRecording.seqAtTime(seg, t), t));
    render();
    document.body.offsetHeight;
    out.push(performance.now() - a);
    await new Promise(r => requestAnimationFrame(r));
  }
  return out;
}"""

# Frame gaps (rAF to rAF) while a real mouse drags along the ruler.
# The overview's marks rebuilt once (its track narrowed by 1 px, so they are
# due), with layout: [ms, marks drawn].
_OVERVIEW_MARKS = """() => { const track = document.getElementById('ov-track');
  track.style.right = '7px';
  const rec = shownRecording(), t = performance.now();
  KleinOverview.update(rec, KleinCursor.cursorPos(clock, performance.now(), rec));
  void document.getElementById('ov-marks').offsetHeight;
  const ms = performance.now() - t;
  track.style.right = '';
  return [ms, document.querySelectorAll('#ov-marks i').length]; }"""

_RAF_START = """() => { window.__raf = []; window.__rafOn = true;
  const loop = (t) => { __raf.push(t); if (__rafOn) requestAnimationFrame(loop); };
  requestAnimationFrame(loop); }"""
_RAF_STOP = """() => { __rafOn = false; const r = __raf;
  return r.slice(1).map((t, i) => t - r[i]); }"""


def drag_frames(page, steps=60, track="#tl-ruler-track"):
    box = page.locator(track).bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 5, y)
    page.evaluate(_RAF_START)
    page.mouse.down()
    for i in range(steps):
        page.mouse.move(box["x"] + 5 + (box["width"] - 10) * i / steps, y)
        page.wait_for_timeout(16)
    page.mouse.up()
    return page.evaluate(_RAF_STOP)


def part_file(args, path, n):
    print(f"\n== file: {n:,} transitions ({args.rate:,}/s for {args.minutes:g} min), "
          f"{path.stat().st_size / 1e6:.0f} MB .btlog")
    # In-process: what one connecting dashboard costs the gateway's event loop.
    gw = KleinGateway("127.0.0.1", 1, 0)
    gw.ctx.term()                           # a file needs no robot socket
    t = time.perf_counter()
    gw.open_file(path)
    print(f"gateway: open_file {time.perf_counter() - t:.1f} s (once, at --open)")
    t = time.perf_counter()
    frames = backfill_frames("file", path.name, gw.recording)
    print(f"gateway: backfill frames for one dashboard {time.perf_counter() - t:.2f} s "
          f"(blocks the event loop), {sum(len(f) for f in frames) / 1e6:.0f} MB")
    del gw, frames

    probe = GatewayProbe(1, extra_args=["--open", str(path)]).start(timeout=300)
    try:
        with BrowserProbe(probe.url, viewport=(1400, 900)) as b:
            page = b.page
            page.goto(probe.url)
            page.evaluate("() => localStorage.clear()")     # the default panels and tab
            for attempt in ("load", "reload"):
                t = time.perf_counter()
                page.goto(probe.url)
                page.wait_for_function("window.kleinDebug && kleinDebug().backfillDone",
                                       timeout=600_000, polling=50)
                backfill = time.perf_counter() - t
                page.click("#drawer-tab-log")           # the drawer opens on the Timeline
                page.wait_for_function("document.querySelectorAll('#log-rows .log-row:not([hidden])').length > 0",
                                       timeout=600_000)
                shown = time.perf_counter() - t
                used, buffers = heap_mb(page)
                print(f"browser {attempt}: backfill done {backfill:.1f} s, Log shown {shown:.1f} s; "
                      f"JS heap {used:.0f} MB + ArrayBuffers {buffers:.0f} MB = "
                      f"{used + buffers:.0f} MB")
            # The Log at the end of all its rows: scroll by wheel.
            page.evaluate("() => { const b = document.getElementById('drawer-body'); b.scrollTop = b.scrollHeight / 2; }")
            box = page.locator("#drawer-body").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.evaluate(_RAF_START)
            for _ in range(30):
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(16)
            print(f"Log, {n:,} rows, wheel scrolling: frame gaps {stats(page.evaluate(_RAF_STOP))}")
            # The Timeline: scrub at the default window, then at the whole recording.
            page.click("#drawer-tab-timeline")
            page.evaluate("() => seek(KleinCursor.pause(shownRecording().segments[0].id, 1))")
            page.evaluate("() => new Promise(r => requestAnimationFrame(r))")
            for label in ("30 s window", "whole recording"):
                if label == "whole recording":
                    while page.evaluate("KleinTimeline.debug().span") < (args.minutes * 60e6):
                        before = page.evaluate("KleinTimeline.debug().span")
                        page.click("#tl-zoom-out")
                        page.wait_for_timeout(50)
                        if page.evaluate("KleinTimeline.debug().span") == before:
                            break
                span = page.evaluate("KleinTimeline.debug().span") / 1e6
                steps = 40 if label == "30 s window" else 8
                js = page.evaluate(_SCRUB, [steps])
                print(f"Timeline scrub, {label} ({span:g} s): per step JS+layout {stats(js)}")
                gaps = drag_frames(page, steps=30 if label == "30 s window" else 6)
                print(f"Timeline scrub, {label}: ruler drag frame gaps {stats(gaps)}")
            # The overview: its marks (gaps and tree-run starts),
            # and the knob dragged across the whole recording (on the Log tab:
            # on the Timeline's, the thumb under the pointer would pan instead).
            page.click("#drawer-tab-log")
            page.wait_for_timeout(500)
            ms, marks = page.evaluate(_OVERVIEW_MARKS)
            print(f"Overview: marks rebuilt in {ms:.1f} ms ({marks} marks)")
            gaps = drag_frames(page, steps=30, track="#ov-track")
            print(f"Overview drag across the whole recording: frame gaps {stats(gaps)}")
            page.click("#drawer-tab-timeline")
            # Panning moves the window: every bar in it is computed again.
            page.click("#tl-zoom-in")
            span = page.evaluate("KleinTimeline.debug().span") / 1e6
            box = page.locator("#tl-lanes").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 40)
            page.evaluate(_RAF_START)
            for _ in range(6):
                page.keyboard.down("Shift")
                page.mouse.wheel(0, 200)
                page.keyboard.up("Shift")
                page.wait_for_timeout(16)
            print(f"Timeline pan, {span:g} s window: frame gaps {stats(page.evaluate(_RAF_STOP))}")
            used, buffers = heap_mb(page)
            print(f"browser after scrubbing: JS heap {used:.0f} MB + ArrayBuffers {buffers:.0f} MB")
    finally:
        probe.stop()
    return probe


def part_zip(args, path):
    print("\n== zip")
    probe = GatewayProbe(1, extra_args=["--open", str(path)]).start(timeout=300)
    try:
        lag = []
        stop = threading.Event()

        def ping():
            while not stop.is_set():
                t = time.perf_counter()
                urllib.request.urlopen(probe.url + "/", timeout=120).read()
                lag.append(time.perf_counter() - t)
                time.sleep(0.02)

        pinger = threading.Thread(target=ping)
        pinger.start()
        time.sleep(0.3)
        t = time.perf_counter()
        body = urllib.request.urlopen(probe.url + "/log.zip", timeout=600).read()
        took = time.perf_counter() - t
        time.sleep(0.3)
        stop.set()
        pinger.join()
        size = path.stat().st_size
        print(f"GET /log.zip: {took:.1f} s for a {size / 2**20:.0f} MiB .btlog "
              f"({size / 2**20 / took:.1f} MiB/s), zip {len(body) / 2**20:.0f} MiB; "
              f"GET / meanwhile: max {max(lag) * 1000:.0f} ms (the event loop's longest block)")
    finally:
        probe.stop()


def busy_share(page, seconds=10):
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")
    get = lambda: {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
    a = get()
    time.sleep(seconds)
    b = get()
    cdp.detach()
    wall = b["Timestamp"] - a["Timestamp"]
    share = lambda name: (b[name] - a[name]) / wall
    return share("TaskDuration"), {"script": share("ScriptDuration"),
                                   "style+layout": share("RecalcStyleDuration")
                                   + share("LayoutDuration")}


def part_live(args, workdir):
    print("\n== live (mock --replay, the dashboard following live)")
    for rate in args.live_rates:
        path = workdir / f"live_{rate}.btlog"
        synthetic_btlog(path, rate, args.live_seconds)
        with ReplayTarget(path) as robot, \
             GatewayProbe(robot.port, debug=True) as gw, \
             BrowserProbe(gw.url, viewport=(1400, 900)) as b:
            page = b.page
            b.open(wait_nodes=13, fresh=True)
            # The Timeline's default 30 s window full of records first.
            page.click("#drawer-tab-timeline")
            time.sleep(args.warmup)
            shares = {}
            for view in ("timeline", "log", "collapsed"):
                if view == "collapsed":
                    page.click("#drawer-collapse")
                else:
                    page.click(f"#drawer-tab-{view}")
                time.sleep(1)
                shares[view], parts = busy_share(page)
                if view == "timeline":
                    timeline_parts = parts
            page.click("#drawer-collapse")
            gaps = gw.require_debug_state()["gaps"]
            seen = page.evaluate("kleinDebug().segments.reduce((n, s) => n + s.headSeq - s.startSeq, 0)")
            split = ", ".join(f"{k} {v:.0%}" for k, v in timeline_parts.items())
            print(f"{rate:>6,}/s: main thread busy — Timeline {shares['timeline']:.0%} ({split}), "
                  f"Log {shares['log']:.0%}, drawer collapsed {shares['collapsed']:.0%}; "
                  f"{seen:,} transitions in the browser, {len(gaps)} overflow/outage gaps")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rate", type=int, default=10_000, help="transitions/s (default 10000)")
    ap.add_argument("--minutes", type=float, default=10.0, help="recording length (default 10)")
    ap.add_argument("--parts", default="file,zip,live", help="any of file,zip,live")
    ap.add_argument("--live-rates", type=lambda s: [int(x) for x in s.split(",")],
                    default=[500, 2000, 5000, 9000],
                    help="replay rates for the live part (default 500,2000,5000,9000)")
    ap.add_argument("--warmup", type=float, default=32.0,
                    help="seconds of live recording before measuring (default 32: "
                         "the Timeline's 30 s window full)")
    ap.add_argument("--live-seconds", type=float, default=60.0,
                    help="length of the replayed file, looped (default 60)")
    args = ap.parse_args()
    parts = set(args.parts.split(","))
    with tempfile.TemporaryDirectory(prefix="klein-measure-") as tmp:
        workdir = Path(tmp)
        path = workdir / "worst_case.btlog"
        n = 0
        if parts & {"file", "zip"}:
            t = time.perf_counter()
            n = synthetic_btlog(path, args.rate, args.minutes * 60)
            print(f"wrote {n:,} transitions in {time.perf_counter() - t:.1f} s")
        if "file" in parts:
            part_file(args, path, n)
        if "zip" in parts:
            part_zip(args, path)
        if "live" in parts:
            part_live(args, workdir)


if __name__ == "__main__":
    main()
