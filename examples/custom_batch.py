"""A tiny in-memory batch producer that emits an explicit receipt sidecar."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from batch_receipts import write_receipt


_COUNTER_NAMES = (
    "fetched",
    "parsed",
    "rejected",
    "attempted",
    "inserted",
    "updated",
    "unchanged",
    "skipped",
    "dead_lettered",
    "failed",
)


def process(records: list[dict[str, Any]], destination: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Apply each valid record to a small in-memory destination and count each outcome."""
    counts = {name: 0 for name in _COUNTER_NAMES}
    for record in records:
        counts["fetched"] += 1
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or "value" not in record:
            counts["rejected"] += 1
            continue

        counts["parsed"] += 1
        counts["attempted"] += 1
        value = record["value"]
        if identifier not in destination:
            destination[identifier] = {"value": value}
            counts["inserted"] += 1
        elif destination[identifier]["value"] == value:
            counts["unchanged"] += 1
        else:
            destination[identifier] = {"value": value}
            counts["updated"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt_path", type=Path, help="explicit output path for the receipt JSON")
    arguments = parser.parse_args()

    destination = {"order-100": {"value": 50}}
    records = [
        {"id": "order-100", "value": 50},
        {"id": "order-101", "value": 75},
    ]
    counts = process(records, destination)
    receipt = {
        "format": "batch-receipts/v1",
        "profile": "record-preserving/same-unit/v1",
        "job": "example-order-import",
        "run": "local-run-001",
        "unit": "order-record",
        "outcome": "success",
        "counts": counts,
    }
    output = write_receipt(receipt, arguments.receipt_path)
    print(f"Wrote receipt: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
