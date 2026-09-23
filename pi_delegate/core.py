from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_TIMEOUT_SECONDS = 240


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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


def write_state(run_dir: Path, task_id: str, status: str, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "updated_at": utc_now(),
    }
    payload.update(extra)
    write_json(run_dir / "RUN_STATE.json", payload)


def run_task(
    *,
    project_root: Path,
    task_file: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if timeout_seconds < 1:
        raise ValueError("timeout must be >= 1 second")

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
        "Do not claim final Codex acceptance."
    )

    command = [node, str(pi_cli), "--no-approve", "--no-session"]
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

    started = time.time()
    started_at = utc_now()
    write_state(run_dir, task_id, "RUNNING", started_at=started_at, worker=selected)

    try:
        proc = subprocess.run(
            command,
            cwd=project_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            env=env,
        )
        stdout_file.write_text(proc.stdout, encoding="utf-8")
        stderr_file.write_text(proc.stderr, encoding="utf-8")

        worker_result_valid = False
        worker_result_error: str | None = None
        if worker_result_file.is_file():
            try:
                worker_result_valid = isinstance(read_json(worker_result_file), dict)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                worker_result_error = str(exc)

        contract_ok = proc.returncode == 0 and report_file.is_file() and worker_result_valid
        final_status = "COMPLETED" if contract_ok else "FAILED"
        result = {
            "task_id": task_id,
            "runner_status": final_status,
            "pi_exit_code": proc.returncode,
            "duration_seconds": round(time.time() - started, 3),
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
        write_state(run_dir, task_id, final_status, pi_exit_code=proc.returncode, worker=selected)
        return (0 if contract_ok else (proc.returncode or 4)), result
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout_file.write_text(stdout, encoding="utf-8")
        stderr_file.write_text(stderr, encoding="utf-8")
        result = {
            "task_id": task_id,
            "runner_status": "TIMEOUT",
            "pi_exit_code": None,
            "duration_seconds": round(time.time() - started, 3),
            "started_at": started_at,
            "finished_at": utc_now(),
            "project_root": str(project_root),
            "task_file": str(task_file),
            "worker": selected,
            "report_exists": report_file.is_file(),
            "worker_result_exists": worker_result_file.is_file(),
            "worker_result_valid": False,
            "worker_result_error": "timeout",
        }
        write_json(run_result_file, result)
        write_state(run_dir, task_id, "TIMEOUT", worker=selected)
        return 124, result


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

