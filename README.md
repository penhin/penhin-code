# Penhin Code

Penhin Code 是一个在本地项目中运行的命令行 coding agent，支持会话恢复、文件与 Shell 工具、子 Agent 和多 Agent 任务编排。

## 安装

推荐使用 `pipx`：

```bash
pipx install penhin-code
```

也可以从源码安装：

```bash
git clone https://github.com/penhin/penhin-code.git
cd penhin-code
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## 开始使用

在要处理的项目目录运行：

```bash
penhin
```

首次启动后执行 `/login`。Penhin 会先让你选择认证方式，再显示可用的 Provider：

- API key：Anthropic、OpenAI、Gemini 或 DeepSeek。
- Account：Claude Pro/Max 或 ChatGPT Plus/Pro，属于实验性 OAuth 支持。

凭证优先保存到系统钥匙串。系统钥匙串不可用时，Penhin 会询问是否改用权限为 `0600` 的本地凭证文件，不会静默降级。认证成功后会列出该 Provider 的兼容模型，选择结果保存在用户配置中。

支持的 Provider 为 `anthropic`、`openai`、`openai-codex`、`gemini` 和 `deepseek`。

## 常用操作

```text
/login [provider]              登录或保存 API key
/logout [provider]             删除 Penhin 保存的凭证
/auth status                   查看认证状态和来源
/provider <provider> [model]   切换 Provider
/model [provider/model]        选择模型；不带参数时打开列表
/thinking [off|high|max]       调整当前模型的思考等级
/status                        查看当前状态
/permission <mode>             切换权限模式
/compact                       压缩当前会话上下文
/help                          查看全部本地命令
```

例如选择 DeepSeek Pro，或同时指定思考等级：

```text
/model deepseek/deepseek-v4-pro
/model deepseek/deepseek-v4-pro:max
```

单次执行或开启新会话：

```bash
penhin --once "解释这个项目"
penhin --new
```

Penhin 默认恢复最近一次会话。子 Agent 会使用隔离的 Git worktree；如果需要它读取当前修改，请先提交这些修改。

## 权限与本地数据

权限模式包括交互确认、自动审查和完全访问。建议从默认模式开始，只在可信项目中扩大权限。

本地会话、任务和编排数据保存在 `.transcripts/`、`.tasks/` 和 `.penhin/`。这些目录不会作为项目源码提交。多 Agent 默认使用本地 SQLite；需要 PostgreSQL 时安装：

```bash
python -m pip install "penhin-code[postgres]"
```

然后设置 `PENHIN_DATABASE_URL`。

## Agent 评测

验证内置评测任务：

```bash
penhin-eval validate --suite baseline-v1
```

完整评测、预算和报告说明见 [docs/evaluation.md](docs/evaluation.md)。版本变化见 [CHANGELOG.md](CHANGELOG.md)。项目采用 [MIT License](LICENSE)。
