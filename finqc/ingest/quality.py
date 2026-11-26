"""Data-quality checks for downloaded market data (A-share Tushare, HK / US IndexOne).

Each check counts offending rows (or securities) in the raw download. Severity:

  error  the download itself is broken (missing sessions, duplicates, status mismatch): re-download
  warn   a known source defect; `handled_by` names the `finqc.ingest.clean` rule that handles it
  info   a property strategies must respect (zero-volume days, gaps), not a defect

The run exits non-zero when any error-level check is non-zero and no cleaning rule handles it.
The report is printed and written as JSON next to the scratch DuckDB file.

Usage:

    python -m finqc.ingest.quality cn
    python -m finqc.ingest.quality hk
    python -m finqc.ingest.quality us --reuse   # reuse the loaded work tables from the last run
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from finqc.ingest import tables
from finqc.ingest.clean import (
    BSE_OPEN,
    DEFAULT_ROOTS,
    FUND_TICKER,
    PRICE_FLOOR,
    SPIKE,
    STITCH,
    STITCH_GAP_DAYS,
    duplicate_losers,
)

Severity = str  # "error" | "warn" | "info"


@dataclass(frozen=True)
class Spec:
    name: str
    severity: Severity
    sql: str  # returns one integer
    handled_by: str | None = None
    unit: str = "rows"


@dataclass(frozen=True)
class Result:
    name: str
    severity: Severity
    value: int
    unit: str
    handled_by: str | None

    @property
    def failed(self) -> bool:
        return self.severity == "error" and self.value > 0 and self.handled_by is None


_named = "(SELECT e.id AS instrument_id, f.symbol, e.d, e.close, e.volume FROM eod e JOIN ref f USING (id))"


def indexone_checks(end: str) -> list[Spec]:
    life = "(SELECT id, min(d) f, max(d) l, count(*) n FROM eod GROUP BY id)"
    return [
        Spec(
            "status_failed",
            "error",
            "SELECT count(*) FROM status WHERE status = 'failed'",
            unit="securities",
        ),
        Spec(
            "status_rowcount_mismatch",
            "error",
            """
            SELECT count(*) FROM status s
            LEFT JOIN (SELECT id, count(*) n FROM eod GROUP BY id) e USING (id)
            WHERE coalesce(e.n, 0) <> coalesce(s.rows, 0)""",
            unit="securities",
        ),
        Spec(
            "data_without_status",
            "error",
            "SELECT count(DISTINCT id) FROM eod WHERE id NOT IN (SELECT id FROM status)",
            unit="securities",
        ),
        Spec(
            "out_of_window",
            "error",
            f"""
            SELECT count(*) FROM eod WHERE d < (SELECT min(d) FROM cal) OR d > DATE '{end}'""",
        ),
        Spec(
            "duplicate_keys",
            "warn",
            "SELECT count(*) FROM (SELECT id, d FROM eod GROUP BY ALL HAVING count(*) > 1)",
            "dedupe",
        ),
        Spec(
            "no_reference",
            "warn",
            "SELECT count(*) FROM eod WHERE id NOT IN (SELECT id FROM ref)",
            "no_reference",
        ),
        Spec(
            "off_calendar",
            "warn",
            "SELECT count(*) FROM eod WHERE d NOT IN (SELECT d FROM cal)",
            "off_calendar",
        ),
        Spec(
            "sessions_without_data",
            "warn",
            "SELECT count(*) FROM cal WHERE d NOT IN (SELECT DISTINCT d FROM eod)",
            unit="sessions",
        ),
        Spec(
            "thin_sessions",
            "warn",
            """
            WITH p AS (SELECT c.d, count(e.id) n FROM cal c JOIN eod e USING (d) GROUP BY c.d),
                 m AS (SELECT n, median(n) OVER (
                           ORDER BY d ROWS BETWEEN 10 PRECEDING AND 10 FOLLOWING) med FROM p)
            SELECT count(*) FROM m WHERE n < 0.5 * med""",
            unit="sessions",
        ),
        Spec("null_close", "error", "SELECT count(*) FROM eod WHERE close IS NULL"),
        Spec("high_below_low", "warn", "SELECT count(*) FROM eod WHERE high < low"),
        Spec(
            "close_outside_range",
            "info",
            """SELECT count(*) FROM eod
               WHERE close > 0 AND (close > high * 1.0001 OR close < low * 0.9999)""",
        ),
        Spec(
            "nonpositive_price",
            "warn",
            "SELECT count(*) FROM eod WHERE close <= 0 OR open <= 0 OR ac <= 0",
            "zero_price",
        ),
        Spec(
            "price_below_floor",
            "warn",
            f"SELECT count(*) FROM eod WHERE close > 0 AND close < {PRICE_FLOOR}",
            "flags.price_imprecise",
        ),
        Spec(
            "adj_below_floor",
            "warn",
            f"SELECT count(*) FROM eod WHERE ac > 0 AND ac < {PRICE_FLOOR}",
            "flags.adj_imprecise",
        ),
        Spec(
            "adj_return_gt_100pct",
            "warn",
            """
            SELECT count(*) FROM (SELECT ac, lag(ac) OVER (PARTITION BY id ORDER BY d) p FROM eod)
            WHERE p > 0 AND ac > 0 AND abs(ac / p - 1) > 1""",
            "flags.adj_imprecise",
        ),
        Spec(
            "reverting_spikes",
            "warn",
            f"""
            SELECT count(*) FROM (
                SELECT close, lag(close) OVER w pc, lead(close) OVER w nc FROM eod
                WINDOW w AS (PARTITION BY id ORDER BY d))
            WHERE {SPIKE}""",
            "price_spike",
        ),
        Spec(
            "open_end_funds",
            "warn",
            f"""
            SELECT count(*) FROM ref
            WHERE security_type = 'Mutual Fund' AND regexp_matches(symbol, '{FUND_TICKER}')
              AND id IN (SELECT DISTINCT id FROM eod)""",
            "open_end_fund",
            "securities",
        ),
        Spec(
            "long_row_gaps",
            "info",
            f"""
            SELECT count(DISTINCT id) FROM (
                SELECT id, d - lag(d) OVER (PARTITION BY id ORDER BY d) g FROM eod)
            WHERE g > {STITCH_GAP_DAYS}""",
            unit="securities",
        ),
        Spec(
            "stitched_histories",
            "warn",
            f"""
            SELECT count(DISTINCT id) FROM (
                SELECT id, d, sac, lag(d) OVER w pd, lag(sac) OVER w ps FROM eod
                WINDOW w AS (PARTITION BY id ORDER BY d))
            WHERE {STITCH}""",
            "stitched_history",
            "securities",
        ),
        Spec(
            "duplicate_histories",
            "warn",
            f"SELECT count(*) FROM ({duplicate_losers(_named)})",
            "duplicate_history",
            "securities",
        ),
        Spec(
            "stale_rows_after_delisting",
            "warn",
            """
            SELECT count(*) FROM eod e JOIN ref f USING (id)
            WHERE TRY_CAST(f.delisted_at AS DATE) < e.d AND coalesce(e.volume, 0) = 0""",
            "delisting_tail",
        ),
        Spec(
            "traded_rows_after_delisting",
            "info",
            """
            SELECT count(*) FROM eod e JOIN ref f USING (id)
            WHERE TRY_CAST(f.delisted_at AS DATE) < e.d AND e.volume > 0""",
        ),
        Spec(
            "zero_volume_rows",
            "info",
            "SELECT count(*) FROM eod WHERE coalesce(volume, 0) = 0",
            "flags.tradable",
        ),
        Spec(
            "missing_sessions_inside_life",
            "info",
            f"""
            SELECT sum(x.expected - x.n) FROM (
                SELECT s.id, s.n, count(c.d) expected
                FROM {life} s JOIN cal c ON c.d BETWEEN s.f AND s.l GROUP BY ALL) x""",
        ),
        Spec(
            "active_lines_last_row_old",
            "info",
            f"""
            SELECT count(*) FROM {life} e JOIN ref f USING (id)
            WHERE f.is_active AND f.delisted_at IS NULL AND e.l < DATE '{end}' - INTERVAL 5 DAY""",
            unit="securities",
        ),
    ]


def cn_checks(raw: Path) -> list[Spec]:
    def scan(dataset: str) -> str:
        return tables.tushare_scan(raw, dataset)

    specs = [
        Spec(
            "sessions_missing",
            "error",
            "SELECT count(*) FROM cal WHERE dt NOT IN (SELECT DISTINCT dt FROM d)",
            unit="sessions",
        ),
        Spec(
            "off_calendar_days",
            "error",
            "SELECT count(DISTINCT dt) FROM d WHERE dt NOT IN (SELECT dt FROM cal)",
            unit="sessions",
        ),
        Spec(
            "daily_duplicates",
            "error",
            "SELECT count(*) FROM (SELECT ts_code, dt FROM d GROUP BY ALL HAVING count(*) > 1)",
        ),
        Spec(
            "adj_factor_duplicates",
            "error",
            "SELECT count(*) FROM (SELECT ts_code, dt FROM adj GROUP BY ALL HAVING count(*) > 1)",
        ),
        Spec(
            "null_ohlc",
            "error",
            """SELECT count(*) FROM d
               WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL""",
        ),
        Spec(
            "nonpositive_price",
            "error",
            "SELECT count(*) FROM d WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0",
        ),
        Spec(
            "ohlc_inconsistent",
            "error",
            """
            SELECT count(*) FROM d WHERE high < low OR close > high + 1e-6 OR close < low - 1e-6
                                      OR open > high + 1e-6 OR open < low - 1e-6""",
        ),
        Spec(
            "not_in_stock_basic",
            "warn",
            """SELECT count(DISTINCT ts_code) FROM d
               WHERE ts_code NOT IN (SELECT ts_code FROM basic)""",
            "not_in_stock_basic",
            "stocks",
        ),
        Spec(
            "bj_pre_bse_rows",
            "warn",
            f"SELECT count(*) FROM d WHERE ends_with(ts_code, '.BJ') AND dt < DATE '{BSE_OPEN}'",
            "bj_pre_bse",
        ),
        Spec(
            "missing_adj_factor",
            "warn",
            "SELECT count(*) FROM d WHERE adj_factor IS NULL",
            "missing_adj_factor",
        ),
        Spec(
            "missing_stk_limit", "info", "SELECT count(*) FROM d WHERE up_limit IS NULL"
        ),
        Spec(
            "missing_daily_basic",
            "info",
            "SELECT count(*) FROM d WHERE total_mv IS NULL",
        ),
        Spec(
            "adj_factor_decrease",
            "warn",
            """
            SELECT count(*) FROM (
                SELECT adj_factor a, lag(adj_factor) OVER (PARTITION BY ts_code ORDER BY dt) p
                FROM adj)
            WHERE a < p * 0.9999""",
            "adj_factor_decrease",
        ),
        Spec(
            "pre_close_mismatch",
            "warn",
            """
            SELECT count(*) FROM (
                SELECT pre_close, adj_factor, lag(close) OVER w pc, lag(adj_factor) OVER w pa FROM d
                WINDOW w AS (PARTITION BY ts_code ORDER BY dt))
            WHERE pc IS NOT NULL AND pre_close > 0 AND adj_factor > 0
              AND abs(pre_close - pc * pa / adj_factor) / pre_close > 0.005""",
        ),
        Spec(
            "close_beyond_limit",
            "warn",
            """
            SELECT count(*) FROM (
                SELECT *, row_number() OVER (PARTITION BY ts_code ORDER BY dt) k FROM d)
            WHERE k > 5 AND up_limit IS NOT NULL
              AND (close > up_limit + 0.011 OR close < down_limit - 0.011)""",
        ),
        Spec(
            "suspended_but_traded",
            "info",
            """
            SELECT count(*) FROM d JOIN susp s USING (ts_code, dt)
            WHERE s.suspend_type = 'S' AND s.suspend_timing IS NULL AND d.vol > 0""",
        ),
        Spec(
            "unexplained_missing_rows",
            "info",
            """
            WITH sp AS (SELECT ts_code, min(dt) f, max(dt) l FROM d GROUP BY 1)
            SELECT count(*) FROM (
                SELECT sp.ts_code, c.dt FROM sp JOIN cal c ON c.dt BETWEEN sp.f AND sp.l
                ANTI JOIN d ON d.ts_code = sp.ts_code AND d.dt = c.dt
                ANTI JOIN susp ON susp.ts_code = sp.ts_code AND susp.dt = c.dt)""",
        ),
        Spec(
            "listed_last_row_old",
            "info",
            """
            SELECT count(*) FROM basic b
            JOIN (SELECT ts_code, max(dt) l FROM d GROUP BY 1) x USING (ts_code)
            WHERE b.list_status = 'L' AND x.l < (SELECT max(dt) FROM cal) - INTERVAL 7 DAY""",
            unit="stocks",
        ),
        Spec(
            "index_daily_missing_sessions",
            "error",
            f"""
            WITH i AS (SELECT ts_code, strptime(trade_date, '%Y%m%d')::DATE dt
                       FROM {scan("index_daily")}),
                 f AS (SELECT ts_code, min(dt) f FROM i GROUP BY 1)
            SELECT count(*) FROM f JOIN cal c ON c.dt >= f.f
            ANTI JOIN i ON i.ts_code = f.ts_code AND i.dt = c.dt""",
        ),
    ]
    for ds in ("income", "balancesheet", "cashflow", "fina_indicator"):
        key = "ts_code, end_date, ann_date, update_flag" + (
            ", f_ann_date, report_type" if ds != "fina_indicator" else ""
        )
        specs += [
            Spec(
                f"{ds}_duplicate_rows",
                "warn",
                f"""SELECT count(*) FROM (
                    SELECT {key} FROM {scan(ds)} GROUP BY ALL HAVING count(*) > 1)""",
            ),
            Spec(
                f"{ds}_null_ann_date",
                "error",
                f"SELECT count(*) FROM {scan(ds)} WHERE ann_date IS NULL",
            ),
            Spec(
                f"{ds}_future_ann_date",
                "error",
                f"""
                SELECT count(*) FROM {scan(ds)}
                WHERE strptime(ann_date, '%Y%m%d')::DATE > (SELECT max(dt) FROM cal)""",
            ),
        ]
    return specs


def run(market: str, root: Path, work: Path, reuse: bool) -> list[Result]:
    con = tables.connect(work / f"{market}.duckdb")
    loaded = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    try:
        if market == "cn":
            if not (reuse and "d" in loaded):
                tables.load_tushare_cn(con, root)
            specs = cn_checks(root)
        else:
            if reuse and "eod" in loaded:
                config = json.loads((root / "run.json").read_text())
            else:
                config = tables.load_indexone(con, root)
            specs = indexone_checks(config["end"])
        results = []
        for spec in specs:
            row = con.execute(spec.sql).fetchone()
            value = int(row[0] or 0) if row else 0
            results.append(
                Result(spec.name, spec.severity, value, spec.unit, spec.handled_by)
            )
    finally:
        con.close()
    return results


def report(market: str, root: Path, results: list[Result]) -> str:
    lines = [
        f"# {market} data quality ({root})",
        "",
        "| check | severity | value | unit | handled by |",
        "|---|---|---:|---|---|",
    ]
    for r in results:
        mark = " **FAIL**" if r.failed else ""
        lines.append(
            f"| {r.name} | {r.severity}{mark} | {r.value:,} | {r.unit} | {r.handled_by or ''} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("market", choices=sorted(DEFAULT_ROOTS))
    parser.add_argument("--root", type=Path, help="raw data root (defaults per market)")
    parser.add_argument(
        "--work", type=Path, default=Path("data/runs/qc"), help="scratch DuckDB dir"
    )
    parser.add_argument(
        "--reuse", action="store_true", help="reuse work tables loaded by the last run"
    )
    args = parser.parse_args()
    root = (args.root or DEFAULT_ROOTS[args.market]).resolve()
    results = run(args.market, root, args.work.resolve(), args.reuse)
    print(report(args.market, root, results))
    out = args.work / f"{args.market}-quality.json"
    out.write_text(
        json.dumps(
            {
                "market": args.market,
                "root": str(root),
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "results": [asdict(r) | {"failed": r.failed} for r in results],
            },
            indent=2,
        )
        + "\n"
    )
    sys.exit(1 if any(r.failed for r in results) else 0)


if __name__ == "__main__":
    main()
