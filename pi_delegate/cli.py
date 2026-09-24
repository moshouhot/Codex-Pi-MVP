from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (
    DEFAULT_HARD_TIMEOUT_SECONDS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    doctor,
    launch_task,
    read_json,
    run_task,
    tail_text,
)


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-delegate")
    parser.add_argument("--version", action="version", version="pi-delegate 0.3.0")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="delegate a TASK.md to Pi and wait for completion")
    run.add_argument("task", type=Path, help="TASK.md path, relative to --project or absolute")
    run.add_argument("--project", type=Path, default=Path.cwd(), help="target project root")
    run.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "legacy alias for --hard-timeout (total wall-clock ceiling in seconds); "
            "overrides --hard-timeout when both are given"
        ),
    )
    run.add_argument(
        "--idle-timeout",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT_SECONDS,
        help="seconds without worker output before the worker is treated as stuck",
    )
    run.add_argument(
        "--hard-timeout",
        type=int,
        default=DEFAULT_HARD_TIMEOUT_SECONDS,
        help="absolute wall-clock safety ceiling in seconds",
    )
    run.add_argument("--provider")
    run.add_argument("--model")
    run.add_argument("--thinking")
    run.add_argument(
        "--no-tee",
        action="store_true",
        help="do not mirror worker stdout/stderr to the supervising console",
    )

    start = sub.add_parser(
        "start",
        help="launch a detached supervisor and return immediately (recommended for Web Codex)",
    )
    start.add_argument("task", type=Path, help="TASK.md path, relative to --project or absolute")
    start.add_argument("--project", type=Path, default=Path.cwd(), help="target project root")
    start.add_argument("--timeout", type=int, default=None, help="legacy hard-timeout alias")
    start.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    start.add_argument("--hard-timeout", type=int, default=DEFAULT_HARD_TIMEOUT_SECONDS)
    start.add_argument("--provider")
    start.add_argument("--model")
    start.add_argument("--thinking")

    status = sub.add_parser("status", help="show RUN_STATE.json from a run directory")
    status.add_argument("run_dir", type=Path)

    result = sub.add_parser("result", help="show runner and worker result JSON")
    result.add_argument("run_dir", type=Path)

    logs = sub.add_parser("logs", help="show a bounded tail of worker stdout/stderr")
    logs.add_argument("run_dir", type=Path)
    logs.add_argument("--tail", type=int, default=50, help="number of lines per stream")
    logs.add_argument(
        "--stream",
        choices=("stdout", "stderr", "both"),
        default="both",
    )

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
                idle_timeout_seconds=args.idle_timeout,
                hard_timeout_seconds=args.hard_timeout,
                provider=args.provider,
                model=args.model,
                thinking=args.thinking,
                tee=not args.no_tee,
            )
            _print(payload)
            return code
        if args.command == "start":
            payload = launch_task(
                project_root=args.project,
                task_file=args.task,
                timeout_seconds=args.timeout,
                idle_timeout_seconds=args.idle_timeout,
                hard_timeout_seconds=args.hard_timeout,
                provider=args.provider,
                model=args.model,
                thinking=args.thinking,
            )
            _print(payload)
            return 0
        if args.command == "logs":
            payload: dict[str, object] = {"run_dir": str(args.run_dir)}
            if args.stream in ("stdout", "both"):
                payload["stdout"] = tail_text(args.run_dir / "stdout.txt", lines=args.tail)
            if args.stream in ("stderr", "both"):
                payload["stderr"] = tail_text(args.run_dir / "stderr.txt", lines=args.tail)
            _print(payload)
            return 0
        if args.command == "doctor":
            code, payload = doctor()
            _print(payload)
            return code
        if args.command == "status":
            _print(read_json(args.run_dir / "RUN_STATE.json"))
            return 0
        if args.command == "result":
            worker_path = args.run_dir / "RESULT.json"
            payload = {
                "runner": read_json(args.run_dir / "RUN_RESULT.json"),
                "worker": read_json(worker_path) if worker_path.is_file() else None,
            }
            _print(payload)
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"pi-delegate: {exc}", file=sys.stderr)
        return 2
    return 2
