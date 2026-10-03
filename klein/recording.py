"""klein.recording — the in-memory recording of a robot's transitions.

Pure data model: no asyncio, no ZeroMQ. The gateway feeds it (arming, ``t``
drains, blackboard polls); everything that shows a past moment reads it back.
See docs/architecture.md, "Recording model".

State is a ``bytearray`` indexed by uid, in the STATUS byte encoding: 0–4 is a
live status, ``10 + X`` is "IDLE after X". Replaying transitions through
``apply_transition`` therefore gives bytes identical to a STATUS reply, and
``decode_state`` gives the ``{uid: {status, from}}`` shape the dashboard paints.

Times are absolute robot µs. A sequence number counts records applied within a
segment, so the state at seq ``n`` is the state after the segment's first
``n`` records.
"""

import array
import bisect
import dataclasses
import json

from .groot2_protocol import IDLE_TRANSITION, NodeStatus, decode_status

CHUNK_SIZE = 1024
_IDLE = int(NodeStatus.IDLE)
_MIB = 1024 * 1024
_RECORD_BYTES = 11          # array storage per record: q (8) + H (2) + B (1)


# --------------------------------------------------------------------------- #
# State encoding
# --------------------------------------------------------------------------- #
def apply_transition(state, uid, status):
    """Apply one live-status transition — the publisher's callback rule
    (groot2_publisher.cpp :: callback): IDLE is stored as ``10 + previous``.

    An IDLE on a node that is already idle (0 or ``10 + X``) changes nothing.
    A robot sends one only after a change klein missed (an overflow, or a
    publisher created mid-tick; the node then keeps its stale value), but
    arming replays the records between ``r start`` and the baseline ``S`` on
    top of that baseline, which already holds them; with this rule the replay
    ends on the baseline's bytes (every other status is idempotent).

    Returns False for such an ignored IDLE, else True."""
    if status == _IDLE:
        current = state[uid]
        if not 0 < current < IDLE_TRANSITION:
            return False
        state[uid] = IDLE_TRANSITION + current
    else:
        state[uid] = status
    return True


def decode_state(state, uids):
    """``{uid: {"status", "from"}}`` for ``uids`` — what ``parse_status`` returns."""
    out = {}
    for uid in uids:
        status, transitioned_from = decode_status(state[uid])
        out[uid] = {"status": status, "from": transitioned_from}
    return out


def equivalent(a, b):
    """True when two states show the same thing. A plain 0 exists only before a
    node's first run, and transitions can never produce it, so it matches any
    ``10 + X``."""
    return len(a) == len(b) and all(
        x == y or (x == 0 and y >= IDLE_TRANSITION) or (y == 0 and x >= IDLE_TRANSITION)
        for x, y in zip(a, b))


def diff_to_transitions(current, target):
    """``[(uid, status)]`` that, applied to ``current``, give a state
    ``equivalent`` to ``target``."""
    out = []
    for uid, (have, want) in enumerate(zip(current, target)):
        if have == want or (want == 0 and have >= IDLE_TRANSITION):
            continue
        if want < IDLE_TRANSITION:
            out.append((uid, want))         # a plain 0 here goes to 10 + have, equivalent
            continue
        live = have if have < IDLE_TRANSITION else 0
        if live != want - IDLE_TRANSITION:
            out.append((uid, want - IDLE_TRANSITION))
        out.append((uid, _IDLE))
    return out


# --------------------------------------------------------------------------- #
# Layouts, segments and chunks
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True, eq=False)
class Layout:
    """One parsed tree XML: its handshake ``generation``, the ``xml``, the
    unrolled ``tree`` sent to dashboards and its sorted ``uids``. Segments
    recorded while the XML is unchanged share one, which is what groups them
    into one run (compared by identity)."""
    generation: int
    xml: str
    tree: dict
    uids: list

    @property
    def size(self):
        """The state's length: max uid + 1."""
        return self.uids[-1] + 1 if self.uids else 1



class Chunk:
    """Up to ``CHUNK_SIZE`` records, plus the state before its first one."""

    __slots__ = ("seq0", "keyframe", "ts", "uid", "status")

    def __init__(self, seq0, keyframe):
        self.seq0 = seq0
        self.keyframe = keyframe
        self.ts = array.array("q")
        self.uid = array.array("H")
        self.status = array.array("B")

    def __len__(self):
        return len(self.ts)

    @property
    def full(self):
        """A full chunk never changes again."""
        return len(self.ts) == CHUNK_SIZE

    def copy(self):
        """The records so far, apart from later appends."""
        chunk = Chunk(self.seq0, self.keyframe)
        chunk.ts, chunk.uid, chunk.status = self.ts[:], self.uid[:], self.status[:]
        return chunk

    @property
    def nbytes(self):
        return _RECORD_BYTES * len(self.ts) + len(self.keyframe)


class Segment:
    """One continuous arm: everything recorded between one ``r start`` and the
    next arm, swap or outage.

    ``layout`` is whatever the caller passes; segments of the same tree may
    share one object. ``t_start`` is the earliest time ``state_at`` can answer:
    ``t_begin``, or once chunks are evicted, the last evicted record's time.
    """

    def __init__(self, id, layout, t_begin, baseline):
        self.id = id
        self.layout = layout
        self.t_begin = t_begin
        self.t_end = None
        self.t_start = t_begin
        self.baseline = bytes(baseline)
        self.state = bytearray(baseline)    # the state at the head
        self.head_seq = 0
        self.last_ts = t_begin              # time of the newest record, or t_begin
        self.chunks = []
        self._chunk_t0 = []                 # each chunk's first ts, for bisect
        self.transition_bytes = 0           # the chunks' nbytes, kept as they change
        self.blackboard = BlackboardTrack()

    @property
    def start_seq(self):
        """The first retained seq (state_at_seq answers from here to head_seq)."""
        return self.chunks[0].seq0 if self.chunks else self.head_seq

    def append(self, records):
        """Append ``(abs_us, uid, status)`` records, in order."""
        state = self.state
        chunk = self.chunks[-1] if self.chunks else None
        for ts, uid, status in records:
            if chunk is None or chunk.full:
                chunk = Chunk(self.head_seq, bytes(state))
                self.chunks.append(chunk)
                self._chunk_t0.append(ts)
                self.transition_bytes += len(chunk.keyframe)
            chunk.ts.append(ts)
            chunk.uid.append(uid)
            chunk.status.append(status)
            apply_transition(state, uid, status)
            self.head_seq += 1
            self.last_ts = ts
            self.transition_bytes += _RECORD_BYTES

    def state_at_seq(self, seq):
        """The state after the first ``seq`` records, or None if evicted."""
        if seq < self.start_seq:
            return None
        if seq == self.head_seq:
            return bytes(self.state)
        chunk = self.chunks[(seq - self.start_seq) // CHUNK_SIZE]
        state = bytearray(chunk.keyframe)
        for i in range(seq - chunk.seq0):
            apply_transition(state, chunk.uid[i], chunk.status[i])
        return bytes(state)

    def seq_at_time(self, t):
        """The number of records with ``ts <= t`` (meaningful for ``t >= t_start``)."""
        i = bisect.bisect_right(self._chunk_t0, t)
        if i == 0:
            return self.start_seq
        chunk = self.chunks[i - 1]
        return chunk.seq0 + bisect.bisect_right(chunk.ts, t)

    def state_at(self, t):
        """The state once every record with ``ts <= t`` applied, or None if evicted."""
        if t < self.t_start:
            return None
        return self.state_at_seq(self.seq_at_time(t))

    def iter_records(self, seq_from, seq_to):
        """Yield ``(seq, ts, uid, status)`` for retained seqs in ``[seq_from, seq_to)``."""
        for chunk in self.chunks:
            lo = max(seq_from, chunk.seq0) - chunk.seq0
            hi = min(seq_to, chunk.seq0 + len(chunk)) - chunk.seq0
            for i in range(lo, hi):
                yield chunk.seq0 + i, chunk.ts[i], chunk.uid[i], chunk.status[i]

    def snapshot(self):
        """A copy that later appends, evictions and blackboard samples leave
        as it is: the full chunks are shared, a partial last one is copied."""
        copy = object.__new__(Segment)
        copy.__dict__.update(self.__dict__)
        copy.chunks = [c if c.full else c.copy() for c in self.chunks]
        copy._chunk_t0 = list(self._chunk_t0)
        copy.state = bytearray(self.state)
        copy.blackboard = self.blackboard.snapshot()
        return copy

    def _drop_first_chunk(self):
        chunk = self.chunks.pop(0)
        del self._chunk_t0[0]
        self.t_start = chunk.ts[-1]
        self.transition_bytes -= chunk.nbytes


# --------------------------------------------------------------------------- #
# Blackboard history
# --------------------------------------------------------------------------- #
class BlackboardTrack:
    """Per-key blackboard changes of one segment.

    Each ``(board, key)`` keeps parallel lists of change times and JSON strings
    (``None`` marks a removal). Unchanged values are not stored again.
    """

    def __init__(self):
        self.t_start = None         # the first sample, moved up by eviction
        self.nbytes = 0
        self._boards = {}           # board -> time first seen, in tree order
        self._current = {}          # board -> {key: json} as of the newest sample
        self._ts = {}               # (board, key) -> [ts]
        self._values = {}           # (board, key) -> [json or None]

    @property
    def boards(self):
        """``{board: time first seen}``, in first-seen order."""
        return self._boards

    def add(self, t, boards):
        """Record one ``{board: {key: value}}`` sample. Returns the changes as
        ``[(board, key, json_or_None)]``."""
        if self.t_start is None:
            self.t_start = t
        changes = []
        for board, entries in boards.items():
            self._boards.setdefault(board, t)
            current = self._current.setdefault(board, {})
            for key, value in entries.items():
                # Compared with sorted keys, stored as sent: the panel shows
                # an object's fields in the robot's order.
                same = json.dumps(value, sort_keys=True)
                if current.get(key) != same:
                    current[key] = same
                    changes.append((board, key, json.dumps(value)))
            for key in [key for key in current if key not in entries]:
                del current[key]
                changes.append((board, key, None))
        for board, key, text in changes:
            self._ts.setdefault((board, key), []).append(t)
            self._values.setdefault((board, key), []).append(text)
            self.nbytes += len(text or "")
        return changes

    def changes(self):
        """Every retained change as ``(t, board, key, json_or_None)``, in time order."""
        return sorted((t, board, key, text)
                      for (board, key), times in self._ts.items()
                      for t, text in zip(times, self._values[(board, key)]))

    def at(self, t):
        """``{board: {key: value}}`` as of time ``t``, or None before ``t_start``."""
        if self.t_start is None or t < self.t_start:
            return None
        out = {board: {} for board, seen in self._boards.items() if seen <= t}
        for (board, key), times in self._ts.items():
            i = bisect.bisect_right(times, t) - 1
            if i >= 0 and self._values[(board, key)][i] is not None:
                out[board][key] = json.loads(self._values[(board, key)][i])
        return out

    def evict_before(self, t):
        """Drop changes older than ``t``, keeping per key the latest one at or
        before ``t`` so ``at`` stays correct from ``t`` on."""
        if self.t_start is None or t <= self.t_start:
            return
        self.t_start = t
        for name in list(self._ts):
            times, values = self._ts[name], self._values[name]
            i = bisect.bisect_right(times, t) - 1
            if i >= 0 and values[i] is None:
                i += 1                      # a removal as the base: the key is just absent
            if i > 0:
                self.nbytes -= sum(len(text or "") for text in values[:i])
                del times[:i], values[:i]
            if not times:
                del self._ts[name], self._values[name]

    def evict_oldest(self):
        """Drop the oldest change that isn't some key's base. False if none is left."""
        nexts = [times[1] for times in self._ts.values() if len(times) > 1]
        if not nexts:
            return False
        self.evict_before(min(nexts))
        return True

    def snapshot(self):
        """A copy that later samples and evictions leave as it is."""
        copy = BlackboardTrack()
        copy.t_start, copy.nbytes = self.t_start, self.nbytes
        copy._boards = dict(self._boards)
        copy._current = {board: dict(entries) for board, entries in self._current.items()}
        copy._ts = {name: list(times) for name, times in self._ts.items()}
        copy._values = {name: list(values) for name, values in self._values.items()}
        return copy

    def clear(self):
        self.__init__()


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
class Recording:
    """Segments in time order, gaps between or inside them, and the limits.

    ``gaps`` are ``(t_from, t_to, kind)`` with kind ``"outage"`` or ``"overflow"``.
    """

    def __init__(self, keep_us=600_000_000, max_bytes=200 * _MIB, bb_max_bytes=64 * _MIB):
        self.keep_us = keep_us
        self.max_bytes = max_bytes
        self.bb_max_bytes = bb_max_bytes
        self.segments = []
        self.gaps = []
        self.head = None
        self._next_id = 0
        # True while a size cap, not the time window, decides how far back
        # that history reaches (shown in the drawer).
        self.transitions_capped = False
        self.blackboard_capped = False
        # Called as listener(event, *args) after each change, for streaming to
        # dashboards (klein/streaming.py). Events: ("segment", segment),
        # ("segment_end", segment), ("append", segment, first_seq), ("head",),
        # ("gap", gap), ("bb", segment, t, changes, new_boards), and ("evict",)
        # when eviction changed the extent().
        self.listener = None

    def _notify(self, *event):
        if self.listener is not None:
            self.listener(*event)

    @property
    def open_segment(self):
        if self.segments and self.segments[-1].t_end is None:
            return self.segments[-1]
        return None

    @property
    def t_min(self):
        """The earliest retained time, or None when nothing is recorded."""
        return self.segments[0].t_start if self.segments else None

    def extent(self):
        """What eviction can change: the earliest time, and per retained segment
        ``[seg, start_seq, t_start, blackboard t_start]``."""
        return (self.t_min,
                [[s.id, s.start_seq, s.t_start, s.blackboard.t_start] for s in self.segments])

    def begin_segment(self, layout, t_begin, baseline):
        if self.open_segment is not None:
            self.end_segment(t_begin)
        segment = Segment(self._next_id, layout, t_begin, baseline)
        self._next_id += 1
        self.segments.append(segment)
        self._notify("segment", segment)
        return segment

    def end_segment(self, t_end):
        segment = self.open_segment
        segment.t_end = t_end
        self._notify("segment_end", segment)

    def append(self, records):
        segment = self.open_segment
        first_seq = segment.head_seq
        segment.append(records)
        if segment.head_seq > first_seq:
            self._notify("append", segment, first_seq)

    def advance_head(self, t):
        self.head = t
        self._notify("head")

    def add_gap(self, t_from, t_to, kind):
        self.gaps.append((t_from, t_to, kind))
        self._notify("gap", self.gaps[-1])

    def add_blackboard(self, t, boards):
        segment = self.open_segment
        track = segment.blackboard
        first = track.t_start is None
        new_boards = [board for board in boards if board not in track.boards]
        changes = track.add(t, boards)
        if first or new_boards or changes:
            self._notify("bb", segment, t, changes, new_boards)
        return changes

    def bytes_used(self):
        """``(transition_bytes, blackboard_bytes)``, an estimate of storage."""
        return (sum(s.transition_bytes for s in self.segments),
                sum(s.blackboard.nbytes for s in self.segments))

    def runs(self):
        """The retained segments grouped into tree runs: maximal consecutive
        segments sharing one layout object (a same-XML restart stays in its
        run, a swap starts a new one). One ``.btlog`` is saved per run."""
        runs = []
        for segment in self.segments:
            if runs and runs[-1][-1].layout is segment.layout:
                runs[-1].append(segment)
            else:
                runs.append([segment])
        return runs

    def evict(self, now):
        """Apply the time window, the total cap and the blackboard sub-cap."""
        before = self.extent()
        cutoff = now - self.keep_us
        while (segment := self._oldest_sealed()) and segment.chunks[0].ts[-1] < cutoff:
            segment._drop_first_chunk()
        for segment in self.segments:
            if segment.t_end is not None and segment.t_end < cutoff:
                segment.blackboard.clear()
            else:
                segment.blackboard.evict_before(cutoff)
        bb_cut = False
        while self.bytes_used()[1] > self.bb_max_bytes and self._evict_oldest_blackboard():
            bb_cut = True

        # Last, so the blackboard is already within its sub-cap and can't push
        # out transitions that fit beside it.
        transitions_cut = False
        total = sum(self.bytes_used())
        while total > self.max_bytes and (segment := self._oldest_sealed()):
            total -= segment.chunks[0].nbytes
            segment._drop_first_chunk()
            transitions_cut = True

        # An ended segment wholly before the window has nothing left to show.
        self.segments = [s for s in self.segments if s.t_end is None or s.t_end >= cutoff]
        self.gaps = [gap for gap in self.gaps if self.t_min is not None and gap[1] >= self.t_min]

        # A cap's cut stays the binding limit until the window's cutoff passes it.
        bb_start = min((s.blackboard.t_start for s in self.segments
                        if s.blackboard.t_start is not None), default=None)
        self.transitions_capped = transitions_cut or (
            self.transitions_capped and self.t_min is not None and self.t_min > cutoff)
        self.blackboard_capped = bb_cut or (
            self.blackboard_capped and bb_start is not None and bb_start > cutoff)
        if self.extent() != before:
            self._notify("evict")

    def _oldest_sealed(self):
        """The segment holding the oldest chunk that can no longer grow, if any."""
        for segment in self.segments:
            if segment.chunks:
                sealed = segment.chunks[0].full or segment.t_end is not None
                return segment if sealed else None
        return None

    def _evict_oldest_blackboard(self):
        for segment in self.segments:
            if segment.blackboard.nbytes == 0:
                continue
            if segment.blackboard.evict_oldest():
                return True
            if segment.t_end is not None:   # an ended segment's bases can go too
                segment.blackboard.clear()
                return True
            return False
        return False


def snapshot_run(run):
    """A tree run (``Recording.runs()``) as segment snapshots, to export
    outside the event loop while the recording goes on."""
    return [segment.snapshot() for segment in run]


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #
class RobotClock:
    """Maps klein's monotonic clock to robot µs, anchored by the ``r start``
    reply: its timestamp is taken to be the midpoint of the round trip."""

    def __init__(self):
        self.start_us = None
        self._mid = None

    def arm(self, start_reply_us, sent_mono, recv_mono):
        self.start_us = start_reply_us
        self._mid = (sent_mono + recv_mono) / 2

    def robot_us(self, mono):
        return self.start_us + round((mono - self._mid) * 1e6)
