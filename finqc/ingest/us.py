"""Download US listed equity and ETF daily history into the local data root.

Prices come from IndexOne (raw, split-adjusted and fully adjusted EOD); see `finqc.ingest.indexone`.
The universe is every line ever listed on NYSE (`XNYS`, which also carries NYSE American and Arca
listings) or Nasdaq (`XNAS`) that was still listed on or after the start date, delisted lines
included, so backtests are free of survivorship bias. OTC lines, warrants, units, rights and
preferreds are excluded. IndexOne types listed ETFs and ETNs as "Mutual Fund"; they are fetched
after all stocks so a partial run already covers the stock universe. Tushare supplies the US
trading calendar and its US security list for cross-checks.

Usage (TUSHARE_TOKEN and INDEXONE_API_KEY in the environment):

    python -m finqc.ingest.us --root data/market/us            # full run / resume
    python -m finqc.ingest.us --root data/market/us --limit 5  # dry run on five securities
    python -m finqc.ingest.us --root data/market/us --summary  # status only, no network
    python -m finqc.ingest.us --root data/market/us --shard 0/3 --skip-reference  # one of 3 parallel workers
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pyarrow as pa
from trademaster.data import IndexOneDataSource

from finqc.ingest.indexone import (
    START,
    cli,
    log,
    save_reference,
    securities,
    tushare_calendar,
)

MICS = ("XNYS", "XNAS")
STOCK_TYPES = (
    "Common Stock",
    "REIT",
    "Depositary Receipt",
    "Partnership Shares",
    "Equity",
)
FUND_TYPES = ("Mutual Fund",)  # IndexOne's type for exchange-traded funds and notes
US_BASIC_PAGE = 6_000


def fetch_reference(root: Path, config: dict[str, str]) -> None:
    import tushare as ts  # imported lazily: only the reference step needs it

    pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
    pages: list[pa.Table] = []
    while True:  # us_basic pages by offset; a short page is the last one
        page = pro.query(
            "us_basic", offset=len(pages) * US_BASIC_PAGE, limit=US_BASIC_PAGE
        )
        pages.append(pa.Table.from_pandas(page, preserve_index=False))
        if len(page) < US_BASIC_PAGE:
            break
    basic = pa.concat_tables(pages, promote_options="default")
    calendar = tushare_calendar(pro, "us_tradecal", config)
    for name, table in (("us_basic", basic), ("us_tradecal", calendar)):
        log(
            step="reference",
            dataset=name,
            rows=table.num_rows,
            path=str(save_reference(root, name, table)),
        )


def universe(source: IndexOneDataSource) -> list[dict[str, Any]]:
    rank = {t: 0 for t in STOCK_TYPES} | {t: 1 for t in FUND_TYPES}
    start = START.isoformat()
    rows = [
        r
        for r in securities(source, MICS)
        if r["security_type"] in rank
        and not (r["delisted_at"] and str(r["delisted_at"]) < start)
    ]
    return sorted(rows, key=lambda r: (rank[r["security_type"]], r["id"]))


if __name__ == "__main__":
    cli(__doc__, "data/market/us", universe, fetch_reference)
