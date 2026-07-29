# Penhin Code

一个面向本地开发的轻量 coding agent：提供交互式 CLI、结构化工具、会话恢复、任务追踪、子 Agent、持久化编排和隔离 worktree。

版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 快速开始

作为命令行工具安装（发布到 PyPI 后）：

```bash
pipx install penhin-code
penhin --help
```

也可直接从 Git 仓库安装：

```bash
pipx install git+https://github.com/penhin/penhin-code.git
```

`pipx` 会为命令行工具创建独立虚拟环境；首次使用前仍需按下文配置 Provider、API Key 和模型。

从源码运行：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p ~/.penhin
cp .env.example ~/.penhin/.env
python main.py
```

默认会恢复最近会话；使用 `python main.py --new` 创建空会话，使用 `python main.py --once "解释当前项目"` 执行一次请求后退出。

## 配置模型与 Provider

可以为多个 Provider 同时保存密钥，并在 CLI 中切换当前 Provider 与模型。配置保存在 `~/.penhin/.env`；进程环境变量可覆盖该配置。

```bash
# Anthropic（默认）
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
MODEL_ID=claude-sonnet-5

# OpenAI（使用 Responses API）
LLM_PROVIDER=openai
OPENAI_API_KEY=...
MODEL_ID=gpt-5.6

# Gemini
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
MODEL_ID=gemini-3.5-flash
```

程序会校验官方 Provider 与模型前缀是否匹配。使用私有 Anthropic/OpenAI 网关时，设置对应 `*_BASE_URL` 会自动允许自定义模型；也可显式设置 `PENHIN_SKIP_MODEL_COMPATIBILITY_CHECK=1`。

常用命令：

```text
/api-key [provider] [key]  查看或保存指定 Provider 的密钥
/model <model>       保存模型并立即用于当前会话
/provider <provider> [model]  切换 Provider；可选地同时指定模型
python main.py --model <model>  # 只覆盖当前会话
python main.py --provider <provider> --model <model>  # 只覆盖当前会话
```

例如，先保存 OpenAI 密钥，再切换：

```text
/api-key openai sk-...
/provider openai gpt-5.6
```

`/provider` 会校验模型兼容性和目标密钥；模型由 `/model` 与 `/status` 单独查看和设置。切换会保留当前会话记录，但建议在切换到能力差异较大的模型时使用 `/compact` 或 `--new` 开启新会话。

启动环境变量优先级为：进程环境变量、`~/.penhin/.env`、项目根目录 `.env`。`/api-key`、`/model` 和 `/provider` 写入用户级 `~/.penhin/.env`。

## 本地编排存储

多 Agent 的任务、尝试、事件、产物和集成记录默认存入项目的 `.penhin/orchestration.sqlite3`，无需安装或配置 PostgreSQL。SQLite 适用于单机运行，并启用 WAL 和事务化任务领取。

需要共享存储或更高并发时，设置：

```bash
PENHIN_DATABASE_URL=postgresql://user:password@host:5432/database
```

也可指定 SQLite 文件：

```bash
PENHIN_DATABASE_URL=sqlite:////absolute/path/to/orchestration.sqlite3
```

显式配置的后端不可用时会报错，不会静默回退到另一份本地数据。

可选编排参数包括：`PENHIN_SCHEDULER_WORKERS`、`PENHIN_WORKER_KILL_GRACE_SECONDS`、`PENHIN_SYNC_AGENT_TIMEOUT_SECONDS`、`PENHIN_AGENT_POLL_INTERVAL_SECONDS`、`PENHIN_SQLITE_CONNECT_TIMEOUT_SECONDS` 和 `PENHIN_SQLITE_BUSY_TIMEOUT_MS`。

## 子 Agent 与集成

可执行的 Agent 会在 `.penhin/worktrees/<job-id>` 建立独立 Git worktree。`general` Agent 可在自己的 worktree 写入；`explore`、`plan`、`verify` 为只读模式。worktree 从当前已提交的 `HEAD` 创建，因此希望子 Agent 看见的改动应先提交。

写入型 Agent 成功后会生成不可变的 change set。使用集成工具可以在独立 `penhin/integration-*` 分支上按顺序 cherry-pick 这些提交；集成不会直接更新主分支。

## 主要工具

```text
todo_set / todo_show / todo_done / todo_clear
task_start / task_show / task_complete
background_start / background_list / background_show
task / verify
agent_plan_create / agent_dag_show / agent_job_show / agent_job_list
agent_artifact_show / agent_job_wait / agent_job_cancel
integration_start / integration_show / integration_verify
glob / list / search / read / edit / write / bash
workspace / compact / snip / load_skill
```

本地状态位于 `.penhin/`、`.tasks/`、`.transcripts/` 与 `.penhin_todos.json`，均不会被文件工具扫描或写入版本控制。

## Agent 评测

项目内置确定性与独立 Judge 结合的评测框架，覆盖主 Agent、角色型子 Agent和多 Agent 编排。离线检查可运行 `penhin-eval validate --suite baseline-v1`；真实基线、预算配置、报告和回归比较见 [docs/evaluation.md](docs/evaluation.md)。
