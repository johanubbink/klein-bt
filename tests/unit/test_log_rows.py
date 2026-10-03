"""The Log tab's rows (``logRows``, ``logRowAt``, ``logRowIndex`` in
``static/recording.js``), run under gjs against what the Python model says.

A seeded Python ``Recording`` (``make_vectors.build_named``: a same-tree
restart, then a tree swap, small chunks with the oldest evicted, a subtree in
each tree) is streamed through ``klein.streaming`` into the browser store; for several filters every row
must equal the Python record it stands for, with the node and subtree names
taken from the layout and ``from`` from Python's ``state_at_seq``. Skips
without gjs.
"""
import unittest
from unittest import mock

from klein import recording
from klein.groot2_protocol import NodeStatus, decode_status
from tests.harness.js import assert_vectors_pass
from tests.harness.model import names
from tests.make_vectors import STATIC, build_named, store_cases

FILTERS = ["", "door", "PICK", "visit", "patrol", "zzz"]


def expected_rows(rec, needle):
    needle = needle.lower()
    rows = []
    for seg in rec.segments:
        table = names(seg.layout.tree)
        for seq, ts, uid, status in seg.iter_records(seg.start_seq, seg.head_seq):
            name, subtree = table[uid]
            if needle and needle not in name.lower() and needle not in subtree.lower():
                continue
            rows.append({"seg": seg.id, "seq": seq, "t": ts, "uid": uid, "name": name,
                         "subtree": subtree, "to": NodeStatus(status).name,
                         "from": decode_status(seg.state_at_seq(seq)[uid])[0]})
    return rows


def vectors():
    """Called with CHUNK_SIZE patched to 8, as the Python model reads it."""
    rec, frames = build_named()
    assert rec.segments[0].start_seq > 0, "the scenario should evict a chunk"
    cases = store_cases("s", frames)
    probes = [(s.id, q) for s in rec.segments
              for q in (0, s.start_seq - 1, s.start_seq, s.start_seq + 3, s.head_seq - 1, s.head_seq)]
    probes += [(-1, 0), (99, 0)]
    for needle in FILTERS:
        rows = expected_rows(rec, needle)
        ref = {"$ref": f"rows {needle}"}
        cases.append({"name": f"{needle!r}: rows", "call": "KleinRecording.logRows",
                      "args": [{"$ref": "s_rec"}, needle], "save": f"rows {needle}"})
        for i, row in enumerate(rows):
            cases.append({"name": f"{needle!r}: row {i}", "call": "KleinRecording.logRowAt",
                          "args": [ref, i], "expect": row})
        for i in (-1, len(rows)):
            cases.append({"name": f"{needle!r}: no row {i}", "call": "KleinRecording.logRowAt",
                          "args": [ref, i], "expect": None})
        keys = [(r["seg"], r["seq"]) for r in rows]
        for seg, seq in probes:
            index = next((i for i, key in enumerate(keys) if key >= (seg, seq)), len(keys))
            cases.append({"name": f"{needle!r}: index of {seg}:{seq}",
                          "call": "KleinRecording.logRowIndex", "args": [ref, seg, seq],
                          "expect": index})
    return {"module": [str(STATIC / "recording.js")], "cases": cases}, rec


class LogRowsTest(unittest.TestCase):
    @mock.patch.object(recording, "CHUNK_SIZE", 8)      # small chunks: boundaries and eviction
    def test_rows_equal_the_python_records(self):
        data, rec = vectors()
        # The scenario is what the docstring says, and the filters bite.
        self.assertEqual([s.layout.generation for s in rec.segments], [1, 1, 2])
        counts = [len(expected_rows(rec, f)) for f in FILTERS]
        self.assertTrue(counts[0] > counts[1] > 0 and counts[-1] == 0, counts)
        assert_vectors_pass(self, data)


if __name__ == "__main__":
    unittest.main()
