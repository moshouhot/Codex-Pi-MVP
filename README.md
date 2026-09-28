# Codex-Pi Delegate

目标：让 Codex 负责规划、核心逻辑和最终验收，把普通实现、调试、构建测试和脏活累活可靠地委派给本机 Pi。用户只操作 Codex，不需要手工切换 Pi。

当前结构已经从实验 Runner 收敛为两层：

- `pi_delegate/`：确定性的 CLI 核心，处理 Pi headless 启动、EOF、超时、状态和结构化结果。
- `skills/codex-pi-delegate/`：Codex 控制策略，规定什么时候下放 Pi、任务契约和独立验收规则。

v0.2 增加 runtime bootstrap/self-healing：Web/Cloud Codex 使用 `coding-tools-mcp` 作为本机执行桥；本地 Codex 使用直接 shell；仅在 `pi_delegate` Python 模块缺失时自动执行 editable install，其他故障按 doctor `reason_code` 定位。

可通过用户级环境变量指定源码目录，例如：

`PI_DELEGATE_SOURCE=<path-to-Codex-Pi-MVP>`

用于模块被卸载后的源码定位。Skill 仍会验证该目录的 `pyproject.toml`，不会仅凭环境变量盲目安装。

v0.2.1 进一步固化 coding-tools-mcp 的工具级路径规则和独立证据读取：`apply_patch` 使用 workspace-root-relative 路径；`read_file/list_dir` 使用 default-cwd-relative 路径；验收必须分别读取 Runner/Worker/Report 证据并由 Codex 独立给出最终状态。

v0.3 把固定 240 秒黑盒等待升级为 Supervisor：默认 **300 秒无 Pi 活动判定卡住**，总 hard timeout 为 **3600 秒**。Runner 使用 Pi 官方 `--mode json` 事件流，所以模型思考增量、tool call/tool execution、自动重试等都会刷新 activity；`RUN_STATE.json` 会记录 PID、elapsed、idle、last_activity、last_event_type、last_tool_name、last_progress 和超时配置。原始 JSONL 事件持续写入 `stdout.txt`，诊断写入 `stderr.txt`。3600 秒只是保险上限；Codex 应优先拆分任务，使单个委派明显早于该上限完成。

v0.4 重点收口稳定性与可观测性，而不是继续增加 Agent 层：

- delegated worker 默认关闭 `AGENTS.md` / `CLAUDE.md` 自动上下文和全局 Skill 自动发现（Pi `--no-context-files --no-skills`），避免小 TASK 被项目上下文带偏；需要专用能力时用重复的 `--skill <name-or-path>` 显式加载，例如 `--skill cent-cdp-browser`；
- Pi settings JSON 使用 BOM-safe 读取，并通过本地 Pi extension 从 `ctx.model` / `ctx.thinkingLevel` 记录 `WORKER_IDENTITY.json`，把“配置期望值”与“实际运行时 provider/model/thinking”分开；
- `stdout.txt` 只保留紧凑 JSON 事件元数据和可见文本，不再持久化完整 system prompt、thinking 内容、tool args/results；真机微型 smoke 从历史 239–493KB 降到 **6.3KB**；
- 区分 `activity` 与 `useful progress`：stderr/MCP retry 可以证明进程还活着，但不会伪装成 TASK 已经推进；
- `status` 会实时检查 supervisor / worker PID，并给出 `lifecycle_classification`，可区分“外层桥调用超时但本地任务仍活着”和“本地进程确实已经消失”；
- `RUN_RESULT.json` 分离 `worker_process_outcome` 与 `artifact_contract_status`：即使 worker 已做完实质工作但 RESULT/REPORT 合同不完整，也不会把两者混成一个模糊的 FAILED；
- Exit Forensics guard 只允许 supervisor 的直接 Pi worker 写当前 invocation，Node 子进程继承 `NODE_OPTIONS` 时不再产生跨 worker 噪声；过滤逻辑仍保留作第二道防线。

Web/Cloud Codex 不应为长任务一直阻塞在一条 `exec_command` 上。由于 coding-tools-mcp 单条执行本身有更短的上限，推荐使用 `start` 启动本机 detached supervisor，然后用 `status` / `logs` 轮询，完成后再读取 `result` 并独立验收。

如果外层 coding-tools-mcp / Codex 调用先超时，不要直接把它解释成 Pi crash。先执行 `status`：若返回 `running_local_processes_alive`，说明外层调用结束了，但本机 supervisor + worker 仍在正常运行。

## MVP 流程

1. Codex 写入 `runs/<task-id>/TASK.md`。
2. Codex 调用 `python pi_runner.py run <task-id>`。
3. Runner 直接启动本机 Pi Node CLI（不依赖 `pi.ps1`），显式关闭 stdin，等待退出并捕获 stdout/stderr。
4. Pi 按任务要求修改 `work/` 下的文件，并生成 `REPORT.md`。
5. Runner 生成 `RUN_RESULT.json`。
6. Codex 独立读取变更、报告和结果，自己验收，不直接相信 Pi 的结论。

## 安全边界

- MVP 的 Pi 工作目录固定为本项目目录。
- 测试任务只允许修改 `work/` 和对应 `runs/<task-id>/`。
- 不接触其他项目。
- Runner 不把 Pi 的“成功声明”当作验收成功；最终验收由 Codex 完成。

## 使用

首次在源码目录安装：

```powershell
python -m pip install -e .
python -m pi_delegate doctor
```

随后即可运行委派任务：

```powershell
python -m pi_delegate run runs/real-001/TASK.md --project .
python -m pi_delegate status runs/real-001
python -m pi_delegate result runs/real-001
```

长任务 / Web Codex 推荐：

```powershell
python -m pi_delegate start .ai/pi/runs/debug-001/TASK.md --project .
python -m pi_delegate status .ai/pi/runs/debug-001
python -m pi_delegate logs .ai/pi/runs/debug-001 --tail 50
python -m pi_delegate result .ai/pi/runs/debug-001
```

需要专用本地 Skill 时显式加载，不重新打开整个全局 Skill 集合：

```powershell
python -m pi_delegate start .ai/pi/runs/browser-001/TASK.md --project . --skill cent-cdp-browser
```

如确有必要才恢复旧式大上下文：`--with-context-files` 允许项目 `AGENTS.md/CLAUDE.md`，`--discover-skills` 允许自动 Skill 发现。delegated worker 默认两者均关闭。

默认监督参数：

- `--idle-timeout 300`：5 分钟没有 Pi JSON 事件或 stderr 活动即判定卡住并终止；
- `--hard-timeout 3600`：单任务绝对上限 1 小时；
- 旧 `--timeout N` 仍兼容，并作为 hard timeout 覆盖值。

`python -m pi_delegate` 是规范入口，不依赖 Python Scripts 是否在 PATH。安装为标准 CLI 后也可直接使用 `pi-delegate` 命令；项目仍保留 `python pi_runner.py run <task-id>` 兼容旧 MVP 用法。

不要在新的委派路径里直接 spawn `pi` / Pi Node CLI。`pi_delegate` 负责非 TTY stdin EOF、Supervisor、超时、退出取证、身份记录和证据合同；绕过它会重新引入 Pi 等待 stdin 等旧问题。

完成后查看：

- `runs/smoke-001/stdout.txt`
- `runs/smoke-001/stderr.txt`
- `runs/smoke-001/RUN_RESULT.json`
- `runs/smoke-001/REPORT.md`

## 退出取证（Exit Forensics）

当 Pi/Node worker 异常终止（典型是 Windows 上退出码 `0xFFFFFFFF` / signed `-1`）时，Supervisor 会追加一份只读证据，用来事后保守分类“进程是怎么结束的”。取证层是**纯观测**的：它不吞掉、不转换、不重试任何失败，也不改变 `runner_status`、`timeout_kind`、`pi_exit_code` 的既有语义。

证据文件是运行目录下的 `EXIT_FORENSICS.jsonl`，**追加写**（append-only）：同一运行目录被复用时旧证据不会被删除，每条记录带 `invocation` 字段，本次调用的报告只统计本次 `invocation` 的记录。记录分两类：

- `source: "supervisor"`：父进程侧动作，例如 `supervisor_spawn_observed`（spawn 已发生）、`supervisor_terminate_attempt` / `supervisor_kill_attempt`（超时触发的终止/强杀尝试）、`supervisor_worker_exit_observed`（观察到的最终退出码）。
- `source: "guard"`：由预加载的 CommonJS guard（`pi_delegate/assets/exit_forensics_guard.cjs`）在真实 Node worker 内产生，例如 `guard_start`、`js_process_exit`（JS 显式 `process.exit`）、`js_really_exit`、`before_exit`、`exit_event`、`uncaught_exception`。guard 只对真实 Node 运行时预加载；Python fake-Pi 测试替身不会加载它。

guard 记录会按 worker PID 过滤：worker 派生的子 Node 进程会继承 `NODE_OPTIONS`，其 guard 记录不参与父 worker 的分类。

v0.4 进一步在 guard 入口校验 parent PID：只有 supervisor 直接启动的 Pi worker 会写本次 forensic invocation；Node 后代进程在写入前就退出 guard。真机/真实 Node 回归中的 `foreign_guard_records_ignored` 为 `0`。

`RUN_RESULT.json` 新增向后兼容的诊断字段 `exit_forensics`，其中包含 `termination_classification`、`classification_reasons`、`js_exit_requested`、`terminal_guard_evidence`、`supervisor_terminate_observed`、`worker_exit_code_normalized`（同时给出 raw / unsigned_32 / signed_32 / hex）等。

### 如何保守解读 `0xFFFFFFFF`

`0xFFFFFFFF`（signed `-1`）**不等于**“被外部杀进程杀掉”。只有当以下证据同时成立时，分类才会是 `abrupt_external_or_native_termination`：

- 有 guard 启动证据（guard 确实加载过），且
- 没有 JS 显式退出请求（无 `js_process_exit` / `js_really_exit`），且
- 没有未捕获异常证据，且
- 没有到达终端退出事件（无 `exit_event`），且
- Supervisor 没有记录过终止/强杀动作。

即便如此，该标签的含义也只是**“与突然的外部/原生终止一致”**，而不是证明了外部杀手。若 guard 观察到了终端 `exit_event`，则更可能是普通错误退出；若 JS 显式请求了退出，则归类为 JS 主动退出。

证据只记录白名单元数据：时间戳、pid/ppid、cwd、Node 版本/平台/架构、退出码、事件类型和有界堆栈/错误信息。**不记录** prompt 文本、凭据、完整 argv、可执行文件路径、脚本名、环境变量值或工具参数。
