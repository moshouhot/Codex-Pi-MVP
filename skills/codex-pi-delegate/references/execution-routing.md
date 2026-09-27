# Execution Routing

Choose the route before creating a Pi task or running `pi-delegate doctor`.

## Web / Cloud Codex: CODEX_FIRST

Default to Codex doing the work itself with the tools and connectors available in the current session. This includes repository inspection, code edits, tests, builds, logs, Git operations, connector actions, and debugging when Codex can reliably perform them.

Pi availability alone is not a delegation reason. Do not delegate merely because the work is tedious, local, code-related, test-heavy, cheaper/easier for Pi, or because Pi has been used before.

Escalate to Pi only when at least one concrete reason applies:

- `local_only_environment`: the required operation or runtime exists only in the local environment Pi can access;
- `tool_unavailable`: Codex lacks a required execution/tool capability for the blocked step;
- `bridge_limit`: Codex's available local bridge cannot reliably perform or sustain the blocked operation;
- `long_debug_loop`: the required local reproduce/instrument/run/repair loop cannot be sustained or observed reliably through Codex's current execution channel;
- `repetitive_local_execution`: the required bounded local batch cannot be executed reliably within Codex's current bridge/tool limits;
- `explicit_user_request`: the user explicitly asks Pi to perform the work.

Codex may escalate immediately when the limitation is already clear; it does not need to deliberately fail first.

### Delegate the smallest blocked unit

Prefer:

```text
Codex: analyze + design + edit code
Pi: run the local-only AutoCAD regression
Codex: inspect evidence + integrate + accept
```

over:

```text
Codex: hand the whole project to Pi because one test needs AutoCAD
```

After Pi returns, Codex resumes ownership of the parent task.

## Specialized route: real browser / CDP / Cent control

When a task requires controlling the user's real local browser session, browser CDP, or Cent Browser, route that browser-control portion to the local worker and prefer the local Skill `cent-cdp-browser`.

For Web/Cloud Codex, this browser-control portion normally qualifies as `local_only_environment`: keep analysis, code changes, orchestration, and final acceptance with Codex, and delegate only the real-machine browser step unless more local work is genuinely blocked.

For local Codex, keep the normal controller-worker split and tell Pi to use `cent-cdp-browser` first for the browser/CDP/Cent step.

The Pi task should state:

```text
Preferred local skill: cent-cdp-browser
Use it first for real browser/CDP/Cent control. Do not reimplement its browser-launch/profile/CDP workflow ad hoc before checking the Skill.
If the Skill is unavailable or does not support the required operation, report that explicitly before using another approach.
```

Do not duplicate `cent-cdp-browser`'s internal rules here. Its own Skill remains authoritative for Cent profile reuse, CDP startup/attach verification, browser interaction, Cloudflare/challenge handling, and other browser-specific safety/verification behavior.

This specialized route does not apply to ordinary web research or remote browser automation that Codex can already perform reliably with its own tools.

## Local Codex: LOCAL_CONTROLLER_WORKER

Keep the established controller-worker workflow unchanged:

- Codex decides architecture, contracts, task decomposition, core algorithms, root-cause conclusions, and acceptance.
- Pi is the preferred executor for implementation, debugging experiments, build/test loops, repetitive fixes, scripts, glue/CI/docs chores, and evidence collection.
- Codex independently reviews and verifies Pi's output before declaring `PASS / FAIL / BLOCKED`.

## Routing examples

### Web: stay with Codex

- Edit several Python/C#/Rust files through coding-tools-mcp, run unit tests, inspect failures, fix them, and commit.
- Review a GitHub PR and make repository changes using available GitHub/local tools.
- Run a bounded build/test command that Codex can execute and observe directly.

### Web: use Pi fallback

- A real AutoCAD instance must be driven locally and Codex's current tools cannot operate the required runtime reliably.
- A debugging loop requires repeated local reproductions and instrumentation that the current Web execution channel cannot sustain or observe reliably.
- A required local tool is unavailable to Codex but is available to Pi.
- A task needs the user's real browser/CDP/Cent session; delegate that browser-control portion to Pi and prefer `cent-cdp-browser`.

### Local: keep existing split

- Codex freezes the design and acceptance contract; Pi implements and runs the regression loop; Codex audits the diff and decides acceptance.

## Anti-patterns

- Do not run `doctor` at the beginning of every Web task "just in case".
- Do not delegate an entire Web task when only one sub-step is blocked.
- Do not treat `coding task`, `debugging task`, `many tests`, or `Pi is installed` as sufficient fallback reasons by themselves.
- Do not let Pi decide architecture, core semantics, or final acceptance merely because fallback was triggered.
- Do not use the Cent/CDP route for ordinary web browsing when Codex can complete the work with its own browser/web tools.
