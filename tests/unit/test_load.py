"""Unit tests for opening a file: ``klein.btlog.load_btlog``,
``KleinGateway.open_file`` and ``klein-bt --open``'s errors.

The loaded recording is compared with a brute-force replay of the file by the
harness's independent reader (``tests/harness/btlog_ref.py``, which shares no
code with klein), at every record time and between them. All in-process.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from klein import cli
from klein import gateway as gateway_module
from klein.btlog import export_blackboard, export_run
from klein.gateway import KleinGateway
from klein.recording import decode_state
from tests.harness import btlog_ref, oracles
from tests.helpers import T11_BOARDS, T11_SIDECAR, opened, sidecar_at, t11_sidecar

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T11 = FIXTURES / "t11_filelogger2.btlog"
GROOT2 = FIXTURES / "groot2_mock.btlog"


def probe_times(times):
    """Every record time, the µs before and after it, and the midpoints."""
    times = sorted(set(times))
    out = set(times)
    for a, b in zip(times, times[1:]):
        out.update((a + 1, b - 1, (a + b) // 2))
    return sorted(out)


class ScratchDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="klein-load-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def copy(self, source, name=None, data=None):
        path = self.dir / (name or source.name)
        path.write_bytes(source.read_bytes() if data is None else data)
        return path


class LoadMatchesReplayTest(unittest.TestCase):
    """``load_btlog`` of both fixtures gives the brute-force replay's state."""

    def test_every_record_and_the_state_at_every_time_equal_a_brute_force_replay(self):
        for path in (T11, GROOT2):
            with self.subTest(path.name):
                ref = btlog_ref.read(path)
                records = btlog_ref.absolute(ref)
                uids = btlog_ref.tree_uids(ref.xml)
                gw = opened(path)
                rec = gw.recording
                (segment,) = rec.segments
                self.assertEqual(segment.layout.uids, uids)
                # Every record, in order, in one ended segment.
                got = [(ts, uid, st) for _seq, ts, uid, st
                       in segment.iter_records(segment.start_seq, segment.head_seq)]
                self.assertEqual(got, records)
                self.assertEqual(segment.t_begin, ref.first_timestamp_us)
                self.assertEqual(segment.state_at_seq(0), bytes(len(segment.baseline)))
                self.assertEqual(segment.t_end, got[-1][0])
                self.assertEqual(rec.head, got[-1][0])
                self.assertEqual(rec.gaps, [])
                times = probe_times([ref.first_timestamp_us]
                                    + [t for t, _u, _s in records])
                for t in times + [records[-1][0] + 1_000_000]:
                    shown = decode_state(segment.state_at(t), uids)
                    truth = btlog_ref.decode(btlog_ref.replay(records, uids, until=t))
                    # The Groot2 file opens with IDLEs on nodes that never ran
                    # (the mock's tree reset): the reference stores them as
                    # "was IDLE", klein, as the publisher shows them, as IDLE.
                    result = oracles.state_matches(shown, truth)
                    self.assertTrue(result, f"t = +{t - ref.first_timestamp_us} us: "
                                            f"{result.detail}")


class SidecarTest(ScratchDirTest):
    def open_t11(self, sidecar=True):
        path = self.copy(T11)
        if sidecar:
            first = btlog_ref.read(T11).first_timestamp_us
            (self.dir / "t11_filelogger2.bb.jsonl").write_text(t11_sidecar(first))
        return opened(path)

    def test_the_sidecar_gives_every_layout_board_and_its_values_at_every_time(self):
        gw = self.open_t11()
        self.assertEqual(gw._blackboard_names, T11_BOARDS)
        segment = gw.recording.segments[0]
        track = segment.blackboard
        first = segment.t_begin
        self.assertEqual(track.t_start, first)
        self.assertEqual(list(track.boards), T11_BOARDS)
        for offset in probe_times([line["t"] for line in T11_SIDECAR] + [9_000_000]):
            with self.subTest(offset=offset):
                self.assertEqual(track.at(first + offset),
                                 sidecar_at(T11_SIDECAR, offset, T11_BOARDS))
        # The board with no lines is there, empty: the panel's "—".
        self.assertEqual(track.at(segment.t_end)["DoorClosed::7"], {})

    def test_a_missing_empty_or_late_sidecar(self):
        first = btlog_ref.read(T11).first_timestamp_us
        sidecar = self.dir / "t11_filelogger2.bb.jsonl"
        with self.subTest("no sidecar: no blackboard"):
            gw = self.open_t11(sidecar=False)
            segment = gw.recording.segments[0]
            self.assertIsNone(segment.blackboard.t_start)
            self.assertIsNone(segment.blackboard.at(segment.t_end))
        with self.subTest("no lines: every board empty"):
            path = self.copy(T11)
            sidecar.write_text(json.dumps({"klein_blackboard": 1, "first_timestamp": first})
                               + "\n")
            track = opened(path).recording.segments[0].blackboard
            self.assertEqual(track.at(first), {board: {} for board in T11_BOARDS})
        with self.subTest("later first line: no sample before it"):
            path = self.copy(T11)
            lines = [{"klein_blackboard": 1, "first_timestamp": first},
                     {"t": 500_000, "board": "MainTree", "key": "door_open", "value": True}]
            sidecar.write_text("".join(json.dumps(line) + "\n" for line in lines))
            track = opened(path).recording.segments[0].blackboard
            self.assertIsNone(track.at(first + 499_999))
            self.assertEqual(track.at(first + 500_000),
                             {"MainTree": {"door_open": True}, "DoorClosed::7": {}})

    def test_saving_an_opened_file_and_opening_that_gives_the_same_recording(self):
        gw = self.open_t11()
        (run,) = gw.recording.runs()
        saved = self.dir / "saved"
        saved.mkdir()
        (saved / "x.btlog").write_bytes(export_run(run, run[0].layout.xml))
        (saved / "x.bb.jsonl").write_bytes(export_blackboard(run, "MainTree"))
        again = opened(saved / "x.btlog").recording.segments[0]
        segment = gw.recording.segments[0]
        records = list(segment.iter_records(0, segment.head_seq))
        for t in probe_times([ts for _seq, ts, _u, _s in records]):
            self.assertEqual(again.state_at(t), segment.state_at(t))
            self.assertEqual(again.blackboard.at(t), segment.blackboard.at(t))

    def test_the_stream_says_file_and_no_robot(self):
        gw = self.open_t11()
        self.assertEqual(json.loads(gw._robot_state_json),
                         {"type": "robot", "connected": False, "recording": "file",
                          "detail": "No robot — viewing t11_filelogger2.btlog"})
        # The streamer's own backfill, as a dashboard gets it.
        sent = []
        gw.streamer.send = lambda clients, frame: sent.append(frame)
        gw.streamer.subscribe(object())
        rec = json.loads(sent[0])
        self.assertEqual((rec["type"], rec["source"], rec["name"]),
                         ("rec", "file", "t11_filelogger2.btlog"))
        types = [json.loads(f)["type"] for f in sent if isinstance(f, str)]
        self.assertEqual(types[-4:], ["segment_end", "head", "evict", "backfill_done"])
        self.assertIn("bb", types)


class DamagedFileTest(ScratchDirTest):
    def cli(self, *argv):
        """``klein-bt`` in-process: ``(exit code, stdout, stderr)``. Stops
        before serving: ``asyncio.run`` is stubbed out."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["klein-bt", *argv]), \
                mock.patch.object(gateway_module.zmq.asyncio, "Context"), \
                mock.patch.object(cli.asyncio, "run",
                                  side_effect=lambda coro: coro.close()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main_cli()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_a_file_that_cannot_be_opened_is_a_clear_command_line_error(self):
        data = T11.read_bytes()
        log = btlog_ref.read(T11)
        no_magic = "not a FileLogger2 .btlog (no BTCPP4-FileLogger2 magic)"
        bad_sidecar = "t11_filelogger2.bb.jsonl is not a klein blackboard sidecar"
        # (label, file name, file content, sidecar content, message, whole message)
        cases = [
            ("bad magic", "magic.btlog", b"BTCPP4-FileLogger1" + data[18:], None,
             no_magic, True),
            ("unknown version", "version.btlog", data[:18] + b"\x07" + data[19:], None,
             "unknown .btlog format version 7", True),
            ("not a btlog", "notes.txt", b"hello\n", None, no_magic, True),
            ("missing", "absent.btlog", None, None, "No such file", False),
            ("sidecar not klein's", "t11_filelogger2.btlog", data, b'{"t": 0}\n',
             bad_sidecar, False),
            ("sidecar not UTF-8", "t11_filelogger2.btlog", data, b"\xff\xfe not UTF-8\n",
             bad_sidecar, False),
            ("unknown uid", "uid.btlog", btlog_ref.build(
                log.xml, log.first_timestamp_us, log.records + [(log.records[-1][0], 999, 1)]),
             None, "a record names a node uid the file's tree doesn't have", True),
        ]
        for label, name, content, sidecar, message, whole in cases:
            with self.subTest(label):
                path = self.dir / name
                if content is not None:
                    path = self.copy(T11, name, content)
                if sidecar is not None:
                    path.with_suffix(".bb.jsonl").write_bytes(sidecar)
                code, out, err = self.cli("--open", str(path), "--no-browser")
                self.assertEqual(code, 1)
                if whole:
                    self.assertEqual(err, f"[klein] cannot open {path}: {message}\n")
                    self.assertEqual(out, "")
                else:
                    self.assertIn(message, err)

    def test_a_good_file_starts_and_reports_a_partial_record(self):
        path = self.copy(T11, "cut.btlog", T11.read_bytes()[:-4])
        with self.subTest("open_file"):
            gw = KleinGateway("127.0.0.1", 1, 0)
            gw.ctx.term()
            self.assertEqual(gw.open_file(path), 5)
            n = len(btlog_ref.read(T11).records)
            self.assertEqual(gw.recording.segments[0].head_seq, n - 1)
        with self.subTest("5 bytes"):
            with mock.patch.object(cli, "_port_available", return_value=True):
                code, out, err = self.cli("--open", str(path), "--no-browser")
            self.assertEqual((code, err), (0, ""))
            self.assertIn("ignored a partial last record (5 bytes)", out)
            self.assertIn(f"file      : {path} (no robot)", out)
        with self.subTest("1 byte"):
            path = self.copy(T11, "cut1.btlog", T11.read_bytes() + b"\x01")
            with mock.patch.object(cli, "_port_available", return_value=True):
                code, out, err = self.cli("--open", str(path), "--no-browser")
            self.assertIn("ignored a partial last record (1 byte).", out)


if __name__ == "__main__":
    unittest.main()
