from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pi_delegate import cli, core


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

            with mock.patch.object(core.subprocess, "Popen", return_value=fake_proc) as popen:
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
            if os.name == "nt":
                self.assertIn("creationflags", kwargs)
                self.assertNotIn("start_new_session", kwargs)
            else:
                self.assertTrue(kwargs["start_new_session"])
            self.assertTrue((run_dir / "supervisor.stdout.txt").is_file())
            self.assertTrue((run_dir / "supervisor.stderr.txt").is_file())

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
