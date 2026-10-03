"""Unit tests for klein.recording — the pure recording model.

Property tests use a seeded RNG and check the model against brute-force
replays, and the publisher rule against the harness's independent reference
(``tests/harness/btlog_ref.py``, which shares no code with klein).
"""
import os
import random
import time
import tracemalloc
import unittest

from klein.recording import (
    CHUNK_SIZE,
    BlackboardTrack,
    Recording,
    RobotClock,
    Segment,
    apply_transition,
    decode_state,
    diff_to_transitions,
    equivalent,
)
from tests.harness import btlog_ref
from tests.helpers import REPORTABLE, STATE_VALUES, random_records

FIXTURES = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")


def brute_states(baseline, records):
    """The state after each prefix of ``records``: ``[state after 0, 1, …, n]``."""
    state = bytearray(baseline)
    out = [bytes(state)]
    for _, uid, status in records:
        apply_transition(state, uid, status)
        out.append(bytes(state))
    return out


def brute_state_at(baseline, records, t):
    return brute_states(baseline, [r for r in records if r[0] <= t])[-1]


class StateEncodingTest(unittest.TestCase):
    def test_apply_transition_matches_the_reference_rule(self):
        with self.subTest("random"):
            # On what a robot sends: never an IDLE to a node that is already idle.
            rng = random.Random(1)
            for _ in range(50):
                state, ref = bytearray(8), {uid: 0 for uid in range(8)}
                for _ in range(200):
                    uid, status = rng.randrange(8), rng.randrange(5)
                    if status == 0 and not 0 < ref[uid] < 10:
                        continue
                    apply_transition(state, uid, status)
                    btlog_ref.apply(ref, uid, status)
                    self.assertEqual(list(state), [ref[uid] for uid in range(8)])
        with self.subTest("t11 fixture"):
            with open(os.path.join(FIXTURES, "t11_filelogger2.btlog"), "rb") as f:
                log = btlog_ref.parse(f.read())
            uids = btlog_ref.tree_uids(log.xml)
            state, ref = bytearray(max(uids) + 1), {uid: 0 for uid in uids}
            seen = set()
            for _, uid, status in log.records:
                apply_transition(state, uid, status)
                btlog_ref.apply(ref, uid, status)
                self.assertEqual({u: state[u] for u in uids}, ref)
                seen.add(state[uid])
            self.assertIn(13, seen)         # PickLock's retries: IDLE after FAILURE
            self.assertEqual(decode_state(state, uids), btlog_ref.decode(ref))

    def test_arming_replays_the_overlap_without_changing_the_state(self):
        # Arming replays records the baseline already holds (see apply_transition).
        for value in (0, 10, 11, 12, 13, 14):
            with self.subTest(f"idle on {value}"):
                state = bytearray([value])
                apply_transition(state, 0, 0)
                self.assertEqual(state[0], value)
        with self.subTest("idle on 2"):
            state = bytearray([2])
            apply_transition(state, 0, 0)
            self.assertEqual(state[0], 12)
        with self.subTest("overlap"):
            # A realistic run of records, applied again on top of the state it
            # produced, ends on that same state.
            rng = random.Random(4)
            for _ in range(500):
                start = bytearray(rng.choice(REPORTABLE) for _ in range(6))
                state, records = bytearray(start), []
                for _ in range(rng.randrange(1, 12)):
                    uid, status = rng.randrange(6), rng.randrange(5)
                    if status == 0 and not 0 < state[uid] < 10:
                        continue
                    apply_transition(state, uid, status)
                    records.append((uid, status))
                baseline, again = bytes(state), bytearray(state)
                for uid, status in records:
                    apply_transition(again, uid, status)
                self.assertEqual(bytes(again), baseline, (start, records))


class DiffTest(unittest.TestCase):
    def test_applying_the_diff_gives_the_target(self):
        with self.subTest("random"):
            rng = random.Random(2)
            for _ in range(2000):
                current = bytes(rng.choice(STATE_VALUES) for _ in range(12))
                # Never "was IDLE" (10): a robot can't report it, transitions can't make it.
                target = bytes(rng.choice(REPORTABLE) for _ in range(12))
                diff = diff_to_transitions(current, target)
                state = bytearray(current)
                for uid, status in diff:
                    apply_transition(state, uid, status)
                self.assertTrue(equivalent(state, target), (current, target, diff))
                # Exact, except where a plain 0 is wanted (transitions can't make one).
                for uid in range(12):
                    if target[uid] != 0:
                        self.assertEqual(state[uid], target[uid])
                self.assertLessEqual(len(diff), 2 * sum(a != b for a, b in zip(current, target)))
        cases = [
            ("equal", [0, 1, 2, 3, 4, 10, 11, 12, 13, 14], [0, 1, 2, 3, 4, 10, 11, 12, 13, 14], []),
            ("equivalent", [12, 10, 0], [0, 0, 0], []),
            ("was x from x", [2], [12], [(0, 0)]),
            ("was x from running", [1], [13], [(0, 3), (0, 0)]),
            ("was x from was y", [11], [13], [(0, 3), (0, 0)]),
        ]
        for label, current, target, expected in cases:
            with self.subTest(label):
                self.assertEqual(diff_to_transitions(bytes(current), bytes(target)), expected)


class SegmentTest(unittest.TestCase):
    def setUp(self):
        rng = random.Random(3)
        self.baseline = bytes(rng.choice(STATE_VALUES) for _ in range(20))
        self.records = random_records(rng, 5000, range(20), idle_on_idle=True)
        self.brute = brute_states(self.baseline, self.records)
        self.rec = Recording()
        self.seg = self.rec.begin_segment("layout", self.records[0][0] - 10, self.baseline)
        for i in range(0, 5000, 337):           # drains of uneven size
            self.rec.append(self.records[i:i + 337])

    def test_lookups_equal_brute_force(self):
        with self.subTest("chunks and keyframes"):
            self.assertEqual(len(self.seg.chunks), 5)
            for chunk in self.seg.chunks:
                self.assertEqual(chunk.keyframe, self.brute[chunk.seq0])
            self.assertEqual([len(c) for c in self.seg.chunks], [CHUNK_SIZE] * 4 + [5000 - 4096])
            self.assertEqual(self.seg.head_seq, 5000)
        with self.subTest("state_at_seq"):
            for seq in range(5001):
                self.assertEqual(self.seg.state_at_seq(seq), self.brute[seq], seq)
        with self.subTest("seq_at_time"):
            # Counts records up to and including t.
            times = [r[0] for r in self.records]
            probes = [times[0] - 5, times[-1] + 5] + times[::23] + [t + 1 for t in times[::41]]
            for t in probes:
                self.assertEqual(self.seg.seq_at_time(t), sum(1 for x in times if x <= t), t)
        with self.subTest("iter_records"):
            got = list(self.seg.iter_records(1000, 1100))
            self.assertEqual(got, [(seq,) + self.records[seq] for seq in range(1000, 1100)])
            self.assertEqual(len(list(self.seg.iter_records(0, 5000))), 5000)

    def test_state_at_seq_after_evicting_the_first_chunks(self):
        now = self.seg.chunks[1].ts[-1] + 1 + self.rec.keep_us
        self.rec.evict(now)
        self.assertEqual(self.seg.start_seq, 2 * CHUNK_SIZE)
        for seq in range(5001):
            expected = self.brute[seq] if seq >= 2 * CHUNK_SIZE else None
            self.assertEqual(self.seg.state_at_seq(seq), expected, seq)
        self.assertEqual(self.rec.t_min, self.seg.t_start)
        self.assertIsNone(self.seg.state_at(self.seg.t_start - 1))
        for t in range(self.seg.t_start, self.records[-1][0] + 2, 97):
            self.assertEqual(self.seg.state_at(t),
                             brute_state_at(self.baseline, self.records, t), t)

    def test_seq_at_time_ties_and_bounds(self):
        seg = Segment(0, None, 0, bytes(3))
        seg.append([(10, 0, 1), (10, 1, 1), (20, 2, 1), (20, 0, 2), (20, 1, 2), (30, 0, 0)])
        self.assertEqual([seg.seq_at_time(t) for t in (5, 10, 15, 20, 29, 30, 99)],
                         [0, 2, 2, 5, 5, 6, 6])
        self.assertEqual(seg.state_at(5), bytes(3))
        self.assertEqual(seg.state_at(20), bytes([2, 2, 1]))
        self.assertEqual(seg.state_at(30), bytes([12, 2, 1]))


class BlackboardTrackTest(unittest.TestCase):
    def test_changes_values_and_lookups(self):
        with self.subTest("changes in time order"):
            track = BlackboardTrack()
            track.add(0, {"Main": {"a": 1, "b": 2}})
            track.add(10, {"Main": {"a": 1, "b": 3}})
            track.add(20, {"Main": {"b": 3}})
            self.assertEqual(track.changes(), [(0, "Main", "a", "1"), (0, "Main", "b", "2"),
                                               (10, "Main", "b", "3"), (20, "Main", "a", None)])
        with self.subTest("unchanged values stored once"):
            track = BlackboardTrack()
            self.assertEqual(track.add(0, {"Main": {"a": 1, "b": {"y": 2, "x": 1}}}),
                             [("Main", "a", "1"), ("Main", "b", '{"y": 2, "x": 1}')])   # as sent
            size = track.nbytes
            self.assertEqual(track.add(5, {"Main": {"b": {"x": 1, "y": 2}, "a": 1}}), [])
            self.assertEqual(track.nbytes, size)
            self.assertEqual(track.add(9, {"Main": {"a": 2, "b": {"x": 1, "y": 2}}}),
                             [("Main", "a", "2")])
        with self.subTest("at: latest change, removed keys dropped"):
            track = BlackboardTrack()
            track.add(10, {"Main": {"a": 1, "b": "x"}, "Sub": {}})
            track.add(20, {"Main": {"a": 2}, "Sub": {}})
            track.add(30, {"Main": {"a": 2, "b": "y"}, "Sub": {"c": [1]}})
            self.assertIsNone(track.at(9))
            self.assertEqual(track.at(10), {"Main": {"a": 1, "b": "x"}, "Sub": {}})
            self.assertEqual(track.at(25), {"Main": {"a": 2}, "Sub": {}})
            self.assertEqual(track.at(99), {"Main": {"a": 2, "b": "y"}, "Sub": {"c": [1]}})

    def test_eviction_keeps_each_keys_base(self):
        track = BlackboardTrack()
        for t in range(10):
            track.add(t, {"Main": {"fixed": "f", "moving": t, "gone": 1} if t < 3 else
                          {"fixed": "f", "moving": t}})
        track.evict_before(6)
        self.assertIsNone(track.at(5))
        self.assertEqual(track.at(6), {"Main": {"fixed": "f", "moving": 6}})
        self.assertEqual(track.at(9), {"Main": {"fixed": "f", "moving": 9}})
        self.assertEqual(track.nbytes, len('"f"') + 4)     # "f" and 6, 7, 8, 9
        self.assertTrue(track.evict_oldest())
        self.assertEqual(track.at(7), {"Main": {"fixed": "f", "moving": 7}})


class EvictionTest(unittest.TestCase):
    def test_time_window_and_total_size(self):
        with self.subTest("time window"):
            rec = Recording(keep_us=1_000_000)
            seg = rec.begin_segment(None, 0, bytes(4))
            rng = random.Random(6)
            records = [(i * 500, rng.randrange(4), rng.randrange(5)) for i in range(10_000)]
            rec.append(records)
            now = records[-1][0]
            rec.evict(now)
            cutoff = now - rec.keep_us
            self.assertLess(seg.t_start, cutoff)        # the whole window is still answerable
            self.assertGreaterEqual(seg.chunks[0].ts[-1], cutoff)
            self.assertEqual(seg.state_at(cutoff), brute_state_at(bytes(4), records, cutoff))
        with self.subTest("total size"):
            rec = Recording(max_bytes=50_000)
            seg = rec.begin_segment(None, 0, bytes(4))
            rec.append([(i, i % 4, 1 + i % 4) for i in range(20_000)])
            rec.evict(20_000)
            self.assertLessEqual(sum(rec.bytes_used()), 50_000)
            self.assertGreater(sum(rec.bytes_used()), 50_000 - CHUNK_SIZE * 11 - 4)
            self.assertEqual(seg.start_seq, seg.chunks[0].seq0)
            self.assertEqual(seg.state_at_seq(seg.start_seq), seg.chunks[0].keyframe)
            self.assertIsNone(seg.state_at_seq(seg.start_seq - 1))

    def test_a_big_blackboard_cannot_push_out_transitions(self):
        with self.subTest("blackboard sub-cap"):
            # A ~70 KB value changing on every 2 Hz poll against a scaled-down sub-cap.
            keep, bb_cap = 60_000_000, 1024 * 1024
            rec = Recording(keep_us=keep, max_bytes=4 * bb_cap, bb_max_bytes=bb_cap)
            seg = rec.begin_segment(None, 0, bytes(8))
            rng = random.Random(7)
            records, values = [], {}
            for poll in range(1200):                    # 120 s of 10 Hz polls
                t = poll * 100_000
                drain = [(t + i, rng.randrange(8), rng.randrange(5)) for i in range(20)]
                rec.append(drain)
                records += drain
                if poll % 5 == 0:                       # 2 Hz blackboard
                    path = {"poses": "x" * 70_000, "poll": poll}
                    rec.add_blackboard(t, {"Main": {"path": path, "mode": "auto"}})
                    values[t] = path
                rec.advance_head(t + 50)
                rec.evict(t + 50)
                transitions, blackboard = rec.bytes_used()
                self.assertLessEqual(blackboard, bb_cap)
                if t > keep:                            # transitions keep their full window
                    self.assertLess(seg.t_start, t + 50 - keep)
            self.assertLess(seg.blackboard.t_start, rec.head - 5_000_000)  # ~14 samples kept
            for t in range(seg.blackboard.t_start, rec.head, 250_000):
                sample = max(s for s in values if s <= t)
                self.assertEqual(seg.blackboard.at(t), {"Main": {"path": values[sample],
                                                                 "mode": "auto"}})
            self.assertIsNone(seg.blackboard.at(seg.blackboard.t_start - 1))
            t = rec.head - keep + 1
            self.assertEqual(seg.state_at(t), brute_state_at(bytes(8), records, t))
        with self.subTest("total cap trims the blackboard first"):
            # Transitions (~2.1 MiB) plus a capped blackboard (1 MiB) fit under the
            # 3 MiB total, so no transition chunk may go however big the blackboard gets.
            mib = 1024 * 1024
            rec = Recording(max_bytes=3 * mib, bb_max_bytes=mib)
            seg = rec.begin_segment(None, 0, bytes(8))
            rec.append([(i, i % 8, 1 + i % 4) for i in range(192 * CHUNK_SIZE)])
            for poll in range(60):
                rec.add_blackboard(poll, {"Main": {"path": "x" * 70_000 + str(poll)}})
                rec.evict(poll)
                transitions, blackboard = rec.bytes_used()
                self.assertLessEqual(blackboard, mib)
                self.assertLessEqual(transitions + blackboard, 3 * mib)
                self.assertEqual((seg.start_seq, len(seg.chunks)), (0, 192))

    def test_capped_says_which_limit_decides_the_history(self):
        """``*_capped``: a size cap cut that history short, until the time
        window's cutoff passes the cut (the drawer's "(size limit)" note)."""
        rec = Recording(keep_us=1_000_000, max_bytes=50_000)
        rec.begin_segment(None, 0, bytes(4))
        rec.append([(i, i % 4, 1 + i % 4) for i in range(20_000)])
        rec.evict(20_000)                           # well inside the window: the size cap cut
        self.assertTrue(rec.transitions_capped)
        self.assertFalse(rec.blackboard_capped)
        cut = rec.t_min
        rec.evict(cut + rec.keep_us - 1)            # the cutoff short of the cut: still the cap
        self.assertTrue(rec.transitions_capped)
        rec.evict(cut + rec.keep_us)                # at it: the window decides again
        self.assertFalse(rec.transitions_capped)

        rec = Recording(keep_us=1_000_000, bb_max_bytes=100_000)
        seg = rec.begin_segment(None, 0, bytes(4))
        for t in range(0, 50):
            rec.add_blackboard(t, {"Main": {"path": "x" * 30_000 + str(t)}})
            rec.evict(t)
        self.assertTrue(rec.blackboard_capped)
        self.assertFalse(rec.transitions_capped)
        rec.evict(seg.blackboard.t_start + rec.keep_us + 1)
        self.assertFalse(rec.blackboard_capped)

        rec = Recording(keep_us=1_000)              # only the window ever binds
        rec.begin_segment(None, 0, bytes(4))
        rec.append([(i, i % 4, 1 + i % 4) for i in range(5_000)])
        rec.add_blackboard(10, {"Main": {"k": 1}})
        rec.evict(5_000)
        self.assertEqual((rec.transitions_capped, rec.blackboard_capped), (False, False))


class SegmentsTest(unittest.TestCase):
    def test_evicted_segments_and_gaps_are_dropped(self):
        with self.subTest("gaps go when every segment does"):
            rec = Recording(keep_us=1000)
            rec.begin_segment(None, 0, bytes(2))
            rec.end_segment(10)
            rec.add_gap(10, 20, "outage")
            rec.evict(2000)
            self.assertEqual((rec.segments, rec.gaps, rec.t_min), ([], [], None))
        with self.subTest("fully evicted ended segments"):
            rec = Recording(keep_us=1000)
            a = rec.begin_segment(None, 0, bytes(2))
            rec.append([(i, 0, 1 + i % 2) for i in range(10)])
            rec.add_blackboard(5, {"Main": {"k": 1}})
            rec.end_segment(10)
            rec.add_gap(10, 20, "outage")
            b = rec.begin_segment(None, 20, bytes(2))
            rec.append([(30, 1, 1)])
            rec.evict(1005)
            self.assertEqual(rec.segments, [a, b])            # a's data still inside the window
            rec.evict(1011)
            self.assertEqual(rec.segments, [b])
            self.assertEqual(rec.gaps, [(10, 20, "outage")])  # still ends after b's start
            rec.evict(1100)
            self.assertEqual(rec.segments, [b])               # the open segment always stays
            self.assertEqual(b.state_at(1100), bytes([0, 1]))

    def test_begin_end_and_gaps(self):
        rec = Recording()
        layout = object()
        a = rec.begin_segment(layout, 100, bytes(2))
        b = rec.begin_segment(layout, 200, bytes(2))      # ends a at 200
        self.assertEqual(a.t_end, 200)
        self.assertIs(rec.open_segment, b)
        rec.end_segment(300)
        rec.add_gap(300, 400, "outage")
        c = rec.begin_segment(layout, 400, bytes(2))
        self.assertEqual(rec.gaps, [(300, 400, "outage")])
        self.assertEqual([s.id for s in rec.segments], [0, 1, 2])
        self.assertIs(a.layout, c.layout)

    def test_runs_group_segments_by_layout(self):
        rec = Recording(keep_us=10_000)
        tree_a, tree_b = object(), object()
        a1 = rec.begin_segment(tree_a, 0, bytes(2))
        rec.append([(i, 0, 1 + i % 2) for i in range(2 * CHUNK_SIZE)])
        a2 = rec.begin_segment(tree_a, 3000, bytes(2))      # restart, same XML
        b = rec.begin_segment(tree_b, 4000, bytes(2))       # swap
        a3 = rec.begin_segment(tree_a, 5000, bytes(2))      # swap back: a new run
        self.assertEqual(rec.runs(), [[a1, a2], [b], [a3]])
        rec.evict(CHUNK_SIZE + 10_000)                      # a1's first chunk goes
        self.assertEqual(len(a1.chunks), 1)
        self.assertEqual(rec.runs(), [[a1, a2], [b], [a3]])
        rec.evict(3001 + 10_000)                            # a1 goes entirely
        self.assertEqual(rec.runs(), [[a2], [b], [a3]])


class RobotClockTest(unittest.TestCase):
    def test_maps_to_the_round_trip_midpoint(self):
        clock = RobotClock()
        clock.arm(1_000_000, 10.0, 10.002)
        self.assertEqual(clock.robot_us(10.001), 1_000_000)
        self.assertEqual(clock.robot_us(11.501), 2_500_000)


class BoundsTest(unittest.TestCase):
    def test_a_million_transitions(self):
        rng = random.Random(8)
        uids = [rng.randrange(40) for _ in range(4096)]
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        rec = Recording()
        seg = rec.begin_segment(None, 0, bytes(40))
        for start in range(0, 1_000_000, 1000):
            rec.append([(start + i, uids[i % 4096], 1 + i % 4) for i in range(1000)])
        traced = tracemalloc.get_traced_memory()[0] - before
        tracemalloc.stop()
        transitions = rec.bytes_used()[0]
        self.assertLess(transitions, 15_000_000)
        self.assertLess(traced, 20_000_000)

        times = [rng.randrange(1_000_000) for _ in range(1000)]
        start = time.perf_counter()
        for t in times:
            seg.state_at(t)
        per_call = (time.perf_counter() - start) / len(times)
        self.assertLess(per_call, 0.001)


if __name__ == "__main__":
    unittest.main()
