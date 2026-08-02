"""Tests for A3 scaling, three-run aggregation, regression, and capacity."""

import dataclasses
import json
import math
import unittest
from pathlib import Path

from tools.performance.a3.models import CapacityVerdict, MetricVerdict
from tools.performance.a3.scaling import (
    CapacityResult,
    LevelAggregation,
    RegressionResult,
    RunSummary,
    ScalingResult,
    aggregate_level,
    derive_capacity,
    evaluate_regression,
    evaluate_scaling,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
PREVIOUS_BASELINE_PATH = FIXTURE_DIR / "previous_baseline.json"

MANIFEST = "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001"

PASS = MetricVerdict.PASS
WARNING = MetricVerdict.PASS_WITH_WARNING
FAIL = MetricVerdict.FAIL
BLOCKED = MetricVerdict.BLOCKED


def base_metrics(level: int) -> dict:
    return {
        "cpu_p95_percent": 60.0,
        "memory_per_user_bytes": 1_000_000.0,
        "latency_p95_ms": 10.0,
        "latency_p99_ms": 20.0,
        "throughput_per_second": float(level),
        "error_rate": 0.001,
    }


def make_run(
    run_number: int,
    level: int = 500,
    verdict: MetricVerdict = PASS,
    metrics: dict = None,
    valid: bool = True,
    catastrophic: bool = False,
    run_id: str = None,
    manifest_id: str = MANIFEST,
) -> RunSummary:
    return RunSummary(
        run_id=run_id or f"run-l{level}-n{run_number}",
        manifest_id=manifest_id,
        load_level=level,
        run_number=run_number,
        valid=valid,
        verdict=verdict,
        metrics=metrics if metrics is not None else base_metrics(level),
        catastrophic=catastrophic,
    )


def make_level(level: int, verdicts=(PASS, PASS, PASS), **run_overrides) -> LevelAggregation:
    return aggregate_level(
        [make_run(i + 1, level, verdicts[i], **run_overrides) for i in range(3)]
    )


def level_agg(level: int, verdict: MetricVerdict = PASS, medians: dict = None) -> LevelAggregation:
    return LevelAggregation(
        load_level=level,
        manifest_id=MANIFEST,
        valid_run_count=3,
        required_valid_run_count=3,
        run_ids=(f"run-l{level}-n1", f"run-l{level}-n2", f"run-l{level}-n3"),
        run_verdicts=(verdict, verdict, verdict),
        verdict=verdict,
        median_metrics=medians if medians is not None else base_metrics(level),
        worst_metrics={},
        stability_metrics={},
        warnings=(),
        failures=(),
    )


def scaling_levels(medians_by_level: dict) -> list:
    return [level_agg(level, PASS, medians) for level, medians in medians_by_level.items()]


ALL_PASS_MEDIANS = {
    500: {"cpu_p95_percent": 60.0, "memory_per_user_bytes": 1_000_000.0, "latency_p95_ms": 10.0, "latency_p99_ms": 20.0, "throughput_per_second": 500.0, "error_rate": 0.001},
    1000: {"cpu_p95_percent": 62.0, "memory_per_user_bytes": 1_100_000.0, "latency_p95_ms": 14.0, "latency_p99_ms": 30.0, "throughput_per_second": 900.0, "error_rate": 0.0015},
    2500: {"cpu_p95_percent": 66.0, "memory_per_user_bytes": 1_150_000.0, "latency_p95_ms": 20.0, "latency_p99_ms": 45.0, "throughput_per_second": 2100.0, "error_rate": 0.0025},
    5000: {"cpu_p95_percent": 70.0, "memory_per_user_bytes": 1_200_000.0, "latency_p95_ms": 28.0, "latency_p99_ms": 70.0, "throughput_per_second": 4000.0, "error_rate": 0.004},
}


class AggregationTests(unittest.TestCase):
    def test_three_pass(self):
        result = make_level(500)
        self.assertIs(result.verdict, PASS)
        self.assertEqual(result.valid_run_count, 3)
        self.assertEqual(result.required_valid_run_count, 3)
        self.assertEqual(result.failures, ())

    def test_one_warning(self):
        self.assertIs(make_level(500, (PASS, PASS, WARNING)).verdict, WARNING)

    def test_one_fail(self):
        result = make_level(500, (PASS, PASS, FAIL))
        self.assertIs(result.verdict, FAIL)

    def test_one_blocked(self):
        self.assertIs(make_level(500, (PASS, PASS, BLOCKED)).verdict, BLOCKED)

    def test_catastrophic_run_blocks(self):
        runs = [make_run(1), make_run(2), make_run(3, catastrophic=True)]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("catastrophic" in f for f in result.failures))

    def test_invalid_runs_ignored(self):
        runs = [make_run(1), make_run(2), make_run(3), make_run(4, valid=False, run_id="run-x")]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, PASS)
        self.assertEqual(result.valid_run_count, 3)

    def test_fewer_than_three_valid_blocked(self):
        runs = [make_run(1), make_run(2), make_run(3, valid=False)]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("insufficient valid runs" in f for f in result.failures))

    def test_more_than_three_valid_rejected(self):
        runs = [make_run(i + 1, run_id=f"run-{i}") for i in range(3)]
        runs.append(make_run(3, run_id="run-replacement-candidate"))
        with self.assertRaises(ValueError):
            aggregate_level(runs)

    def test_duplicate_run_id_blocked(self):
        runs = [make_run(1), make_run(2), make_run(3, run_id="run-l500-n1")]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("duplicate run_id" in f for f in result.failures))

    def test_duplicate_run_number_blocked(self):
        runs = [make_run(1), make_run(2), make_run(2, run_id="run-other")]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("run_number" in f for f in result.failures))

    def test_missing_run_number_blocked(self):
        runs = [make_run(1), make_run(2), make_run(4)]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("run_number" in f for f in result.failures))

    def test_manifest_mismatch_blocked(self):
        runs = [make_run(1), make_run(2), make_run(3, manifest_id="a3-20260802-0000000-ubuntu2404-8c16t-32g-999")]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("manifest" in f for f in result.failures))

    def test_load_level_mismatch_blocked(self):
        runs = [make_run(1), make_run(2), make_run(3, level=1000)]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("load_level" in f for f in result.failures))

    def test_malformed_metrics_blocked(self):
        bad = base_metrics(500)
        bad["error_rate"] = 1.5
        runs = [make_run(1), make_run(2), make_run(3, metrics=bad)]
        result = aggregate_level(runs)
        self.assertIs(result.verdict, BLOCKED)
        self.assertTrue(any("malformed" in f for f in result.failures))

    def test_nan_metrics_blocked(self):
        bad = base_metrics(500)
        bad["cpu_p95_percent"] = float("nan")
        runs = [make_run(1), make_run(2), make_run(3, metrics=bad)]
        self.assertIs(aggregate_level(runs).verdict, BLOCKED)

    def test_bool_metric_blocked(self):
        bad = base_metrics(500)
        bad["throughput_per_second"] = True
        runs = [make_run(1), make_run(2), make_run(3, metrics=bad)]
        self.assertIs(aggregate_level(runs).verdict, BLOCKED)

    def test_median_worst_stability(self):
        def metrics_with(latency, cpu, throughput):
            m = base_metrics(500)
            m["latency_p95_ms"] = latency
            m["cpu_p95_percent"] = cpu
            m["throughput_per_second"] = throughput
            return m

        runs = [
            make_run(1, metrics=metrics_with(9.0, 55.0, 600.0)),
            make_run(2, metrics=metrics_with(10.0, 60.0, 500.0)),
            make_run(3, metrics=metrics_with(11.0, 65.0, 550.0)),
        ]
        result = aggregate_level(runs)
        self.assertEqual(result.median_metrics["latency_p95_ms"], 10.0)
        self.assertEqual(result.worst_metrics["latency_p95_ms"], 11.0)
        self.assertEqual(result.worst_metrics["cpu_p95_percent"], 65.0)
        self.assertEqual(result.worst_metrics["throughput_per_second"], 500.0)
        stability = result.stability_metrics
        self.assertEqual(stability["latency_p95_ms.min"], 9.0)
        self.assertEqual(stability["latency_p95_ms.max"], 11.0)
        self.assertEqual(stability["latency_p95_ms.range"], 2.0)
        self.assertEqual(stability["latency_p95_ms.mean"], 10.0)
        self.assertAlmostEqual(
            stability["latency_p95_ms.stddev"], math.sqrt(2.0 / 3.0)
        )

    def test_runs_sorted_by_run_number(self):
        runs = [make_run(3), make_run(1), make_run(2)]
        result = aggregate_level(runs)
        self.assertEqual(
            result.run_ids, ("run-l500-n1", "run-l500-n2", "run-l500-n3")
        )
        self.assertEqual(result.run_verdicts, (PASS, PASS, PASS))

    def test_out_of_order_runs_same_result(self):
        forward = aggregate_level([make_run(1), make_run(2), make_run(3)])
        backward = aggregate_level([make_run(3), make_run(2), make_run(1)])
        self.assertEqual(forward, backward)


class ScalingTests(unittest.TestCase):
    def _evaluate(self, medians_by_level):
        return evaluate_scaling(scaling_levels(medians_by_level))

    def _with(self, level, metric, value):
        medians = {k: dict(v) for k, v in ALL_PASS_MEDIANS.items()}
        medians[level][metric] = value
        return medians

    def test_all_pass(self):
        result = self._evaluate(ALL_PASS_MEDIANS)
        self.assertTrue(result.passed)
        self.assertTrue(all(check.passed for check in result.checks))
        self.assertIsNone(result.first_degradation_level)

    def test_p95_exact_boundary(self):
        # 500 -> 1000: 10 * 1.5 = 15 exactly passes; 15.1 fails.
        self.assertTrue(self._evaluate(self._with(1000, "latency_p95_ms", 15.0)).passed)
        result = self._evaluate(self._with(1000, "latency_p95_ms", 15.1))
        self.assertFalse(result.passed)
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("P95_LATENCY_SCALING_EXCEEDED", codes)

    def test_p99_exact_boundary(self):
        self.assertTrue(self._evaluate(self._with(1000, "latency_p99_ms", 35.0)).passed)
        result = self._evaluate(self._with(1000, "latency_p99_ms", 35.1))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("P99_LATENCY_SCALING_EXCEEDED", codes)

    def test_memory_exact_boundary(self):
        self.assertTrue(self._evaluate(self._with(2500, "memory_per_user_bytes", 1_200_000.0)).passed)
        result = self._evaluate(self._with(2500, "memory_per_user_bytes", 1_210_000.0))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("MEMORY_PER_USER_SCALING_EXCEEDED", codes)

    def test_error_rate_exact_boundary(self):
        self.assertTrue(self._evaluate(self._with(1000, "error_rate", 0.002)).passed)
        result = self._evaluate(self._with(1000, "error_rate", 0.0021))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("ERROR_RATE_SCALING_EXCEEDED", codes)

    def test_throughput_exact_boundary(self):
        # 500 -> 1000: user growth 2.0, required throughput growth 1.6.
        self.assertTrue(self._evaluate(self._with(1000, "throughput_per_second", 800.0)).passed)
        result = self._evaluate(self._with(1000, "throughput_per_second", 799.0))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("THROUGHPUT_SCALING_BELOW_MINIMUM", codes)

    def test_missing_level(self):
        medians = {k: v for k, v in ALL_PASS_MEDIANS.items() if k != 1000}
        result = self._evaluate(medians)
        self.assertFalse(result.passed)
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("LEVEL_MISSING", codes)

    def test_blocked_level(self):
        levels = scaling_levels(ALL_PASS_MEDIANS)
        levels[1] = level_agg(1000, BLOCKED, ALL_PASS_MEDIANS[1000])
        result = evaluate_scaling(levels)
        self.assertFalse(result.passed)
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("LEVEL_BLOCKED", codes)

    def test_zero_denominator_deterministic_failure(self):
        medians = self._with(500, "error_rate", 0.0)
        result = self._evaluate(medians)
        self.assertFalse(result.passed)
        failed = [c for c in result.checks if not c.passed]
        self.assertTrue(failed)
        self.assertTrue(any(c.observed_ratio is None for c in failed))

    def test_invalid_metric(self):
        medians = self._with(1000, "latency_p95_ms", float("nan"))
        result = self._evaluate(medians)
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("INVALID_SCALING_METRIC", codes)

    def test_first_degradation_from_warning(self):
        levels = scaling_levels(ALL_PASS_MEDIANS)
        levels[2] = level_agg(2500, WARNING, ALL_PASS_MEDIANS[2500])
        result = evaluate_scaling(levels)
        self.assertEqual(result.first_degradation_level, 2500)

    def test_first_degradation_from_failed_check(self):
        result = self._evaluate(self._with(1000, "latency_p95_ms", 20.0))
        self.assertEqual(result.first_degradation_level, 1000)

    def test_checks_sorted_deterministically(self):
        result = self._evaluate(ALL_PASS_MEDIANS)
        keys = [(c.to_level, c.code, c.metric) for c in result.checks]
        self.assertEqual(keys, sorted(keys))

    def test_out_of_order_levels_same_result(self):
        levels = scaling_levels(ALL_PASS_MEDIANS)
        forward = evaluate_scaling(levels)
        backward = evaluate_scaling(list(reversed(levels)))
        self.assertEqual(forward, backward)


def _fixture_medians() -> dict:
    previous = json.loads(PREVIOUS_BASELINE_PATH.read_text(encoding="utf-8"))
    return {
        int(level): dict(data["median_metrics"])
        for level, data in previous["levels"].items()
    }


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = json.loads(PREVIOUS_BASELINE_PATH.read_text(encoding="utf-8"))
        cls.pass_medians = _fixture_medians()

    def _current(self, medians_by_level):
        return scaling_levels(medians_by_level)

    def _evaluate(self, medians_by_level=None, previous=None):
        if medians_by_level is None:
            medians_by_level = self.pass_medians
        return evaluate_regression(
            self._current(medians_by_level),
            self.previous if previous is None else previous,
        )

    def _with(self, level, metric, value):
        medians = {k: dict(v) for k, v in self.pass_medians.items()}
        medians[level][metric] = value
        return medians

    def test_all_budgets_pass(self):
        result = self._evaluate()
        self.assertTrue(result.passed)
        self.assertEqual(result.compared_levels, (500, 1000, 2500, 5000))

    def test_cpu_exact_boundary(self):
        # previous 500 cpu 60 -> 66 exactly 1.10 passes; 66.1 fails.
        self.assertTrue(self._evaluate(self._with(500, "cpu_p95_percent", 66.0)).passed)
        result = self._evaluate(self._with(500, "cpu_p95_percent", 66.1))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("CPU_REGRESSION_EXCEEDED", codes)

    def test_memory_exact_boundary(self):
        self.assertTrue(self._evaluate(self._with(500, "memory_per_user_bytes", 1_100_000.0)).passed)
        result = self._evaluate(self._with(500, "memory_per_user_bytes", 1_100_001.0))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("MEMORY_PER_USER_REGRESSION_EXCEEDED", codes)

    def test_p95_exact_boundary(self):
        self.assertTrue(self._evaluate(self._with(500, "latency_p95_ms", 11.5)).passed)
        result = self._evaluate(self._with(500, "latency_p95_ms", 11.6))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("P95_LATENCY_REGRESSION_EXCEEDED", codes)

    def test_p99_exact_boundary(self):
        self.assertTrue(self._evaluate(self._with(500, "latency_p99_ms", 24.0)).passed)
        result = self._evaluate(self._with(500, "latency_p99_ms", 24.1))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("P99_LATENCY_REGRESSION_EXCEEDED", codes)

    def test_throughput_exact_minus_ten_percent(self):
        self.assertTrue(self._evaluate(self._with(500, "throughput_per_second", 450.0)).passed)
        result = self._evaluate(self._with(500, "throughput_per_second", 449.0))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("THROUGHPUT_REGRESSION_EXCEEDED", codes)

    def test_error_rate_exact_boundary(self):
        self.assertTrue(self._evaluate(self._with(500, "error_rate", 0.00125)).passed)
        result = self._evaluate(self._with(500, "error_rate", 0.0013))
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("ERROR_RATE_REGRESSION_EXCEEDED", codes)

    def test_missing_previous_level(self):
        previous = json.loads(PREVIOUS_BASELINE_PATH.read_text(encoding="utf-8"))
        del previous["levels"]["5000"]
        result = self._evaluate(previous=previous)
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("PREVIOUS_LEVEL_MISSING", codes)
        self.assertFalse(result.passed)

    def test_missing_current_level(self):
        medians = {k: v for k, v in ALL_PASS_MEDIANS.items() if k != 5000}
        result = self._evaluate(medians)
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("CURRENT_LEVEL_MISSING", codes)
        self.assertFalse(result.passed)

    def test_malformed_previous_baseline(self):
        for bad in (
            {"version": 2, "approval_state": "APPROVED", "manifest_id": MANIFEST, "levels": {}},
            {"version": 1, "approval_state": "PENDING", "manifest_id": MANIFEST, "levels": {}},
            {"version": 1, "approval_state": "APPROVED", "manifest_id": "..", "levels": {}},
            {"version": 1, "approval_state": "APPROVED", "manifest_id": MANIFEST, "levels": {"999": {}}},
        ):
            result = self._evaluate(previous=bad)
            self.assertFalse(result.passed)
            codes = [c.code for c in result.checks]
            self.assertIn("MANIFEST_COMPARISON_INVALID", codes)

    def test_non_dict_previous_raises(self):
        with self.assertRaises(ValueError):
            evaluate_regression(self._current(self.pass_medians), ["not", "a", "dict"])

    def test_invalid_metric_in_previous(self):
        previous = json.loads(PREVIOUS_BASELINE_PATH.read_text(encoding="utf-8"))
        previous["levels"]["500"]["median_metrics"]["cpu_p95_percent"] = "junk"
        result = self._evaluate(previous=previous)
        codes = [c.code for c in result.checks if not c.passed]
        self.assertIn("INVALID_REGRESSION_METRIC", codes)

    def test_current_fail_keeps_regression_failed(self):
        levels = scaling_levels(self.pass_medians)
        levels[0] = level_agg(500, FAIL, self.pass_medians[500])
        result = evaluate_regression(levels, self.previous)
        self.assertFalse(result.passed)
        self.assertTrue(result.checks)

    def test_checks_sorted_deterministically(self):
        result = self._evaluate()
        keys = [(c.load_level, c.code, c.metric) for c in result.checks]
        self.assertEqual(keys, sorted(keys))


class CapacityTests(unittest.TestCase):
    def _derive(self, verdicts_by_level):
        return derive_capacity(
            [level_agg(level, verdict) for level, verdict in verdicts_by_level]
        )

    def test_all_pass(self):
        result = self._derive([(500, PASS), (1000, PASS), (2500, PASS), (5000, PASS)])
        self.assertEqual(result.safe_capacity, 5000)
        self.assertIsNone(result.conditional_capacity)
        self.assertEqual(result.tested_ceiling, 5000)
        self.assertIs(result.verdict, CapacityVerdict.PASS)
        self.assertIsNone(result.first_degradation_level)

    def test_pass_then_warning(self):
        result = self._derive([(500, PASS), (1000, PASS), (2500, PASS), (5000, WARNING)])
        self.assertEqual(result.safe_capacity, 2500)
        self.assertEqual(result.conditional_capacity, 5000)
        self.assertIs(result.verdict, CapacityVerdict.PASS_WITH_WARNING)
        self.assertEqual(result.first_degradation_level, 5000)

    def test_pass_then_fail(self):
        result = self._derive([(500, PASS), (1000, PASS), (2500, FAIL), (5000, FAIL)])
        self.assertEqual(result.safe_capacity, 1000)
        self.assertEqual(result.tested_ceiling, 5000)
        self.assertIs(result.verdict, CapacityVerdict.FAIL)
        self.assertEqual(result.first_degradation_level, 2500)

    def test_all_fail(self):
        result = self._derive([(500, FAIL), (1000, FAIL)])
        self.assertIsNone(result.safe_capacity)
        self.assertIs(result.verdict, CapacityVerdict.FAIL)

    def test_first_level_blocked(self):
        result = self._derive([(500, BLOCKED), (1000, PASS), (2500, PASS)])
        self.assertIs(result.verdict, CapacityVerdict.BLOCKED)
        self.assertIsNone(result.safe_capacity)
        self.assertEqual(result.tested_ceiling, 2500)
        self.assertEqual(result.first_degradation_level, 500)
        self.assertTrue(result.notes)

    def test_missing_500_level(self):
        result = self._derive([(1000, PASS), (2500, PASS)])
        self.assertIsNone(result.safe_capacity)
        self.assertEqual(result.tested_ceiling, 2500)
        self.assertTrue(
            any("500" in note for note in result.notes), result.notes
        )

    def test_non_monotonic_progression(self):
        result = self._derive([(500, FAIL), (1000, PASS), (2500, PASS)])
        self.assertIs(result.verdict, CapacityVerdict.PASS_WITH_WARNING)
        self.assertTrue(
            any("non-monotonic" in note for note in result.notes), result.notes
        )
        self.assertEqual(result.first_degradation_level, 500)

    def test_blocked_excluded_from_ceiling(self):
        result = self._derive([(500, PASS), (1000, PASS), (2500, FAIL), (5000, BLOCKED)])
        self.assertEqual(result.tested_ceiling, 2500)
        self.assertIs(result.verdict, CapacityVerdict.BLOCKED)
        self.assertEqual(result.safe_capacity, 1000)
        self.assertEqual(result.first_degradation_level, 2500)

    def test_no_levels_not_established(self):
        result = self._derive([])
        self.assertIs(result.verdict, CapacityVerdict.NOT_ESTABLISHED)
        self.assertIsNone(result.safe_capacity)
        self.assertIsNone(result.tested_ceiling)

    def test_out_of_order_levels_same_result(self):
        forward = self._derive([(500, PASS), (1000, WARNING)])
        backward = derive_capacity(
            [level_agg(1000, WARNING), level_agg(500, PASS)]
        )
        self.assertEqual(forward, backward)

    def test_missing_500_all_pass_not_established(self):
        result = self._derive([(1000, PASS), (2500, PASS), (5000, PASS)])
        self.assertIsNone(result.safe_capacity)
        self.assertIsNone(result.conditional_capacity)
        self.assertEqual(result.tested_ceiling, 5000)
        self.assertIs(result.verdict, CapacityVerdict.NOT_ESTABLISHED)
        self.assertTrue(any("500" in note for note in result.notes))

    def test_missing_500_warnings_not_established(self):
        result = self._derive([(1000, WARNING), (2500, WARNING)])
        self.assertIsNone(result.safe_capacity)
        self.assertIsNone(result.conditional_capacity)
        self.assertIs(result.verdict, CapacityVerdict.NOT_ESTABLISHED)

    def test_missing_500_with_fail(self):
        result = self._derive([(1000, PASS), (2500, FAIL)])
        self.assertIs(result.verdict, CapacityVerdict.FAIL)
        self.assertIsNone(result.safe_capacity)

    def test_missing_500_with_blocked(self):
        result = self._derive([(1000, BLOCKED), (2500, PASS)])
        self.assertIs(result.verdict, CapacityVerdict.BLOCKED)
        self.assertIsNone(result.safe_capacity)

    def test_pass_at_500_followed_by_higher_pass_unchanged(self):
        result = self._derive([(500, PASS), (1000, PASS)])
        self.assertIs(result.verdict, CapacityVerdict.PASS)
        self.assertEqual(result.safe_capacity, 1000)
        self.assertIsNone(result.first_degradation_level)


class InvalidRunIsolationTests(unittest.TestCase):
    """Invalid runs must never affect valid-run identity or metric checks."""

    def test_invalid_other_level_run_ignored(self):
        runs = [make_run(1), make_run(2), make_run(3)]
        runs.append(make_run(1, level=1000, valid=False, run_id="run-invalid"))
        result = aggregate_level(runs)
        self.assertIs(result.verdict, PASS)
        self.assertEqual(result.load_level, 500)
        self.assertEqual(result.valid_run_count, 3)

    def test_invalid_run_manifest_ignored(self):
        runs = [make_run(1), make_run(2), make_run(3)]
        runs.append(
            make_run(4, valid=False, run_id="run-invalid", manifest_id="a3-20260802-0000000-ubuntu2404-8c16t-32g-999")
        )
        result = aggregate_level(runs)
        self.assertIs(result.verdict, PASS)
        self.assertEqual(result.failures, ())

    def test_invalid_run_duplicate_identity_ignored(self):
        runs = [make_run(1), make_run(2), make_run(3)]
        # Same run_id and run_number as a valid run, but invalid.
        runs.append(make_run(1, valid=False))
        result = aggregate_level(runs)
        self.assertIs(result.verdict, PASS)
        self.assertEqual(result.failures, ())

    def test_invalid_run_malformed_metrics_ignored(self):
        bad = base_metrics(500)
        bad["error_rate"] = 1.5
        bad["cpu_p95_percent"] = float("nan")
        runs = [make_run(1), make_run(2), make_run(3)]
        runs.append(make_run(4, valid=False, run_id="run-invalid", metrics=bad))
        result = aggregate_level(runs)
        self.assertIs(result.verdict, PASS)
        self.assertEqual(result.failures, ())

    def test_invalid_catastrophic_and_blocked_runs_ignored(self):
        runs = [make_run(1), make_run(2), make_run(3)]
        runs.append(make_run(4, valid=False, run_id="run-cat", catastrophic=True))
        runs.append(make_run(5, valid=False, run_id="run-blocked", verdict=BLOCKED))
        result = aggregate_level(runs)
        self.assertIs(result.verdict, PASS)
        self.assertEqual(result.failures, ())


class ImmutabilityTests(unittest.TestCase):
    def test_run_summary_metrics_immutable(self):
        metrics = base_metrics(500)
        run = make_run(1, metrics=metrics)
        metrics["cpu_p95_percent"] = 999.0
        self.assertEqual(run.metrics["cpu_p95_percent"], 60.0)
        with self.assertRaises(TypeError):
            run.metrics["cpu_p95_percent"] = 1.0

    def test_level_aggregation_mappings_immutable(self):
        result = make_level(500)
        with self.assertRaises(TypeError):
            result.median_metrics["cpu_p95_percent"] = 1.0
        with self.assertRaises(TypeError):
            result.stability_metrics["x"] = 1.0

    def test_results_frozen(self):
        result = make_level(500)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.verdict = FAIL
        capacity = self._capacity()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            capacity.safe_capacity = 1

    def _capacity(self):
        return derive_capacity([level_agg(500, PASS)])


class DeterminismTests(unittest.TestCase):
    def test_no_wallclock_dependencies(self):
        first = evaluate_scaling(scaling_levels(ALL_PASS_MEDIANS))
        second = evaluate_scaling(scaling_levels(ALL_PASS_MEDIANS))
        self.assertEqual(first, second)
        capacity_first = derive_capacity([level_agg(500, PASS), level_agg(1000, WARNING)])
        capacity_second = derive_capacity([level_agg(500, PASS), level_agg(1000, WARNING)])
        self.assertEqual(capacity_first, capacity_second)


if __name__ == "__main__":
    unittest.main()
