"""The dashboard's recording mirror (``window.kleinDebug``) against the
gateway's own recording (``GET /debug/state``), live and after a reload."""
import unittest

from tests.harness.model import Model
from tests.harness.oracles import state_in_frames, state_matches
from tests.ui import DashboardCase


class BrowserMirrorTest(DashboardCase):
    """The dashboard's mirror (``window.kleinDebug``) against the
    gateway's own recording (``GET /debug/state``), live and after a reload,
    over a run with tree swaps."""

    ROBOT = {"switch_every": 100}
    GATEWAY = {"debug": True, "poll_interval": 0.01}
    VIEWPORT = (1680, 1000)
    OPEN = 1

    def compare(self, browser, gw):
        mine = browser.klein_debug()
        theirs = gw.debug_state()               # read second: it can only be ahead
        self.assertTrue(mine["backfillDone"])
        segs = {s["id"]: s for s in theirs["segments"]}
        self.assertEqual([s["id"] for s in mine["segments"]], list(segs)[:len(mine["segments"])])
        for s in mine["segments"]:
            t = segs[s["id"]]
            self.assertEqual((s["startSeq"], s["tBegin"]), (t["start_seq"], t["t_begin"]))
            self.assertLessEqual(s["headSeq"], t["head_seq"])
            if s["tEnd"] is not None:
                self.assertEqual((s["headSeq"], s["tEnd"]), (t["head_seq"], t["t_end"]))
        self.assertLessEqual(mine["head"], theirs["head"])
        self.assertEqual(mine["tMin"], theirs["t_min"])
        last = mine["segments"][-1]
        reference = Model(theirs).decoded(last["id"], last["headSeq"], mine["stateAtHead"])
        result = state_matches(mine["stateAtHead"], reference, allow_was_vs_idle=False)
        self.assertTrue(result, result.detail)
        return mine, theirs

    def test_the_mirror_equals_the_gateway_live_and_after_a_reload(self):
        browser, gw = self.browser, self.gw
        browser.wait_connected()
        # A tree swap recorded, and the new tree's segment under way.
        self.page.wait_for_function("kleinDebug().segments.length >= 2"
                                    " && kleinDebug().segments.at(-1).headSeq > 0",
                                    timeout=15000)
        mine, theirs = self.compare(browser, gw)
        # The cards show what the page painted, and that is a STATUS frame
        # within one poll either way (the head runs a poll ahead). The tree
        # swaps about every second, so read between swaps: no card in its
        # exit transition, and all three frames from the displayed tree.
        for _ in range(5):
            browser.wait_for_nodes(1)
            snap = browser.snapshot()
            n = snap["statusCount"]
            frames = browser.status_frames(n - 1, n + 1)
            if all(set(f) == set(snap["displayed"]) == set(snap["state"]) for f in frames):
                break
        painted = state_matches(snap["state"], snap["displayed"], allow_was_vs_idle=False)
        self.assertTrue(painted, painted.detail)
        near = state_in_frames(snap["displayed"], frames)
        self.assertTrue(near, near.detail)
        self.assertGreaterEqual(len(mine["segments"]), 2, "expected a tree swap")

        self.page.reload()
        browser.wait_for_nodes(1)
        self.page.wait_for_function("window.kleinDebug() && window.kleinDebug().backfillDone")
        again, _ = self.compare(browser, gw)
        # The tree swaps about every second, so compare segment by segment:
        # each one seen before the reload has at least as many records now.
        before = {s["id"]: s["headSeq"] for s in mine["segments"]}
        after = {s["id"]: s["headSeq"] for s in again["segments"]}
        self.assertTrue(set(before) <= set(after), (before, after))
        for seg_id, head_seq in before.items():
            self.assertGreaterEqual(after[seg_id], head_seq, seg_id)


if __name__ == "__main__":
    unittest.main()
