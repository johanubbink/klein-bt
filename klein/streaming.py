"""klein.streaming — the recording, streamed to dashboards over the WebSocket.

The browser keeps a mirror of the ``Recording`` (``static/recording.js``) and
does all viewing from it; this module is what fills the mirror. A dashboard
that connects is sent a **backfill** (the whole retained recording, as the same
messages the live stream uses), then ``backfill_done``, and only then joins the
**incremental** stream. Both happen in one synchronous step, so no incremental
frame can overtake the backfill. See docs/protocol.md, "Streaming the
recording".

The frame encoders are pure. ``Streamer`` is the one stateful piece: the
recording's listener, which turns each change into frames and hands them to a
``send(clients, frame)`` function (``websockets.broadcast`` in the gateway).
"""

import json
import struct

from .groot2_protocol import encode_transition

# Binary records frame (little-endian): kind, segment id, the chunk's first
# seq, this frame's first seq, base µs, keyframe length; then the keyframe, a
# u32 record count and that many 9-byte transition records whose timestamps
# are u48 offsets from base µs.
RECORDS_CHUNK_START = 1     # starts a chunk: carries its keyframe
RECORDS_APPEND = 2          # appends to the segment's current chunk
RECORDS_HEADER_FORMAT = "<BIIIqH"
RECORDS_HEADER_SIZE = struct.calcsize(RECORDS_HEADER_FORMAT)     # 23


# --------------------------------------------------------------------------- #
# Frame encoders (pure)
# --------------------------------------------------------------------------- #
def encode_records(kind, seg, seq0, first_seq, keyframe, ts, uids, statuses):
    """One binary records frame. ``keyframe`` is empty for ``RECORDS_APPEND``."""
    base = min(ts)
    out = bytearray(struct.pack(RECORDS_HEADER_FORMAT, kind, seg, seq0, first_seq,
                                base, len(keyframe)))
    out += keyframe
    out += struct.pack("<I", len(ts))
    for t, uid, status in zip(ts, uids, statuses):
        out += encode_transition(t - base, uid, status)
    return bytes(out)


def records_frames(segment, first_seq):
    """The records frames for ``segment``'s seqs from ``first_seq`` to its head:
    an append to the chunk ``first_seq`` falls in, unless it starts there, then
    one chunk start per later chunk."""
    frames = []
    chunks = segment.chunks
    first = len(chunks)                     # a drain touches the last chunk or two
    while first > 0 and chunks[first - 1].seq0 + len(chunks[first - 1]) > first_seq:
        first -= 1
    for chunk in chunks[first:]:
        lo = max(first_seq, chunk.seq0) - chunk.seq0
        frames.append(encode_records(
            RECORDS_CHUNK_START if lo == 0 else RECORDS_APPEND, segment.id, chunk.seq0,
            chunk.seq0 + lo, chunk.keyframe if lo == 0 else b"",
            chunk.ts[lo:], chunk.uid[lo:], chunk.status[lo:]))
    return frames


def _text(frame_type, **fields):
    return json.dumps({"type": frame_type, **fields})


def rec_frame(source, name, keep_us):
    return _text("rec", source=source, name=name, keep_us=keep_us)


def segment_frame(segment, previous):
    """``state`` is the state at ``start_seq``, the first retained seq: the
    baseline for a new segment. ``layout`` is left out when ``previous``, the
    segment before it, has the same one (the browser then shares that one's)."""
    fields = {"seg": segment.id, "layout_id": segment.layout.generation,
              "t_begin": segment.t_begin, "max_uid": len(segment.baseline) - 1,
              "uids": segment.layout.uids, "start_seq": segment.start_seq,
              "state": list(segment.state_at_seq(segment.start_seq))}
    if previous is None or previous.layout is not segment.layout:
        fields["layout"] = segment.layout.tree
    return _text("segment", **fields)


def segment_end_frame(segment):
    return _text("segment_end", seg=segment.id, t_end=segment.t_end)


def gap_frame(gap):
    t_from, t_to, kind = gap
    return _text("gap", t_from=t_from, t_to=t_to, kind=kind)


def bb_frame(segment, t, changes, boards):
    """``changes`` are ``(board, key, json_or_None)``; None is a removal.
    ``boards`` are the boards first seen at ``t`` (they show even when empty).
    The values' JSON text goes in as it is stored."""
    head = _text("bb", seg=segment.id, t=t,
                 removed=[[b, k] for b, k, v in changes if v is None], boards=list(boards))
    values = ", ".join(f"[{json.dumps(b)}, {json.dumps(k)}, {v}]"
                       for b, k, v in changes if v is not None)
    return f'{head[:-1]}, "changes": [{values}]}}'


def head_frame(recording):
    """Also what the drawer's chip shows: the bytes kept, and which histories
    a size cap has cut short."""
    capped = [name for name, cut in (("transitions", recording.transitions_capped),
                                     ("blackboard", recording.blackboard_capped)) if cut]
    return _text("head", seg=recording.segments[-1].id, t=recording.head,
                 bytes=list(recording.bytes_used()), capped=capped)


def evict_frame(recording):
    t_min, segments = recording.extent()
    return _text("evict", t_min=t_min, segments=segments)


def blackboard_frames(segment):
    """A segment's retained blackboard history as ``bb`` frames, one per sample
    time that changed something (or first saw a board), oldest first."""
    track = segment.blackboard
    if track.t_start is None:
        return []
    by_time = {}
    for t, board, key, text in track.changes():
        by_time.setdefault(t, []).append((board, key, text))
    times = sorted(set(by_time) | set(track.boards.values()) | {track.t_start})
    return [bb_frame(segment, t, by_time.get(t, []),
                     [b for b, seen in track.boards.items() if seen == t])
            for t in times]


def backfill_frames(source, name, recording):
    """Everything a newly connected dashboard needs, in stream order."""
    frames = [rec_frame(source, name, recording.keep_us)]
    frames += [gap_frame(gap) for gap in recording.gaps]
    previous = None
    for segment in recording.segments:
        frames.append(segment_frame(segment, previous))
        frames += blackboard_frames(segment)
        frames += records_frames(segment, segment.start_seq)
        if segment.t_end is not None:
            frames.append(segment_end_frame(segment))
        previous = segment
    if recording.head is not None and recording.segments:
        frames.append(head_frame(recording))
    frames.append(evict_frame(recording))
    frames.append(_text("backfill_done"))
    return frames


# --------------------------------------------------------------------------- #
# Streamer: backfill on subscribe, then every change as it happens
# --------------------------------------------------------------------------- #
class Streamer:
    """Streams one recording to subscribed clients through ``send(clients, frame)``.
    ``source`` is ``"robot"`` or ``"file"`` (``--open``), ``name`` what it shows."""

    def __init__(self, recording, send, source="robot", name=""):
        self.recording = recording
        self.send = send
        self.source, self.name = source, name
        self.clients = set()
        recording.listener = self._on_change

    def subscribe(self, client):
        """Send ``client`` the backfill, then add it to the live stream. No
        await in between, so nothing can be sent to it out of order."""
        for frame in backfill_frames(self.source, self.name, self.recording):
            self.send({client}, frame)
        self.clients.add(client)

    def unsubscribe(self, client):
        self.clients.discard(client)

    def _on_change(self, event, *args):
        if self.clients:
            for frame in self._frames(event, *args):
                self.send(self.clients, frame)

    def _frames(self, event, *args):
        recording = self.recording
        if event == "evict":
            return [evict_frame(recording)]
        if event == "append":
            segment, first_seq = args
            return records_frames(segment, first_seq)
        if event == "head":
            return [head_frame(recording)]
        if event == "segment":
            (segment,) = args
            previous = recording.segments[-2] if len(recording.segments) > 1 else None
            return [segment_frame(segment, previous)]
        if event == "segment_end":
            return [segment_end_frame(args[0])]
        if event == "gap":
            return [gap_frame(args[0])]
        if event == "bb":
            segment, t, changes, new_boards = args
            return [bb_frame(segment, t, changes, new_boards)]
        raise ValueError(f"unknown recording event {event!r}")
