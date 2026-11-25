# qc/（预留）

QuantConnect 暂不启用。启用时遵守两条约定：

1. **策略核心复用**：策略逻辑写在 `finqc/strategies/core/`（纯函数：as-of 数据 → 目标权重），这里只放 LEAN 项目和一层薄适配器，不复制策略逻辑。
2. **统一摘要**：QC 回测结果通过 `qc_digest.py` 转成与本地相同的摘要格式（`engine: "qc"`），写入 `ledger/experiments/`。
