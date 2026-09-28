"""Exit-forensics support for pi-delegate.

This module provides a low-intrusion evidence layer around the Pi/Node worker so
that an abnormal termination (notably Windows ``0xFFFFFFFF`` / signed ``-1``) can
be classified conservatively afterwards.

Design rules (mirrors TASK.md):

* Observational only. Nothing here suppresses, converts or retries a failure.
* The guard is a small CommonJS preload applied with ``NODE_OPTIONS=--require``.
* Only genuine Node runtimes get the preload; the Python fake-Pi test harness is
  left untouched.
* Evidence is appended as JSONL inside the run directory.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Environment variable consumed by the preload guard.
FORENSICS_ENV_VAR = "PI_DELEGATE_EXIT_FORENSICS_PATH"
# Environment variable carrying the parent supervisor pid to the guard.
FORENSICS_PARENT_ENV_VAR = "PI_DELEGATE_EXIT_FORENSICS_PARENT_PID"
# Environment variable carrying the guard path to the parent supervisor.
FORENSICS_GUARD_ENV_VAR = "PI_DELEGATE_EXIT_FORENSICS_GUARD"
# Environment variable identifying one supervisor invocation. The guard copies
# it into every record so a run directory that accumulates evidence across
# invocations can still be read per-invocation without deleting old evidence.
FORENSICS_INVOCATION_ENV_VAR = "PI_DELEGATE_EXIT_FORENSICS_INVOCATION"
# One-shot handoff from ``start`` to its detached ``run`` supervisor. The CLI
# consumes this value before the worker environment is constructed.
FORENSICS_START_INVOCATION_ENV_VAR = "PI_DELEGATE_START_EXIT_FORENSICS_INVOCATION"

FORENSICS_FILE_NAME = "EXIT_FORENSICS.jsonl"
GUARD_FILE_NAME = "exit_forensics_guard.cjs"

# Classification labels (kept stable for downstream consumers).
CLASS_JS_REQUESTED_EXIT = "js_requested_exit"
CLASS_NORMAL_COMPLETION = "normal_completion"
CLASS_UNCAUGHT_EXCEPTION = "uncaught_exception"
CLASS_SUPERVISOR_TIMEOUT = "supervisor_timeout_termination"
CLASS_ABRUPT = "abrupt_external_or_native_termination"
CLASS_ERROR_EXIT = "error_exit"
CLASS_INDETERMINATE = "indeterminate_exit"

_NODE_EXE_RE = re.compile(r"^node(\.exe)?$", re.IGNORECASE)

# Supervisor events proving the supervisor tried to stop the worker.
_TERMINATE_EVENTS = frozenset(
    {
        "supervisor_terminate_attempt",
        "supervisor_kill_attempt",
        # Legacy/alternative names kept for forward/backward compatibility.
        "supervisor_terminate",
        "supervisor_kill",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def guard_path() -> Path:
    """Absolute path to the packaged CommonJS guard."""
    return Path(__file__).resolve().parent / "assets" / GUARD_FILE_NAME


def build_preload_env(
    env: dict[str, str],
    evidence_path: Path,
    node_executable: str | os.PathLike[str] | None,
    invocation_id: str | None = None,
) -> bool:
    """Attach the guard preload to ``env`` for a genuine Node runtime.

    Returns ``True`` when the preload was attached. The Python fake-Pi harness
    (``sys.executable``) is left completely untouched.

    ``NODE_OPTIONS`` is whitespace-tokenized by Node, so the guard path is
    emitted with forward slashes and wrapped in double quotes. This is the only
    form verified to survive a guard path containing spaces (a real risk because
    the package may be installed under ``C:\\Program Files``).

    Note that ``NODE_OPTIONS`` is inherited by any Node subprocess the worker
    spawns, so guard records must additionally be filtered by worker pid.
    """
    if not is_node_executable(node_executable):
        return False
    guard = guard_path()
    if not guard.is_file():
        return False
    guard_arg = str(guard).replace("\\", "/")
    require = f'--require "{guard_arg}"'
    existing = (env.get("NODE_OPTIONS") or "").strip()
    env["NODE_OPTIONS"] = f"{existing} {require}".strip() if existing else require
    env[FORENSICS_ENV_VAR] = str(evidence_path)
    env[FORENSICS_PARENT_ENV_VAR] = str(os.getpid())
    if invocation_id:
        env[FORENSICS_INVOCATION_ENV_VAR] = invocation_id
    return True


def is_node_executable(executable: str | os.PathLike[str] | None) -> bool:
    """Return True only for a plausible genuine Node runtime.

    The Python fake-Pi harness substitutes ``sys.executable`` (python.exe), so
    this check keeps the preload away from the fake worker.
    """
    if not executable:
        return False
    name = os.path.basename(str(executable)).strip()
    if _NODE_EXE_RE.match(name):
        return True
    # Some managers expose versioned shims such as ``node20`` / ``nodejs``.
    if re.match(r"^nodejs(\.exe)?$", name, re.IGNORECASE):
        return True
    if re.match(r"^node\d+(\.\d+)*(\.exe)?$", name, re.IGNORECASE):
        return True
    return False


def format_exit_code(code: Any) -> dict[str, Any]:
    """Normalize an exit code into raw/signed/hex diagnostic forms.

    ``4294967295`` -> ``{"raw": 4294967295, "signed": -1, "hex": "0xFFFFFFFF"}``.
    Non-integer values are passed through with ``normalized=False``.
    """
    info: dict[str, Any] = {"value": code, "normalized": False}
    if isinstance(code, bool) or not isinstance(code, int):
        return info
    unsigned = code & 0xFFFFFFFF
    signed = unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned
    info.update(
        {
            "raw": code,
            "unsigned_32": unsigned,
            "signed_32": signed,
            "hex": f"0x{unsigned:08X}",
            "normalized": True,
        }
    )
    return info


class ForensicsRecorder:
    """Append-only, best-effort JSONL writer for supervisor-side evidence.

    Each recorder owns an invocation id. Records are never deleted, so evidence
    from earlier invocations in the same run directory remains on disk and is
    simply not part of this invocation's report.
    """

    def __init__(
        self,
        run_dir: Path,
        invocation_id: str | None = None,
        source: str = "supervisor",
    ) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / FORENSICS_FILE_NAME
        self.invocation_id = invocation_id or uuid.uuid4().hex
        self.source = source

    def append(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "event": event,
            "source": self.source,
            "pid": os.getpid(),
            "invocation": self.invocation_id,
            "ts": _utc_now(),
        }
        record.update(fields)
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError:
            # Evidence must never break the runner.
            pass

    def read_records(self) -> list[dict[str, Any]]:
        """Return only the records belonging to this invocation."""
        return [
            record
            for record in self.read_all_records()
            if record.get("invocation") == self.invocation_id
        ]

    def read_all_records(self) -> list[dict[str, Any]]:
        """Return every well-formed record on disk, including older invocations."""
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
        except OSError:
            return records
        return records


def summarize_records(
    records: list[dict[str, Any]], worker_pid: int | None = None
) -> dict[str, Any]:
    """Extract the diagnostic facts used for classification.

    ``worker_pid`` filters out guard records produced by Node child processes
    that inherited ``NODE_OPTIONS`` from the worker (for example a Node tool the
    worker spawned). Only the worker's own guard records may drive the
    classification.
    """
    guard_started = False
    js_exit: dict[str, Any] | None = None
    really_exit: dict[str, Any] | None = None
    exit_event: dict[str, Any] | None = None
    before_exit: dict[str, Any] | None = None
    uncaught: list[dict[str, Any]] = []
    supervisor_actions: list[dict[str, Any]] = []
    foreign_guard_records = 0

    for record in records:
        event = record.get("event")
        source = record.get("source")
        if source == "supervisor":
            supervisor_actions.append(record)
            continue
        if source == "guard":
            if worker_pid is not None and record.get("pid") != worker_pid:
                foreign_guard_records += 1
                continue
        if event == "guard_start":
            guard_started = True
        elif event == "js_process_exit":
            js_exit = record
        elif event == "js_really_exit":
            really_exit = record
        elif event == "exit_event":
            exit_event = record
        elif event == "before_exit":
            before_exit = record
        elif event == "uncaught_exception":
            uncaught.append(record)

    return {
        "guard_started": guard_started,
        "js_process_exit": js_exit,
        "js_really_exit": really_exit,
        "exit_event": exit_event,
        "before_exit": before_exit,
        "uncaught_exceptions": uncaught,
        "supervisor_actions": supervisor_actions,
        "foreign_guard_records": foreign_guard_records,
    }


def classify(
    *,
    returncode: int | None,
    timeout_kind: str | None,
    summary: dict[str, Any],
) -> tuple[str, list[str]]:
    """Conservatively classify how the worker terminated.

    Returns ``(classification, reasons)``. ``reasons`` documents why the label
    was chosen. This never asserts a proven external killer.
    """
    reasons: list[str] = []
    guard_started = bool(summary.get("guard_started"))
    js_exit = summary.get("js_process_exit")
    really_exit = summary.get("js_really_exit")
    uncaught = summary.get("uncaught_exceptions") or []
    supervisor_actions = summary.get("supervisor_actions") or []

    terminate_actions = [
        action
        for action in supervisor_actions
        if action.get("event") in _TERMINATE_EVENTS
    ]

    if timeout_kind is not None:
        if terminate_actions:
            reasons.append(
                f"supervisor classified the run as a {timeout_kind} timeout and recorded "
                f"{len(terminate_actions)} terminate/kill attempt(s); the attempts do not "
                "prove signal delivery or establish that they caused the worker exit"
            )
        else:
            reasons.append(
                f"supervisor classified the run as a {timeout_kind} timeout but recorded no "
                "terminate/kill attempt"
            )
        return CLASS_SUPERVISOR_TIMEOUT, reasons

    if js_exit is not None or really_exit is not None:
        requested = js_exit or really_exit
        reasons.append(
            f"guard observed JS-requested exit via {requested.get('event')} "
            f"(code={requested.get('code')!r})"
        )
        if returncode == 0:
            return CLASS_NORMAL_COMPLETION, reasons
        return CLASS_JS_REQUESTED_EXIT, reasons

    if uncaught:
        reasons.append(
            f"guard observed {len(uncaught)} uncaught exception(s) without a JS exit request"
        )
        return CLASS_UNCAUGHT_EXCEPTION, reasons

    if not guard_started:
        reasons.append(
            "no guard startup evidence was recorded; guard may not have loaded or the "
            "process ended before the preload ran"
        )
        if returncode == 0:
            return CLASS_NORMAL_COMPLETION, reasons
        return CLASS_INDETERMINATE, reasons

    if returncode == 0:
        reasons.append("guard started and the worker exited with code 0")
        return CLASS_NORMAL_COMPLETION, reasons

    terminal_evidence = summary.get("exit_event") is not None

    # Guard started, non-zero exit, no JS exit request and no crash evidence.
    if returncode is not None and (returncode & 0xFFFFFFFF) == 0xFFFFFFFF:
        if terminal_evidence:
            reasons.append(
                "exit code normalized to 0xFFFFFFFF (signed -1) but the guard observed a "
                "terminal exit event, so the process reached its normal exit path; this "
                "is treated as an error exit rather than an abrupt kill"
            )
            return CLASS_ERROR_EXIT, reasons
        if terminate_actions:
            # An attempt can fail or race with another termination source. Keep
            # the outcome indeterminate unless timeout metadata or direct exit
            # evidence supports a stronger classification.
            causes = sorted(
                {
                    str(action["termination_cause"])
                    for action in terminate_actions
                    if action.get("termination_cause")
                }
            )
            cause_text = f" (cause: {', '.join(causes)})" if causes else ""
            reasons.append(
                f"exit code normalized to 0xFFFFFFFF (signed -1); the supervisor recorded "
                f"{len(terminate_actions)} terminate/kill attempt(s){cause_text}, but an "
                "attempt does not prove signal delivery or causation"
            )
            return CLASS_INDETERMINATE, reasons
        reasons.append(
            "exit code normalized to 0xFFFFFFFF (signed -1) with guard startup evidence "
            "but no JS exit request, no uncaught-exception evidence, no terminal exit "
            "event and no supervisor terminate action; this is consistent with an "
            "abrupt external or native termination but does not prove an external killer"
        )
        return CLASS_ABRUPT, reasons

    if returncode is not None:
        reasons.append(
            f"guard started and the worker exited with code {returncode} without explicit "
            "JS exit or crash evidence"
        )
        return CLASS_ERROR_EXIT, reasons

    reasons.append("guard started but no exit code or terminal evidence was recorded")
    return CLASS_INDETERMINATE, reasons


def build_forensics_report(
    *,
    returncode: int | None,
    timeout_kind: str | None,
    records: list[dict[str, Any]],
    preload_active: bool | None = None,
    worker_pid: int | None = None,
) -> dict[str, Any]:
    """Assemble the backward-compatible diagnostic payload for RUN_RESULT.json.

    ``preload_active`` records whether the guard preload was actually attached to
    the worker environment (``None`` means unknown / not applicable).
    ``worker_pid`` restricts guard evidence to the worker process itself.
    """
    summary = summarize_records(records, worker_pid=worker_pid)
    classification, reasons = classify(
        returncode=returncode, timeout_kind=timeout_kind, summary=summary
    )
    js_exit = summary.get("js_process_exit") or summary.get("js_really_exit")
    uncaught = summary.get("uncaught_exceptions") or []
    terminate_actions = [
        action
        for action in (summary.get("supervisor_actions") or [])
        if action.get("event") in _TERMINATE_EVENTS
    ]
    return {
        "forensics_file": FORENSICS_FILE_NAME,
        "preload_active": preload_active,
        "worker_pid": worker_pid,
        "guard_started": bool(summary.get("guard_started")),
        "terminal_guard_evidence": summary.get("exit_event") is not None,
        "js_exit_requested": js_exit is not None,
        "js_exit_code": js_exit.get("code") if js_exit else None,
        "uncaught_exception_observed": bool(uncaught),
        "supervisor_terminate_observed": bool(terminate_actions),
        "supervisor_termination_attempts": [
            {
                key: action[key]
                for key in ("event", "termination_cause", "timeout_kind", "exception_type")
                if key in action
            }
            for action in terminate_actions
        ],
        "foreign_guard_records_ignored": int(summary.get("foreign_guard_records") or 0),
        "worker_exit_code_raw": returncode,
        "worker_exit_code_normalized": format_exit_code(returncode),
        "termination_classification": classification,
        "classification_reasons": reasons,
        "record_count": len(records),
    }
