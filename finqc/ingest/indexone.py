"""Shared IndexOne end-of-day downloader used by the per-market ingest modules.

Every response is stored by TradeMaster's `IndexOneDataSource` as immutable Parquet keyed by
request hash, so re-running only fetches what is still missing. Each worker shard owns its own
IndexOne cache (DuckDB allows a single writer) and appends one JSON line per security to its own
status log; the latest line per security across all logs is the security's current status.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from trademaster.data import IndexOneDataSource
from trademaster.data.indexone_client import IndexOneCredentialError

START = date(2010, 1, 1)
TUSHARE_ROW_CAP = 2_000  # trade-calendar endpoints return at most 2,000 rows per call
SECURITY_FIELDS = (
    "id",
    "symbol",
    "name",
    "isin",
    "figi_composite",
    "figi_share_class",
    "mic",
    "currency",
    "security_type",
    "listed_at",
    "delisted_at",
    "listing_status",
    "primary_listing",
    "is_active",
    "is_archived",
    "successor_id",
    "sector",
    "industry",
)
EOD_FIELDS = (
    "id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "splitAdjOpen",
    "splitAdjHigh",
    "splitAdjLow",
    "splitAdjClose",
    "splitAdjVolume",
    "adjOpen",
    "adjHigh",
    "adjLow",
    "adjClose",
    "adjVolume",
    "marketCap",
)

Universe = Callable[[IndexOneDataSource], list[dict[str, Any]]]


def log(**event: Any) -> None:
    print(json.dumps(event, ensure_ascii=False, default=str), flush=True)


def run_config(root: Path, end: date | None) -> dict[str, str]:
    """Pin the request window on first run so resumed runs hit the same cached requests."""
    path = root / "run.json"
    if path.exists():
        config: dict[str, str] = json.loads(path.read_text())
        if end is not None and end.isoformat() != config["end"]:
            raise SystemExit(
                f"run.json pins end={config['end']}; use a new --root for a new window"
            )
        return config
    config = {
        "start": START.isoformat(),
        "end": (end or datetime.now(UTC).date()).isoformat(),
    }
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    return config


def save_reference(root: Path, name: str, table: pa.Table) -> Path:
    """Write an immutable, content-addressed Parquet object for Tushare reference data."""
    folder = root / "tushare" / name
    folder.mkdir(parents=True, exist_ok=True)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    payload = sink.getvalue().to_pybytes()
    path = folder / f"{hashlib.sha256(payload).hexdigest()}.parquet"
    if not path.exists():
        path.write_bytes(payload)
    return path


def tushare_calendar(pro: Any, api: str, config: dict[str, str]) -> pa.Table:
    """Fetch a Tushare trade calendar one year at a time to stay under the row cap."""
    years = []
    for year in range(int(config["start"][:4]), int(config["end"][:4]) + 1):
        chunk = pro.query(
            api,
            start_date=max(f"{year}0101", config["start"].replace("-", "")),
            end_date=min(f"{year}1231", config["end"].replace("-", "")),
        )
        if len(chunk) >= TUSHARE_ROW_CAP:
            raise RuntimeError(f"{api} {year} hit the Tushare row cap")
        years.append(pa.Table.from_pandas(chunk, preserve_index=False))
    return pa.concat_tables(years).sort_by("cal_date")


def securities(source: IndexOneDataSource, mics: Sequence[str]) -> list[dict[str, Any]]:
    """Every security line ever listed on the given exchanges, delisted lines included."""
    rows: list[dict[str, Any]] = []
    for mic in mics:
        table = source.query(
            "securities",
            params={
                "filters": [{"field": "mic", "operator": "eq", "value": mic}],
                "is_active": False,  # keep delisted lines
                "is_listed": False,  # no point-in-time screen: every line that ever traded
                "drop_duplicates": False,
            },
            fields=SECURITY_FIELDS,
            page_limit=200_000,
        )
        rows.extend(table.to_pylist())
    return rows


def _last_status(root: Path) -> dict[str, dict[str, Any]]:
    """Latest record per security across every shard's status log."""
    latest: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("eod_status*.jsonl")):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record["id"] not in latest or record["at"] >= latest[record["id"]]["at"]:
                latest[record["id"]] = record
    return latest


def download(
    root: Path,
    config: dict[str, str],
    universe: Universe,
    limit: int | None,
    shard: tuple[int, int],
) -> int:
    """Fetch EOD history for this shard's pending securities; return the failure count.

    `universe` must return a deterministic order: shards take every n-th entry of it.
    """
    index, count = shard
    suffix = "" if count == 1 else f"-{index}of{count}"
    status_path = root / f"eod_status{suffix}.jsonl"
    done = {k for k, v in _last_status(root).items() if v["status"] in ("ok", "empty")}
    with IndexOneDataSource(root=root / f"indexone{suffix}") as source:
        selected = universe(source)
        mine = [r for i, r in enumerate(selected) if i % count == index]
        log(
            step="universe",
            shard=f"{index}/{count}",
            securities=len(selected),
            shard_size=len(mine),
            types=Counter(r["security_type"] for r in selected).most_common(),
            already_done=len(done & {r["id"] for r in mine}),
        )
        todo = [r for r in mine if r["id"] not in done][:limit]
        failures = 0
        started = time.monotonic()
        with status_path.open("a") as status:
            for i, sec in enumerate(todo, 1):
                record: dict[str, Any] = {"id": sec["id"], "symbol": sec["symbol"]}
                try:
                    result = source.query_with_evidence(
                        "eod",
                        params={
                            "filters": [
                                {"field": "id", "operator": "eq", "value": sec["id"]},
                                {
                                    "field": "date",
                                    "operator": "gte",
                                    "value": config["start"],
                                },
                                {
                                    "field": "date",
                                    "operator": "lte",
                                    "value": config["end"],
                                },
                            ]
                        },
                        fields=EOD_FIELDS,
                        page_limit=20_000,
                    )
                except IndexOneCredentialError:
                    raise  # wrong key or plan: stop instead of failing every security
                # Transient network, row ceiling or schema errors: record and retry on the next run.
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    record |= {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                else:
                    dates = [str(d) for d in result.table.column("date").to_pylist()]
                    record |= {
                        "status": "ok" if dates else "empty",
                        "rows": len(dates),
                        "first": min(dates) if dates else None,
                        "last": max(dates) if dates else None,
                        "content_sha256": result.evidence.content_sha256,
                    }
                record["at"] = datetime.now(UTC).isoformat(timespec="seconds")
                status.write(json.dumps(record, ensure_ascii=False) + "\n")
                status.flush()
                if i % 25 == 0 or i == len(todo):
                    rate = i / max(time.monotonic() - started, 1e-9) * 60
                    log(
                        step="eod",
                        done=i,
                        of=len(todo),
                        failures=failures,
                        per_min=round(rate, 1),
                    )
    return failures


def summary(root: Path) -> None:
    latest = _last_status(root)
    counts = Counter(v["status"] for v in latest.values())
    rows = sum(v.get("rows", 0) for v in latest.values())
    log(step="summary", securities=len(latest), statuses=dict(counts), eod_rows=rows)


def cli(
    doc: str | None,
    default_root: str,
    universe: Universe,
    fetch_reference: Callable[[Path, dict[str, str]], None],
) -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description=doc, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=Path(default_root))
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument(
        "--limit", type=int, help="download at most N pending securities"
    )
    parser.add_argument(
        "--summary", action="store_true", help="print status without network access"
    )
    parser.add_argument("--skip-reference", action="store_true")
    parser.add_argument(
        "--shard", default="0/1", help="i/n: process every n-th security from i"
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if args.summary:
        summary(root)
        return
    index, count = (int(x) for x in args.shard.split("/"))
    if not 0 <= index < count:
        raise SystemExit("--shard must be i/n with 0 <= i < n")
    config = run_config(root, args.end)
    log(step="start", root=str(root), shard=args.shard, **config)
    if not args.skip_reference:
        fetch_reference(root, config)
    failures = download(root, config, universe, args.limit, (index, count))
    summary(root)
    sys.exit(1 if failures else 0)
