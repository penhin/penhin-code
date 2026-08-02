# Agent 评测

评测任务在临时 Git 仓库运行，确定性检查决定任务完成与否，独立 Gemini Judge 只提供质量维度评分。原始运行数据保存在 `.benchmarks/runs/`，不会进入版本控制。

## 离线校验

```bash
penhin-eval validate --suite baseline-v1
python -m pytest -q tests/test_evaluation.py
```

## 真实基线配置

先通过 `/login` 选择主 Provider、凭证和模型。Judge 模型与价格仍必须显式设置：

```text
PENHIN_EVAL_JUDGE_PROVIDER=gemini
PENHIN_EVAL_JUDGE_MODEL=gemini-3-flash-preview
PENHIN_EVAL_PRIMARY_INPUT_USD_PER_MTOK=...
PENHIN_EVAL_PRIMARY_OUTPUT_USD_PER_MTOK=...
PENHIN_EVAL_JUDGE_INPUT_USD_PER_MTOK=0.50
PENHIN_EVAL_JUDGE_OUTPUT_USD_PER_MTOK=3.00
PENHIN_EVAL_MAX_TOTAL_TOKENS=6000000
PENHIN_EVAL_MAX_USD=30
PENHIN_EVAL_WORKERS=3
```

价格缺失时离线校验仍可运行，但真实评测会在任何模型调用前失败。

```bash
penhin-eval run --suite baseline-v1 --repetitions 3
penhin-eval run --suite baseline-v1 --repetitions 1 --case main-explore-bug --case child-explore-bug --case multi-parallel-analysis
penhin-eval run --resume <run-id>
penhin-eval report <run-id>
penhin-eval trace <run-id> --case <case-id> --repetition 1
penhin-eval baseline set <run-id>
penhin-eval compare <new-run-id> --baseline <baseline-run-id>
```

正式基线必须包含 30 个任务的三次重复、没有安全违规且产品仓库状态与运行前一致。整批达到 600 万 token 或 30 美元上限时停止派发新任务，可使用 `--resume` 继续，但继续前需要提高或恢复可用预算。

## 多 Agent 链路诊断

事件使用 `penhin.eval.event/v2`，包含 event ID、trace ID、root task、Job、attempt、artifact 和 integration 关联字段。报告聚合 Planner 协议有效率、Job 终态覆盖率、悬挂 Job、无效 artifact、失败阶段、错误码、集成冲突和观测关键路径。

`penhin-eval trace` 会生成按时间排序的链路，并把首个具体错误标记为 `root_cause`。诊断文件同时写入批次的 `traces/<case-id>-<repetition>.json`。事件只记录脱敏错误分类、schema 错误和内容摘要，不保存完整模型响应或命令参数。
