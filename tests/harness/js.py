"""Run DOM-free JavaScript against JSON vectors with gjs (see tests/js/run.js)."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.harness.targets import REPO_ROOT

RUNNER = REPO_ROOT / "tests" / "js" / "run.js"


def require_gjs():
    """The gjs executable, or ``unittest.SkipTest`` when it is not installed."""
    gjs = shutil.which("gjs")
    if gjs is None:
        raise unittest.SkipTest("gjs (GNOME JavaScript) not installed; JS model tests need it")
    return gjs


def run_js_vectors(vectors_path, timeout=60):
    """Run one vector file; returns ``(exit_status, output)``.

    Exit 0 = every case passed, 1 = a case failed, 2 = could not load.
    """
    result = subprocess.run([require_gjs(), "-m", str(RUNNER), str(vectors_path)],
                            capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT))
    return result.returncode, result.stdout + result.stderr


def assert_vectors_pass(test, data):
    """Run the vectors ``data`` (a dict, as ``run.js`` reads it) and fail
    ``test`` unless every one of its cases passed."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vectors.json"
        path.write_text(json.dumps(data))
        code, out = run_js_vectors(path)
    n = len(data["cases"])
    test.assertEqual(code, 0, "\n".join(l for l in out.splitlines() if not l.startswith("ok"))[:4000])
    test.assertIn(f"# {n}/{n} passed", out)
