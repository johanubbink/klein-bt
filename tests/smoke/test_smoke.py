"""One end-to-end run, the CI smoke test: the mock robot and ``klein-bt`` as
real processes. The dashboard page is served, a WebSocket gets the layout and
live status, the gateway records, and the recording downloads as a valid
``.btlog`` of the mock's tree. No timing assertions beyond generous timeouts.
"""
import json
import unittest

from klein import mock_robot
from tests.harness import btlog_ref
from tests.harness.probes import GatewayProbe
from tests.harness.targets import MockTarget


class SmokeTest(unittest.TestCase):
    def test_mock_to_dashboard_to_saved_file(self):
        with MockTarget() as robot, GatewayProbe(robot.port, poll_interval=0.01) as gw:
            status, headers, body = gw.fetch("/")
            self.assertEqual(status, 200)
            self.assertIn(b"<html", body.lower())

            with gw.watch() as ws:
                layout = ws.wait_for(lambda w: w.of_type("layout"))
                self.assertTrue(layout, "no layout frame")
                self.assertTrue(ws.wait_for(lambda w: len(w.of_type("status")) >= 3),
                                "no live status frames")
                robot_frames = ws.wait_for(lambda w: w.of_type("robot"))
                self.assertEqual(robot_frames[-1]["recording"], "on", robot_frames[-1])

                def saved(_):
                    # The first run's file, once it holds some 90 records past its XML.
                    status, _h, body = gw.fetch("/log/runs")
                    if status != 200 or not json.loads(body):
                        return None
                    run = json.loads(body)[0]["run"]
                    status, _h, data = gw.fetch(f"/log.btlog?run={run}")
                    return (status, data) if status == 200 and len(data) > 3000 else None
                status, data = ws.wait_for(saved, timeout=15) or (None, b"")
        self.assertEqual(status, 200)
        log = btlog_ref.parse(data)
        self.assertEqual(log.xml, mock_robot.CROSSDOOR_XML)
        self.assertEqual(log.trailing, 0)
        self.assertGreater(len(log.records), 20)
        uids = btlog_ref.tree_uids(log.xml)
        self.assertTrue({uid for _t, uid, _s in log.records} <= set(uids))


if __name__ == "__main__":
    unittest.main()
