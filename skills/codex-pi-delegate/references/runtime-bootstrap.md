# Runtime Bootstrap

Use this bootstrap before the first Pi delegation in a session, after an environment change, or when a previous run failed before Pi started.

## 1. Choose the local execution channel

The Skill itself does not give a cloud/web Codex access to the user's Windows machine.

- If running in a web/cloud Codex and `coding-tools-mcp` is available, use `coding-tools-mcp` for all local commands, file operations, doctor checks, installs, Pi runs, status reads, and acceptance commands.
- If running in a local Codex/CLI/App with direct shell access to the target machine, use the local shell directly. Do not require `coding-tools-mcp` merely for consistency.
- If neither direct local shell nor a local execution bridge is available, stop local delegation as `BLOCKED` and tell the user that a local execution channel is required. Do not pretend Pi can be launched from the cloud alone.

### coding-tools-mcp path discipline

Do not assume all coding-tools-mcp functions use the same path base. Resolve both the workspace root and default cwd from the environment, then apply the tool-specific rule below.

- `apply_patch`: patch file paths are workspace-root-relative. Example: with workspace `C:\workspace` and target project `C:\workspace\project`, create the task as `project/.ai/pi/runs/<task-id>/TASK.md`.
- `read_file` / `list_dir`: relative paths are resolved from the current/default cwd. Example: if default cwd is already `C:\workspace\project`, read `.ai/pi/runs/<task-id>/RESULT.json`, not `project/.ai/...`.
- `exec_command`: its `workdir` is workspace-root-relative. If `workdir="project"`, command-internal relative paths such as `.ai/pi/runs/<task-id>/TASK.md` resolve from `C:\workspace\project`.

This asymmetry is intentional to document because mixing the bases causes two opposite bugs:

- passing `project/...` to a default-cwd-relative read can resolve as `...\project\project\...`;
- passing `.ai/...` to `apply_patch` can create `...\workspace\.ai\...` instead of `...\workspace\project\.ai\...`.

After every create/read failure, inspect the tool's returned/resolved path before retrying. Correct the path once; never create a second directory merely to satisfy a mistaken path.

Do not assume the default cwd remains stable across a long Pi run or unrelated local-tool activity. Immediately before the acceptance/evidence-read batch, resolve the current default cwd again. If it changed, correct paths from the newly reported cwd rather than reusing stale assumptions.

If `exec_command` rejects an otherwise safe verification command only because the top-level executable is not allowlisted, an allowed platform wrapper may invoke the exact same command without changing its arguments or semantics (for example, on Windows `cmd /c bash tools/selftest.sh`). Record that the first attempt was bridge-policy-rejected. Do not use wrappers to bypass safety gates, add shell chaining, or broaden the command beyond the originally intended verification.

## 2. Use the canonical invocation

Prefer:

```powershell
python -m pi_delegate doctor
```

Use `pi-delegate doctor` only as a convenience after PATH registration is known to work. `python -m pi_delegate` is the stable contract because it does not depend on the Python Scripts directory being present in PATH.

## 3. Self-heal only a missing pi_delegate module

If `python -m pi_delegate doctor` runs and returns JSON, do **not** reinstall `pi-delegate`. Diagnose the returned `reason_code` instead.

Only bootstrap-install when Python reports that module `pi_delegate` is missing (for example `No module named pi_delegate`).

Resolve the source directory in this order:

1. Environment variable `PI_DELEGATE_SOURCE`, if set.
2. The current project/workspace, when its `pyproject.toml` declares project name `pi-delegate`.
3. Another source directory already visible through the authorized local execution channel whose `pyproject.toml` declares project name `pi-delegate`.

Before installing, verify the selected directory exists and contains the expected `pyproject.toml`; never install an arbitrary similarly named directory.

When a local execution channel exists, self-heal automatically with:

```powershell
python -m pip install -e "<resolved-source>"
python -m pi_delegate doctor
```

This applies equally to:

- web/cloud Codex using `coding-tools-mcp`;
- local Codex using its direct shell.

Do not make the user copy/paste the install command when the agent can execute it safely itself.

If no source directory can be resolved, report the missing module and the exact install command the user can run once the source path is known. Do not invent a path.

## 4. Interpret doctor reason codes

`python -m pi_delegate doctor` returns structured JSON.

| reason_code | Meaning | Action |
|---|---|---|
| `READY` | Runtime is usable | Continue delegation |
| `NODE_NOT_FOUND` | Node.js is unavailable from PATH | Fix Node/PATH; do not reinstall pi-delegate |
| `PI_CLI_NOT_FOUND` | Pi itself is missing or its CLI path is wrong | Fix/install Pi or `PI_DELEGATE_PI_CLI`; do not reinstall pi-delegate |
| `PI_VERSION_FAILED` | Pi CLI launched but returned non-zero | Diagnose Pi; do not reinstall pi-delegate |
| `PI_VERSION_TIMEOUT` | Pi version probe hung/timed out | Diagnose Pi/process environment; do not reinstall pi-delegate |

Provider/model/API failures happen after this runtime bootstrap. Diagnose those at the Pi/provider layer rather than reinstalling `pi-delegate`.

## 5. PATH registration is optional

Editable installation may also create a `pi-delegate` console command, but PATH availability is not the correctness criterion.

After installation, it is useful to check:

```powershell
where pi-delegate
pi-delegate doctor
```

If those fail while `python -m pi_delegate doctor` is `READY`, continue using the canonical Python-module form instead of treating PATH as a blocker.

