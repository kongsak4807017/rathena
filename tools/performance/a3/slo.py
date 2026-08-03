"""A3 percentile, rate, SLO, warning, and catastrophic verdict engine.

Evaluates steady-state metric bundles against the approved thresholds in
``tools/performance/a3/config/slo-thresholds.json`` (injectable for tests).
Warning zone: observed >= 90% of a positive upper bound is
PASS_WITH_WARNING; strictly above the hard limit is FAIL. Zero-tolerance
metrics have no warning state. Catastrophic zero-tolerance violations and
explicit catastrophic signals block the run immediately.
"""

import dataclasses
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from tools.performance.a3.io import read_json
from tools.performance.a3.models import MetricVerdict

_THRESHOLDS_PATH = Path(__file__).resolve().parent / "config" / "slo-thresholds.json"

CATASTROPHIC_INVALID_RUN = "INVALID_RUN"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MetricObservation:
    metric: str
    statistic: str
    observed: float


@dataclasses.dataclass(frozen=True)
class MetricEvaluation:
    metric: str
    statistic: str
    observed: Optional[float]
    threshold: Optional[float]
    warning_threshold: Optional[float]
    verdict: MetricVerdict
    code: str
    message: str
    catastrophic: bool
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclasses.dataclass(frozen=True)
class CatastrophicSignal:
    code: str
    message: str
    source: str
    observed: Any


@dataclasses.dataclass(frozen=True)
class RunSLOResult:
    status: MetricVerdict
    evaluations: Tuple[MetricEvaluation, ...]
    catastrophic_signals: Tuple[CatastrophicSignal, ...]
    evaluated_metrics: Tuple[str, ...]
    blocked_metrics: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile on a sorted copy of ``values``."""
    if not _is_number(q) or not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be a number within [0, 1], got {q!r}")
    if not values:
        raise ValueError("values must not be empty")
    checked: List[float] = []
    for value in values:
        if not _is_number(value):
            raise ValueError(f"values must be finite numbers, got {value!r}")
        checked.append(float(value))
    ordered = sorted(checked)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _validated_samples(samples: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    parsed: List[Tuple[float, float]] = []
    for sample in samples:
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            raise ValueError(f"malformed sample: {sample!r}")
        timestamp, value = sample
        if not _is_number(timestamp) or not _is_number(value):
            raise ValueError(f"non-finite sample: {sample!r}")
        parsed.append((float(timestamp), float(value)))
    for previous, current in zip(parsed, parsed[1:]):
        if current[0] <= previous[0]:
            raise ValueError("sample timestamps must be strictly increasing")
    return parsed


def counter_rate(samples: Sequence[Tuple[float, float]]) -> float:
    """Rate between first and last counter sample; resets are rejected."""
    if len(samples) < 2:
        raise ValueError("counter_rate requires at least two samples")
    parsed = _validated_samples(samples)
    for previous, current in zip(parsed, parsed[1:]):
        if current[1] < 0 or previous[1] < 0:
            raise ValueError("counter values must be non-negative")
        if current[1] < previous[1]:
            raise ValueError("counter decrease indicates a reset; rejected")
    first_timestamp, first_value = parsed[0]
    last_timestamp, last_value = parsed[-1]
    elapsed = last_timestamp - first_timestamp
    if elapsed <= 0:
        raise ValueError("elapsed time must be > 0")
    return (last_value - first_value) / elapsed


def longest_sustained_duration(
    samples: Sequence[Tuple[float, float]],
    predicate: Callable[[float], bool],
) -> float:
    """Longest contiguous time span in which ``predicate`` stays true.

    A run ends at the timestamp of the first false sample; if the final
    sample remains true, its own timestamp ends the run.
    """
    if not samples:
        return 0.0
    parsed: List[Tuple[float, Any]] = []
    for sample in samples:
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            raise ValueError(f"malformed sample: {sample!r}")
        timestamp, value = sample
        if not _is_number(timestamp):
            raise ValueError(f"non-finite sample timestamp: {sample!r}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"non-finite sample value: {sample!r}")
        parsed.append((float(timestamp), value))
    for previous, current in zip(parsed, parsed[1:]):
        if current[0] <= previous[0]:
            raise ValueError("sample timestamps must be strictly increasing")
    best = 0.0
    run_start: Optional[float] = None
    for timestamp, value in parsed:
        if predicate(value):
            if run_start is None:
                run_start = timestamp
        else:
            if run_start is not None:
                best = max(best, timestamp - run_start)
                run_start = None
    if run_start is not None:
        best = max(best, parsed[-1][0] - run_start)
    return best


# ---------------------------------------------------------------------------
# Metric specification table (threshold keys -> committed slo-thresholds.json)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _MetricSpec:
    metric: str
    statistic: str
    kind: str  # "percentile" | "max" | "scalar" | "sustained" | "zero"
    bundle_path: Tuple[str, ...]
    threshold_path: Tuple[str, ...]
    q: float = 0.0
    sustained_above_path: Tuple[str, ...] = ()
    catastrophic_code: Optional[str] = None


_METRIC_SPECS: Tuple[_MetricSpec, ...] = (
    _MetricSpec("cpu.median_percent", "p50", "percentile", ("cpu_percent", "samples"), ("cpu", "median_max_percent"), q=0.5),
    _MetricSpec("cpu.p95_percent", "p95", "percentile", ("cpu_percent", "samples"), ("cpu", "p95_max_percent"), q=0.95),
    _MetricSpec("cpu.sustained_above_95_seconds", "sustained_seconds", "sustained", ("cpu_percent", "samples"), ("cpu", "sustained_max_seconds"), sustained_above_path=("cpu", "sustained_above_percent")),
    _MetricSpec("memory.rss_percent", "max", "max", ("memory", "rss_percent_of_ram"), ("memory", "total_rss_max_percent_of_ram")),
    _MetricSpec("memory.growth_percent", "scalar", "scalar", ("memory", "steady_state_growth_percent"), ("memory", "steady_state_growth_max_percent")),
    _MetricSpec("memory.swap_in", "scalar", "zero", ("memory", "swap_in"), ("memory", "swap_in_max")),
    _MetricSpec("memory.swap_out", "scalar", "zero", ("memory", "swap_out"), ("memory", "swap_out_max")),
    _MetricSpec("memory.oom", "scalar", "zero", ("memory", "oom"), ("memory", "oom_max"), catastrophic_code="oom"),
    _MetricSpec("memory.allocation_failure", "scalar", "zero", ("memory", "allocation_failure"), ("memory", "allocation_failure_max"), catastrophic_code="allocation_failure"),
    _MetricSpec("tick.p50_ms", "p50", "percentile", ("tick_latency_ms", "samples"), ("tick_latency_ms", "p50_max"), q=0.5),
    _MetricSpec("tick.p95_ms", "p95", "percentile", ("tick_latency_ms", "samples"), ("tick_latency_ms", "p95_max"), q=0.95),
    _MetricSpec("tick.p99_ms", "p99", "percentile", ("tick_latency_ms", "samples"), ("tick_latency_ms", "p99_max"), q=0.99),
    _MetricSpec("tick.max_ms", "max", "max", ("tick_latency_ms", "samples"), ("tick_latency_ms", "max_max")),
    _MetricSpec("tick.sustained_above_50_seconds", "sustained_seconds", "sustained", ("tick_latency_ms", "samples"), ("tick_latency_ms", "sustained_max_seconds"), sustained_above_path=("tick_latency_ms", "sustained_above_ms")),
    _MetricSpec("packet.p95_ms", "p95", "percentile", ("packet_processing_ms", "samples"), ("packet_processing_ms", "p95_max"), q=0.95),
    _MetricSpec("packet.p99_ms", "p99", "percentile", ("packet_processing_ms", "samples"), ("packet_processing_ms", "p99_max"), q=0.99),
    _MetricSpec("packet.max_ms", "max", "max", ("packet_processing_ms", "samples"), ("packet_processing_ms", "max_max")),
    _MetricSpec("packet.backlog_growth_seconds", "sustained_seconds", "sustained", ("packet_processing_ms", "backlog_growth_samples"), ("packet_processing_ms", "backlog_growth_max_seconds")),
    _MetricSpec("packet.dropped_or_rejected_ratio", "scalar", "scalar", ("packet_processing_ms", "dropped_or_rejected_ratio"), ("packet_processing_ms", "dropped_or_rejected_max_ratio")),
    _MetricSpec("sql.p95_ms", "p95", "percentile", ("sql_ms", "samples"), ("sql_ms", "p95_max"), q=0.95),
    _MetricSpec("sql.p99_ms", "p99", "percentile", ("sql_ms", "samples"), ("sql_ms", "p99_max"), q=0.99),
    _MetricSpec("sql.max_ms", "max", "max", ("sql_ms", "samples"), ("sql_ms", "max_max")),
    _MetricSpec("sql.slow_ratio", "scalar", "scalar", ("sql_ms", "slow_query_ratio"), ("sql_ms", "slow_query_max_ratio")),
    _MetricSpec("sql.failure_ratio", "scalar", "scalar", ("sql_ms", "execution_failure_ratio"), ("sql_ms", "execution_failure_max_ratio")),
    _MetricSpec("sql.connection_usage_p95_ratio", "p95", "percentile", ("sql_ms", "connection_usage_ratio_samples"), ("sql_ms", "connection_usage_p95_max_ratio_of_configured_max"), q=0.95),
    _MetricSpec("sql.acquisition_failure", "scalar", "zero", ("sql_ms", "connection_acquisition_failure"), ("sql_ms", "connection_acquisition_failure_max")),
    _MetricSpec("sql.deadlock", "scalar", "zero", ("sql_ms", "deadlock"), ("sql_ms", "deadlock_max"), catastrophic_code="deadlock"),
    _MetricSpec("sql.lock_wait_timeout", "scalar", "zero", ("sql_ms", "lock_wait_timeout"), ("sql_ms", "lock_wait_timeout_max")),
    _MetricSpec("script.p95_ms", "p95", "percentile", ("script_ms", "samples"), ("script_ms", "p95_max"), q=0.95),
    _MetricSpec("script.p99_ms", "p99", "percentile", ("script_ms", "samples"), ("script_ms", "p99_max"), q=0.99),
    _MetricSpec("script.max_ms", "max", "max", ("script_ms", "samples"), ("script_ms", "max_max")),
    _MetricSpec("script.slow_ratio", "scalar", "scalar", ("script_ms", "slow_script_ratio"), ("script_ms", "slow_script_max_ratio")),
    _MetricSpec("script.failure_ratio", "scalar", "scalar", ("script_ms", "execution_failure_ratio"), ("script_ms", "execution_failure_max_ratio")),
    _MetricSpec("script.unknown_ratio", "scalar", "scalar", ("script_ms", "unknown_category_ratio"), ("script_ms", "unknown_category_max_ratio")),
    _MetricSpec("script.category_latency_multiple", "scalar", "scalar", ("script_ms", "category_latency_multiple_of_500_baseline"), ("script_ms", "category_latency_max_multiple_of_500_user_baseline")),
    _MetricSpec("errors.login_failure_ratio", "scalar", "scalar", ("errors", "login_failure_ratio"), ("errors", "login_failure_max_ratio")),
    _MetricSpec("errors.character_selection_failure_ratio", "scalar", "scalar", ("errors", "character_selection_failure_ratio"), ("errors", "character_selection_failure_max_ratio")),
    _MetricSpec("errors.unexpected_disconnect_ratio", "scalar", "scalar", ("errors", "unexpected_disconnect_ratio"), ("errors", "unexpected_steady_state_disconnect_max_ratio")),
    _MetricSpec("errors.process_crash", "scalar", "zero", ("errors", "process_crash"), ("errors", "process_crash_max"), catastrophic_code="process_crash"),
    _MetricSpec("errors.data_corruption", "scalar", "zero", ("errors", "data_corruption"), ("errors", "data_corruption_max"), catastrophic_code="data_corruption"),
    _MetricSpec("storage.utilization_p95_percent", "p95", "percentile", ("storage", "utilization_percent_samples"), ("storage", "utilization_p95_max_percent"), q=0.95),
    _MetricSpec("storage.await_p95_ms", "p95", "percentile", ("storage", "await_ms_samples"), ("storage", "await_p95_max_ms"), q=0.95),
    _MetricSpec("storage.await_p99_ms", "p99", "percentile", ("storage", "await_ms_samples"), ("storage", "await_p99_max_ms"), q=0.99),
    _MetricSpec("storage.queue_growth_seconds", "sustained_seconds", "sustained", ("storage", "queue_depth_growth_samples"), ("storage", "queue_depth_growth_max_seconds")),
    _MetricSpec("network.utilization_p95_percent", "p95", "percentile", ("network", "utilization_percent_samples"), ("network", "utilization_p95_max_percent_of_1gbps"), q=0.95),
    _MetricSpec("network.packet_loss_ratio", "scalar", "scalar", ("network", "packet_loss_ratio"), ("network", "packet_loss_max_ratio")),
    _MetricSpec("network.tcp_retransmission_ratio", "scalar", "scalar", ("network", "tcp_retransmission_ratio"), ("network", "tcp_retransmission_max_ratio")),
    _MetricSpec("network.socket_error", "scalar", "zero", ("network", "socket_error"), ("network", "socket_error_max")),
    _MetricSpec("network.listen_drop", "scalar", "zero", ("network", "listen_drop"), ("network", "listen_drop_max")),
)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class _Missing(Exception):
    pass


def _extract(data: Any, path: Tuple[str, ...]) -> Any:
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise _Missing(".".join(path))
        node = node[key]
    return node


def _sample_values(raw: Any) -> List[Tuple[float, float]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("samples must be a non-empty list")
    return _validated_samples(raw)


def _observed(spec: _MetricSpec, bundle: dict, thresholds: dict) -> float:
    raw = _extract(bundle, spec.bundle_path)
    if spec.kind == "percentile":
        values = [value for _, value in _sample_values(raw)]
        return percentile(values, spec.q)
    if spec.kind == "max":
        values = [value for _, value in _sample_values(raw)]
        return max(values)
    if spec.kind == "sustained":
        parsed = _sample_values(raw)
        if spec.sustained_above_path:
            level = _extract(thresholds, spec.sustained_above_path)
            predicate = lambda value: value > level
        else:
            predicate = lambda value: value > 0
        return longest_sustained_duration(parsed, predicate)
    # scalar / zero
    if not _is_number(raw):
        raise ValueError(f"metric value must be a finite number, got {raw!r}")
    return float(raw)


def _evaluate(spec: _MetricSpec, bundle: dict, thresholds: dict, warning_ratio: float) -> Tuple[MetricEvaluation, Optional[CatastrophicSignal]]:
    hard = _extract(thresholds, spec.threshold_path)
    warning_threshold = None if spec.kind == "zero" else hard * warning_ratio
    try:
        observed = _observed(spec, bundle, thresholds)
    except _Missing:
        return (
            MetricEvaluation(spec.metric, spec.statistic, None, hard, warning_threshold, MetricVerdict.BLOCKED, "METRIC_MISSING", f"required metric {spec.metric} missing", False, {}),
            None,
        )
    except (ValueError, TypeError) as exc:
        return (
            MetricEvaluation(spec.metric, spec.statistic, None, hard, warning_threshold, MetricVerdict.BLOCKED, "METRIC_INVALID", f"metric {spec.metric} malformed: {exc}", False, {}),
            None,
        )

    details = {"observed": observed}
    if spec.kind == "zero":
        if observed == 0:
            return (
                MetricEvaluation(spec.metric, spec.statistic, observed, hard, None, MetricVerdict.PASS, "SLO_PASS", "zero-tolerance metric clean", False, details),
                None,
            )
        if spec.catastrophic_code:
            signal = CatastrophicSignal(
                code=spec.catastrophic_code,
                message=f"{spec.metric} observed {observed}",
                source=spec.metric,
                observed=observed,
            )
            return (
                MetricEvaluation(spec.metric, spec.statistic, observed, hard, None, MetricVerdict.BLOCKED, "CATASTROPHIC", f"catastrophic signal {spec.catastrophic_code}", True, details),
                signal,
            )
        return (
            MetricEvaluation(spec.metric, spec.statistic, observed, hard, None, MetricVerdict.FAIL, "SLO_FAIL", "zero-tolerance metric violated", False, details),
            None,
        )

    if observed > hard:
        return (
            MetricEvaluation(spec.metric, spec.statistic, observed, hard, warning_threshold, MetricVerdict.FAIL, "SLO_FAIL", f"observed {observed} exceeds hard limit {hard}", False, details),
            None,
        )
    if observed >= warning_threshold:
        return (
            MetricEvaluation(spec.metric, spec.statistic, observed, hard, warning_threshold, MetricVerdict.PASS_WITH_WARNING, "SLO_WARNING", f"observed {observed} within warning zone of {hard}", False, details),
            None,
        )
    return (
        MetricEvaluation(spec.metric, spec.statistic, observed, hard, warning_threshold, MetricVerdict.PASS, "SLO_PASS", "within threshold", False, details),
        None,
    )


def evaluate_run_slos(metric_bundle: dict, thresholds: Optional[dict] = None) -> RunSLOResult:
    """Evaluate every approved metric and classify the run."""
    if thresholds is None:
        thresholds = read_json(_THRESHOLDS_PATH)
    warning_ratio = thresholds.get("warning_zone_ratio", 0.9)

    evaluations: List[MetricEvaluation] = []
    signals: List[CatastrophicSignal] = []
    for spec in _METRIC_SPECS:
        evaluation, signal = _evaluate(spec, metric_bundle, thresholds, warning_ratio)
        evaluations.append(evaluation)
        if signal is not None:
            signals.append(signal)

    evaluations.sort(key=lambda e: (e.metric, e.statistic))
    signals.sort(key=lambda s: (s.code, s.source, s.message))
    blocked = tuple(sorted(e.metric for e in evaluations if e.verdict is MetricVerdict.BLOCKED))
    evaluated = tuple(sorted({e.metric for e in evaluations}))
    return RunSLOResult(
        status=classify_run(evaluations, signals),
        evaluations=tuple(evaluations),
        catastrophic_signals=tuple(signals),
        evaluated_metrics=evaluated,
        blocked_metrics=blocked,
    )


def evaluate_valid_run_slos(
    validity_result,
    metric_bundle: dict,
    thresholds: Optional[dict] = None,
) -> RunSLOResult:
    """Evaluate SLOs only for a Task 6 valid run; invalid runs are BLOCKED."""
    if not validity_result.valid:
        return RunSLOResult(
            status=MetricVerdict.BLOCKED,
            evaluations=(),
            catastrophic_signals=(),
            evaluated_metrics=(),
            blocked_metrics=("run_validity",),
        )
    return evaluate_run_slos(metric_bundle, thresholds)


def classify_run(
    evaluations: Sequence[MetricEvaluation],
    catastrophic_signals: Sequence[CatastrophicSignal] = (),
) -> MetricVerdict:
    """Approved classification order for one run."""
    if catastrophic_signals:
        return MetricVerdict.BLOCKED
    if not evaluations:
        return MetricVerdict.BLOCKED
    verdicts = {evaluation.verdict for evaluation in evaluations}
    if MetricVerdict.BLOCKED in verdicts:
        return MetricVerdict.BLOCKED
    if MetricVerdict.FAIL in verdicts:
        return MetricVerdict.FAIL
    if MetricVerdict.PASS_WITH_WARNING in verdicts:
        return MetricVerdict.PASS_WITH_WARNING
    return MetricVerdict.PASS
