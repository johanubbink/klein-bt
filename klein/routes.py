"""klein.routes — the plain-HTTP side of the gateway's one port.

The gateway serves the dashboard and its WebSocket from one ``websockets``
server (docs/architecture.md, "Browser side: one port"). Before any
WebSocket handshake, websockets hands each request to ``Routes.handle``,
which answers it over HTTP, or lets ``/ws`` upgrade:

* the dashboard: every .html, .css and .js file in ``static/`` at
  ``/<name>`` (``/`` is ``/index.html``), read into memory once at startup;
* while recording, the recorded tree runs as downloads: ``/log/runs``,
  ``/log.btlog?run=N``, ``/log.bb.jsonl?run=N``, and all of them in one
  ``/log.zip`` (the Save button);
* with ``--debug``, ``/debug/state``: a read-only dump for the test harness.

Everything else is a 404. The recording and the dump are read through the
providers the gateway passes in, so the tests serve a bare ``Recording``.
"""

import asyncio
import io
import json
import sys
import time
import zipfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.datastructures import Headers
from websockets.http11 import Response

from .btlog import export_blackboard, export_run
from .recording import snapshot_run

STATIC_DIR = Path(__file__).resolve().parent / "static"    # the dashboard's files

# Static files served over HTTP at "/<name>" ("/" is "/index.html"): every file
# in static/ with one of these types, so a new script needs no list. The
# dashboard can't render without the page, D3 and app.js.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
RENDER_CRITICAL = ("index.html", "d3.v7.min.js", "app.js")
INDEX_FALLBACK = b"<!doctype html><h1>klein: index.html missing from package</h1>"
_TEXT = "text/plain; charset=utf-8"

# The recording's downloads (docs/architecture.md, "Browser side: one port"):
# a run's files by suffix (None: all of them in one zip) and their types.
_LOG_SUFFIXES = (".btlog", ".bb.jsonl")
_LOG_TYPES = {".btlog": "application/octet-stream", ".bb.jsonl": "application/x-ndjson",
              None: "application/zip"}
_LOG_ROUTES = ("/log/runs", "/log.zip", *(f"/log{suffix}" for suffix in _LOG_SUFFIXES))


def http_response(status, body, content_type, attachment=None):
    """A whole HTTP response, never cached. ``attachment`` is a filename: the
    browser saves the body under it."""
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = "no-store"
    if attachment is not None:
        headers["Content-Disposition"] = f'attachment; filename="{attachment}"'
    return Response(status, HTTPStatus(status).phrase, headers, body)


def run_entries(runs):
    """The ``GET /log/runs`` entry of each tree run (``Recording.runs()``).

    Run indices count from the oldest retained run, so they shift when
    eviction drops one. ``blackboard`` is False when the run has no
    blackboard sample (a file opened without its sidecar): its
    ``/log.bb.jsonl`` is then a 404, not a header-only sidecar that would
    reopen as empty boards. Two runs of one tree that start in the same
    second get ``_2``, ``_3``… after the time, so every name (and the zip's
    entries) stays unique.
    """
    entries = []
    used = set()
    for i, run in enumerate(runs):
        start = run[0].t_start
        tree_id = run[0].layout.tree["root_tree_id"]
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(start / 1e6))
        stem = base = f"{tree_id}_{stamp}"
        n = 1
        while stem in used:
            n += 1
            stem = f"{base}_{n}"
        used.add(stem)
        entries.append({"run": i, "tree_id": tree_id, "t_begin": start,
                        "t_end": run[-1].t_end, "filename": f"{stem}.btlog",
                        "blackboard": any(s.blackboard.t_start is not None for s in run)})
    return entries


def _log_files(run, entry, suffixes=_LOG_SUFFIXES):
    """Run ``run``'s downloads among ``suffixes``, ``{suffix: (filename,
    bytes)}``: its ``.btlog`` and, when it has a blackboard, its
    ``.bb.jsonl`` under the same stem."""
    stem = entry["filename"].removesuffix(".btlog")
    files = {}
    if ".btlog" in suffixes:
        files[".btlog"] = (entry["filename"], export_run(run, run[0].layout.xml))
    if ".bb.jsonl" in suffixes and entry["blackboard"]:
        files[".bb.jsonl"] = (stem + ".bb.jsonl", export_blackboard(run, entry["tree_id"]))
    return files


async def _download(runs, suffix, name=None):
    """Export ``runs`` (``[(run, entry)]``, snapshots) in a worker thread:
    the one run's ``suffix`` file, or with ``suffix`` None every run's files
    as one deflated ``.zip`` named ``name``."""
    def build():
        if suffix is not None:
            (run, entry), = runs
            return _log_files(run, entry, (suffix,))[suffix]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for run, entry in runs:
                for filename, data in _log_files(run, entry).values():
                    archive.writestr(filename, data)
        return name, buffer.getvalue()
    filename, body = await asyncio.to_thread(build)
    return http_response(200, body, _LOG_TYPES[suffix], filename)


def _log_response(recording, path, query):
    """``GET /log/runs``, ``/log.zip``, ``/log.btlog?run=N`` and
    ``/log.bb.jsonl?run=N``. Each request reads the runs once and snapshots
    them in one go (no await), so eviction can't change them halfway
    through; a download returns a coroutine (``_download``) that exports the
    snapshots off the event loop."""
    runs = recording.runs()
    entries = run_entries(runs)
    if path == "/log/runs":
        return http_response(200, json.dumps(entries).encode(), "application/json")
    if path == "/log.zip":
        name = time.strftime("klein_%Y-%m-%d_%H-%M-%S.zip")
        return _download([(snapshot_run(run), entry) for run, entry in zip(runs, entries)],
                         None, name)
    index = parse_qs(query).get("run", [""])[0]
    if not index.isdigit() or int(index) >= len(runs):
        return http_response(404, b"no such run", _TEXT)
    run, entry = runs[int(index)], entries[int(index)]
    suffix = path.removeprefix("/log")
    if suffix == ".bb.jsonl" and not entry["blackboard"]:  # e.g. a file opened without one
        return http_response(404, b"no blackboard in this run", _TEXT)
    return _download([(snapshot_run(run), entry)], suffix)


class Routes:
    """Answers the one port's HTTP requests.

    ``recording()`` returns the ``Recording`` to serve downloads of, or None
    (``--record-buffer 0``: the ``/log`` routes 404); it is asked on every
    request, since ``--open`` replaces the gateway's. ``debug_state()``
    returns the ``/debug/state`` dump; None (no ``--debug``) makes it a 404.
    """

    def __init__(self, static_dir, recording=lambda: None, debug_state=None):
        self.static_dir = Path(static_dir)
        self._recording = recording
        self._debug_state = debug_state
        self._static = {}                   # request path -> (body, content type), by load()

    def load(self):
        """Read the static files into memory once, keyed by URL path.

        A missing ``index.html`` falls back to a stub page; any other missing
        file just means its route 404s (and, for D3 or app.js, that the
        dashboard can't render).
        """
        try:
            files = sorted(f for f in self.static_dir.iterdir() if f.suffix in CONTENT_TYPES)
        except OSError:
            files = []
        for f in files:
            try:
                self._static[f"/{f.name}"] = (f.read_bytes(), CONTENT_TYPES[f.suffix])
            except OSError:
                pass                        # unreadable: its route 404s
        for name in RENDER_CRITICAL:
            if f"/{name}" not in self._static:
                print(f"[klein] warning: {name} not bundled; dashboard will not render.",
                      file=sys.stderr)
        self._static.setdefault("/index.html", (INDEX_FALLBACK, CONTENT_TYPES[".html"]))

    def handle(self, target):
        """The answer to ``GET target`` (a path, with any query): a
        ``Response``, an awaitable of one (a download, which exports off the
        event loop), or None for ``/ws``, which websockets then upgrades to a
        WebSocket."""
        url = urlsplit(target)
        path = url.path
        if path == "/ws":
            return None
        if path == "/debug/state" and self._debug_state is not None:
            return http_response(200, json.dumps(self._debug_state()).encode(),
                                 "application/json")
        recording = self._recording()
        if path in _LOG_ROUTES and recording is not None:
            return _log_response(recording, path, url.query)
        asset = self._static.get("/index.html" if path == "/" else path)
        if asset is None:
            return http_response(404, b"not found", _TEXT)
        return http_response(200, *asset)
