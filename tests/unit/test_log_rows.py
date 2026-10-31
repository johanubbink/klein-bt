"""The Log tab's rows (``logRows`` in ``static/recording.js``: its ``count``,
``at`` and ``indexOf``) and the row it keeps at its top across a new listing
(``anchorAfter`` in ``static/view.js``), run under gjs against what the
Python model says.

A seeded Python ``Recording`` (``make_vectors.build_named``: a same-tree
restart, a tree swap, then a new run of the first tree after an outage, small
chunks with the oldest evicted, a subtree in each tree) is streamed through
``klein.streaming`` into the browser store; for several filters every row
must equal the Python record it stands for, with the node and subtree names
taken from the layout and ``from`` from Python's ``state_at_seq``; a record
kept, evicted, filtered out, past the head or of a segment that is gone,
as the row at the top, is found again exactly when it is still listed.
Without a recording there are no rows. Skips without gjs.
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
        ref, saved = {"$ref": f"rows {needle}"}, f"$rows {needle}"
        cases.append({"name": f"{needle!r}: rows", "call": "KleinRecording.logRows",
                      "args": [{"$ref": "s_rec"}, needle], "save": f"rows {needle}"})
        cases.append({"name": f"{needle!r}: count", "get": f"{saved}.count", "expect": len(rows)})
        for i, row in enumerate(rows):
            cases.append({"name": f"{needle!r}: row {i}", "call": f"{saved}.at", "args": [i],
                          "expect": row})
        for i in (-1, len(rows)):
            cases.append({"name": f"{needle!r}: no row {i}", "call": f"{saved}.at", "args": [i],
                          "expect": None})
        keys = [(r["seg"], r["seq"]) for r in rows]
        for seg, seq in probes:
            index = next((i for i, key in enumerate(keys) if key >= (seg, seq)), len(keys))
            cases.append({"name": f"{needle!r}: index of {seg}:{seq}",
                          "call": f"{saved}.indexOf", "args": [seg, seq], "expect": index})
            cases.append({"name": f"{needle!r}: anchor {seg}:{seq}",
                          "call": "KleinView.anchorAfter", "args": [ref, {"seg": seg, "seq": seq}],
                          "expect": keys.index((seg, seq)) if (seg, seq) in keys else None})
    cases += [{"name": "no recording: rows", "call": "KleinRecording.logRows", "args": [None, ""],
               "save": "no rows"},
              {"name": "no recording: count", "get": "$no rows.count", "expect": 0},
              {"name": "no recording: no row 0", "call": "$no rows.at", "args": [0], "expect": None},
              {"name": "no recording: index of 0:0", "call": "$no rows.indexOf", "args": [0, 0],
               "expect": 0}]
    return {"modules": {"KleinRecording": str(STATIC / "recording.js"),
                        "KleinView": str(STATIC / "view.js")}, "cases": cases}, rec


class LogRowsTest(unittest.TestCase):
    @mock.patch.object(recording, "CHUNK_SIZE", 8)      # small chunks: boundaries and eviction
    def test_rows_equal_the_python_records(self):
        data, rec = vectors()
        # The scenario is what the docstring says, and the filters bite.
        self.assertEqual([s.layout.generation for s in rec.segments], [1, 1, 2, 3])
        counts = [len(expected_rows(rec, f)) for f in FILTERS]
        self.assertTrue(counts[0] > counts[1] > 0 and counts[-1] == 0, counts)
        anchors = [c["expect"] for c in data["cases"] if ": anchor " in c["name"]]
        self.assertTrue(None in anchors and any(anchors), "rows found again and gone")
        assert_vectors_pass(self, data)


if __name__ == "__main__":
    unittest.main()
