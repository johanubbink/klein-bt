"""klein's recording read from the outside: the gateway's ``GET /debug/state``
dump, and the dashboard's wording, both written out here from their rules.

``Model(dump)`` re-derives from the dump alone what each segment holds at any
seq or time, the blackboard at a time, a node's intervals and (given each
segment's layout) the Log's rows. The UI tests compare the browser with it,
the integration tests a saved file. Like the rest of the harness it imports
nothing from klein.
"""
import json
import math
import time
from decimal import ROUND_HALF_UP, Decimal

STATUS = ["IDLE", "RUNNING", "SUCCESS", "FAILURE", "SKIPPED"]
IDLE_TRANSITION = 10


def apply(state, uid, status):
    """One recorded transition on a STATUS-encoded state, as klein replays its
    records: IDLE after a live status X is ``10 + X``, and an IDLE on a node
    that is already idle (0 or ``10 + X``: an arm's overlap, replayed)
    changes nothing."""
    if status:
        state[uid] = status
    elif 0 < state[uid] < IDLE_TRANSITION:
        state[uid] += IDLE_TRANSITION


def decode(value):
    """A STATUS byte as ``{"status", "from"}``: ``10 + X`` is IDLE, was X."""
    if value >= IDLE_TRANSITION:
        return {"status": "IDLE", "from": STATUS[value - IDLE_TRANSITION]}
    return {"status": STATUS[value], "from": None}


def card_label(value):
    """A card's status pill for a STATUS byte: "RUNNING", or "was SUCCESS"."""
    return f"was {STATUS[value - IDLE_TRANSITION]}" if value >= IDLE_TRANSITION else STATUS[value]


def names(tree):
    """uid -> (name, subtree) for an unrolled layout: a node belongs to the
    nearest subtree root above it (itself included), else the main tree."""
    out = {}

    def walk(node, subtree):
        if node.get("is_subtree_root"):
            subtree = node["name"]
        out[node["uid"]] = (node["name"], subtree)
        for child in node["children"]:
            walk(child, subtree)
    walk(tree, tree["root_tree_id"])
    return out


# --------------------------------------------------------------------------- #
# The dashboard's wording
# --------------------------------------------------------------------------- #
def fmt_time(t):
    """The Log's time column: local time of day to the µs (a narrow space
    between the ms and the µs digits)."""
    us = f"{t % 1_000_000:06d}"
    return time.strftime("%H:%M:%S", time.localtime(t // 1_000_000)) + f".{us[:3]}\u202f{us[3:]}"


def _to_fixed(x, digits):
    """JS ``x.toFixed(digits)`` for x >= 0: the exact double, ties rounded up
    (Python's format rounds ties to even: 1.25 -> "1.2" where JS says "1.3")."""
    return str(Decimal(x).quantize(Decimal(1).scaleb(-digits), ROUND_HALF_UP))


def fmt_delta(us):
    """The Log's Δ column."""
    if us < 1e3:
        return f"{us} µs"
    if us < 1e4:
        return f"{_to_fixed(us / 1e3, 1)} ms"
    if us < 1e6:
        return f"{math.floor(us / 1e3 + 0.5)} ms"      # JS Math.round: ties up
    if us < 6e7:
        return f"{_to_fixed(us / 1e6, 2 if us < 1e7 else 1)} s"
    return f"{_to_fixed(us / 6e7, 1)} min"


def fmt_span(us):
    """A kept span, as the chip and the dropped-history notes word it: whole
    seconds under a minute, then minutes, then hours and minutes."""
    seconds = us / 1e6
    if seconds < 60:
        return f"{int(seconds)} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h" + (f" {minutes % 60} min" if minutes % 60 else "")


# --------------------------------------------------------------------------- #
# The debug dump
# --------------------------------------------------------------------------- #
class Model:
    """A ``/debug/state`` dump, segment by segment. ``segments`` maps each id
    to ``(segment, its records)``: the dump lists every segment's kept records
    in segment order, ``head_seq - start_seq`` of them each. ``layouts``
    (``{str(id): layout}``) names each segment's nodes, for ``rows``."""

    def __init__(self, dump, layouts=None):
        self.state = dump
        self.segments = {}
        self.names = {}             # id -> {uid: (name, subtree)}, with layouts
        at = 0
        for s in dump["segments"]:
            n = s["head_seq"] - s["start_seq"]
            self.segments[s["id"]] = (s, [tuple(r) for r in dump["records"][at:at + n]])
            at += n
            if layouts is not None:
                self.names[s["id"]] = names(layouts[str(s["id"])])

    def records(self, seg):
        return self.segments[seg][1]

    def record(self, seg, seq):
        s, records = self.segments[seg]
        return records[seq - s["start_seq"]]

    def state_at_seq(self, seg, seq):
        """``Segment.state_at_seq``: the start state with the records before seq applied."""
        s, records = self.segments[seg]
        state = bytearray(s["start_state"])
        for _ts, uid, status in records[:seq - s["start_seq"]]:
            apply(state, uid, status)
        return state

    def seq_at(self, t):
        """``(seg, seq)`` at time t: the latest segment begun by then, with
        every record up to t applied."""
        seg = max(s["id"] for s in self.state["segments"] if s["t_begin"] <= t)
        s, records = self.segments[seg]
        return seg, s["start_seq"] + sum(1 for r in records if r[0] <= t)

    def labels(self, seg, seq, uids):
        """The cards' labels for ``uids`` at (seg, seq)."""
        state = self.state_at_seq(seg, seq)
        return {uid: card_label(state[uid]) for uid in uids}

    def decoded(self, seg, seq, uids):
        """``{uid: {"status", "from"}}`` for ``uids`` (ints or their strings) at (seg, seq)."""
        state = self.state_at_seq(seg, seq)
        return {uid: decode(state[int(uid)]) for uid in uids}

    def rows(self, needle=""):
        """((seg, seq), [time, Δ, subtree, node, from, to], t, uid) of each Log
        row whose node or subtree name holds ``needle``; Δ is to the row above
        as listed, or "new tree" / "resumed" at a segment's first."""
        needle = needle.lower()
        out, prev = [], None
        for seg, (s, records) in self.segments.items():
            table = self.names[seg]
            st = bytearray(s["start_state"])
            for seq, (ts, uid, status) in enumerate(records, s["start_seq"]):
                before = decode(st[uid])["status"]
                apply(st, uid, status)
                name, sub = table[uid]
                if needle and needle not in name.lower() and needle not in sub.lower():
                    continue
                if prev is None:
                    delta = ""
                elif prev[0]["id"] != s["id"]:
                    delta = "new tree" if prev[0]["layout_id"] != s["layout_id"] else "resumed"
                else:
                    delta = fmt_delta(ts - prev[1])
                out.append(((s["id"], seq), [fmt_time(ts), delta, sub, name, before, STATUS[status]],
                            ts, uid))
                prev = (s, ts)
        return out

    def bb_at(self, seg, t):
        """``BlackboardTrack.at``: ``None`` before the segment's first sample,
        else every board seen by t with each key's latest value."""
        e = next(b for b in self.state["blackboard"] if b["seg"] == seg)
        if e["t_start"] is None or t < e["t_start"]:
            return None
        out = {b: {} for b, seen in e["boards"].items() if seen <= t}
        last = {}
        for ts, board, key, text in e["changes"]:
            if ts <= t:
                last[(board, key)] = text
        for (board, key), text in last.items():
            if text is not None:
                out[board][key] = json.loads(text)
        return out

    def intervals(self, seg, uid, t0, t1):
        """``[(start, end, byte)]`` of uid over [t0, t1], each run of one
        state, from the segment's kept records."""
        s, records = self.segments[seg]
        state = bytearray(s["start_state"])
        i = 0
        while i < len(records) and records[i][0] <= t0:
            apply(state, records[i][1], records[i][2])
            i += 1
        out, start, value = [], t0, state[uid]
        for ts, u, status in records[i:]:
            if ts > t1:
                break
            apply(state, u, status)
            if u == uid and state[uid] != value:
                out.append((start, ts, value))
                start, value = ts, state[uid]
        out.append((start, t1, value))
        return out
