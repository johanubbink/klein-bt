"""Unit tests for saving a recording: ``klein.btlog.export_run`` and
``export_blackboard``, and the gateway's ``/log`` routes.

Every exported file is read back and replayed from all-IDLE with the harness's
independent reference (``tests/harness/btlog_ref.py``, which shares no code
with klein), and compared with the recording it came from.
"""
import asyncio
import io
import json
import os
import random
import time
import types
import unittest
import unittest.mock
import zipfile

from klein.btlog import export_blackboard, export_run, read_btlog
from klein.recording import CHUNK_SIZE, Recording
from tests.harness import btlog_ref
from tests.helpers import (REPORTABLE, answer, equivalent, layout, new_gateway, random_records,
                           read_sidecar, sidecar_at)

FIXTURES = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
T0 = 1_760_000_000_000_000                  # µs since the epoch


def tree_layout(tree_id, n_uids):
    """A layout as the gateway makes one: a Sequence of ``n_uids - 1`` actions."""
    nodes = "".join(f'<Action _uid="{uid}"/>' for uid in range(2, n_uids + 1))
    xml = (f'<root main_tree_to_execute="{tree_id}"><BehaviorTree ID="{tree_id}">'
           f'<Sequence _uid="1">{nodes}</Sequence></BehaviorTree></root>')
    return layout(tree={"root_tree_id": tree_id}, uids=range(1, n_uids + 1), xml=xml)


def robot_records(rng, n, n_uids, t0):
    """``n`` records on uids 1..n_uids, as a robot sends them."""
    return random_records(rng, n, range(1, n_uids + 1), t0)


def random_state(rng, size):
    return bytes([0] + [rng.choice(REPORTABLE) for _ in range(size - 1)])


def recorded_state(run, t):
    """What the recording shows at ``t``: the latest segment begun by then (in a
    gap, the previous segment's last state)."""
    return [s for s in run if s.t_start <= t][-1].state_at(t)


class ExportRunTest(unittest.TestCase):
    def assert_replays_the_recording(self, run, data):
        """Replaying ``data`` from all-IDLE equals the recording at every record
        time and at every midpoint between two."""
        log = btlog_ref.parse(data)
        self.assertEqual(log.first_timestamp_us, run[0].t_start)
        self.assertEqual(log.xml, run[0].layout.xml)
        uids = btlog_ref.tree_uids(log.xml)
        states = btlog_ref.states_at_record_times(btlog_ref.absolute(log), uids)
        times = sorted(states)
        self.assertEqual(times[0], run[0].t_start)
        checks = [(t, states[t]) for t in times]
        checks += [((a + b) // 2, states[a]) for a, b in zip(times, times[1:]) if b - a > 1]
        for t, replayed in checks:
            expected = recorded_state(run, t)
            for uid in uids:
                self.assertTrue(equivalent(replayed[uid], expected[uid]),
                                (t - run[0].t_start, uid, replayed[uid], expected[uid]))
        return log

    def test_a_fresh_tree_exports_its_records_unchanged(self):
        with self.subTest("random"):
            rng = random.Random(1)
            tree = tree_layout("Main", 9)
            rec = Recording()
            seg = rec.begin_segment(tree, T0, bytes(tree.size))
            records = robot_records(rng, 3000, 9, T0)
            rec.append(records)
            data = export_run(rec.runs()[0], tree.xml)
            expected = btlog_ref.build(tree.xml, T0, [(t - T0, u, s) for t, u, s in records])
            self.assertEqual(data, expected)
            self.assert_replays_the_recording([seg], data)
        with self.subTest("t11 fixture"):
            with open(os.path.join(FIXTURES, "t11_filelogger2.btlog"), "rb") as f:
                fixture = f.read()
            log = read_btlog(fixture)
            tree = layout(tree={"root_tree_id": "MainTree"}, uids=btlog_ref.tree_uids(log.xml),
                          xml=log.xml)
            rec = Recording()
            rec.begin_segment(tree, log.first_timestamp, bytes(tree.size))
            rec.append([(log.first_timestamp + t, u, s) for t, u, s in log.records])
            self.assertEqual(export_run(rec.runs()[0], log.xml), fixture)

    def test_a_baseline_becomes_a_prefix_at_offset_0(self):
        with self.subTest("snapshot"):
            rng = random.Random(2)
            tree = tree_layout("Main", 9)
            rec = Recording()
            rec.begin_segment(tree, T0, random_state(rng, tree.size))
            rec.append(robot_records(rng, 500, 9, T0 + 40))
            log = self.assert_replays_the_recording(rec.runs()[0],
                                                    export_run(rec.runs()[0], tree.xml))
            self.assertEqual(log.records[0][0], 0)
            self.assertGreater(len(log.records), 500)
        with self.subTest("after eviction: the retained start"):
            rng = random.Random(3)
            tree = tree_layout("Main", 9)
            rec = Recording(keep_us=200_000)
            seg = rec.begin_segment(tree, T0, random_state(rng, tree.size))
            records = robot_records(rng, 3 * CHUNK_SIZE + 100, 9, T0)
            rec.append(records)
            rec.evict(seg.chunks[1].ts[-1] + 1 + rec.keep_us)
            self.assertEqual(seg.start_seq, 2 * CHUNK_SIZE)
            self.assertGreater(seg.t_start, seg.t_begin)
            data = export_run(rec.runs()[0], tree.xml)
            log = self.assert_replays_the_recording([seg], data)
            prefix = [r for r in log.records if r[0] == 0]
            self.assertGreater(len(prefix), 0)
            self.assertEqual(len(log.records) - len(prefix),
                             sum(1 for r in records[2 * CHUNK_SIZE:] if r[0] > seg.t_start))

    def test_arm_overlap_idles_are_left_out_of_the_file(self):
        # The first drain after arming replays what the baseline S already
        # held: here uid 3 (baseline "was SUCCESS") gets its IDLE again, and
        # uid 5 (never ran) an IDLE from before. klein ignores both; the file
        # must not carry them, or a plain reader shows "was IDLE".
        tree = tree_layout("Main", 6)
        baseline = bytes([0, 1, 1, 12, 2, 0, 0])
        rec = Recording()
        seg = rec.begin_segment(tree, T0, baseline)
        rec.append([(T0 + 3, 3, 0), (T0 + 3, 5, 0), (T0 + 10, 4, 0), (T0 + 20, 3, 1),
                    (T0 + 30, 3, 3), (T0 + 30, 3, 0), (T0 + 45, 6, 1)])
        data = export_run(rec.runs()[0], tree.xml)
        log = btlog_ref.parse(data)
        replayed, idle_on_idle = {uid: 0 for uid in range(1, 7)}, []
        for offset, uid, status in log.records:
            if status == 0 and not 0 < replayed[uid] < 10:
                idle_on_idle.append((offset, uid))
            btlog_ref.apply(replayed, uid, status)
        self.assertEqual(idle_on_idle, [])
        states = btlog_ref.states_at_record_times(btlog_ref.absolute(log), range(1, 7))
        self.assertGreater(len(states), 4)
        for t, state in states.items():
            self.assertEqual([state[uid] for uid in range(1, 7)], list(seg.state_at(t))[1:],
                             t - T0)

    def test_an_outage_and_an_overflow_rearm_stay_in_one_file(self):
        rng = random.Random(4)
        tree = tree_layout("Main", 9)
        rec = Recording()
        a = rec.begin_segment(tree, T0, random_state(rng, tree.size))
        rec.append(robot_records(rng, 800, 9, T0 + 10))
        rec.end_segment(a.last_ts + 50_000)
        rec.add_gap(a.t_end, a.t_end + 3_000_000, "outage")
        b = rec.begin_segment(tree, a.t_end + 3_000_000, random_state(rng, tree.size))
        rec.append(robot_records(rng, 1000, 9, b.t_begin + 5))
        rec.add_gap(b.last_ts, b.last_ts + 10, "overflow")
        c = rec.begin_segment(tree, b.last_ts + 400, random_state(rng, tree.size))
        rec.append(robot_records(rng, 300, 9, c.t_begin + 5))
        self.assertEqual(rec.runs(), [[a, b, c]])
        log = self.assert_replays_the_recording([a, b, c], export_run([a, b, c], tree.xml))
        # Each re-arm opens with the transitions from the previous end to its baseline.
        for previous, segment in ((a, b), (b, c)):
            offset = segment.t_begin - a.t_begin
            boundary = [r for r in log.records if r[0] == offset]
            changed = sum(not equivalent(x, y) for x, y in zip(previous.state, segment.baseline))
            self.assertGreaterEqual(len(boundary), changed)
            self.assertGreater(changed, 0)

    def test_a_swap_and_a_same_xml_restart_give_two_files(self):
        rng = random.Random(5)
        tree_a, tree_b = tree_layout("TreeA", 9), tree_layout("TreeB", 5)
        rec = Recording()
        a = rec.begin_segment(tree_a, T0, random_state(rng, tree_a.size))
        rec.append(robot_records(rng, 400, 9, T0 + 3))
        b1 = rec.begin_segment(tree_b, a.last_ts + 100, random_state(rng, tree_b.size))
        rec.append(robot_records(rng, 300, 5, b1.t_begin + 3))
        rec.end_segment(b1.last_ts + 100)
        rec.add_gap(b1.t_end, b1.t_end + 2_500_000, "outage")
        b2 = rec.begin_segment(tree_b, b1.t_end + 2_500_000, random_state(rng, tree_b.size))
        rec.append(robot_records(rng, 300, 5, b2.t_begin + 3))
        runs = rec.runs()
        self.assertEqual(runs, [[a], [b1, b2]])
        file_a = self.assert_replays_the_recording(runs[0], export_run(runs[0], tree_a.xml))
        file_b = self.assert_replays_the_recording(runs[1], export_run(runs[1], tree_b.xml))
        self.assertEqual(file_a.xml, tree_a.xml)
        self.assertEqual(file_b.xml, tree_b.xml)
        # Only its own transitions: A's file ends with A, B's begins at B.
        self.assertLessEqual(btlog_ref.absolute(file_a)[-1][0], a.last_ts)
        self.assertEqual(file_b.first_timestamp_us, b1.t_begin)
        starts = (b1.t_begin, b2.t_begin)          # where the prefix and the restart go
        self.assertEqual([r for r in btlog_ref.absolute(file_b) if r[0] not in starts],
                         [(ts, uid, status) for s in (b1, b2)
                          for _seq, ts, uid, status in s.iter_records(0, s.head_seq)])
        self.assertTrue(all(u <= 5 for _t, u, _s in file_b.records))


class ExportBlackboardTest(unittest.TestCase):
    def assert_sidecar_matches(self, run, tree_id="Main"):
        """At every sample time (and between), the sidecar's latest values equal
        that segment's ``BlackboardTrack.at``."""
        header, lines = read_sidecar(export_blackboard(run, tree_id))
        first = run[0].t_start
        self.assertEqual(header, {"klein_blackboard": 1, "first_timestamp": first,
                                  "tree_id": tree_id})
        self.assertEqual([line["t"] for line in lines], sorted(line["t"] for line in lines))
        self.assertGreaterEqual(lines[0]["t"], 0)
        checked = 0
        for segment in run:
            track = segment.blackboard
            times = sorted({t for t, *_ in track.changes()} | {track.t_start})
            for t in times + [t + 1 for t in times]:
                if t < max(track.t_start, first):
                    continue
                expected = {b: keys for b, keys in track.at(t).items() if keys}
                self.assertEqual(sidecar_at(lines, t - first), expected, t - first)
                checked += 1
        self.assertGreater(checked, 0)
        return lines

    def sample(self, rec, t, i):
        boards = {"Main": {"n": i, "pose": {"x": i / 4, "y": [i, "a"]}, "mode": "auto"},
                  "Sub::3": {"flag": i % 3 == 0}}
        if i % 4 == 1:
            boards["Main"]["blink"] = "on"          # appears, then is removed
        rec.add_blackboard(t, boards)

    def test_the_sidecar_matches_the_track(self):
        with self.subTest("one segment"):
            rec = Recording()
            rec.begin_segment(tree_layout("Main", 3), T0, bytes(4))
            for i in range(20):
                self.sample(rec, T0 + 1000 + i * 500_000, i)
            lines = self.assert_sidecar_matches(rec.runs()[0])
            self.assertTrue(any(line.get("removed") for line in lines))
        with self.subTest("after eviction: the values at the start are at offset 0"):
            tree = tree_layout("Main", 3)
            rec = Recording(keep_us=4_000_000)
            seg = rec.begin_segment(tree, T0, bytes(4))
            for i in range(20):
                t = T0 + i * 500_000
                rec.append([(t, 1 + i % 3, 1 + i % 4)])
                self.sample(rec, t + 10, i)
            rec.evict(T0 + 19 * 500_000 + 10)
            self.assertGreater(seg.blackboard.t_start, T0)
            lines = self.assert_sidecar_matches(rec.runs()[0])
            start = seg.blackboard.t_start - seg.t_start
            self.assertEqual({(l["board"], l["key"]) for l in lines if l["t"] == start},
                             {("Main", "n"), ("Main", "pose"), ("Main", "mode"),
                              ("Sub::3", "flag")})
        with self.subTest("transitions evicted past the first sample"):
            tree = tree_layout("Main", 3)
            rec = Recording()
            seg = rec.begin_segment(tree, T0, bytes(4))
            rec.append([(T0 + i, 1 + i % 3, 1 + i % 4) for i in range(2 * CHUNK_SIZE)])
            for i in range(5):
                self.sample(rec, T0 + i * 300, i)
            rec.max_bytes = sum(rec.bytes_used()) - 1          # one chunk has to go
            rec.evict(T0 + 2 * CHUNK_SIZE)
            self.assertGreater(seg.t_start, seg.blackboard.t_start)
            lines = self.assert_sidecar_matches(rec.runs()[0])
            self.assertEqual(lines[0]["t"], 0)

    def test_a_restart_replaces_the_previous_segments_values(self):
        tree = tree_layout("Main", 3)
        rec = Recording()
        rec.begin_segment(tree, T0, bytes(4))
        for i in range(6):
            self.sample(rec, T0 + i * 500_000, i)
        rec.add_blackboard(T0 + 3_000_000, {"Main": {"only_before": 1}, "Sub::3": {}})
        b = rec.begin_segment(tree, T0 + 4_000_000, bytes(4))
        for i in range(6):
            self.sample(rec, b.t_begin + 100 + i * 500_000, 10 + i)
        lines = self.assert_sidecar_matches(rec.runs()[0])
        self.assertIn({"t": b.t_begin + 100 - T0, "board": "Main", "key": "only_before",
                       "removed": True}, lines)


class LogRoutesTest(unittest.TestCase):
    def setUp(self):
        self.gw = new_gateway(self, recording=Recording())
        rec = self.gw.recording
        self.tree_a, self.tree_b = tree_layout("TreeA", 4), tree_layout("TreeB", 3)
        rec.begin_segment(self.tree_a, T0, bytes(5))
        rec.append([(T0 + 10, 1, 1), (T0 + 20, 2, 2)])
        rec.add_blackboard(T0 + 15, {"TreeA": {"x": 1}})
        rec.begin_segment(self.tree_b, T0 + 1_000_000, bytes(4))
        rec.append([(T0 + 1_000_010, 1, 1)])

    def request(self, path):
        return answer(self.gw, path)

    def test_runs_btlog_and_sidecar_downloads(self):
        runs = self.request("/log/runs")
        filenames = [run["filename"] for run in json.loads(runs.body)]
        with self.subTest("/log/runs"):
            self.assertEqual(runs.status_code, 200)
            self.assertEqual(runs.headers["Content-Type"], "application/json")
            def stamp(us):                              # the gateway machine's local time
                return time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(us / 1e6))
            self.assertEqual(json.loads(runs.body), [
                {"run": 0, "tree_id": "TreeA", "t_begin": T0, "t_end": T0 + 1_000_000,
                 "filename": f"TreeA_{stamp(T0)}.btlog", "blackboard": True},
                {"run": 1, "tree_id": "TreeB", "t_begin": T0 + 1_000_000, "t_end": None,
                 "filename": f"TreeB_{stamp(T0 + 1_000_000)}.btlog", "blackboard": False},
            ])
        with self.subTest("/log.btlog"):
            response = self.request("/log.btlog?run=1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
            self.assertEqual(response.headers["Content-Disposition"],
                             f'attachment; filename="{filenames[1]}"')
            self.assertEqual(response.headers["Content-Length"], str(len(response.body)))
            self.assertEqual(response.body,
                             export_run(self.gw.recording.runs()[1], self.tree_b.xml))
            log = btlog_ref.parse(response.body)
            self.assertEqual((log.xml, log.records), (self.tree_b.xml, [(10, 1, 1)]))
        with self.subTest("/log.bb.jsonl"):
            response = self.request("/log.bb.jsonl?run=0")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Content-Disposition"],
                             f'attachment; filename="{filenames[0][:-len(".btlog")]}.bb.jsonl"')
            self.assertEqual(read_sidecar(response.body),
                             ({"klein_blackboard": 1, "first_timestamp": T0, "tree_id": "TreeA"},
                              [{"t": 15, "board": "TreeA", "key": "x", "value": 1}]))

    def test_zip_compresses_off_the_event_loop(self):
        """The files are exported at once, from one state; the compression,
        slowed here to 0.3 s, runs in a thread while the event loop goes on."""
        real = zipfile.ZipFile.writestr

        def slow(archive, *args, **kwargs):
            time.sleep(0.1)
            return real(archive, *args, **kwargs)

        async def save_while_ticking():
            ticks = 0

            async def tick():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1
            ticker = asyncio.create_task(tick())
            pending = self.gw._process_request(None, types.SimpleNamespace(path="/log.zip"))
            self.gw.recording.append([(T0 + 1_000_020, 2, 1)])   # after the export: not in it
            response = await pending
            ticker.cancel()
            return response, ticks

        with unittest.mock.patch.object(zipfile.ZipFile, "writestr", slow):
            response, ticks = asyncio.run(save_while_ticking())
        self.assertGreater(ticks, 10, "the event loop ran while the zip was compressed")
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            self.assertEqual(len(archive.namelist()), 3)
            btlog = next(n for n in archive.namelist() if n.startswith("TreeB"))
            self.assertEqual(len(read_btlog(archive.read(btlog)).records), 1)

    def test_zip_holds_every_run_equal_to_its_routes(self):
        """One .btlog per run, and a .bb.jsonl only for a run with a blackboard."""
        before = time.strftime("klein_%Y-%m-%d_%H-%M-%S.zip")
        response = self.request("/log.zip")
        after = time.strftime("klein_%Y-%m-%d_%H-%M-%S.zip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/zip")
        self.assertIn(response.headers["Content-Disposition"],
                      {f'attachment; filename="{before}"', f'attachment; filename="{after}"'})
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        want = {}
        for kind, run in (("btlog", 0), ("bb.jsonl", 0), ("btlog", 1)):
            route = self.request(f"/log.{kind}?run={run}")
            name = route.headers["Content-Disposition"].split('"')[1]
            want[name] = route.body
        self.assertEqual(entries, want)
        stems = {name.split(".")[0] for name in entries}
        self.assertEqual(len(stems), 2)                 # each sidecar pairs with its .btlog

    def test_runs_of_one_tree_in_the_same_second_get_unique_names(self):
        """A, B, A, A' within one second: the names (and the zip's entries) differ."""
        gw = new_gateway(self, recording=Recording())
        second = T0 // 1_000_000 * 1_000_000
        tree_a2 = tree_layout("TreeA", 4)                    # a new layout object: a new run
        for i, (tree, size) in enumerate([(self.tree_a, 5), (self.tree_b, 4),
                                          (self.tree_a, 5), (tree_a2, 5)]):
            gw.recording.begin_segment(tree, second + 1000 * i, bytes(size))
            gw.recording.append([(second + 1000 * i + 10, 1, 1)])
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(second / 1e6))
        names = [entry["filename"] for entry in
                 json.loads(answer(gw, "/log/runs").body)]
        self.assertEqual(names, [f"TreeA_{stamp}.btlog", f"TreeB_{stamp}.btlog",
                                 f"TreeA_{stamp}_2.btlog", f"TreeA_{stamp}_3.btlog"])
        response = answer(gw, "/log.zip")
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            self.assertEqual(sorted(n for n in archive.namelist() if n.endswith(".btlog")),
                             sorted(names))

    def test_what_is_not_there_is_404(self):
        off = new_gateway(self)
        # A run with no blackboard has no sidecar, not a header-only one: reopened,
        # it would show empty boards instead of "No blackboard in this file.".
        cases = [("no blackboard", self.gw, "/log.bb.jsonl?run=1", b"no blackboard in this run")]
        cases += [(f"unknown run {path}", self.gw, path, None)
                  for path in ("/log.btlog?run=2", "/log.btlog?run=-1", "/log.btlog?run=x",
                               "/log.btlog", "/log.bb.jsonl?run=2")]
        cases += [(f"recording off {path}", off, path, None)
                  for path in ("/log/runs", "/log.zip", "/log.btlog?run=0", "/log.bb.jsonl?run=0")]
        for label, gw, path, body in cases:
            with self.subTest(label):
                response = answer(gw, path)
                self.assertEqual(response.status_code, 404)
                if body is not None:
                    self.assertEqual(response.body, body)
        self.assertEqual(self.request("/log.btlog?run=1").status_code, 200)


if __name__ == "__main__":
    unittest.main()
