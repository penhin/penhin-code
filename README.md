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
tools.py             工具 schema、工具 handler、文件/命令工具实现
result.py            统一的工具返回结果对象
atomic_io.py         内部状态文件的原子写入和 JSON/JSONL helper
todo.py              todo 工具和持久化状态逻辑
task.py              当前主任务状态机
skills.py            skills 描述加载和完整 skill 内容读取
subagent.py          子 agent loop
compact.py           micro / auto compact 逻辑
transcript.py        transcript 保存和读取
tests/               按领域拆分的离线测试，test_smoke.py 是总入口
requirements.txt     Python 依赖
.env.example         环境变量示例
```

`.penhin_todos.json`、`.tasks/` 和 `.transcripts/` 是本地运行状态，会被文件工具和 git 忽略。

### 当前工具

```text
todo_set       设置 todo 列表
todo_show      查看 todo 列表
todo_done      标记 todo 项完成
todo_clear     清空 todo 列表
task_start     开始追踪当前主任务
task_show      查看当前或指定主任务
task_complete  标记当前主任务完成
task_block     标记当前主任务阻塞
task_clear     清除当前主任务指针
task_list      查看所有主任务
task_switch    切换当前主任务
background_start 启动后台任务
background_list  查看后台任务状态
background_show  查看后台任务结果
task           委派一个子 agent 执行聚焦任务
load_skill     读取完整 skill 内容
workspace      查看当前工作区信息
compact        压缩长上下文
list           列出项目文件
search         搜索项目文本
read           读取文件，可显示行号
edit           用唯一 old 文本替换为 new 文本
write          写入文件
bash           运行命令、测试或观察运行时行为
```

### 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

然后编辑 `.env`，设置 `ANTHROPIC_API_KEY` 和 `MODEL_ID`。

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

### 预期结果

你会看到 `penhin >>` 提示符。启动时会显示当前 session 是恢复还是新建；输入任务后，模型会按需调用工具，终端会打印工具名、call id、输入摘要、耗时和结果规模，最后模型给出文本回答。

### 测试

```bash
.venv/bin/python tests/test_smoke.py
```

测试不需要真实 LLM API，会覆盖工具注册、IO helper、todo、task status、transcript、session resume、tool runtime 日志和 compact 的离线行为。`tests/test_smoke.py` 会调用拆分后的各领域测试。

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
tools.py             tool schemas, tool handlers, file/command tools
result.py            shared tool result object
atomic_io.py         atomic writes and JSON/JSONL helpers for internal state
todo.py              todo tool and persistent todo state
task.py              current high-level task state machine
skills.py            skill description loader and full skill reader
subagent.py          subagent loop
compact.py           micro / auto compaction logic
transcript.py        transcript save/read support
tests/               split offline tests; test_smoke.py is the aggregate runner
requirements.txt     Python dependencies
.env.example         environment variable example
```

`.penhin_todos.json`, `.tasks/`, and `.transcripts/` are local runtime state. They are ignored by file tools and git.

### Current Tools

```text
todo_set       set the todo list
todo_show      show the todo list
todo_done      mark one todo item done
todo_clear     clear the todo list
task_start     start tracking the current high-level task
task_show      show the current or selected high-level task
task_complete  mark the current high-level task completed
task_block     mark the current high-level task blocked
task_clear     clear the current high-level task pointer
task_list      show all high-level tasks
task_switch    switch the current high-level task
background_start start a background task
background_list  show background task statuses
background_show  show a background task result
task           delegate a focused task to a subagent
load_skill     load full skill content
workspace      show workspace information
compact        compact long context
list           list project files
search         search project text
read           read files, optionally with line numbers
edit           replace unique old text with new text
write          write files
bash           run commands, tests, or inspect runtime behavior
```

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and set `ANTHROPIC_API_KEY` and `MODEL_ID`.

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

### Expected Result

You should see the `penhin >>` prompt. Startup logs show whether the session was resumed or created fresh. After you enter a task, the model may call tools; the terminal logs tool name, call id, input summary, duration, and result size before the model answers in text.

### Testing

```bash
.venv/bin/python tests/test_smoke.py
```

The tests do not require a real LLM API. They cover offline behavior for tool registration, IO helpers, todo, task status, transcripts, session resume, tool runtime logs, and compaction. `tests/test_smoke.py` runs the split domain tests.
