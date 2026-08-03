"""Tests for A3 configuration loading, models, and JSON/SHA-256 helpers."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.performance.a3.config import load_config
from tools.performance.a3.io import read_json, sha256_file, write_json_atomic
from tools.performance.a3.models import (
    CapacityVerdict,
    LoadLevel,
    MetricVerdict,
    RunPhase,
    RunStatus,
)

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "a3.example.json"

EXPECTED_WORKLOAD = {
    "movement_direction_changes": 0.35,
    "idle_heartbeat": 0.20,
    "combat": 0.15,
    "npc_interaction": 0.10,
    "item_inventory": 0.08,
    "map_change_warp": 0.05,
    "chat": 0.04,
    "login_logout_character_select": 0.03,
}


def _load_example_dict() -> dict:
    return json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))


class ExampleConfigTests(unittest.TestCase):
    def test_example_config_has_exact_load_levels(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertEqual(config.load_levels, (500, 1000, 2500, 5000))

    def test_example_config_requires_three_valid_runs(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertEqual(config.valid_runs_per_level, 3)

    def test_example_config_uses_five_second_sampling(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertEqual(config.scrape_interval_seconds, 5)

    def test_example_config_uses_twenty_webgl_clients(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertEqual(config.webgl_clients, 20)

    def test_example_config_lifecycle_durations(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertEqual(config.preconditioning_seconds, 600)
        self.assertEqual(config.ramp_seconds, 300)
        self.assertEqual(config.steady_state_seconds, 1200)
        self.assertEqual(config.cooldown_seconds, 300)

    def test_example_config_guardrail_constants(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertEqual(config.workload_mix_tolerance_percentage_points, 5)
        self.assertEqual(config.prometheus_missing_data_limit_seconds, 15)
        self.assertAlmostEqual(config.target_concurrency_floor_ratio, 0.98)

    def test_workload_profile_matches_exact_proportions(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertEqual(dict(config.workload_mix), EXPECTED_WORKLOAD)

    def test_workload_profile_sums_to_one(self):
        config = load_config(EXAMPLE_CONFIG)
        self.assertAlmostEqual(sum(config.workload_mix.values()), 1.0, places=9)


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def _write_config(self, overrides=None, remove_keys=()) -> Path:
        data = _load_example_dict()
        if overrides:
            data.update(overrides)
        for key in remove_keys:
            data.pop(key, None)
        path = self.tmp_dir / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_missing_required_key_is_rejected_and_named(self):
        path = self._write_config(remove_keys=("scrape_interval_seconds",))
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("scrape_interval_seconds", str(ctx.exception))

    def test_unknown_key_is_rejected_and_named(self):
        path = self._write_config(overrides={"surprise_field": 1})
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("surprise_field", str(ctx.exception))

    def test_invalid_load_level_is_rejected_and_named(self):
        path = self._write_config(overrides={"load_levels": [500, 1000, 2000, 5000]})
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("load_levels", str(ctx.exception))

    def test_wrong_run_count_is_rejected_and_named(self):
        path = self._write_config(overrides={"valid_runs_per_level": 2})
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("valid_runs_per_level", str(ctx.exception))

    def test_wrong_webgl_client_count_is_rejected_and_named(self):
        path = self._write_config(overrides={"webgl_clients": 10})
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("webgl_clients", str(ctx.exception))

    def test_non_five_second_sampling_is_rejected_and_named(self):
        path = self._write_config(overrides={"scrape_interval_seconds": 10})
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("scrape_interval_seconds", str(ctx.exception))

    def test_workload_sum_outside_tolerance_is_rejected_and_named(self):
        bad_mix = copy.deepcopy(EXPECTED_WORKLOAD)
        bad_mix["combat"] = 0.25
        path = self._write_config(overrides={"workload_mix": bad_mix})
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("workload_mix", str(ctx.exception))

    def test_workload_missing_category_is_rejected_and_named(self):
        bad_mix = copy.deepcopy(EXPECTED_WORKLOAD)
        removed = bad_mix.pop("chat")
        bad_mix["combat"] += removed
        path = self._write_config(overrides={"workload_mix": bad_mix})
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("workload_mix", str(ctx.exception))


class IoHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def test_atomic_json_round_trip(self):
        path = self.tmp_dir / "nested" / "value.json"
        value = {"b": [1, 2, 3], "a": {"x": "y"}, "n": None}
        write_json_atomic(path, value)
        self.assertEqual(read_json(path), value)

    def test_atomic_write_deterministic_formatting(self):
        path = self.tmp_dir / "ordered.json"
        write_json_atomic(path, {"z": 1, "a": 2})
        first = path.read_text(encoding="utf-8")
        write_json_atomic(path, {"a": 2, "z": 1})
        self.assertEqual(first, path.read_text(encoding="utf-8"))

    def test_atomic_write_leaves_no_temp_files_on_failure(self):
        path = self.tmp_dir / "boom.json"
        with self.assertRaises(TypeError):
            write_json_atomic(path, {"bad": object()})
        self.assertEqual(list(self.tmp_dir.iterdir()), [])

    def test_sha256_known_value(self):
        path = self.tmp_dir / "hello.bin"
        payload = b"a3-baseline" * 100000  # larger than 1 MiB to exercise chunking
        path.write_bytes(payload)
        self.assertEqual(
            sha256_file(path),
            hashlib.sha256(b"a3-baseline" * 100000).hexdigest(),
        )


class EnumStabilityTests(unittest.TestCase):
    def test_load_level_values(self):
        self.assertEqual(
            sorted(level.value for level in LoadLevel),
            [500, 1000, 2500, 5000],
        )

    def test_run_phase_values(self):
        self.assertEqual(
            {phase.value for phase in RunPhase},
            {
                "ENVIRONMENT_CHECK",
                "SERVICE_START",
                "PRECONDITIONING",
                "RAMP_UP",
                "STEADY_STATE",
                "COOL_DOWN",
                "VALIDATION",
                "REPORTING",
                "ABORTED",
                "ARTIFACT_CAPTURE",
                "ROOT_CAUSE_ANALYSIS",
            },
        )

    def test_run_status_values(self):
        self.assertEqual(
            {status.value for status in RunStatus},
            {"PENDING", "RUNNING", "VALID", "INVALID", "ABORTED"},
        )

    def test_metric_verdict_values(self):
        self.assertEqual(
            {verdict.value for verdict in MetricVerdict},
            {"PASS", "PASS_WITH_WARNING", "FAIL", "BLOCKED"},
        )

    def test_capacity_verdict_values(self):
        self.assertEqual(
            {verdict.value for verdict in CapacityVerdict},
            {"PASS", "PASS_WITH_WARNING", "FAIL", "BLOCKED", "NOT_ESTABLISHED"},
        )


if __name__ == "__main__":
    unittest.main()
