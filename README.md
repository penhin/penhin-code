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
