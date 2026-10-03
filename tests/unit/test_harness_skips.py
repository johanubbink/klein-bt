"""Targets and probes that need an optional dependency skip, never fail, without it."""
import os
import sys
import unittest
from unittest import mock

from tests.harness.probes import BrowserProbe
from tests.harness.targets import T11Target


class OptionalDepsTest(unittest.TestCase):
    def test_missing_dependency_raises_skip(self):
        with self.subTest("BehaviorTree.CPP build"):
            with mock.patch.dict(os.environ,
                                 {"KLEIN_BTCPP_DIR": "/nonexistent/BehaviorTree.CPP"}):
                target = T11Target()
                try:
                    with self.assertRaises(unittest.SkipTest) as ctx:
                        target.start()
                finally:
                    target.cleanup()
            self.assertIn("BehaviorTree.CPP build incomplete", str(ctx.exception))
        with self.subTest("Playwright"):
            with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
                with self.assertRaises(unittest.SkipTest) as ctx:
                    BrowserProbe("http://127.0.0.1:1").start()
            self.assertIn("Playwright not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
