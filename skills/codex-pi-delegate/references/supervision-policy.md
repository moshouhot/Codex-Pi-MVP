# Supervision Policy

Use this policy after the runtime is `READY` and before starting Pi.

## Choose task size first

The default hard timeout is **3600 seconds**, but it is only a safety ceiling. Do not deliberately create an hour-long monolithic task just because the runner permits it.

Prefer one coherent task that has a clear intermediate outcome and is expected to finish comfortably below the ceiling. Split the work before delegation when it contains several independent phases, broad repository cleanup plus feature work, many unrelated failing subsystems, or a debugging campaign that can naturally be divided into reproduce / diagnose / repair / regression rounds.

If a task times out or stalls, inspect partial evidence and divide the next task more narrowly instead of simply increasing the hard timeout or rerunning the same prompt.

## Web/Cloud execution

For nontrivial, debugging, test-heavy, or uncertain-duration work, prefer:

```powershell
python -m pi_delegate start <TASK.md> --project <project-root>
```

`start` launches a detached local supervisor and returns promptly, so the Pi worker does not depend on one coding-tools-mcp execution call remaining open. Poll without hammering the local bridge, typically every 30–60 seconds when an update is useful:

```powershell
python -m pi_delegate status <run-dir>
python -m pi_delegate logs <run-dir> --tail 50
```

After a terminal state, read:

```powershell
python -m pi_delegate result <run-dir>
```

Blocking `run` is acceptable for short, predictable work where keeping one local command open is simpler.

## Local Codex execution

Local Codex may use blocking `run` for normal bounded work or detached `start` for long/uncertain work. Use the same supervision and acceptance rules either way.

## Activity model

The runner invokes Pi with `--mode json`. Pi JSONL events, plus stderr activity, update the activity clock. This is more reliable than waiting for final prose because events can show ongoing model/tool activity even when Pi has not produced a user-facing message.

`RUN_STATE.json` can expose:

- `worker_pid`
- `elapsed_seconds`
- `idle_seconds`
- `last_activity_at`
- `last_activity_source`
- `last_event_type`
- `last_tool_name`
- `last_progress`
- `idle_timeout_seconds`
- `hard_timeout_seconds`

Use these fields only as operational telemetry. Do not expose or reconstruct Pi's hidden chain of thought. A thinking event may prove the worker is active without revealing its private reasoning content.

## Timeout rules

Default rules:

- **idle timeout = 300 seconds**: no Pi JSON event and no stderr activity for five minutes means the worker is considered stuck;
- **hard timeout = 3600 seconds**: absolute wall-clock safety ceiling for one worker run.

On `timeout_kind=idle`:

1. Treat Pi as stuck rather than as a normal completion.
2. Read bounded logs and the real repository diff/status.
3. Check whether `RESULT.json` or `REPORT.md` exists; artifacts written before termination are useful evidence but do not override the runner timeout.
4. Preserve useful partial work when it is valid and in scope.
5. Replan and delegate a smaller repair/continuation task instead of blindly repeating the same task.

On `timeout_kind=hard`, treat the task design itself as suspect unless there is strong evidence of a legitimately long bounded operation. Prefer splitting the next round rather than raising the ceiling.

## Detect direction drift

The supervisor reports activity; it does not decide whether Pi's technical direction is correct. For consequential work, Codex should occasionally inspect real `git status` / `git diff` when risk or scope justifies it.

Stop or replan when Pi touches forbidden scope, changes a frozen public contract, alters acceptance tests just to pass, or pursues a materially different architecture without Codex approval.

