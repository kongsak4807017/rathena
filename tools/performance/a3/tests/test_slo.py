"""Tests for the A3 percentile/rate/SLO/warning/catastrophic verdict engine."""

import copy
import csv
import dataclasses
import json
import math
import unittest
from pathlib import Path

from tools.performance.a3.io import read_json
from tools.performance.a3.models import MetricVerdict
from tools.performance.a3.slo import (
    CatastrophicSignal,
    MetricEvaluation,
    RunSLOResult,
    _METRIC_SPECS,
    classify_run,
    counter_rate,
    evaluate_run_slos,
    evaluate_valid_run_slos,
    longest_sustained_duration,
    percentile,
)
from tools.performance.a3.validity import validate_run

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
THRESHOLDS_PATH = CONFIG_DIR / "slo-thresholds.json"
CSV_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "steady_state_timeseries.csv"
)
VALID_RUN_PATH = Path(__file__).resolve().parent / "fixtures" / "valid_run.json"


def thresholds() -> dict:
    return read_json(THRESHOLDS_PATH)


def valid_run_data() -> dict:
    return json.loads(VALID_RUN_PATH.read_text(encoding="utf-8"))


def samples(*values, start=0, step=5):
    return [[start + i * step, v] for i, v in enumerate(values)]


def valid_bundle() -> dict:
    return {
        "cpu_percent": {"samples": samples(50, 55, 52, 51)},
        "memory": {
            "rss_percent_of_ram": samples(70, 71, 70),
            "steady_state_growth_percent": 4.0,
            "swap_in": 0,
            "swap_out": 0,
            "oom": 0,
            "allocation_failure": 0,
        },
        "tick_latency_ms": {"samples": samples(5, 6, 5)},
        "packet_processing_ms": {
            "samples": samples(2, 3, 2),
            "backlog_growth_samples": samples(0, 0, 0),
            "dropped_or_rejected_ratio": 0.00005,
        },
        "sql_ms": {
            "samples": samples(10, 12, 11),
            "slow_query_ratio": 0.002,
            "execution_failure_ratio": 0.00005,
            "connection_acquisition_failure": 0,
            "deadlock": 0,
            "lock_wait_timeout": 0,
            "connection_usage_ratio_samples": samples(0.5, 0.6, 0.55),
        },
        "script_ms": {
            "samples": samples(2, 3, 2),
            "slow_script_ratio": 0.002,
            "execution_failure_ratio": 0.00005,
            "unknown_category_ratio": 0.02,
            "category_latency_multiple_of_500_baseline": 1.5,
        },
        "errors": {
            "login_failure_ratio": 0.0005,
            "character_selection_failure_ratio": 0.0005,
            "unexpected_disconnect_ratio": 0.002,
            "process_crash": 0,
            "data_corruption": 0,
        },
        "storage": {
            "utilization_percent_samples": samples(60, 62, 61),
            "await_ms_samples": samples(2, 3, 2),
            "queue_depth_growth_samples": samples(0, 0, 0),
        },
        "network": {
            "utilization_percent_samples": samples(50, 55, 52),
            "packet_loss_ratio": 0.0005,
            "tcp_retransmission_ratio": 0.0005,
            "socket_error": 0,
            "listen_drop": 0,
        },
    }


def evaluations_by_metric(result: RunSLOResult) -> dict:
    return {evaluation.metric: evaluation for evaluation in result.evaluations}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


class PercentileTests(unittest.TestCase):
    def test_boundaries(self):
        values = [float(v) for v in range(1, 101)]
        self.assertEqual(percentile(values, 0.0), 1.0)
        self.assertEqual(percentile(values, 0.5), 50.5)
        self.assertEqual(percentile(values, 1.0), 100.0)

    def test_p95_p99_interpolation(self):
        values = [float(v) for v in range(1, 101)]
        # index = 99 * 0.95 = 94.05 -> 95 + (96-95)*0.05
        self.assertAlmostEqual(percentile(values, 0.95), 95.05)
        self.assertAlmostEqual(percentile(values, 0.99), 99.01)

    def test_unsorted_input(self):
        self.assertEqual(percentile([30.0, 10.0, 20.0], 0.5), 20.0)

    def test_single_value(self):
        self.assertEqual(percentile([42.0], 0.95), 42.0)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            percentile([], 0.5)

    def test_invalid_q(self):
        for q in (-0.1, 1.1, True, "0.5"):
            with self.assertRaises(ValueError, msg=q):
                percentile([1.0, 2.0], q)

    def test_non_finite_rejected(self):
        with self.assertRaises(ValueError):
            percentile([1.0, float("nan")], 0.5)
        with self.assertRaises(ValueError):
            percentile([1.0, float("inf")], 0.5)
        with self.assertRaises(ValueError):
            percentile([1.0, True], 0.5)

    def test_caller_input_unchanged(self):
        values = [30.0, 10.0, 20.0]
        percentile(values, 0.5)
        self.assertEqual(values, [30.0, 10.0, 20.0])


class CounterRateTests(unittest.TestCase):
    def test_simple_rate(self):
        self.assertEqual(counter_rate([(0, 0.0), (10, 100.0)]), 10.0)

    def test_irregular_timestamps(self):
        self.assertAlmostEqual(counter_rate([(0, 0.0), (3, 30.0), (10, 110.0)]), 11.0)

    def test_constant_counter(self):
        self.assertEqual(counter_rate([(0, 5.0), (10, 5.0)]), 0.0)

    def test_reset_rejected(self):
        with self.assertRaises(ValueError):
            counter_rate([(0, 100.0), (10, 50.0)])

    def test_duplicate_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            counter_rate([(5, 0.0), (5, 1.0)])

    def test_non_monotonic_rejected(self):
        with self.assertRaises(ValueError):
            counter_rate([(10, 0.0), (5, 1.0)])

    def test_negative_and_non_finite_rejected(self):
        with self.assertRaises(ValueError):
            counter_rate([(0, -1.0), (10, 0.0)])
        with self.assertRaises(ValueError):
            counter_rate([(0, float("nan")), (10, 0.0)])
        with self.assertRaises(ValueError):
            counter_rate([(0, 0.0), (float("inf"), 1.0)])
        with self.assertRaises(ValueError):
            counter_rate([(0, True), (10, 2.0)])

    def test_one_sample_rejected(self):
        with self.assertRaises(ValueError):
            counter_rate([(0, 1.0)])


class SustainedDurationTests(unittest.TestCase):
    def test_spec_example(self):
        data = [(0, False), (5, True), (10, True), (15, True), (20, False)]
        self.assertEqual(
            longest_sustained_duration(data, lambda value: value is True), 15.0
        )

    def test_final_sample_true_uses_its_timestamp(self):
        data = [(0, 1.0), (5, 2.0), (10, 3.0)]
        self.assertEqual(
            longest_sustained_duration(data, lambda value: value > 0.5), 10.0
        )

    def test_longest_run_wins(self):
        data = [(0, 1.0), (5, 0.0), (10, 1.0), (20, 1.0), (25, 0.0)]
        self.assertEqual(
            longest_sustained_duration(data, lambda value: value > 0.5), 15.0
        )

    def test_no_true_samples(self):
        self.assertEqual(
            longest_sustained_duration([(0, 0.0), (5, 0.0)], lambda v: v > 1.0),
            0.0,
        )

    def test_irregular_spacing_uses_actual_timestamps(self):
        data = [(0, 1.0), (7, 1.0), (30, 0.0)]
        self.assertEqual(
            longest_sustained_duration(data, lambda value: value > 0.5), 30.0
        )

    def test_non_increasing_timestamps_rejected(self):
        with self.assertRaises(ValueError):
            longest_sustained_duration([(5, 1.0), (5, 1.0)], lambda v: True)
        with self.assertRaises(ValueError):
            longest_sustained_duration([(10, 1.0), (5, 1.0)], lambda v: True)

    def test_malformed_samples_rejected(self):
        with self.assertRaises(ValueError):
            longest_sustained_duration([(0, float("nan"))], lambda v: True)
        with self.assertRaises(ValueError):
            longest_sustained_duration([(float("inf"), 1.0)], lambda v: True)


# ---------------------------------------------------------------------------
# Warning-zone logic (via evaluate_run_slos on single metrics)
# ---------------------------------------------------------------------------


class WarningZoneTests(unittest.TestCase):
    def _verdict_for_cpu_median(self, value):
        bundle = valid_bundle()
        bundle["cpu_percent"]["samples"] = samples(value, value, value)
        result = evaluate_run_slos(bundle, thresholds())
        return evaluations_by_metric(result)["cpu.median_percent"].verdict

    def test_below_90_percent_passes(self):
        # 90% of 75 = 67.5
        self.assertIs(self._verdict_for_cpu_median(67.4), MetricVerdict.PASS)

    def test_exactly_90_percent_is_warning(self):
        self.assertIs(
            self._verdict_for_cpu_median(67.5), MetricVerdict.PASS_WITH_WARNING
        )

    def test_between_warning_and_hard_is_warning(self):
        self.assertIs(
            self._verdict_for_cpu_median(70.0), MetricVerdict.PASS_WITH_WARNING
        )

    def test_exactly_hard_limit_is_warning_not_fail(self):
        self.assertIs(
            self._verdict_for_cpu_median(75.0), MetricVerdict.PASS_WITH_WARNING
        )

    def test_above_hard_limit_fails(self):
        self.assertIs(self._verdict_for_cpu_median(75.1), MetricVerdict.FAIL)

    def test_warning_threshold_recorded(self):
        bundle = valid_bundle()
        result = evaluate_run_slos(bundle, thresholds())
        evaluation = evaluations_by_metric(result)["cpu.median_percent"]
        self.assertEqual(evaluation.threshold, 75)
        self.assertEqual(evaluation.warning_threshold, 67.5)

    def test_zero_tolerance_has_no_warning(self):
        bundle = valid_bundle()
        bundle["memory"]["swap_in"] = 1
        result = evaluate_run_slos(bundle, thresholds())
        evaluation = evaluations_by_metric(result)["memory.swap_in"]
        self.assertIs(evaluation.verdict, MetricVerdict.FAIL)
        self.assertIsNone(evaluation.warning_threshold)
        bundle = valid_bundle()
        result = evaluate_run_slos(bundle, thresholds())
        evaluation = evaluations_by_metric(result)["memory.swap_in"]
        self.assertIs(evaluation.verdict, MetricVerdict.PASS)

    def test_lower_bound_metrics_absent_from_approved_thresholds(self):
        # The sections evaluated by Task 7 define only upper bounds and
        # zero-tolerance metrics; there is no lower-bound warning rule to
        # apply, so no metric may silently invent one. (scaling_guardrails
        # lower bounds are Task 8 scope and intentionally not evaluated.)
        config = thresholds()
        for name in (
            "cpu",
            "memory",
            "tick_latency_ms",
            "packet_processing_ms",
            "sql_ms",
            "script_ms",
            "errors",
            "storage",
            "network",
        ):
            for key in config[name]:
                self.assertNotIn("_min", key)


# ---------------------------------------------------------------------------
# Metric evaluation across the full approved metric set
# ---------------------------------------------------------------------------


def _get(data, *path):
    node = data
    for key in path:
        node = node[key]
    return node


def _set(data, path, value):
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _delete(data, path):
    node = data
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]


class MetricSetTests(unittest.TestCase):
    def test_valid_bundle_all_metrics_pass(self):
        result = evaluate_run_slos(valid_bundle(), thresholds())
        self.assertIs(result.status, MetricVerdict.PASS)
        expected_metrics = tuple(sorted(spec.metric for spec in _METRIC_SPECS))
        self.assertEqual(result.evaluated_metrics, expected_metrics)
        self.assertEqual(result.blocked_metrics, ())
        for evaluation in result.evaluations:
            self.assertIs(
                evaluation.verdict, MetricVerdict.PASS, evaluation.metric
            )
            self.assertEqual(evaluation.code, "SLO_PASS", evaluation.metric)

    def test_default_thresholds_from_committed_config(self):
        result = evaluate_run_slos(valid_bundle())
        self.assertIs(result.status, MetricVerdict.PASS)

    def test_every_metric_fail_case(self):
        config = thresholds()
        for spec in _METRIC_SPECS:
            with self.subTest(metric=spec.metric):
                bundle = valid_bundle()
                hard = _get(config, *spec.threshold_path)
                if spec.kind in ("percentile", "max"):
                    _set(bundle, spec.bundle_path, samples(hard * 2, hard * 2))
                elif spec.kind == "scalar":
                    _set(bundle, spec.bundle_path, hard * 2 if hard else 1)
                elif spec.kind == "zero":
                    _set(bundle, spec.bundle_path, 1)
                elif spec.kind == "sustained":
                    level = (
                        _get(config, *spec.sustained_above_path)
                        if spec.sustained_above_path
                        else 0
                    )
                    _set(
                        bundle,
                        spec.bundle_path,
                        [[0, level + 1], [40, level + 1], [45, level]],
                    )
                result = evaluate_run_slos(bundle, config)
                evaluation = evaluations_by_metric(result)[spec.metric]
                if spec.catastrophic_code:
                    self.assertIs(evaluation.verdict, MetricVerdict.BLOCKED)
                    self.assertTrue(evaluation.catastrophic)
                else:
                    self.assertIs(evaluation.verdict, MetricVerdict.FAIL)

    def test_every_metric_missing_blocked(self):
        for spec in _METRIC_SPECS:
            with self.subTest(metric=spec.metric):
                bundle = valid_bundle()
                _delete(bundle, spec.bundle_path)
                result = evaluate_run_slos(bundle, thresholds())
                evaluation = evaluations_by_metric(result)[spec.metric]
                self.assertIs(evaluation.verdict, MetricVerdict.BLOCKED)
                self.assertEqual(evaluation.code, "METRIC_MISSING")
                self.assertIsNone(evaluation.observed)

    def test_every_metric_malformed_blocked(self):
        for spec in _METRIC_SPECS:
            with self.subTest(metric=spec.metric):
                bundle = valid_bundle()
                _set(bundle, spec.bundle_path, "junk")
                result = evaluate_run_slos(bundle, thresholds())
                evaluation = evaluations_by_metric(result)[spec.metric]
                self.assertIs(evaluation.verdict, MetricVerdict.BLOCKED)
                self.assertEqual(evaluation.code, "METRIC_INVALID")

    def test_blocked_metrics_listed(self):
        bundle = valid_bundle()
        del bundle["cpu_percent"]
        result = evaluate_run_slos(bundle, thresholds())
        self.assertIn("cpu.median_percent", result.blocked_metrics)
        self.assertEqual(
            result.blocked_metrics, tuple(sorted(result.blocked_metrics))
        )
        self.assertIs(result.status, MetricVerdict.BLOCKED)


class SustainedThresholdTests(unittest.TestCase):
    def _cpu_sustained_verdict(self, duration):
        bundle = valid_bundle()
        bundle["cpu_percent"]["samples"] = [[0, 100], [duration, 50]]
        result = evaluate_run_slos(bundle, thresholds())
        return evaluations_by_metric(result)["cpu.sustained_above_95_seconds"]

    def test_cpu_25_30_35_seconds(self):
        self.assertIs(
            self._cpu_sustained_verdict(25).verdict, MetricVerdict.PASS
        )
        self.assertIs(
            self._cpu_sustained_verdict(30).verdict,
            MetricVerdict.PASS_WITH_WARNING,
        )
        self.assertIs(
            self._cpu_sustained_verdict(35).verdict, MetricVerdict.FAIL
        )
        self.assertEqual(self._cpu_sustained_verdict(35).observed, 35.0)

    def _tick_verdict(self, duration):
        bundle = valid_bundle()
        bundle["tick_latency_ms"]["samples"] = [[0, 60], [duration, 50]]
        result = evaluate_run_slos(bundle, thresholds())
        return evaluations_by_metric(result)["tick.sustained_above_50_seconds"]

    def test_tick_5_10_15_seconds(self):
        self.assertIs(self._tick_verdict(5).verdict, MetricVerdict.PASS)
        self.assertIs(
            self._tick_verdict(10).verdict, MetricVerdict.PASS_WITH_WARNING
        )
        self.assertIs(self._tick_verdict(15).verdict, MetricVerdict.FAIL)

    def _backlog_verdict(self, duration):
        bundle = valid_bundle()
        bundle["packet_processing_ms"]["backlog_growth_samples"] = [
            [0, 1],
            [duration, 0],
        ]
        result = evaluate_run_slos(bundle, thresholds())
        return evaluations_by_metric(result)["packet.backlog_growth_seconds"]

    def test_backlog_5_10_15_seconds(self):
        self.assertIs(self._backlog_verdict(5).verdict, MetricVerdict.PASS)
        self.assertIs(
            self._backlog_verdict(10).verdict, MetricVerdict.PASS_WITH_WARNING
        )
        self.assertIs(self._backlog_verdict(15).verdict, MetricVerdict.FAIL)

    def _queue_verdict(self, duration):
        bundle = valid_bundle()
        bundle["storage"]["queue_depth_growth_samples"] = [
            [0, 1],
            [duration, 0],
        ]
        result = evaluate_run_slos(bundle, thresholds())
        return evaluations_by_metric(result)["storage.queue_growth_seconds"]

    def test_queue_5_10_15_seconds(self):
        self.assertIs(self._queue_verdict(5).verdict, MetricVerdict.PASS)
        self.assertIs(
            self._queue_verdict(10).verdict, MetricVerdict.PASS_WITH_WARNING
        )
        self.assertIs(self._queue_verdict(15).verdict, MetricVerdict.FAIL)

    def test_irregular_spacing(self):
        bundle = valid_bundle()
        bundle["cpu_percent"]["samples"] = [[0, 100], [7, 100], [30, 50]]
        result = evaluate_run_slos(bundle, thresholds())
        evaluation = evaluations_by_metric(result)["cpu.sustained_above_95_seconds"]
        self.assertEqual(evaluation.observed, 30.0)
        self.assertIs(evaluation.verdict, MetricVerdict.PASS_WITH_WARNING)


# ---------------------------------------------------------------------------
# Catastrophic signals and classification
# ---------------------------------------------------------------------------


CATASTROPHIC_CODES = (
    "process_crash",
    "data_corruption",
    "deadlock",
    "oom",
    "allocation_failure",
    "database_inconsistency",
    "service_restart_during_run",
    "manifest_drift",
    "metric_pipeline_failure",
)


def passing_evaluation(metric="cpu.median_percent") -> MetricEvaluation:
    return MetricEvaluation(
        metric=metric,
        statistic="p50",
        observed=50.0,
        threshold=75.0,
        warning_threshold=67.5,
        verdict=MetricVerdict.PASS,
        code="SLO_PASS",
        message="within threshold",
        catastrophic=False,
        details={},
    )


class CatastrophicTests(unittest.TestCase):
    def test_each_signal_individually_blocks(self):
        for code in CATASTROPHIC_CODES:
            with self.subTest(code=code):
                signal = CatastrophicSignal(
                    code=code, message="boom", source="test", observed=1
                )
                self.assertIs(
                    classify_run([passing_evaluation()], [signal]),
                    MetricVerdict.BLOCKED,
                )

    def test_catastrophic_zero_tolerance_metrics(self):
        cases = (
            (("memory", "oom"), "memory.oom", "oom"),
            (("memory", "allocation_failure"), "memory.allocation_failure", "allocation_failure"),
            (("sql_ms", "deadlock"), "sql.deadlock", "deadlock"),
            (("errors", "process_crash"), "errors.process_crash", "process_crash"),
            (("errors", "data_corruption"), "errors.data_corruption", "data_corruption"),
        )
        for path, metric, code in cases:
            with self.subTest(metric=metric):
                bundle = valid_bundle()
                _set(bundle, path, 1)
                result = evaluate_run_slos(bundle, thresholds())
                evaluation = evaluations_by_metric(result)[metric]
                self.assertIs(evaluation.verdict, MetricVerdict.BLOCKED)
                self.assertTrue(evaluation.catastrophic)
                self.assertIs(result.status, MetricVerdict.BLOCKED)
                signals = [s.code for s in result.catastrophic_signals]
                self.assertIn(code, signals)

    def test_multiple_signals_sorted(self):
        bundle = valid_bundle()
        bundle["memory"]["oom"] = 1
        bundle["sql_ms"]["deadlock"] = 1
        result = evaluate_run_slos(bundle, thresholds())
        keys = [(s.code, s.source, s.message) for s in result.catastrophic_signals]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([s.code for s in result.catastrophic_signals], ["deadlock", "oom"])

    def test_non_catastrophic_zero_tolerance_fails_without_signal(self):
        bundle = valid_bundle()
        bundle["network"]["socket_error"] = 1
        result = evaluate_run_slos(bundle, thresholds())
        evaluation = evaluations_by_metric(result)["network.socket_error"]
        self.assertIs(evaluation.verdict, MetricVerdict.FAIL)
        self.assertFalse(evaluation.catastrophic)
        self.assertEqual(result.catastrophic_signals, ())
        self.assertIs(result.status, MetricVerdict.FAIL)

    def test_invalid_run_blocked_without_evaluation(self):
        invalid = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "invalid_run.json").read_text(encoding="utf-8")
        )
        validity = validate_run(invalid)
        self.assertFalse(validity.valid)
        result = evaluate_valid_run_slos(validity, valid_bundle(), thresholds())
        self.assertIs(result.status, MetricVerdict.BLOCKED)
        self.assertEqual(result.evaluations, ())
        self.assertEqual(result.evaluated_metrics, ())
        self.assertEqual(result.blocked_metrics, ("run_validity",))
        codes = [signal.code for signal in result.catastrophic_signals]
        self.assertEqual(codes, ["INVALID_RUN"])

    def test_valid_run_evaluates_normally(self):
        validity = validate_run(valid_run_data())
        self.assertTrue(validity.valid)
        result = evaluate_valid_run_slos(validity, valid_bundle(), thresholds())
        self.assertIs(result.status, MetricVerdict.PASS)


class ClassificationTests(unittest.TestCase):
    def _with_verdict(self, verdict):
        return dataclasses.replace(passing_evaluation(), verdict=verdict)

    def test_all_pass(self):
        self.assertIs(classify_run([passing_evaluation()]), MetricVerdict.PASS)

    def test_warning_only(self):
        self.assertIs(
            classify_run(
                [passing_evaluation(), self._with_verdict(MetricVerdict.PASS_WITH_WARNING)]
            ),
            MetricVerdict.PASS_WITH_WARNING,
        )

    def test_fail(self):
        self.assertIs(
            classify_run(
                [passing_evaluation(), self._with_verdict(MetricVerdict.FAIL)]
            ),
            MetricVerdict.FAIL,
        )

    def test_blocked_metric(self):
        self.assertIs(
            classify_run(
                [passing_evaluation(), self._with_verdict(MetricVerdict.BLOCKED)]
            ),
            MetricVerdict.BLOCKED,
        )

    def test_catastrophic_beats_fail(self):
        signal = CatastrophicSignal(code="oom", message="m", source="s", observed=1)
        self.assertIs(
            classify_run([self._with_verdict(MetricVerdict.FAIL)], [signal]),
            MetricVerdict.BLOCKED,
        )

    def test_empty_evaluations_blocked(self):
        self.assertIs(classify_run([]), MetricVerdict.BLOCKED)


# ---------------------------------------------------------------------------
# CSV fixture integration
# ---------------------------------------------------------------------------


class CsvFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with CSV_PATH.open(encoding="utf-8", newline="") as handle:
            cls.rows = list(csv.DictReader(handle))

    def _column(self, name):
        return [(float(r["timestamp"]), float(r[name])) for r in self.rows]

    def test_fixture_shape(self):
        self.assertEqual(len(self.rows), 60)
        self.assertEqual(
            set(self.rows[0]),
            {
                "timestamp",
                "cpu_percent",
                "tick_latency_ms",
                "packet_processing_ms",
                "storage_utilization_percent",
                "storage_await_ms",
                "network_utilization_percent",
            },
        )
        for row in self.rows:
            for value in row.values():
                self.assertTrue(math.isfinite(float(value)))

    def test_cpu_sustained_from_csv(self):
        duration = longest_sustained_duration(
            self._column("cpu_percent"), lambda value: value > 95
        )
        self.assertEqual(duration, 35.0)

    def test_cpu_percentiles_from_csv(self):
        values = [v for _, v in self._column("cpu_percent")]
        self.assertLess(percentile(values, 0.5), 75)
        self.assertGreater(percentile(values, 0.95), 85)

    def test_csv_bundle_cpu_verdicts(self):
        bundle = valid_bundle()
        bundle["cpu_percent"]["samples"] = [
            [t, v] for t, v in self._column("cpu_percent")
        ]
        result = evaluate_run_slos(bundle, thresholds())
        by_metric = evaluations_by_metric(result)
        self.assertEqual(
            by_metric["cpu.sustained_above_95_seconds"].observed, 35.0
        )
        self.assertIs(
            by_metric["cpu.sustained_above_95_seconds"].verdict, MetricVerdict.FAIL
        )
        self.assertIs(by_metric["cpu.median_percent"].verdict, MetricVerdict.PASS)
        self.assertIs(by_metric["cpu.p95_percent"].verdict, MetricVerdict.FAIL)


# ---------------------------------------------------------------------------
# Determinism and immutability
# ---------------------------------------------------------------------------


def serialize_result(result: RunSLOResult) -> str:
    payload = {
        "status": result.status.value,
        "evaluated_metrics": list(result.evaluated_metrics),
        "blocked_metrics": list(result.blocked_metrics),
        "catastrophic_signals": [
            dataclasses.asdict(signal) for signal in result.catastrophic_signals
        ],
        "evaluations": [
            {
                "metric": evaluation.metric,
                "statistic": evaluation.statistic,
                "observed": evaluation.observed,
                "threshold": evaluation.threshold,
                "warning_threshold": evaluation.warning_threshold,
                "verdict": evaluation.verdict.value,
                "code": evaluation.code,
                "message": evaluation.message,
                "catastrophic": evaluation.catastrophic,
                "details": dict(evaluation.details),
            }
            for evaluation in result.evaluations
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DeterminismTests(unittest.TestCase):
    def test_evaluations_sorted_by_metric_then_statistic(self):
        result = evaluate_run_slos(valid_bundle(), thresholds())
        keys = [(e.metric, e.statistic) for e in result.evaluations]
        self.assertEqual(keys, sorted(keys))

    def test_byte_equivalent_serialization(self):
        first = evaluate_run_slos(valid_bundle(), thresholds())
        second = evaluate_run_slos(valid_bundle(), thresholds())
        self.assertEqual(serialize_result(first), serialize_result(second))


class ImmutabilityTests(unittest.TestCase):
    def test_result_and_evaluations_frozen(self):
        result = evaluate_run_slos(valid_bundle(), thresholds())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.status = MetricVerdict.FAIL
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.evaluations[0].observed = 0.0

    def test_evaluation_details_immutable(self):
        details = {"samples": 3}
        evaluation = dataclasses.replace(passing_evaluation(), details=details)
        details["samples"] = 999
        self.assertEqual(evaluation.details["samples"], 3)
        with self.assertRaises(TypeError):
            evaluation.details["samples"] = 1

    def test_signal_frozen(self):
        signal = CatastrophicSignal(code="oom", message="m", source="s", observed=1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            signal.code = "x"


if __name__ == "__main__":
    unittest.main()
