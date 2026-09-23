"""Compatibility wrapper for the original MVP command.

Legacy usage:
    python pi_runner.py run <task-id>
"""

from __future__ import annotations

import sys
from pathlib import Path

from pi_delegate.cli import main as delegate_main


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "run":
        print("usage: python pi_runner.py run <task-id>", file=sys.stderr)
        return 2
    task_id = sys.argv[2]
    task = Path("runs") / task_id / "TASK.md"
    return delegate_main(["run", str(task), "--project", "."])


if __name__ == "__main__":
    raise SystemExit(main())

