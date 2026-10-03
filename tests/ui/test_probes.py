"""``BrowserProbe`` in a real browser: it reads what the user sees, and it
works against the real BehaviorTree.CPP t11 robot. Skips without Playwright
or a browser, and the t11 test without a BehaviorTree.CPP build."""
import unittest

from tests.harness.oracles import state_matches
from tests.harness.probes import GatewayProbe
from tests.harness.targets import MockTarget, T11Target
from tests.ui import DashboardCase


class BrowserProbeTest(DashboardCase):
    """One browser; each test brings its own robot and gateway."""

    ROBOT = GATEWAY = OPEN = None
    # Wide enough that the camera's fixed 0.8 scale shows the whole CrossDoor
    # tree beside the sidebar (at 1400 px SmashDoor runs off the right edge).
    VIEWPORT = (1680, 1000)

    def _open(self, gateway):
        b = self.browser
        b.base_url = gateway.url
        b.open(wait_nodes=13)
        b.wait_connected()
        b.wait_for_status_frames(3)
        return b

    def test_snapshot_reads_the_card_text_so_tampering_is_caught(self):
        # Hand-edit one card's pill in the same JS task as the read: the probe's
        # state comes from the text the user sees, not from the d3 datum, so it
        # no longer equals what the page says it painted.
        with MockTarget() as robot, GatewayProbe(robot.port) as gw:
            tampered = self._open(gw).snapshot(
                "const g = [...document.querySelectorAll('#canvas g.node')]"
                ".find(n => n.__data__.data.uid === 13);"
                "const t = g.querySelector('.node-status-text');"
                "t.textContent = t.textContent === 'FAILURE' ? 'SUCCESS' : 'FAILURE'")
        self.assertIsNotNone(tampered["displayed"])
        failed = state_matches(tampered["state"], tampered["displayed"])
        self.assertFalse(failed)
        self.assertIn("uid 13", failed.detail)

    def test_t11_shows_thirteen_cards_online(self):
        with T11Target() as robot, GatewayProbe(robot.port) as gw:
            snap = self._open(gw).snapshot()
        self.assertEqual(sorted(n["uid"] for n in snap["nodes"]), list(range(1, 14)))
        self.assertEqual(snap["connection"]["state"], "online", snap["connection"])


if __name__ == "__main__":
    unittest.main()
