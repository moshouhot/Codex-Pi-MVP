# Delegation Policy

Apply this policy together with `execution-routing.md`.

## Web / Cloud Codex

Do not prefer Pi by default. Codex should implement, debug, test, and operate available tools/connectors itself whenever it can do so reliably. Use Pi only after a concrete fallback reason is identified, and delegate only the blocked portion when practical.

The sections below describe the responsibility boundary once Pi is used, and remain the default operating model for local Codex.

## Keep with Codex

Codex owns decisions that define the solution rather than merely execute it:
- architecture and cross-module boundaries;
- task sequencing and risk controls;
- core algorithms and invariants;
- public API/schema/state-machine semantics;
- security boundaries, transaction semantics, and destructive behavior;
- deciding whether a fix addresses root cause;
- final acceptance.

Codex may still ask Pi to implement these designs, but the task must freeze the important invariants and Codex must review the result independently.

## Delegate to Pi

In local Codex mode, prefer Pi for work that is execution-heavy and evidence-producing. In Web/Cloud mode, these are candidates only after a real fallback condition exists:
- ordinary code implementation;
- test creation and regression execution;
- compiler/build failures;
- repetitive code edits and compatibility changes;
- debugging experiments, logs, reproduction, and instrumentation;
- scripts, CI, glue code, documentation cleanup;
- collecting evidence requested by Codex.

## Avoid bad delegation

Do not send an entire vague project to Pi. Give one coherent bounded task with a concrete contract.

In Web/Cloud mode, do not delegate simply to save Codex effort, time, or context. Pi is fallback capacity for a real capability/reliability gap (or an explicit user request), not a default worker.

Do not size a task around the one-hour hard timeout. The 3600-second limit is a safety ceiling, not a target runtime. If the work naturally contains several independently verifiable stages, split it before delegation.

Do not ask Pi to decide its own acceptance criteria for a consequential task. Codex defines them first.

Do not accept a worker statement such as “all tests pass” without checking the real files and rerunning the meaningful checks.

