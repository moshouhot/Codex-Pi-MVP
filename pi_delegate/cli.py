from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import DEFAULT_TIMEOUT_SECONDS, doctor, read_json, run_task


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-delegate")
    parser.add_argument("--version", action="version", version="pi-delegate 0.2.0")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="delegate a TASK.md to Pi and wait for completion")
    run.add_argument("task", type=Path, help="TASK.md path, relative to --project or absolute")
    run.add_argument("--project", type=Path, default=Path.cwd(), help="target project root")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--provider")
    run.add_argument("--model")
    run.add_argument("--thinking")

    status = sub.add_parser("status", help="show RUN_STATE.json from a run directory")
    status.add_argument("run_dir", type=Path)

    result = sub.add_parser("result", help="show runner and worker result JSON")
    result.add_argument("run_dir", type=Path)

    sub.add_parser("doctor", help="check Node, Pi CLI, version and default worker settings")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            code, payload = run_task(
                project_root=args.project,
                task_file=args.task,
                timeout_seconds=args.timeout,
                provider=args.provider,
                model=args.model,
                thinking=args.thinking,
            )
            _print(payload)
            return code
        if args.command == "doctor":
            code, payload = doctor()
            _print(payload)
            return code
        if args.command == "status":
            _print(read_json(args.run_dir / "RUN_STATE.json"))
            return 0
        if args.command == "result":
            payload = {
                "runner": read_json(args.run_dir / "RUN_RESULT.json"),
                "worker": read_json(args.run_dir / "RESULT.json"),
            }
            _print(payload)
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"pi-delegate: {exc}", file=sys.stderr)
        return 2
    return 2

