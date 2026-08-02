"""A3 cross-level scaling, three-run aggregation, regression, and capacity.

Aggregates the three valid runs per load level, evaluates the approved
scaling guardrails between consecutive levels, compares level medians
against the previous approved baseline within regression budgets, and
derives safe/conditional capacity verdicts. Pure and deterministic: no
wall-clock time, hostname, PID, or randomness.
"""

import dataclasses
import math
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from tools.performance.a3.models import CapacityVerdict, MetricVerdict
from tools.performance.a3.slo import percentile

REQUIRED_VALID_RUN_COUNT = 3
LOAD_LEVELS = (500, 1000, 2500, 5000)
CONSECUTIVE_PAIRS = ((500, 1000), (1000, 2500), (2500, 5000))
BASELINE_LEVEL = 500

REQUIRED_METRICS = (
    "cpu_p95_percent",
    "memory_per_user_bytes",
    "latency_p95_ms",
    "latency_p99_ms",
    "throughput_per_second",
    "error_rate",
)
# Throughput is the only lower-bound (higher-is-better) metric.
THROUGHPUT_METRIC = "throughput_per_second"

P95_SCALING_BUDGET = 1.50
P99_SCALING_BUDGET = 1.75
MEMORY_PER_USER_BUDGET = 1.20
ERROR_RATE_SCALING_BUDGET = 2.00
THROUGHPUT_PROPORTIONAL_FLOOR = 0.80

REGRESSION_BUDGETS = (
    ("CPU_REGRESSION_EXCEEDED", "cpu_p95_percent", "<=", 1.10),
    ("MEMORY_PER_USER_REGRESSION_EXCEEDED", "memory_per_user_bytes", "<=", 1.10),
    ("P95_LATENCY_REGRESSION_EXCEEDED", "latency_p95_ms", "<=", 1.15),
    ("P99_LATENCY_REGRESSION_EXCEEDED", "latency_p99_ms", "<=", 1.20),
    ("THROUGHPUT_REGRESSION_EXCEEDED", "throughput_per_second", ">=", 0.90),
    ("ERROR_RATE_REGRESSION_EXCEEDED", "error_rate", "<=", 1.25),
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RunSummary:
    run_id: str
    manifest_id: str
    load_level: int
    run_number: int
    valid: bool
    verdict: MetricVerdict
    metrics: Mapping[str, float]
    catastrophic: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclasses.dataclass(frozen=True)
class LevelAggregation:
    load_level: int
    manifest_id: str
    valid_run_count: int
    required_valid_run_count: int
    run_ids: Tuple[str, ...]
    run_verdicts: Tuple[MetricVerdict, ...]
    verdict: MetricVerdict
    median_metrics: Mapping[str, float]
    worst_metrics: Mapping[str, float]
    stability_metrics: Mapping[str, float]
    warnings: Tuple[str, ...]
    failures: Tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("median_metrics", "worst_metrics", "stability_metrics"):
            object.__setattr__(
                self, field, MappingProxyType(dict(getattr(self, field)))
            )


@dataclasses.dataclass(frozen=True)
class ScalingCheck:
    code: str
    from_level: int
    to_level: int
    metric: str
    observed_ratio: Optional[float]
    threshold_ratio: float
    passed: bool
    message: str


@dataclasses.dataclass(frozen=True)
class ScalingResult:
    passed: bool
    checks: Tuple[ScalingCheck, ...]
    first_degradation_level: Optional[int]


@dataclasses.dataclass(frozen=True)
class RegressionCheck:
    code: str
    load_level: int
    metric: str
    current: Optional[float]
    previous: Optional[float]
    change_ratio: Optional[float]
    budget_ratio: float
    passed: bool
    message: str


@dataclasses.dataclass(frozen=True)
class RegressionResult:
    passed: bool
    checks: Tuple[RegressionCheck, ...]
    compared_levels: Tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class CapacityResult:
    safe_capacity: Optional[int]
    conditional_capacity: Optional[int]
    tested_ceiling: Optional[int]
    verdict: CapacityVerdict
    first_degradation_level: Optional[int]
    notes: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _validate_metrics(metrics: Mapping[str, Any]) -> Optional[str]:
    """Return a deterministic defect description, or None when clean."""
    for name in REQUIRED_METRICS:
        if name not in metrics:
            return f"missing metric {name}"
        value = metrics[name]
        if not _is_number(value):
            return f"metric {name} must be a finite number"
        if value < 0:
            return f"metric {name} must not be negative"
    if metrics["error_rate"] > 1.0:
        return "metric error_rate must be within [0, 1]"
    if metrics["throughput_per_second"] <= 0:
        return "metric throughput_per_second must be > 0"
    return None


def _population_stddev(values: Sequence[float], mean: float) -> float:
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_level(runs: Sequence[RunSummary]) -> LevelAggregation:
    """Aggregate exactly three valid runs of one load level.

    Invalid runs never count toward the three. Fewer than three valid runs,
    identity inconsistencies, catastrophic runs, or malformed metrics make
    the level BLOCKED with deterministic failure descriptions. More than
    three valid runs raise :class:`ValueError`.
    """
    runs = list(runs)
    failures: List[str] = []
    warnings: List[str] = []

    levels = {run.load_level for run in runs}
    if len(levels) > 1:
        failures.append(f"load_level mismatch across runs: {sorted(levels)}")
    load_level = runs[0].load_level if runs else 0

    valid_runs = [run for run in runs if run.valid]

    malformed: List[str] = []
    usable: List[RunSummary] = []
    for run in valid_runs:
        defect = _validate_metrics(run.metrics)
        if defect is not None:
            malformed.append(f"run {run.run_id} malformed metrics: {defect}")
        else:
            usable.append(run)
    failures.extend(malformed)

    if len(usable) > REQUIRED_VALID_RUN_COUNT:
        raise ValueError(
            f"expected exactly {REQUIRED_VALID_RUN_COUNT} valid runs after "
            f"filtering, got {len(usable)}"
        )

    ordered = sorted(usable, key=lambda run: run.run_number)

    run_ids = [run.run_id for run in usable]
    if len(run_ids) != len(set(run_ids)):
        failures.append("duplicate run_id among valid runs")

    run_numbers = sorted(run.run_number for run in usable)
    if len(usable) == REQUIRED_VALID_RUN_COUNT and run_numbers != [1, 2, 3]:
        failures.append(
            f"run_number values must be exactly {{1, 2, 3}}, got {run_numbers}"
        )
    elif len(usable) != len(set(run_numbers)):
        failures.append(f"duplicate run_number values: {run_numbers}")

    manifests = {run.manifest_id for run in usable}
    if len(manifests) > 1:
        failures.append(f"manifest_id mismatch across valid runs: {sorted(manifests)}")
    manifest_id = usable[0].manifest_id if usable else (runs[0].manifest_id if runs else "")

    for run in ordered:
        if run.catastrophic:
            failures.append(f"run {run.run_id} is catastrophic")

    if len(usable) < REQUIRED_VALID_RUN_COUNT:
        failures.append(
            f"insufficient valid runs: {len(usable)} of "
            f"{REQUIRED_VALID_RUN_COUNT} required"
        )

    complete = len(usable) == REQUIRED_VALID_RUN_COUNT
    medians: Dict[str, float] = {}
    worst: Dict[str, float] = {}
    stability: Dict[str, float] = {}
    if complete and not malformed:
        for name in REQUIRED_METRICS:
            values = [float(run.metrics[name]) for run in ordered]
            medians[name] = percentile(values, 0.5)
            if name == THROUGHPUT_METRIC:
                worst[name] = min(values)
            else:
                worst[name] = max(values)
            mean = sum(values) / len(values)
            stability[f"{name}.min"] = min(values)
            stability[f"{name}.max"] = max(values)
            stability[f"{name}.range"] = max(values) - min(values)
            stability[f"{name}.mean"] = mean
            stability[f"{name}.stddev"] = _population_stddev(values, mean)

    if failures:
        verdict = MetricVerdict.BLOCKED
    elif any(run.verdict is MetricVerdict.BLOCKED for run in ordered):
        verdict = MetricVerdict.BLOCKED
        failures.extend(
            f"run {run.run_id} verdict BLOCKED"
            for run in ordered
            if run.verdict is MetricVerdict.BLOCKED
        )
    elif any(run.verdict is MetricVerdict.FAIL for run in ordered):
        verdict = MetricVerdict.FAIL
        failures.extend(
            f"run {run.run_id} verdict FAIL"
            for run in ordered
            if run.verdict is MetricVerdict.FAIL
        )
    elif any(run.verdict is MetricVerdict.PASS_WITH_WARNING for run in ordered):
        verdict = MetricVerdict.PASS_WITH_WARNING
        warnings.extend(
            f"run {run.run_id} verdict PASS_WITH_WARNING"
            for run in ordered
            if run.verdict is MetricVerdict.PASS_WITH_WARNING
        )
    else:
        verdict = MetricVerdict.PASS

    return LevelAggregation(
        load_level=load_level,
        manifest_id=manifest_id,
        valid_run_count=len(usable),
        required_valid_run_count=REQUIRED_VALID_RUN_COUNT,
        run_ids=tuple(run.run_id for run in ordered),
        run_verdicts=tuple(run.verdict for run in ordered),
        verdict=verdict,
        median_metrics=medians,
        worst_metrics=worst,
        stability_metrics=stability,
        warnings=tuple(warnings),
        failures=tuple(failures),
    )


# ---------------------------------------------------------------------------
# Scaling
# ---------------------------------------------------------------------------


def _ratio_check(
    code: str,
    from_level: int,
    to_level: int,
    metric: str,
    current: Any,
    previous: Any,
    budget: float,
    lower_bound: bool = False,
) -> ScalingCheck:
    if not _is_number(current) or not _is_number(previous) or previous < 0 or current < 0:
        return ScalingCheck("INVALID_SCALING_METRIC", from_level, to_level, metric, None, budget, False, f"invalid scaling metric values for {metric}")
    if previous == 0:
        return ScalingCheck(code, from_level, to_level, metric, None, budget, False, f"zero denominator for {metric}; ratio undefined")
    ratio = current / previous
    if lower_bound:
        passed = ratio >= budget
        message = f"{metric} ratio {ratio!r} vs required >= {budget}"
    else:
        passed = ratio <= budget
        message = f"{metric} ratio {ratio!r} vs budget <= {budget}"
    return ScalingCheck(code, from_level, to_level, metric, ratio, budget, passed, message)


def evaluate_scaling(levels: Sequence[LevelAggregation]) -> ScalingResult:
    """Evaluate the approved guardrails between consecutive load levels."""
    by_level = {level.load_level: level for level in levels}
    checks: List[ScalingCheck] = []

    baseline = by_level.get(BASELINE_LEVEL)
    for from_level, to_level in CONSECUTIVE_PAIRS:
        current = by_level.get(to_level)
        previous = by_level.get(from_level)
        if previous is None or current is None:
            missing = from_level if previous is None else to_level
            checks.append(
                ScalingCheck("LEVEL_MISSING", from_level, to_level, "", None, 0.0, False, f"level {missing} required for comparison is missing")
            )
            continue
        if previous.verdict is MetricVerdict.BLOCKED or current.verdict is MetricVerdict.BLOCKED:
            checks.append(
                ScalingCheck("LEVEL_BLOCKED", from_level, to_level, "", None, 0.0, False, f"level {from_level} or {to_level} is BLOCKED")
            )
            continue
        prev = previous.median_metrics
        cur = current.median_metrics
        checks.append(
            _ratio_check("P95_LATENCY_SCALING_EXCEEDED", from_level, to_level, "latency_p95_ms", cur.get("latency_p95_ms"), prev.get("latency_p95_ms"), P95_SCALING_BUDGET)
        )
        checks.append(
            _ratio_check("P99_LATENCY_SCALING_EXCEEDED", from_level, to_level, "latency_p99_ms", cur.get("latency_p99_ms"), prev.get("latency_p99_ms"), P99_SCALING_BUDGET)
        )
        checks.append(
            _ratio_check("ERROR_RATE_SCALING_EXCEEDED", from_level, to_level, "error_rate", cur.get("error_rate"), prev.get("error_rate"), ERROR_RATE_SCALING_BUDGET)
        )
        # Throughput proportionality: growth >= user growth * 0.80.
        user_growth = to_level / from_level
        required = user_growth * THROUGHPUT_PROPORTIONAL_FLOOR
        checks.append(
            _ratio_check("THROUGHPUT_SCALING_BELOW_MINIMUM", from_level, to_level, "throughput_per_second", cur.get("throughput_per_second"), prev.get("throughput_per_second"), required, lower_bound=True)
        )

    # Memory per user vs the 500-user baseline for every higher level.
    for to_level in LOAD_LEVELS[1:]:
        current = by_level.get(to_level)
        if current is None or current.verdict is MetricVerdict.BLOCKED:
            continue
        if baseline is None or baseline.verdict is MetricVerdict.BLOCKED:
            checks.append(
                ScalingCheck("LEVEL_MISSING", BASELINE_LEVEL, to_level, "memory_per_user_bytes", None, MEMORY_PER_USER_BUDGET, False, "500-user baseline missing for memory-per-user comparison")
            )
            continue
        checks.append(
            _ratio_check("MEMORY_PER_USER_SCALING_EXCEEDED", BASELINE_LEVEL, to_level, "memory_per_user_bytes", current.median_metrics.get("memory_per_user_bytes"), baseline.median_metrics.get("memory_per_user_bytes"), MEMORY_PER_USER_BUDGET)
        )

    checks.sort(key=lambda c: (c.to_level, c.code, c.metric))

    degradation: List[int] = []
    for level in sorted(by_level):
        if by_level[level].verdict in (
            MetricVerdict.PASS_WITH_WARNING,
            MetricVerdict.FAIL,
            MetricVerdict.BLOCKED,
        ):
            degradation.append(level)
    for check in checks:
        if not check.passed and check.to_level:
            degradation.append(check.to_level)
    first_degradation = min(degradation) if degradation else None

    return ScalingResult(
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
        first_degradation_level=first_degradation,
    )


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------


def _validate_previous_baseline(previous: Any) -> List[str]:
    if not isinstance(previous, dict):
        raise ValueError("previous baseline must be a dict")
    defects: List[str] = []
    if previous.get("version") != 1:
        defects.append("version must be 1")
    if previous.get("approval_state") != "APPROVED":
        defects.append("approval_state must be APPROVED")
    manifest_id = previous.get("manifest_id")
    if (
        not isinstance(manifest_id, str)
        or not manifest_id
        or ".." in manifest_id
        or "\x00" in manifest_id
        or not all(c.isascii() and (c.isalnum() or c in ".-_") for c in manifest_id)
    ):
        defects.append("manifest_id is not a safe identifier")
    levels = previous.get("levels")
    if not isinstance(levels, dict):
        defects.append("levels must be an object")
    else:
        for key in sorted(levels):
            if key not in {str(level) for level in LOAD_LEVELS}:
                defects.append(f"unexpected level key {key}")
    return defects


def evaluate_regression(
    current: Sequence[LevelAggregation],
    previous: dict,
) -> RegressionResult:
    """Compare current level medians with the previous approved baseline."""
    defects = _validate_previous_baseline(previous)
    checks: List[RegressionCheck] = []
    compared: List[int] = []

    for defect in defects:
        checks.append(
            RegressionCheck("MANIFEST_COMPARISON_INVALID", 0, "", None, None, None, 0.0, False, f"previous baseline invalid: {defect}")
        )

    current_by_level = {level.load_level: level for level in current}
    previous_levels = previous.get("levels") if isinstance(previous, dict) else None
    if not isinstance(previous_levels, dict):
        previous_levels = {}

    if not defects:
        for level in LOAD_LEVELS:
            current_level = current_by_level.get(level)
            previous_level = previous_levels.get(str(level))
            if current_level is None:
                if previous_level is not None:
                    checks.append(
                        RegressionCheck("CURRENT_LEVEL_MISSING", level, "", None, None, None, 0.0, False, f"current level {level} missing")
                    )
                continue
            if previous_level is None:
                checks.append(
                    RegressionCheck("PREVIOUS_LEVEL_MISSING", level, "", None, None, None, 0.0, False, f"previous baseline level {level} missing")
                )
                continue
            compared.append(level)
            previous_metrics = (
                previous_level.get("median_metrics")
                if isinstance(previous_level, dict)
                else None
            )
            for code, metric, direction, budget in REGRESSION_BUDGETS:
                cur = current_level.median_metrics.get(metric)
                prev = (
                    previous_metrics.get(metric)
                    if isinstance(previous_metrics, dict)
                    else None
                )
                if not _is_number(cur) or not _is_number(prev):
                    checks.append(
                        RegressionCheck("INVALID_REGRESSION_METRIC", level, metric, cur if _is_number(cur) else None, prev if _is_number(prev) else None, None, budget, False, f"metric {metric} missing or non-finite")
                    )
                    continue
                if prev == 0:
                    checks.append(
                        RegressionCheck(code, level, metric, cur, prev, None, budget, False, f"zero previous value for {metric}; ratio undefined")
                    )
                    continue
                ratio = cur / prev
                if direction == "<=":
                    passed = ratio <= budget
                else:
                    passed = ratio >= budget
                checks.append(
                    RegressionCheck(code, level, metric, cur, prev, ratio, budget, passed, f"{metric} ratio {ratio!r} vs budget {direction} {budget}")
                )

    checks.sort(key=lambda c: (c.load_level, c.code, c.metric))

    checks_passed = all(check.passed for check in checks)
    absolute_slo_ok = all(
        level.verdict not in (MetricVerdict.FAIL, MetricVerdict.BLOCKED)
        for level in current
    )
    return RegressionResult(
        passed=checks_passed and absolute_slo_ok,
        checks=tuple(checks),
        compared_levels=tuple(sorted(compared)),
    )


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

_VERDICT_SEVERITY = {
    MetricVerdict.PASS: 0,
    MetricVerdict.PASS_WITH_WARNING: 1,
    MetricVerdict.FAIL: 2,
    MetricVerdict.BLOCKED: 3,
}


def derive_capacity(level_verdicts: Sequence[LevelAggregation]) -> CapacityResult:
    """Derive safe/conditional capacity and the tested ceiling."""
    levels = sorted(level_verdicts, key=lambda level: level.load_level)
    notes: List[str] = []

    if not levels:
        return CapacityResult(
            safe_capacity=None,
            conditional_capacity=None,
            tested_ceiling=None,
            verdict=CapacityVerdict.NOT_ESTABLISHED,
            first_degradation_level=None,
            notes=("no load levels tested",),
        )

    verdict_of = {level.load_level: level.verdict for level in levels}
    tested = sorted(verdict_of)
    blocked = [level for level in tested if verdict_of[level] is MetricVerdict.BLOCKED]
    failed = [level for level in tested if verdict_of[level] is MetricVerdict.FAIL]
    warnings = [level for level in tested if verdict_of[level] is MetricVerdict.PASS_WITH_WARNING]
    passes = [level for level in tested if verdict_of[level] is MetricVerdict.PASS]
    completed = passes + warnings + failed

    tested_ceiling = max(completed) if completed else None

    if BASELINE_LEVEL not in verdict_of:
        notes.append("missing 500-user level prevents safe capacity establishment")

    def clean_below(level: int) -> bool:
        return all(
            verdict_of[lower] not in (MetricVerdict.FAIL, MetricVerdict.BLOCKED)
            for lower in tested
            if lower < level
        )

    safe_candidates = [
        level
        for level in passes
        if BASELINE_LEVEL in verdict_of and clean_below(level)
    ]
    safe_capacity = max(safe_candidates) if safe_candidates else None

    conditional_candidates = [level for level in warnings if clean_below(level)]
    conditional_capacity = max(conditional_candidates) if conditional_candidates else None

    degraded = sorted(warnings + failed + blocked)
    first_degradation = degraded[0] if degraded else None

    # Non-monotonic progression: a worse verdict below a better one.
    non_monotonic = any(
        _VERDICT_SEVERITY[verdict_of[lower]] > _VERDICT_SEVERITY[verdict_of[higher]]
        for lower in tested
        for higher in tested
        if lower < higher
    )
    if non_monotonic:
        notes.append("non-monotonic verdict progression detected")

    if blocked:
        notes.append(f"level {min(blocked)} blocked; capacity result not usable")
        verdict = CapacityVerdict.BLOCKED
    elif not passes and not warnings:
        verdict = CapacityVerdict.FAIL if failed else CapacityVerdict.NOT_ESTABLISHED
    else:
        highest_established = max(passes + warnings)
        if any(level > highest_established for level in failed):
            verdict = CapacityVerdict.FAIL
        elif conditional_capacity is not None and (
            safe_capacity is None or conditional_capacity > safe_capacity
        ):
            verdict = CapacityVerdict.PASS_WITH_WARNING
        elif non_monotonic:
            # A higher PASS must not hide a lower-level inconsistency.
            verdict = CapacityVerdict.PASS_WITH_WARNING
        else:
            verdict = CapacityVerdict.PASS

    return CapacityResult(
        safe_capacity=safe_capacity,
        conditional_capacity=conditional_capacity,
        tested_ceiling=tested_ceiling,
        verdict=verdict,
        first_degradation_level=first_degradation,
        notes=tuple(sorted(notes)),
    )
