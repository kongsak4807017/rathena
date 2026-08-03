"""Tests for the A3 orchestration CLI (prepare/control/run/evaluate/report/approve)."""

import dataclasses
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.performance.a3.approval import ApprovalState
from tools.performance.a3.cli import (
    CLIDependencies,
    CLIError,
    CycleState,
    CycleStateStore,
    build_parser,
    dispatch,
    main,
)
from tools.performance.a3.models import CapacityVerdict, MetricVerdict
from tools.performance.a3.scaling import (
    CapacityResult,
    LevelAggregation,
    RegressionResult,
    ScalingResult,
)
from tools.performance.a3.validity import ValidityResult

CYCLE = "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001"
MANIFEST_ID = CYCLE
UTC = "2026-08-02T21:00:00Z"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSAL = 3
EXIT_OPERATIONAL = 4
EXIT_CATASTROPHIC = 5
EXIT_INTERNAL = 10


def manifest() -> dict:
    return {
        "manifest_id": MANIFEST_ID,
        "manifest_sha256": "a" * 64,
        "capture_errors": [],
        "eligible_for_execution": True,
        "source": {"git_commit_sha": "f82d9b00e28d6b8dba6abddce90ed50a433d42a1"},
    }


def run_entry(level, number, valid=True, verdict="PASS", run_id=None, catastrophic=False):
    return {
        "run_id": run_id or f"run-l{level}-n{number}",
        "load_level": level,
        "run_number": number,
        "valid": valid,
        "verdict": verdict,
        "manifest_id": MANIFEST_ID,
        "catastrophic": catastrophic,
        "metrics": {
            "cpu_p95_percent": 60.0,
            "memory_per_user_bytes": 1_000_000.0,
            "latency_p95_ms": 10.0,
            "latency_p99_ms": 20.0,
            "throughput_per_second": float(level),
            "error_rate": 0.001,
        },
    }


def make_state(store_root, **overrides):
    values = dict(
        version=1,
        state=ApprovalState.DRAFT,
        baseline_cycle_id=CYCLE,
        manifest_id=MANIFEST_ID,
        config_path="cfg.json",
        artifact_root=".",
        controls={},
        runs=(),
        evaluated=False,
        reported=False,
        approved=False,
        catastrophic=False,
        last_error=None,
    )
    values.update(overrides)
    return CycleState(**values)


class FakeCalls:
    def __init__(self):
        self.calls = []

    def record(self, name, payload=None):
        self.calls.append((name, payload))

    def names(self):
        return [name for name, _ in self.calls]


def make_deps(root, calls=None, **overrides):
    calls = calls or FakeCalls()
    outputs = []

    def fake_load_config(path):
        calls.record("load_config", str(path))
        return {"config": str(path)}

    def fake_capture_manifest(repo_root, config):
        calls.record("capture_manifest")
        return manifest()

    def fake_verify_manifest(expected, actual):
        calls.record("verify_manifest", (expected, actual))
        return []

    def fake_build_dataset_plan(seed):
        calls.record("build_dataset_plan", seed)
        return {"seed": seed}

    def fake_dataset_counts(plan):
        calls.record("dataset_counts")
        return {"accounts": 6000, "characters": 12000, "guilds": 200, "parties": 500}

    def fake_controller(request):
        class Controller:
            def run_preflight(self):
                calls.record("lifecycle.preflight")

            def run_service_start(self, commands):
                calls.record("lifecycle.service_start")

            def run_preconditioning(self):
                calls.record("lifecycle.preconditioning")

            def run_ramp_up(self):
                calls.record("lifecycle.ramp_up")

            def run_steady_state(self):
                calls.record("lifecycle.steady_state")

            def run_cooldown(self):
                calls.record("lifecycle.cooldown")

            def run_validation(self):
                calls.record("lifecycle.validation")

            def run_reporting(self):
                calls.record("lifecycle.reporting")

            def abort(self, reason, catastrophic):
                calls.record("lifecycle.abort", reason)

        calls.record("create_run_controller", request)
        return Controller()

    def fake_collectors(request):
        class Collectors:
            def start(self, context):
                calls.record("collectors.start", context)

            def stop(self):
                calls.record("collectors.stop")

        calls.record("create_collectors")
        return Collectors()

    def fake_harness(request):
        calls.record("run_harness", request["run_id"])
        return {
            "run_data": {"run_id": request["run_id"]},
            "metric_bundle": {},
            "catastrophic": False,
            "metrics": {
                "cpu_p95_percent": 60.0,
                "memory_per_user_bytes": 1_000_000.0,
                "latency_p95_ms": 10.0,
                "latency_p99_ms": 20.0,
                "throughput_per_second": float(request["load_level"]),
                "error_rate": 0.001,
            },
            "worst_metrics": {},
            "timeseries_rows": [],
            "workload_rows": [],
            "anomalies": [],
            "prometheus_queries": {"start": 0, "end": 0, "step": 5, "queries": []},
            "source_files": {},
            "created_utc": "2026-08-02T20:00:00Z",
        }

    def fake_validity(run_data):
        calls.record("evaluate_validity")
        return ValidityResult(
            valid=True,
            reasons=(),
            checked_gates=("gate",),
            run_id=run_data["run_id"],
            manifest_id=MANIFEST_ID,
        )

    def fake_slos(validity, bundle, thresholds):
        calls.record("evaluate_slos", thresholds)
        return types.SimpleNamespace(catastrophic_signals=(), status=MetricVerdict.PASS)

    def fake_write_run_artifacts(**kwargs):
        calls.record("write_run_artifacts", kwargs)
        return {"complete": True}

    deps = CLIDependencies(
        load_config=fake_load_config,
        capture_manifest=fake_capture_manifest,
        capture_runtime_manifest=lambda repo_root, config: calls.record("capture_runtime_manifest") or manifest(),
        verify_manifest=fake_verify_manifest,
        load_slo_thresholds=lambda: calls.record("load_slo_thresholds") or {"warning_zone_ratio": 0.9},
        build_dataset_plan=fake_build_dataset_plan,
        dataset_counts=fake_dataset_counts,
        emit_dataset_sql=lambda plan, path: calls.record("emit_dataset_sql"),
        create_run_controller=fake_controller,
        create_collector_controller=fake_collectors,
        run_harness=fake_harness,
        evaluate_validity=fake_validity,
        evaluate_slos=fake_slos,
        aggregate_level=lambda runs: calls.record("aggregate_level", len(runs)) or _level(runs[0].load_level if runs else 500),
        evaluate_scaling=lambda levels: calls.record("evaluate_scaling") or ScalingResult(True, (), None),
        evaluate_regression=lambda levels, previous: calls.record("evaluate_regression") or RegressionResult(True, (), (500,)),
        derive_capacity=lambda levels: calls.record("derive_capacity") or _capacity(),
        load_previous_baseline=lambda: calls.record("load_previous_baseline") or {"version": 1},
        load_manifest=lambda manifest_id: calls.record("load_manifest") or manifest(),
        write_run_artifacts=fake_write_run_artifacts,
        write_cycle_reports=None,
        approve_baseline=None,
        reject_baseline=None,
        render_prometheus_config=lambda cycle, manifest_id: calls.record("render_prometheus") or "global: {}\n",
        render_dashboard=lambda template, cycle, manifest_id, run_id: calls.record("render_dashboard") or {"rendered": True},
        state_store=CycleStateStore(root),
        stdout=lambda text: outputs.append(text),
        stderr=lambda text: outputs.append(("ERR", text)),
    )
    deps = dataclasses.replace(deps, **overrides)
    return deps, calls, outputs


def _level(level):
    return LevelAggregation(
        load_level=level,
        manifest_id=MANIFEST_ID,
        valid_run_count=3,
        required_valid_run_count=3,
        run_ids=(f"run-l{level}-n1", f"run-l{level}-n2", f"run-l{level}-n3"),
        run_verdicts=(MetricVerdict.PASS,) * 3,
        verdict=MetricVerdict.PASS,
        median_metrics={},
        worst_metrics={},
        stability_metrics={},
        warnings=(),
        failures=(),
    )


def _capacity():
    return CapacityResult(
        safe_capacity=5000,
        conditional_capacity=None,
        tested_ceiling=5000,
        verdict=CapacityVerdict.PASS,
        first_degradation_level=None,
        notes=(),
    )


def run_main(argv, deps):
    return main(argv, dependencies=deps)


def controller_factory_with_failures(calls, failures=None):
    failures = failures or {}

    class Controller:
        def _check(self, name):
            if name in failures:
                raise failures[name]

        def run_preflight(self):
            self._check("preflight")
            calls.record("lifecycle.preflight")

        def run_service_start(self, commands):
            self._check("service_start")
            calls.record("lifecycle.service_start")

        def run_preconditioning(self):
            self._check("preconditioning")
            calls.record("lifecycle.preconditioning")

        def run_ramp_up(self):
            self._check("ramp_up")
            calls.record("lifecycle.ramp_up")

        def run_steady_state(self):
            self._check("steady_state")
            calls.record("lifecycle.steady_state")

        def run_cooldown(self):
            self._check("cooldown")
            calls.record("lifecycle.cooldown")

        def run_validation(self):
            self._check("validation")
            calls.record("lifecycle.validation")

        def run_reporting(self):
            self._check("reporting")
            calls.record("lifecycle.reporting")

        def abort(self, reason, catastrophic=False):
            calls.record("lifecycle.abort", reason)

    def factory(request):
        calls.record("create_run_controller", request)
        return Controller()

    return factory


def collectors_factory_with_failures(calls, fail_start=False, fail_stop=False):
    class Collectors:
        def start(self, context):
            calls.record("collectors.start", context)
            if fail_start:
                raise RuntimeError("start failed")

        def stop(self):
            calls.record("collectors.stop")
            if fail_stop:
                raise RuntimeError("stop jammed")

    def factory(request):
        calls.record("create_collectors")
        return Collectors()

    return factory


class RecordingStore:
    def __init__(self, store, calls):
        self._store = store
        self._calls = calls

    def path_for(self, cycle):
        return self._store.path_for(cycle)

    def exists(self, cycle):
        return self._store.exists(cycle)

    def read(self, cycle):
        return self._store.read(cycle)

    def write(self, state):
        self._calls.record("state.write")
        return self._store.write(state)


def full_source_harness(calls, catastrophic=False):
    from tools.performance.a3.tests.test_reporting import build_source_files

    def harness(request):
        calls.record("run_harness", request["run_id"])
        sources = build_source_files(
            Path(request["artifact_root"]) / "raw-run-src"
        )
        return {
            "run_data": {"run_id": request["run_id"]},
            "metric_bundle": {},
            "catastrophic": catastrophic,
            "metrics": {},
            "worst_metrics": {},
            "timeseries_rows": [],
            "workload_rows": [],
            "anomalies": [],
            "prometheus_queries": {"start": 0, "end": 0, "step": 5, "queries": []},
            "source_files": sources,
            "created_utc": "2026-08-02T20:00:00Z",
        }

    return harness


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()

    def store(self):
        return CycleStateStore(self.root)

    def write_state(self, **overrides):
        state = make_state(self.root, **overrides)
        self.store().write(state)
        return state


class ParserTests(unittest.TestCase):
    def test_prepare_args(self):
        args = build_parser().parse_args(["prepare", "--config", "c.json"])
        self.assertEqual(args.command, "prepare")
        self.assertEqual(args.config, "c.json")
        self.assertEqual(args.artifact_root, ".")
        self.assertFalse(args.dry_run)

    def test_control_subcommands(self):
        args = build_parser().parse_args(["control", "idle", "--cycle", CYCLE])
        self.assertEqual(args.control_command, "idle")
        args = build_parser().parse_args(["control", "webgl", "--cycle", CYCLE])
        self.assertEqual(args.control_command, "webgl")

    def test_run_choices(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--cycle", CYCLE, "--users", "2500", "--run", "2"])
        self.assertEqual(args.users, 2500)
        self.assertEqual(args.run, 2)
        for bad in (["--users", "250", "--run", "1"], ["--users", "500", "--run", "4"]):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["run", "--cycle", CYCLE, *bad])
            self.assertEqual(ctx.exception.code, 2)

    def test_approve_required_fields(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["approve", "--cycle", CYCLE, "--rationale", "x", "--approved-utc", UTC])
        with self.assertRaises(SystemExit):
            parser.parse_args(["approve", "--cycle", CYCLE, "--approver", "x", "--approved-utc", UTC])
        with self.assertRaises(SystemExit):
            parser.parse_args(["approve", "--cycle", CYCLE, "--approver", "x", "--rationale", "y"])

    def test_unknown_command(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["bogus"])

    def test_dry_run_flag(self):
        args = build_parser().parse_args(["prepare", "--config", "c.json", "--dry-run"])
        self.assertTrue(args.dry_run)


class PrepareTests(CliTestBase):
    def test_valid_dry_run(self):
        deps, calls, outputs = make_deps(self.root)
        code = run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root), "--dry-run"], deps)
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(outputs[0])
        self.assertEqual(payload["command"], "prepare")
        self.assertEqual(payload["baseline_cycle_id"], CYCLE)
        self.assertTrue(payload["dry_run"])
        self.assertIn("dataset", payload)
        self.assertIn("planned_paths", payload)
        self.assertIn("load_config", calls.names())
        self.assertIn("capture_manifest", calls.names())
        self.assertIn("build_dataset_plan", calls.names())
        self.assertNotIn("create_run_controller", calls.names())
        self.assertNotIn("create_collectors", calls.names())
        self.assertNotIn("run_harness", calls.names())
        self.assertFalse((self.root / "artifacts").exists())

    def test_invalid_config_exit_2(self):
        def bad_config(path):
            raise ValueError("bad config")

        deps, calls, outputs = make_deps(self.root, load_config=bad_config)
        code = run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_USAGE)

    def test_manifest_capture_errors_refused(self):
        bad = manifest()
        bad["capture_errors"] = [{"field": "x"}]
        deps, calls, _ = make_deps(self.root, capture_manifest=lambda r, c: bad)
        code = run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_ineligible_manifest_refused(self):
        bad = manifest()
        bad["eligible_for_execution"] = False
        deps, calls, _ = make_deps(self.root, capture_manifest=lambda r, c: bad)
        code = run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_real_prepare_persists_draft(self):
        deps, calls, outputs = make_deps(self.root)
        code = run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_OK)
        state = self.store().read(CYCLE)
        self.assertIs(state.state, ApprovalState.DRAFT)
        self.assertEqual(state.manifest_id, MANIFEST_ID)
        self.assertIn("render_prometheus", calls.names())
        payload = json.loads(outputs[0])
        self.assertFalse(payload["dry_run"])

    def test_repeated_prepare_refused(self):
        deps, calls, _ = make_deps(self.root)
        run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps)
        code = run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_deterministic_plan_output(self):
        deps1, _, outputs1 = make_deps(self.root)
        run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root), "--dry-run"], deps1)
        deps2, _, outputs2 = make_deps(self.root)
        run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root), "--dry-run"], deps2)
        self.assertEqual(outputs1, outputs2)


class ControlTests(CliTestBase):
    def test_missing_cycle(self):
        deps, calls, _ = make_deps(self.root)
        code = run_main(["control", "idle", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_idle_persists_completion(self):
        self.write_state()
        runner = mock.Mock(return_value={"verdict": "PASS", "duration_seconds": 600})
        deps, calls, _ = make_deps(self.root, run_control=runner)
        code = run_main(["control", "idle", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_OK)
        runner.assert_called_once()
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["name"], "idle")
        self.assertEqual(kwargs["duration_seconds"], 600)
        state = self.store().read(CYCLE)
        self.assertTrue(state.controls["idle"]["completed"])

    def test_webgl_before_idle_rejected(self):
        self.write_state()
        deps, calls, _ = make_deps(self.root, run_control=mock.Mock())
        code = run_main(["control", "webgl", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_webgl_requires_twenty_clients(self):
        self.write_state(controls={"idle": {"completed": True, "verdict": "PASS"}})
        deps, calls, _ = make_deps(
            self.root,
            run_control=mock.Mock(return_value={"verdict": "PASS", "clients": 20}),
        )
        code = run_main(["control", "webgl", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_OK)
        state = self.store().read(CYCLE)
        self.assertTrue(state.controls["webgl"]["completed"])
        self.assertEqual(state.controls["webgl"]["clients"], 20)

        root2 = Path(self._tmp.name) / "root2"
        root2.mkdir()
        CycleStateStore(root2).write(
            make_state(root2, controls={"idle": {"completed": True, "verdict": "PASS"}})
        )
        deps2, _, outputs2 = make_deps(
            root2,
            run_control=mock.Mock(return_value={"verdict": "PASS", "clients": 19}),
        )
        code = run_main(["control", "webgl", "--cycle", CYCLE, "--artifact-root", str(root2)], deps2)
        self.assertEqual(code, EXIT_OPERATIONAL)

    def test_repeated_control_rejected(self):
        self.write_state(controls={"idle": {"completed": True, "verdict": "PASS"}})
        deps, calls, _ = make_deps(self.root, run_control=mock.Mock())
        code = run_main(["control", "idle", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_dry_run_does_not_execute(self):
        self.write_state()
        runner = mock.Mock()
        deps, calls, outputs = make_deps(self.root, run_control=runner)
        code = run_main(["control", "idle", "--cycle", CYCLE, "--artifact-root", str(self.root), "--dry-run"], deps)
        self.assertEqual(code, EXIT_OK)
        runner.assert_not_called()
        state = self.store().read(CYCLE)
        self.assertEqual(dict(state.controls), {})


CONTROLS_DONE = {
    "idle": {"completed": True, "verdict": "PASS"},
    "webgl": {"completed": True, "verdict": "PASS", "clients": 20},
}


class RunTests(CliTestBase):
    def _argv(self, users=500, run=1, dry=False):
        argv = ["run", "--cycle", CYCLE, "--users", str(users), "--run", str(run), "--artifact-root", str(self.root)]
        if dry:
            argv.append("--dry-run")
        return argv

    def test_controls_required(self):
        self.write_state()
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_load_progression_enforced(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(users=1000), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_three_valid_runs_unlock_next_level(self):
        runs = tuple(run_entry(500, n) for n in (1, 2, 3))
        self.write_state(controls=CONTROLS_DONE, runs=runs)
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(users=1000), deps)
        self.assertEqual(code, EXIT_OK)

    def test_duplicate_finalized_run_refused(self):
        self.write_state(controls=CONTROLS_DONE, runs=(run_entry(500, 1),))
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_invalid_run_preserved_replacement_allowed(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        validity_invalid = ValidityResult(
            valid=False, reasons=(), checked_gates=(), run_id="run-l500-n1", manifest_id=MANIFEST_ID
        )
        deps = dataclasses.replace(deps, evaluate_validity=lambda run_data: validity_invalid)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        state = self.store().read(CYCLE)
        self.assertEqual(len(state.runs), 1)
        self.assertFalse(state.runs[0]["valid"])
        # Replacement attempt with the same base identity is allowed and the
        # invalid evidence is preserved (no overwrite).
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        state = self.store().read(CYCLE)
        self.assertEqual(len(state.runs), 2)
        self.assertNotEqual(state.runs[0]["run_id"], state.runs[1]["run_id"])

    def test_manifest_drift_rejected(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, _, _ = make_deps(self.root, verify_manifest=lambda e, a: ["build.map_server_sha256 changed"])
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_lifecycle_order_and_artifact_write(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            calls.names(),
            [
                "load_config",
                "load_manifest",
                "capture_runtime_manifest",
                "verify_manifest",
                "create_run_controller",
                "lifecycle.preflight",
                "lifecycle.service_start",
                "create_collectors",
                "collectors.start",
                "run_harness",
                "lifecycle.preconditioning",
                "lifecycle.ramp_up",
                "lifecycle.steady_state",
                "lifecycle.cooldown",
                "collectors.stop",
                "lifecycle.validation",
                "lifecycle.reporting",
                "evaluate_validity",
                "load_slo_thresholds",
                "evaluate_slos",
                "write_run_artifacts",
            ],
        )

    def test_collectors_stopped_on_harness_failure(self):
        self.write_state(controls=CONTROLS_DONE)

        def bad_harness(request):
            calls.record("run_harness")
            raise RuntimeError("harness crashed")

        deps, calls, outputs = make_deps(self.root, run_harness=bad_harness)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OPERATIONAL)
        names = calls.names()
        self.assertIn("collectors.stop", names)
        self.assertLess(names.index("run_harness"), names.index("collectors.stop"))
        state = self.store().read(CYCLE)
        self.assertFalse(state.catastrophic)
        self.assertEqual(names.count("collectors.stop"), 1)

    def test_valid_fail_run_allows_continuation(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        deps = dataclasses.replace(
            deps,
            evaluate_slos=lambda v, b, t: types.SimpleNamespace(
                catastrophic_signals=(), status=MetricVerdict.FAIL
            ),
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        state = self.store().read(CYCLE)
        self.assertEqual(state.runs[0]["verdict"], "FAIL")
        # A non-catastrophic FAIL does not block the next run at the level.
        code = run_main(self._argv(run=2), deps)
        self.assertEqual(code, EXIT_OK)

    def test_catastrophic_run_blocks_cycle(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        deps = dataclasses.replace(
            deps, run_harness=full_source_harness(calls, catastrophic=True)
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_CATASTROPHIC)
        state = self.store().read(CYCLE)
        self.assertTrue(state.catastrophic)

    def test_dry_run_no_side_effects(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, outputs = make_deps(self.root)
        code = run_main(self._argv(dry=True), deps)
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(outputs[0])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["users"], 500)
        self.assertEqual(payload["run_number"], 1)
        self.assertIn("phases", payload)
        for forbidden in (
            "create_run_controller",
            "create_collectors",
            "run_harness",
            "write_run_artifacts",
            "capture_runtime_manifest",
            "verify_manifest",
            "load_slo_thresholds",
        ):
            self.assertNotIn(forbidden, calls.names())
        state = self.store().read(CYCLE)
        self.assertEqual(len(state.runs), 0)


class EvaluateTests(CliTestBase):
    def _argv(self):
        return ["evaluate", "--cycle", CYCLE, "--artifact-root", str(self.root)]

    def _ready_state(self, **overrides):
        runs = tuple(
            run_entry(level, number)
            for level in (500, 1000, 2500, 5000)
            for number in (1, 2, 3)
        )
        values = dict(controls=CONTROLS_DONE, runs=runs)
        values.update(overrides)
        self.write_state(**values)

    def test_fewer_than_three_valid_rejected(self):
        runs = tuple(run_entry(500, n) for n in (1, 2)) + (
            run_entry(500, 3, valid=False),
        )
        self.write_state(controls=CONTROLS_DONE, runs=runs)
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_mixed_manifest_rejected(self):
        runs = list(run_entry(500, n) for n in (1, 2, 3))
        bad = run_entry(500, 3)
        bad["manifest_id"] = "a3-20260802-0000000-ubuntu2404-8c16t-32g-999"
        runs[2] = bad
        self.write_state(controls=CONTROLS_DONE, runs=tuple(runs))
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_invalid_runs_ignored_in_aggregation(self):
        runs = list(run_entry(500, n) for n in (1, 2, 3))
        runs.append(run_entry(500, 3, valid=False, run_id="run-l500-n3-r2"))
        self.write_state(controls=CONTROLS_DONE, runs=tuple(runs))
        aggregated = []

        def fake_aggregate(runs_arg):
            aggregated.append(len([r for r in runs_arg if r.valid]))
            return _level(runs_arg[0].load_level)

        deps, _, _ = make_deps(self.root, aggregate_level=fake_aggregate)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(aggregated, [3])

    def test_full_evaluation_success(self):
        self._ready_state()
        deps, calls, outputs = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        for expected in ("aggregate_level", "evaluate_scaling", "evaluate_regression", "derive_capacity"):
            self.assertIn(expected, calls.names())
        state = self.store().read(CYCLE)
        self.assertIs(state.state, ApprovalState.CI_EVALUATED)
        self.assertTrue(state.evaluated)
        self.assertTrue(state.controls["ci"]["evaluated"])
        self.assertEqual(state.controls["ci"]["status"], "success")

    def test_fail_cycle_still_ci_evaluated(self):
        self._ready_state()
        failing = LevelAggregation(
            load_level=500,
            manifest_id=MANIFEST_ID,
            valid_run_count=3,
            required_valid_run_count=3,
            run_ids=("a", "b", "c"),
            run_verdicts=(MetricVerdict.PASS, MetricVerdict.PASS, MetricVerdict.FAIL),
            verdict=MetricVerdict.FAIL,
            median_metrics={},
            worst_metrics={},
            stability_metrics={},
            warnings=(),
            failures=("run c verdict FAIL",),
        )
        deps, calls, _ = make_deps(
            self.root,
            aggregate_level=lambda runs: failing,
            derive_capacity=lambda levels: dataclasses.replace(_capacity(), verdict=CapacityVerdict.FAIL),
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        state = self.store().read(CYCLE)
        self.assertIs(state.state, ApprovalState.CI_EVALUATED)

    def test_catastrophic_cycle_refused(self):
        self._ready_state(catastrophic=True)
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_failure_leaves_state_unchanged(self):
        self._ready_state()

        def bad_capacity(levels):
            raise RuntimeError("boom")

        deps, _, _ = make_deps(self.root, derive_capacity=bad_capacity)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_INTERNAL)
        state = self.store().read(CYCLE)
        self.assertIs(state.state, ApprovalState.DRAFT)
        self.assertFalse(state.evaluated)


class ReportTests(CliTestBase):
    def _argv(self):
        return ["report", "--cycle", CYCLE, "--artifact-root", str(self.root)]

    def _evaluated_state(self):
        runs = tuple(
            run_entry(level, number)
            for level in (500, 1000, 2500, 5000)
            for number in (1, 2, 3)
        )
        self.write_state(controls=CONTROLS_DONE, runs=runs)
        deps, _, _ = make_deps(self.root)
        code = run_main(["evaluate", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_OK)

    def test_evaluate_required(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, _, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_report_success(self):
        from tools.performance.a3.reporting import write_cycle_reports

        self._evaluated_state()
        calls = []
        real_writer = write_cycle_reports

        def recording_writer(**kwargs):
            calls.append(kwargs)
            return real_writer(**kwargs)

        deps, _, outputs = make_deps(self.root, write_cycle_reports=recording_writer)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(calls), 1)
        state = self.store().read(CYCLE)
        self.assertTrue(state.reported)
        self.assertIs(state.state, ApprovalState.AWAITING_APPROVAL)
        payload = json.loads(outputs[0])
        for key in ("technical_report", "executive_summary", "comparison_csv", "artifact_index"):
            self.assertIn(key, payload)
        cycle_dir = self.root / "artifacts" / "performance" / "a3" / CYCLE
        self.assertTrue((cycle_dir / "checksums.json").is_file())

    def test_repeat_report_refused(self):
        from tools.performance.a3.reporting import write_cycle_reports

        self._evaluated_state()
        deps, _, _ = make_deps(self.root, write_cycle_reports=write_cycle_reports)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_writer_failure_leaves_state_unchanged(self):
        self._evaluated_state()

        def bad_writer(**kwargs):
            raise RuntimeError("write failed")

        deps, _, _ = make_deps(self.root, write_cycle_reports=bad_writer)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_INTERNAL)
        state = self.store().read(CYCLE)
        self.assertFalse(state.reported)
        self.assertIs(state.state, ApprovalState.CI_EVALUATED)


class ApprovalTests(CliTestBase):
    def _argv(self):
        return [
            "approve", "--cycle", CYCLE, "--approver", "J. Operator",
            "--rationale", "Reviewed all artifacts thoroughly.",
            "--approved-utc", UTC, "--artifact-root", str(self.root),
        ]

    def _awaiting_state(self):
        self.write_state(
            controls={**CONTROLS_DONE, "ci": {"evaluated": True, "status": "success"},
                      "cycle_summary": {
                          "capacity": {"verdict": "PASS", "safe_capacity": 5000,
                                       "conditional_capacity": None, "tested_ceiling": 5000,
                                       "first_degradation_level": None, "notes": []},
                          "levels": {"500": {"verdict": "PASS"}, "1000": {"verdict": "PASS"},
                                     "2500": {"verdict": "PASS"}, "5000": {"verdict": "PASS"}},
                          "warnings": [],
                          "git_sha": "f82d9b00e28d6b8dba6abddce90ed50a433d42a1",
                          "report_checksums_sha256": "a" * 64,
                      }},
            state=ApprovalState.AWAITING_APPROVAL,
            evaluated=True,
            reported=True,
        )

    def test_awaiting_required(self):
        self.write_state(state=ApprovalState.CI_EVALUATED, evaluated=True)
        deps, _, _ = make_deps(self.root, approve_baseline=mock.Mock())
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_approve_success_exact_values(self):
        self._awaiting_state()
        approver = mock.Mock(
            return_value=types.SimpleNamespace(
                approval_path=self.root / "approved-baseline.json",
                record=types.SimpleNamespace(approval_record_sha256="b" * 64),
            )
        )
        deps, _, outputs = make_deps(self.root, approve_baseline=approver)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        kwargs = approver.call_args.kwargs
        self.assertEqual(kwargs["approver"], "J. Operator")
        self.assertEqual(kwargs["rationale"], "Reviewed all artifacts thoroughly.")
        self.assertEqual(kwargs["approved_utc"], UTC)
        state = self.store().read(CYCLE)
        self.assertTrue(state.approved)
        self.assertIs(state.state, ApprovalState.APPROVED)

    def test_approval_failure_leaves_state_unchanged(self):
        self._awaiting_state()

        def bad_approve(**kwargs):
            raise ValueError("bad rationale")

        deps, _, _ = make_deps(self.root, approve_baseline=bad_approve)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_USAGE)
        state = self.store().read(CYCLE)
        self.assertFalse(state.approved)
        self.assertIs(state.state, ApprovalState.AWAITING_APPROVAL)

    def test_repeat_approval_refused(self):
        self._awaiting_state()
        approver = mock.Mock(
            return_value=types.SimpleNamespace(
                approval_path=self.root / "approved-baseline.json",
                record=types.SimpleNamespace(approval_record_sha256="b" * 64),
            )
        )
        deps, _, _ = make_deps(self.root, approve_baseline=approver)
        self.assertEqual(run_main(self._argv(), deps), EXIT_OK)
        self.assertEqual(run_main(self._argv(), deps), EXIT_REFUSAL)

    def test_secret_never_printed(self):
        self._awaiting_state()
        sentinel = "hunter2s3ntinel"
        argv = [
            "approve", "--cycle", CYCLE, "--approver", "J. Operator",
            "--rationale", f"password {sentinel} is long enough.",
            "--approved-utc", UTC, "--artifact-root", str(self.root),
        ]
        deps, _, outputs = make_deps(self.root, approve_baseline=mock.Mock())
        code = run_main(argv, deps)
        self.assertNotEqual(code, EXIT_OK)
        for output in outputs:
            self.assertNotIn(sentinel, str(output))

    def test_reject_success(self):
        self._awaiting_state()
        rejecter = mock.Mock(
            return_value=types.SimpleNamespace(
                approval_path=self.root / "rejected-baseline.json",
                record=types.SimpleNamespace(approval_record_sha256="c" * 64),
            )
        )
        deps, _, _ = make_deps(self.root, reject_baseline=rejecter)
        argv = [
            "reject", "--cycle", CYCLE, "--approver", "J. Operator",
            "--rationale", "Does not meet the bar.",
            "--rejected-utc", UTC, "--artifact-root", str(self.root),
        ]
        code = run_main(argv, deps)
        self.assertEqual(code, EXIT_OK)
        state = self.store().read(CYCLE)
        self.assertIs(state.state, ApprovalState.REJECTED)


class StateStoreTests(CliTestBase):
    def test_exact_path(self):
        store = self.store()
        expected = (
            self.root / "artifacts" / "performance" / "a3" / CYCLE / "cycle-state.json"
        )
        self.assertEqual(store.path_for(CYCLE), expected)

    def test_round_trip(self):
        state = self.write_state(controls={"idle": {"completed": True}})
        loaded = self.store().read(CYCLE)
        self.assertEqual(loaded, state)
        self.assertIs(loaded.state, ApprovalState.DRAFT)
        self.assertTrue(loaded.controls["idle"]["completed"])

    def test_deterministic_bytes(self):
        state = make_state(self.root)
        self.store().write(state)
        first = self.store().path_for(CYCLE).read_bytes()
        self.store().write(state)
        self.assertEqual(first, self.store().path_for(CYCLE).read_bytes())

    def test_no_temp_files(self):
        self.write_state()
        cycle_dir = self.store().path_for(CYCLE).parent
        self.assertEqual(list(cycle_dir.rglob("*.tmp")), [])

    def test_missing_state(self):
        with self.assertRaises(CLIError):
            self.store().read(CYCLE)

    def test_malformed_json(self):
        path = self.store().path_for(CYCLE)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(CLIError):
            self.store().read(CYCLE)

    def test_unknown_fields_rejected(self):
        self.write_state()
        path = self.store().path_for(CYCLE)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["surprise"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CLIError):
            self.store().read(CYCLE)

    def test_wrong_version_rejected(self):
        self.write_state()
        path = self.store().path_for(CYCLE)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 2
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CLIError):
            self.store().read(CYCLE)

    def test_symlink_rejection(self):
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(CLIError):
                self.store().write(make_state(self.root))

    def test_secret_marker_rejected(self):
        state = make_state(self.root, last_error="the password leaked")
        with self.assertRaises(CLIError):
            self.store().write(state)

    def test_nested_immutability(self):
        state = make_state(self.root, controls={"idle": {"completed": True}})
        with self.assertRaises(TypeError):
            state.controls["idle"]["completed"] = False
        with self.assertRaises(TypeError):
            state.controls["x"] = 1
        self.assertIsInstance(state.runs, tuple)


class OutputTests(CliTestBase):
    def test_error_channel_separation(self):
        deps, _, outputs = make_deps(self.root)
        code = run_main(["control", "idle", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_REFUSAL)
        stdout_lines = [o for o in outputs if not (isinstance(o, tuple) and o[0] == "ERR")]
        stderr_lines = [o for o in outputs if isinstance(o, tuple) and o[0] == "ERR"]
        self.assertEqual(stdout_lines, [])
        self.assertTrue(stderr_lines)

    def test_no_stack_trace_on_internal_error(self):
        def bad_config(path):
            raise RuntimeError("unexpected boom")

        deps, _, outputs = make_deps(self.root, load_config=bad_config)
        code = run_main(["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps)
        self.assertEqual(code, EXIT_INTERNAL)
        stderr_lines = [o for o in outputs if isinstance(o, tuple) and o[0] == "ERR"]
        self.assertTrue(stderr_lines)
        self.assertNotIn("Traceback", str(stderr_lines))

    def test_main_none_uses_sys_argv(self):
        deps, _, _ = make_deps(self.root)
        with mock.patch("sys.argv", ["cli", "bogus-command"]):
            code = main(None, dependencies=deps)
        self.assertEqual(code, EXIT_USAGE)

    def test_main_list_testable(self):
        deps, _, _ = make_deps(self.root)
        code = main(["control", "idle", "--cycle", CYCLE, "--artifact-root", str(self.root)], dependencies=deps)
        self.assertEqual(code, EXIT_REFUSAL)

    def test_init_exports(self):
        import tools.performance.a3 as package

        self.assertTrue(callable(package.main))
        self.assertTrue(callable(package.build_parser))


class CollectorSafetyTests(CliTestBase):
    def _argv(self):
        return ["run", "--cycle", CYCLE, "--users", "500", "--run", "1", "--artifact-root", str(self.root)]

    def test_stop_called_once_on_each_lifecycle_failure(self):
        from tools.performance.a3.lifecycle import CatastrophicRunError

        cases = {
            "harness": RuntimeError("harness crashed"),
            "preconditioning": RuntimeError("preconditioning failed"),
            "ramp_up": RuntimeError("ramp failed"),
            "steady_state": RuntimeError("steady failed"),
            "cooldown": RuntimeError("cooldown failed"),
            "validation": RuntimeError("validation failed"),
            "reporting": RuntimeError("reporting failed"),
        }
        for index, (phase, error) in enumerate(cases.items()):
            with self.subTest(phase=phase):
                root = Path(self._tmp.name) / f"root-{phase}"
                root.mkdir()
                CycleStateStore(root).write(make_state(root, controls=CONTROLS_DONE))
                calls = FakeCalls()
                harness_error = error if phase == "harness" else None
                deps, calls, _ = make_deps(
                    root,
                    calls=calls,
                    create_run_controller=controller_factory_with_failures(
                        calls, {} if phase == "harness" else {phase: error}
                    ),
                    run_harness=(
                        (lambda request: (_ for _ in ()).throw(error))
                        if phase == "harness"
                        else full_source_harness(calls)
                    ),
                )
                code = run_main(
                    ["run", "--cycle", CYCLE, "--users", "500", "--run", "1", "--artifact-root", str(root)],
                    deps,
                )
                self.assertEqual(code, EXIT_OPERATIONAL, phase)
                self.assertEqual(calls.names().count("collectors.stop"), 1, phase)
                state = CycleStateStore(root).read(CYCLE)
                self.assertFalse(state.catastrophic, phase)
                self.assertEqual(len(state.runs), 0, phase)

    def test_start_failure_means_no_stop(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(
            self.root,
            create_collector_controller=collectors_factory_with_failures(FakeCalls(), fail_start=True),
        )
        # Use the same calls object for consistent counting.
        calls = FakeCalls()
        deps = dataclasses.replace(
            deps,
            create_collector_controller=collectors_factory_with_failures(calls, fail_start=True),
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OPERATIONAL)
        self.assertEqual(calls.names().count("collectors.stop"), 0)

    def test_normal_run_stop_exactly_once(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(calls.names().count("collectors.stop"), 1)

    def test_stop_failure_after_success_is_operational(self):
        self.write_state(controls=CONTROLS_DONE)
        calls = FakeCalls()
        deps, calls, outputs = make_deps(
            self.root,
            calls=calls,
            create_collector_controller=collectors_factory_with_failures(calls, fail_stop=True),
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OPERATIONAL)
        self.assertEqual(calls.names().count("collectors.stop"), 1)
        stderr = [o for o in outputs if isinstance(o, tuple) and o[0] == "ERR"]
        self.assertIn("stop jammed", str(stderr))

    def test_dual_failure_preserves_both_errors(self):
        self.write_state(controls=CONTROLS_DONE)
        calls = FakeCalls()

        def bad_harness(request):
            calls.record("run_harness")
            raise RuntimeError("harness crashed")

        deps, calls, outputs = make_deps(
            self.root,
            calls=calls,
            run_harness=bad_harness,
            create_collector_controller=collectors_factory_with_failures(calls, fail_stop=True),
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OPERATIONAL)
        self.assertEqual(calls.names().count("collectors.stop"), 1)
        stderr = str([o for o in outputs if isinstance(o, tuple) and o[0] == "ERR"])
        self.assertIn("harness crashed", stderr)
        self.assertIn("stop jammed", stderr)


class FailureClassificationTests(CliTestBase):
    def _argv(self):
        return ["run", "--cycle", CYCLE, "--users", "500", "--run", "1", "--artifact-root", str(self.root)]

    def _recording_deps(self, root, calls, **overrides):
        deps, calls, outputs = make_deps(root, calls=calls, **overrides)
        return dataclasses.replace(
            deps, state_store=RecordingStore(CycleStateStore(root), calls)
        ), calls, outputs

    def test_confirmed_catastrophic_harness_result(self):
        self.write_state(controls=CONTROLS_DONE)
        calls = FakeCalls()
        deps, calls, _ = self._recording_deps(
            self.root, calls, run_harness=full_source_harness(calls, catastrophic=True)
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_CATASTROPHIC)
        names = calls.names()
        self.assertEqual(
            names,
            [
                "load_config",
                "load_manifest",
                "capture_runtime_manifest",
                "verify_manifest",
                "create_run_controller",
                "lifecycle.preflight",
                "lifecycle.service_start",
                "create_collectors",
                "collectors.start",
                "run_harness",
                "lifecycle.abort",
                "collectors.stop",
                "write_run_artifacts",
                "state.write",
            ],
        )
        writer = [p for n, p in calls.calls if n == "write_run_artifacts"][0]
        self.assertEqual(
            set(writer),
            {
                "artifact_root",
                "baseline_cycle_id",
                "run_payload",
                "summary_payload",
                "timeseries_rows",
                "workload_rows",
                "slo_result",
                "anomalies",
                "prometheus_queries",
                "source_files",
            },
        )
        self.assertEqual(writer["run_payload"]["final_phase"], "ARTIFACT_CAPTURE")
        self.assertEqual(writer["summary_payload"]["verdict"], "BLOCKED")
        state = self.store().read(CYCLE)
        self.assertTrue(state.catastrophic)
        self.assertEqual(len(state.runs), 1)
        self.assertFalse(state.runs[0]["valid"])
        self.assertEqual(state.runs[0]["verdict"], "BLOCKED")
        self.assertTrue(state.runs[0]["catastrophic"])

    def test_confirmed_catastrophic_phase_exception(self):
        from tools.performance.a3.lifecycle import CatastrophicRunError

        self.write_state(controls=CONTROLS_DONE)
        calls = FakeCalls()
        deps, calls, _ = self._recording_deps(
            self.root,
            calls,
            create_run_controller=controller_factory_with_failures(
                calls, {"preconditioning": CatastrophicRunError("server crashed")}
            ),
            run_harness=full_source_harness(calls),
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_CATASTROPHIC)
        names = calls.names()
        self.assertEqual(calls.names().count("collectors.stop"), 1)
        self.assertLess(names.index("lifecycle.abort"), names.index("collectors.stop"))
        self.assertLess(names.index("collectors.stop"), names.index("write_run_artifacts"))
        self.assertLess(names.index("write_run_artifacts"), names.index("state.write"))
        state = self.store().read(CYCLE)
        self.assertTrue(state.catastrophic)
        self.assertEqual(state.runs[0]["verdict"], "BLOCKED")

    def test_catastrophic_slo_signal(self):
        self.write_state(controls=CONTROLS_DONE)
        calls = FakeCalls()
        deps, calls, _ = self._recording_deps(
            self.root,
            calls,
            run_harness=full_source_harness(calls),
            evaluate_slos=lambda v, b, t: types.SimpleNamespace(
                catastrophic_signals=("sig",), status=MetricVerdict.BLOCKED
            ),
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_CATASTROPHIC)
        self.assertIn("lifecycle.abort", calls.names())
        state = self.store().read(CYCLE)
        self.assertTrue(state.catastrophic)

    def test_preservation_failure_keeps_operational(self):
        self.write_state(controls=CONTROLS_DONE)
        calls = FakeCalls()

        def bad_writer(**kwargs):
            calls.record("write_run_artifacts")
            raise RuntimeError("checksum failed")

        deps, calls, _ = self._recording_deps(
            self.root,
            calls,
            run_harness=full_source_harness(calls, catastrophic=True),
            write_run_artifacts=bad_writer,
        )
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OPERATIONAL)
        self.assertEqual(calls.names().count("collectors.stop"), 1)
        state = self.store().read(CYCLE)
        self.assertFalse(state.catastrophic)
        self.assertEqual(len(state.runs), 0)

    def test_harness_exception_not_inferred_catastrophic(self):
        self.write_state(controls=CONTROLS_DONE)

        def bad_harness(request):
            raise RuntimeError("harness crashed")

        deps, calls, _ = make_deps(self.root, run_harness=bad_harness)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OPERATIONAL)
        state = self.store().read(CYCLE)
        self.assertFalse(state.catastrophic)
        self.assertEqual(len(state.runs), 0)


class RunOrderingTests(CliTestBase):
    def test_normal_order(self):
        self.write_state(controls=CONTROLS_DONE)
        calls = FakeCalls()
        deps, calls, _ = make_deps(self.root, calls=calls)
        deps = dataclasses.replace(
            deps, state_store=RecordingStore(CycleStateStore(self.root), calls)
        )
        code = run_main(
            ["run", "--cycle", CYCLE, "--users", "500", "--run", "1", "--artifact-root", str(self.root)],
            deps,
        )
        self.assertEqual(code, EXIT_OK)
        names = calls.names()
        expected = [
            "collectors.start",
            "run_harness",
            "lifecycle.preconditioning",
            "lifecycle.ramp_up",
            "lifecycle.steady_state",
            "lifecycle.cooldown",
            "collectors.stop",
            "evaluate_validity",
            "evaluate_slos",
            "write_run_artifacts",
            "state.write",
        ]
        positions = [names.index(step) for step in expected]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()


class DryRunIsolationTests(CliTestBase):
    def _file_tree(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def _evaluated_state(self):
        runs = tuple(
            run_entry(level, number)
            for level in (500, 1000, 2500, 5000)
            for number in (1, 2, 3)
        )
        self.write_state(controls=CONTROLS_DONE, runs=runs)
        deps, _, _ = make_deps(self.root)
        self.assertEqual(
            run_main(["evaluate", "--cycle", CYCLE, "--artifact-root", str(self.root)], deps),
            EXIT_OK,
        )

    def test_evaluate_dry_run_no_mutation(self):
        runs = tuple(run_entry(500, n) for n in (1, 2, 3))
        self.write_state(controls=CONTROLS_DONE, runs=runs)
        before = self._file_tree()
        deps, calls, outputs = make_deps(self.root)
        code = run_main(
            ["evaluate", "--cycle", CYCLE, "--artifact-root", str(self.root), "--dry-run"], deps
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(outputs[0])
        self.assertTrue(payload["dry_run"])
        self.assertIn("planned_levels", payload)
        for forbidden in (
            "aggregate_level",
            "evaluate_scaling",
            "evaluate_regression",
            "derive_capacity",
        ):
            self.assertNotIn(forbidden, calls.names())
        self.assertEqual(self._file_tree(), before)

    def test_report_dry_run_no_mutation(self):
        self._evaluated_state()
        before = self._file_tree()
        writer = mock.Mock()
        deps, calls, outputs = make_deps(self.root, write_cycle_reports=writer)
        code = run_main(
            ["report", "--cycle", CYCLE, "--artifact-root", str(self.root), "--dry-run"], deps
        )
        self.assertEqual(code, EXIT_OK)
        writer.assert_not_called()
        payload = json.loads(outputs[0])
        self.assertTrue(payload["dry_run"])
        self.assertIn("planned_paths", payload)
        self.assertEqual(self._file_tree(), before)

    def test_approve_dry_run_no_mutation_no_echo(self):
        self._evaluated_state()
        state = self.store().read(CYCLE)
        self.store().write(
            dataclasses.replace(state, state=ApprovalState.AWAITING_APPROVAL, reported=True)
        )
        before = self._file_tree()
        approver = mock.Mock()
        deps, calls, outputs = make_deps(self.root, approve_baseline=approver)
        code = run_main(
            [
                "approve", "--cycle", CYCLE, "--approver", "J. Operator",
                "--rationale", "Reviewed all artifacts thoroughly.",
                "--approved-utc", UTC, "--artifact-root", str(self.root), "--dry-run",
            ],
            deps,
        )
        self.assertEqual(code, EXIT_OK)
        approver.assert_not_called()
        payload = json.loads(outputs[0])
        self.assertTrue(payload["dry_run"])
        self.assertNotIn("J. Operator", outputs[0])
        self.assertNotIn("Reviewed all artifacts thoroughly.", outputs[0])
        self.assertEqual(self._file_tree(), before)

    def test_reject_dry_run_no_mutation(self):
        self._evaluated_state()
        state = self.store().read(CYCLE)
        self.store().write(
            dataclasses.replace(state, state=ApprovalState.AWAITING_APPROVAL, reported=True)
        )
        before = self._file_tree()
        rejecter = mock.Mock()
        deps, calls, outputs = make_deps(self.root, reject_baseline=rejecter)
        code = run_main(
            [
                "reject", "--cycle", CYCLE, "--approver", "J. Operator",
                "--rationale", "Does not meet the bar.",
                "--rejected-utc", UTC, "--artifact-root", str(self.root), "--dry-run",
            ],
            deps,
        )
        self.assertEqual(code, EXIT_OK)
        rejecter.assert_not_called()
        payload = json.loads(outputs[0])
        self.assertTrue(payload["dry_run"])
        self.assertNotIn("J. Operator", outputs[0])
        self.assertEqual(self._file_tree(), before)


class PrepareIntegrationTests(CliTestBase):
    FIXTURE_MANIFEST = (
        Path(__file__).resolve().parent / "fixtures" / "valid_manifest.json"
    )

    def _deps_with_real_dataset(self, calls=None, **overrides):
        from tools.performance.a3.dataset import build_dataset_plan, emit_dataset_sql

        captured = json.loads(self.FIXTURE_MANIFEST.read_text(encoding="utf-8"))
        deps, calls, outputs = make_deps(
            self.root,
            calls=calls,
            capture_manifest=lambda repo_root, config: captured,
            build_dataset_plan=build_dataset_plan,
            emit_dataset_sql=emit_dataset_sql,
            **overrides,
        )
        return deps, calls, outputs, captured

    def test_real_prepare_writes_all_artifacts(self):
        from tools.performance.a3.config import load_config

        from tools.performance.a3.cli import default_dependencies

        real_renderers = default_dependencies(self.root)
        deps, calls, outputs, captured = self._deps_with_real_dataset()
        deps = dataclasses.replace(
            deps,
            load_config=load_config,
            render_prometheus_config=real_renderers.render_prometheus_config,
            render_dashboard=real_renderers.render_dashboard,
        )
        code = run_main(
            [
                "prepare",
                "--config",
                "tools/performance/a3/config/a3.example.json",
                "--artifact-root",
                str(self.root),
            ],
            deps,
        )
        self.assertEqual(code, EXIT_OK)
        cycle_dir = self.root / "artifacts" / "performance" / "a3" / CYCLE
        written_manifest = json.loads((cycle_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(written_manifest, captured)
        sql_path = cycle_dir / "dataset" / "a3-dataset.sql"
        metadata_path = cycle_dir / "dataset" / "a3-dataset.sql.metadata.json"
        self.assertTrue(sql_path.is_file())
        self.assertTrue(metadata_path.is_file())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["seed"], 20260802)
        prometheus_text = (cycle_dir / "prometheus.yml").read_text(encoding="utf-8")
        self.assertIn(CYCLE, prometheus_text)
        self.assertNotIn("${A3_BASELINE_CYCLE_ID}", prometheus_text)
        dashboard_text = (cycle_dir / "grafana-dashboard.json").read_text(encoding="utf-8")
        self.assertNotIn("${A3_", dashboard_text)
        self.assertTrue((cycle_dir / "cycle-state.json").is_file())
        state = self.store().read(CYCLE)
        self.assertIs(state.state, ApprovalState.DRAFT)
        payload = json.loads(outputs[0])
        self.assertEqual(payload.get("execution"), "not executed (planning only)")

    def test_failure_before_state_write_leaves_no_state(self):
        deps, calls, outputs, captured = self._deps_with_real_dataset()

        def bad_emit(plan, path):
            raise RuntimeError("disk full")

        deps = dataclasses.replace(deps, emit_dataset_sql=bad_emit)
        code = run_main(
            ["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps
        )
        self.assertEqual(code, EXIT_INTERNAL)
        cycle_dir = self.root / "artifacts" / "performance" / "a3" / CYCLE
        self.assertFalse((cycle_dir / "cycle-state.json").exists())
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_load_manifest_after_prepare(self):
        deps, calls, outputs, captured = self._deps_with_real_dataset()
        code = run_main(
            ["prepare", "--config", "cfg.json", "--artifact-root", str(self.root)], deps
        )
        self.assertEqual(code, EXIT_OK)
        from tools.performance.a3.cli import default_dependencies

        real_deps = default_dependencies(self.root)
        loaded = real_deps.load_manifest(CYCLE)
        self.assertEqual(loaded, captured)


class RunIntegrationTests(CliTestBase):
    def _argv(self, users=500, run=1):
        return [
            "run", "--cycle", CYCLE, "--users", str(users), "--run", str(run),
            "--artifact-root", str(self.root),
        ]

    def test_full_manifest_comparison(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        verify_calls = [payload for name, payload in calls.calls if name == "verify_manifest"]
        self.assertEqual(len(verify_calls), 1)
        expected, actual = verify_calls[0]
        self.assertEqual(expected["manifest_id"], MANIFEST_ID)
        self.assertEqual(actual["manifest_id"], MANIFEST_ID)
        self.assertIn("capture_errors", expected)

    def test_real_config_passed_to_controller(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        controller_calls = [
            payload for name, payload in calls.calls if name == "create_run_controller"
        ]
        self.assertEqual(len(controller_calls), 1)
        self.assertIsNotNone(controller_calls[0]["config"])

    def test_thresholds_dict_passed_to_slo(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        slo_calls = [payload for name, payload in calls.calls if name == "evaluate_slos"]
        self.assertEqual(len(slo_calls), 1)
        self.assertEqual(slo_calls[0], {"warning_zone_ratio": 0.9})

    def test_exact_task9_writer_kwargs(self):
        self.write_state(controls=CONTROLS_DONE)
        deps, calls, _ = make_deps(self.root)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OK)
        writer_calls = [
            payload for name, payload in calls.calls if name == "write_run_artifacts"
        ]
        self.assertEqual(len(writer_calls), 1)
        kwargs = writer_calls[0]
        self.assertEqual(
            set(kwargs),
            {
                "artifact_root",
                "baseline_cycle_id",
                "run_payload",
                "summary_payload",
                "timeseries_rows",
                "workload_rows",
                "slo_result",
                "anomalies",
                "prometheus_queries",
                "source_files",
            },
        )
        payload = kwargs["run_payload"]
        for field in ("version", "baseline_cycle_id", "run_id", "manifest_id", "load_level", "run_number", "validity", "final_phase", "artifact_status", "created_utc"):
            self.assertIn(field, payload)
        summary = kwargs["summary_payload"]
        for field in ("version", "run_id", "manifest_id", "load_level", "verdict", "valid"):
            self.assertIn(field, summary)

    def test_writer_failure_leaves_no_run_entry(self):
        self.write_state(controls=CONTROLS_DONE)

        def bad_writer(**kwargs):
            raise RuntimeError("checksum failed")

        deps, calls, _ = make_deps(self.root, write_run_artifacts=bad_writer)
        code = run_main(self._argv(), deps)
        self.assertEqual(code, EXIT_OPERATIONAL)
        state = self.store().read(CYCLE)
        self.assertEqual(len(state.runs), 0)

    def test_full_stack_integration_no_signature_mismatch(self):
        """Real Task 2/3/5/6/7/9 adapters + fake process/harness adapters."""
        from tools.performance.a3.collectors import CollectorController, CollectorProcess
        from tools.performance.a3.config import load_config as real_load_config
        from tools.performance.a3.dataset import build_dataset_plan, emit_dataset_sql
        from tools.performance.a3.manifest import verify_manifest as real_verify
        from tools.performance.a3.reporting import write_run_artifacts as real_write
        from tools.performance.a3.slo import evaluate_valid_run_slos
        from tools.performance.a3.tests.test_collectors import FakeFactory
        from tools.performance.a3.tests.test_slo import thresholds as slo_thresholds
        from tools.performance.a3.tests.test_slo import valid_bundle
        from tools.performance.a3.validity import validate_run as real_validate

        fixture_dir = Path(__file__).resolve().parent / "fixtures"
        fixture_manifest = json.loads((fixture_dir / "valid_manifest.json").read_text(encoding="utf-8"))
        valid_run_data = json.loads((fixture_dir / "valid_run.json").read_text(encoding="utf-8"))

        # Prepare with real dataset emission to seed the cycle.
        deps, calls, outputs = make_deps(
            self.root,
            capture_manifest=lambda repo_root, config: fixture_manifest,
            build_dataset_plan=build_dataset_plan,
            emit_dataset_sql=emit_dataset_sql,
        )
        self.assertEqual(
            run_main(
                [
                    "prepare",
                    "--config",
                    "tools/performance/a3/config/a3.example.json",
                    "--artifact-root",
                    str(self.root),
                ],
                deps,
            ),
            EXIT_OK,
        )

        # Controls via fakes.
        runner = mock.Mock(return_value={"verdict": "PASS", "clients": 20})
        deps = dataclasses.replace(deps, run_control=runner)
        for name in ("idle", "webgl"):
            self.assertEqual(
                run_main(
                    ["control", name, "--cycle", CYCLE, "--artifact-root", str(self.root)],
                    deps,
                ),
                EXIT_OK,
            )

        # Run with real Task 5 controller (fake process factory), real
        # validity/SLO/artifact writer, fake harness returning contract data.
        raw_dir = self.root / "raw-integration"
        from tools.performance.a3.tests.test_reporting import build_source_files

        sources = build_source_files(raw_dir)
        timeseries_row = {
            "timestamp": 1000,
            "phase": "STEADY_STATE",
            "active_users": 500,
            "cpu_percent": 50.0,
            "memory_rss_bytes": 20_000_000_000,
            "tick_latency_ms": 5.0,
            "packet_processing_ms": 2.0,
            "sql_latency_ms": 10.0,
            "script_latency_ms": 2.0,
            "storage_utilization_percent": 45.0,
            "storage_await_ms": 2.0,
            "network_utilization_percent": 40.0,
        }
        workload_row = {
            "timestamp": 1000,
            "phase": "STEADY_STATE",
            "active_users": 500,
            "category": "combat",
            "event_count": 100,
            "error_count": 0,
        }

        def harness(request):
            return {
                "run_data": valid_run_data,
                "metric_bundle": valid_bundle(),
                "catastrophic": False,
                "metrics": {
                    "cpu_p95_percent": 60.0,
                    "memory_per_user_bytes": 1_000_000.0,
                    "latency_p95_ms": 10.0,
                    "latency_p99_ms": 20.0,
                    "throughput_per_second": 500.0,
                    "error_rate": 0.001,
                },
                "worst_metrics": {},
                "timeseries_rows": [timeseries_row],
                "workload_rows": [workload_row],
                "anomalies": [],
                "prometheus_queries": {"start": 1000, "end": 1010, "step": 5, "queries": []},
                "source_files": sources,
                "created_utc": "2026-08-02T20:00:00Z",
            }

        factory = FakeFactory()
        deps = dataclasses.replace(
            deps,
            load_config=real_load_config,
            verify_manifest=real_verify,
            create_collector_controller=lambda request: CollectorController(
                process_factory=factory
            ),
            run_harness=harness,
            evaluate_validity=real_validate,
            evaluate_slos=evaluate_valid_run_slos,
            load_slo_thresholds=slo_thresholds,
            write_run_artifacts=real_write,
        )
        code = run_main(
            ["run", "--cycle", CYCLE, "--users", "500", "--run", "1", "--artifact-root", str(self.root)],
            deps,
        )
        self.assertEqual(code, EXIT_OK)
        run_dir = (
            self.root / "artifacts" / "performance" / "a3" / CYCLE / "runs" / "run-l500-n1"
        )
        for name in ("run.json", "summary.json", "slo-verdict.json", "checksums.json"):
            self.assertTrue((run_dir / name).is_file(), name)
        state = self.store().read(CYCLE)
        self.assertEqual(len(state.runs), 1)
        self.assertTrue(state.runs[0]["valid"])
        self.assertEqual(state.runs[0]["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
