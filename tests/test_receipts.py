from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from batch_receipts import (
    AcceptancePolicy,
    InvalidReceiptError,
    ReceiptInputError,
    check_receipt,
    read_receipt,
    validate_structure,
    write_receipt,
)
from batch_receipts.cli import main


FORMAT = "batch-receipts/v1"
PROFILE = "record-preserving/same-unit/v1"
COUNTERS = {
    "fetched": 2,
    "parsed": 2,
    "rejected": 0,
    "attempted": 2,
    "inserted": 1,
    "updated": 0,
    "unchanged": 1,
    "skipped": 0,
    "dead_lettered": 0,
    "failed": 0,
}


def receipt(**changes):
    value = {
        "format": FORMAT,
        "profile": PROFILE,
        "job": "example-import",
        "run": "run-opaque-001",
        "unit": "order-record",
        "outcome": "success",
        "counts": dict(COUNTERS),
    }
    value.update(changes)
    return value


def codes(report):
    return {violation.code for violation in report.violations}


class StructureTests(unittest.TestCase):
    def test_valid_record_preserving_receipt_and_bindings(self):
        report = check_receipt(
            receipt(),
            expected_job="example-import",
            expected_run="run-opaque-001",
        )
        self.assertTrue(report.valid)
        self.assertTrue(report.accepted)
        self.assertFalse(report.independently_verified)
        self.assertEqual(report.violations, ())

    def test_counts_may_exceed_postgres_integer_range(self):
        count = 3_000_000_000
        large = receipt(counts={
            "fetched": count,
            "parsed": count,
            "rejected": 0,
            "attempted": count,
            "inserted": count,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "dead_lettered": 0,
            "failed": 0,
        })
        self.assertTrue(check_receipt(large).accepted)

    def test_all_three_conservation_equations_are_enforced(self):
        cases = (
            ({"fetched": 3}, "counts.conservation.fetched"),
            ({"parsed": 1}, "counts.conservation.parsed"),
            ({"inserted": 0}, "counts.conservation.attempted"),
        )
        for changes, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                changed = receipt(counts={**COUNTERS, **changes})
                report = check_receipt(changed)
                self.assertFalse(report.valid)
                self.assertIn(expected_code, codes(report))

    def test_identity_is_opaque_but_nonempty_and_expected_values_bind(self):
        opaque = receipt(job="tenant / batch #4", run="0xrun:not-an-integer")
        self.assertTrue(check_receipt(opaque).valid)
        mismatch = check_receipt(
            opaque,
            expected_job="tenant / batch #4",
            expected_run="another-run",
        )
        self.assertFalse(mismatch.valid)
        self.assertIn("identity.run_mismatch", codes(mismatch))
        job_mismatch = check_receipt(opaque, expected_job="another-job")
        self.assertFalse(job_mismatch.valid)
        self.assertIn("identity.job_mismatch", codes(job_mismatch))
        for field in ("job", "run", "unit"):
            with self.subTest(field=field):
                self.assertIn(
                    f"field.{field}.nonempty_string",
                    codes(check_receipt(receipt(**{field: "  "}))),
                )

    def test_expected_bindings_reject_non_string_and_empty_values(self):
        for kwargs in ({"expected_job": True}, {"expected_run": ""}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(validate_structure(receipt(), **kwargs).valid)

    def test_unknown_version_profile_and_top_level_fields_are_rejected(self):
        for changes, expected_code in (
            ({"format": "batch-receipts/v999"}, "format.unsupported"),
            ({"profile": "fanout/v1"}, "profile.unsupported"),
            ({"unknown_field": "outside extensions"}, "field.unknown"),
        ):
            with self.subTest(expected_code=expected_code):
                self.assertIn(expected_code, codes(check_receipt(receipt(**changes))))

    def test_extensions_are_preserved_and_json_safe(self):
        value = receipt(extensions={"producer": {"revision": "r7", "retry": 2}})
        self.assertTrue(check_receipt(value).accepted)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "receipt.json")
            write_receipt(value, path)
            self.assertEqual(read_receipt(path)["extensions"], value["extensions"])
        bad = receipt(extensions={"bad": object()})
        report = check_receipt(bad)
        self.assertFalse(report.valid)
        self.assertTrue(any(v.path.startswith("extensions") for v in report.violations))

    def test_boolean_and_other_non_integer_counts_are_rejected(self):
        for bad_value in (True, 1.0, "1", None, -1):
            with self.subTest(value=bad_value):
                changed = receipt(counts={**COUNTERS, "fetched": bad_value})
                report = check_receipt(changed)
                self.assertFalse(report.valid)
                self.assertIn("count.nonnegative_integer", codes(report))

    def test_missing_and_unknown_counter_keys_are_rejected(self):
        missing = dict(COUNTERS)
        del missing["updated"]
        unknown = {**COUNTERS, "unknown": 0}
        self.assertFalse(check_receipt(receipt(counts=missing)).valid)
        self.assertFalse(check_receipt(receipt(counts=unknown)).valid)

    def test_direct_api_rejects_non_json_cycles_depth_and_size(self):
        cyclic = receipt(extensions={})
        cyclic["extensions"]["self"] = cyclic["extensions"]
        self.assertIn("json.cycle", codes(check_receipt(cyclic)))
        non_finite = receipt(extensions={"value": float("nan")})
        self.assertIn("json.non_finite", codes(check_receipt(non_finite)))

        deeply_nested = receipt(extensions={})
        value = deeply_nested["extensions"]
        for _ in range(70):
            value["child"] = {}
            value = value["child"]
        self.assertIn("json.too_deep", codes(check_receipt(deeply_nested)))

        oversized = receipt(extensions={"blob": "x" * 1_000_001})
        self.assertIn("json.too_large", codes(check_receipt(oversized)))


class OutcomeAndPolicyTests(unittest.TestCase):
    def test_failed_receipt_is_valid_but_not_accepted(self):
        failed_counts = {
            "fetched": 1,
            "parsed": 1,
            "rejected": 0,
            "attempted": 1,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "dead_lettered": 0,
            "failed": 1,
        }
        report = check_receipt(receipt(outcome="failed", reason="sink unavailable", counts=failed_counts))
        self.assertTrue(report.valid)
        self.assertFalse(report.accepted)
        self.assertIn("policy.failed_outcome", codes(report))
        self.assertIn("policy.failures", codes(report))
        self.assertFalse(report.independently_verified)

    def test_failed_and_skipped_outcomes_require_reason(self):
        self.assertIn("outcome.reason_required", codes(check_receipt(receipt(outcome="failed"))))
        self.assertIn("outcome.reason_required", codes(check_receipt(receipt(outcome="skipped"))))

    def test_skipped_outcome_requires_no_processed_records_and_explicit_policy(self):
        skipped = receipt(
            outcome="skipped",
            reason="nothing was scheduled",
            counts={name: 0 for name in COUNTERS},
        )
        default = check_receipt(skipped)
        self.assertTrue(default.valid)
        self.assertFalse(default.accepted)
        self.assertIn("policy.skipped_outcome", codes(default))
        allowed = check_receipt(skipped, policy=AcceptancePolicy(allow_skipped=True))
        self.assertTrue(allowed.accepted)
        processed = receipt(outcome="skipped", reason="not empty")
        self.assertFalse(check_receipt(processed).valid)

    def test_no_change_requires_unchanged_records_no_writes_or_failures(self):
        counts = {
            "fetched": 2,
            "parsed": 2,
            "rejected": 0,
            "attempted": 2,
            "inserted": 0,
            "updated": 0,
            "unchanged": 2,
            "skipped": 0,
            "dead_lettered": 0,
            "failed": 0,
        }
        no_change = receipt(outcome="no_change", counts=counts)
        self.assertTrue(check_receipt(no_change).valid)
        self.assertFalse(check_receipt(no_change).accepted)
        self.assertTrue(
            check_receipt(no_change, policy=AcceptancePolicy(allow_no_change=True)).accepted
        )
        mislabeled_success = check_receipt(receipt(counts=counts))
        self.assertFalse(mislabeled_success.valid)
        self.assertIn("outcome.no_change_required", codes(mislabeled_success))
        for changes in (
            {"inserted": 1, "unchanged": 1},
            {"updated": 1, "unchanged": 1},
            {"failed": 1, "unchanged": 1},
        ):
            altered = {**counts, **changes}
            # Keep the terminal equation balanced for the failure case.
            if altered["failed"]:
                altered["unchanged"] = 0
            self.assertFalse(check_receipt(receipt(outcome="no_change", counts=altered)).valid)

    def test_no_change_check_is_optional_but_validated_if_present(self):
        counts = {
            "fetched": 1,
            "parsed": 1,
            "rejected": 0,
            "attempted": 1,
            "inserted": 0,
            "updated": 0,
            "unchanged": 1,
            "skipped": 0,
            "dead_lettered": 0,
            "failed": 0,
        }
        with_check = receipt(
            outcome="no_change",
            counts=counts,
            no_change_check={"method": "compare by primary key", "checked_at": "2026-10-07T12:00:00Z"},
        )
        self.assertTrue(check_receipt(with_check).valid)
        for checked_at in ("not-a-time", "2026-10-07T12:00:00", ""):
            bad = receipt(
                outcome="no_change",
                counts=counts,
                no_change_check={"method": "exact compare", "checked_at": checked_at},
            )
            self.assertIn("no_change_check.checked_at", codes(check_receipt(bad)))

    def test_empty_job_requires_explicit_allowance(self):
        empty = receipt(counts={name: 0 for name in COUNTERS})
        default = check_receipt(empty)
        self.assertTrue(default.valid)
        self.assertFalse(default.accepted)
        self.assertIn("policy.empty", codes(default))
        self.assertTrue(check_receipt(empty, policy=AcceptancePolicy(allow_empty=True)).accepted)

    def test_reject_skip_and_deadletter_ceilings_are_integer_policy(self):
        counts = {
            "fetched": 4,
            "parsed": 3,
            "rejected": 1,
            "attempted": 1,
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "skipped": 1,
            "dead_lettered": 1,
            "failed": 0,
        }
        value = receipt(counts=counts)
        report = check_receipt(value)
        self.assertFalse(report.accepted)
        self.assertTrue(
            check_receipt(
                value,
                policy=AcceptancePolicy(max_rejected=1, max_skipped=1, max_dead_lettered=1),
            ).accepted
        )
        for kwargs in (
            {"max_rejected": True},
            {"max_skipped": -1},
            {"allow_empty": 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                AcceptancePolicy(**kwargs)

    def test_synthetic_examples_show_validity_and_acceptance_separately(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        accepted = check_receipt(read_receipt(examples / "accepted.json"))
        unchanged = check_receipt(read_receipt(examples / "unchanged.json"))
        unchanged_allowed = check_receipt(
            read_receipt(examples / "unchanged.json"),
            policy=AcceptancePolicy(allow_no_change=True),
        )
        empty = check_receipt(read_receipt(examples / "empty.json"))
        empty_allowed = check_receipt(
            read_receipt(examples / "empty.json"),
            policy=AcceptancePolicy(allow_empty=True),
        )
        failed = check_receipt(read_receipt(examples / "failed.json"))
        self.assertTrue(accepted.accepted)
        self.assertTrue(unchanged.valid)
        self.assertFalse(unchanged.accepted)
        self.assertTrue(unchanged_allowed.accepted)
        self.assertTrue(empty.valid)
        self.assertFalse(empty.accepted)
        self.assertTrue(empty_allowed.accepted)
        self.assertTrue(failed.valid)
        self.assertFalse(failed.accepted)

class FileAndCliTests(unittest.TestCase):
    def test_duplicate_keys_nonfinite_and_deep_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.json")
            malformed = (
                '{"format":"batch-receipts/v1","format":"batch-receipts/v1"}',
                '{"value":NaN}',
                '{"value":' + "[" * 70 + "0" + "]" * 70 + "}",
            )
            for text in malformed:
                with self.subTest(text=text[:50]):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ReceiptInputError):
                        read_receipt(path)

    def test_file_size_limit_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "receipt.json")
            write_receipt(receipt(extensions={"note": "round trip"}), path)
            loaded = read_receipt(path)
            self.assertTrue(check_receipt(loaded).accepted)
            path.write_bytes(b" " * 1_000_001)
            with self.assertRaises(ReceiptInputError) as caught:
                read_receipt(path)
            self.assertEqual(caught.exception.code, "json.too_large")

    def test_invalid_receipts_are_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "not-created", "receipt.json")
            bad = receipt(counts={**COUNTERS, "fetched": 4})
            with self.assertRaises(InvalidReceiptError):
                write_receipt(bad, target)
            self.assertFalse(target.parent.exists())

    def test_replace_failure_preserves_existing_file_and_cleans_temporary_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "receipt.json")
            write_receipt(receipt(), target)
            original = target.read_bytes()
            with mock.patch("batch_receipts.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_receipt(receipt(run="next-run"), target)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["receipt.json"])

    def test_cli_reports_accepted_rejected_invalid_and_io_exit_codes_as_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "receipt.json")
            write_receipt(receipt(), path)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                accepted_code = main(["check", str(path), "--expect-job", "example-import", "--expect-run", "run-opaque-001"])
            accepted_json = json.loads(stdout.getvalue())
            self.assertEqual(accepted_code, 0)
            self.assertTrue(accepted_json["accepted"])

            rejected = receipt(counts={name: 0 for name in COUNTERS})
            path.write_text(json.dumps(rejected), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rejected_code = main(["check", str(path)])
            self.assertEqual(rejected_code, 1)
            self.assertFalse(json.loads(stdout.getvalue())["accepted"])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                empty_allowed_code = main(["check", str(path), "--allow-empty"])
            self.assertEqual(empty_allowed_code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["accepted"])

            unchanged_path = Path(__file__).resolve().parents[1] / "examples" / "unchanged.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                unchanged_code = main(["check", str(unchanged_path), "--allow-no-change"])
            self.assertEqual(unchanged_code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["accepted"])

            path.write_text("{broken", encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                invalid_code = main(["check", str(path)])
            self.assertEqual(invalid_code, 1)
            self.assertFalse(json.loads(stdout.getvalue())["valid"])

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                io_code = main(["check", str(Path(directory, "missing.json"))])
            self.assertEqual(io_code, 2)
            io_report = json.loads(stdout.getvalue())
            self.assertFalse(io_report["valid"])
            self.assertEqual(io_report["violations"][0]["code"], "io.read")

    def test_invalid_filesystem_paths_return_json_instead_of_a_traceback(self):
        for path in ("", "invalid\x00path"):
            with self.subTest(path=path):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(["check", path])
                report = json.loads(stdout.getvalue())
                self.assertEqual(code, 2)
                self.assertFalse(report["accepted"])
                self.assertEqual(report["violations"][0]["code"], "io.read")

    def test_cli_usage_errors_are_machine_readable_and_have_exit_two(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["unknown"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["violations"][0]["code"], "usage")


if __name__ == "__main__":
    unittest.main()
