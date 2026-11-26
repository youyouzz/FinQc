"""Cleaning rules and quality checks on small synthetic raw stores laid out like the real ones."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finqc.ingest import clean, quality

START, END = date(2010, 1, 4), date(2016, 12, 30)


def weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


SESSIONS = weekdays(START, END)


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def bar(sid: str, d: date, close: float, volume: float = 1000.0, **kw: float) -> dict:
    row = {
        "id": sid,
        "date": d.isoformat(),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "splitAdjOpen": close,
        "splitAdjHigh": close,
        "splitAdjLow": close,
        "splitAdjClose": close,
        "splitAdjVolume": volume,
        "adjOpen": close,
        "adjHigh": close,
        "adjLow": close,
        "adjClose": close,
        "adjVolume": volume,
        "marketCap": 1e9,
    }
    return row | kw


def security(
    sid: str, symbol: str, kind: str = "Common Stock", delisted: str | None = None
) -> dict:
    return {
        "id": sid,
        "symbol": symbol,
        "name": symbol,
        "isin": None,
        "figi_composite": sid,
        "figi_share_class": None,
        "mic": "XNAS",
        "currency": "USD",
        "security_type": kind,
        "listed_at": "2000-01-03",
        "delisted_at": delisted,
        "listing_status": "primary",
        "primary_listing": True,
        "is_active": delisted is None,
        "is_archived": False,
        "successor_id": None,
        "sector": None,
        "industry": None,
    }


@pytest.fixture
def us_root(tmp_path: Path) -> Path:
    root = tmp_path / "us"
    first = SESSIONS[:20]
    eod: dict[str, list[dict]] = {}
    # NORM: duplicate row, a Saturday row, a reverting x10 spike, a raw close rounded to 0.00.
    eod["NORM"] = [bar("NORM", d, 10.0) for d in first]
    eod["NORM"] += [bar("NORM", first[3], 10.0), bar("NORM", date(2010, 1, 9), 10.0)]
    eod["NORM"][5] = bar("NORM", first[5], 100.0)
    eod["NORM"][8] = bar("NORM", first[8], 0.0, adjClose=0.3)
    eod["FUND"] = [
        bar("FUND", d, 20.0, volume=0.0) for d in first
    ]  # open-end fund: dropped
    eod["ETF"] = [
        bar("ETF", d, 30.0) for d in first
    ]  # ETF with the same IndexOne type: kept
    # STITCH: another company reuses the id after a two-year gap at ten times the price.
    eod["STITCH"] = [bar("STITCH", d, 1.0) for d in first]
    eod["STITCH"] += [bar("STITCH", d, 10.0) for d in SESSIONS[600:620]]
    # SUSP: same company after a 400-day suspension: one instrument.
    eod["SUSP"] = [bar("SUSP", d, 5.0) for d in first] + [
        bar("SUSP", d, 5.0) for d in SESSIONS[300:320]
    ]
    # OLD: four years without rows at a similar price: still split.
    eod["OLD"] = [bar("OLD", d, 5.0) for d in first] + [
        bar("OLD", d, 6.0) for d in SESSIONS[1100:1110]
    ]
    # DELIST: delisted on session 10; stale zero-volume rows continue afterwards.
    eod["DELIST"] = [
        bar("DELIST", d, 7.0, volume=0.0 if i > 10 else 1000.0)
        for i, d in enumerate(first)
    ]
    # DUPOLD's history was backfilled into DUPNEW, which then continues: DUPOLD is dropped.
    eod["DUPOLD"] = [bar("DUPOLD", d, 3.0 + i / 100) for i, d in enumerate(first)]
    eod["DUPNEW"] = [
        bar("DUPNEW", d, 3.0 + i / 100) for i, d in enumerate(SESSIONS[:25])
    ]
    for i, (sid, rows) in enumerate(eod.items()):
        write(
            root / f"indexone-{i % 3}of3/raw/indexone/i1_core_eod/{sid}.parquet", rows
        )
    refs = [
        security("NORM", "NORM.US"),
        security("FUND", "ABCDX.US", "Mutual Fund"),
        security("ETF", "SPY.US", "Mutual Fund"),
        security("STITCH", "STCH.US"),
        security("SUSP", "SUSP.US"),
        security("OLD", "OLD.US"),
        security("DELIST", "DLST.US", delisted=first[10].isoformat()),
        security("DUPOLD", "DUP.US"),
        security("DUPNEW", "DUP.US"),
    ]
    for i in range(3):
        write(
            root
            / f"indexone-{i}of3/raw/indexone/core_securities_reference/ref.parquet",
            refs,
        )
    write(
        root / "tushare/us_tradecal/cal.parquet",
        [
            {"cal_date": d.strftime("%Y%m%d"), "is_open": 1, "pretrade_date": None}
            for d in SESSIONS
        ],
    )
    (root / "run.json").write_text(
        json.dumps({"start": START.isoformat(), "end": END.isoformat()})
    )
    status = [
        {
            "id": sid,
            "symbol": sid,
            "status": "ok",
            "rows": len(rows),
            "at": "2026-11-26T00:00:00+00:00",
        }
        for sid, rows in eod.items()
    ]
    (root / "eod_status-0of3.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in status)
    )
    return root


def table(path: Path) -> list[dict]:
    rel = duckdb.sql(f"SELECT * FROM '{path}' ORDER BY ALL")
    return [dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()]


def test_indexone_rules(us_root: Path, tmp_path: Path) -> None:
    out = us_root / "clean"
    manifest = clean.run("us", us_root, out, tmp_path / "work")
    counts = manifest["counts"]
    assert counts["dedupe"] == 1
    assert counts["off_calendar"] == 1
    assert counts["open_end_fund_lines"] == 1 and counts["open_end_fund"] == 20
    assert counts["price_spike"] == 1
    assert (
        counts["stitched_history_lines"] == 2
    )  # STITCH (price jump) and OLD (over 3 years)
    assert counts["delisting_tail"] == 9
    assert counts["duplicate_history_instruments"] == 1
    assert counts["duplicate_history"] == 20
    assert manifest["outputs"].keys() == {"daily.parquet", "instruments.parquet"}

    instruments = {r["instrument_id"]: r for r in table(out / "instruments.parquet")}
    assert "FUND" not in instruments and "ETF" in instruments
    assert {
        "STITCH",
        "STITCH@2010-01-04",
        "OLD",
        "OLD@2010-01-04",
        "SUSP",
    } <= instruments.keys()
    assert "SUSP@2010-01-04" not in instruments
    assert "DUPOLD" not in instruments and "DUPNEW" in instruments
    assert instruments["STITCH@2010-01-04"]["delisted_on"] == SESSIONS[20]
    assert instruments["STITCH"]["listed_on"] == SESSIONS[600]
    assert instruments["STITCH"]["delisted_on"] is None
    assert instruments["DELIST"]["delisted_on"] == SESSIONS[11]

    daily = table(out / "daily.parquet")
    norm = {r["session"]: r for r in daily if r["instrument_id"] == "NORM"}
    assert SESSIONS[5] not in norm  # spike dropped
    zero = norm[SESSIONS[8]]
    assert zero["close"] is None and zero["adj_close"] == 0.3
    assert not zero["tradable"] and zero["price_imprecise"] and zero["adj_imprecise"]
    assert norm[SESSIONS[9]]["tradable"] and not norm[SESSIONS[9]]["price_imprecise"]
    for row in daily:
        listing = instruments[row["instrument_id"]]
        assert listing["listed_on"] <= row["session"]
        assert listing["delisted_on"] is None or row["session"] < listing["delisted_on"]


def test_rerun_replaces_output(us_root: Path, tmp_path: Path) -> None:
    out = us_root / "clean"
    first = clean.run("us", us_root, out, tmp_path / "work")
    (out / "stray.txt").write_text("x")
    second = clean.run("us", us_root, out, tmp_path / "work")
    assert first["outputs"] == second["outputs"]  # deterministic output
    assert not (out / "stray.txt").exists()


def test_quality_indexone(us_root: Path, tmp_path: Path) -> None:
    results = {
        r.name: r for r in quality.run("us", us_root, tmp_path / "work", reuse=False)
    }
    assert results["duplicate_keys"].value == 1
    assert results["open_end_funds"].value == 1
    assert results["stitched_histories"].value == 2
    assert results["duplicate_histories"].value == 1
    assert not any(r.failed for r in results.values())

    # A status log that disagrees with the stored rows is a broken download.
    log = us_root / "eod_status-0of3.jsonl"
    log.write_text(log.read_text().replace('"rows": 20', '"rows": 19', 1))
    results = {
        r.name: r for r in quality.run("us", us_root, tmp_path / "work", reuse=False)
    }
    assert results["status_rowcount_mismatch"].failed


@pytest.fixture
def cn_raw(tmp_path: Path) -> Path:
    raw = tmp_path / "full-a" / "raw"
    sessions = [
        date(2021, 11, 10),
        date(2021, 11, 11),
        date(2021, 11, 12),
        date(2021, 11, 15),
        date(2021, 11, 16),
    ]
    ymd = [d.strftime("%Y%m%d") for d in sessions]
    codes = ["600000.SH", "920001.BJ", "000022.SZ"]
    daily, adj, limit, basic_rows = [], [], [], []
    for code in codes:
        for i, day in enumerate(ymd):
            daily.append(
                {
                    "ts_code": code,
                    "trade_date": day,
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "pre_close": 10.0,
                    "vol": 1.0,
                    "amount": 10.0,
                }
            )
            limit.append(
                {
                    "ts_code": code,
                    "trade_date": day,
                    "up_limit": 11.0,
                    "down_limit": 9.0,
                }
            )
            if code == "920001.BJ" and i == 4:
                continue  # missing adjustment factor after the exchange opened
            if code == "000022.SZ":
                continue  # old code: no factors at all
            factor = (
                2.0 if (code == "600000.SH" and i < 3) else 1.0
            )  # factor falls on day 4
            adj.append({"ts_code": code, "trade_date": day, "adj_factor": factor})
    for code in codes[:2]:
        basic_rows.append(
            {
                "ts_code": code,
                "symbol": code[:6],
                "name": code,
                "market": "",
                "exchange": "",
                "curr_type": "CNY",
                "list_status": "L",
                "list_date": "20000101",
                "delist_date": None,
            }
        )
    write(raw / "daily/a.parquet", daily)
    write(raw / "adj_factor/a.parquet", adj)
    write(raw / "stk_limit/a.parquet", limit)
    write(
        raw / "daily_basic/a.parquet",
        [
            {
                "ts_code": r["ts_code"],
                "trade_date": r["trade_date"],
                "turnover_rate": 1.0,
                "total_mv": 1.0,
                "circ_mv": 1.0,
                "pe_ttm": 1.0,
                "pb": 1.0,
                "ps_ttm": 1.0,
                "dv_ttm": 1.0,
            }
            for r in daily
        ],
    )
    write(
        raw / "suspend_d/a.parquet",
        [
            {
                "ts_code": "600000.SH",
                "trade_date": "20211110",
                "suspend_type": "R",
                "suspend_timing": None,
            }
        ],
    )
    write(raw / "stock_basic/a.parquet", basic_rows)
    write(
        raw / "trade_cal/a.parquet",
        [
            {"exchange": "SSE", "cal_date": d, "is_open": 1, "pretrade_date": None}
            for d in ymd
        ],
    )
    return raw


def test_cn_exclusions(cn_raw: Path, tmp_path: Path) -> None:
    out = cn_raw.parent / "clean"
    manifest = clean.run("cn", cn_raw, out, tmp_path / "work")
    rows = table(out / "exclusions.parquet")
    by_rule = {(r["rule"], r["ts_code"]): r for r in rows}
    assert by_rule["not_in_stock_basic", "000022.SZ"]["rows"] == 5
    bj = by_rule["bj_pre_bse", "920001.BJ"]
    assert (bj["start_date"], bj["end_date"], bj["rows"]) == (
        date(2021, 11, 10),
        date(2021, 11, 12),
        3,
    )
    missing = by_rule["missing_adj_factor", "920001.BJ"]
    assert (missing["start_date"], missing["action"]) == (
        date(2021, 11, 16),
        "no_adjusted",
    )
    assert ("missing_adj_factor", "000022.SZ") not in by_rule  # already dropped
    assert by_rule["adj_factor_decrease", "600000.SH"]["start_date"] == date(
        2021, 11, 15
    )
    assert manifest["counts"]["bj_pre_bse"]["rows"] == 3
