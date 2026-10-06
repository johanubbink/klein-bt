"""Probes: read what klein actually did, from the outside.

* ``WireTap`` sits between klein and a robot and decodes every Groot2 message.
* ``GatewayProbe`` runs klein's gateway as a subprocess and talks HTTP/WS to it.
* ``BrowserProbe`` drives the dashboard in a real browser (Playwright).

None of them import klein's gateway code: a probe that shared klein's decoding
would agree with klein's bugs.
"""
import asyncio
import base64
import itertools
import json
import os
import struct
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.harness.targets import PYTHON, REPO_ROOT, Process, free_port, launch

ARTIFACTS_DIR = REPO_ROOT / "tests" / "ui" / "artifacts"      # gitignored
SHOTS = bool(os.environ.get("KLEIN_SHOTS"))     # BrowserProbe.screenshot saves PNGs


# --------------------------------------------------------------------------- #
# WireTap
# --------------------------------------------------------------------------- #
ZMTP_GREETING_SIZE = 64
FLAG_MORE, FLAG_LONG, FLAG_COMMAND = 0x01, 0x02, 0x04


def decode_message(direction, frames):
    """One ZMTP message (all frames up to MORE=0) as a Groot2 record dict."""
    frames = frames[1:]                         # drop the REQ/REP envelope delimiter
    rec = {"dir": direction, "sizes": [len(f) for f in frames]}
    head = frames[0]
    if head == b"error":                        # e.g. `r` on a BT.CPP without recording
        rec["error"] = frames[1].decode("utf-8", "replace")
        return rec
    rec["type"] = chr(head[1])
    rec["id"] = struct.unpack_from("<I", head, 2)[0]
    if len(head) >= 22:                         # replies carry the tree UUID
        rec["uuid"] = head[6:22].hex()
    if len(frames) < 2:
        return rec
    body = frames[1]
    kind = rec["type"]
    if direction == "req":
        rec["arg"] = body[:200].decode("utf-8", "replace")
    elif kind == "t":
        rec["transitions"] = [
            (int.from_bytes(body[o:o + 6], "little"), *struct.unpack_from("<HB", body, o + 6))
            for o in range(0, len(body), 9)]
    elif kind == "S":
        rec["status"] = dict(struct.iter_unpack("<HB", body))
    elif kind == "r":
        rec["payload"] = body.decode("utf-8", "replace")
    return rec


class _ZmtpParser:
    """Incremental ZMTP 3 stream decoder for one direction of one connection."""

    def __init__(self, emit):
        self._buf = bytearray()
        self._greeted = False
        self._frames = []
        self._emit = emit

    def feed(self, data):
        self._buf += data
        if not self._greeted:
            if len(self._buf) < ZMTP_GREETING_SIZE:
                return
            del self._buf[:ZMTP_GREETING_SIZE]
            self._greeted = True
        while len(self._buf) >= 2:
            flags = self._buf[0]
            if flags & FLAG_LONG:
                if len(self._buf) < 9:
                    return
                size, hdr = struct.unpack_from(">Q", self._buf, 1)[0], 9
            else:
                size, hdr = self._buf[1], 2
            if len(self._buf) < hdr + size:
                return
            body = bytes(self._buf[hdr:hdr + size])
            del self._buf[:hdr + size]
            if flags & FLAG_COMMAND:            # READY, PING, … — not messages
                continue
            self._frames.append(body)
            if not flags & FLAG_MORE:
                frames, self._frames = self._frames, []
                self._emit(frames)


class WireTap:
    """A transparent TCP proxy in front of a robot that logs every Groot2 message.

    Point klein at ``tap.port``; the tap forwards bytes unchanged to the
    upstream port, decoding ZMTP 3 on the side. It runs its own asyncio loop in
    a thread, so it works from plain synchronous tests::

        with WireTap(robot.port) as tap:
            ... start klein with --robot-port tap.port ...
            tap.request_sequence(ignore="B")      # e.g. "TSSSSS"

    Each request record carries ``t`` (monotonic), ``wall`` (time.time()),
    ``conn`` (connection number), ``type``, ``id``, ``arg``, ``sizes``, and once
    answered, ``reply`` — the reply record, with ``uuid``, ``sizes``, ``t`` and
    the decoded ``transitions`` (t), ``status`` (S) or ``payload`` (r).
    """

    def __init__(self, upstream_port):
        self.upstream = ("127.0.0.1", upstream_port)
        self.port = None
        self._lock = threading.Lock()
        self._messages = []
        self._pending = {}          # conn -> requests awaiting their reply
        self._conn_ids = itertools.count(1)
        self._loop = None
        self._thread = None
        self._server = None
        self._writers = set()

    # -- lifecycle ------------------------------------------------------ #
    def start(self):
        ready = threading.Event()
        self._loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._open())
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="wiretap", daemon=True)
        self._thread.start()
        if not ready.wait(5):
            raise RuntimeError("wire tap failed to start")
        return self

    async def _open(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self):
        if self._loop is None:
            return
        async def close():
            self._server.close()
            for w in list(self._writers):
                w.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(close(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()
        self._loop = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- proxying ------------------------------------------------------- #
    async def _handle(self, client_r, client_w):
        conn = next(self._conn_ids)
        try:
            up_r, up_w = await asyncio.open_connection(*self.upstream)
        except OSError:                     # robot down: klein sees a refused connection
            client_w.close()
            return
        self._writers |= {client_w, up_w}
        req = _ZmtpParser(lambda frames: self._record(conn, "req", frames))
        rep = _ZmtpParser(lambda frames: self._record(conn, "rep", frames))
        await asyncio.gather(self._pipe(client_r, up_w, req), self._pipe(up_r, client_w, rep))
        self._writers -= {client_w, up_w}

    @staticmethod
    async def _pipe(reader, writer, parser):
        try:
            while data := await reader.read(65536):
                parser.feed(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    def _record(self, conn, direction, frames):
        rec = decode_message(direction, frames)
        rec.update(t=time.monotonic(), wall=time.time(), conn=conn)
        with self._lock:
            self._messages.append(rec)
            pending = self._pending.setdefault(conn, [])
            if direction == "req":
                pending.append(rec)
            elif pending:                   # REQ/REP strictly alternates per connection
                pending.pop(0)["reply"] = rec

    # -- reading -------------------------------------------------------- #
    def messages(self):
        with self._lock:
            return list(self._messages)

    def requests(self, types=None):
        """Request records in order (optionally only these letters)."""
        return [m for m in self.messages()
                if m["dir"] == "req" and (types is None or m["type"] in types)]

    def request_sequence(self, ignore=""):
        """The request letters in order, e.g. ``"TSSBSS"``; ``ignore`` drops
        letters (``"B"`` hides the 2 Hz blackboard poll)."""
        return "".join(m["type"] for m in self.requests() if m["type"] not in ignore)

    def transitions(self):
        """Every transition in every answered ``t``, as ``(offset_us, uid, status)``."""
        return [tr for m in self.requests("t") for tr in m.get("reply", {}).get("transitions", [])]

    def wait_for(self, predicate, timeout=10.0, interval=0.05):
        """Poll ``predicate(tap)`` until true; returns it, or False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate(self)
            if result:
                return result
            time.sleep(interval)
        return predicate(self)


# --------------------------------------------------------------------------- #
# GatewayProbe
# --------------------------------------------------------------------------- #
class GatewayProbe(Process):
    """klein's gateway as a subprocess (``python -m klein.cli``) on a free port.

    ``fetch(path)`` does HTTP; ``watch()`` holds a WebSocket open — which is
    also what makes a gateway with ``--record-buffer 0`` poll the robot at all
    — and collects the frames it receives. ``debug_state()`` reads ``GET
    /debug/state``, the read-only dump behind ``--debug`` (``debug=True``);
    without it the route 404s and this returns ``None`` (use
    ``require_debug_state()`` to skip instead). ``poll_interval`` (seconds)
    speeds up the status poll, and so the mock, which steps once per poll.
    """

    # ``poll_interval`` runs the same CLI with the gateway's status poll
    # interval changed — a module setting, deliberately not a flag.
    _FAST_POLL = ("import sys; from klein import cli, gateway; "
                  "gateway.POLL_INTERVAL = float(sys.argv.pop(1)); cli.main_cli()")

    name = "gateway"

    def __init__(self, robot_port, debug=False, extra_args=(), poll_interval=None):
        super().__init__()
        self.robot_port = robot_port
        self.args = (["--debug"] if debug else []) + list(extra_args)
        self.launcher = (["-m", "klein.cli"] if poll_interval is None
                         else ["-c", self._FAST_POLL, str(poll_interval)])

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout=10.0):
        argv = lambda port: [PYTHON, *self.launcher, "--robot-host", "127.0.0.1",
                             "--robot-port", str(self.robot_port), "--port", str(port),
                             "--no-browser", *self.args]

        def ready(port, proc):
            # Served by this gateway, which says so once it listens: one that
            # lost its port exits, while whatever took the port answers / too.
            seen = len(self.log())
            live = f"live on http://localhost:{port}"
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    if live in self.log()[seen:] and self.fetch("/", timeout=0.5)[0] == 200:
                        return
                except OSError:
                    pass
                time.sleep(0.05)
            raise RuntimeError(f"gateway never served / on port {port}\n{self.log()[-2000:]}")

        launch(self, argv, free_port, ready=ready)
        return self

    def fetch(self, path, timeout=5.0):
        """``(status, headers, body_bytes)`` for a GET; HTTP errors are returned, not raised."""
        try:
            with urllib.request.urlopen(self.url + path, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def debug_state(self):
        status, _headers, body = self.fetch("/debug/state")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"/debug/state answered {status}: {body[:200]!r}")
        return json.loads(body)

    def require_debug_state(self):
        state = self.debug_state()
        if state is None:
            raise unittest.SkipTest("gateway has no /debug/state (it is served "
                                    "only with --debug: GatewayProbe(debug=True))")
        return state

    def watch(self):
        """A context manager holding one WebSocket open; see ``DashboardSocket``."""
        return DashboardSocket(self.url.replace("http", "ws", 1) + "/ws")


class DashboardSocket:
    """A WebSocket to the gateway, collecting frames on a background thread.

    ``frames`` holds decoded JSON for text frames and raw ``bytes`` for binary
    ones, in arrival order. ``of_type("layout")`` filters text frames.
    """

    def __init__(self, url):
        self.url = url
        self.frames = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._error = None

    def __enter__(self):
        from websockets.sync.client import connect
        self._ws = connect(self.url, open_timeout=5, max_size=None)

        def run():
            while not self._stop.is_set():
                try:
                    msg = self._ws.recv(timeout=0.1)
                except TimeoutError:
                    continue
                except Exception as exc:        # closed by the server
                    self._error = exc
                    return
                frame = json.loads(msg) if isinstance(msg, str) else msg
                with self._lock:
                    self.frames.append(frame)

        self._thread = threading.Thread(target=run, name="dashboard-ws", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(2)
        self._ws.close()

    def of_type(self, kind):
        with self._lock:
            return [f for f in self.frames if isinstance(f, dict) and f.get("type") == kind]

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate(self)
            if result:
                return result
            time.sleep(0.05)
        return predicate(self)


# --------------------------------------------------------------------------- #
# BrowserProbe
# --------------------------------------------------------------------------- #
# Installed before the dashboard's own scripts: remembers the last status frame
# the page received, so a test can read the DOM and the frame it should show in
# one synchronous evaluate (JS runs one task at a time, so the two agree).
_CAPTURE_WS = """
(() => {
  const Native = window.WebSocket;
  const cap = window.__kleinProbe = { frames: 0, types: {}, lastStatus: null, statuses: [] };
  function Tapped(...args) {
    const ws = new Native(...args);
    ws.addEventListener("message", (ev) => {
      if (typeof ev.data !== "string") return;
      try {
        const m = JSON.parse(ev.data);
        cap.frames++;
        cap.types[m.type] = (cap.types[m.type] || 0) + 1;
        if (m.type === "status") {
          cap.lastStatus = m.data;
          cap.statuses.push(m.data);          // the last few, for lag-tolerant checks
          if (cap.statuses.length > 50) cap.statuses.shift();
        }
      } catch (e) { /* not JSON: not ours to judge */ }
    });
    return ws;
  }
  Tapped.prototype = Native.prototype;
  for (const k of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) Tapped[k] = Native[k];
  window.WebSocket = Tapped;
})();
"""

# Every node card, straight from the DOM. The uid is read from the d3 datum the
# card is bound to; everything else is what the user sees.
_READ_NODES = """
() => [...document.querySelectorAll("#canvas g.node")].map((g) => {
  const d = (g.__data__ && g.__data__.data) || {};
  const q = (s) => g.querySelector(s);
  const attr = (s, a) => (q(s) ? q(s).getAttribute(a) : null);
  return {
    uid: d.uid === undefined ? null : d.uid,
    id: d.id === undefined ? null : d.id,
    name: q(".node-name") ? q(".node-name").textContent : null,
    type: q(".node-type") ? q(".node-type").textContent : null,
    label: q(".node-status-text") ? q(".node-status-text").textContent : null,
    classes: g.getAttribute("class"),
    rect_classes: attr(".node-rect", "class"),
    pill_classes: attr(".status-pill", "class"),
    stroke: q(".node-rect") ? q(".node-rect").style.stroke : null,
    pill_fill: q(".status-pill") ? q(".status-pill").style.fill : null,
  };
})
"""

_READ_CONNECTION = """
() => {
  const dot = document.getElementById("conn-dot");
  const text = document.getElementById("conn-text");
  return { classes: dot ? dot.getAttribute("class") : null,
           text: text ? text.textContent : null };
}
"""


# Resolves once the next frame has been painted (the first rAF reaches the
# frame, the second runs after it rendered).
NEXT_FRAME = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"

# The drawer's "Jump to live" button ("Jump to end" for an opened file), in
# its transport row.
BACK_TO_LIVE = "#tr-jump"

# Recording frames handed straight to the page's store, then one render.
_FEED = """(frames) => { for (const f of frames) {
    if (f.text) { recordingStore.ingest(JSON.parse(f.text)); continue; }
    recordingStore.ingest(Uint8Array.from(atob(f.b64), c => c.charCodeAt(0)).buffer); }
    requestRender({ boards: true }); }"""

# What the blackboard panel prints for each value: the dashboard's own
# renderer (its formatting is tested elsewhere; callers check which value).
_SUMMARIES = """(boards) => Object.fromEntries(Object.entries(boards).map(([name, entries]) =>
  [name, Object.fromEntries(Object.entries(entries).map(([k, v]) =>
    [k, KleinRenderers.renderValue(v).summary]))]))"""


def label_to_state(label):
    """A status pill's text as ``{"status", "from"}``: "was SUCCESS" is IDLE-from-SUCCESS."""
    if label and label.startswith("was "):
        return {"status": "IDLE", "from": label[4:]}
    return {"status": label, "from": None}


def dot_state(classes):
    """``"online"`` (green), ``"warn"`` (amber) or ``"offline"`` (red) from the dot's classes."""
    names = (classes or "").split()
    if "online" in names:
        return "online"
    if "warn" in names:
        return "warn"
    return "offline"


class BrowserProbe:
    """The dashboard in a real headless browser, via Playwright's sync API.

    Uses the installed Google Chrome (``channel="chrome"``) and falls back to
    Playwright's own Chromium; raises ``unittest.SkipTest`` when Playwright or
    any browser is unavailable, so UI checks skip rather than fail.
    """

    def __init__(self, base_url, viewport=(1400, 900)):
        self.base_url = base_url
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self._pw = self.browser = self.page = None
        self.console = []                   # console messages + page errors

    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright not installed (pip install -e '.[dev]')")
        self._pw = sync_playwright().start()
        errors = []
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                self.browser = self._pw.chromium.launch(headless=True, **kwargs)
                break
            except Exception as exc:
                errors.append(f"{kwargs or 'bundled chromium'}: {str(exc).splitlines()[0]}")
        if self.browser is None:
            self._pw.stop()
            raise unittest.SkipTest("no browser Playwright can launch: " + "; ".join(errors))
        self.page = self.browser.new_page(viewport=self.viewport)
        self.page.add_init_script(_CAPTURE_WS)
        self.page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}"))
        self.page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        return self

    def stop(self):
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- navigation ------------------------------------------------------ #
    def open(self, path="/", wait_nodes=1, timeout=10.0, fresh=False):
        """Load the dashboard and wait for ``wait_nodes`` settled cards (0:
        don't wait). ``fresh`` clears its localStorage first (panel sizes,
        folds, the drawer tab), so it opens as on a first visit."""
        if fresh:
            self.page.goto(self.base_url + path)
            self.page.evaluate("() => localStorage.clear()")
        self.page.goto(self.base_url + path)
        if wait_nodes:
            self.wait_for_nodes(wait_nodes, timeout)
        return self

    def next_frame(self):
        """Wait until the page has painted its next frame."""
        self.page.evaluate(NEXT_FRAME)

    def go_live(self):
        """Back to live (what "Jump to live" and Esc do), unless already live."""
        self.page.evaluate("() => { if (clock.mode !== 'live') KleinDrawer.goLive(); }")
        self.page.wait_for_function("!document.body.classList.contains('viewing-past')")

    def feed(self, frames):
        """Hand recording frames (``bytes`` binary, ``str`` JSON, as
        ``klein.streaming`` makes them) straight to the page's store, then
        wait for the render they ask for."""
        self.page.evaluate(_FEED, [{"b64": base64.b64encode(f).decode()} if isinstance(f, bytes)
                                   else {"text": f} for f in frames])
        self.next_frame()

    def page_errors(self):
        """The uncaught page errors since the last call (and clears them)."""
        errors = [m for m in self.console if m.startswith("pageerror")]
        self.console.clear()
        return errors

    def wait_for_nodes(self, count=1, timeout=10.0):
        """Wait until at least ``count`` node cards exist and have settled: no
        card or link has a d3 transition left (d3 deletes ``__transition``
        from an element when its last one ends; the enter ones run 250 ms)."""
        self.page.wait_for_function(
            f"document.querySelectorAll('#canvas g.node').length >= {int(count)}"
            " && ![...document.querySelectorAll('#canvas g.node, #canvas path.link')]"
            ".some(el => el.__transition)",
            timeout=timeout * 1000)

    def wait_connected(self, timeout=10.0):
        self.page.wait_for_function(
            "(document.getElementById('conn-dot') || {}).className"
            " && document.getElementById('conn-dot').classList.contains('online')",
            timeout=timeout * 1000)

    def wait_for_status_frames(self, count=1, timeout=10.0):
        self.page.wait_for_function(
            f"(window.__kleinProbe.types.status || 0) >= {int(count)}", timeout=timeout * 1000)

    # -- reading --------------------------------------------------------- #
    def nodes(self):
        return self.page.evaluate(_READ_NODES)

    def state(self, nodes=None):
        """``{uid: {"status", "from"}}`` as the cards show it."""
        return {n["uid"]: label_to_state(n["label"])
                for n in (nodes if nodes is not None else self.nodes()) if n["uid"] is not None}

    def snapshot(self, before_js=None, painted=False):
        """The cards, the last status frame the page received, and the dot — read
        in one evaluate, so they are mutually consistent. ``before_js`` (a
        statement) runs first in the same task, e.g. to tamper with the DOM.

        Also ``displayed``/``displaySource`` (what ``window.kleinDebug`` says the
        page last painted, ``None`` without it) and ``statusCount`` (status
        frames received so far; see ``status_frames``).

        The page paints on the next animation frame, so ``displayed`` can be
        several frames older than ``statusCount`` (a 10 ms poll, a loaded
        machine). ``painted=True`` reads at a moment the page has painted
        every frame it received (no render queued), so the two agree."""
        script = ("() => {" + (before_js or "") + "; const d = window.kleinDebug;"
                  " const dbg = typeof d === 'function' ? d() : null;"
                  " return { nodes: (" + _READ_NODES + ")(),"
                  " lastStatus: window.__kleinProbe.lastStatus,"
                  " statusCount: window.__kleinProbe.types.status || 0,"
                  " displayed: dbg && dbg.displayed, displaySource: dbg && dbg.displaySource,"
                  " frames: window.__kleinProbe.types,"
                  " connection: (" + _READ_CONNECTION + ")() }; }")
        if painted:
            snap = self.page.wait_for_function(
                f"() => !renderQueued && ({script})()", polling="raf", timeout=10000).json_value()
        else:
            snap = self.page.evaluate(script)
        snap["state"] = self.state(snap["nodes"])
        snap["connection"]["state"] = dot_state(snap["connection"]["classes"])
        last = snap.get("lastStatus") or {}
        snap["lastStatus"] = {int(k): v for k, v in last.items()}
        if snap["displayed"] is not None:
            snap["displayed"] = {int(k): v for k, v in snap["displayed"].items()}
        return snap

    def status_frames(self, first, last):
        """Status frames number ``first..last`` (1-based, as ``statusCount``
        counts them), waiting for ``last`` to arrive; the page keeps the latest 50."""
        self.page.wait_for_function(
            f"(window.__kleinProbe.types.status || 0) >= {int(last)}", timeout=10000)
        frames, count = self.page.evaluate(
            "() => [window.__kleinProbe.statuses, window.__kleinProbe.types.status]")
        frames = frames[len(frames) - (count - first + 1):][:last - first + 1]
        return [{int(k): v for k, v in f.items()} for f in frames]

    def value_summaries(self, boards):
        """``{board: {key: summary}}``: how the blackboard panel prints each
        value of ``boards``, by the dashboard's own renderer."""
        return self.page.evaluate(_SUMMARIES, boards)

    def klein_debug(self):
        """``window.kleinDebug()`` (the dashboard's read-only debug accessor), or
        ``None`` on a page that does not define it."""
        return self.page.evaluate(
            "() => { const d = window.kleinDebug;"
            " return d === undefined ? null : typeof d === 'function' ? d() : d; }")

    # -- acting ---------------------------------------------------------- #
    def card(self, uid):
        """A locator for the node card bound to ``uid``."""
        index = next((i for i, n in enumerate(self.nodes()) if n["uid"] == uid), None)
        if index is None:
            raise LookupError(f"no card for uid {uid}")
        return self.page.locator("#canvas g.node").nth(index)

    def click(self, selector=None, uid=None, **kwargs):
        target = self.card(uid) if uid is not None else self.page.locator(selector)
        target.click(**kwargs)

    def drag(self, start, end, steps=10):
        """Mouse-drag from ``(x, y)`` to ``(x, y)`` in viewport pixels."""
        mouse = self.page.mouse
        mouse.move(*start)
        mouse.down()
        mouse.move(*end, steps=steps)
        mouse.up()

    def key(self, key):
        self.page.keyboard.press(key)

    def wait_settled(self, timeout=5.0):
        """Wait until the camera stops moving: the canvas group's transform is
        unchanged across two reads 150 ms apart (zoom/pan transitions run 250+ ms)."""
        read = "() => document.querySelector('#canvas g.draw-group').getAttribute('transform')"
        deadline = time.monotonic() + timeout
        last = self.page.evaluate(read)
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(150)
            now = self.page.evaluate(read)
            if now == last:
                return
            last = now
        raise TimeoutError("the canvas never stopped moving")

    def screenshot(self, group, name):
        """With ``KLEIN_SHOTS=1``, save a full-page PNG to
        ``tests/ui/artifacts/<group>/<name>.png`` once the camera has settled and
        return the path; otherwise do nothing and return ``None``. The shots are
        for looking at a change (a person or an agent), never for an assertion."""
        if not SHOTS:
            return None
        self.wait_settled()
        path = ARTIFACTS_DIR / group / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)
        return path

