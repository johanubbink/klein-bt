"""The browser-side models, run under gjs against JSON vectors.

First the runner itself: that it *fails* — with a non-zero exit — on wrong
expectations (Maps and Sets included), unknown functions and cases with
nothing to check. Then the dashboard's recording mirror (``recording.js``,
``cursor.js``) against the vectors ``tests/make_vectors.py`` generates from the
Python model. Everything skips when gjs is missing.
"""
import unittest
from pathlib import Path

from tests import make_vectors
from tests.harness.js import assert_vectors_pass, run_js_vectors

JS_DIR = Path(__file__).resolve().parents[1] / "js"


class JsRunnerSelfTest(unittest.TestCase):
    def test_known_bad_vectors_fail(self):
        code, out = run_js_vectors(JS_DIR / "selftest_bad_vectors.json")
        self.assertEqual(code, 1, out)
        self.assertIn("# 1/6 passed", out)
        self.assertIn('expected {"3":0}', out)          # wrong value
        self.assertIn("is not a function", out)          # unknown function
        self.assertIn('expected {}\n     got [[11,1]]', out)   # a Map is not {}
        self.assertIn('expected [4]\n     got [11]', out)       # a Set is its values
        self.assertIn('no "expect"', out)                 # a case that checks nothing


class ReplayVectorsTest(unittest.TestCase):
    def test_the_browser_model_answers_as_the_python_model(self):
        data = make_vectors.vectors()
        self.assertGreater(len(data["cases"]), 1000)
        assert_vectors_pass(self, data)


if __name__ == "__main__":
    unittest.main()
