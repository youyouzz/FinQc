# FinQc

私有量化研究系统：A 股、港股、美股日频数据本地化，TradeMaster 引擎回测，宏观 / 风控 / AI 研究层。规则与目录说明见 [AGENTS.md](AGENTS.md)。

## 初始化

```bash
git clone --recurse-submodules <FinQc 仓库地址>
cp .env.example .env   # 填入 TUSHARE_TOKEN、INDEXONE_API_KEY
uv sync                # 需要 Python 3.12+、uv、Rust 1.97+（会编译 TradeMaster 的 Rust 扩展）
```

## 同步引擎上游

```bash
cd TradeMaster
git fetch upstream
git checkout main && git merge --ff-only upstream/main && git push origin main
git checkout finqc && git merge main && git push origin finqc
cd .. && git add TradeMaster && git commit -m "Bump TradeMaster"
```
