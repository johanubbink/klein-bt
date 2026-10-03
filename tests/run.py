"""Run the test suite in parallel, one test class per task.

    python tests/run.py                  # every tier, -j os.cpu_count()
    python tests/run.py --tier ci        # what CI runs: tests/unit + tests/smoke
    python tests/run.py tests/ui         # one tier (a directory) ...
    python tests/run.py -j 4 tests.ui.test_log   # ... or some modules or classes
    python tests/run.py -v               # also print what each class printed

Finds the same tests as ``python -m unittest discover -s tests -t .``. Most of
the suite's time is spent waiting on robots, gateways and browsers, so classes
run side by side in worker processes; ``setUpClass`` still runs once per class.
The few classes in ``ALONE`` run after the rest, one at a time. Stdlib only.
"""
import argparse
import contextlib
import io
import os
import sys
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The tiers are directories: unit (in-process), smoke (one end-to-end run),
# integration (real robots and gateways), ui (a real browser).
TIERS = {
    "ci": ["tests/unit", "tests/smoke"],
    "all": ["tests"],
}

# Real-time checks that fail when other classes load the machine. They run
# after the rest, one at a time: even side by side they flaked (a 6.8 ms
# lag, a short t11 recording in about 1 run in 4).
ALONE = {
    # t11's mission runs on its own clock: a gateway slow to start arms late
    # and records too little of it.
    "tests.integration.test_gateway_recording.T11RecordingTest",
    # The outage must start within one 5 ms poll of the last answered drain.
    "tests.integration.test_gateway_recording.MockRecordingTest",
}


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def class_names(names):
    """The test classes to run, as importable dotted names, in discovery order."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in names:
        if (REPO_ROOT / name).is_dir():
            suite.addTest(loader.discover(name, top_level_dir=str(REPO_ROOT)))
        else:
            suite.addTest(loader.loadTestsFromName(name))
    found = {}
    for test in _iter_tests(suite):
        if isinstance(test, unittest.loader._FailedTest):
            name = test._testMethodName         # the module; loading it again reports the error
        else:
            name = f"{type(test).__module__}.{type(test).__qualname__}"
        found.setdefault(name)
    return list(found)


def run_class(name):
    """Worker: run one class, return a picklable summary of its result."""
    out = io.StringIO()
    start = time.monotonic()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        suite = unittest.TestLoader().loadTestsFromName(name)
        result = unittest.TestResult()
        suite.run(result)
    return {
        "name": name,
        "time": time.monotonic() - start,
        "tests": result.testsRun,
        "failures": [(str(t), tb) for t, tb in result.failures],
        "errors": [(str(t), tb) for t, tb in result.errors],
        "skipped": [(str(t), reason) for t, reason in result.skipped],
        "unexpected": [str(t) for t in result.unexpectedSuccesses],
        "output": out.getvalue(),
    }


def _init_worker():
    os.chdir(REPO_ROOT)
    sys.path.insert(0, str(REPO_ROOT))


def _prebuild_t11():
    """Build t11 once up front, so parallel classes never race to compile it."""
    try:
        from tests.harness.targets import build_t11
        build_t11()
    except unittest.SkipTest:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("names", nargs="*",
                        help="directories, modules or classes (default: the --tier)")
    parser.add_argument("--tier", choices=sorted(TIERS), default="all")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each class's output as it finishes")
    args = parser.parse_args(argv)
    _init_worker()

    start = time.monotonic()
    classes = class_names(args.names or TIERS[args.tier])
    if not args.names and args.tier == "all":
        for name in sorted(ALONE - set(classes)):     # renamed or deleted: fix ALONE
            print(f"warning: ALONE names {name}, which no longer exists", flush=True)
    _prebuild_t11()
    results = []

    def run_all(names, jobs):
        # spawn, not fork: the parent has imported every test module (zmq included).
        with ProcessPoolExecutor(jobs, mp_context=get_context("spawn"),
                                 initializer=_init_worker) as pool:
            futures = {pool.submit(run_class, name): name for name in names}
            for future in as_completed(futures):
                try:
                    r = future.result()
                except Exception as e:      # the worker died (os._exit, a crash)
                    r = {"name": futures[future], "time": 0.0, "tests": 0, "failures": [],
                         "errors": [(futures[future], f"worker died: {e!r}\n")],
                         "skipped": [], "unexpected": [], "output": ""}
                results.append(r)
                bad = r["failures"] or r["errors"] or r["unexpected"]
                print(f"{'FAIL' if bad else 'ok  '} {r['time']:6.1f}s  {r['name']} "
                      f"({r['tests']}){'  [alone]' if r['name'] in ALONE else ''}", flush=True)
                if args.verbose and r["output"].strip():
                    print("    " + r["output"].strip().replace("\n", "\n    "), flush=True)

    run_all([c for c in classes if c not in ALONE], args.jobs)
    run_all([c for c in classes if c in ALONE], 1)
    wall = time.monotonic() - start

    def every(key):
        return [item for r in results for item in r[key]]

    for kind in ("failures", "errors"):
        for test, tb in every(kind):
            print(f"\n{'=' * 70}\n{kind[:-1].upper()}: {test}\n{'-' * 70}\n{tb}")
    for r in results:
        if (r["failures"] or r["errors"]) and r["output"].strip() and not args.verbose:
            print(f"\n--- output of {r['name']} ---\n{r['output'].rstrip()}")
    skipped = every("skipped")
    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for test, reason in skipped:
            print(f"  {test}: {reason}")

    print("\nSlowest classes:")
    for r in sorted(results, key=lambda r: -r["time"])[:8]:
        print(f"  {r['time']:6.1f}s  {r['name']}")
    total = sum(r["tests"] for r in results)
    failures, errors, unexpected = every("failures"), every("errors"), every("unexpected")
    print(f"\nRan {total} tests in {len(results)} classes in {wall:.1f}s with {args.jobs} jobs: "
          f"{len(failures)} failures, {len(errors)} errors, {len(skipped)} skipped"
          + (f", {len(unexpected)} unexpected successes" if unexpected else ""))
    ok = total and not (failures or errors or unexpected)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
