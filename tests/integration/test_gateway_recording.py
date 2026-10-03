"""The recording gateway against real publishers over ZeroMQ.

The gateway runs as a subprocess with ``--debug`` and no dashboard connected;
what it recorded is read from ``GET /debug/state`` and checked with the
harness oracles against the robot's own ground truth. Each test then also
downloads every tree run (``/log/runs``, ``/log.btlog``, ``/log.bb.jsonl``)
and checks the files replay to what was recorded; with ``KLEIN_SHOTS=1`` copies
go to ``tests/ui/artifacts/groot2/`` for opening in Groot2. The t11 check skips
when BehaviorTree.CPP or g++ is missing.
"""
import json
import time
import unittest

from klein import mock_robot
from tests.harness import btlog_ref
from tests.harness.model import Model
from tests.harness.oracles import request_sequence_matches, state_matches, transitions_match
from tests.harness.probes import ARTIFACTS_DIR, SHOTS, GatewayProbe, WireTap
from tests.harness.targets import MockTarget, T11Target
from tests.helpers import equivalent, read_sidecar, sidecar_at


# The mock steps once per STATUS poll, so polling every 5 ms runs it ~15x
# faster with the same coverage. Request timeouts stay real time (2 s).
POLL_INTERVAL = 0.005
# Mock status polls per tree, ~1 s at that rate: more than one lap of either
# tree (CrossDoor is 76 polls, patrol 54), so each segment sees whole missions.
SWITCH_EVERY = 120
OUTAGE = 2.3                # s the mock stays down: longer than REQUEST_TIMEOUT (2 s)


def _truth_by_publisher(robot):
    """``[(marker, records)]``: the truth log cut at each publisher marker."""
    truth, markers = robot.ground_truth(), robot.publishers()
    bounds = [m["index"] for m in markers] + [len(truth)]
    return [(m, truth[bounds[i]:bounds[i + 1]]) for i, m in enumerate(markers)]


def _publisher_of(segment, publishers):
    """The publisher a segment recorded: the latest one started before its arm."""
    return [p for p in publishers if p[0]["t"] <= segment["t_begin"]][-1]


def _state_mismatches(segment, records, truth):
    """``[(t, uid, klein, truth)]`` where the segment's start state plus its
    records differs from the publisher's own state (its truth replayed from
    all-IDLE), at each of the segment's record times. The mock stamps both
    with one clock, so the times compare directly."""
    ours, theirs = dict(enumerate(segment["start_state"])), {}
    mine, truths = iter(records + [(float("inf"), 0, 0)]), iter(truth + [(float("inf"), 0, 0)])
    k, r = next(mine), next(truths)
    bad = []
    for t in sorted({rec[0] for rec in records}):
        while k[0] <= t:
            btlog_ref.apply(ours, k[1], k[2])
            k = next(mine)
        while r[0] <= t:
            btlog_ref.apply(theirs, r[1], r[2])
            r = next(truths)
        bad += [(t, uid, ours[uid], theirs.get(uid, 0)) for uid in ours
                if ours[uid] != theirs.get(uid, 0)]
    return bad


def _download_runs(gw):
    """``[(entry, btlog response, bb.jsonl response)]`` for every run in ``/log/runs``."""
    status, _headers, body = gw.fetch("/log/runs")
    assert status == 200, (status, body)
    return [(entry, gw.fetch(f"/log.btlog?run={entry['run']}"),
             gw.fetch(f"/log.bb.jsonl?run={entry['run']}")) for entry in json.loads(body)]


def _keep_for_groot2(name, data):
    """With ``KLEIN_SHOTS=1``, keep a saved file in ``tests/ui/artifacts/groot2/``,
    for opening in Groot2 by hand."""
    if SHOTS:
        path = ARTIFACTS_DIR / "groot2" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class SavedRunChecks:
    """Checks a downloaded run against the debug dump fetched just before it."""

    def check_saved_run(self, download, state, run_ids, xml):
        entry, (status, headers, data), (bb_status, bb_headers, sidecar) = download
        self.assertEqual((status, bb_status), (200, 200))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["Content-Disposition"],
                         f'attachment; filename="{entry["filename"]}"')
        self.assertEqual(bb_headers["Content-Disposition"],
                         f'attachment; filename="{entry["filename"][:-6]}.bb.jsonl"')
        log = btlog_ref.parse(data)
        self.assertEqual((log.version, log.trailing), (1, 0))
        self.assertEqual(log.xml, xml)
        model = Model(state)
        segments = [s for s in state["segments"] if s["id"] in run_ids]
        self.assertEqual(log.first_timestamp_us, segments[0]["t_begin"])
        self.assertEqual(entry["t_begin"], segments[0]["t_begin"])

        # Every recorded record is in the file, in order (it adds only each
        # segment's opening transitions, and what was drained since the dump).
        file_records = btlog_ref.absolute(log)
        recorded = [r for s in segments for r in model.records(s["id"])]
        remaining = iter(file_records)
        self.assertTrue(all(r in remaining for r in recorded), "recorded records missing")

        # Replayed from all-IDLE, the file shows what klein recorded (the
        # segment's start state plus its records) at every record time of the
        # file and between two, wherever a segment was recording.
        uids = btlog_ref.tree_uids(log.xml)
        replayed = btlog_ref.states_at_record_times(file_records, uids)
        times = sorted(replayed)
        checks = [(t, replayed[t]) for t in times]
        checks += [((a + b) // 2, replayed[a]) for a, b in zip(times, times[1:]) if b - a > 1]
        checked = 0
        for segment in segments:
            end = segment["t_end"] if segment["t_end"] is not None else state["head"] + 1
            expected = dict(enumerate(segment["start_state"]))
            records = iter(model.records(segment["id"]) + [(float("inf"), 0, 0)])
            pending = next(records)
            for t, got in sorted((c for c in checks if segment["t_begin"] <= c[0] < end),
                                 key=lambda c: c[0]):
                while pending[0] <= t:
                    btlog_ref.apply(expected, pending[1], pending[2])
                    pending = next(records)
                bad = [u for u in uids if not equivalent(got[u], expected[u])]
                self.assertEqual(bad, [], (segment["id"], t - segment["t_begin"]))
                checked += 1
        self.assertGreater(checked, len(set(r[0] for r in recorded)))

        # The sidecar: at every recorded blackboard change, its latest values
        # hold that change.
        header, lines = read_sidecar(sidecar)
        self.assertEqual(header, {"klein_blackboard": 1,
                                  "first_timestamp": log.first_timestamp_us,
                                  "tree_id": entry["tree_id"]})
        changes = [c for b in state["blackboard"] if b["seg"] in run_ids for c in b["changes"]]
        for t, board, key, text in changes:
            values = sidecar_at(lines, t - log.first_timestamp_us).get(board, {})
            if text is None:
                self.assertNotIn(key, values)
            else:
                self.assertEqual(values.get(key), json.loads(text), (t, board, key))
        return log, len(changes)


class MockRecordingTest(SavedRunChecks, unittest.TestCase):
    """The mock, polled fast (~6 s): a ``--switch-every`` swap there and back,
    then a restart on the same tree after an outage. Every segment's records
    equal its publisher's truth from klein's arm on; the head state equals the
    latest STATUS; blackboard samples lie inside their segments."""

    def test_records_match_truth_across_swaps_and_a_restart(self):
        with MockTarget(switch_every=SWITCH_EVERY) as robot, WireTap(robot.port) as tap, \
                GatewayProbe(tap.port, debug=True, poll_interval=POLL_INTERVAL) as gw:
            # Two swaps (crossdoor -> patrol -> crossdoor), then a restart.
            self.assertTrue(tap.wait_for(
                lambda t: len(t.requests("T")) >= 3, timeout=15), tap.request_sequence())
            time.sleep(0.3)                     # well short of the next swap
            # Down for longer than a request timeout, so klein sees an outage. (A
            # restart quicker than that reaches klein as a new UUID instead: a
            # new segment in the same run, with no gap.)
            stopped = time.monotonic()
            robot.stop()
            time.sleep(OUTAGE)
            robot.restart()
            self.assertTrue(tap.wait_for(
                lambda t: len(t.requests("T")) >= 4, timeout=10), gw.log()[-2000:])
            # One CrossDoor lap (76 polls) after the arm, before the next swap.
            resumed = len(tap.requests("S"))
            self.assertTrue(tap.wait_for(
                lambda t: len(t.requests("S")) >= resumed + 85, timeout=10))
            state, replies = self._state_with_latest_status(gw, tap)
            downloads = _download_runs(gw)
            seq = tap.request_sequence(ignore="B")
            publishers = _truth_by_publisher(robot)

        self.assertNotIn("poller error", gw.log())
        # The wire: arm on every handshake, then S t pairs; never `r stop`.
        startup = request_sequence_matches(seq[:40], r"TrS(St)+S?t?")
        self.assertTrue(startup, startup.detail)
        whole = request_sequence_matches(seq, r"(TrS(St)+S*)+(t)?")
        self.assertTrue(whole, whole.detail)
        self.assertEqual({r["arg"] for r in tap.requests("r")}, {"start"})

        # Segments and runs: crossdoor, patrol, crossdoor + the restart's resume.
        segments = state["segments"]
        self.assertEqual(len(segments), 4, segments)
        self.assertEqual(state["runs"], [[0], [1], [2, 3]])
        layouts = [s["layout_id"] for s in segments]
        self.assertEqual(len(set(layouts[:3])), 3)
        self.assertEqual(layouts[2], layouts[3])
        self.assertEqual([g[2] for g in state["gaps"]], ["outage"])
        self.assertEqual(state["gaps"][0][:2], [segments[2]["t_end"], segments[3]["t_begin"]])
        # The outage starts where klein last heard from the robot: its last
        # answered `t` drain before the stop (wall clock, as the mock's is).
        last_drain = [m["reply"] for m in tap.requests("t")
                      if "reply" in m and m["reply"]["t"] < stopped][-1]
        lag = segments[2]["t_end"] - round(last_drain["wall"] * 1e6)
        self.assertLess(abs(lag), POLL_INTERVAL * 1e6, lag)
        self.assertGreaterEqual(segments[3]["t_begin"] - segments[2]["t_end"],
                                OUTAGE * 1e6)
        self.assertTrue(state["recording"])

        # Each segment against its own publisher's truth, from the arm on.
        model = Model(state)
        for segment in segments:
            marker, truth = _publisher_of(segment, publishers)
            records = model.records(segment["id"])
            result = transitions_match(records, truth, from_time=segment["t_begin"])
            self.assertTrue(result, f"segment {segment['id']}: {result.detail}")
            # And the state, from the arm on: the baseline plus the records is
            # the publisher's own state at every record time (no lost step).
            bad = _state_mismatches(segment, records, truth)
            self.assertEqual(bad[:5], [], f"segment {segment['id']}: {len(bad)} mismatches")

        # The head state equals a STATUS reply exactly (arming loses nothing),
        # allowing one poll of lag.
        results = [state_matches(state["state"], btlog_ref.decode(reply), allow_was_vs_idle=False)
                   for reply in replies]
        self.assertTrue(any(results), [r.detail for r in results])

        # Blackboard samples are stamped inside their segment: from its arm to
        # its end, or for the open one to the head (plus the B poll that may
        # have landed after the last drain, before the dump).
        samples = 0
        for board in state["blackboard"]:
            segment = next(s for s in segments if s["id"] == board["seg"])
            upper = (segment["t_end"] if segment["t_end"] is not None
                     else state["head"] + 50_000)
            times = [change[0] for change in board["changes"]]
            samples += len(times)
            self.assertTrue(all(segment["t_begin"] <= t <= upper for t in times),
                            (segment, times))
        self.assertGreater(samples, 0)

        # Saved: one file per tree run, each that tree's FULLTREE XML, each
        # replaying to what was recorded (run 2 across the outage).
        self.assertEqual([d[0]["run"] for d in downloads], [0, 1, 2])
        self.assertEqual([d[0]["tree_id"] for d in downloads],
                         ["MainTree", "PatrolTree", "MainTree"])
        xml = {"MainTree": mock_robot.CROSSDOOR_XML, "PatrolTree": mock_robot.PATROL_XML}
        for download, run_ids in zip(downloads, state["runs"]):
            log, changes = self.check_saved_run(download, state, run_ids,
                                                xml[download[0]["tree_id"]])
        self.assertIsNone(downloads[2][0]["t_end"])
        for name, download in (("mock_crossdoor", downloads[0]),
                               ("mock_crossdoor_restart", downloads[2])):
            _keep_for_groot2(name + ".btlog", download[1][2])
            _keep_for_groot2(name + ".bb.jsonl", download[2][2])

    @staticmethod
    def _state_with_latest_status(gw, tap):
        """The debug dump, plus the STATUS replies that could be its head: those
        answered while the dump was being fetched, and one poll either side.
        The mock steps on each S and klein drains that step right after, so
        klein's head equals one of them."""
        def answered():
            return [m["reply"] for m in tap.requests("S") if "reply" in m]
        t0 = time.monotonic()
        state = gw.require_debug_state()
        t1 = time.monotonic()
        tap.wait_for(lambda _t: any(r["t"] > t1 for r in answered()), timeout=2)
        replies = answered()
        before = [r["status"] for r in replies if r["t"] < t0][-1:]
        during = [r["status"] for r in replies if t0 <= r["t"] <= t1]
        after = [r["status"] for r in replies if r["t"] > t1][:1]
        replies = before + during + after
        return state, replies


class T11RecordingTest(SavedRunChecks, unittest.TestCase):
    """~5 s behind the real BehaviorTree.CPP t11: klein's records equal the
    robot's own FileLogger2 from klein's arm on, at a constant offset < 50 µs.

    A mission takes 3.7 s, then t11 sleeps 2 s: the dump is read once klein
    has the first mission's end, in that pause. Only the mission's first µs
    burst comes before klein arms.
    """

    MISSION_END = [[1, 2], [1, 0]]          # the mission Sequence: SUCCESS, then IDLE

    def test_records_equal_the_filelogger(self):
        with T11Target() as robot, WireTap(robot.port) as tap, \
                GatewayProbe(tap.port, debug=True) as gw:
            deadline = time.monotonic() + 15
            while True:
                state = gw.require_debug_state()
                pairs = [r[1:] for r in state["records"]]
                if any(pairs[i:i + 2] == self.MISSION_END for i in range(len(pairs))):
                    break
                self.assertLess(time.monotonic(), deadline, "no mission end recorded")
                time.sleep(0.05)
            downloads = _download_runs(gw)
            truth = robot.ground_truth()
            seq = tap.request_sequence(ignore="B")
        self.assertEqual(len(state["segments"]), 1, state["segments"])
        segment = state["segments"][0]
        records = [tuple(r) for r in state["records"]]
        # The whole mission after the arm: every retry, through its end (the
        # mission Sequence, uid 1, SUCCESS then IDLE) with nothing after it.
        pairs = [r[1:] for r in records]
        self.assertEqual(pairs[-2:], [(1, 2), (1, 0)], pairs)
        self.assertGreater(len(records), 35)
        result = transitions_match(records, truth, from_time=segment["t_begin"])
        self.assertTrue(result, result.detail)
        wire = request_sequence_matches(seq, r"TrS(St)+S?")
        self.assertTrue(wire, wire.detail)

        # Saved: one run, the robot's FULLTREE XML, replaying to what was recorded.
        self.assertEqual(len(downloads), 1)
        log, changes = self.check_saved_run(downloads[0], state, [segment["id"]], robot.xml)
        _keep_for_groot2("t11.btlog", downloads[0][1][2])
        _keep_for_groot2("t11.bb.jsonl", downloads[0][2][2])


if __name__ == "__main__":
    unittest.main()
