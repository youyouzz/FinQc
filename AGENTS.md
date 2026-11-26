# FinQc Agent Guide

FinQc 是私有研究系统：A 股、港股、美股日频数据下载到本地，用 TradeMaster 引擎回测，在其上构建宏观、风控、策略与 AI 研究层。QuantConnect 预留，暂不启用。

## 目录与边界

| 路径 | 职责 | 规则 |
|---|---|---|
| `TradeMaster/` | 回测引擎（submodule，指向自己 fork 的 `finqc` 分支） | 进入该目录工作时遵守 `TradeMaster/AGENTS.md`。只放**通用**引擎能力（数据适配器、市场规则、快照、接口），按可被上游接受的标准写（英文文档、测试、spec）。**绝不放策略、台账、AI 或任何私有内容**——fork 是公开的。 |
| `finqc/ingest/` | 数据下载调度与增量更新；质量检查与清洗 | 调用引擎内的 Tushare / IndexOne 适配器，不重复实现。每次下载后跑 `quality`、`clean`，见 `docs/data-quality.md`。 |
| `finqc/macro/` `finqc/risk/` | 宏观状态模型、风控规则 | 必须是确定性规则，可回测。 |
| `finqc/strategies/core/` | 策略核心：纯函数（as-of 数据 → 目标权重） | 不依赖任何引擎 API。 |
| `finqc/strategies/adapters/` | 引擎适配器（`trademaster.py`，将来 `qc.py`） | 只做薄包装。 |
| `finqc/research/` | 台账 API、预注册、holdout、DSR/PBO、统一摘要 | |
| `finqc/ai/` | MCP、新闻、预测、简报 | 见下方 AI 规则。 |
| `jobs/` | 定时任务（每日下载、每周预测） | |
| `ledger/` | 实验、预测、简报记录 | 只追加，见 `ledger/README.md`。 |
| `qc/` | QuantConnect（预留） | 见 `qc/README.md`。 |
| `docs/` | 调研与设计文档 | |
| `data/` | 本地数据（不进 git） | 见下方数据规则。 |

依赖方向只能是 `finqc → trademaster`，引擎代码永远不 import `finqc`。

## AI 规则

- **AI 不进入回测决策。** 回测中的任何信号、仓位、风控都来自确定性代码，不调用大模型。大模型记得历史走势，让它在历史时点做判断等于作弊。
- AI 负责：提出假设、写代码、读摘要写报告、做**带时间戳的前瞻**预测与简报。
- 所有检索与数据读取强制按 as-of 日期过滤，不得读取决策时点之后的信息。
- AI 只能在 `ledger/forecasts/`、`ledger/briefs/` 新增文件，不修改已有记录；`ledger/experiments/` 的新实验需先写预注册。
- 优化闭环：AI 提议 → 预注册 → 回测 → 样本外验证 → 人工审核。每个策略族有试验预算；holdout 只用一次；结论需过 DSR/PBO。
- 宏观 / 风控预测每周存档并按 Brier 分评分；连续达标的部分才可固化为规则。

## 数据规则

- `data/market/{cn,hk,us}`：行情与财务（Tushare：A 股、港股；IndexOne：美股）。`data/macro`：宏观（保留修订版本 / vintage）。`data/fx`：汇率。`data/text`：新闻与政策文本及索引。
- `market/*/raw`、`macro/`、`text/` 不可重建，需备份；`market/*/clean`、`snapshots/`、`runs/` 可由原始数据重建，可清理。港股 / 美股的原始数据是 `indexone*/`、`tushare/` 和 `eod_status*.jsonl`。
- 回测只用清洗后的数据：港股 / 美股读 `clean/`，A 股套用 `clean/exclusions.parquet`；`price_imprecise` / `adj_imprecise` 行不进入信号。
- 每条实验记录写明 TradeMaster commit、数据快照 `snapshot_id` 与策略版本，保证可复现。
- 密钥（`TUSHARE_TOKEN`、`INDEXONE_API_KEY`）只从环境变量读取，不写入代码、日志、manifest 或台账。

## 运行环境

- Mac mini M4 16GB，日常应用占用约 12GB：DuckDB 设 `memory_limit` 和外置临时目录；只读需要的列；开着日常应用时并行不超过 2 个进程；重任务放到夜间。
- 回测从仓库根目录运行，并显式传入 `data/` 下的路径，避免 TradeMaster 把 `data/`、`artifacts/` 写进自己的目录。
