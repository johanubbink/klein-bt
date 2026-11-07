"""Lint and complexity gate for klein: ruff on the Python, cognitive complexity
(complexipy) on klein's own Python, cyclomatic complexity (lizard) on the
dashboard's JS. Tools come from ``pip install -e '.[dev]'``; see
docs/testing.md, "Lint and complexity".

    .venv/bin/python scripts/check.py                 # ruff + hotspot report
    .venv/bin/python scripts/check.py --diff main     # gate a branch against main

Without ``--diff`` it fails only on ruff and lists the functions over the
limits (hotspots: candidates for a refactor, not failures). With ``--diff
REF`` the complexity limits are a ratchet on the changed files: a function
over its limit fails if it is new or got more complex than at the merge base
with REF. Functions that got simpler are listed too.
"""
import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PY_COGNITIVE_MAX = 15   # complexipy's default
JS_CCN_MAX = 15

LINT_PATHS = ["klein", "tests", "scripts"]
PY_COMPLEXITY_DIRS = ("klein/", "scripts/")      # tests are long on purpose
JS_DIR = "klein/static/"
JS_EXCLUDE = ("d3.",)                             # vendored


def tool(name):
    """The tool beside this interpreter (the venv's), else on PATH."""
    local = Path(sys.executable).parent / name
    found = str(local) if local.exists() else shutil.which(name)
    if not found:
        sys.exit(f"check.py: {name} not found; pip install -e '.[dev]'")
    return found


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def is_py(path):
    return path.endswith(".py") and path.startswith(PY_COMPLEXITY_DIRS)


def is_js(path):
    name = path.rsplit("/", 1)[-1]
    return path.startswith(JS_DIR) and path.endswith(".js") and not name.startswith(JS_EXCLUDE)


def scores(paths, cwd):
    """{(path, function): score} for the given repo-relative paths under cwd;
    Python by cognitive complexity, JS by CCN. Same-named functions in a file
    (JS anonymous ones) keep their highest score."""
    out = {}

    def keep(key, score):
        out[key] = max(score, out.get(key, 0))

    py = [p for p in paths if is_py(p) and (cwd / p).exists()]
    js = [p for p in paths if is_js(p) and (cwd / p).exists()]
    if py:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "c.json"
            subprocess.run([tool("complexipy"), *py, "--output-format", "json",
                            "--output", str(report), "-q", "-i"],
                           cwd=cwd, check=True, capture_output=True)
            for row in json.loads(report.read_text()):
                keep((row["path"], row["function_name"]), row["complexity"])
    if js:
        text = subprocess.run([tool("lizard"), "--csv", *js], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout
        for row in csv.reader(io.StringIO(text)):
            keep((row[6], row[7]), int(row[1]))
    return out


def limit(path):
    return PY_COGNITIVE_MAX if path.endswith(".py") else JS_CCN_MAX


def over(table):
    return {k: v for k, v in table.items() if v > limit(k[0])}


def ruff():
    result = subprocess.run([tool("ruff"), "check", *LINT_PATHS], cwd=ROOT)
    return result.returncode == 0


def hotspots():
    paths = git("ls-files", "klein", "scripts").split()
    found = sorted(over(scores(paths, ROOT)).items(), key=lambda kv: -kv[1])
    print(f"\nComplexity hotspots (Python cognitive > {PY_COGNITIVE_MAX}, "
          f"JS CCN > {JS_CCN_MAX}): {len(found)}")
    for (path, name), score in found:
        print(f"  {score:3d}  {path}  {name}")


def changed_files(base):
    tracked = git("diff", "--name-only", base).split()
    untracked = git("ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked + untracked))


def scores_at(commit, paths):
    """scores() of the paths as they were at commit (new files: absent)."""
    with tempfile.TemporaryDirectory() as tmp:
        old_root = Path(tmp)
        for p in paths:
            shown = subprocess.run(["git", "show", f"{commit}:{p}"], cwd=ROOT,
                                   capture_output=True)
            if shown.returncode == 0:
                (old_root / p).parent.mkdir(parents=True, exist_ok=True)
                (old_root / p).write_bytes(shown.stdout)
        return scores(paths, old_root)


def ratchet(ref):
    """Fail on changed functions over a limit that are new or got worse."""
    base = git("merge-base", ref, "HEAD").strip()
    paths = [p for p in changed_files(base) if is_py(p) or is_js(p)]
    old, new = scores_at(base, paths), scores(paths, ROOT)
    worse = {k: v for k, v in over(new).items() if v > old.get(k, 0)}
    better = {k: (old[k], v) for k, v in new.items() if k in old and v < old[k]}
    print(f"\nComplexity ratchet vs {ref} (merge base {base[:8]}), "
          f"{len(paths)} changed source file(s):")
    for (path, name), score in sorted(worse.items(), key=lambda kv: -kv[1]):
        was = old.get((path, name))
        how = "new" if was is None else f"was {was}"
        print(f"  FAIL {score:3d} > {limit(path)}  {path}  {name}  ({how})")
    for (path, name), (was, now) in sorted(better.items()):
        print(f"  better  {was} -> {now}  {path}  {name}")
    if not worse:
        print("  ok: no changed function is over its limit and worse than before")
    return not worse


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--diff", metavar="REF",
                        help="gate the complexity of files changed since the merge base with REF")
    args = parser.parse_args()
    ok = ruff()
    if args.diff:
        ok = ratchet(args.diff) and ok
    else:
        hotspots()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
