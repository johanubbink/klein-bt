"""Streaming the recording to dashboards (klein/streaming.py).

A small Python mirror, written here from docs/protocol.md rather than shared
with klein, applies the frames a client receives; it must end up holding what
the ``Recording`` holds, whether the client watched from the start
(incremental) or joined late (backfill). The browser's own mirror is checked
against the same scenario through the vectors tests/make_vectors.py builds
(see tests/unit/test_js_model.py).
"""
import asyncio
import contextlib
import io
import json
import random
import struct
import unittest
from unittest import mock

from klein import gateway, recording
from klein.recording import BlackboardTrack, Recording
from klein.streaming import Streamer, encode_records
from tests import make_vectors
from tests.helpers import FakeRobot, layout, new_gateway


# --------------------------------------------------------------------------- #
# A client-side mirror, from the documented frame layout
# --------------------------------------------------------------------------- #
def decode_records(frame):
    kind, seg, seq0, first, base, key_len = struct.unpack_from("<BIIIqH", frame)
    at = 23
    keyframe = frame[at:at + key_len]
    at += key_len
    (n,) = struct.unpack_from("<I", frame, at)
    at += 4
    assert len(frame) == at + 9 * n, "frame length"
    records = []
    for i in range(n):
        o = at + 9 * i
        uid, status = struct.unpack_from("<HB", frame, o + 6)
        records.append((base + int.from_bytes(frame[o:o + 6], "little"), uid, status))
    return {"kind": kind, "seg": seg, "seq0": seq0, "first": first,
            "base": base, "keyframe": keyframe, "records": records}


class Mirror:
    """What a dashboard knows after applying ``frames`` in order."""

    def __init__(self, frames):
        self.segments, self.gaps, self.head, self.types = {}, [], None, []
        self.rec = None
        for frame in frames:
            self.feed(frame)

    def feed(self, frame):
        if isinstance(frame, bytes):
            d = decode_records(frame)
            self.types.append(("records", d["kind"]))
            seg = self.segments[d["seg"]]
            assert d["first"] == seg["head_seq"], (d["first"], seg["head_seq"])
            if d["kind"] == 1:
                assert d["seq0"] == d["first"]
                seg["chunks"].append([d["seq0"], d["keyframe"], []])
            else:
                assert d["keyframe"] == b"" and seg["chunks"][-1][0] == d["seq0"]
            seg["chunks"][-1][2].extend(d["records"])
            seg["head_seq"] += len(d["records"])
            return
        msg = json.loads(frame)
        self.types.append(msg["type"])
        kind = msg["type"]
        if kind == "rec":
            self.rec = msg
            self.segments, self.gaps, self.head = {}, [], None
        elif kind == "segment":
            previous = list(self.segments.values())[-1] if self.segments else None
            self.segments[msg["seg"]] = {
                "layout_id": msg["layout_id"], "t_begin": msg["t_begin"], "t_start": msg["t_begin"],
                "t_end": None, "start_seq": msg["start_seq"], "head_seq": msg["start_seq"],
                "layout": msg["layout"] if "layout" in msg else previous["layout"],
                "uids": msg["uids"], "state0": msg["state"], "chunks": [], "bb": BlackboardTrack()}
        elif kind == "segment_end":
            self.segments[msg["seg"]]["t_end"] = msg["t_end"]
        elif kind == "gap":
            self.gaps.append((msg["t_from"], msg["t_to"], msg["kind"]))
        elif kind == "head":
            self.head = msg["t"]
        elif kind == "bb":
            track = self.segments[msg["seg"]]["bb"]
            if track.t_start is None:
                track.t_start = msg["t"]
            for board in msg["boards"]:
                track._boards.setdefault(board, msg["t"])
            for board, key, value in msg["changes"]:
                track._ts.setdefault((board, key), []).append(msg["t"])
                track._values.setdefault((board, key), []).append(json.dumps(value))
            for board, key in msg["removed"]:
                track._ts.setdefault((board, key), []).append(msg["t"])
                track._values.setdefault((board, key), []).append(None)
        elif kind == "evict":
            kept = {s[0]: s for s in msg["segments"]}
            self.segments = {i: s for i, s in self.segments.items() if i in kept}
            for i, seg in self.segments.items():
                _, start_seq, t_start, bb_start = kept[i]
                seg["chunks"] = [c for c in seg["chunks"] if c[0] >= start_seq]
                seg["start_seq"] = seg["chunks"][0][0] if seg["chunks"] else seg["head_seq"]
                assert seg["start_seq"] == start_seq
                seg["t_start"] = t_start
                if bb_start is None:
                    seg["bb"].clear()
                else:
                    seg["bb"].evict_before(bb_start)
            self.gaps = [g for g in self.gaps if msg["t_min"] is not None and g[1] >= msg["t_min"]]


class MirrorChecks:
    def assertMirrors(self, mirror, rec):
        self.assertEqual(list(mirror.segments), [s.id for s in rec.segments])
        self.assertEqual(mirror.gaps, rec.gaps)
        self.assertEqual(mirror.head, rec.head)
        for s in rec.segments:
            m = mirror.segments[s.id]
            self.assertEqual((m["layout_id"], m["t_begin"], m["t_start"], m["t_end"],
                              m["start_seq"], m["head_seq"]),
                             (s.layout.generation, s.t_begin, s.t_start, s.t_end,
                              s.start_seq, s.head_seq), s.id)
            self.assertEqual(m["layout"], s.layout.tree)
            self.assertEqual(m["chunks"], [[c.seq0, c.keyframe, list(zip(c.ts, c.uid, c.status))]
                                           for c in s.chunks])
            # The blackboard answers alike at every sample time and beside it.
            track = s.blackboard
            times = {t for t, *_ in track.changes()} | set(track.boards.values())
            for t in sorted({t + d for t in times for d in (-1, 0, 1)}):
                self.assertEqual(m["bb"].at(t), track.at(t), (s.id, t))


def tiny_layout(generation, uids):
    return layout(tree={"uid": uids[0], "g": generation}, uids=uids, generation=generation)


def capture():
    """``(send, frames)``: a Streamer send function that logs per client."""
    frames = {}

    def send(clients, frame):
        for client in clients:
            frames.setdefault(client, []).append(frame)
    return send, frames


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class RecordsFrameTest(unittest.TestCase):
    def test_records_frames_round_trip(self):
        with self.subTest("chunk start"):
            ts = [1_759_300_000_000_000, 1_759_300_000_000_000, 1_759_300_000_000_007]
            frame = encode_records(1, 70000, 2048, 2048, b"\x00\x01\x0c", ts,
                                   [1, 2, 513], [1, 0, 4])
            self.assertEqual(len(frame), 23 + 3 + 4 + 9 * 3)
            self.assertEqual(decode_records(frame), {
                "kind": 1, "seg": 70000, "seq0": 2048, "first": 2048,
                "base": ts[0], "keyframe": b"\x00\x01\x0c",
                "records": [(ts[0], 1, 1), (ts[1], 2, 0), (ts[2], 513, 4)]})
        with self.subTest("append: no keyframe, offsets use all 48 bits"):
            base = 1_000
            ts = [base, base + 2 ** 48 - 1]
            frame = encode_records(2, 1, 1024, 1500, b"", ts, [65535, 7], [2, 3])
            d = decode_records(frame)
            self.assertEqual((d["kind"], d["seq0"], d["first"], d["keyframe"], d["base"]),
                             (2, 1024, 1500, b"", base))
            self.assertEqual(d["records"], [(ts[0], 65535, 2), (ts[1], 7, 3)])


class IncrementalFramesTest(unittest.TestCase):
    """Each kind of change to the recording, and the frames it produces."""

    def setUp(self):
        patcher = mock.patch.object(recording, "CHUNK_SIZE", 4)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rec = Recording(keep_us=1_000)
        send, self.frames = capture()
        Streamer(self.rec, send).subscribe("c")
        self.layout = tiny_layout(1, [1, 2, 3])

    def new(self):
        """Frames since the last call, as types (records frames as (kind, first seq))."""
        out = []
        for f in self.frames["c"]:
            out.append((decode_records(f)["kind"], decode_records(f)["first"])
                       if isinstance(f, bytes) else json.loads(f))
        self.frames["c"].clear()
        return out

    def test_each_change(self):
        rec, a = self.rec, self.layout
        # The backfill of an empty recording.
        self.assertEqual([f["type"] for f in self.new()], ["rec", "evict", "backfill_done"])
        rec.begin_segment(a, 100, bytearray([0, 0, 1, 12]))
        (seg,) = self.new()
        self.assertEqual((seg["type"], seg["seg"], seg["start_seq"], seg["state"], seg["max_uid"]),
                         ("segment", 0, 0, [0, 0, 1, 12], 3))
        self.assertEqual(seg["layout"], a.tree)

        rec.append([(101, 1, 1), (101, 2, 1)])              # a new chunk
        self.assertEqual(self.new(), [(1, 0)])
        rec.append([(102, 2, 2)])                           # appended to it
        self.assertEqual(self.new(), [(2, 2)])
        rec.append([(103, 2, 0), (104, 3, 1), (105, 3, 2)])  # fills it, then a new one
        self.assertEqual(self.new(), [(2, 3), (1, 4)])
        rec.append([])
        self.assertEqual(self.new(), [])

        rec.advance_head(106)
        self.assertEqual(self.new(), [{"type": "head", "seg": 0, "t": 106,
                                       "bytes": list(rec.bytes_used()), "capped": []}])
        rec.transitions_capped = rec.blackboard_capped = True
        rec.advance_head(106)
        self.assertEqual(self.new()[0]["capped"], ["transitions", "blackboard"])

        rec.add_blackboard(106, {"B": {"k": 1, "gone": 2}, "E": {}})
        rec.add_blackboard(107, {"B": {"k": 1, "gone": 2}, "E": {}})   # unchanged: nothing
        rec.add_blackboard(108, {"B": {"k": [3]}, "E": {}})
        first, second = self.new()
        self.assertEqual((first["changes"], first["removed"], first["boards"]),
                         ([["B", "k", 1], ["B", "gone", 2]], [], ["B", "E"]))
        self.assertEqual((second["t"], second["changes"], second["removed"], second["boards"]),
                         (108, [["B", "k", [3]]], [["B", "gone"]], []))

        rec.add_gap(108, 120, "overflow")
        self.assertEqual(self.new(), [{"type": "gap", "t_from": 108,
                                       "t_to": 120, "kind": "overflow"}])

        rec.begin_segment(a, 120, bytearray(4))             # same tree: no layout again
        end, seg = self.new()
        self.assertEqual((end["type"], end["seg"], end["t_end"]), ("segment_end", 0, 120))
        self.assertEqual((seg["type"], seg["seg"]), ("segment", 1))
        self.assertNotIn("layout", seg)
        rec.begin_segment(tiny_layout(2, [1]), 130, bytearray(2))     # a swap: layout sent
        self.assertIn("layout", self.new()[1])

        rec.evict(140)                                      # nothing old enough yet
        self.assertEqual(self.new(), [])
        rec.evict(1_104)                                    # segment 0's first chunk goes
        (evict,) = self.new()
        self.assertEqual(evict["type"], "evict")
        self.assertEqual(evict["segments"][0][:3], [0, 4, 103])
        self.assertEqual(evict["t_min"], 103)


class MirrorTest(MirrorChecks, unittest.TestCase):
    def test_incremental_and_backfill_mirror_the_recording(self):
        with self.subTest("vector scenario"):
            with mock.patch.object(recording, "CHUNK_SIZE", make_vectors.CHUNK_SIZE):
                rec, live, late = make_vectors.build()
            self.assertGreater(rec.segments[0].start_seq, 0)    # chunks were evicted
            self.assertEqual(len(rec.segments), 3)
            for frames in (live, late):
                mirror = Mirror(frames)
                self.assertMirrors(mirror, rec)
            mirror = Mirror(late)
            done = mirror.types.index("backfill_done")
            self.assertEqual(done, len(late) - 1)
            self.assertEqual(mirror.types[0], "rec")
        with self.subTest("full-size chunks"):
            rng = random.Random(7)
            rec = Recording(keep_us=5_000)
            send, frames = capture()
            streamer = Streamer(rec, send)
            streamer.subscribe("live")
            rec.begin_segment(tiny_layout(1, list(range(1, 30))), 0, bytearray(30))
            t = 0
            for i in range(300):
                batch = []
                for _ in range(rng.randint(0, 25)):
                    t += rng.choice((0, 1, 9))
                    batch.append((t, rng.randint(1, 29), rng.randint(0, 3)))
                rec.append(batch)
                t += 10
                rec.advance_head(t)
                rec.evict(t)
                if i == 150:
                    streamer.subscribe("mid")
            streamer.subscribe("late")
            segment = rec.segments[0]
            self.assertGreater(segment.start_seq, 0)
            self.assertGreater(len(segment.chunks), 1)
            for client in ("live", "mid", "late"):
                self.assertMirrors(Mirror(frames[client]), rec)


class FakeSocket:
    """Just enough of a server connection for ``ws_handler``."""

    def __init__(self):
        self.frames = []
        self.closed = asyncio.Event()

    async def send(self, _message):        # layout/robot frames: not checked here
        await asyncio.sleep(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.closed.wait()
        raise StopAsyncIteration


class BackfillOrderTest(MirrorChecks, unittest.IsolatedAsyncioTestCase):
    async def test_clients_joining_while_drains_arrive_mirror_the_recording(self):
        """Dashboards connect at random moments of a busy run; each one's frames
        must start with a whole backfill and then continue without a gap or a
        repeat, which the mirror checks record by record."""
        gw = new_gateway(self, recording=Recording())
        patch = mock.patch.object(gateway, "POLL_INTERVAL", 0)
        patch.start()
        self.addCleanup(patch.stop)
        gw.streamer.send = lambda clients, frame: [c.frames.append(frame) for c in clients]
        robot = FakeRobot(limit=400)
        robot.drains = [[(10 * i + j, 1 + (i + j) % 13, (i + j) % 4) for j in range(3)]
                        for i in range(300)]
        gw._request = robot
        rng = random.Random(3)
        sockets = []

        async def run():
            await gw.fetch_layout()
            await gw.status_poller()

        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(recording, "CHUNK_SIZE", 16), \
             mock.patch.object(gateway.websockets, "broadcast"):
            task = asyncio.create_task(run())
            handlers = []
            for _ in range(12):
                for _ in range(rng.randint(0, 30)):
                    await asyncio.sleep(0)
                sockets.append(FakeSocket())
                handlers.append(asyncio.create_task(gw.ws_handler(sockets[-1])))
            await asyncio.wait_for(robot.done.wait(), 5)
            task.cancel()
            for sock in sockets:
                sock.closed.set()
            await asyncio.gather(*handlers)
            with contextlib.suppress(asyncio.CancelledError):
                await task
        rec = gw.recording
        self.assertGreater(rec.segments[0].head_seq, 100)
        self.assertGreater(len(rec.segments[0].chunks), 3)
        self.assertEqual(gw.streamer.clients, set())
        joined_at = set()
        for sock in sockets:
            mirror = Mirror(sock.frames)
            self.assertEqual(mirror.types[0], "rec")
            self.assertEqual(mirror.types.count("backfill_done"), 1)
            self.assertEqual(mirror.types.count("rec"), 1)
            self.assertMirrors(mirror, rec)
            # How many chunks its backfill carried: it differs per joining moment.
            joined_at.add(sum(1 for t in mirror.types[:mirror.types.index("backfill_done")]
                              if t == ("records", 1)))
        self.assertGreater(len(joined_at), 3, "clients should join at different moments")


if __name__ == "__main__":
    unittest.main()
