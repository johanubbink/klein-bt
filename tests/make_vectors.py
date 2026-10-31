"""The browser model's vectors, built from the Python model at test time.

A seeded Python ``Recording`` (three segments: a tree swap and a same-tree
restart after an outage, chunks with duplicate timestamps, early chunks
evicted, blackboard changes and removals) is streamed through
``klein.streaming`` twice: to a client subscribed from the start (every
incremental frame) and to one that joins at the end (the backfill). The
vectors feed both frame lists into a ``KleinRecording.createStore()`` under gjs
(``tests/js/run.js``) and expect, for each, what the Python model answers.

Functions the Python model has (``state_at_seq``, ``seq_at_time``,
``iter_records``, ``BlackboardTrack.at``) give their own expectations; the
rest (``intervals``, ``timeAtSeq``, the cursor) are written out
here in Python from their definitions in docs/architecture.md, "Browser
model", and the Timeline's window (``window.js``) step by step from its
rules in "The drawer".

Chunks are 8 records here, not 1024, so the vectors stay small: the browser
takes chunk boundaries from the frames and never assumes a size.

``build_named`` is a second scenario, on trees with named nodes and
subtrees, for the Log's and the Timeline's helpers (``test_log_rows``,
``test_timeline_model``); ``store_cases`` feeds any frames into a store.

    python -m tests.make_vectors FILE.json   # write them out, e.g. to read a failure
"""
import base64
import json
import random
from pathlib import Path
from unittest import mock

from klein import recording
from klein.recording import BlackboardTrack, Recording, apply_transition, decode_state
from klein.streaming import Streamer
from tests.harness.model import BB_RECENT, Model
from tests.helpers import layout

STATIC = Path(__file__).resolve().parents[1] / "klein" / "static"
CHUNK_SIZE = 8
T0 = 1_759_300_000_000_000          # absolute robot µs, as the gateway records them


def _layout(generation, uids):
    tree = {"id": f"{generation}:1", "uid": uids[0], "name": f"Tree{generation}",
            "children": [{"id": f"{generation}:{u}", "uid": u, "children": []}
                         for u in uids[1:]]}
    return layout(tree=tree, uids=uids, generation=generation)


def build():
    """``(recording, live frames, late frames)``, built under the small chunk size."""
    rng = random.Random(20261001)
    rec = Recording(keep_us=3_000)
    frames = {"live": [], "late": []}
    streamer = Streamer(rec, lambda clients, f: [frames[c].append(f) for c in clients])
    streamer.subscribe("live")
    t = T0
    live = set()                    # uids last seen in a live (non-IDLE) status

    def begin(layout, baseline):
        live.clear()
        live.update(uid for uid, value in enumerate(baseline) if 0 < value < 10)
        rec.begin_segment(layout, t, baseline)

    def drains(layout, count, bb_every=3):
        nonlocal t
        for i in range(count):
            records = []
            for _ in range(rng.randint(1, 6)):
                t += rng.choice((0, 0, 1, 7, 40))       # duplicates on purpose
                uid, status = rng.choice(layout.uids), rng.choice((0, 1, 1, 2, 3))
                if status == 0 and uid not in live:     # a robot never idles an idle node
                    status = 1
                (live.add if status else live.discard)(uid)
                records.append((t, uid, status))
            rec.append(records)
            t += 50
            if i % bb_every == 0:
                boards = {"Main": {"n": i, "pose": {"x": i % 4, "y": [1, 2]}}}
                if i % 2:
                    boards["Main"]["flag"] = True       # removed again next time
                if layout.generation == 2:
                    boards["Sub"] = {} if i % 4 else {"goal": "door"}
                rec.add_blackboard(t, boards)
            rec.advance_head(t)
            rec.evict(t)

    a, b = _layout(1, [1, 2, 3, 4, 5]), _layout(2, [1, 2, 3, 4, 5, 6, 7])
    begin(a, bytearray([0, 0, 0, 12, 0, 0]))
    drains(a, 14)
    t += 100
    begin(b, bytearray(b.size))          # a swap: ends segment 0
    drains(b, 9)
    end = t
    rec.end_segment(end)                                # an outage
    t += 900
    rec.add_gap(end, t, "outage")
    begin(b, bytearray([0, 1, 1, 0, 0, 0, 0, 0]))   # same tree: layout shared
    # The arm overlap: records from before the baseline S, which already holds
    # them, replayed on top. An IDLE on an idle node (here 0 and "was
    # SUCCESS") changes nothing.
    rec.append([(t, 3, 0), (t, 4, 2), (t, 4, 0), (t, 4, 0)])
    drains(b, 10)
    streamer.subscribe("late")
    return rec, frames["live"], frames["late"]


def _node(uid, name, children=(), subtree=False):
    node = {"id": f"n{uid}", "uid": uid, "name": name, "children": list(children)}
    if subtree:
        node["is_subtree_root"] = True
    return node


# Two trees with named nodes and a subtree each, for the Log's and the
# Timeline's helpers (which show names).
CROSS = layout(tree={**_node(1, "Sequence", [
    _node(2, "PickLock"), _node(3, "IsDoorOpen"),
    _node(4, "DoorClosed", [_node(5, "tryOpen", [_node(6, "OpenDoor")])], subtree=True)]),
    "root_tree_id": "MainTree"}, uids=[1, 2, 3, 4, 5, 6], generation=1)
PATROL = layout(tree={**_node(1, "ReactiveSequence", [
    _node(2, "VisitWaypoints", [_node(3, "MoveTo"), _node(4, "Dwell")], subtree=True)]),
    "root_tree_id": "PatrolTree"}, uids=[1, 2, 3, 4], generation=2)


def build_named():
    """``(recording, frames)`` on ``CROSS`` and ``PATROL``: a same-tree
    restart, then a tree swap, the oldest chunks evicted; ``frames`` is every
    frame a dashboard subscribed from the start got. Build it under a small
    chunk size."""
    rng = random.Random(8)
    rec = Recording(keep_us=40_000)
    frames = []
    Streamer(rec, lambda clients, f: frames.append(f)).subscribe("x")
    t = T0
    for tree, count in ((CROSS, 40), (CROSS, 15), (PATROL, 20)):
        live = set()
        rec.begin_segment(tree, t, bytes(tree.size))
        for _ in range(count):
            records = []
            for _ in range(rng.randint(1, 5)):
                t += rng.choice((0, 3, 40, 900))
                uid, status = rng.choice(tree.uids), rng.choice((0, 1, 1, 2, 3))
                if status == 0 and uid not in live:
                    status = 1
                (live.add if status else live.discard)(uid)
                records.append((t, uid, status))
            rec.append(records)
            rec.advance_head(t)
            rec.evict(t)
            t += 30
        rec.end_segment(t)
        t += 500
    return rec, frames


# --------------------------------------------------------------------------- #
# Reference definitions for what the Python model has no function for
# --------------------------------------------------------------------------- #
def time_at_seq(segment, seq):
    if seq <= segment.start_seq:
        return segment.t_start
    return next(segment.iter_records(seq - 1, seq))[1]


def intervals(segment, uid, t0, t1):
    t0 = max(t0, segment.t_start)
    if t0 > t1:
        return []
    state = bytearray(segment.state_at(t0))
    out, start, value = [], t0, state[uid]
    for _seq, ts, u, status in segment.iter_records(segment.seq_at_time(t0),
                                                    segment.seq_at_time(t1)):
        if u != uid:
            continue
        apply_transition(state, uid, status)
        if state[uid] != value:
            out.append({"t0": start, "t1": ts, "status": value})
            start, value = ts, state[uid]
    out.append({"t0": start, "t1": t1, "status": value})
    return out


def _head_pos(rec):
    last = rec.segments[-1]
    return {"seg": last.id, "seq": last.head_seq,
            "t": rec.head if rec.head is not None else last.t_begin, "live": True, "mode": "live"}


def _retained(rec, seg_id, seq):
    segment = next((s for s in rec.segments if s.id == seg_id), None)
    if segment is None:
        return rec.segments[0], rec.segments[0].start_seq
    return segment, min(max(seq, segment.start_seq), segment.head_seq)


def cursor_pos(clock, now_ms, rec):
    if not rec.segments:
        return None
    if clock["mode"] == "live":
        return _head_pos(rec)
    start, from_seq = _retained(rec, clock["seg"], clock["seq"])
    # A clock's own t holds where eviction moved nothing; a playing one's always.
    kept = start.id == clock["seg"] and from_seq == clock["seq"]
    t0 = (clock["t"] if "t" in clock and (kept or clock["mode"] == "playing")
          else time_at_seq(start, from_seq))
    if clock["mode"] == "paused":
        return {"seg": start.id, "seq": from_seq, "t": t0, "live": False, "mode": "paused"}
    t = t0 + (now_ms - clock["startMs"]) * 1000
    if rec.head is not None and t >= rec.head:
        return _head_pos(rec)
    i = rec.segments.index(start)
    while i + 1 < len(rec.segments) and rec.segments[i + 1].t_begin <= t:
        i += 1
    segment = rec.segments[i]
    seq = segment.start_seq if t < segment.t_start else segment.seq_at_time(t)
    if segment is start:
        seq = max(seq, from_seq)
    return {"seg": segment.id, "seq": seq, "t": t, "live": False, "mode": "playing"}


def evicted(clock, now_ms, rec):
    """True when eviction dropped the moment a clock shows."""
    if clock["mode"] == "live" or not rec.segments:
        return False
    if clock["mode"] == "paused":
        segment = next((s for s in rec.segments if s.id == clock["seg"]), None)
        return segment is None or clock["seq"] < segment.start_seq
    pos = cursor_pos(clock, now_ms, rec)
    return not pos["live"] and pos["t"] < next(s for s in rec.segments
                                               if s.id == pos["seg"]).t_start


def history_dropped(rec):
    first = rec.segments[0] if rec.segments else None
    return first is not None and (first.id > 0 or first.t_start > first.t_begin)


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
def _arg(frame):
    if isinstance(frame, bytes):
        return {"$base64": base64.b64encode(frame).decode()}
    return json.loads(frame)


def _state(state):
    return None if state is None else {str(i): v for i, v in enumerate(state)}


def _describe(rec):
    segment = rec.segments[-1]
    return {
        "segments": [{"id": s.id, "layoutId": s.layout.generation, "startSeq": s.start_seq,
                      "headSeq": s.head_seq, "tBegin": s.t_begin, "tStart": s.t_start,
                      "tEnd": s.t_end} for s in rec.segments],
        "gaps": [list(g) for g in rec.gaps],
        "head": rec.head,
        "tMin": rec.t_min,
        "stateAtHead": {str(k): v for k, v in decode_state(segment.state,
                                                           segment.layout.uids).items()},
        "backfillDone": True,
    }


def store_cases(name, frames):
    """Cases feeding ``frames`` into a new store saved as ``name``, its
    recording then saved as ``<name>_rec``."""
    cases = [{"name": f"{name}: a store", "call": "KleinRecording.createStore", "save": name}]
    cases += [{"name": f"{name}: ingest frame {i} ({'binary' if isinstance(f, bytes) else json.loads(f)['type']})",
               "call": f"${name}.ingest", "args": [_arg(f)], "expect": True}
              for i, f in enumerate(frames)]
    cases.append({"name": f"{name}: the recording", "get": f"${name}.recording",
                  "save": f"{name}_rec"})
    return cases


def _store_cases(name, frames, rec):
    cases = store_cases(name, frames)
    cases.append({"name": f"{name}: a status frame is not the recording's",
                  "call": f"${name}.ingest", "args": [{"type": "status", "data": {}}],
                  "expect": False})
    cases.append({"name": f"{name}: describe", "call": "KleinRecording.describe",
                  "args": [{"$ref": f"{name}_rec"}], "expect": _describe(rec)})
    cases.append({"name": f"{name}: historyDropped", "call": "KleinRecording.historyDropped",
                  "args": [{"$ref": f"{name}_rec"}], "expect": history_dropped(rec)})
    for s in rec.segments:
        seg = f"{name}_s{s.id}"
        ref = {"$ref": seg}
        cases.append({"name": f"{seg}: get", "call": f"${name}_rec.segment", "args": [s.id],
                      "save": seg})
        # Every seq, one evicted one before them, one past the head.
        for seq in range(max(0, s.start_seq - 1), s.head_seq + 1):
            cases.append({"name": f"{seg}: stateAtSeq {seq}", "call": "KleinRecording.stateAtSeq",
                          "args": [ref, seq], "expect": _state(s.state_at_seq(seq))})
            cases.append({"name": f"{seg}: timeAtSeq {seq}", "call": "KleinRecording.timeAtSeq",
                          "args": [ref, seq], "expect": time_at_seq(s, max(seq, s.start_seq))})
        times = sorted({ts + d for _q, ts, _u, _s in s.iter_records(s.start_seq, s.head_seq)
                        for d in (-1, 0, 1)})
        for t in times:
            cases.append({"name": f"{seg}: seqAtTime {t - T0}", "call": "KleinRecording.seqAtTime",
                          "args": [ref, t], "expect": s.seq_at_time(t)})
        for t in [s.t_start - 1, s.t_start, *times[::5]]:
            expect = None if t < s.t_start else _state(s.state_at(t))
            cases.append({"name": f"{seg}: stateAt {t - T0}", "call": "KleinRecording.stateAt",
                          "args": [ref, t], "expect": expect})
        for a, b in [(s.start_seq, s.head_seq), (s.start_seq + 3, s.start_seq + 11),
                     (s.head_seq - 2, s.head_seq + 5), (0, s.start_seq)]:
            cases.append({"name": f"{seg}: recordsRange {a}..{b}",
                          "call": "KleinRecording.recordsRange", "args": [ref, a, b],
                          "expect": [list(r) for r in s.iter_records(a, b)]})
        end = s.t_end if s.t_end is not None else rec.head
        mid = (s.t_start + end) // 2
        for uid in s.layout.uids[1:4]:
            for t0, t1 in [(s.t_begin, end), (mid, end), (s.t_start + 5, mid), (end, end - 1)]:
                cases.append({"name": f"{seg}: intervals uid {uid} {t0 - T0}..{t1 - T0}",
                              "call": "KleinRecording.intervals", "args": [ref, uid, t0, t1],
                              "expect": intervals(s, uid, t0, t1)})
        track = s.blackboard
        samples = sorted({t for t, *_ in track.changes()} | set(track.boards.values()))
        for t in sorted({t + d for t in samples for d in (-1, 0, 1)} | {end}):
            cases.append({"name": f"{seg}: bbAt {t - T0}", "call": "KleinRecording.bbAt",
                          "args": [ref, t], "expect": track.at(t)})
    return cases


def _cursor_cases(name, rec):
    ref = {"$ref": f"{name}_rec"}
    s0, s1, s2 = rec.segments
    mid0 = (s0.start_seq + s0.head_seq) // 2
    clocks = [
        {"mode": "live"},
        {"mode": "paused", "seg": s0.id, "seq": mid0},
        {"mode": "paused", "seg": s0.id, "seq": 0},                 # evicted: clamped
        {"mode": "paused", "seg": 99, "seq": 3},                    # gone: oldest retained
        {"mode": "paused", "seg": s2.id, "seq": s2.head_seq},
        {"mode": "paused", "seg": s1.id, "seq": s1.start_seq},
        {"mode": "paused", "seg": s0.id, "seq": s0.start_seq},
        {"mode": "playing", "seg": s0.id, "seq": mid0, "startMs": 1000},
        {"mode": "playing", "seg": s1.id, "seq": 2, "startMs": 1000},
        # With their own times: kept, a playing one whose start was evicted
        # (it plays on from its time), and one whose time is evicted too.
        {"mode": "paused", "seg": s0.id, "seq": mid0, "t": time_at_seq(s0, mid0) + 7},
        {"mode": "paused", "seg": s0.id, "seq": 0, "t": s0.t_begin},
        {"mode": "playing", "seg": s0.id, "seq": 0, "startMs": 1000,
         "t": time_at_seq(s0, mid0)},
        {"mode": "playing", "seg": 99, "seq": 3, "startMs": 1000, "t": s0.t_start - 5000},
    ]
    cases = [
        {"name": "cursor: live", "call": "KleinCursor.live", "expect": {"mode": "live"}},
        {"name": "cursor: pause", "call": "KleinCursor.pause", "args": [1, 5],
         "expect": {"mode": "paused", "seg": 1, "seq": 5}},
        {"name": "cursor: play", "call": "KleinCursor.play", "args": [1, 5, 1234.5],
         "expect": {"mode": "playing", "seg": 1, "seq": 5, "startMs": 1234.5}},
    ]
    for i, clock in enumerate(clocks):
        nows = [1000, 1000.2, 1001, 1003, 1010, 1100] if clock["mode"] == "playing" else [0]
        for now in nows:
            cases.append({"name": f"{name} cursor {i}: cursorPos at {now}",
                          "call": "KleinCursor.cursorPos", "args": [clock, now, ref],
                          "expect": cursor_pos(clock, now, rec)})
            cases.append({"name": f"{name} cursor {i}: evicted at {now}",
                          "call": "KleinCursor.evicted", "args": [clock, now, ref],
                          "expect": evicted(clock, now, rec)})
    return cases


# --------------------------------------------------------------------------- #
# The Timeline's window (window.js)
# --------------------------------------------------------------------------- #
def _window(t1, span, follow):
    return {"t0": t1 - span, "t1": t1, "span": span, "follow": follow}


def _window_call(w, name, call, args, expect):
    """``call`` on the window saved as ``w`` (it returns nothing), then the
    window its ``get()`` gives."""
    return [{"name": f"{name}: {call}", "call": f"${w}.{call}", "args": args, "save": f"{w}_"},
            {"name": f"{name}: get after {call}", "call": f"${w}.get", "expect": expect}]


def _paused_at(rec, t):
    """The cursor paused at robot time t, as KleinCursor.pauseAt gives it."""
    segment = [s for s in rec.segments if s.t_begin <= t][-1]
    return {"seg": segment.id, "seq": segment.seq_at_time(t), "t": t, "live": False}


def _window_cases(name, rec):
    """A fresh ``KleinWindow`` over the recording (3 ms kept), one call at a
    time, each expecting the window after it (the steps say what each pins)."""
    ref = {"$ref": f"{name}_rec"}
    head, t_min = rec.head, rec.t_min       # no blackboard sample after the head
    live, at = _head_pos(rec), lambda t: _paused_at(rec, t)
    c = t_min + 1200                        # where the scrubs leave the cursor
    steps = [
        ("live: 30 s, longer than the recording, ends at the head",
         "sync", [ref, live], _window(head, 30_000_000, True)),
        ("1 ms at the head follows", "set", [head, 1000], _window(head, 1000, True)),
        ("panned off the head, it stops following", "set", [head - 500],
         _window(head - 500, 1000, False)),
        ("live, it stays there", "sync", [ref, live], _window(head - 500, 1000, False)),
        ("never before the oldest record", "set", [t_min - 5000],
         _window(t_min + 1000, 1000, False)),
        ("never past the head; back on it, it follows", "set", [head + 5000],
         _window(head, 1000, True)),
        ("a cursor moved outside is centred", "sync", [ref, at(t_min + 1500)],
         _window(t_min + 2000, 1000, True)),
        ("paused, panned off the cursor", "set", [head], _window(head, 1000, False)),
        ("a cursor that stayed is left outside", "sync", [ref, at(t_min + 1500)],
         _window(head, 1000, False)),
        ("a scrub holds it", "hold", [True], _window(head, 1000, False)),
        ("held, a moved cursor is left outside", "sync", [ref, at(c)],
         _window(head, 1000, False)),
        ("the playhead's release leaves it", "hold", [False], _window(head, 1000, False)),
        ("and so does the next sync", "sync", [ref, at(c)], _window(head, 1000, False)),
        ("the overview's release reveals the cursor, centred", "reveal", [],
         _window(c + 500, 1000, False)),
        ("zoomed around an anchor, 200 µs at least", "zoomBy", [-1e4, c + 250],
         _window(c + 300, 200, False)),
        ("panned back over the cursor", "set", [c + 150], _window(c + 150, 200, False)),
        ("+ around the cursor in view", "step", [1], _window(c + 375, 500, False)),
        ("panned off the cursor", "set", [c + 1000], _window(c + 1000, 500, False)),
        ("+ around the middle", "step", [1], _window(c + 1250, 1000, False)),
        ("zoomed around an anchor outside, held at its edge", "zoomBy", [-1e4, t_min - 10**6],
         _window(c + 450, 200, False)),
        ("1 h at most, which ends at the head", "zoomBy", [1e4, c + 500],
         _window(head, 3_600_000_000, False)),
        ("+ at the longest rung", "step", [1], _window(head, 3_600_000_000, False)),
        ("fit: everything kept", "fit", [], _window(head, head - t_min, False)),
        ("back to live, it follows", "sync", [ref, live], _window(head, head - t_min, True)),
        ("live, − keeps the head at the right edge", "step", [-1], _window(head, 2000, True)),
    ]
    w = f"{name}_window"
    cases = [{"name": f"{w}: a fresh one", "call": "KleinWindow.create", "save": w}]
    for i, (what, call, args, expect) in enumerate(steps):
        cases += _window_call(w, f"{w} {i}: {what}", call, args, expect)
    return cases


# --------------------------------------------------------------------------- #
# bbChange, expected from the harness Model (not from klein)
# --------------------------------------------------------------------------- #
SEC = 1_000_000
# (seconds after T0, boards): every sample whole, as the gateway polls them.
# Main/n changes at every sample from 1.5 s (streaming), Sub/goal is removed
# and set again, Main/k removed and set again, Main/late appears mid-way.
BB_SAMPLES = [
    (0.0, {"Main": {"phase": "a", "n": 0, "k": 1}, "Sub": {"goal": "door"}}),
    (0.5, {"Main": {"phase": "a", "n": 0, "k": 1}, "Sub": {"goal": "door"}}),
    (1.0, {"Main": {"phase": "b", "n": 0, "k": 1}, "Sub": {"goal": "door"}}),
    *[(1.5 + i / 2, {"Main": {"phase": "b", "n": i + 1, "k": 1}, "Sub": {"goal": "door"}})
      for i in range(5)],
    (4.0, {"Main": {"phase": "b", "n": 5, "k": 1}, "Sub": {}}),
    (4.5, {"Main": {"phase": "b", "n": 5}, "Sub": {}}),
    (5.0, {"Main": {"phase": "b", "n": 5}, "Sub": {"goal": {"x": 1}}}),
    (5.5, {"Main": {"phase": "b", "n": 5, "k": 2, "late": "new"}, "Sub": {"goal": {"x": 1}}}),
    (6.0, {"Main": {"phase": "c", "n": 5, "k": 2, "late": "new"}, "Sub": {"goal": {"x": 1}}}),
]
BB_KEYS = [("Main", "phase"), ("Main", "n"), ("Main", "k"), ("Main", "late"),
           ("Sub", "goal"), ("Sub", "nope")]


def _bb_change_cases(name, evict_before=None):
    """The BB_SAMPLES recording streamed into a store, then (with
    ``evict_before``, seconds) evicted up to that time; each key's
    ``bbChange`` around every sample, at the 3 s boundaries and at live t
    (the newest change), expected from ``Model.bb_change``."""
    keep = 10 * SEC
    rec = Recording(keep_us=keep)
    frames = []
    Streamer(rec, lambda clients, f: frames.append(f)).subscribe(name)
    tree = _layout(1, [1, 2])
    rec.begin_segment(tree, T0, bytearray(tree.size))
    for dt, boards in BB_SAMPLES:
        rec.add_blackboard(T0 + int(dt * SEC), boards)
        rec.advance_head(T0 + int(dt * SEC))
    if evict_before is not None:
        rec.evict(T0 + int(evict_before * SEC) + keep)
    # The /debug/state blackboard entry, which is all bb_change reads.
    model = Model({"segments": [], "records": [], "blackboard": [
        {"seg": s.id, "t_start": s.blackboard.t_start, "boards": dict(s.blackboard.boards),
         "changes": [list(c) for c in s.blackboard.changes()]} for s in rec.segments]})
    seg = rec.segments[0].id
    cases = store_cases(name, frames)
    cases.append({"name": f"{name}: segment", "call": f"${name}_rec.segment", "args": [seg],
                  "save": f"{name}_seg"})
    cases.append({"name": f"{name}: its track", "get": f"${name}_seg.bb", "save": f"{name}_track"})
    samples = [T0 + int(dt * SEC) for dt, _ in BB_SAMPLES]
    times = sorted({t + d for t in samples for d in (-1, 0, 1)}
                   | {T0 + int(4.5 * SEC) - 1, T0 + int(4.5 * SEC) + 1,  # n's (t - 3 s, t]
                      T0 + 7 * SEC})
    for t in times:
        for board, key in BB_KEYS:
            t_change, recent = model.bb_change(seg, board, key, t)
            expect = {"tChange": t_change, "recent": recent}
            cases.append({"name": f"{name}: bbChange {board}/{key} at {(t - T0) / SEC:+}",
                          "call": "KleinRecording.bbChange",
                          "args": [{"$ref": f"{name}_track"}, board, key, t], "expect": expect})
    return cases


# The status frames' samples without a recording: BB_SAMPLES (a change, a
# removal, a key streaming at 2 Hz), then a new board and a gap of 2.5 s.
BB_FRAMES = BB_SAMPLES + [
    (6.5, {"Main": {"phase": "c", "n": 5, "k": 2, "late": "new"}, "Sub": {"goal": {"x": 1}},
           "Late": {"x": 1}}),
    (7.0, {"Main": {"phase": "c", "n": 5, "k": 2, "late": "new"}, "Sub": {"goal": {"x": 1}},
           "Late": {"x": 2}}),
    (9.5, {"Main": {"phase": "d", "n": 5, "k": 2, "late": "new"}, "Sub": {"goal": {"x": 1}},
           "Late": {"x": 2}}),
]


def _model(track, seg=0):
    """A Model of one Python track, as its /debug/state blackboard entry."""
    return Model({"segments": [], "records": [], "blackboard": [
        {"seg": seg, "t_start": track.t_start, "boards": dict(track.boards),
         "changes": [list(c) for c in track.changes()]}]})


def bb_frame_vectors():
    """BB_FRAMES fed whole into a track by ``addBoards`` and evicted to the
    last BB_RECENT, as the dashboard does without a recording; after each,
    every key's ``bbChange`` and the boards at the newest sample, expected
    from the harness Model of the same samples in a Python track."""
    cases = [{"name": "frames: a track", "call": "KleinRecording.createTrack", "save": "frames"}]
    track = BlackboardTrack()
    for i, (dt, boards) in enumerate(BB_FRAMES):
        t = T0 + int(dt * SEC)
        track.add(t, boards)
        track.evict_before(t - BB_RECENT)
        model = _model(track)
        at = f"at {(t - T0) / SEC:+}"
        cases.append({"name": f"frames {i}: addBoards", "call": "$frames.addBoards",
                      "args": [t, boards], "save": f"frames_{i}"})
        cases.append({"name": f"frames {i}: evictBefore", "call": "$frames.evictBefore",
                      "args": [t - BB_RECENT], "save": f"frames_{i}_evict"})
        cases.append({"name": f"frames: boards {at}", "call": "$frames.at", "args": [t],
                      "expect": model.bb_at(0, t)})
        for board, key in BB_KEYS + [("Late", "x")]:
            t_change, recent = model.bb_change(0, board, key, t)
            cases.append({"name": f"frames: bbChange {board}/{key} {at}",
                          "call": "KleinRecording.bbChange",
                          "args": [{"$ref": "frames"}, board, key, t],
                          "expect": {"tChange": t_change, "recent": recent}})
    return cases


def bb_change_vectors():
    """Whole and after an eviction that cuts between Main/k's removal and
    its return and keeps Sub/goal's set value as the base (not a change)."""
    cases = (_bb_change_cases("bb") + _bb_change_cases("bb_evicted", evict_before=5.2)
             + bb_frame_vectors())
    return {"modules": {"KleinRecording": str(STATIC / "recording.js")}, "cases": cases}


# --------------------------------------------------------------------------- #
# Tree runs and counts, expected from the harness Model
# --------------------------------------------------------------------------- #
def dump(rec):
    """What ``GET /debug/state`` dumps of rec's segments and records: all
    the Model needs for tree runs."""
    return {"segments": [{"id": s.id, "t_begin": s.t_begin, "start_seq": s.start_seq,
                          "head_seq": s.head_seq, "start_state": list(s.state_at_seq(s.start_seq)),
                          "layout_id": s.layout.generation} for s in rec.segments],
            "records": [[ts, uid, status] for s in rec.segments
                        for _seq, ts, uid, status in s.iter_records(s.start_seq, s.head_seq)]}


def _tree_run_cases(name, rec):
    return [{"name": f"{name}: treeRuns", "call": "KleinRecording.treeRuns",
             "args": [{"$ref": f"{name}_rec"}], "expect": Model(dump(rec)).runs()}]


def _count_cases(name, rec):
    """``countIn`` between neighbouring edges at every 7th record time, a µs
    either side, each segment's begin and past both ends, and over wider
    spans across segments, the gap and the evicted start; ``busiestColumn``
    at a few column lengths."""
    ref = {"$ref": f"{name}_rec"}
    model = Model(dump(rec))
    times = sorted({r[0] for _s, records in model.segments.values() for r in records})
    edges = sorted({t + e for t in times[::7] for e in (-1, 0, 1)}
                   | {s.t_begin for s in rec.segments} | {rec.t_min - 1, rec.head + 1})
    cases = [{"name": f"{name}: countIn {len(e)} edges from {e[0] - T0}", "call": "KleinRecording.countIn",
              "args": [ref, e], "expect": [model.count_in(a, b) for a, b in zip(e, e[1:])]}
             for e in (edges, edges[::9], [edges[0], edges[-1]], edges[5:6])]
    cases += [{"name": f"{name}: busiestColumn {d}", "call": "KleinRecording.busiestColumn",
               "args": [ref, d], "expect": model.busiest_column(d)} for d in (1, 7, 40, 1000)]
    return cases


def _growing_cases(d=50):
    """``busiestColumn`` (at one column length, as the overview asks it) and
    two windows, asked again as the recording grows: a drain adding to the
    column the last one ended in, then a new segment after a gap. Live, a
    200 µs window follows the head. Paused, a 30 s one (longer than the
    recording, so at the head) holds still while a scrub lasts and catches
    up with the head after the release."""
    rec, frames, ref = Recording(), [], {"$ref": "grow_rec"}
    Streamer(rec, lambda clients, f: frames.append(f)).subscribe("grow")
    a, b = _layout(1, [1, 2, 3]), _layout(2, [1, 2, 3, 4])
    rec.begin_segment(a, T0, bytearray(a.size))
    steps = [lambda: rec.append([(T0 + 10, 1, 1), (T0 + 10, 2, 1), (T0 + 20, 2, 2)]),
             lambda: rec.append([(T0 + 20, 3, 1), (T0 + 30, 1, 2), (T0 + 200, 1, 1)]),
             lambda: (rec.end_segment(T0 + 300), rec.add_gap(T0 + 300, T0 + 400, "outage"),
                      rec.begin_segment(b, T0 + 400, bytearray(b.size)),
                      rec.append([(T0 + 401, 4, 1), (T0 + 420, 4, 2), (T0 + 449, 1, 1)]))]
    # After each step, given the cursor live and paused at T0 + 10: the
    # windows' calls, [(window, call, args, the window expected)]. The heads
    # are T0 + 20, + 200 and + 449.
    wide = lambda t: _window(t, 30_000_000, True)      # 30 s: longer than the recording
    windows = [
        lambda live, paused: [("following", "sync", [ref, live], wide(T0 + 20)),
                              ("following", "set", [T0 + 20, 200], _window(T0 + 20, 200, True)),
                              ("held", "sync", [ref, paused], wide(T0 + 20)),
                              ("held", "hold", [True], wide(T0 + 20))],
        lambda live, paused: [("following", "sync", [ref, live], _window(T0 + 200, 200, True)),
                              ("held", "sync", [ref, paused], wide(T0 + 20)),
                              ("held", "hold", [False], wide(T0 + 20))],
        lambda live, paused: [("held", "sync", [ref, paused], wide(T0 + 449))]]
    cases = store_cases("grow", frames)
    cases += [{"name": f"grow: a {w} window", "call": "KleinWindow.create", "save": f"grow_{w}"}
              for w in ("following", "held")]
    for i, step in enumerate(steps):
        del frames[:]
        step()
        rec.advance_head(rec.segments[-1].last_ts)
        cases += [{"name": f"grow: ingest {i}.{j}", "call": "$grow.ingest", "args": [_arg(f)],
                   "expect": True} for j, f in enumerate(frames)]
        cases.append({"name": f"grow: busiestColumn {d} after step {i}",
                      "call": "KleinRecording.busiestColumn", "args": [ref, d],
                      "expect": Model(dump(rec)).busiest_column(d)})
        for w, call, args, expect in windows[i](_head_pos(rec), _paused_at(rec, T0 + 10)):
            cases += _window_call(f"grow_{w}", f"grow: the {w} window after step {i}",
                                  call, args, expect)
    return cases


# --------------------------------------------------------------------------- #
# What one render shows (view.js)
# --------------------------------------------------------------------------- #
# The latest frames, for when the recording isn't the source: a tree of their
# own, a status, the boards (None before the first blackboard frame) and
# their track, fed one sample at the gateway's own clock (FRAMES_T).
FRAMES_TREE = {"id": "9:1", "uid": 1, "name": "FromFrames", "children": []}
FRAMES_STATUS = {"1": {"status": "RUNNING", "from": "IDLE"}}
FRAMES_BOARDS = {"Main": {"x": 1}}
FRAMES_T = 42_000_000
# What the blackboard panel says when it lists no board.
NONE_TEXT = "This robot reports no blackboards."
EARLY_TEXT = "No blackboard sample yet at this moment."
DROPPED_TEXT = "Blackboard history from this moment was dropped."
FILE_TEXT = "No blackboard in this file."
WAITING_TEXT = "Waiting for values…"


def _frames(boards=FRAMES_BOARDS):
    return {"tree": FRAMES_TREE, "status": FRAMES_STATUS, "boards": boards,
            "track": {"$ref": "frames_track"}}


def _status(segment, seq):
    return {str(uid): v for uid, v in decode_state(segment.state_at_seq(seq),
                                                   segment.layout.uids).items()}


def _of_recording(rec, clock, now):
    """What a frame shows of ``rec`` at ``clock``: the position (cursor_pos),
    its segment, tree and state."""
    pos = cursor_pos(clock, now, rec)
    segment = next(s for s in rec.segments if s.id == pos["seg"])
    return {"pos": pos, "source": "recording", "seg.id": segment.id,
            "tree.id": segment.layout.tree["id"], "status": _status(segment, pos["seq"])}


# What a frame of the latest frames shows (the cursor aside).
OF_FRAMES = {"source": "frames", "seg": None, "tree": FRAMES_TREE, "status": FRAMES_STATUS,
             "t": FRAMES_T, "mode": "live", "viewingPast": False, "noBoards": False}


def _frame_cases(name, rec, clock, expect, panels=(), now=0, support="on", frames=None):
    """``KleinView.frame`` saved as ``name`` and each path of ``expect`` read
    from it; then ``KleinView.panel`` at it for each ``(wasLive, want)`` in
    ``panels``, ``want`` None (the panel left as it is) or paths to read."""
    frames = frames or _frames()
    args = {"rec": rec, "clock": clock, "nowMs": now, "frames": frames, "support": support}
    cases = [{"name": f"{name}: frame", "call": "KleinView.frame", "args": [args], "save": name}]
    cases += [{"name": f"{name}: {path}", "get": f"${name}.{path}", "expect": value}
              for path, value in expect.items()]
    for was_live, want in panels:
        p = f"{name}_panel_{'live' if was_live else 'past'}"
        call = {"name": p, "call": "KleinView.panel", "args": [{"$ref": name}, frames, was_live]}
        if want is None:
            cases.append({**call, "expect": None})
            continue
        cases.append({**call, "save": p})
        cases += [{"name": f"{p}: {path}", "get": f"${p}.{path}", "expect": value}
                  for path, value in want.items()]
    return cases


def _frame_scenario():
    """A robot's recording streamed into a store a step at a time (before
    and after its backfill, a first segment, the first chunk and blackboard
    evicted, the robot gone, a tree swap), with a frame after each step;
    then an opened file without a sidecar."""
    rec, sent = Recording(keep_us=2_000), []
    Streamer(rec, lambda clients, f: sent.append(f)).subscribe("view")
    ref, at = {"$ref": "view_rec"}, lambda t: T0 + t
    cases = [{"name": "view: a store", "call": "KleinRecording.createStore", "save": "view"},
             {"name": "frames: a track", "call": "KleinRecording.createTrack",
              "save": "frames_track"},
             {"name": "frames: a sample", "call": "$frames_track.addBoards",
              "args": [FRAMES_T, FRAMES_BOARDS], "save": "frames_track_"}]

    def feed(step, count=None):
        out = [{"name": f"view: ingest {step}.{i}", "call": "$view.ingest", "args": [_arg(f)],
                "expect": True} for i, f in enumerate(sent[:count])]
        del sent[:count]
        return out + [{"name": f"view: the recording after {step}", "get": "$view.recording",
                       "save": "view_rec"}]

    live, still = {"mode": "live"}, {"evicted": False, "playing": False}
    # Without a recording, and before its backfill is done: the frames.
    cases += _frame_cases("no recording", None, live, {
        **OF_FRAMES, **still, "clock": live, "pos": None}, panels=[
        (True, {"boards": FRAMES_BOARDS, "opts.t": FRAMES_T, "opts.mode": "live",
                "opts.track.tLast": FRAMES_T, "opts.empty": NONE_TEXT})])
    cases += _frame_cases("no blackboard frame yet", None, live, {"source": "frames"},
                          frames=_frames(boards=None), panels=[
        (True, None), (False, {"boards": {}, "opts.empty": WAITING_TEXT, "opts.mode": "live"})])
    cases += feed("rec", 1)
    cases += _frame_cases("before the backfill", ref, live, {**OF_FRAMES, "pos": None})
    cases += feed("backfill")
    cases += _frame_cases("nothing recorded yet", ref, live, {**OF_FRAMES, "pos": None})

    # A first segment: transitions to +500 µs, blackboard samples at +250,
    # +450 and, after the head, +600.
    a = _layout(1, [1, 2, 3])
    rec.begin_segment(a, T0, bytearray(a.size))
    rec.append([(at(100), 1, 1), (at(100), 2, 1), (at(200), 2, 2), (at(300), 3, 1),
                (at(400), 3, 3), (at(500), 1, 2)])
    rec.add_blackboard(at(250), {"Main": {"n": 1}})
    rec.add_blackboard(at(450), {"Main": {"n": 2}})
    rec.advance_head(at(500))
    rec.add_blackboard(at(600), {"Main": {"n": 3}})
    cases += feed("a segment")
    seg = rec.segments[0]
    cases += _frame_cases("live", ref, live, {
        **_of_recording(rec, live, 0), **still, "clock": live, "t": at(600), "mode": "live",
        "viewingPast": False, "noBoards": False}, panels=[
        (True, {"boards": {"Main": {"n": 3}}, "opts.t": at(600), "opts.mode": "live",
                "opts.track.tLast": at(600), "opts.empty": NONE_TEXT})])
    cases += _frame_cases("live, a robot that can't record", ref, live,
                          {"source": "recording"}, support="unsupported")
    paused = {"mode": "paused", "seg": 0, "seq": 5}
    cases += _frame_cases("paused in the past", ref, paused, {
        **_of_recording(rec, paused, 0), **still, "clock": paused, "t": at(400),
        "mode": "paused", "viewingPast": True, "noBoards": False}, panels=[
        (True, {"boards": seg.blackboard.at(at(400)), "opts.t": at(400),
                "opts.mode": "paused", "opts.empty": NONE_TEXT})])
    early = {"mode": "paused", "seg": 0, "seq": 3}
    cases += _frame_cases("paused before the first sample", ref, early, {
        "t": at(200), "viewingPast": True}, panels=[
        (True, {"boards": {}, "opts.t": at(200), "opts.empty": EARLY_TEXT})])
    playing = {"mode": "playing", "seg": 0, "seq": 3, "startMs": 1000}
    cases += _frame_cases("playing", ref, playing, {
        **_of_recording(rec, playing, 1000.125), "clock": playing, "evicted": False,
        "playing": True, "t": at(325), "mode": "playing", "viewingPast": True}, now=1000.125,
        panels=[(False, {"boards": seg.blackboard.at(at(325)), "opts.mode": "playing"})])
    cases += _frame_cases("playing reaches the head", ref, playing, {
        **_of_recording(rec, live, 0), **still, "clock": live, "t": at(600), "mode": "live",
        "viewingPast": False}, now=1000.5)

    # Eviction: 16 more transitions to +3700 µs and a sample at +3000, then
    # everything before +1700 µs dropped: the first chunk (to seq 8) and the
    # blackboard before it.
    rec.append([(at(700 + 200 * i), 1 + i % 3, 1 + i % 2) for i in range(16)])
    rec.add_blackboard(at(3000), {"Main": {"n": 4}})
    rec.advance_head(at(3700))
    rec.evict(at(3700))
    assert seg.start_seq == 8 and seg.blackboard.t_start == at(1700)
    cases += feed("an eviction")
    clamped = {"mode": "paused", "seg": 0, "seq": 9}     # just after the oldest kept record
    assert evicted(early, 0, rec) and not evicted(clamped, 0, rec)
    cases += _frame_cases("a pause whose moment was evicted", ref, early, {
        **_of_recording(rec, clamped, 0), "clock": clamped, "evicted": True,
        "playing": False, "t": at(1100), "mode": "paused", "viewingPast": True}, panels=[
        (True, {"boards": {}, "opts.empty": DROPPED_TEXT})])
    cases += _frame_cases("then it stays", ref, clamped, {"clock": clamped, "evicted": False})

    # The robot gone: the segment ended. Live, its head stays the source
    # until klein says the robot is back but can't record.
    rec.end_segment(at(3800))
    cases += feed("the robot gone")
    kept = {"mode": "paused", "seg": 0, "seq": 20}
    head = {**_of_recording(rec, live, 0), "t": at(3700), "viewingPast": False}
    cases += _frame_cases("ended, live", ref, live, head)
    cases += _frame_cases("ended, live, back unable to record", ref, live, {
        **OF_FRAMES, **still, "clock": live, "pos": head["pos"]}, support="unsupported",
        panels=[(True, {"boards": FRAMES_BOARDS, "opts.t": FRAMES_T,
                        "opts.track.tLast": FRAMES_T})])
    cases += _frame_cases("ended, paused, back unable to record", ref, kept, {
        **_of_recording(rec, kept, 0), "viewingPast": True}, support="unsupported", panels=[
        (True, {"boards": seg.blackboard.at(at(3300)), "opts.mode": "paused"})])

    # A tree swap: the new segment has no blackboard sample yet.
    b = _layout(2, [1, 2, 3, 4])
    rec.begin_segment(b, at(4000), bytearray(b.size))
    rec.append([(at(4100), 4, 1)])
    rec.advance_head(at(4100))
    cases += feed("a tree swap")
    cases += _frame_cases("a new tree, live", ref, live, {
        **_of_recording(rec, live, 0), "t": at(4100), "viewingPast": False}, panels=[
        (True, None), (False, {"boards": {}, "opts.empty": WAITING_TEXT, "opts.mode": "live"})])

    # An opened file without a .bb.jsonl sidecar.
    opened, sent_file = Recording(), []
    c = _layout(3, [1, 2])
    opened.begin_segment(c, T0, bytearray(c.size))
    opened.append([(at(10), 1, 1), (at(20), 2, 1), (at(30), 2, 2)])
    opened.advance_head(at(30))
    opened.end_segment(at(30))
    Streamer(opened, lambda clients, f: sent_file.append(f), source="file",
             name="alone.btlog").subscribe("file")
    cases += store_cases("opened", sent_file)
    file_ref, end = {"$ref": "opened_rec"}, {"mode": "paused", "seg": 0, "seq": 2}
    for name, clock, past in (("a file without a sidecar, at its end", live, False),
                              ("a file without a sidecar, paused", end, True)):
        cases += _frame_cases(name, file_ref, clock, {
            **_of_recording(opened, clock, 0), "viewingPast": past, "noBoards": True},
            support="file", panels=[(True, {"boards": {}, "opts.empty": FILE_TEXT,
                                            "opts.mode": "paused" if past else "live"})])
    return cases


def frame_vectors():
    """The view's frame and the blackboard panel's input (``KleinView.frame``
    and ``panel``), case by case: which source shows, the clock rewritten,
    the flags and the panel's words by hand from docs/architecture.md,
    "Rendering"; the position, state and boards from the Python model."""
    with mock.patch.object(recording, "CHUNK_SIZE", CHUNK_SIZE):
        cases = _frame_scenario()
    return {"modules": {"KleinRecording": str(STATIC / "recording.js"),
                        "KleinView": str(STATIC / "view.js")},
            "cases": cases}


def vectors():
    with mock.patch.object(recording, "CHUNK_SIZE", CHUNK_SIZE):
        rec, live, late = build()
        cases = (_store_cases("live", live, rec) + _store_cases("late", late, rec)
                 + _cursor_cases("live", rec) + _cursor_cases("late", rec)
                 + _tree_run_cases("live", rec) + _tree_run_cases("late", rec)
                 + _count_cases("live", rec) + _count_cases("late", rec)
                 + _window_cases("live", rec) + _growing_cases())
    return {"modules": {"KleinRecording": str(STATIC / "recording.js"),
                        "KleinCursor": str(STATIC / "cursor.js"),
                        "KleinWindow": str(STATIC / "window.js")},
            "cases": cases}


def render():
    return json.dumps(vectors(), separators=(",", ":"), sort_keys=True) + "\n"


if __name__ == "__main__":
    import sys
    Path(sys.argv[1]).write_text(render())
    print(f"wrote {sys.argv[1]} ({len(vectors()['cases'])} cases)")
