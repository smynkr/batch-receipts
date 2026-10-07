"""Offline validation and atomic writing for same-unit batch receipts."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

FORMAT_VERSION = "batch-receipts/v1"
PROFILE = "record-preserving/same-unit/v1"
MAX_JSON_BYTES = 1_000_000
MAX_JSON_DEPTH = 64

_COUNTER_FIELDS = (
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
_REQUIRED_FIELDS = (
    "format",
    "profile",
    "job",
    "run",
    "unit",
    "outcome",
    "counts",
)
_ALLOWED_FIELDS = frozenset(_REQUIRED_FIELDS) | frozenset(
    {"reason", "no_change_check", "extensions"}
)
_OUTCOMES = frozenset({"success", "no_change", "skipped", "failed"})


@dataclass(frozen=True, slots=True)
class Violation:
    """One machine-readable structural or policy finding."""

    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True, slots=True)
class StructuralReport:
    """Result of schema, identity-binding, and conservation validation."""

    valid: bool
    violations: tuple[Violation, ...]


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Combined validation and acceptance result; never independent verification."""

    valid: bool
    accepted: bool
    violations: tuple[Violation, ...]

    @property
    def independently_verified(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "accepted": self.accepted,
            "independently_verified": False,
            "violations": [violation.to_dict() for violation in self.violations],
        }


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    """Explicit acceptance choices; defaults reject empty or partial work."""

    allow_empty: bool = False
    allow_no_change: bool = False
    allow_skipped: bool = False
    max_rejected: int = 0
    max_skipped: int = 0
    max_dead_lettered: int = 0

    def __post_init__(self) -> None:
        for name in ("allow_empty", "allow_no_change", "allow_skipped"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        for name in ("max_rejected", "max_skipped", "max_dead_lettered"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")


class ReceiptInputError(ValueError):
    """Malformed, oversized, or otherwise unsafe JSON receipt input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidReceiptError(ValueError):
    """Raised when the atomic writer is asked to write a structurally invalid receipt."""

    def __init__(self, violations: tuple[Violation, ...]) -> None:
        super().__init__("receipt is structurally invalid")
        self.violations = violations


class _JSONProblem(Exception):
    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


def _child_path(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    if key.isidentifier():
        return key if path == "$" else f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=True)}]"


def _check_json_value(
    value: Any,
    path: str,
    depth: int,
    active: set[int],
    state: dict[str, int],
) -> None:
    state["nodes"] += 1
    if state["nodes"] > MAX_JSON_BYTES:
        raise _JSONProblem("json.too_large", path, f"JSON has more than {MAX_JSON_BYTES} values")
    if depth > MAX_JSON_DEPTH:
        raise _JSONProblem(
            "json.too_deep", path, f"JSON nesting exceeds {MAX_JSON_DEPTH} levels"
        )

    if value is None or type(value) is bool:
        return
    if isinstance(value, str):
        _check_json_string(value, path, state)
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _JSONProblem("json.non_finite", path, "JSON numbers must be finite")
        return

    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in active:
            raise _JSONProblem("json.cycle", path, "JSON values must not contain cycles")
        active.add(identity)
        try:
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise _JSONProblem(
                            "json.invalid_key", path, "JSON object keys must be strings"
                        )
                    child = _child_path(path, key)
                    _check_json_string(key, child, state)
                    _check_json_value(item, child, depth + 1, active, state)
            else:
                for index, item in enumerate(value):
                    _check_json_value(item, _child_path(path, index), depth + 1, active, state)
        finally:
            active.remove(identity)
        return

    raise _JSONProblem(
        "json.invalid_value", path, f"{type(value).__name__} is not a JSON value"
    )


def _check_json_string(value: str, path: str, state: dict[str, int]) -> None:
    if len(value) > MAX_JSON_BYTES:
        raise _JSONProblem("json.too_large", path, f"JSON exceeds {MAX_JSON_BYTES} bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _JSONProblem(
            "json.invalid_value", path, "JSON strings must be valid UTF-8 text"
        ) from exc
    state["string_bytes"] += size
    if state["string_bytes"] > MAX_JSON_BYTES:
        raise _JSONProblem("json.too_large", path, f"JSON exceeds {MAX_JSON_BYTES} bytes")


def _check_json_limits(payload: Any) -> None:
    _check_json_value(payload, "$", 1, set(), {"nodes": 0, "string_bytes": 0})
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        encoded_size = 0
        for chunk in encoder.iterencode(payload):
            if len(chunk) > MAX_JSON_BYTES:
                raise _JSONProblem(
                    "json.too_large", "$", f"JSON exceeds {MAX_JSON_BYTES} bytes"
                )
            encoded_size += len(chunk.encode("utf-8"))
            if encoded_size > MAX_JSON_BYTES:
                raise _JSONProblem(
                    "json.too_large", "$", f"JSON exceeds {MAX_JSON_BYTES} bytes"
                )
    except _JSONProblem:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise _JSONProblem("json.invalid_value", "$", "value cannot be encoded as safe JSON") from exc


def _check_text_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ReceiptInputError(
                    "json.too_deep", f"JSON nesting exceeds {MAX_JSON_DEPTH} levels"
                )
        elif character in "]}":
            depth = max(0, depth - 1)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptInputError("json.duplicate_key", "JSON object contains a duplicate key")
        result[key] = value
    return result


def _reject_non_finite(token: str) -> None:
    raise ReceiptInputError("json.non_finite", f"non-finite JSON number {token!r} is not allowed")


def read_receipt(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read one bounded UTF-8 JSON object; filesystem errors remain ``OSError``."""
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("path must be an explicit filesystem path")
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str):
        raise TypeError("path must resolve to a string path")
    if not raw_path:
        raise ValueError("path must not be empty")
    try:
        with open(raw_path, "rb") as source:
            raw = source.read(MAX_JSON_BYTES + 1)
    except OSError:
        raise
    if len(raw) > MAX_JSON_BYTES:
        raise ReceiptInputError("json.too_large", f"receipt exceeds {MAX_JSON_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptInputError("json.encoding", "receipt must be UTF-8 JSON") from exc
    _check_text_depth(text)
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except ReceiptInputError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ReceiptInputError("json.malformed", f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReceiptInputError("json.root_object", "receipt JSON root must be an object")
    try:
        _check_json_limits(payload)
    except _JSONProblem as exc:
        raise ReceiptInputError(exc.code, exc.message) from exc
    return payload


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_expected_identity(value: Any) -> bool:
    return value is None or _nonempty_string(value)


def _validate_structure(
    payload: Any,
    *,
    expected_job: str | None,
    expected_run: str | None,
) -> StructuralReport:
    violations: list[Violation] = []
    add = lambda code, path, message: violations.append(Violation(code, path, message))

    if not _valid_expected_identity(expected_job):
        add("binding.invalid_expected_job", "expected_job", "expected job must be a non-empty string")
    if not _valid_expected_identity(expected_run):
        add("binding.invalid_expected_run", "expected_run", "expected run must be a non-empty string")
    if not isinstance(payload, dict):
        add("json.root_object", "$", "receipt must be a JSON object")
        return StructuralReport(False, tuple(violations))

    try:
        _check_json_limits(payload)
    except _JSONProblem as exc:
        add(exc.code, exc.path, exc.message)
        return StructuralReport(False, tuple(violations))

    for key in payload:
        if key not in _ALLOWED_FIELDS:
            add("field.unknown", str(key), "unknown top-level fields are not allowed outside extensions")
    for field in _REQUIRED_FIELDS:
        if field not in payload:
            add("field.required", field, "required field is missing")

    if payload.get("format") != FORMAT_VERSION:
        add("format.unsupported", "format", f"format must be {FORMAT_VERSION!r}")
    if payload.get("profile") != PROFILE:
        add("profile.unsupported", "profile", f"profile must be {PROFILE!r}")

    for field in ("job", "run", "unit"):
        value = payload.get(field)
        if not _nonempty_string(value):
            add(f"field.{field}.nonempty_string", field, f"{field} must be a non-empty string")
    if _nonempty_string(payload.get("job")) and _valid_expected_identity(expected_job):
        if expected_job is not None and payload["job"] != expected_job:
            add("identity.job_mismatch", "job", "receipt job does not match the expected job")
    if _nonempty_string(payload.get("run")) and _valid_expected_identity(expected_run):
        if expected_run is not None and payload["run"] != expected_run:
            add("identity.run_mismatch", "run", "receipt run does not match the expected run")

    outcome = payload.get("outcome")
    outcome_valid = isinstance(outcome, str) and outcome in _OUTCOMES
    if not outcome_valid:
        add("outcome.unsupported", "outcome", "outcome must be success, no_change, skipped, or failed")

    reason_present = "reason" in payload
    if outcome in ("failed", "skipped"):
        if not _nonempty_string(payload.get("reason")):
            add("outcome.reason_required", "reason", f"outcome {outcome!r} requires a non-empty reason")
    elif reason_present:
        add("outcome.reason_unexpected", "reason", "reason is only valid for failed or skipped outcomes")

    counts = payload.get("counts")
    counts_valid: dict[str, bool] = {}
    if not isinstance(counts, dict):
        add("counts.object", "counts", "counts must be an object")
        counts = {}
    for name in counts:
        if name not in _COUNTER_FIELDS:
            add("count.unknown", f"counts.{name}", "unknown counters are unsupported by this profile")
    for name in _COUNTER_FIELDS:
        if name not in counts:
            counts_valid[name] = False
            add("count.required", f"counts.{name}", "required counter is missing")
            continue
        value = counts[name]
        valid = type(value) is int and value >= 0
        counts_valid[name] = valid
        if not valid:
            add(
                "count.nonnegative_integer",
                f"counts.{name}",
                "counter must be a non-negative integer (booleans are not integers here)",
            )

    def equation(total: str, components: tuple[str, ...], code: str) -> None:
        if counts_valid.get(total) and all(counts_valid.get(name) for name in components):
            actual = counts[total]
            expected = sum(counts[name] for name in components)
            if actual != expected:
                terms = "+".join(components)
                add(code, f"counts.{total}", f"{total}={actual}, but {terms}={expected}")

    equation("fetched", ("parsed", "rejected"), "counts.conservation.fetched")
    equation("parsed", ("attempted", "skipped", "dead_lettered"), "counts.conservation.parsed")
    equation(
        "attempted",
        ("inserted", "updated", "unchanged", "failed"),
        "counts.conservation.attempted",
    )

    if counts_valid.get("failed") and counts["failed"] > 0 and outcome_valid and outcome != "failed":
        add("outcome.failure_count_mismatch", "outcome", "failed records require outcome='failed'")
    if outcome == "no_change" and all(counts_valid.get(name) for name in _COUNTER_FIELDS):
        if counts["inserted"] != 0 or counts["updated"] != 0 or counts["failed"] != 0:
            add(
                "outcome.no_change_writes",
                "counts",
                "no_change requires zero inserts, updates, and failures",
            )
        if counts["attempted"] == 0:
            add("outcome.no_change_empty", "counts.attempted", "no_change requires at least one unchanged attempted record")
    if outcome == "success" and all(counts_valid.get(name) for name in _COUNTER_FIELDS):
        if (
            counts["attempted"] > 0
            and counts["inserted"] == 0
            and counts["updated"] == 0
            and counts["failed"] == 0
            and counts["unchanged"] == counts["attempted"]
        ):
            add(
                "outcome.no_change_required",
                "outcome",
                "all attempted records are unchanged; use outcome='no_change'",
            )
    if outcome == "skipped" and all(counts_valid.get(name) for name in _COUNTER_FIELDS):
        if any(counts[name] != 0 for name in _COUNTER_FIELDS):
            add("outcome.skipped_processed", "counts", "skipped outcome must contain no processed records")

    if "extensions" in payload and not isinstance(payload["extensions"], dict):
        add("extensions.object", "extensions", "extensions must be an object")

    if "no_change_check" in payload:
        check = payload["no_change_check"]
        if outcome != "no_change":
            add("no_change_check.unexpected", "no_change_check", "no_change_check is only valid for no_change outcome")
        if not isinstance(check, dict):
            add("no_change_check.object", "no_change_check", "no_change_check must be an object")
        else:
            for name in check:
                if name not in {"method", "checked_at"}:
                    add("no_change_check.unknown", f"no_change_check.{name}", "unknown no_change_check fields are not allowed")
            for name in ("method", "checked_at"):
                if name not in check:
                    add("no_change_check.required", f"no_change_check.{name}", "required field is missing")
            method = check.get("method")
            if not _nonempty_string(method):
                add("no_change_check.method", "no_change_check.method", "method must be a non-empty string")
            checked_at = check.get("checked_at")
            if not _nonempty_string(checked_at) or not _timezone_aware_timestamp(checked_at):
                add(
                    "no_change_check.checked_at",
                    "no_change_check.checked_at",
                    "checked_at must be a valid timezone-aware ISO 8601 timestamp",
                )

    return StructuralReport(not violations, tuple(violations))


def _timezone_aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        timestamp = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(timestamp)
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def validate_structure(
    payload: Any,
    *,
    expected_job: str | None = None,
    expected_run: str | None = None,
) -> StructuralReport:
    """Validate schema, exact expected identities, JSON safety, and same-unit counts."""
    return _validate_structure(payload, expected_job=expected_job, expected_run=expected_run)


def check_receipt(
    payload: Any,
    *,
    policy: AcceptancePolicy = AcceptancePolicy(),
    expected_job: str | None = None,
    expected_run: str | None = None,
) -> CheckReport:
    """Validate a receipt, then apply an explicit conservative acceptance policy."""
    if not isinstance(policy, AcceptancePolicy):
        raise TypeError("policy must be an AcceptancePolicy")
    structural = validate_structure(
        payload,
        expected_job=expected_job,
        expected_run=expected_run,
    )
    if not structural.valid:
        return CheckReport(False, False, structural.violations)

    counts = payload["counts"]
    outcome = payload["outcome"]
    violations: list[Violation] = []
    if outcome == "failed":
        violations.append(Violation("policy.failed_outcome", "outcome", "failed outcomes are not accepted by default policy"))
    if counts["failed"] > 0:
        violations.append(Violation("policy.failures", "counts.failed", "failed records are not accepted by default policy"))
    if all(counts[name] == 0 for name in _COUNTER_FIELDS) and outcome == "success" and not policy.allow_empty:
        violations.append(Violation("policy.empty", "counts", "empty jobs require allow_empty=True"))
    if outcome == "no_change" and not policy.allow_no_change:
        violations.append(Violation("policy.no_change", "outcome", "no_change requires allow_no_change=True"))
    if outcome == "skipped" and not policy.allow_skipped:
        violations.append(Violation("policy.skipped_outcome", "outcome", "skipped outcomes require allow_skipped=True"))
    for name, ceiling in (
        ("rejected", policy.max_rejected),
        ("skipped", policy.max_skipped),
        ("dead_lettered", policy.max_dead_lettered),
    ):
        if counts[name] > ceiling:
            violations.append(
                Violation(
                    f"policy.{name}_exceeded",
                    f"counts.{name}",
                    f"{name}={counts[name]} exceeds configured ceiling {ceiling}",
                )
            )
    return CheckReport(True, not violations, tuple(violations))


def write_receipt(payload: Any, path: str | os.PathLike[str]) -> Path:
    """Write a structurally valid receipt via sibling-temp fsync and atomic replace.

    Replacement provides atomic visibility, not a guarantee of power-loss durability.
    Policy rejection is intentionally separate: valid failed-run receipts can be written.
    """
    structural = validate_structure(payload)
    if not structural.valid:
        raise InvalidReceiptError(structural.violations)
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("path must be an explicit filesystem path")
    raw_path = os.fspath(path)
    if not raw_path:
        raise ValueError("path must not be empty")
    target = Path(raw_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None
        with stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return target


__all__ = [
    "AcceptancePolicy",
    "CheckReport",
    "FORMAT_VERSION",
    "InvalidReceiptError",
    "PROFILE",
    "ReceiptInputError",
    "StructuralReport",
    "Violation",
    "check_receipt",
    "read_receipt",
    "validate_structure",
    "write_receipt",
]
