"""The dashboard in a real browser. ``DashboardCase`` is the shared set-up."""
import unittest

from tests.harness.probes import BrowserProbe, GatewayProbe
from tests.harness.targets import MockTarget


class DashboardCase(unittest.TestCase):
    """A robot, a gateway and a browser for the class, set by class options:

    * ``ROBOT``: ``ROBOT_CLASS(**ROBOT)`` (a ``MockTarget`` by default), or
      ``None`` for no robot;
    * ``GATEWAY``: ``GatewayProbe`` options, against the robot (or a port
      nothing answers on), or ``None`` for no gateway; ``make_gateway`` builds
      it, for a class that needs another one;
    * ``VIEWPORT``, and ``OPEN``: how many cards to wait for on opening the
      dashboard fresh (``BrowserProbe.open(fresh=True)``), or ``None`` to
      leave the page blank.

    Each one stops with the class, even when ``setUpClass`` fails half way.
    Every test fails on an uncaught page error.
    """

    ROBOT = {}
    ROBOT_CLASS = MockTarget
    GATEWAY = {}
    VIEWPORT = (1400, 900)
    OPEN = 13

    @classmethod
    def setUpClass(cls):
        cls.robot = cls.gw = None
        if cls.ROBOT is not None:
            cls.robot = cls.ROBOT_CLASS(**cls.ROBOT)
            cls.addClassCleanup(cls.robot.stop)
            cls.robot.start()
        if cls.GATEWAY is not None:
            cls.gw = cls.make_gateway(cls.robot.port if cls.robot else 1)
            cls.addClassCleanup(cls.gw.stop)
            cls.gw.start()
        cls.browser = BrowserProbe(cls.gw.url if cls.gw else "", viewport=cls.VIEWPORT)
        cls.addClassCleanup(cls.browser.stop)
        cls.browser.start()
        cls.page = cls.browser.page
        if cls.OPEN is not None:
            cls.browser.open(wait_nodes=cls.OPEN, fresh=True)

    @classmethod
    def make_gateway(cls, robot_port):
        """The class's gateway, not yet started."""
        return GatewayProbe(robot_port, **cls.GATEWAY)

    def tearDown(self):
        errors = self.browser.page_errors()
        self.assertFalse(errors, errors)

    @classmethod
    def frame(cls):
        """Wait until the page has painted its next frame."""
        cls.browser.next_frame()
