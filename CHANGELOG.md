# Changelog

## 0.1.1 — 2026-10-07

- Escape surrogate-encoded filesystem paths in CLI JSON reports so unreadable paths remain valid machine-readable output.

## 0.1.0 — 2026-10-07

- Add stdlib-only validation for `batch-receipts/v1` and `record-preserving/same-unit/v1` receipts, including strict JSON limits and all three record-conservation equations.
- Separate structural validity from conservative acceptance policy, with explicit empty, unchanged, skipped, and partial-record ceilings.
- Add `batch-receipts check` / `python -m batch_receipts`, expected job/run bindings, machine-readable reports, and atomic validated sidecar writing.
- Include synthetic outcome fixtures and a runnable custom in-memory batch producer example.
