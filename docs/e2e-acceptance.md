# 多 Agent 端到端验收

端到端测试必须在临时 Git 仓库中运行，使用独立的 PostgreSQL 命名空间和来自 `.env` 的真实 Provider 凭证；不得将产品仓库本身作为 Agent 的目标工作区。

## 确定性判定

测试框架负责 pass/fail 断言，包括 Job 与 Integration 状态迁移、Artifact schema 有效性、Git 提交祖先关系、变更文件白名单、命令退出码和清理结果。LLM 不能是唯一判定依据。

## LLM 辅助场景

真实 Planner 只能用于生成受场景契约约束的 `penhin.dag/v1` 计划。执行前应校验计划的角色图、允许路径、最大节点数和必需的最终节点。单独的 verifier 调用只能对可读证据作分类；必须将其结构化结论与确定性检查比对，不一致即记录为失败。

## 必测场景

1. 正常路径：explore → implement → verify，生成已提交的 change set、完成集成、通过质量门禁并清理。
2. 无效 Artifact：格式错误的 handoff 使 Job 失败，并阻止依赖 Job 调度。
3. 重启：Worker 运行中停止并重启 Scheduler，不得创建重复 attempt。
4. 冲突：两个有效 change set 发生重叠；Integration 进入 `needs_resolution`，目标分支保持不变。
5. 取消与超时：终止 Worker 进程组，数据库终态必须与事件轨迹一致。

每个场景都必须在 `finally` 清理临时仓库、Agent worktree、分支、integration worktree 和数据库记录。可保存 LLM prompt、结构化响应、模型标识和运行 ID 作为测试证据，但绝不能保存凭证。
