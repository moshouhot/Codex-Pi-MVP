---
name: codex-pi-delegate
description: Route coding work between Codex and a local headless Pi worker. Use for Codex/Pi controller-worker workflows, local implementation/debugging/build-test delegation, when Web/Cloud Codex may need a local Pi fallback through coding-tools-mcp, or when work needs real local browser/CDP/Cent control. In Web/Cloud sessions, keep execution with Codex by default and delegate only work Codex cannot reliably complete with its available tools; for real browser/CDP/Cent control, prefer the local worker with cent-cdp-browser. In local Codex sessions, keep the established controller-worker split where Codex plans/audits and Pi implements/debugs/tests.
---

# Codex Pi Delegate

Keep the user in one Codex conversation; do not require manual Pi operation. First choose the execution route from `references/execution-routing.md`.

## Execution modes

### Web / Cloud Codex: Codex-first

Codex should perform the task itself with its available tools and connectors whenever it can do so reliably. Pi is a fallback, not the default executor. Do not invoke Pi merely because Pi is installed or because the task involves coding, debugging, tests, Git, or local files.

Escalate only the blocked/local-only portion to Pi when a concrete fallback reason exists. Codex still owns architecture, root-cause judgment, integration, review, and final acceptance.

### Local Codex: existing controller-worker mode

Keep the established split unchanged:

- Codex owns architecture, plan, task decomposition, contracts/invariants, core algorithm decisions, root-cause judgment, review, and final `PASS / FAIL / BLOCKED`.
- Pi handles ordinary implementation/refactoring within the contract, build/test loops, repetitive fixes, debugging experiments, scripts, CI/glue/docs cleanup, and evidence collection.
- For critical code, freeze design constraints before Pi implements and review the changes independently before acceptance.

## Workflow

1. Inspect the target repository/context and determine whether this is Web/Cloud Codex or local Codex. Read `references/execution-routing.md` first.
2. In Web/Cloud mode, attempt the work with Codex's own available tools/connectors. Do not bootstrap or probe Pi yet. In local mode, use the existing delegation policy from `references/delegation-policy.md`.
3. In Web/Cloud mode, escalate only when a concrete Pi fallback condition from `execution-routing.md` is met. If only one portion is blocked, delegate only that portion rather than handing the whole task to Pi.
4. If the delegated work requires real browser/CDP/Cent control on the user's machine, use the specialized browser route in `execution-routing.md`: keep the surrounding task with Codex, but tell the local worker to prefer `cent-cdp-browser` for that browser-control portion.
5. When Pi will actually be used, define one bounded task with observable acceptance criteria, create `.ai/pi/runs/<task-id>/TASK.md` using `references/task-contract.md`, then establish/bootstrap the local runtime from `references/runtime-bootstrap.md`.
6. If module `pi_delegate` alone is missing and a local execution channel exists, self-heal as defined in `runtime-bootstrap.md`. Do not reinstall for Node, Pi CLI, provider/model, timeout, or API failures.
7. Choose blocking vs detached Pi execution using `references/supervision-policy.md`. Supervise rather than wait blindly. Default idle timeout is 300 seconds without Pi JSON activity; default hard timeout is 3600 seconds and is only a safety ceiling.
8. Treat `RUN_STATE.json`, `RUN_RESULT.json`, `RESULT.json`, `REPORT.md`, stdout, and stderr as worker evidence, not final truth. Use `last_event_type`, `last_tool_name`, `last_progress`, and `idle_seconds` for operational telemetry; never expose hidden thinking content.
9. When Pi was used, read `RUN_RESULT.json`, `RESULT.json`, and `REPORT.md` as distinct artifacts and follow `references/acceptance-policy.md`.
10. Independently inspect the real repository state and rerun the important acceptance checks. Codex must explicitly make the final `PASS / FAIL / BLOCKED` decision whether or not Pi was used.
11. If verification finds a defect or idle timeout, inspect partial logs/diff/evidence first, then create a smaller repair task when Pi is still the appropriate executor. Do not blindly rerun the same oversized task.
12. Report the final status only from Codex's independent evidence.

## Non-negotiable runner contract

Preserve these behaviors when troubleshooting or evolving the CLI:
- invoke the Pi Node CLI directly in headless mode;
- use Pi `--mode json` so structured agent/tool/retry events provide the activity signal;
- use `--no-session` for delegated runs unless persistence is intentionally required;
- close stdin explicitly (`DEVNULL`/EOF) so Pi does not wait forever in non-TTY mode;
- for Web/Cloud long tasks, detach the local supervisor with `start` instead of attempting to keep one coding-tools-mcp command open for the full worker lifetime;
- default to a 300-second idle timeout and 3600-second hard timeout unless the task requires a stricter bound;
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

