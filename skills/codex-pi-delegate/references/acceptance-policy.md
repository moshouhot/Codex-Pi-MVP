# Acceptance Policy

Pi completion is not final acceptance.

After every delegated implementation:

Before reading evidence through coding-tools-mcp, re-resolve its current/default cwd; do not assume the cwd from before a long worker run is still active.

1. Read `RUN_STATE.json` and require a terminal state rather than stale `RUNNING`.
2. Read `RUN_RESULT.json` separately and confirm the runner contract: Pi exit 0, `runner_status=COMPLETED`, `worker_result_valid=true`, and report/result presence flags true.
3. Read Pi-owned `RESULT.json` separately. Confirm its `task_id` matches the delegated task and treat all worker claims as testimony, not acceptance.
4. Read Pi-owned `REPORT.md` separately. Confirm the content belongs to the same task and is not accidentally a response returned for another file path.
5. If any evidence read returns null, `NOT_FOUND`, an unexpected resolved path, or content whose reported path does not match the requested artifact, retry exactly once after correcting cwd-relative path resolution. If the second read is still inconsistent, do not silently accept; classify evidence retrieval as `BLOCKED` unless the missing evidence can be independently reproduced from the real repository state.
6. Inspect the actual changed files / `git status` / `git diff`.
7. Confirm Pi stayed within the allowed task scope. Treat unexpected files as a finding.
8. Rerun the important build/tests independently. Do not rely only on Pi's transcript.
9. Check the task's functional contract and edge cases, especially anything affecting APIs, algorithms, state, transactions, concurrency, or security.
10. Decide one status: `PASS`, `FAIL`, or `BLOCKED` from independent evidence, and state that Codex made this final decision.

If a repair round is needed, send Pi the concrete failing evidence and expected behavior. Do not merely say “fix it”.

