"""The Timeline tab's pure helpers in ``static/recording.js``, under gjs.

* ``intervalsAll`` (every node's intervals in one pass) equals ``intervals``
  per uid, as ``tests/make_vectors.py`` writes it from the Python model, over
  windows across a same-tree restart, a tree swap, evicted chunks, before the
  retained start and past the head;
* ``timelineMarks`` turns those intervals into bars, caps, single marks,
  failures and changes by the rule in docs/architecture.md, written again
  here;
* ``timelineSections`` lists a tree as sections per subtree, with folds and
  filters, as written again here in a different shape (a flat walk, then
  grouping by owning section);
* ``timelineWindow`` (``static/view.js``) gives a window's transition count,
  its marks per uid over every segment of the drawn tree (joined across a
  restart), the bands of other trees (another tree, or a new run of the
  same one) and gaps, and the recording's start edge, from each tree's side:
  windows over everything, the evicted start, the start edge at either end,
  across the restart, the gap and the new run, between and just touching
  segments, the open tail past the head and past it, and too many records.

Skips without gjs.
"""
import unittest
from unittest import mock

from klein import recording
from tests.harness.js import assert_vectors_pass
from tests.harness.model import Model
from tests.make_vectors import STATIC, build_named, dump, intervals, store_cases

RUNNING, SUCCESS, FAILURE, SKIPPED = 1, 2, 3, 4
OUTCOMES = (SUCCESS, FAILURE, SKIPPED)
MAX_RECORDS = 20_000                # timeline.js's MAX_WINDOW_RECORDS


def marks(ivs):
    """The Timeline's shapes for one node's intervals."""
    out = {"bars": [], "marks": [], "failures": [], "changes": []}
    for i, iv in enumerate(ivs):
        after = ivs[i + 1]["status"] if i + 1 < len(ivs) else None
        if iv["status"] == RUNNING:
            out["bars"].append({"t0": iv["t0"], "t1": iv["t1"],
                                "end": after if after in OUTCOMES else None})
        if i == 0:
            continue                    # set before the window: not a change
        out["changes"].append(iv["t0"])
        if iv["status"] == FAILURE:
            out["failures"].append(iv["t0"])
        if iv["status"] in OUTCOMES and ivs[i - 1]["status"] != RUNNING:
            out["marks"].append({"t": iv["t0"], "status": iv["status"]})
    return out


def node(id, uid, name, kind, children=(), subtree=False):
    n = {"id": id, "uid": uid, "name": name, "type": name if kind != "SubTree" else "SubTree",
         "category": kind, "children": list(children)}
    if subtree:
        n["is_subtree_root"] = True
    return n


# Two levels of subtree (tints 1 and 2), a main-tree row after a nested
# section, and a node without a uid.
TREE = {**node("a", 1, "Sequence", "Control", [
    node("b", 2, "Script", "Action"),
    node("c", 3, "Fallback", "Control", [
        node("d", 4, "Inverter", "Decorator", [node("e", 5, "IsDoorClosed", "Condition")]),
        node("f", 6, "DoorClosed", "SubTree", [
            node("g", 7, "tryOpen", "Control", [
                node("h", 8, "OpenDoor", "Action"),
                node("i", 9, "Unlock", "SubTree", [
                    node("j", 10, "PickLock", "Action"), node("k", None, "Wait", "Action")],
                    subtree=True)])], subtree=True)]),
    node("l", 11, "PassThroughDoor", "Action")]), "root_tree_id": "MainTree"}

CASES = [((), ""), (("d",), ""), (("f",), ""), (("c",), ""), (("a",), ""), (("d", "i"), ""),
         ((), "door"), ((), "pick"), (("f",), "pick"), ((), "UNLOCK"), ((), "zzz"),
         ((), "maintree"), (("c",), "script")]


def sections(tree, folded, needle):
    """``timelineSections``, from its definition: walk the whole tree once,
    decide which nodes are listed, then group them under their sections."""
    needle = needle.strip().lower()
    flat = []

    def walk(n, depth, tint, subtree, shown, owner):
        section = depth == 0 or bool(n.get("is_subtree_root"))
        if n.get("is_subtree_root"):
            subtree, tint = n["name"], tint + 1
        info = {"n": n, "depth": depth, "tint": min(tint, 3), "subtree": subtree,
                "shown": shown, "owner": owner, "section": section, "below": []}
        flat.append(info)
        is_folded = n["id"] in folded and bool(n["children"])
        start = len(flat)
        for child in n["children"]:
            walk(child, depth + 1, tint, subtree, shown and not is_folded,
                 n["id"] if section else owner)
        info["below"] = flat[start:]
        info["folded"] = is_folded
    walk(tree, 0, 0, tree.get("root_tree_id") or tree["name"], True, None)

    by_id = {i["n"]["id"]: i for i in flat}
    listed = {i["n"]["id"] for i in flat
              if (i["shown"] or needle)
              and (not needle or needle in i["n"]["name"].lower() or needle in i["subtree"].lower())}
    context = set()
    for nid in list(listed):
        owner = by_id[nid]["owner"]
        while owner is not None:
            if owner not in listed:
                context.add(owner)
            owner = by_id[owner]["owner"]
    if tree["id"] not in listed | context:
        return None

    def item(i):
        n, below = i["n"], i["below"]
        out = {"id": n["id"], "uid": n["uid"], "name": n["name"], "type": n["type"],
               "category": n["category"], "depth": i["depth"], "tint": i["tint"],
               "section": i["section"],
               "label": tree["root_tree_id"] if i["depth"] == 0 else n["name"],
               "count": len(below), "expandable": bool(n["children"]), "folded": i["folded"],
               "hidden": len(below) if i["folded"] else 0,
               "inner": [b["n"]["uid"] for b in below if b["n"]["uid"] is not None],
               "context": n["id"] in context, "items": []}
        if i["section"]:
            out["items"] = [item(j) for j in flat
                            if j["owner"] == n["id"] and j["n"]["id"] in listed | context]
        return out
    return item(by_id[tree["id"]])


def tree_name(layout):
    return layout.tree.get("root_tree_id") or layout.tree["name"]


def window(rec, seg, t0, t1, most):
    """``timelineWindow``, from its definition: the transitions in the window
    (counted by the harness Model); each segment in it, to its end (else the
    next one's begin, else the head's time), as its marks per uid when it ran
    seg's layout and the window holds at most ``most``, else as a band; the
    gaps it overlaps; the recording's start when inside it."""
    model = Model(dump(rec))
    count = model.count_in(t0, t1)
    per, bands = {}, []
    for i, s in enumerate(rec.segments):
        later = rec.segments[i + 1:]
        end = s.t_end if s.t_end is not None else later[0].t_begin if later else model.head_time()
        if end < t0 or s.t_begin > t1:
            continue
        if s.layout.generation != seg.layout.generation:
            name = tree_name(s.layout)
            title = (f"Earlier run of {name}" if name == tree_name(seg.layout)
                     else f"The robot ran another tree: {name}")
            bands.append({"from": s.t_begin, "to": end, "kind": "tree", "label": f"tree: {name}",
                          "title": title})
        elif count <= most:
            for uid in s.layout.uids:
                for kind, shapes in marks(intervals(s, uid, t0, min(t1, end))).items():
                    per.setdefault(uid, {}).setdefault(kind, []).extend(shapes)
    bands += [{"from": a, "to": b, "kind": "gap", "label": kind, "title": f"Not recorded ({kind})"}
              for a, b, kind in rec.gaps if b >= t0 and a <= t1]
    return {"count": count, "tooMany": count > most, "per": [[u, m] for u, m in sorted(per.items())],
            "bands": bands, "edge": rec.t_min if t0 <= rec.t_min <= t1 else None}


def window_cases(rec):
    """``timelineWindow`` over a table of windows, from the side of each
    tree (the cursor's segment saved as ``seg<id>``)."""
    s0, s1, s2, s3 = rec.segments
    t_min, head = rec.t_min, Model(dump(rec)).head_time()
    windows = {
        "everything": (t_min - 1000, head + 1000),
        "the evicted start": (s0.t_begin, t_min - 1),
        "the start edge at t0": (t_min, t_min + 2000),
        "the start edge at t1": (t_min - 2000, t_min),
        "across the restart": (s0.t_end - 3000, s1.t_begin + 3000),
        "between two segments": (s0.t_end + 1, s1.t_begin - 1),
        "touching both": (s0.t_end, s1.t_begin),
        "across the gap and the new run": (s2.t_end - 2000, s3.t_begin + 2000),
        "the open tail": (s3.t_begin + 1000, head + 500),
        "past the head's time": (head + 10, head + 1000),
    }
    cases = []
    for what, (t0, t1) in windows.items():
        for s in (s0, s2, s3):
            cases.append({"name": f"timelineWindow {what}, seg {s.id}",
                          "call": "KleinView.timelineWindow",
                          "args": [{"$ref": "s_rec"}, {"$ref": f"seg{s.id}"}, t0, t1, MAX_RECORDS],
                          "expect": window(rec, s, t0, t1, MAX_RECORDS)})
    t0, t1 = windows["everything"]
    count = Model(dump(rec)).count_in(t0, t1)
    for most in (count - 1, count):
        cases.append({"name": f"timelineWindow everything, at most {most}",
                      "call": "KleinView.timelineWindow",
                      "args": [{"$ref": "s_rec"}, {"$ref": f"seg{s0.id}"}, t0, t1, most],
                      "expect": window(rec, s0, t0, t1, most)})
    return cases


def vectors():
    """Called with CHUNK_SIZE patched to 8, as ``build_named`` wants."""
    rec, frames = build_named()
    cases = store_cases("s", frames)
    for k, seg in enumerate(rec.segments):
        cases.append({"name": f"segment {seg.id}", "call": "$s_rec.segments.at", "args": [k],
                      "save": f"seg{seg.id}"})
        times = [ts for _seq, ts, _uid, _status in seg.iter_records(seg.start_seq, seg.head_seq)]
        lo, hi, mid = times[0], times[-1], times[len(times) // 2]
        windows = [(seg.t_start - 500, hi + 500), (lo, mid), (mid, mid), (mid - 30, mid + 30),
                   (seg.t_begin - 900, seg.t_start - 1), (hi + 10, hi + 400)]
        uids = seg.layout.uids
        for t0, t1 in windows:
            name = f"seg {seg.id} [{t0 - lo}, {t1 - lo}]"
            expect = {str(uid): intervals(seg, uid, t0, t1) for uid in uids}
            cases.append({"name": f"{name} intervalsAll", "call": "KleinRecording.intervalsAll",
                          "args": [{"$ref": f"seg{seg.id}"}, uids, t0, t1], "expect": expect})
            for uid in uids:
                ivs = expect[str(uid)]
                cases.append({"name": f"{name} uid {uid} timelineMarks",
                              "call": "KleinRecording.timelineMarks", "args": [ivs],
                              "expect": marks(ivs)})
    # Shapes the random recording may not hold: zero-length RUNNING, a node
    # finishing in its first tick, halted to IDLE, skipped, still running.
    hand = [{"t0": 0, "t1": 5, "status": 0}, {"t0": 5, "t1": 5, "status": RUNNING},
            {"t0": 5, "t1": 9, "status": FAILURE}, {"t0": 9, "t1": 12, "status": 13},
            {"t0": 12, "t1": 12, "status": SUCCESS}, {"t0": 12, "t1": 20, "status": RUNNING},
            {"t0": 20, "t1": 21, "status": 11}, {"t0": 21, "t1": 22, "status": SKIPPED},
            {"t0": 22, "t1": 30, "status": RUNNING}]
    cases.append({"name": "hand-made timelineMarks", "call": "KleinRecording.timelineMarks",
                  "args": [hand], "expect": marks(hand)})
    for folded, needle in CASES:
        cases.append({"name": f"sections folded={folded} filter={needle!r}",
                      "call": "KleinRecording.timelineSections",
                      "args": [TREE, list(folded), needle],
                      "expect": sections(TREE, set(folded), needle)})
    cases += window_cases(rec)
    return {"modules": {"KleinRecording": str(STATIC / "recording.js"),
                        "KleinView": str(STATIC / "view.js")}, "cases": cases}, rec


class TimelineModelTest(unittest.TestCase):
    def test_reference_rules_bite(self):
        """The written-out references say what the docstring claims."""
        self.assertEqual(marks([{"t0": 0, "t1": 4, "status": 0}, {"t0": 4, "t1": 4, "status": 2}]),
                         {"bars": [], "marks": [{"t": 4, "status": 2}], "failures": [],
                          "changes": [4]})
        top = sections(TREE, set(), "")
        self.assertEqual([i["label"] for i in top["items"]],
                         ["Script", "Fallback", "Inverter", "IsDoorClosed", "DoorClosed",
                          "PassThroughDoor"])
        door = top["items"][4]
        self.assertEqual([i["label"] for i in door["items"]], ["tryOpen", "OpenDoor", "Unlock"])
        self.assertEqual((door["tint"], door["items"][2]["tint"]), (1, 2))
        self.assertEqual(sections(TREE, {"f"}, "")["items"][4]["items"], [])
        pick = sections(TREE, set(), "pick")
        self.assertTrue(pick["context"] and pick["items"][0]["context"])
        self.assertIsNone(sections(TREE, set(), "zzz"))

    @mock.patch.object(recording, "CHUNK_SIZE", 8)
    def test_helpers_equal_the_python_references(self):
        data, rec = vectors()
        self.assertGreater(rec.segments[0].start_seq, 0, "a chunk was evicted")
        # The windows hold each kind of band, the start edge and none, bars
        # and none, and too many records.
        got = [c["expect"] for c in data["cases"] if c["name"].startswith("timelineWindow")]
        self.assertEqual({b["title"] for w in got for b in w["bands"]},
                         {"Earlier run of MainTree", "The robot ran another tree: MainTree",
                          "The robot ran another tree: PatrolTree", "Not recorded (outage)"})
        self.assertEqual({w["edge"] for w in got}, {None, rec.t_min})
        self.assertEqual({bool(w["per"]) for w in got}, {True, False})
        self.assertEqual({w["tooMany"] for w in got}, {True, False})
        assert_vectors_pass(self, data)


if __name__ == "__main__":
    unittest.main()
