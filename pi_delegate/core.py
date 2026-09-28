from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import forensics


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_IDLE_TIMEOUT_SECONDS = 300
DEFAULT_HARD_TIMEOUT_SECONDS = 3600
# Backward-compatible alias: older callers passed a single total timeout.
DEFAULT_TIMEOUT_SECONDS = DEFAULT_HARD_TIMEOUT_SECONDS

# How often the supervisor checks for idle/hard timeout and refreshes RUN_STATE.
POLL_INTERVAL_SECONDS = 0.1
STATE_PERSIST_INTERVAL_SECONDS = 5.0
READER_JOIN_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 5.0
_READ_CHUNK = 65536


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def discover_pi_cli() -> Path:
    override = os.environ.get("PI_DELEGATE_PI_CLI")
    if override:
        return Path(override).expanduser().resolve()
    return (
        Path.home()
        / "AppData/Roaming/npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    )


def load_pi_defaults() -> dict[str, Any]:
    settings = Path.home() / ".pi/agent/settings.json"
    if not settings.is_file():
        return {}
    try:
        data = read_json(settings)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {
        "provider": data.get("defaultProvider"),
        "model": data.get("defaultModel"),
        "thinking": data.get("defaultThinkingLevel"),
    }


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_task(project_root: Path, task_file: Path) -> tuple[Path, str, Path]:
    project_root = project_root.expanduser().resolve()
    task_file = task_file.expanduser()
    if not task_file.is_absolute():
        task_file = project_root / task_file
    task_file = task_file.resolve()

    if not project_root.is_dir():
        raise ValueError(f"project root not found: {project_root}")
    if not task_file.is_file():
        raise ValueError(f"task file not found: {task_file}")
    if not _inside(task_file, project_root):
        raise ValueError("task file must be inside project root")

    task_id = task_file.parent.name
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"invalid task id derived from run directory: {task_id}")
    return task_file, task_id, task_file.parent


def resolve_timeouts(
    timeout_seconds: int | None = None,
    idle_timeout_seconds: int | None = None,
    hard_timeout_seconds: int | None = None,
) -> tuple[int, int]:
    """Resolve effective (idle, hard) timeout limits.

    The legacy ``timeout_seconds`` argument is an alias/override for the hard
    timeout so existing callers keep a total-duration ceiling.
    """
    idle = (
        DEFAULT_IDLE_TIMEOUT_SECONDS
        if idle_timeout_seconds is None
        else idle_timeout_seconds
    )
    if timeout_seconds is not None:
        hard = timeout_seconds
    elif hard_timeout_seconds is not None:
        hard = hard_timeout_seconds
    else:
        hard = DEFAULT_HARD_TIMEOUT_SECONDS
    if idle < 1:
        raise ValueError("idle timeout must be >= 1 second")
    if hard < 1:
        raise ValueError("hard timeout must be >= 1 second")
    return idle, hard


def write_state(run_dir: Path, task_id: str, status: str, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "updated_at": utc_now(),
    }
    payload.update(extra)
    write_json(run_dir / "RUN_STATE.json", payload)


class _ActivityClock:
    """Thread-safe record of the last time the worker produced output."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_monotonic = time.monotonic()
        self._last_wall = utc_now()
        self._last_source = "start"
        self._last_event_type: str | None = None
        self._last_tool_name: str | None = None
        self._last_progress = "worker starting"

    def touch(
        self,
        *,
        source: str = "stream",
        event_type: str | None = None,
        tool_name: str | None = None,
        progress: str | None = None,
    ) -> None:
        with self._lock:
            self._last_monotonic = time.monotonic()
            self._last_wall = utc_now()
            self._last_source = source
            if event_type is not None:
                self._last_event_type = event_type
            if tool_name is not None:
                self._last_tool_name = tool_name
            if progress is not None:
                self._last_progress = progress

    def snapshot(self) -> tuple[float, str]:
        with self._lock:
            return self._last_monotonic, self._last_wall

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_monotonic

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_activity_source": self._last_source,
                "last_event_type": self._last_event_type,
                "last_tool_name": self._last_tool_name,
                "last_progress": self._last_progress,
            }


def _pump_stream(
    read_fd: int,
    sink_path: Path,
    label: str,
    clock: _ActivityClock,
    tee: bool,
) -> None:
    """Stream a worker pipe to disk (and optionally upward) until EOF.

    Activity is recorded as soon as raw bytes are received, before any
    decoding or tee work, so partial UTF-8 and newline-less output still
    reset the idle watchdog.
    """
    try:
        sink = open(sink_path, "wb")
    except OSError:
        sink = None
    try:
        while True:
            try:
                chunk = os.read(read_fd, _READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            clock.touch(source=label, progress=f"{label} activity")
            if sink is not None:
                try:
                    sink.write(chunk)
                    sink.flush()
                except OSError:
                    pass
            if tee:
                try:
                    text = chunk.decode("utf-8", "replace")
                    stream = sys.stdout if label == "stdout" else sys.stderr
                    stream.write(text)
                    stream.flush()
                except Exception:
                    # Tee failures must never stop log capture.
                    pass
    finally:
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass


def _event_details(event: dict[str, Any]) -> tuple[str, str | None, str]:
    event_type = str(event.get("type") or "json_event")
    tool_name = event.get("toolName") if isinstance(event.get("toolName"), str) else None
    progress = event_type
    if event_type == "message_update":
        nested = event.get("assistantMessageEvent")
        if isinstance(nested, dict):
            nested_type = str(nested.get("type") or "update")
            progress = f"message:{nested_type}"
            nested_tool = nested.get("toolName")
            if isinstance(nested_tool, str):
                tool_name = nested_tool
    elif event_type.startswith("tool_execution_"):
        progress = f"{event_type}:{tool_name or 'tool'}"
    elif event_type == "auto_retry_start":
        progress = f"auto_retry:{event.get('attempt', '?')}"
    elif event_type == "compaction_start":
        progress = f"compaction:{event.get('reason', 'unknown')}"
    return event_type, tool_name, progress


def _tee_json_event(event: dict[str, Any]) -> None:
    """Surface concise useful progress without exposing thinking deltas."""
    event_type = event.get("type")
    if event_type == "message_update":
        nested = event.get("assistantMessageEvent")
        if not isinstance(nested, dict):
            return
        nested_type = nested.get("type")
        if nested_type == "text_delta":
            delta = nested.get("delta")
            if isinstance(delta, str):
                sys.stdout.write(delta)
                sys.stdout.flush()
        elif nested_type == "toolcall_start":
            tool = nested.get("toolName") or "tool"
            sys.stdout.write(f"\n[pi] tool call: {tool}\n")
            sys.stdout.flush()
        return
    if event_type == "tool_execution_start":
        sys.stdout.write(f"\n[pi] tool start: {event.get('toolName') or 'tool'}\n")
        sys.stdout.flush()
    elif event_type == "tool_execution_end":
        suffix = "error" if event.get("isError") else "ok"
        sys.stdout.write(f"\n[pi] tool end: {event.get('toolName') or 'tool'} ({suffix})\n")
        sys.stdout.flush()
    elif event_type == "auto_retry_start":
        sys.stderr.write(
            f"\n[pi] auto retry {event.get('attempt', '?')}: {event.get('errorMessage', '')}\n"
        )
        sys.stderr.flush()


def _pump_json_stream(
    read_fd: int,
    sink_path: Path,
    clock: _ActivityClock,
    tee: bool,
) -> None:
    """Consume Pi JSONL stdout continuously and record structured progress."""
    sink = open(sink_path, "wb")
    pending = b""
    try:
        while True:
            try:
                chunk = os.read(read_fd, _READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            clock.touch(source="stdout", progress="stdout bytes")
            sink.write(chunk)
            sink.flush()
            pending += chunk
            while b"\n" in pending:
                raw_line, pending = pending.split(b"\n", 1)
                raw_line = raw_line.rstrip(b"\r")
                if not raw_line:
                    continue
                text = raw_line.decode("utf-8", "replace")
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    if tee:
                        sys.stdout.write(text + "\n")
                        sys.stdout.flush()
                    continue
                if isinstance(event, dict):
                    event_type, tool_name, progress = _event_details(event)
                    clock.touch(
                        source="pi_json_event",
                        event_type=event_type,
                        tool_name=tool_name,
                        progress=progress,
                    )
                    if tee:
                        _tee_json_event(event)
        if pending:
            text = pending.decode("utf-8", "replace")
            if tee:
                sys.stdout.write(text)
                sys.stdout.flush()
    finally:
        sink.close()


def _terminate_process(
    proc: subprocess.Popen,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
    on_action: Callable[[str], None] | None = None,
) -> None:
    """Best-effort, bounded termination. Never blocks indefinitely.

    ``on_action`` receives ``"terminate"`` / ``"kill"`` for every signal the
    supervisor attempts, including attempts that raise ``OSError``. The record
    documents intent only; it does not prove signal delivery or causation.
    """
    if proc.poll() is not None:
        return
    if on_action is not None:
        on_action("terminate")
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if on_action is not None:
        on_action("kill")
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def _close_pipe(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _validate_worker_result(worker_result_file: Path) -> tuple[bool, str | None]:
    if not worker_result_file.is_file():
        return False, None
    try:
        return isinstance(read_json(worker_result_file), dict), None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, str(exc)


def tail_text(path: Path, *, lines: int = 50, max_bytes: int = 65536) -> str:
    """Return a bounded UTF-8 tail without loading an arbitrarily large log."""
    if lines < 1:
        raise ValueError("lines must be >= 1")
    if max_bytes < 1024:
        raise ValueError("max_bytes must be >= 1024")
    if not path.is_file():
        return ""
    size = path.stat().st_size
    offset = max(0, size - max_bytes)
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(max_bytes)
    text = data.decode("utf-8", "replace")
    if offset:
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else ""
    return "\n".join(text.splitlines()[-lines:])


def launch_task(
    *,
    project_root: Path,
    task_file: Path,
    timeout_seconds: int | None = None,
    idle_timeout_seconds: int | None = None,
    hard_timeout_seconds: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
) -> dict[str, Any]:
    """Launch a detached pi-delegate supervisor and return immediately.

    This is the preferred entrypoint for Web/Cloud Codex because the local
    supervisor can outlive a single execution-bridge call while continuing to
    persist RUN_STATE.json, stdout.txt and stderr.txt.
    """
    idle_limit, hard_limit = resolve_timeouts(
        timeout_seconds, idle_timeout_seconds, hard_timeout_seconds
    )
    project_root = project_root.expanduser().resolve()
    task_file, task_id, run_dir = resolve_task(project_root, task_file)

    state_file = run_dir / "RUN_STATE.json"
    if state_file.is_file():
        try:
            existing = read_json(state_file)
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}
        if existing.get("status") == "RUNNING":
            raise ValueError(
                f"run already reports RUNNING: {run_dir}; use a new task id or resolve the stale run first"
            )

    command = [
        sys.executable,
        "-m",
        "pi_delegate",
        "run",
        str(task_file),
        "--project",
        str(project_root),
        "--idle-timeout",
        str(idle_limit),
        "--hard-timeout",
        str(hard_limit),
        "--no-tee",
    ]
    if provider:
        command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])
    if thinking:
        command.extend(["--thinking", thinking])

    supervisor_stdout = run_dir / "supervisor.stdout.txt"
    supervisor_stderr = run_dir / "supervisor.stderr.txt"
    launcher_recorder = forensics.ForensicsRecorder(run_dir, source="launcher")
    supervisor_env = os.environ.copy()
    # A nested start gets a fresh identity even if this launcher was itself
    # started by an instrumented Node worker with a different invocation id.
    # Use a separate one-shot variable so this token is not inherited by Node.
    supervisor_env.pop(forensics.FORENSICS_INVOCATION_ENV_VAR, None)
    supervisor_env[forensics.FORENSICS_START_INVOCATION_ENV_VAR] = (
        launcher_recorder.invocation_id
    )
    launch_kwargs: dict[str, Any] = {
        "cwd": project_root,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "env": supervisor_env,
    }
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        launch_kwargs["creationflags"] = creation_flags
    else:
        launch_kwargs["start_new_session"] = True

    run_dir.mkdir(parents=True, exist_ok=True)
    with supervisor_stdout.open("ab") as out_handle, supervisor_stderr.open("ab") as err_handle:
        proc = subprocess.Popen(
            command,
            stdout=out_handle,
            stderr=err_handle,
            **launch_kwargs,
        )

    launcher_pid = os.getpid()
    launcher_ppid = os.getppid()
    detached_process = bool(creation_flags & getattr(subprocess, "DETACHED_PROCESS", 0x8))
    new_process_group = bool(
        creation_flags & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    )
    launcher_recorder.append(
        "launcher_spawn_observed",
        launcher_ppid=launcher_ppid,
        supervisor_pid=proc.pid,
        process_creation_flags=creation_flags,
        detached_process=detached_process,
        new_process_group=(new_process_group or bool(launch_kwargs.get("start_new_session"))),
        start_new_session=bool(launch_kwargs.get("start_new_session", False)),
    )

    return {
        "task_id": task_id,
        "launch_status": "STARTED",
        "launcher_pid": launcher_pid,
        "launcher_ppid": launcher_ppid,
        "supervisor_pid": proc.pid,
        "supervisor_invocation_id": launcher_recorder.invocation_id,
        "process_creation_flags": creation_flags,
        "detached_process": detached_process,
        "new_process_group": (new_process_group or bool(launch_kwargs.get("start_new_session"))),
        "start_new_session": bool(launch_kwargs.get("start_new_session", False)),
        "run_dir": str(run_dir),
        "project_root": str(project_root),
        "task_file": str(task_file),
        "idle_timeout_seconds": idle_limit,
        "hard_timeout_seconds": hard_limit,
        "status_file": str(state_file),
        "stdout_file": str(run_dir / "stdout.txt"),
        "stderr_file": str(run_dir / "stderr.txt"),
    }


def run_task(
    *,
    project_root: Path,
    task_file: Path,
    timeout_seconds: int | None = None,
    idle_timeout_seconds: int | None = None,
    hard_timeout_seconds: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    forensics_invocation_id: str | None = None,
    tee: bool = True,
    terminate_grace_seconds: float = TERMINATE_GRACE_SECONDS,
    reader_join_seconds: float = READER_JOIN_SECONDS,
) -> tuple[int, dict[str, Any]]:
    idle_limit, hard_limit = resolve_timeouts(
        timeout_seconds, idle_timeout_seconds, hard_timeout_seconds
    )

    project_root = project_root.expanduser().resolve()
    task_file, task_id, run_dir = resolve_task(project_root, task_file)
    stdout_file = run_dir / "stdout.txt"
    stderr_file = run_dir / "stderr.txt"
    run_result_file = run_dir / "RUN_RESULT.json"
    worker_result_file = run_dir / "RESULT.json"
    report_file = run_dir / "REPORT.md"
    supervisor_pid = os.getpid()
    supervisor_ppid = os.getppid()
    # The CLI passes the one-shot id consumed from ``start``'s private
    # environment variable. Direct calls get a new invocation id each time.
    recorder = forensics.ForensicsRecorder(run_dir, invocation_id=forensics_invocation_id)
    recorder.append(
        "supervisor_start",
        supervisor_pid=supervisor_pid,
        supervisor_ppid=supervisor_ppid,
    )

    pi_cli = discover_pi_cli()
    if not pi_cli.is_file():
        raise FileNotFoundError(f"Pi CLI not found: {pi_cli}")
    node = shutil.which("node")
    if not node:
        raise FileNotFoundError("node executable not found in PATH")

    relative_task = task_file.relative_to(project_root).as_posix()
    prompt = (
        f"Read @{relative_task} and execute it exactly. "
        "Do not modify files outside the allowed paths stated in TASK.md. "
        "When finished, write both RESULT.json and REPORT.md in the task run directory, "
        "then give a concise final summary. RESULT.json must be valid JSON. "
        "Do not claim final Codex acceptance. "
        "During long work, periodically state concise progress in normal assistant text."
    )

    command = [node, str(pi_cli), "--no-approve", "--no-session", "--mode", "json"]
    if provider:
        command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])
    if thinking:
        command.extend(["--thinking", thinking])
    command.extend(["-p", prompt])

    env = os.environ.copy()
    env["PI_SKIP_VERSION_CHECK"] = "1"
    defaults = load_pi_defaults()
    selected = {
        "provider": provider or defaults.get("provider"),
        "model": model or defaults.get("model"),
        "thinking": thinking or defaults.get("thinking"),
    }

    # Exit forensics: attach a passive preload guard to genuine Node workers
    # only. The Python fake-Pi harness (sys.executable) is left untouched.
    # Evidence is appended, never truncated, so a re-used run directory keeps
    # its earlier evidence; only this invocation's records are reported.
    preload_active = forensics.build_preload_env(
        env, recorder.path, node, invocation_id=recorder.invocation_id
    )

    started_monotonic = time.monotonic()
    started_at = utc_now()
    clock = _ActivityClock()
    write_state(
        run_dir,
        task_id,
        "RUNNING",
        started_at=started_at,
        forensics_invocation_id=recorder.invocation_id,
        supervisor_pid=supervisor_pid,
        supervisor_ppid=supervisor_ppid,
        worker=selected,
        worker_pid=None,
        elapsed_seconds=0.0,
        idle_seconds=0.0,
        last_activity_at=started_at,
        idle_timeout_seconds=idle_limit,
        hard_timeout_seconds=hard_limit,
    )

    proc: subprocess.Popen | None = None
    threads: list[threading.Thread] = []
    timeout_kind: str | None = None
    termination_cause: str | None = None
    termination_exception_type: str | None = None

    def _record_termination(action: str) -> None:
        recorder.append(
            f"supervisor_{action}_attempt",
            worker_pid=proc.pid if proc is not None else None,
            timeout_kind=timeout_kind,
            termination_cause=termination_cause,
            exception_type=termination_exception_type,
        )

    try:
        proc = subprocess.Popen(
            command,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert proc.stdout is not None and proc.stderr is not None
        clock.touch(source="process", progress="Pi process started")
        # Evidence metadata is deliberately restricted to the allowlist in
        # TASK.md: no executable path, no argv/command, no environment values.
        recorder.append(
            "supervisor_spawn_observed",
            supervisor_pid=supervisor_pid,
            supervisor_ppid=supervisor_ppid,
            worker_pid=proc.pid,
            worker_ppid=supervisor_pid,
            preload_active=preload_active,
        )

        threads = [
            threading.Thread(
                target=_pump_json_stream,
                args=(proc.stdout.fileno(), stdout_file, clock, tee),
                daemon=True,
            ),
            threading.Thread(
                target=_pump_stream,
                args=(proc.stderr.fileno(), stderr_file, "stderr", clock, tee),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        last_persist = 0.0
        while True:
            returncode = proc.poll()
            if returncode is not None:
                break
            now_monotonic = time.monotonic()
            elapsed = now_monotonic - started_monotonic
            idle = clock.idle_seconds()
            if now_monotonic - last_persist >= STATE_PERSIST_INTERVAL_SECONDS:
                last_persist = now_monotonic
                _, last_wall = clock.snapshot()
                activity_meta = clock.metadata()
                write_state(
                    run_dir,
                    task_id,
                    "RUNNING",
                    started_at=started_at,
                    forensics_invocation_id=recorder.invocation_id,
                    supervisor_pid=supervisor_pid,
                    supervisor_ppid=supervisor_ppid,
                    worker=selected,
                    worker_pid=proc.pid,
                    elapsed_seconds=round(elapsed, 3),
                    idle_seconds=round(idle, 3),
                    last_activity_at=last_wall,
                    idle_timeout_seconds=idle_limit,
                    hard_timeout_seconds=hard_limit,
                    **activity_meta,
                )
            if idle >= idle_limit:
                timeout_kind = "idle"
                termination_cause = "idle_timeout"
                _terminate_process(
                    proc, terminate_grace_seconds, on_action=_record_termination
                )
                break
            if elapsed >= hard_limit:
                timeout_kind = "hard"
                termination_cause = "hard_timeout"
                _terminate_process(
                    proc, terminate_grace_seconds, on_action=_record_termination
                )
                break
            time.sleep(POLL_INTERVAL_SECONDS)
    except BaseException as exc:
        termination_cause = "supervisor_exception"
        termination_exception_type = type(exc).__name__
        recorder.append(
            "supervisor_exception_observed",
            supervisor_pid=supervisor_pid,
            supervisor_ppid=supervisor_ppid,
            exception_type=termination_exception_type,
        )
        if proc is not None:
            _terminate_process(
                proc,
                terminate_grace_seconds,
                on_action=_record_termination,
            )
        raise
    finally:
        for thread in threads:
            thread.join(timeout=reader_join_seconds)
        if proc is not None:
            _close_pipe(proc.stdout)
            _close_pipe(proc.stderr)

    assert proc is not None
    _, last_wall = clock.snapshot()
    activity_meta = clock.metadata()
    elapsed_seconds = round(time.monotonic() - started_monotonic, 3)
    recorder.append(
        "supervisor_worker_exit_observed",
        worker_pid=proc.pid,
        returncode=proc.returncode,
        returncode_normalized=forensics.format_exit_code(proc.returncode),
        timeout_kind=timeout_kind,
    )
    exit_forensics = forensics.build_forensics_report(
        returncode=proc.returncode,
        timeout_kind=timeout_kind,
        records=recorder.read_records(),
        preload_active=preload_active,
        worker_pid=proc.pid,
    )

    if timeout_kind is not None:
        worker_result_valid, worker_result_error = _validate_worker_result(worker_result_file)
        result = {
            "task_id": task_id,
            "runner_status": "TIMEOUT",
            "timeout_kind": timeout_kind,
            "pi_exit_code": None,
            "forensics_invocation_id": recorder.invocation_id,
            "supervisor_pid": supervisor_pid,
            "supervisor_ppid": supervisor_ppid,
            "worker_pid": proc.pid,
            "duration_seconds": elapsed_seconds,
            "elapsed_seconds": elapsed_seconds,
            "idle_seconds": round(clock.idle_seconds(), 3),
            "idle_timeout_seconds": idle_limit,
            "hard_timeout_seconds": hard_limit,
            "last_activity_at": last_wall,
            **activity_meta,
            "started_at": started_at,
            "finished_at": utc_now(),
            "project_root": str(project_root),
            "task_file": str(task_file),
            "worker": selected,
            "report_exists": report_file.is_file(),
            "worker_result_exists": worker_result_file.is_file(),
            "worker_result_valid": worker_result_valid,
            "worker_result_error": worker_result_error,
            "runner_error": f"{timeout_kind}_timeout",
            "exit_forensics": exit_forensics,
        }
        write_json(run_result_file, result)
        write_state(
            run_dir,
            task_id,
            "TIMEOUT",
            forensics_invocation_id=recorder.invocation_id,
            worker=selected,
            supervisor_pid=supervisor_pid,
            supervisor_ppid=supervisor_ppid,
            worker_pid=proc.pid,
            timeout_kind=timeout_kind,
            started_at=started_at,
            elapsed_seconds=elapsed_seconds,
            idle_seconds=round(clock.idle_seconds(), 3),
            last_activity_at=last_wall,
            idle_timeout_seconds=idle_limit,
            hard_timeout_seconds=hard_limit,
            **activity_meta,
        )
        recorder.append(
            "supervisor_result_written",
            runner_status="TIMEOUT",
            supervisor_cli_exit_code=124,
            worker_pid=proc.pid,
        )
        return 124, result

    returncode = proc.returncode
    worker_result_valid, worker_result_error = _validate_worker_result(worker_result_file)
    contract_ok = returncode == 0 and report_file.is_file() and worker_result_valid
    final_status = "COMPLETED" if contract_ok else "FAILED"
    result = {
        "task_id": task_id,
        "runner_status": final_status,
        "timeout_kind": None,
        "pi_exit_code": returncode,
        "forensics_invocation_id": recorder.invocation_id,
        "supervisor_pid": supervisor_pid,
        "supervisor_ppid": supervisor_ppid,
        "worker_pid": proc.pid,
        "duration_seconds": elapsed_seconds,
        "elapsed_seconds": elapsed_seconds,
        "idle_seconds": round(clock.idle_seconds(), 3),
        "idle_timeout_seconds": idle_limit,
        "hard_timeout_seconds": hard_limit,
        "last_activity_at": last_wall,
        **activity_meta,
        "started_at": started_at,
        "finished_at": utc_now(),
        "project_root": str(project_root),
        "task_file": str(task_file),
        "worker": selected,
        "report_exists": report_file.is_file(),
        "worker_result_exists": worker_result_file.is_file(),
        "worker_result_valid": worker_result_valid,
        "worker_result_error": worker_result_error,
        "exit_forensics": exit_forensics,
    }
    write_json(run_result_file, result)
    write_state(
        run_dir,
        task_id,
        final_status,
        forensics_invocation_id=recorder.invocation_id,
        pi_exit_code=returncode,
        worker=selected,
        supervisor_pid=supervisor_pid,
        supervisor_ppid=supervisor_ppid,
        worker_pid=proc.pid,
        timeout_kind=None,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        idle_seconds=round(clock.idle_seconds(), 3),
        last_activity_at=last_wall,
        idle_timeout_seconds=idle_limit,
        hard_timeout_seconds=hard_limit,
        **activity_meta,
    )
    supervisor_cli_exit_code = 0 if contract_ok else (returncode or 4)
    recorder.append(
        "supervisor_result_written",
        runner_status=final_status,
        supervisor_cli_exit_code=supervisor_cli_exit_code,
        worker_pid=proc.pid,
    )
    return supervisor_cli_exit_code, result


def doctor() -> tuple[int, dict[str, Any]]:
    node = shutil.which("node")
    pi_cli = discover_pi_cli()
    info: dict[str, Any] = {
        "reason_code": "CHECKING",
        "python": sys.version.split()[0],
        "node": node,
        "pi_cli": str(pi_cli),
        "pi_cli_exists": pi_cli.is_file(),
        "defaults": load_pi_defaults(),
    }
    if not node:
        info["ok"] = False
        info["reason_code"] = "NODE_NOT_FOUND"
        info["message"] = "Node.js executable was not found in PATH."
        info["reinstall_pi_delegate_recommended"] = False
        return 3, info
    if not pi_cli.is_file():
        info["ok"] = False
        info["reason_code"] = "PI_CLI_NOT_FOUND"
        info["message"] = "Pi Node CLI was not found at the configured/discovered path."
        info["reinstall_pi_delegate_recommended"] = False
        return 3, info
    try:
        proc = subprocess.run(
            [node, str(pi_cli), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        info["pi_version"] = proc.stdout.strip()
        info["pi_version_exit_code"] = proc.returncode
        info["ok"] = proc.returncode == 0
        info["reason_code"] = "READY" if proc.returncode == 0 else "PI_VERSION_FAILED"
        info["message"] = (
            "Pi delegate runtime is ready."
            if proc.returncode == 0
            else "Pi CLI returned a non-zero exit code while checking its version."
        )
        info["reinstall_pi_delegate_recommended"] = False
        return (0 if proc.returncode == 0 else 4), info
    except subprocess.TimeoutExpired:
        info["ok"] = False
        info["reason_code"] = "PI_VERSION_TIMEOUT"
        info["error"] = "Pi --version timed out"
        info["message"] = "Pi CLI did not answer the version probe before timeout."
        info["reinstall_pi_delegate_recommended"] = False
        return 124, info
