"""Command-line interface for checking receipt files."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import (
    AcceptancePolicy,
    CheckReport,
    ReceiptInputError,
    Violation,
    check_receipt,
    read_receipt,
)


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _nonnegative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _write_report(report: CheckReport) -> None:
    json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


def _error_report(code: str, message: str, path: str = "$") -> CheckReport:
    return CheckReport(
        valid=False,
        accepted=False,
        violations=(Violation(code, path, message),),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(prog="batch-receipts", description="Validate a same-unit batch receipt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="validate and apply the receipt acceptance policy")
    check.add_argument("receipt", help="receipt JSON file")
    check.add_argument("--expect-job", help="require this opaque job identity")
    check.add_argument("--expect-run", help="require this opaque run identity")
    check.add_argument("--allow-empty", action="store_true", help="accept an empty successful job")
    check.add_argument("--allow-no-change", action="store_true", help="accept an all-unchanged no_change outcome")
    check.add_argument("--allow-skipped", action="store_true", help="accept a skipped run with no processed records")
    check.add_argument("--max-rejected", type=_nonnegative_integer, default=0, help="maximum rejected-record count")
    check.add_argument("--max-skipped", type=_nonnegative_integer, default=0, help="maximum skipped-record count")
    check.add_argument("--max-dead-lettered", type=_nonnegative_integer, default=0, help="maximum dead-letter count")

    try:
        arguments = parser.parse_args(argv)
    except _UsageError as exc:
        _write_report(_error_report("usage", str(exc)))
        return 2

    if arguments.command != "check":
        _write_report(_error_report("usage", "a command is required"))
        return 2
    try:
        payload = read_receipt(arguments.receipt)
    except ReceiptInputError as exc:
        _write_report(_error_report(exc.code, exc.message))
        return 1
    except (OSError, ValueError) as exc:
        _write_report(_error_report("io.read", str(exc), arguments.receipt))
        return 2

    report = check_receipt(
        payload,
        policy=AcceptancePolicy(
            allow_empty=arguments.allow_empty,
            allow_no_change=arguments.allow_no_change,
            allow_skipped=arguments.allow_skipped,
            max_rejected=arguments.max_rejected,
            max_skipped=arguments.max_skipped,
            max_dead_lettered=arguments.max_dead_lettered,
        ),
        expected_job=arguments.expect_job,
        expected_run=arguments.expect_run,
    )
    _write_report(report)
    return 0 if report.accepted else 1
