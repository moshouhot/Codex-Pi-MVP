from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pi_delegate import cli, core, forensics


def _make_run(root: Path, name: str) -> tuple[Path, Path]:
    run_dir = root / "runs" / name
    run_dir.mkdir(parents=True)
    task = run_dir / "TASK.md"
    task.write_text("task", encoding="utf-8")
    return run_dir, task


@contextlib.contextmanager
def _fake_pi(root: Path, script_body: str, run_dir: Path, extra_env: dict | None = None):
    """Run a real Python child in place of the Node Pi CLI."""
    script = root / "fake_pi.py"
    script.write_text(script_body, encoding="utf-8")
    env = {"FAKE_RUN_DIR": str(run_dir), "PYTHONDONTWRITEBYTECODE": "1"}
    if extra_env:
        env.update(extra_env)
    with mock.patch.object(core, "discover_pi_cli", return_value=script), mock.patch.object(
        core.shutil, "which", return_value=sys.executable
    ), mock.patch.dict(os.environ, env):
        yield script


def _wait_for_text(path: Path, text: str, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if text in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


SUCCESS_SCRIPT = """
import json, os, pathlib, sys
run_dir = pathlib.Path(os.environ["FAKE_RUN_DIR"])
if "--mode" not in sys.argv or sys.argv[sys.argv.index("--mode") + 1] != "json":
    sys.exit(8)
stdin_data = sys.stdin.read()
if stdin_data != "":
    sys.exit(9)
(run_dir / "RESULT.json").write_text(
    json.dumps({"task_id": "case", "status": "completed"}), encoding="utf-8"
)
(run_dir / "REPORT.md").write_text("ok", encoding="utf-8")
sys.stdout.write("done\\n")
sys.stdout.flush()
sys.stderr.write("note\\n")
sys.stderr.flush()
"""

JSON_EVENT_SCRIPT = """
import json, os, pathlib, sys, time
run_dir = pathlib.Path(os.environ["FAKE_RUN_DIR"])
events = [
    {"type": "session", "version": 3, "id": "test", "timestamp": "now", "cwd": str(run_dir)},
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "message_update", "usage": {}, "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "thinking"}},
    {"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "bash", "args": {"command": "echo ok"}},
    {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "bash", "result": {}, "isError": False},
]
for event in events:
    print(json.dumps(event), flush=True)
    time.sleep(0.05)
(run_dir / "RESULT.json").write_text(json.dumps({"task_id": "case-json"}), encoding="utf-8")
(run_dir / "REPORT.md").write_text("ok", encoding="utf-8")
print(json.dumps({"type": "agent_settled"}), flush=True)
"""

STREAMING_SCRIPT = """
import os, pathlib, sys, time
run_dir = pathlib.Path(os.environ["FAKE_RUN_DIR"])
sentinel = pathlib.Path(os.environ["FAKE_SENTINEL"])
sys.stdout.write("early-partial")
sys.stdout.flush()
deadline = time.time() + 8
while not sentinel.exists():
    if time.time() > deadline:
        sys.exit(7)
    time.sleep(0.05)
(run_dir / "RESULT.json").write_text('{"task_id": "case"}', encoding="utf-8")
(run_dir / "REPORT.md").write_text("ok", encoding="utf-8")
sys.stdout.write("\\nlate\\n")
sys.stdout.flush()
"""

STDERR_ACTIVITY_SCRIPT = """
import json, os, pathlib, sys, time
run_dir = pathlib.Path(os.environ["FAKE_RUN_DIR"])
for _ in range(6):
    sys.stderr.write("tick\\n")
    sys.stderr.flush()
    time.sleep(0.3)
(run_dir / "RESULT.json").write_text(
    json.dumps({"task_id": "case", "status": "completed"}), encoding="utf-8"
)
(run_dir / "REPORT.md").write_text("ok", encoding="utf-8")
"""

IDLE_SCRIPT = """
import sys, time
sys.stdout.write("start\\n")
sys.stdout.flush()
time.sleep(30)
"""

BUSY_SCRIPT = """
import sys, time
end = time.time() + 30
while time.time() < end:
    sys.stdout.write("tick\\n")
    sys.stdout.flush()
    time.sleep(0.1)
"""

OBSERVABILITY_SCRIPT = """
import json, os, pathlib, sys, time
run_dir = pathlib.Path(os.environ["FAKE_RUN_DIR"])
for _ in range(8):
    sys.stdout.write("beat\\n")
    sys.stdout.flush()
    time.sleep(0.2)
(run_dir / "RESULT.json").write_text(
    json.dumps({"task_id": "case", "status": "completed"}), encoding="utf-8"
)
(run_dir / "REPORT.md").write_text("ok", encoding="utf-8")
"""


class ResolveTaskTests(unittest.TestCase):
    def test_resolve_task_inside_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "abc-001")
            resolved, task_id, resolved_run = core.resolve_task(root, task)
            self.assertEqual(task_id, "abc-001")
            self.assertEqual(resolved, task.resolve())
            self.assertEqual(resolved_run, run_dir.resolve())

    def test_rejects_task_outside_project(self):
        with tempfile.TemporaryDirectory() as project_td, tempfile.TemporaryDirectory() as other_td:
            root = Path(project_td)
            task = Path(other_td) / "abc" / "TASK.md"
            task.parent.mkdir()
            task.write_text("task", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside project root"):
                core.resolve_task(root, task)


class TimeoutResolutionTests(unittest.TestCase):
    def test_defaults(self):
        idle, hard = core.resolve_timeouts()
        self.assertEqual(idle, 300)
        self.assertEqual(hard, 3600)
        self.assertEqual(core.DEFAULT_TIMEOUT_SECONDS, 3600)

    def test_legacy_timeout_is_hard_alias(self):
        idle, hard = core.resolve_timeouts(timeout_seconds=240)
        self.assertEqual(idle, 300)
        self.assertEqual(hard, 240)

    def test_legacy_timeout_overrides_explicit_hard(self):
        idle, hard = core.resolve_timeouts(
            timeout_seconds=120, idle_timeout_seconds=10, hard_timeout_seconds=999
        )
        self.assertEqual(idle, 10)
        self.assertEqual(hard, 120)

    def test_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            core.resolve_timeouts(idle_timeout_seconds=0)
        with self.assertRaises(ValueError):
            core.resolve_timeouts(hard_timeout_seconds=0)


class DetachedLaunchTests(unittest.TestCase):
    def test_launch_task_returns_promptly_with_detached_supervisor_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-start")
            fake_proc = mock.Mock(pid=9876)

            with mock.patch.dict(
                os.environ,
                {forensics.FORENSICS_INVOCATION_ENV_VAR: "inherited-invocation"},
            ), mock.patch.object(
                core.subprocess, "Popen", return_value=fake_proc
            ) as popen:
                payload = core.launch_task(
                    project_root=root,
                    task_file=task,
                    idle_timeout_seconds=11,
                    hard_timeout_seconds=22,
                    provider="provider-x",
                    model="model-y",
                    thinking="high",
                )

            self.assertEqual(payload["launch_status"], "STARTED")
            self.assertEqual(payload["supervisor_pid"], 9876)
            self.assertEqual(payload["launcher_pid"], os.getpid())
            self.assertNotEqual(payload["supervisor_invocation_id"], "inherited-invocation")
            self.assertEqual(payload["idle_timeout_seconds"], 11)
            self.assertEqual(payload["hard_timeout_seconds"], 22)
            command = popen.call_args.args[0]
            self.assertEqual(command[:4], [sys.executable, "-m", "pi_delegate", "run"])
            self.assertIn("--no-tee", command)
            self.assertEqual(command[command.index("--idle-timeout") + 1], "11")
            self.assertEqual(command[command.index("--hard-timeout") + 1], "22")
            self.assertEqual(command[command.index("--provider") + 1], "provider-x")
            self.assertEqual(command[command.index("--model") + 1], "model-y")
            self.assertEqual(command[command.index("--thinking") + 1], "high")
            kwargs = popen.call_args.kwargs
            self.assertIs(kwargs["stdin"], core.subprocess.DEVNULL)
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(
                kwargs["env"][forensics.FORENSICS_START_INVOCATION_ENV_VAR],
                payload["supervisor_invocation_id"],
            )
            self.assertNotIn(forensics.FORENSICS_INVOCATION_ENV_VAR, kwargs["env"])
            if os.name == "nt":
                self.assertIn("creationflags", kwargs)
                self.assertEqual(
                    payload["process_creation_flags"], kwargs["creationflags"]
                )
                self.assertTrue(payload["detached_process"])
                self.assertTrue(payload["new_process_group"])
                self.assertNotIn("start_new_session", kwargs)
            else:
                self.assertTrue(kwargs["start_new_session"])
                self.assertEqual(payload["process_creation_flags"], 0)
                self.assertTrue(payload["new_process_group"])
            self.assertTrue((run_dir / "supervisor.stdout.txt").is_file())
            self.assertTrue((run_dir / "supervisor.stderr.txt").is_file())
            launch_records = [
                json.loads(line)
                for line in (run_dir / forensics.FORENSICS_FILE_NAME)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            event = next(r for r in launch_records if r["event"] == "launcher_spawn_observed")
            self.assertEqual(event["source"], "launcher")
            self.assertEqual(event["pid"], os.getpid())
            self.assertEqual(event["launcher_ppid"], os.getppid())
            self.assertEqual(event["supervisor_pid"], 9876)
            self.assertEqual(event["invocation"], payload["supervisor_invocation_id"])

    def test_launch_task_refuses_existing_running_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-running")
            core.write_state(run_dir, "case-running", "RUNNING", worker_pid=123)
            with self.assertRaisesRegex(ValueError, "already reports RUNNING"):
                core.launch_task(project_root=root, task_file=task)

    def test_cli_parses_start_and_run_no_tee(self):
        start_args = cli.build_parser().parse_args(
            ["start", "TASK.md", "--idle-timeout", "300", "--hard-timeout", "3600"]
        )
        self.assertEqual(start_args.command, "start")
        self.assertEqual(start_args.idle_timeout, 300)
        self.assertEqual(start_args.hard_timeout, 3600)
        run_args = cli.build_parser().parse_args(["run", "TASK.md", "--no-tee"])
        self.assertTrue(run_args.no_tee)

    def test_cli_consumes_start_invocation_once(self):
        import io

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, task = _make_run(root, "case-cli-invocation")
            token = "one-shot-supervisor-id"
            with mock.patch.dict(
                os.environ,
                {forensics.FORENSICS_START_INVOCATION_ENV_VAR: token},
            ), mock.patch.object(
                cli, "run_task", return_value=(0, {"runner_status": "COMPLETED"})
            ) as run_task, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(["run", str(task), "--project", str(root)]), 0
                )
                self.assertNotIn(
                    forensics.FORENSICS_START_INVOCATION_ENV_VAR, os.environ
                )
                self.assertEqual(
                    run_task.call_args.kwargs["forensics_invocation_id"], token
                )


class LogTailTests(unittest.TestCase):
    def test_tail_text_is_bounded_to_requested_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stdout.txt"
            path.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n", encoding="utf-8")
            tail = core.tail_text(path, lines=3, max_bytes=4096)
            self.assertEqual(tail.splitlines(), ["line-97", "line-98", "line-99"])

    def test_tail_text_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(core.tail_text(Path(td) / "missing.txt"), "")

    def test_cli_result_keeps_runner_evidence_when_worker_result_is_missing(self):
        import io

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            core.write_json(
                run_dir / "RUN_RESULT.json",
                {"task_id": "timeout-case", "runner_status": "TIMEOUT", "timeout_kind": "idle"},
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["result", str(run_dir)])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["runner"]["runner_status"], "TIMEOUT")
            self.assertIsNone(payload["worker"])


class SupervisorTests(unittest.TestCase):
    def test_successful_completion_and_devnull_stdin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-001")
            with _fake_pi(root, SUCCESS_SCRIPT, run_dir):
                code, result = core.run_task(project_root=root, task_file=task, tee=False)
            self.assertEqual(code, 0)
            self.assertEqual(result["runner_status"], "COMPLETED")
            self.assertEqual(result["timeout_kind"], None)
            self.assertEqual(result["idle_timeout_seconds"], 300)
            self.assertEqual(result["hard_timeout_seconds"], 3600)
            self.assertTrue(result["worker_result_valid"])
            self.assertTrue((run_dir / "REPORT.md").is_file())
            self.assertEqual((run_dir / "stdout.txt").read_text(encoding="utf-8"), "done\n")
            self.assertEqual((run_dir / "stderr.txt").read_text(encoding="utf-8"), "note\n")
            state = core.read_json(run_dir / "RUN_STATE.json")
            self.assertEqual(state["status"], "COMPLETED")
            self.assertEqual(state["forensics_invocation_id"], result["forensics_invocation_id"])
            self.assertEqual(state["supervisor_pid"], os.getpid())
            self.assertEqual(state["supervisor_ppid"], os.getppid())
            self.assertEqual(result["supervisor_pid"], os.getpid())

    def test_run_uses_launcher_invocation_and_records_supervisor_lineage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-invocation")
            invocation_id = "launcher-invocation-id"
            with _fake_pi(root, SUCCESS_SCRIPT, run_dir), mock.patch.dict(
                os.environ,
                {forensics.FORENSICS_INVOCATION_ENV_VAR: "guard-parent-id"},
            ):
                _, result = core.run_task(
                    project_root=root,
                    task_file=task,
                    tee=False,
                    forensics_invocation_id=invocation_id,
                )

            records = [
                json.loads(line)
                for line in (run_dir / forensics.FORENSICS_FILE_NAME)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(records)
            self.assertEqual({r["invocation"] for r in records}, {invocation_id})
            start = next(r for r in records if r["event"] == "supervisor_start")
            spawn = next(r for r in records if r["event"] == "supervisor_spawn_observed")
            self.assertEqual(start["pid"], os.getpid())
            self.assertEqual(start["invocation"], invocation_id)
            self.assertEqual(start["supervisor_ppid"], os.getppid())
            self.assertEqual(spawn["supervisor_pid"], os.getpid())
            self.assertEqual(spawn["supervisor_ppid"], os.getppid())
            self.assertEqual(spawn["worker_ppid"], os.getpid())
            self.assertEqual(result["supervisor_ppid"], os.getppid())
            self.assertEqual(result["forensics_invocation_id"], invocation_id)
            self.assertEqual(
                core.read_json(run_dir / "RUN_STATE.json")["forensics_invocation_id"],
                invocation_id,
            )
            self.assertTrue(
                any(r["event"] == "supervisor_result_written" for r in records)
            )

    def test_repeated_direct_runs_do_not_reuse_guard_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-fresh-invocations")
            with _fake_pi(root, SUCCESS_SCRIPT, run_dir), mock.patch.dict(
                os.environ,
                {forensics.FORENSICS_INVOCATION_ENV_VAR: "guard-parent-id"},
            ):
                _, first = core.run_task(project_root=root, task_file=task, tee=False)
                _, second = core.run_task(project_root=root, task_file=task, tee=False)

            self.assertNotEqual(
                first["forensics_invocation_id"], second["forensics_invocation_id"]
            )
            self.assertNotEqual(
                first["forensics_invocation_id"], "guard-parent-id"
            )
            self.assertNotEqual(
                second["forensics_invocation_id"], "guard-parent-id"
            )
            records = [
                json.loads(line)
                for line in (run_dir / forensics.FORENSICS_FILE_NAME)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                {r["invocation"] for r in records},
                {first["forensics_invocation_id"], second["forensics_invocation_id"]},
            )

    def test_stdout_streams_before_process_exit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-stream")
            sentinel = root / "sentinel.txt"
            stdout_file = run_dir / "stdout.txt"
            seen = threading.Event()

            def monitor() -> None:
                if _wait_for_text(stdout_file, "early-partial"):
                    seen.set()
                    sentinel.write_text("go", encoding="utf-8")

            watcher = threading.Thread(target=monitor, daemon=True)
            watcher.start()
            with _fake_pi(root, STREAMING_SCRIPT, run_dir, {"FAKE_SENTINEL": str(sentinel)}):
                code, result = core.run_task(project_root=root, task_file=task, tee=False)
            watcher.join(timeout=5)
            self.assertTrue(seen.is_set(), "partial stdout was not visible before exit")
            self.assertEqual(code, 0)
            self.assertEqual(result["runner_status"], "COMPLETED")
            self.assertIn("early-partial", stdout_file.read_text(encoding="utf-8"))

    def test_stderr_activity_resets_idle_watchdog(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-stderr")
            with _fake_pi(root, STDERR_ACTIVITY_SCRIPT, run_dir):
                code, result = core.run_task(
                    project_root=root,
                    task_file=task,
                    idle_timeout_seconds=1,
                    hard_timeout_seconds=30,
                    tee=False,
                )
            self.assertEqual(code, 0)
            self.assertEqual(result["runner_status"], "COMPLETED")
            self.assertEqual(result["timeout_kind"], None)

    def test_idle_timeout_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-idle")
            with _fake_pi(root, IDLE_SCRIPT, run_dir):
                code, result = core.run_task(
                    project_root=root,
                    task_file=task,
                    idle_timeout_seconds=1,
                    hard_timeout_seconds=30,
                    tee=False,
                    terminate_grace_seconds=0.5,
                )
            self.assertEqual(code, 124)
            self.assertEqual(result["runner_status"], "TIMEOUT")
            self.assertEqual(result["timeout_kind"], "idle")
            self.assertGreaterEqual(result["idle_seconds"], 1)
            state = core.read_json(run_dir / "RUN_STATE.json")
            self.assertEqual(state["status"], "TIMEOUT")
            self.assertEqual(state["timeout_kind"], "idle")
            persisted = core.read_json(run_dir / "RUN_RESULT.json")
            self.assertEqual(persisted["timeout_kind"], "idle")

    def test_hard_timeout_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-hard")
            with _fake_pi(root, BUSY_SCRIPT, run_dir):
                code, result = core.run_task(
                    project_root=root,
                    task_file=task,
                    idle_timeout_seconds=5,
                    hard_timeout_seconds=1,
                    tee=False,
                    terminate_grace_seconds=0.5,
                )
            self.assertEqual(code, 124)
            self.assertEqual(result["runner_status"], "TIMEOUT")
            self.assertEqual(result["timeout_kind"], "hard")
            state = core.read_json(run_dir / "RUN_STATE.json")
            self.assertEqual(state["timeout_kind"], "hard")
            self.assertEqual(core.read_json(run_dir / "RUN_RESULT.json")["timeout_kind"], "hard")

    def test_legacy_timeout_alias_applies_to_hard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-alias")
            with _fake_pi(root, BUSY_SCRIPT, run_dir):
                code, result = core.run_task(
                    project_root=root,
                    task_file=task,
                    timeout_seconds=1,
                    tee=False,
                    terminate_grace_seconds=0.5,
                )
            self.assertEqual(code, 124)
            self.assertEqual(result["timeout_kind"], "hard")
            self.assertEqual(result["hard_timeout_seconds"], 1)
            self.assertEqual(result["idle_timeout_seconds"], 300)

    def test_live_run_state_observability(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-observe")
            snapshots: list[dict] = []
            stop = threading.Event()

            def watch() -> None:
                while not stop.is_set():
                    try:
                        state = core.read_json(run_dir / "RUN_STATE.json")
                    except (OSError, ValueError, json.JSONDecodeError):
                        state = None
                    if state and state.get("status") == "RUNNING" and state.get("worker_pid"):
                        snapshots.append(state)
                    time.sleep(0.05)

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            with _fake_pi(root, OBSERVABILITY_SCRIPT, run_dir):
                code, result = core.run_task(project_root=root, task_file=task, tee=False)
            stop.set()
            watcher.join(timeout=5)

            self.assertEqual(code, 0)
            self.assertTrue(snapshots, "no live RUN_STATE snapshots captured while running")
            required = {
                "worker_pid",
                "supervisor_pid",
                "supervisor_ppid",
                "forensics_invocation_id",
                "elapsed_seconds",
                "idle_seconds",
                "last_activity_at",
                "idle_timeout_seconds",
                "hard_timeout_seconds",
            }
            self.assertTrue(required <= set(snapshots[0]), snapshots[0])
            self.assertEqual(result["worker_pid"], snapshots[0]["worker_pid"])

    def test_json_event_stream_updates_structured_progress(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-json")
            with _fake_pi(root, JSON_EVENT_SCRIPT, run_dir), mock.patch.object(
                core, "STATE_PERSIST_INTERVAL_SECONDS", 0.05
            ):
                code, result = core.run_task(project_root=root, task_file=task, tee=False)
            self.assertEqual(code, 0)
            self.assertEqual(result["last_activity_source"], "pi_json_event")
            self.assertEqual(result["last_event_type"], "agent_settled")
            self.assertEqual(result["last_tool_name"], "bash")
            self.assertEqual(result["last_progress"], "agent_settled")
            raw_lines = (run_dir / "stdout.txt").read_text(encoding="utf-8").splitlines()
            self.assertTrue(raw_lines)
            self.assertEqual(json.loads(raw_lines[0])["type"], "session")
            state = core.read_json(run_dir / "RUN_STATE.json")
            self.assertEqual(state["last_event_type"], "agent_settled")

    def test_tee_surfaces_live_output(self):
        import io

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-tee")
            buffer = io.StringIO()
            with _fake_pi(root, SUCCESS_SCRIPT, run_dir), contextlib.redirect_stdout(buffer):
                code, _ = core.run_task(project_root=root, task_file=task, tee=True)
            self.assertEqual(code, 0)
            self.assertIn("done", buffer.getvalue())

    def test_unresponsive_process_cleanup_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-mock")
            fake_pi = root / "cli.js"
            fake_pi.write_text("// fake", encoding="utf-8")

            r_out, w_out = os.pipe()
            r_err, w_err = os.pipe()
            os.close(w_out)
            os.close(w_err)
            stdout_pipe = os.fdopen(r_out, "rb")
            stderr_pipe = os.fdopen(r_err, "rb")

            proc = mock.Mock()
            proc.pid = 4242
            proc.poll.return_value = None
            proc.returncode = None
            proc.stdout = stdout_pipe
            proc.stderr = stderr_pipe
            proc.wait.side_effect = core.subprocess.TimeoutExpired(cmd="pi", timeout=1)

            with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
                core.shutil, "which", return_value="node"
            ), mock.patch.object(core.subprocess, "Popen", return_value=proc):
                started = time.monotonic()
                code, result = core.run_task(
                    project_root=root,
                    task_file=task,
                    idle_timeout_seconds=1,
                    hard_timeout_seconds=30,
                    tee=False,
                    terminate_grace_seconds=0.1,
                )
                elapsed = time.monotonic() - started

            self.assertEqual(code, 124)
            self.assertEqual(result["timeout_kind"], "idle")
            self.assertLess(elapsed, 5.0)
            proc.terminate.assert_called_once()
            proc.kill.assert_called_once()


class ExitForensicsUnitTests(unittest.TestCase):
    def test_format_exit_code_normalizes_windows_unsigned_minus_one(self):
        info = forensics.format_exit_code(4294967295)
        self.assertTrue(info["normalized"])
        self.assertEqual(info["unsigned_32"], 4294967295)
        self.assertEqual(info["signed_32"], -1)
        self.assertEqual(info["hex"], "0xFFFFFFFF")

    def test_format_exit_code_passes_through_non_integers(self):
        self.assertFalse(forensics.format_exit_code(None)["normalized"])
        self.assertFalse(forensics.format_exit_code("boom")["normalized"])
        # bool is not treated as an exit code
        self.assertFalse(forensics.format_exit_code(True)["normalized"])

    def test_is_node_executable_rejects_python_and_accepts_node(self):
        self.assertTrue(forensics.is_node_executable("node"))
        self.assertTrue(forensics.is_node_executable("C:/Program Files/nodejs/node.exe"))
        self.assertTrue(forensics.is_node_executable("nodejs"))
        self.assertFalse(forensics.is_node_executable(sys.executable))
        self.assertFalse(forensics.is_node_executable("python.exe"))
        self.assertFalse(forensics.is_node_executable(None))

    def test_build_preload_env_skips_fake_pi_harness(self):
        env: dict = {}
        active = forensics.build_preload_env(env, Path("x.jsonl"), sys.executable)
        self.assertFalse(active)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn(forensics.FORENSICS_ENV_VAR, env)

    def test_build_preload_env_quotes_space_containing_guard_path(self):
        with tempfile.TemporaryDirectory() as td:
            guard = Path(td) / "sub dir" / "exit_forensics_guard.cjs"
            guard.parent.mkdir(parents=True)
            guard.write_text("// guard", encoding="utf-8")
            with mock.patch.object(forensics, "guard_path", return_value=guard):
                env: dict = {}
                active = forensics.build_preload_env(env, Path(td) / "fx.jsonl", "node")
        self.assertTrue(active)
        self.assertIn("--require \"", env["NODE_OPTIONS"])
        self.assertNotIn("\\", env["NODE_OPTIONS"])
        self.assertEqual(env[forensics.FORENSICS_ENV_VAR], str(Path(td) / "fx.jsonl"))

    def test_build_preload_env_preserves_existing_node_options(self):
        env = {"NODE_OPTIONS": "--max-old-space-size=512"}
        active = forensics.build_preload_env(env, Path("x.jsonl"), "node")
        self.assertTrue(active)
        self.assertTrue(env["NODE_OPTIONS"].startswith("--max-old-space-size=512"))
        self.assertIn("--require", env["NODE_OPTIONS"])

    def test_classify_abrupt_is_cautious_not_proven_external(self):
        records = [
            {"event": "guard_start", "source": "guard"},
            {"event": "supervisor_spawn_observed", "source": "supervisor"},
            {"event": "supervisor_worker_exit_observed", "source": "supervisor"},
        ]
        report = forensics.build_forensics_report(
            returncode=4294967295, timeout_kind=None, records=records
        )
        self.assertEqual(report["termination_classification"], forensics.CLASS_ABRUPT)
        reason = " ".join(report["classification_reasons"])
        self.assertIn("does not", reason)
        self.assertIn("prove", reason)
        self.assertFalse(report["supervisor_terminate_observed"])
        self.assertFalse(report["js_exit_requested"])

    def test_classify_js_exit_wins_over_unsigned_code(self):
        records = [
            {"event": "guard_start", "source": "guard"},
            {"event": "js_process_exit", "source": "guard", "code": -1},
        ]
        report = forensics.build_forensics_report(
            returncode=4294967295, timeout_kind=None, records=records
        )
        self.assertEqual(report["termination_classification"], forensics.CLASS_JS_REQUESTED_EXIT)
        self.assertTrue(report["js_exit_requested"])
        self.assertEqual(report["js_exit_code"], -1)

    def test_classify_abrupt_requires_guard_startup_evidence(self):
        records = [{"event": "supervisor_spawn_observed", "source": "supervisor"}]
        report = forensics.build_forensics_report(
            returncode=4294967295, timeout_kind=None, records=records
        )
        self.assertEqual(report["termination_classification"], forensics.CLASS_INDETERMINATE)
        self.assertFalse(report["guard_started"])

    def test_classify_terminal_guard_evidence_blocks_abrupt_label(self):
        records = [
            {"event": "guard_start", "source": "guard"},
            {"event": "exit_event", "source": "guard", "code": -1},
        ]
        report = forensics.build_forensics_report(
            returncode=4294967295, timeout_kind=None, records=records
        )
        self.assertTrue(report["terminal_guard_evidence"])
        self.assertEqual(report["termination_classification"], forensics.CLASS_ERROR_EXIT)

    def test_before_exit_alone_does_not_block_abrupt_label(self):
        # A beforeExit handler may schedule more work; the process can still be
        # killed afterwards, so beforeExit is not terminal evidence.
        records = [
            {"event": "guard_start", "source": "guard"},
            {"event": "before_exit", "source": "guard", "code": 0},
        ]
        report = forensics.build_forensics_report(
            returncode=4294967295, timeout_kind=None, records=records
        )
        self.assertFalse(report["terminal_guard_evidence"])
        self.assertEqual(report["termination_classification"], forensics.CLASS_ABRUPT)

    def test_foreign_guard_records_from_child_pids_are_ignored(self):
        # A Node child spawned by the worker inherits NODE_OPTIONS; its own guard
        # records must not classify the parent worker.
        records = [
            {"event": "guard_start", "source": "guard", "pid": 100},
            {"event": "js_process_exit", "source": "guard", "pid": 999, "code": 0},
            {"event": "uncaught_exception", "source": "guard", "pid": 999},
        ]
        report = forensics.build_forensics_report(
            returncode=4294967295, timeout_kind=None, records=records, worker_pid=100
        )
        self.assertTrue(report["guard_started"])
        self.assertFalse(report["js_exit_requested"])
        self.assertFalse(report["uncaught_exception_observed"])
        self.assertEqual(report["foreign_guard_records_ignored"], 2)
        self.assertEqual(report["termination_classification"], forensics.CLASS_ABRUPT)

    def test_supervisor_termination_attempt_without_timeout_is_indeterminate(self):
        # A recorded attempt does not prove signal delivery or causation.
        records = [
            {"event": "guard_start", "source": "guard"},
            {
                "event": "supervisor_terminate_attempt",
                "source": "supervisor",
                "termination_cause": "supervisor_exception",
            },
        ]
        report = forensics.build_forensics_report(
            returncode=4294967295, timeout_kind=None, records=records
        )
        self.assertEqual(
            report["termination_classification"], forensics.CLASS_INDETERMINATE
        )
        self.assertTrue(report["supervisor_terminate_observed"])
        self.assertEqual(
            report["supervisor_termination_attempts"][0]["termination_cause"],
            "supervisor_exception",
        )
        self.assertIn("does not prove", " ".join(report["classification_reasons"]))

    def test_classify_timeout_termination(self):
        records = [
            {"event": "guard_start", "source": "guard"},
            {"event": "supervisor_terminate_attempt", "source": "supervisor"},
        ]
        report = forensics.build_forensics_report(
            returncode=None, timeout_kind="idle", records=records
        )
        self.assertEqual(report["termination_classification"], forensics.CLASS_SUPERVISOR_TIMEOUT)
        self.assertTrue(report["supervisor_terminate_observed"])

    def test_classify_normal_completion(self):
        records = [
            {"event": "guard_start", "source": "guard"},
            {"event": "js_process_exit", "source": "guard", "code": 0},
        ]
        report = forensics.build_forensics_report(
            returncode=0, timeout_kind=None, records=records
        )
        self.assertEqual(report["termination_classification"], forensics.CLASS_NORMAL_COMPLETION)

    def test_recorder_events_are_timestamped(self):
        with tempfile.TemporaryDirectory() as td:
            recorder = forensics.ForensicsRecorder(Path(td))
            recorder.append("supervisor_spawn_observed", worker_pid=1)
            records = recorder.read_records()
        self.assertEqual(len(records), 1)
        self.assertIn("ts", records[0])
        self.assertEqual(records[0]["source"], "supervisor")
        self.assertEqual(records[0]["invocation"], recorder.invocation_id)

    def test_recorder_does_not_delete_prior_invocation_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            first = forensics.ForensicsRecorder(Path(td))
            first.append("supervisor_spawn_observed", worker_pid=1)
            second = forensics.ForensicsRecorder(Path(td))
            second.append("supervisor_spawn_observed", worker_pid=2)
            self.assertEqual(len(second.read_all_records()), 2)
            self.assertEqual(len(second.read_records()), 1)
            self.assertEqual(second.read_records()[0]["worker_pid"], 2)
            self.assertEqual(len(first.read_records()), 1)
            self.assertEqual(first.read_records()[0]["worker_pid"], 1)

    def test_terminate_process_records_attempt_even_when_signal_raises(self):
        actions: list[str] = []
        proc = mock.Mock()
        proc.pid = 1
        proc.poll.return_value = None
        proc.terminate.side_effect = OSError("boom")
        proc.kill.side_effect = OSError("boom")
        proc.wait.side_effect = core.subprocess.TimeoutExpired(cmd="pi", timeout=1)
        core._terminate_process(proc, grace_seconds=0.01, on_action=actions.append)
        self.assertEqual(actions, ["terminate", "kill"])

    def test_terminate_process_skips_recording_when_already_exited(self):
        actions: list[str] = []
        proc = mock.Mock()
        proc.poll.return_value = 0
        core._terminate_process(proc, grace_seconds=0.01, on_action=actions.append)
        self.assertEqual(actions, [])
        proc.terminate.assert_not_called()

    def test_guard_asset_exists_and_avoids_forbidden_metadata(self):
        guard = forensics.guard_path()
        self.assertTrue(guard.is_file(), f"missing packaged guard: {guard}")
        source = guard.read_text(encoding="utf-8")
        # The allowlist forbids recording executable paths, script names and
        # argv. Assert the identifiers never reappear in the guard.
        self.assertNotIn("exec_path", source)
        self.assertNotIn("main_script", source)
        self.assertNotIn("process.argv", source)

    def test_pyproject_declares_guard_as_package_data(self):
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        self.assertIn("[tool.setuptools.package-data]", text)
        self.assertIn("assets/*.cjs", text)


class ExitForensicsIntegrationTests(unittest.TestCase):
    def test_fake_harness_emits_supervisor_evidence_without_guard_preload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-fx-fake")
            with _fake_pi(root, SUCCESS_SCRIPT, run_dir):
                code, result = core.run_task(project_root=root, task_file=task, tee=False)
            self.assertEqual(code, 0)
            self.assertEqual(result["runner_status"], "COMPLETED")
            self.assertIsNone(result["timeout_kind"])
            self.assertEqual(result["pi_exit_code"], 0)
            diagnostics = result["exit_forensics"]
            self.assertFalse(diagnostics["preload_active"])
            self.assertFalse(diagnostics["guard_started"])
            self.assertEqual(diagnostics["termination_classification"], forensics.CLASS_NORMAL_COMPLETION)
            events = [
                json.loads(line)["event"]
                for line in (run_dir / forensics.FORENSICS_FILE_NAME)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertIn("supervisor_spawn_observed", events)
            self.assertIn("supervisor_worker_exit_observed", events)
            # guard must not be preloaded into the Python fake harness
            self.assertNotIn("guard_start", events)

    def test_supervisor_evidence_metadata_stays_within_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-fx-allowlist")
            with _fake_pi(root, SUCCESS_SCRIPT, run_dir):
                core.run_task(project_root=root, task_file=task, tee=False)
            records = [
                json.loads(line)
                for line in (run_dir / forensics.FORENSICS_FILE_NAME)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            spawn = [r for r in records if r["event"] == "supervisor_spawn_observed"]
            self.assertTrue(spawn)
            forbidden = {"node", "command", "argv", "prompt", "env", "environment"}
            for record in records:
                self.assertFalse(
                    forbidden & set(record),
                    f"forbidden metadata keys present: {forbidden & set(record)}",
                )
            # The allowlisted facts that should remain available:
            self.assertEqual(spawn[0]["preload_active"], False)
            self.assertIn("worker_pid", spawn[0])
            self.assertIn("ts", spawn[0])

    def test_timeout_forensics_records_terminate_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-fx-timeout")
            with _fake_pi(root, IDLE_SCRIPT, run_dir):
                code, result = core.run_task(
                    project_root=root,
                    task_file=task,
                    idle_timeout_seconds=1,
                    hard_timeout_seconds=30,
                    tee=False,
                    terminate_grace_seconds=0.5,
                )
            self.assertEqual(code, 124)
            self.assertEqual(result["runner_status"], "TIMEOUT")
            self.assertEqual(result["timeout_kind"], "idle")
            self.assertIsNone(result["pi_exit_code"])
            diagnostics = result["exit_forensics"]
            self.assertTrue(diagnostics["supervisor_terminate_observed"])
            self.assertEqual(
                diagnostics["termination_classification"],
                forensics.CLASS_SUPERVISOR_TIMEOUT,
            )
            persisted = core.read_json(run_dir / "RUN_RESULT.json")
            self.assertEqual(persisted["timeout_kind"], "idle")
            self.assertIn("exit_forensics", persisted)

    def test_supervisor_exception_records_cause_and_rethrows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-fx-exception")
            with _fake_pi(root, IDLE_SCRIPT, run_dir), mock.patch.object(
                core.time, "sleep", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    core.run_task(
                        project_root=root,
                        task_file=task,
                        tee=False,
                        terminate_grace_seconds=0.5,
                    )

            records = [
                json.loads(line)
                for line in (run_dir / forensics.FORENSICS_FILE_NAME)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            exception = next(
                r for r in records if r["event"] == "supervisor_exception_observed"
            )
            attempt = next(
                r for r in records if r["event"] == "supervisor_terminate_attempt"
            )
            self.assertEqual(exception["exception_type"], "KeyboardInterrupt")
            self.assertEqual(attempt["termination_cause"], "supervisor_exception")
            self.assertEqual(attempt["exception_type"], "KeyboardInterrupt")
            self.assertIsNone(attempt["timeout_kind"])
            self.assertFalse(
                any(r["event"] == "supervisor_worker_exit_observed" for r in records)
            )
            self.assertFalse((run_dir / "RUN_RESULT.json").exists())

    def test_exception_immediately_after_spawn_keeps_cleanup_callback_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-fx-early-exception")
            real_popen = core.subprocess.Popen
            launched = []

            def capture_popen(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                launched.append(child)
                return child

            with _fake_pi(root, IDLE_SCRIPT, run_dir), mock.patch.object(
                core._ActivityClock, "touch", side_effect=RuntimeError("trigger")
            ), mock.patch.object(
                core.subprocess, "Popen", side_effect=capture_popen
            ):
                with self.assertRaisesRegex(RuntimeError, "trigger"):
                    core.run_task(
                        project_root=root,
                        task_file=task,
                        tee=False,
                        terminate_grace_seconds=0.5,
                    )

            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].poll(), "spawned worker was not cleaned up")
            records = [
                json.loads(line)
                for line in (run_dir / forensics.FORENSICS_FILE_NAME)
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(
                any(
                    r["event"] == "supervisor_terminate_attempt"
                    and r["termination_cause"] == "supervisor_exception"
                    for r in records
                )
            )

    def test_frozen_contract_fields_unchanged_by_forensics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-fx-contract")
            with _fake_pi(root, SUCCESS_SCRIPT, run_dir):
                _, result = core.run_task(project_root=root, task_file=task, tee=False)
            for key in (
                "runner_status",
                "timeout_kind",
                "pi_exit_code",
                "worker_pid",
                "duration_seconds",
                "report_exists",
                "worker_result_exists",
                "worker_result_valid",
            ):
                self.assertIn(key, result)
            self.assertEqual(result["runner_status"], "COMPLETED")
            self.assertIsNone(result["timeout_kind"])
            self.assertEqual(result["pi_exit_code"], 0)


@unittest.skipUnless(
    shutil.which("node") and forensics.guard_path().is_file(),
    "real Node runtime and packaged guard are required",
)
class ExitForensicsRealNodeTests(unittest.TestCase):
    """Smoke tests against a genuine Node worker.

    These assert that the preload guard loads, observes the exit path, and does
    not alter the observed return code or the runner contract.
    """

    _SCRIPT = (
        "const fs = require('fs');\n"
        "const path = require('path');\n"
        "const runDir = process.env.FAKE_RUN_DIR;\n"
        "fs.writeFileSync(path.join(runDir,'RESULT.json'), "
        "JSON.stringify({task_id:'c',status:'completed'}));\n"
        "fs.writeFileSync(path.join(runDir,'REPORT.md'), 'ok');\n"
        "process.stdout.write('done' + String.fromCharCode(10));\n"
        "process.exit(Number(process.env.FAKE_EXIT_CODE));\n"
    )

    def _run(self, root: Path, exit_code: str):
        run_dir, task = _make_run(root, f"case-node-{exit_code.replace('-', 'm')}")
        script = root / "fake_cli.js"
        script.write_text(self._SCRIPT, encoding="utf-8")
        env = {"FAKE_RUN_DIR": str(run_dir), "FAKE_EXIT_CODE": exit_code}
        node = shutil.which("node")
        with mock.patch.object(core, "discover_pi_cli", return_value=script), mock.patch.object(
            core.shutil, "which", return_value=node
        ), mock.patch.dict(os.environ, env):
            return core.run_task(project_root=root, task_file=task, tee=False), run_dir

    def test_guard_observes_normal_exit_without_changing_code(self):
        with tempfile.TemporaryDirectory() as td:
            (code, result), run_dir = self._run(Path(td), "0")
        self.assertEqual(code, 0)
        self.assertEqual(result["pi_exit_code"], 0)
        diagnostics = result["exit_forensics"]
        self.assertTrue(diagnostics["preload_active"])
        self.assertTrue(diagnostics["guard_started"])
        self.assertTrue(diagnostics["js_exit_requested"])
        self.assertEqual(diagnostics["termination_classification"], forensics.CLASS_NORMAL_COMPLETION)

    def test_guard_distinguishes_js_exit_minus_one(self):
        with tempfile.TemporaryDirectory() as td:
            (code, result), run_dir = self._run(Path(td), "-1")
        self.assertEqual(result["runner_status"], "FAILED")
        # The OS truncates the code differently per platform: Windows preserves
        # 0xFFFFFFFF, POSIX keeps the low byte (255). The guard-requested code is
        # the platform-independent truth.
        expected = 4294967295 if os.name == "nt" else 255
        self.assertEqual(result["pi_exit_code"], expected)
        diagnostics = result["exit_forensics"]
        self.assertTrue(diagnostics["guard_started"])
        self.assertTrue(diagnostics["js_exit_requested"])
        self.assertEqual(diagnostics["js_exit_code"], -1)
        self.assertEqual(
            diagnostics["termination_classification"], forensics.CLASS_JS_REQUESTED_EXIT
        )
        self.assertEqual(
            diagnostics["worker_exit_code_normalized"]["signed_32"],
            -1 if os.name == "nt" else 255,
        )
        self.assertNotEqual(
            diagnostics["termination_classification"], forensics.CLASS_ABRUPT
        )

    def test_guard_records_uncaught_exception(self):
        script = (
            "const fs=require('fs'); const path=require('path');\n"
            "fs.writeFileSync(path.join(process.env.FAKE_RUN_DIR,'RESULT.json'),'{}');\n"
            "throw new Error('boom-from-worker');\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-node-uncaught")
            cli_script = root / "fake_cli.js"
            cli_script.write_text(script, encoding="utf-8")
            node = shutil.which("node")
            with mock.patch.object(core, "discover_pi_cli", return_value=cli_script), mock.patch.object(
                core.shutil, "which", return_value=node
            ), mock.patch.dict(os.environ, {"FAKE_RUN_DIR": str(run_dir)}):
                _, result = core.run_task(project_root=root, task_file=task, tee=False)
        diagnostics = result["exit_forensics"]
        self.assertTrue(diagnostics["guard_started"])
        self.assertTrue(diagnostics["uncaught_exception_observed"])
        self.assertFalse(diagnostics["js_exit_requested"])
        self.assertEqual(
            diagnostics["termination_classification"], forensics.CLASS_UNCAUGHT_EXCEPTION
        )

    def test_guard_observes_ordinary_error_exit_one(self):
        with tempfile.TemporaryDirectory() as td:
            (code, result), run_dir = self._run(Path(td), "1")
        self.assertEqual(result["pi_exit_code"], 1)
        diagnostics = result["exit_forensics"]
        self.assertTrue(diagnostics["guard_started"])
        # exit(1) is a JS-requested exit; classification must reflect that, not a
        # generic error, because the guard observed the request.
        self.assertEqual(
            diagnostics["termination_classification"], forensics.CLASS_JS_REQUESTED_EXIT
        )
        self.assertEqual(diagnostics["js_exit_code"], 1)

    def test_guard_records_abrupt_termination_cautiously(self):
        script = (
            "const fs=require('fs'); const path=require('path');\n"
            "fs.writeFileSync(path.join(process.env.FAKE_RUN_DIR,'RESULT.json'),'{}');\n"
            "process.kill(process.pid, 'SIGKILL');\n"
            "setTimeout(()=>{}, 5000);\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir, task = _make_run(root, "case-node-abrupt")
            cli_script = root / "fake_cli.js"
            cli_script.write_text(script, encoding="utf-8")
            node = shutil.which("node")
            with mock.patch.object(core, "discover_pi_cli", return_value=cli_script), mock.patch.object(
                core.shutil, "which", return_value=node
            ), mock.patch.dict(os.environ, {"FAKE_RUN_DIR": str(run_dir)}):
                _, result = core.run_task(project_root=root, task_file=task, tee=False)
        diagnostics = result["exit_forensics"]
        self.assertTrue(diagnostics["guard_started"])
        self.assertFalse(diagnostics["js_exit_requested"])
        self.assertFalse(diagnostics["terminal_guard_evidence"])
        self.assertFalse(diagnostics["supervisor_terminate_observed"])
        # Windows cannot portably produce 0xFFFFFFFF here, so accept any cautious
        # label, but never a self/external claim that the evidence cannot support.
        self.assertIn(
            diagnostics["termination_classification"],
            {
                forensics.CLASS_ABRUPT,
                forensics.CLASS_ERROR_EXIT,
                forensics.CLASS_INDETERMINATE,
            },
        )
        reason = " ".join(diagnostics["classification_reasons"]).lower()
        self.assertNotIn("proven", reason)
        self.assertNotIn("external killer", reason)


class DoctorTests(unittest.TestCase):
    def test_ready_reason_code(self):
        fake_pi = Path("C:/fake/pi/cli.js")
        proc = mock.Mock(returncode=0, stdout="0.86.1\n", stderr="")
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(core.shutil, "which", return_value="node"), mock.patch.object(
            core.subprocess, "run", return_value=proc
        ):
            code, result = core.doctor()
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason_code"], "READY")
        self.assertFalse(result["reinstall_pi_delegate_recommended"])

    def test_node_missing_reason_code(self):
        with mock.patch.object(core.shutil, "which", return_value=None):
            code, result = core.doctor()
        self.assertEqual(code, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "NODE_NOT_FOUND")
        self.assertFalse(result["reinstall_pi_delegate_recommended"])

    def test_pi_cli_missing_reason_code(self):
        fake_pi = Path("C:/missing/pi/cli.js")
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=False
        ), mock.patch.object(core.shutil, "which", return_value="node"):
            code, result = core.doctor()
        self.assertEqual(code, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "PI_CLI_NOT_FOUND")

    def test_pi_version_failure_reason_code(self):
        fake_pi = Path("C:/fake/pi/cli.js")
        proc = mock.Mock(returncode=7, stdout="", stderr="boom")
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(core.shutil, "which", return_value="node"), mock.patch.object(
            core.subprocess, "run", return_value=proc
        ):
            code, result = core.doctor()
        self.assertEqual(code, 4)
        self.assertEqual(result["reason_code"], "PI_VERSION_FAILED")

    def test_pi_version_timeout_reason_code(self):
        fake_pi = Path("C:/fake/pi/cli.js")
        timeout = core.subprocess.TimeoutExpired(cmd="pi", timeout=10)
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(core.shutil, "which", return_value="node"), mock.patch.object(
            core.subprocess, "run", side_effect=timeout
        ):
            code, result = core.doctor()
        self.assertEqual(code, 124)
        self.assertEqual(result["reason_code"], "PI_VERSION_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
