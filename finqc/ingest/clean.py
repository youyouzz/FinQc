"""Deterministic cleaning rules for downloaded market data. Raw files are never modified.

HK / US (IndexOne): writes a cleaned daily table and an instrument table to `<root>/clean/`.
A-share (Tushare): the engine's full-A panel builder reads the raw store itself, so this writes
`exclusions.parquet` (ts_code, date range, rule, action) for consumers to apply on top of it.
Every output directory carries `clean.json` with the rule parameters, per-rule counts, the input
identity and the output hashes. Outputs are rebuildable and replaced as a whole on each run.

Rules (see docs/data-quality.md for the evidence behind each one):

IndexOne
  dedupe            keep one row per (id, date)
  no_reference      drop rows whose id is missing from the securities reference
  off_calendar      drop rows on days the Tushare exchange calendar marks closed
  open_end_fund     drop US "Mutual Fund" lines with mutual-fund tickers (4+ letters ending in X):
                    NAV series that never trade on the exchange; ETFs and ETNs are kept
  price_spike       drop one-day raw-close spikes (x5 or /5) that reverse the next day
  stitched_history  split an id where a row gap over 365 days comes with a 3x split-adjusted price
                    jump, or at any gap over 3 years (another company reusing the id); earlier
                    segments become `<id>@<first date>`. Other gaps are long suspensions
  delisting_tail    drop zero-volume rows after `delisted_at` that follow the last traded day
  duplicate_history drop an instrument whose traded closes (>= 80% of them) repeat another
                    instrument of the same symbol: IndexOne backfills old ids into new ones
  zero_price        null raw / adjusted prices that IndexOne's 2-decimal rounding turned into 0
  flags             tradable (volume > 0 and positive open/close); price_imprecise / adj_imprecise
                    when the price is below PRICE_FLOOR, where 2-decimal rounding exceeds 1%

A-share
  not_in_stock_basic   drop codes absent from stock_basic (old codes duplicating a successor)
  bj_pre_bse           drop BJ rows before the Beijing Stock Exchange opened (NEEQ trading)
  missing_adj_factor   rows without an adjustment factor: no adjusted price
  adj_factor_decrease  adjustment factor fell: flag for review / re-download

Usage:

    python -m finqc.ingest.clean hk
    python -m finqc.ingest.clean us
    python -m finqc.ingest.clean cn
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from finqc.ingest import tables

RULESET_VERSION = "1"
PRICE_FLOOR = 0.5  # 0.005 rounding on a 2-decimal price is <= 1% at or above this price
FUND_TICKER = r"^[A-Z]{4,}X\.US$"  # NASDAQ mutual-fund quotation symbols
SPIKE_RATIO = 5.0
STITCH_GAP_DAYS = 365
STITCH_JUMP = 3.0
STITCH_ALWAYS_DAYS = 3 * 365
DUP_OVERLAP = 0.8
DUP_MIN_ROWS = 20
# A different company reusing the id: a row gap over a year across which the split-adjusted price
# jumps STITCH_JUMP-fold, or any gap over three years. Shorter gaps without a jump are long
# suspensions and stay one instrument. Splitting never drops rows, so false positives are cheap.
STITCH = f"""(d - pd > {STITCH_ALWAYS_DAYS}
    OR (d - pd > {STITCH_GAP_DAYS} AND ps > 0 AND sac > 0
        AND (sac > ps * {STITCH_JUMP} OR sac < ps / {STITCH_JUMP})))"""
BSE_OPEN = "2021-11-15"
# A one-day jump by SPIKE_RATIO that reverses the next day; `pc` / `nc` are the neighbour closes.
SPIKE = f"""(pc > 0 AND close > 0 AND nc > 0
    AND ((close > pc * {SPIKE_RATIO} AND nc < close / {SPIKE_RATIO})
         OR (close < pc / {SPIKE_RATIO} AND nc > close * {SPIKE_RATIO})))"""

DEFAULT_ROOTS = {
    "cn": Path("data/market/cn/full-a/raw"),
    "hk": Path("data/market/hk"),
    "us": Path("data/market/us"),
}


def duplicate_losers(source: str) -> str:
    """SQL selecting the instrument ids to drop because another instrument carries the same
    traded history. `source` needs instrument_id, symbol, d, close and volume.

    IndexOne sometimes opens a new id for a line and backfills the old id's history into it, so
    both would be in the universe at once. When two instruments of the same symbol share at
    least DUP_OVERLAP of the shorter one's traded closes (and DUP_MIN_ROWS rows), keep the one
    with more traded rows, then the later last row, then the smaller id.
    """
    return f"""
        WITH t AS (SELECT instrument_id, symbol, d, close FROM {source}
                   WHERE volume > 0 AND close > 0),
        n AS (SELECT instrument_id, count(*) AS traded, max(d) AS last_d FROM t GROUP BY ALL),
        pairs AS (
            SELECT a.instrument_id AS ia, b.instrument_id AS ib, count(*) AS hit
            FROM t a JOIN t b ON a.symbol = b.symbol AND a.d = b.d AND a.close = b.close
                             AND a.instrument_id < b.instrument_id
            GROUP BY ALL HAVING count(*) >= {DUP_MIN_ROWS})
        SELECT DISTINCT CASE
                   WHEN na.traded < nb.traded
                     OR (na.traded = nb.traded AND na.last_d < nb.last_d) THEN p.ia
                   ELSE p.ib END AS instrument_id
        FROM pairs p JOIN n na ON na.instrument_id = p.ia JOIN n nb ON nb.instrument_id = p.ib
        WHERE p.hit >= {DUP_OVERLAP} * least(na.traded, nb.traded)
    """


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0] or 0) if row else 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _input_identity(files: list[str]) -> str:
    """Raw objects are content-addressed, so their sorted names identify the input."""
    return hashlib.sha256(
        "\n".join(sorted(Path(f).name for f in files)).encode()
    ).hexdigest()


def clean_indexone(
    con: duckdb.DuckDBPyConnection, root: Path, staging: Path
) -> dict[str, Any]:
    config = tables.load_indexone(con, root)
    counts: dict[str, int] = {}

    def step(name: str, sql: str, source: str, target: str) -> None:
        con.execute(f"CREATE OR REPLACE TEMP TABLE {target} AS {sql}")
        counts[name] = _count(con, f"SELECT count(*) FROM {source}") - _count(
            con, f"SELECT count(*) FROM {target}"
        )

    con.execute("""
        CREATE OR REPLACE TEMP TABLE c0 AS
        SELECT * FROM eod
        QUALIFY row_number() OVER (PARTITION BY id, d ORDER BY volume DESC NULLS LAST, close, ac) = 1
    """)
    counts["dedupe"] = _count(con, "SELECT count(*) FROM eod") - _count(
        con, "SELECT count(*) FROM c0"
    )
    step(
        "no_reference",
        """SELECT c.*, f.symbol, f.security_type, f.is_active,
                  TRY_CAST(f.delisted_at AS DATE) AS delisted_at
           FROM c0 c JOIN ref f USING (id)""",
        "c0",
        "c1",
    )
    step("off_calendar", "SELECT * FROM c1 WHERE d IN (SELECT d FROM cal)", "c1", "c2")
    step(
        "open_end_fund",
        f"""SELECT * FROM c2 WHERE NOT (security_type = 'Mutual Fund'
                                        AND regexp_matches(symbol, '{FUND_TICKER}'))""",
        "c2",
        "c3",
    )
    counts["open_end_fund_lines"] = _count(
        con,
        f"""SELECT count(*) FROM ref WHERE security_type = 'Mutual Fund'
            AND regexp_matches(symbol, '{FUND_TICKER}')
            AND id IN (SELECT DISTINCT id FROM c2)""",
    )
    step(
        "price_spike",
        f"""SELECT * EXCLUDE (pc, nc) FROM (
                SELECT *, lag(close) OVER w AS pc, lead(close) OVER w AS nc FROM c3
                WINDOW w AS (PARTITION BY id ORDER BY d))
            WHERE NOT {SPIKE}""",
        "c3",
        "c4",
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE c5 AS
        WITH g AS (
            SELECT *, CASE WHEN {STITCH} THEN 1 ELSE 0 END AS brk FROM (
                SELECT *, lag(d) OVER w AS pd, lag(sac) OVER w AS ps FROM c4
                WINDOW w AS (PARTITION BY id ORDER BY d))),
        s AS (SELECT * EXCLUDE (pd, ps), sum(brk) OVER (PARTITION BY id ORDER BY d) AS seg FROM g)
        SELECT * EXCLUDE (brk),
               max(seg) OVER (PARTITION BY id) AS nseg,
               CASE WHEN seg = max(seg) OVER (PARTITION BY id) THEN id
                    ELSE id || '@' || strftime(min(d) OVER (PARTITION BY id, seg), '%Y-%m-%d')
               END AS instrument_id
        FROM s
    """)
    counts["stitched_history_lines"] = _count(
        con, "SELECT count(DISTINCT id) FROM c5 WHERE nseg > 0"
    )
    counts["stitched_history_segments"] = _count(
        con, "SELECT count(DISTINCT instrument_id) FROM c5 WHERE seg < nseg"
    )
    step(
        "delisting_tail",
        """SELECT * EXCLUDE (last_traded) FROM (
               SELECT *, max(CASE WHEN volume > 0 THEN d END)
                         OVER (PARTITION BY instrument_id) AS last_traded
               FROM c5)
           WHERE NOT (seg = nseg AND delisted_at IS NOT NULL AND d > delisted_at
                      AND coalesce(volume, 0) = 0
                      AND d > coalesce(last_traded, DATE '1900-01-01'))""",
        "c5",
        "c6",
    )
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE dup AS {duplicate_losers('(SELECT * FROM c6)')}"
    )
    counts["duplicate_history_instruments"] = _count(con, "SELECT count(*) FROM dup")
    step(
        "duplicate_history",
        "SELECT * FROM c6 WHERE instrument_id NOT IN (SELECT instrument_id FROM dup)",
        "c6",
        "c7",
    )
    counts["zero_price_rows"] = _count(
        con, "SELECT count(*) FROM c7 WHERE close <= 0 OR open <= 0 OR ac <= 0"
    )

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE daily AS
        SELECT instrument_id, id AS source_id, symbol, d AS session,
               CASE WHEN open > 0 THEN open END AS open,
               CASE WHEN high > 0 THEN high END AS high,
               CASE WHEN low > 0 THEN low END AS low,
               CASE WHEN close > 0 THEN close END AS close,
               volume,
               CASE WHEN sac > 0 THEN sac END AS split_adj_close,
               CASE WHEN ao > 0 THEN ao END AS adj_open,
               CASE WHEN ac > 0 THEN ac END AS adj_close,
               mcap AS market_cap,
               coalesce(volume, 0) > 0 AND open > 0 AND close > 0 AS tradable,
               NOT (open >= {PRICE_FLOOR} AND close >= {PRICE_FLOOR}) AS price_imprecise,
               NOT coalesce(ac >= {PRICE_FLOOR}, false) AS adj_imprecise
        FROM c7
    """)
    for flag in ("tradable", "price_imprecise", "adj_imprecise"):
        counts[f"{flag}_rows"] = _count(con, f"SELECT count(*) FROM daily WHERE {flag}")

    con.execute(
        "CREATE OR REPLACE TEMP TABLE calnext AS SELECT d, lead(d) OVER (ORDER BY d) AS nd FROM cal"
    )
    con.execute("""
        CREATE OR REPLACE TEMP TABLE instruments AS
        WITH per AS (
            SELECT instrument_id, any_value(id) AS source_id, any_value(symbol) AS symbol,
                   min(d) AS first_session, max(d) AS last_session, count(*) AS rows,
                   bool_or(seg < nseg OR delisted_at IS NOT NULL OR NOT is_active) AS ended
            FROM c7 GROUP BY instrument_id)
        SELECT p.instrument_id, p.source_id, p.symbol, f.name, f.security_type, f.currency,
               f.mic, f.isin, p.first_session AS listed_on,
               CASE WHEN p.ended THEN coalesce(n.nd, p.last_session + 1) END AS delisted_on,
               p.first_session, p.last_session, p.rows
        FROM per p JOIN ref f ON f.id = p.source_id
        LEFT JOIN calnext n ON n.d = p.last_session
    """)

    staging.mkdir(parents=True)
    con.execute(
        f"COPY (SELECT * FROM daily ORDER BY instrument_id, session) "
        f"TO '{staging / 'daily.parquet'}' (FORMAT parquet, COMPRESSION zstd)"
    )
    con.execute(
        f"COPY (SELECT * FROM instruments ORDER BY instrument_id) "
        f"TO '{staging / 'instruments.parquet'}' (FORMAT parquet, COMPRESSION zstd)"
    )
    eod_files = [
        str(p) for p in root.glob("indexone*/raw/indexone/i1_core_eod/*.parquet")
    ]
    return {
        "window": config,
        "params": {
            "price_floor": PRICE_FLOOR,
            "fund_ticker": FUND_TICKER,
            "spike_ratio": SPIKE_RATIO,
            "stitch_gap_days": STITCH_GAP_DAYS,
            "stitch_jump": STITCH_JUMP,
            "stitch_always_days": STITCH_ALWAYS_DAYS,
            "dup_overlap": DUP_OVERLAP,
            "dup_min_rows": DUP_MIN_ROWS,
        },
        "input": {
            "eod_objects": len(eod_files),
            "eod_identity": _input_identity(eod_files),
        },
        "counts": counts,
        "rows": {
            "input": _count(con, "SELECT count(*) FROM eod"),
            "daily": _count(con, "SELECT count(*) FROM daily"),
            "instruments": _count(con, "SELECT count(*) FROM instruments"),
        },
    }


def clean_cn(
    con: duckdb.DuckDBPyConnection, raw: Path, staging: Path
) -> dict[str, Any]:
    tables.load_tushare_cn(con, raw)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE exclusions AS
        WITH dropped AS (
            SELECT ts_code, dt, 'not_in_stock_basic' AS rule FROM d
            WHERE ts_code NOT IN (SELECT ts_code FROM basic)
            UNION ALL
            SELECT ts_code, dt, 'bj_pre_bse' FROM d
            WHERE ends_with(ts_code, '.BJ') AND dt < DATE '{BSE_OPEN}'
              AND ts_code IN (SELECT ts_code FROM basic)),
        kept AS (SELECT * FROM d ANTI JOIN dropped USING (ts_code, dt)),
        islands AS (
            SELECT ts_code, dt, adj_factor IS NULL AS missing,
                   row_number() OVER (PARTITION BY ts_code ORDER BY dt)
                   - row_number() OVER (PARTITION BY ts_code, adj_factor IS NULL ORDER BY dt) AS grp
            FROM kept),
        decreases AS (
            SELECT ts_code, dt FROM (
                SELECT ts_code, dt, adj_factor,
                       lag(adj_factor) OVER (PARTITION BY ts_code ORDER BY dt) AS prev
                FROM adj)
            WHERE adj_factor < prev * 0.9999)
        SELECT ts_code, min(dt) AS start_date, max(dt) AS end_date, rule, 'drop' AS action,
               count(*) AS rows
        FROM dropped GROUP BY ts_code, rule
        UNION ALL
        SELECT ts_code, min(dt), max(dt), 'missing_adj_factor', 'no_adjusted', count(*)
        FROM islands WHERE missing GROUP BY ts_code, grp
        UNION ALL
        SELECT ts_code, dt, dt, 'adj_factor_decrease', 'review', 1 FROM decreases
    """)
    staging.mkdir(parents=True)
    con.execute(
        f"COPY (SELECT * FROM exclusions ORDER BY ts_code, start_date, rule) "
        f"TO '{staging / 'exclusions.parquet'}' (FORMAT parquet)"
    )
    summary = con.execute("""
        SELECT rule, action, count(DISTINCT ts_code), count(*), sum(rows)
        FROM exclusions GROUP BY ALL ORDER BY rule
    """).fetchall()
    daily_files = [str(p) for p in raw.glob("daily/*.parquet")]
    return {
        "params": {"bse_open": BSE_OPEN},
        "input": {
            "daily_objects": len(daily_files),
            "daily_identity": _input_identity(daily_files),
        },
        "counts": {
            rule: {
                "action": action,
                "stocks": stocks,
                "ranges": ranges,
                "rows": int(rows),
            }
            for rule, action, stocks, ranges, rows in summary
        },
        "rows": {"daily": _count(con, "SELECT count(*) FROM d")},
    }


def run(market: str, root: Path, out: Path, work: Path) -> dict[str, Any]:
    staging = out.with_name(out.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    con = tables.connect(work / f"{market}-clean.duckdb")
    try:
        if market == "cn":
            manifest = clean_cn(con, root, staging)
        else:
            manifest = clean_indexone(con, root, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        con.close()
    manifest = {
        "market": market,
        "ruleset_version": RULESET_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **manifest,
        "outputs": {p.name: _sha256(p) for p in sorted(staging.glob("*.parquet"))},
    }
    (staging / "clean.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n"
    )
    if out.exists():
        shutil.rmtree(out)
    staging.rename(out)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("market", choices=sorted(DEFAULT_ROOTS))
    parser.add_argument("--root", type=Path, help="raw data root (defaults per market)")
    parser.add_argument(
        "--out", type=Path, help="output directory (default: <market root>/clean)"
    )
    parser.add_argument(
        "--work", type=Path, default=Path("data/runs/qc"), help="scratch DuckDB dir"
    )
    args = parser.parse_args()
    root = (args.root or DEFAULT_ROOTS[args.market]).resolve()
    out = args.out or (root.parent / "clean" if args.market == "cn" else root / "clean")
    manifest = run(args.market, root, out.resolve(), args.work.resolve())
    print(
        json.dumps(
            {k: manifest[k] for k in ("market", "counts", "rows")},
            indent=2,
            default=str,
        )
    )
    for suffix in ("", ".wal"):  # scratch tables only; outputs are in `out`
        (args.work / f"{args.market}-clean.duckdb{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
