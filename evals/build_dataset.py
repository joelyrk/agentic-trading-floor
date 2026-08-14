"""Build the checked-in historical-v1 fixture from the cited source CSV.

This is a maintainer utility; replay itself is completely offline. It selects
adjusted daily closes for AAPL, MSFT, NVDA, and XOM, derives an equal-weight
SPX proxy, and writes 30 one-session scenarios with five-session trailing data.
"""

import argparse
import csv
import hashlib
import json
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SYMBOLS = ("AAPL", "MSFT", "NVDA", "XOM")


def _dump(path: Path, value) -> str:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def build(source: Path, output: Path) -> None:
    with source.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    usable = [
        row for row in rows if row["Date"].startswith("2014-") and all(row[s] for s in SYMBOLS)
    ]
    usable.sort(key=lambda row: row["Date"])
    selected = usable[-36:]
    if len(selected) != 36:
        raise ValueError("source must contain at least 36 complete 2014 sessions")

    prices: list[dict[str, Decimal]] = []
    for row in selected:
        values = {symbol: Decimal(row[symbol]) for symbol in SYMBOLS}
        values["SPX"] = sum(values.values()) / Decimal(len(values))
        prices.append(values)

    decisions, outcomes = [], []
    for index in range(5, 35):
        row = selected[index]
        day = datetime.strptime(row["Date"], "%Y-%m-%d").date()
        market_at = datetime.combine(day, time(21), tzinfo=timezone.utc)
        decision_at = market_at + timedelta(minutes=5)
        scenario_id = f"hist-2014-{index - 4:02d}"
        trailing = {
            symbol: str(prices[index][symbol] / prices[index - 5][symbol] - 1)
            for symbol in prices[index]
        }
        price_payload = {symbol: str(value) for symbol, value in prices[index].items()}
        decisions.append(
            {
                "schema_version": "1.0",
                "scenario_id": scenario_id,
                "decision_at": decision_at.isoformat(),
                "market_timestamp": market_at.isoformat(),
                "retrieved_at": (market_at + timedelta(minutes=1)).isoformat(),
                "prices": price_payload,
                "trailing_returns": trailing,
                "sources": [
                    {
                        "source_id": f"{scenario_id}-price-trend",
                        "published_at": market_at.isoformat(),
                        "retrieved_at": (market_at + timedelta(minutes=1)).isoformat(),
                        "sentiment": str(
                            sum(Decimal(trailing[s]) for s in SYMBOLS) / Decimal(len(SYMBOLS))
                        ),
                    }
                ],
                "benchmark_symbol": "SPX",
            }
        )
        next_row = selected[index + 1]
        next_day = datetime.strptime(next_row["Date"], "%Y-%m-%d").date()
        outcomes.append(
            {
                "schema_version": "1.0",
                "scenario_id": scenario_id,
                "outcome_at": datetime.combine(next_day, time(21), tzinfo=timezone.utc).isoformat(),
                "prices": {symbol: str(value) for symbol, value in prices[index + 1].items()},
            }
        )

    output.mkdir(parents=True, exist_ok=True)
    decision_hash = _dump(output / "decision_fixtures.json", decisions)
    outcome_hash = _dump(output / "outcome_fixtures.json", outcomes)
    manifest = {
        "schema_version": "1.0",
        "dataset_id": "historical-v1",
        "dataset_version": "1.0.0",
        "description": "Thirty one-session historical replay scenarios from adjusted 2014 closes; SPX is an explicitly derived equal-weight proxy, not the S&P 500 index.",
        "scenario_count": 30,
        "symbols": [*SYMBOLS, "SPX"],
        "decision_fixtures": {
            "path": "decision_fixtures.json",
            "sha256": decision_hash,
        },
        "outcome_fixtures": {"path": "outcome_fixtures.json", "sha256": outcome_hash},
    }
    _dump(output / "manifest.json", manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
