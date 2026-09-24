# Delegation Policy

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

Prefer Pi for work that is execution-heavy and evidence-producing:
- ordinary code implementation;
- test creation and regression execution;
- compiler/build failures;
- repetitive code edits and compatibility changes;
- debugging experiments, logs, reproduction, and instrumentation;
- scripts, CI, glue code, documentation cleanup;
- collecting evidence requested by Codex.

## Avoid bad delegation

Do not send an entire vague project to Pi. Give one coherent bounded task with a concrete contract.

Do not size a task around the one-hour hard timeout. The 3600-second limit is a safety ceiling, not a target runtime. If the work naturally contains several independently verifiable stages, split it before delegation.

Do not ask Pi to decide its own acceptance criteria for a consequential task. Codex defines them first.

Do not accept a worker statement such as “all tests pass” without checking the real files and rerunning the meaningful checks.

