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

Web/Cloud Codex 不应为长任务一直阻塞在一条 `exec_command` 上。由于 coding-tools-mcp 单条执行本身有更短的上限，推荐使用 `start` 启动本机 detached supervisor，然后用 `status` / `logs` 轮询，完成后再读取 `result` 并独立验收。

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

默认监督参数：

- `--idle-timeout 300`：5 分钟没有 Pi JSON 事件或 stderr 活动即判定卡住并终止；
- `--hard-timeout 3600`：单任务绝对上限 1 小时；
- 旧 `--timeout N` 仍兼容，并作为 hard timeout 覆盖值。

`python -m pi_delegate` 是规范入口，不依赖 Python Scripts 是否在 PATH。安装为标准 CLI 后也可直接使用 `pi-delegate` 命令；项目仍保留 `python pi_runner.py run <task-id>` 兼容旧 MVP 用法。

完成后查看：

- `runs/smoke-001/stdout.txt`
- `runs/smoke-001/stderr.txt`
- `runs/smoke-001/RUN_RESULT.json`
- `runs/smoke-001/REPORT.md`
