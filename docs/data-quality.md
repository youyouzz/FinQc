# 数据质量检查与清洗规则

代码：`finqc/ingest/quality.py`（检查）、`finqc/ingest/clean.py`（清洗）、`finqc/ingest/tables.py`（共用加载）。
原始数据永不修改；清洗产物可随时删除重建。

## 用法

从仓库根目录运行（`PYTHONPATH=.`，用 TradeMaster/.venv 的 Python）。**一次只跑一个市场**：美股单次约 2–3 分钟、DuckDB 上限 4GB。

```bash
python -m finqc.ingest.quality cn     # 也可以是 hk / us；有未处理的 error 时退出码为 1
python -m finqc.ingest.clean cn       # 写出清洗产物和 clean.json
```

| 产物 | 位置 | 内容 |
|---|---|---|
| 检查报告 | `data/runs/qc/{market}-quality.json` | 每项检查的数值、级别、对应的清洗规则 |
| 检查工作库 | `data/runs/qc/{market}.duckdb` | 加载后的工作表；`--reuse` 复用，可删除 |
| 港股 / 美股清洗结果 | `data/market/{hk,us}/clean/` | `daily.parquet`、`instruments.parquet`、`clean.json` |
| A 股排除清单 | `data/market/cn/full-a/clean/` | `exclusions.parquet`、`clean.json` |

检查级别：

- **error**：下载本身有问题（缺交易日、重复、状态日志不符），需要重新下载。
- **warn**：已知的数据源缺陷，`handled_by` 写明由哪条清洗规则处理。
- **info**：策略必须遵守的数据特性（零成交日、缺口），不算缺陷。

## 已发现的问题（2026-11-26 首次检查）

### A 股（Tushare）：基本干净

4,102 个交易日全覆盖，5,860 只股票，1,463 万行；无重复，OHLC 无矛盾；前收盘与复权因子连续性仅 16 行不一致；指数行情、申万行业完整。

| 问题 | 规模 | 处理 |
|---|---|---|
| 北交所股票在 2021-11-15 开市前的新三板成交 | 229 只、81,330 行，最早到 2010 年；无复权因子 / 涨跌停 / daily_basic | `bj_pre_bse`：排除 |
| 北交所 2021-11-15 至 2022-12-30 无复权因子 | 136 只、10,153 行 | `missing_adj_factor`：无复权价 |
| 旧代码与新代码历史重复（000022→001872、000043→001914、300114→302132） | 3 只、7,637 行，不在 stock_basic | `not_in_stock_basic`：排除 |
| 复权因子下降 | 7 处，其中 3 处在 2026-11-23 | `adj_factor_decrease`：待复核 / 重新下载 |
| 财报完全重复行 | 28 / 39 / 15 行（利润表 / 资产负债表 / 现金流量表） | 时点（as-of）层去重 |

### 港股、美股（IndexOne）：共性问题

- **价格只有 2 位小数**：接口本身如此，适配器没有截断。港股 0.25 港元以下的最小价位是 0.001，所以：
  - 港股 34% 的行收盘价低于 0.5 港元（取整误差超过 1%），5.5 万行价格被取整为 0.00；
  - 美股约 8.5% 的普通股行收盘价低于 1 美元。
  - 无法补救，只能打标并在策略中过滤。
- **`delisted_at` 不可靠**：退市日之后仍有数据（港股 173 只中 160 只，美股 5,557 只中 2,296 只）。这些行 98–99% 是零成交量的平盘填充。
- **同一 ID 混入不同公司的历史**：有的 ID 拼接了两家公司（如 AMAC.US 前段是 2010–2011 年的旧公司，后段是 2026 年的新 SPAC）；有的新 ID 回填了旧 ID 的全部历史（如 BIP.US、LEDR.US），同一段历史出现两次。
- **其他**：港股数据截至 2026-11-24（下载时 11-25 尚未发布）；有零星原始数据错误，例如 2233.HK 在 2010-08-20 的收盘价为 695，应为 6.95。

### 美股特有

- IndexOne 的 "Mutual Fund" 类型中有 4,399 只是普通开放式基金（FNARX、VMGRX、TRRAX 等），只有净值、成交量全为 0，不能在交易所交易，约占美股行数的 35%。ETF/ETN 与它们类型相同，靠代码区分：开放式基金代码为 4 个以上字母且以 X 结尾，ETF 没有这种代码。
- 63 行落在交易所休市日（GC.US、INFO.US 等），1 个重复键。

## 清洗规则（ruleset 1）

### 港股 / 美股：按顺序执行

| 规则 | 内容 | 港股 | 美股 |
|---|---|---:|---:|
| `dedupe` | 每个 (id, date) 保留一行 | 0 | 1 |
| `no_reference` | 去掉证券参考表中没有的 id | 0 | 0 |
| `off_calendar` | 去掉 Tushare 交易日历中休市日的行 | 0 | 63 |
| `open_end_fund` | 去掉开放式基金（`^[A-Z]{4,}X\.US$` 且类型为 Mutual Fund） | 0 | 4,399 只 / 1,574 万行 |
| `price_spike` | 去掉单日 ×5 或 ÷5、次日反转的尖峰 | 378 | 830 |
| `stitched_history` | 拆分 ID：缺口超过 365 天且复权价跳变 3 倍以上，或缺口超过 3 年。前段改为 `<id>@<首日>`；其余缺口视为长期停牌，不拆 | 2 | 137 |
| `delisting_tail` | 去掉 `delisted_at` 之后、最后成交日之后的零成交行 | 12,879 | 72,790 |
| `duplicate_history` | 同代码两个标的的成交收盘价重合 ≥80%（且 ≥20 行）时去掉较短者 | 0 | 25 只 / 43,653 行 |
| `zero_price` | 取整为 0 的原始价 / 复权价置空（引擎要求原始价为正） | 55,013 | 140,110 |

清洗后：港股 2,812 个标的、761 万行；美股 15,279 个标的、2,968 万行。

`daily.parquet` 列：`instrument_id, source_id, symbol, session, open, high, low, close, volume, split_adj_close, adj_open, adj_close, market_cap, tradable, price_imprecise, adj_imprecise`。

- `tradable`：成交量大于 0，且开盘、收盘价为正。
- `price_imprecise` / `adj_imprecise`：原始价 / 复权价低于 0.5，此时 2 位小数的取整误差可能超过 1%。**策略的信号和股票池必须跳过这些行。**

`instruments.parquet` 的 `listed_on` / `delisted_on` 遵循引擎 `Instrument` 的约定：`delisted_on` 是第一个不在股票池内的交易日；仍在交易的标的为空。所有行都落在各自的上市区间内。

### A 股：排除清单

引擎的 full-A 面板直接读取原始数据，所以 A 股只输出 `exclusions.parquet`，列为 `ts_code, start_date, end_date, rule, action, rows`。使用方按 `action` 处理：

- `drop`：去掉该区间的行；
- `no_adjusted`：该区间不使用复权价；
- `review`：人工复核或重新下载。

## 维护

- 每次增量下载后先跑 `quality`，出现 error 就停下排查；然后重跑 `clean`。
- 修改规则时提升 `clean.RULESET_VERSION`，并在实验记录中写明 ruleset 版本和 `clean.json` 中的 `outputs` 哈希。
- 多个 DuckDB 进程不能共用临时目录：各进程会覆盖对方的溢写文件，导致崩溃或**静默算错**。`tables.connect` 已为每个进程分配独立目录，不要改回共用。
