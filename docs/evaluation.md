# Agent 评测

评测任务在临时 Git 仓库运行，确定性检查决定任务完成与否，独立 Gemini Judge 只提供质量维度评分。原始运行数据保存在 `.benchmarks/runs/`，不会进入版本控制。

## 离线校验

```bash
penhin-eval validate --suite baseline-v1
python -m pytest -q tests/test_evaluation.py
```

## 真实基线配置

除正常的 `LLM_PROVIDER`、`MODEL_ID` 和 Provider 密钥外，必须显式设置：

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
penhin-eval baseline set <run-id>
penhin-eval compare <new-run-id> --baseline <baseline-run-id>
```

正式基线必须包含 30 个任务的三次重复、没有安全违规且产品仓库状态与运行前一致。整批达到 600 万 token 或 30 美元上限时停止派发新任务，可使用 `--resume` 继续，但继续前需要提高或恢复可用预算。
