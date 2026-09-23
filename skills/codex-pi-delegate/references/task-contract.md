# Task Contract

Use a task packet under the target project, preferably:

```text
.ai/pi/runs/<task-id>/
  TASK.md
  RUN_STATE.json
  RUN_RESULT.json
  RESULT.json
  REPORT.md
  stdout.txt
  stderr.txt
```

## TASK.md contents

```markdown
# TASK <task-id>

## Goal
<observable outcome>

## Allowed scope
- <files/directories Pi may modify>

## Constraints
- <interfaces/invariants/compatibility requirements>
- Do not change acceptance tests merely to make them pass unless explicitly requested.

## Work
<implementation/debugging work Pi should perform>

## Verification
- <commands/tests Pi must really run>

## Deliverables
Write RESULT.json and REPORT.md in this run directory.
Do not claim final Codex acceptance.
```

## RESULT.json minimum

```json
{
  "task_id": "example-001",
  "status": "completed",
  "tests_passed": true,
  "changed_files": [],
  "blockers": []
}
```

Treat this as worker testimony. Runner validity only means it is structurally usable; Codex still verifies the claims.

