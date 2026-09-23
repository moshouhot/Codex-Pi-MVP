---
name: codex-pi-delegate
description: Delegate local coding implementation, debugging, build/test loops, repetitive engineering work, and evidence collection from Codex/ChatGPT to a headless Pi worker through pi-delegate while keeping architecture, task decomposition, core algorithm decisions, root-cause judgment, and final acceptance with Codex. Use when a local software task should follow a controller-worker workflow such as “Codex plans and audits; Pi implements and debugs”, including web/cloud Codex sessions that must use coding-tools-mcp as the local execution bridge and local Codex sessions with direct shell access.
---

# Codex Pi Delegate

Use Codex as the controller and Pi as the execution worker. Keep the user in one Codex conversation; do not require manual Pi operation.

## Core responsibility split

Keep these with Codex:
- architecture and overall plan;
- task decomposition and scope;
- public contracts, invariants, state machines, and core algorithm decisions;
- root-cause judgment when debugging matters;
- independent review and final `PASS / FAIL / BLOCKED` decision.

Delegate these to Pi by default:
- ordinary implementation and refactoring within a defined contract;
- build errors, repetitive fixes, test writing, test execution, logs, and debugging experiments;
- glue code, scripts, CI chores, documentation cleanup, and evidence collection.

For critical code, let Pi implement only after Codex defines the design constraints. Review those changes line-by-line before acceptance.

## Workflow

1. Inspect the target repository and understand the current baseline before delegation.
2. Define one bounded task with observable acceptance criteria. Read `references/delegation-policy.md` when deciding what stays with Codex versus Pi.
3. Create a run directory inside the target project, preferably `.ai/pi/runs/<task-id>/`, and write `TASK.md` using `references/task-contract.md`.
4. Establish the local execution channel and bootstrap the runtime using `references/runtime-bootstrap.md`. In web/cloud Codex, use `coding-tools-mcp` when available; in local Codex, use the direct local shell. Prefer `python -m pi_delegate doctor` as the canonical check.
5. If module `pi_delegate` alone is missing and a local execution channel exists, self-heal by locating the verified source and running editable installation as defined in `references/runtime-bootstrap.md`. Do not reinstall for Node, Pi CLI, provider/model, timeout, or API failures.
6. Execute Pi headlessly: `python -m pi_delegate run <task-file> --project <project-root>`. Add `--provider`, `--model`, or `--thinking` only when the task requires explicit routing.
7. Treat `RUN_STATE.json`, `RUN_RESULT.json`, `RESULT.json`, `REPORT.md`, stdout, and stderr as worker evidence, not final truth.
8. Read `RUN_RESULT.json`, `RESULT.json`, and `REPORT.md` as distinct artifacts; do not infer one from another. If a coding-tools-mcp read returns null, a mismatched resolved path, or `NOT_FOUND`, correct cwd-relative path resolution and retry once before classifying the evidence gap. Follow `references/acceptance-policy.md`.
9. Independently inspect the real repository state and rerun the important acceptance checks. Codex must explicitly make the final `PASS / FAIL / BLOCKED` decision.
10. If verification finds a concrete defect, create a new repair task that states the observed failure and expected correction. Delegate again rather than silently accepting or endlessly patching around symptoms.
11. Report the final status only from Codex's independent evidence.

## Non-negotiable runner contract

Preserve these behaviors when troubleshooting or evolving the CLI:
- invoke the Pi Node CLI directly in headless mode;
- use `--no-session` for delegated runs unless persistence is intentionally required;
- close stdin explicitly (`DEVNULL`/EOF) so Pi does not wait forever in non-TTY mode;
- keep the inner worker timeout comfortably below the outer tool/process timeout;
- persist `RUN_STATE.json` and `RUN_RESULT.json` for completion and failure paths;
- require valid worker `RESULT.json` plus `REPORT.md` before the runner declares `COMPLETED`;
- never equate Pi exit code 0 with Codex acceptance.

## Safety boundary

The CLI is an orchestrator, not an operating-system sandbox. Keep allowed paths explicit in every `TASK.md` and verify the actual diff afterward.

Require user authorization before delegating work that can materially affect the live system, production, credentials, paid resources, destructive data, real AutoCAD sessions, system directories, or other difficult-to-reverse state. Routine source edits, local builds, tests, and reversible debugging do not need repeated approval.

## Failure handling

Classify the failure layer before retrying:
`Codex/controller -> pi-delegate -> Pi CLI -> provider -> model/API -> Pi tools -> result contract -> independent acceptance`.

Do not hide `TIMEOUT`, missing reports, malformed `RESULT.json`, nonzero exit codes, or verification failures behind a generic success message.

Do not use reinstalling `pi-delegate` as a generic repair. Bootstrap-install only when Python cannot import module `pi_delegate`; otherwise use the doctor `reason_code` and diagnose the actual failing layer.

