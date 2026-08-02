"""Tests for A3 run-validity gates and metric-integrity checks."""

import copy
import dataclasses
import json
import unittest
from pathlib import Path

from tools.performance.a3.prometheus import MetricSample, MetricSeries
from tools.performance.a3.validity import (
    MetricIntegrityIssue,
    MetricIntegrityResult,
    ValidityReason,
    ValidityResult,
    combine_validity_results,
    validate_metric_integrity,
    validate_run,
    validity_result_to_dict,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
VALID_RUN_PATH = FIXTURE_DIR / "valid_run.json"
INVALID_RUN_PATH = FIXTURE_DIR / "invalid_run.json"
RUN_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "run.schema.json"
)


def valid_run() -> dict:
    return json.loads(VALID_RUN_PATH.read_text(encoding="utf-8"))


def invalid_run() -> dict:
    return json.loads(INVALID_RUN_PATH.read_text(encoding="utf-8"))


def mutate(run: dict, *path, value) -> dict:
    run = copy.deepcopy(run)
    node = run
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return run


class _Delete:
    pass


_DELETE = _Delete()


def codes(result) -> list:
    return [reason.code for reason in result.reasons]


def issue_codes(result) -> list:
    return [issue.code for issue in result.issues]


class FixtureTests(unittest.TestCase):
    def test_valid_fixture_passes_all_gates(self):
        result = validate_run(valid_run())
        self.assertTrue(result.valid)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.run_id, "run-l500-n1")
        self.assertEqual(
            result.manifest_id, "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001"
        )

    def test_invalid_fixture_reports_many_independent_reasons(self):
        result = validate_run(invalid_run())
        self.assertFalse(result.valid)
        self.assertGreater(len(result.reasons), 5)
        for expected in (
            "TARGET_CONCURRENCY_BELOW_MINIMUM",
            "DISCONNECT_RATIO_EXCEEDED",
            "BACKGROUND_CPU_EXCEEDED",
            "PACKET_LOSS_EXCEEDED",
            "WORKLOAD_MIX_OUT_OF_TOLERANCE",
            "MANIFEST_ID_MISMATCH",
            "MANIFEST_DRIFT",
            "PHASE_DURATION_MISMATCH",
            "VALIDATION_NOT_COMPLETED",
            "COLLECTOR_MISSING",
            "COLLECTOR_START_ERROR",
            "WEBGL_CLIENT_COUNT_MISMATCH",
            "WEBGL_LAUNCH_FAILURE",
            "GENERATED_USER_COUNT_MISMATCH",
            "HARNESS_ERROR",
            "CLOCK_REGRESSION",
            "PROMETHEUS_GAP_EXCEEDED",
            "PROCESS_RESTART",
        ):
            self.assertIn(expected, codes(result))

    def test_reasons_sorted_deterministically(self):
        result = validate_run(invalid_run())
        keys = [(r.code, r.field, r.message) for r in result.reasons]
        self.assertEqual(keys, sorted(keys))

    def test_all_failures_use_error_severity(self):
        result = validate_run(invalid_run())
        self.assertTrue(all(r.severity == "error" for r in result.reasons))

    def test_non_dict_root_raises_value_error(self):
        with self.assertRaises(ValueError):
            validate_run(["not", "a", "dict"])

    def test_one_bad_field_does_not_stop_other_gates(self):
        run = mutate(valid_run(), "observed_concurrency_min", value="junk")
        result = validate_run(run)
        self.assertFalse(result.valid)
        # Only the concurrency gate should produce reasons.
        self.assertEqual(set(codes(result)), {"TARGET_CONCURRENCY_BELOW_MINIMUM"})


class RunIdentityTests(unittest.TestCase):
    def test_safe_identifiers_accepted(self):
        run = mutate(valid_run(), "run_id", value="a.b-c_d123")
        self.assertTrue(validate_run(run).valid)

    def _rejects(self, field, code, values):
        for bad in values:
            with self.subTest(**{field: bad}):
                run = mutate(valid_run(), field, value=bad)
                self.assertIn(code, codes(validate_run(run)))

    def test_invalid_run_id(self):
        self._rejects(
            "run_id", "INVALID_RUN_ID", ["", "a/b", "a\\b", "..", "a..b", "x\x00y", "café", "a" * 129, 5]
        )

    def test_invalid_manifest_id(self):
        self._rejects(
            "manifest_id", "INVALID_MANIFEST_ID", ["", "a/b", "..", "a" * 129]
        )

    def test_invalid_baseline_cycle_id(self):
        self._rejects(
            "baseline_cycle_id",
            "INVALID_BASELINE_CYCLE_ID",
            ["", "a/b", "..", "a" * 129],
        )

    def test_invalid_load_level(self):
        for bad in (250, 2000, "500", True, None):
            with self.subTest(load_level=bad):
                run = mutate(valid_run(), "load_level", value=bad)
                self.assertIn("INVALID_LOAD_LEVEL", codes(validate_run(run)))

    def test_all_load_levels_accepted(self):
        for level in (500, 1000, 2500, 5000):
            run = mutate(valid_run(), "load_level", value=level)
            self.assertNotIn("INVALID_LOAD_LEVEL", codes(validate_run(run)))

    def test_invalid_run_number(self):
        for bad in (0, 4, "1", None):
            with self.subTest(run_number=bad):
                run = mutate(valid_run(), "run_number", value=bad)
                self.assertIn("INVALID_RUN_NUMBER", codes(validate_run(run)))


class ConcurrencyTests(unittest.TestCase):
    def test_exactly_98_percent_passes(self):
        run = mutate(valid_run(), "observed_concurrency_min", value=490)
        self.assertTrue(validate_run(run).valid)

    def test_below_98_percent_fails(self):
        run = mutate(valid_run(), "observed_concurrency_min", value=489)
        self.assertIn(
            "TARGET_CONCURRENCY_BELOW_MINIMUM", codes(validate_run(run))
        )

    def test_malformed_target(self):
        run = mutate(valid_run(), "target_synthetic_users", value="500")
        self.assertIn(
            "TARGET_CONCURRENCY_BELOW_MINIMUM", codes(validate_run(run))
        )

    def test_zero_target(self):
        run = mutate(valid_run(), "target_synthetic_users", value=0)
        self.assertIn(
            "TARGET_CONCURRENCY_BELOW_MINIMUM", codes(validate_run(run))
        )

    def test_observed_above_target_allowed(self):
        run = mutate(valid_run(), "observed_concurrency_min", value=520)
        self.assertTrue(validate_run(run).valid)


class DisconnectTests(unittest.TestCase):
    def test_exactly_one_percent_passes(self):
        run = mutate(valid_run(), "unexpected_disconnects", value=5)
        self.assertTrue(validate_run(run).valid)

    def test_above_one_percent_fails(self):
        run = mutate(valid_run(), "unexpected_disconnects", value=6)
        self.assertIn("DISCONNECT_RATIO_EXCEEDED", codes(validate_run(run)))

    def test_zero_denominator_no_disconnects_passes(self):
        run = mutate(valid_run(), "connection_attempts", value=0)
        run = mutate(run, "unexpected_disconnects", value=0)
        self.assertNotIn("DISCONNECT_RATIO_EXCEEDED", codes(validate_run(run)))

    def test_zero_denominator_with_disconnects_fails(self):
        run = mutate(valid_run(), "connection_attempts", value=0)
        run = mutate(run, "unexpected_disconnects", value=1)
        self.assertIn("DISCONNECT_RATIO_EXCEEDED", codes(validate_run(run)))

    def test_malformed_counts(self):
        for bad in (-1, True, 2.5, "x"):
            with self.subTest(bad=bad):
                run = mutate(valid_run(), "unexpected_disconnects", value=bad)
                self.assertIn(
                    "INVALID_CONNECTION_ATTEMPTS", codes(validate_run(run))
                )


class ProcessStabilityTests(unittest.TestCase):
    def test_each_process_restart_detected(self):
        for name in ("login", "char", "map"):
            run = mutate(valid_run(), "processes", name, "restarts", value=1)
            result = validate_run(run)
            self.assertIn("PROCESS_RESTART", codes(result))
            fields = [r.field for r in result.reasons if r.code == "PROCESS_RESTART"]
            self.assertEqual(fields, [f"processes.{name}.restarts"])

    def test_crash_oom_missing(self):
        run = mutate(valid_run(), "processes", "map", "crashes", value=1)
        self.assertIn("PROCESS_CRASH", codes(validate_run(run)))
        run = mutate(valid_run(), "processes", "char", "oom", value=1)
        self.assertIn("PROCESS_OOM", codes(validate_run(run)))
        run = mutate(
            valid_run(), "processes", "login", "steady_state_missing", value=True
        )
        self.assertIn("PROCESS_MISSING_STEADY_STATE", codes(validate_run(run)))

    def test_missing_process_entry(self):
        run = mutate(valid_run(), "processes", "map", value=_DELETE)
        self.assertFalse(validate_run(run).valid)

    def test_harness_restart_and_error(self):
        run = mutate(valid_run(), "processes", "harness", "restarts", value=1)
        self.assertIn("HARNESS_RESTART", codes(validate_run(run)))
        run = mutate(valid_run(), "processes", "harness", "errors", value=2)
        self.assertIn("HARNESS_ERROR", codes(validate_run(run)))


class CpuNetworkTests(unittest.TestCase):
    def test_cpu_exactly_five_passes(self):
        run = mutate(valid_run(), "background_cpu_percent_max", value=5.0)
        self.assertTrue(validate_run(run).valid)

    def test_cpu_above_fails(self):
        run = mutate(valid_run(), "background_cpu_percent_max", value=5.1)
        self.assertIn("BACKGROUND_CPU_EXCEEDED", codes(validate_run(run)))

    def test_packet_loss_exactly_point_one_percent_passes(self):
        run = mutate(valid_run(), "packet_loss_ratio", value=0.001)
        self.assertTrue(validate_run(run).valid)

    def test_packet_loss_above_fails(self):
        run = mutate(valid_run(), "packet_loss_ratio", value=0.0011)
        self.assertIn("PACKET_LOSS_EXCEEDED", codes(validate_run(run)))

    def test_non_finite_values(self):
        for bad in (float("nan"), float("inf")):
            run = mutate(valid_run(), "background_cpu_percent_max", value=bad)
            self.assertIn("BACKGROUND_CPU_EXCEEDED", codes(validate_run(run)))
            run = mutate(valid_run(), "packet_loss_ratio", value=bad)
            self.assertIn("PACKET_LOSS_EXCEEDED", codes(validate_run(run)))

    def test_ratio_above_one_invalid(self):
        run = mutate(valid_run(), "packet_loss_ratio", value=1.5)
        self.assertIn("PACKET_LOSS_EXCEEDED", codes(validate_run(run)))

    def test_percent_above_100_invalid(self):
        run = mutate(valid_run(), "background_cpu_percent_max", value=120.0)
        self.assertIn("BACKGROUND_CPU_EXCEEDED", codes(validate_run(run)))


class PrometheusGapTests(unittest.TestCase):
    def test_gap_exactly_15_passes(self):
        run = mutate(
            valid_run(), "metric_integrity", "maximum_missing_gap_seconds", value=15
        )
        self.assertTrue(validate_run(run).valid)

    def test_gap_above_15_fails(self):
        run = mutate(
            valid_run(), "metric_integrity", "maximum_missing_gap_seconds", value=16
        )
        self.assertIn("PROMETHEUS_GAP_EXCEEDED", codes(validate_run(run)))


class WorkloadMixTests(unittest.TestCase):
    def test_within_tolerance_passes(self):
        mix = valid_run()["workload_mix"]
        mix["combat"] = 0.20  # exactly +5 points
        mix["chat"] = 0.01
        mix["movement_direction_changes"] = 0.33  # keep total 1.0
        run = mutate(valid_run(), "workload_mix", value=mix)
        self.assertTrue(validate_run(run).valid)

    def test_above_tolerance_fails(self):
        mix = valid_run()["workload_mix"]
        mix["combat"] = 0.2001
        mix["movement_direction_changes"] = 0.3499
        run = mutate(valid_run(), "workload_mix", value=mix)
        self.assertIn("WORKLOAD_MIX_OUT_OF_TOLERANCE", codes(validate_run(run)))

    def test_missing_category(self):
        mix = valid_run()["workload_mix"]
        del mix["chat"]
        run = mutate(valid_run(), "workload_mix", value=mix)
        self.assertIn("WORKLOAD_CATEGORY_MISSING", codes(validate_run(run)))

    def test_unexpected_category(self):
        mix = valid_run()["workload_mix"]
        mix["farming"] = 0.01
        run = mutate(valid_run(), "workload_mix", value=mix)
        self.assertIn("WORKLOAD_CATEGORY_UNEXPECTED", codes(validate_run(run)))

    def test_invalid_value(self):
        mix = valid_run()["workload_mix"]
        mix["chat"] = "often"
        run = mutate(valid_run(), "workload_mix", value=mix)
        self.assertIn("WORKLOAD_MIX_OUT_OF_TOLERANCE", codes(validate_run(run)))
        mix = valid_run()["workload_mix"]
        mix["chat"] = 1.5
        run = mutate(valid_run(), "workload_mix", value=mix)
        self.assertIn("WORKLOAD_MIX_OUT_OF_TOLERANCE", codes(validate_run(run)))

    def test_total_not_one(self):
        mix = valid_run()["workload_mix"]
        mix["chat"] = 0.05
        run = mutate(valid_run(), "workload_mix", value=mix)
        result = validate_run(run)
        self.assertIn("WORKLOAD_MIX_OUT_OF_TOLERANCE", codes(result))
        messages = [r.message for r in result.reasons if "total" in r.message]
        self.assertTrue(messages)


class ManifestIdentityTests(unittest.TestCase):
    def test_mismatch(self):
        run = mutate(valid_run(), "manifest_id", value="a3-20260802-0000000-ubuntu2404-8c16t-32g-002")
        self.assertIn("MANIFEST_ID_MISMATCH", codes(validate_run(run)))

    def test_drift_non_empty(self):
        run = mutate(
            valid_run(),
            "runtime_manifest_drift",
            value=["operating_system.kernel_version changed"],
        )
        result = validate_run(run)
        self.assertIn("MANIFEST_DRIFT", codes(result))

    def test_malformed_drift(self):
        run = mutate(valid_run(), "runtime_manifest_drift", value="not-a-list")
        self.assertIn("MANIFEST_DRIFT", codes(validate_run(run)))


class PhaseTests(unittest.TestCase):
    def test_each_duration_mismatch(self):
        cases = (
            ("preconditioning_seconds", 600),
            ("ramp_seconds", 300),
            ("steady_state_seconds", 1200),
            ("cooldown_seconds", 300),
        )
        for field, expected in cases:
            run = mutate(valid_run(), "phases", field, value=expected + 1)
            result = validate_run(run)
            self.assertIn("PHASE_DURATION_MISMATCH", codes(result), field)
            reasons = [
                r for r in result.reasons if r.code == "PHASE_DURATION_MISMATCH"
            ]
            self.assertEqual(reasons[0].field, f"phases.{field}")
            self.assertEqual(reasons[0].expected, expected)

    def test_validation_not_completed(self):
        run = mutate(valid_run(), "phases", "validation_completed", value=False)
        self.assertIn("VALIDATION_NOT_COMPLETED", codes(validate_run(run)))


class CollectorGateTests(unittest.TestCase):
    def test_each_missing_collector(self):
        for name in ("pidstat", "sar", "vmstat", "iostat"):
            run = mutate(valid_run(), "collectors", name, value=_DELETE)
            self.assertIn("COLLECTOR_MISSING", codes(validate_run(run)), name)

    def test_output_missing(self):
        run = mutate(
            valid_run(), "collectors", "sar", "stdout_present", value=False
        )
        self.assertIn("COLLECTOR_OUTPUT_MISSING", codes(validate_run(run)))

    def test_record_missing(self):
        run = mutate(
            valid_run(), "collectors", "sar", "record_present", value=False
        )
        self.assertIn("COLLECTOR_MISSING", codes(validate_run(run)))

    def test_start_and_stop_errors(self):
        run = mutate(
            valid_run(), "collectors", "vmstat", "start_error", value="boom"
        )
        self.assertIn("COLLECTOR_START_ERROR", codes(validate_run(run)))
        run = mutate(
            valid_run(), "collectors", "vmstat", "stop_error", value="stuck"
        )
        self.assertIn("COLLECTOR_STOP_ERROR", codes(validate_run(run)))

    def test_unexpected_collector(self):
        run = valid_run()
        run["collectors"]["extra"] = {
            "record_present": True,
            "stdout_present": True,
            "start_error": None,
            "stop_error": None,
        }
        self.assertIn("COLLECTOR_UNEXPECTED", codes(validate_run(run)))


class WebglTests(unittest.TestCase):
    def test_too_few_or_many(self):
        for bad in (19, 21):
            run = mutate(valid_run(), "webgl_clients_observed", value=bad)
            self.assertIn(
                "WEBGL_CLIENT_COUNT_MISMATCH", codes(validate_run(run))
            )

    def test_launch_failure(self):
        run = mutate(valid_run(), "webgl_launch_failures", value=1)
        self.assertIn("WEBGL_LAUNCH_FAILURE", codes(validate_run(run)))

    def test_disconnect_threshold_edge(self):
        # 20 clients: 0 disconnects passes; ratio > 1% (1/20 = 5%) fails.
        run = mutate(valid_run(), "webgl_unexpected_disconnects", value=1)
        self.assertIn(
            "WEBGL_DISCONNECT_RATIO_EXCEEDED", codes(validate_run(run))
        )
        # Exactly 1%: 1 disconnect out of 100 observed (edge requires
        # adjusting observed; use expected=20 but observed edge zero-case
        # below instead).
        self.assertTrue(validate_run(valid_run()).valid)

    def test_zero_observed_clients(self):
        run = mutate(valid_run(), "webgl_clients_observed", value=0)
        result = validate_run(run)
        self.assertIn("WEBGL_CLIENT_COUNT_MISMATCH", codes(result))


class LoadGeneratorTests(unittest.TestCase):
    def test_generated_count_mismatch(self):
        run = mutate(
            valid_run(), "load_generator", "generated_user_count", value=499
        )
        self.assertIn(
            "GENERATED_USER_COUNT_MISMATCH", codes(validate_run(run))
        )

    def test_workload_event_total_invalid(self):
        for bad in (0, -1):
            run = mutate(
                valid_run(), "load_generator", "workload_event_total", value=bad
            )
            self.assertIn(
                "WORKLOAD_EVENT_TOTAL_INVALID", codes(validate_run(run))
            )

    def test_harness_error_restart(self):
        run = mutate(valid_run(), "load_generator", "errors", value=1)
        self.assertIn("HARNESS_ERROR", codes(validate_run(run)))
        run = mutate(valid_run(), "load_generator", "restarts", value=1)
        self.assertIn("HARNESS_RESTART", codes(validate_run(run)))


class TimingTests(unittest.TestCase):
    def test_monotonic_required(self):
        run = mutate(
            valid_run(), "timing", "uses_monotonic_durations", value=False
        )
        self.assertIn("MONOTONIC_TIMING_REQUIRED", codes(validate_run(run)))

    def test_clock_regression(self):
        run = mutate(valid_run(), "timing", "clock_regression_count", value=2)
        self.assertIn("CLOCK_REGRESSION", codes(validate_run(run)))

    def test_time_sync_unhealthy(self):
        run = mutate(valid_run(), "timing", "time_sync_healthy", value=False)
        self.assertIn("TIME_SYNC_UNHEALTHY", codes(validate_run(run)))


# ---------------------------------------------------------------------------
# Metric integrity
# ---------------------------------------------------------------------------


def make_series(samples, labels=None, name="up"):
    merged = {"__name__": name}
    if labels:
        merged.update(labels)
    return MetricSeries(
        labels=merged,
        samples=tuple(MetricSample(float(t), float(v)) for t, v in samples),
    )


def full_series(name="up", start=1000, points=9, step=5, skip=()):
    return make_series(
        [(start + i * step, 1) for i in range(points) if i not in skip],
        name=name,
    )


class MetricIntegrityTests(unittest.TestCase):
    def test_valid_complete_series(self):
        result = validate_metric_integrity((full_series(),), 1000, 1040)
        self.assertTrue(result.valid)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.checked_series, 1)
        self.assertEqual(result.expected_step_seconds, 5)
        self.assertEqual(result.expected_start, 1000)
        self.assertEqual(result.expected_end, 1040)

    def test_invalid_step(self):
        result = validate_metric_integrity((full_series(),), 1000, 1040, step=10)
        self.assertIn("INVALID_STEP", issue_codes(result))
        self.assertFalse(result.valid)

    def test_invalid_time_range(self):
        result = validate_metric_integrity((full_series(),), 1040, 1000)
        self.assertIn("INVALID_TIME_RANGE", issue_codes(result))
        result = validate_metric_integrity((full_series(),), "a", 1000)
        self.assertIn("INVALID_TIME_RANGE", issue_codes(result))

    def test_empty_series_set(self):
        result = validate_metric_integrity((), 1000, 1040)
        self.assertIn("EMPTY_SERIES_SET", issue_codes(result))
        self.assertFalse(result.valid)

    def test_duplicate_identity(self):
        series = full_series()
        result = validate_metric_integrity((series, series), 1000, 1040)
        self.assertIn("DUPLICATE_SERIES_IDENTITY", issue_codes(result))

    def test_empty_series(self):
        result = validate_metric_integrity(
            (make_series([], name="up"),), 1000, 1040
        )
        self.assertIn("EMPTY_SERIES", issue_codes(result))

    def test_non_monotonic_timestamp(self):
        series = make_series([(1000, 1), (1010, 1), (1005, 1)] + [(1015 + 5 * i, 1) for i in range(6)])
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertIn("NON_MONOTONIC_TIMESTAMP", issue_codes(result))

    def test_duplicate_timestamp(self):
        series = make_series([(1000, 1), (1005, 1), (1005, 2)] + [(1015 + 5 * i, 1) for i in range(6)])
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertIn("DUPLICATE_TIMESTAMP", issue_codes(result))

    def test_non_finite_values(self):
        series = make_series(
            [(1000 + 5 * i, 1) for i in range(8)] + [(1040, float("nan"))]
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertIn("NON_FINITE_VALUE", issue_codes(result))
        series = make_series(
            [(float("inf"), 1)] + [(1005 + 5 * i, 1) for i in range(8)]
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertIn("NON_FINITE_TIMESTAMP", issue_codes(result))

    def test_missing_gaps_up_to_15_seconds_valid(self):
        for skip_count in (1, 2, 3):
            series = full_series(skip=tuple(range(2, 2 + skip_count)))
            result = validate_metric_integrity((series,), 1000, 1040)
            self.assertTrue(result.valid, skip_count)
            self.assertNotIn("MISSING_GAP_EXCEEDED", issue_codes(result))
            self.assertEqual(
                issue_codes(result).count("MISSING_SAMPLE"), skip_count
            )

    def test_missing_gap_20_seconds_invalid(self):
        series = full_series(skip=(2, 3, 4, 5))
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_GAP_EXCEEDED", issue_codes(result))
        issue = [
            i for i in result.issues if i.code == "MISSING_GAP_EXCEEDED"
        ][0]
        self.assertEqual(issue.details["gap_seconds"], 20)

    def test_samples_outside_range_ignored(self):
        series = make_series(
            [(500, 1)] + [(1000 + 5 * i, 1) for i in range(9)] + [(2000, 1)]
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertTrue(result.valid)

    def test_counter_reset_on_total_suffix(self):
        series = make_series(
            [(1000 + 5 * i, v) for i, v in enumerate([10, 20, 30, 5, 15, 20, 25, 30, 35])],
            name="queries_total",
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertFalse(result.valid)
        resets = [i for i in result.issues if i.code == "COUNTER_RESET"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0].details["previous_value"], 30.0)
        self.assertEqual(resets[0].details["value"], 5.0)
        self.assertEqual(resets[0].details["previous_timestamp"], 1010.0)
        self.assertEqual(resets[0].details["timestamp"], 1015.0)

    def test_counter_reset_via_metric_semantics(self):
        series = make_series(
            [(1000 + 5 * i, v) for i, v in enumerate([10, 20, 5, 15, 20, 25, 30, 35, 40])],
            labels={"metric_semantics": "counter"},
            name="custom_metric",
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertIn("COUNTER_RESET", issue_codes(result))

    def test_gauge_decrease_ignored(self):
        series = make_series(
            [(1000 + 5 * i, v) for i, v in enumerate([30, 20, 10, 15, 20, 25, 30, 35, 40])],
            name="temperature_celsius",
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertTrue(result.valid)

    def test_count_suffix_reset(self):
        series = make_series(
            [(1000 + 5 * i, v) for i, v in enumerate([10, 5, 10, 15, 20, 25, 30, 35, 40])],
            name="events_count",
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertIn("COUNTER_RESET", issue_codes(result))

    def test_secret_in_identity(self):
        series = make_series(
            [(1000 + 5 * i, 1) for i in range(9)],
            labels={"db_password_source": "vault"},
            name="up",
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertFalse(result.valid)
        secret_issues = [
            i for i in result.issues if i.code == "SECRET_IN_SERIES_IDENTITY"
        ]
        self.assertTrue(secret_issues)
        for issue in result.issues:
            self.assertNotIn("vault", issue.message)
            self.assertNotIn("vault", json.dumps(dict(issue.details)))
        self.assertEqual(secret_issues[0].identity, "<redacted>")

    def test_secret_in_label(self):
        series = make_series(
            [(1000 + 5 * i, 1) for i in range(9)],
            labels={"note": "the authorization header"},
            name="up",
        )
        result = validate_metric_integrity((series,), 1000, 1040)
        self.assertIn("SECRET_IN_LABEL", issue_codes(result))
        self.assertFalse(result.valid)

    def test_issues_sorted_deterministically(self):
        bad_a = make_series([], name="aaa")
        bad_b = make_series(
            [(1000, float("nan"))] + [(1005 + 5 * i, 1) for i in range(8)],
            name="bbb",
        )
        result = validate_metric_integrity((bad_b, bad_a), 1000, 1040)
        keys = [
            (i.code, i.identity, i.timestamp if i.timestamp is not None else -1, i.message)
            for i in result.issues
        ]
        self.assertEqual(keys, sorted(keys))


class CombinationTests(unittest.TestCase):
    def _metric_result(self, valid=True):
        if valid:
            return validate_metric_integrity((full_series(),), 1000, 1040)
        return validate_metric_integrity((), 1000, 1040)

    def test_both_valid(self):
        combined = combine_validity_results(
            validate_run(valid_run()), self._metric_result(True)
        )
        self.assertTrue(combined.valid)
        self.assertIn("metric_integrity", combined.checked_gates)

    def test_run_invalid_only(self):
        combined = combine_validity_results(
            validate_run(invalid_run()), self._metric_result(True)
        )
        self.assertFalse(combined.valid)

    def test_metric_invalid_only(self):
        combined = combine_validity_results(
            validate_run(valid_run()), self._metric_result(False)
        )
        self.assertFalse(combined.valid)
        self.assertIn("EMPTY_SERIES_SET", codes(combined))

    def test_both_invalid(self):
        combined = combine_validity_results(
            validate_run(invalid_run()), self._metric_result(False)
        )
        self.assertFalse(combined.valid)

    def test_deterministic_sorting_and_identity_preserved(self):
        combined = combine_validity_results(
            validate_run(invalid_run()), self._metric_result(False)
        )
        keys = [(r.code, r.field, r.message) for r in combined.reasons]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(combined.run_id, "run-l500-n2")
        self.assertEqual(
            combined.manifest_id, "a3-20260802-0000000-ubuntu2404-8c16t-32g-999"
        )

    def test_deduplication(self):
        metric_result = self._metric_result(False)
        issue = metric_result.issues[0]
        duplicate = ValidityReason(
            code=issue.code,
            field=issue.identity,
            message=issue.message,
            observed=dict(issue.details),
            expected=None,
            severity="error",
        )
        run_result = validate_run(valid_run())
        run_result = dataclasses.replace(
            run_result, reasons=run_result.reasons + (duplicate,)
        )
        combined = combine_validity_results(run_result, metric_result)
        matching = [
            r
            for r in combined.reasons
            if r.code == duplicate.code and r.message == duplicate.message
        ]
        self.assertEqual(len(matching), 1)


class ImmutabilityTests(unittest.TestCase):
    def test_result_frozen(self):
        result = validate_run(valid_run())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.valid = False

    def test_reasons_tuple(self):
        result = validate_run(invalid_run())
        self.assertIsInstance(result.reasons, tuple)

    def test_issue_details_immutable(self):
        series = full_series(skip=(2, 3, 4, 5))
        result = validate_metric_integrity((series,), 1000, 1040)
        issue = [i for i in result.issues if i.code == "MISSING_GAP_EXCEEDED"][0]
        with self.assertRaises(TypeError):
            issue.details["gap_seconds"] = 1

    def test_caller_mutation_does_not_alter_details(self):
        details = {"gap_seconds": 20}
        issue = MetricIntegrityIssue(
            code="MISSING_GAP_EXCEEDED",
            identity="up{}",
            timestamp=None,
            message="gap",
            details=details,
        )
        details["gap_seconds"] = 999
        self.assertEqual(issue.details["gap_seconds"], 20)


class SchemaContractTests(unittest.TestCase):
    """run.schema.json validity section must match emitted ValidityResult."""

    def _walk(self, value, node, path="$"):
        if isinstance(value, dict) and isinstance(node, dict) and "properties" in node:
            required = set(node.get("required", []))
            missing = sorted(required - set(value))
            self.assertEqual(missing, [], f"{path}: missing {missing}")
            if node.get("additionalProperties") is False:
                extra = sorted(set(value) - set(node["properties"]))
                self.assertEqual(extra, [], f"{path}: unexpected {extra}")
            for key, item in value.items():
                if key in node["properties"]:
                    self._walk(item, node["properties"][key], f"{path}.{key}")
        elif isinstance(value, list) and isinstance(node, dict) and "items" in node:
            for index, item in enumerate(value):
                self._walk(item, node["items"], f"{path}[{index}]")

    def test_validity_result_matches_run_schema_validity_section(self):
        schema = json.loads(RUN_SCHEMA_PATH.read_text(encoding="utf-8"))
        node = schema["properties"]["validity"]
        combined = combine_validity_results(
            validate_run(invalid_run()),
            validate_metric_integrity((full_series(),), 1000, 1040),
        )
        artifact = validity_result_to_dict(combined)
        self._walk(artifact, node, "$.validity")


if __name__ == "__main__":
    unittest.main()
