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
rest (``intervals``, ``nextSeq``, ``timeAtSeq``, the cursor) are written out
here in Python from their definitions in docs/architecture.md, "Browser
model".

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
from klein.recording import Recording, apply_transition, decode_state
from klein.streaming import Streamer
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


def next_seq(segment, seq, direction):
    n = seq + direction
    return n if segment.start_seq <= n <= segment.head_seq else None


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
            "t": rec.head if rec.head is not None else last.t_begin, "live": True}


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
        return {"seg": start.id, "seq": from_seq, "t": t0, "live": False}
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
    return {"seg": segment.id, "seq": seq, "t": t, "live": False}


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


def step(clock, rec, direction, now_ms=0):
    pos = cursor_pos(clock, now_ms, rec)
    segment = next(s for s in rec.segments if s.id == pos["seg"])
    seq = next_seq(segment, pos["seq"], direction)
    if seq is not None:
        return {"mode": "paused", "seg": segment.id, "seq": seq}
    i = rec.segments.index(segment) + direction
    if not 0 <= i < len(rec.segments):
        return {"mode": "paused", "seg": segment.id, "seq": pos["seq"]}
    neighbour = rec.segments[i]
    return {"mode": "paused", "seg": neighbour.id,
            "seq": neighbour.start_seq if direction > 0 else neighbour.head_seq}


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
        for seq in (s.start_seq, s.start_seq + 1, s.head_seq - 1, s.head_seq):
            for d in (-1, 1):
                cases.append({"name": f"{seg}: nextSeq {seq} {d:+}", "call": "KleinRecording.nextSeq",
                              "args": [ref, seq, d], "expect": next_seq(s, seq, d)})
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
        for d in (-1, 1):
            cases.append({"name": f"{name} cursor {i}: step {d:+}", "call": "KleinCursor.step",
                          "args": [clock, ref, d, 1001], "expect": step(clock, rec, d, 1001)})
    # Stepping walks consecutive positions from the start to the head.
    clock, walk = {"mode": "paused", "seg": s0.id, "seq": s0.start_seq}, []
    while True:
        walk.append(clock)
        following = step(clock, rec, 1)
        if following == clock:
            break
        clock = following
    for i, clock in enumerate(walk[:-1]):
        cases.append({"name": f"{name} walk {i}", "call": "KleinCursor.step",
                      "args": [clock, ref, 1], "expect": walk[i + 1]})
        cases.append({"name": f"{name} walk back {i}", "call": "KleinCursor.step",
                      "args": [walk[i + 1], ref, -1], "expect": clock})
    return cases


def vectors():
    with mock.patch.object(recording, "CHUNK_SIZE", CHUNK_SIZE):
        rec, live, late = build()
        cases = (_store_cases("live", live, rec) + _store_cases("late", late, rec)
                 + _cursor_cases("live", rec) + _cursor_cases("late", rec))
    return {"module": [str(STATIC / "recording.js"), str(STATIC / "cursor.js")],
            "cases": cases}


def render():
    return json.dumps(vectors(), separators=(",", ":"), sort_keys=True) + "\n"


if __name__ == "__main__":
    import sys
    Path(sys.argv[1]).write_text(render())
    print(f"wrote {sys.argv[1]} ({len(vectors()['cases'])} cases)")
