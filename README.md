# Penhin Code

一个面向本地开发的轻量 coding agent：提供交互式 CLI、结构化工具、会话恢复、任务追踪、子 Agent、持久化编排和隔离 worktree。

## 快速开始

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

Provider 是启动配置，不支持在同一会话中热切换。请在 `~/.penhin/.env` 或启动环境中选择一个 Provider、设置对应密钥和模型，然后重启程序。

```bash
# Anthropic（默认）
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
MODEL_ID=claude-sonnet-4-6

# OpenAI（使用 Responses API）
LLM_PROVIDER=openai
OPENAI_API_KEY=...
MODEL_ID=gpt-4.1

# Gemini
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
MODEL_ID=gemini-2.5-flash
```

程序会校验官方 Provider 与模型前缀是否匹配。使用私有 Anthropic/OpenAI 网关时，设置对应 `*_BASE_URL` 会自动允许自定义模型；也可显式设置 `PENHIN_SKIP_MODEL_COMPATIBILITY_CHECK=1`。

常用命令：

```text
/api-key <key>       保存当前 Provider 的密钥
/model <model>       保存模型并立即用于当前会话
python main.py --model <model>  # 只覆盖当前会话
```

启动环境变量优先级为：进程环境变量、`~/.penhin/.env`、项目根目录 `.env`。`/api-key` 和 `/model` 写入用户级 `~/.penhin/.env`。

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
