"""A3 strict run-validity gates and metric-integrity checks.

Evaluates the approved Task 6 validity gates independently: one bad field
never stops other gates, ordinary invalid run data yields reasons instead of
exceptions, and all failure reasons use severity "error". No SLO threshold
evaluation happens here.
"""

import dataclasses
import json
import math
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from tools.performance.a3.prometheus import (
    MetricSeries,
    detect_counter_resets,
    detect_missing_samples,
)

EXPECTED_STEP_SECONDS = 5
MAX_MISSING_GAP_SECONDS = 15
TARGET_CONCURRENCY_FLOOR_RATIO = 0.98
MAX_UNEXPECTED_DISCONNECT_RATIO = 0.01
MAX_BACKGROUND_CPU_PERCENT = 5.0
MAX_PACKET_LOSS_RATIO = 0.001
WORKLOAD_MIX_TOLERANCE = 0.05
WORKLOAD_TOTAL_TOLERANCE = 1e-9
WEBGL_CLIENTS_EXPECTED = 20
VALID_LOAD_LEVELS = (500, 1000, 2500, 5000)
VALID_RUN_NUMBERS = (1, 2, 3)
EXPECTED_COLLECTORS = ("pidstat", "sar", "vmstat", "iostat")
EXPECTED_PROCESSES = ("login", "char", "map")

EXPECTED_PHASE_DURATIONS = {
    "preconditioning_seconds": 600,
    "ramp_seconds": 300,
    "steady_state_seconds": 1200,
    "cooldown_seconds": 300,
}

WORKLOAD_TARGETS = {
    "movement_direction_changes": 0.35,
    "idle_heartbeat": 0.20,
    "combat": 0.15,
    "npc_interaction": 0.10,
    "item_inventory": 0.08,
    "map_change_warp": 0.05,
    "chat": 0.04,
    "login_logout_character_select": 0.03,
}

CHECKED_GATES = (
    "run_identity",
    "target_concurrency",
    "unexpected_disconnects",
    "prometheus_continuity",
    "process_stability",
    "background_cpu",
    "network_quality",
    "workload_mix",
    "manifest_identity",
    "phase_completeness",
    "collector_integrity",
    "webgl_validation",
    "load_generator_integrity",
    "timing_source",
)

SECRET_MARKERS = (
    "password",
    "token",
    "secret",
    "api_key",
    "private_key",
    "authorization",
    "bearer",
)

REDACTED = "<redacted>"

_COUNTER_SUFFIXES = ("_total", "_count")

_IDENTIFIER_MAX_LENGTH = 128


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ValidityReason:
    code: str
    field: str
    message: str
    observed: Any
    expected: Any
    severity: str


@dataclasses.dataclass(frozen=True)
class MetricIntegrityIssue:
    code: str
    identity: str
    timestamp: Optional[float]
    message: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclasses.dataclass(frozen=True)
class ValidityResult:
    valid: bool
    reasons: Tuple[ValidityReason, ...]
    checked_gates: Tuple[str, ...]
    run_id: str
    manifest_id: str


@dataclasses.dataclass(frozen=True)
class MetricIntegrityResult:
    valid: bool
    issues: Tuple[MetricIntegrityIssue, ...]
    checked_series: int
    expected_step_seconds: int
    expected_start: float
    expected_end: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reason(code: str, field: str, message: str, observed: Any, expected: Any) -> ValidityReason:
    return ValidityReason(
        code=code,
        field=field,
        message=message,
        observed=_json_safe(observed),
        expected=_json_safe(expected),
        severity="error",
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _get(data: Any, *path: str) -> Any:
    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _is_safe_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _IDENTIFIER_MAX_LENGTH or "\x00" in value or ".." in value:
        return False
    return all(
        char.isascii() and (char.isalnum() or char in ".-_") for char in value
    )


def _sort_reasons(reasons: List[ValidityReason]) -> Tuple[ValidityReason, ...]:
    return tuple(sorted(reasons, key=lambda r: (r.code, r.field, r.message)))


# ---------------------------------------------------------------------------
# Run-validity gates
# ---------------------------------------------------------------------------


def _gate_run_identity(run: dict, reasons: List[ValidityReason]) -> None:
    for field, code in (
        ("run_id", "INVALID_RUN_ID"),
        ("manifest_id", "INVALID_MANIFEST_ID"),
        ("baseline_cycle_id", "INVALID_BASELINE_CYCLE_ID"),
    ):
        if not _is_safe_identifier(run.get(field)):
            reasons.append(
                _reason(code, field, f"{field} is not a safe identifier", run.get(field), "safe identifier")
            )
    if run.get("load_level") not in VALID_LOAD_LEVELS or isinstance(run.get("load_level"), bool):
        reasons.append(
            _reason("INVALID_LOAD_LEVEL", "load_level", "load_level must be one of 500/1000/2500/5000", run.get("load_level"), list(VALID_LOAD_LEVELS))
        )
    if run.get("run_number") not in VALID_RUN_NUMBERS or isinstance(run.get("run_number"), bool):
        reasons.append(
            _reason("INVALID_RUN_NUMBER", "run_number", "run_number must be 1, 2, or 3", run.get("run_number"), list(VALID_RUN_NUMBERS))
        )


def _gate_target_concurrency(run: dict, reasons: List[ValidityReason]) -> None:
    target = run.get("target_synthetic_users")
    observed = run.get("observed_concurrency_min")
    field = "observed_concurrency_min"
    if not _is_int(target) or target <= 0:
        reasons.append(
            _reason("TARGET_CONCURRENCY_BELOW_MINIMUM", "target_synthetic_users", "target_synthetic_users must be a positive integer", target, "> 0")
        )
        return
    if not _is_number(observed) or observed < 0:
        reasons.append(
            _reason("TARGET_CONCURRENCY_BELOW_MINIMUM", field, "observed_concurrency_min must be a non-negative number", observed, ">= 0")
        )
        return
    if observed / target < TARGET_CONCURRENCY_FLOOR_RATIO:
        reasons.append(
            _reason("TARGET_CONCURRENCY_BELOW_MINIMUM", field, f"observed concurrency {observed} below 98% of target {target}", observed, f">= {TARGET_CONCURRENCY_FLOOR_RATIO} * {target}")
        )


def _gate_disconnects(run: dict, reasons: List[ValidityReason]) -> None:
    attempts = run.get("connection_attempts")
    disconnects = run.get("unexpected_disconnects")
    if not _is_int(attempts) or attempts < 0 or not _is_int(disconnects) or disconnects < 0:
        reasons.append(
            _reason("INVALID_CONNECTION_ATTEMPTS", "unexpected_disconnects", "connection_attempts and unexpected_disconnects must be non-negative integers", {"connection_attempts": attempts, "unexpected_disconnects": disconnects}, "non-negative integers")
        )
        return
    ratio = disconnects / attempts if attempts > 0 else (0.0 if disconnects == 0 else 1.0)
    if ratio > MAX_UNEXPECTED_DISCONNECT_RATIO:
        reasons.append(
            _reason("DISCONNECT_RATIO_EXCEEDED", "unexpected_disconnects", f"unexpected disconnect ratio {ratio!r} exceeds 0.01", disconnects, f"<= 1% of {attempts}")
        )


def _gate_prometheus_gap(run: dict, reasons: List[ValidityReason]) -> None:
    gap = _get(run, "metric_integrity", "maximum_missing_gap_seconds")
    field = "metric_integrity.maximum_missing_gap_seconds"
    if not _is_number(gap) or gap < 0:
        reasons.append(
            _reason("PROMETHEUS_GAP_EXCEEDED", field, "maximum_missing_gap_seconds must be a non-negative number", gap, ">= 0")
        )
        return
    if gap > MAX_MISSING_GAP_SECONDS:
        reasons.append(
            _reason("PROMETHEUS_GAP_EXCEEDED", field, f"missing-data gap {gap}s exceeds 15s", gap, f"<= {MAX_MISSING_GAP_SECONDS}")
        )


def _gate_processes(run: dict, reasons: List[ValidityReason]) -> None:
    processes = run.get("processes")
    for name in EXPECTED_PROCESSES:
        entry = processes.get(name) if isinstance(processes, dict) else None
        if not isinstance(entry, dict):
            reasons.append(
                _reason("PROCESS_MISSING_STEADY_STATE", f"processes.{name}", "process entry missing or malformed", entry, "process record")
            )
            continue
        for key, code in (("restarts", "PROCESS_RESTART"), ("crashes", "PROCESS_CRASH"), ("oom", "PROCESS_OOM")):
            value = entry.get(key)
            if not _is_int(value) or value < 0:
                reasons.append(
                    _reason(code, f"processes.{name}.{key}", "count must be a non-negative integer", value, ">= 0")
                )
            elif value > 0:
                reasons.append(
                    _reason(code, f"processes.{name}.{key}", f"{name} recorded {value} {key}", value, 0)
                )
        missing = entry.get("steady_state_missing")
        if missing is not False:
            reasons.append(
                _reason("PROCESS_MISSING_STEADY_STATE", f"processes.{name}.steady_state_missing", f"{name} disappeared during steady state", missing, False)
            )
    harness = processes.get("harness") if isinstance(processes, dict) else None
    if not isinstance(harness, dict):
        reasons.append(
            _reason("HARNESS_ERROR", "processes.harness", "harness entry missing or malformed", harness, "harness record")
        )
        return
    restarts = harness.get("restarts")
    if not _is_int(restarts) or restarts < 0 or restarts > 0:
        reasons.append(
            _reason("HARNESS_RESTART", "processes.harness.restarts", "harness must not restart", restarts, 0)
        )
    errors = harness.get("errors")
    if not _is_int(errors) or errors < 0 or errors > 0:
        reasons.append(
            _reason("HARNESS_ERROR", "processes.harness.errors", "harness errors must be zero", errors, 0)
        )


def _gate_background_cpu(run: dict, reasons: List[ValidityReason]) -> None:
    value = run.get("background_cpu_percent_max")
    field = "background_cpu_percent_max"
    if not _is_number(value) or not 0.0 <= value <= 100.0:
        reasons.append(
            _reason("BACKGROUND_CPU_EXCEEDED", field, "background CPU must be a percentage within [0, 100]", value, "[0, 100]")
        )
        return
    if value > MAX_BACKGROUND_CPU_PERCENT:
        reasons.append(
            _reason("BACKGROUND_CPU_EXCEEDED", field, f"background CPU {value}% exceeds 5%", value, f"<= {MAX_BACKGROUND_CPU_PERCENT}")
        )


def _gate_packet_loss(run: dict, reasons: List[ValidityReason]) -> None:
    value = run.get("packet_loss_ratio")
    field = "packet_loss_ratio"
    if not _is_number(value) or not 0.0 <= value <= 1.0:
        reasons.append(
            _reason("PACKET_LOSS_EXCEEDED", field, "packet loss ratio must be within [0, 1]", value, "[0, 1]")
        )
        return
    if value > MAX_PACKET_LOSS_RATIO:
        reasons.append(
            _reason("PACKET_LOSS_EXCEEDED", field, f"packet loss ratio {value} exceeds 0.001", value, f"<= {MAX_PACKET_LOSS_RATIO}")
        )


def _gate_workload_mix(run: dict, reasons: List[ValidityReason]) -> None:
    mix = run.get("workload_mix")
    if not isinstance(mix, dict):
        reasons.append(
            _reason("WORKLOAD_CATEGORY_MISSING", "workload_mix", "workload_mix must be an object", mix, dict)
        )
        return
    total = 0.0
    counted = 0
    for category in sorted(WORKLOAD_TARGETS):
        if category not in mix:
            reasons.append(
                _reason("WORKLOAD_CATEGORY_MISSING", f"workload_mix.{category}", "required workload category missing", None, category)
            )
            continue
        value = mix[category]
        field = f"workload_mix.{category}"
        if not _is_number(value) or not 0.0 <= value <= 1.0:
            reasons.append(
                _reason("WORKLOAD_MIX_OUT_OF_TOLERANCE", field, "workload proportion must be a number within [0, 1]", value, "[0, 1]")
            )
            continue
        total += value
        counted += 1
        target = WORKLOAD_TARGETS[category]
        if abs(value - target) > WORKLOAD_MIX_TOLERANCE + WORKLOAD_TOTAL_TOLERANCE:
            reasons.append(
                _reason("WORKLOAD_MIX_OUT_OF_TOLERANCE", field, f"workload proportion {value} differs from target {target} by more than 0.05", value, f"{target} +/- {WORKLOAD_MIX_TOLERANCE}")
            )
    for category in sorted(set(mix) - set(WORKLOAD_TARGETS)):
        reasons.append(
            _reason("WORKLOAD_CATEGORY_UNEXPECTED", f"workload_mix.{category}", "unknown workload category", category, None)
        )
    if counted == len(WORKLOAD_TARGETS) and abs(total - 1.0) > WORKLOAD_TOTAL_TOLERANCE:
        reasons.append(
            _reason("WORKLOAD_MIX_OUT_OF_TOLERANCE", "workload_mix", f"workload total {total!r} differs from 1.0", total, 1.0)
        )


def _gate_manifest(run: dict, reasons: List[ValidityReason]) -> None:
    manifest_id = run.get("manifest_id")
    frozen = run.get("frozen_manifest_id")
    if manifest_id != frozen:
        reasons.append(
            _reason("MANIFEST_ID_MISMATCH", "manifest_id", "run manifest_id does not match frozen manifest_id", manifest_id, frozen)
        )
    drift = run.get("runtime_manifest_drift")
    if not isinstance(drift, list):
        reasons.append(
            _reason("MANIFEST_DRIFT", "runtime_manifest_drift", "runtime_manifest_drift must be a list", drift, list)
        )
        return
    for entry in drift:
        reasons.append(
            _reason("MANIFEST_DRIFT", "runtime_manifest_drift", f"runtime manifest drift: {entry}", entry, [])
        )


def _gate_phases(run: dict, reasons: List[ValidityReason]) -> None:
    phases = run.get("phases")
    for field, expected in sorted(EXPECTED_PHASE_DURATIONS.items()):
        value = phases.get(field) if isinstance(phases, dict) else None
        if not _is_int(value):
            reasons.append(
                _reason("PHASE_DURATION_MISMATCH", f"phases.{field}", "phase duration must be an integer", value, expected)
            )
        elif value != expected:
            reasons.append(
                _reason("PHASE_DURATION_MISMATCH", f"phases.{field}", f"phase duration {value}s does not match approved {expected}s", value, expected)
            )
    completed = phases.get("validation_completed") if isinstance(phases, dict) else None
    if completed is not True:
        reasons.append(
            _reason("VALIDATION_NOT_COMPLETED", "phases.validation_completed", "validation phase did not complete", completed, True)
        )


def _gate_collectors(run: dict, reasons: List[ValidityReason]) -> None:
    collectors = run.get("collectors")
    if not isinstance(collectors, dict):
        reasons.append(
            _reason("COLLECTOR_MISSING", "collectors", "collectors must be an object", collectors, dict)
        )
        return
    for name in EXPECTED_COLLECTORS:
        entry = collectors.get(name)
        if not isinstance(entry, dict) or entry.get("record_present") is not True:
            reasons.append(
                _reason("COLLECTOR_MISSING", f"collectors.{name}", f"collector {name} missing or has no record", entry if not isinstance(entry, dict) else entry.get("record_present"), "record present")
            )
            continue
        if entry.get("stdout_present") is not True:
            reasons.append(
                _reason("COLLECTOR_OUTPUT_MISSING", f"collectors.{name}.stdout_present", f"collector {name} output missing", entry.get("stdout_present"), True)
            )
        if entry.get("start_error") is not None:
            reasons.append(
                _reason("COLLECTOR_START_ERROR", f"collectors.{name}.start_error", f"collector {name} failed to start", entry.get("start_error"), None)
            )
        if entry.get("stop_error") is not None:
            reasons.append(
                _reason("COLLECTOR_STOP_ERROR", f"collectors.{name}.stop_error", f"collector {name} failed to stop cleanly", entry.get("stop_error"), None)
            )
    for name in sorted(set(collectors) - set(EXPECTED_COLLECTORS)):
        reasons.append(
            _reason("COLLECTOR_UNEXPECTED", f"collectors.{name}", "unexpected collector record", name, sorted(EXPECTED_COLLECTORS))
        )


def _gate_webgl(run: dict, reasons: List[ValidityReason]) -> None:
    expected = run.get("webgl_clients_expected")
    observed = run.get("webgl_clients_observed")
    if expected != WEBGL_CLIENTS_EXPECTED or isinstance(expected, bool):
        reasons.append(
            _reason("WEBGL_CLIENT_COUNT_MISMATCH", "webgl_clients_expected", "expected WebGL clients must be exactly 20", expected, WEBGL_CLIENTS_EXPECTED)
        )
    if observed != WEBGL_CLIENTS_EXPECTED or isinstance(observed, bool):
        reasons.append(
            _reason("WEBGL_CLIENT_COUNT_MISMATCH", "webgl_clients_observed", "observed WebGL clients must be exactly 20", observed, WEBGL_CLIENTS_EXPECTED)
        )
    failures = run.get("webgl_launch_failures")
    if not _is_int(failures) or failures != 0:
        reasons.append(
            _reason("WEBGL_LAUNCH_FAILURE", "webgl_launch_failures", "WebGL launch failures must be zero", failures, 0)
        )
    disconnects = run.get("webgl_unexpected_disconnects")
    if not _is_int(disconnects) or disconnects < 0:
        reasons.append(
            _reason("WEBGL_DISCONNECT_RATIO_EXCEEDED", "webgl_unexpected_disconnects", "WebGL disconnects must be a non-negative integer", disconnects, ">= 0")
        )
        return
    denominator = observed if _is_int(observed) else 0
    ratio = disconnects / denominator if denominator > 0 else (0.0 if disconnects == 0 else 1.0)
    if ratio > MAX_UNEXPECTED_DISCONNECT_RATIO:
        reasons.append(
            _reason("WEBGL_DISCONNECT_RATIO_EXCEEDED", "webgl_unexpected_disconnects", f"WebGL disconnect ratio {ratio!r} exceeds 0.01", disconnects, f"<= 1% of {denominator}")
        )


def _gate_load_generator(run: dict, reasons: List[ValidityReason]) -> None:
    generator = run.get("load_generator")
    target = run.get("target_synthetic_users")
    generated = generator.get("generated_user_count") if isinstance(generator, dict) else None
    if generated != target or isinstance(generated, bool):
        reasons.append(
            _reason("GENERATED_USER_COUNT_MISMATCH", "load_generator.generated_user_count", "generated user count must equal target synthetic users", generated, target)
        )
    events = generator.get("workload_event_total") if isinstance(generator, dict) else None
    if not _is_int(events) or events <= 0:
        reasons.append(
            _reason("WORKLOAD_EVENT_TOTAL_INVALID", "load_generator.workload_event_total", "workload event total must be a positive integer", events, "> 0")
        )
    errors = generator.get("errors") if isinstance(generator, dict) else None
    if not _is_int(errors) or errors != 0:
        reasons.append(
            _reason("HARNESS_ERROR", "load_generator.errors", "harness errors must be zero", errors, 0)
        )
    restarts = generator.get("restarts") if isinstance(generator, dict) else None
    if not _is_int(restarts) or restarts != 0:
        reasons.append(
            _reason("HARNESS_RESTART", "load_generator.restarts", "harness must not restart", restarts, 0)
        )


def _gate_timing(run: dict, reasons: List[ValidityReason]) -> None:
    timing = run.get("timing")
    monotonic = timing.get("uses_monotonic_durations") if isinstance(timing, dict) else None
    if monotonic is not True:
        reasons.append(
            _reason("MONOTONIC_TIMING_REQUIRED", "timing.uses_monotonic_durations", "run must use monotonic durations", monotonic, True)
        )
    regressions = timing.get("clock_regression_count") if isinstance(timing, dict) else None
    if not _is_int(regressions) or regressions != 0:
        reasons.append(
            _reason("CLOCK_REGRESSION", "timing.clock_regression_count", "clock regression count must be zero", regressions, 0)
        )
    healthy = timing.get("time_sync_healthy") if isinstance(timing, dict) else None
    if healthy is not True:
        reasons.append(
            _reason("TIME_SYNC_UNHEALTHY", "timing.time_sync_healthy", "time synchronization must be healthy", healthy, True)
        )


def validate_run(run_data: dict) -> ValidityResult:
    """Evaluate all approved run-validity gates independently.

    Ordinary invalid data yields ``ValidityResult(valid=False, ...)``;
    :class:`ValueError` is raised only when the root is not a dict.
    """
    if not isinstance(run_data, dict):
        raise ValueError(f"run_data must be a dict, got {type(run_data).__name__}")
    reasons: List[ValidityReason] = []
    _gate_run_identity(run_data, reasons)
    _gate_target_concurrency(run_data, reasons)
    _gate_disconnects(run_data, reasons)
    _gate_prometheus_gap(run_data, reasons)
    _gate_processes(run_data, reasons)
    _gate_background_cpu(run_data, reasons)
    _gate_packet_loss(run_data, reasons)
    _gate_workload_mix(run_data, reasons)
    _gate_manifest(run_data, reasons)
    _gate_phases(run_data, reasons)
    _gate_collectors(run_data, reasons)
    _gate_webgl(run_data, reasons)
    _gate_load_generator(run_data, reasons)
    _gate_timing(run_data, reasons)
    return ValidityResult(
        valid=not reasons,
        reasons=_sort_reasons(reasons),
        checked_gates=CHECKED_GATES,
        run_id=run_data.get("run_id") if isinstance(run_data.get("run_id"), str) else "",
        manifest_id=run_data.get("manifest_id") if isinstance(run_data.get("manifest_id"), str) else "",
    )


# ---------------------------------------------------------------------------
# Metric integrity
# ---------------------------------------------------------------------------


def _contains_secret(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def _is_counter_series(series: MetricSeries) -> bool:
    if str(series.labels.get("metric_semantics", "")).lower() == "counter":
        return True
    name = str(series.labels.get("__name__", ""))
    return name.endswith(_COUNTER_SUFFIXES)


def _sort_issues(issues: List[MetricIntegrityIssue]) -> Tuple[MetricIntegrityIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda i: (
                i.code,
                i.identity,
                i.timestamp if i.timestamp is not None else -1.0,
                i.message,
            ),
        )
    )


def validate_metric_integrity(
    series_set: Tuple[MetricSeries, ...],
    expected_start: float,
    expected_end: float,
    step: int = EXPECTED_STEP_SECONDS,
) -> MetricIntegrityResult:
    """Validate continuity and integrity of Task 5 metric series.

    Missing points are reported as MISSING_SAMPLE issues; a continuous gap
    greater than 15 seconds additionally yields MISSING_GAP_EXCEEDED and
    makes the result invalid. Counter resets are issues only for counter
    series (``metric_semantics="counter"`` or ``_total``/``_count`` suffix).
    """
    issues: List[MetricIntegrityIssue] = []

    if step != EXPECTED_STEP_SECONDS:
        issues.append(
            MetricIntegrityIssue("INVALID_STEP", "", None, f"step must be exactly {EXPECTED_STEP_SECONDS}", {"step": step})
        )
    range_valid = True
    for bound, name in ((expected_start, "expected_start"), (expected_end, "expected_end")):
        if not _is_number(bound):
            issues.append(
                MetricIntegrityIssue("INVALID_TIME_RANGE", "", None, f"{name} must be a finite number", {name: str(bound)})
            )
            range_valid = False
    if range_valid and expected_end < expected_start:
        issues.append(
            MetricIntegrityIssue("INVALID_TIME_RANGE", "", None, "expected_end must be >= expected_start", {"expected_start": expected_start, "expected_end": expected_end})
        )
        range_valid = False

    if not series_set:
        issues.append(
            MetricIntegrityIssue("EMPTY_SERIES_SET", "", None, "series_set must not be empty", {})
        )

    identities = [series.identity for series in series_set]
    for identity in sorted({i for i in identities if identities.count(i) > 1}):
        issues.append(
            MetricIntegrityIssue("DUPLICATE_SERIES_IDENTITY", identity, None, "duplicate series identity", {"identity": identity})
        )

    for series in series_set:
        identity = series.identity
        if _contains_secret(identity):
            issues.append(
                MetricIntegrityIssue("SECRET_IN_SERIES_IDENTITY", REDACTED, None, "series identity contains a secret marker", {"identity": REDACTED})
            )
        for key, value in series.labels.items():
            if _contains_secret(str(value)):
                issues.append(
                    MetricIntegrityIssue("SECRET_IN_LABEL", REDACTED, None, f"label {key} value contains a secret marker", {"label": key, "value": REDACTED})
                )

        samples = list(series.samples)
        if not samples:
            issues.append(
                MetricIntegrityIssue("EMPTY_SERIES", identity, None, "series has no samples", {"identity": identity})
            )
            continue

        seen_timestamps = set()
        monotonic_reported = False
        duplicate_reported = False
        for index, sample in enumerate(samples):
            timestamp = sample.timestamp
            value = sample.value
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not math.isfinite(timestamp):
                issues.append(
                    MetricIntegrityIssue("NON_FINITE_TIMESTAMP", identity, None, "non-finite sample timestamp", {"identity": identity, "timestamp": str(timestamp)})
                )
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                issues.append(
                    MetricIntegrityIssue("NON_FINITE_VALUE", identity, float(timestamp), "non-finite sample value", {"identity": identity, "value": str(value)})
                )
            if timestamp in seen_timestamps:
                if not duplicate_reported:
                    issues.append(
                        MetricIntegrityIssue("DUPLICATE_TIMESTAMP", identity, float(timestamp), "duplicate sample timestamp", {"identity": identity, "timestamp": float(timestamp)})
                    )
                    duplicate_reported = True
            elif index > 0 and timestamp < samples[index - 1].timestamp:
                if not monotonic_reported:
                    issues.append(
                        MetricIntegrityIssue("NON_MONOTONIC_TIMESTAMP", identity, float(timestamp), "sample timestamps must be strictly increasing", {"identity": identity, "timestamp": float(timestamp)})
                    )
                    monotonic_reported = True
            seen_timestamps.add(timestamp)

        if range_valid:
            missing = detect_missing_samples(series, expected_start, expected_end, step)
            for point in missing:
                issues.append(
                    MetricIntegrityIssue("MISSING_SAMPLE", identity, float(point), f"missing sample at expected timestamp {point}", {"identity": identity, "timestamp": float(point)})
                )
            # Consecutive missing runs: gap = run length * step.
            run_length = 0
            run_start: Optional[float] = None
            runs: List[Tuple[float, int]] = []
            for point in missing:
                if run_length and point == run_start + run_length * step:
                    run_length += 1
                else:
                    if run_length:
                        runs.append((run_start, run_length))
                    run_start = point
                    run_length = 1
            if run_length:
                runs.append((run_start, run_length))
            for start_point, length in runs:
                gap_seconds = length * step
                if gap_seconds > MAX_MISSING_GAP_SECONDS:
                    issues.append(
                        MetricIntegrityIssue("MISSING_GAP_EXCEEDED", identity, float(start_point), f"missing-data gap {gap_seconds}s exceeds {MAX_MISSING_GAP_SECONDS}s", {"identity": identity, "gap_seconds": gap_seconds, "start_timestamp": float(start_point)})
                    )

        if _is_counter_series(series):
            for reset in detect_counter_resets(series):
                issues.append(
                    MetricIntegrityIssue("COUNTER_RESET", identity, float(reset.timestamp), "counter reset detected", {"identity": identity, "previous_timestamp": reset.previous_timestamp, "previous_value": reset.previous_value, "timestamp": reset.timestamp, "value": reset.value})
                )

    sorted_issues = _sort_issues(issues)
    valid = not any(issue.code != "MISSING_SAMPLE" for issue in sorted_issues)
    return MetricIntegrityResult(
        valid=valid,
        issues=sorted_issues,
        checked_series=len(series_set),
        expected_step_seconds=step,
        expected_start=expected_start,
        expected_end=expected_end,
    )


# ---------------------------------------------------------------------------
# Combination and serialization
# ---------------------------------------------------------------------------

_INFORMATIONAL_ISSUE_CODES = frozenset({"MISSING_SAMPLE"})


def _issue_to_reason(issue: MetricIntegrityIssue) -> ValidityReason:
    severity = "info" if issue.code in _INFORMATIONAL_ISSUE_CODES else "error"
    return ValidityReason(
        code=issue.code,
        field=issue.identity,
        message=issue.message,
        observed=_json_safe(dict(issue.details)),
        expected=None,
        severity=severity,
    )


def combine_validity_results(
    run_result: ValidityResult,
    metric_result: MetricIntegrityResult,
) -> ValidityResult:
    """Aggregate run and metric validity into one deterministic result."""
    combined: List[ValidityReason] = list(run_result.reasons)
    combined.extend(_issue_to_reason(issue) for issue in metric_result.issues)
    seen = set()
    unique: List[ValidityReason] = []
    for reason in combined:
        key = (
            reason.code,
            reason.field,
            reason.message,
            json.dumps(_json_safe(reason.observed), sort_keys=True),
            json.dumps(_json_safe(reason.expected), sort_keys=True),
            reason.severity,
        )
        if key not in seen:
            seen.add(key)
            unique.append(reason)
    gates = tuple(run_result.checked_gates)
    if "metric_integrity" not in gates:
        gates = gates + ("metric_integrity",)
    return ValidityResult(
        valid=run_result.valid and metric_result.valid,
        reasons=_sort_reasons(unique),
        checked_gates=gates,
        run_id=run_result.run_id,
        manifest_id=run_result.manifest_id,
    )


def validity_result_to_dict(result: ValidityResult) -> Dict[str, Any]:
    """Serialize a ValidityResult to a deterministic JSON-safe dict."""
    return {
        "valid": result.valid,
        "run_id": result.run_id,
        "manifest_id": result.manifest_id,
        "checked_gates": list(result.checked_gates),
        "reasons": [
            {
                "code": reason.code,
                "field": reason.field,
                "message": reason.message,
                "observed": _json_safe(reason.observed),
                "expected": _json_safe(reason.expected),
                "severity": reason.severity,
            }
            for reason in result.reasons
        ],
    }
