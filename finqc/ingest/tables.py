"""Load downloaded raw data into DuckDB work tables shared by the quality checks and cleaning rules.

Raw files are never modified. Work tables live in a scratch DuckDB file (by default under
`data/runs/qc/`), which can be deleted and rebuilt at any time.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
from pathlib import Path

import duckdb

MEMORY_LIMIT = (
    "4GB"  # Mac mini with daily apps open: keep DuckDB well below free memory
)
THREADS = 4


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
    con.execute(f"SET threads={THREADS}")
    # A private spill directory per process: concurrent DuckDB processes sharing one temp directory
    # overwrite each other's spill files, which crashes them or silently corrupts query results.
    spill = path.parent / "tmp" / f"{path.stem}-{os.getpid()}"
    spill.mkdir(parents=True, exist_ok=True)
    atexit.register(shutil.rmtree, spill, ignore_errors=True)
    con.execute(f"SET temp_directory='{spill}'")
    con.execute("SET preserve_insertion_order=false")
    return con


def _files(root: Path, pattern: str) -> list[str]:
    files = sorted(str(p) for p in root.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no files match {root / pattern}")
    return files


def load_indexone(con: duckdb.DuckDBPyConnection, root: Path) -> dict[str, str]:
    """Create `eod`, `ref`, `cal` and `status` for an IndexOne market root (HK or US).

    Reads every cache under `indexone*/`: shard caches and the unsharded one a dry run writes.
    Returns the pinned run window from `run.json`.
    """
    config: dict[str, str] = json.loads((root / "run.json").read_text())
    eod = _files(root, "indexone*/raw/indexone/i1_core_eod/*.parquet")
    ref = _files(root, "indexone*/raw/indexone/core_securities_reference/*.parquet")
    cal = _files(root, "tushare/*_tradecal/*.parquet")
    status = _files(root, "eod_status*.jsonl")
    start, end = config["start"], config["end"]
    con.execute(f"""
        CREATE OR REPLACE TABLE eod AS
        SELECT id, CAST(date AS DATE) AS d, open, high, low, close, volume,
               splitAdjClose AS sac, adjOpen AS ao, adjHigh AS ah, adjLow AS al,
               adjClose AS ac, marketCap AS mcap
        FROM read_parquet({eod})
        ORDER BY id, d
    """)
    # Shards hold overlapping reference snapshots; keep one deterministic row per id.
    con.execute(f"""
        CREATE OR REPLACE TABLE ref AS
        SELECT * EXCLUDE (filename) FROM read_parquet({ref}, filename=true, union_by_name=true)
        QUALIFY row_number() OVER (PARTITION BY id ORDER BY filename DESC) = 1
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE cal AS
        SELECT DISTINCT strptime(cal_date, '%Y%m%d')::DATE AS d FROM read_parquet({cal})
        WHERE is_open = 1
          AND strptime(cal_date, '%Y%m%d')::DATE BETWEEN DATE '{start}' AND DATE '{end}'
        ORDER BY d
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE status AS
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (PARTITION BY id ORDER BY "at" DESC) AS rn
            FROM read_json({status}, format='newline_delimited', columns={{
                id: 'VARCHAR', symbol: 'VARCHAR', status: 'VARCHAR', rows: 'BIGINT',
                "first": 'VARCHAR', "last": 'VARCHAR', "at": 'VARCHAR'}})
        ) WHERE rn = 1
    """)
    return config


def load_tushare_cn(con: duckdb.DuckDBPyConnection, raw: Path) -> None:
    """Create `cal`, `basic`, `d` (daily joined with adj_factor, stk_limit, daily_basic), `adj`
    and `susp` for the A-share Tushare raw store. Financial tables are read in place."""

    def scan(dataset: str) -> str:
        return tushare_scan(raw, dataset)

    con.execute(f"""
        CREATE OR REPLACE TABLE cal AS
        SELECT DISTINCT strptime(cal_date, '%Y%m%d')::DATE AS dt FROM {scan("trade_cal")}
        WHERE is_open = 1 AND exchange = 'SSE'
          AND strptime(cal_date, '%Y%m%d')::DATE <= (
              SELECT max(strptime(trade_date, '%Y%m%d')::DATE) FROM {scan("daily")})
          AND strptime(cal_date, '%Y%m%d')::DATE >= (
              SELECT min(strptime(trade_date, '%Y%m%d')::DATE) FROM {scan("daily")})
        ORDER BY dt
    """)
    con.execute(f"CREATE OR REPLACE TABLE basic AS SELECT * FROM {scan('stock_basic')}")
    con.execute(f"""
        CREATE OR REPLACE TABLE d AS
        SELECT a.ts_code, strptime(a.trade_date, '%Y%m%d')::DATE AS dt,
               a.open, a.high, a.low, a.close, a.pre_close, a.vol, a.amount,
               f.adj_factor, l.up_limit, l.down_limit, b.total_mv, b.turnover_rate
        FROM {scan("daily")} a
        LEFT JOIN {scan("adj_factor")} f USING (ts_code, trade_date)
        LEFT JOIN {scan("stk_limit")} l USING (ts_code, trade_date)
        LEFT JOIN {scan("daily_basic")} b USING (ts_code, trade_date)
        ORDER BY ts_code, dt
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE adj AS
        SELECT ts_code, strptime(trade_date, '%Y%m%d')::DATE AS dt, adj_factor
        FROM {scan("adj_factor")}
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE susp AS
        SELECT ts_code, strptime(trade_date, '%Y%m%d')::DATE AS dt, suspend_type, suspend_timing
        FROM {scan("suspend_d")}
    """)


def tushare_scan(raw: Path, dataset: str) -> str:
    """SQL table expression reading one raw Tushare dataset."""
    _files(raw, f"{dataset}/*.parquet")  # fail clearly when the dataset is missing
    return f"read_parquet('{raw / dataset}/*.parquet', union_by_name=true)"
