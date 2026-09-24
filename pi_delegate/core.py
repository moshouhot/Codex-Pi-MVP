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
from typing import Any


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
) -> None:
    """Best-effort, bounded termination. Never blocks indefinitely."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
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
    launch_kwargs: dict[str, Any] = {
        "cwd": project_root,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        launch_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
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

    return {
        "task_id": task_id,
        "launch_status": "STARTED",
        "supervisor_pid": proc.pid,
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

    started_monotonic = time.monotonic()
    started_at = utc_now()
    clock = _ActivityClock()
    write_state(
        run_dir,
        task_id,
        "RUNNING",
        started_at=started_at,
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
                _terminate_process(proc, terminate_grace_seconds)
                break
            if elapsed >= hard_limit:
                timeout_kind = "hard"
                _terminate_process(proc, terminate_grace_seconds)
                break
            time.sleep(POLL_INTERVAL_SECONDS)
    except BaseException:
        if proc is not None:
            _terminate_process(proc, terminate_grace_seconds)
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

    if timeout_kind is not None:
        worker_result_valid, worker_result_error = _validate_worker_result(worker_result_file)
        result = {
            "task_id": task_id,
            "runner_status": "TIMEOUT",
            "timeout_kind": timeout_kind,
            "pi_exit_code": None,
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
        }
        write_json(run_result_file, result)
        write_state(
            run_dir,
            task_id,
            "TIMEOUT",
            worker=selected,
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
    }
    write_json(run_result_file, result)
    write_state(
        run_dir,
        task_id,
        final_status,
        pi_exit_code=returncode,
        worker=selected,
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
    return (0 if contract_ok else (returncode or 4)), result


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
