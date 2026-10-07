# batch-receipts

An offline Python library and CLI for checking self-consistency and explicit acceptance policy in same-unit batch receipts. It does not independently verify what a producer or destination actually did.

## Install

Install from GitHub:

```sh
python -m pip install "batch-receipts @ git+https://github.com/smynkr/batch-receipts.git"
```

For a checkout:

```sh
git clone https://github.com/smynkr/batch-receipts.git
cd batch-receipts
python -m pip install .
```

The runtime uses only the Python standard library and requires Python 3.12 or newer.

## Produce and check a receipt

`examples/custom_batch.py` demonstrates a small in-memory producer that counts its own record outcomes and writes a sidecar to an explicit path:

```sh
python examples/custom_batch.py order-import.receipt.json
batch-receipts check order-import.receipt.json \
  --expect-job example-order-import \
  --expect-run local-run-001
```

The same CLI is available as `python -m batch_receipts check ...`. The example destination is an in-memory dictionary, not a durable database; a receipt alone never proves persistence.

The synthetic fixtures in `examples/` show the default accepted case and valid-but-policy-rejected unchanged, empty, and failed outcomes:

```sh
batch-receipts check examples/accepted.json
batch-receipts check examples/unchanged.json --allow-no-change
batch-receipts check examples/empty.json --allow-empty
batch-receipts check examples/failed.json
```

The failed example is structurally valid but remains rejected; there is intentionally no `--allow-failed` option.

## Receipt format

The only supported contract is format `batch-receipts/v1` with profile `record-preserving/same-unit/v1`. A receipt is a JSON object with these required fields:

```json
{
  "format": "batch-receipts/v1",
  "profile": "record-preserving/same-unit/v1",
  "job": "example-order-import",
  "run": "local-run-001",
  "unit": "order-record",
  "outcome": "success",
  "counts": {
    "fetched": 2,
    "parsed": 2,
    "rejected": 0,
    "attempted": 2,
    "inserted": 1,
    "updated": 0,
    "unchanged": 1,
    "skipped": 0,
    "dead_lettered": 0,
    "failed": 0
  }
}
```

`job`, `run`, and `unit` are opaque non-empty strings. Expected job and run identities can be bound with `--expect-job` / `--expect-run` or with `check_receipt(..., expected_job=..., expected_run=...)`; they are compared exactly and are not parsed as identifiers.

All ten counters are required non-negative JSON integers (`true`/`false` are not integers here). There is no database-specific upper bound. The counters must conserve the same work-unit count at each stage:

- `fetched = parsed + rejected`
- `parsed = attempted + skipped + dead_lettered`
- `attempted = inserted + updated + unchanged + failed`

The work unit named by `unit` must mean the same record at each stage. This profile does not support fan-out, aggregation, deletion, or unlisted counters; the tool never infers how to account for them. Unknown versions, profiles, top-level fields, and counters are rejected. Forward-compatible producer data belongs only in the optional JSON-object `extensions` field.

Allowed outcomes:

- `success`: the producer reports a completed run. An all-zero successful receipt is structurally valid but rejected by default policy. If every attempted record is unchanged, use `no_change`.
- `no_change`: requires at least one attempted record and zero inserts, updates, and failures (therefore every attempted record is unchanged). Optional `no_change_check` metadata has `method` and timezone-aware ISO 8601 `checked_at` strings. `method` is producer-defined; the fields document the producer's assertion and are not evidence or an independently performed check.
- `skipped`: requires a non-empty `reason` and all counters zero, meaning no records were processed.
- `failed`: requires a non-empty `reason`. A failed receipt can be structurally valid, including when a run failed before any records were counted; the default policy never accepts a failed outcome or failed records.

## Validation and acceptance policy

Structural validation answers whether the document matches this versioned schema, its identities match any supplied bindings, and the three equations hold. Acceptance is a separate policy decision. The Python API exposes both:

```python
from batch_receipts import AcceptancePolicy, check_receipt, validate_structure

structure = validate_structure(
    receipt,
    expected_job="example-order-import",
    expected_run="local-run-001",
)
report = check_receipt(
    receipt,
    expected_job="example-order-import",
    expected_run="local-run-001",
    policy=AcceptancePolicy(allow_no_change=True, max_rejected=2),
)
print(structure.valid, report.valid, report.accepted)
print([item.to_dict() for item in report.violations])
```

`CheckReport` has separate `valid` and `accepted` values plus structured `code`, `path`, and `message` violations. `independently_verified` is always `false`: the receipt is producer-supplied evidence, not independent verification.

The conservative default policy rejects failed outcomes and failed records, any rejected/skipped/dead-lettered records, empty successful jobs, unchanged `no_change` jobs, and skipped runs. Explicit policy options are `allow_empty`, `allow_no_change`, and `allow_skipped`, plus integer ceilings `max_rejected`, `max_skipped`, and `max_dead_lettered` (all default to zero). The CLI exposes the same options. There is no fractional threshold and no policy flag that admits failed runs.

## CLI output and file behavior

`batch-receipts check RECEIPT.json` writes one machine-readable JSON report to stdout. Exit codes are:

- `0`: structurally valid and accepted by the selected policy.
- `1`: malformed or structurally invalid receipt, identity mismatch, or policy rejection.
- `2`: file I/O or command-line usage error.

Bad input produces JSON violations without a Python traceback. The reader limits input to 1,000,000 bytes and 64 nesting levels, rejects duplicate object keys and non-finite numbers, and applies the same JSON safety limits to direct Python values.

`write_receipt(payload, path)` requires an explicit path and writes only structurally valid receipts. It creates a sibling temporary file, fsyncs that file, then atomically replaces the destination and cleans up the temporary on failure. This gives atomic visibility, not a guarantee of power-loss durability; the parent directory is not fsynced. Writing a structurally valid failed receipt is allowed so reporting a failed run is distinct from accepting it. The writer does not use environment paths or silently do nothing.

## Scope and trust limits

This is a small offline receipt-consistency and policy utility, not an ingestion framework. It does not fetch or hash artifacts, read databases, sign receipts, prove source completeness, reconcile destination rows, or claim exactly-once processing. A producer can report invented counts or a successful write that never happened; `no_change_check` is likewise only a producer-defined assertion. Use destination-specific checks when independent evidence is required.

There is no service, credential, telemetry, datastore, scheduler, or import-time network dependency. The supported scope is intentionally narrow and has no SLA or guarantee of external adoption. Consumers needing transformations, fan-out, aggregation, deletion, artifact provenance, or destination verification need another contract.

The package is MIT-licensed; see [LICENSE](LICENSE). Software licensing does not determine rights to any data a producer processes.

## Development and shipping

Use feature branches and pull requests. Run the behavioral suite and exercise the installed CLI before release. Changes require independent review, resolved substantive findings, and successful candidate CI. Do not commit downloaded datasets, credentials, local review logs, or private project history. The initial default branch is an owner-authorized fresh repository bootstrap.

Run the tests from a checkout with:

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

