"""The oracles in ``tests/harness/oracles.py`` fail on known-bad input.

An oracle that wrongly passes would let every test built on it pass silently,
so each one is fed a table of broken variants of real data (the t11 FileLogger2
fixture, the mock's groot2 fixture) and must reject every row, naming the
cause in its ``detail``. (The reference ``.btlog`` reader and writer are
checked on the fixtures in ``test_btlog``.)
"""
import unittest
from pathlib import Path

from tests.harness import btlog_ref
from tests.harness.oracles import (
    btlog_equivalent,
    request_sequence_matches,
    state_in_frames,
    state_matches,
    transitions_match,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GROOT2_FIXTURE = FIXTURES / "groot2_mock.btlog"
T11_FIXTURE = FIXTURES / "t11_filelogger2.btlog"


class TransitionsMatchTest(unittest.TestCase):
    """Truth is the real t11 FileLogger2 fixture; "klein" is it seen through a
    constant 7 µs clock offset, the way klein's ``r start`` anchor differs from it."""

    def test_bad_records_fail(self):
        truth = btlog_ref.absolute(btlog_ref.read(T11_FIXTURE))
        klein = [(t + 7, uid, st) for t, uid, st in truth]

        flipped = list(klein)
        t, uid, st = flipped[20]
        flipped[20] = (t, uid, 2 if st != 2 else 3)

        # Armed 3 µs before a record that the next one follows by >= 100 µs:
        # klein's list skips both, and the second is past the ambiguous zone
        # around the arm, so it is owed.
        i = next(i for i in range(len(truth) - 2) if truth[i + 1][0] - truth[i][0] >= 100)
        arm = truth[i][0] - 3

        # A 12 µs step for a stretch between two quiet gaps (>= 1 ms), so no
        # record moves past a neighbour and only the timing is wrong.
        gaps = [k for k in range(1, len(truth)) if truth[k][0] - truth[k - 1][0] >= 1000]
        drift = [(t + (12 if gaps[0] <= k < gaps[1] else 0), uid, st)
                 for k, (t, uid, st) in enumerate(truth)]

        cases = [
            ("one status flipped", flipped, truth, {}, "record #20"),
            ("one record dropped", klein[:30] + klein[31:], truth, {}, "record #30"),
            ("a record skipped after the arm", klein[i + 2:], truth, {"from_time": arm},
             "lacks"),
            ("offset beyond tolerance", [(t + 80, u, s) for t, u, s in truth], truth, {},
             "exceeds 50 us"),
            ("offset not constant", drift, truth, {}, "not constant"),
            ("a spurious trailing record", klein + [(klein[-1][0] + 1000, 13, 1)], truth, {},
             f"klein has {len(truth) + 1} records where the truth has {len(truth)}"),
            ("klein empty", [], truth, {}, "nothing to compare"),
            ("truth empty", klein, [], {}, "nothing to compare"),
        ]
        for label, observed, reference, kwargs, why in cases:
            with self.subTest(label):
                result = transitions_match(observed, reference, **kwargs)
                self.assertFalse(result, result.detail)
                self.assertIn(why, result.detail)


class StateMatchesTest(unittest.TestCase):
    def setUp(self):
        log = btlog_ref.read(T11_FIXTURE)
        mid = log.records[30][0]
        self.reference = btlog_ref.decode(
            btlog_ref.replay(log.records, btlog_ref.tree_uids(log.xml), until=mid))
        self.was = next(u for u, e in self.reference.items() if e["from"])

    def altered(self, uid, entry=None):
        state = dict(self.reference)
        if entry is None:
            del state[uid]
        else:
            state[uid] = entry
        return state

    def test_bad_states_fail(self):
        was, ref = self.was, self.reference
        other = "FAILURE" if ref[was]["from"] != "FAILURE" else "SUCCESS"
        plain_idle = {"status": "IDLE", "from": None}
        cases = [
            # "was X" against plain IDLE passes only with the allowance.
            ("was X versus plain IDLE, strict", self.altered(was, plain_idle), ref,
             {"allow_was_vs_idle": False}, f"uid {was}"),
            # Both sides carry history, so it must agree even with the allowance.
            ("was SUCCESS versus was FAILURE", self.altered(was, {"status": "IDLE", "from": other}),
             ref, {}, f"uid {was}"),
            ("one status altered", self.altered(11, {"status": "SUCCESS", "from": None}), ref, {},
             "1 of 13 nodes differ"),
            ("a missing node", self.altered(4), ref, {}, "uid 4"),
            ("both empty", {}, {}, {}, "both states are empty"),
        ]
        for label, observed, reference, kwargs, why in cases:
            with self.subTest(label):
                result = state_matches(observed, reference, **kwargs)
                self.assertFalse(result, result.detail)
                self.assertIn(why, result.detail)

    def test_state_in_frames_passes_on_any_and_fails_on_none(self):
        altered = self.altered(11, {"status": "SUCCESS", "from": None})
        self.assertTrue(state_in_frames(self.reference, [altered, self.reference]))
        failed = state_in_frames(self.reference, [altered, altered])
        self.assertFalse(failed)
        self.assertIn("uid 11", failed.detail)
        # Strict: a "was X" that the frame reports as plain IDLE doesn't match.
        plain = self.altered(self.was, {"status": "IDLE", "from": None})
        self.assertFalse(state_in_frames(self.reference, [plain]))


class BtlogEquivalentTest(unittest.TestCase):
    def setUp(self):
        self.data = GROOT2_FIXTURE.read_bytes()
        self.log = btlog_ref.parse(self.data)

    def _tampered(self, index, field):
        pos = btlog_ref.record_offset(self.log, index) + {"status": 8, "uid": 6, "time": 0}[field]
        data = bytearray(self.data)
        data[pos] ^= 0x01
        return bytes(data)

    def test_altered_files_fail(self):
        log = self.log
        cases = [
            ("a record's status byte", self._tampered(300, "status"), "replayed state differs"),
            ("a record's uid byte", self._tampered(300, "uid"), "replayed state differs"),
            ("a record's time byte", self._tampered(300, "time"), "replayed state differs"),
            ("the first timestamp",
             btlog_ref.build(log.xml, log.first_timestamp_us + 1000, log.records),
             "header differs"),
            ("the XML", btlog_ref.build(log.xml.replace("PickLock", "PickLocK"),
                                        log.first_timestamp_us, log.records), "XML differs"),
        ]
        for label, other, why in cases:
            with self.subTest(label):
                result = btlog_equivalent(self.data, other)
                self.assertFalse(result, result.detail)
                self.assertIn(why, result.detail)


class RequestSequenceMatchesTest(unittest.TestCase):
    def test_pass_and_fail(self):
        self.assertTrue(request_sequence_matches("TSSSS", r"T(S)+"))
        self.assertTrue(request_sequence_matches("TrSStSt", r"TrS(St)+"))
        wrong = request_sequence_matches("TSSSS", r"TrS(St)+")
        self.assertFalse(wrong)
        self.assertIn("does not match", wrong.detail)
        self.assertFalse(request_sequence_matches("TSSStS", r"T(S)+"))    # a stray t


if __name__ == "__main__":
    unittest.main()
