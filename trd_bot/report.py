"""Deterministic research reports; never interpret synthetic results as performance."""
import csv
import json
import logging
import math
import os
from pathlib import Path

from .core import atomic_json

LOG = logging.getLogger("trd_bot")
TRADE_COLUMNS = ("id", "order_id", "received_ms", "side", "price", "amount", "fee", "status")
EQUITY_COLUMNS = ("event_id", "received_ms", "cash", "base", "equity", "peak", "drawdown", "position_fraction")
REJECTION_COLUMNS = ("event_id", "received_ms", "reason", "kind")


def _csv(path, rows, columns):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def export_report(directory: Path, summary: dict, fills: list[dict], audit: list[dict],
                  equity: list[dict], coverage: dict) -> dict:
    source = str(summary.get("source", "unknown"))
    data_kind = str(coverage.get("data_kind", summary.get("data_kind", source)))
    synthetic = "synthetic" in data_kind.lower() or data_kind == "demo"
    label = ("合成資料／非歷史績效" if synthetic else
             "公開行情研究資料／本機模擬成交" if source == "binance_public" else
             "重播或未分類研究資料／來源請見資料覆蓋紀錄")
    report = dict(summary)
    report.update(report_schema=2, mode="paper_only", data_coverage=coverage,
                  trades=fills, audit=audit, equity_curve=equity,
                  research_limits={
                      "data_label": label,
                      "synthetic_data": synthetic,
                      "profitability_verified": False,
                      "live_execution_verified": False,
                      "fill_model_verified": False,
                      "caveat": "成交量參與及排隊為近似；結果不證明獲利、實盤安全或真實成交率。",
                  })
    # Canonical key order and Decimal strings make repeated exports byte-identical.
    report = json.loads(json.dumps(report, default=str, sort_keys=True, allow_nan=False))
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _csv(directory / "trades.csv", fills, TRADE_COLUMNS)
        _csv(directory / "equity.csv", equity, EQUITY_COLUMNS)
        rejected = [row for row in audit if "reject" in str(row.get("kind", "")).lower()]
        _csv(directory / "rejections.csv", rejected, REJECTION_COLUMNS)
        atomic_json(directory / "report.json", report)
    except (OSError, ValueError):
        LOG.exception("Research report export failed: %s", directory)
        raise
    LOG.info("Research report exported: %s; fills=%s", directory, len(fills))
    return report


def compare_results(results: dict[str, dict]) -> dict:
    if "no_trade" not in results:
        raise ValueError("Comparison requires a no_trade baseline")
    return {"no_trade": results["no_trade"],
            "scenarios": {name: results[name] for name in sorted(results) if name != "no_trade"}}


def split_events(events: list[dict], split_fraction=.7) -> tuple[list[dict], list[dict]]:
    """Chronological split; equal receive times stay together, with no tuning."""
    if (isinstance(split_fraction, bool) or not isinstance(split_fraction, (int, float))
            or not math.isfinite(split_fraction) or not 0 < split_fraction < 1):
        raise ValueError("split_fraction must be between zero and one")
    previous = -1
    for event in events:
        stamp = event.get("received_ms")
        if type(stamp) is not int or stamp < 0 or stamp < previous:
            raise ValueError("Events require ordered integer received_ms")
        previous = stamp
    boundary = int(len(events) * split_fraction)
    while 0 < boundary < len(events) and events[boundary - 1]["received_ms"] == events[boundary]["received_ms"]:
        boundary += 1
    return events[:boundary], events[boundary:]
