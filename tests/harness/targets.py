"""Robot targets for integration checks, behind one interface.

Every target is a real Groot2 publisher process on a free port::

    with MockTarget() as robot:          # or ReplayTarget(path), T11Target()
        robot.port                       # its REP port (it may bind port + 1 too)
        robot.ground_truth()             # [(absolute_us, uid, live_status)]

``ground_truth()`` is what the target *actually did*, from its own side, never
from anything klein said: the mock's ``--truth-log``, or t11's own FileLogger2
file. Oracles compare klein's view against it.

Processes run with this checkout on ``PYTHONPATH`` (never the installed
console scripts, which may point at another tree) and are killed on ``stop()``,
on context exit, and at interpreter exit as a last resort. Their files outlive
the ``with`` block, so ``ground_truth()`` can be read after it. ``Process`` is
the base the targets share with ``probes.GatewayProbe``.
"""
import atexit
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import weakref
from pathlib import Path

import zmq

from tests.harness import btlog_ref

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = Path(__file__).resolve().parent
BUILD_DIR = HARNESS_DIR / ".build"          # gitignored: compiled t11 lives here
PYTHON = sys.executable

_LIVE = set()                               # every process a target started


def _kill_all():
    for proc in list(_LIVE):
        terminate(proc)


atexit.register(_kill_all)


def terminate(proc, grace=3.0):
    if proc.poll() is None:
        # SIGTERM: nothing here needs a clean shutdown (the truth log is flushed
        # per batch, FileLogger2 every 10 ms).
        proc.terminate()
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    _LIVE.discard(proc)


def scratch_dir(owner, prefix):
    """A temp directory removed when ``owner`` is garbage-collected (or at exit)."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    weakref.finalize(owner, shutil.rmtree, path, ignore_errors=True)
    return path


def repo_env():
    """An environment whose ``python -m klein.…`` imports this checkout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"]
                                          if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def spawn(argv, log_path, cwd=REPO_ROOT, env=None):
    """Start a tracked subprocess with stdout+stderr going to ``log_path``."""
    log = open(log_path, "ab")
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env or repo_env(),
                                stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    finally:
        log.close()
    _LIVE.add(proc)
    return proc


def free_port(pair=False):
    """A TCP port free right now on 127.0.0.1 (and ``port + 1`` too, if ``pair``)."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not pair:
            return port
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
    raise RuntimeError("no free port pair found")


def robot_request(port, request_type, payload=None, timeout=1.0):
    """One Groot2 request straight to a publisher; returns the reply frames.

    ``request_type`` is a letter (``"T"``, ``"S"``, ``"r"``, …). Raises
    ``TimeoutError`` when nothing answers. Uses its own socket, so it never
    disturbs a klein gateway talking to the same robot.
    """
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    try:
        sock.connect(f"tcp://127.0.0.1:{port}")
        header = struct.pack("<BBI", 2, ord(request_type), 1)
        sock.send_multipart([header] if payload is None else [header, payload])
        if not sock.poll(int(timeout * 1000)):
            raise TimeoutError(f"no reply from port {port} within {timeout}s")
        return sock.recv_multipart()
    finally:
        sock.close()


def wait_for_robot(port, proc=None, timeout=10.0, log_path=None):
    """Block until a publisher on ``port`` answers FULLTREE; returns its XML."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            break
        try:
            reply = robot_request(port, "T", timeout=0.5)
            if len(reply) >= 2 and reply[0] != b"error":
                return reply[1].decode("utf-8")
        except TimeoutError:
            pass
    tail = Path(log_path).read_text(errors="replace")[-2000:] if log_path else ""
    raise RuntimeError(f"robot on port {port} never answered FULLTREE\n{tail}")


PORT_TAKEN = "already in use"      # zmq's EADDRINUSE text, and the gateway's own message
PORT_RETRIES = 3


def launch(owner, argv_for, pick_port, cwd=REPO_ROOT, env=None, ready=None):
    """Spawn ``argv_for(port)`` and wait for ``ready(port, proc)``, retrying with
    a fresh port when the process lost its port to someone else.

    ``free_port`` releases the port before the process binds it, so another
    socket (an ephemeral client port, say) can take it in between. That is the
    only failure retried; ``pick_port`` is None when the port is fixed (a
    restart on the same port), and then nothing is retried.
    """
    for attempt in range(PORT_RETRIES):
        if owner.port is None or (attempt and pick_port):
            owner.port = pick_port()
        seen = owner.log_path.stat().st_size if owner.log_path.exists() else 0
        owner.proc = spawn(argv_for(owner.port), owner.log_path, cwd=cwd, env=env)
        try:
            return ready(owner.port, owner.proc)
        except RuntimeError:
            lost = PORT_TAKEN in owner.log_path.read_text(errors="replace")[seen:]
            terminate(owner.proc)
            owner.proc = None
            if not (lost and pick_port and attempt + 1 < PORT_RETRIES):
                raise


def _ready_robot(owner):
    return lambda port, proc: wait_for_robot(port, proc, log_path=owner.log_path)


class Process:
    """A tracked subprocess on a free port: ``port``, ``proc``, ``log()``,
    ``stop()``, and a context manager that starts it and stops it again.

    Its work directory (the log, and whatever the process writes there)
    outlives the ``with`` block, so logs and ground truth can be read after
    it; it goes when the object is garbage-collected, or on ``cleanup()``.
    """

    name = "process"

    def __init__(self):
        self.port = None
        self.proc = None
        self.workdir = scratch_dir(self, f"klein-{self.name}-")
        self.log_path = self.workdir / f"{self.name}.log"

    def start(self):
        raise NotImplementedError

    def stop(self):
        if self.proc is not None:
            terminate(self.proc)
            self.proc = None

    def log(self):
        """The process's combined stdout/stderr so far (for failure messages)."""
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def cleanup(self):
        """Stop, and delete the work directory."""
        self.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


class Target(Process):
    """Base: a publisher process with ``start``/``stop``/``port``/``ground_truth``."""

    name = "target"

    def __init__(self):
        super().__init__()
        self.xml = None                     # FULLTREE as served, once started

    def ground_truth(self):
        raise NotImplementedError


def read_truth_log(path):
    """Parse a mock ``--truth-log``: ``([(abs_us, uid, status)], [publisher markers])``.

    A marker is ``{"index", "name", "uuid", "t"}``, where ``index`` is how many
    records came before it. A last line still being written is skipped.
    """
    records, markers = [], []
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return records, markers
    lines = text.split("\n")
    for line in lines[:-1]:             # the part after the last "\n" is incomplete
        if line.startswith("# publisher "):
            parts = dict(p.split("=", 1) for p in line.split()[3:])
            markers.append({"index": len(records), "name": line.split()[2],
                            "uuid": parts.get("uuid"), "t": int(parts.get("t", 0))})
        elif line and not line.startswith("#"):
            t, uid, status = line.split()
            records.append((int(t), int(uid), int(status)))
    return records, markers


class MockTarget(Target):
    """``klein.mock_robot`` on a free port, with its ``--truth-log`` as ground truth.

    Options map to the mock's flags: ``tree``, ``switch_every``, ``replay`` (a
    ``.btlog`` path). ``restart()`` replaces the process on the same port — a
    new publisher UUID, as a real robot restart gives — and keeps appending to
    the same truth log.

    Remember the live mock only advances on STATUS requests: no poller, no
    transitions.
    """

    name = "mock"

    def __init__(self, tree=None, switch_every=0, replay=None):
        super().__init__()
        self.truth_path = self.workdir / "truth.log"
        self.args = []
        if tree:
            self.args += ["--tree", tree]
        if switch_every:
            self.args += ["--switch-every", str(switch_every)]
        if replay:
            self.args += ["--replay", str(Path(replay).resolve())]

    def start(self):
        if self.proc is not None:
            return self
        self._start(pick_port=free_port)
        return self

    def _start(self, pick_port):
        argv = lambda port: [PYTHON, "-m", "klein.mock_robot", "--host", "127.0.0.1",
                             "--port", str(port), "--truth-log", str(self.truth_path),
                             *self.args]
        self.xml = launch(self, argv, pick_port, ready=_ready_robot(self))

    def restart(self):
        """A new mock process on the *same* port (so no port retry)."""
        self.stop()
        self._start(pick_port=None)
        return self

    def ground_truth(self):
        return read_truth_log(self.truth_path)[0]

    def publishers(self):
        """One marker per publisher the truth log saw (start, swaps, restarts)."""
        return read_truth_log(self.truth_path)[1]


class ReplayTarget(MockTarget):
    """The mock in ``--replay`` mode: a recorded ``.btlog`` served live, looping.

    Real-robot timing (µs bursts, retries inside one poll) with no C++ at test
    time; ground truth is the same truth log as ``MockTarget``'s.
    """

    name = "replay"

    def __init__(self, btlog_path):
        super().__init__(replay=btlog_path)


def find_btcpp():
    """The BehaviorTree.CPP checkout: ``$KLEIN_BTCPP_DIR``, or a
    ``BehaviorTree.CPP`` directory next to this repo or any parent of it."""
    env = os.environ.get("KLEIN_BTCPP_DIR")
    if env:
        return Path(env)
    for parent in REPO_ROOT.parents:
        candidate = parent / "BehaviorTree.CPP"
        if candidate.is_dir():
            return candidate
    return None


def build_t11():
    """Compile (or reuse) the port-argument t11; returns its path or raises SkipTest."""
    bt = find_btcpp()
    if bt is None:
        raise unittest.SkipTest("no BehaviorTree.CPP checkout found (set KLEIN_BTCPP_DIR)")
    source = bt / "examples" / "t11_groot_howto.cpp"
    lib = bt / "build" / "libbehaviortree_cpp.so"
    sample = bt / "build" / "sample_nodes" / "lib" / "libbt_sample_nodes.a"
    for need in (source, lib, sample):
        if not need.exists():
            raise unittest.SkipTest(f"BehaviorTree.CPP build incomplete: {need} missing")
    if shutil.which("g++") is None:
        raise unittest.SkipTest("g++ not installed; cannot build t11_groot_howto")
    binary = BUILD_DIR / "t11_port"
    inputs = (source, lib, sample, HARNESS_DIR / "build_t11.sh")
    if binary.exists() and binary.stat().st_mtime >= max(p.stat().st_mtime for p in inputs):
        return binary
    result = subprocess.run(["sh", str(HARNESS_DIR / "build_t11.sh"), str(bt), str(BUILD_DIR)],
                            capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise unittest.SkipTest(f"building t11 failed:\n{result.stderr[-2000:]}")
    return binary


class T11Target(Target):
    """The real BehaviorTree.CPP ``t11_groot_howto`` (CrossDoor, looping forever).

    Built on first use into ``tests/harness/.build/`` and run in a temp cwd, where
    it writes ``t11_groot_howto.btlog`` — its FileLogger2, the ground truth.
    Binds ``port`` and ``port + 1``. ``start()`` raises ``unittest.SkipTest`` when
    g++ or the BehaviorTree.CPP build is unavailable.
    """

    name = "t11"
    BTLOG_NAME = "t11_groot_howto.btlog"
    FLUSH_WAIT = 0.05               # s; FileLogger2 flushes every <= 10 ms

    def __init__(self):
        super().__init__()
        self.btlog_path = self.workdir / self.BTLOG_NAME

    def start(self):
        if self.proc is not None:
            return self
        binary = build_t11()
        self.xml = launch(self, lambda port: [str(binary), str(port)],
                          lambda: free_port(pair=True), cwd=self.workdir,
                          env=dict(os.environ), ready=_ready_robot(self))
        return self

    def btlog_bytes(self):
        """The FileLogger2 file as written so far, cut to whole records."""
        data = self.btlog_path.read_bytes()
        log = btlog_ref.parse(data)
        return data[:len(data) - log.trailing]

    def ground_truth(self):
        """The file's records. FileLogger2's writer thread flushes every <= 10 ms,
        so a transition already on the wire can be missing from the file for that
        long: wait it out first, or the truth ends early."""
        if self.proc is not None:
            time.sleep(self.FLUSH_WAIT)
        return btlog_ref.absolute(btlog_ref.parse(self.btlog_bytes()))
