# penhin-code

## 中文

一个受 learn-claude-code 启发的极简 coding-agent harness。

当前版本包含：

- 一个 CLI 循环
- 一条真实 LLM 调用路径
- 一组结构化工具
- todo、task status、skills、subagent、compact 和 transcript 的最小 harness 能力

### 项目结构

```text
main.py              CLI 入口和 agent loop
runtime.py           LLM client、模型配置和 usage 打印
tools.py             工具 schema、工具 handler、文件/命令工具实现
result.py            统一的工具返回结果对象
todo.py              todo 工具和持久化状态逻辑
task.py              当前主任务状态机
skills.py            skills 描述加载和完整 skill 内容读取
subagent.py          子 agent loop
compact.py           micro / auto compact 逻辑
transcript.py        transcript 保存和读取
tests/test_smoke.py  离线 smoke 测试
requirements.txt     Python 依赖
.env.example         环境变量示例
```

`.penhin_todos.json`、`.tasks/` 和 `.transcripts/` 是本地运行状态，会被文件工具和 git 忽略。

### 当前工具

```text
todo          规划和追踪多步骤任务
task_status   追踪当前主任务状态
task          委派一个子 agent 执行聚焦任务
load_skill    读取完整 skill 内容
workspace     查看当前工作区信息
compact       压缩长上下文
list          列出项目文件
search        搜索项目文本
read          读取文件，可显示行号
edit          用唯一 old 文本替换为 new 文本
write         写入文件
bash          运行命令、测试或观察运行时行为
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

可以输入：

```text
list files in this directory
```

### 预期结果

你会看到 `penhin >>` 提示符。输入任务后，模型会按需调用工具，终端会打印工具名和结构化结果，最后模型给出文本回答。

### 测试

```bash
.venv/bin/python tests/test_smoke.py
```

测试不需要真实 LLM API，会覆盖工具注册、todo、task status、transcript 和 compact 的离线行为。

---

## English

A tiny coding-agent harness inspired by learn-claude-code.

The current version includes:

- one CLI loop
- one real LLM call path
- a small set of structured tools
- minimal harness capabilities for todo, task status, skills, subagents, compaction, and transcripts

### Project Structure

```text
main.py              CLI entrypoint and agent loop
runtime.py           LLM client, model config, and usage printing
tools.py             tool schemas, tool handlers, file/command tools
result.py            shared tool result object
todo.py              todo tool and persistent todo state
task.py              current high-level task state machine
skills.py            skill description loader and full skill reader
subagent.py          subagent loop
compact.py           micro / auto compaction logic
transcript.py        transcript save/read support
tests/test_smoke.py  offline smoke tests
requirements.txt     Python dependencies
.env.example         environment variable example
```

`.penhin_todos.json`, `.tasks/`, and `.transcripts/` are local runtime state. They are ignored by file tools and git.

### Current Tools

```text
todo          plan and track multi-step tasks
task_status   track current high-level task state
task          delegate a focused task to a subagent
load_skill    load full skill content
workspace     show workspace information
compact       compact long context
list          list project files
search        search project text
read          read files, optionally with line numbers
edit          replace unique old text with new text
write         write files
bash          run commands, tests, or inspect runtime behavior
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

Try:

```text
list files in this directory
```

### Expected Result

You should see the `penhin >>` prompt. After you enter a task, the model may call tools, the terminal will print the tool name and structured result, and then the model will answer in text.

### Testing

```bash
.venv/bin/python tests/test_smoke.py
```

The tests do not require a real LLM API. They cover offline behavior for tool registration, todo, task status, transcripts, and compaction.
