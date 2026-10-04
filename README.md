# Penhin Code

Penhin 是在本地项目目录中运行的命令行 coding agent。它可以读取和修改文件、运行命令，并把会话保存在当前项目中。

## 开始使用

安装后，进入你想处理的项目目录，直接运行：

```powershell
penhin
```

首次使用时输入 `/login`，按提示选择 Provider 并登录。随后直接描述你的需求即可：

```text
帮我理解这个项目的结构，并找出测试入口。
```

一次性执行而不进入交互界面：

```powershell
penhin --once "解释这个项目的主要模块"
```

## 安装

推荐通过 pip 安装：

```powershell
python -m pip install --user penhin-code
```

如果从源码运行，克隆后在仓库根目录执行：

```powershell
python -m pip install --user -e .
```

重新打开终端后即可使用 `penhin`。如果 Windows 提示找不到命令，请将 Python 用户 Scripts 目录加入 `PATH`。

## 常用命令

```text
/login                 登录或保存 API Key
/logout                删除已保存的凭据
/auth status           查看认证状态
/model                 选择模型
/permission <mode>     调整工具执行权限
/status                查看当前会话状态
/help                  查看全部本地命令
```

Penhin 默认创建新会话。使用 `penhin --resume <id>` 恢复指定会话，使用 `penhin --sessions` 可查看已有会话。

## 规划与实现

默认模型工具只有 `read`、`edit`、`bash` 和 `plan`。简单明确的修改可以直接执行；复杂工作或你明确要求「先规划」时，模型先进入规划，根据你的设想逐步澄清需求。过程中可以提出多组问题，每组提供三个选项和「其它」。选择「其它」后，选项会切换为空白输入框，不显示占位文字或键位提示。

问题回答只用于完善计划，不代表同意实施。模型最后展示一份完整计划，由你选择「实施计划」或「计划有问题」。选择实施后自动继续，具体工具调用仍遵守当前权限设置；选择计划有问题后，可在空白输入框中反馈，模型随后修订计划并重新等待批准。

取消会暂停规划；回答、反馈、待答问题和最终批准都随会话保存，也随分支和派生会话恢复。恢复后可输入待选项目的编号或直接反馈；如果取消时已经进入自由输入框，后续数字也按文字处理。旧版三方案检查点恢复后需要重新提交完整计划并批准。

规划期间允许读取文件，禁止执行修改和 shell 命令。规划状态与权限模式独立，不能通过切换到 `full-access` 跳过方案选择。上下文仍会自动压缩，也可以使用 `/compact [提示]` 手动压缩；压缩和任务管理不占用模型工具目录。

## 插件

插件必须由用户显式安装，既不会随 Penhin 内置，也不会自动激活。安装已签名、已验证的 Artifact 后，再授权并在当前会话中激活：

```text
/plugin install <name> <source> [--project|--global]
/plugin authorize <name>
/plugin activate <name>
```

若要一步完成验证安装、授权并在当前会话启用，可使用：

```text
/plugin add <name> <source> [--project|--global]
```

`--project` 是默认值：Artifact 和 `plugins.lock.json` 写入当前项目的 `.penhin/`。`--global` 则写入 `~/.penhin/`。安装时会验证 Artifact 的发布者、签名、内容摘要、依赖锁和模型资产锁。更新也采用相同的验证安装流程；当前会话仅会在执行 `/plugin reload` 后切换到新 Artifact。

## 验证安装

```powershell
penhin --version
penhin --quality-gate
```

需要运行内置评测时：

```powershell
penhin-eval validate --suite baseline-v1
```

更多评测选项见 [docs/evaluation.md](docs/evaluation.md)。
