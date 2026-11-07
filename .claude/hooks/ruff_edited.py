"""PostToolUse hook: ruff-check a Python file Claude just edited, with the
project's rules (pyproject.toml). Findings go back to Claude (exit 2); a
missing ruff or a non-Python file is a silent no-op."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_ruff(root):
    for venv in (root / ".venv", root.parent / ".venv"):
        candidate = venv / "bin" / "ruff"
        if candidate.exists():
            return str(candidate)
    return shutil.which("ruff")


def main():
    event = json.load(sys.stdin)
    path = (event.get("tool_input") or {}).get("file_path", "")
    if not path.endswith(".py") or not Path(path).exists():
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    ruff = find_ruff(root)
    if not ruff:
        return 0
    result = subprocess.run([ruff, "check", "--output-format", "concise", path],
                            cwd=root, capture_output=True, text=True)
    if result.returncode == 0:
        return 0
    print(f"ruff found problems in {path} (fix them, or say why not):\n"
          f"{result.stdout.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
