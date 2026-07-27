# penhin-code

## 中文

一个受 learn-claude-code 启发的极简 coding-agent harness。

当前版本包含：

- 一个 CLI 循环
- 一条真实 LLM 调用路径
- 一组结构化工具
- 默认恢复最近会话，也可显式新开会话
- todo、task status、skills、subagent、compact、transcript 和工具观测日志的最小 harness 能力

### 项目结构

```text
main.py              CLI 入口、会话恢复和交互循环
runtime.py           LLM client、模型配置、logging 配置和 usage 打印
tool_runtime.py      工具权限、审批、执行和观测日志
message_flow.py      LLM 内容块解析、文本提取和工具结果组装 helper
tools/               工具 schema、工具 handler、文件/命令工具实现
result.py            统一的工具返回结果对象
atomic_io.py         内部状态文件的原子写入和 JSON/JSONL helper
todo.py              todo 工具和持久化状态逻辑
task.py              当前主任务状态机
skills.py            skills 描述加载和完整 skill 内容读取
subagent.py          子 agent loop
compact.py           micro / auto compact 逻辑
transcript.py        transcript 保存和读取
requirements.txt     Python 依赖
.env.example         环境变量示例
```

`~/.penhin/config.json` 和 `~/.penhin/.env` 是用户级配置，不随工作区移动。`~/.penhin/.env` 用于 `ANTHROPIC_API_KEY`、`MODEL_ID` 等环境变量。
`.penhin_todos.json`、`.tasks/` 和 `.transcripts/` 是本地运行状态，会被文件工具和 git 忽略。

### PostgreSQL orchestration store

The first multi-agent foundation persists agent jobs, attempts, structured artifacts, and immutable events in PostgreSQL. Start the local database with Docker Compose:

```bash
cp .env.example .env  # only when .env does not exist
docker compose up -d postgres
```

Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT`, and `PENHIN_DATABASE_URL` in `.env`. The supplied development defaults expose PostgreSQL on `localhost:55432` and retain data in the `penhin-postgres` Docker volume. The runtime creates the schema on first orchestration operation.

The first stage records only read-only agent jobs. `task`, `verify`, and `background_start` preserve their existing behavior while recording attempts and artifacts. `agent_job_show`, `agent_job_list`, and `agent_artifact_show` provide read-only inspection.

### 当前工具

```text
todo_set       设置 todo 列表
todo_show      查看 todo 列表
todo_done      标记 todo 项完成
todo_clear     清空 todo 列表
task_start     开始追踪当前主任务
task_show      查看当前或指定主任务
task_complete  标记当前主任务完成
background_start 启动后台任务
background_list  查看后台任务状态
background_show  查看后台任务结果
task           委派一个隔离上下文的子 agent 执行聚焦任务，可选 explore/plan/general 类型
glob           使用 glob 模式搜索文件
load_skill     读取完整 skill 内容
workspace      查看当前工作区信息
compact        压缩长上下文
snip           标记指定历史轮次，后续 API 请求中省略
list           列出项目文件
search         搜索项目文本
read           读取文件，可显示行号
edit           用唯一 old 文本替换为 new 文本
write          写入文件
bash           运行命令或观察运行时行为
```

### 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p ~/.penhin
cp .env.example ~/.penhin/.env
```

配置模型和 API key 有两种方式。

方式一：编辑 `~/.penhin/.env`：

```bash
ANTHROPIC_API_KEY=sk-ant-xxx
MODEL_ID=claude-sonnet-4-6
```

方式二：先启动 CLI，再用命令保存：

```text
/api-key sk-ant-xxx
/model claude-sonnet-4-6
```

项目根目录的 `.env` 仍作为兼容 fallback 被读取。

### 运行

```bash
python main.py
```

默认会尝试恢复 `.transcripts/` 里的最近会话。需要干净开始时：

```bash
python main.py --new
```

一次性执行任务：

```bash
python main.py --once "summarize this project"
```

可以输入：

```text
list files in this directory
```

---

## English

A tiny coding-agent harness inspired by learn-claude-code.

The current version includes:

- one CLI loop
- one real LLM call path
- a small set of structured tools
- default latest-session resume, with an explicit new-session flag
- minimal harness capabilities for todo, task status, skills, subagents, compaction, transcripts, and tool observability

### Project Structure

```text
main.py              CLI entrypoint, session resume, and interactive loop
runtime.py           LLM client, model config, logging setup, and usage printing
tool_runtime.py      tool policy, approval, execution, and observability logs
message_flow.py      LLM content block parsing, text extraction, and tool result helpers
tools/               tool schemas, tool handlers, file/command tools
result.py            shared tool result object
atomic_io.py         atomic writes and JSON/JSONL helpers for internal state
todo.py              todo tool and persistent todo state
task.py              current high-level task state machine
skills.py            skill description loader and full skill reader
subagent.py          subagent loop
compact.py           micro / auto compaction logic
transcript.py        transcript save/read support
requirements.txt     Python dependencies
.env.example         environment variable example
```

`~/.penhin/config.json` and `~/.penhin/.env` are user-level config and do not move with the workspace. Use `~/.penhin/.env` for `ANTHROPIC_API_KEY`, `MODEL_ID`, and similar environment variables.
`.penhin_todos.json`, `.tasks/`, and `.transcripts/` are local runtime state. They are ignored by file tools and git.

### PostgreSQL orchestration store

The first multi-agent foundation persists agent jobs, attempts, structured artifacts, and immutable events in PostgreSQL. Start the local database with Docker Compose:

```bash
cp .env.example .env  # only when .env does not exist
docker compose up -d postgres
```

Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT`, and `PENHIN_DATABASE_URL` in `.env`. The supplied development defaults expose PostgreSQL on `localhost:55432` and retain data in the `penhin-postgres` Docker volume. The runtime creates the schema on first orchestration operation.

The first stage records only read-only agent jobs. `task`, `verify`, and `background_start` preserve their existing behavior while recording attempts and artifacts. `agent_job_show`, `agent_job_list`, and `agent_artifact_show` provide read-only inspection.

### Current Tools

```text
todo_set       set the todo list
todo_show      show the todo list
todo_done      mark one todo item done
todo_clear     clear the todo list
task_start     start tracking the current high-level task
task_show      show the current or selected high-level task
task_complete  mark the current high-level task completed
background_start start a background task
background_list  show background task statuses
background_show  show a background task result
task           delegate a focused task to an isolated-context subagent, optionally explore/plan/general
glob           search files using glob patterns
load_skill     load full skill content
workspace      show workspace information
compact        compact long context
snip           mark selected historical turns so future API requests omit them
list           list project files
search         search project text
read           read files, optionally with line numbers
edit           replace unique old text with new text
write          write files
bash           run commands or inspect runtime behavior
```

### Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p ~/.penhin
cp .env.example ~/.penhin/.env
```

There are two ways to configure the model and API key.

Option one: edit `~/.penhin/.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-xxx
MODEL_ID=claude-sonnet-4-6
```

Option two: start the CLI, then save them with commands:

```text
/api-key sk-ant-xxx
/model claude-sonnet-4-6
```

A project-root `.env` is still read as a compatibility fallback.

### Run

```bash
python main.py
```

By default, the CLI tries to resume the latest session from `.transcripts/`. To start clean:

```bash
python main.py --new
```

To run one task and exit:

```bash
python main.py --once "summarize this project"
```

Try:

```text
list files in this directory
```
