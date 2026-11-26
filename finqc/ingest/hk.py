"""Download Hong Kong equity daily history into the local data root.

Prices come from IndexOne (raw, split-adjusted and fully adjusted EOD); see `finqc.ingest.indexone`.
Tushare supplies reference data that IndexOne lacks: board-lot sizes (`trade_unit`), the full
delisting list, and the HKEX trading calendar.

Usage (TUSHARE_TOKEN and INDEXONE_API_KEY in the environment):

    python -m finqc.ingest.hk --root data/market/hk            # full run / resume
    python -m finqc.ingest.hk --root data/market/hk --limit 5  # dry run on five securities
    python -m finqc.ingest.hk --root data/market/hk --summary  # status only, no network
    python -m finqc.ingest.hk --root data/market/hk --shard 0/3 --skip-reference  # one of 3 parallel workers
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pyarrow as pa
from trademaster.data import IndexOneDataSource

from finqc.ingest.indexone import cli, log, save_reference, securities, tushare_calendar

SECURITY_TYPES = ("Common Stock", "REIT", "Equity")


def fetch_reference(root: Path, config: dict[str, str]) -> None:
    import tushare as ts  # imported lazily: only the reference step needs it

    pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
    frames = [pro.query("hk_basic", list_status=s) for s in ("L", "D", "P")]
    basic = pa.concat_tables(
        [pa.Table.from_pandas(f, preserve_index=False) for f in frames if len(f)],
        promote_options="default",
    )
    calendar = tushare_calendar(pro, "hk_tradecal", config)
    for name, table in (("hk_basic", basic), ("hk_tradecal", calendar)):
        log(
            step="reference",
            dataset=name,
            rows=table.num_rows,
            path=str(save_reference(root, name, table)),
        )


def universe(source: IndexOneDataSource) -> list[dict[str, Any]]:
    rows = [
        r for r in securities(source, ["XHKG"]) if r["security_type"] in SECURITY_TYPES
    ]
    return sorted(rows, key=lambda r: r["id"])


if __name__ == "__main__":
    cli(__doc__, "data/market/hk", universe, fetch_reference)
